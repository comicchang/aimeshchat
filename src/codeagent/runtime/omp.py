"""OMPRuntimeAdapter — full-capability OMP runtime via tmux supervisor + plugin.

Default mode ``interactive_plugin``: a new interactive tmux pane runs
``omp --cwd <workdir> [profile args]`` (NO ``-c``, NO ``--print``) under
the supervisor. The plugin handshake registers the runtime and receives
the initial task; the same OMP process can accept steer/followUp and
park hot. All six capabilities are declared ONLY after the handshake.

Explicit ``short_task=true`` uses the bounded OMPRunner ``--print`` path;
that handle declares only stream_events/warm_resume and must NOT be used
for persist-oracle hot or in-loop messages.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Iterator, Optional
from uuid import uuid4

from codeagent.runtime.base import (
    CAP_HOT_RESUME,
    CAP_IN_LOOP_MESSAGES,
    CAP_NATIVE_UI,
    CAP_STREAM_EVENTS,
    CAP_TOOL_STATS,
    CAP_WARM_RESUME,
    RUNTIME_OMP,
    RuntimeAdapter,
    RuntimeErrorCode,
    RuntimeHandle,
    UNSUPPORTED_RUNTIME,
)
from codeagent.runtime.supervisor import RuntimeSpec, spawn_runtime

log = logging.getLogger(__name__)

_FULL_CAPS = frozenset({
    CAP_STREAM_EVENTS, CAP_IN_LOOP_MESSAGES, CAP_TOOL_STATS, CAP_NATIVE_UI,
    CAP_HOT_RESUME, CAP_WARM_RESUME,
})
_SHORT_TASK_CAPS = frozenset({CAP_STREAM_EVENTS, CAP_WARM_RESUME})


class OMPRuntimeAdapter(RuntimeAdapter):
    name = RUNTIME_OMP
    capabilities = _FULL_CAPS  # interactive_plugin mode

    def spawn(self, request: dict, context: Optional[dict] = None) -> RuntimeHandle:
        short_task = bool(request.get("short_task", False))
        mode = "short_task" if short_task else "interactive_plugin"
        runtime_id = f"omp-{uuid4().hex[:10]}"
        spec = RuntimeSpec(
            runtime_id=runtime_id,
            session_id=request.get("session_id", ""),
            agent_id=request.get("agent_id", ""),
            runtime=RUNTIME_OMP,
            review_key=request.get("review_key", ""),
            generation=int(request.get("generation", 1)),
            backend_session_id=request.get("backend_session_id", ""),
            workdir=request.get("workdir", ""),
            task=request.get("task", ""),
            model=request.get("model", ""),
            profile_args=list(request.get("profile_args", []) or []),
            gateway_socket=request.get("gateway_socket", ""),
            owner_pid=int(request.get("owner_pid", 0) or 0),
            nonce=request.get("nonce", ""),
            mode=mode,
            host_alias=request.get("host_alias", "__local__"),
            capabilities=list(_SHORT_TASK_CAPS if short_task else _FULL_CAPS),
            env={str(k): str(v) for k, v in (request.get("env", {}) or {}).items()},
        )
        if short_task:
            return self._spawn_short_task(request, runtime_id)
        handle = spawn_runtime(spec)
        return RuntimeHandle(
            runtime_id=runtime_id,
            runtime=RUNTIME_OMP,
            backend_session_id=spec.backend_session_id,
            host_alias=spec.host_alias,
            generation=spec.generation,
            capabilities=_SHORT_TASK_CAPS if short_task else _FULL_CAPS,
            supervisor="tmux",
            mode=mode,
            extra={"spec_path": spec.spec_path},
        )

    def _spawn_short_task(self, request: dict, runtime_id: str) -> RuntimeHandle:
        """Bounded short task via OMPRunner --print (explicit opt-in only)."""
        from codeagent.domain import RunRequest
        from codeagent.runners.omp import OMPRunner
        from codeagent.runners.base import RunnerConfig

        runner = OMPRunner(config=RunnerConfig(timeout=int(request.get("timeout", 600))))
        result = runner.run(RunRequest(
            task=request.get("task", ""),
            workdir=request.get("workdir", ""),
            backend=RUNTIME_OMP,
            agent=request.get("agent_id", ""),
            model=request.get("model", ""),
            session_key=request.get("review_key", ""),
            request_id=request.get("request_id", ""),
            run_id=request.get("run_id", ""),
            review_key=request.get("review_key", ""),
        ))
        return RuntimeHandle(
            runtime_id=runtime_id,
            runtime=RUNTIME_OMP,
            backend_session_id=result.session_id or "",
            generation=int(request.get("generation", 1)),
            capabilities=_SHORT_TASK_CAPS,
            supervisor="process",
            mode="short_task",
            extra={"result": {"returncode": result.returncode, "stdout": result.stdout}},
        )

    def send(self, handle: RuntimeHandle, message: dict) -> dict:
        if handle.mode == "short_task":
            raise RuntimeErrorCode(
                UNSUPPORTED_RUNTIME,
                "short_task runtime cannot receive in-loop messages (no hot/in-loop capability)",
            )
        # In-loop steering: the plugin reads the agent inbox and delivers
        # via pi.sendMessage(..., {triggerTurn:true, deliverAs:"steer"}).
        # Here we only enqueue the message into the agent's mailbox inbox.
        from codeagent.mailbox.service import MailboxService

        svc = MailboxService()
        receipt = svc.send(
            session_id=handle.extra.get("session_id", message.get("session_id", "")),
            from_id=message.get("from", "manager"),
            to_id=handle.extra.get("agent_id", message.get("agent_id", "")),
            subject=message.get("subject", "steer"),
            body=message.get("body", ""),
            kind=message.get("kind", "TASK"),
            run_id=message.get("run_id", ""),
            request_id=message.get("request_id", ""),
            require_ack=bool(message.get("require_ack", False)),
        )
        return {"msg_id": receipt.msg_id, "status": receipt.status}

    def subscribe(self, handle: RuntimeHandle, cursor: str = "") -> Iterator[Any]:
        raise NotImplementedError("subscribe: use events watch / gateway events.list")

    def probe(self, handle: RuntimeHandle) -> dict:
        from codeagent.runtime.supervisor import probe_runtime as _probe

        if handle.mode == "short_task":
            return {"alive": False, "reason": "short_task is bounded — no persistent runtime"}
        # TmuxRuntimeHandle probe (pane + supervisor PID + markers).
        spec_path = handle.extra.get("spec_path", "")
        try:
            from codeagent.launchers.tmux import probe_runtime as _tmux_probe
            import json as _json
            from pathlib import Path

            spec = RuntimeSpec.from_dict(_json.loads(Path(spec_path).read_text()))
            # Build a minimal handle shape for the tmux probe.
            from codeagent.launchers.tmux import TmuxRuntimeHandle, tmux_socket_path

            th = TmuxRuntimeHandle(
                runtime_id=handle.runtime_id,
                socket_path=tmux_socket_path(),
                session="postmesh-gateway",
                window="",
                pane_id="",
                host_alias=handle.host_alias,
                runtime=RUNTIME_OMP,
                pid=None,
                started_at="",
                generation=handle.generation,
                diagnostic_log=Path(spec_path).parent / f"{handle.runtime_id}.log",
            )
            return _tmux_probe(th)
        except Exception as exc:
            return {"alive": False, "reason": str(exc)}

    def resume(self, handle: RuntimeHandle, prompt: str) -> RuntimeHandle:
        # Warm resume: new pane with --resume <backend_session_id>.
        from codeagent.runtime.supervisor import RuntimeSpec, spawn_runtime

        spec = RuntimeSpec(
            runtime_id=f"omp-{uuid4().hex[:10]}",
            session_id=handle.extra.get("session_id", ""),
            agent_id=handle.extra.get("agent_id", ""),
            runtime=RUNTIME_OMP,
            review_key=handle.extra.get("review_key", ""),
            generation=handle.generation + 1,
            backend_session_id=handle.backend_session_id,
            workdir=handle.extra.get("workdir", ""),
            task=prompt,
            gateway_socket=handle.extra.get("gateway_socket", ""),
            mode="interactive_plugin",
            host_alias=handle.host_alias,
            capabilities=list(_FULL_CAPS),
        )
        new_handle = spawn_runtime(spec)
        return RuntimeHandle(
            runtime_id=new_handle.runtime_id,
            runtime=RUNTIME_OMP,
            backend_session_id=spec.backend_session_id,
            host_alias=handle.host_alias,
            generation=spec.generation,
            capabilities=_FULL_CAPS,
            supervisor="tmux",
            mode="warm",
            extra=dict(handle.extra, spec_path=spec.spec_path, session_id=spec.session_id, agent_id=spec.agent_id),
        )

    def stop(self, handle: RuntimeHandle, reason: str) -> None:
        from codeagent.runtime.supervisor import stop_runtime as _stop

        spec_path = handle.extra.get("spec_path", "")
        if not spec_path:
            return
        try:
            from codeagent.launchers.tmux import TmuxRuntimeHandle, tmux_socket_path
            from pathlib import Path

            th = TmuxRuntimeHandle(
                runtime_id=handle.runtime_id,
                socket_path=tmux_socket_path(),
                session="postmesh-gateway",
                window="", pane_id="",
                host_alias=handle.host_alias,
                runtime=RUNTIME_OMP, pid=None, started_at="",
                generation=handle.generation,
                diagnostic_log=Path(spec_path).parent / f"{handle.runtime_id}.log",
            )
            _stop(th, grace_seconds=10)
        except Exception as exc:
            log.warning("OMP stop failed: %s", exc)
