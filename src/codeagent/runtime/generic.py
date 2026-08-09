"""GenericRuntimeAdapter — stdio NDJSON agent (cold-only by default).

The adapter runs a configured argv (never through a shell) speaking the
NDJSON protocol ``task/progress/result/error`` over stdio. Capabilities
are fixed at ``stream_events``; without a backend session it only
supports cold spawns — one-shot output is never masqueraded as a
persistent context. Required capabilities that cannot be satisfied make
the registry return UNSUPPORTED_CAPABILITY.
"""
from __future__ import annotations

import json
import logging
import subprocess
from typing import Any, Iterator, Optional
from uuid import uuid4

from codeagent.runtime.base import (
    CAP_STREAM_EVENTS,
    RUNTIME_GENERIC,
    RuntimeAdapter,
    RuntimeHandle,
)

log = logging.getLogger(__name__)

_CAPS = frozenset({CAP_STREAM_EVENTS})


class GenericRuntimeAdapter(RuntimeAdapter):
    name = RUNTIME_GENERIC
    capabilities = _CAPS

    def spawn(self, request: dict, context: Optional[dict] = None) -> RuntimeHandle:
        argv = list(request.get("argv", []) or request.get("profile_args", []) or [])
        if not argv:
            raise ValueError("generic runtime requires 'argv' (configured, no shell)")
        cwd = request.get("workdir", "") or None
        runtime_id = f"generic-{uuid4().hex[:10]}"
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, cwd=cwd,
            )
        except (OSError, FileNotFoundError) as exc:
            raise RuntimeError(f"generic spawn failed: {exc}") from exc

        # Bounded cold task: send the task frame, read until result/error.
        task = request.get("task", "")
        try:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps({"type": "task", "task": task}) + "\n")
            proc.stdin.flush()
            result = None
            progress: list[dict] = []
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                t = obj.get("type")
                if t == "progress":
                    progress.append(obj)
                elif t == "result":
                    result = obj
                    break
                elif t == "error":
                    result = {"error": obj.get("error", "unknown error")}
                    break
            rc = proc.wait(timeout=30)
        except Exception as exc:
            proc.kill()
            proc.wait()
            raise RuntimeError(f"generic protocol failure: {exc}") from exc

        return RuntimeHandle(
            runtime_id=runtime_id,
            runtime=RUNTIME_GENERIC,
            backend_session_id="",  # cold-only: no backend session
            host_alias=request.get("host_alias", "__local__"),
            generation=int(request.get("generation", 1)),
            capabilities=_CAPS,
            supervisor="process",
            mode="cold",
            extra={"result": result, "progress": progress, "returncode": rc},
        )

    def send(self, handle: RuntimeHandle, message: dict) -> dict:
        # Cold-only: no persistent agent to message.
        raise NotImplementedError("generic runtime is cold-only — no in-loop send")

    def subscribe(self, handle: RuntimeHandle, cursor: str = "") -> Iterator[Any]:
        raise NotImplementedError("subscribe: use events watch / gateway events.list")

    def probe(self, handle: RuntimeHandle) -> dict:
        return {
            "alive": bool(handle.extra.get("result")),
            "runtime": RUNTIME_GENERIC,
            "backend_session_id": "",
            "cold_only": True,
        }

    def resume(self, handle: RuntimeHandle, prompt: str) -> RuntimeHandle:
        raise NotImplementedError("generic runtime has no backend session — cold-only")

    def stop(self, handle: RuntimeHandle, reason: str) -> None:
        return
