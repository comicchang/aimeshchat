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
OPTIONAL_FIELDS = frozenset({"reply_to", "run_id", "request_id", "attachments"})
AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,31}$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
# "*" is a reserved recipient meaning broadcast to every roster member except the sender.
BROADCAST_TO = "*"
DEFAULT_MEDIA_TYPE = "application/octet-stream"
ATTACHMENT_FIELDS = frozenset({"artifact_id", "source_host", "remote_root", "relative_path", "size", "sha256"})


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


@dataclass(frozen=True)
class AttachmentRef:
    """Reference to an artifact attached to a message.

    The payload itself lives on ``source_host`` under ``remote_root``;
    ``relative_path`` locates it within that root. ``sha256`` is the digest
    of the artifact content, used by consumers to verify the pull.
    """

    artifact_id: str
    source_host: str
    remote_root: str
    relative_path: str
    size: int
    sha256: str
    media_type: str = DEFAULT_MEDIA_TYPE

    def to_dict(self) -> dict:
        return {
            "artifact_id": self.artifact_id,
            "source_host": self.source_host,
            "remote_root": self.remote_root,
            "relative_path": self.relative_path,
            "size": self.size,
            "sha256": self.sha256,
            "media_type": self.media_type,
        }

    @classmethod
    def from_dict(cls, d: dict) -> AttachmentRef:
        return cls(
            artifact_id=d["artifact_id"],
            source_host=d["source_host"],
            remote_root=d["remote_root"],
            relative_path=d["relative_path"],
            size=d["size"],
            sha256=d["sha256"],
            media_type=d.get("media_type", DEFAULT_MEDIA_TYPE),
        )


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
    trace_id: str = ""
    causation_id: str = ""
    attachments: list[AttachmentRef] = field(default_factory=list)

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
        if self.trace_id:
            d["trace_id"] = self.trace_id
        if self.causation_id:
            d["causation_id"] = self.causation_id
        if self.attachments:
            d["attachments"] = [a.to_dict() for a in self.attachments]
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
            trace_id=d.get("trace_id", ""),
            causation_id=d.get("causation_id", ""),
            attachments=[AttachmentRef.from_dict(a) for a in d.get("attachments", []) if isinstance(a, dict)],
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


def attachment_error(att: dict) -> Optional[str]:
    """Return a human-readable reason if an attachment ref dict is invalid, else None."""
    if not isinstance(att, dict):
        return "attachment must be an object"
    for key in ("artifact_id", "source_host", "remote_root", "relative_path"):
        v = att.get(key)
        if not isinstance(v, str) or not v.strip():
            return f"attachment {key} must be a non-empty string"
    size = att.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return "attachment size must be a non-negative integer"
    sha = att.get("sha256")
    if not isinstance(sha, str) or not SHA256_RE.match(sha):
        return "attachment sha256 must be a 64-char hex digest"
    rp = att.get("relative_path")
    if rp.startswith("/") or "\\" in rp or ".." in rp:
        return "attachment relative_path must be a safe relative path"
    mt = att.get("media_type", DEFAULT_MEDIA_TYPE)
    if not isinstance(mt, str) or not mt.strip():
        return "attachment media_type must be a non-empty string"
    return None


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
    if expected_agent is not None and msg["to"] != expected_agent and msg["to"] != BROADCAST_TO:
        return False, f"recipient mismatch: {msg['to']} vs {expected_agent}"
    if filename is not None and msg["msg_id"] + ".json" != filename:
        return False, f"msg_id mismatch: {msg['msg_id']} vs {filename}"
    if "/" in msg["msg_id"] or "\\" in msg["msg_id"]:
        return False, f"invalid msg_id (path separator): {msg['msg_id']}"
    if "attachments" in msg:
        atts = msg["attachments"]
        if not isinstance(atts, list):
            return False, "attachments must be a list"
        for att in atts:
            err = attachment_error(att)
            if err is not None:
                return False, f"invalid attachment: {err}"
    return True, ""
