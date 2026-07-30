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

# ── message type constants ──────────────────────────────────────────────
MSG_READY = "ready"
MSG_ACCEPTED = "accepted"
MSG_SESSION = "session"
MSG_RESULT = "result"
MSG_ERROR = "error"
MSG_PONG = "pong"
MSG_CAPABILITIES = "capabilities"

TERMINAL_TYPES = frozenset({MSG_RESULT, MSG_ERROR})
LIFECYCLE_TYPES = frozenset({MSG_READY, MSG_ACCEPTED, MSG_SESSION, MSG_RESULT, MSG_ERROR})

# ── command constants ───────────────────────────────────────────────────
CMD_RUN = "run"
CMD_PING = "ping"
CMD_CAPABILITIES = "capabilities"


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


def decode_line(line: str | bytes) -> WireMessage:
    """Parse a JSONL line into a WireMessage.

    Raises:
        ValueError: if the line is not valid JSON or has no ``type`` key.
    """
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    line = line.strip()
    if not line:
        raise ValueError("empty wire line")
    try:
        obj: dict[str, Any] = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc

    msg_type = obj.pop("type", None)
    if not msg_type:
        raise ValueError("wire message has no 'type' field")
    return WireMessage(type=msg_type, payload=obj)


# ── message factories (client side) ─────────────────────────────────────


def make_request(
    *,
    command: str = CMD_RUN,
    task: str = "",
    workdir: str = "",
    backend: str = "",
    agent: str | None = None,
    model: str | None = None,
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


# ── message factories (remote side) ─────────────────────────────────────


def make_ready(*, wire_version: int = WIRE_VERSION, package_version: str = "0.1.0") -> dict[str, Any]:
    return {"type": MSG_READY, "wire_version": wire_version, "package_version": package_version}


def make_accepted(*, wire_version: int = WIRE_VERSION) -> dict[str, Any]:
    return {"type": MSG_ACCEPTED, "wire_version": wire_version}


def make_session(session_id: str) -> dict[str, Any]:
    return {"type": MSG_SESSION, "id": session_id}


def make_result(*, stdout: str = "", stderr: str = "", exit_code: int = 0) -> dict[str, Any]:
    return {"type": MSG_RESULT, "stdout": stdout, "stderr": stderr, "exit_code": exit_code}


def make_error(message: str) -> dict[str, Any]:
    return {"type": MSG_ERROR, "message": message}


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
