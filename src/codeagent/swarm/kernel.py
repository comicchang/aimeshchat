"""SwarmKernel — IRC-style session/roster/ACL/routing kernel.

Owns session lifecycle, roster/ACL enforcement, and message routing.
Does NOT own transport I/O — that is delegated to a DeliverySink.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import logging
import os
import shlex
import subprocess
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Protocol
from uuid import uuid4

from codeagent.constants import ISO_TIMESTAMP_FORMAT
from codeagent.mailbox.protocol import (
    BROADCAST_TO,
    AttachmentRef,
    Message,
    validate_agent_id,
    validate_message,
)
from codeagent.mailbox.store import MailboxStore

log = logging.getLogger(__name__)
from codeagent.swarm.delivery import SendReceipt as DeliverySendReceipt
from codeagent.swarm.model import (
    ACL,
    Address,
    AddressKind,
    AgentLocation,
    Channel,
    DeliveryReceipt,
    Envelope,
    ExecutionMode,
    PollResult,
    Registration,
    ReturnMode,
    Roster,
    SendReceipt,
    Session,
    Subscription,
    _iter_inbox_files,
)


# ── Delivery sink protocol ─────────────────────────────────────────────


class DeliverySink(Protocol):
    """Anything that can deliver an envelope to an agent inbox."""
    def deliver(self, session_id: str, target_agent: str, envelope: Envelope,
                msg_id: str, created_at: str, from_id: str) -> DeliverySendReceipt: ...


class LocalDeliverySink:
    """Writes directly to MailboxStore — used in-process and for tests."""

    def __init__(self, store: MailboxStore):
        self._store = store

    def deliver(self, session_id: str, target_agent: str, envelope: Envelope,
                msg_id: str, created_at: str, from_id: str) -> DeliverySendReceipt:
        # When target_agent is BROADCAST_TO, use store's built-in broadcast fan-out
        self._store.send(
            session_id=session_id,
            from_id=from_id,
            to_id=target_agent,
            subject=envelope.subject,
            body=envelope.body,
            kind=envelope.kind,
            reply_to=envelope.reply_to,
            run_id=envelope.run_id,
            request_id=envelope.request_id,
            trace_id=envelope.trace_id,
            causation_id=envelope.causation_id,
            attachments=[a.to_dict() for a in envelope.attachments] if envelope.attachments else None,
            msg_id=msg_id,
        )
        return DeliverySendReceipt(status="delivered", msg_id=msg_id)


# ── SwarmKernel ────────────────────────────────────────────────────────


def _synchronized(method: Callable) -> Callable:
    """P1-4: Serialize a kernel public method behind its instance RLock.

    Gateway threads call kernel methods concurrently; without serialization,
    dict mutations during iteration raise ``RuntimeError``.  ``RLock`` (not
    plain ``Lock``) allows re-entrant calls (e.g. ``send`` → ``direct``).

    P1-6: 投递类方法（send/direct/broadcast/channel/notice）不使用本装饰器。
    它们改为两阶段——锁内只做路由决策（快速路径），锁外做 transport 往返
    （SSH 60s 量级），避免 kernel 全局 RLock 持锁跨网络 I/O 串行化网关。
    """
    def _wrapper(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)
    _wrapper.__wrapped__ = method          # type: ignore[attr-defined]
    return _wrapper


class SwarmKernel:
    """Protocol kernel — owns session/roster/ACL/routing, not transport.

    Parameters
    ----------
    store : MailboxStore
        Filesystem-backed store for persistence.
    sink : DeliverySink
        Pluggable delivery backend (LocalDeliverySink for tests, C2 for prod).
    """

    # P1-6: broadcast 并行投递阈值——fan-out ≥ 4 才上 ThreadPoolExecutor；
    # 小 fan-out 保持顺序投递（线程池唤醒开销 > 并行收益，且 sink 调用
    # 顺序确定，mock/测试依赖该顺序语义）。
    _BROADCAST_PARALLEL_MIN = 4
    # P1-6: 并行投递 worker 上限——避免大 roster 一次性打爆 N 个 SSH 往返。
    _BROADCAST_MAX_WORKERS = 8

    def __init__(self, store: MailboxStore, sink: Optional[DeliverySink] = None):
        self._store = store
        self._sink = sink or LocalDeliverySink(store)
        # P1-4: 串行化 kernel 状态访问（gateway 多线程并发防 dict RuntimeError 崩溃）。
        # P1-6: 该锁只保护路由决策（session/roster/ACL/channels 等状态），
        # 绝不跨 sink 投递持锁——transport 往返在锁外执行（见 direct/broadcast）。
        self._lock = threading.RLock()
        # session_id → Session
        self._sessions: dict[str, Session] = {}
        # (session_id, agent_id) → AgentLocation
        self._routing: dict[tuple[str, str], AgentLocation] = {}
        # session_id → { channel_id → Channel }
        self._channels: dict[str, dict[str, Channel]] = {}
        # session_id → { agent_id → list[Subscription] }
        self._subscriptions: dict[str, dict[str, list[Subscription]]] = {}
        # Topic-based notice routing: session_id → { topic → set[agent_id] }
        self._topic_subscriptions: dict[str, dict[str, set[str]]] = {}
        # Optional receiver for push-mode delivery (D2).
        self._receiver: Any = None
        # P3-11: per-agent registration tokens — {(session_id, agent_id): token}.
        self._registration_tokens: dict[tuple[str, str], str] = {}

        # Restore persisted sessions so each CLI invocation (new process,
        # new kernel) sees sessions created by earlier invocations.
        self._load_persisted_sessions()

    def _load_persisted_sessions(self) -> None:
        """Rebuild in-memory sessions from session.json / swarm-meta.json.

        The CLI runs one subcommand per process; without this, a fresh
        kernel has an empty ``_sessions`` dict and ``register``/``direct``
        fail with "session not found" right after ``create-session``.
        """
        try:
            root = self._store.root
            if not root.is_dir():
                return
            for session_dir in sorted(root.iterdir()):
                if not session_dir.is_dir():
                    continue
                session_file = session_dir / "session.json"
                if not session_file.exists():
                    continue
                try:
                    data = json.loads(session_file.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                    continue
                sid = data.get("session_id") or session_dir.name
                manager = data.get("manager", "")
                agents = data.get("agents", [])
                members = sorted(set(agents) | ({manager} if manager else set()))

                acl = ACL(
                    authority=manager,
                    allowed_senders=list(members),
                    room_members=list(members),
                    policy="open",
                )
                # B4-Manifest: session.json 的 acl 是权威（ensure 同步副本）；
                # swarm-meta.json 仅本机控制面（routing/channels）。远端 ensure
                # 只写 session.json——远端 kernel 必须优先读它，否则 restricted
                # policy 恢复 open。
                session_acl = data.get("acl")
                if isinstance(session_acl, dict):
                    acl = ACL(
                        authority=session_acl.get("authority", manager),
                        allowed_senders=session_acl.get("allowed_senders", members),
                        room_members=session_acl.get("room_members", members),
                        policy=session_acl.get("policy", "open"),
                    )
                meta_file = session_dir / "swarm-meta.json"
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    acl_data = meta.get("acl", {})
                    if acl_data and not session_acl:
                        # 旧 session（无 session.json acl）回退 swarm-meta
                        acl = ACL(
                            authority=acl_data.get("authority", manager),
                            allowed_senders=acl_data.get("allowed_senders", members),
                            room_members=acl_data.get("room_members", members),
                            policy=acl_data.get("policy", "open"),
                        )
                    chans = {}
                    for cid, cdata in (meta.get("channels") or {}).items():
                        chans[cid] = Channel(
                            channel_id=cdata.get("channel_id", cid),
                            members=list(cdata.get("members", [])),
                        )
                    for aid, rdata in (meta.get("routing") or {}).items():
                        em_raw = rdata.get("execution_mode")
                        rm_raw = rdata.get("return_mode")
                        self._routing[(sid, aid)] = AgentLocation(
                            agent_id=rdata.get("agent_id", aid),
                            host_alias=rdata.get("host_alias", ""),
                            backend=rdata.get("backend", "cli"),
                            execution_mode=ExecutionMode(em_raw) if em_raw else None,
                            mailbox_root=rdata.get("mailbox_root", ""),
                            return_mode=ReturnMode(rm_raw) if rm_raw else None,
                        )
                    # P3-11: load persisted registration tokens.
                    for aid, tok in (meta.get("registration_tokens") or {}).items():
                        self._registration_tokens[(sid, aid)] = tok
                except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                    chans = {}

                self._sessions[sid] = Session(
                    session_id=sid,
                    manager_id=manager,
                    roster=Roster(members=members),
                    acl=acl,
                    created_at=data.get("created_at", ""),
                    execution_modes=data.get("execution_modes", {}),
                    return_modes=data.get("return_modes", {}),
                )
                self._channels[sid] = chans
                self._subscriptions[sid] = {}
        except OSError:
            pass

    # ── Session lifecycle ──────────────────────────────────────────────

    @_synchronized  # P1-4
    def create_session(
        self,
        session_id: str,
        manager_id: str,
        roster: list[str],
        acl: Optional[ACL] = None,
        execution_modes: Optional[dict[str, str]] = None,
        return_modes: Optional[dict[str, str]] = None,
    ) -> Session:
        """Create a new swarm session with roster and ACL.

        Validates all agent IDs and persists session.json via MailboxStore.
        The manager is automatically included in roster and allowed_senders.
        """
        validate_agent_id(session_id)
        validate_agent_id(manager_id)
        for aid in roster:
            validate_agent_id(aid)

        if session_id in self._sessions:
            raise ValueError(f"session already exists: {session_id}")

        all_members = sorted(set(roster) | {manager_id})
        acl = acl or ACL(
            authority=manager_id,
            allowed_senders=list(all_members),
            room_members=list(all_members),
            policy="open",
        )
        # Ensure manager is in allowed_senders and room_members
        if manager_id not in acl.allowed_senders:
            acl.allowed_senders = list(set(acl.allowed_senders) | {manager_id})
        if manager_id not in acl.room_members:
            acl.room_members = list(set(acl.room_members) | {manager_id})

        session = Session(
            session_id=session_id,
            manager_id=manager_id,
            roster=Roster(members=all_members),
            acl=acl,
            created_at=datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT),
            execution_modes=dict(execution_modes) if execution_modes else {},
            return_modes=dict(return_modes) if return_modes else {},
        )

        # Persist to filesystem via MailboxStore (ACL 权威入 session.json，
        # 供远端 ensure 同步——swarm-meta.json 仅本地控制面)
        self._store.session_init(
            session_id, manager_id,
            [a for a in all_members if a != manager_id],
            acl={
                "authority": acl.authority,
                "allowed_senders": acl.allowed_senders,
                "room_members": acl.room_members,
                "policy": acl.policy,
            },
            execution_modes=dict(execution_modes) if execution_modes else None,
            return_modes=dict(return_modes) if return_modes else None,
        )

        # Store swarm-level metadata alongside session.json (locked).
        def _init_meta(meta: dict) -> None:
            meta["acl"] = {
                "authority": acl.authority,
                "allowed_senders": acl.allowed_senders,
                "room_members": acl.room_members,
                "policy": acl.policy,
            }

        self._persist_meta(session_id, _init_meta)

        self._sessions[session_id] = session
        self._channels[session_id] = {}
        self._subscriptions[session_id] = {}
        return session

    def _require_session(self, session_id: str) -> Session:
        if session_id not in self._sessions:
            raise ValueError(f"session not found: {session_id}")
        return self._sessions[session_id]

    # ── Register / Unregister ──────────────────────────────────────────

    @_synchronized  # P1-4
    def register(self, location: AgentLocation, session_id: str,
                 token: str = "") -> Registration:
        """Register an agent's location in the routing table.

        The agent must be in the session's roster.  The mapping is
        persisted to swarm-meta.json so later CLI processes (one per
        subcommand) resolve the host for cross-host delivery.

        P3-11: When a token is registered for this agent (via
        set_registration_token), the caller-supplied token must match.
        Cross-host re-registration logs a warning (last-wins, but visible).
        """
        session = self._require_session(session_id)
        if location.agent_id not in session.roster:
            raise ValueError(f"agent not in roster: {location.agent_id}")

        # P3-11: token validation — check against stored expected token.
        key = (session_id, location.agent_id)
        expected_token = self._registration_tokens.get(key)
        if expected_token and token != expected_token:
            raise PermissionError(
                f"registration token mismatch for agent {location.agent_id}"
            )

        # P3-11: conflict detection — warn when same agent re-registers
        # from a different host (last-wins but the change is logged).
        existing = self._routing.get(key)
        if existing is not None and existing.host_alias != location.host_alias:
            log.warning(
                "P3-11: agent %s re-registered from host %s → %s "
                "(last-wins, previous location overwritten)",
                location.agent_id, existing.host_alias, location.host_alias,
            )

        self._routing[(session_id, location.agent_id)] = location
        self._persist_routing(session_id)

        # Persist execution_mode/return_mode to session.json so they
        # survive across kernel restarts (session-level metadata).
        em_update: Optional[dict[str, str]] = None
        rm_update: Optional[dict[str, str]] = None
        if location.execution_mode is not None:
            session.execution_modes[location.agent_id] = location.execution_mode.value
            em_update = {location.agent_id: location.execution_mode.value}
        if location.return_mode is not None:
            session.return_modes[location.agent_id] = location.return_mode.value
            rm_update = {location.agent_id: location.return_mode.value}
        if em_update or rm_update:
            self._store.session_init(
                session_id, session.manager_id, [],
                execution_modes=em_update,
                return_modes=rm_update,
            )

        return Registration(
            agent_id=location.agent_id,
            session_id=session_id,
            location=location,
        )

    @_synchronized  # P1-4
    def set_agent_card(self, session_id: str, agent_id: str,
                       card: dict) -> None:
        """P2 (oracle): 持久化 agent_card（每 agent 一张，与 ACL/权限解耦）。

        card 字段：display_name/description/agent_version/capabilities[]。
        纯 advertisement——不授予任何 ACL 权限。
        """
        if agent_id not in self._require_session(session_id).roster:
            raise ValueError(f"agent not in roster: {agent_id}")
        allowed = {"display_name", "description", "agent_version", "capabilities"}
        clean = {k: v for k, v in card.items() if k in allowed}
        if not clean:
            raise ValueError("agent card must include at least one of "
                             f"{sorted(allowed)}")
        if isinstance(clean.get("capabilities"), list):
            clean["capabilities"] = [
                str(c)[:64] for c in clean["capabilities"][:32]
            ]
        for k in ("display_name", "description", "agent_version"):
            if isinstance(clean.get(k), str):
                clean[k] = clean[k][:256]

        def _update(meta: dict) -> None:
            cards = meta.setdefault("agent_cards", {})
            cards[agent_id] = clean

        self._persist_meta(session_id, _update)

    def get_agent_cards(self, session_id: str) -> dict:
        """P2: 读回本 session 的 agent_cards（swarm-meta.json）。"""
        try:
            meta_path = self._store.session_dir(session_id) / "swarm-meta.json"
            if not meta_path.exists():
                return {}
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return meta.get("agent_cards", {})
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return {}

    @_synchronized  # P1-4
    def set_registration_token(self, session_id: str, agent_id: str,
                               token: str) -> None:
        """P3-11: Register an expected token for an agent.

        When set, subsequent register() calls for this agent must supply
        the matching token.  Persisted to swarm-meta.json for cross-process
        visibility.
        """
        self._require_session(session_id)
        if agent_id not in self._sessions[session_id].roster:
            raise ValueError(f"agent not in roster: {agent_id}")
        self._registration_tokens[(session_id, agent_id)] = token

        def _update(meta: dict) -> None:
            tokens = meta.setdefault("registration_tokens", {})
            tokens[agent_id] = token

        self._persist_meta(session_id, _update)

    def _load_registration_tokens(self, session_id: str) -> None:
        """P3-11: Load persisted registration tokens from swarm-meta.json."""
        try:
            meta_path = self._store.session_dir(session_id) / "swarm-meta.json"
            if not meta_path.exists():
                return
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            tokens = meta.get("registration_tokens", {})
            for agent_id, tok in tokens.items():
                self._registration_tokens[(session_id, agent_id)] = tok
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            pass

    @_synchronized  # P1-4
    def unregister(self, session_id: str, agent_id: str) -> None:
        """Remove an agent from the routing table."""
        self._require_session(session_id)
        self._routing.pop((session_id, agent_id), None)
        self._persist_routing(session_id, deleted={agent_id})

    def _persist_routing(self, session_id: str, deleted: Optional[set[str]] = None) -> None:
        """Persist the routing table into swarm-meta.json (locked, merge).

        Only entries for *session_id* are written; agents registered by
        other kernels in other sessions are preserved on disk.  Entries
        in *deleted* are explicitly removed from disk (unregister of THIS
        kernel's own agent) — a stale entry must not linger for a fresh
        kernel that loads from disk.
        """
        deleted = deleted or set()

        def _update(meta: dict) -> None:
            routing = meta.get("routing", {})
            for (sid, aid), loc in self._routing.items():
                if sid == session_id:
                    entry: dict[str, Any] = {
                        "agent_id": loc.agent_id,
                        "host_alias": loc.host_alias,
                        "backend": loc.backend,
                    }
                    if loc.execution_mode is not None:
                        entry["execution_mode"] = loc.execution_mode.value
                    if loc.mailbox_root:
                        entry["mailbox_root"] = loc.mailbox_root
                    if loc.return_mode is not None:
                        entry["return_mode"] = loc.return_mode.value
                    routing[aid] = entry
            for aid in deleted:
                routing.pop(aid, None)
            meta["routing"] = routing

        self._persist_meta(session_id, _update)

    # ── Channel management ─────────────────────────────────────────────

    @_synchronized  # P1-4
    def create_channel(
        self,
        session_id: str,
        channel_id: str,
        members: list[str],
        acl: Optional[ACL] = None,
    ) -> Channel:
        """Create a named channel within a session."""
        session = self._require_session(session_id)
        for mid in members:
            if mid not in session.roster:
                raise ValueError(f"channel member not in roster: {mid}")
        if channel_id in self._channels.get(session_id, {}):
            raise ValueError(f"channel already exists: {channel_id}")

        channel = Channel(
            channel_id=channel_id,
            members=list(members),
            acl=acl,
        )
        self._channels.setdefault(session_id, {})[channel_id] = channel
        self._persist_channels(session_id)
        return channel

    def _persist_channels(self, session_id: str) -> None:
        """Persist channels into swarm-meta.json (locked, merge).

        Only channels for *session_id* are written; channels created by
        other kernels in other sessions are preserved on disk.
        """
        def _update(meta: dict) -> None:
            channels = meta.get("channels", {})
            for cid, ch in self._channels.get(session_id, {}).items():
                channels[cid] = {
                    "channel_id": ch.channel_id,
                    "members": list(ch.members),
                }
            meta["channels"] = channels

        self._persist_meta(session_id, _update)

    def _persist_meta(self, session_id: str, update: Callable[[dict], None]) -> None:
        """Locked read-modify-write of swarm-meta.json.

        Uses a separate ``.swarm-meta.lock`` file (never replaced by
        ``os.replace``) so the lock inode is stable across writers.
        A unique tmp file per writer avoids shared-name collisions.

        If the lock cannot be acquired the error propagates — fail-closed
        rather than silently writing without exclusion.
        """
        meta_path = self._store.session_dir(session_id) / "swarm-meta.json"
        lock_path = self._store.session_dir(session_id) / ".swarm-meta.lock"
        meta_path.parent.mkdir(parents=True, exist_ok=True)

        lock_fd = open(lock_path, "a+")  # never os.replace'd — stable inode
        try:
            fcntl.lockf(lock_fd, fcntl.LOCK_EX)
            meta: dict = {}
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                pass
            update(meta)
            # Unique tmp name (pid + uuid) — no shared .tmp-swarm-meta collision
            tmp = meta_path.parent / f".tmp-swarm-meta-{os.getpid()}-{uuid4().hex[:8]}.json"
            with open(tmp, "w") as f:
                f.write(json.dumps(meta, indent=2, ensure_ascii=False))
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(meta_path))  # only meta replaced; lock_fd stays on lock_path
        finally:
            fcntl.lockf(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

    # ── ACL checks ─────────────────────────────────────────────────────

    def _check_direct(self, session: Session, sender: str, recipient: str) -> None:
        """Direct: sender ∈ allowed_senders AND recipient ∈ room_members."""
        if sender not in session.acl.allowed_senders:
            raise PermissionError(f"sender not in allowed_senders: {sender}")
        if recipient not in session.acl.room_members:
            raise PermissionError(f"recipient not in room_members: {recipient}")

    def _check_channel(self, session: Session, channel: Channel,
                       sender: str) -> None:
        """Channel: sender ∈ channel.members."""
        if sender not in channel.members:
            raise PermissionError(f"sender not in channel members: {sender}")

    def _check_broadcast(self, session: Session, sender: str) -> None:
        """Broadcast: sender is authority or policy allows."""
        if session.acl.policy == "open":
            return
        if sender != session.acl.authority:
            raise PermissionError(f"sender is not broadcast authority: {sender}")

    def _check_notice(self, session: Session, sender: str) -> None:
        """Notice: sender ∈ allowed_senders."""
        if sender not in session.acl.allowed_senders:
            raise PermissionError(f"sender not in allowed_senders: {sender}")

    # ── Message routing ────────────────────────────────────────────────

    def _gen_msg_id(self) -> str:
        return str(uuid4())

    def _ensure_trace_id(self, envelope: Envelope) -> None:
        """B2: 每消息注入 trace_id（uuid4 hex）——跨主机追踪链路。
        仅当调用方未显式设置时生成；同一 envelope 复用同一 trace。"""
        if not envelope.trace_id:
            envelope.trace_id = uuid4().hex

    def _gen_created_at(self) -> str:
        return datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT)

    # P1-6: send 是纯派发器（只读 target.kind，不触碰共享状态），不持锁。
    # 各分支（direct/broadcast/channel/notice）自己两阶段：锁内路由决策、
    # 锁外投递——若 send 整体持锁会把锁跨过内层方法的 transport 往返。
    def send(self, session_id: str, sender: str, target: Address,
             envelope: Envelope) -> SendReceipt:
        """Route a message according to its Address kind.

        Dispatches to direct/broadcast/channel/notice.
        """
        if target.kind == AddressKind.DIRECT:
            return self.direct(session_id, sender, target.agent_id, envelope)
        elif target.kind == AddressKind.BROADCAST:
            receipts = self.broadcast(session_id, sender, envelope)
            if not receipts:
                return SendReceipt(
                    msg_id="",
                    status="empty_roster",
                    session_id=session_id,
                )
            status = "delivered"
            for dr in receipts:
                if dr.status != "delivered":
                    status = "accepted"
                    break
            return SendReceipt(
                msg_id=receipts[0].msg_id,
                status=status,
                session_id=session_id,
            )
        elif target.kind == AddressKind.CHANNEL:
            receipts = self.channel(session_id, sender, target.channel_id, envelope)
            status = "delivered"
            for dr in receipts:
                if dr.status != "delivered":
                    status = "accepted"
                    break
            return SendReceipt(
                msg_id=receipts[0].msg_id if receipts else "",
                status=status,
                session_id=session_id,
            )
        elif target.kind == AddressKind.NOTICE:
            receipts = self.notice(session_id, sender, target.topic, envelope)
            status = "delivered"
            for dr in receipts:
                if dr.status != "delivered":
                    status = "accepted"
                    break
            return SendReceipt(
                msg_id=receipts[0].msg_id if receipts else "",
                status=status,
                session_id=session_id,
            )
        else:
            raise ValueError(f"unknown address kind: {target.kind}")

    # P1-6: 两阶段投递——锁内只做路由决策（session/ACL/trace/msg_id 装配，
    # 快速路径），锁外做 sink 投递（durable outbox 写 + transport 往返）。
    # 修复：kernel 全局 RLock 持锁跨网络 I/O 会让网关全局串行化。
    def direct(self, session_id: str, sender: str, to_agent: str,
               envelope: Envelope) -> SendReceipt:
        """Send a direct message to one recipient. No fanout."""
        with self._lock:
            session = self._require_session(session_id)
            self._check_direct(session, sender, to_agent)

            self._ensure_trace_id(envelope)
            msg_id = self._gen_msg_id()
            created_at = self._gen_created_at()

        # P1-6 阶段 2：锁外投递——sink.deliver 可能含 SSH 往返（60s 量级），
        # 不得占着全局 RLock。
        sink_receipt = self._sink.deliver(session_id, to_agent, envelope, msg_id, created_at, sender)
        return SendReceipt(msg_id=msg_id, status=sink_receipt.status,
                           session_id=session_id, target=to_agent,
                           queued=getattr(sink_receipt, "queued", False))

    # P1-6: 两阶段投递 + 并行 fan-out。路由决策（roster 快照 + msg_id 装配）
    # 锁内完成；sink 投递（transport 往返）锁外执行——broadcast 是 N 个
    # 独立 SSH 往返，串行投递会按 N 倍阻塞网关，必须并行。
    def broadcast(self, session_id: str, sender: str,
                  envelope: Envelope) -> list[DeliveryReceipt]:
        """Broadcast to all room members except sender.

        Each recipient gets its own msg_id — DeliveryEngine's msg_id
        idempotency would otherwise short-circuit every recipient after
        the first (one outbox entry per msg_id, so only the first
        fan-out copy is actually sent).  Per-recipient ids keep every
        copy durable and deliverable across hosts.
        """
        with self._lock:
            session = self._require_session(session_id)
            self._check_broadcast(session, sender)

            self._ensure_trace_id(envelope)
            created_at = self._gen_created_at()
            recipients = session.roster.without(sender)
            # msg_id 在锁内统一装配（uuid4 线程安全，但集中生成保持 trace
            # 语义与确定性）；envelope 只读共享给各 worker（deliver_sink
            # 内部浅拷贝，不 mutate 原 envelope）。
            msg_ids = [self._gen_msg_id() for _ in recipients]

        def _deliver_one(recipient: str, msg_id: str) -> DeliveryReceipt:
            # P1-6 阶段 2：锁外投递——每个接收方独立的 transport 往返。
            sink_receipt = self._sink.deliver(
                session_id, recipient, envelope, msg_id, created_at, sender
            )
            return DeliveryReceipt(
                msg_id=msg_id, recipient=recipient,
                status=sink_receipt.status,
                error=getattr(sink_receipt, "error", ""),
            )

        # P1-6: 并行投递（ThreadPoolExecutor）——pool.map 结果按 roster 顺序
        # 返回，调用方聚合逻辑（send 的 max-status）不受影响。小 fan-out
        # 保持顺序：线程池唤醒开销 > 并行收益，且 sink 调用顺序确定
        # （顺序 side_effect 的 mock 依赖该语义）。
        if len(recipients) >= self._BROADCAST_PARALLEL_MIN:
            workers = min(len(recipients), self._BROADCAST_MAX_WORKERS)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                return list(pool.map(_deliver_one, recipients, msg_ids))
        return [_deliver_one(r, m) for r, m in zip(recipients, msg_ids)]

    # P1-6: 与 direct/broadcast 相同的两阶段——锁内路由决策（channel 查表 +
    # 成员快照 + msg_id 装配），锁外投递（transport 往返不占全局 RLock）。
    def channel(self, session_id: str, sender: str, channel_id: str,
                envelope: Envelope) -> list[DeliveryReceipt]:
        """Send to a channel (fan out to channel members except sender)."""
        with self._lock:
            session = self._require_session(session_id)
            channels = self._channels.get(session_id, {})
            if channel_id not in channels:
                raise ValueError(f"channel not found: {channel_id}")
            ch = channels[channel_id]
            self._check_channel(session, ch, sender)

            self._ensure_trace_id(envelope)
            created_at = self._gen_created_at()
            members = [m for m in ch.members if m != sender]
            # per-recipient msg_id: no delivery short-circuit
            msg_ids = [self._gen_msg_id() for _ in members]

        # P1-6 阶段 2：锁外投递。channel 成员通常较少，顺序投递即可——
        # 关键是不再占着全局锁。
        receipts = []
        for member, msg_id in zip(members, msg_ids):
            sink_receipt = self._sink.deliver(session_id, member, envelope, msg_id, created_at, sender)
            receipts.append(DeliveryReceipt(
                msg_id=msg_id, recipient=member,
                status=sink_receipt.status,
                error=getattr(sink_receipt, "error", ""),
            ))
        return receipts

    # P1-6: 与 direct/broadcast 相同的两阶段——锁内路由决策（topic 订阅者
    # 快照 + msg_id 装配），锁外投递（transport 往返不占全局 RLock）。
    def notice(self, session_id: str, sender: str, topic: str,
               envelope: Envelope, ttl: int = 0) -> list[DeliveryReceipt]:
        """Send a notice, fanning out to topic subscribers (or session).

        If agents subscribed to *topic*, the notice goes only to them.
        Otherwise it falls back to all room members (session-wide notice).
        """
        with self._lock:
            session = self._require_session(session_id)
            self._check_notice(session, sender)

            self._ensure_trace_id(envelope)
            created_at = self._gen_created_at()

            # Topic-based fan-out: subscribers of this topic only.
            topic_members = self._topic_subscriptions.get(session_id, {}).get(topic, set())
            if topic_members:
                targets = topic_members
            else:
                targets = set(session.acl.room_members)
            targets.discard(sender)
            targets = sorted(targets)
            # per-recipient msg_id: no delivery short-circuit
            msg_ids = [self._gen_msg_id() for _ in targets]

        # P1-6 阶段 2：锁外投递。
        receipts = []
        for member, msg_id in zip(targets, msg_ids):
            sink_receipt = self._sink.deliver(session_id, member, envelope, msg_id, created_at, sender)
            receipts.append(DeliveryReceipt(
                msg_id=msg_id, recipient=member,
                status=sink_receipt.status,
                error=getattr(sink_receipt, "error", ""),
            ))
        return receipts

    def _history_filtered(self, session_id: str, field_name: str,
                          field_value: str) -> list[dict]:
        """P3-14: Scan history files filtering by a single field.

        Unlike read_history() (full validation + all fields), this reads
        each file once and filters immediately — O(files) I/O but skips
        the validate_message overhead and does not buffer unrelated msgs.
        """
        hdir = self._store.history_dir(session_id)
        if not hdir.is_dir():
            return []
        result = []
        for f in sorted(hdir.iterdir()):
            if not f.is_file() or f.suffix != ".json":
                continue
            try:
                msg = json.loads(f.read_bytes())
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                continue
            if msg.get(field_name) == field_value:
                result.append(msg)
        return result

    @_synchronized  # P1-4
    def trace(self, session_id: str, trace_id: str) -> dict:
        """Top4: 按 trace_id 聚合 canonical history —— 跨主机消息链 +
        每 leaf 投递状态（delivered/consumed/无）。

        trace_id 由 kernel 入口生成（direct/broadcast/channel/notice），
        fan-out 各 msg_id 共用同一 trace；causation_id 表达转发关系。

        P3-14: 使用 _history_filtered 按 trace_id 索引，避免全量
        read_history（跳过 validate_message + 不相关消息的读取）。
        """
        self._require_session(session_id)
        # P3-14: targeted scan instead of full read_history.
        msgs = self._history_filtered(session_id, "trace_id", trace_id)
        if not msgs:
            raise ValueError(f"no messages with trace_id: {trace_id}")

        # leaf 投递状态：有 engine（EngineDeliverySink）查 outbox markers；
        # 无 engine（LocalDeliverySink 直写 inbox）→ history 存在即已投递。
        engine = getattr(self._sink, "_engine", None)
        outbox_root = None
        if engine is not None:
            outbox_root = getattr(engine, "_outbox", None)
        leaves = []
        for m in sorted(msgs, key=lambda x: x.get("created_at", "")):
            mid = m.get("msg_id", "")
            state = "delivered" if outbox_root is None else "unknown"
            if outbox_root is not None:
                sd = outbox_root / session_id
                if (sd / f".delivered-{mid}").exists():
                    state = "delivered"
            leaves.append({
                "msg_id": mid,
                "from": m.get("from", ""),
                "to": m.get("to", ""),
                "subject": m.get("subject", ""),
                "kind": m.get("kind", ""),
                "causation_id": m.get("causation_id", ""),
                "created_at": m.get("created_at", ""),
                "state": state,
            })
        return {
            "trace_id": trace_id,
            "session_id": session_id,
            "leaf_count": len(leaves),
            "leaves": leaves,
        }

    # ── Poll ───────────────────────────────────────────────────────────

    @_synchronized  # P1-4
    def poll(self, session_id: str, agent_id: str,
             cursor: str = "", limit: int = 50) -> PollResult:
        """Read messages from agent inbox, filtering by cursor.

        P1-7: Cursor is a composite key ``"created_at|msg_id"`` so that
        messages arriving in the same second are never skipped.  Old-format
        cursors (bare ``created_at`` without ``|``) are still accepted —
        they compare as ``(ts, "")`` which correctly includes all same-second
        messages.
        """
        self._require_session(session_id)

        # P1-7: parse composite cursor into (timestamp, msg_id) tuple.
        if cursor and "|" in cursor:
            cursor_ts, cursor_id = cursor.rsplit("|", 1)
        else:
            cursor_ts, cursor_id = cursor, ""

        inbox = self._store.agent_subdir(session_id, agent_id, "inbox")

        # Read all messages, filter by cursor.  Use the store's mtime-ordered
        # listing so poll() order matches read()/ack() (oldest first) — a
        # mismatch here made poll-then-ack select different messages.
        files = self._store.list_messages(inbox)

        messages: list[dict] = []
        for f in files:
            try:
                msg = json.loads(f.read_bytes())
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                continue
            # P1-7: composite (created_at, msg_id) comparison — strict >
            # ensures same-second messages with different ids are all returned.
            if cursor:
                msg_ts = msg.get("created_at", "")
                msg_id = msg.get("msg_id", "")
                if (msg_ts, msg_id) <= (cursor_ts, cursor_id):
                    continue
            messages.append(msg)
            if len(messages) >= limit:
                break

        # P1-7: new cursor includes msg_id for same-second safety.
        if messages:
            last = messages[-1]
            new_cursor = f"{last['created_at']}|{last['msg_id']}"
        else:
            new_cursor = cursor
        has_more = len(messages) == limit

        # Fire subscription callbacks for new messages
        subs = self._subscriptions.get(session_id, {}).get(agent_id, [])
        for sub in subs:
            for msg in messages:
                if self._matches_subscription(sub, msg):
                    try:
                        sub.callback(msg)
                    except Exception:
                        pass  # callback errors are swallowed

        return PollResult(messages=messages, cursor=new_cursor, has_more=has_more)

    def _matches_subscription(self, sub: Subscription, msg: dict) -> bool:
        """Check if a message matches a subscription's filters."""
        if sub.channels:
            msg_channel = msg.get("channel_id", "")
            if msg_channel not in sub.channels:
                return False
        if sub.kinds:
            if msg.get("kind", "") not in sub.kinds:
                return False
        return True

    # ── Subscribe ──────────────────────────────────────────────────────

    @_synchronized  # P1-4
    def attach_receiver(self, receiver: Any) -> None:
        """Attach a SwarmReceiver for push-mode callback routing.

        When a receiver is attached, ``subscribe()`` also registers the
        callback with the receiver so it fires on stream/watch events.
        """
        self._receiver = receiver

    @_synchronized  # P1-4
    def subscribe(
        self,
        session_id: str,
        agent_id: str,
        callback: Callable[[dict], None],
        channels: Optional[list[str]] = None,
        kinds: Optional[list[str]] = None,
        topics: Optional[list[str]] = None,
    ) -> Subscription:
        """Register an in-memory callback for new messages.

        Callbacks fire on poll() — push simulation until D2 wires real push.
        When a SwarmReceiver is attached, the callback is also registered
        there for real-time push delivery.

        *topics* registers the agent for topic-based notice fan-out.
        """
        self._require_session(session_id)
        sub = Subscription(
            agent_id=agent_id,
            session_id=session_id,
            callback=callback,
            channels=channels or [],
            kinds=kinds or [],
            topics=topics or [],
        )
        self._subscriptions.setdefault(session_id, {}).setdefault(agent_id, []).append(sub)
        # Topic-based routing registry (notice fan-out targets).
        for topic in sub.topics:
            self._topic_subscriptions.setdefault(session_id, {}).setdefault(topic, set()).add(agent_id)
        # Route to attached receiver for push-mode delivery (D2).
        if (
            self._receiver is not None
            and session_id == self._receiver.session_id
            and agent_id == self._receiver.agent_id
        ):
            self._receiver.subscribe(callback, channels, kinds)
        return sub

    # ── Ack ────────────────────────────────────────────────────────────

    @_synchronized  # P1-4
    def ack(self, session_id: str, agent_id: str, msg_id: str,
            phase: str = "consumed") -> str:
        """Acknowledge a message — finalize or release based on phase.

        phase="consumed" → finalize (move to archive)
        phase="released" → release (move back to inbox)

        P3-o: try finalize() first (requires claim file from two-phase
        read); on failure fall back to finalize_from_inbox() (no claim
        needed — covers auto-consumed messages from SwarmReceiver).
        """
        self._require_session(session_id)
        if phase == "consumed":
            try:
                return self._store.finalize(session_id, agent_id, msg_id, owner=agent_id)
            except ValueError:
                # P3-o: no claim file → message was auto-consumed (inbox→
                # archive directly).  Use finalize_from_inbox instead.
                return self._store.finalize_from_inbox(session_id, agent_id, msg_id, owner=agent_id)
        elif phase == "released":
            return self._store.release(session_id, agent_id, msg_id, owner=agent_id)
        else:
            raise ValueError(f"unknown ack phase: {phase}")

    # ── Accessors ──────────────────────────────────────────────────────

    @_synchronized  # P1-4
    def get_session(self, session_id: str) -> Optional[Session]:
        return self._sessions.get(session_id)

    @_synchronized  # P1-4
    def get_location(self, session_id: str, agent_id: str) -> Optional[AgentLocation]:
        return self._routing.get((session_id, agent_id))

    # ── Manager-pull: pull_remote / ingest / replay ─────────────────

    @_synchronized  # P1-4
    def pull_remote(self, session_id: str, from_host: str) -> list[dict]:
        """Pull messages from the Manager inbox on a remote worker host.

        Finds agents registered with ``ReturnMode.MANAGER_PULL`` on
        *from_host*, then SSHes to that host to read the Manager's
        host-local inbox via ``codeagent mailbox read --host``.

        Returns a list of claimed message dicts (may be empty).
        """
        session = self._require_session(session_id)

        # Find manager-pull agents registered on from_host.
        # Fallback: when loc.return_mode is unset (CLI register),
        # check session.return_modes dict (set at session creation
        # or via --return-mode arg).
        pull_agents = [
            aid for (sid, aid), loc in self._routing.items()
            if sid == session_id
            and loc.host_alias == from_host
            and (
                loc.return_mode == ReturnMode.MANAGER_PULL
                or (
                    loc.return_mode is None
                    and session.return_modes.get(aid)
                    == ReturnMode.MANAGER_PULL.value
                )
            )
        ]
        if not pull_agents:
            return []

        messages: list[dict] = []
        manager_id = session.manager_id

        # P3-13: every pull_agent read targets the SAME manager inbox on
        # from_host, differing only by mailbox_root.  Group by root so N
        # agents on one host cost one ``aimeshchat mailbox read`` subprocess
        # instead of N.
        by_root: dict[str, list[str]] = {}
        for agent_id in pull_agents:
            agent_loc = self._routing.get((session_id, agent_id))
            root = agent_loc.mailbox_root if agent_loc else ""
            by_root.setdefault(root, []).append(agent_id)

        for agent_mailbox_root, root_agents in by_root.items():
            cmd = [
                "aimeshchat", "mailbox", "read",
                "--session", session_id,
                "--agent", manager_id,
                "--owner", manager_id,
                "--host", from_host,
                "--json",
            ]
            if agent_mailbox_root:
                cmd.extend(["--mailbox-root", agent_mailbox_root])
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=30,
                )
                if result.returncode != 0:
                    log.warning(
                        "pull_remote: mailbox read failed for agents=%s host=%s: %s",
                        root_agents, from_host, result.stderr.strip(),
                    )
                    continue
                raw = result.stdout.strip()
                if not raw:
                    continue
                msg = json.loads(raw)
                if isinstance(msg, dict):
                    msg["_pull_host"] = from_host
                    msg["_pull_mailbox_root"] = agent_mailbox_root
                    messages.append(msg)
                elif isinstance(msg, list):
                    for m in msg:
                        m["_pull_host"] = from_host
                        m["_pull_mailbox_root"] = agent_mailbox_root
                    messages.extend(msg)
            except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
                log.warning(
                    "pull_remote: error reading from agents=%s host=%s: %s",
                    root_agents, from_host, exc,
                )
        return messages

    def finalize_remote(self, from_host: str, session_id: str,
                        manager_id: str, msg: dict,
                        mailbox_root: str = "") -> None:
        """Finalize a message on the remote mailbox after successful read."""
        msg_id = msg.get("msg_id", "")
        if not msg_id:
            return
        finalize_cmd = [
            "aimeshchat", "mailbox", "finalize",
            "--host", from_host,
            "--session", session_id,
            "--agent", manager_id,
            "--owner", manager_id,
            "--msg-id", msg_id,
        ]
        if mailbox_root:
            finalize_cmd.extend(["--mailbox-root", mailbox_root])
        try:
            result = subprocess.run(
                finalize_cmd, capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                log.warning(
                    "pull_remote: finalize failed for msg=%s host=%s: %s",
                    msg_id, from_host, result.stderr.strip(),
                )
        except (subprocess.TimeoutExpired, OSError) as exc:
            log.warning(
                "pull_remote: finalize error for msg=%s host=%s: %s",
                msg_id, from_host, exc,
            )

    def release_remote(self, session_id: str, msg_id: str, from_host: str,
                       manager_id: str, mailbox_root: str = "") -> bool:
        """Release message back to remote inbox (on ingest/ACL failure)."""
        cmd = ["aimeshchat", "mailbox", "release",
               "--host", from_host, "--session", session_id,
               "--agent", manager_id, "--owner", manager_id,
               "--msg-id", msg_id]
        if mailbox_root:
            cmd.insert(1, "--mailbox-root")
            cmd.insert(2, mailbox_root)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return r.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    def _batch_remote_mailbox(self, from_host: str, subcmd: str,
                              session_id: str, manager_id: str,
                              msg_ids: list[str],
                              mailbox_root: str = "") -> bool:
        """P3-13: Run one mailbox subcommand for many msg_ids in ONE SSH call.

        Reuses the host's ControlMaster socket (same connection the
        transport layer multiplexes) so the whole batch costs a single
        subprocess + SSH session instead of one per msg_id.  The remote
        side runs plain local mailbox ops (no ``--host`` — aimeshchat's
        ``--host`` would re-dispatch through the router and double-hop).

        Returns True if every command in the chain exited 0 (aggregate —
        the remote ``aimeshchat`` per-msg exit codes are not individually
        surfaced).
        """
        if not msg_ids:
            return True
        # Remote shell prefix: same PATH fix the SSHTransport.mailbox path
        # applies (non-interactive SSH omits ~/.local/bin), plus MAILBOX_ROOT
        # so the remote store resolves to the same root as the single-msg
        # finalize_remote/release_remote calls.
        parts = ["export PATH=$HOME/.local/bin:$PATH"]
        if mailbox_root:
            parts.append(f"export MAILBOX_ROOT={shlex.quote(mailbox_root)}")
        for mid in msg_ids:
            cmd_parts = ["aimeshchat", "mailbox", subcmd,
                         "--session", session_id,
                         "--agent", manager_id,
                         "--owner", manager_id,
                         "--msg-id", mid]
            parts.append(" ".join(shlex.quote(p) for p in cmd_parts))
        remote_cmd = "; ".join(parts)
        try:
            from codeagent.transport.control_master import ControlMaster
            cm = ControlMaster(from_host)
            if not cm.is_alive():
                cm.create()
            ssh_cmd = cm.ssh_cmd(remote_cmd)
            result = subprocess.run(
                ssh_cmd, capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                log.warning(
                    "P3-13: batch %s host=%s rc=%d stderr=%s",
                    subcmd, from_host, result.returncode,
                    result.stderr.strip()[:200],
                )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, OSError, ImportError) as exc:
            log.warning(
                "P3-13: batch %s host=%s error: %s",
                subcmd, from_host, exc,
            )
            return False

    def batch_finalize_remote(self, from_host: str, session_id: str,
                              manager_id: str, msg_ids: list[str],
                              mailbox_root: str = "") -> None:
        """P3-13: Finalize multiple messages in a single SSH call.

        Replaces N ``aimeshchat mailbox finalize`` subprocesses (one per
        msg_id) with a single ControlMaster-reusing SSH invocation.
        """
        self._batch_remote_mailbox(
            from_host, "finalize", session_id, manager_id,
            list(msg_ids), mailbox_root,
        )

    def batch_release_remote(self, session_id: str, msg_ids: list[str],
                             from_host: str, manager_id: str,
                             mailbox_root: str = "") -> dict[str, bool]:
        """P3-13: Release multiple messages in a single SSH call.

        Returns {msg_id: ok} — all ids share the aggregate exit code
        (the remote chain runs every release even if one fails).
        """
        ok = self._batch_remote_mailbox(
            from_host, "release", session_id, manager_id,
            list(msg_ids), mailbox_root,
        )
        return {mid: ok for mid in msg_ids}

    @_synchronized  # P1-4
    def ingest(self, session_id: str, messages: list[dict]) -> list[str]:
        """Validate and persist messages into canonical history.

        Each message is validated against the session (roster/ACL via
        ``validate_message``).  Valid messages are appended to the
        session's canonical history (``history/<msg_id>.json``, O_EXCL
        atomic).  Duplicates and invalid messages are silently skipped
        with a warning log.

        Returns the list of successfully persisted msg_ids.
        """
        session = self._require_session(session_id)
        persisted: list[str] = []
        for msg in messages:
            if not isinstance(msg, dict):
                log.warning("ingest: skipping non-dict message: %s", type(msg).__name__)
                continue
            ok, reason = validate_message(msg, session_id)
            if not ok:
                log.warning("ingest: skipping invalid message: %s", reason)
                continue
            sender = msg.get("from", "")
            if sender and sender not in session.roster:
                log.debug("ingest: sender %s not in roster, skipping", sender)
                continue
            recipient = msg.get("to", "")
            if recipient and recipient != session.manager_id:
                log.debug("ingest: recipient %s != manager_id %s, skipping",
                          recipient, session.manager_id)
                continue
            try:
                self._store.append_history(session_id, msg)
                persisted.append(msg["msg_id"])
            except ValueError as exc:
                # Duplicate msg_id or other store-level validation failure
                log.warning("ingest: skipping message %s: %s",
                            msg.get("msg_id", "?"), exc)
        return persisted

    @_synchronized  # P1-4
    def replay(self, session_id: str, request_id: str) -> list[dict]:
        """Return history entries for *request_id* in chronological order.

        Filters the canonical session history by ``request_id`` and
        returns matching messages sorted oldest-first (``created_at``
        ascending) so callers can reconstruct the request→response
        timeline.

        P3-14: 使用 _history_filtered 按 request_id 索引，避免全量
        read_history（跳过 validate_message + 不相关消息的读取）。
        """
        self._require_session(session_id)
        # P3-14: targeted scan instead of full read_history.
        filtered = self._history_filtered(session_id, "request_id", request_id)
        filtered.sort(key=lambda m: (m.get("created_at", ""), m.get("msg_id", "")))
        return filtered
