"""JSONL wire protocol for communication with remote exec helpers.

Wire format: one JSON object per line (newline-delimited JSON).

Client → Remote:
    {"wire_version": 1, "command": "run", "task": "...", "workdir": "...", ...}

Remote → Client (lifecycle):
    {"type": "ready"}                          — helper started
    {"type": "accepted", "wire_version": 1}    — request accepted
    {"type": "session", "id": "..."}           — session ID (optional, may arrive after result)
    {"type": "result", "stdout": "...", "exit_code": 0, "stderr": "..."}
    {"type": "error", "message": "..."}        — unrecoverable error

Other messages:
    {"type": "pong", ...}                      — response to ping
    {"type": "capabilities", ...}              — response to capabilities
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from codeagent.constants import DEFAULT_EXEC_TIMEOUT, MAX_LINE_LENGTH

# v2 adds session_key/request_id/run_id/review_key/require_ack/capabilities
# round-trip on `run`, plus stream_kind ("mailbox"|"runtime"|"all") and
# runtime/request filters on `stream`.
WIRE_VERSION = 2

# ── message type constants ──────────────────────────────────────────────
MSG_READY = "ready"
MSG_ACCEPTED = "accepted"
MSG_SESSION = "session"
MSG_RESULT = "result"
MSG_ERROR = "error"
MSG_PONG = "pong"
MSG_CAPABILITIES = "capabilities"
MSG_MAILBOX_RESULT = "mailbox_result"
MSG_STREAM_EVENT = "stream_event"

TERMINAL_TYPES = frozenset({MSG_RESULT, MSG_ERROR, MSG_MAILBOX_RESULT})
LIFECYCLE_TYPES = frozenset({MSG_READY, MSG_ACCEPTED, MSG_SESSION, MSG_RESULT, MSG_ERROR, MSG_MAILBOX_RESULT})
STREAM_TYPES = frozenset({MSG_STREAM_EVENT})

# P1-3: one-shot transports must not report success when an exchange ends
# (EOF) without a terminal frame — the frame may have been dropped by the
# 1 MiB line guard or truncated mid-stream.  Shared diagnostic for the
# got_terminal invariant.
NO_TERMINAL_FRAME_MSG = (
    "wire error: no terminal frame (result/error/mailbox_result) received; "
    "the helper's response was lost or truncated"
)

# P1-4: consecutive unparseable frames beyond this count abort a one-shot
# parse loop instead of skipping garbage silently (which could hide the
# loss of the terminal frame).
MAX_CONSECUTIVE_BAD_FRAMES = 5

# ── command constants ───────────────────────────────────────────────────
CMD_RUN = "run"
CMD_PING = "ping"
CMD_CAPABILITIES = "capabilities"
CMD_MAILBOX = "mailbox"
CMD_STREAM = "stream"


@dataclass(frozen=True)
class WireMessage:
    """A single decoded wire-protocol message."""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)

    # ── convenience accessors for common fields ─────────────────────────

    @property
    def ok(self) -> bool:
        """True unless this is an error message."""
        return self.type != MSG_ERROR

    @property
    def message(self) -> str:
        """Error message, if present."""
        return self.payload.get("message", "")

    @property
    def session_id(self) -> str | None:
        """Session ID from a 'session' message."""
        return self.payload.get("id")

    @property
    def stdout(self) -> str:
        """stdout from a 'result' message."""
        return self.payload.get("stdout", "")

    @property
    def stderr(self) -> str:
        """stderr from a 'result' message."""
        return self.payload.get("stderr", "")

    @property
    def exit_code(self) -> int:
        """exit_code from a 'result' message (0 if absent)."""
        return self.payload.get("exit_code", 0)

    @property
    def wire_version(self) -> int:
        """Wire version from the payload, if present."""
        return self.payload.get("wire_version", 0)

    @property
    def request_id(self) -> str:
        """Request ID for stream correlation."""
        return self.payload.get("request_id", "")

    @property
    def cursor(self) -> str:
        """Cursor for resumable event delivery."""
        return self.payload.get("cursor", "")


# ── encode / decode ─────────────────────────────────────────────────────


def encode_line(obj: dict[str, Any]) -> bytes:
    """Serialize a dict to a JSONL line (bytes, newline-terminated)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


