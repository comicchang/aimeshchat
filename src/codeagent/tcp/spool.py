"""Durable spool for TCP mailbox forwarding.

Write-ahead log: messages are persisted to local filesystem BEFORE
being sent over TCP. On crash recovery, unsent messages are replayed.

Directory layout:
  $MAILBOX_ROOT/.spool/<session_id>/<host_alias>/
    <uuid>.json          # pending message
    <uuid>.json.acked    # delivered (ACK received)
    <uuid>.json.failed   # delivery failed (exhausted retries)
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from codeagent.constants import SPOOL_MAX_RETRIES, SPOOL_TTL_SECONDS

logger = logging.getLogger(__name__)

# ── extensions ──────────────────────────────────────────────────────────
_PENDING = ".json"
_ACKED = ".json.acked"
_FAILED = ".json.failed"


# ── data model ──────────────────────────────────────────────────────────


@dataclass
class SpoolEntry:
    """One pending/acked/failed message in the spool."""

    uuid: str
    session_id: str
    from_id: str
    to_id: str
    msg_id: str
    payload: dict
    created_at: float
    host_alias: str
    attempts: int = 0
    status: str = "pending"  # pending / acked / failed

    # ── serialisation ───────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SpoolEntry":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "SpoolEntry":
        return cls.from_dict(json.loads(text))


# ── store ───────────────────────────────────────────────────────────────


class SpoolStore:
    """Filesystem-backed WAL spool for durable TCP forwarding.

    Each entry is a single JSON file.  Atomic writes use the same
    O_EXCL + fsync + os.replace pattern as MailboxStore.append_history.
    """

    def __init__(self, root: Path) -> None:
        self._root = root / ".spool"

    # ── helpers ─────────────────────────────────────────────────────────

    def _host_dir(self, session_id: str, host_alias: str) -> Path:
        d = self._root / session_id / host_alias
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _stem_for(entry: SpoolEntry) -> str:
        """Return the base filename stem (without extension)."""
        return entry.uuid

    @staticmethod
    def _status_for(path: Path) -> str:
        name = path.name
        if name.endswith(_ACKED):
            return "acked"
        if name.endswith(_FAILED):
            return "failed"
        return "pending"

    @classmethod
    def _load_entry(cls, path: Path) -> Optional[SpoolEntry]:
        """Load a single entry file; return None on corrupt/unreadable."""
        try:
            text = path.read_text(encoding="utf-8")
            entry = SpoolEntry.from_json(text)
            entry.status = cls._status_for(path)
            return entry
        except Exception:
            logger.warning("corrupt spool entry skipped: %s", path, exc_info=True)
            return None

    # ── public API ──────────────────────────────────────────────────────

    def write(self, entry: SpoolEntry) -> Path:
        """Persist *entry* durably and return the file path.

        Uses O_EXCL + fsync + os.replace for crash safety.
        """
        host_dir = self._host_dir(entry.session_id, entry.host_alias)
        stem = self._stem_for(entry)
        dest = host_dir / f"{stem}{_PENDING}"
        tmp = host_dir / f".tmp-{stem}{_PENDING}"

        with open(tmp, "x") as f:  # O_EXCL — fail if tmp left over
            f.write(entry.to_json())
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(dest))
        return dest

    def ack(self, spool_uuid: str, session_id: str, host_alias: str) -> Path:
        """Mark an entry as delivered (rename .json → .json.acked)."""
        host_dir = self._host_dir(session_id, host_alias)
        src = host_dir / f"{spool_uuid}{_PENDING}"
        dst = host_dir / f"{spool_uuid}{_ACKED}"
        if not src.exists():
            raise FileNotFoundError(f"pending entry not found: {src}")
        os.replace(str(src), str(dst))
        return dst

    def fail(self, spool_uuid: str, session_id: str, host_alias: str) -> Path:
        """Mark an entry as failed (rename .json → .json.failed)."""
        host_dir = self._host_dir(session_id, host_alias)
        src = host_dir / f"{spool_uuid}{_PENDING}"
        dst = host_dir / f"{spool_uuid}{_FAILED}"
        if not src.exists():
            raise FileNotFoundError(f"pending entry not found: {src}")
        os.replace(str(src), str(dst))
        return dst

    def pending(self, session_id: str, host_alias: str) -> list[SpoolEntry]:
        """Return all pending entries for a (session, host) pair."""
        host_dir = self._host_dir(session_id, host_alias)
        results: list[SpoolEntry] = []
        for p in sorted(host_dir.iterdir()):
            if p.suffix == ".json" and not p.name.startswith(".tmp-"):
                entry = self._load_entry(p)
                if entry is not None:
                    results.append(entry)
        return results

    def replay(self) -> list[SpoolEntry]:
        """Recover all pending entries across every session/host.

        Intended for call once after process restart.  Returns entries
        sorted by creation time (oldest first).
        """
        results: list[SpoolEntry] = []
        if not self._root.exists():
            return results
        for session_dir in self._root.iterdir():
            if not session_dir.is_dir():
                continue
            for host_dir in session_dir.iterdir():
                if not host_dir.is_dir():
                    continue
                for p in sorted(host_dir.iterdir()):
                    if p.suffix == ".json" and not p.name.startswith(".tmp-"):
                        entry = self._load_entry(p)
                        if entry is not None:
                            results.append(entry)
        results.sort(key=lambda e: e.created_at)
        return results

    def cleanup(self, max_age_seconds: int = SPOOL_TTL_SECONDS) -> int:
        """Remove acked/failed entries older than *max_age_seconds*.

        Returns the number of files deleted.
        """
        now = time.time()
        removed = 0
        if not self._root.exists():
            return 0
        for session_dir in self._root.iterdir():
            if not session_dir.is_dir():
                continue
            for host_dir in session_dir.iterdir():
                if not host_dir.is_dir():
                    continue
                for p in host_dir.iterdir():
                    if p.name.startswith(".tmp-"):
                        continue
                    suffix = p.suffix  # e.g. ".acked"
                    full_suffix = p.name[p.name.index(".") :]  # ".json.acked"
                    if full_suffix not in (_ACKED, _FAILED):
                        continue
                    try:
                        age = now - p.stat().st_mtime
                        if age > max_age_seconds:
                            p.unlink()
                            removed += 1
                    except OSError:
                        continue
        return removed
