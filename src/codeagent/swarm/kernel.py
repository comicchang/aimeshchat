"""SwarmKernel — IRC-style session/roster/ACL/routing kernel.

Owns session lifecycle, roster/ACL enforcement, and message routing.
Does NOT own transport I/O — that is delegated to a DeliverySink.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
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


class SwarmKernel:
    """Protocol kernel — owns session/roster/ACL/routing, not transport.

    Parameters
    ----------
    store : MailboxStore
        Filesystem-backed store for persistence.
    sink : DeliverySink
        Pluggable delivery backend (LocalDeliverySink for tests, C2 for prod).
    """

    def __init__(self, store: MailboxStore, sink: Optional[DeliverySink] = None):
        self._store = store
        self._sink = sink or LocalDeliverySink(store)
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

    def register(self, location: AgentLocation, session_id: str,
                 token: str = "") -> Registration:
        """Register an agent's location in the routing table.

        The agent must be in the session's roster.  The mapping is
        persisted to swarm-meta.json so later CLI processes (one per
        subcommand) resolve the host for cross-host delivery.
        """
        session = self._require_session(session_id)
        if location.agent_id not in session.roster:
            raise ValueError(f"agent not in roster: {location.agent_id}")
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

    def direct(self, session_id: str, sender: str, to_agent: str,
               envelope: Envelope) -> SendReceipt:
        """Send a direct message to one recipient. No fanout."""
        session = self._require_session(session_id)
        self._check_direct(session, sender, to_agent)

        self._ensure_trace_id(envelope)
        msg_id = self._gen_msg_id()
        created_at = self._gen_created_at()

        sink_receipt = self._sink.deliver(session_id, to_agent, envelope, msg_id, created_at, sender)
        return SendReceipt(msg_id=msg_id, status=sink_receipt.status,
                           session_id=session_id, target=to_agent,
                           queued=getattr(sink_receipt, "queued", False))

    def broadcast(self, session_id: str, sender: str,
                  envelope: Envelope) -> list[DeliveryReceipt]:
        """Broadcast to all room members except sender.

        Each recipient gets its own msg_id — DeliveryEngine's msg_id
        idempotency would otherwise short-circuit every recipient after
        the first (one outbox entry per msg_id, so only the first
        fan-out copy is actually sent).  Per-recipient ids keep every
        copy durable and deliverable across hosts.
        """
        session = self._require_session(session_id)
        self._check_broadcast(session, sender)

        self._ensure_trace_id(envelope)
        created_at = self._gen_created_at()
        recipients = session.roster.without(sender)

        receipts = []
        for r in recipients:
            msg_id = self._gen_msg_id()
            sink_receipt = self._sink.deliver(session_id, r, envelope, msg_id, created_at, sender)
            receipts.append(DeliveryReceipt(
                msg_id=msg_id, recipient=r,
                status=sink_receipt.status,
                error=getattr(sink_receipt, "error", ""),
            ))
        return receipts

    def channel(self, session_id: str, sender: str, channel_id: str,
                envelope: Envelope) -> list[DeliveryReceipt]:
        """Send to a channel (fan out to channel members except sender)."""
        session = self._require_session(session_id)
        channels = self._channels.get(session_id, {})
        if channel_id not in channels:
            raise ValueError(f"channel not found: {channel_id}")
        ch = channels[channel_id]
        self._check_channel(session, ch, sender)

        self._ensure_trace_id(envelope)
        created_at = self._gen_created_at()

        receipts = []
        for member in ch.members:
            if member == sender:
                continue
            msg_id = self._gen_msg_id()  # per-recipient: no delivery short-circuit
            sink_receipt = self._sink.deliver(session_id, member, envelope, msg_id, created_at, sender)
            receipts.append(DeliveryReceipt(
                msg_id=msg_id, recipient=member,
                status=sink_receipt.status,
                error=getattr(sink_receipt, "error", ""),
            ))
        return receipts

    def notice(self, session_id: str, sender: str, topic: str,
               envelope: Envelope, ttl: int = 0) -> list[DeliveryReceipt]:
        """Send a notice, fanning out to topic subscribers (or session).

        If agents subscribed to *topic*, the notice goes only to them.
        Otherwise it falls back to all room members (session-wide notice).
        """
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

        receipts = []
        for member in sorted(targets):
            msg_id = self._gen_msg_id()  # per-recipient: no delivery short-circuit
            sink_receipt = self._sink.deliver(session_id, member, envelope, msg_id, created_at, sender)
            receipts.append(DeliveryReceipt(
                msg_id=msg_id, recipient=member,
                status=sink_receipt.status,
                error=getattr(sink_receipt, "error", ""),
            ))
        return receipts

    def trace(self, session_id: str, trace_id: str) -> dict:
        """Top4: 按 trace_id 聚合 canonical history —— 跨主机消息链 +
        每 leaf 投递状态（delivered/consumed/无）。

        trace_id 由 kernel 入口生成（direct/broadcast/channel/notice），
        fan-out 各 msg_id 共用同一 trace；causation_id 表达转发关系。
        """
        self._require_session(session_id)
        history = self._store.read_history(session_id)
        msgs = [m for m in history if m.get("trace_id") == trace_id]
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

    def poll(self, session_id: str, agent_id: str,
             cursor: str = "", limit: int = 50) -> PollResult:
        """Read messages from agent inbox, filtering by cursor.

        Cursor is the created_at of the last consumed message.
        Also fires any registered subscription callbacks for new messages.
        """
        self._require_session(session_id)

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
            if cursor and msg.get("created_at", "") <= cursor:
                continue
            messages.append(msg)
            if len(messages) >= limit:
                break

        new_cursor = messages[-1]["created_at"] if messages else cursor
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

    def attach_receiver(self, receiver: Any) -> None:
        """Attach a SwarmReceiver for push-mode callback routing.

        When a receiver is attached, ``subscribe()`` also registers the
        callback with the receiver so it fires on stream/watch events.
        """
        self._receiver = receiver

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

    def ack(self, session_id: str, agent_id: str, msg_id: str,
            phase: str = "consumed") -> str:
        """Acknowledge a message — finalize or release based on phase.

        phase="consumed" → finalize (move to archive)
        phase="released" → release (move back to inbox)
        """
        self._require_session(session_id)
        if phase == "consumed":
            return self._store.finalize(session_id, agent_id, msg_id, owner=agent_id)
        elif phase == "released":
            return self._store.release(session_id, agent_id, msg_id, owner=agent_id)
        else:
            raise ValueError(f"unknown ack phase: {phase}")

    # ── Accessors ──────────────────────────────────────────────────────

    def get_session(self, session_id: str) -> Optional[Session]:
        return self._sessions.get(session_id)

    def get_location(self, session_id: str, agent_id: str) -> Optional[AgentLocation]:
        return self._routing.get((session_id, agent_id))
