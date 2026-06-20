"""Bounded-call tests: the bridge must return INSIDE the MCP client's ~180s tool-call cap.

These stub the OpenAI client (no API key / network needed) and assert the three guards
added for large document payloads:
  * the client is constructed with a sub-cap request timeout and no retries;
  * every chat.completions.create() carries the output-token cap;
  * a timeout-class error from create() is converted to a clean, retriable `gpt_timeout`
    response (not a silent overrun) and leaves the session free of a dangling pending tool.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import server as srv  # noqa: E402

# Capture the REAL factory at import time, before any _install() below swaps it for a stub.
_ORIGINAL_OPENAI_CLIENT = srv._openai_client


# ---------------------- fakes ----------------------

class _StubTimeout(Exception):
    """Class name contains 'Timeout' so _is_timeout_error matches it via the name fallback."""


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


class _RecordingCompletions:
    """Returns scripted messages (or raises a scripted exception) and records create() kwargs."""

    def __init__(self, scripted) -> None:
        self._scripted = list(scripted)
        self.calls = 0
        self.call_kwargs = []

    def create(self, **kwargs):
        self.call_kwargs.append(kwargs)
        item = self._scripted[self.calls]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(item)


class _FakeClient:
    def __init__(self, scripted) -> None:
        self.completions = _RecordingCompletions(scripted)
        self.chat = types.SimpleNamespace(completions=self.completions)


def _final_msg(score: int = 92) -> _FakeMessage:
    return _FakeMessage(content=json.dumps({
        "role": "critic",
        "candidate_id": "cand-1",
        "counter_draft": None,
        "score_of_candidate": {"value": score, "rationale": "ok"},
        "critique_items": [],
        "notes": "",
    }), tool_calls=None)


def _install(scripted, td) -> _FakeClient:
    srv._STORE = srv.SessionStore(td)
    fake = _FakeClient(scripted)
    srv._openai_client = lambda: fake
    return fake


# ---------------------- client construction ----------------------

class ClientBounded(unittest.TestCase):
    def test_openai_client_built_with_timeout_and_no_retries(self) -> None:
        captured = {}

        class _CapOpenAI:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        fake_openai = types.ModuleType("openai")
        fake_openai.OpenAI = _CapOpenAI
        old_mod = sys.modules.get("openai")
        old_key = os.environ.get("OPENAI_API_KEY")
        sys.modules["openai"] = fake_openai
        os.environ["OPENAI_API_KEY"] = "test-key"
        try:
            client = _ORIGINAL_OPENAI_CLIENT()  # runs the real factory against the faked module
            self.assertIsInstance(client, _CapOpenAI)
            self.assertEqual(captured["timeout"], srv.DUET_OPENAI_TIMEOUT)
            self.assertEqual(captured["max_retries"], srv.DUET_OPENAI_MAX_RETRIES)
            self.assertEqual(captured["api_key"], "test-key")
        finally:
            if old_mod is not None:
                sys.modules["openai"] = old_mod
            else:
                sys.modules.pop("openai", None)
            if old_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = old_key


# ---------------------- output-token cap ----------------------

class OutputCap(unittest.TestCase):
    def test_create_carries_output_token_cap(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake = _install([_final_msg(90)], td)
            r = srv._start_turn_impl(None, "critic", "spec", "draft", "")
            self.assertEqual(r["status"], "final")
            kw = fake.completions.call_kwargs[0]
            self.assertIn(srv.DUET_OUTPUT_TOKEN_PARAM, kw)
            self.assertEqual(kw[srv.DUET_OUTPUT_TOKEN_PARAM], srv.DUET_MAX_OUTPUT_TOKENS)


# ---------------------- fail-fast on timeout ----------------------

class TimeoutFailsFast(unittest.TestCase):
    def test_timeout_returns_retriable_error_and_clean_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _install([_StubTimeout("read timed out")], td)
            r = srv._start_turn_impl(None, "critic", "spec", "a big candidate", "",
                                     documents=[{"name": "d.txt", "content": "X" * 1000}])
            self.assertEqual(r["status"], "error")
            self.assertEqual(r["payload"]["error"], "gpt_timeout")
            self.assertIs(r["payload"]["retriable"], True)
            self.assertIn("concise", r["payload"]["hint"].lower())
            # Session must be left clean (no dangling pending tool) so a condensed retry works.
            sess = srv._STORE.get(r["session_id"])
            self.assertIsNotNone(sess)
            self.assertIsNone(sess.pending_tool_use_id)
            self.assertIsNone(sess.pending_tool_name)

    def test_non_timeout_exception_propagates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _install([ValueError("boom")], td)
            with self.assertRaises(ValueError):
                srv._start_turn_impl(None, "critic", "spec", "draft", "")


if __name__ == "__main__":
    unittest.main()