# ── required-field schema per response message type ────────────────────
_RESPONSE_REQUIRED: dict[str, dict[str, type]] = {
    MSG_ACCEPTED:      {"wire_version": int},
    # P1-4: wire_version is enforced under strict decoding.  One-shot
    # transports pass strict=False so a v1 remote's bare {"type":"ready"}
    # decodes with version 0 and the caller's version check reports a
    # mismatch instead of the frame being silently skipped.
    MSG_READY:         {"wire_version": int},
    MSG_SESSION:       {"id": str},
    MSG_RESULT:        {"exit_code": int, "stdout": str, "stderr": str},
    MSG_ERROR:         {"message": str},
    MSG_PONG:          {"wire_version": int},
    MSG_CAPABILITIES:  {},  # no strictly required fields beyond type
    MSG_MAILBOX_RESULT: {"exit_code": int, "stdout": str, "stderr": str},
    MSG_STREAM_EVENT:  {"request_id": str, "session_id": str, "cursor": str, "payload": dict},
}


def decode_line(line: str | bytes, *, strict: bool = True) -> WireMessage:
    """Parse a JSONL line into a validated ``WireMessage``.

    Args:
        strict: when True (default), the full required-field schema is
            enforced, including ``wire_version`` on ready frames.  When
            False (one-shot transports), a ``ready`` frame missing
            ``wire_version`` is tolerated and decoded with version 0 —
            v1 remotes send a bare ``{"type":"ready"}`` and the frame
            must reach the caller's version check so a mismatch is
            reported instead of the frame being silently skipped.

    Raises:
        ValueError: on empty input, non-dict JSON, missing ``type``,
            lines exceeding ``MAX_LINE_LENGTH``, or wrong field types
            for the declared message type.
    """
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    line = line.strip()
    if not line:
        raise ValueError("empty wire line")
    if len(line) > MAX_LINE_LENGTH:
        raise ValueError(f"wire line exceeds {MAX_LINE_LENGTH} bytes")
    try:
        obj: dict[str, Any] = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError(f"wire message must be a JSON object, got {type(obj).__name__}")

    msg_type = obj.pop("type", None)
    if not isinstance(msg_type, str) or not msg_type:
        raise ValueError("wire message 'type' must be a non-empty string")

    # Validate required fields and their types for known message types.
    schema = _RESPONSE_REQUIRED.get(msg_type)
    if schema is not None:
        for field, expected_type in schema.items():
            if field not in obj:
                # P1-4: lenient mode — a v1 ready frame has no
                # wire_version; decode it as 0 so the caller's version
                # check reports a mismatch instead of dropping the frame.
                if not strict and msg_type == MSG_READY and field == "wire_version":
                    obj[field] = 0
                    continue
                raise ValueError(f"{msg_type!r} message missing required field {field!r}")
            if not isinstance(obj[field], expected_type):
                raise ValueError(
                    f"{msg_type!r} message field {field!r} must be {expected_type.__name__}, "
                    f"got {type(obj[field]).__name__}"
                )
    return WireMessage(type=msg_type, payload=obj)


# ── request encoding / decoding ─────────────────────────────────────────

_REQUEST_REQUIRED: dict[str, dict[str, type]] = {
    CMD_RUN:          {"task": str, "workdir": str, "timeout": int},
    CMD_PING:         {},
    CMD_CAPABILITIES: {},
    CMD_MAILBOX:      {"args": list},
    CMD_STREAM:       {"session_id": str, "cursor": str},
}


def encode_request(command: str, **kwargs: Any) -> str:
    """Validate and encode a request as a JSONL string.

    Raises:
        ValueError: on unknown command, missing required fields, or
            wrong field types.
    """
    if not isinstance(command, str) or not command:
        raise ValueError("command must be a non-empty string")
    schema = _REQUEST_REQUIRED.get(command)
    if schema is None:
        raise ValueError(f"unknown command: {command}")
    for field, expected_type in schema.items():
        if field not in kwargs:
            raise ValueError(f"command {command!r} requires field {field!r}")
        if not isinstance(kwargs[field], expected_type):
            raise ValueError(
                f"command {command!r} field {field!r} must be {expected_type.__name__}, "
                f"got {type(kwargs[field]).__name__}"
            )
    obj: dict[str, Any] = {"wire_version": WIRE_VERSION, "command": command, **kwargs}
    # P3-m: append newline to produce a valid JSONL line (str, not bytes).
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"


