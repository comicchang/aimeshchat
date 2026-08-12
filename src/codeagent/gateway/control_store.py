"""ControlStore — SQLite (WAL) 控制面存储（AgentGateway 权威状态）。

与 events.sqlite3（observability，synchronous=NORMAL）分离：控制面状态
（reviews / runtime_generations / commands）一旦丢失会直接导致重复投递
或幽灵 turn，因此关键控制事务（入队、领取、ack、状态归约落盘）使用
synchronous=FULL —— WAL 模式下每次 COMMIT 前 fsync，崩溃后不丢已确认
的控制写入。

表（设计 §4）：
  reviews               review_key → swarm_session/runtime/profile 绑定镜像
  runtime_generations   runtime_id → 当前 generation + 三维状态 + binding 事实
  commands              runtime.send 持久命令状态机（request_id 幂等键）
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Optional

from codeagent.constants import ISO_TIMESTAMP_FORMAT
from codeagent.gateway.events import gateway_state_dir

log = logging.getLogger(__name__)


def control_db_path() -> Path:
    """$XDG_DATA_HOME/aimeshchat/gateway/control.sqlite3."""
    return gateway_state_dir() / "control.sqlite3"


_CREATE_TABLES = (
    """\
CREATE TABLE IF NOT EXISTS reviews (
    review_key       TEXT PRIMARY KEY,
    swarm_session_id TEXT NOT NULL UNIQUE,
    runtime_id       TEXT NOT NULL DEFAULT '',
    profile_id       TEXT NOT NULL DEFAULT '',
    mailbox_agent_id TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
)
""",
    """\
CREATE TABLE IF NOT EXISTS runtime_generations (
    runtime_id         TEXT PRIMARY KEY,
    current_generation INTEGER NOT NULL DEFAULT 0,
    owner_nonce        TEXT NOT NULL DEFAULT '',
    presence           TEXT NOT NULL DEFAULT 'alive',
    binding            TEXT NOT NULL DEFAULT 'pending',
    backend_session_id TEXT NOT NULL DEFAULT '',
    binding_epoch      INTEGER NOT NULL DEFAULT 0,
    agent_state        TEXT NOT NULL DEFAULT 'idle',
    last_state_seq     INTEGER NOT NULL DEFAULT 0,
    updated_at         TEXT NOT NULL,
    model_context      TEXT NOT NULL DEFAULT '{}'
)
""",
    """\
