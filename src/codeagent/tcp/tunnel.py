"""SSH tunnel management for cross-host TCP forwarding.

Provides ``PortAllocator`` for deterministic port assignment and
``TunnelManager`` for the full lifecycle of SSH port-forwarding tunnels
(Worker→Manager via ``-L`` and Manager→Worker via ``-R``).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum

from codeagent.constants import TCP_DAEMON_PORT, TCP_PORT_BASE

log = logging.getLogger(__name__)

# ── SSH tunnel options ──────────────────────────────────────────────────

_BASE_SSH_OPTS: list[str] = [
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=15",
]

_MAX_BACKOFF = 30  # seconds


# ── data types ──────────────────────────────────────────────────────────


class TunnelDirection(Enum):
    """Tunnel forwarding direction."""

    LOCAL = "local"   # -L  (Worker → Manager)
    REMOTE = "remote"  # -R  (Manager → Worker)


class TunnelState(Enum):
    """Operational state of a tunnel."""

    STARTING = "starting"
    RUNNING = "running"
    RECONNECTING = "reconnecting"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass
class TunnelInfo:
    """Snapshot of a single tunnel's status."""

    host_alias: str
    direction: TunnelDirection
    state: TunnelState
    local_port: int
    remote_port: int
    pid: int | None = None
    uptime: float = 0.0
    restarts: int = 0


# ── PortAllocator ───────────────────────────────────────────────────────


class PortAllocator:
    """Deterministic port assignment with conflict detection.

    * Local port is always ``TCP_DAEMON_PORT`` (5555).
    * Remote port is ``TCP_PORT_BASE + hash(host_alias) % 10_000``,
      giving a range of 15555–25554.
    """

    @staticmethod
    def local_port() -> int:
        """Return the fixed local daemon port."""
        return TCP_DAEMON_PORT

    @staticmethod
    def remote_port(host_alias: str) -> int:
        """Return a deterministic remote port for *host_alias*."""
        digest = hashlib.sha256(host_alias.encode()).hexdigest()
        offset = int(digest[:8], 16) % 10_000
        return TCP_PORT_BASE + offset

    @staticmethod
    def is_port_available(port: int, host: str = "127.0.0.1") -> bool:
        """Check whether *port* is free by attempting to bind.

        Returns ``True`` if the port can be bound (i.e. is available),
        ``False`` if it is already in use or *port* is out of range.
        """
        if not (0 <= port <= 65535):
            return False
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
                sock.bind((host, port))
                return True
        except OSError:
            return False


# ── TunnelManager ───────────────────────────────────────────────────────


class TunnelManager:
    """Manages SSH port-forwarding tunnels for multiple host aliases.

    Each tunnel runs as a background ``ssh`` subprocess.  The manager
    handles lifecycle, health checks, and automatic reconnection with
    exponential backoff (capped at 30 s).
    """

    def __init__(
        self,
        *,
        ssh_bin: str = "ssh",
        health_interval: float = 30.0,
    ) -> None:
        self._ssh = ssh_bin
        self._health_interval = health_interval
        # host_alias → _TunnelEntry
        self._tunnels: dict[str, _TunnelEntry] = {}
        self._lock = asyncio.Lock()

    # ── public API ──────────────────────────────────────────────────────

    async def ensure(
        self,
        host_alias: str,
        *,
        direction: TunnelDirection = TunnelDirection.LOCAL,
    ) -> None:
        """Ensure an SSH tunnel for *host_alias* exists and is running.

        Idempotent — does nothing if the tunnel is already healthy.
        """
        async with self._lock:
            entry = self._tunnels.get(host_alias)
            if entry is not None and entry.state is TunnelState.RUNNING:
                return
            if entry is not None:
                # Tear down stale entry before re-creating.
                entry.kill()
            entry = _TunnelEntry(
                host_alias=host_alias,
                direction=direction,
                ssh_bin=self._ssh,
                health_interval=self._health_interval,
            )
            self._tunnels[host_alias] = entry
            entry.start()

    async def teardown(self, host_alias: str) -> bool:
        """Tear down the tunnel for *host_alias*.  Returns ``True`` if found."""
        async with self._lock:
            entry = self._tunnels.pop(host_alias, None)
            if entry is None:
                return False
            entry.kill()
            return True

    async def teardown_all(self) -> int:
        """Tear down every managed tunnel.  Returns count of tunnels stopped."""
        async with self._lock:
            count = 0
            for entry in list(self._tunnels.values()):
                entry.kill()
                count += 1
            self._tunnels.clear()
            return count

    async def status(self) -> dict[str, TunnelInfo]:
        """Return a ``{host_alias: TunnelInfo}`` snapshot of every tunnel."""
        async with self._lock:
            return {alias: entry.info() for alias, entry in self._tunnels.items()}

    # ── health check loop ───────────────────────────────────────────────

    async def run_health_loop(self) -> None:
        """Periodically check tunnel health and reconnect dead ones.

        Intended to run as an ``asyncio.Task``.
        """
        while True:
            await asyncio.sleep(self._health_interval)
            async with self._lock:
                for alias, entry in list(self._tunnels.items()):
                    if entry.state is TunnelState.STOPPED:
                        continue
                    if not entry.is_process_alive():
                        log.warning("tunnel dead for %s — reconnecting", alias)
                        entry.reconnect()


