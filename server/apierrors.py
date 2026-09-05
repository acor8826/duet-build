"""Vendor-error classification and bounded retry, shared by the bridge
(`server.py`) and the server-side loop (`duet_run.py`).

Why this module exists: both vendors answer 429 for two unrelated conditions —
the account is out of money (terminal) and the account is over its per-minute
tier limit (transient, clears in seconds). With `max_retries=0` on both SDK
clients (deliberate: SDK-level retries would multiply wall-clock and blow the
MCP client's ~180s tool-call cap) a raw 429 escaped as an opaque exception, and
every one of them reads to the caller like "out of credit". That sends you to
the billing page when a funded pay-as-you-go account is simply being throttled.

These helpers split the two apart by the vendor's own error code, so a
transient throttle can be retried inside the time budget and a genuine billing
stop can be reported as exactly that.

OpenAI codes: ``insufficient_quota`` / ``billing_hard_limit_reached`` are
terminal; ``rate_limit_exceeded`` is transient. Anthropic answers 400
``invalid_request_error`` with "credit balance is too low" for the terminal
case, 429 ``rate_limit_error`` and 529 ``overloaded_error`` for transient ones.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional, TypeVar

T = TypeVar("T")

# Longest single backoff we will sit through. A Retry-After longer than this
# means the tier limit will not clear inside one tool call, so we surface the
# throttle to the caller instead of burning the window waiting.
MAX_BACKOFF_S = 30.0

# Codes that mean "this account cannot spend money right now" on either vendor.
_QUOTA_CODES = frozenset({
    "insufficient_quota",
    "billing_hard_limit_reached",
    "billing_not_active",
    "account_deactivated",
})
# Substrings that identify the same condition when the code is absent or unknown
# (Anthropic reports it as a plain invalid_request_error).
_QUOTA_MARKERS = (
    "credit balance is too low",
    "exceeded your current quota",
    "billing hard limit",
    "insufficient_quota",
    "insufficient funds",
    "purchase credits",
)
_RATE_LIMIT_CODES = frozenset({"rate_limit_exceeded", "rate_limit_error"})
_OVERLOADED_CODES = frozenset({"overloaded_error", "server_error", "api_error"})
_RETRIABLE_STATUSES = frozenset({408, 409, 500, 502, 503, 504, 529})


# ---------------------- shape probes ----------------------

def status_of(exc: Exception) -> Optional[int]:
    """HTTP status carried by an OpenAI/Anthropic APIStatusError, else None.

    A None here is the signal that the exception is not a vendor HTTP error at
    all (a local ValueError, say), which callers use to let it propagate.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def error_code(exc: Exception) -> str:
    """Vendor error code/type from the response body, lowercased ('' if absent).

    Both SDKs expose the parsed body on `.body`; OpenAI nests it under "error",
    Anthropic under "error" as well, and either may put the discriminator in
    "code" or in "type".
    """
    body: Any = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        err = err if isinstance(err, dict) else body
        for key in ("code", "type"):
            value = err.get(key)
            if isinstance(value, str) and value:
                return value.lower()
    code = getattr(exc, "code", None)
    return code.lower() if isinstance(code, str) else ""


def error_message(exc: Exception) -> str:
    """Human-readable vendor message, falling back to str(exc)."""
    body: Any = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        err = err if isinstance(err, dict) else body
        message = err.get("message")
        if isinstance(message, str) and message:
            return message
    return str(exc)


# ---------------------- classification ----------------------

def is_timeout_error(exc: Exception) -> bool:
    """True for OpenAI/Anthropic timeout / connection errors.

    Matched by type where the SDK is importable, else by class name so a test
    double (or a vendor SDK we did not import) is still recognised.
    """
    for module in ("openai", "anthropic"):
        try:
            mod = __import__(module)
            if isinstance(exc, (mod.APITimeoutError, mod.APIConnectionError)):
                return True
        except Exception:  # pragma: no cover - SDKs are present in practice
            pass
    name = type(exc).__name__
    return "Timeout" in name or "APIConnectionError" in name


def is_quota_error(exc: Exception) -> bool:
    """True when the account is out of credit / past a hard billing limit.

    Terminal: no amount of backoff fixes it, so callers must not retry.
    """
    if status_of(exc) is None:
        return False
    if error_code(exc) in _QUOTA_CODES:
        return True
    message = error_message(exc).lower()
    return any(marker in message for marker in _QUOTA_MARKERS)


def is_rate_limit_error(exc: Exception) -> bool:
    """True for a per-minute/per-tier throttle — a funded account going too fast."""
    if is_quota_error(exc):
        return False
    if error_code(exc) in _RATE_LIMIT_CODES:
        return True
    return status_of(exc) == 429


def is_transient_error(exc: Exception) -> bool:
    """True when a short backoff is worth spending on this error."""
    if is_quota_error(exc):
        return False
    if is_rate_limit_error(exc):
        return True
    status = status_of(exc)
    if status is not None and status in _RETRIABLE_STATUSES:
        return True
    return status is not None and error_code(exc) in _OVERLOADED_CODES


def retry_after_seconds(exc: Exception) -> Optional[float]:
    """The vendor's own Retry-After for this error, in seconds, if it sent one."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        raw_ms = headers.get("retry-after-ms")
        if raw_ms:
            return float(raw_ms) / 1000.0
        raw = headers.get("retry-after")
        if raw:
            return float(raw)
    except (TypeError, ValueError):
        return None
    return None


def error_summary(exc: Exception) -> str:
    """One-line `status=… code=…` summary of a vendor error, for a log line."""
    return f"status={status_of(exc)} code={error_code(exc) or '?'}"


def describe(exc: Exception, vendor: str) -> Dict[str, Any]:
    """Compact, JSON-safe description of a vendor error for an error payload."""
    return {
        "vendor": vendor,
        "status": status_of(exc),
        "code": error_code(exc) or None,
        "detail": error_message(exc)[:500],
    }


# ---------------------- bounded retry ----------------------

def call_with_backoff(
    fn: Callable[[], T],
    *,
    attempts: int,
    base_delay: float,
    deadline: Optional[float] = None,
    min_headroom: float = 20.0,
    log: Optional[Callable[[str], None]] = None,
) -> T:
    """Call ``fn``, retrying transient vendor errors while the budget allows.

    ``attempts`` is the number of RETRIES (0 = call once, never retry). Backoff
    honours the vendor's Retry-After when present, else doubles ``base_delay``.
    A retry is skipped — and the error raised — when the sleep plus
    ``min_headroom`` would push past ``deadline`` (a `time.monotonic()` stamp),
    so a retry can never be the reason a call overruns the MCP client's window.
    Quota errors are never retried; non-vendor exceptions propagate untouched.
    """
    for attempt in range(attempts + 1):
        try:
            return fn()
        except Exception as e:
            if attempt >= attempts or not is_transient_error(e):
                raise
            delay = retry_after_seconds(e)
            if delay is None:
                delay = base_delay * (2 ** attempt)
            delay = max(0.0, min(delay, MAX_BACKOFF_S))
            if deadline is not None and time.monotonic() + delay + min_headroom > deadline:
                raise
            if log is not None:
                log(
                    f"transient {type(e).__name__} "
                    f"(status={status_of(e)}, code={error_code(e) or '?'}); "
                    f"retry {attempt + 1}/{attempts} in {delay:.1f}s"
                )
            time.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
