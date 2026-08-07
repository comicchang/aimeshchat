"""SQLite session registry with per-key file locking.

Storage: $XDG_STATE_HOME/codeagent/sessions.sqlite3 (fallback ~/.local/state/…)
Schema:  sessions(key TEXT PK, session_id, backend, host, workdir,
                   agent, model, topic, status, created_at, updated_at)

State machine
-------------
absent → starting → observed(id) → active | failed | interrupted

- "starting" is written immediately on spawn.
- "observed" is written once the runner captures a session_id.
- Terminal state depends on exit: 0 → active, non-zero → failed, signal → interrupted.

Concurrency
-----------
Each key gets a separate flock file (see lock.py).  The DB itself uses
WAL mode; all writes happen inside the lock so the transaction is short.
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Generator, Optional, TypeVar

from codeagent.domain import LOCAL_HOST_MARKER, RunRequest, RunResult, SessionRecord, Target
from codeagent.session.key import compute_session_key
from codeagent.session.lock import SessionLock

T = TypeVar("T")


def _state_dir() -> Path:
    """$XDG_STATE_HOME/codeagent, defaulting to ~/.local/state/codeagent."""
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "codeagent"


_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS sessions (
    key         TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL DEFAULT '',
    backend     TEXT NOT NULL DEFAULT '',
    host        TEXT NOT NULL DEFAULT '',
    workdir     TEXT NOT NULL DEFAULT '',
    agent       TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL DEFAULT '',
    topic       TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'starting',
    created_at  REAL NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL DEFAULT 0
)
"""

_COLUMNS = (
    "key", "session_id", "backend", "host", "workdir",
    "agent", "model", "topic", "status", "created_at", "updated_at",
)


def _row_to_record(row) -> SessionRecord:
    """Convert a sqlite3.Row or tuple to SessionRecord."""
    if hasattr(row, 'keys'):
        # sqlite3.Row — access by column name
        return SessionRecord(**{col: row[col] for col in _COLUMNS})
    # Fallback for tuple rows (shouldn't happen with row_factory set)
    return SessionRecord(**dict(zip(_COLUMNS, row)))


