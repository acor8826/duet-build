"""Round-trip + suspend/resume tests for the state store."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Ensure server/ is importable.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from state import Session, SessionStore  # noqa: E402
from rubric import (  # noqa: E402
    CritiqueItem,
    Score,
    WorkProduct,
    convergence_check,
)


class StateRoundTrip(unittest.TestCase):
    def test_put_get_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = SessionStore(td)
            s = Session(
                session_id="sess-test",
                role="critic",
                spec="hello",
                history=[{"role": "system", "content": "x"}],
            )
            store.put(s)
            got = store.get("sess-test")
            assert got is not None
            self.assertEqual(got.session_id, "sess-test")
            self.assertEqual(got.role, "critic")
            self.assertEqual(got.spec, "hello")
            self.assertEqual(got.history[0]["content"], "x")

    def test_put_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = SessionStore(td)
            s = Session(session_id="sess-x", role="critic", spec="s")
            store.put(s)
            # File exists, no temp leftovers.
            files = list(Path(td, "sessions").iterdir())
            tmps = [f for f in files if ".tmp" in f.name]
            self.assertEqual(tmps, [])

    def test_pending_tool_state_persists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = SessionStore(td)
            s = Session(
                session_id="sess-p",
                role="critic",
                spec="s",
                pending_tool_use_id="call-1",
                pending_tool_name="claude_slash_command",
                pending_tool_args={"name": "austlii-legal-research", "args": "Coco v R"},
            )
            store.put(s)
            store._cache.clear()  # force disk read
            got = store.get("sess-p")
            assert got is not None
            self.assertEqual(got.pending_tool_use_id, "call-1")
            self.assertEqual(got.pending_tool_name, "claude_slash_command")
            self.assertEqual(got.pending_tool_args["name"], "austlii-legal-research")


class DocumentFieldsState(unittest.TestCase):
    def test_document_fields_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = SessionStore(td)
            s = Session(
                session_id="sess-doc",
                role="critic",
                spec="s",
                documents=[{"name": "a.txt", "content": "BODY", "source": "vault"}],
                available_documents=[{"name": "b.pdf", "description": "big"}],
                doc_requests_made=3,
            )
            store.put(s)
            store._cache.clear()  # force disk read
            got = store.get("sess-doc")
            assert got is not None
            self.assertEqual(got.documents[0]["content"], "BODY")
            self.assertEqual(got.available_documents[0]["name"], "b.pdf")
            self.assertEqual(got.doc_requests_made, 3)

    def test_legacy_session_json_loads_with_defaults(self) -> None:
        # A session file written before the document fields existed must still load.
        with tempfile.TemporaryDirectory() as td:
            store = SessionStore(td)
            legacy = {
                "session_id": "sess-old",
                "role": "critic",
                "spec": "s",
                "history": [{"role": "system", "content": "x"}],
                "pending_tool_use_id": None,
                "pending_tool_name": None,
                "pending_tool_args": None,
                "last_final": None,
                "closed": False,
            }
            Path(td, "sessions", "sess-old.json").write_text(
                json.dumps(legacy), encoding="utf-8")
            got = store.get("sess-old")
            assert got is not None
            self.assertEqual(got.documents, [])
            self.assertEqual(got.available_documents, [])
            self.assertEqual(got.doc_requests_made, 0)


class ConvergenceTests(unittest.TestCase):
    def _score(self, v: int) -> Score:
        return Score(value=v, rationale="ok")

    def _crit(self, addressed: bool) -> CritiqueItem:
        return CritiqueItem(
            id="c1",
            severity="major",
            finding="missing citation",
            suggested_fix="add citation",
            addressed=addressed,
        )

    def test_both_above_threshold_no_open_converges(self) -> None:
        ok, _ = convergence_check(self._score(96), self._score(95), [], 95)
        self.assertTrue(ok)

    def test_both_above_threshold_with_open_does_not_converge(self) -> None:
        ok, reason = convergence_check(
            self._score(96), self._score(98), [self._crit(addressed=False)], 95
        )
        self.assertFalse(ok)
        self.assertIn("unaddressed", reason)

    def test_one_below_threshold(self) -> None:
        ok, reason = convergence_check(self._score(96), self._score(80), [], 95)
        self.assertFalse(ok)
        self.assertIn("below threshold", reason)

    def test_addressed_items_dont_block(self) -> None:
        ok, _ = convergence_check(
            self._score(96), self._score(96), [self._crit(addressed=True)], 95
        )
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
