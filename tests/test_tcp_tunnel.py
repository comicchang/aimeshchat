"""Tests for codeagent.tcp.tunnel — SSH tunnel management."""
from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import subprocess
import time
from unittest import mock

import pytest

from codeagent.constants import TCP_DAEMON_PORT, TCP_PORT_BASE
from codeagent.tcp.tunnel import (
    PortAllocator,
    TunnelDirection,
    TunnelInfo,
    TunnelManager,
    TunnelState,
    _TunnelEntry,
    build_ssh_tunnel_cmd,
)


# ─────────────────────────────────────────────────────────────────────────────
# PortAllocator — deterministic hash
# ─────────────────────────────────────────────────────────────────────────────


class TestPortAllocatorDeterministic:
    """Remote port allocation is stable and within the expected range."""

    def test_local_port_is_constant(self):
        assert PortAllocator.local_port() == TCP_DAEMON_PORT == 5555

    def test_remote_port_range(self):
        """Remote port must be in [TCP_PORT_BASE, TCP_PORT_BASE + 9999]."""
        for alias in ("alpha", "beta", "gamma", "delta", "worker-1", "prod-box"):
            port = PortAllocator.remote_port(alias)
            assert TCP_PORT_BASE <= port < TCP_PORT_BASE + 10_000

    def test_remote_port_deterministic(self):
        """Same alias always yields the same port."""
        for alias in ("host-A", "host-B", "long-alias-name-here"):
            first = PortAllocator.remote_port(alias)
            second = PortAllocator.remote_port(alias)
            assert first == second

    def test_remote_port_matches_formula(self):
        alias = "test-host"
        digest = hashlib.sha256(alias.encode()).hexdigest()
        expected = TCP_PORT_BASE + (int(digest[:8], 16) % 10_000)
        assert PortAllocator.remote_port(alias) == expected

    @pytest.mark.parametrize("alias", ["a", "bb", "ccc", "worker-99", "abcdefghijklmnop"])
    def test_various_aliases_in_range(self, alias: str):
        port = PortAllocator.remote_port(alias)
        assert TCP_PORT_BASE <= port < TCP_PORT_BASE + 10_000

    def test_different_aliases_can_differ(self):
        """Two distinct aliases produce (usually) different ports."""
        ports = {PortAllocator.remote_port(f"host-{i}") for i in range(100)}
        # With 100 hosts and a 10k range, collisions are extremely unlikely.
        # Allow a few collisions but not all the same.
        assert len(ports) > 50


# ─────────────────────────────────────────────────────────────────────────────
# PortAllocator — conflict detection
# ─────────────────────────────────────────────────────────────────────────────


