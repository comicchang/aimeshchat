"""SwarmKernel — IRC-style session/roster/ACL/routing kernel.

Owns session lifecycle, roster/ACL enforcement, and message routing.
Does NOT own transport I/O — that is delegated to a DeliverySink.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Callable, Optional, Protocol
from uuid import uuid4

from codeagent.mailbox.protocol import (
    BROADCAST_TO,
    AttachmentRef,
    Message,
    validate_agent_id,
    validate_message,
)
from codeagent.mailbox.store import MailboxStore
from codeagent.swarm.model import (
    ACL,
    Address,
    AddressKind,
    AgentLocation,
    Channel,
    DeliveryReceipt,
    Envelope,
    PollResult,
    Registration,
    Roster,
    SendReceipt,
    Session,
    Subscription,
)


# ── Delivery sink protocol ─────────────────────────────────────────────


class DeliverySink(Protocol):
    """Anything that can deliver an envelope to an agent inbox."""
    def deliver(self, session_id: str, target_agent: str, envelope: Envelope,
                msg_id: str, created_at: str, from_id: str) -> None: ...


class LocalDeliverySink:
    """Writes directly to MailboxStore — used in-process and for tests."""

    def __init__(self, store: MailboxStore):
        self._store = store

    def deliver(self, session_id: str, target_agent: str, envelope: Envelope,
                msg_id: str, created_at: str, from_id: str) -> None:
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
            attachments=[a.to_dict() for a in envelope.attachments] if envelope.attachments else None,
        )


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

    # ── Session lifecycle ──────────────────────────────────────────────

    def create_session(
        self,
        session_id: str,
        manager_id: str,
        roster: list[str],
        acl: Optional[ACL] = None,
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
            created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

        # Persist to filesystem via MailboxStore
        self._store.session_init(
            session_id, manager_id,
            [a for a in all_members if a != manager_id],
        )

        # Store swarm-level metadata alongside session.json
        meta = {
            "acl": {
                "authority": acl.authority,
                "allowed_senders": acl.allowed_senders,
                "room_members": acl.room_members,
                "policy": acl.policy,
            },
        }
        meta_path = self._store.session_dir(session_id) / "swarm-meta.json"
        tmp = meta_path.parent / ".tmp-swarm-meta.json"
        with open(tmp, "w") as f:
            f.write(json.dumps(meta, indent=2, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(meta_path))

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

        The agent must be in the session's roster.
        """
        session = self._require_session(session_id)
        if location.agent_id not in session.roster:
            raise ValueError(f"agent not in roster: {location.agent_id}")
        self._routing[(session_id, location.agent_id)] = location
        return Registration(
            agent_id=location.agent_id,
            session_id=session_id,
            location=location,
        )

    def unregister(self, session_id: str, agent_id: str) -> None:
        """Remove an agent from the routing table."""
        self._require_session(session_id)
        self._routing.pop((session_id, agent_id), None)

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
        return channel

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

    def _gen_created_at(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def send(self, session_id: str, sender: str, target: Address,
             envelope: Envelope) -> SendReceipt:
        """Route a message according to its Address kind.

        Dispatches to direct/broadcast/channel/notice.
        """
        if target.kind == AddressKind.DIRECT:
            return self.direct(session_id, sender, target.agent_id, envelope)
        elif target.kind == AddressKind.BROADCAST:
            receipts = self.broadcast(session_id, sender, envelope)
            return SendReceipt(
                msg_id=receipts[0].msg_id if receipts else "",
                status="delivered",
                session_id=session_id,
            )
        elif target.kind == AddressKind.CHANNEL:
            return self.channel(session_id, sender, target.channel_id, envelope)
        elif target.kind == AddressKind.NOTICE:
            return self.notice(session_id, sender, target.topic, envelope)
        else:
            raise ValueError(f"unknown address kind: {target.kind}")

    def direct(self, session_id: str, sender: str, to_agent: str,
               envelope: Envelope) -> SendReceipt:
        """Send a direct message to one recipient. No fanout."""
        session = self._require_session(session_id)
        self._check_direct(session, sender, to_agent)

        msg_id = self._gen_msg_id()
        created_at = self._gen_created_at()

        self._sink.deliver(session_id, to_agent, envelope, msg_id, created_at, sender)
        return SendReceipt(msg_id=msg_id, status="delivered",
                           session_id=session_id, target=to_agent)

    def broadcast(self, session_id: str, sender: str,
                  envelope: Envelope) -> list[DeliveryReceipt]:
        """Broadcast to all room members except sender."""
        session = self._require_session(session_id)
        self._check_broadcast(session, sender)

        msg_id = self._gen_msg_id()
        created_at = self._gen_created_at()
        recipients = session.roster.without(sender)

        # Deliver to mailbox via BROADCAST_TO marker (store fans out)
        self._sink.deliver(session_id, BROADCAST_TO, envelope, msg_id, created_at, sender)

        receipts = []
        for r in recipients:
            receipts.append(DeliveryReceipt(
                msg_id=msg_id, recipient=r, status="delivered",
            ))
        return receipts

    def channel(self, session_id: str, sender: str, channel_id: str,
                envelope: Envelope) -> SendReceipt:
        """Send to a channel (fan out to channel members except sender)."""
        session = self._require_session(session_id)
        channels = self._channels.get(session_id, {})
        if channel_id not in channels:
            raise ValueError(f"channel not found: {channel_id}")
        ch = channels[channel_id]
        self._check_channel(session, ch, sender)

        msg_id = self._gen_msg_id()
        created_at = self._gen_created_at()

        for member in ch.members:
            if member == sender:
                continue
            self._sink.deliver(session_id, member, envelope, msg_id, created_at, sender)

        return SendReceipt(msg_id=msg_id, status="delivered",
                           session_id=session_id, target=f"#{channel_id}")

    def notice(self, session_id: str, sender: str, topic: str,
               envelope: Envelope, ttl: int = 0) -> SendReceipt:
        """Send a notice to the session (room_members), with optional TTL."""
        session = self._require_session(session_id)
        self._check_notice(session, sender)

        msg_id = self._gen_msg_id()
        created_at = self._gen_created_at()

        for member in session.acl.room_members:
            if member == sender:
                continue
            self._sink.deliver(session_id, member, envelope, msg_id, created_at, sender)

        return SendReceipt(msg_id=msg_id, status="delivered",
                           session_id=session_id, target="notice")

    # ── Poll ───────────────────────────────────────────────────────────

    def poll(self, session_id: str, agent_id: str,
             cursor: str = "", limit: int = 50) -> PollResult:
        """Read messages from agent inbox, filtering by cursor.

        Cursor is the created_at of the last consumed message.
        Also fires any registered subscription callbacks for new messages.
        """
        self._require_session(session_id)

        inbox = self._store.agent_subdir(session_id, agent_id, "inbox")
        files = self._store.list_messages(inbox)

        # Read all messages, filter by cursor
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

    def subscribe(
        self,
        session_id: str,
        agent_id: str,
        callback: Callable[[dict], None],
        channels: Optional[list[str]] = None,
        kinds: Optional[list[str]] = None,
    ) -> Subscription:
        """Register an in-memory callback for new messages.

        Callbacks fire on poll() — push simulation until D2 wires real push.
        """
        self._require_session(session_id)
        sub = Subscription(
            agent_id=agent_id,
            session_id=session_id,
            callback=callback,
            channels=channels or [],
            kinds=kinds or [],
        )
        self._subscriptions.setdefault(session_id, {}).setdefault(agent_id, []).append(sub)
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
