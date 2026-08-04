"""Park 注册表 — SQLite 持久化 + per-key flock。

与 SessionRegistry（session/registry.py）使用相同的 flock + WAL 模式。
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

from codeagent.domain.park import Lifecycle, ParkManifest
from codeagent.park.constants import PARK_DEFAULTS, park_state_dir
from codeagent.session.lock import SessionLock

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS park_leases (
    key            TEXT PRIMARY KEY,
    manifest_json  TEXT NOT NULL,
    lifecycle      TEXT NOT NULL DEFAULT 'hot_parked',
    soft_expires   REAL NOT NULL DEFAULT 0,
    hard_expires   REAL NOT NULL DEFAULT 0,
    last_activity  REAL NOT NULL DEFAULT 0
)
"""


class ParkRegistry:
    """Park 实例注册表 — 每个 review_key 一个实例。

    acquire: 原子 insert-or-fail。同一个 key 只能有一个活跃实例。
    renew:   更新 last_activity + soft_expires。
    release: 标记 lifecycle=RELEASED。
    lookup:  按 key 查询 ParkManifest。
    sweep:   驱逐过期/TTL/超限实例，返回被驱逐的 key 列表。
    list_active: 返回所有 HOT_PARKED 实例。
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = db_path or (park_state_dir() / "park.sqlite3")
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_CREATE_TABLE)
        self._conn.commit()

    def _lock(self, key: str) -> SessionLock:
        return SessionLock(key)

    # ── CRUD ─────────────────────────────────────────────────

    def acquire(self, review_key: str, manifest: ParkManifest) -> bool:
        """尝试 acquire 一个 park 实例。已存在则返回 False。"""
        with self._lock(review_key) as lock:
            if not lock:
                return False
            row = self._conn.execute(
                "SELECT 1 FROM park_leases WHERE key = ? AND lifecycle = 'hot_parked'",
                (review_key,),
            ).fetchone()
            if row:
                return False
            now = time.time()
            params = (
                review_key,
                json.dumps(self._manifest_to_dict(manifest)),
                manifest.lifecycle.value,
                manifest.soft_expires_at or (now + PARK_DEFAULTS["ttl_seconds"]),
                manifest.hard_expires_at or (now + PARK_DEFAULTS["hard_limit_seconds"]),
                now,
            )
            self._conn.execute(
                "INSERT INTO park_leases (key, manifest_json, lifecycle, soft_expires, hard_expires, last_activity) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                params,
            )
            self._conn.commit()
            return True

    def renew(self, review_key: str) -> None:
        """续租：更新 last_activity 和 soft_expires。"""
        now = time.time()
        with self._lock(review_key):
            self._conn.execute(
                "UPDATE park_leases SET last_activity = ?, soft_expires = ? WHERE key = ?",
                (now, now + PARK_DEFAULTS["ttl_seconds"], review_key),
            )
            self._conn.commit()

    def release(self, review_key: str) -> None:
        """释放 park 实例（标记为 RELEASED）。"""
        with self._lock(review_key):
            row = self._conn.execute(
                "SELECT manifest_json FROM park_leases WHERE key = ?",
                (review_key,),
            ).fetchone()
            if row is None:
                return
            d = json.loads(row[0])
            d["lifecycle"] = "released"
            self._conn.execute(
                "UPDATE park_leases SET lifecycle = 'released', manifest_json = ? WHERE key = ?",
                (json.dumps(d), review_key),
            )
            self._conn.commit()

    def lookup(self, review_key: str) -> Optional[ParkManifest]:
        """按 key 查询。"""
        row = self._conn.execute(
            "SELECT manifest_json FROM park_leases WHERE key = ?",
            (review_key,),
        ).fetchone()
        if row is None:
            return None
        return self._dict_to_manifest(json.loads(row[0]))

    def list_active(self) -> list[ParkManifest]:
        """返回所有 HOT_PARKED 实例。"""
        rows = self._conn.execute(
            "SELECT manifest_json FROM park_leases WHERE lifecycle = 'hot_parked'",
        ).fetchall()
        return [self._dict_to_manifest(json.loads(r[0])) for r in rows]

    def sweep(self) -> list[str]:
        """驱逐过期实例。返回被驱逐的 key 列表。"""
        now = time.time()
        expired = self._conn.execute(
            "SELECT key, manifest_json FROM park_leases WHERE lifecycle = 'hot_parked' "
            "AND ((soft_expires > 0 AND soft_expires < ?) OR (hard_expires > 0 AND hard_expires < ?))",
            (now, now),
        ).fetchall()
        keys = [r[0] for r in expired]
        for key, mj in expired:
            with self._lock(key):
                d = json.loads(mj)
                d["lifecycle"] = "cold_resumable"
                self._conn.execute(
                    "UPDATE park_leases SET lifecycle = 'cold_resumable', manifest_json = ? WHERE key = ?",
                    (json.dumps(d), key),
                )
        self._conn.commit()
        return keys

    # ── 序列化 ──────────────────────────────────────────────

    @staticmethod
    def _manifest_to_dict(m: ParkManifest) -> dict:
        d = {
            "review_key": m.review_key,
            "swarm_session_id": m.swarm_session_id,
            "agent_type": m.agent_type,
            "model": m.model,
            "host": m.host,
            "workdir": m.workdir,
            "lifecycle": m.lifecycle.value,
            "peer_agent_id": m.peer_agent_id,
            "mailbox_agent_id": m.mailbox_agent_id,
            "backend_session_id": m.backend_session_id,
            "parent_process_generation": m.parent_process_generation,
            "created_at": m.created_at,
            "last_activity_at": m.last_activity_at,
            "soft_expires_at": m.soft_expires_at,
            "hard_expires_at": m.hard_expires_at,
            "round": m.round,
            "last_msg_id": m.last_msg_id,
            "summary_uri": m.summary_uri,
            "transcript_uri": m.transcript_uri,
            "artifact_refs": m.artifact_refs,
            "config_fingerprint": m.config_fingerprint,
            "schema_version": m.schema_version,
        }
        return d

    @staticmethod
    def _dict_to_manifest(d: dict) -> ParkManifest:
        d["lifecycle"] = Lifecycle(d.get("lifecycle", "hot_parked"))
        return ParkManifest(**d)