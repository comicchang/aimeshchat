"""SSH ControlMaster socket management.

Each remote host gets an independent SSH ControlMaster socket.
Socket path: ``$XDG_RUNTIME_DIR/codeagent/ssh/<host-hash>.sock``
Fallback:    ``$TMPDIR/codeagent-<UID>/ssh/<host-hash>.sock``

The *host-hash* is a stable 12-char hex digest of the SSH alias,
so different aliases never share a socket.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
from pathlib import Path

from codeagent.transport.base import TransportError

log = logging.getLogger(__name__)

# SSH options applied to every master creation.
_MASTER_OPTS: list[str] = [
    "-o", "ControlMaster=yes",
    "-o", "ControlPersist=no",
    "-o", "ClearAllForwardings=yes",
    "-o", "ForwardAgent=no",
    "-o", "ForwardX11=no",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
]


def _host_hash(alias: str) -> str:
    """Stable short hash for a host alias."""
    return hashlib.sha256(alias.encode()).hexdigest()[:12]


def _socket_dir() -> Path:
    """Return the directory for ControlMaster sockets, creating it if needed."""
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        d = Path(xdg) / "codeagent" / "ssh"
    else:
        uid = os.getuid()
        d = Path(os.environ.get("TMPDIR", "/tmp")) / f"codeagent-{uid}" / "ssh"
    d.mkdir(parents=True, exist_ok=True)
    return d


def socket_path(alias: str) -> Path:
    """Return the ControlMaster socket path for *alias*."""
    return _socket_dir() / f"{_host_hash(alias)}.sock"


class ControlMaster:
    """Manages a single SSH ControlMaster socket for one host.

    Usage::

        cm = ControlMaster(alias="myhost")
        cm.create()       # establish master
        cm.is_alive()     # True
        cm.stop()         # tear down
    """

    def __init__(self, alias: str, *, ssh_bin: str = "ssh") -> None:
        self.alias = alias
        self._ssh = ssh_bin
        self._socket = socket_path(alias)

    @property
    def socket(self) -> Path:
        return self._socket

    # ── lifecycle ───────────────────────────────────────────────────────

    def create(self) -> None:
        """Open a ControlMaster connection.

        Idempotent — if the socket is already alive, this is a no-op.
        """
        if self.is_alive():
            log.debug("master already alive for %s", self.alias)
            return

        ssh = shutil.which(self._ssh)
        if not ssh:
            raise TransportError(f"ssh binary not found: {self._ssh}")

        cmd = [
            ssh,
            "-M", "-N", "-f",
            "-S", str(self._socket),
            *_MASTER_OPTS,
            self.alias,
        ]
        log.debug("creating master: %s", " ".join(cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise TransportError(
                f"failed to create master for {self.alias}: "
                f"{proc.stderr.strip() or proc.stdout.strip() or 'exit code ' + str(proc.returncode)}"
            )
        log.info("master created for %s (socket %s)", self.alias, self._socket)

    def is_alive(self) -> bool:
        """Check if the ControlMaster socket is active."""
        return self._check() == 0

    def check(self) -> bool:
        """Alias for ``is_alive()``."""
        return self.is_alive()

    def stop(self) -> None:
        """Shut down the ControlMaster.

        Idempotent — no-op if already stopped.
        """
        if not self._socket.exists():
            return

        ssh = shutil.which(self._ssh)
        if not ssh:
            # Socket file exists but ssh gone — just clean up.
            self._cleanup_socket()
            return

        cmd = [ssh, "-O", "exit", "-S", str(self._socket), self.alias]
        log.debug("stopping master: %s", " ".join(cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            log.warning("master exit for %s returned %d: %s",
                        self.alias, proc.returncode, proc.stderr.strip())
        self._cleanup_socket()
        log.info("master stopped for %s", self.alias)

    # ── ssh command builder ─────────────────────────────────────────────

    def ssh_cmd(self, *remote_args: str) -> list[str]:
        """Build an ``ssh`` command that reuses this master socket.

        Returns the full argv (ssh binary + options + alias + remote args).
        """
        ssh = shutil.which(self._ssh)
        if not ssh:
            raise TransportError(f"ssh binary not found: {self._ssh}")
        return [ssh, "-S", str(self._socket), self.alias, *remote_args]

    # ── internals ───────────────────────────────────────────────────────

    def _check(self) -> int:
        """Run ``ssh -O check`` and return the exit code."""
        if not self._socket.exists():
            return 1

        ssh = shutil.which(self._ssh)
        if not ssh:
            return 1

        cmd = [ssh, "-O", "check", "-S", str(self._socket), self.alias]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return proc.returncode

    def _cleanup_socket(self) -> None:
        """Remove the socket file if it exists."""
        try:
            self._socket.unlink(missing_ok=True)
        except OSError:
            pass
