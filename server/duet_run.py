"""Server-side full-loop duet orchestration (the `duet_run` tool).

Runs the entire draft -> critique -> score -> converge -> verify consensus loop
*inside the bridge*, so any surface that can reach the connector (claude.ai web /
desktop / mobile chat, cowork, Claude Code) gets one-call duet without needing a
local Claude Code orchestrator to drive `duet_gpt_start_turn` / resume by hand.

Role assignment:
  * Opus side  -> Anthropic API (ANTHROPIC_API_KEY). Lead author: drafts and
    iteratively revises the candidate, self-scoring each turn.
  * GPT side   -> OpenAI API (existing OPENAI_API_KEY path). Critic: scores the
    Opus candidate and raises critique items.
  * Verifier   -> a fresh Anthropic call with no loop history (design intent:
    the verifier must not be anchored by the back-and-forth).

Acceptance is decided by `rubric.acceptance_check` (the same two-gate rule the
Claude Code skill uses), keeping server-side and skill behaviour in sync.

DELIBERATE LIMITATION: the GPT->local-slash-command round-trip (rule #3 of the
Claude Code skill, e.g. GPT asking for /austlii-legal-research) is NOT available
here. There is no local orchestrator server-side to execute those commands, so
the GPT critic runs without the `claude_slash_command` tool and is told so.

Documents follow the same boundary: this path is PUSH-ONLY. A caller may pass
`documents` whose full text is given to both models (so the work is grounded in the
real source), but GPT cannot PULL additional documents here — that interactive,
multi-step request (e.g. fetching a file from a co-work vault) needs the bridge
start/resume path (`duet_gpt_start_turn` with `request_document`).
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from rubric import acceptance_check  # reuse the canonical two-gate acceptance rule
from jsonutil import _json_obj  # tolerant JSON extraction (shared with server.py)

OPUS_MODEL = os.environ.get("DUET_OPUS_MODEL", "claude-opus-4-8")
GPT_MODEL = os.environ.get("OPENAI_PARTNER_MODEL", "gpt-5.5")
DEFAULT_THRESHOLD = int(os.environ.get("DUET_CONFIDENCE_THRESHOLD", "95"))
DEFAULT_CAP = int(os.environ.get("DUET_ITERATION_CAP", "8"))
MAX_DOC_CHARS = int(os.environ.get("DUET_MAX_DOC_CHARS", "100000"))  # per-document content cap (push)

# Bounded-call knobs (shared names with server.py). run_duet runs the whole loop inside one
# synchronous tool call, so an unbounded model call can outrun the client's ~180s cap. These
# bound each individual model call; the full-loop duration is still inherently long (this is
# the headless fallback path), but a single hung call now fails fast and retriably.
OPENAI_TIMEOUT = float(os.environ.get("DUET_OPENAI_TIMEOUT", "150"))  # per-call request timeout (s)
OPENAI_MAX_RETRIES = int(os.environ.get("DUET_OPENAI_MAX_RETRIES", "0"))
MAX_OUTPUT_TOKENS = int(os.environ.get("DUET_MAX_OUTPUT_TOKENS", "4000"))
OUTPUT_TOKEN_PARAM = os.environ.get("DUET_OUTPUT_TOKEN_PARAM", "max_tokens")
GPT_REASONING_EFFORT = os.environ.get("DUET_GPT_REASONING_EFFORT", "").strip()  # "" => model default


class DuetTimeout(Exception):
    """A single model call outran the time budget — surfaced as a retriable error."""


def _is_timeout_error(exc: Exception) -> bool:
    """True for OpenAI/Anthropic timeout / connection errors (match by type, else by name)."""
    try:
        from openai import APITimeoutError, APIConnectionError
        if isinstance(exc, (APITimeoutError, APIConnectionError)):
            return True
    except Exception:  # pragma: no cover
        pass
    try:
        import anthropic
        if isinstance(exc, (anthropic.APITimeoutError, anthropic.APIConnectionError)):
            return True
    except Exception:  # pragma: no cover
        pass
    name = type(exc).__name__
    return "Timeout" in name or "APIConnectionError" in name


def opus_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


# ---------------------- helpers ----------------------

def _as_int(v: Any) -> int:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return 0


def _render_documents(documents: Optional[List[Dict[str, Any]]]) -> str:
    """Render pushed documents as delimited blocks for the prompts.

    Server-side duet_run is PUSH-ONLY: documents are supplied by the caller and given to
    both models so the work is grounded in the real source. There is no orchestrator here,
    so GPT cannot pull additional documents (that is the bridge start/resume path's job).
    """
    if not documents:
        return ""
    blocks = ["ATTACHED DOCUMENTS (use their actual contents — do not paraphrase from memory):"]
    for d in documents:
        name = d.get("name") or d.get("id") or "document"
        source = d.get("source")
        content = "" if d.get("content") is None else str(d.get("content"))
        truncated = bool(d.get("truncated"))
        if len(content) > MAX_DOC_CHARS:
            content = content[:MAX_DOC_CHARS]
            truncated = True
        header = f"=== DOCUMENT: {name}"
        if source:
            header += f" [{source}]"
        if truncated:
            header += " (truncated)"
        header += " ==="
        blocks.append(f"{header}\n{content}\n=== END DOCUMENT: {name} ===")
    return "\n".join(blocks) + "\n\n"


# ---------------------- model calls ----------------------

def _opus_call(system: str, user: str, max_tokens: int = 4096) -> str:
    import anthropic
    client = anthropic.Anthropic(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        timeout=OPENAI_TIMEOUT,
        max_retries=OPENAI_MAX_RETRIES,
    )
    try:
        msg = client.messages.create(
            model=OPUS_MODEL,
            max_tokens=max_tokens,
            # cache the (reused) system prompt across the loop's Opus turns
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
    except Exception as e:
        if _is_timeout_error(e):
            raise DuetTimeout(f"Opus call timed out after ~{OPENAI_TIMEOUT:.0f}s") from e
        raise
    return "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", None) == "text")


def _gpt_call(system: str, user: str) -> str:
    from openai import OpenAI
    client = OpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        timeout=OPENAI_TIMEOUT,
        max_retries=OPENAI_MAX_RETRIES,
    )
    extra = {OUTPUT_TOKEN_PARAM: MAX_OUTPUT_TOKENS}
    if GPT_REASONING_EFFORT:
        extra["reasoning_effort"] = GPT_REASONING_EFFORT
    try:
        resp = client.chat.completions.create(
            model=GPT_MODEL,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            **extra,
        )
    except Exception as e:
        if _is_timeout_error(e):
            raise DuetTimeout(f"GPT call timed out after ~{OPENAI_TIMEOUT:.0f}s") from e
        raise
    return resp.choices[0].message.content or "{}"


# ---------------------- prompts ----------------------

_OPUS_SYSTEM = (
    "You are Claude Opus 4.8, the lead author in a two-model consensus system called "
    "\"duet\", collaborating with OpenAI GPT-5.5. You draft and iteratively improve a "
    "deliverable until both models independently score the same candidate at or above "
    "{threshold}/100 against the rubric (accuracy, completeness, clarity, rigour, "
    "fitness-for-purpose). Be rigorous and genuinely self-critical — do not inflate your "
    "self-score. Return ONLY a single JSON object. No markdown fences, no prose outside JSON."
)

_VERIFIER_SYSTEM = (
    "You are an INDEPENDENT VERIFIER. You see only the SPEC and the final candidate, with "
    "no prior conversation, so your read is fresh and unanchored. Score the candidate 0-100 "
    "against the rubric and decide PASS (score >= {threshold} AND no blocking defects) or "
    "FAIL. Be skeptical: verify any factual/citation claims you can reason about. Return ONLY "
    "a single JSON object, no markdown fences."
)


def _draft_user(spec: str, docs_block: str = "", project_block: str = "") -> str:
    return (
        f"{project_block}"
        f"SPEC:\n{spec}\n\n"
        f"{docs_block}"
        "Produce the best possible first draft. Return JSON exactly:\n"
        '{"candidate_id":"cand-1","candidate_text":"<the full artifact>",'
        '"self_score":{"value":<0-100 int>,"rationale":"<one paragraph keyed to the rubric>"}}'
    )


def _revise_user(spec: str, candidate: str, cand_id: str, gpt_score: int,
                 crit: List[Dict[str, Any]], next_n: int, docs_block: str = "",
                 project_block: str = "") -> str:
    lines = []
    for c in crit:
        sev = c.get("severity", "?")
        lines.append(f"- [{c.get('id','?')}|{sev}] {c.get('finding','')} -> {c.get('suggested_fix','')}")
    items = "\n".join(lines) if lines else "(no specific items returned)"
    return (
        f"{project_block}"
        f"SPEC:\n{spec}\n\n"
        f"{docs_block}"
        f"CURRENT CANDIDATE ({cand_id}):\n{candidate}\n\n"
        f"GPT-5.5 scored this candidate {gpt_score}/100 and raised these critique items:\n{items}\n\n"
        "Revise the artifact to resolve EVERY blocker/major/moderate item (minor/nit are optional). "
        "Do not regress on points already correct. Return JSON exactly:\n"
        f'{{"candidate_id":"cand-{next_n}","candidate_text":"<the full revised artifact>",'
        '"self_score":{"value":<0-100 int>,"rationale":"<one paragraph keyed to the rubric>"},'
        '"addressed_ids":["<ids of items you resolved>"]}'
    )


def _critic_user(spec: str, candidate: str, docs_block: str = "", project_block: str = "") -> str:
    return (
        f"{project_block}"
        f"SPEC:\n{spec}\n\n"
        f"{docs_block}"
        f"CURRENT CANDIDATE FROM OPUS:\n{candidate}\n\n"
        "NOTE: External tools / slash-command requests are NOT available in this mode, and "
        "you cannot pull additional documents here; critique using the spec, the candidate, "
        "and any ATTACHED DOCUMENTS above.\n\n"
        "Respond with a JSON object matching the WorkProduct schema:\n"
        '{"role":"critic","candidate_id":"<id>","counter_draft":null,'
        '"score_of_candidate":{"value":<0-100 int>,"rationale":"<keyed to rubric>"},'
        '"critique_items":[{"id":"c1","severity":"blocker|major|moderate|minor|nit",'
        '"finding":"...","suggested_fix":"...","addressed":false}],"notes":"..."}\n'
        "Do not wrap in markdown fences."
    )


def _verify_user(spec: str, candidate: str, docs_block: str = "", project_block: str = "") -> str:
    return (
        f"{project_block}"
        f"SPEC:\n{spec}\n\n"
        f"{docs_block}"
        f"FINAL CANDIDATE:\n{candidate}\n\n"
        "Return JSON exactly:\n"
        '{"score":{"value":<0-100 int>,"rationale":"<why>"},'
        '"verdict":"PASS|FAIL","blocking_findings":["<any blocking defects>"]}'
    )


# ---------------------- main loop ----------------------

def run_duet(spec: str, threshold: Optional[int] = None,
             iteration_cap: Optional[int] = None,
             documents: Optional[List[Dict[str, Any]]] = None,
             project_name: str = "") -> Dict[str, Any]:
    """Run the full server-side consensus loop, failing fast on a model timeout.

    Thin wrapper over `_run_duet_inner`: if any single Opus/GPT call outruns the time
    budget (`DuetTimeout`), return a clean, retriable error instead of hanging past the
    client's ~180s tool-call cap.
    """
    try:
        return _run_duet_inner(spec, threshold=threshold,
                               iteration_cap=iteration_cap, documents=documents,
                               project_name=project_name)
    except DuetTimeout as e:
        return {
            "status": "error",
            "error": f"gpt_timeout: {e}",
            "retriable": True,
            "hint": (
                "A model call exceeded the time budget. Retry with a shorter spec / fewer "
                "or smaller documents, or drive the loop client-side via "
                "duet_gpt_start_turn / duet_gpt_resume_turn for finer-grained calls."
            ),
        }


def _run_duet_inner(spec: str, threshold: Optional[int] = None,
                    iteration_cap: Optional[int] = None,
                    documents: Optional[List[Dict[str, Any]]] = None,
                    project_name: str = "") -> Dict[str, Any]:
    """Run the full server-side consensus loop and return the final artifact.

    documents (optional, PUSH-ONLY): list of {name, content, source?} whose full text is
    given to both models so the work is grounded in the real source. GPT cannot pull more
    documents here (no orchestrator); use the duet_gpt_start_turn / resume bridge path for
    the interactive, multi-step document pull (e.g. from a co-work vault).

    project_name (optional): matter name, prepended to every Opus/GPT prompt. NOTE: this
    headless path has NO live Drive — there is no Responses-API Drive connector here, so case
    documents are only visible if PUSHed via ``documents``. The interactive bridge path
    (duet_gpt_start_turn) is the one that gives GPT live Drive access.
    """
    if not spec or not spec.strip():
        return {"status": "error", "error": "spec is required and must be non-empty."}
    if not opus_available():
        return {
            "status": "error",
            "error": (
                "ANTHROPIC_API_KEY is not configured on the bridge, so the server-side "
                "Opus role is unavailable. Either set that secret on the Cloud Run service, "
                "or drive the loop client-side with duet_gpt_start_turn / duet_gpt_resume_turn."
            ),
        }

    threshold = int(threshold) if threshold else DEFAULT_THRESHOLD
    cap = int(iteration_cap) if iteration_cap else DEFAULT_CAP
    opus_sys = _OPUS_SYSTEM.format(threshold=threshold)
    docs_block = _render_documents(documents)
    project_block = f"PROJECT / MATTER:\n{project_name}\n\n" if project_name else ""

    transcript: List[Dict[str, Any]] = []
    opus_scores: List[int] = []
    gpt_scores: List[int] = []

    # 1. Initial Opus draft.
    draft = _json_obj(_opus_call(opus_sys, _draft_user(spec, docs_block, project_block)))
    candidate = draft.get("candidate_text", "") or ""
    cand_id = draft.get("candidate_id", "cand-1")
    if not candidate:
        return {"status": "error", "error": "Opus did not return a usable draft.",
                "raw": draft, "transcript": transcript}
    opus_scores.append(_as_int((draft.get("self_score") or {}).get("value")))
    transcript.append({"step": "opus_draft", "candidate_id": cand_id,
                       "self_score": opus_scores[-1]})

    ac: Dict[str, Any] = {"accepted": False, "gate": "", "ranked_suggestions": [],
                          "reason": "no iterations run"}
    last_crit: List[Dict[str, Any]] = []

    # 2. Critique / revise loop.
    for n in range(1, cap + 1):
        gp = _json_obj(_gpt_call(_PROMPTS_critic(), _critic_user(spec, candidate, docs_block, project_block)))
        gpt_scores.append(_as_int((gp.get("score_of_candidate") or {}).get("value")))
        last_crit = gp.get("critique_items", []) or []
        transcript.append({"step": "gpt_critique", "iter": n, "gpt_score": gpt_scores[-1],
                           "num_items": len(last_crit)})

        ac = acceptance_check(opus_scores, gpt_scores, last_crit, threshold=threshold)
        if ac["accepted"]:
            break
        if n >= cap:
            break  # don't revise past the last evaluated candidate (keeps candidate==scored)

        rev = _json_obj(_opus_call(opus_sys, _revise_user(
            spec, candidate, cand_id, gpt_scores[-1], last_crit, n + 1, docs_block, project_block)))
        candidate = rev.get("candidate_text", candidate) or candidate
        cand_id = rev.get("candidate_id", f"cand-{n + 1}")
        opus_scores.append(_as_int((rev.get("self_score") or {}).get("value")))
        transcript.append({"step": "opus_revise", "iter": n, "candidate_id": cand_id,
                           "self_score": opus_scores[-1]})

    accepted = bool(ac.get("accepted"))
    gate = ac.get("gate", "")
    suggestions = ac.get("ranked_suggestions", []) or []

    # 3. Independent verifier (fresh context) — always run on the final candidate.
    ver = _json_obj(_opus_call(_VERIFIER_SYSTEM.format(threshold=threshold),
                               _verify_user(spec, candidate, docs_block, project_block)))
    ver_value = _as_int((ver.get("score") or {}).get("value"))
    ver_verdict = str(ver.get("verdict", "")).upper()
    ver_pass = ver_verdict == "PASS" and ver_value >= threshold
    transcript.append({"step": "verify", "score": ver_value, "verdict": ver_verdict or "?"})

    def _norm(c: Any) -> Dict[str, Any]:
        g = (lambda k, d="": c.get(k, d) if isinstance(c, dict) else getattr(c, k, d))
        return {"id": g("id"), "severity": g("severity"), "finding": g("finding"),
                "suggested_fix": g("suggested_fix")}

    return {
        "status": "ok",
        "accepted": accepted and ver_pass,
        "consensus_accepted": accepted,
        "verifier_passed": ver_pass,
        "escalated": not accepted,
        "gate": gate,
        "iterations": len(gpt_scores),
        "iteration_cap": cap,
        "threshold": threshold,
        "opus_scores": opus_scores,
        "gpt_scores": gpt_scores,
        "verifier": {"value": ver_value, "verdict": ver_verdict or "?",
                     "rationale": (ver.get("score") or {}).get("rationale", ""),
                     "blocking_findings": ver.get("blocking_findings", [])},
        "candidate_id": cand_id,
        "final_candidate": candidate,
        "suggested_improvements": [_norm(c) for c in suggestions],
        "reason": ac.get("reason", ""),
        "models": {"opus": OPUS_MODEL, "gpt": GPT_MODEL},
        "documents_attached": len(documents or []),
        "limitations": [
            "GPT->local-slash-command round-trip (rule #3) is unavailable in server-side mode.",
            "Documents are PUSH-ONLY here: GPT cannot pull additional documents (no "
            "orchestrator). Use the duet_gpt_start_turn / resume bridge path for the "
            "interactive, multi-step document pull (e.g. from a co-work vault).",
        ],
        "transcript": transcript,
    }


def _PROMPTS_critic() -> str:
    """Lazy fetch of the GPT critic system prompt (avoids import cycle at module load)."""
    from prompts.system_prompts import PROMPTS
    return PROMPTS["critic"]
