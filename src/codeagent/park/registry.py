"""Park 注册表 — SQLite 持久化 + per-key flock。

与 SessionRegistry（session/registry.py）使用相同的 flock + WAL 模式。
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Optional

from codeagent.domain.park import Lifecycle, ParkManifest
from codeagent.park.constants import PARK_DEFAULTS, park_state_dir
from codeagent.park.snapshot import ReviewSnapshot, latest_snapshot, save_snapshot
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
    lookup:  按 review_key 查询 ParkManifest。
    lookup_by_field: 按 peer_agent_id/mailbox_agent_id/backend_session_id 反查。
    sweep:   驱逐过期/TTL/超限实例，返回被驱逐的 key 列表。
    list_active: 返回所有 HOT_PARKED 实例。
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = db_path or (park_state_dir() / "park.sqlite3")
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # 不持久化 conn——每次操作创建临时连接（多线程安全，与 SessionRegistry 一致）
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_CREATE_TABLE)
            conn.commit()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        with sqlite3.connect(str(self._db_path)) as conn:
            yield conn

    def _lock(self, key: str) -> SessionLock:
        return SessionLock(key)

    # ── CRUD ─────────────────────────────────────────────────

    def acquire(self, review_key: str, manifest: ParkManifest) -> bool:
        """尝试 acquire 一个 park 实例。已存在则返回 False。"""
        with self._lock(review_key):
            with self._connect() as conn:
                row = conn.execute(
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
                conn.execute(
                    "INSERT INTO park_leases (key, manifest_json, lifecycle, soft_expires, hard_expires, last_activity) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    params,
                )
                conn.commit()
                return True

    def renew(self, review_key: str) -> None:
        """续租：更新 last_activity 和 soft_expires。"""
        now = time.time()
        with self._lock(review_key):
            with self._connect() as conn:
                conn.execute(
                    "UPDATE park_leases SET last_activity = ?, soft_expires = ? WHERE key = ?",
                    (now, now + PARK_DEFAULTS["ttl_seconds"], review_key),
                )
                conn.commit()

    def release(self, review_key: str) -> None:
        """释放 park 实例（标记为 RELEASED）。"""
        with self._lock(review_key):
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT manifest_json FROM park_leases WHERE key = ?",
                    (review_key,),
                ).fetchone()
                if row is None:
                    return
                d = json.loads(row[0])
                d["lifecycle"] = "released"
                conn.execute(
                    "UPDATE park_leases SET lifecycle = 'released', manifest_json = ? WHERE key = ?",
                    (json.dumps(d), review_key),
                )
                conn.commit()

    def lookup(self, review_key: str) -> Optional[ParkManifest]:
        """按 review_key 查询。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT manifest_json FROM park_leases WHERE key = ?",
                (review_key,),
            ).fetchone()
        if row is None:
            return None
        return self._dict_to_manifest(json.loads(row[0]))

    def lookup_by_field(self, field: str, value: str) -> Optional[ParkManifest]:
        """按 manifest 内的字段反查（peer_agent_id/mailbox_agent_id/backend_session_id）。

        遍历 HOT_PARKED 实例的 manifest_json，匹配指定字段值。
        key 是 review_key，不是这些 ID，所以不能直接用 SQL 索引。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT manifest_json FROM park_leases WHERE lifecycle = 'hot_parked'",
            ).fetchall()
        for (mj,) in rows:
            d = json.loads(mj)
            if d.get(field) == value:
                return self._dict_to_manifest(d)
        return None

    def list_active(self) -> list[ParkManifest]:
        """返回所有 HOT_PARKED 实例。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT manifest_json FROM park_leases WHERE lifecycle = 'hot_parked'",
            ).fetchall()
        return [self._dict_to_manifest(json.loads(r[0])) for r in rows]

    def sweep(self) -> list[str]:
        """驱逐过期/超限实例。返回被驱逐的 key 列表。

        驱逐顺序：
        1. TTL 过期（soft_expires / hard_expires）→ cold_resumable
        2. 超过 max_hot_parked 上限 → LRU（最久未使用优先）
        3. 超过 max_rounds 上限 → 强制释放
        驱逐前若存在 snapshot 则保留；不存在则跳过。
        """
        import logging
        log = logging.getLogger(__name__)
        now = time.time()
        evicted: list[str] = []

        # 1. TTL 驱逐
        with self._connect() as conn:
            expired = conn.execute(
                "SELECT key, manifest_json, last_activity FROM park_leases "
                "WHERE lifecycle = 'hot_parked' "
                "AND ((soft_expires > 0 AND soft_expires < ?) OR (hard_expires > 0 AND hard_expires < ?))",
                (now, now),
            ).fetchall()
        for key, mj, _last_act in expired:
            log.warning("park sweep TTL: evicting %s", key)
            self._evict_one(key, mj, "cold_resumable")
            evicted.append(key)

        # 2. LRU 驱逐（超限）
        max_hot = PARK_DEFAULTS["max_hot_parked"]
        while True:
            with self._connect() as conn:
                active = conn.execute(
                    "SELECT key, manifest_json, last_activity FROM park_leases "
                    "WHERE lifecycle = 'hot_parked' ORDER BY last_activity ASC",
                ).fetchall()
            if len(active) <= max_hot:
                break
            key, mj, _ = active[0]  # 最久未使用
            log.warning("park sweep LRU: evicting %s", key)
            self._evict_one(key, mj, "cold_resumable")
            evicted.append(key)

        # 3. max_rounds 驱逐
        max_rounds = PARK_DEFAULTS["max_rounds"]
        with self._connect() as conn:
            over_rounds = conn.execute(
                "SELECT key, manifest_json FROM park_leases "
                "WHERE lifecycle = 'hot_parked'",
            ).fetchall()
        for key, mj in over_rounds:
            d = json.loads(mj)
            if d.get("round", 0) >= max_rounds:
                log.warning("park sweep max_rounds: releasing %s (round=%d)", key, d.get("round", 0))
                self._evict_one(key, mj, "cold_resumable")
                evicted.append(key)

        return evicted

    def _evict_one(self, key: str, manifest_json: str, target_lifecycle: str) -> None:
        """驱逐单个实例：先 snapshot（若存在），再改 lifecycle。"""
        import logging
        log = logging.getLogger(__name__)
        # 确保驱逐前有 snapshot（若已有则跳过）
        if latest_snapshot(key) is None:
            d = json.loads(manifest_json)
            snap = ReviewSnapshot(
                review_key=key,
                round=0,
                last_conclusion="(auto-snapshot on eviction)",
                generated_at=time.time(),
            )
            try:
                save_snapshot(snap)
            except Exception as exc:
                log.warning("park evict: snapshot failed for %s: %s", key, exc)
        with self._lock(key):
            with self._connect() as conn:
                d = json.loads(manifest_json)
                d["lifecycle"] = target_lifecycle
                conn.execute(
                    "UPDATE park_leases SET lifecycle = ?, manifest_json = ? WHERE key = ?",
                    (target_lifecycle, json.dumps(d), key),
                )
                conn.commit()

    # ── 序列化 ──────────────────────────────────────────────

    @staticmethod
    def _manifest_to_dict(m: ParkManifest) -> dict:
        d: dict[str, Any] = {
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