# ── internal tunnel entry ──────────────────────────────────────────────


class _TunnelEntry:
    """Internal bookkeeping for one SSH tunnel subprocess."""

    def __init__(
        self,
        *,
        host_alias: str,
        direction: TunnelDirection,
        ssh_bin: str,
        health_interval: float,
    ) -> None:
        self.host_alias = host_alias
        self.direction = direction
        self._ssh_bin = ssh_bin
        self._health_interval = health_interval
        self._proc: subprocess.Popen[bytes] | None = None
        self._state = TunnelState.STARTING
        self._started_at = 0.0
        self._restarts = 0
        self._backoff = 1.0  # seconds; doubles on each failure up to _MAX_BACKOFF

    # ── properties ──────────────────────────────────────────────────────

    @property
    def state(self) -> TunnelState:
        return self._state

    @property
    def local_port(self) -> int:
        return PortAllocator.local_port()

    @property
    def remote_port(self) -> int:
        return PortAllocator.remote_port(self.host_alias)

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    # ── lifecycle ───────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the SSH tunnel subprocess."""
        cmd = build_ssh_tunnel_cmd(
            host_alias=self.host_alias,
            direction=self.direction,
            ssh_bin=self._ssh_bin,
        )
        log.debug("starting tunnel: %s", " ".join(cmd))
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            log.error("ssh binary not found: %s", self._ssh_bin)
            self._state = TunnelState.FAILED
            return
        self._state = TunnelState.RUNNING
        self._started_at = time.monotonic()
        log.info(
            "tunnel started for %s (pid=%s, dir=%s)",
            self.host_alias,
            self._proc.pid,
            self.direction.value,
        )

    def kill(self) -> None:
        """Kill the SSH subprocess (idempotent)."""
        if self._proc is not None:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=3)
            except OSError:
                pass
            self._proc = None
        self._state = TunnelState.STOPPED
        log.info("tunnel stopped for %s", self.host_alias)

    def is_process_alive(self) -> bool:
        """Check if the SSH subprocess is still running."""
        if self._proc is None:
            return False
        return self._proc.poll() is None

    def reconnect(self) -> None:
        """Kill the old process and re-launch with exponential backoff."""
        self.kill()
        self._state = TunnelState.RECONNECTING
        self._restarts += 1
        # Exponential backoff: 1s, 2s, 4s, …, capped at _MAX_BACKOFF.
        wait = self._backoff
        self._backoff = min(self._backoff * 2, _MAX_BACKOFF)
        log.info(
            "reconnecting tunnel for %s (attempt %d, backoff %.1fs)",
            self.host_alias,
            self._restarts,
            wait,
        )
        time.sleep(wait)
        self.start()

    def info(self) -> TunnelInfo:
        """Return a snapshot of this tunnel's status."""
        uptime = 0.0
        if self._state is TunnelState.RUNNING and self._started_at > 0:
            uptime = time.monotonic() - self._started_at
        return TunnelInfo(
            host_alias=self.host_alias,
            direction=self.direction,
            state=self._state,
            local_port=self.local_port,
            remote_port=self.remote_port,
            pid=self.pid,
            uptime=uptime,
            restarts=self._restarts,
        )


# ── SSH command construction ───────────────────────────────────────────


def build_ssh_tunnel_cmd(
    host_alias: str,
    *,
    direction: TunnelDirection,
    local_port: int | None = None,
    remote_port: int | None = None,
    ssh_bin: str = "ssh",
) -> list[str]:
    """Build the ``ssh`` argv for a port-forwarding tunnel.

    Parameters
    ----------
    host_alias:
        The SSH host alias from ``~/.ssh/config``.
    direction:
        ``TunnelDirection.LOCAL`` (``-L``, Worker→Manager) or
        ``TunnelDirection.REMOTE`` (``-R``, Manager→Worker).
    local_port:
        Override the local port.  Defaults to ``TCP_DAEMON_PORT``.
    remote_port:
        Override the remote port.  Defaults to the deterministic
        ``PortAllocator.remote_port(host_alias)``.
    ssh_bin:
        Path/name of the ``ssh`` binary.

    Returns
    -------
    list[str]
        Full ``argv`` suitable for ``subprocess.Popen``.
    """
    lp = local_port if local_port is not None else PortAllocator.local_port()
    rp = remote_port if remote_port is not None else PortAllocator.remote_port(host_alias)

    if direction is TunnelDirection.LOCAL:
        # Worker→Manager: -L binds local port, connects to remote daemon.
        forward_spec = f"127.0.0.1:{lp}:127.0.0.1:{rp}"
    else:
        # Manager→Worker: -R binds remote port, connects to local daemon.
        forward_spec = f"127.0.0.1:{rp}:127.0.0.1:{lp}"

    return [
        ssh_bin,
        "-L" if direction is TunnelDirection.LOCAL else "-R",
        forward_spec,
        *_BASE_SSH_OPTS,
        "-N", "-T",
        host_alias,
    ]
