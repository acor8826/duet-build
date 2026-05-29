"""FastMCP bridge for the duet skill.

Exposes four tools used by the Claude-side orchestrator to drive a GPT-5.5
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

load_dotenv()

# Lazy-import openai so tests can monkeypatch.
def _openai_client():
    from openai import OpenAI
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"])


MODEL = os.environ.get("OPENAI_PARTNER_MODEL", "gpt-5.5")
STATE_DIR = os.environ.get("DUET_STATE_DIR", str(Path.home() / ".claude" / "duet"))
TRANSPORT = os.environ.get("DUET_TRANSPORT", "stdio")
PORT = int(os.environ.get("PORT", "8080"))
BEARER = os.environ.get("DUET_MCP_BEARER")

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


def _build_user_message(spec: str, candidate: Optional[str], history_note: str = "") -> str:
    parts = ["SPEC:", spec, ""]
    if candidate:
        parts += ["CURRENT CANDIDATE FROM OPUS:", candidate, ""]
    if history_note:
        parts += ["NOTE:", history_note, ""]
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
    """Run the OpenAI chat loop until either GPT emits a tool_call (suspend) or returns a final."""
    client = _openai_client()
    while True:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=session.history,
            tools=[CLAUDE_SLASH_TOOL],
            tool_choice="auto",
            response_format={"type": "json_object"} if not _has_open_tool_call(session) else None,
        )
        choice = resp.choices[0]
        msg = choice.message

        # Persist the assistant message in history.
        asst_entry: Dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            asst_entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ]
        session.history.append(asst_entry)

        if msg.tool_calls:
            tc = msg.tool_calls[0]  # serialise tool calls one at a time
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw": tc.function.arguments}
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

        # No tool call → final.
        try:
            wp_dict = json.loads(msg.content or "{}")
        except json.JSONDecodeError:
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


def _has_open_tool_call(session: Session) -> bool:
    if not session.history:
        return False
    last = session.history[-1]
    return bool(last.get("tool_calls"))


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
        session = Session(session_id=sid, role=role, spec=spec, history=[
            {"role": "system", "content": PROMPTS[role]},
            {"role": "user", "content": _build_user_message(spec, candidate, history_note)},
        ])
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
    session.history.append(
        {"role": "tool", "tool_call_id": tool_use_id, "content": tool_result}
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
    return {"ok": True, "model": MODEL, "transport": TRANSPORT, "state_dir": STATE_DIR}


if mcp is not None:

    @mcp.tool()
    def duet_gpt_start_turn(
        role: str,
        spec: str,
        candidate: Optional[str] = None,
        history_note: str = "",
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Start a GPT turn. Returns either {status:'tool_request', ...} or {status:'final', ...}."""
        return _start_turn_impl(session_id, role, spec, candidate, history_note)

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
