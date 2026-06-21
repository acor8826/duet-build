"""Case-context grounding tests: project/matter + folder catalogue injection, the new
Session fields, and the Responses-API (live-Drive) loop.

Like the other bridge tests these stub the OpenAI client, so no API key / network is
needed. Two fakes are used: a chat.completions fake (default path) and a responses fake
(DUET_USE_RESPONSES_API path) that records create() kwargs so we can assert the
instructions / previous_response_id chaining.
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
from state import Session, SessionStore  # noqa: E402


# ---------------------- chat.completions fakes (default path) ----------------------

class _FakeMessage:
    def __init__(self, content=None, tool_calls=None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _FakeResponse:
    def __init__(self, message) -> None:
        self.choices = [types.SimpleNamespace(message=message)]


class _ChatCompletions:
    def __init__(self, scripted) -> None:
        self._scripted = list(scripted)
        self.calls = 0
        self.call_kwargs = []

    def create(self, **kwargs):
        self.call_kwargs.append(kwargs)
        msg = self._scripted[self.calls]
        self.calls += 1
        return _FakeResponse(msg)


class _ChatClient:
    def __init__(self, scripted) -> None:
        self.chat = types.SimpleNamespace(completions=_ChatCompletions(scripted))


def _final_chat_msg(score: int = 96) -> _FakeMessage:
    return _FakeMessage(content=json.dumps({
        "role": "critic", "candidate_id": "cand-1", "counter_draft": None,
        "score_of_candidate": {"value": score, "rationale": "r"},
        "critique_items": [], "notes": "",
    }))


# ---------------------- responses fakes (live-Drive path) ----------------------

class _RespFunctionCall:
    def __init__(self, call_id, name, arguments) -> None:
        self.type = "function_call"
        self.call_id = call_id
        self.name = name
        self.arguments = arguments


class _RespResult:
    def __init__(self, id, output, output_text="") -> None:
        self.id = id
        self.output = output
        self.output_text = output_text


class _Responses:
    def __init__(self, scripted) -> None:
        self._scripted = list(scripted)
        self.calls = 0
        self.call_kwargs = []

    def create(self, **kwargs):
        self.call_kwargs.append(kwargs)
        r = self._scripted[self.calls]
        self.calls += 1
        return r


class _RespClient:
    def __init__(self, scripted) -> None:
        self.responses = _Responses(scripted)


def _final_resp(score: int = 96) -> _RespResult:
    return _RespResult("resp-final", [], json.dumps({
        "role": "critic", "candidate_id": "cand-1", "counter_draft": None,
        "score_of_candidate": {"value": score, "rationale": "r"},
        "critique_items": [], "notes": "",
    }))


_CATALOGUE = [
    {"folder_name": "Federal Court Appeal", "folder_id": "fca-1",
     "files": [{"name": "appeal-book.pdf", "file_id": "f1", "mime": "application/pdf"}]},
    {"folder_name": "Supreme Court Case", "folder_id": "scc-1",
     "files": [{"name": "judgment.pdf", "file_id": "f2"}]},
]


# ---------------------- 1. injection into the user message ----------------------

class CatalogueInjection(unittest.TestCase):
    def test_matter_and_catalogue_precede_spec(self) -> None:
        msg = srv._build_user_message(
            "the spec", "the candidate", "",
            project_name="Smith v Commonwealth", folder_catalogue=_CATALOGUE)
        # Ordering: matter, then catalogue, then SPEC.
        self.assertLess(msg.index("PROJECT / MATTER"), msg.index("CASE-FOLDER CATALOGUE"))
        self.assertLess(msg.index("CASE-FOLDER CATALOGUE"), msg.index("SPEC:"))
        # Matter + both folders + files are listed.
        self.assertIn("Smith v Commonwealth", msg)
        self.assertIn("Federal Court Appeal", msg)
        self.assertIn("Supreme Court Case", msg)
        self.assertIn("appeal-book.pdf", msg)
        self.assertIn("judgment.pdf", msg)
        self.assertIn("[id:f1]", msg)

    def test_no_matter_leaves_message_unchanged(self) -> None:
        msg = srv._build_user_message("the spec", None, "")
        self.assertNotIn("PROJECT / MATTER", msg)
        self.assertNotIn("CASE-FOLDER CATALOGUE", msg)
        self.assertTrue(msg.startswith("SPEC:"))


# ---------------------- 2. new Session fields ----------------------

class CaseContextState(unittest.TestCase):
    def test_fields_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = SessionStore(td)
            s = Session(session_id="sess-c", role="critic", spec="s",
                        project_name="Smith v Commonwealth", folder_catalogue=_CATALOGUE,
                        last_response_id="resp-7")
            store.put(s)
            store._cache.clear()  # force disk read
            got = store.get("sess-c")
            assert got is not None
            self.assertEqual(got.project_name, "Smith v Commonwealth")
            self.assertEqual(got.folder_catalogue[0]["folder_name"], "Federal Court Appeal")
            self.assertEqual(got.last_response_id, "resp-7")

    def test_legacy_session_json_loads_with_defaults(self) -> None:
        # A session file written before the case-context fields existed must still load.
        with tempfile.TemporaryDirectory() as td:
            store = SessionStore(td)
            legacy = {
                "session_id": "sess-old", "role": "critic", "spec": "s",
                "history": [{"role": "system", "content": "x"}],
                "documents": [], "available_documents": [], "doc_requests_made": 0,
            }
            Path(td, "sessions", "sess-old.json").write_text(
                json.dumps(legacy), encoding="utf-8")
            got = store.get("sess-old")
            assert got is not None
            self.assertEqual(got.project_name, "")
            self.assertEqual(got.folder_catalogue, [])
            self.assertIsNone(got.last_response_id)


# ---------------------- 3. start_turn threads context into the session ----------------------

class StartTurnThreadsContext(unittest.TestCase):
    def test_session_carries_matter_and_injected_message(self) -> None:
        orig_flag, orig_client = srv.DUET_USE_RESPONSES_API, srv._openai_client
        srv.DUET_USE_RESPONSES_API = False
        try:
            with tempfile.TemporaryDirectory() as td:
                srv._STORE = srv.SessionStore(td)
                srv._openai_client = lambda: _ChatClient([_final_chat_msg(96)])
                r = srv._start_turn_impl(
                    None, "critic", "score this", "draft A", "",
                    project_name="Smith v Commonwealth", folder_catalogue=_CATALOGUE)
                self.assertEqual(r["status"], "final")
                sess = srv._STORE.get(r["session_id"])
                self.assertEqual(sess.project_name, "Smith v Commonwealth")
                self.assertEqual(len(sess.folder_catalogue), 2)
                user_msg = sess.history[1]["content"]
                self.assertIn("PROJECT / MATTER", user_msg)
                self.assertIn("Federal Court Appeal", user_msg)
        finally:
            srv.DUET_USE_RESPONSES_API, srv._openai_client = orig_flag, orig_client


# ---------------------- 4. Responses-API (live-Drive) loop ----------------------

class ResponsesLoop(unittest.TestCase):
    def test_suspend_resume_chains_previous_response_id(self) -> None:
        scripted = [
            _RespResult("resp-1", [_RespFunctionCall(
                "call-1", "request_document", json.dumps({"name": "exhibit.pdf"}))]),
            _final_resp(95),
        ]
        orig_flag, orig_client = srv.DUET_USE_RESPONSES_API, srv._openai_client
        srv.DUET_USE_RESPONSES_API = True
        try:
            with tempfile.TemporaryDirectory() as td:
                srv._STORE = srv.SessionStore(td)
                fake = _RespClient(scripted)
                srv._openai_client = lambda: fake

                r1 = srv._start_turn_impl(
                    None, "critic", "score this", "draft A", "",
                    project_name="Smith v Commonwealth", folder_catalogue=_CATALOGUE)
                self.assertEqual(r1["status"], "tool_request")
                self.assertEqual(r1["payload"]["tool_name"], "request_document")
                sid = r1["session_id"]
                self.assertEqual(srv._STORE.get(sid).last_response_id, "resp-1")

                kw0 = fake.responses.call_kwargs[0]
                # First call: system as instructions, user message as input (no chaining).
                self.assertIn("instructions", kw0)
                self.assertNotIn("previous_response_id", kw0)
                self.assertEqual(kw0["input"][0]["role"], "user")
                self.assertIn("Federal Court Appeal", kw0["input"][0]["content"])
                # Function tools are flattened to the Responses shape (name at top level).
                fn_names = [t.get("name") for t in kw0["tools"] if t.get("type") == "function"]
                self.assertIn("request_document", fn_names)
                self.assertIn("claude_slash_command", fn_names)

                r2 = srv._resume_turn_impl(
                    sid, r1["payload"]["tool_use_id"],
                    json.dumps({"found": True, "name": "exhibit.pdf", "content": "BODY"}))
                self.assertEqual(r2["status"], "final")
                self.assertEqual(r2["payload"]["role"], "critic")

                kw1 = fake.responses.call_kwargs[1]
                # Continuation: chain on the prior response, send only the function output.
                self.assertEqual(kw1["previous_response_id"], "resp-1")
                # instructions are not inherited across previous_response_id — resent every call.
                self.assertIn("instructions", kw1)
                self.assertEqual(kw1["input"][0]["type"], "function_call_output")
                self.assertEqual(kw1["input"][0]["call_id"], "call-1")
                # tools (carrying the connector authorization) are re-sent on the continuation.
                self.assertIn("tools", kw1)
        finally:
            srv.DUET_USE_RESPONSES_API, srv._openai_client = orig_flag, orig_client


class ReasoningEffort(unittest.TestCase):
    def _run(self, responses_api: bool, effort: str):
        orig = (srv.DUET_USE_RESPONSES_API, srv.DUET_GPT_REASONING_EFFORT, srv._openai_client)
        srv.DUET_USE_RESPONSES_API = responses_api
        srv.DUET_GPT_REASONING_EFFORT = effort
        try:
            with tempfile.TemporaryDirectory() as td:
                srv._STORE = srv.SessionStore(td)
                fake = _RespClient([_final_resp(96)]) if responses_api else _ChatClient([_final_chat_msg(96)])
                srv._openai_client = lambda: fake
                srv._start_turn_impl(None, "critic", "spec", "draft", "")
                return (fake.responses.call_kwargs[0] if responses_api
                        else fake.chat.completions.call_kwargs[0])
        finally:
            srv.DUET_USE_RESPONSES_API, srv.DUET_GPT_REASONING_EFFORT, srv._openai_client = orig

    def test_chat_path_sends_reasoning_effort(self) -> None:
        kw = self._run(responses_api=False, effort="high")
        self.assertEqual(kw["reasoning_effort"], "high")

    def test_responses_path_sends_reasoning_object(self) -> None:
        kw = self._run(responses_api=True, effort="xhigh")
        self.assertEqual(kw["reasoning"], {"effort": "xhigh"})

    def test_unset_sends_no_reasoning(self) -> None:
        kw = self._run(responses_api=False, effort="")
        self.assertNotIn("reasoning_effort", kw)
        self.assertNotIn("reasoning", kw)


if __name__ == "__main__":
    unittest.main()
