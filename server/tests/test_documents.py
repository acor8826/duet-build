"""Document-exchange tests: push rendering, multi-step pull, budget guard, mixed tools.

Like test_suspend_resume.py these stub the OpenAI client, so no API key / network is
needed. The fake records the kwargs of each create() call so we can assert the Pattern A
loop behaviour (tools offered with response_format unconstrained while the document budget
remains; forced closure once it is exhausted).
"""
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


# ---------------------- fakes ----------------------

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


class _RecordingCompletions:
    """Returns scripted messages in order and records every create() kwargs."""

    def __init__(self, scripted) -> None:
        self._scripted = list(scripted)
        self.calls = 0
        self.call_kwargs = []

    def create(self, **kwargs):
        self.call_kwargs.append(kwargs)
        msg = self._scripted[self.calls]
        self.calls += 1
        return _FakeResponse(msg)


class _FakeClient:
    def __init__(self, scripted) -> None:
        self.completions = _RecordingCompletions(scripted)
        self.chat = types.SimpleNamespace(completions=self.completions)


def _tc_msg(call_id: str, name: str, args: dict) -> _FakeMessage:
    return _FakeMessage(content=None,
                        tool_calls=[_FakeToolCall(call_id, name, json.dumps(args))])


def _final_msg(score: int = 92) -> _FakeMessage:
    return _FakeMessage(content=json.dumps({
        "role": "critic",
        "candidate_id": "cand-1",
        "counter_draft": None,
        "score_of_candidate": {"value": score, "rationale": "grounded in the document"},
        "critique_items": [],
        "notes": "",
    }), tool_calls=None)


def _install(scripted, td) -> _FakeClient:
    srv._STORE = srv.SessionStore(td)
    fake = _FakeClient(scripted)
    srv._openai_client = lambda: fake
    return fake


# ---------------------- push (Claude -> GPT) ----------------------

class PushRendering(unittest.TestCase):
    def test_documents_and_catalog_rendered(self) -> None:
        msg = srv._build_user_message(
            "the spec", "the candidate", "",
            documents=[{"name": "a.txt", "content": "HELLO BODY", "source": "vault"}],
            available_documents=[{"name": "b.pdf", "description": "big doc", "source": "vault"}],
        )
        self.assertIn("=== DOCUMENT: a.txt [vault] ===", msg)
        self.assertIn("HELLO BODY", msg)
        self.assertIn("AVAILABLE DOCUMENTS", msg)
        self.assertIn("b.pdf", msg)
        self.assertIn("big doc", msg)
        self.assertIn("request_document", msg)

    def test_oversized_document_is_truncated(self) -> None:
        orig = srv.DUET_MAX_DOC_CHARS
        srv.DUET_MAX_DOC_CHARS = 5
        try:
            msg = srv._build_user_message(
                "spec", None, "",
                documents=[{"name": "big.txt", "content": "HELLOWORLD"}],
            )
            self.assertIn("(truncated)", msg)
            self.assertIn("HELLO", msg)
            self.assertNotIn("HELLOWORLD", msg)
        finally:
            srv.DUET_MAX_DOC_CHARS = orig


# ---------------------- pull (GPT -> Claude, multi-step) ----------------------

