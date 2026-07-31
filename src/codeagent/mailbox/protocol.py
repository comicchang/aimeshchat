"""Mailbox protocol — message types, validation, constants."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from codeagent.constants import LEASE_TIMEOUT_S

# ── Constants ──────────────────────────────────────────────────────────

VALID_KINDS = frozenset({"TASK", "REPORT", "PROGRESS", "EVIDENCE", "QUESTION", "RESPONSE", "NOTICE"})
VALID_STATES = frozenset({"IDLE", "BUSY", "DONE", "BLOCKED"})
REQUIRED_FIELDS = frozenset({"session_id", "from", "to", "subject", "body", "kind", "msg_id", "created_at"})
OPTIONAL_FIELDS = frozenset({"reply_to", "run_id", "request_id"})
AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,31}$")


class MessageKind(str, Enum):
    TASK = "TASK"
    REPORT = "REPORT"
    PROGRESS = "PROGRESS"
    EVIDENCE = "EVIDENCE"
    QUESTION = "QUESTION"
    RESPONSE = "RESPONSE"
    NOTICE = "NOTICE"


class AgentState(str, Enum):
    IDLE = "IDLE"
    BUSY = "BUSY"
    DONE = "DONE"
    BLOCKED = "BLOCKED"


@dataclass
class Message:
    session_id: str
    from_id: str
    to_id: str
    subject: str
    body: str
    kind: str
    msg_id: str
    created_at: str
    reply_to: str = ""
    run_id: str = ""
    request_id: str = ""

    def to_dict(self) -> dict:
        d = {
            "session_id": self.session_id,
            "from": self.from_id,
            "to": self.to_id,
            "subject": self.subject,
            "body": self.body,
            "kind": self.kind,
            "msg_id": self.msg_id,
            "created_at": self.created_at,
        }
        if self.reply_to:
            d["reply_to"] = self.reply_to
        if self.run_id:
            d["run_id"] = self.run_id
        if self.request_id:
            d["request_id"] = self.request_id
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Message:
        return cls(
            session_id=d["session_id"],
            from_id=d["from"],
            to_id=d["to"],
            subject=d["subject"],
            body=d["body"],
            kind=d["kind"],
            msg_id=d["msg_id"],
            created_at=d["created_at"],
            reply_to=d.get("reply_to", ""),
            run_id=d.get("run_id", ""),
            request_id=d.get("request_id", ""),
        )


@dataclass
class StatusSnapshot:
    session_id: str
    state: str
    current_task: str = ""
    last_conclusion: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "state": self.state,
            "current_task": self.current_task,
            "last_conclusion": self.last_conclusion,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> StatusSnapshot:
        return cls(
            session_id=d.get("session_id", ""),
            state=d.get("state", ""),
            current_task=d.get("current_task", ""),
            last_conclusion=d.get("last_conclusion", ""),
            updated_at=d.get("updated_at", ""),
        )


# ── Validation ─────────────────────────────────────────────────────────

def validate_agent_id(aid: str) -> None:
    if not AGENT_ID_RE.match(aid):
        raise ValueError(f"invalid agent id: {aid!r}")


def validate_message(msg: dict, expected_session_id: Optional[str] = None,
                     expected_agent: Optional[str] = None, filename: Optional[str] = None) -> tuple[bool, str]:
    """Full schema validation of message dict.

    Args:
        expected_session_id: if set, verify msg.session_id matches
        expected_agent: if set, verify msg.to matches (recipient check)
        filename: if set, verify msg_id + '.json' == filename (integrity check)
    """
    if not isinstance(msg, dict):
        return False, "not a JSON object"
    missing = REQUIRED_FIELDS - set(msg.keys())
    if missing:
        return False, f"missing fields: {', '.join(sorted(missing))}"
    if msg["kind"] not in VALID_KINDS:
        return False, f"invalid kind: {msg['kind']}"
    for field_name in ("subject", "body", "session_id", "from", "to", "msg_id", "created_at"):
        if not isinstance(msg.get(field_name), str):
            return False, f"{field_name} must be string"
    if not msg["subject"].strip():
        return False, "subject must be non-empty"
    if not msg["body"].strip():
        return False, "body must be non-empty"
    if expected_session_id is not None and msg["session_id"] != expected_session_id:
        return False, f"session_id mismatch: {msg['session_id']} vs {expected_session_id}"
    if expected_agent is not None and msg["to"] != expected_agent:
        return False, f"recipient mismatch: {msg['to']} vs {expected_agent}"
    if filename is not None and msg["msg_id"] + ".json" != filename:
        return False, f"msg_id mismatch: {msg['msg_id']} vs {filename}"
    if "/" in msg["msg_id"] or "\\" in msg["msg_id"]:
        return False, f"invalid msg_id (path separator): {msg['msg_id']}"
    return True, ""
