"""FastMCP bridge for the duet skill.

Exposes four tools used by the Claude-side orchestrator to drive a GPT-5.6
conversation that can itself request Claude Code slash commands. The bridge
implements the suspend-on-tool-call pattern: when GPT emits a tool_call, the
bridge persists session state and returns `{status: "tool_request"}` to the
caller. The caller (Claude orchestrator) executes the requested slash command
and resumes via duet_gpt_resume_turn.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from pydantic import BaseModel

# Allow `python server.py` to find sibling modules.
sys.path.insert(0, str(Path(__file__).parent))

from state import Session, SessionStore  # noqa: E402
from rubric import WorkProduct  # noqa: E402
from prompts.system_prompts import PROMPTS  # noqa: E402
from jsonutil import _json_obj  # noqa: E402  (tolerant final-WorkProduct parsing)
import duet_run as duet_run_mod  # noqa: E402  (aliased: the MCP tool below is named duet_run)

load_dotenv()

MODEL = os.environ.get("OPENAI_PARTNER_MODEL", "gpt-5.6")
STATE_DIR = os.environ.get("DUET_STATE_DIR", str(Path.home() / ".claude" / "duet"))
TRANSPORT = os.environ.get("DUET_TRANSPORT", "stdio")
PORT = int(os.environ.get("PORT", "8080"))
BEARER = os.environ.get("DUET_MCP_BEARER")

# Document-exchange limits.
DUET_MAX_DOC_CHARS = int(os.environ.get("DUET_MAX_DOC_CHARS", "100000"))  # per-document content cap
DUET_MAX_DOC_REQUESTS = int(os.environ.get("DUET_MAX_DOC_REQUESTS", "4"))  # request_document budget per turn

# Bounded-call knobs. The OpenAI loop runs synchronously inside the MCP tool handler, so
# the HTTP response blocks until GPT finishes. On a large pushed payload that generation
# can outrun the MCP CLIENT's ~180s tool-call cap (the cap is client-side, not here — Cloud
# Run allows 900s), and the call silently overruns instead of returning. These keep every
# call inside the window: a sub-cap request timeout, no retry storms, a bounded response,
# and a cumulative cap on pushed-document text. All are env-tunable with safe defaults.
DUET_OPENAI_TIMEOUT = float(os.environ.get("DUET_OPENAI_TIMEOUT", "150"))  # seconds, < 180s client cap
DUET_OPENAI_MAX_RETRIES = int(os.environ.get("DUET_OPENAI_MAX_RETRIES", "0"))  # SDK retries would multiply wall-clock
DUET_MAX_OUTPUT_TOKENS = int(os.environ.get("DUET_MAX_OUTPUT_TOKENS", "4000"))  # cap response generation
DUET_OUTPUT_TOKEN_PARAM = os.environ.get("DUET_OUTPUT_TOKEN_PARAM", "max_tokens")  # or "max_completion_tokens"
DUET_MAX_TOTAL_DOC_CHARS = int(os.environ.get("DUET_MAX_TOTAL_DOC_CHARS", "120000"))  # cumulative pushed-doc cap

# Below this candidate size the payload is "small" and gets no concise-critique nudge.
_CONCISE_NUDGE_CANDIDATE_CHARS = 8000


# Lazy-import openai so tests can monkeypatch.
def _openai_client():
    from openai import OpenAI
    return OpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        timeout=DUET_OPENAI_TIMEOUT,
        max_retries=DUET_OPENAI_MAX_RETRIES,
    )


def _is_timeout_error(exc: Exception) -> bool:
    """True if exc is an OpenAI timeout / connection error.

    Lets the loop return a clean, retriable signal inside the client window instead of
    letting the call overrun the ~180s cap. Matches by type when openai is importable,
    else falls back to the class name so a stubbed client (tests) can signal a timeout.
    """
    try:
        from openai import APITimeoutError, APIConnectionError
        if isinstance(exc, (APITimeoutError, APIConnectionError)):
            return True
    except Exception:  # pragma: no cover - openai always present in practice
        pass
    name = type(exc).__name__
    return "Timeout" in name or "APIConnectionError" in name

_STORE = SessionStore(STATE_DIR)


# The single tool we tell GPT it can call.
CLAUDE_SLASH_TOOL = {
    "type": "function",
    "function": {
        "name": "claude_slash_command",
        "description": (
            "Ask the Claude Code orchestrator to execute one of its slash commands "
            "(for example /austlii-legal-research) and return the result. "
            "Use this whenever a tool would give a more authoritative answer than "
            "your own recollection — especially for citations, legislation, or current facts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Slash command name, no leading slash."},
                "args": {"type": "string", "description": "Arguments to pass after the command name."},
            },
            "required": ["name", "args"],
            "additionalProperties": False,
        },
    },
}


# The second tool: GPT asks the orchestrator to send it the actual text of a document.
# The bridge has no filesystem/vault access — it only relays the request via the same
# suspend/resume channel as claude_slash_command; the Claude orchestrator on the surface
# (Claude Code / cowork / web) resolves it (e.g. from a co-work vault) and resumes.
REQUEST_DOCUMENT_TOOL = {
    "type": "function",
    "function": {
        "name": "request_document",
        "description": (
            "Request the actual full text of a document so you can ground your critique in "
            "the real source instead of guessing. The Claude orchestrator will fetch it "
            "(for example from the co-work vault, the project files, or an upload) and return "
            "its text. Use this whenever your advice depends on a document's real contents — "
            "especially for documents named in the spec or listed under AVAILABLE DOCUMENTS. "
            "You may request several documents in turn before giving your final answer. The "
            "result is a JSON object: {\"found\":true,\"name\":..,\"content\":..,\"truncated\":..} "
            "or {\"found\":false,\"reason\":..,\"available\":[..]} if it could not be located."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Document name/identifier/path to fetch (e.g. from the AVAILABLE DOCUMENTS catalog)."},
                "query": {"type": "string", "description": "What you are looking for in it / why you need it."},
                "source_hint": {"type": "string", "description": "Optional hint on where to look, e.g. 'cowork_vault', 'project_files', 'any'."},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
}


def _truncate_doc(content: str) -> tuple[str, bool]:
    """Cap a document body at DUET_MAX_DOC_CHARS; return (text, was_truncated)."""
    content = "" if content is None else str(content)
    if len(content) > DUET_MAX_DOC_CHARS:
        return content[:DUET_MAX_DOC_CHARS], True
    return content, False


def _render_document_block(doc: Dict[str, Any], budget: Optional[int] = None) -> tuple[str, int]:
    """Render one pushed document as a clearly delimited block GPT can quote/pin-cite.

    Applies the per-document cap (DUET_MAX_DOC_CHARS) and, when ``budget`` is given, the
    remaining cumulative cap (whichever is smaller). Returns (block, content_chars_used)
    so the caller can decrement the running cumulative budget.
    """
    name = doc.get("name") or doc.get("id") or "document"
    source = doc.get("source")
    content, truncated = _truncate_doc(doc.get("content", ""))
    truncated = truncated or bool(doc.get("truncated"))
    if budget is not None and len(content) > budget:
        content = content[:budget]
        truncated = True
    header = f"=== DOCUMENT: {name}"
    if source:
        header += f" [{source}]"
    if truncated:
        header += " (truncated)"
    header += " ==="
    return f"{header}\n{content}\n=== END DOCUMENT: {name} ===", len(content)


def _build_user_message(
    spec: str,
    candidate: Optional[str],
    history_note: str = "",
    documents: Optional[List[Dict[str, Any]]] = None,
    available_documents: Optional[List[Dict[str, Any]]] = None,
) -> str:
    parts = ["SPEC:", spec, ""]
    if candidate:
        parts += ["CURRENT CANDIDATE FROM OPUS:", candidate, ""]
    if documents:
        parts.append("ATTACHED DOCUMENTS (use their actual contents in your analysis):")
        # Cumulative cap across all pushed docs (the per-doc cap alone lets N big docs sum
        # into one over-long blocking call). Once spent, omit the rest with a clear marker
        # so GPT knows they exist and can pull them individually via request_document.
        remaining = DUET_MAX_TOTAL_DOC_CHARS
        omitted = 0
        for d in documents:
            if remaining <= 0:
                omitted += 1
                continue
            block, used = _render_document_block(d, budget=remaining)
            remaining -= used
            parts += [block, ""]
        if omitted:
            parts += [
                f"[{omitted} document(s) omitted to fit the size budget — request them "
                "individually via request_document]",
                "",
            ]
    if available_documents:
        parts += [
            "AVAILABLE DOCUMENTS — you do not have these yet, but you can fetch the full",
            "text of any of them by calling the request_document tool with its name:",
        ]
        for d in available_documents:
            name = d.get("name") or d.get("id") or "document"
            line = f"- {name}"
            if d.get("source"):
                line += f" [{d['source']}]"
            if d.get("description"):
                line += f": {d['description']}"
            parts.append(line)
        parts.append("")
    if history_note:
        parts += ["NOTE:", history_note, ""]
    # On a large payload, ask for a tight critique so the response stays inside the time
    # budget (the manual condense-and-ask-for-concise workaround, made automatic).
    large_payload = bool(documents) or (
        candidate is not None and len(candidate) > _CONCISE_NUDGE_CANDIDATE_CHARS)
    if large_payload:
        parts += [
            "NOTE — the attached material is large: keep your critique tight and focused. "
            "Pin-point the highest-severity issues with cites; do not restate or summarise "
            "the documents. Return only the JSON.",
            "",
        ]
    parts += [
        "Respond with a JSON object matching the WorkProduct schema:",
        '{"role":..., "candidate_id":..., "counter_draft":..., '
        '"score_of_candidate":{"value":..,"rationale":..}, '
        '"critique_items":[{"id":..,"severity":..,"finding":..,"suggested_fix":..,"addressed":false}], '
        '"notes":...}',
        "Do not wrap in markdown fences.",
    ]
    return "\n".join(parts)


def _run_openai_loop(session: Session) -> Dict[str, Any]:
    """Run the OpenAI chat loop until GPT emits a tool_call (suspend) or returns a final.

    GPT may call either tool — `claude_slash_command` or `request_document`. While the
    per-turn document-request budget remains, the tools are offered with `response_format`
    left UNCONSTRAINED, so GPT can make a *second* tool call after seeing the first result;
    that is what makes the document pull genuinely multi-step (forcing `json_object` on
    every call, as the old gating effectively did, suppresses the second tool call). Once
    the budget is exhausted we force closure with `tool_choice="none"` + `json_object` so
    GPT must emit a final WorkProduct. The final content is parsed tolerantly because an
    unconstrained response may arrive fenced or prose-wrapped.
    """
    client = _openai_client()
    while True:
        if session.doc_requests_made < DUET_MAX_DOC_REQUESTS:
            call_kwargs: Dict[str, Any] = {
                "tools": [CLAUDE_SLASH_TOOL, REQUEST_DOCUMENT_TOOL],
                "tool_choice": "auto",
                "parallel_tool_calls": False,  # serialise tool calls one at a time
            }
        else:
            # Document-request budget exhausted — force a final WorkProduct.
            call_kwargs = {
                "tools": [CLAUDE_SLASH_TOOL, REQUEST_DOCUMENT_TOOL],
                "tool_choice": "none",
                "response_format": {"type": "json_object"},
            }
        # Bound the response so generation can't run past the client window.
        call_kwargs[DUET_OUTPUT_TOKEN_PARAM] = DUET_MAX_OUTPUT_TOKENS
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=session.history,
                **call_kwargs,
            )
        except Exception as e:
            if not _is_timeout_error(e):
                raise
            # The call outran the time budget. Return a clean, retriable signal INSIDE the
            # client window instead of letting it overrun the ~180s cap. No pending tool was
            # set this iteration, so the session stays clean for a condensed retry.
            print(
                f"[duet-bridge] session={session.session_id}: GPT call timed out after "
                f"~{DUET_OPENAI_TIMEOUT:.0f}s ({type(e).__name__}); returning gpt_timeout.",
                file=sys.stderr,
            )
            return {
                "status": "error",
                "payload": {
                    "error": "gpt_timeout",
                    "retriable": True,
                    "elapsed_s": DUET_OPENAI_TIMEOUT,
                    "hint": (
                        "The payload was too large to critique within the time budget. Retry "
                        "with a tightly condensed candidate and an explicit request for a "
                        "concise critique, send fewer/smaller documents, or advertise them via "
                        "available_documents and let GPT pull them one at a time."
                    ),
                },
            }
        choice = resp.choices[0]
        msg = choice.message

        if msg.tool_calls:
            tc = msg.tool_calls[0]  # serialise tool calls one at a time
            # Persist ONLY the served tool call so history stays valid (a single tool
            # result answers it); any extra parallel calls are dropped and GPT can
            # re-request next round.
            if len(msg.tool_calls) > 1:
                dropped = [t.function.name for t in msg.tool_calls[1:]]
                print(
                    f"[duet-bridge] session={session.session_id}: serving "
                    f"{tc.function.name!r}, dropped {len(dropped)} parallel tool call(s) "
                    f"{dropped} (GPT may re-request next round)",
                    file=sys.stderr,
                )
            session.history.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [{
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }],
            })
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw": tc.function.arguments}
            if tc.function.name == "request_document":
                session.doc_requests_made += 1
            session.pending_tool_use_id = tc.id
            session.pending_tool_name = tc.function.name
            session.pending_tool_args = args
            _STORE.put(session)
            return {
                "status": "tool_request",
                "payload": {
                    "tool_name": tc.function.name,
                    "tool_args": args,
                    "tool_use_id": tc.id,
                },
            }

        # No tool call → final. Persist the assistant message and parse tolerantly.
        session.history.append({"role": "assistant", "content": msg.content or ""})
        wp_dict = _json_obj(msg.content or "")
        if not wp_dict:
            wp_dict = {"role": session.role, "candidate_id": "unknown",
                       "notes": msg.content or "", "critique_items": []}
        # Validate via pydantic (best-effort).
        try:
            wp = WorkProduct.model_validate(wp_dict)
            payload = wp.model_dump()
        except Exception as e:
            payload = {"_validation_error": str(e), "_raw": wp_dict}
        session.last_final = payload
        session.pending_tool_use_id = None
        session.pending_tool_name = None
        session.pending_tool_args = None
        _STORE.put(session)
        return {"status": "final", "payload": payload}


# ---------------------- MCP tool surface ----------------------

try:
    from fastmcp import FastMCP
except ImportError:  # pragma: no cover
    FastMCP = None  # type: ignore

mcp = FastMCP("duet-bridge") if FastMCP else None


def _start_turn_impl(
    session_id: Optional[str],
    role: str,
    spec: str,
    candidate: Optional[str],
    history_note: str,
    documents: Optional[List[Dict[str, Any]]] = None,
    available_documents: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    if role not in PROMPTS:
        return {"status": "error", "payload": {
            "error": f"unknown role: {role!r}",
            "valid_roles": sorted(PROMPTS),
        }}
    sid = session_id or f"sess-{uuid.uuid4().hex[:12]}"
    existing = _STORE.get(sid)
    if existing and not existing.closed:
        # Reuse — caller may want to continue. But typical pattern: start = fresh.
        session = existing
    else:
        session = Session(
            session_id=sid,
            role=role,
            spec=spec,
            documents=list(documents or []),
            available_documents=list(available_documents or []),
            history=[
                {"role": "system", "content": PROMPTS[role]},
                {"role": "user", "content": _build_user_message(
                    spec, candidate, history_note, documents, available_documents)},
            ],
        )
        _STORE.put(session)
    result = _run_openai_loop(session)
    result["session_id"] = sid
    return result


def _resume_turn_impl(
    session_id: str, tool_use_id: str, tool_result: str
) -> Dict[str, Any]:
    session = _STORE.get(session_id)
    if not session:
        return {"status": "error", "payload": {"error": f"unknown session: {session_id}"}}
    if session.pending_tool_use_id != tool_use_id:
        return {
            "status": "error",
            "payload": {
                "error": "tool_use_id mismatch",
                "expected": session.pending_tool_use_id,
                "got": tool_use_id,
            },
        }
    # For request_document, GPT's prompt expects a JSON object ({found, content, ...}).
    # If the orchestrator passed a non-JSON string, wrap it so GPT receives a well-formed
    # result rather than a raw blob. Slash-command results are passed through untouched.
    content = tool_result
    if session.pending_tool_name == "request_document":
        try:
            if not isinstance(json.loads(tool_result), dict):
                raise ValueError
        except (ValueError, TypeError):
            content = json.dumps({"found": True, "content": tool_result})
    session.history.append(
        {"role": "tool", "tool_call_id": tool_use_id, "content": content}
    )
    session.pending_tool_use_id = None
    session.pending_tool_name = None
    session.pending_tool_args = None
    _STORE.put(session)
    result = _run_openai_loop(session)
    result["session_id"] = session_id
    return result


def _close_session_impl(session_id: str) -> Dict[str, Any]:
    session = _STORE.get(session_id)
    if session:
        session.closed = True
        _STORE.put(session)
    return {"ok": True, "session_id": session_id}


def _health_impl() -> Dict[str, Any]:
    return {
        "ok": True,
        "model": MODEL,
        "transport": TRANSPORT,
        "state_dir": STATE_DIR,
        "opus_model": duet_run_mod.OPUS_MODEL,
        "duet_run_available": duet_run_mod.opus_available(),
    }


if mcp is not None:

    @mcp.tool()
    def duet_gpt_start_turn(
        role: str,
        spec: str,
        candidate: Optional[str] = None,
        history_note: str = "",
        session_id: Optional[str] = None,
        documents: Optional[List[Dict[str, Any]]] = None,
        available_documents: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Start a GPT turn. Returns either {status:'tool_request', ...} or {status:'final', ...}.

        Document exchange (optional):
          * documents: list of {name, content, mime?, source?} whose FULL TEXT is sent to
            GPT in this turn — so its critique is grounded in the real source, not a
            paraphrase. Per-document content is capped at DUET_MAX_DOC_CHARS.
          * available_documents: list of {name, description?, source?} advertised as a
            catalog GPT can pull from on demand by calling the request_document tool. GPT's
            pull comes back as status:'tool_request' with tool_name='request_document'; the
            orchestrator resolves it (e.g. from a co-work vault) and replies via
            duet_gpt_resume_turn. GPT may pull several documents in succession before its
            final answer (a two-way, multi-step exchange).
        """
        return _start_turn_impl(
            session_id, role, spec, candidate, history_note, documents, available_documents)

    @mcp.tool()
    def duet_gpt_resume_turn(
        session_id: str, tool_use_id: str, tool_result: str
    ) -> Dict[str, Any]:
        """Resume a suspended GPT turn with the result of a slash-command execution."""
        return _resume_turn_impl(session_id, tool_use_id, tool_result)

    @mcp.tool()
    def duet_gpt_close_session(session_id: str) -> Dict[str, Any]:
        """Mark a session closed (frees from in-memory cache; on-disk record kept for audit)."""
        return _close_session_impl(session_id)

    @mcp.tool()
    def duet_health() -> Dict[str, Any]:
        """Health probe — returns model + transport configuration."""
        return _health_impl()

    @mcp.tool()
    def duet_run(
        spec: str,
        threshold: Optional[int] = None,
        iteration_cap: Optional[int] = None,
        documents: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Run the FULL two-model consensus loop server-side and return the final artifact.

        One call does the whole thing: Claude Opus 4.8 drafts, GPT-5.6 critiques and
        scores, Opus revises, repeating until both models accept the same candidate
        (rubric.acceptance_check) or the iteration cap is hit, then an independent
        verifier (fresh context) signs off. Designed so any surface that can reach this
        connector (web/desktop/mobile chat, cowork) gets duet without a local
        orchestrator.

        Args:
            spec: The task / deliverable specification (required).
            threshold: Acceptance score 0-100 (default 95).
            iteration_cap: Max critique/revise rounds (default 8).
            documents: Optional list of {name, content, source?} whose full text is given
                to both models so the work is grounded in the real source. Server-side
                duet_run is PUSH-ONLY for documents — there is no orchestrator here, so GPT
                cannot pull additional documents (use the duet_gpt_start_turn / resume
                bridge path for the interactive, multi-step pull, e.g. from a co-work vault).

        Returns a dict with accepted, gate, final_candidate, scores, verifier,
        suggested_improvements, and a step transcript. Requires ANTHROPIC_API_KEY to be
        configured on the bridge for the Opus role; returns status:'error' otherwise.
        """
        return duet_run_mod.run_duet(
            spec, threshold=threshold, iteration_cap=iteration_cap, documents=documents)


def _build_http_app():
    """Wrap FastMCP's http_app with MCP OAuth 2.1 endpoints + bearer middleware.

    OAuth design (single-tenant, owner-gated):
      * Static `DUET_MCP_BEARER` doubles as the issued access token. Any client
        that completes the auth code + PKCE flow receives that token, so the
        existing bearer check works unchanged for /mcp traffic.
      * Consent is gated by the bearer itself — only someone who knows the
        deployer's bearer can authorize a new client. Prevents drive-by DCR
        from getting access.
      * Auth codes live in-memory with 5-minute TTL. Cloud Run cold starts can
        drop them; user just clicks Connect again.
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse, HTMLResponse, RedirectResponse, Response
    from starlette.routing import Route
    import base64, hashlib, secrets, time

    PUBLIC_PREFIXES = ("/.well-known/", "/register", "/authorize", "/token")

    # code -> {"client_id","redirect_uri","code_challenge","code_challenge_method","exp"}
    _codes: Dict[str, Dict[str, Any]] = {}
    CODE_TTL = 300

    def _issuer(request) -> str:
        scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
        host = request.headers.get("x-forwarded-host") or request.url.netloc
        return f"{scheme}://{host}"

    def _purge():
        now = time.time()
        for k in [k for k, v in _codes.items() if v["exp"] < now]:
            _codes.pop(k, None)

    async def well_known_protected_resource(request):
        iss = _issuer(request)
        return JSONResponse({
            "resource": f"{iss}/mcp",
            "authorization_servers": [iss],
            "bearer_methods_supported": ["header"],
        })

    async def well_known_auth_server(request):
        iss = _issuer(request)
        return JSONResponse({
            "issuer": iss,
            "authorization_endpoint": f"{iss}/authorize",
            "token_endpoint": f"{iss}/token",
            "registration_endpoint": f"{iss}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": ["mcp"],
        })

    async def register(request):
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        client_id = "duet-" + secrets.token_urlsafe(12)
        return JSONResponse({
            "client_id": client_id,
            "client_id_issued_at": int(time.time()),
            "redirect_uris": body.get("redirect_uris", []),
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
        }, status_code=201)

    CONSENT_HTML = """<!doctype html><html><head><title>duet-bridge authorization</title>
<style>body{font-family:system-ui;max-width:480px;margin:4em auto;padding:0 1em;color:#222}
h1{font-size:1.3em}input{width:100%;padding:.6em;font-size:1em;box-sizing:border-box;margin:.4em 0 1em}
button{padding:.7em 1.4em;font-size:1em;cursor:pointer}.client{font-family:monospace;background:#f4f4f4;padding:.4em .6em;border-radius:4px}</style>
</head><body><h1>Authorize MCP client</h1>
<p>Client <span class="client">__CLIENT__</span> is requesting access to your duet-bridge.</p>
<p>Paste your <code>DUET_MCP_BEARER</code> to confirm.</p>
<form method="post"><input type="password" name="bearer" autofocus required>
<input type="hidden" name="state" value="__STATE__"><button type="submit">Authorize</button></form>
__ERROR__</body></html>"""

    async def authorize(request):
        _purge()
        if request.method == "GET":
            qp = request.query_params
            need = ["client_id", "redirect_uri", "response_type", "code_challenge", "code_challenge_method"]
            missing = [k for k in need if not qp.get(k)]
            if missing:
                return JSONResponse({"error": "invalid_request", "missing": missing}, status_code=400)
            if qp["response_type"] != "code":
                return JSONResponse({"error": "unsupported_response_type"}, status_code=400)
            if qp["code_challenge_method"] != "S256":
                return JSONResponse({"error": "invalid_request", "detail": "S256 required"}, status_code=400)
            state_blob = base64.urlsafe_b64encode(json.dumps({
                "client_id": qp["client_id"],
                "redirect_uri": qp["redirect_uri"],
                "code_challenge": qp["code_challenge"],
                "state": qp.get("state", ""),
            }).encode()).decode()
            return HTMLResponse(CONSENT_HTML.replace("__CLIENT__", qp["client_id"]).replace("__STATE__", state_blob).replace("__ERROR__", ""))
        # POST
        form = await request.form()
        if form.get("bearer") != BEARER:
            try:
                ctx = json.loads(base64.urlsafe_b64decode(form.get("state", "").encode()).decode())
            except Exception:
                ctx = {"client_id": "?", "state": ""}
            err = '<p style="color:#b00">Incorrect bearer. Try again.</p>'
            return HTMLResponse(CONSENT_HTML.replace("__CLIENT__", ctx.get("client_id", "?")).replace("__STATE__", form.get("state", "")).replace("__ERROR__", err), status_code=401)
        try:
            ctx = json.loads(base64.urlsafe_b64decode(form.get("state", "").encode()).decode())
        except Exception:
            return JSONResponse({"error": "invalid_state"}, status_code=400)
        code = secrets.token_urlsafe(24)
        _codes[code] = {
            "client_id": ctx["client_id"],
            "redirect_uri": ctx["redirect_uri"],
            "code_challenge": ctx["code_challenge"],
            "exp": time.time() + CODE_TTL,
        }
        sep = "&" if "?" in ctx["redirect_uri"] else "?"
        target = f"{ctx['redirect_uri']}{sep}code={code}"
        if ctx.get("state"):
            target += f"&state={ctx['state']}"
        return RedirectResponse(target, status_code=302)

    async def token(request):
        _purge()
        form = await request.form()
        if form.get("grant_type") != "authorization_code":
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
        code = form.get("code", "")
        rec = _codes.pop(code, None)
        if not rec or rec["exp"] < time.time():
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if form.get("client_id") != rec["client_id"] or form.get("redirect_uri") != rec["redirect_uri"]:
            return JSONResponse({"error": "invalid_grant", "detail": "client/redirect mismatch"}, status_code=400)
        verifier = form.get("code_verifier", "")
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if expected != rec["code_challenge"]:
            return JSONResponse({"error": "invalid_grant", "detail": "PKCE verifier mismatch"}, status_code=400)
        return JSONResponse({
            "access_token": BEARER,
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "mcp",
        })

    class BearerAuth(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            path = request.url.path
            if any(path == p or path.startswith(p + "/") or path.startswith(p) for p in PUBLIC_PREFIXES):
                return await call_next(request)
            if request.headers.get("authorization", "") != f"Bearer {BEARER}":
                # Advertise the protected-resource metadata per RFC 9728 so OAuth
                # clients can discover the auth server from a 401.
                iss = _issuer(request)
                return JSONResponse(
                    {"error": "unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": f'Bearer resource_metadata="{iss}/.well-known/oauth-protected-resource"'},
                )
            return await call_next(request)

    app = mcp.http_app()
    # Register OAuth routes on the underlying Starlette app.
    app.router.routes.insert(0, Route("/.well-known/oauth-protected-resource", well_known_protected_resource, methods=["GET"]))
    app.router.routes.insert(0, Route("/.well-known/oauth-protected-resource/mcp", well_known_protected_resource, methods=["GET"]))
    app.router.routes.insert(0, Route("/.well-known/oauth-authorization-server", well_known_auth_server, methods=["GET"]))
    app.router.routes.insert(0, Route("/register", register, methods=["POST"]))
    app.router.routes.insert(0, Route("/authorize", authorize, methods=["GET", "POST"]))
    app.router.routes.insert(0, Route("/token", token, methods=["POST"]))
    app.add_middleware(BearerAuth)
    return app


if __name__ == "__main__":
    if mcp is None:
        print("fastmcp not installed; cannot start server.", file=sys.stderr)
        sys.exit(2)
    if TRANSPORT == "http":
        if not BEARER:
            print("DUET_MCP_BEARER not set; refusing to start http transport.", file=sys.stderr)
            sys.exit(2)
        import uvicorn
        uvicorn.run(_build_http_app(), host="0.0.0.0", port=PORT)
    else:
        mcp.run()  # default stdio
