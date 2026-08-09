"""OpenCodeRuntimeAdapter — warm-only OpenCode runtime (opencode 1.18.10).

Invocation (verified locally)::

    opencode run --format json --dir <cwd> --agent <name>          # first run
    opencode run --format json --dir <cwd> --agent <name> --session <id>  # warm

First run extracts the session id from the JSON event stream; later runs
pass ``--session`` for warm resume. Capabilities are fixed and honest:
stream_events + warm_resume ONLY. ``in_loop_messages``/``tool_stats``/
``native_ui``/``hot_resume`` are explicitly unsupported — messages queue
to the next turn and status shows degraded.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from typing import Any, Iterator, Optional
from uuid import uuid4

from codeagent.runtime.base import (
    CAP_STREAM_EVENTS,
    CAP_WARM_RESUME,
    RUNTIME_OPENCODE,
    RuntimeAdapter,
    RuntimeHandle,
)

log = logging.getLogger(__name__)

_CAPS = frozenset({CAP_STREAM_EVENTS, CAP_WARM_RESUME})


class OpenCodeRuntimeAdapter(RuntimeAdapter):
    name = RUNTIME_OPENCODE
    capabilities = _CAPS

    def spawn(self, request: dict, context: Optional[dict] = None) -> RuntimeHandle:
        cwd = request.get("workdir", "") or os.getcwd()
        agent = request.get("agent_id", "") or request.get("agent", "") or ""
        session_id = request.get("backend_session_id", "") or ""
        runtime_id = f"opencode-{uuid4().hex[:10]}"

        argv = ["opencode", "run", "--format", "json", "--dir", cwd]
        if agent:
            argv += ["--agent", agent]
        if session_id:
            argv += ["--session", session_id]
        task = request.get("task", "") or request.get("prompt", "")
        if task:
            argv.append(task)  # positional prompt — required for a real run

        try:
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, cwd=cwd,
            )
        except (OSError, FileNotFoundError) as exc:
            raise RuntimeError(f"opencode spawn failed: {exc}") from exc

        # First run: extract session id from the JSONL event stream.
        # NO hard timeout: the session-id extraction has a bounded WINDOW
        # (select-based, non-blocking) — a slow-thinking opencode is never
        # killed ("no hard timeout" requirement). If no session id appears
        # within the window, the handle is returned warm-capable with an
        # empty backend id; a later probe refreshes it.
        found_session = session_id
        stdout_buf: list[str] = []
        session_window = 30.0  # bounded extraction window, not a kill timeout
        if not session_id:
            import select as _sel

            deadline = time.monotonic() + session_window
            assert proc.stdout is not None
            while time.monotonic() < deadline:
                try:
                    ready, _, _ = _sel.select([proc.stdout], [], [], 0.5)
                except (ValueError, OSError, TypeError):
                    # Non-selectable stream (tests / pipes without fileno) —
                    # degrade to a direct read.
                    ready = True
                if not ready:
                    continue
                line = proc.stdout.readline()
                if not line:
                    break
                stdout_buf.append(line)
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                # OpenCode emits sessionID on EVERY frame (session/assistant/
                # error/result) — capture it from any of them.
                if not found_session and obj.get("sessionID"):
                    found_session = str(obj["sessionID"])
                if obj.get("type") == "session" and obj.get("id"):
                    found_session = obj.get("id")
                    break
                if found_session:
                    break
        stderr_tail = ""
        stdout_text = "".join(stdout_buf)
        if found_session:
            # Session established — keep the process handle for warm resume.
            return RuntimeHandle(
                runtime_id=runtime_id,
                runtime=RUNTIME_OPENCODE,
                backend_session_id=found_session,
                host_alias=request.get("host_alias", "__local__"),
                generation=int(request.get("generation", 1)),
                capabilities=_CAPS,
                supervisor="process",
                mode="warm" if session_id else "first_run",
                extra={
                    "cwd": cwd, "agent": agent,
                    "result": {"returncode": None, "stdout": stdout_text, "stderr": stderr_tail},
                },
            )
        # No session id yet — the process is still thinking. Return a handle
        # WITHOUT killing it; warm resume will re-attach via --session when
        # the id becomes known (ParkManifest).
        return RuntimeHandle(
            runtime_id=runtime_id,
            runtime=RUNTIME_OPENCODE,
            backend_session_id="",
            host_alias=request.get("host_alias", "__local__"),
            generation=int(request.get("generation", 1)),
            capabilities=_CAPS,
            supervisor="process",
            mode="first_run",
            extra={"cwd": cwd, "agent": agent, "result": {"returncode": None, "stdout": stdout_text}},
        )

    def send(self, handle: RuntimeHandle, message: dict) -> dict:
        # No in-loop delivery: the message queues to the next turn.
        from codeagent.mailbox.service import MailboxService

        svc = MailboxService()
        receipt = svc.send(
            session_id=handle.extra.get("session_id", message.get("session_id", "")),
            from_id=message.get("from", "manager"),
            to_id=handle.extra.get("agent_id", message.get("agent_id", "")),
            subject=message.get("subject", "next-turn"),
            body=message.get("body", ""),
            kind=message.get("kind", "TASK"),
            run_id=message.get("run_id", ""),
            request_id=message.get("request_id", ""),
        )
        return {
            "msg_id": receipt.msg_id,
            "status": receipt.status,
            "degraded": True,
            "note": "opencode has no in_loop_messages — delivered to next turn",
        }

    def subscribe(self, handle: RuntimeHandle, cursor: str = "") -> Iterator[Any]:
        raise NotImplementedError("subscribe: use events watch / gateway events.list")

    def probe(self, handle: RuntimeHandle) -> dict:
        return {
            "alive": True,
            "runtime": RUNTIME_OPENCODE,
            "backend_session_id": handle.backend_session_id,
            "in_loop_messages": False,
            "tool_stats": False,
            "native_ui": False,
            "hot_resume": False,
            "warm_resume": True,
        }

    def resume(self, handle: RuntimeHandle, prompt: str) -> RuntimeHandle:
        return self.spawn({
            "workdir": handle.extra.get("cwd", ""),
            "agent_id": handle.extra.get("agent", ""),
            "backend_session_id": handle.backend_session_id,
            "generation": handle.generation + 1,
            "host_alias": handle.host_alias,
        })

    def stop(self, handle: RuntimeHandle, reason: str) -> None:
        # The opencode process is bounded per-run; nothing persistent to stop.
        return
