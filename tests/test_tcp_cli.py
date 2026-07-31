"""Tests for TCP mailbox daemon CLI integration.

Covers:
  - mailbox-daemon start/stop/status subcommands
  - --transport tcp routes through daemon socket
  - --transport auto auto-detects daemon and falls back
  - --help output for new subcommands
"""
from __future__ import annotations

import json
import socket
from pathlib import Path
from unittest import mock

import pytest

from codeagent.constants import TCP_DAEMON_PORT
from codeagent.tcp.cli import _cmd_start, _cmd_status, _cmd_stop, send_to_daemon


# ── Helpers ──────────────────────────────────────────────────────────────


def _ns(**kw) -> mock.MagicMock:
    """Build a mock argparse.Namespace with defaults."""
    defaults = {
        "host": "127.0.0.1",
        "port": TCP_DAEMON_PORT,
        "foreground": False,
        "pid_file": None,
        "mailbox_root": None,
        "subcmd": None,
    }
    defaults.update(kw)
    ns = mock.MagicMock()
    for k, v in defaults.items():
        setattr(ns, k, v)
    return ns


# ── 1. mailbox-daemon start/stop/status subcommands ─────────────────────


class TestDaemonStartStopStatus:
    """Direct CLI handler tests for start, stop, and status."""

    def test_start_forks_and_creates_pid(self, tmp_path: Path):
        """start writes a PID file and returns 0."""
        pid_file = tmp_path / "daemon.pid"
        args = _ns(subcmd="start", pid_file=str(pid_file), mailbox_root=str(tmp_path / "mb"))

        # Mock the fork and daemon run
        with (
            mock.patch("codeagent.tcp.cli.os.fork", return_value=42),
            mock.patch("codeagent.tcp.cli._process_alive", return_value=True),
            mock.patch("codeagent.tcp.cli.time.sleep"),
            mock.patch("codeagent.tcp.cli._write_pid") as mock_write,
        ):
            rc = _cmd_start(args)

        assert rc == 0
        mock_write.assert_called_once_with(pid_file, 42)

    def test_start_already_running(self, tmp_path: Path):
        """start with a running daemon returns 0 without forking."""
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("12345")
        args = _ns(subcmd="start", pid_file=str(pid_file))

        with (
            mock.patch("codeagent.tcp.cli._process_alive", return_value=True),
            mock.patch("codeagent.tcp.cli.os.fork") as mock_fork,
        ):
            rc = _cmd_start(args)

        assert rc == 0
        mock_fork.assert_not_called()

    def test_start_stale_pid(self, tmp_path: Path):
        """start with a stale PID file cleans up and forks."""
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("99999")
        args = _ns(subcmd="start", pid_file=str(pid_file), mailbox_root=str(tmp_path / "mb"))

        # First call: stale PID → False; second call: child alive → True
        alive_side = [False, True]

        with (
            mock.patch("codeagent.tcp.cli._process_alive", side_effect=alive_side),
            mock.patch("codeagent.tcp.cli.os.fork", return_value=43),
            mock.patch("codeagent.tcp.cli.time.sleep"),
            mock.patch("codeagent.tcp.cli._write_pid") as mock_write,
            mock.patch("codeagent.tcp.cli._remove_pid") as mock_rm,
        ):
            rc = _cmd_start(args)

        assert rc == 0
        mock_rm.assert_called_once_with(pid_file)
        mock_write.assert_called_once_with(pid_file, 43)

    def test_stop_running_daemon(self, tmp_path: Path):
        """stop sends SIGTERM and removes PID file."""
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("42")
        args = _ns(subcmd="stop", pid_file=str(pid_file))

        # 1 (initial alive) → SIGTERM → loop check 0 (dead, break) → final check (dead)
        alive_side = [True, False, False]

        with (
            mock.patch("codeagent.tcp.cli._read_pid", return_value=42),
            mock.patch("codeagent.tcp.cli._process_alive", side_effect=alive_side),
            mock.patch("codeagent.tcp.cli.os.kill") as mock_kill,
            mock.patch("codeagent.tcp.cli.time.sleep"),
            mock.patch("codeagent.tcp.cli._remove_pid") as mock_rm,
        ):
            rc = _cmd_stop(args)

        assert rc == 0
        mock_kill.assert_called_once_with(42, mock.ANY)  # SIGTERM
        mock_rm.assert_called_once()

    def test_stop_no_pid_file(self, tmp_path: Path):
        """stop with no PID file returns 0."""
        args = _ns(subcmd="stop", pid_file=str(tmp_path / "nope.pid"))
        with mock.patch("codeagent.tcp.cli._read_pid", return_value=None):
            rc = _cmd_stop(args)
        assert rc == 0

    def test_stop_stale_pid_file(self, tmp_path: Path):
        """stop with stale PID file cleans up and returns 0."""
        args = _ns(subcmd="stop", pid_file=str(tmp_path / "stale.pid"))

        with (
            mock.patch("codeagent.tcp.cli._read_pid", return_value=999),
            mock.patch("codeagent.tcp.cli._process_alive", return_value=False),
            mock.patch("codeagent.tcp.cli._remove_pid") as mock_rm,
        ):
            rc = _cmd_stop(args)

        assert rc == 0
        mock_rm.assert_called_once()

    def test_status_running_with_probe(self, tmp_path: Path):
        """status returns daemon info when process is alive and probe succeeds."""
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("42")
        args = _ns(subcmd="status", pid_file=str(pid_file))

        probe_result = {
            "running": True,
            "host": "127.0.0.1",
            "port": TCP_DAEMON_PORT,
            "connected_hosts": ["host-a"],
            "num_sessions": 1,
        }

        with (
            mock.patch("codeagent.tcp.cli._read_pid", return_value=42),
            mock.patch("codeagent.tcp.cli._process_alive", return_value=True),
            mock.patch("codeagent.tcp.cli._probe_daemon", return_value=probe_result),
        ):
            rc = _cmd_status(args)

        assert rc == 0

    def test_status_running_no_probe(self, tmp_path: Path):
        """status with alive process but unreachable probe returns warning."""
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("42")
        args = _ns(subcmd="status", pid_file=str(pid_file))

        with (
            mock.patch("codeagent.tcp.cli._read_pid", return_value=42),
            mock.patch("codeagent.tcp.cli._process_alive", return_value=True),
            mock.patch("codeagent.tcp.cli._probe_daemon", return_value=None),
        ):
            rc = _cmd_status(args)

        assert rc == 0

    def test_status_not_running(self, tmp_path: Path):
        """status with no PID file returns running=False."""
        args = _ns(subcmd="status", pid_file=str(tmp_path / "nope.pid"))

        with mock.patch("codeagent.tcp.cli._read_pid", return_value=None):
            rc = _cmd_status(args)

        assert rc == 0

    def test_status_stale_pid(self, tmp_path: Path):
        """status with stale PID file cleans up and returns running=False."""
        pid_file = tmp_path / "stale.pid"
        pid_file.write_text("999")
        args = _ns(subcmd="status", pid_file=str(pid_file))

        with (
            mock.patch("codeagent.tcp.cli._read_pid", return_value=999),
            mock.patch("codeagent.tcp.cli._process_alive", return_value=False),
            mock.patch("codeagent.tcp.cli._remove_pid") as mock_rm,
        ):
            rc = _cmd_status(args)

        assert rc == 0
        mock_rm.assert_called_once_with(pid_file)


