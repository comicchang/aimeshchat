"""AgentGateway — per-device orchestration service.

The gateway composes the existing authorities (MailboxStore/SwarmKernel/
ParkRegistry/MailboxService/runtime launcher) and exposes them over a
local UDS. It does NOT re-implement protocol logic — mailbox semantics
live in MailboxService, delivery in DeliveryEngine, park leases in
ParkRegistry. This module wires them behind fixed RPC methods.

Fail-closed rules:
  - identity (session_id/agent_id/runtime_id) must match a registered
    runtime for owner-scoped operations
  - generation must not be stale
  - unknown runtimes → UNSUPPORTED_RUNTIME
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from codeagent.constants import ISO_TIMESTAMP_FORMAT
from codeagent.gateway.control_store import ControlStore
from codeagent.gateway.events import EventStore, control_socket_path
from codeagent.gateway.model import (
    ERR_GENERATION_STALE,
    ERR_NOT_AUTHORIZED,
    ERR_NOT_FOUND,
    ERR_OWNER_MISMATCH,
    ERR_PROTOCOL,
    ERR_PROTOCOL_CONFLICT,
    ERR_UNSUPPORTED_RUNTIME,
    GATEWAY_PROTOCOL_VERSION,
    GatewayError,
    RuntimeEventDraft,
)
from codeagent.mailbox.service import MailboxService
from codeagent.mailbox.store import MailboxStore
from codeagent.swarm.kernel import SwarmKernel

log = logging.getLogger(__name__)

# ── 三维状态（设计 §1）───────────────────────────────────────────────
# presence：进程层存活（heartbeat / 进程退出驱动）
PRESENCE_ALIVE = "alive"
PRESENCE_STALE = "stale"
PRESENCE_DEAD = "dead"
# binding：backend session 注册 / 解绑驱动
BINDING_PENDING = "pending"
BINDING_BOUND = "bound"
BINDING_LOST = "lost"
# agent_state：OMP lifecycle/registry 事件驱动
AGENT_RUNNING = "agent_running"
AGENT_IDLE = "idle"
AGENT_ENDED = "ended"

# 权威 lifecycle 事件（runtime.lifecycle / 归约入口）
LIFECYCLE_EVENTS = frozenset({
    "session_ready", "agent_start", "turn_start", "turn_end", "agent_end",
    "session_shutdown", "registry_parked", "registry_removed", "process_exit",
    "heartbeat",
})

# ── runtime.send 持久命令状态机（设计 §3）────────────────────────────
CMD_QUEUED = "QUEUED"
CMD_CLAIMED = "CLAIMED"
CMD_REVIVING = "REVIVING"
CMD_TRIGGERING = "TRIGGERING"
CMD_TURN_TRIGGERED = "TURN_TRIGGERED"
CMD_FAILED_SAFE = "FAILED_SAFE"
CMD_AMBIGUOUS = "AMBIGUOUS"
CMD_TRIGGER_UNKNOWN = "TRIGGER_UNKNOWN"

CMD_STATES = frozenset({
    CMD_QUEUED, CMD_CLAIMED, CMD_REVIVING, CMD_TRIGGERING, CMD_TURN_TRIGGERED,
    CMD_FAILED_SAFE, CMD_AMBIGUOUS, CMD_TRIGGER_UNKNOWN,
})

# 合法迁移表：terminal 状态（TURN_TRIGGERED/FAILED_SAFE/AMBIGUOUS/
# TRIGGER_UNKNOWN）不可再迁移。
CMD_TRANSITIONS = {
    CMD_QUEUED: {CMD_CLAIMED, CMD_FAILED_SAFE, CMD_AMBIGUOUS},
    CMD_CLAIMED: {CMD_REVIVING, CMD_TRIGGERING, CMD_FAILED_SAFE, CMD_AMBIGUOUS},
    CMD_REVIVING: {CMD_TRIGGERING, CMD_FAILED_SAFE, CMD_AMBIGUOUS},
    CMD_TRIGGERING: {CMD_TURN_TRIGGERED, CMD_AMBIGUOUS, CMD_TRIGGER_UNKNOWN},
    CMD_TURN_TRIGGERED: set(),
    CMD_FAILED_SAFE: set(),
    CMD_AMBIGUOUS: set(),
    CMD_TRIGGER_UNKNOWN: set(),
}

# 命令状态 → runtime.send 返回语义（设计 §3 返回语义表）。
CMD_STATUS_BY_STATE = {
    CMD_QUEUED: "mailbox_persisted",
    CMD_CLAIMED: "claimed",
    CMD_REVIVING: "session_live",
    CMD_TRIGGERING: "ambiguous",       # 可能已触发，未确认 → 禁止自动重投
    CMD_TURN_TRIGGERED: "turn_triggered",
    CMD_FAILED_SAFE: "failed_safe",
    CMD_AMBIGUOUS: "ambiguous",
    CMD_TRIGGER_UNKNOWN: "ambiguous",
}

ERR_IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"


def _sha256(text: str) -> str:
    """payload_hash 幂等键：body 的 sha256。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT)


@dataclass
class RuntimeRecord:
    """A registered/launched runtime known to this gateway."""

    runtime_id: str
    session_id: str
    agent_id: str
    generation: int
    review_key: str = ""
    backend_session_id: str = ""
    host_alias: str = "__local__"
    runtime: str = ""  # "omp" | "opencode" | "generic"
    owner_pid: int = 0
    nonce: str = ""
    status: str = "starting"  # starting | active | offline | stopped | unknown
    created_at: str = ""
    last_activity: float = 0.0
    started_at: float = 0.0  # A12: epoch of process start (real elapsed baseline)
    spec: dict = field(default_factory=dict)
    initial_task: str = ""
    # ── 三维状态（设计 §1，取代单一 status 作为路由权威）──────────────
    # status 字段保留仅用于旧 API 兼容；hot 路由以三维为准。
    presence: str = PRESENCE_ALIVE     # alive | stale | dead
    binding: str = BINDING_PENDING     # pending | bound | lost
    agent_state: str = AGENT_IDLE      # agent_running | idle | ended
    binding_epoch: int = 0             # backend session 绑定代数（防陈旧 binding）
    capabilities: list = field(default_factory=list)  # 插件握手上报能力
    # ── Q5b: 主 agent 当前模型上下文（runtime.context 机制）───────────
    # 插件在 model_change/thinking_level 时经 runtime.context_set 原子更新
    # provider/model/variant/epoch；内存态（同 capabilities，不落盘），
    # 供 oracle CLI default 继承（AIMESHCHAT_RUNTIME_ID → context_get）。
    model_context: dict = field(default_factory=dict)


@dataclass
class _HubPeer:
    """Hub-layer peer ↔ swarm agent mapping (P8.1 cross-device routing)."""

    peer_id: str
    session_id: str
    agent_id: str
    host_alias: str
    mailbox_root: str = ""
    status: str = "online"  # online | offline | unknown
    registered_at: float = 0.0


