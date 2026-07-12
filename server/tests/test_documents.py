"""Document-exchange tests: push rendering, multi-step pull, budget guard, mixed tools.

Like test_suspend_resume.py these stub the OpenAI client, so no API key / network is
needed. The fake records the kwargs of each create() call so we can assert the Pattern A
loop behaviour on the Responses API (tools offered with the text format unconstrained
while the document budget remains; forced closure once it is exhausted).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import server as srv  # noqa: E402


# ---------------------- fakes ----------------------

class _FakeResponse:
    def __init__(self, output, output_text=None, status="completed") -> None:
        self.output = output
        self.output_text = output_text
        self.status = status


class _RecordingResponses:
    """Returns scripted responses in order and records every create() kwargs."""

    def __init__(self, scripted) -> None:
        self._scripted = list(scripted)
        self.calls = 0
        self.call_kwargs = []

    def create(self, **kwargs):
        snap = dict(kwargs)
        if isinstance(snap.get("input"), list):
            snap["input"] = [dict(m) for m in snap["input"]]
        self.call_kwargs.append(snap)
        resp = self._scripted[self.calls]
        self.calls += 1
        return resp


class _FakeClient:
    def __init__(self, scripted) -> None:
        self.responses = _RecordingResponses(scripted)


def _tc_resp(call_id: str, name: str, args: dict) -> _FakeResponse:
    return _FakeResponse(output=[
        {"type": "reasoning", "encrypted_content": f"ENC-{call_id}"},
        {"type": "function_call", "id": f"fc-{call_id}", "call_id": call_id,
         "name": name, "arguments": json.dumps(args)},
    ])


def _final_resp(score: int = 92) -> _FakeResponse:
    text = json.dumps({
        "role": "critic",
        "candidate_id": "cand-1",
        "counter_draft": None,
        "score_of_candidate": {"value": score, "rationale": "grounded in the document"},
        "critique_items": [],
        "notes": "",
    })
    return _FakeResponse(
        output=[{"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": text}]}],
        output_text=text,
    )


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

    def test_cumulative_doc_budget_trims_and_notes(self) -> None:
        # Per-doc cap is generous; the CUMULATIVE cap is what bounds a multi-doc push so the
        # one blocking critique call stays inside the client window.
        orig_total = srv.DUET_MAX_TOTAL_DOC_CHARS
        srv.DUET_MAX_TOTAL_DOC_CHARS = 15
        try:
            msg = srv._build_user_message(
                "spec", None, "",
                documents=[
                    {"name": "d1", "content": "AAAAAAAAAA"},  # 10 -> fits (remaining 5)
                    {"name": "d2", "content": "BBBBBBBBBB"},  # 10 -> truncated to 5 (remaining 0)
                    {"name": "d3", "content": "CCCCCCCCCC"},  # omitted, budget spent
                ],
            )
            self.assertIn("AAAAAAAAAA", msg)          # first doc whole
            self.assertIn("BBBBB", msg)               # second doc partially included
            self.assertNotIn("BBBBBBBBBB", msg)       # ...but truncated by the cumulative cap
            self.assertNotIn("CCCCCCCCCC", msg)       # third doc omitted entirely
            self.assertIn("1 document(s) omitted", msg)
            self.assertIn("request them", msg)        # points GPT at request_document
        finally:
            srv.DUET_MAX_TOTAL_DOC_CHARS = orig_total


# ---------------------- pull (GPT -> Claude, multi-step) ----------------------

class MultiStepPull(unittest.TestCase):
    def test_two_document_requests_then_final(self) -> None:
        scripted = [
            _tc_resp("call-1", "request_document", {"name": "contract.pdf", "query": "indemnity"}),
            _tc_resp("call-2", "request_document", {"name": "exhibit-a.pdf"}),
            _final_resp(94),
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
            _tc_resp("call-1", "request_document", {"name": "missing.pdf"}),
            _final_resp(90),
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
            _tc_resp("call-1", "request_document", {"name": "d1"}),
            _tc_resp("call-2", "request_document", {"name": "d2"}),
            _final_resp(91),
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

                kw = fake.responses.call_kwargs
                # First two calls: budget remained -> tools offered, no forced json_object.
                self.assertEqual(kw[0]["tool_choice"], "auto")
                self.assertNotIn("text", kw[0])
                self.assertFalse(kw[0]["parallel_tool_calls"])
                self.assertEqual(kw[1]["tool_choice"], "auto")
                # Third call: budget exhausted -> forced final.
                self.assertEqual(kw[2]["tool_choice"], "none")
                self.assertEqual(kw[2]["text"], {"format": {"type": "json_object"}})
        finally:
            srv.DUET_MAX_DOC_REQUESTS = orig


class MixedTools(unittest.TestCase):
    def test_request_document_then_slash_command(self) -> None:
        scripted = [
            _tc_resp("call-1", "request_document", {"name": "brief.txt"}),
            _tc_resp("call-2", "claude_slash_command", {"name": "austlii-legal-research", "args": "Coco v R"}),
            _final_resp(93),
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
            _tc_resp("call-1", "request_document", {"name": "contract.pdf"}),
            _final_resp(90),
        ]
        with tempfile.TemporaryDirectory() as td:
            _install(scripted, td)
            r1 = srv._start_turn_impl(None, "critic", "spec", "draft", "")
            sid = r1["session_id"]
            # Orchestrator returns RAW TEXT (not JSON) — bridge should wrap it so GPT
            # receives a well-formed request_document result.
            r2 = srv._resume_turn_impl(sid, r1["payload"]["tool_use_id"], "CONTRACT BODY TEXT")
            self.assertEqual(r2["status"], "final")
            outputs = [m for m in srv._STORE.get(sid).history
                       if m.get("type") == "function_call_output"]
            self.assertEqual(len(outputs), 1)
            parsed = json.loads(outputs[0]["output"])  # must be valid JSON now
            self.assertTrue(parsed["found"])
            self.assertEqual(parsed["content"], "CONTRACT BODY TEXT")

    def test_valid_json_request_document_result_passes_through(self) -> None:
        scripted = [
            _tc_resp("call-1", "request_document", {"name": "contract.pdf"}),
            _final_resp(90),
        ]
        with tempfile.TemporaryDirectory() as td:
            _install(scripted, td)
            r1 = srv._start_turn_impl(None, "critic", "spec", "draft", "")
            sid = r1["session_id"]
            payload = json.dumps({"found": True, "name": "contract.pdf", "content": "BODY"})
            srv._resume_turn_impl(sid, r1["payload"]["tool_use_id"], payload)
            outputs = [m for m in srv._STORE.get(sid).history
                       if m.get("type") == "function_call_output"]
            self.assertEqual(outputs[0]["output"], payload)  # unchanged

    def test_slash_command_result_not_wrapped(self) -> None:
        scripted = [
            _tc_resp("call-1", "claude_slash_command", {"name": "x", "args": "y"}),
            _final_resp(90),
        ]
        with tempfile.TemporaryDirectory() as td:
            _install(scripted, td)
            r1 = srv._start_turn_impl(None, "critic", "spec", "draft", "")
            sid = r1["session_id"]
            srv._resume_turn_impl(sid, r1["payload"]["tool_use_id"], "RAW SLASH OUTPUT")
            outputs = [m for m in srv._STORE.get(sid).history
                       if m.get("type") == "function_call_output"]
            self.assertEqual(outputs[0]["output"], "RAW SLASH OUTPUT")  # passed through


# ---------------------- parallel tool calls (replay safety) ----------------------

class ParallelCallsDropped(unittest.TestCase):
    def test_extra_function_calls_not_persisted(self) -> None:
        # Two calls in one response: only the first may be persisted, else the next
        # replay contains an unanswered function_call and the API rejects it.
        double = _FakeResponse(output=[
            {"type": "function_call", "id": "fc-1", "call_id": "call-1",
             "name": "request_document", "arguments": json.dumps({"name": "a"})},
            {"type": "function_call", "id": "fc-2", "call_id": "call-2",
             "name": "request_document", "arguments": json.dumps({"name": "b"})},
        ])
        scripted = [double, _final_resp(90)]
        with tempfile.TemporaryDirectory() as td:
            _install(scripted, td)
            r1 = srv._start_turn_impl(None, "critic", "spec", "draft", "")
            self.assertEqual(r1["status"], "tool_request")
            self.assertEqual(r1["payload"]["tool_use_id"], "call-1")
            sid = r1["session_id"]
            calls_in_history = [m for m in srv._STORE.get(sid).history
                                if m.get("type") == "function_call"]
            self.assertEqual(len(calls_in_history), 1)
            self.assertEqual(calls_in_history[0]["call_id"], "call-1")
            r2 = srv._resume_turn_impl(sid, "call-1", "{\"found\":true}")
            self.assertEqual(r2["status"], "final")


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