class TestPortConflictDetection:
    """is_port_available correctly detects occupied ports."""

    def test_available_port_returns_true(self):
        # Find a random free port.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]
        # After closing, it should be available.
        assert PortAllocator.is_port_available(free_port) is True

    def test_occupied_port_returns_false(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            occupied_port = s.getsockname()[1]
            assert PortAllocator.is_port_available(occupied_port) is False

    def test_negative_port_returns_false(self):
        assert PortAllocator.is_port_available(-1) is False


# ─────────────────────────────────────────────────────────────────────────────
# SSH command construction
# ─────────────────────────────────────────────────────────────────────────────


class TestBuildSSHTunnelCmd:
    """build_ssh_tunnel_cmd produces the expected argv."""

    def test_local_direction(self):
        cmd = build_ssh_tunnel_cmd("myhost", direction=TunnelDirection.LOCAL)
        assert cmd[0] == "ssh"
        assert "-L" in cmd
        idx = cmd.index("-L")
        spec = cmd[idx + 1]
        lp = TCP_DAEMON_PORT
        rp = PortAllocator.remote_port("myhost")
        assert spec == f"127.0.0.1:{lp}:127.0.0.1:{rp}"
        assert "-N" in cmd
        assert "-T" in cmd
        assert "ExitOnForwardFailure=yes" in " ".join(cmd)
        assert "ServerAliveInterval=15" in " ".join(cmd)
        assert cmd[-1] == "myhost"

    def test_remote_direction(self):
        cmd = build_ssh_tunnel_cmd("worker", direction=TunnelDirection.REMOTE)
        assert "-R" in cmd
        idx = cmd.index("-R")
        spec = cmd[idx + 1]
        lp = TCP_DAEMON_PORT
        rp = PortAllocator.remote_port("worker")
        assert spec == f"127.0.0.1:{rp}:127.0.0.1:{lp}"

    def test_custom_ports(self):
        cmd = build_ssh_tunnel_cmd(
            "h",
            direction=TunnelDirection.LOCAL,
            local_port=9000,
            remote_port=9001,
        )
        idx = cmd.index("-L")
        assert cmd[idx + 1] == "127.0.0.1:9000:127.0.0.1:9001"

    def test_custom_ssh_bin(self):
        cmd = build_ssh_tunnel_cmd(
            "h",
            direction=TunnelDirection.LOCAL,
            ssh_bin="/usr/local/bin/ssh",
        )
        assert cmd[0] == "/usr/local/bin/ssh"


# ─────────────────────────────────────────────────────────────────────────────
# TunnelInfo dataclass
# ─────────────────────────────────────────────────────────────────────────────


class TestTunnelInfo:
    def test_defaults(self):
        info = TunnelInfo(
            host_alias="h",
            direction=TunnelDirection.LOCAL,
            state=TunnelState.STOPPED,
            local_port=5555,
            remote_port=15555,
        )
        assert info.pid is None
        assert info.uptime == 0.0
        assert info.restarts == 0


# ─────────────────────────────────────────────────────────────────────────────
# _TunnelEntry — lifecycle (mocked subprocess)
# ─────────────────────────────────────────────────────────────────────────────


class TestTunnelEntryLifecycle:
    """Unit tests for _TunnelEntry with a mocked SSH subprocess."""

    def test_start_transitions_to_running(self):
        entry = _TunnelEntry(
            host_alias="h1",
            direction=TunnelDirection.LOCAL,
            ssh_bin="ssh",
            health_interval=30.0,
        )
        mock_proc = mock.MagicMock(spec=subprocess.Popen)
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None
        with mock.patch("subprocess.Popen", return_value=mock_proc):
            entry.start()
        assert entry.state is TunnelState.RUNNING
        assert entry.pid == 12345

    def test_kill_transitions_to_stopped(self):
        entry = _TunnelEntry(
            host_alias="h1",
            direction=TunnelDirection.LOCAL,
            ssh_bin="ssh",
            health_interval=30.0,
        )
        mock_proc = mock.MagicMock(spec=subprocess.Popen)
        mock_proc.pid = 100
        mock_proc.poll.return_value = None
        with mock.patch("subprocess.Popen", return_value=mock_proc):
            entry.start()
        entry.kill()
        assert entry.state is TunnelState.STOPPED
        assert entry.pid is None
        mock_proc.terminate.assert_called_once()

    def test_kill_is_idempotent(self):
        entry = _TunnelEntry(
            host_alias="h1",
            direction=TunnelDirection.LOCAL,
            ssh_bin="ssh",
            health_interval=30.0,
        )
        entry.kill()  # no subprocess, should not raise
        assert entry.state is TunnelState.STOPPED

    def test_is_process_alive_delegates_to_poll(self):
        entry = _TunnelEntry(
            host_alias="h1",
            direction=TunnelDirection.LOCAL,
            ssh_bin="ssh",
            health_interval=30.0,
        )
        assert entry.is_process_alive() is False

        mock_proc = mock.MagicMock(spec=subprocess.Popen)
        mock_proc.pid = 999
        mock_proc.poll.return_value = None
        with mock.patch("subprocess.Popen", return_value=mock_proc):
            entry.start()
        assert entry.is_process_alive() is True

        mock_proc.poll.return_value = 1  # exited
        assert entry.is_process_alive() is False

    def test_info_snapshot(self):
        entry = _TunnelEntry(
            host_alias="alpha",
            direction=TunnelDirection.REMOTE,
            ssh_bin="ssh",
            health_interval=30.0,
        )
        mock_proc = mock.MagicMock(spec=subprocess.Popen)
        mock_proc.pid = 42
        mock_proc.poll.return_value = None
        with mock.patch("subprocess.Popen", return_value=mock_proc):
            entry.start()

        info = entry.info()
        assert isinstance(info, TunnelInfo)
        assert info.host_alias == "alpha"
        assert info.direction is TunnelDirection.REMOTE
        assert info.state is TunnelState.RUNNING
        assert info.local_port == TCP_DAEMON_PORT
        assert info.remote_port == PortAllocator.remote_port("alpha")
        assert info.pid == 42
        assert info.uptime >= 0.0

    def test_start_failure_sets_failed(self):
        entry = _TunnelEntry(
            host_alias="h1",
            direction=TunnelDirection.LOCAL,
            ssh_bin="nonexistent-ssh-binary",
            health_interval=30.0,
        )
        entry.start()
        assert entry.state is TunnelState.FAILED
        assert entry.pid is None

    def test_reconnect_increments_restarts(self):
        entry = _TunnelEntry(
            host_alias="h1",
            direction=TunnelDirection.LOCAL,
            ssh_bin="ssh",
            health_interval=30.0,
        )
        mock_proc = mock.MagicMock(spec=subprocess.Popen)
        mock_proc.pid = 500
        mock_proc.poll.return_value = None

        with mock.patch("subprocess.Popen", return_value=mock_proc), \
             mock.patch("time.sleep"):  # skip backoff sleep
            entry.start()
            entry.reconnect()

        assert entry._restarts == 1
        assert entry.state is TunnelState.RUNNING

    def test_backoff_doubles(self):
        entry = _TunnelEntry(
            host_alias="h1",
            direction=TunnelDirection.LOCAL,
            ssh_bin="ssh",
            health_interval=30.0,
        )
        assert entry._backoff == 1.0

        mock_proc = mock.MagicMock(spec=subprocess.Popen)
        mock_proc.pid = 600
        mock_proc.poll.return_value = None

        sleep_calls: list[float] = []
        orig_sleep = time.sleep

        def capture_sleep(dur: float) -> None:
            sleep_calls.append(dur)

        with mock.patch("subprocess.Popen", return_value=mock_proc), \
             mock.patch("time.sleep", side_effect=capture_sleep):
            entry.start()
            entry.reconnect()   # backoff 1.0 → 2.0
            entry.reconnect()   # backoff 2.0 → 4.0

        assert sleep_calls == [1.0, 2.0]
        assert entry._backoff == 4.0

    def test_backoff_capped_at_30s(self):
        entry = _TunnelEntry(
            host_alias="h1",
            direction=TunnelDirection.LOCAL,
            ssh_bin="ssh",
            health_interval=30.0,
        )
        mock_proc = mock.MagicMock(spec=subprocess.Popen)
        mock_proc.pid = 700
        mock_proc.poll.return_value = None

        sleep_calls: list[float] = []

        def capture_sleep(dur: float) -> None:
            sleep_calls.append(dur)

        with mock.patch("subprocess.Popen", return_value=mock_proc), \
             mock.patch("time.sleep", side_effect=capture_sleep):
            entry.start()
            for _ in range(10):
                entry.reconnect()

        # Last sleep should be 30.0 (cap).
        assert sleep_calls[-1] == 30.0
        # All sleeps should be <= 30.
        assert all(s <= 30.0 for s in sleep_calls)


# ─────────────────────────────────────────────────────────────────────────────
# TunnelManager — high-level lifecycle (mocked subprocess)
# ─────────────────────────────────────────────────────────────────────────────


def _async_run(coro):
    """Helper to run a coroutine in tests."""
    return asyncio.run(coro)


class TestTunnelManagerLifecycle:
    """Test TunnelManager with mocked SSH processes."""

    def test_ensure_creates_tunnel(self):
        mgr = TunnelManager()
        mock_proc = mock.MagicMock(spec=subprocess.Popen)
        mock_proc.pid = 1000
        mock_proc.poll.return_value = None

        with mock.patch("subprocess.Popen", return_value=mock_proc):
            _async_run(mgr.ensure("host-a"))

        status = _async_run(mgr.status())
        assert "host-a" in status
        assert status["host-a"].state is TunnelState.RUNNING

    def test_ensure_idempotent(self):
        mgr = TunnelManager()
        mock_proc = mock.MagicMock(spec=subprocess.Popen)
        mock_proc.pid = 2000
        mock_proc.poll.return_value = None

        with mock.patch("subprocess.Popen", return_value=mock_proc):
            _async_run(mgr.ensure("host-a"))
            _async_run(mgr.ensure("host-a"))  # second call — no-op

        # Only one tunnel entry.
        status = _async_run(mgr.status())
        assert len(status) == 1

    def test_teardown_removes_tunnel(self):
        mgr = TunnelManager()
        mock_proc = mock.MagicMock(spec=subprocess.Popen)
        mock_proc.pid = 3000
        mock_proc.poll.return_value = None

        with mock.patch("subprocess.Popen", return_value=mock_proc):
            _async_run(mgr.ensure("host-b"))

        result = _async_run(mgr.teardown("host-b"))
        assert result is True
        status = _async_run(mgr.status())
        assert "host-b" not in status

    def test_teardown_nonexistent_returns_false(self):
        mgr = TunnelManager()
        result = _async_run(mgr.teardown("ghost"))
        assert result is False

    def test_teardown_all(self):
        mgr = TunnelManager()
        mock_proc = mock.MagicMock(spec=subprocess.Popen)
        mock_proc.pid = 4000
        mock_proc.poll.return_value = None

        with mock.patch("subprocess.Popen", return_value=mock_proc):
            _async_run(mgr.ensure("a"))
            _async_run(mgr.ensure("b"))
            _async_run(mgr.ensure("c"))

        count = _async_run(mgr.teardown_all())
        assert count == 3
        status = _async_run(mgr.status())
        assert len(status) == 0

    def test_status_returns_tunnel_info(self):
        mgr = TunnelManager()
        mock_proc = mock.MagicMock(spec=subprocess.Popen)
        mock_proc.pid = 5000
        mock_proc.poll.return_value = None

        with mock.patch("subprocess.Popen", return_value=mock_proc):
            _async_run(mgr.ensure("worker-1", direction=TunnelDirection.REMOTE))

        status = _async_run(mgr.status())
        info = status["worker-1"]
        assert isinstance(info, TunnelInfo)
        assert info.direction is TunnelDirection.REMOTE
        assert info.local_port == TCP_DAEMON_PORT
        assert info.remote_port == PortAllocator.remote_port("worker-1")

    def test_ensure_replaces_dead_tunnel(self):
        mgr = TunnelManager()
        proc_alive = mock.MagicMock(spec=subprocess.Popen)
        proc_alive.pid = 6000
        proc_alive.poll.return_value = None

        proc_dead = mock.MagicMock(spec=subprocess.Popen)
        proc_dead.pid = 6001
        proc_dead.poll.return_value = 1  # dead

        with mock.patch("subprocess.Popen", side_effect=[proc_alive]):
            _async_run(mgr.ensure("host-x"))

        # Simulate death.
        entry = mgr._tunnels["host-x"]
        entry._proc = proc_dead

        proc_new = mock.MagicMock(spec=subprocess.Popen)
        proc_new.pid = 6002
        proc_new.poll.return_value = None

        with mock.patch("subprocess.Popen", return_value=proc_new):
            _async_run(mgr.ensure("host-x"))  # should replace

        status = _async_run(mgr.status())
        assert status["host-x"].state is TunnelState.RUNNING


# ─────────────────────────────────────────────────────────────────────────────
# TunnelManager — health check loop
# ─────────────────────────────────────────────────────────────────────────────


class TestHealthCheckLoop:
    def test_dead_tunnel_triggers_reconnect(self):
        """When a tunnel process dies, health_loop reconnects it."""
        mgr = TunnelManager(health_interval=0.05)  # fast loop for testing

        mock_proc = mock.MagicMock(spec=subprocess.Popen)
        mock_proc.pid = 7000
        mock_proc.poll.return_value = None

        with mock.patch("subprocess.Popen", return_value=mock_proc):
            _async_run(mgr.ensure("host-y"))

        # Simulate the process dying.
        entry = mgr._tunnels["host-y"]
        mock_proc.poll.return_value = 1  # exited

        mock_proc2 = mock.MagicMock(spec=subprocess.Popen)
        mock_proc2.pid = 7001
        mock_proc2.poll.return_value = None

        async def _run():
            with mock.patch("subprocess.Popen", return_value=mock_proc2), \
                 mock.patch("time.sleep"):  # skip backoff
                loop_task = asyncio.ensure_future(mgr.run_health_loop())
                await asyncio.sleep(0.15)
                loop_task.cancel()
                try:
                    await loop_task
                except asyncio.CancelledError:
                    pass

        _async_run(_run())

        status = _async_run(mgr.status())
        assert status["host-y"].state is TunnelState.RUNNING


# ─────────────────────────────────────────────────────────────────────────────
# Concurrent tunnels
# ─────────────────────────────────────────────────────────────────────────────


class TestConcurrentTunnels:
    """Multiple tunnels can coexist."""

    def test_many_tunnels(self):
        mgr = TunnelManager()
        mock_proc = mock.MagicMock(spec=subprocess.Popen)
        mock_proc.pid = 8000
        mock_proc.poll.return_value = None

        with mock.patch("subprocess.Popen", return_value=mock_proc):
            for i in range(10):
                _async_run(mgr.ensure(f"host-{i}"))

        status = _async_run(mgr.status())
        assert len(status) == 10
        for i in range(10):
            assert f"host-{i}" in status
            assert status[f"host-{i}"].state is TunnelState.RUNNING

    def test_partial_teardown(self):
        mgr = TunnelManager()
        mock_proc = mock.MagicMock(spec=subprocess.Popen)
        mock_proc.pid = 9000
        mock_proc.poll.return_value = None

        with mock.patch("subprocess.Popen", return_value=mock_proc):
            for i in range(5):
                _async_run(mgr.ensure(f"worker-{i}"))

        _async_run(mgr.teardown("worker-0"))
        _async_run(mgr.teardown("worker-3"))

        status = _async_run(mgr.status())
        assert len(status) == 3
        assert "worker-0" not in status
        assert "worker-3" not in status
        assert "worker-1" in status


# ─────────────────────────────────────────────────────────────────────────────
# Integration test — real SSH localhost tunnel (gated)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="integration tests disabled (use --run-integration)",
)
class TestLocalhostSSHTunnel:
    """Integration test that opens a real SSH tunnel to localhost.

    Requires ``ssh localhost`` to work without a password (SSH key configured).
    """

    def test_local_forward_roundtrip(self, tmp_path):
        """Bind a local port via -L, verify we can connect to it through ssh."""
        # Start a simple TCP listener on a random port to act as the "remote daemon".
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        remote_port = listener.getsockname()[1]

        # Pick a local port for the tunnel.
        local_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        local_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        local_sock.bind(("127.0.0.1", 0))
        local_port = local_sock.getsockname()[1]
        local_sock.close()

        cmd = build_ssh_tunnel_cmd(
            "localhost",
            direction=TunnelDirection.LOCAL,
            local_port=local_port,
            remote_port=remote_port,
            ssh_bin="ssh",
        )
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # Wait for the tunnel to be ready.
            time.sleep(2)
            assert proc.poll() is None, "ssh process exited early"

            # Connect through the tunnel.
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.settimeout(5)
            try:
                client.connect(("127.0.0.1", local_port))
                # Accept on the listener side.
                conn, _ = listener.accept()
                conn.sendall(b"hello")
                data = client.recv(32)
                assert data == b"hello"
                conn.close()
            finally:
                client.close()
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            listener.close()