class AgentGateway:
    """Local gateway service — fixed RPC surface over existing authorities."""

    def __init__(
        self,
        store: Optional[MailboxStore] = None,
        events: Optional[EventStore] = None,
        kernel: Optional[SwarmKernel] = None,
        restore_from_park: bool = True,
        peers_file: Optional[Path] = None,
    ) -> None:
        self._store = store or MailboxStore()
        self._events = events or EventStore()
        # Kernel wired with EngineDeliverySink — cross-device routing via
        # DeliveryEngine (durable outbox → transport → remote inbox).
        self._engine = None
        if kernel is None:
            from codeagent.swarm.delivery import DeliveryEngine, EngineDeliverySink

            engine = DeliveryEngine(mailbox_store=self._store, transport_router=None)
            sink = EngineDeliverySink(engine)
            self._kernel = SwarmKernel(store=self._store, sink=sink)
            sink.set_kernel(self._kernel)
            self._engine = engine
        else:
            self._kernel = kernel
        self._svc = MailboxService(store=self._store, kernel=self._kernel)
        self._runtimes: dict[str, RuntimeRecord] = {}
        # P8.3 presence: heartbeat timeout → offline sweep.
        # A4: 120s → 300s to cover slow networks; cold-start grace below
        # (timeout * 2) is unchanged.
        # P2: 300s trades liveness detection speed for resilience on
        # unreliable networks / slow-starting runtimes (e.g. remote SSH
        # tunnel flaps, OMP cold-start).  Shorter timeouts catch genuine
        # failures faster but cause false positives on transient pauses.
        self._offline_timeout: float = 300.0
        self._sweep_interval: float = 30.0
        self._sweep_stop = threading.Event()
        self._runtimes_lock = threading.RLock()
        self._sweep_thread: Optional[threading.Thread] = None
        # 三维 presence 衰变：stale 持续超过该阈值 → dead（设计 §1）。
        self._dead_timeout: float = 600.0
        # 控制面存储（reviews/runtime_generations/commands，关键事务 FULL）。
        # db 与 events 同目录（control.sqlite3）—— 测试传 events 路径即自动隔离。
        self._control = ControlStore(db_path=Path(self._events._db_path).with_name("control.sqlite3"))
        # A6: hourly retention sweep (EventStore.sweep + outbox TTL).
        self._retention_sweep_interval: float = 3600.0
        self._last_retention_sweep: float = 0.0
        self._start_sweep_loop()
        # P8.1 hub: peer ↔ swarm agent mapping for cross-device routing.
        self._peers: dict[str, _HubPeer] = {}
        self._peers_lock = threading.RLock()
        # F2: hub peers are PERSISTED (they map remote devices, not local
        # processes) — survive gateway restarts.
        self._peers_file = peers_file or (control_socket_path().parent / "peers.json")
        self._restore_peers()
        # actas: runtime role exclusivity claims (session_id:agent_id → claim).
        self._claims: dict[str, dict] = {}
        # Cross-device write merge bookkeeping (session_id, target_path) →
        # artifact_sha256 — persisted so conflict detection survives restarts.
        self._merges: dict[tuple[str, str], str] = {}
        # P2-2: RLock for merges dict concurrent access.
        self._merges_lock = threading.RLock()
        self._restore_merges()
        # A3: gateway restart recovery — rebuild runtime records from the
        # park registry so hot detection / presence survive a restart even
        # for runtimes without a plugin re-register (opencode/generic).
        # Tests disable it to keep isolation from the real park DB.
        if restore_from_park:
            self._restore_runtimes_from_park()

    # ── F2: hub peer persistence ──────────────────────────────────────

    def _save_peers(self) -> None:
        """Persist hub peers atomically (survive gateway restarts)."""
        try:
            self._peers_file.parent.mkdir(parents=True, exist_ok=True)
            # P2-2: snapshot peers under lock; write file outside lock.
            with self._peers_lock:
                data = [{
                    "peer_id": p.peer_id, "session_id": p.session_id,
                    "agent_id": p.agent_id, "host_alias": p.host_alias,
                    "mailbox_root": p.mailbox_root, "status": p.status,
                    "registered_at": p.registered_at,
                } for p in self._peers.values()]
            tmp = self._peers_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            tmp.replace(self._peers_file)
        except OSError as exc:
            log.warning("hub peers persist failed: %s", exc)

    def _restore_peers(self) -> None:
        """Rebuild hub peers from the persisted file on gateway startup."""
        try:
            if not self._peers_file.exists():
                return
            data = json.loads(self._peers_file.read_text(encoding="utf-8"))
            # P2-2: lock peer writes during restore.
            with self._peers_lock:
                for d in data:
                    peer = _HubPeer(
                        peer_id=d.get("peer_id", ""),
                        session_id=d.get("session_id", ""),
                        agent_id=d.get("agent_id", ""),
                        host_alias=d.get("host_alias", ""),
                        mailbox_root=d.get("mailbox_root", ""),
                        status=d.get("status", "unknown"),
                        registered_at=d.get("registered_at", 0.0),
                    )
                    if peer.peer_id:
                        self._peers[peer.peer_id] = peer
                if self._peers:
                    log.info("gateway: restored %d hub peer(s)", len(self._peers))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("hub peers restore failed: %s", exc)

    def _restore_runtimes_from_park(self) -> None:
        """Rebuild in-memory runtime records from HOT_PARKED park manifests.

        A2: restored records are PLACEHOLDERS, not live runtimes — they are
        created with status='unknown' and last_activity=0 (no liveness
        signal). Only a fresh plugin re-register (heartbeat NOT_FOUND →
        plugin re-registers) flips them to active; until then probes report
        not-alive instead of faking a hot runtime.
        """
        try:
            from codeagent.park.registry import ParkRegistry

            manifests = ParkRegistry().list_active()
        except Exception as exc:
            log.warning("gateway: park restore unavailable: %s", exc)
            return
        restored = 0
        # P2-2: lock runtime writes during park restore.
        with self._runtimes_lock:
            for m in manifests:
                if not m.swarm_session_id or not m.backend_session_id:
                    continue
                # P3-g: use full review_key suffix (up to 32 chars) to avoid
                # collision from 12-char truncation; prefix with random hex if
                # key is still very short.
                _rk_slug = m.review_key.replace(':', '-')
                if len(_rk_slug) >= 24:
                    runtime_id = f"park-{_rk_slug[-24:]}"
                else:
                    runtime_id = f"park-{_rk_slug}-{secrets.token_hex(4)}"
                if runtime_id in self._runtimes:
                    continue
                self._runtimes[runtime_id] = RuntimeRecord(
                    runtime_id=runtime_id,
                    session_id=m.swarm_session_id,
                    agent_id=m.mailbox_agent_id or "oracle",
                    generation=int(m.round or 0) + 1,
                    review_key=m.review_key,
                    backend_session_id=m.backend_session_id,
                    host_alias=m.host or "__local__",
                    runtime=m.agent_type or "omp",  # A2: read agent backend from manifest
                    status="unknown",  # A2: placeholder — not a live runtime
                    created_at=m.created_at and datetime.fromtimestamp(m.created_at, tz=timezone.utc).strftime(ISO_TIMESTAMP_FORMAT) or "",
                    last_activity=0.0,  # A2: no heartbeat signal until re-register
                    started_at=m.created_at,  # A12: manifest created_at as start marker
                    # 三维（设计 §1）：HOT_PARKED 占位 —— presence 非 alive
                    # （不得伪装 hot），binding 有 backend session 事实，
                    # agent_state=ended（parked → ended）。
                    presence=PRESENCE_STALE,
                    binding=BINDING_BOUND if m.backend_session_id else BINDING_PENDING,
                    agent_state=AGENT_ENDED,
                )
                self._persist_control_state(self._runtimes[runtime_id])
                restored += 1
        if restored:
            log.info("gateway: restored %d runtime(s) from park registry", restored)

    def stop(self) -> None:
        """Stop the presence sweep loop (idempotent)."""
        self._sweep_stop.set()
        if self._sweep_thread is not None and self._sweep_thread.is_alive():
            self._sweep_thread.join(timeout=5)
            self._sweep_thread = None

    # ── P8.3 presence sweep ────────────────────────────────────────────

    def _start_sweep_loop(self) -> None:
        """Background daemon thread — marks stale runtimes offline."""
        self._sweep_thread = threading.Thread(target=self._sweep_loop, daemon=True)
        self._sweep_thread.start()

    def _sweep_loop(self) -> None:
        while not self._sweep_stop.wait(self._sweep_interval):
            try:
                self._sweep_once()
                self._retention_sweep()
            except Exception as exc:
                log.warning("gateway: sweep iteration failed: %s", exc)

    def _retention_sweep(self) -> None:
        """A6: hourly retention — EventStore.sweep() + outbox TTL sweep.

        EventStore.sweep() prunes TOOL_* detail rows older than 7 days
        (previously dead code); the DeliveryEngine outbox sweep removes
        terminal delivered entries/markers past their TTL. Both are
        time-throttled to once per hour per gateway process.
        """
        now = time.time()
        if now - self._last_retention_sweep < self._retention_sweep_interval:
            return
        self._last_retention_sweep = now
        try:
            removed = self._events.sweep()
            if removed:
                log.info("gateway: events retention sweep removed %d row(s)", removed)
        except Exception as exc:
            log.warning("gateway: events retention sweep failed: %s", exc)
        if self._engine is not None:
            try:
                cleaned = self._engine.sweep()
                if cleaned:
                    log.info("gateway: outbox retention sweep removed %d entry(ies)", cleaned)
            except Exception as exc:
                log.warning("gateway: outbox retention sweep failed: %s", exc)

    def _sweep_once(self) -> list[str]:
        """Mark active-but-stale runtimes offline; sync hub peers.

        Returns the list of runtime ids transitioned to offline.
        Lock-protected: a concurrent heartbeat/event that refreshed
        last_activity between the scan and the transition re-verifies
        under the lock — no false offline flip (P8 review Q5 race fix).
        """
        offline: list[str] = []
        with self._runtimes_lock:
            now = time.time()
            for rid, record in list(self._runtimes.items()):
                if record.status != "active":
                    continue
                # Re-verify under the lock: a heartbeat/event may have
                # refreshed last_activity since the initial scan.
                # Cold-start grace: double the timeout for runtimes < 3 min old
                # so slow-starting processes aren't prematurely swept offline.
                timeout = self._offline_timeout
                if record.created_at:
                    try:
                        created_ts = datetime.fromisoformat(
                            record.created_at
                        ).timestamp()
                        if (now - created_ts) < 180:
                            timeout = self._offline_timeout * 2
                    except (ValueError, OSError):
                        pass
                if (now - record.last_activity) <= timeout:
                    continue
                record.status = "offline"
                record.presence = PRESENCE_STALE  # 三维：alive → stale
                offline.append(rid)
                try:
                    self._events.append_local(RuntimeEventDraft(
                        runtime_id=rid, generation=record.generation,
                        session_id=record.session_id, agent_id=record.agent_id,
                        request_id="", run_id="", kind="AGENT_STATUS",
                        created_at=datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT),
                        payload={
                            "old_status": "active", "new_status": "offline",
                            "reason": "heartbeat_timeout",
                            "last_activity": record.last_activity,
                            "presence": record.presence,
                            "binding": record.binding,
                            "agent_state": record.agent_state,
                        },
                    ))
                except Exception as exc:
                    log.warning("gateway: offline event append failed: %s", exc)
                # 离线是三维状态变化 → 镜像到控制面。
                self._persist_control_state(record)
            # 三维 presence 衰变：stale 持续超过 _dead_timeout → dead。
            for rid, rec in list(self._runtimes.items()):
                if (rec.status == "offline" and rec.presence == PRESENCE_STALE
                        and rec.last_activity > 0
                        and (now - rec.last_activity) > self._dead_timeout):
                    rec.presence = PRESENCE_DEAD
                    self._persist_control_state(rec)
            # A3: dropped stopped records — runtime_stop keeps the record
            # briefly (so the stopping caller can read back the state), the
            # sweep removes it so stopped runtimes never linger/aggregate.
            for rid in [rid for rid, rec in list(self._runtimes.items()) if rec.status == "stopped"]:
                self._runtimes.pop(rid, None)
                # 同步清理 ControlStore.runtime_generations 记录
                try:
                    self._control.delete_generation(rid)
                except Exception as exc:
                    log.warning("gateway: control generation delete failed for %s: %s", rid, exc)
            # A9: prune expired session claims — _claims grows unboundedly
            # otherwise (TTL 3600s default, takeover already handles expiry).
            if self._claims:
                expired = [k for k, c in self._claims.items() if c["expires_at"] <= now]
                for k in expired:
                    self._claims.pop(k, None)
        # P8.1 presence 联动：关联 hub peer 同步 offline（锁外，避免死锁）。
        # P2-2: snapshot rid→(session_id, agent_id) under runtimes lock FIRST,
        # then sync peers under peers lock without touching _runtimes.
        if offline:
            offline_meta: dict[str, tuple[str, str]] = {}
            with self._runtimes_lock:
                for rid in offline:
                    record = self._runtimes.get(rid)
                    if record is not None:
                        offline_meta[rid] = (record.session_id, record.agent_id)
            with self._peers_lock:
                for rid, (sid, aid) in offline_meta.items():
                    for peer in self._peers.values():
                        if peer.agent_id == aid and peer.session_id == sid:
                            peer.status = "offline"
                if offline_meta:
                    self._save_peers()
        return offline

    # ── capabilities ───────────────────────────────────────────────────

    def capabilities(self, params: Optional[dict] = None) -> dict:
        from codeagent.runtime.registry import RuntimeRegistry  # lazy (P5)

        try:
            runtimes = sorted(RuntimeRegistry().names())
        except Exception as exc:
            # Degraded: capability listing failure must not take the gateway
            # down, but must NOT be silent (it once masked an unbound-method bug).
            log.warning("gateway: runtime capability listing failed: %s", exc)
            runtimes = ["omp"]
        return {
            "version": GATEWAY_PROTOCOL_VERSION,
            "runtimes": runtimes,
            "features": [
                "runtime_events", "message_receipts", "park", "events_cursor",
            ],
        }

    # ── session.ensure ─────────────────────────────────────────────────

    def session_ensure(self, params: dict) -> dict:
        """Create (or merge) a session from the Manager's authoritative manifest.

        The remote gateway caches a READ-ONLY copy of the Manager's
        manifest — it never invents its own manager/roster for the same
        session id.
        """
        session_id = params.get("session_id", "")
        manager_id = params.get("manager_id", "")
        roster = params.get("roster", []) or []
        acl = params.get("acl")
        if not session_id or not manager_id:
            raise GatewayError("PROTOCOL", "session.ensure requires session_id + manager_id")
        # Validate against an existing local session if present (authority check).
        existing = self._store.read_session(session_id)
        if existing is not None:
            old_manager = existing.get("manager", "")
            if old_manager and old_manager != manager_id:
                raise GatewayError(
                    "MANIFEST_CONFLICT",
                    f"session {session_id} already has manager={old_manager!r}; "
                    f"refusing manager={manager_id!r}",
                )
        self._store.session_init(
            session_id, manager_id, [a for a in roster if a != manager_id],
            acl=acl,
        )
        meta = self._store.read_session(session_id) or {}
        return {
            "session_id": session_id,
            "manager_id": meta.get("manager", manager_id),
            "roster": sorted(set(meta.get("agents", [])) | ({manager_id} if manager_id else set())),
            "manifest_revision": meta.get("manifest_revision", 1),
        }

    # ── runtime lifecycle ──────────────────────────────────────────────

    def runtime_register(self, params: dict) -> dict:
        """Plugin handshake — validate identity, return initial task.

        Identity: session_id/agent_id/runtime_id/review_key/generation/
        gateway_socket/owner_pid/nonce. Registration is owner-scoped:
        only the launcher's owner_pid+nonce+generation may register.
        """
        session_id = params.get("session_id", "")
        agent_id = params.get("agent_id", "")
        runtime_id = params.get("runtime_id", "")
        generation = int(params.get("generation", 0) or 0)
        review_key = params.get("review_key", "")
        backend_session_id = params.get("backend_session_id", "")
        runtime = params.get("runtime", "omp")
        owner_pid = int(params.get("owner_pid", 0) or 0)
        nonce = params.get("nonce", "")
        # 设计 §2：插件握手上报能力（park_revive / correlated_turn_ack）。
        capabilities = [str(c) for c in (params.get("capabilities") or [])]

        if not session_id or not agent_id or not runtime_id:
            raise GatewayError(ERR_NOT_AUTHORIZED, "runtime.register requires session_id/agent_id/runtime_id")

        # P2-2: lock the entire check-then-set for runtimes dict.
        with self._runtimes_lock:
            # Generation staleness: a registration for an OLDER generation of a
            # known runtime_id is rejected (fail closed).
            existing = self._runtimes.get(runtime_id)
            if existing is not None and generation < existing.generation:
                raise GatewayError(
                    ERR_GENERATION_STALE,
                    f"generation {generation} < registered {existing.generation} for {runtime_id}",
                )
            # A7: owner identity consistency — a SAME-generation re-registration
            # must present the same owner_pid+nonce the gateway stored (the
            # supervisor writes identity.json 0600 and the plugin echoes it back).
            # A2/A2.3: a NEW generation (launcher restart after stop/removal) is
            # allowed to take over with a fresh identity — recovery path.
            if existing is not None and generation == existing.generation:
                if (existing.owner_pid or existing.nonce) and (
                    existing.owner_pid != owner_pid or existing.nonce != nonce
                ):
                    raise GatewayError(
                        ERR_OWNER_MISMATCH,
                        f"owner identity mismatch for {runtime_id}: "
                        f"pid {existing.owner_pid} != {owner_pid} or nonce mismatch",
                    )

            # Roster membership (authoritative session.json).
            meta = self._store.read_session(session_id)
            if meta is None:
                raise GatewayError(ERR_NOT_AUTHORIZED, f"session not found: {session_id}")
            roster = {meta.get("manager", "")} | set(meta.get("agents", []))
            if agent_id not in roster:
                raise GatewayError(ERR_NOT_AUTHORIZED, f"agent {agent_id!r} not in session roster")

            # binding 代数：backend session (重)绑定计数 —— session 更换时 +1，
            # 用于 ack/投递时校验 binding 未被陈旧引用覆盖。
            binding_epoch = 0
            if existing is not None:
                binding_epoch = existing.binding_epoch
                if backend_session_id and backend_session_id != existing.backend_session_id:
                    binding_epoch += 1
            elif backend_session_id:
                binding_epoch = 1

            record = RuntimeRecord(
                runtime_id=runtime_id,
                session_id=session_id,
                agent_id=agent_id,
                generation=generation,
                review_key=review_key,
                backend_session_id=backend_session_id,
                runtime=runtime,
                owner_pid=owner_pid,
                nonce=nonce,
                status="active",
                created_at=datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT),
                last_activity=time.time(),
                started_at=time.time(),  # A12: real elapsed baseline
                # 三维（设计 §1）：register = session_ready → idle + alive；
                # binding 依 backend_session_id 是否在场。
                presence=PRESENCE_ALIVE,
                binding=BINDING_BOUND if backend_session_id else BINDING_PENDING,
                agent_state=AGENT_IDLE,
                binding_epoch=binding_epoch,
                capabilities=capabilities,
            )
            # Preserve spec/initial_task from a prior spawn of the same runtime.
            if existing is not None:
                record.spec = existing.spec
                record.initial_task = existing.initial_task
            self._runtimes[runtime_id] = record
            self._persist_control_state(record)
            if review_key:
                try:
                    self._control.upsert_review(
                        review_key=review_key, swarm_session_id=session_id,
                        runtime_id=runtime_id, profile_id=agent_id,
                        mailbox_agent_id=agent_id,
                    )
                except Exception as exc:
                    log.warning("gateway: review mirror failed for %s: %s", review_key, exc)

        # Sync the real backend session id into the park manifest so warm
        # resume survives a gateway restart (in-memory record lost).
        if review_key and backend_session_id:
            try:
                from codeagent.domain.park import Lifecycle
                from codeagent.park.registry import ParkRegistry

                registry = ParkRegistry()
                m = registry.lookup(review_key)
                # A1: only warm up HOT_PARKED leases — a RELEASED manifest
                # must NOT be resurrected by a plugin re-register.
                if m is not None and m.lifecycle == Lifecycle.HOT_PARKED:
                    from dataclasses import replace

                    registry.update(review_key, replace(
                        m,
                        backend_session_id=backend_session_id,
                        lifecycle=Lifecycle.HOT_PARKED,
                    ))
            except Exception as exc:
                log.warning("gateway: park backend sync failed: %s", exc)

        # Initial task: the oldest TASK addressed to this agent, if any.
        initial_task = ""
        initial_task_msg_id = ""
        try:
            inbox = self._store.agent_subdir(session_id, agent_id, "inbox")
            files = self._store.list_messages(inbox)
            for f in files:
                try:
                    msg = json.loads(f.read_bytes())
                except (json.JSONDecodeError, OSError):
                    continue
                if msg.get("kind") == "TASK":
                    initial_task = msg.get("body", "")
                    initial_task_msg_id = msg.get("msg_id", "")
                    break
        except Exception as exc:
            log.warning("gateway: initial task scan failed: %s", exc)
        record.initial_task = initial_task

        self._events.append_local(RuntimeEventDraft(
            runtime_id=runtime_id, generation=generation,
            session_id=session_id, agent_id=agent_id,
            request_id="", run_id="", kind="RUNTIME_STATE",
            created_at=record.created_at,
            payload={"state": "active", "runtime": runtime, "backend_session_id": backend_session_id},
        ))
        return {
            "runtime_id": runtime_id,
            "session_id": session_id,
            "agent_id": agent_id,
            "generation": generation,
            "initial_task": initial_task,
            "initial_task_msg_id": initial_task_msg_id,
        }

    def runtime_declare(self, params: dict) -> dict:
        """P3: Weak presence declaration for native_resume sessions.

        Unlike runtime.register, this does NOT require owner_pid/nonce —
        there is no plugin handshake.  It registers a placeholder record
        for status/presence awareness so ``runtime.info`` / heartbeat sweep
        can see the session.  ``review_key`` must exist in the park registry
        to prevent arbitrary runtime forgery (A7 weak gate).

        Params: review_key, backend_session_id, mode (default 'native_resume'),
                runtime_id (optional; derived if absent), agent_id (default 'oracle').
        """
        review_key = params.get("review_key", "")
        backend_session_id = params.get("backend_session_id", "")
        mode = params.get("mode", "native_resume")
        runtime_id = params.get("runtime_id", "")
        agent_id = params.get("agent_id", "oracle")

        if not review_key:
            raise GatewayError(ERR_NOT_AUTHORIZED, "runtime.declare requires review_key")
        if not backend_session_id:
            raise GatewayError(ERR_NOT_AUTHORIZED, "runtime.declare requires backend_session_id")

        # A7: review_key must exist in the park registry — this is the weak
        # identity gate for declare (prevents arbitrary runtime forgery).
        from codeagent.park.registry import ParkRegistry

        manifest = ParkRegistry().lookup(review_key)
        if manifest is None:
            raise GatewayError(ERR_NOT_FOUND, f"review_key not found: {review_key}")

        session_id = manifest.swarm_session_id or ""

        with self._runtimes_lock:
            existing = None
            if runtime_id:
                existing = self._runtimes.get(runtime_id)
            if existing is None:
                # Idempotent lookup by backend_session_id (oracle omits runtime_id).
                for rec in self._runtimes.values():
                    if rec.backend_session_id == backend_session_id:
                        existing = rec
                        runtime_id = rec.runtime_id
                        break

            if existing is not None:
                # Idempotent re-declare: refresh activity.
                existing.status = "active"
                existing.last_activity = time.time()
                existing.presence = PRESENCE_ALIVE
                existing.binding = BINDING_BOUND
                existing.agent_state = AGENT_IDLE
                record = existing
            else:
                if not runtime_id:
                    import hashlib

                    slug = hashlib.sha256(
                        backend_session_id.encode()
                    ).hexdigest()[:16]
                    runtime_id = f"native-{slug}"
                record = RuntimeRecord(
                    runtime_id=runtime_id,
                    session_id=session_id,
                    agent_id=agent_id,
                    generation=0,
                    review_key=review_key,
                    backend_session_id=backend_session_id,
                    runtime="native",
                    status="active",
                    created_at=datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT),
                    last_activity=time.time(),
                    # 三维：declare = session_ready → idle + alive + bound
                    presence=PRESENCE_ALIVE,
                    binding=BINDING_BOUND,
                    agent_state=AGENT_IDLE,
                    binding_epoch=1,
                )
                self._runtimes[runtime_id] = record
            self._persist_control_state(record)
            if review_key:
                try:
                    self._control.upsert_review(
                        review_key=review_key, swarm_session_id=session_id,
                        runtime_id=runtime_id, profile_id=agent_id,
                        mailbox_agent_id=agent_id,
                    )
                except Exception as exc:
                    log.warning("gateway: review mirror failed for %s: %s", review_key, exc)

        return {
            "runtime_id": runtime_id,
            "status": record.status,
            "mode": mode,
        }

    def runtime_spawn(self, params: dict) -> dict:
        """Spawn a runtime via the launcher + supervisor.

        Delegates to the RuntimeRegistry adapter (P5); the record keeps
        spec for probe/stop and the plugin handshake.
        """
        runtime = params.get("runtime", "omp")
        session_id = params.get("session_id", "")
        agent_id = params.get("agent_id", "")
        review_key = params.get("review_key", "")
        workdir = params.get("workdir", "")
        task = params.get("task", "")
        model = params.get("model", "")
        short_task = bool(params.get("short_task", False))

        from codeagent.runtime.registry import RuntimeRegistry

        registry = RuntimeRegistry()
        handle = registry.spawn(
            runtime,
            request={
                "session_id": session_id,
                "agent_id": agent_id,
                "review_key": review_key,
                "workdir": workdir,
                "task": task,
                "model": model,
                "short_task": short_task,
            },
        )
        record = RuntimeRecord(
            runtime_id=handle.runtime_id,
            session_id=session_id,
            agent_id=agent_id,
            generation=handle.generation,
            review_key=review_key,
            backend_session_id=handle.backend_session_id,
            host_alias=handle.host_alias,
            runtime=runtime,
            status="active",
            created_at=datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT),
            last_activity=time.time(),
            started_at=time.time(),  # A12: real elapsed baseline
            spec=params,
            initial_task=task,
            # 三维：spawn 后进程已起但 backend session 尚未经插件握手注册
            # → presence alive + binding pending + idle。
            presence=PRESENCE_ALIVE,
            binding=BINDING_PENDING,
            agent_state=AGENT_IDLE,
        )
        # P2-2: lock runtimes dict write.
        with self._runtimes_lock:
            self._runtimes[handle.runtime_id] = record
            self._persist_control_state(record)
            if review_key:
                try:
                    self._control.upsert_review(
                        review_key=review_key, swarm_session_id=session_id,
                        runtime_id=handle.runtime_id, profile_id=agent_id,
                        mailbox_agent_id=agent_id,
                    )
                except Exception as exc:
                    log.warning("gateway: review mirror failed for %s: %s", review_key, exc)
        return {
            "runtime_id": handle.runtime_id,
            "generation": handle.generation,
            "backend_session_id": handle.backend_session_id,
            "mode": getattr(handle, "mode", ""),
            "capabilities": sorted(getattr(handle, "capabilities", [])),
        }

    def runtime_heartbeat(self, params: dict) -> dict:
        runtime_id = params.get("runtime_id", "")
        # A2.3: NOT_FOUND here is the RECOVERY trigger — the plugin treats it
        # as "gateway lost me (restart/stop)" and re-registers with a NEW
        # generation; runtime_register accepts the re-register (see A7/A2.3
        # owner check — a newer generation takes over the runtime_id).
        record = self._require_runtime(runtime_id)
        with self._runtimes_lock:
            # P3-d: re-read record under lock to avoid stale-dict read after
            # a concurrent runtime_register replaced the record object.
            record = self._runtimes.get(runtime_id)
            if record is None:
                raise GatewayError(ERR_NOT_FOUND, f"runtime disappeared during heartbeat: {runtime_id}")
            was_offline = record.status == "offline"
            record.last_activity = time.time()
            # 三维：heartbeat → 只更新 presence（设计 §1 归约表）。
            record.presence = PRESENCE_ALIVE
            # P8.3: heartbeat restores an offline runtime.
            if was_offline:
                record.status = "active"
                self._persist_control_state(record)  # stale→alive 是状态变化
                try:
                    self._events.append_local(RuntimeEventDraft(
                        runtime_id=runtime_id, generation=record.generation,
                        session_id=record.session_id, agent_id=record.agent_id,
                        request_id="", run_id="", kind="AGENT_STATUS",
                        created_at=datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT),
                        payload={"old_status": "offline", "new_status": "active", "reason": "heartbeat"},
                    ))
                except Exception as exc:
                    log.warning("gateway: restore event append failed: %s", exc)
        if was_offline:
            with self._peers_lock:
                for peer in self._peers.values():
                    if peer.agent_id == record.agent_id and peer.session_id == record.session_id:
                        peer.status = "online"
            self._save_peers()
        # Park lease renewal happens in-process (no subprocess).
        if record.review_key:
            try:
                from codeagent.park.registry import ParkRegistry

                ParkRegistry().renew(record.review_key)
            except Exception as exc:
                log.warning("gateway: park renew failed: %s", exc)
        return {"runtime_id": runtime_id, "status": record.status}

    def runtime_status(self, params: dict) -> dict:
        """Query one runtime's presence status (in-memory, no probe)."""
        runtime_id = params.get("runtime_id", "")
        record = self._require_runtime(runtime_id)
        return {
            "runtime_id": runtime_id,
            "status": record.status,
            "last_activity": record.last_activity,
            "host_alias": record.host_alias,
            "session_id": record.session_id,
            "agent_id": record.agent_id,
            # 三维（设计 §1）—— 新增字段向后兼容。
            "presence": record.presence,
            "binding": record.binding,
            "agent_state": record.agent_state,
            "binding_epoch": record.binding_epoch,
        }

    # ── Q5b: runtime.context（default 继承主 agent 模型）───────────────

    def runtime_context_set(self, params: dict) -> dict:
        """Q5b: 原子更新一个 runtime 的 model_context（插件 model_change 上报）。

        参数：runtime_id（必填）、provider/model/variant/epoch（epoch 为
        模型切换代数，便于调用方判断上下文新鲜度）。

        语义：记录主 agent 当前已解析模型，供 oracle CLI default 继承
        （AIMESHCHAT_RUNTIME_ID → runtime.context_get）。与 heartbeat 不同，
        不更新 last_activity —— 模型切换不是活性信号，避免掩盖失联 runtime。
        """
        runtime_id = params.get("runtime_id", "")
        if not runtime_id:
            raise GatewayError(ERR_PROTOCOL, "runtime.context_set requires runtime_id")
        provider = params.get("provider", "") or ""
        model = params.get("model", "") or ""
        variant = params.get("variant", "") or ""
        epoch_raw = params.get("epoch", 0)
        try:
            epoch = int(epoch_raw or 0)
        except (TypeError, ValueError):
            raise GatewayError(ERR_PROTOCOL, "runtime.context_set epoch must be an int")
        with self._runtimes_lock:
            record = self._runtimes.get(runtime_id)
            if record is None:
                raise GatewayError(ERR_NOT_FOUND, f"unknown runtime: {runtime_id}")
            # 与 runtime_event 同款 generation 校验：提供则必须匹配，
            # 防止陈旧代际的插件把新 runtime 的上下文覆盖掉。
            generation = params.get("generation")
            if generation is not None:
                try:
                    generation = int(generation)
                except (TypeError, ValueError):
                    raise GatewayError(ERR_PROTOCOL, "runtime.context_set generation must be an int")
                if generation != record.generation:
                    raise GatewayError(
                        ERR_GENERATION_STALE,
                        f"generation {generation} != registered {record.generation} "
                        f"for {runtime_id}",
                    )
            record.model_context = {
                "provider": provider,
                "model": model,
                "variant": variant,
                "epoch": epoch,
            }
            snapshot = dict(record.model_context)
        return {"runtime_id": runtime_id, "model_context": snapshot}

    def runtime_context_get(self, params: dict) -> dict:
        """Q5b: 查询一个 runtime 的 model_context（runtime_id 或 review_key）。

        与 runtime_info 相同 A3 语义：优先 live 记录，过滤 stopped。
        记录存在但尚未上报（无 model_context）→ 返回空 dict，由调用方
        决定（oracle CLI → MODEL_CONTEXT_UNAVAILABLE）。
        """
        runtime_id = params.get("runtime_id", "")
        review_key = params.get("review_key", "")
        with self._runtimes_lock:
            record = None
            if runtime_id:
                record = self._runtimes.get(runtime_id)
                # A3: stopped 记录视为不存在（sweep 前不报旧答案）。
                if record is not None and record.status == "stopped":
                    record = None
            elif review_key:
                candidates = [r for r in self._runtimes.values() if r.review_key == review_key]
                live = [r for r in candidates if r.status != "stopped"]
                record = max(live, key=lambda r: r.last_activity) if live else None
            if record is None:
                raise GatewayError(
                    ERR_NOT_FOUND,
                    f"no runtime for runtime_id={runtime_id!r} review_key={review_key!r}",
                )
            snapshot = dict(record.model_context or {})
            record_id = record.runtime_id
            record_key = record.review_key
        return {
            "runtime_id": record_id,
            "review_key": record_key,
            "model_context": snapshot,
        }

    def runtimes_list(self, params: dict) -> dict:
        """List registered runtimes and their presence status."""
        session_id = params.get("session_id", "")
        results = []
        # P2-2: snapshot runtimes under lock to avoid dict resize during iteration.
        with self._runtimes_lock:
            snapshot = list(self._runtimes.items())
        for rid, rec in snapshot:
            if session_id and rec.session_id != session_id:
                continue
            results.append({
                "runtime_id": rid,
                "session_id": rec.session_id,
                "agent_id": rec.agent_id,
                "status": rec.status,
                "last_activity": rec.last_activity,
                "host_alias": rec.host_alias,
                "review_key": rec.review_key,
                "backend_session_id": rec.backend_session_id,
                "presence": rec.presence,
                "binding": rec.binding,
                "agent_state": rec.agent_state,
                "binding_epoch": rec.binding_epoch,
            })
        return {"runtimes": results}

    def runtime_event(self, params: dict) -> dict:
        """Append a producer event (plugin/supervisor) to the EventStore."""
        draft = RuntimeEventDraft.from_dict(params.get("event", {}) or {})
        # P3-h: hold the lock across generation check + append + liveness
        # update so a concurrent runtime_register cannot swap the record
        # between the check and the append (TOCTOU on generation).
        with self._runtimes_lock:
            record = self._runtimes.get(draft.runtime_id)
            if record is None:
                raise GatewayError(ERR_NOT_FOUND, f"unknown runtime: {draft.runtime_id}")
            if draft.generation != record.generation:
                raise GatewayError(
                    ERR_GENERATION_STALE,
                    f"generation {draft.generation} != registered {record.generation} "
                    f"for {draft.runtime_id}",
                )
            # Producers never number their own events — ignore host/sequence.
            ev = self._events.append_local(draft)
            # Any event is activity: keep the runtime's liveness window fresh
            # (hot detection + park renew cadence).
            record.last_activity = time.time()
            # 三维归约（设计 §1）：TURN_STARTED → agent_running；
            # payload 可显式携带 agent_state/binding 跳转（插件经事件上报）。
            before = (record.presence, record.binding, record.agent_state)
            if draft.kind == "TURN_STARTED":
                record.agent_state = AGENT_RUNNING
            if isinstance(draft.payload, dict):
                st = draft.payload.get("agent_state")
                if st in (AGENT_RUNNING, AGENT_IDLE, AGENT_ENDED):
                    record.agent_state = st
                bind = draft.payload.get("binding")
                if bind in (BINDING_BOUND, BINDING_PENDING, BINDING_LOST):
                    record.binding = bind
            if (record.presence, record.binding, record.agent_state) != before:
                self._persist_control_state(record)
        return {"event_id": ev.event_id, "source_sequence": ev.source_sequence}

    def runtime_send(self, params: dict) -> dict:
        """设计 §3：持久命令状态机投递。

        状态：QUEUED → CLAIMED → REVIVING → TRIGGERING → TURN_TRIGGERED
        失败旁路：FAILED_SAFE / AMBIGUOUS / TRIGGER_UNKNOWN。

        幂等：request_id+payload_hash 为幂等键 —— 同一对键返回原
        command/turn，不重复注入；同 request_id 不同 payload →
        IDEMPOTENCY_CONFLICT。

        返回语义（status 字段，设计 §3 表）：
          mailbox_persisted  仅写入持久队列（QUEUED）
          claimed            插件已领取（CLAIMED）
          session_live       revive/binding 完成（REVIVING）
          turn_triggered     OMP 已建立关联 turn（成功，需 TURN_TRIGGERED ack）
          binding_pending    未注入（binding 未建立），允许稍后重试
          failed_safe        明确未触发，可安全重试
          ambiguous          可能已触发，禁止自动重投

        不承诺虚假 exactly-once：TRIGGERING 崩溃后重查 → AMBIGUOUS。
        TURN_TRIGGERED ack 链（插件相关 ack 握手）为 TODO —— 本方法只
        返回已确认阶段，绝不把未确认投递报成 turn_triggered。
        """
        runtime_id = params.get("runtime_id", "")
        request_id = params.get("request_id", "")
        body = params.get("body", "")
        if not runtime_id or not request_id:
            raise GatewayError(ERR_PROTOCOL, "runtime.send requires runtime_id + request_id")
        record = self._require_runtime(runtime_id)
        payload_hash = params.get("payload_hash", "") or _sha256(body)
        if not payload_hash:
            raise GatewayError(ERR_PROTOCOL, "runtime.send requires a non-empty body or payload_hash")

        # ── 幂等重放：同一 request_id+payload_hash 返回原 command/turn ──
        existing = self._control.get_command(request_id)
        if existing is not None:
            if existing["payload_hash"] != payload_hash:
                raise GatewayError(
                    ERR_IDEMPOTENCY_CONFLICT,
                    f"request_id {request_id!r} already used with a different payload",
                    {"request_id": request_id, "state": existing["state"]},
                )
            return self._command_result(existing)

        # ── 1) 入队（关键事务 synchronous=FULL）─────────────────────────
        command_id = f"cmd-{uuid4().hex}"
        created = self._control.enqueue_command(
            request_id=request_id,
            command_id=command_id,
            msg_id="",  # mailbox 写入后回填
            runtime_id=runtime_id,
            generation=record.generation,
            payload_hash=payload_hash,
            state=CMD_QUEUED,
            binding_epoch=record.binding_epoch,
            backend_session_id=record.backend_session_id,
        )
        if not created:
            # 并发入队：另一线程先到 → 走幂等重放。
            return self._command_result(self._control.get_command(request_id))

        # ── 2) 持久队列：mailbox 写入（真实投递载体）────────────────────
        try:
            receipt = self._svc.send(
                session_id=record.session_id,
                from_id=params.get("from", "manager"),
                to_id=record.agent_id,
                subject=params.get("subject", "steer"),
                body=body,
                kind=params.get("kind", "TASK"),
                reply_to=params.get("reply_to", ""),
                run_id=params.get("run_id", ""),
                request_id=request_id,
                require_ack=bool(params.get("require_ack", False)),
            )
            if receipt.status == "failed":
                raise GatewayError(ERR_NOT_FOUND, receipt.error or "runtime_send mailbox failed")
        except Exception as exc:
            # 明确未触发（mailbox 未写入即未投递）→ FAILED_SAFE，可安全重试。
            self._control.update_command(
                request_id, state=CMD_FAILED_SAFE, detail={"reason": str(exc)},
            )
            return self._command_result(self._control.get_command(request_id))
        self._control.update_command(
            request_id, msg_id=receipt.msg_id, state=CMD_QUEUED,
            detail={"mailbox": "persisted", "msg_id": receipt.msg_id},
        )

        # ── 3) hot 门控（三维，设计 §1）────────────────────────────────
        with self._runtimes_lock:
            cur = self._runtimes.get(runtime_id)
            if cur is not None:
                record = cur
        if not self._is_hot(record):
            gate_detail = {
                "presence": record.presence, "binding": record.binding,
                "agent_state": record.agent_state,
            }
            if record.binding != BINDING_BOUND:
                # 未注入：binding 未建立 → binding_pending，允许稍后重试。
                self._control.update_command(
                    request_id, state=CMD_QUEUED,
                    detail={"gate": "binding_pending", **gate_detail},
                )
                return self._command_result(self._control.get_command(request_id))
            # presence 非 alive → 仅持久队列（mailbox_persisted）。
            self._control.update_command(
                request_id, state=CMD_QUEUED,
                detail={"gate": "not_hot", **gate_detail},
            )
            return self._command_result(self._control.get_command(request_id))

        # ── 4) ended/parked → REVIVING：发起 park-revive ────────────────
        if record.agent_state == AGENT_ENDED:
            self._control.update_command(
                request_id, state=CMD_REVIVING,
                binding_epoch=record.binding_epoch,
                backend_session_id=record.backend_session_id,
                detail={"revive": "starting", "agent_state": record.agent_state},
            )
            try:
                from codeagent.park.router import park_revive

                rv = park_revive(record.review_key, body)
                if rv.success and rv.method in ("hot", "warm"):
                    # revive/binding 完成（hot/warm 保活原 backend session）
                    # → session_live；最终 turn 关联依赖 ack 链（TODO）。
                    self._control.update_command(
                        request_id, state=CMD_REVIVING,
                        detail={"revive": rv.method},
                    )
                    return self._command_result(self._control.get_command(request_id))
                if rv.success and rv.method == "cold":
                    # cold 复活 = 需新 spawn + 重新绑定后才能注入 → 回到
                    # QUEUED（binding_pending，允许稍后重试）。
                    self._control.update_command(
                        request_id, state=CMD_QUEUED,
                        detail={"gate": "binding_pending", "revive": "cold"},
                    )
                    return self._command_result(self._control.get_command(request_id))
                self._control.update_command(
                    request_id, state=CMD_FAILED_SAFE,
                    detail={"revive_failed": rv.method},
                )
                return self._command_result(self._control.get_command(request_id))
            except Exception as exc:
                self._control.update_command(
                    request_id, state=CMD_FAILED_SAFE,
                    detail={"revive_error": str(exc)},
                )
                return self._command_result(self._control.get_command(request_id))

        # ── 5) hot（agent_running/idle）：mailbox 持久队列已写入 ────────
        # OMP turn 关联需插件精确 claim + TURN_TRIGGERED ack（ack 链 TODO）；
        # 不承诺虚假 exactly-once → 保持 QUEUED，返回 mailbox_persisted，
        # 后续经 runtime.command_ack 推进状态。
        return self._command_result(self._control.get_command(request_id))

    def runtime_lifecycle(self, params: dict) -> dict:
        """三维状态归约入口（设计 §1 权威事件）。

        Params: {runtime_id, event, payload?}
        event ∈ {session_ready, agent_start, turn_start, turn_end, agent_end,
                 session_shutdown, registry_parked, registry_removed,
                 process_exit, heartbeat}
        """
        runtime_id = params.get("runtime_id", "")
        event = params.get("event", "")
        if not runtime_id or event not in LIFECYCLE_EVENTS:
            raise GatewayError(
                ERR_PROTOCOL,
                "runtime.lifecycle requires runtime_id + known event; "
                f"got event={event!r}",
            )
        with self._runtimes_lock:
            record = self._runtimes.get(runtime_id)
            if record is None:
                raise GatewayError(ERR_NOT_FOUND, f"unknown runtime: {runtime_id}")
            if event == "heartbeat":
                record.last_activity = time.time()
            self._reduce_lifecycle(record, event, params.get("payload"))
            self._persist_control_state(record)
            result = self._record_state_dict(record)
        try:
            self._events.append_local(RuntimeEventDraft(
                runtime_id=runtime_id, generation=record.generation,
                session_id=record.session_id, agent_id=record.agent_id,
                request_id="", run_id="", kind="AGENT_STATUS",
                created_at=datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT),
                payload={
                    "lifecycle": event, "agent_state": record.agent_state,
                    "presence": record.presence, "binding": record.binding,
                },
            ))
        except Exception as exc:
            log.warning("gateway: lifecycle event append failed: %s", exc)
        return result

    def runtime_command_ack(self, params: dict) -> dict:
        """插件 ack 链的 gateway 半边（设计 §2/§3）。

        插件领取 command 后按序上报 CLAIMED → REVIVING → TRIGGERING →
        TURN_TRIGGERED；gateway 校验迁移合法性并持久化（关键事务 FULL）。
        TURN_TRIGGERED 的 turn_id 与 OMP turn_start 的关联校验（correlated
        ack 握手）为 TODO —— 插件侧未实现前仅做机械状态推进。
        """
        request_id = params.get("request_id", "")
        state = params.get("state", "")
        runtime_id = params.get("runtime_id", "")
        turn_id = params.get("turn_id", "")
        generation = params.get("generation")
        if not request_id or state not in CMD_STATES:
            raise GatewayError(
                ERR_PROTOCOL,
                f"runtime.command_ack requires request_id + valid state; got state={state!r}",
            )
        row = self._control.get_command(request_id)
        if row is None:
            raise GatewayError(ERR_NOT_FOUND, f"unknown command: {request_id}")
        if runtime_id and runtime_id != row["runtime_id"]:
            raise GatewayError(
                ERR_NOT_AUTHORIZED,
                f"command {request_id} belongs to runtime {row['runtime_id']}, not {runtime_id}",
            )
        if generation is not None:
            try:
                generation = int(generation)
            except (TypeError, ValueError):
                raise GatewayError(
                    ERR_PROTOCOL, "runtime.command_ack generation must be an int",
                ) from None
            if generation < int(row["generation"]):
                raise GatewayError(
                    ERR_GENERATION_STALE,
                    f"ack generation {generation} < command generation {row['generation']}",
                )
        allowed = CMD_TRANSITIONS.get(row["state"], set())
        if state not in allowed:
            raise GatewayError(
                ERR_PROTOCOL_CONFLICT,
                f"invalid command transition {row['state']} → {state} for {request_id}",
            )
        updated = self._control.update_command(
            request_id, state=state,
            turn_id=turn_id or row.get("turn_id", ""),
            detail={"ack": state, "ack_at": _now_iso()},
        )
        return self._command_result(updated)

    def runtime_command_status(self, params: dict) -> dict:
        """查询命令当前状态（幂等重放 / 轮询用）。"""
        request_id = params.get("request_id", "")
        if not request_id:
            raise GatewayError(ERR_PROTOCOL, "runtime.command_status requires request_id")
        row = self._control.get_command(request_id)
        if row is None:
            raise GatewayError(ERR_NOT_FOUND, f"unknown command: {request_id}")
        return self._command_result(row)

    def runtime_probe(self, params: dict) -> dict:
        runtime_id = params.get("runtime_id", "")
        record = self._require_runtime(runtime_id)
        try:
            from codeagent.runtime.registry import RuntimeRegistry

            health = RuntimeRegistry().probe(runtime_id)
        except Exception as exc:
            # P3-a: log probe fallback so sink assembly failures aren't silent.
            log.warning("gateway: runtime_probe registry probe failed for %s: %s", runtime_id, exc)
            # A2: status='unknown' (park-restored placeholder) must NEVER
            # report alive without a REAL probe — only genuinely registered
            # runtimes (active + heartbeat) get the liveness fallback.
            alive = (
                record.status == "active"
                and record.last_activity > 0
                and (time.time() - record.last_activity) < 120
            )
            health = {
                "alive": alive,
                "reason": f"registry probe unavailable: {exc}",
                "status": record.status,
            }
        return {
            "runtime_id": runtime_id,
            "generation": record.generation,
            "presence": record.presence,
            "binding": record.binding,
            "agent_state": record.agent_state,
            "health": health,
        }

    def _purge_stopped_for_key(self, review_key: str, keep_runtime_id: str = "") -> list[str]:
        """清理某 review_key 下所有 stopped 旧 runtime 记录（内存 + ControlStore）。

        保留 keep_runtime_id（刚停止的，供调用方短暂读回状态），清理其余
        更早的 stopped 记录，防止多次 release/revive 累积。
        """
        purged: list[str] = []
        with self._runtimes_lock:
            for rid, rec in list(self._runtimes.items()):
                if rec.review_key != review_key:
                    continue
                if rec.status != "stopped":
                    continue
                if rid == keep_runtime_id:
                    continue
                self._runtimes.pop(rid, None)
                # 同步清理 ControlStore.runtime_generations 记录
                try:
                    self._control.delete_generation(rid)
                except Exception as exc:
                    log.warning("gateway: control generation delete failed for %s: %s", rid, exc)
                purged.append(rid)
        if purged:
            log.info("gateway: purged %d stopped runtime(s) for review_key=%s", len(purged), review_key)
        return purged

    def runtime_stop(self, params: dict) -> dict:
        runtime_id = params.get("runtime_id", "")
        record = self._require_runtime(runtime_id)
        reason = params.get("reason", "stopped")
        try:
            from codeagent.runtime.registry import RuntimeRegistry

            RuntimeRegistry().stop(runtime_id, reason)
        except Exception as exc:
            log.warning("gateway: runtime stop failed (marking stopped anyway): %s", exc)
        # P2-2: lock status mutation.
        with self._runtimes_lock:
            record.status = "stopped"
            # 三维：stop = 进程退出 → ended + dead。
            record.agent_state = AGENT_ENDED
            record.presence = PRESENCE_DEAD
            self._persist_control_state(record)
        # A3: the record stays in-memory (stopped) so the stopping caller can
        # read back the state; the sweep removes it shortly after so stopped
        # runtimes never linger or get aggregated by runtime_info.
        self._events.append_local(RuntimeEventDraft(
            runtime_id=runtime_id, generation=record.generation,
            session_id=record.session_id, agent_id=record.agent_id,
            request_id="", run_id="", kind="RUNTIME_STATE",
            created_at=datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT),
            payload={"state": "stopped", "reason": reason},
        ))
        # 立即清理同 review_key 下其他 stopped 旧记录（防多次 release/revive 累积）。
        if record.review_key:
            self._purge_stopped_for_key(record.review_key, keep_runtime_id=runtime_id)
        return {"runtime_id": runtime_id, "status": "stopped"}

    def runtime_purge_stopped(self, params: dict) -> dict:
        """清理指定 review_key 的所有 stopped 记录（内存 + ControlStore）。

        release 流程在 runtime.stop 之后调用——调用方已从 runtime.stop 的
        返回值读回停止状态，此处把该 key 剩余 stopped 记录全部清除（含刚
        停止的），使 release 后不残留任何记录，不等 sweep。
        """
        review_key = params.get("review_key", "")
        if not review_key:
            raise GatewayError(ERR_PROTOCOL, "runtime.purge_stopped requires review_key")
        purged = self._purge_stopped_for_key(review_key)
        return {"review_key": review_key, "purged": purged}

    def runtime_info(self, params: dict) -> dict:
        """Aggregate runtime observability for park info (by review_key or id)."""
        runtime_id = params.get("runtime_id", "")
        review_key = params.get("review_key", "")
        # P2-2: snapshot record under lock to avoid dict resize during iteration.
        with self._runtimes_lock:
            record = None
            if runtime_id:
                record = self._runtimes.get(runtime_id)
                # A3: never report a stopped runtime as the answer — the sweep
                # cleans stopped records; before it does, treat them as absent.
                if record is not None and record.status == "stopped":
                    record = None
            elif review_key:
                # A3: prefer the LIVE record (registered by plugin/adoption) over
                # stopped or park-restored placeholders: filter stopped records,
                # then take the newest by last_activity desc (restored records
                # carry last_activity=0 so a live registration wins naturally).
                candidates = [r for r in self._runtimes.values() if r.review_key == review_key]
                live = [r for r in candidates if r.status != "stopped"]
                record = max(live, key=lambda r: r.last_activity) if live else None
        if record is None:
            raise GatewayError(ERR_NOT_FOUND, f"no runtime for review_key={review_key!r} runtime_id={runtime_id!r}")
        # A8: aggregate ONLY this generation's events — a re-registered
        # runtime must not inherit the previous generation's stats.
        agg = self._events.aggregate(record.runtime_id, record.generation)
        try:
            from codeagent.runtime.registry import RuntimeRegistry

            health = RuntimeRegistry().probe(record.runtime_id)
        except Exception as exc:
            # P3-a: log probe fallback so receipt fallback isn't silent.
            log.warning("gateway: runtime_info probe failed for %s: %s", record.runtime_id, exc)
            # Plugin-registered runtimes have no registry handle — fall back
            # to the gateway record's own liveness signal (recent heartbeat).
            # A2: status='unknown' (park-restored placeholder) must never
            # report alive without a REAL probe.
            alive = (
                record.status == "active"
                and record.last_activity > 0
                and (time.time() - record.last_activity) < 120
            )
            health = {
                "alive": alive,
                "reason": f"registry probe unavailable: {exc}",
                "status": record.status,
                "last_activity_s": int(time.time() - record.last_activity) if record.last_activity > 0 else None,
            }
        return {
            "runtime_id": record.runtime_id,
            "session_id": record.session_id,
            "agent_id": record.agent_id,
            "generation": record.generation,
            "review_key": record.review_key,
            "backend_session_id": record.backend_session_id,
            "status": record.status,
            # 三维（设计 §1）—— 新增字段向后兼容。
            "presence": record.presence,
            "binding": record.binding,
            "agent_state": record.agent_state,
            "binding_epoch": record.binding_epoch,
            # A12: elapsed is now the real runtime age (since started_at);
            # idle seconds move to their own field. elapsed stays for
            # compatibility with callers of the old (mislabeled) field.
            "started_at": record.started_at,
            "elapsed": max(0, int(time.time() - record.started_at)) if record.started_at else 0,
            # A12: idle_s is only meaningful after a first activity signal —
            # placeholders (last_activity=0) report None, like health.last_activity_s.
            "idle_s": max(0, int(time.time() - record.last_activity)) if record.last_activity > 0 else None,
            "last_event": agg,
            "tool_stats": {"tool_count": agg.get("tool_count", 0), "error_count": agg.get("error_count", 0)},
            "runtime_health": health,
        }

    # ── events ─────────────────────────────────────────────────────────

    def events_list(self, params: dict) -> dict:
        events, cursor = self._events.list_after(
            cursor=int(params.get("cursor", 0)),
            filters=params.get("filters"),
            limit=int(params.get("limit", 200)),
            session_id=params.get("session_id", ""),
            runtime_id=params.get("runtime_id", ""),
        )
        return {
            "events": [e.to_dict() for e in events],
            "cursor": cursor,
        }

    # ── message (MailboxService bridge) ────────────────────────────────

    def message_send(self, params: dict) -> dict:
        receipt = self._svc.send(
            session_id=params.get("session_id", ""),
            from_id=params.get("from", ""),
            to_id=params.get("to", ""),
            subject=params.get("subject", ""),
            body=params.get("body", ""),
            kind=params.get("kind", "REPORT"),
            reply_to=params.get("reply_to", ""),
            run_id=params.get("run_id", ""),
            request_id=params.get("request_id", ""),
            require_ack=bool(params.get("require_ack", False)),
        )
        if receipt.status == "failed":
            raise GatewayError(ERR_NOT_FOUND, receipt.error or "message_send failed")
        return {"msg_id": receipt.msg_id, "status": receipt.status, "detail": receipt.detail}

    def message_peek(self, params: dict) -> dict:
        return self._svc.peek(
            params.get("session_id", ""), params.get("agent", ""),
            int(params.get("max_messages", 5)), int(params.get("max_subject", 80)),
        )

    def message_read(self, params: dict) -> dict:
        from codeagent.mailbox.service import ACK_ROUTE_UNRESOLVED

        outcome = self._svc.read(
            params.get("session_id", ""), params.get("agent", ""),
            params.get("owner", ""),
            msg_id=params.get("msg_id", ""),  # P1-8: targeted claim
        )
        if outcome.status == ACK_ROUTE_UNRESOLVED:
            raise GatewayError(ERR_NOT_AUTHORIZED, outcome.error or "ack route unresolved")
        result = {"status": outcome.status, "message": outcome.message}
        if outcome.receipt is not None:
            result["receipt"] = outcome.receipt.to_dict()
        return result

    def message_finalize(self, params: dict) -> dict:
        out = self._store.finalize(
            params.get("session_id", ""), params.get("agent", ""),
            params.get("msg_id", ""), params.get("owner", ""),
        )
        return {"status": out}

    def message_release(self, params: dict) -> dict:
        out = self._store.release(
            params.get("session_id", ""), params.get("agent", ""),
            params.get("msg_id", ""), params.get("owner", ""),
        )
        return {"status": out}

    # ── artifact verification ──────────────────────────────────────────

    def artifact_verify(self, params: dict) -> dict:
        """Verify a local artifact and record the verdict in the RequestLedger.

        Params::

            {session_id, request_id, run_id, path, sha256, size,
             agent_id?}

        If *agent_id* is omitted the service scans the session directory
        to locate the agent whose events tree contains *request_id*.
        """
        from codeagent.artifact import verify_artifact
        from codeagent.mailbox.store import RequestLedger

        session_id: str = params.get("session_id", "")
        request_id: str = params.get("request_id", "")
        run_id: str = params.get("run_id", "")
        artifact_path: str = params.get("path", "")
        sha256: str = params.get("sha256", "")
        size: int = int(params.get("size", 0))
        agent_id: str = params.get("agent_id", "")

        if not all([session_id, request_id, run_id, artifact_path, sha256]):
            raise GatewayError(ERR_PROTOCOL, "artifact.verify: missing required params")

        # Resolve agent_id — caller may omit it; scan session directory.
        sd = self._store.session_dir(session_id)
        if not agent_id:
            # Walk session_dir/<agent_id>/events/<request_id> to find the agent.
            if sd.is_dir():
                for child in sorted(sd.iterdir()):
                    if not child.is_dir():
                        continue
                    if (child / "events" / request_id).is_dir():
                        agent_id = child.name
                        break
            if not agent_id:
                raise GatewayError(
                    ERR_NOT_FOUND,
                    f"no agent found with events for request {request_id!r} "
                    f"in session {session_id!r}",
                )

        ledger = RequestLedger(sd, agent_id)

        # Verify the artifact file.
        # verify_artifact raises ValueError for: not-a-file, size mismatch,
        # sha256 mismatch.  We let file-not-found propagate as-is; only
        # hash/size mismatches are recorded as BLOCKED.
        try:
            verified = verify_artifact(Path(artifact_path), sha256, size)
        except ValueError as exc:
            if "not a file" in str(exc):
                raise
            verified = False

        result = ledger.record_artifact_verdict(request_id, run_id, verified)

        terminal = result["terminal"]
        # Map terminal CAS result to return status.
        if not result["cas"]:
            # Already in a terminal state — report "EXISTS" to signal idempotency.
            return {"verified": verified, "terminal": terminal, "status": "EXISTS"}
        return {"verified": verified, "terminal": terminal}

    # ── park bridge ────────────────────────────────────────────────────

    def park_revive(self, params: dict) -> dict:
        from codeagent.park.router import park_revive

        rv = park_revive(params.get("review_key", ""), params.get("prompt", "") or "")
        return {"method": rv.method, "success": rv.success, "context": rv.context}

    def park_release(self, params: dict) -> dict:
        from codeagent.park.registry import ParkRegistry

        ParkRegistry().release(params.get("review_key", ""))
        return {"released": params.get("review_key", "")}

    # ── P8.1 hub cross-device routing ──────────────────────────────────

    def hub_register(self, params: dict) -> dict:
        """Register a hub peer ↔ swarm agent mapping.

        Also registers the agent in the kernel routing table so
        EngineDeliverySink resolves the remote host for cross-device
        delivery.
        """
        peer_id = params.get("peer_id", "")
        session_id = params.get("session_id", "")
        agent_id = params.get("agent_id", "")
        host_alias = params.get("host_alias", "")
        if not peer_id or not session_id or not agent_id or not host_alias:
            raise GatewayError("PROTOCOL", "hub.register requires peer_id/session_id/agent_id/host_alias")
        peer = _HubPeer(
            peer_id=peer_id,
            session_id=session_id,
            agent_id=agent_id,
            host_alias=host_alias,
            mailbox_root=params.get("mailbox_root", ""),
            status="online",
            registered_at=time.time(),
        )
        # P2-2: lock peers dict write.
        with self._peers_lock:
            self._peers[peer_id] = peer
        # Register in the kernel routing so delivery resolves the remote host.
        try:
            from codeagent.swarm.model import AgentLocation, ExecutionMode

            persisted = self._store.read_session(session_id)
            authority = (persisted or {}).get("manager", "hub") if persisted else "hub"
            # Materialize the session in the live kernel WITHOUT hijacking an
            # existing persisted authority.
            if self._kernel.get_session(session_id) is None:
                try:
                    self._kernel.create_session(session_id, authority, [agent_id, "manager"])
                except ValueError:
                    pass  # concurrent creation — proceed to merge
            # Persist the roster/ACL merge (manager + agent) under the
            # existing authority; never replace it.
            if persisted is not None:
                try:
                    self._store.session_init(
                        session_id, authority, [agent_id, "manager"],
                        acl={
                            "authority": authority,
                            "allowed_senders": ["hub", "manager", agent_id],
                            "room_members": ["hub", "manager", agent_id],
                            "policy": "open",
                        },
                    )
                except Exception as exc:
                    log.warning("hub_register: session manifest merge failed for %s: %s", session_id, exc)
            # Existing sessions (live kernel) need the manager merged in-memory
            # too; the persisted session.json merge happens via store on next
            # kernel load.
            session = self._kernel.get_session(session_id)
            if session is not None:
                if "manager" not in session.roster:
                    session.roster.members.append("manager")
                session.acl.allowed_senders = list(set(session.acl.allowed_senders) | {"manager"})
                session.acl.room_members = list(set(session.acl.room_members) | {"manager"})
            self._kernel.register(
                AgentLocation(
                    agent_id=agent_id,
                    host_alias=host_alias,
                    backend="cli",
                    execution_mode=ExecutionMode.MAILBOX_WORKER,
                ),
                session_id,
            )
        except Exception as exc:
            log.warning("hub_register: routing registration failed for %s: %s", peer_id, exc)
        self._save_peers()
        return {"peer_id": peer_id, "status": peer.status, "agent_id": agent_id}

    def hub_send(self, params: dict) -> dict:
        """Cross-device hub message send (peer → remote agent inbox).

        Checks presence (P8.3) first — offline peers fail fast, never blind
        send. Routes through the kernel's EngineDeliverySink so the durable
        outbox → transport → remote inbox path is used; require_ack flows
        through protocol v2.
        """
        peer_id = params.get("peer_id", "")
        # P2-2: lock peers dict read.
        with self._peers_lock:
            peer = self._peers.get(peer_id)
        if peer is None:
            raise GatewayError(ERR_NOT_FOUND, f"unknown peer: {peer_id}")
        if peer.status == "offline":
            # P3-f: raise instead of returning ok=True for unreachable peer.
            raise GatewayError(ERR_NOT_FOUND, f"peer {peer_id} is offline")

        from codeagent.swarm.model import Envelope

        env = Envelope(
            subject=params.get("subject", "hub-message"),
            body=params.get("content", params.get("body", "")),
            kind=params.get("kind", "TASK"),
            run_id=params.get("run_id", "") or f"run-{uuid4().hex[:10]}",
            request_id=params.get("request_id", "") or f"req-{uuid4().hex[:10]}",
            require_ack=bool(params.get("require_ack", True)),
        )
        try:
            receipt = self._kernel.direct(
                peer.session_id, params.get("from", "hub"), peer.agent_id, env,
            )
        except (ValueError, PermissionError) as exc:
            raise GatewayError(ERR_NOT_FOUND, f"hub_send failed: {exc}") from exc
        return {"msg_id": receipt.msg_id, "status": receipt.status, "peer_id": peer_id}

    def hub_status(self, params: dict) -> dict:
        """Query peer presence (one peer or all)."""
        peer_id = params.get("peer_id", "")
        if peer_id:
            # P2-2: lock peers dict read.
            with self._peers_lock:
                peer = self._peers.get(peer_id)
            if peer is None:
                raise GatewayError(ERR_NOT_FOUND, f"unknown peer: {peer_id}")
            return {
                "peer_id": peer_id, "status": peer.status,
                "host_alias": peer.host_alias,
                "session_id": peer.session_id, "agent_id": peer.agent_id,
            }
        # P2-2: snapshot peers under lock to avoid dict resize during iteration.
        with self._peers_lock:
            snapshot = list(self._peers.values())
        return {"peers": [
            {"peer_id": p.peer_id, "status": p.status, "host_alias": p.host_alias,
             "session_id": p.session_id, "agent_id": p.agent_id}
            for p in snapshot
        ]}

    def hub_unregister(self, params: dict) -> dict:
        peer_id = params.get("peer_id", "")
        # P2-2: lock peers dict check-then-pop.
        with self._peers_lock:
            if peer_id not in self._peers:
                raise GatewayError(ERR_NOT_FOUND, f"unknown peer: {peer_id}")
            peer = self._peers.pop(peer_id)
        self._save_peers()
        # Clean the kernel routing entry too (unregister from the session).
        try:
            self._kernel.unregister(peer.session_id, peer.agent_id)
        except Exception as exc:
            log.warning("hub_unregister: kernel unregister failed for %s: %s", peer_id, exc)
        return {"peer_id": peer_id, "unregistered": True}

    # ── actas: runtime role exclusivity (session.claim) ───────────────

    def session_claim(self, params: dict) -> dict:
        """Claim exclusive runtime authority over an agent role in a session.

        actas 运行时独占（agmsg actas 借鉴）：同一 session 的同一 agent_id
        在同一时刻只能被一个 runtime 认领。claim 带 owner（runtime_id）与
        TTL（默认 3600s）；同 owner 重复 claim 幂等续期；不同 owner 抢占
        已被认领的角色 → ERR_PROTOCOL_CONFLICT。release 显式释放。
        """
        session_id = params.get("session_id", "")
        agent_id = params.get("agent_id", "")
        owner = params.get("owner", "")  # runtime_id or peer_id
        ttl = float(params.get("ttl", 3600))
        if not session_id or not agent_id or not owner:
            raise GatewayError("PROTOCOL", "session.claim requires session_id/agent_id/owner")
        # P3-e: TTL bounds — must be positive and within sane upper limit.
        _CLAIM_TTL_MAX = 86400  # 24h upper bound
        if ttl <= 0 or ttl > _CLAIM_TTL_MAX:
            raise GatewayError(
                ERR_PROTOCOL,
                f"session.claim ttl must be in (0, {_CLAIM_TTL_MAX}]; got {ttl}",
            )
        now = time.time()
        claim_key = f"{session_id}:{agent_id}"
        with self._runtimes_lock:
            existing = self._claims.get(claim_key)
            if existing is not None and existing["owner"] != owner:
                if existing["expires_at"] > now:
                    raise GatewayError(
                        ERR_PROTOCOL_CONFLICT,
                        f"role {agent_id} in {session_id} already claimed by {existing['owner']} "
                        f"until {existing['expires_at']:.0f}",
                    )
                # expired claim → takeover allowed
            self._claims[claim_key] = {"owner": owner, "claimed_at": now, "expires_at": now + ttl}
            # P3-e: lock read — return the persisted claim, not a stale dict ref.
            persisted = self._claims[claim_key]
        return {
            "session_id": session_id, "agent_id": agent_id,
            "owner": owner, "expires_at": persisted["expires_at"],
        }

    def session_release(self, params: dict) -> dict:
        session_id = params.get("session_id", "")
        agent_id = params.get("agent_id", "")
        owner = params.get("owner", "")
        claim_key = f"{session_id}:{agent_id}"
        with self._runtimes_lock:
            existing = self._claims.get(claim_key)
            if existing is None:
                return {"released": False, "reason": "no claim"}
            if owner and existing["owner"] != owner:
                raise GatewayError(ERR_NOT_AUTHORIZED, f"claim owned by {existing['owner']}, not {owner}")
            self._claims.pop(claim_key, None)
        return {"released": True, "session_id": session_id, "agent_id": agent_id}

    # ── write merge (cross-device) ──────────────────────────────────────

    @staticmethod
    def write_parse_body(body: str) -> dict:
        """Parse a TASK/REPORT body JSON for merge fields.

        Returns a dict with ``base_revision``, ``target_path``, and
        ``artifact_id`` if present and valid; returns ``{}`` on any
        parse or schema error (never raises).
        """
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(data, dict):
            return {}
        result: dict = {}
        for key in ("base_revision", "target_path", "artifact_id"):
            val = data.get(key)
            if isinstance(val, str) and val:
                result[key] = val
        return result

    def write_merge(self, params: dict) -> dict:
        """Manager-side merge guard for cross-device writes.

        Params: {session_id, request_id, run_id, target_path,
                 base_revision, artifact_sha256, body}

        If the same ``target_path`` already has a recorded
        ``artifact_sha256`` that differs, raises
        ``ERR_PROTOCOL_CONFLICT``.  Otherwise records the mapping
        and returns ``{merged: True}``.
        """
        session_id = params.get("session_id", "")
        target_path = params.get("target_path", "")
        artifact_sha256 = params.get("artifact_sha256", "")
        if not session_id or not target_path or not artifact_sha256:
            raise GatewayError(
                ERR_NOT_FOUND,
                "write.merge requires session_id, target_path, artifact_sha256",
            )
        key = (session_id, target_path)
        # P2-3: lock check-then-set to prevent concurrent conflict writes
        # both seeing no conflict and both writing (last-write-wins).
        with self._merges_lock:
            existing = self._merges.get(key)
            if existing is not None and existing != artifact_sha256:
                raise GatewayError(
                    ERR_PROTOCOL_CONFLICT,
                    f"conflict on {target_path!r}: existing sha256={existing!r} "
                    f"!= incoming sha256={artifact_sha256!r}",
                )
            self._merges[key] = artifact_sha256
        self._save_merges()
        return {"merged": True}

    def _save_merges(self) -> None:
        """Persist merge records (conflict detection survives restarts)."""
        try:
            self._peers_file.parent.mkdir(parents=True, exist_ok=True)
            # P2-2: snapshot merges under lock; write file outside lock.
            with self._merges_lock:
                data = [{"session_id": s, "target_path": t, "artifact_sha256": h}
                        for (s, t), h in self._merges.items()]
            tmp = self._peers_file.with_name("merges.json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            tmp.replace(self._peers_file.with_name("merges.json"))
        except OSError as exc:
            log.warning("merges persist failed: %s", exc)

    def _restore_merges(self) -> None:
        try:
            p = self._peers_file.with_name("merges.json")
            if not p.exists():
                return
            data = json.loads(p.read_text(encoding="utf-8"))
            # P2-2: lock merges dict writes during restore.
            with self._merges_lock:
                for d in data:
                    sid = d.get("session_id", "")
                    tgt = d.get("target_path", "")
                    h = d.get("artifact_sha256", "")
                    if sid and tgt and h:
                        self._merges[(sid, tgt)] = h
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("merges restore failed: %s", exc)

    # P2-13: merge_reset — clear stale merge latches so (session, path)
    # pairs don't accumulate indefinitely.  The manager calls this after
    # confirming the merge was written to the repo, or when abandoning
    # a merge attempt.
    def merge_reset(self, params: dict) -> dict:
        """Clear merge conflict records.

        Params: {session_id, target_path?}
        If ``target_path`` is given, clears only that (session, path) pair.
        Otherwise clears all records for the session.
        Returns ``{reset: <count>}``.
        """
        session_id = params.get("session_id", "")
        target_path = params.get("target_path", "")
        if not session_id:
            raise GatewayError(ERR_NOT_FOUND, "merge.reset requires session_id")
        removed = 0
        with self._merges_lock:
            if target_path:
                if self._merges.pop((session_id, target_path), None) is not None:
                    removed = 1
            else:
                keys = [k for k in self._merges if k[0] == session_id]
                for k in keys:
                    del self._merges[k]
                    removed += 1
        if removed:
            self._save_merges()
        return {"reset": removed}

    # ── internals ──────────────────────────────────────────────────────

    def _require_runtime(self, runtime_id: str) -> RuntimeRecord:
        # P2-2: lock runtime dict read.
        with self._runtimes_lock:
            record = self._runtimes.get(runtime_id)
        if record is None:
            raise GatewayError(ERR_NOT_FOUND, f"unknown runtime: {runtime_id}")
        return record

    # ── 三维状态（设计 §1）─────────────────────────────────────────────

    @staticmethod
    def _reduce_lifecycle(record: RuntimeRecord, event: str,
                          payload: Optional[dict] = None) -> None:
        """三维状态归约 —— 权威事件驱动（设计 §1 归约表）。

          session_ready                       → idle（+ alive）
          agent_start / turn_start            → agent_running
          turn_end                            → idle
          agent_end                           → idle（正常结束；park 须另报
                                                registry_parked/session_shutdown）
          session_shutdown / registry_parked /
          registry_removed / process_exit     → ended
          heartbeat                           → 只更新 presence
        """
        if event == "heartbeat":
            record.presence = PRESENCE_ALIVE
            return
        if event == "session_ready":
            record.presence = PRESENCE_ALIVE
            record.agent_state = AGENT_IDLE
            return
        if event in ("agent_start", "turn_start"):
            record.agent_state = AGENT_RUNNING
            return
        if event in ("turn_end", "agent_end"):
            # 正常结束 → idle；不能仅凭 agent_end 推断 parked/ended（设计 §1 注）。
            record.agent_state = AGENT_IDLE
            return
        if event in ("session_shutdown", "registry_parked",
                     "registry_removed", "process_exit"):
            record.agent_state = AGENT_ENDED
            return
        # 未知事件：保持状态不变（fail-closed，不猜测）。
        log.warning("gateway: unknown lifecycle event %r for %s", event, record.runtime_id)

    def _is_hot(self, record: RuntimeRecord) -> bool:
        """hot 投递门控（设计 §1）：三维状态正交判定。

        presence=alive AND binding=bound AND
        agent_state ∈ {agent_running, idle, ended}。
        （设计同时要求插件 capability 含 park_revive + correlated_turn_ack；
        P0 以三维为主，capability 收紧留给插件握手后收紧。）
        """
        return (
            record.presence == PRESENCE_ALIVE
            and record.binding == BINDING_BOUND
            and record.agent_state in (AGENT_RUNNING, AGENT_IDLE, AGENT_ENDED)
        )

    def _persist_control_state(self, record: RuntimeRecord) -> None:
        """镜像三维状态到 ControlStore.runtime_generations（关键事务 FULL）。

        尽力而为：落盘失败仅告警，不阻断主流程（内存记录仍是操作权威）。
        """
        try:
            self._control.upsert_generation(
                runtime_id=record.runtime_id,
                current_generation=record.generation,
                owner_nonce=record.nonce,
                presence=record.presence,
                binding=record.binding,
                backend_session_id=record.backend_session_id,
                binding_epoch=record.binding_epoch,
                agent_state=record.agent_state,
            )
        except Exception as exc:
            log.warning("gateway: control state persist failed for %s: %s",
                        record.runtime_id, exc)

    @staticmethod
    def _record_state_dict(record: RuntimeRecord) -> dict:
        return {
            "runtime_id": record.runtime_id,
            "presence": record.presence,
            "binding": record.binding,
            "agent_state": record.agent_state,
            "binding_epoch": record.binding_epoch,
            "status": record.status,  # 旧字段保持兼容
        }

    # ── runtime.send 命令结果（设计 §3 返回语义）───────────────────────

    def _command_result(self, row: dict) -> dict:
        """命令行 → runtime.send 返回体：state 为机器状态，status 为语义。

        QUEUED 的分支语义（设计 §3 表）：
          - 因 binding 未建立而未注入 → binding_pending（允许稍后重试）
          - 其余（presence 非 alive / 已持久队列）→ mailbox_persisted
        """
        state = row["state"]
        status = CMD_STATUS_BY_STATE.get(state, "mailbox_persisted")
        detail = row.get("detail") or {}
        if state == CMD_QUEUED and detail.get("gate") == "binding_pending":
            status = "binding_pending"
        return {
            "request_id": row["request_id"],
            "command_id": row["command_id"],
            "msg_id": row.get("msg_id", ""),
            "turn_id": row.get("turn_id", ""),
            "runtime_id": row["runtime_id"],
            "generation": row.get("generation", 0),
            "state": state,
            "status": status,
            "detail": detail,
        }

    # ── dispatch ───────────────────────────────────────────────────────

    def dispatch(self, method: str, params: dict) -> dict:
        """Route a gateway method name to its handler (fail-closed)."""
        handlers = {
            "capabilities.get": self.capabilities,
            "session.ensure": self.session_ensure,
            "runtime.register": self.runtime_register,
            "runtime.declare": self.runtime_declare,  # P3: weak presence declaration
            "runtime.spawn": self.runtime_spawn,
            "runtime.heartbeat": self.runtime_heartbeat,
            "runtime.event": self.runtime_event,
            # Q5b: runtime.context —— 插件 model_change 上报 + oracle CLI 继承查询。
            "runtime.context_set": self.runtime_context_set,
            "runtime.context_get": self.runtime_context_get,
            "runtime.send": self.runtime_send,
            "runtime.lifecycle": self.runtime_lifecycle,  # 三维归约入口（设计§1）
            "runtime.command_ack": self.runtime_command_ack,  # 命令状态机 ack（§3）
            "runtime.command_status": self.runtime_command_status,  # 命令状态查询
            "runtime.probe": self.runtime_probe,
            "runtime.stop": self.runtime_stop,
            "runtime.purge_stopped": self.runtime_purge_stopped,  # 释放时清理旧 stopped 记录
            "runtime.info": self.runtime_info,
            "runtime.status": self.runtime_status,
            "runtime.list": self.runtimes_list,
            "events.list": self.events_list,
            "message.send": self.message_send,
            "message.peek": self.message_peek,
            "message.read": self.message_read,
            "message.finalize": self.message_finalize,
            "message.release": self.message_release,
            "park.revive": self.park_revive,
            "park.release": self.park_release,
            "hub.register": self.hub_register,
            "hub.send": self.hub_send,
            "hub.status": self.hub_status,
            "hub.unregister": self.hub_unregister,
            "session.claim": self.session_claim,
            "session.release": self.session_release,
            "write.merge": self.write_merge,
            "write.merge_reset": self.merge_reset,  # P2-13
            "artifact.verify": self.artifact_verify,
        }
        handler = handlers.get(method)
        if handler is None:
            raise GatewayError("PROTOCOL", f"unknown gateway method: {method}")
        return handler(params)
