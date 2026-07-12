"""Suspend/resume round-trip test with a stubbed OpenAI Responses client."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import server as srv  # noqa: E402
from state import Session  # noqa: E402


class _FakeResponse:
    def __init__(self, output, output_text=None, status="completed") -> None:
        self.output = output  # list of plain dicts (server normalises via _item_dict)
        self.output_text = output_text
        self.status = status


class _RecordingResponses:
    def __init__(self, scripted) -> None:
        self._scripted = list(scripted)
        self.calls = 0
        self.call_kwargs = []

    def create(self, **kwargs):
        snap = dict(kwargs)
        if isinstance(snap.get("input"), list):
            snap["input"] = [dict(m) for m in snap["input"]]  # history list mutates later
        self.call_kwargs.append(snap)
        item = self._scripted[self.calls]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


class _FakeClient:
    def __init__(self, scripted) -> None:
        self.responses = _RecordingResponses(scripted)


def _tc_resp(call_id: str, name: str, args: dict) -> _FakeResponse:
    """A response whose output is a reasoning item + one function_call."""
    return _FakeResponse(output=[
        {"type": "reasoning", "encrypted_content": f"ENC-{call_id}"},
        {"type": "function_call", "id": f"fc-{call_id}", "call_id": call_id,
         "name": name, "arguments": json.dumps(args)},
    ])


def _final_resp(payload: dict) -> _FakeResponse:
    text = json.dumps(payload)
    return _FakeResponse(
        output=[{"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": text}]}],
        output_text=text,
    )


class SuspendResumeRoundTrip(unittest.TestCase):
    def test_tool_call_suspends_then_resume_returns_final(self) -> None:
        scripted = [
            _tc_resp("call-1", "claude_slash_command",
                     {"name": "austlii-legal-research", "args": "Coco v R"}),
            _final_resp({
                "role": "critic",
                "candidate_id": "cand-1",
                "counter_draft": "improved draft",
                "score_of_candidate": {"value": 92, "rationale": "missing pin cite"},
                "critique_items": [{
                    "id": "c1", "severity": "minor",
                    "finding": "no pin cite", "suggested_fix": "add pin cite",
                    "addressed": False,
                }],
                "notes": "",
            }),
        ]

        with tempfile.TemporaryDirectory() as td:
            srv._STORE = srv.SessionStore(td)
            fake = _FakeClient(scripted)
            srv._openai_client = lambda: fake

            r1 = srv._start_turn_impl(None, "critic", "score this", "draft A", "")
            self.assertEqual(r1["status"], "tool_request")
            self.assertEqual(r1["payload"]["tool_name"], "claude_slash_command")
            self.assertEqual(r1["payload"]["tool_args"]["name"], "austlii-legal-research")
            self.assertEqual(r1["payload"]["tool_use_id"], "call-1")
            sid = r1["session_id"]

            # The reasoning item AND the function_call must be persisted for replay.
            hist = srv._STORE.get(sid).history
            self.assertIn({"type": "reasoning", "encrypted_content": "ENC-call-1"}, hist)
            self.assertTrue(any(m.get("type") == "function_call" for m in hist))

            r2 = srv._resume_turn_impl(sid, "call-1", "Coco v R (1994) 179 CLR 427")
            self.assertEqual(r2["status"], "final")
            self.assertEqual(r2["payload"]["role"], "critic")
            self.assertEqual(r2["payload"]["score_of_candidate"]["value"], 92)

            # Second call must have replayed the reasoning item, the function_call,
            # and its function_call_output, in order.
            replayed = fake.responses.call_kwargs[1]["input"]
            types_ = [m.get("type") for m in replayed]
            self.assertIn("reasoning", types_)
            self.assertLess(types_.index("reasoning"), types_.index("function_call"))
            self.assertLess(types_.index("function_call"), types_.index("function_call_output"))
            fco = next(m for m in replayed if m.get("type") == "function_call_output")
            self.assertEqual(fco["call_id"], "call-1")
            self.assertEqual(fco["output"], "Coco v R (1994) 179 CLR 427")

    def test_calls_are_stateless_with_encrypted_reasoning(self) -> None:
        scripted = [_final_resp({"role": "critic", "candidate_id": "c", "critique_items": []})]
        with tempfile.TemporaryDirectory() as td:
            srv._STORE = srv.SessionStore(td)
            fake = _FakeClient(scripted)
            srv._openai_client = lambda: fake
            srv._start_turn_impl(None, "critic", "spec", "draft", "")
            kw = fake.responses.call_kwargs[0]
            self.assertIs(kw["store"], False)
            self.assertIn("reasoning.encrypted_content", kw["include"])


class LegacySessionFormat(unittest.TestCase):
    """Sessions persisted by the pre-Responses (chat.completions) bridge."""

    def _legacy_session(self, sid: str) -> Session:
        return Session(
            session_id=sid, role="critic", spec="spec",
            history=[
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "user"},
                {"role": "assistant", "content": "",
                 "tool_calls": [{"id": "call-1", "type": "function",
                                 "function": {"name": "x", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "call-1", "content": "result"},
            ],
            pending_tool_use_id="call-1", pending_tool_name="x", pending_tool_args={},
        )

    def test_resume_of_legacy_session_errors_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            srv._STORE = srv.SessionStore(td)
            srv._STORE.put(self._legacy_session("sess-legacy"))
            r = srv._resume_turn_impl("sess-legacy", "call-1", "result")
            self.assertEqual(r["status"], "error")
            self.assertEqual(r["payload"]["error"], "legacy_session_format")
            self.assertIn("duet_gpt_start_turn", r["payload"]["hint"])

    def test_start_turn_on_legacy_session_id_starts_fresh(self) -> None:
        scripted = [_final_resp({"role": "critic", "candidate_id": "c", "critique_items": []})]
        with tempfile.TemporaryDirectory() as td:
            srv._STORE = srv.SessionStore(td)
            srv._STORE.put(self._legacy_session("sess-legacy"))
            fake = _FakeClient(scripted)
            srv._openai_client = lambda: fake
            r = srv._start_turn_impl("sess-legacy", "critic", "new spec", "draft", "")
            self.assertEqual(r["status"], "final")
            # The replayed input must be the FRESH two-message history, not the
            # legacy chat-format one (which the Responses API would reject).
            replayed = fake.responses.call_kwargs[0]["input"]
            self.assertEqual(len(replayed), 2)
            self.assertFalse(any(m.get("role") == "tool" for m in replayed))


class UnknownRoleError(unittest.TestCase):
    def test_unknown_role_echoes_valid_roles(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            srv._STORE = srv.SessionStore(td)
            r = srv._start_turn_impl(None, "red_team", "spec", None, "")
            self.assertEqual(r["status"], "error")
            self.assertIn("valid_roles", r["payload"])
            self.assertEqual(
                r["payload"]["valid_roles"],
                ["counter_drafter", "critic", "roster_proposer", "verifier"],
            )


if __name__ == "__main__":
    unittest.main()
