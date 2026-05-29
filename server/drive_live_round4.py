"""Round 4: candidate is GPT-5.5's own counter-draft from round 3."""
from __future__ import annotations
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import server as srv  # noqa: E402
from drive_live_demo import drive_turn  # noqa: E402

REVISED = Path(r"C:\Users\acor8\.claude\duet\sessions\job-c33926d97c-candidate-r4.txt").read_text(encoding="utf-8").strip()


def main() -> int:
    if not os.environ.get("OPENAI_API_KEY"):
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent / ".env")
    print("=== CRITIC TURN ROUND 4 (candidate = gpt-5.5's own r3 counter-draft) ===")
    sid, out = drive_turn("critic", candidate=REVISED)
    print("session_id:", sid)
    print("status:", out["status"])
    print(json.dumps(out["payload"], indent=2, default=str)[:5000])
    if out["status"] == "final" and isinstance(out["payload"], dict):
        score = (out["payload"].get("score_of_candidate") or {}).get("value")
        items = out["payload"].get("critique_items") or []
        open_items = [c for c in items if not c.get("addressed")]
        print()
        print("CRITIC SCORE:", score)
        print("OPEN CRITIQUE ITEMS:", len(open_items))
    return 0


if __name__ == "__main__":
    sys.exit(main())
