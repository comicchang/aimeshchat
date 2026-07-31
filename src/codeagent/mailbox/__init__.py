"""Mailbox — session-based direct-inbox protocol for multi-agent orchestration.

Modules:
    protocol: Message types, validation, constants
    store:    Filesystem I/O (MailboxStore)
"""
from codeagent.mailbox.protocol import (
    BROADCAST_TO,
    AgentState,
    AttachmentRef,
    Message,
    MessageKind,
    StatusSnapshot,
    validate_agent_id,
    validate_message,
)
from codeagent.mailbox.store import MailboxStore, resolve_root

__all__ = [
    "AgentState",
    "AttachmentRef",
    "BROADCAST_TO",
    "MailboxStore",
    "Message",
    "MessageKind",
    "StatusSnapshot",
    "resolve_root",
    "validate_agent_id",
    "validate_message",
]
