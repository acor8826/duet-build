"""In-memory + on-disk (+ optional GCS) session store for the duet bridge.

Each GPT-side coroutine is keyed by session_id. The conversation history (OpenAI
Responses-API input item list, reasoning items included) and pending tool-call
metadata is persisted after every step so a restart can resume mid-iteration.

On Cloud Run the disk tier lives on /tmp, which dies with the instance — a long
orchestrator-side research pause (minutes to hours between suspend and resume)
can outlive the instance and lose the suspended session. Setting
DUET_STATE_GCS_BUCKET adds a write-through GCS tier: puts upload the session
JSON, and a get that misses memory and disk falls back to the bucket, so
suspended turns survive instance recycling and scale-to-zero.
"""
from __future__ import annotations

import json
import os
import sys
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
    history: List[Dict[str, Any]] = field(default_factory=list)  # Responses-API input item list
    pending_tool_use_id: Optional[str] = None
    pending_tool_name: Optional[str] = None
    pending_tool_args: Optional[Dict[str, Any]] = None
    last_final: Optional[Dict[str, Any]] = None
    closed: bool = False
    # Document exchange (push + multi-step pull). All default-valued so older
    # on-disk session JSON (written before these fields existed) still loads via
    # from_dict / cls(**d).
    documents: List[Dict[str, Any]] = field(default_factory=list)  # full text pushed to GPT at start
    available_documents: List[Dict[str, Any]] = field(default_factory=list)  # catalog GPT may pull from
    doc_requests_made: int = 0  # count of request_document tool calls this turn (budget guard)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Session":
        return cls(**d)


class SessionStore:
    def __init__(self, state_dir: str, gcs_bucket: Optional[str] = None):
        self.state_dir = Path(state_dir) / "sessions"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, Session] = {}
        self.gcs_bucket = (
            gcs_bucket if gcs_bucket is not None
            else os.environ.get("DUET_STATE_GCS_BUCKET") or None
        )
        self._bucket = None  # lazy google-cloud-storage handle

    def _path(self, session_id: str) -> Path:
        return self.state_dir / f"{session_id}.json"

    def _gcs(self):
        """Lazy bucket handle; import deferred so the dependency is optional."""
        if not self.gcs_bucket:
            return None
        if self._bucket is None:
            from google.cloud import storage  # noqa: PLC0415
            self._bucket = storage.Client().bucket(self.gcs_bucket)
        return self._bucket

    def _blob_name(self, session_id: str) -> str:
        return f"sessions/{session_id}.json"

    def get(self, session_id: str) -> Optional[Session]:
        with _LOCK:
            if session_id in self._cache:
                return self._cache[session_id]
            p = self._path(session_id)
            data = None
            if p.exists():
                with p.open("r", encoding="utf-8") as f:
                    data = json.load(f)
            elif self.gcs_bucket:
                # Disk miss — the instance may have been recycled during a long
                # orchestrator-side pause; fall back to the durable tier.
                try:
                    blob = self._gcs().blob(self._blob_name(session_id))
                    if blob.exists():
                        data = json.loads(blob.download_as_text())
                except Exception as e:  # durable tier is best-effort on reads too
                    print(f"[duet-state] GCS read failed for {session_id}: {e}",
                          file=sys.stderr)
            if data is None:
                return None
            s = Session.from_dict(data)
            self._cache[session_id] = s
            return s

    def put(self, session: Session) -> None:
        with _LOCK:
            self._cache[session.session_id] = session
            p = self._path(session.session_id)
            payload = json.dumps(session.to_dict(), indent=2)
            # Atomic write: tmp file in same dir, then os.replace.
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{session.session_id}.",
                suffix=".json.tmp",
                dir=str(self.state_dir),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                os.replace(tmp_name, p)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
            if self.gcs_bucket:
                # Write-through to the durable tier. Best-effort: a GCS hiccup
                # must not fail the turn (disk+memory still hold the session).
                try:
                    self._gcs().blob(self._blob_name(session.session_id)) \
                        .upload_from_string(payload, content_type="application/json")
                except Exception as e:
                    print(f"[duet-state] GCS write failed for "
                          f"{session.session_id}: {e}", file=sys.stderr)

    def delete(self, session_id: str) -> None:
        with _LOCK:
            self._cache.pop(session_id, None)
            p = self._path(session_id)
            if p.exists():
                p.unlink()
            if self.gcs_bucket:
                try:
                    self._gcs().blob(self._blob_name(session_id)).delete()
                except Exception:
                    pass  # absent blob or transient error — nothing to do
