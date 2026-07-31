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
from dataclasses import dataclass, field
from typing import Any

WIRE_VERSION = 1
MAX_LINE_LENGTH = 1_048_576  # 1 MiB — reject absurdly large lines

# ── message type constants ──────────────────────────────────────────────
MSG_READY = "ready"
MSG_ACCEPTED = "accepted"
MSG_SESSION = "session"
MSG_RESULT = "result"
MSG_ERROR = "error"
MSG_PONG = "pong"
MSG_CAPABILITIES = "capabilities"
MSG_MAILBOX_RESULT = "mailbox_result"

TERMINAL_TYPES = frozenset({MSG_RESULT, MSG_ERROR, MSG_MAILBOX_RESULT})
LIFECYCLE_TYPES = frozenset({MSG_READY, MSG_ACCEPTED, MSG_SESSION, MSG_RESULT, MSG_ERROR, MSG_MAILBOX_RESULT})

# ── command constants ───────────────────────────────────────────────────
CMD_RUN = "run"
CMD_PING = "ping"
CMD_CAPABILITIES = "capabilities"
CMD_MAILBOX = "mailbox"


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


# ── encode / decode ─────────────────────────────────────────────────────


def encode_line(obj: dict[str, Any]) -> bytes:
    """Serialize a dict to a JSONL line (bytes, newline-terminated)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


# ── required-field schema per response message type ────────────────────
_RESPONSE_REQUIRED: dict[str, dict[str, type]] = {
    MSG_ACCEPTED:     {"wire_version": int},
    MSG_READY:        {"wire_version": int},
    MSG_SESSION:      {"id": str},
    MSG_RESULT:       {"exit_code": int, "stdout": str, "stderr": str},
    MSG_ERROR:        {"message": str},
    MSG_PONG:         {"wire_version": int},
    MSG_CAPABILITIES: {},  # no strictly required fields beyond type
}


def decode_line(line: str | bytes) -> WireMessage:
    """Parse a JSONL line into a validated ``WireMessage``.

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
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


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
    timeout: int = 600,
    wire_version: int = WIRE_VERSION,
) -> dict[str, Any]:
    """Build a request dict to send to the remote helper."""
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
