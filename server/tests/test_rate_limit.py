"""Quota-vs-throttle tests: a 429 must not be reported as "out of credit".

Both vendors answer 429 for two unrelated conditions — a terminal billing stop and a
transient per-minute tier ceiling — and the bridge used to let either escape as a raw
exception, which reads to the caller as a credit problem even on a funded pay-as-you-go
account. These assert the split:
  * `apierrors` classifies each condition from the vendor's own status/code/message;
  * a transient throttle is retried inside a wall-clock budget, honouring Retry-After,
    and never retried when the sleep would push the tool call past its deadline;
  * a billing stop is NEVER retried and comes back as `gpt_quota_exhausted`;
  * an exhausted throttle comes back as retriable `gpt_rate_limited` carrying
    Retry-After — not as a traceback — with the session left clean for the retry.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import apierrors  # noqa: E402
import duet_run  # noqa: E402
import server as srv  # noqa: E402


# ---------------------- fakes ----------------------

class _FakeHeaders(dict):
    """Headers behave like the SDK's case-insensitive mapping for the keys we read."""

    def get(self, key, default=None):  # noqa: D102
        return super().get(key.lower(), default)


class _FakeHTTPResponse:
    def __init__(self, status_code: int, headers=None) -> None:
        self.status_code = status_code
        self.headers = _FakeHeaders(headers or {})


class _FakeAPIError(Exception):
    """Stands in for openai.APIStatusError / anthropic.APIStatusError."""

    def __init__(self, status_code: int, code: str = "", message: str = "",
                 headers=None) -> None:
        super().__init__(message or code or f"HTTP {status_code}")
        self.status_code = status_code
        self.response = _FakeHTTPResponse(status_code, headers)
        self.body = {"error": {"code": code, "message": message}} if (code or message) else None


def _rate_limited(retry_after: str | None = None) -> _FakeAPIError:
    headers = {"retry-after": retry_after} if retry_after else None
    return _FakeAPIError(429, "rate_limit_exceeded",
                         "Rate limit reached for gpt-6-astra in organization org-x on "
                         "tokens per min (TPM): Limit 30000, Used 29500.", headers)


def _out_of_credit() -> _FakeAPIError:
    return _FakeAPIError(429, "insufficient_quota",
                         "You exceeded your current quota, please check your plan and "
                         "billing details.")


class _FakeResponse:
    def __init__(self, output, output_text=None, status="completed") -> None:
        self.output = output
        self.output_text = output_text
        self.status = status


class _RecordingResponses:
    def __init__(self, scripted) -> None:
        self._scripted = list(scripted)
        self.calls = 0

    def create(self, **kwargs):
        item = self._scripted[self.calls]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


class _FakeClient:
    def __init__(self, scripted) -> None:
        self.responses = _RecordingResponses(scripted)