class MultiStepPull(unittest.TestCase):
    def test_two_document_requests_then_final(self) -> None:
        scripted = [
            _tc_msg("call-1", "request_document", {"name": "contract.pdf", "query": "indemnity"}),
            _tc_msg("call-2", "request_document", {"name": "exhibit-a.pdf"}),
            _final_msg(94),
        ]
        with tempfile.TemporaryDirectory() as td:
            _install(scripted, td)

            r1 = srv._start_turn_impl(None, "critic", "score this", "draft A", "",
                                      available_documents=[{"name": "contract.pdf"}])
            self.assertEqual(r1["status"], "tool_request")
            self.assertEqual(r1["payload"]["tool_name"], "request_document")
            self.assertEqual(r1["payload"]["tool_args"]["name"], "contract.pdf")
            sid = r1["session_id"]
            self.assertEqual(srv._STORE.get(sid).doc_requests_made, 1)

            r2 = srv._resume_turn_impl(
                sid, r1["payload"]["tool_use_id"],
                json.dumps({"found": True, "name": "contract.pdf", "content": "CONTRACT TEXT",
                            "mime": "text/plain", "truncated": False}))
            self.assertEqual(r2["status"], "tool_request")
            self.assertEqual(r2["payload"]["tool_name"], "request_document")
            self.assertEqual(r2["payload"]["tool_args"]["name"], "exhibit-a.pdf")
            self.assertEqual(srv._STORE.get(sid).doc_requests_made, 2)

            r3 = srv._resume_turn_impl(
                sid, r2["payload"]["tool_use_id"],
                json.dumps({"found": True, "name": "exhibit-a.pdf", "content": "EXHIBIT TEXT"}))
            self.assertEqual(r3["status"], "final")
            self.assertEqual(r3["payload"]["role"], "critic")
            self.assertEqual(r3["payload"]["score_of_candidate"]["value"], 94)

    def test_not_found_result_still_reaches_final(self) -> None:
        scripted = [
            _tc_msg("call-1", "request_document", {"name": "missing.pdf"}),
            _final_msg(90),
        ]
        with tempfile.TemporaryDirectory() as td:
            _install(scripted, td)
            r1 = srv._start_turn_impl(None, "critic", "spec", "draft", "")
            self.assertEqual(r1["payload"]["tool_name"], "request_document")
            r2 = srv._resume_turn_impl(
                r1["session_id"], r1["payload"]["tool_use_id"],
                json.dumps({"found": False, "reason": "not in vault", "available": ["a.pdf"]}))
            self.assertEqual(r2["status"], "final")
            self.assertEqual(r2["payload"]["role"], "critic")


class BudgetGuard(unittest.TestCase):
    def test_forces_final_once_budget_exhausted(self) -> None:
        orig = srv.DUET_MAX_DOC_REQUESTS
        srv.DUET_MAX_DOC_REQUESTS = 2
        scripted = [
            _tc_msg("call-1", "request_document", {"name": "d1"}),
            _tc_msg("call-2", "request_document", {"name": "d2"}),
            _final_msg(91),
        ]
        try:
            with tempfile.TemporaryDirectory() as td:
                fake = _install(scripted, td)
                r1 = srv._start_turn_impl(None, "critic", "spec", "draft", "")
                sid = r1["session_id"]
                r2 = srv._resume_turn_impl(sid, r1["payload"]["tool_use_id"], "{\"found\":true}")
                r3 = srv._resume_turn_impl(sid, r2["payload"]["tool_use_id"], "{\"found\":true}")
                self.assertEqual(r3["status"], "final")
                self.assertEqual(srv._STORE.get(sid).doc_requests_made, 2)

                kw = fake.completions.call_kwargs
                # First two calls: budget remained -> tools offered, no forced json_object.
                self.assertEqual(kw[0]["tool_choice"], "auto")
                self.assertNotIn("response_format", kw[0])
                self.assertFalse(kw[0]["parallel_tool_calls"])
                self.assertEqual(kw[1]["tool_choice"], "auto")
                # Third call: budget exhausted -> forced final.
                self.assertEqual(kw[2]["tool_choice"], "none")
                self.assertEqual(kw[2]["response_format"], {"type": "json_object"})
        finally:
            srv.DUET_MAX_DOC_REQUESTS = orig


class MixedTools(unittest.TestCase):
    def test_request_document_then_slash_command(self) -> None:
        scripted = [
            _tc_msg("call-1", "request_document", {"name": "brief.txt"}),
            _tc_msg("call-2", "claude_slash_command", {"name": "austlii-legal-research", "args": "Coco v R"}),
            _final_msg(93),
        ]
        with tempfile.TemporaryDirectory() as td:
            _install(scripted, td)
            r1 = srv._start_turn_impl(None, "critic", "spec", "draft", "")
            self.assertEqual(r1["payload"]["tool_name"], "request_document")
            sid = r1["session_id"]
            r2 = srv._resume_turn_impl(sid, r1["payload"]["tool_use_id"], "{\"found\":true}")
            self.assertEqual(r2["payload"]["tool_name"], "claude_slash_command")
            self.assertEqual(r2["payload"]["tool_args"]["name"], "austlii-legal-research")
            # slash command must NOT count against the document budget
            self.assertEqual(srv._STORE.get(sid).doc_requests_made, 1)
            r3 = srv._resume_turn_impl(sid, r2["payload"]["tool_use_id"], "Coco v R (1994) 179 CLR 427")
            self.assertEqual(r3["status"], "final")


# ---------------------- resume result normalization (hardening) ----------------------

