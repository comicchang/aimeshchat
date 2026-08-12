"""Tmux runtime launcher — spawn agent panes on a private tmux socket.

The tmux pane runs ONLY the supervisor (``python -m codeagent.runtime.supervisor``
with a 0600 RuntimeSpec JSON path) — no shell redirection is ever composed
into a tmux command. The supervisor owns the agent process lifecycle and
reports PID/exit to the gateway.

Private socket: ${TMPDIR:-/tmp}/aimeshchat-tmux/codeagent.sock, session
``aimeshchat-gateway`` — keeps gateway-managed runtimes out of the user's
interactive tmux sessions.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from codeagent.constants import ISO_TIMESTAMP_FORMAT

log = logging.getLogger(__name__)

TMUX_SOCKET_DIR_ENV = "AIMESHCHAT_TMUX_SOCKET_DIR"
TMUX_SESSION_NAME = "aimeshchat-gateway"


def tmux_socket_dir() -> Path:
    """${TMPDIR:-/tmp}/aimeshchat-tmux — private socket directory."""
    override = os.environ.get(TMUX_SOCKET_DIR_ENV)
    base = Path(override) if override else Path(os.environ.get("TMPDIR", "/tmp")) / "aimeshchat-tmux"
    base.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    return base


def tmux_socket_path() -> Path:
    return tmux_socket_dir() / "codeagent.sock"


def tmux_cmd(*args: str) -> list[str]:
    """Build a tmux argv with the private socket.

    ``-S`` takes an ABSOLUTE socket path (``-L`` would treat it as a name
    under the tmux runtime dir and create a broken nested path).
    """
    return ["tmux", "-S", str(tmux_socket_path()), *args]


@dataclass
class PaneConfig:
    """Legacy configuration for a tmux pane (kept for compatibility)."""
    session: str = "agents"
    window: str = "main"
    shell: str = "zsh"
    cwd: str = ""
    env: Optional[dict[str, str]] = None


@dataclass
class TmuxRuntimeHandle:
    """Handle to a runtime supervised inside a tmux pane."""

    runtime_id: str
    socket_path: Path
    session: str
    window: str
    pane_id: str
    host_alias: str
    runtime: str
    pid: Optional[int]
    started_at: str
    generation: int
    diagnostic_log: Path

    def to_dict(self) -> dict:
        return {
            "runtime_id": self.runtime_id,
            "socket_path": str(self.socket_path),
            "session": self.session,
            "window": self.window,
            "pane_id": self.pane_id,
            "host_alias": self.host_alias,
            "runtime": self.runtime,
            "pid": self.pid,
            "started_at": self.started_at,
            "generation": self.generation,
            "diagnostic_log": str(self.diagnostic_log),
        }


# The swarm/tmux display name stays the existing sid scheme — never use a
# cleaned logical ID as a backend session ID.
def runtime_sid(review_key: str) -> str:
    """Short stable-ish runtime display name (≤ 32 chars, tmux-safe)."""
    safe = review_key.replace(":", "-")[-12:]
    return f"ora-{safe}-{uuid4().hex[:10]}"[:32]


def _tmux_ok() -> bool:
    """True when the tmux binary exists and is executable."""
    from shutil import which

    return which("tmux") is not None


def _tmux(*args: str, timeout: int = 10) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            tmux_cmd(*args), capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode, r.stdout, r.stderr
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 1, "", str(exc)


def send_keys(pane_target: str, text: str, enter: bool = True) -> bool:
    """Send keystrokes to a tmux pane (legacy helper)."""
    cmd = tmux_cmd("send-keys", "-t", pane_target, text)
    if enter:
        cmd.append("Enter")
    return subprocess.run(cmd, capture_output=True).returncode == 0


def capture_pane(pane_target: str, lines: int = 50) -> str:
    """Capture output from a tmux pane (diagnostics only — never task state)."""
    rc, out, _ = _tmux("capture-pane", "-t", pane_target, "-p", "-S", f"-{lines}")
    return out


def create_pane(config: PaneConfig) -> Optional[str]:
    """Legacy: create a new tmux pane and return its target."""
    rc, out, _ = _tmux("split-window", "-t", f"{config.session}:{config.window}",
                       *(("-c", config.cwd) if config.cwd else ()),
                       *(["-P", "-F", "#{pane_id}", config.shell] if config.shell else ()))
    if rc == 0:
        return out.strip()
    return None


def kill_pane(pane_target: str) -> bool:
    """Legacy: kill a tmux pane."""
    return _tmux("kill-pane", "-t", pane_target)[0] == 0


def ensure_tmux_server() -> bool:
    """Ensure the private tmux server + gateway session exist. Returns True on success."""
    if not _tmux_ok():
        return False
    rc, _, err = _tmux("has-session", "-t", TMUX_SESSION_NAME)
    if rc == 0:
        return True
    rc, _, err = _tmux("new-session", "-d", "-s", TMUX_SESSION_NAME, "-x", "220", "-y", "50")
    if rc != 0:
        log.warning("tmux new-session failed: %s", err)
        return False
    return True


def spawn_runtime(spec_path: Path) -> TmuxRuntimeHandle:
    """Create a tmux pane running the supervisor for *spec_path*.

    The pane command is a single argv invocation (no shell redirection):
        python -m codeagent.runtime.supervisor <spec_path>
    """
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    runtime_id = spec["runtime_id"]
    runtime = spec.get("runtime", "omp")
    review_key = spec.get("review_key", "")
    generation = int(spec.get("generation", 1))
    host_alias = spec.get("host_alias", "__local__")

    if not _tmux_ok():
        raise RuntimeError("tmux runtime unavailable: tmux binary not found")
    if not ensure_tmux_server():
        raise RuntimeError("tmux runtime unavailable: cannot start private tmux server")

    sid = runtime_sid(review_key or runtime_id)
    rc, out, err = _tmux("new-window", "-t", f"{TMUX_SESSION_NAME}:", "-n", sid, "-P", "-F", "#{pane_id}")
    if rc != 0:
        raise RuntimeError(f"tmux new-window failed: {err}")
    pane_id = out.strip().splitlines()[0] if out.strip() else ""
    if not pane_id:
        raise RuntimeError("tmux new-window returned no pane id")

    # Supervisor command — argv only, never a composed shell string.
    supervisor = [sys.executable, "-m", "codeagent.runtime.supervisor", str(spec_path)]
    quoted = " ".join(shlex.quote(a) for a in supervisor)
    rc, _, err = _tmux("send-keys", "-t", pane_id, quoted, "Enter")
    if rc != 0:
        _tmux("kill-pane", "-t", pane_id)
        raise RuntimeError(f"tmux send-keys failed: {err}")

    started_at = datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT)
    diagnostic_log = spec_path.parent / f"{runtime_id}.log"
    return TmuxRuntimeHandle(
        runtime_id=runtime_id,
        socket_path=tmux_socket_path(),
        session=TMUX_SESSION_NAME,
        window=sid,
        pane_id=pane_id,
        host_alias=host_alias,
        runtime=runtime,
        pid=None,
        started_at=started_at,
        generation=generation,
        diagnostic_log=diagnostic_log,
    )


def probe_runtime(handle: TmuxRuntimeHandle) -> dict:
    """Probe runtime health: tmux pane alive + supervisor PID + markers.

    Returns a RuntimeHealth dict. ``capture_pane`` output is NEVER used to
    infer task state — only process/pane liveness.
    """
    health: dict = {"alive": False, "pane_alive": False, "pid_alive": False, "markers": {}}
    rc, _, _ = _tmux("has-session", "-t", handle.session)
    health["pane_alive"] = rc == 0
    pid_file = handle.diagnostic_log.parent / f"{handle.runtime_id}.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            health["pid_alive"] = True
            health["pid"] = pid
        except (ValueError, ProcessLookupError, OSError):
            health["pid_alive"] = False
    marker_dir = handle.diagnostic_log.parent
    for marker in ("SHELL_READY", "CWD_VERIFIED", "AGENT_STARTED", "AGENT_EXITED"):
        p = marker_dir / f"{handle.runtime_id}.{marker}"
        if p.exists():
            health["markers"][marker] = p.read_text(errors="replace").strip()[:200]
    health["alive"] = health["pane_alive"] and health["pid_alive"]
    return health


def stop_runtime(handle: TmuxRuntimeHandle, grace_seconds: int = 10) -> bool:
    """Terminate the supervisor (SIGTERM → grace → SIGKILL), then the pane."""
    pid_file = handle.diagnostic_log.parent / f"{handle.runtime_id}.pid"
    pid: Optional[int] = None
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            pid = None
    if pid is not None:
        try:
            os.kill(pid, 15)  # SIGTERM
        except (ProcessLookupError, OSError):
            pid = None
        if pid is not None:
            deadline = time.monotonic() + grace_seconds
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except (ProcessLookupError, OSError):
                    break
                time.sleep(0.2)
            try:
                os.kill(pid, 0)
                os.kill(pid, 9)  # SIGKILL
            except (ProcessLookupError, OSError):
                pass
    rc, _, err = _tmux("kill-pane", "-t", handle.pane_id)
    if rc != 0:
        log.warning("tmux kill-pane failed: %s", err)
    return True
