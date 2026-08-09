"""Runtime adapters + supervisor — Agent lifecycle detached from the call shell."""
from codeagent.runtime.supervisor import (
    MARKER_AGENT_EXITED,
    MARKER_AGENT_STARTED,
    MARKER_CWD_VERIFIED,
    MARKER_SHELL_READY,
    RuntimeHealth,
    RuntimeSpec,
    probe_runtime,
    spawn_runtime,
    stop_runtime,
)

__all__ = [
    "MARKER_AGENT_EXITED",
    "MARKER_AGENT_STARTED",
    "MARKER_CWD_VERIFIED",
    "MARKER_SHELL_READY",
    "RuntimeHealth",
    "RuntimeSpec",
    "probe_runtime",
    "spawn_runtime",
    "stop_runtime",
]