def _final_resp(score: int = 92) -> _FakeResponse:
    text = json.dumps({
        "role": "critic",
        "candidate_id": "cand-1",
        "counter_draft": None,
        "score_of_candidate": {"value": score, "rationale": "ok"},
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


# ---------------------- classification ----------------------

class Classification(unittest.TestCase):
    def test_openai_insufficient_quota_is_quota_not_throttle(self) -> None:
        exc = _out_of_credit()
        self.assertTrue(apierrors.is_quota_error(exc))
        self.assertFalse(apierrors.is_rate_limit_error(exc))
        self.assertFalse(apierrors.is_transient_error(exc))  # never worth a retry

    def test_openai_rate_limit_is_throttle_not_quota(self) -> None:
        exc = _rate_limited()
        self.assertFalse(apierrors.is_quota_error(exc))
        self.assertTrue(apierrors.is_rate_limit_error(exc))
        self.assertTrue(apierrors.is_transient_error(exc))

    def test_anthropic_low_credit_balance_is_quota_despite_400(self) -> None:
        # Anthropic reports the billing stop as a 400 invalid_request_error, so the
        # status alone cannot classify it — the message has to.
        exc = _FakeAPIError(400, "invalid_request_error",
                            "Your credit balance is too low to access the Anthropic API.")
        self.assertTrue(apierrors.is_quota_error(exc))
        self.assertFalse(apierrors.is_rate_limit_error(exc))

    def test_anthropic_overloaded_is_transient(self) -> None:
        exc = _FakeAPIError(529, "overloaded_error", "Overloaded")
        self.assertTrue(apierrors.is_transient_error(exc))
        self.assertFalse(apierrors.is_quota_error(exc))

    def test_non_vendor_exception_classifies_as_nothing(self) -> None:
        exc = ValueError("boom")
        self.assertIsNone(apierrors.status_of(exc))
        self.assertFalse(apierrors.is_quota_error(exc))
        self.assertFalse(apierrors.is_rate_limit_error(exc))
        self.assertFalse(apierrors.is_transient_error(exc))

    def test_retry_after_headers_parsed(self) -> None:
        self.assertEqual(apierrors.retry_after_seconds(_rate_limited("12")), 12.0)
        exc = _FakeAPIError(429, "rate_limit_exceeded", "slow down",
                            {"retry-after-ms": "2500"})
        self.assertEqual(apierrors.retry_after_seconds(exc), 2.5)
        self.assertIsNone(apierrors.retry_after_seconds(_rate_limited()))


# ---------------------- bounded retry ----------------------

class Backoff(unittest.TestCase):
    def test_retries_throttle_then_succeeds(self) -> None:
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] < 3:
                raise _rate_limited()
            return "ok"

        with mock.patch.object(apierrors.time, "sleep") as slept:
            self.assertEqual(
                apierrors.call_with_backoff(fn, attempts=2, base_delay=5), "ok")
        self.assertEqual(calls["n"], 3)
        self.assertEqual([c.args[0] for c in slept.call_args_list], [5.0, 10.0])

    def test_honours_retry_after_over_base_delay(self) -> None:
        with mock.patch.object(apierrors.time, "sleep") as slept:
            with self.assertRaises(_FakeAPIError):
                apierrors.call_with_backoff(
                    lambda: (_ for _ in ()).throw(_rate_limited("3")),
                    attempts=1, base_delay=30)
        self.assertEqual(slept.call_args_list[0].args[0], 3.0)

    def test_quota_error_is_never_retried(self) -> None:
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise _out_of_credit()

        with mock.patch.object(apierrors.time, "sleep") as slept:
            with self.assertRaises(_FakeAPIError):
                apierrors.call_with_backoff(fn, attempts=3, base_delay=1)
        self.assertEqual(calls["n"], 1)
        slept.assert_not_called()

    def test_retry_skipped_when_it_would_outrun_the_deadline(self) -> None:
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise _rate_limited("25")

        with mock.patch.object(apierrors.time, "sleep") as slept:
            with self.assertRaises(_FakeAPIError):
                # 25s of backoff + 20s headroom does not fit a 30s remaining budget.
                apierrors.call_with_backoff(
                    fn, attempts=3, base_delay=5,
                    deadline=apierrors.time.monotonic() + 30)
        self.assertEqual(calls["n"], 1)
        slept.assert_not_called()


# ---------------------- bridge behaviour ----------------------

class BridgeThrottling(unittest.TestCase):
    def test_throttle_is_retried_and_the_turn_still_completes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake = _install([_rate_limited("1"), _final_resp(91)], td)
            with mock.patch.object(apierrors.time, "sleep"):
                r = srv._start_turn_impl(None, "critic", "spec", "draft", "")
            self.assertEqual(r["status"], "final")
            self.assertEqual(fake.responses.calls, 2)

    def test_exhausted_throttle_returns_retriable_rate_limited(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake = _install([_rate_limited("7")] * (srv.DUET_RATE_LIMIT_RETRIES + 1), td)
            with mock.patch.object(apierrors.time, "sleep"):
                r = srv._start_turn_impl(None, "critic", "spec", "draft", "")
            self.assertEqual(r["status"], "error")
            self.assertEqual(r["payload"]["error"], "gpt_rate_limited")
            self.assertIs(r["payload"]["retriable"], True)
            self.assertEqual(r["payload"]["retry_after_s"], 7.0)
            self.assertEqual(r["payload"]["openai"]["code"], "rate_limit_exceeded")
            # The hint must not send the reader to a billing page.
            self.assertIn("not an out-of-credit stop", r["payload"]["hint"].lower())
            self.assertEqual(fake.responses.calls, srv.DUET_RATE_LIMIT_RETRIES + 1)
            # Session stays clean so the same turn can simply be retried.
            sess = srv._STORE.get(r["session_id"])
            self.assertIsNotNone(sess)
            self.assertIsNone(sess.pending_tool_use_id)

    def test_billing_stop_returns_non_retriable_quota_error_without_retrying(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake = _install([_out_of_credit()], td)
            with mock.patch.object(apierrors.time, "sleep") as slept:
                r = srv._start_turn_impl(None, "critic", "spec", "draft", "")
            self.assertEqual(r["status"], "error")
            self.assertEqual(r["payload"]["error"], "gpt_quota_exhausted")
            self.assertIs(r["payload"]["retriable"], False)
            self.assertEqual(r["payload"]["openai"]["status"], 429)
            self.assertEqual(r["payload"]["openai"]["code"], "insufficient_quota")
            self.assertIn("project", r["payload"]["hint"].lower())
            self.assertEqual(fake.responses.calls, 1)
            slept.assert_not_called()


class HealthProbe(unittest.TestCase):
    """`duet_health(probe=True)` must name which of the three failures is in play."""

    def _probe_raising(self, exc):
        class _Models:
            def retrieve(self, _model):
                raise exc

        class _Client:
            models = _Models()

        srv._openai_client = lambda: _Client()
        return srv._health_impl(probe=True)["probe"]

    def test_probe_omitted_unless_requested(self) -> None:
        self.assertNotIn("probe", srv._health_impl())

    def test_probe_reports_billing_stop(self) -> None:
        probe = self._probe_raising(_out_of_credit())
        self.assertFalse(probe["model_available"])
        self.assertTrue(probe["diagnosis"].startswith("billing:"))

    def test_probe_reports_throttle_as_not_a_credit_problem(self) -> None:
        probe = self._probe_raising(_rate_limited())
        self.assertTrue(probe["diagnosis"].startswith("throttled:"))
        self.assertIn("NOT a credit problem", probe["diagnosis"])

    def test_probe_reports_no_access_to_a_staged_rollout_model(self) -> None:
        probe = self._probe_raising(
            _FakeAPIError(404, "model_not_found",
                          "The model `gpt-6-astra` does not exist or you do not have "
                          "access to it."))
        self.assertTrue(probe["diagnosis"].startswith("access:"))
        self.assertEqual(probe["openai"]["status"], 404)

    def test_probe_reports_reachable_model(self) -> None:
        class _Models:
            def retrieve(self, _model):
                return {"id": srv.MODEL}

        class _Client:
            models = _Models()

        srv._openai_client = lambda: _Client()
        self.assertEqual(srv._health_impl(probe=True)["probe"], {"model_available": True})


class ServerSideLoopVendorErrors(unittest.TestCase):
    """duet_run (the headless server-side loop) maps the same two conditions per vendor."""

    def test_anthropic_billing_stop_names_anthropic_and_is_not_retriable(self) -> None:
        err = duet_run._vendor_failure(
            _FakeAPIError(400, "invalid_request_error",
                          "Your credit balance is too low to access the Anthropic API."),
            "anthropic",
        )
        self.assertEqual(err.payload["error"], "anthropic_quota_exhausted")
        self.assertIs(err.payload["retriable"], False)
        self.assertIn("ANTHROPIC_API_KEY", err.payload["hint"])

    def test_openai_throttle_is_retriable_and_carries_retry_after(self) -> None:
        err = duet_run._vendor_failure(_rate_limited("4"), "openai")
        self.assertEqual(err.payload["error"], "openai_rate_limited")
        self.assertIs(err.payload["retriable"], True)
        self.assertEqual(err.payload["retry_after_s"], 4.0)
        self.assertIn("not an out-of-credit stop", err.payload["hint"].lower())

    def test_run_duet_returns_the_payload_instead_of_raising(self) -> None:
        failure = duet_run._vendor_failure(_out_of_credit(), "openai")
        with mock.patch.object(duet_run, "_run_duet_inner", side_effect=failure):
            result = duet_run.run_duet("spec")
        self.assertEqual(result["error"], "openai_quota_exhausted")
        self.assertEqual(result["status"], "error")

    def test_call_model_maps_throttle_after_exhausting_retries(self) -> None:
        with mock.patch.object(apierrors.time, "sleep"):
            with self.assertRaises(duet_run.DuetVendorError) as ctx:
                duet_run._call_model(
                    lambda: (_ for _ in ()).throw(_rate_limited("2")), "openai")
        self.assertEqual(ctx.exception.payload["error"], "openai_rate_limited")

    def test_call_model_lets_unrelated_exceptions_propagate(self) -> None:
        with self.assertRaises(ValueError):
            duet_run._call_model(lambda: (_ for _ in ()).throw(ValueError("boom")), "openai")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
