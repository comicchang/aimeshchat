"""Mailbox protocol — message types, validation, constants."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from codeagent.constants import LEASE_TIMEOUT_S

# ── Constants ──────────────────────────────────────────────────────────

# Protocol version 2 adds require_ack + RECEIPT(READ). v1 messages are
# still readable (protocol_version=1 / require_ack=False on load); all
# newly written messages carry PROTOCOL_VERSION.
PROTOCOL_VERSION = 2

VALID_KINDS = frozenset({"TASK", "REPORT", "PROGRESS", "EVIDENCE", "QUESTION", "RESPONSE", "NOTICE", "RECEIPT"})
VALID_STATES = frozenset({"IDLE", "BUSY", "DONE", "BLOCKED"})
REQUIRED_FIELDS = frozenset({"session_id", "from", "to", "subject", "body", "kind", "msg_id", "created_at"})
OPTIONAL_FIELDS = frozenset({"protocol_version", "require_ack", "receipt_type", "reply_to", "run_id", "request_id", "trace_id", "causation_id", "attachments"})
# Per-kind field requirements beyond the global REQUIRED_FIELDS.
# Keys are kind strings; values are sets of field names that MUST be present
# (and non-empty-string) when a message carries that kind.
KIND_CONDITIONAL_REQUIRED: dict[str, frozenset[str]] = {
    "TASK":    frozenset({"run_id", "request_id"}),
    "REPORT":  frozenset({"run_id", "request_id", "reply_to"}),
    # A RECEIPT must identify the message it acknowledges (reply_to), the
    # run/request it belongs to, and carry a receipt_type. require_ack is
    # forced False below — receipts never demand receipts (no loops).
    "RECEIPT": frozenset({"reply_to", "run_id", "request_id", "receipt_type"}),
}
AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,31}$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
# P1-2: request_id/run_id/reply_to are used verbatim as filesystem path
# components (RequestLedger._events_dir builds <agent>/events/<request_id>).
# A crafted value with path separators, ".." or glob metacharacters would
# write JSONL outside the agent's events tree. Restrict to a conservative
# safe charset (same family as AGENT_ID_RE, plus dots and longer ids).
PATH_SAFE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
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
    RECEIPT = "RECEIPT"


class ReceiptType(str, Enum):
    """Kinds of delivery/consumption receipts."""
    READ = "READ"


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
    protocol_version: int = PROTOCOL_VERSION
    require_ack: bool = False
    receipt_type: str = ""

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
            "protocol_version": self.protocol_version,
            "require_ack": self.require_ack,
        }
        if self.receipt_type:
            d["receipt_type"] = self.receipt_type
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
            # v1 messages (no protocol_version key) read back as v1 and are
            # never treated as requiring an ack — no guessing.
            protocol_version=d.get("protocol_version", 1),
            require_ack=bool(d.get("require_ack", False)),
            receipt_type=d.get("receipt_type", ""),
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


def validate_path_component(value: str, field_name: str) -> None:
    """Reject values unsafe to embed in a filesystem path component.

    Applies to request_id/run_id/reply_to, which the request ledger uses
    as directory names. Raises :class:`ValueError` on unsafe values so
    callers can defend even when a message bypassed ``validate_message``
    (P1-2 defense-in-depth). Exact ``.``/``..`` are rejected too — as a
    single path component they resolve to the events dir itself / its
    parent, escaping the per-request subdir.
    """
    if not isinstance(value, str) or value in (".", "..") or not PATH_SAFE_RE.match(value):
        raise ValueError(f"invalid {field_name}: {value!r}")


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
    # v2 protocol fields: strict typing, lenient on absence (v1 compat).
    pv = msg.get("protocol_version", 1)
    if not isinstance(pv, int) or isinstance(pv, bool) or pv < 1:
        return False, "protocol_version must be a positive int"
    if not isinstance(msg.get("require_ack", False), bool):
        return False, "require_ack must be bool"
    rt = msg.get("receipt_type", "")
    if not isinstance(rt, str):
        return False, "receipt_type must be string"
    if rt and rt not in {t.value for t in ReceiptType}:
        return False, f"invalid receipt_type: {rt}"
    if not msg["subject"].strip():
        return False, "subject must be non-empty"
    if not msg["body"].strip():
        return False, "body must be non-empty"
    # Kind-conditional required fields (after basic field validation)
    kind_extras = KIND_CONDITIONAL_REQUIRED.get(msg["kind"], frozenset())
    if kind_extras:
        missing_extras = kind_extras - set(msg.keys())
        if missing_extras:
            return False, f"kind {msg['kind']} requires fields: {', '.join(sorted(missing_extras))}"
        for ef in sorted(kind_extras):
            val = msg.get(ef, "")
            if not isinstance(val, str) or not val.strip():
                return False, f"kind {msg['kind']} requires non-empty {ef}"
    # Receipt loop prevention: receipts must never demand receipts, and a
    # READ receipt must name the message it acknowledges via reply_to.
    if msg["kind"] == "RECEIPT":
        if msg.get("require_ack", False):
            return False, "RECEIPT must not set require_ack (no receipt loops)"
        if msg.get("receipt_type", "") != ReceiptType.READ.value:
            return False, f"RECEIPT requires receipt_type={ReceiptType.READ.value!r}"
        if not msg.get("reply_to", "").strip():
            return False, "RECEIPT requires non-empty reply_to (acked msg_id)"
    if expected_session_id is not None and msg["session_id"] != expected_session_id:
        return False, f"session_id mismatch: {msg['session_id']} vs {expected_session_id}"
    if expected_agent is not None and msg["to"] != expected_agent and msg["to"] != BROADCAST_TO:
        return False, f"recipient mismatch: {msg['to']} vs {expected_agent}"
    if filename is not None and msg["msg_id"] + ".json" != filename:
        return False, f"msg_id mismatch: {msg['msg_id']} vs {filename}"
    if "/" in msg["msg_id"] or "\\" in msg["msg_id"]:
        return False, f"invalid msg_id (path separator): {msg['msg_id']}"
    # P1-2: request_id/run_id/reply_to become directory names in the
    # RequestLedger events tree — reject path separators, ".." and glob
    # metacharacters (PATH_SAFE_RE) so a crafted message cannot escape the
    # events root. Absent/empty values stay allowed (v1 compat); present
    # values must be path-safe. Exact "."/".." are also rejected: as a
    # single path component they resolve to the events dir / its parent.
    for field_name in ("request_id", "run_id", "reply_to"):
        val = msg.get(field_name, "")
        if val and not isinstance(val, str):
            return False, f"{field_name} must be string"
        if val and (val in (".", "..") or not PATH_SAFE_RE.match(val)):
            return False, f"invalid {field_name} (unsafe path component): {val!r}"
    if "attachments" in msg:
        atts = msg["attachments"]
        if not isinstance(atts, list):
            return False, "attachments must be a list"
        for att in atts:
            err = attachment_error(att)
            if err is not None:
                return False, f"invalid attachment: {err}"
    return True, ""
