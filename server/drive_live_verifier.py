"""Run the INDEPENDENT VERIFIER role against the live GPT-5.5 model.
Fresh session_id, role=verifier so the system prompt is the verifier prompt
(sees only spec + final candidate, no critique history)."""
from __future__ import annotations
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import server as srv  # noqa: E402
from drive_live_demo import drive_turn  # noqa: E402

FINAL = Path(r"C:\Users\acor8\.claude\duet\sessions\job-c33926d97c-candidate-r8.txt").read_text(encoding="utf-8").strip()


def main() -> int:
    if not os.environ.get("OPENAI_API_KEY"):
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent / ".env")
    print("=== INDEPENDENT VERIFIER (live gpt-5.5, fresh context, sees only spec+final) ===")
    sid, out = drive_turn("verifier", candidate=FINAL)
    print("session_id:", sid)
    print("status:", out["status"])
    print(json.dumps(out["payload"], indent=2, default=str)[:5000])
    if out["status"] == "final" and isinstance(out["payload"], dict):
        score = (out["payload"].get("score_of_candidate") or {}).get("value")
        items = out["payload"].get("critique_items") or []
        blockers = [c for c in items if c.get("severity") == "blocker" and not c.get("addressed")]
        print()
        print("VERIFIER SCORE:", score)
        print("BLOCKERS:", len(blockers))
        if score and score >= 95 and len(blockers) == 0:
            print(">>> VERIFIER PASS <<<")
    return 0


if __name__ == "__main__":
    sys.exit(main())
