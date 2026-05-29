"""In-memory + on-disk session store for the duet bridge.

Each GPT-side coroutine is keyed by session_id. The conversation history (OpenAI
message list) and pending tool-call metadata is persisted after every step so a
restart can resume mid-iteration.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional


_LOCK = threading.RLock()


@dataclass
class Session:
    session_id: str
    role: str  # counter_drafter | critic | verifier | roster_proposer
    spec: str
    history: List[Dict[str, Any]] = field(default_factory=list)  # OpenAI message list
    pending_tool_use_id: Optional[str] = None
    pending_tool_name: Optional[str] = None
    pending_tool_args: Optional[Dict[str, Any]] = None
    last_final: Optional[Dict[str, Any]] = None
    closed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Session":
        return cls(**d)


class SessionStore:
    def __init__(self, state_dir: str):
        self.state_dir = Path(state_dir) / "sessions"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, Session] = {}

    def _path(self, session_id: str) -> Path:
        return self.state_dir / f"{session_id}.json"

    def get(self, session_id: str) -> Optional[Session]:
        with _LOCK:
            if session_id in self._cache:
                return self._cache[session_id]
            p = self._path(session_id)
            if not p.exists():
                return None
            with p.open("r", encoding="utf-8") as f:
                data = json.load(f)
            s = Session.from_dict(data)
            self._cache[session_id] = s
            return s

    def put(self, session: Session) -> None:
        with _LOCK:
            self._cache[session.session_id] = session
            p = self._path(session.session_id)
            # Atomic write: tmp file in same dir, then os.replace.
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{session.session_id}.",
                suffix=".json.tmp",
                dir=str(self.state_dir),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(session.to_dict(), f, indent=2)
                os.replace(tmp_name, p)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise

    def delete(self, session_id: str) -> None:
        with _LOCK:
            self._cache.pop(session_id, None)
            p = self._path(session_id)
            if p.exists():
                p.unlink()
