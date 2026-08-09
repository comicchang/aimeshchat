"""Runtime adapter contracts — Agent lifecycle detached from the call shell.

A RuntimeAdapter owns the lifecycle of one Agent runtime:
  - spawn(request, context) -> RuntimeHandle
  - send(handle, message)
  - subscribe(handle, cursor) -> Iterator[RuntimeEvent]
  - probe(handle) -> RuntimeHealth
  - resume(handle, prompt) -> RuntimeHandle
  - stop(handle, reason)

Handles carry runtime_id/runtime/backend_session_id/host_alias/generation/
capabilities/supervisor/mode. Capability names are fixed:
stream_events / in_loop_messages / tool_stats / native_ui / hot_resume /
warm_resume. Adapters must NEVER claim a capability they do not provide.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

# Fixed capability names.
CAP_STREAM_EVENTS = "stream_events"
CAP_IN_LOOP_MESSAGES = "in_loop_messages"
CAP_TOOL_STATS = "tool_stats"
CAP_NATIVE_UI = "native_ui"
CAP_HOT_RESUME = "hot_resume"
CAP_WARM_RESUME = "warm_resume"
ALL_CAPABILITIES = frozenset({
    CAP_STREAM_EVENTS, CAP_IN_LOOP_MESSAGES, CAP_TOOL_STATS, CAP_NATIVE_UI,
    CAP_HOT_RESUME, CAP_WARM_RESUME,
})

# Runtime names.
RUNTIME_OMP = "omp"
RUNTIME_OPENCODE = "opencode"
RUNTIME_GENERIC = "generic"

# Error codes.
UNSUPPORTED_RUNTIME = "UNSUPPORTED_RUNTIME"
UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"


class RuntimeErrorCode(Exception):
    """Structured runtime error (fail-closed capability selection)."""

    def __init__(self, code: str, message: str, context: Optional[dict] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.context = context or {}


@dataclass(frozen=True)
class RuntimeHandle:
    """Opaque handle to a supervised Agent runtime."""

    runtime_id: str
    runtime: str
    backend_session_id: str = ""
    host_alias: str = "__local__"
    generation: int = 1
    capabilities: frozenset[str] = frozenset()
    supervisor: str = ""  # "tmux" | "process" | "none"
    mode: str = "interactive_plugin"  # interactive_plugin | short_task | warm | cold
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "runtime_id": self.runtime_id,
            "runtime": self.runtime,
            "backend_session_id": self.backend_session_id,
            "host_alias": self.host_alias,
            "generation": self.generation,
            "capabilities": sorted(self.capabilities),
            "supervisor": self.supervisor,
            "mode": self.mode,
        }


class RuntimeAdapter:
    """Base class — subclasses implement the lifecycle contract."""

    name = "base"

    def spawn(self, request: dict, context: Optional[dict] = None) -> RuntimeHandle:
        raise NotImplementedError

    def send(self, handle: RuntimeHandle, message: dict) -> dict:
        raise NotImplementedError

    def subscribe(self, handle: RuntimeHandle, cursor: str = "") -> Iterator[Any]:
        raise NotImplementedError

    def probe(self, handle: RuntimeHandle) -> dict:
        raise NotImplementedError

    def resume(self, handle: RuntimeHandle, prompt: str) -> RuntimeHandle:
        raise NotImplementedError

    def stop(self, handle: RuntimeHandle, reason: str) -> None:
        raise NotImplementedError