# ── 2. --transport tcp routes through daemon socket ──────────────────────


class TestTransportTCP:
    """mailbox --transport tcp connects via daemon socket."""

    def test_local_transport_tcp_daemon_running(self, capsys):
        """With --transport tcp and daemon running, uses socket."""
        from codeagent.cli import _cmd_mailbox

        args = mock.MagicMock()
        args.mailbox_args = ["peek", "--session", "s1", "--agent", "a1"]
        args.host = None
        args.mailbox_root = None
        args.transport = "tcp"

        with (
            mock.patch("codeagent.tcp.cli._probe_daemon", return_value={"running": True}),
            mock.patch(
                "codeagent.tcp.cli.send_to_daemon",
                return_value=(0, '{"messages": []}\n', ""),
            ),
        ):
            rc = _cmd_mailbox(args)

        assert rc == 0
        out = capsys.readouterr().out
        assert "messages" in out

    def test_local_transport_tcp_daemon_not_running(self, capsys):
        """With --transport tcp and no daemon, returns error."""
        from codeagent.cli import _cmd_mailbox

        args = mock.MagicMock()
        args.mailbox_args = ["peek", "--session", "s1", "--agent", "a1"]
        args.host = None
        args.mailbox_root = None
        args.transport = "tcp"

        with mock.patch("codeagent.tcp.cli._probe_daemon", return_value=None):
            rc = _cmd_mailbox(args)

        assert rc == 1
        err = capsys.readouterr().err
        assert "daemon not running" in err

    def test_remote_transport_tcp_uses_daemon(self, capsys):
        """With --transport tcp and --host, uses daemon socket."""
        from codeagent.cli import _cmd_mailbox

        args = mock.MagicMock()
        args.mailbox_args = ["send", "--session", "s1", "--from", "main",
                             "--to", "worker", "--subject", "hi", "--body", "yo"]
        args.host = "remote-host"
        args.mailbox_root = None
        args.transport = "tcp"

        with mock.patch(
            "codeagent.tcp.cli.send_to_daemon",
            return_value=(0, "sent\n", ""),
        ):
            rc = _cmd_mailbox(args)

        assert rc == 0
        out = capsys.readouterr().out
        assert "sent" in out