class SessionRegistry:
    """SQLite-backed session registry with per-key flocking.

    Auto-resume behaviour (the default):
        ``lookup(key)`` returns the record; the caller checks ``status``
        and, if "active", passes the stored ``session_id`` back to the
        runner.

    ``--new-session``  →  caller skips lookup, calls ``mark_starting(key, …)``
    ``--no-auto-resume``  →  caller skips lookup, but still records the result.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = db_path or _state_dir() / "sessions.sqlite3"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # -- Schema bootstrap ------------------------------------------------------

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(_CREATE_TABLE)

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self._db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=3000")
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- Public API ------------------------------------------------------------

    def compute_key(self, request: RunRequest, target: Target) -> str:
        """Delegate to the pure key-computation module."""
        return compute_session_key(request, target)

    def lookup(self, key: str) -> Optional[SessionRecord]:
        """Return the record for *key*, or None if no session exists."""
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM sessions WHERE key = ?",
                (key,),
            ).fetchone()
        return _row_to_record(row) if row else None

    def mark_starting(self, key: str, request: RunRequest, target: Target, *, clear_session: bool = False) -> None:
        """Write a "starting" record for *key*.

        Called immediately when a new session is being spawned (i.e.
        ``--new-session`` or no existing record found).  Holds
        SessionLock("session:" + key) to prevent races with concurrent spawns.

        If *clear_session* is True, the old session_id is discarded
        (used with ``--new-session`` to avoid resuming a stale session).
        """
        now = time.time()
        backend = (request.backend or "opencode").lower()
        host = target.ssh_alias if not target.is_local else LOCAL_HOST_MARKER
        workdir = target.workdir or request.workdir
        agent = request.agent or ""
        model = request.model or ""
        topic = request.topic or ""

        with SessionLock("session:" + key), self._connect() as conn:
            if clear_session:
                # Force-clear old session_id for --new-session
                conn.execute(
                    """\
                    INSERT INTO sessions
                        (key, session_id, backend, host, workdir, agent, model,
                         topic, status, created_at, updated_at)
                    VALUES (?, '', ?, ?, ?, ?, ?, ?, 'starting', ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        session_id = '',
                        backend    = excluded.backend,
                        host       = excluded.host,
                        workdir    = excluded.workdir,
                        agent      = excluded.agent,
                        model      = excluded.model,
                        topic      = excluded.topic,
                        status     = 'starting',
                        updated_at = excluded.updated_at
                    """,
                    (key, backend, host, workdir, agent, model, topic, now, now),
                )
            else:
                conn.execute(
                    """\
                    INSERT INTO sessions
                        (key, session_id, backend, host, workdir, agent, model,
                         topic, status, created_at, updated_at)
                    VALUES (?, '', ?, ?, ?, ?, ?, ?, 'starting', ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        session_id = COALESCE(NULLIF(excluded.session_id, ''), sessions.session_id),
                        backend    = excluded.backend,
                        host       = excluded.host,
                        workdir    = excluded.workdir,
                        agent      = excluded.agent,
                        model      = excluded.model,
                        topic      = excluded.topic,
                        status     = 'starting',
                        updated_at = excluded.updated_at
                    """,
                    (key, backend, host, workdir, agent, model, topic, now, now),
                )

    def mark_observed(self, key: str, session_id: str) -> None:
        """Transition to "observed" once the runner has captured the session id."""
        now = time.time()
        with SessionLock("session:" + key), self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET session_id = ?, status = 'observed', updated_at = ? "
                "WHERE key = ?",
                (session_id, now, key),
            )

    def mark_active(self, key: str) -> None:
        """Transition to "active" — runner confirmed the session is running."""
        now = time.time()
        with SessionLock("session:" + key), self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET status = 'active', updated_at = ? "
                "WHERE key = ?",
                (now, key),
            )

    def mark_failed(self, key: str) -> None:
        """Transition to "failed" — runner reported an error."""
        now = time.time()
        with SessionLock("session:" + key), self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET status = 'failed', updated_at = ? "
                "WHERE key = ?",
                (now, key),
            )

    def upsert(
        self,
        key: str,
        result: RunResult,
        request: RunRequest,
        target: Target,
    ) -> SessionRecord:
        """Insert or update the record after a run completes.

        Transitions to the appropriate terminal state based on exit code / signal.
        """
        now = time.time()
        backend = (request.backend or result.backend or "opencode").lower()
        host = target.ssh_alias if not target.is_local else LOCAL_HOST_MARKER
        workdir = target.workdir or request.workdir or result.workdir
        agent = request.agent or ""
        model = request.model or ""
        topic = request.topic or ""
        session_id = result.session_id or ""

        # Determine terminal status.
        if result.returncode == 0:
            status = "active"
        elif result.returncode < 0:
            status = "interrupted"
        else:
            status = "failed"

        with SessionLock("session:" + key), self._connect() as conn:
            existing = conn.execute(
                "SELECT created_at FROM sessions WHERE key = ?", (key,)
            ).fetchone()
            created = existing[0] if existing else now

            conn.execute(
                """\
                INSERT INTO sessions
                    (key, session_id, backend, host, workdir, agent, model,
                     topic, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    session_id = excluded.session_id,
                    backend    = excluded.backend,
                    host       = excluded.host,
                    workdir    = excluded.workdir,
                    agent      = excluded.agent,
                    model      = excluded.model,
                    topic      = excluded.topic,
                    status     = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (key, session_id, backend, host, workdir, agent, model,
                 topic, status, created, now),
            )
        return SessionRecord(
            key=key,
            session_id=session_id,
            backend=backend,
            host=host,
            workdir=workdir,
            agent=agent,
            model=model,
            topic=topic,
            status=status,
            created_at=created,
            updated_at=now,
        )

    def delete(self, key: str) -> bool:
        """Delete the record for *key*.  Returns True if a row was removed."""
        with SessionLock("session:" + key), self._connect() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE key = ?", (key,))
            return cur.rowcount > 0

    def bind(self, key: str, session_id: str) -> None:
        """Manually bind a session_id to a key (``codeagent sessions bind``)."""
        now = time.time()
        with SessionLock("session:" + key), self._connect() as conn:
            cur = conn.execute(
                "UPDATE sessions SET session_id = ?, status = 'active', updated_at = ? "
                "WHERE key = ?",
                (session_id, now, key),
            )
            if cur.rowcount == 0:
                conn.execute(
                    """\
                    INSERT INTO sessions
                        (key, session_id, backend, host, workdir, agent, model,
                         topic, status, created_at, updated_at)
                    VALUES (?, ?, '', '', '', '', '', '', 'active', ?, ?)
                    """,
                    (key, session_id, now, now),
                )

    def list_all(
        self,
        *,
        host: Optional[str] = None,
        topic: Optional[str] = None,
    ) -> list[SessionRecord]:
        """List all records, optionally filtered by host and/or topic."""
        clauses: list[str] = []
        params: list[str] = []
        if host:
            clauses.append("host = ?")
            params.append(host)
        if topic:
            clauses.append("topic = ?")
            params.append(topic)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT {', '.join(_COLUMNS)} FROM sessions{where} ORDER BY updated_at DESC"

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_record(r) for r in rows]

    def cleanup_stale(self, max_age_seconds: float = 86400) -> int:
        """Remove sessions stuck in 'starting' or 'observed' for too long.

        Returns the number of rows deleted.  Holds per-key locks for
        each stale session to avoid racing with active operations.
        """
        cutoff = time.time() - max_age_seconds
        # First, collect the keys to delete
        with self._connect() as conn:
            stale_keys = [
                row[0] for row in conn.execute(
                    "SELECT key FROM sessions WHERE status IN ('starting', 'observed') "
                    "AND updated_at < ?",
                    (cutoff,),
                ).fetchall()
            ]

        deleted = 0
        # Delete each under its own lock
        for key in stale_keys:
            with SessionLock("session:" + key), self._connect() as conn:
                cur = conn.execute(
                    "DELETE FROM sessions WHERE key = ? AND status IN ('starting', 'observed') "
                    "AND updated_at < ?",
                    (key, cutoff),
                )
                deleted += cur.rowcount

        return deleted

    @contextmanager
    def run_with_lock(self, key: str) -> Generator[SessionLock, None, None]:
        """Hold SessionLock("session:" + key) for the entire execution turn.

        Use this when the caller needs to perform multiple registry
        operations atomically under a single lock:

            with registry.run_with_lock(key) as lock:
                # multiple reads/writes here
                pass
        """
        with SessionLock("session:" + key) as lock:
            yield lock