def decode_request(line: str | bytes) -> dict[str, Any]:
    """Parse and validate a request line (client → remote).

    Returns the parsed dict (with ``command`` present).  Raises
    ``ValueError`` on any validation failure.
    """
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    line = line.strip()
    if not line:
        raise ValueError("empty wire line")
    if len(line) > MAX_LINE_LENGTH:
        raise ValueError(f"wire line exceeds {MAX_LINE_LENGTH} bytes")
    try:
        obj: dict[str, Any] = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError(f"wire request must be a JSON object, got {type(obj).__name__}")

    cmd = obj.get("command")
    if not isinstance(cmd, str) or not cmd:
        raise ValueError("request 'command' must be a non-empty string")
    schema = _REQUEST_REQUIRED.get(cmd)
    if schema is None:
        raise ValueError(f"unknown command: {cmd}")
    for field, expected_type in schema.items():
        if field not in obj:
            raise ValueError(f"command {cmd!r} missing required field {field!r}")
        if not isinstance(obj[field], expected_type):
            raise ValueError(
                f"command {cmd!r} field {field!r} must be {expected_type.__name__}, "
                f"got {type(obj[field]).__name__}"
            )
    return obj


# ── message factories (client side) ─────────────────────────────────────


def make_request(
    *,
    command: str = CMD_RUN,
    task: str = "",
    workdir: str = "",
    backend: str = "",
    agent: str | None = None,
    model: str | None = None,
    skills: str | None = None,
    session_id: str | None = None,
    skip_permissions: bool = True,
    timeout: int = DEFAULT_EXEC_TIMEOUT,
    wire_version: int = WIRE_VERSION,
    session_key: str | None = None,
    request_id: str = "",
    run_id: str = "",
    review_key: str = "",
    require_ack: bool = False,
    capabilities: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    """Build a request dict to send to the remote helper.

    v2: ``session_key``/``request_id``/``run_id``/``review_key``/
    ``require_ack``/``capabilities`` round-trip verbatim to the remote so
    Manager-side correlation is preserved (session_key must NOT collapse
    to None on the wire).
    """
    req: dict[str, Any] = {
        "wire_version": wire_version,
        "command": command,
    }
    if command == CMD_RUN:
        req["task"] = task
        req["workdir"] = workdir
        if backend:
            req["backend"] = backend
        if agent:
            req["agent"] = agent
        if model:
            req["model"] = model
        if skills:
            req["skills"] = skills
        if session_id:
            req["resume_session_id"] = session_id
        req["skip_permissions"] = skip_permissions
        req["timeout"] = timeout
        # v2 correlation fields — always present so the remote never
        # guesses about them.
        req["session_key"] = session_key or ""
        req["request_id"] = request_id
        req["run_id"] = run_id
        req["review_key"] = review_key
        req["require_ack"] = require_ack
        if capabilities:
            req["capabilities"] = list(capabilities)
    return req


def make_stream_request(
    *,
    session_id: str,
    cursor: str = "0",
    timeout: int = DEFAULT_EXEC_TIMEOUT,
    request_id: str = "",
    wire_version: int = WIRE_VERSION,
    stream_kind: str = "mailbox",
    runtime_id: str = "",
    agent_id: str = "",
) -> dict[str, Any]:
    """Build a stream subscription request.

    ``cursor`` is an opaque resume token — pass ``"0"`` for a fresh
    subscription or the last received cursor to resume after reconnect.
    ``request_id`` correlates the response; auto-generated if empty.

    v2: ``stream_kind`` selects the event source — ``"mailbox"`` (inbox
    cursor), ``"runtime"`` (local Gateway EventStore), or ``"all"``
    (composite base64url cursor). ``runtime_id``/``agent_id`` filter the
    runtime/request scope.
    """
    import uuid
    req: dict[str, Any] = {
        "wire_version": wire_version,
        "command": CMD_STREAM,
        "session_id": session_id,
        "cursor": cursor,
        "timeout": timeout,
        "request_id": request_id or uuid.uuid4().hex[:12],
        "stream_kind": stream_kind,
    }
    if runtime_id:
        req["runtime_id"] = runtime_id
    if agent_id:
        req["agent_id"] = agent_id
    return req


def make_ping() -> dict[str, Any]:
    """Build a ping request."""
    return {"wire_version": WIRE_VERSION, "command": CMD_PING}


def make_capabilities_request() -> dict[str, Any]:
    """Build a capabilities request."""
    return {"wire_version": WIRE_VERSION, "command": CMD_CAPABILITIES}

def make_mailbox_request(
    *,
    args: list[str],
    mailbox_root: str = "",
    wire_version: int = WIRE_VERSION,
) -> dict[str, Any]:
    """Build a mailbox wire request."""
    req: dict[str, Any] = {
        "wire_version": wire_version,
        "command": CMD_MAILBOX,
        "args": args,
    }
    if mailbox_root:
        req["mailbox_root"] = mailbox_root
    return req


# ── message factories (remote side) ─────────────────────────────────────


def make_ready(*, wire_version: int = WIRE_VERSION, package_version: str | None = None) -> dict[str, Any]:
    if package_version is None:
        from codeagent import __version__
        package_version = __version__
    return {"type": MSG_READY, "wire_version": wire_version, "package_version": package_version}


def make_accepted(*, wire_version: int = WIRE_VERSION) -> dict[str, Any]:
    return {"type": MSG_ACCEPTED, "wire_version": wire_version}


def make_session(session_id: str) -> dict[str, Any]:
    return {"type": MSG_SESSION, "id": session_id}


def make_result(*, stdout: str = "", stderr: str = "", exit_code: int = 0) -> dict[str, Any]:
    return {"type": MSG_RESULT, "stdout": stdout, "stderr": stderr, "exit_code": exit_code}


def make_error(message: str) -> dict[str, Any]:
    return {"type": MSG_ERROR, "message": message}

def make_mailbox_result(
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
) -> dict[str, Any]:
    """Build a mailbox_result response."""
    return {"type": MSG_MAILBOX_RESULT, "stdout": stdout, "stderr": stderr, "exit_code": exit_code}


def make_pong(*, wire_version: int = WIRE_VERSION, hostname: str = "", capabilities: list[str] | None = None) -> dict[str, Any]:
    obj: dict[str, Any] = {"type": MSG_PONG, "wire_version": wire_version}
    if hostname:
        obj["hostname"] = hostname
    if capabilities is not None:
        obj["capabilities"] = capabilities
    return obj


def make_capabilities(*, wire_version: int = WIRE_VERSION, backends: list[str] | None = None, features: list[str] | None = None) -> dict[str, Any]:
    obj: dict[str, Any] = {"type": MSG_CAPABILITIES, "wire_version": wire_version}
    if backends is not None:
        obj["backends"] = backends
    if features is not None:
        obj["features"] = features
    return obj


def make_stream_event(
    *,
    request_id: str,
    session_id: str,
    cursor: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Build a stream_event push frame.

    ``cursor`` is an opaque token the client sends back on reconnect to
    resume delivery without message loss.
    ``payload`` carries the event body (e.g. a mailbox message summary).
    """
    return {
        "type": MSG_STREAM_EVENT,
        "request_id": request_id,
        "session_id": session_id,
        "cursor": cursor,
        "payload": payload,
    }


# ── composite stream cursor (stream_kind="all") ────────────────────────

import base64
import json as _json

COMPOSITE_CURSOR_VERSION = 2


def make_composite_cursor(mailbox_cursor: str, runtime_event_id: int = 0) -> str:
    """Encode a composite (mailbox + runtime) cursor as base64url JSON.

    The CLIENT treats this as opaque — it only stores and returns it.
    Only the server parses the inner fields.
    """
    obj = {
        "v": COMPOSITE_CURSOR_VERSION,
        "mailbox": mailbox_cursor or "0",
        "runtime": int(runtime_event_id or 0),
    }
    raw = _json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def split_composite_cursor(cursor: str) -> tuple[str, int]:
    """Parse a composite cursor into (mailbox_cursor, runtime_event_id).

    Tries the composite format first; anything that does not decode to a
    v2 composite object (e.g. legacy "epoch/seq" mailbox cursors) is
    treated as a plain mailbox cursor.
    """
    if not cursor or not isinstance(cursor, str):
        return "0", 0
    try:
        padded = cursor.encode("ascii") + b"=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded)
        obj = _json.loads(raw.decode("utf-8"))
        if obj.get("v") == COMPOSITE_CURSOR_VERSION:
            return str(obj.get("mailbox", "0")), int(obj.get("runtime", 0))
    except Exception:
        # P3-n: warn on parse failure instead of silent degradation
        logging.getLogger(__name__).warning(
            "split_composite_cursor: failed to parse composite cursor %r, "
            "falling back to plain mailbox cursor", cursor[:64]
        )
    return cursor, 0
