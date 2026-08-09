"""EventStore — SQLite (WAL) event library for runtime/UI/stat observability.

Persistence contract:
  - ``append_local(draft)`` assigns the per-(source_host, runtime_id,
    generation) ``source_sequence`` inside one transaction. Producers
    (plugin / supervisor) never number their own events.
  - ``ingest_remote(event)`` keeps the remote ``source_sequence`` and is
    idempotent on the 4-tuple (source_host, runtime_id, generation,
    source_sequence) — crash replay only ACKs, never double-appends.
  - ``list_after(cursor, filters, limit)`` returns events after the local
    monotonic ``event_id`` cursor.

Retention: tool update details are pruned after 7 days by ``sweep()``;
terminal/receipt/error events are retained past runtime release.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator, Optional

from codeagent.gateway.model import (
    EVENT_KIND_TOOL_UPDATE,
    EVENT_KINDS,
    RuntimeEvent,
    RuntimeEventDraft,
)

log = logging.getLogger(__name__)

TOOL_UPDATE_RETENTION_DAYS = 7


def gateway_state_dir() -> Path:
    """$XDG_DATA_HOME/codeagent/gateway — control socket + event db live here."""
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "codeagent" / "gateway"


def control_socket_path() -> Path:
    return gateway_state_dir() / "control.sock"


def events_db_path() -> Path:
    return gateway_state_dir() / "events.sqlite3"


_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS runtime_events (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_host     TEXT NOT NULL,
    runtime_id      TEXT NOT NULL,
    generation      INTEGER NOT NULL,
    source_sequence INTEGER NOT NULL,
    session_id      TEXT NOT NULL DEFAULT '',
    agent_id        TEXT NOT NULL DEFAULT '',
    request_id      TEXT NOT NULL DEFAULT '',
    run_id          TEXT NOT NULL DEFAULT '',
    kind            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    payload         TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source_host, runtime_id, generation, source_sequence)
)
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_events_cursor ON runtime_events(event_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_kind ON runtime_events(kind)",
    "CREATE INDEX IF NOT EXISTS idx_events_runtime ON runtime_events(runtime_id, generation)",
)


class EventStore:
    """Append-only SQLite event library with per-source sequencing."""

    def __init__(self, db_path: Optional[Path] = None, source_host: str = "") -> None:
        self._db_path = db_path or events_db_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # Directory 0700, db 0600 — events can contain payload details.
        try:
            os.chmod(self._db_path.parent, 0o700)
        except OSError:
            pass
        self._source_host = source_host or _local_hostname()
        self._init_db()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self._db_path), timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(_CREATE_TABLE)
            for idx in _INDEXES:
                conn.execute(idx)

    # ── local append ───────────────────────────────────────────────────

    def append_local(self, draft: RuntimeEventDraft) -> RuntimeEvent:
        """Append a locally-produced event, assigning source_sequence.

        The sequence is the max+1 within (source_host, runtime_id,
        generation) — producers must NOT number their own events.
        BEGIN IMMEDIATE takes the write lock before SELECT MAX so two
        concurrent appends for the same runtime cannot compute the same
        sequence (UNIQUE race).
        """
        draft.validate()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT COALESCE(MAX(source_sequence), 0) FROM runtime_events "
                    "WHERE source_host = ? AND runtime_id = ? AND generation = ?",
                    (self._source_host, draft.runtime_id, draft.generation),
                ).fetchone()
                seq = int(row[0]) + 1
                cur = conn.execute(
                    "INSERT INTO runtime_events "
                    "(source_host, runtime_id, generation, source_sequence, session_id, "
                    " agent_id, request_id, run_id, kind, created_at, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._source_host, draft.runtime_id, draft.generation, seq,
                        draft.session_id, draft.agent_id, draft.request_id, draft.run_id,
                        draft.kind, draft.created_at,
                        json.dumps(draft.payload, ensure_ascii=False),
                    ),
                )
                event_id = int(cur.lastrowid)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return self._row_to_event(self._fetch_by_id(event_id))

    # ── remote ingest ──────────────────────────────────────────────────

    def ingest_remote(self, event: dict) -> RuntimeEvent:
        """Ingest a remotely-produced event, keeping ITS source_sequence.

        Idempotent on (source_host, runtime_id, generation, source_sequence):
        a duplicate insert is a no-op returning the existing row.
        """
        host = event.get("source_host", "")
        runtime_id = event.get("runtime_id", "")
        generation = int(event.get("generation", 0))
        seq = int(event.get("source_sequence", 0))
        if not host or not runtime_id:
            raise ValueError("ingest_remote requires source_host + runtime_id")
        if seq <= 0:
            raise ValueError("ingest_remote requires source_sequence > 0")
        kind = event.get("kind", "")
        if kind not in EVENT_KINDS:
            raise ValueError(f"invalid event kind {kind!r}")

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT event_id FROM runtime_events "
                "WHERE source_host = ? AND runtime_id = ? AND generation = ? AND source_sequence = ?",
                (host, runtime_id, generation, seq),
            ).fetchone()
            if existing is not None:
                return self._row_to_event(self._fetch_by_id(int(existing[0])))
            cur = conn.execute(
                "INSERT OR IGNORE INTO runtime_events "
                "(source_host, runtime_id, generation, source_sequence, session_id, "
                " agent_id, request_id, run_id, kind, created_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    host, runtime_id, generation, seq,
                    event.get("session_id", ""), event.get("agent_id", ""),
                    event.get("request_id", ""), event.get("run_id", ""),
                    kind, event.get("created_at", ""),
                    json.dumps(event.get("payload", {}) or {}, ensure_ascii=False),
                ),
            )
            event_id = int(cur.lastrowid)
        return self._row_to_event(self._fetch_by_id(event_id))

    # ── query ──────────────────────────────────────────────────────────

    def list_after(
        self,
        cursor: int = 0,
        filters: Optional[list[str]] = None,
        limit: int = 200,
        session_id: str = "",
        runtime_id: str = "",
    ) -> tuple[list[RuntimeEvent], int]:
        """Return events after *cursor* (local event_id), oldest first.

        Returns ``(events, next_cursor)`` where next_cursor is the last
        event_id seen (or the input cursor when empty).
        """
        if filters is not None:
            invalid = set(filters) - EVENT_KINDS
            if invalid:
                raise ValueError(f"invalid event kind filter: {sorted(invalid)}")
        limit = max(1, min(int(limit), 1000))
        where = ["event_id > ?"]
        args: list[Any] = [int(cursor)]
        if filters:
            placeholders = ",".join("?" * len(filters))
            where.append(f"kind IN ({placeholders})")
            args.extend(filters)
        if session_id:
            where.append("session_id = ?")
            args.append(session_id)
        if runtime_id:
            where.append("runtime_id = ?")
            args.append(runtime_id)
        sql = (
            "SELECT event_id, source_host, runtime_id, generation, source_sequence, "
            " session_id, agent_id, request_id, run_id, kind, created_at, payload "
            f"FROM runtime_events WHERE {' AND '.join(where)} "
            "ORDER BY event_id ASC LIMIT ?"
        )
        args.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        events = [self._row_to_event(r) for r in rows]
        next_cursor = events[-1].event_id if events else int(cursor)
        return events, next_cursor

    def aggregate(self, runtime_id: str) -> dict[str, Any]:
        """Aggregate per-runtime stats (last event / tool counts / errors)."""
        with self._connect() as conn:
            last = conn.execute(
                "SELECT event_id, kind, created_at, payload FROM runtime_events "
                "WHERE runtime_id = ? ORDER BY event_id DESC LIMIT 1",
                (runtime_id,),
            ).fetchone()
            tool_started = conn.execute(
                "SELECT COUNT(*) FROM runtime_events "
                "WHERE runtime_id = ? AND kind = 'TOOL_STARTED'",
                (runtime_id,),
            ).fetchone()[0]
            errors = conn.execute(
                "SELECT COUNT(*) FROM runtime_events "
                "WHERE runtime_id = ? AND kind = 'ERROR'",
                (runtime_id,),
            ).fetchone()[0]
            first = conn.execute(
                "SELECT created_at FROM runtime_events WHERE runtime_id = ? "
                "ORDER BY event_id ASC LIMIT 1",
                (runtime_id,),
            ).fetchone()
        return {
            "tool_count": int(tool_started),
            "error_count": int(errors),
            "first_seen_at": first[0] if first else "",
            "last_event_id": int(last[0]) if last else 0,
            "last_event_kind": last[1] if last else "",
            "last_event_at": last[2] if last else "",
            "last_event_payload": json.loads(last[3]) if last else {},
        }

    # ── retention ──────────────────────────────────────────────────────

    def sweep(self, now: Optional[float] = None) -> int:
        """Prune tool-update detail events older than 7 days.

        Terminal/receipt/error events are retained past release — only
        TOOL_STARTED/TOOL_UPDATED/TOOL_FINISHED detail rows expire.
        Returns the number of deleted rows.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=TOOL_UPDATE_RETENTION_DAYS)
        cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        placeholders = ",".join("?" * len(EVENT_KIND_TOOL_UPDATE))
        with self._connect() as conn:
            cur = conn.execute(
                f"DELETE FROM runtime_events WHERE kind IN ({placeholders}) "
                "AND created_at < ?",
                (*sorted(EVENT_KIND_TOOL_UPDATE), cutoff_iso),
            )
        return int(cur.rowcount)

    # ── internals ──────────────────────────────────────────────────────

    def _fetch_by_id(self, event_id: int) -> tuple:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT event_id, source_host, runtime_id, generation, source_sequence, "
                " session_id, agent_id, request_id, run_id, kind, created_at, payload "
                "FROM runtime_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"event not found: {event_id}")
        return row

    @staticmethod
    def _row_to_event(row) -> RuntimeEvent:
        return RuntimeEvent(
            event_id=int(row[0]),
            source_host=row[1],
            runtime_id=row[2],
            generation=int(row[3]),
            source_sequence=int(row[4]),
            session_id=row[5],
            agent_id=row[6],
            request_id=row[7],
            run_id=row[8],
            kind=row[9],
            created_at=row[10],
            payload=json.loads(row[11]) if row[11] else {},
        )


def _local_hostname() -> str:
    try:
        return os.uname().nodename.split(".", 1)[0]
    except Exception:
        return "localhost"