# ── 3. --transport auto auto-detects daemon and falls back ───────────────


class TestTransportAuto:
    """mailbox --transport auto tries daemon, then falls back."""

    def test_auto_daemon_running_uses_tcp(self, capsys):
        """With auto transport and daemon running, uses socket."""
        from codeagent.cli import _cmd_mailbox

        args = mock.MagicMock()
        args.mailbox_args = ["peek", "--session", "s1", "--agent", "a1"]
        args.host = None
        args.mailbox_root = None
        args.transport = "auto"

        with (
            mock.patch("codeagent.tcp.cli._probe_daemon", return_value={"running": True}),
            mock.patch(
                "codeagent.tcp.cli.send_to_daemon",
                return_value=(0, '{"ok": true}\n', ""),
            ),
        ):
            rc = _cmd_mailbox(args)

        assert rc == 0

    def test_auto_daemon_not_running_falls_back(self, capsys):
        """With auto transport and no daemon, falls back to local CLI."""
        from codeagent.cli import _cmd_mailbox

        args = mock.MagicMock()
        args.mailbox_args = ["peek", "--session", "s1", "--agent", "a1"]
        args.host = None
        args.mailbox_root = None
        args.transport = "auto"

        with (
            mock.patch("codeagent.tcp.cli._probe_daemon", return_value=None),
            mock.patch("codeagent.mailbox.cli.main") as mock_mb,
        ):
            rc = _cmd_mailbox(args)

        assert rc == 0
        mock_mb.assert_called_once()

    def test_auto_remote_host_daemon_running_uses_tcp(self, capsys):
        """With auto transport + remote host and daemon running, uses TCP."""
        from codeagent.cli import _cmd_mailbox

        args = mock.MagicMock()
        args.mailbox_args = ["send", "--session", "s1", "--from", "main",
                             "--to", "worker", "--subject", "hi", "--body", "yo"]
        args.host = "remote-host"
        args.mailbox_root = None
        args.transport = "auto"

        with (
            mock.patch("codeagent.tcp.cli._probe_daemon", return_value={"running": True}),
            mock.patch(
                "codeagent.tcp.cli.send_to_daemon",
                return_value=(0, "sent\n", ""),
            ),
        ):
            rc = _cmd_mailbox(args)

        assert rc == 0

    def test_auto_remote_host_daemon_not_running_uses_ssh(self, capsys):
        """With auto transport + remote host and no daemon, falls back to SSH."""
        from codeagent.cli import _cmd_mailbox

        args = mock.MagicMock()
        args.mailbox_args = ["send", "--session", "s1", "--from", "main",
                             "--to", "worker", "--subject", "hi", "--body", "yo"]
        args.host = "remote-host"
        args.mailbox_root = None
        args.transport = "auto"

        fake_repo_map = mock.MagicMock()
        host_spec = mock.MagicMock()
        host_spec.ssh_alias = "remote-host"
        fake_repo_map.hosts = {"remote-host": host_spec}

        with (
            mock.patch("codeagent.tcp.cli._probe_daemon", return_value=None),
            mock.patch("codeagent.config.repo_map.load_repo_map", return_value=fake_repo_map),
            mock.patch("codeagent.domain.resolve_is_local", return_value=False),
            mock.patch("codeagent.transport.control_master.ControlMaster") as mock_cm,
            mock.patch("codeagent.transport.ssh._run_ssh_mailbox", return_value=(0, "sent\n", "")),
        ):
            mock_cm.return_value.is_alive.return_value = True
            mock_cm.return_value.ssh_cmd.return_value = ["ssh", "remote-host"]
            rc = _cmd_mailbox(args)

        assert rc == 0


