"""Runtime supervisor — launches an Agent inside a tmux pane and reports to the Gateway.

A tmux pane runs ONLY this supervisor: it reads a 0600 RuntimeSpec JSON
(path as argv[1]), spawns the Agent with argv+env (no shell), writes
markers (SHELL_READY/CWD_VERIFIED/AGENT_STARTED/AGENT_EXITED) plus a
diagnostic log, and reports PID/exit/signal to the Gateway's EventStore
via ``runtime.event``.

``spawn_runtime``/``probe_runtime``/``stop_runtime`` are the gateway-side
facade over the tmux launcher (launchers/tmux.py).
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from codeagent.constants import ISO_TIMESTAMP_FORMAT

log = logging.getLogger(__name__)

MARKER_SHELL_READY = "SHELL_READY"
MARKER_CWD_VERIFIED = "CWD_VERIFIED"
MARKER_AGENT_STARTED = "AGENT_STARTED"
MARKER_AGENT_EXITED = "AGENT_EXITED"

_OMP_BINARY = "omp"


@dataclass
class RuntimeSpec:
    """0600 spec written by the launcher, read by the supervisor."""

    runtime_id: str
    session_id: str
    agent_id: str
    runtime: str = "omp"  # omp | opencode | generic
    review_key: str = ""
    generation: int = 1
    backend_session_id: str = ""
    workdir: str = ""
    task: str = ""
    model: str = ""
    profile_args: list[str] = field(default_factory=list)
    gateway_socket: str = ""
    owner_pid: int = 0
    nonce: str = ""
    mode: str = "interactive_plugin"  # interactive_plugin | short_task
    host_alias: str = "__local__"
    capabilities: list[str] = field(default_factory=list)
    spec_path: str = ""
    env: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "runtime": self.runtime,
            "review_key": self.review_key,
            "generation": self.generation,
            "backend_session_id": self.backend_session_id,
            "workdir": self.workdir,
            "task": self.task,
            "model": self.model,
            "profile_args": list(self.profile_args),
            "gateway_socket": self.gateway_socket,
            "owner_pid": self.owner_pid,
            "nonce": self.nonce,
            "mode": self.mode,
            "host_alias": self.host_alias,
            "capabilities": list(self.capabilities),
            "spec_path": self.spec_path,
            "env": dict(self.env),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RuntimeSpec":
        return cls(
            runtime_id=d.get("runtime_id", ""),
            session_id=d.get("session_id", ""),
            agent_id=d.get("agent_id", ""),
            runtime=d.get("runtime", "omp"),
            review_key=d.get("review_key", ""),
            generation=int(d.get("generation", 1)),
            backend_session_id=d.get("backend_session_id", ""),
            workdir=d.get("workdir", ""),
            task=d.get("task", ""),
            model=d.get("model", ""),
            profile_args=list(d.get("profile_args", []) or []),
            gateway_socket=d.get("gateway_socket", ""),
            owner_pid=int(d.get("owner_pid", 0) or 0),
            nonce=d.get("nonce", ""),
            mode=d.get("mode", "interactive_plugin"),
            host_alias=d.get("host_alias", "__local__"),
            capabilities=list(d.get("capabilities", []) or []),
            spec_path=d.get("spec_path", ""),
            env={str(k): str(v) for k, v in (d.get("env", {}) or {}).items()},
        )


@dataclass(frozen=True)
class RuntimeHealth:
    """Probe result — process-level liveness only, never task state."""

    alive: bool
    pane_alive: bool = False
    pid_alive: bool = False
    pid: Optional[int] = None
    markers: dict[str, str] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "alive": self.alive,
            "pane_alive": self.pane_alive,
            "pid_alive": self.pid_alive,
            "pid": self.pid,
            "markers": self.markers,
            "reason": self.reason,
        }


# ── spec writing / marker helpers ──────────────────────────────────────


def write_spec(spec: RuntimeSpec, dir: Optional[Path] = None) -> Path:
    """Persist a 0600 RuntimeSpec JSON next to the marker dir. Returns the path."""
    d = dir or _runtime_dir(spec.runtime_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "spec.json"
    spec.spec_path = str(path)  # markers/pid/log land beside the spec
    tmp = d / f".tmp-spec-{uuid4().hex[:8]}.json"
    with open(tmp, "w") as f:
        f.write(json.dumps(spec.to_dict(), indent=2, ensure_ascii=False))
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(str(tmp), str(path))
    os.chmod(path, 0o600)
    return path


def _runtime_dir(runtime_id: str) -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    d = base / "postmesh" / "runtime" / runtime_id
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _spec_dir(spec: RuntimeSpec) -> Path:
    """Directory holding the spec — markers/pid/log live beside it."""
    if spec.spec_path:
        return Path(spec.spec_path).parent
    return _runtime_dir(spec.runtime_id)


# ── gateway-side facade (spawn/probe/stop) ─────────────────────────────


def spawn_runtime(spec: RuntimeSpec) -> Any:
    """Spawn a supervised runtime in a tmux pane. Returns TmuxRuntimeHandle."""
    from codeagent.launchers.tmux import spawn_runtime as _tmux_spawn

    d = _runtime_dir(spec.runtime_id)
    spec.spec_path = str(d / "spec.json")
    spec_path = write_spec(spec, dir=d)
    return _tmux_spawn(spec_path)


def probe_runtime(handle: Any) -> RuntimeHealth:
    """Probe a TmuxRuntimeHandle — pane + supervisor PID + markers only."""
    from codeagent.launchers.tmux import probe_runtime as _tmux_probe

    return RuntimeHealth(**_tmux_probe(handle))


def stop_runtime(handle: Any, grace_seconds: int = 10) -> bool:
    """Stop a supervised runtime (SIGTERM → grace → SIGKILL, then pane)."""
    from codeagent.launchers.tmux import stop_runtime as _tmux_stop

    return _tmux_stop(handle, grace_seconds=grace_seconds)


# ── supervisor main ────────────────────────────────────────────────────


def _build_agent_argv(spec: RuntimeSpec) -> list[str]:
    """Build the agent argv — NEVER through a shell.

    - OMP interactive_plugin: ``omp --cwd <workdir> [profile args]``
      (NO ``-c``, NO ``--print`` — a real interactive process that can
      receive steer/followUp and park hot).
    - OMP warm: ``omp --resume <backend_session_id> --cwd <workdir>``
    - OMP short_task: ``omp --print --mode json --cwd <workdir> @<prompt-file>``
      (only for explicit bounded short tasks — no hot/in-loop claims)
    - opencode: ``opencode run --format json --dir <workdir>
      [--agent <agent_id>] [--session <backend_session_id>] <task>``
      (task is a positional prompt — required for a real run)
    """
    if spec.runtime == "omp":
        argv = [_OMP_BINARY]
        if spec.mode == "short_task":
            argv += ["--print", "--mode", "json"]
        if spec.backend_session_id and spec.mode != "short_task":
            argv += ["--resume", spec.backend_session_id]
        if spec.workdir:
            argv += ["--cwd", spec.workdir]
        if spec.model:
            argv += ["--model", spec.model]
        argv += list(spec.profile_args)
        if spec.mode == "short_task":
            prompt_file = _write_prompt_file(spec.task)
            argv.append(f"@{prompt_file}")
        return argv
    if spec.runtime == "opencode":
        argv = ["opencode", "run", "--format", "json", "--dir", spec.workdir or "."]
        if spec.agent_id:
            argv += ["--agent", spec.agent_id]
        if spec.backend_session_id:
            argv += ["--session", spec.backend_session_id]
        # P0-6: append the task as a positional prompt (mirrors
        # OpenCodeRuntimeAdapter.spawn) — previously the task was dropped
        # from argv, so a supervised opencode run started with no instruction.
        if spec.task:
            argv.append(spec.task)
        return argv
    if spec.runtime == "generic":
        # Configured argv (never shell-interpreted) via profile_args.
        argv = list(spec.profile_args)
        return argv
    raise ValueError(f"unsupported runtime: {spec.runtime}")


def _write_prompt_file(task: str) -> str:
    import secrets
    import stat as stat_mod
    import tempfile

    fd, path = tempfile.mkstemp(prefix="omp_prompt_", suffix=".txt")
    try:
        os.fchmod(fd, stat_mod.S_IRUSR | stat_mod.S_IWUSR)
        os.write(fd, (task or "").encode("utf-8"))
    finally:
        os.close(fd)
    return path


def _report(gateway_socket: str, spec: RuntimeSpec, kind: str, payload: dict) -> None:
    """Best-effort runtime.event report to the local gateway."""
    try:
        from codeagent.gateway.client import GatewayClient

        client = GatewayClient(socket_path=Path(gateway_socket), timeout=5)
        client.call("runtime.event", {
            "event": {
                "runtime_id": spec.runtime_id,
                "generation": spec.generation,
                "session_id": spec.session_id,
                "agent_id": spec.agent_id,
                "request_id": "",
                "run_id": "",
                "kind": kind,
                "created_at": datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT),
                "payload": payload,
            }
        })
    except Exception as exc:
        log.debug("supervisor report to gateway failed: %s", exc)


def _mark(spec: RuntimeSpec, name: str, content: str = "") -> None:
    d = _spec_dir(spec)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{spec.runtime_id}.{name}").write_text(content)


def main(argv: list[str] | None = None) -> int:
    """Supervisor entry: ``python -m codeagent.runtime.supervisor <spec_path>``."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python -m codeagent.runtime.supervisor <spec.json>", file=sys.stderr)
        return 2
    spec_path = Path(argv[0])
    if not spec_path.exists():
        print(f"supervisor: spec not found: {spec_path}", file=sys.stderr)
        return 2
    try:
        spec = RuntimeSpec.from_dict(json.loads(spec_path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        print(f"supervisor: bad spec: {exc}", file=sys.stderr)
        return 2

    _mark(spec, MARKER_SHELL_READY, datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT))

    # CWD verification (marker only — fail-fast on missing workdir).
    if spec.workdir:
        wd = os.path.expanduser(spec.workdir)
        if not os.path.isdir(wd):
            _mark(spec, MARKER_CWD_VERIFIED, f"missing:{wd}")
            print(f"supervisor: workdir not found: {wd}", file=sys.stderr)
            _report(spec.gateway_socket, spec, "ERROR", {"error": f"workdir not found: {wd}"})
            return 1
        _mark(spec, MARKER_CWD_VERIFIED, wd)

    try:
        argv_list = _build_agent_argv(spec)
    except ValueError as exc:
        print(f"supervisor: {exc}", file=sys.stderr)
        _mark(spec, MARKER_AGENT_EXITED, f"unsupported_runtime:{exc}")
        _report(spec.gateway_socket, spec, "ERROR", {"error": str(exc)})
        return 1

    env = os.environ.copy()
    # Spec-provided env (e.g. OMP_MEMORY_CONFIG_PATH from oracle start).
    for k, v in spec.env.items():
        env[k] = v
    env.setdefault("POSTMESH_RUNTIME_ID", spec.runtime_id)
    env.setdefault("POSTMESH_GATEWAY_SOCKET", spec.gateway_socket)
    if spec.review_key:
        env.setdefault("REVIEW_KEY", spec.review_key)
    if spec.session_id:
        env.setdefault("OMP_SESSION_ID", spec.session_id)
        env.setdefault("SWARM_SESSION_ID", spec.session_id)
    if spec.agent_id:
        env.setdefault("OMP_WORKER_ID", spec.agent_id)

    d = _spec_dir(spec)

    # ── Launcher identity file (0600) → OMP plugin handshake ──────────
    # The plugin reads OMP_MAILBOX_IDENTITY_FILE at startup; ownership is
    # validated via owner_pid+nonce+generation. Shutdown only cleans up
    # when all three match.
    if spec.gateway_socket:
        # owner_pid MUST be the supervisor (long-lived runtime owner) — the
        # launcher CLI process exits right after spawn, and the plugin's
        # stale-identity check (process.kill(owner,0)) would reject it.
        identity = {
            "session_id": spec.session_id,
            "agent_id": spec.agent_id,
            "runtime_id": spec.runtime_id,
            "review_key": spec.review_key,
            "generation": spec.generation,
            "gateway_socket": spec.gateway_socket,
            "owner_pid": os.getpid(),
            "nonce": spec.nonce,
        }
        identity_path = d / "identity.json"
        tmp_id = d / f".tmp-identity-{uuid4().hex[:8]}.json"
        with open(tmp_id, "w") as f:
            f.write(json.dumps(identity))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_id, 0o600)
        os.replace(str(tmp_id), str(identity_path))
        os.chmod(identity_path, 0o600)
        env["OMP_MAILBOX_IDENTITY_FILE"] = str(identity_path)
        if spec.nonce:
            env["OMP_MAILBOX_NONCE"] = spec.nonce
        # Runtime adapter mode for the plugin.
        env["CODEAGENT_ROLE"] = "oracle" if spec.agent_id.startswith("oracle") else "worker"
        env["POSTMESH_GATEWAY_SOCKET"] = spec.gateway_socket

    log_path = d / f"{spec.runtime_id}.log"
    try:
        proc = subprocess.Popen(
            argv_list,
            cwd=os.path.expanduser(spec.workdir) if spec.workdir else None,
            env=env,
            stdout=open(log_path, "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        print(f"supervisor: spawn failed: {exc}", file=sys.stderr)
        _mark(spec, MARKER_AGENT_EXITED, f"spawn_failed:{exc}")
        _report(spec.gateway_socket, spec, "ERROR", {"error": str(exc)})
        return 1

    # PID marker + gateway report.
    pid_file = d / f"{spec.runtime_id}.pid"
    pid_file.write_text(str(proc.pid))
    _mark(spec, MARKER_AGENT_STARTED, str(proc.pid))
    _report(spec.gateway_socket, spec, "RUNTIME_STATE", {
        "state": "agent_started", "pid": proc.pid, "argv": argv_list,
    })

    rc = proc.wait()
    try:
        pid_file.unlink(missing_ok=True)
    except OSError:
        pass
    _mark(spec, MARKER_AGENT_EXITED, str(rc))
    _report(spec.gateway_socket, spec, "RUNTIME_STATE", {
        "state": "agent_exited", "exit_code": rc,
    })
    return rc


if __name__ == "__main__":
    sys.exit(main())
