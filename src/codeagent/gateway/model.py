"""Gateway wire models — NDJSON request/response + runtime event types.

The gateway is a per-device local control plane (UDS). Frames are one
JSON object per line; a single connection carries exactly one request
and one response. Producers submit RuntimeEventDraft; the persisted form
is RuntimeEvent (source_sequence assigned by the EventStore).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

GATEWAY_PROTOCOL_VERSION = 1
MAX_FRAME_LENGTH = 1_048_576  # 1 MiB — matches wire MAX_LINE_LENGTH

# Fixed event kinds (UI / stats / reconnect backfill).
EVENT_KINDS = frozenset({
    "RUNTIME_STATE", "MESSAGE_DELIVERED", "MESSAGE_READ", "TURN_STARTED",
    "ASSISTANT_PROGRESS", "TOOL_STARTED", "TOOL_UPDATED", "TOOL_FINISHED",
    "USAGE", "TASK_STATE", "ERROR", "AGENT_STATUS",
})
# Tool update detail kinds — pruned by gateway sweep after 7 days.
EVENT_KIND_TOOL_UPDATE = frozenset({"TOOL_STARTED", "TOOL_UPDATED", "TOOL_FINISHED"})
# Kinds retained after runtime release.
EVENT_KIND_TERMINAL = frozenset({"TASK_STATE", "MESSAGE_READ", "ERROR"})


class GatewayError(Exception):
    """Structured gateway error — serialized as {code, message, context}."""

    def __init__(self, code: str, message: str, context: Optional[dict] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.context = context or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "context": self.context}


# Standard error codes (fail-closed).
ERR_NOT_AUTHORIZED = "NOT_AUTHORIZED"          # identity missing / not in roster
ERR_OWNER_MISMATCH = "OWNER_MISMATCH"          # owner_pid+nonce+generation mismatch
ERR_VERSION_INCOMPATIBLE = "VERSION_INCOMPATIBLE"
ERR_GENERATION_STALE = "GENERATION_STALE"      # runtime generation expired
ERR_UNSUPPORTED_RUNTIME = "UNSUPPORTED_RUNTIME"
ERR_UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
ERR_REMOTE_UPGRADE_REQUIRED = "REMOTE_UPGRADE_REQUIRED"
ERR_FRAME_TOO_LARGE = "FRAME_TOO_LARGE"
ERR_PROTOCOL = "PROTOCOL"                      # malformed frame / unknown method
ERR_NOT_FOUND = "NOT_FOUND"
ERR_PROTOCOL_CONFLICT = "PROTOCOL_CONFLICT"    # cross-device write SHA conflict


@dataclass(frozen=True)
class GatewayRequest:
    v: int
    id: str
    method: str
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "GatewayRequest":
        if not isinstance(d, dict):
            raise GatewayError(ERR_PROTOCOL, "request must be a JSON object")
        v = d.get("v")
        rid = d.get("id")
        method = d.get("method")
        if not isinstance(v, int) or isinstance(v, bool) or v < 1:
            raise GatewayError(ERR_PROTOCOL, "request v must be a positive int")
        if not isinstance(rid, str) or not rid:
            raise GatewayError(ERR_PROTOCOL, "request id must be a non-empty string")
        if not isinstance(method, str) or not method:
            raise GatewayError(ERR_PROTOCOL, "request method must be a non-empty string")
        params = d.get("params", {})
        if not isinstance(params, dict):
            raise GatewayError(ERR_PROTOCOL, "request params must be an object")
        return cls(v=v, id=rid, method=method, params=params)

    def to_json(self) -> str:
        return json.dumps({
            "v": self.v, "id": self.id, "method": self.method, "params": self.params,
        }, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class GatewayResponse:
    v: int
    id: str
    ok: bool
    result: dict[str, Any] = field(default_factory=dict)
    error: Optional[dict] = None

    def to_json(self) -> str:
        return json.dumps({
            "v": self.v, "id": self.id, "ok": self.ok,
            "result": self.result, "error": self.error,
        }, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def ok_response(cls, req: GatewayRequest, result: dict) -> "GatewayResponse":
        return cls(v=req.v, id=req.id, ok=True, result=result)

    @classmethod
    def error_response(cls, req: GatewayRequest, err: GatewayError) -> "GatewayResponse":
        return cls(v=req.v, id=req.id, ok=False, error=err.to_dict())

    @classmethod
    def parse(cls, line: str) -> "GatewayResponse":
        d = json.loads(line)
        return cls(
            v=d.get("v", 0), id=d.get("id", ""), ok=bool(d.get("ok")),
            result=d.get("result", {}) or {}, error=d.get("error"),
        )


@dataclass(frozen=True)
class RuntimeEventDraft:
    """Producer-submitted event — host/sequence are assigned by the store.

    Producers may submit host/sequence but the Gateway ignores them
    (``source_host``/``source_sequence`` come from the local EventStore
    append or from a remote ingest frame).
    """

    runtime_id: str
    generation: int
    session_id: str
    agent_id: str
    request_id: str
    run_id: str
    kind: str
    created_at: str
    payload: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.runtime_id or not self.session_id:
            raise GatewayError(ERR_PROTOCOL, "runtime event requires runtime_id + session_id")
        if self.kind not in EVENT_KINDS:
            raise GatewayError(
                ERR_PROTOCOL,
                f"invalid event kind {self.kind!r}; expected one of {sorted(EVENT_KINDS)}",
            )
        if not isinstance(self.payload, dict):
            raise GatewayError(ERR_PROTOCOL, "runtime event payload must be an object")

    def to_dict(self) -> dict:
        return {
            "runtime_id": self.runtime_id,
            "generation": self.generation,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "request_id": self.request_id,
            "run_id": self.run_id,
            "kind": self.kind,
            "created_at": self.created_at,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RuntimeEventDraft":
        return cls(
            runtime_id=d.get("runtime_id", ""),
            generation=int(d.get("generation", 0)),
            session_id=d.get("session_id", ""),
            agent_id=d.get("agent_id", ""),
            request_id=d.get("request_id", ""),
            run_id=d.get("run_id", ""),
            kind=d.get("kind", ""),
            created_at=d.get("created_at", ""),
            payload=d.get("payload", {}) or {},
        )


@dataclass(frozen=True)
class RuntimeEvent:
    """Persisted event — local monotonic event_id + per-source sequence."""

    event_id: int
    source_host: str
    runtime_id: str
    generation: int
    source_sequence: int
    session_id: str
    agent_id: str
    request_id: str
    run_id: str
    kind: str
    created_at: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "source_host": self.source_host,
            "runtime_id": self.runtime_id,
            "generation": self.generation,
            "source_sequence": self.source_sequence,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "request_id": self.request_id,
            "run_id": self.run_id,
            "kind": self.kind,
            "created_at": self.created_at,
            "payload": self.payload,
        }