# ── 4. --help output ────────────────────────────────────────────────────


class TestHelpOutput:
    """CLI --help output for new subcommands and options."""

    def test_mailbox_daemon_help(self, capsys):
        """mailbox-daemon --help shows daemon_args."""
        from codeagent.cli import main

        with pytest.raises(SystemExit, match="0"):
            main(["mailbox-daemon", "--help"])
        out = capsys.readouterr().out
        # The mailbox-daemon subparser uses REMAINDER for daemon_args,
        # so --help shows the subparser itself, not the sub-subcommands.
        assert "daemon_args" in out or "Arguments" in out

    def test_mailbox_daemon_start_help(self, capsys):
        """mailbox-daemon start --help shows --host, --port, --foreground."""
        from codeagent.cli import main

        # _cmd_mailbox_daemon catches SystemExit from argparse --help
        rc = main(["mailbox-daemon", "start", "--help"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "--host" in out
        assert "--port" in out
        assert "--foreground" in out

    def test_mailbox_transport_help(self, capsys):
        """mailbox --help shows --transport option."""
        from codeagent.cli import main

        with pytest.raises(SystemExit, match="0"):
            main(["mailbox", "--help"])
        out = capsys.readouterr().out
        assert "--transport" in out
        assert "auto" in out
        assert "tcp" in out
        assert "file" in out


# ── 5. send_to_daemon helper ─────────────────────────────────────────────


class TestSendToDaemon:
    """send_to_daemon: socket communication with the daemon."""

    def test_send_success(self):
        """send_to_daemon returns parsed response on success."""
        response = json.dumps({"exit_code": 0, "stdout": "ok\n", "stderr": ""}).encode() + b"\n"

        fake_sock = mock.MagicMock()
        fake_sock.recv.side_effect = [response]

        with mock.patch("codeagent.tcp.cli.socket.create_connection", return_value=fake_sock):
            rc, out, err = send_to_daemon(["peek", "--session", "s1", "--agent", "a1"])

        assert rc == 0
        assert "ok" in out

    def test_send_connection_refused(self):
        """send_to_daemon returns error on connection refused."""
        with mock.patch(
            "codeagent.tcp.cli.socket.create_connection",
            side_effect=ConnectionRefusedError,
        ):
            rc, out, err = send_to_daemon(["peek"])

        assert rc == 1
        assert "cannot connect" in err

    def test_send_no_response(self):
        """send_to_daemon returns error when daemon sends nothing."""
        fake_sock = mock.MagicMock()
        fake_sock.recv.return_value = b""

        with mock.patch("codeagent.tcp.cli.socket.create_connection", return_value=fake_sock):
            rc, out, err = send_to_daemon(["peek"])

        assert rc == 1
        assert "no response" in err

    def test_send_includes_mailbox_root(self):
        """send_to_daemon includes mailbox_root in the request."""
        response = json.dumps({"exit_code": 0, "stdout": "", "stderr": ""}).encode() + b"\n"

        fake_sock = mock.MagicMock()
        fake_sock.recv.return_value = response

        with mock.patch("codeagent.tcp.cli.socket.create_connection", return_value=fake_sock):
            send_to_daemon(["peek"], mailbox_root="/custom/root")

        sent = fake_sock.sendall.call_args[0][0]
        req = json.loads(sent.decode().strip())
        assert req["mailbox_root"] == "/custom/root"


# ── 6. Daemon entry-point ───────────────────────────────────────────────


class TestDaemonMain:
    """tcp.cli.main entry-point dispatch."""

    def test_main_no_subcmd(self):
        """main() with no subcmd returns 1."""
        from codeagent.tcp.cli import main

        rc = main([])
        assert rc == 1

    def test_main_start(self):
        """main(['start']) dispatches to _cmd_start."""
        from codeagent.tcp.cli import main

        with mock.patch("codeagent.tcp.cli._cmd_start", return_value=0) as mock_start:
            rc = main(["start"])

        assert rc == 0
        mock_start.assert_called_once()

    def test_main_stop(self):
        """main(['stop']) dispatches to _cmd_stop."""
        from codeagent.tcp.cli import main

        with mock.patch("codeagent.tcp.cli._cmd_stop", return_value=0) as mock_stop:
            rc = main(["stop"])

        assert rc == 0
        mock_stop.assert_called_once()

    def test_main_status(self):
        """main(['status']) dispatches to _cmd_status."""
        from codeagent.tcp.cli import main

        with mock.patch("codeagent.tcp.cli._cmd_status", return_value=0) as mock_status:
            rc = main(["status"])

        assert rc == 0
        mock_status.assert_called_once()


# ── 7. Integration: full CLI dispatch ────────────────────────────────────


class TestCLIDispatch:
    """End-to-end dispatch from main() through to handlers."""

    def test_mailbox_daemon_dispatch(self):
        """codeagent mailbox-daemon start dispatches correctly."""
        from codeagent.cli import main

        with mock.patch("codeagent.tcp.cli.main", return_value=0) as mock_daemon:
            rc = main(["mailbox-daemon", "start"])

        assert rc == 0
        mock_daemon.assert_called_once_with(["start"])

    def test_mailbox_daemon_stop(self):
        """codeagent mailbox-daemon stop dispatches correctly."""
        from codeagent.cli import main

        with mock.patch("codeagent.tcp.cli.main", return_value=0) as mock_daemon:
            rc = main(["mailbox-daemon", "stop"])

        assert rc == 0
        mock_daemon.assert_called_once_with(["stop"])

    def test_mailbox_daemon_status(self):
        """codeagent mailbox-daemon status dispatches correctly."""
        from codeagent.cli import main

        with mock.patch("codeagent.tcp.cli.main", return_value=0) as mock_daemon:
            rc = main(["mailbox-daemon", "status"])

        assert rc == 0
        mock_daemon.assert_called_once_with(["status"])

    def test_mailbox_default_transport(self):
        """mailbox subcommand defaults to --transport auto."""
        from codeagent.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["mailbox", "peek"])
        assert args.transport == "auto"

    def test_mailbox_explicit_transport(self):
        """mailbox --transport tcp is parsed correctly."""
        from codeagent.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["mailbox", "--transport", "tcp", "peek"])
        assert args.transport == "tcp"