class ResumeResultNormalization(unittest.TestCase):
    def test_non_json_request_document_result_is_wrapped(self) -> None:
        scripted = [
            _tc_msg("call-1", "request_document", {"name": "contract.pdf"}),
            _final_msg(90),
        ]
        with tempfile.TemporaryDirectory() as td:
            _install(scripted, td)
            r1 = srv._start_turn_impl(None, "critic", "spec", "draft", "")
            sid = r1["session_id"]
            # Orchestrator returns RAW TEXT (not JSON) — bridge should wrap it so GPT
            # receives a well-formed request_document result.
            r2 = srv._resume_turn_impl(sid, r1["payload"]["tool_use_id"], "CONTRACT BODY TEXT")
            self.assertEqual(r2["status"], "final")
            tool_msgs = [m for m in srv._STORE.get(sid).history if m.get("role") == "tool"]
            self.assertEqual(len(tool_msgs), 1)
            parsed = json.loads(tool_msgs[0]["content"])  # must be valid JSON now
            self.assertTrue(parsed["found"])
            self.assertEqual(parsed["content"], "CONTRACT BODY TEXT")

    def test_valid_json_request_document_result_passes_through(self) -> None:
        scripted = [
            _tc_msg("call-1", "request_document", {"name": "contract.pdf"}),
            _final_msg(90),
        ]
        with tempfile.TemporaryDirectory() as td:
            _install(scripted, td)
            r1 = srv._start_turn_impl(None, "critic", "spec", "draft", "")
            sid = r1["session_id"]
            payload = json.dumps({"found": True, "name": "contract.pdf", "content": "BODY"})
            srv._resume_turn_impl(sid, r1["payload"]["tool_use_id"], payload)
            tool_msgs = [m for m in srv._STORE.get(sid).history if m.get("role") == "tool"]
            self.assertEqual(tool_msgs[0]["content"], payload)  # unchanged

    def test_slash_command_result_not_wrapped(self) -> None:
        scripted = [
            _tc_msg("call-1", "claude_slash_command", {"name": "x", "args": "y"}),
            _final_msg(90),
        ]
        with tempfile.TemporaryDirectory() as td:
            _install(scripted, td)
            r1 = srv._start_turn_impl(None, "critic", "spec", "draft", "")
            sid = r1["session_id"]
            srv._resume_turn_impl(sid, r1["payload"]["tool_use_id"], "RAW SLASH OUTPUT")
            tool_msgs = [m for m in srv._STORE.get(sid).history if m.get("role") == "tool"]
            self.assertEqual(tool_msgs[0]["content"], "RAW SLASH OUTPUT")  # passed through


# ---------------------- duet_run push-only ----------------------

class DuetRunPushOnly(unittest.TestCase):
    def test_documents_reach_prompts_and_pull_is_unavailable(self) -> None:
        import duet_run as dr

        seen = {"opus": [], "gpt": []}

        def fake_opus(system, user, max_tokens=4096):
            seen["opus"].append(user)
            return json.dumps({
                "candidate_id": "cand-1", "candidate_text": "ARTIFACT",
                "self_score": {"value": 96, "rationale": "r"},
                "score": {"value": 96, "rationale": "r"},
                "verdict": "PASS", "blocking_findings": [],
            })

        def fake_gpt(system, user):
            seen["gpt"].append(user)
            return json.dumps({
                "role": "critic", "candidate_id": "cand-1",
                "score_of_candidate": {"value": 96, "rationale": "r"},
                "critique_items": [], "notes": "",
            })

        orig_avail, orig_opus, orig_gpt = dr.opus_available, dr._opus_call, dr._gpt_call
        dr.opus_available = lambda: True
        dr._opus_call = fake_opus
        dr._gpt_call = fake_gpt
        try:
            out = dr.run_duet("write a memo", documents=[{"name": "source.txt", "content": "DOCBODY-XYZ"}])
        finally:
            dr.opus_available, dr._opus_call, dr._gpt_call = orig_avail, orig_opus, orig_gpt

        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["documents_attached"], 1)
        # Document body reached both the drafting and the critique prompts.
        self.assertTrue(any("DOCBODY-XYZ" in u for u in seen["opus"]))
        self.assertTrue(any("DOCBODY-XYZ" in u for u in seen["gpt"]))
        self.assertTrue(any("=== DOCUMENT: source.txt ===" in u for u in seen["gpt"]))
        # Pull remains documented as unavailable server-side.
        self.assertTrue(any("PUSH-ONLY" in lim for lim in out["limitations"]))


if __name__ == "__main__":
    unittest.main()
