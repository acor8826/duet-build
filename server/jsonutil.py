"""Tolerant JSON extraction helpers shared by the duet bridge and full-loop.

Both the suspend/resume bridge (`server.py`) and the server-side loop
(`duet_run.py`) need to recover a JSON object from model output that may be
wrapped in markdown fences or surrounded by prose. Keeping the helpers here
avoids duplication and lets `server.py` parse final WorkProducts tolerantly when
`response_format` is left unconstrained (so GPT can still emit tool calls).
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict


def strip_fences(text: str) -> str:
    """Strip a leading/trailing ```lang fence from a model response, if present."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9]*\n", "", t)
        t = re.sub(r"\n```\s*$", "", t)
    return t.strip()


def json_obj(text: str) -> Dict[str, Any]:
    """Best-effort parse of a single JSON object from possibly-fenced/prose text."""
    t = strip_fences(text)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {}


# Backwards-compatible private aliases (these helpers originated in duet_run.py).
_strip_fences = strip_fences
_json_obj = json_obj