CREATE TABLE IF NOT EXISTS commands (
    request_id         TEXT PRIMARY KEY,
    command_id         TEXT NOT NULL,
    msg_id             TEXT NOT NULL DEFAULT '',
    turn_id            TEXT NOT NULL DEFAULT '',
    runtime_id         TEXT NOT NULL,
    generation         INTEGER NOT NULL DEFAULT 0,
    payload_hash       TEXT NOT NULL,
    state              TEXT NOT NULL,
    binding_epoch      INTEGER NOT NULL DEFAULT 0,
    backend_session_id TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    detail             TEXT NOT NULL DEFAULT '{}'
)
""",
)

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_commands_runtime ON commands(runtime_id, state)",
    "CREATE INDEX IF NOT EXISTS idx_commands_state ON commands(state)",
    "CREATE INDEX IF NOT EXISTS idx_generations_binding ON runtime_generations(binding, presence)",
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT)


class ControlStore:
    """控制面持久化：reviews / runtime_generations / commands。

    进程级单连接（check_same_thread=False）+ _lock 串行化，与 EventStore
    同模式；关键控制事务经 ``_connect(critical=True)`` 提升为
    synchronous=FULL。
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = db_path or control_db_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # 目录 0700 —— 控制面含命令内容，权限与 events 对齐。
        try:
            os.chmod(self._db_path.parent, 0o700)
        except OSError:
            pass
        self._conn = sqlite3.connect(str(self._db_path), timeout=10,
                                     check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._lock = threading.Lock()
        self._init_db()

    def close(self) -> None:
        """关闭单连接（幂等）。"""
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    @contextmanager
    def _connect(self, critical: bool = False) -> Generator[sqlite3.Connection, None, None]:
        """事务上下文；*critical*（关键控制事务）→ synchronous=FULL。"""
        with self._lock:
            try:
                if critical:
                    self._conn.execute("PRAGMA synchronous=FULL")
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
            finally:
                if critical:
                    self._conn.execute("PRAGMA synchronous=NORMAL")

    def _init_db(self) -> None:
        with self._connect() as conn:
            for ddl in _CREATE_TABLES:
                conn.execute(ddl)
            for idx in _INDEXES:
                conn.execute(idx)
            # 向后兼容迁移：为已有 runtime_generations 表补 model_context 列
            # （旧库 CREATE TABLE IF NOT EXISTS 不会加新列）。
            try:
                conn.execute(
                    "ALTER TABLE runtime_generations"
                    " ADD COLUMN model_context TEXT NOT NULL DEFAULT '{}'"
                )
            except sqlite3.OperationalError:
                pass  # 列已存在（新库 CREATE TABLE 已包含）

    # ── reviews ───────────────────────────────────────────────────────

    def upsert_review(self, review_key: str, swarm_session_id: str,
                      runtime_id: str = "", profile_id: str = "",
                      mailbox_agent_id: str = "") -> None:
        """镜像 park 关联到 reviews（关键事务）。

        swarm_session_id 有 UNIQUE 约束 —— 同一 session 被第二个
        review_key 认领时记警告并跳过（镜像尽力而为，不阻断注册）。
        """
        now = _utcnow_iso()
        try:
            with self._connect(critical=True) as conn:
                conn.execute(
                    "INSERT INTO reviews (review_key, swarm_session_id, runtime_id,"
                    " profile_id, mailbox_agent_id, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(review_key) DO UPDATE SET"
                    " swarm_session_id=excluded.swarm_session_id,"
                    " runtime_id=excluded.runtime_id, profile_id=excluded.profile_id,"
                    " mailbox_agent_id=excluded.mailbox_agent_id, updated_at=excluded.updated_at",
                    (review_key, swarm_session_id, runtime_id, profile_id,
                     mailbox_agent_id, now, now),
                )
        except sqlite3.IntegrityError as exc:
            log.warning("control_store: review upsert conflict for %s: %s", review_key, exc)

    def get_review(self, review_key: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT review_key, swarm_session_id, runtime_id, profile_id,"
                " mailbox_agent_id FROM reviews WHERE review_key = ?",
                (review_key,),
            ).fetchone()
        return self._review_row(row)

    def get_review_by_session(self, swarm_session_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT review_key, swarm_session_id, runtime_id, profile_id,"
                " mailbox_agent_id FROM reviews WHERE swarm_session_id = ?",
                (swarm_session_id,),
            ).fetchone()
        return self._review_row(row)

    @staticmethod
    def _review_row(row) -> Optional[dict]:
        if row is None:
            return None
        return {
            "review_key": row[0], "swarm_session_id": row[1], "runtime_id": row[2],
            "profile_id": row[3], "mailbox_agent_id": row[4],
        }

    # ── runtime_generations ───────────────────────────────────────────

    def upsert_generation(self, runtime_id: str, current_generation: int,
                          owner_nonce: str = "", presence: str = "alive",
                          binding: str = "pending", backend_session_id: str = "",
                          binding_epoch: int = 0, agent_state: str = "idle",
                          model_context: str = "") -> None:
        """三维状态归约落盘（关键事务；last_state_seq 单调递增）。

        model_context：JSON 字符串，持久化 runtime.context（provider/model/
        variant/epoch）。传空字符串时保留已有值（不覆盖）。
        """
        now = _utcnow_iso()
        with self._connect(critical=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT last_state_seq, model_context FROM runtime_generations"
                    " WHERE runtime_id = ?",
                    (runtime_id,),
                ).fetchone()
                seq = int(row[0]) + 1 if row else 1
                # model_context 为空时保留已有值（避免无上下文的归约覆盖已持久化的上下文）
                effective_ctx = model_context if model_context else (row[1] if row else '{}')
                conn.execute(
                    "INSERT INTO runtime_generations (runtime_id, current_generation,"
                    " owner_nonce, presence, binding, backend_session_id, binding_epoch,"
                    " agent_state, last_state_seq, updated_at, model_context)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(runtime_id) DO UPDATE SET"
                    " current_generation=excluded.current_generation,"
                    " owner_nonce=excluded.owner_nonce, presence=excluded.presence,"
                    " binding=excluded.binding,"
                    " backend_session_id=excluded.backend_session_id,"
                    " binding_epoch=excluded.binding_epoch,"
                    " agent_state=excluded.agent_state,"
                    " last_state_seq=excluded.last_state_seq, updated_at=excluded.updated_at,"
                    " model_context=excluded.model_context",
                    (runtime_id, int(current_generation), owner_nonce, presence,
                     binding, backend_session_id, int(binding_epoch), agent_state,
                     seq, now, effective_ctx),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def delete_generation(self, runtime_id: str) -> bool:
        """删除 runtime_generations 行（清理旧 stopped 记录）。"""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM runtime_generations WHERE runtime_id = ?",
                (runtime_id,),
            )
        return cur.rowcount > 0

    def get_generation(self, runtime_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT runtime_id, current_generation, owner_nonce, presence, binding,"
                " backend_session_id, binding_epoch, agent_state, last_state_seq,"
                " updated_at, model_context"
                " FROM runtime_generations WHERE runtime_id = ?",
                (runtime_id,),
            ).fetchone()
        if row is None:
            return None
        # model_context 向后兼容：旧库无此列时 row[10] 为默认 '{}'
        ctx_raw = row[10] if row[10] else '{}'
        try:
            model_context = json.loads(ctx_raw)
        except (json.JSONDecodeError, TypeError):
            model_context = {}
        return {
            "runtime_id": row[0], "current_generation": int(row[1]),
            "owner_nonce": row[2], "presence": row[3], "binding": row[4],
            "backend_session_id": row[5], "binding_epoch": int(row[6]),
            "agent_state": row[7], "last_state_seq": int(row[8]),
            "updated_at": row[9], "model_context": model_context,
        }

    # ── commands（runtime.send 持久命令状态机）─────────────────────────

    def enqueue_command(self, request_id: str, command_id: str, runtime_id: str,
                        generation: int, payload_hash: str, state: str,
                        binding_epoch: int = 0, backend_session_id: str = "",
                        msg_id: str = "", created_at: str = "",
                        detail: Optional[dict] = None) -> bool:
        """入队持久命令（关键事务 synchronous=FULL）。

        request_id 为主键幂等：已存在时返回 False（调用方走幂等重放，
        不重复注入）。
        """
        now = created_at or _utcnow_iso()
        with self._connect(critical=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO commands (request_id, command_id, msg_id, turn_id,"
                    " runtime_id, generation, payload_hash, state, binding_epoch,"
                    " backend_session_id, created_at, updated_at, detail)"
                    " VALUES (?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (request_id, command_id, msg_id, runtime_id, int(generation),
                     payload_hash, state, int(binding_epoch), backend_session_id,
                     now, now, json.dumps(detail or {}, ensure_ascii=False)),
                )
                created = int(cur.rowcount or 0) > 0
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return created

    def get_command(self, request_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT request_id, command_id, msg_id, turn_id, runtime_id, generation,"
                " payload_hash, state, binding_epoch, backend_session_id, created_at,"
                " updated_at, detail FROM commands WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return self._command_row(row)

    def update_command(self, request_id: str, state: Optional[str] = None,
                       msg_id: Optional[str] = None, turn_id: Optional[str] = None,
                       binding_epoch: Optional[int] = None,
                       backend_session_id: Optional[str] = None,
                       detail: Optional[dict] = None) -> Optional[dict]:
        """推进命令状态（关键事务）；None 字段保持不变。返回更新后行。"""
        now = _utcnow_iso()
        sets: list[str] = []
        args: list[Any] = []
        if state is not None:
            sets.append("state = ?")
            args.append(state)
        if msg_id is not None:
            sets.append("msg_id = ?")
            args.append(msg_id)
        if turn_id is not None:
            sets.append("turn_id = ?")
            args.append(turn_id)
        if binding_epoch is not None:
            sets.append("binding_epoch = ?")
            args.append(int(binding_epoch))
        if backend_session_id is not None:
            sets.append("backend_session_id = ?")
            args.append(backend_session_id)
        if detail is not None:
            sets.append("detail = ?")
            args.append(json.dumps(detail, ensure_ascii=False))
        if not sets:
            return self.get_command(request_id)
        sets.append("updated_at = ?")
        args.append(now)
        args.append(request_id)
        with self._connect(critical=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    f"UPDATE commands SET {', '.join(sets)} WHERE request_id = ?", args,
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return self.get_command(request_id)

    def list_commands(self, runtime_id: str = "", state: Optional[str] = None,
                      limit: int = 100) -> list[dict]:
        """按 runtime/state 列出命令（观测用，最新在前）。"""
        where: list[str] = []
        args: list[Any] = []
        if runtime_id:
            where.append("runtime_id = ?")
            args.append(runtime_id)
        if state is not None:
            where.append("state = ?")
            args.append(state)
        sql = (
            "SELECT request_id, command_id, msg_id, turn_id, runtime_id, generation,"
            " payload_hash, state, binding_epoch, backend_session_id, created_at,"
            " updated_at, detail FROM commands"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [self._command_row(r) for r in rows]

    @staticmethod
    def _command_row(row) -> Optional[dict]:
        if row is None:
            return None
        return {
            "request_id": row[0], "command_id": row[1], "msg_id": row[2],
            "turn_id": row[3], "runtime_id": row[4], "generation": int(row[5]),
            "payload_hash": row[6], "state": row[7], "binding_epoch": int(row[8]),
            "backend_session_id": row[9], "created_at": row[10], "updated_at": row[11],
            "detail": json.loads(row[12]) if row[12] else {},
        }
