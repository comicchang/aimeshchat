"""Tests for codeagent.transport.local and codeagent.transport.ssh."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from codeagent.domain import HostSpec, RunRequest, RunResult
from codeagent.transport.base import TransportError
from codeagent.transport.control_master import ControlMaster, list_sockets, socket_path, stop_by_alias, stop_all
from codeagent.transport.local import LocalTransport, _run_wire
from codeagent.transport.ssh import SSHTransport, _run_ssh_wire, _is_ssh_error
from codeagent.transport.relay import RelayTransport
from codeagent.wire.protocol import (
    MSG_ACCEPTED,
    MSG_ERROR,
    MSG_READY,
    MSG_RESULT,
    MSG_SESSION,
    encode_line,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run_request(**overrides: Any) -> RunRequest:
    """Build a RunRequest with sensible defaults."""
    defaults = dict(
        task="do stuff",
        workdir="/tmp",
        backend="opencode",
        agent=None,
        model=None,
        skip_permissions=True,
        session_key=None,
        new_session=False,
    )
    defaults.update(overrides)
    return RunRequest(**defaults)


def _remote_exec_responds(ready: bool = True, session_id: str | None = None) -> list[bytes]:
    """Build a sequence of JSONL response lines from a remote exec helper."""
    lines: list[bytes] = []
    if ready:
        lines.append(encode_line({"type": MSG_READY, "wire_version": 1, "package_version": "0.1.0"}))
    lines.append(encode_line({"type": MSG_ACCEPTED, "wire_version": 1}))
    if session_id:
        lines.append(encode_line({"type": MSG_SESSION, "id": session_id}))
    lines.append(encode_line({"type": MSG_RESULT, "stdout": "ok", "stderr": "", "exit_code": 0}))
    return lines


def _mock_popen_success(session_id: str | None = None, returncode: int = 0):
    """Return a MagicMock Popen that succeeds."""
    mock_proc = MagicMock()
    stdout_lines = _remote_exec_responds(session_id=session_id)
    stdout_bytes = b"".join(stdout_lines)
    mock_proc.communicate.return_value = (stdout_bytes, b"")
    mock_proc.returncode = returncode
    return mock_proc


def _mock_popen_timeout():
    """Return a MagicMock Popen that times out on communicate()."""
    mock_proc = MagicMock()
    mock_proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=5)
    mock_proc.kill = MagicMock()
    mock_proc.wait = MagicMock()
    return mock_proc


def _mock_popen_exit255_stderr(stderr: str = "Connection refused"):
    """Return a MagicMock Popen that exits 255 with SSH error on stderr."""
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (b"", stderr.encode("utf-8"))
    mock_proc.returncode = 255
    return mock_proc


# ---------------------------------------------------------------------------
# ControlMaster tests
# ---------------------------------------------------------------------------


class TestControlMaster:
    """Tests for ControlMaster socket management."""

    def test_socket_path_deterministic(self):
        """Same alias always produces same socket path."""
        p1 = socket_path("myhost")
        p2 = socket_path("myhost")
        assert p1 == p2

    def test_socket_path_different_alias(self):
        """Different aliases produce different socket paths."""
        p1 = socket_path("host-a")
        p2 = socket_path("host-b")
        assert p1 != p2

    def test_list_sockets_empty_dir(self, tmp_path: Path):
        """list_sockets() returns empty list when no sockets exist."""
        with patch("codeagent.transport.control_master._socket_dir", return_value=tmp_path):
            assert list_sockets() == []

    def test_list_sockets_finds_socks(self, tmp_path: Path):
        """list_sockets() finds .sock files and reads .meta for alias."""
        sock = tmp_path / "abc123.sock"
        sock.touch()
        meta = tmp_path / "abc123.meta"
        meta.write_text('{"alias": "myhost", "created": 1234567890.0}')
        with patch("codeagent.transport.control_master._socket_dir", return_value=tmp_path):
            sockets = list_sockets()
            assert len(sockets) == 1
            assert sockets[0] == ("myhost", sock)

    def test_list_sockets_socks_without_meta(self, tmp_path: Path):
        """list_sockets() falls back to stem as alias when .meta missing."""
        sock = tmp_path / "abc123.sock"
        sock.touch()
        with patch("codeagent.transport.control_master._socket_dir", return_value=tmp_path):
            sockets = list_sockets()
            assert len(sockets) == 1
            assert sockets[0] == ("abc123", sock)

    def test_list_sockets_ignores_non_socks(self, tmp_path: Path):
        """list_sockets() ignores non-.sock files."""
        (tmp_path / "somefile.txt").touch()
        with patch("codeagent.transport.control_master._socket_dir", return_value=tmp_path):
            assert list_sockets() == []

    @patch("shutil.which", return_value="/usr/bin/ssh")
    @patch("subprocess.run")
    def test_create_uses_ssh_o_check(self, mock_run: MagicMock, mock_which: MagicMock, tmp_path: Path):
        """is_alive() uses ssh -O check."""
        mock_run.return_value = MagicMock(returncode=0)
        cm = ControlMaster("testhost", ssh_bin="/usr/bin/ssh")
        # Force socket path to tmp
        cm._socket = tmp_path / "test.sock"
        cm._socket.touch()
        cm.is_alive()
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "-O" in args
        assert "check" in args

    @patch("shutil.which", return_value="/usr/bin/ssh")
    @patch("subprocess.run")
    def test_stop_uses_ssh_o_exit(self, mock_run: MagicMock, mock_which: MagicMock, tmp_path: Path):
        """stop() uses ssh -O exit."""
        mock_run.return_value = MagicMock(returncode=0)
        cm = ControlMaster("testhost", ssh_bin="/usr/bin/ssh")
        cm._socket = tmp_path / "test.sock"
        cm._socket.touch()
        cm.stop()
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "-O" in args
        assert "exit" in args

    def test_ssh_cmd_builds_correct_argv(self, tmp_path: Path):
        """ssh_cmd() builds correct argv with -S socket."""
        with patch("shutil.which", return_value="/usr/bin/ssh"):
            cm = ControlMaster("testhost", ssh_bin="/usr/bin/ssh")
            cm._socket = tmp_path / "test.sock"
            cmd = cm.ssh_cmd("python3", "-m", "codeagent.remote_exec")
            assert cmd == [
                "/usr/bin/ssh",
                "-S", str(cm._socket),
                "testhost",
                "python3", "-m", "codeagent.remote_exec",
            ]

        @patch("shutil.which", return_value="/usr/bin/ssh")
        @patch("subprocess.run")
        def test_create_writes_meta_file(self, mock_run: MagicMock, mock_which: MagicMock, tmp_path: Path):
            """create() writes a .meta file alongside the socket."""
            mock_run.return_value = MagicMock(returncode=0)
            cm = ControlMaster("testhost", ssh_bin="/usr/bin/ssh")
            cm._socket = tmp_path / "test.sock"
            cm._socket.touch()
            # Patch _check to return non-alive so create proceeds.
            with patch.object(cm, "_check", return_value=1):
                cm.create()
            meta = tmp_path / "test.meta"
            assert meta.exists()
            import json
            info = json.loads(meta.read_text())
            assert info["alias"] == "testhost"
            assert "created" in info

        def test_stop_removes_meta_file(self, tmp_path: Path):
            """stop() removes the .meta file alongside the socket."""
            cm = ControlMaster("testhost", ssh_bin="/usr/bin/ssh")
            cm._socket = tmp_path / "test.sock"
            cm._socket.touch()
            meta = tmp_path / "test.meta"
            meta.write_text('{"alias": "testhost"}')
            with patch("shutil.which", return_value="/usr/bin/ssh"), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0)):
                cm.stop()
            assert not meta.exists()


    # ---------------------------------------------------------------------------
    # stop_by_alias / stop_all tests
    # ---------------------------------------------------------------------------


    class TestStopByAlias:
        """Tests for stop_by_alias() and stop_all()."""

        @patch("shutil.which", return_value="/usr/bin/ssh")
        @patch("subprocess.run")
        def test_stop_by_alias_finds_and_stops(self, mock_run: MagicMock, mock_which: MagicMock, tmp_path: Path):
            """stop_by_alias() finds socket via .meta and sends ssh -O exit."""
            sock = tmp_path / "abc123.sock"
            sock.touch()
            meta = tmp_path / "abc123.meta"
            meta.write_text('{"alias": "myhost", "created": 1234567890.0}')
            mock_run.return_value = MagicMock(returncode=0)

            with patch("codeagent.transport.control_master._socket_dir", return_value=tmp_path):
                result = stop_by_alias("myhost")

            assert result is True
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert "-O" in args
            assert "exit" in args
            assert not sock.exists()
            assert not meta.exists()

        def test_stop_by_alias_no_match(self, tmp_path: Path):
            """stop_by_alias() returns False when no .meta matches."""
            sock = tmp_path / "abc123.sock"
            sock.touch()
            meta = tmp_path / "abc123.meta"
            meta.write_text('{"alias": "otherhost", "created": 1234567890.0}')

            with patch("codeagent.transport.control_master._socket_dir", return_value=tmp_path):
                result = stop_by_alias("myhost")

            assert result is False

        def test_stop_by_alias_no_meta(self, tmp_path: Path):
            """stop_by_alias() returns False when socket has no .meta file."""
            sock = tmp_path / "abc123.sock"
            sock.touch()

            with patch("codeagent.transport.control_master._socket_dir", return_value=tmp_path):
                result = stop_by_alias("myhost")

            assert result is False

        @patch("shutil.which", return_value="/usr/bin/ssh")
        @patch("subprocess.run")
        def test_stop_all_stops_all(self, mock_run: MagicMock, mock_which: MagicMock, tmp_path: Path):
            """stop_all() stops all sockets with .meta files."""
            for name, alias in [("aaa", "host-a"), ("bbb", "host-b")]:
                (tmp_path / f"{name}.sock").touch()
                (tmp_path / f"{name}.meta").write_text(f'{{"alias": "{alias}"}}')
            mock_run.return_value = MagicMock(returncode=0)

            with patch("codeagent.transport.control_master._socket_dir", return_value=tmp_path):
                stop_all()

            assert mock_run.call_count == 2
            # Both sockets and metas should be cleaned up.
            assert list(tmp_path.glob("*.sock")) == []
            assert list(tmp_path.glob("*.meta")) == []

        def test_stop_all_empty_dir(self, tmp_path: Path):
            """stop_all() is a no-op on empty dir."""
            with patch("codeagent.transport.control_master._socket_dir", return_value=tmp_path):
                stop_all()  # should not raise


# ---------------------------------------------------------------------------
# _run_wire (local) tests
# ---------------------------------------------------------------------------


class TestRunWire:
    """Tests for the shared _run_wire function."""

    @patch("subprocess.Popen")
    def test_success_basic(self, mock_popen: MagicMock):
        """Successful execution returns correct RunResult."""
        mock_proc = _mock_popen_success()
        mock_popen.return_value = mock_proc
        result = _run_wire(
            ["python3", "-m", "codeagent.remote_exec"],
            {"wire_version": 1, "command": "run", "task": "test"},
            workdir="/tmp",
            host_name="__local__",
            backend="opencode",
        )
        assert result.returncode == 0
        assert result.stdout == "ok"
        assert result.stderr == ""
        assert result.session_id is None
        assert result.host == "__local__"
        assert result.backend == "opencode"
        assert result.workdir == "/tmp"

    @patch("subprocess.Popen")
    def test_success_with_session(self, mock_popen: MagicMock):
        """Session ID is extracted from response."""
        mock_proc = _mock_popen_success(session_id="sess-123")
        mock_popen.return_value = mock_proc
        result = _run_wire(
            ["python3", "-m", "codeagent.remote_exec"],
            {"wire_version": 1, "command": "run", "task": "test"},
            workdir="/tmp",
            host_name="__local__",
            backend="opencode",
        )
        assert result.session_id == "sess-123"

    @patch("subprocess.Popen")
    def test_timeout_raises_transport_error(self, mock_popen: MagicMock):
        """Timeout raises TransportError after killing process."""
        mock_proc = _mock_popen_timeout()
        mock_popen.return_value = mock_proc
        with pytest.raises(TransportError, match="timed out"):
            _run_wire(
                ["python3"],
                {"wire_version": 1, "command": "run", "task": "test"},
                workdir="/tmp",
                host_name="__local__",
                backend="opencode",
                timeout=5,
            )
        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_called_once()

    @patch("subprocess.Popen")
    def test_file_not_found_raises(self, mock_popen: MagicMock):
        """Missing binary raises TransportError."""
        mock_popen.side_effect = FileNotFoundError("no such binary")
        with pytest.raises(TransportError, match="not found"):
            _run_wire(
                ["nonexistent-binary"],
                {"wire_version": 1, "command": "run", "task": "test"},
                workdir="/tmp",
                host_name="__local__",
                backend="opencode",
            )

    @patch("subprocess.Popen")
    def test_error_message_propagated(self, mock_popen: MagicMock):
        """Error message from helper is propagated to stderr."""
        mock_proc = MagicMock()
        stdout = encode_line({"type": MSG_ERROR, "message": "workdir not found"})
        mock_proc.communicate.return_value = (stdout, b"")
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc
        result = _run_wire(
            ["python3"],
            {"wire_version": 1, "command": "run", "task": "test"},
            workdir="/tmp",
            host_name="__local__",
            backend="opencode",
        )
        # Wire error: process exited 0 but wire error reported
        # returncode follows process exit; error detail in stderr
        assert result.returncode == 0
        assert result.stderr == "workdir not found"

    @patch("subprocess.Popen")
    def test_stderr_fallback(self, mock_popen: MagicMock):
        """Raw stderr used when no structured result."""
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (b"some noise\n", b"fatal error\n")
        mock_proc.returncode = 1
        mock_popen.return_value = mock_proc
        result = _run_wire(
            ["python3"],
            {"wire_version": 1, "command": "run", "task": "test"},
            workdir="/tmp",
            host_name="__local__",
            backend="opencode",
        )
        assert result.stderr == "fatal error\n"
        assert result.returncode == 1

    @patch("subprocess.Popen")
    def test_ready_message_skipped(self, mock_popen: MagicMock):
        """MSG_READY is handled gracefully (skipped)."""
        mock_proc = MagicMock()
        ready = encode_line({"type": MSG_READY, "wire_version": 1, "package_version": "0.1.0"})
        result_msg = encode_line({"type": MSG_RESULT, "stdout": "done", "stderr": "", "exit_code": 0})
        mock_proc.communicate.return_value = (ready + result_msg, b"")
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc
        result = _run_wire(
            ["python3"],
            {"wire_version": 1, "command": "run", "task": "test"},
            workdir="/tmp",
            host_name="__local__",
            backend="opencode",
        )
        assert result.stdout == "done"

    @patch("subprocess.Popen")
    def test_non_json_noise_skipped(self, mock_popen: MagicMock):
        """Non-JSON output is skipped without affecting result."""
        mock_proc = MagicMock()
        stdout = b"some noise\n" + encode_line({"type": MSG_RESULT, "stdout": "ok", "stderr": "", "exit_code": 0})
        mock_proc.communicate.return_value = (stdout, b"")
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc
        result = _run_wire(
            ["python3"],
            {"wire_version": 1, "command": "run", "task": "test"},
            workdir="/tmp",
            host_name="__local__",
            backend="opencode",
        )
        assert result.stdout == "ok"


# ---------------------------------------------------------------------------
# LocalTransport tests
# ---------------------------------------------------------------------------


class TestLocalTransport:
    """Tests for LocalTransport.execute()."""

    @patch("subprocess.Popen")
    def test_execute_basic(self, mock_popen: MagicMock):
        """execute() delegates to _run_wire correctly."""
        mock_proc = _mock_popen_success()
        mock_popen.return_value = mock_proc
        transport = LocalTransport()
        req = _make_run_request()
        result = transport.execute(req, HostSpec(name="local", ssh_alias="local", hostnames=("localhost",), transport="local"), "/tmp")
        assert result.returncode == 0
        assert result.stdout == "ok"

    @patch("subprocess.Popen")
    def test_execute_with_session_id(self, mock_popen: MagicMock):
        """session_id parameter is passed through to make_request."""
        mock_proc = _mock_popen_success(session_id="sess-abc")
        mock_popen.return_value = mock_proc
        transport = LocalTransport()
        req = _make_run_request()
        result = transport.execute(
            req,
            HostSpec(name="local", ssh_alias="local", hostnames=("localhost",), transport="local"),
            "/tmp",
            session_id="resume-123",
        )
        # Verify Popen was called with correct args
        assert mock_popen.called
        args = mock_popen.call_args
        assert args[1]["stdin"] == subprocess.PIPE
        # Check that the wire request payload contains resume_session_id
        call_input = args[1].get("input") or args[1].get("args")
        # We can verify indirectly through the result
        assert result.session_id == "sess-abc"

    @patch("subprocess.Popen")
    def test_execute_session_id_in_wire_request(self, mock_popen: MagicMock):
        """Wire request contains resume_session_id when session_id provided."""
        captured_input = {}

        def capture_communicate(input=None, **kwargs):
            captured_input["payload"] = input
            stdout = _remote_exec_responds()
            return (b"".join(stdout), b"")

        mock_proc = MagicMock()
        mock_proc.communicate.side_effect = capture_communicate
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        transport = LocalTransport()
        req = _make_run_request()
        transport.execute(
            req,
            HostSpec(name="local", ssh_alias="local", hostnames=("localhost",), transport="local"),
            "/tmp",
            session_id="resume-456",
        )
        import json
        payload = json.loads(captured_input["payload"].decode("utf-8").strip())
        assert payload["resume_session_id"] == "resume-456"

    @patch("subprocess.Popen")
    def test_execute_no_session_id_omitted(self, mock_popen: MagicMock):
        """resume_session_id omitted when session_id is None."""
        captured_input = {}

        def capture_communicate(input=None, **kwargs):
            captured_input["payload"] = input
            stdout = _remote_exec_responds()
            return (b"".join(stdout), b"")

        mock_proc = MagicMock()
        mock_proc.communicate.side_effect = capture_communicate
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        transport = LocalTransport()
        req = _make_run_request()
        transport.execute(
            req,
            HostSpec(name="local", ssh_alias="local", hostnames=("localhost",), transport="local"),
            "/tmp",
        )
        import json
        payload = json.loads(captured_input["payload"].decode("utf-8").strip())
        assert "resume_session_id" not in payload

    def test_warm_is_noop(self):
        """warm() is a no-op for local transport."""
        transport = LocalTransport()
        host = HostSpec(name="local", ssh_alias="local", hostnames=("localhost",), transport="local")
        transport.warm(host)  # should not raise

    def test_check_always_true(self):
        """check() always returns True for local transport."""
        transport = LocalTransport()
        host = HostSpec(name="local", ssh_alias="local", hostnames=("localhost",), transport="local")
        assert transport.check(host) is True

    def test_stop_is_noop(self):
        """stop() is a no-op for local transport."""
        transport = LocalTransport()
        host = HostSpec(name="local", ssh_alias="local", hostnames=("localhost",), transport="local")
        transport.stop(host)  # should not raise


# ---------------------------------------------------------------------------
# _run_ssh_wire tests
# ---------------------------------------------------------------------------


class TestRunSSHWire:
    """Tests for the SSH wire-protocol runner."""

    @patch("subprocess.Popen")
    def test_success_basic(self, mock_popen: MagicMock):
        """Successful SSH execution returns correct RunResult."""
        mock_proc = _mock_popen_success()
        mock_popen.return_value = mock_proc
        result = _run_ssh_wire(
            ["ssh", "-S", "/tmp/s.sock", "host", "python3", "-m", "codeagent.remote_exec"],
            {"wire_version": 1, "command": "run", "task": "test"},
            workdir="/tmp",
            host_name="host",
            backend="opencode",
        )
        assert result.returncode == 0
        assert result.stdout == "ok"
        assert result.host == "host"

    @patch("subprocess.Popen")
    def test_timeout_raises_transport_error(self, mock_popen: MagicMock):
        """Timeout raises TransportError after killing process."""
        mock_proc = _mock_popen_timeout()
        mock_popen.return_value = mock_proc
        with pytest.raises(TransportError, match="timed out"):
            _run_ssh_wire(
                ["ssh", "host", "python3"],
                {"wire_version": 1, "command": "run", "task": "test"},
                workdir="/tmp",
                host_name="host",
                backend="opencode",
                timeout=5,
            )
        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_called_once()

    @patch("subprocess.Popen")
    def test_ssh_stderr_captured(self, mock_popen: MagicMock):
        """SSH stderr is captured in RunResult when no structured result."""
        mock_proc = _mock_popen_exit255_stderr("Connection refused")
        mock_popen.return_value = mock_proc
        result = _run_ssh_wire(
            ["ssh", "host", "python3"],
            {"wire_version": 1, "command": "run", "task": "test"},
            workdir="/tmp",
            host_name="host",
            backend="opencode",
        )
        assert result.stderr == "Connection refused"
        assert result.returncode == 255

    @patch("subprocess.Popen")
    def test_exit_code_propagated(self, mock_popen: MagicMock):
        """Non-zero SSH exit code is propagated when no wire exit code."""
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (b"", b"some error\n")
        mock_proc.returncode = 127
        mock_popen.return_value = mock_proc
        result = _run_ssh_wire(
            ["ssh", "host", "python3"],
            {"wire_version": 1, "command": "run", "task": "test"},
            workdir="/tmp",
            host_name="host",
            backend="opencode",
        )
        assert result.returncode == 127

    @patch("subprocess.Popen")
    def test_ready_message_skipped(self, mock_popen: MagicMock):
        """MSG_READY is handled gracefully (skipped)."""
        mock_proc = MagicMock()
        ready = encode_line({"type": MSG_READY, "wire_version": 1, "package_version": "0.1.0"})
        result_msg = encode_line({"type": MSG_RESULT, "stdout": "done", "stderr": "", "exit_code": 0})
        mock_proc.communicate.return_value = (ready + result_msg, b"")
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc
        result = _run_ssh_wire(
            ["ssh", "host", "python3"],
            {"wire_version": 1, "command": "run", "task": "test"},
            workdir="/tmp",
            host_name="host",
            backend="opencode",
        )
        assert result.stdout == "done"


# ---------------------------------------------------------------------------
# SSHTransport tests
# ---------------------------------------------------------------------------


class TestSSHTransport:
    """Tests for SSHTransport.execute()."""

    @patch("codeagent.transport.ssh.ControlMaster")
    @patch("subprocess.Popen")
    def test_execute_basic(self, mock_popen: MagicMock, mock_cm_cls: MagicMock):
        """execute() delegates to _run_ssh_wire correctly."""
        mock_cm = MagicMock()
        mock_cm.is_alive.return_value = True
        mock_cm.ssh_cmd.return_value = ["ssh", "-S", "/tmp/s.sock", "host", "python3", "-m", "codeagent.remote_exec"]
        mock_cm_cls.return_value = mock_cm

        mock_proc = _mock_popen_success()
        mock_popen.return_value = mock_proc

        transport = SSHTransport()
        host = HostSpec(name="remote", ssh_alias="remote-host", hostnames=("remote-host",))
        req = _make_run_request()
        result = transport.execute(req, host, "/workdir")

        assert result.returncode == 0
        assert result.stdout == "ok"
        assert result.host == "remote-host"

    @patch("codeagent.transport.ssh.ControlMaster")
    @patch("subprocess.Popen")
    def test_execute_with_session_id(self, mock_popen: MagicMock, mock_cm_cls: MagicMock):
        """session_id parameter is passed through correctly."""
        mock_cm = MagicMock()
        mock_cm.is_alive.return_value = True
        mock_cm.ssh_cmd.return_value = ["ssh", "-S", "/tmp/s.sock", "host", "python3", "-m", "codeagent.remote_exec"]
        mock_cm_cls.return_value = mock_cm

        mock_proc = _mock_popen_success(session_id="remote-sess")
        mock_popen.return_value = mock_proc

        transport = SSHTransport()
        host = HostSpec(name="remote", ssh_alias="remote-host", hostnames=("remote-host",))
        req = _make_run_request()
        result = transport.execute(req, host, "/workdir", session_id="resume-789")

        assert result.session_id == "remote-sess"

    @patch("codeagent.transport.ssh.ControlMaster")
    @patch("subprocess.Popen")
    def test_execute_shell_prefix(self, mock_popen: MagicMock, mock_cm_cls: MagicMock):
        """shell_prefix is prepended to remote command."""
        mock_cm = MagicMock()
        mock_cm.is_alive.return_value = True
        mock_cm.ssh_cmd.return_value = [
            "ssh", "-S", "/tmp/s.sock", "host",
            "sh", "-c", "source ~/.zshrc &&; python3 -m codeagent.remote_exec",
        ]
        mock_cm_cls.return_value = mock_cm

        mock_proc = _mock_popen_success()
        mock_popen.return_value = mock_proc

        transport = SSHTransport()
        host = HostSpec(
            name="remote",
            ssh_alias="remote-host",
            hostnames=("remote-host",),
            shell_prefix="source ~/.zshrc &&",
        )
        req = _make_run_request()
        transport.execute(req, host, "/workdir")

        # Verify shell_prefix was wrapped in sh -c for remote expansion
        mock_cm.ssh_cmd.assert_called_once()
        args = mock_cm.ssh_cmd.call_args[0]
        assert args[0] == "sh"
        assert args[1] == "-c"
        assert "source ~/.zshrc &&" in args[2]
        assert "codeagent-remote-exec" in args[2]

    @patch("codeagent.transport.ssh.ControlMaster")
    @patch("subprocess.Popen")
    def test_execute_fallback_on_exit_255(self, mock_popen: MagicMock, mock_cm_cls: MagicMock):
        """On exit 255 + SSH error, retries with fallback_ssh_alias."""
        primary_cm = MagicMock()
        primary_cm.is_alive.return_value = True
        primary_cm.ssh_cmd.return_value = [
            "ssh", "-S", "/tmp/primary.sock", "primary", "python3", "-m", "codeagent.remote_exec",
        ]

        fallback_cm = MagicMock()
        fallback_cm.is_alive.return_value = True
        fallback_cm.ssh_cmd.return_value = [
            "ssh", "-S", "/tmp/fallback.sock", "fallback", "python3", "-m", "codeagent.remote_exec",
        ]

        # First call to ControlMaster("primary"), second call to ControlMaster("fallback")
        mock_cm_cls.side_effect = [primary_cm, fallback_cm]

        # First Popen: exit 255 with error, second: success
        mock_proc_fail = _mock_popen_exit255_stderr("Connection refused")
        mock_proc_ok = _mock_popen_success()
        mock_popen.side_effect = [mock_proc_fail, mock_proc_ok]

        transport = SSHTransport()
        host = HostSpec(
            name="remote",
            ssh_alias="primary",
            hostnames=("primary",),
            fallback_ssh_alias="fallback",
        )
        req = _make_run_request()
        result = transport.execute(req, host, "/workdir")

        assert result.returncode == 0
        assert result.stdout == "ok"
        # Should have been called twice (primary + fallback)
        assert mock_popen.call_count == 2

    @patch("codeagent.transport.ssh.ControlMaster")
    @patch("subprocess.Popen")
    def test_execute_no_fallback_on_non_ssh_error(self, mock_popen: MagicMock, mock_cm_cls: MagicMock):
        """Exit 255 without SSH error pattern does NOT trigger fallback."""
        mock_cm = MagicMock()
        mock_cm.is_alive.return_value = True
        mock_cm.ssh_cmd.return_value = ["ssh", "-S", "/tmp/s.sock", "host", "python3"]
        mock_cm_cls.return_value = mock_cm

        # Exit 255 but with a non-SSH error
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (b"", b"some random error")
        mock_proc.returncode = 255
        mock_popen.return_value = mock_proc

        transport = SSHTransport()
        host = HostSpec(
            name="remote",
            ssh_alias="primary",
            hostnames=("primary",),
            fallback_ssh_alias="fallback",
        )
        req = _make_run_request()
        result = transport.execute(req, host, "/workdir")

        assert result.returncode == 255
        # Only called once — no fallback
        assert mock_popen.call_count == 1

    @patch("codeagent.transport.ssh.ControlMaster")
    @patch("subprocess.Popen")
    def test_execute_no_fallback_without_fallback_alias(self, mock_popen: MagicMock, mock_cm_cls: MagicMock):
        """Exit 255 + SSH error without fallback_ssh_alias does NOT retry."""
        mock_cm = MagicMock()
        mock_cm.is_alive.return_value = True
        mock_cm.ssh_cmd.return_value = ["ssh", "-S", "/tmp/s.sock", "host", "python3"]
        mock_cm_cls.return_value = mock_cm

        mock_proc = _mock_popen_exit255_stderr("Connection refused")
        mock_popen.return_value = mock_proc

        transport = SSHTransport()
        host = HostSpec(
            name="remote",
            ssh_alias="primary",
            hostnames=("primary",),
            fallback_ssh_alias="",
        )
        req = _make_run_request()
        result = transport.execute(req, host, "/workdir")

        assert result.returncode == 255
        assert mock_popen.call_count == 1

    @patch("codeagent.transport.ssh.ControlMaster")
    @patch("subprocess.Popen")
    def test_execute_lazy_warm(self, mock_popen: MagicMock, mock_cm_cls: MagicMock):
        """ControlMaster is created lazily if not alive."""
        mock_cm = MagicMock()
        mock_cm.is_alive.side_effect = [False, True]  # first check: not alive, after create: alive
        mock_cm.ssh_cmd.return_value = ["ssh", "-S", "/tmp/s.sock", "host", "python3", "-m", "codeagent.remote_exec"]
        mock_cm_cls.return_value = mock_cm

        mock_proc = _mock_popen_success()
        mock_popen.return_value = mock_proc

        transport = SSHTransport()
        host = HostSpec(name="remote", ssh_alias="remote-host", hostnames=("remote-host",))
        req = _make_run_request()
        transport.execute(req, host, "/workdir")

        mock_cm.create.assert_called_once()

    @patch("codeagent.transport.ssh.ControlMaster")
    @patch("subprocess.Popen")
    def test_execute_fallback_on_warm_failure(self, mock_popen: MagicMock, mock_cm_cls: MagicMock):
        """On ControlMaster create failure, retries with fallback_ssh_alias."""
        primary_cm = MagicMock()
        primary_cm.is_alive.return_value = False
        primary_cm.create.side_effect = TransportError("failed to create master for primary")

        fallback_cm = MagicMock()
        fallback_cm.is_alive.return_value = True
        fallback_cm.ssh_cmd.return_value = [
            "ssh", "-S", "/tmp/fallback.sock", "fallback", "python3", "-m", "codeagent.remote_exec",
        ]

        mock_cm_cls.side_effect = [primary_cm, fallback_cm]
        mock_proc_ok = _mock_popen_success()
        mock_popen.return_value = mock_proc_ok

        transport = SSHTransport()
        host = HostSpec(
            name="remote",
            ssh_alias="primary",
            hostnames=("primary",),
            fallback_ssh_alias="fallback",
        )
        req = _make_run_request()
        result = transport.execute(req, host, "/workdir")

        assert result.returncode == 0
        assert result.stdout == "ok"
        assert result.host == "fallback"
        primary_cm.create.assert_called_once()
        fallback_cm.create.assert_not_called()  # fallback is_alive → True
        # Only one Popen call — fallback execution only
        assert mock_popen.call_count == 1

    @patch("codeagent.transport.ssh.ControlMaster")
    def test_execute_warm_failure_no_fallback_raises(self, mock_cm_cls: MagicMock):
        """ControlMaster create failure without fallback re-raises TransportError."""
        mock_cm = MagicMock()
        mock_cm.is_alive.return_value = False
        mock_cm.create.side_effect = TransportError("failed to create master")
        mock_cm_cls.return_value = mock_cm

        transport = SSHTransport()
        host = HostSpec(
            name="remote",
            ssh_alias="primary",
            hostnames=("primary",),
            fallback_ssh_alias="",
        )
        req = _make_run_request()
        with pytest.raises(TransportError, match="failed to create master"):
            transport.execute(req, host, "/workdir")

    def test_check_returns_false_for_unknown_host(self):
        """check() returns False for hosts with no ControlMaster."""
        transport = SSHTransport()
        host = HostSpec(name="unknown", ssh_alias="unknown-host", hostnames=("unknown-host",))
        assert transport.check(host) is False

    @patch("codeagent.transport.ssh.ControlMaster")
    def test_stop_calls_stop_by_alias(self, mock_cm_cls: MagicMock):
        """stop() delegates to stop_by_alias for cross-process support."""
        mock_cm = MagicMock()
        mock_cm.is_alive.return_value = True
        mock_cm_cls.return_value = mock_cm

        transport = SSHTransport()
        host = HostSpec(name="remote", ssh_alias="remote-host", hostnames=("remote-host",))
        transport.warm(host)

        with patch("codeagent.transport.ssh.stop_by_alias") as mock_stop:
            transport.stop(host)
            mock_stop.assert_called_once_with("remote-host", ssh_bin="ssh")

    @patch("codeagent.transport.ssh.list_sockets")
    def test_list_sockets_returns_tuples(self, mock_list: MagicMock):
        """list_sockets() returns list of (alias, path) tuples."""
        mock_list.return_value = [("host-a", Path("/tmp/a.sock")), ("host-b", Path("/tmp/b.sock"))]
        transport = SSHTransport()
        result = transport.list_sockets()
        assert len(result) == 2
        assert result[0] == ("host-a", Path("/tmp/a.sock"))
        assert result[1] == ("host-b", Path("/tmp/b.sock"))
        mock_list.assert_called_once()

    def test_check_cross_process_via_meta(self, tmp_path: Path):
        """check() finds socket via .meta files in a fresh process (empty _masters)."""
        sock = tmp_path / "abc123.sock"
        sock.touch()
        meta = tmp_path / "abc123.meta"
        meta.write_text('{"alias": "myhost", "created": 1234567890.0}')

        transport = SSHTransport()  # _masters is empty — fresh process
        host = HostSpec(name="myhost", ssh_alias="myhost", hostnames=("myhost",))

        with patch("codeagent.transport.control_master._socket_dir", return_value=tmp_path), \
             patch("shutil.which", return_value="/usr/bin/ssh"), \
             patch("subprocess.run", return_value=MagicMock(returncode=0)):
            assert transport.check(host) is True

    def test_check_cross_process_no_match(self, tmp_path: Path):
        """check() returns False when no .meta matches the alias."""
        sock = tmp_path / "abc123.sock"
        sock.touch()
        meta = tmp_path / "abc123.meta"
        meta.write_text('{"alias": "otherhost", "created": 1234567890.0}')

        transport = SSHTransport()
        host = HostSpec(name="myhost", ssh_alias="myhost", hostnames=("myhost",))

        with patch("codeagent.transport.control_master._socket_dir", return_value=tmp_path):
            assert transport.check(host) is False

    def test_stop_cross_process_via_meta(self, tmp_path: Path):
        """stop() works in a fresh process (empty _masters) via .meta files."""
        sock = tmp_path / "abc123.sock"
        sock.touch()
        meta = tmp_path / "abc123.meta"
        meta.write_text('{"alias": "myhost", "created": 1234567890.0}')

        transport = SSHTransport()  # _masters is empty — fresh process
        host = HostSpec(name="myhost", ssh_alias="myhost", hostnames=("myhost",))

        with patch("codeagent.transport.control_master._socket_dir", return_value=tmp_path), \
             patch("shutil.which", return_value="/usr/bin/ssh"), \
             patch("subprocess.run", return_value=MagicMock(returncode=0)):
            transport.stop(host)

        assert not sock.exists()
        assert not meta.exists()


# ---------------------------------------------------------------------------
# _is_ssh_error tests
# ---------------------------------------------------------------------------


class TestIsSSHError:
    """Tests for the SSH error pattern detection."""

    def test_connection_refused(self):
        assert _is_ssh_error("ssh: connect to host 10.0.0.1 port 22: Connection refused") is True

    def test_connection_timed_out(self):
        assert _is_ssh_error("Connection timed out") is True

    def test_no_route(self):
        assert _is_ssh_error("No route to host") is True

    def test_non_ssh_error(self):
        assert _is_ssh_error("some random error message") is False

    def test_empty_stderr(self):
        assert _is_ssh_error("") is False

    def test_multiple_patterns(self):
        assert _is_ssh_error("Connection refused and also Host key verification failed") is True


# ---------------------------------------------------------------------------
# RelayTransport tests
# ---------------------------------------------------------------------------


class TestRelayTransport:
    """Tests for RelayTransport."""

    def test_relay_transport_init(self, tmp_path: Path):
        """RelayTransport requires a valid relay_zsh path."""
        relay_zsh = tmp_path / "relay.zsh"
        relay_zsh.touch()
        transport = RelayTransport(str(relay_zsh))
        assert transport._relay_zsh == str(relay_zsh)

    def test_relay_transport_init_missing_zsh(self):
        """RelayTransport raises TransportError if relay_zsh is empty."""
        with pytest.raises(TransportError, match="relay_zsh is required"):
            RelayTransport("")

    def test_relay_transport_init_not_found(self, tmp_path: Path):
        """RelayTransport raises TransportError if relay_zsh doesn't exist."""
        with pytest.raises(TransportError, match="relay_zsh not found"):
            RelayTransport(str(tmp_path / "nonexistent.zsh"))

    def test_relay_builds_correct_command(self, tmp_path: Path):
        """RelayTransport.execute builds correct relay command."""
        relay_zsh = tmp_path / "relay.zsh"
        relay_zsh.touch()
        transport = RelayTransport(str(relay_zsh))

        host = HostSpec(
            name="test",
            ssh_alias="dev.example.com",
            hostnames=("dev.example.com",),
            description="test relay host",
        )
        request = _make_run_request(
            task="test task",
            workdir="/tmp",
            backend="opencode",
            skip_permissions=True,
            timeout=120,
        )

        # Mock _run_with_pty to capture argv
        captured_argv = None

        def mock_run_with_pty(argv, timeout=600):
            nonlocal captured_argv
            captured_argv = argv
            return RunResult(returncode=0, stdout="", stderr="")

        transport._run_with_pty = mock_run_with_pty

        transport.execute(request, host, "/tmp")

        assert captured_argv is not None
        assert captured_argv[0] == "zsh"
        assert captured_argv[1] == "-c"
        # The command should contain source, relay-login, and the target
        cmd = captured_argv[2]
        assert "source" in cmd
        assert "relay-login" in cmd
        assert "dev.example.com" in cmd
        assert "base64 -d" in cmd
        assert "codeagent-remote-exec" in cmd
