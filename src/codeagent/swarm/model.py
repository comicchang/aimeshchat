"""Swarm data types — agents, addresses, envelopes, sessions, ACL."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import json
from pathlib import Path
from typing import Iterator

from codeagent.mailbox.protocol import AttachmentRef


# ── Enums ──────────────────────────────────────────────────────────────


class AddressKind(str, Enum):
    """Routing target kind."""
    DIRECT = "direct"
    CHANNEL = "channel"
    BROADCAST = "broadcast"
    NOTICE = "notice"


# ── Agent / Location ───────────────────────────────────────────────────


@dataclass(frozen=True)
class AgentLocation:
    """Where an agent lives."""
    agent_id: str
    host_alias: str       # SSH alias, or "__local__" for co-located
    backend: str           # "cli" | "omp" | "tmux" | "custom"
    capabilities: tuple[str, ...] = ()


# ── Address ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Address:
    """Routing target for a message."""
    kind: AddressKind
    agent_id: str = ""       # DIRECT target
    channel_id: str = ""     # CHANNEL target
    topic: str = ""          # NOTICE topic


# ── Envelope ───────────────────────────────────────────────────────────


@dataclass
class Envelope:
    """Swarm-level message payload — wraps a mailbox Message for kernel routing."""
    subject: str
    body: str
    kind: str = "TASK"
    attachments: list[AttachmentRef] = field(default_factory=list)
    reply_to: str = ""
    run_id: str = ""
    request_id: str = ""
    trace_id: str = ""
    causation_id: str = ""


# ── Session / Roster / ACL ─────────────────────────────────────────────


@dataclass
class Roster:
    """Ordered set of agent_ids belonging to a session."""
    members: list[str] = field(default_factory=list)

    def __contains__(self, agent_id: str) -> bool:
        return agent_id in self.members

    def __iter__(self):
        return iter(self.members)

    def __len__(self):
        return len(self.members)

    def without(self, agent_id: str) -> list[str]:
        return [m for m in self.members if m != agent_id]


@dataclass
class ACL:
    """Access control list for a session or channel.

    authority:          the agent_id that may broadcast unrestricted
    allowed_senders:    agents allowed to send direct/notice messages
    room_members:       agents allowed to participate (superset for direct)
    policy:             free-form policy string ("open" | "restricted" | …)
    """
    authority: str = ""
    allowed_senders: list[str] = field(default_factory=list)
    room_members: list[str] = field(default_factory=list)
    policy: str = "open"


@dataclass
class Channel:
    """A named sub-room within a session."""
    channel_id: str
    members: list[str] = field(default_factory=list)
    acl: Optional[ACL] = None


@dataclass
class Session:
    """Swarm session — groups agents under one conversation."""
    session_id: str
    manager_id: str
    roster: Roster = field(default_factory=Roster)
    acl: ACL = field(default_factory=ACL)
    channels: dict[str, Channel] = field(default_factory=dict)
    created_at: str = ""


# ── Receipts / Results ─────────────────────────────────────────────────


@dataclass(frozen=True)
class SendReceipt:
    """Returned after a successful send."""
    msg_id: str
    status: str            # "accepted" | "delivered" | "consumed"
    session_id: str = ""
    target: str = ""
    queued: bool = False   # True when durable-outbox written but transport pending


@dataclass(frozen=True)
class DeliveryReceipt:
    """Per-recipient delivery outcome (used by broadcast/channel)."""
    msg_id: str
    recipient: str
    status: str            # "delivered" | "failed"
    error: str = ""


@dataclass(frozen=True)
class PollResult:
    """Result of a poll() call."""
    messages: list[dict]
    cursor: str            # opaque cursor for next page
    has_more: bool = False


@dataclass(frozen=True)
class Registration:
    """Returned after register()."""
    agent_id: str
    session_id: str
    location: AgentLocation = field(default_factory=lambda: AgentLocation("", "", ""))


@dataclass
class Subscription:
    """In-memory callback registration."""
    agent_id: str
    session_id: str
    callback: object       # callable[[dict], None]
    channels: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)


# ── Shared inbox scan helper ──────────────────────────────────────────


def _iter_inbox_files(inbox: Path, skip_prefixes: tuple[str, ...] = (".sync-conflict-", ".tmp-")) -> Iterator[Path]:
    """Yield non-hidden .json files in *inbox*, skipping *skip_prefixes*.

    Used by both ``SwarmKernel.poll`` and ``SwarmReceiver._scan_inbox``
    to avoid duplicating file-discovery logic.
    """
    if not inbox.is_dir():
        return
    for f in inbox.iterdir():
        if not f.is_file() or f.suffix != ".json":
            continue
        name = f.name
        if name.startswith("."):
            continue
        if any(name.startswith(pfx) for pfx in skip_prefixes):
            continue
        yield f
