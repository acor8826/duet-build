"""Drive one live duet round using the actual FastMCP bridge functions
against GPT-5.5. Handles the suspend-on-tool-call coroutine pattern by
forwarding /austlii-legal-research requests to a curl-based AustLII fetcher.

Used to validate the cross-vendor consensus link in build mode (the MCP
server itself doesn't need to be running; we just call the internal
_start_turn_impl / _resume_turn_impl functions directly).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import server as srv  # noqa: E402

SPEC = (
    "Draft a one-paragraph statement on the principle of legality in "
    "Australian statutory interpretation, with a verified AustLII citation."
)

# Candidate that converged in the degraded-mode run (revision 3).
CANDIDATE = (
    'The principle of legality is a presumption of statutory construction: '
    'courts will not impute to Parliament an intention to abrogate or '
    'curtail fundamental common-law rights, freedoms or immunities except '
    'by clear words or necessary implication. In Coco v The Queen [1994] '
    'HCA 15; (1994) 179 CLR 427 at 437, Mason CJ, Brennan, Gaudron and '
    'McHugh JJ held at [10] that "[t]he courts should not impute to the '
    'legislature an intention to interfere with fundamental rights. Such '
    'an intention must be clearly manifested by unmistakable and '
    'unambiguous language", their Honours later (at [12]) grounding the '
    'rationale (via Bropho v Western Australia [1990] HCA 24; (1990) 171 '
    'CLR 1 at 18) in the long-standing assumption stated in Potter v '
    'Minahan [1908] HCA 63; (1908) 7 CLR 277 at 304 that it is "in the '
    'last degree improbable that the legislature would overthrow '
    'fundamental principles ... without expressing its intention with '
    'irresistible clearness".'
)


def execute_austlii(args_str: str) -> str:
    """Approximate /austlii-legal-research by curl-fetching the case URL.

    Accepts either an explicit URL or a citation like '[1994] HCA 15'.
    Returns a text excerpt around the principle-of-legality passage.
    """
    args = args_str.strip()
    if args.startswith("http"):
        url = args
    elif "1994" in args and "HCA" in args.upper() and "15" in args:
        url = "https://www.austlii.edu.au/cgi-bin/viewdoc/au/cases/cth/HCA/1994/15.html"
    elif "1990" in args and "HCA" in args.upper() and "24" in args:
        url = "https://www.austlii.edu.au/cgi-bin/viewdoc/au/cases/cth/HCA/1990/24.html"
    elif "1908" in args and "HCA" in args.upper() and "63" in args:
        url = "https://www.austlii.edu.au/cgi-bin/viewdoc/au/cases/cth/HCA/1908/63.html"
    else:
        return f"Could not map '{args}' to a known AustLII URL. Tell the orchestrator the URL directly."

    p = subprocess.run(
        [
            "curl", "-sS",
            "-A", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
            "-H", "Referer: https://www.austlii.edu.au/forms/search1.html",
            url,
        ],
        capture_output=True, text=True, timeout=30,
    )
    if p.returncode != 0:
        return f"curl failed: rc={p.returncode} stderr={p.stderr[:200]}"
    body = p.stdout
    # Strip HTML and extract the principle-of-legality region (around 'should not impute').
    import re
    text = re.sub(r"<[^>]+>", " ", body)
    text = re.sub(r"\s+", " ", text)
    idx = text.lower().find("should not impute")
    if idx < 0:
        idx = text.lower().find("fundamental rights")
    if idx < 0:
        return text[:2000]
    return text[max(0, idx - 400): idx + 1600]


def drive_turn(role: str, candidate: str | None = CANDIDATE, max_tool_loops: int = 4):
    out = srv._start_turn_impl(None, role, SPEC, candidate, "")
    sid = out["session_id"]
    tool_loops = 0
    while out["status"] == "tool_request" and tool_loops < max_tool_loops:
        tool_loops += 1
        p = out["payload"]
        print(f"[tool_request {tool_loops}] {p['tool_name']} args={p['tool_args']}")
        slash_args = p["tool_args"].get("args") or p["tool_args"].get("query") or ""
        result_text = execute_austlii(slash_args)
        print(f"[tool_result {tool_loops}] {result_text[:200]}...")
        out = srv._resume_turn_impl(sid, p["tool_use_id"], result_text)
    return sid, out


def main() -> int:
    if not os.environ.get("OPENAI_API_KEY"):
        # Pull from .env if present
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent / ".env")
    print("model =", srv.MODEL)
    print("=== CRITIC TURN (gpt-5.5 on Opus's converged candidate) ===")
    sid, out = drive_turn("critic")
    print("session_id:", sid)
    print("status:", out["status"])
    print("payload keys:", list(out["payload"].keys()) if isinstance(out["payload"], dict) else type(out["payload"]))
    print(json.dumps(out["payload"], indent=2, default=str)[:4000])
    print()
    if out["status"] == "final" and isinstance(out["payload"], dict) and "score_of_candidate" in out["payload"]:
        score = (out["payload"].get("score_of_candidate") or {}).get("value")
        print("CRITIC SCORE:", score)
    return 0


if __name__ == "__main__":
    sys.exit(main())
