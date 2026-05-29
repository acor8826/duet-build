"""Suspend/resume round-trip test with a stubbed OpenAI client."""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import server as srv  # noqa: E402


class _FakeFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, id: str, name: str, arguments: str) -> None:
        self.id = id
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, content=None, tool_calls=None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message) -> None:
        self.message = message


class _FakeResponse:
    def __init__(self, message) -> None:
        self.choices = [_FakeChoice(message)]


class _FakeChatCompletions:
    def __init__(self, scripted) -> None:
        self._scripted = list(scripted)
        self.calls = 0

    def create(self, **kwargs):  # noqa: ARG002
        msg = self._scripted[self.calls]
        self.calls += 1
        return _FakeResponse(msg)


class _FakeClient:
    def __init__(self, scripted) -> None:
        self.chat = types.SimpleNamespace(completions=_FakeChatCompletions(scripted))


class SuspendResumeRoundTrip(unittest.TestCase):
    def test_tool_call_suspends_then_resume_returns_final(self) -> None:
        scripted = [
            _FakeMessage(
                content=None,
                tool_calls=[_FakeToolCall("call-1", "claude_slash_command",
                                          json.dumps({"name": "austlii-legal-research",
                                                      "args": "Coco v R"}))],
            ),
            _FakeMessage(
                content=json.dumps({
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
                tool_calls=None,
            ),
        ]

        with tempfile.TemporaryDirectory() as td:
            srv._STORE = srv.SessionStore(td)
            fake = _FakeClient(scripted)
            srv._openai_client = lambda: fake

            r1 = srv._start_turn_impl(None, "critic", "score this", "draft A", "")
            self.assertEqual(r1["status"], "tool_request")
            self.assertEqual(r1["payload"]["tool_name"], "claude_slash_command")
            self.assertEqual(r1["payload"]["tool_args"]["name"], "austlii-legal-research")
            sid = r1["session_id"]

            r2 = srv._resume_turn_impl(sid, r1["payload"]["tool_use_id"], "Coco v R (1994) 179 CLR 427")
            self.assertEqual(r2["status"], "final")
            self.assertEqual(r2["payload"]["role"], "critic")
            self.assertEqual(r2["payload"]["score_of_candidate"]["value"], 92)


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
