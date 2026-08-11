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
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator, Optional

from codeagent.gateway.model import (
    EVENT_KIND_TERMINAL,
    EVENT_KIND_TOOL_UPDATE,
    EVENT_KINDS,
    RuntimeEvent,
    RuntimeEventDraft,
)

log = logging.getLogger(__name__)

TOOL_UPDATE_RETENTION_DAYS = 7
# P2-10: terminal/receipt/error events retained for 90 days before sweep.
TERMINAL_RETENTION_DAYS = 90
# P2-10: total event count cap — oldest rows pruned when exceeded.
MAX_TOTAL_EVENTS = 100_000


def gateway_state_dir() -> Path:
    """$XDG_DATA_HOME/postmesh/gateway — control socket + event db live here."""
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "postmesh" / "gateway"


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
        # P2-10: process-level singleton connection — WAL multi-read
        # single-write; PRAGMAs set once, not per-operation.
        # check_same_thread=False: the _lock serialises all access; the
        # connection is created in __init__ but used by server threads.
        self._conn = sqlite3.connect(str(self._db_path), timeout=10,
                                     check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._lock = threading.Lock()
        self._init_db()

    # P2-10: singleton connection — no per-operation open/close.
    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def close(self) -> None:
        """Close the singleton connection (idempotent)."""
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

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

        P2-10: constructs RuntimeEvent from lastrowid directly — no
        extra _fetch_by_id round-trip on the singleton connection.
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
        # P2-10: return directly from draft + lastrowid, no extra fetch.
        return RuntimeEvent(
            event_id=event_id,
            source_host=self._source_host,
            runtime_id=draft.runtime_id,
            generation=draft.generation,
            source_sequence=seq,
            session_id=draft.session_id,
            agent_id=draft.agent_id,
            request_id=draft.request_id,
            run_id=draft.run_id,
            kind=draft.kind,
            created_at=draft.created_at,
            payload=draft.payload,
        )

    # ── remote ingest ──────────────────────────────────────────────────

    def ingest_remote(self, event: dict) -> Optional[RuntimeEvent]:
        """Ingest a remotely-produced event, keeping ITS source_sequence.

        Idempotent on (source_host, runtime_id, generation, source_sequence):
        a duplicate insert is a no-op returning the existing row.

        P2-9: returns None for unknown event kinds (logged + skipped) so the
        caller can advance the cursor past the event.
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
        # P2-9: skip unknown event kinds instead of raising — a newer remote
        # may send kinds this host doesn't recognise.  Log a warning and return
        # None so the caller can advance the cursor past the skipped event.
        if kind not in EVENT_KINDS:
            log.warning(
                "ingest_remote: skipping unknown event kind %r "
                "(host=%s, runtime=%s, seq=%s)",
                kind, host, runtime_id, seq,
            )
            return None

        # P2-1: BEGIN IMMEDIATE takes the write lock BEFORE the SELECT so a
        # concurrent ingest for the same 4-tuple cannot slip an INSERT between
        # our check and ours — prevents lastrowid==0 → ValueError. The
        # re-query after INSERT OR IGNORE is defence-in-depth.
        # P2-10: snapshot payload once for both insert and return value.
        snap_session = event.get("session_id", "")
        snap_agent = event.get("agent_id", "")
        snap_request = event.get("request_id", "")
        snap_run = event.get("run_id", "")
        snap_created = event.get("created_at", "")
        snap_payload = event.get("payload", {}) or {}
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT event_id FROM runtime_events "
                    "WHERE source_host = ? AND runtime_id = ? AND generation = ? AND source_sequence = ?",
                    (host, runtime_id, generation, seq),
                ).fetchone()
                if existing is not None:
                    event_id = int(existing[0])
                else:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO runtime_events "
                        "(source_host, runtime_id, generation, source_sequence, session_id, "
                        " agent_id, request_id, run_id, kind, created_at, payload) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            host, runtime_id, generation, seq,
                            snap_session, snap_agent, snap_request, snap_run,
                            kind, snap_created,
                            json.dumps(snap_payload, ensure_ascii=False),
                        ),
                    )
                    if cur.lastrowid and cur.lastrowid > 0:
                        event_id = int(cur.lastrowid)
                    else:
                        # P2-1: defence-in-depth — re-query after INSERT OR IGNORE no-op.
                        row = conn.execute(
                            "SELECT event_id FROM runtime_events "
                            "WHERE source_host = ? AND runtime_id = ? "
                            "AND generation = ? AND source_sequence = ?",
                            (host, runtime_id, generation, seq),
                        ).fetchone()
                        if row is None:
                            raise ValueError(
                                f"ingest_remote failed to persist event for "
                                f"{host}/{runtime_id}/{generation}/{seq}"
                            )
                        event_id = int(row[0])
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        # P2-10: return directly from input + event_id, no extra fetch.
        return RuntimeEvent(
            event_id=event_id,
            source_host=host,
            runtime_id=runtime_id,
            generation=generation,
            source_sequence=seq,
            session_id=snap_session,
            agent_id=snap_agent,
            request_id=snap_request,
            run_id=snap_run,
            kind=kind,
            created_at=snap_created,
            payload=snap_payload,
        )

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

    def aggregate(self, runtime_id: str, generation: Optional[int] = None) -> dict[str, Any]:
        """Aggregate per-runtime stats (last event / tool counts / errors).

        A8: when *generation* is given, only events of that generation count —
        a re-registered runtime must not inherit the previous generation's
        stats. ``None`` (default) aggregates across all generations.
        """
        gen_where = " AND generation = ?" if generation is not None else ""
        gen_args: tuple[Any, ...] = (generation,) if generation is not None else ()
        with self._connect() as conn:
            last = conn.execute(
                "SELECT event_id, kind, created_at, payload FROM runtime_events "
                f"WHERE runtime_id = ?{gen_where} ORDER BY event_id DESC LIMIT 1",
                (runtime_id, *gen_args),
            ).fetchone()
            tool_started = conn.execute(
                "SELECT COUNT(*) FROM runtime_events "
                f"WHERE runtime_id = ?{gen_where} AND kind = 'TOOL_STARTED'",
                (runtime_id, *gen_args),
            ).fetchone()[0]
            errors = conn.execute(
                "SELECT COUNT(*) FROM runtime_events "
                f"WHERE runtime_id = ?{gen_where} AND kind = 'ERROR'",
                (runtime_id, *gen_args),
            ).fetchone()[0]
            first = conn.execute(
                "SELECT created_at FROM runtime_events "
                f"WHERE runtime_id = ?{gen_where} ORDER BY event_id ASC LIMIT 1",
                (runtime_id, *gen_args),
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
        """Prune events by age and total count.

        P2-4: TOOL_STARTED/TOOL_FINISHED are RETAINED (lifecycle kinds) so
        per-runtime tool_count never resets to zero after 7 days (regression
        A6). Only TOOL_UPDATED detail rows expire after 7 days.
        P2-10: terminal events (TASK_STATE, MESSAGE_READ, ERROR) expire after
        90 days; total row count capped at MAX_TOTAL_EVENTS (oldest first).
        Returns the number of deleted rows.
        """
        cutoff_tool = datetime.now(timezone.utc) - timedelta(days=TOOL_UPDATE_RETENTION_DAYS)
        cutoff_tool_iso = cutoff_tool.strftime("%Y-%m-%dT%H:%M:%SZ")
        # P2-10: 90-day retention for terminal/receipt/error events.
        cutoff_terminal = datetime.now(timezone.utc) - timedelta(days=TERMINAL_RETENTION_DAYS)
        cutoff_terminal_iso = cutoff_terminal.strftime("%Y-%m-%dT%H:%M:%SZ")
        placeholders_tool = ",".join("?" * len(EVENT_KIND_TOOL_UPDATE))
        placeholders_term = ",".join("?" * len(EVENT_KIND_TERMINAL))
        with self._connect() as conn:
            # 1. Prune old TOOL_UPDATED (>7 days).
            cur = conn.execute(
                f"DELETE FROM runtime_events WHERE kind IN ({placeholders_tool}) "
                "AND created_at < ?",
                (*sorted(EVENT_KIND_TOOL_UPDATE), cutoff_tool_iso),
            )
            removed = int(cur.rowcount)
            # 2. P2-10: Prune old terminal events (>90 days).
            cur = conn.execute(
                f"DELETE FROM runtime_events WHERE kind IN ({placeholders_term}) "
                "AND created_at < ?",
                (*sorted(EVENT_KIND_TERMINAL), cutoff_terminal_iso),
            )
            removed += int(cur.rowcount)
            # 3. P2-10: Total row count cap — evict oldest rows.
            count = conn.execute(
                "SELECT COUNT(*) FROM runtime_events"
            ).fetchone()[0]
            if count > MAX_TOTAL_EVENTS:
                excess = count - MAX_TOTAL_EVENTS
                cur = conn.execute(
                    "DELETE FROM runtime_events WHERE event_id IN "
                    "(SELECT event_id FROM runtime_events "
                    "ORDER BY event_id ASC LIMIT ?)",
                    (excess,),
                )
                removed += int(cur.rowcount)
        return removed

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
