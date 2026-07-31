"""Integration tests — gated on --run-integration flag.

These tests exercise real external services (localhost SSH daemon) and
are skipped by default.  Run them explicitly with::

    uv run pytest --run-integration tests/test_integration.py

``pytest.config`` was removed in pytest 7, so ``conftest.pytest_configure``
mirrors the ``--run-integration`` flag into ``RUN_INTEGRATION`` for
collection-time evaluation of the marker below.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from codeagent.domain import HostSpec
from codeagent.transport.ssh import SSHTransport
from codeagent.wire.protocol import decode_line, encode_line, make_ping

requires_integration = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="integration tests disabled (use --run-integration)",
)


def _remote_exec_argv() -> list[str]:
    """Resolve the remote-exec helper for a localhost SSH session.

    Prefers the installed ``codeagent-remote-exec`` entry point (its
    shebang is absolute, so it works under the minimal PATH ssh
    provides); falls back to ``python -m codeagent.remote_exec``.
    """
    entry = shutil.which("codeagent-remote-exec")
    if entry:
        return [entry]
    return [sys.executable, "-m", "codeagent.remote_exec"]


@requires_integration
class TestLocalhostSSH:
    """End-to-end SSH ControlMaster + wire protocol against localhost."""

    def test_warm_ping_verify_stop(self, tmp_path: pytest.TempPathFactory) -> None:
        """warm ControlMaster → run ping → verify response → stop."""
        host = HostSpec(
            name="localhost-it",
            ssh_alias="localhost",
            hostnames=("localhost",),
            description="integration test host",
        )
        transport = SSHTransport()
        try:
            # 1. Warm: establish ControlMaster.
            transport.warm(host)
            assert transport.check(host), "ControlMaster not alive after warm()"

            cm = transport._masters[host.ssh_alias]

            # 2. Run ping over the warm socket.
            ssh_cmd = cm.ssh_cmd(*_remote_exec_argv())
            proc = subprocess.Popen(
                ssh_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = proc.communicate(
                input=encode_line(make_ping()), timeout=30
            )
            assert proc.returncode == 0, f"ssh failed: {stderr.decode(errors='replace')}"

            # 3. Verify the pong response.
            responses = [
                decode_line(line)
                for line in stdout.decode("utf-8", errors="replace").splitlines()
                if line.strip()
            ]
            pongs = [m for m in responses if m.type == "pong"]
            assert pongs, f"no pong received; got: {responses}"
            assert pongs[0].payload.get("wire_version") == 1
            assert pongs[0].payload.get("hostname"), "pong missing hostname"

            # 4. Stop: tear down the ControlMaster.
            transport.stop(host)
            assert not transport.check(host), "ControlMaster still alive after stop()"
        finally:
            transport.stop(host)
