"""Tests for RelayTransport._run_with_pty robustness.

The PTY select-loop is exercised with **real** file descriptors so
``os.read`` / ``select`` / ``os.close`` behave naturally, and a mocked
``Popen`` so no real relay-login process is spawned.

- The payload channel is a real ``os.pipe()`` (the code treats the pty
  master opaquely — it only ``select``s and ``os.read``s it), which
  gives a deterministic data-then-EOF sequence.
- The interactive-stdin test uses a real pty pair (the code ``os.write``s
  to that master, so it must actually be writable).
"""
from __future__ import annotations

import base64
import fcntl
import json
import os
import pty
import re
import signal
import subprocess
import termios
from unittest.mock import MagicMock, patch

import pytest

from codeagent.domain import HostSpec, RunRequest, RunResult
from codeagent.transport.relay import RelayTransport
from codeagent.wire.protocol import WIRE_VERSION

_WIRE_OK = (
    f'{{"type":"ready","wire_version":{WIRE_VERSION},"package_version":"0.2.0"}}\n'
    f'{{"type":"accepted","wire_version":{WIRE_VERSION}}}\n'
    '{"type":"session","id":"sess-9"}\n'
    '{"type":"result","stdout":"ok","stderr":"","exit_code":0}\n'
).encode("utf-8")


@pytest.fixture
def transport(tmp_path) -> RelayTransport:
    relay_zsh = tmp_path / "relay.zsh"
    relay_zsh.touch()
    return RelayTransport(str(relay_zsh))


def _make_proc(returncode: int = 0, wait_side_effect=None) -> MagicMock:
    proc = MagicMock()
    proc.pid = 4242
    proc.returncode = returncode
    proc.wait.return_value = None
    if wait_side_effect is not None:
        proc.wait.side_effect = wait_side_effect
    return proc


class _PipeChannel:
    """A real pipe standing in for the pty master.

    ``send(payload)`` writes then closes the write end → the read end
    yields the payload followed by EOF.  ``close_write()`` alone yields
    immediate EOF.  Leaving the write end open makes select idle (used
    for the timeout test).
    """

    def __init__(self, payload: bytes = b""):
        self.r_fd, self.w_fd = os.pipe()
        if payload:
            self.send(payload)

    def send(self, payload: bytes) -> None:
        os.write(self.w_fd, payload)
        self.close_write()

    def close_write(self) -> None:
        try:
            os.close(self.w_fd)
        except OSError:
            pass

    def patch_openpty(self):
        slave_fd = os.open(os.devnull, os.O_RDONLY)
        return patch(
            "codeagent.transport.relay.pty.openpty",
            return_value=(self.r_fd, slave_fd),
        )


class TestRelayPty:
    """Bounded select-loop + wire parsing + escalating termination."""

    def test_wire_result_success(self, transport: RelayTransport):
        """Ready/accepted/session/result lines produce a correct RunResult."""
        proc = _make_proc()
        chan = _PipeChannel(_WIRE_OK)

        with (
            chan.patch_openpty(),
            patch("codeagent.transport.relay.subprocess.Popen", return_value=proc) as popen,
            patch("codeagent.transport.relay.os.killpg") as killpg,
        ):
            result = transport._run_with_pty(["zsh", "-c", "true"], timeout=30)

        popen.assert_called_once()
        assert result.returncode == 0
        assert result.stdout == "ok"
        assert result.session_id == "sess-9"
        assert result.stderr == ""
        killpg.assert_not_called()

    def test_wire_error_message(self, transport: RelayTransport):
        """MSG_ERROR is surfaced on stderr with its exit code."""
        proc = _make_proc()
        chan = _PipeChannel(b'{"type":"error","message":"workdir not found"}\n')

        with (
            chan.patch_openpty(),
            patch("codeagent.transport.relay.subprocess.Popen", return_value=proc),
            patch("codeagent.transport.relay.os.killpg"),
        ):
            result = transport._run_with_pty(["zsh", "-c", "true"], timeout=30)

        assert result.returncode == 1
        assert "workdir not found" in result.stderr

    def test_non_wire_output_goes_to_stderr(self, transport: RelayTransport):
        """Relay UI / QR lines that are not wire JSON land on stderr."""
        proc = _make_proc()
        chan = _PipeChannel(b"scan this QR code: 12345\n" + _WIRE_OK)

        with (
            chan.patch_openpty(),
            patch("codeagent.transport.relay.subprocess.Popen", return_value=proc),
            patch("codeagent.transport.relay.os.killpg"),
        ):
            result = transport._run_with_pty(["zsh", "-c", "true"], timeout=30)

        assert result.returncode == 0
        assert result.stdout == "ok"
        assert "scan this QR code" in result.stderr

    def test_wire_version_mismatch(self, transport: RelayTransport):
        """A wrong remote wire version discards the result and errors out."""
        proc = _make_proc()
        payload = (
            b'{"type":"ready","wire_version":99,"package_version":"0.2.0"}\n'
            b'{"type":"result","stdout":"ok","stderr":"","exit_code":0}\n'
        )
        chan = _PipeChannel(payload)

        with (
            chan.patch_openpty(),
            patch("codeagent.transport.relay.subprocess.Popen", return_value=proc),
            patch("codeagent.transport.relay.os.killpg"),
        ):
            result = transport._run_with_pty(["zsh", "-c", "true"], timeout=30)

        assert result.returncode == 1
        assert "wire version mismatch" in result.stderr
        assert "ignoring result" in result.stderr
        assert result.stdout == ""

    def test_eof_breaks_without_kill(self, transport: RelayTransport):
        """Immediate EOF reaps the child without sending signals."""
        proc = _make_proc(returncode=0)
        chan = _PipeChannel()
        chan.close_write()  # EOF with no payload

        with (
            chan.patch_openpty(),
            patch("codeagent.transport.relay.subprocess.Popen", return_value=proc),
            patch("codeagent.transport.relay.os.killpg") as killpg,
        ):
            result = transport._run_with_pty(["zsh", "-c", "true"], timeout=30)

        assert result.returncode == 0
        proc.wait.assert_called()
        killpg.assert_not_called()

    def test_timeout_uses_sigterm_then_sigkill(self, transport: RelayTransport):
        """Timeout escalates SIGTERM → wait → SIGKILL on the process group."""
        proc = _make_proc(wait_side_effect=[subprocess.TimeoutExpired("x", 5), None])
        chan = _PipeChannel()  # write end left open → select idles till deadline
        try:
            with (
                chan.patch_openpty(),
                patch("codeagent.transport.relay.subprocess.Popen", return_value=proc),
                patch("codeagent.transport.relay.os.killpg") as killpg,
            ):
                result = transport._run_with_pty(["zsh", "-c", "true"], timeout=0.05)
        finally:
            chan.close_write()

        assert result.returncode == -1
        assert "timeout after 0.05s" in result.stderr
        calls = [c.args[1] for c in killpg.call_args_list]
        assert calls == [signal.SIGTERM, signal.SIGKILL], calls

    def test_iteration_cap_aborts(self, transport: RelayTransport):
        """A busy loop that never progresses is stopped by the iteration cap."""
        proc = _make_proc()
        chan = _PipeChannel()
        with (
            patch("codeagent.transport.relay._MAX_PTY_ITERATIONS", 3),
            chan.patch_openpty(),
            patch("codeagent.transport.relay.subprocess.Popen", return_value=proc),
            patch("codeagent.transport.relay.select.select", return_value=([], [], [])),
            patch("codeagent.transport.relay.os.killpg") as killpg,
        ):
            result = transport._run_with_pty(["zsh", "-c", "true"], timeout=60)

        assert result.returncode == -1
        assert "iteration cap reached after 3 iterations" in result.stderr
        killpg.assert_called()

    def test_buffer_overflow_aborts(self, transport: RelayTransport):
        """Output that never produces a newline trips the buffer cap."""
        proc = _make_proc()
        # Patch the module's imported MAX_LINE_LENGTH so a small blob trips
        # it — the patch must stay active while _run_with_pty executes.
        with patch("codeagent.transport.relay.MAX_LINE_LENGTH", 100):
            chan = _PipeChannel(b"x" * 200)  # payload exceeds the patched cap
            with (
                chan.patch_openpty(),
                patch("codeagent.transport.relay.subprocess.Popen", return_value=proc),
                patch("codeagent.transport.relay.os.killpg") as killpg,
            ):
                result = transport._run_with_pty(["zsh", "-c", "true"], timeout=30)

        assert result.returncode == -1
        assert "buffer exceeded limit" in result.stderr
        killpg.assert_called()

    def test_select_error_aborts_and_kills(self, transport: RelayTransport):
        """select() failure cannot be recovered — child is terminated."""
        proc = _make_proc(returncode=1)
        chan = _PipeChannel()
        with (
            chan.patch_openpty(),
            patch("codeagent.transport.relay.subprocess.Popen", return_value=proc),
            patch("codeagent.transport.relay.select.select", side_effect=OSError("bad fd")),
            patch("codeagent.transport.relay.os.killpg") as killpg,
        ):
            result = transport._run_with_pty(["zsh", "-c", "true"], timeout=30)

        assert result.returncode == 1
        killpg.assert_called()

    def test_read_error_aborts_and_kills(self, transport: RelayTransport):
        """os.read() failure on the PTY master terminates the child."""
        proc = _make_proc(returncode=2)
        chan = _PipeChannel()

        def failing_read(fd: int, n: int) -> bytes:
            if fd == chan.r_fd:
                raise OSError("master gone")
            return os.read(fd, n)

        with (
            chan.patch_openpty(),
            patch("codeagent.transport.relay.subprocess.Popen", return_value=proc),
            patch("codeagent.transport.relay.select.select", return_value=([chan.r_fd], [], [])),
            patch("codeagent.transport.relay.os.read", side_effect=failing_read),
            patch("codeagent.transport.relay.os.killpg") as killpg,
        ):
            result = transport._run_with_pty(["zsh", "-c", "true"], timeout=30)

        assert result.returncode == 2
        killpg.assert_called()

    def test_stdin_forwarding(self, transport: RelayTransport):
        """Interactive stdin (QR/expect) is forwarded to the PTY master."""
        proc = _make_proc()
        chan = _PipeChannel(_WIRE_OK)

        # A real pty acts as the interactive stdin source.
        stdin_master, stdin_slave = pty.openpty()
        os.write(stdin_slave, b"y\r")

        class _FakeStdin:
            def isatty(self) -> bool:
                return True

            def fileno(self) -> int:
                return stdin_master

        written: list[bytes] = []

        def recording_write(fd: int, data: bytes) -> int:
            written.append(data)
            return len(data)

        try:
            with (
                chan.patch_openpty(),
                patch("codeagent.transport.relay.subprocess.Popen", return_value=proc),
                patch("codeagent.transport.relay.sys.stdin", _FakeStdin()),
                patch("codeagent.transport.relay.os.write", side_effect=recording_write),
                patch("codeagent.transport.relay.os.killpg") as killpg,
            ):
                result = transport._run_with_pty(["zsh", "-c", "true"], timeout=30)
        finally:
            os.close(stdin_master)
            os.close(stdin_slave)

        assert result.returncode == 0
        assert written == [b"y\r"], written
        killpg.assert_not_called()

    def test_relay_execution_error_captured(self, transport: RelayTransport):
        """Unexpected exceptions inside the runner become a relay error."""
        with patch("codeagent.transport.relay.pty.openpty", side_effect=OSError("no ptys")):
            result = transport._run_with_pty(["zsh", "-c", "true"], timeout=30)

        assert result.returncode == 1
        assert "relay execution error" in result.stderr

    def test_terminate_group_killpg_failure_and_exhausted(self, transport: RelayTransport):
        """killpg failures are swallowed; exhausted escalation logs a warning."""
        proc = _make_proc(
            wait_side_effect=[
                subprocess.TimeoutExpired("x", 5),
                subprocess.TimeoutExpired("x", 5),
            ]
        )
        chan = _PipeChannel()
        try:
            with (
                chan.patch_openpty(),
                patch("codeagent.transport.relay.subprocess.Popen", return_value=proc),
                patch(
                    "codeagent.transport.relay.os.killpg",
                    side_effect=ProcessLookupError("no such process"),
                ) as killpg,
                patch("codeagent.transport.relay.log") as m_log,
            ):
                result = transport._run_with_pty(["zsh", "-c", "true"], timeout=0.05)
        finally:
            chan.close_write()

        assert result.returncode == -1
        assert "timeout after 0.05s" in result.stderr
        # Both escalation stages fired; each killpg miss was swallowed.
        assert killpg.call_count == 2
        m_log.warning.assert_called_once()

    def test_preexec_sets_controlling_tty(self, transport: RelayTransport):
        """The child preexec hook creates a session and claims the TTY."""
        proc = _make_proc()
        chan = _PipeChannel(_WIRE_OK)
        ioctl_calls: list[tuple[int, int]] = []

        def fake_popen(*args, **kwargs):
            kwargs["preexec_fn"]()  # run in the simulated child context
            ioctl_calls.extend(c.args for c in m_ioctl.call_args_list)
            return proc

        with (
            chan.patch_openpty(),
            patch("codeagent.transport.relay.subprocess.Popen", side_effect=fake_popen),
            patch("codeagent.transport.relay.os.setsid") as m_setsid,
            patch("fcntl.ioctl") as m_ioctl,
            patch("codeagent.transport.relay.os.killpg") as killpg,
        ):
            result = transport._run_with_pty(["zsh", "-c", "true"], timeout=30)

        assert result.returncode == 0
        assert result.stdout == "ok"
        m_setsid.assert_called_once()
        # TIOCSCTTY was claimed on a real slave fd.
        assert len(ioctl_calls) == 1
        assert ioctl_calls[0][1] == termios.TIOCSCTTY
        killpg.assert_not_called()

    def test_preexec_tolerates_ioctl_failure(self, transport: RelayTransport):
        """A failed TIOCSCTTY claim must not abort the run."""
        proc = _make_proc()
        chan = _PipeChannel(_WIRE_OK)

        def fake_popen(*args, **kwargs):
            kwargs["preexec_fn"]()
            return proc

        with (
            chan.patch_openpty(),
            patch("codeagent.transport.relay.subprocess.Popen", side_effect=fake_popen),
            patch("codeagent.transport.relay.os.setsid"),
            patch("fcntl.ioctl", side_effect=OSError("EPERM: not a session leader")),
            patch("codeagent.transport.relay.os.killpg") as killpg,
        ):
            result = transport._run_with_pty(["zsh", "-c", "true"], timeout=30)

        assert result.returncode == 0
        assert result.stdout == "ok"
        killpg.assert_not_called()

    def test_slave_close_failure_tolerated(self, transport: RelayTransport):
        """A failed close of the slave fd must not abort the run."""
        proc = _make_proc()
        slave_fd = os.open(os.devnull, os.O_RDONLY)
        chan = _PipeChannel(_WIRE_OK)
        real_close = os.close

        def raising_close(fd):
            if fd == slave_fd:
                raise OSError("fd already closed")
            return real_close(fd)

        with (
            patch("codeagent.transport.relay.pty.openpty", return_value=(chan.r_fd, slave_fd)),
            patch("codeagent.transport.relay.subprocess.Popen", return_value=proc),
            patch("codeagent.transport.relay.os.close", side_effect=raising_close),
            patch("codeagent.transport.relay.os.killpg") as killpg,
        ):
            result = transport._run_with_pty(["zsh", "-c", "true"], timeout=30)

        assert result.returncode == 0
        assert result.stdout == "ok"
        killpg.assert_not_called()

    @pytest.mark.parametrize("failure", ["isatty", "fileno"])
    def test_stdin_probe_failures_tolerated(self, transport: RelayTransport, failure: str):
        """A broken stdin probe (no tty / closed stream) disables forwarding."""
        proc = _make_proc()
        chan = _PipeChannel(_WIRE_OK)

        class _BrokenStdin:
            def isatty(self) -> bool:
                if failure == "isatty":
                    raise AttributeError("no isatty")
                return True

            def fileno(self) -> int:
                if failure == "fileno":
                    raise ValueError("I/O operation on closed file")
                return 0

        with (
            chan.patch_openpty(),
            patch("codeagent.transport.relay.subprocess.Popen", return_value=proc),
            patch("codeagent.transport.relay.sys.stdin", _BrokenStdin()),
            patch("codeagent.transport.relay.os.killpg") as killpg,
        ):
            result = transport._run_with_pty(["zsh", "-c", "true"], timeout=30)

        assert result.returncode == 0
        assert result.stdout == "ok"
        killpg.assert_not_called()

    def test_loop_breaks_on_deadline(self, transport: RelayTransport):
        """The remaining<=0 guard breaks out of the loop without a kill."""
        proc = _make_proc(returncode=0)
        chan = _PipeChannel()
        try:
            with (
                chan.patch_openpty(),
                patch("codeagent.transport.relay.subprocess.Popen", return_value=proc),
                # deadline=30, while-check passes, remaining hits 0 exactly,
                # timed_out re-check returns False → normal reap.
                patch("codeagent.transport.relay.time.time", side_effect=[0.0, 0.0, 30.0, 29.0]),
                patch("codeagent.transport.relay.os.killpg") as killpg,
            ):
                result = transport._run_with_pty(["zsh", "-c", "true"], timeout=30)
        finally:
            chan.close_write()

        assert result.returncode == 0
        killpg.assert_not_called()

    def test_stdin_eof_disables_forwarding(self, transport: RelayTransport):
        """Reading EOF from stdin stops stdin forwarding."""
        proc = _make_proc()
        chan = _PipeChannel(_WIRE_OK)
        stdin_r, stdin_w = os.pipe()
        os.close(stdin_w)  # immediate EOF on the read end

        class _FakeStdin:
            def isatty(self) -> bool:
                return True

            def fileno(self) -> int:
                return stdin_r

        try:
            with (
                chan.patch_openpty(),
                patch("codeagent.transport.relay.subprocess.Popen", return_value=proc),
                patch("codeagent.transport.relay.sys.stdin", _FakeStdin()),
                patch("codeagent.transport.relay.os.write") as m_write,
                patch("codeagent.transport.relay.os.killpg") as killpg,
            ):
                result = transport._run_with_pty(["zsh", "-c", "true"], timeout=30)
        finally:
            os.close(stdin_r)

        assert result.returncode == 0
        assert result.stdout == "ok"
        m_write.assert_not_called()
        killpg.assert_not_called()

    def test_stdin_read_error_disables_forwarding(self, transport: RelayTransport):
        """An OSError reading stdin disables forwarding without aborting."""
        proc = _make_proc()
        chan = _PipeChannel(_WIRE_OK)
        stdin_master, stdin_slave = pty.openpty()
        os.write(stdin_slave, b"y")  # make the master readable

        class _FakeStdin:
            def isatty(self) -> bool:
                return True

            def fileno(self) -> int:
                return stdin_master

        real_read = os.read

        def failing_read(fd, n):
            if fd == stdin_master:
                raise OSError("stdin read failed")
            return real_read(fd, n)

        try:
            with (
                chan.patch_openpty(),
                patch("codeagent.transport.relay.subprocess.Popen", return_value=proc),
                patch("codeagent.transport.relay.sys.stdin", _FakeStdin()),
                patch("codeagent.transport.relay.os.read", side_effect=failing_read),
                patch("codeagent.transport.relay.os.killpg") as killpg,
            ):
                result = transport._run_with_pty(["zsh", "-c", "true"], timeout=30)
        finally:
            os.close(stdin_master)
            os.close(stdin_slave)

        assert result.returncode == 0
        assert result.stdout == "ok"
        killpg.assert_not_called()

    def test_blank_lines_skipped(self, transport: RelayTransport):
        """Blank lines in the stream are skipped without touching stderr."""
        proc = _make_proc()
        chan = _PipeChannel(b"\n\n" + _WIRE_OK)

        with (
            chan.patch_openpty(),
            patch("codeagent.transport.relay.subprocess.Popen", return_value=proc),
            patch("codeagent.transport.relay.os.killpg") as killpg,
        ):
            result = transport._run_with_pty(["zsh", "-c", "true"], timeout=30)

        assert result.returncode == 0
        assert result.stdout == "ok"
        assert result.stderr == ""
        killpg.assert_not_called()

    def test_unhandled_wire_type_goes_to_stderr(self, transport: RelayTransport):
        """Valid wire lines of unhandled types are surfaced on stderr."""
        proc = _make_proc()
        chan = _PipeChannel(b'{"type":"pong","wire_version":WIRE_VERSION}\n' + _WIRE_OK)

        with (
            chan.patch_openpty(),
            patch("codeagent.transport.relay.subprocess.Popen", return_value=proc),
            patch("codeagent.transport.relay.os.killpg") as killpg,
        ):
            result = transport._run_with_pty(["zsh", "-c", "true"], timeout=30)

        assert result.returncode == 0
        assert result.stdout == "ok"
        assert "pong" in result.stderr
        killpg.assert_not_called()

    def test_reap_timeout_kills_child(self, transport: RelayTransport):
        """If the child doesn't exit within the reap window, kill it."""
        proc = _make_proc(
            returncode=0,
            wait_side_effect=[subprocess.TimeoutExpired("x", 5), None],
        )
        chan = _PipeChannel()
        chan.close_write()  # immediate EOF → normal reap path

        with (
            chan.patch_openpty(),
            patch("codeagent.transport.relay.subprocess.Popen", return_value=proc),
            patch("codeagent.transport.relay.os.killpg") as killpg,
        ):
            result = transport._run_with_pty(["zsh", "-c", "true"], timeout=30)

        assert result.returncode == 0
        proc.kill.assert_called_once()
        killpg.assert_not_called()

    def test_master_close_failure_tolerated(self, transport: RelayTransport):
        """A failed close of the PTY master in cleanup is ignored."""
        proc = _make_proc()
        chan = _PipeChannel(_WIRE_OK)
        real_close = os.close

        def raising_close(fd):
            if fd == chan.r_fd:
                raise OSError("master close failed")
            return real_close(fd)

        with (
            chan.patch_openpty(),
            patch("codeagent.transport.relay.subprocess.Popen", return_value=proc),
            patch("codeagent.transport.relay.os.close", side_effect=raising_close),
            patch("codeagent.transport.relay.os.killpg") as killpg,
        ):
            result = transport._run_with_pty(["zsh", "-c", "true"], timeout=30)

        assert result.returncode == 0
        assert result.stdout == "ok"
        killpg.assert_not_called()

    def test_popen_failure_closes_slave_fd(self, transport: RelayTransport):
        """Popen failure still cleans up the slave fd in the finally block."""
        slave_fd = os.open(os.devnull, os.O_RDONLY)
        chan = _PipeChannel()
        real_close = os.close
        try:

            def raising_close(fd):
                if fd == slave_fd:
                    raise OSError("slave close failed")
                return real_close(fd)

            with (
                patch("codeagent.transport.relay.pty.openpty", return_value=(chan.r_fd, slave_fd)),
                patch("codeagent.transport.relay.subprocess.Popen", side_effect=OSError("zsh missing")),
                patch("codeagent.transport.relay.os.close", side_effect=raising_close),
            ):
                result = transport._run_with_pty(["zsh", "-c", "true"], timeout=30)
        finally:
            chan.close_write()

        assert result.returncode == 1
        assert "relay execution error" in result.stderr


class TestRelayTransportSurface:
    """RelayTransport API surface: no-op lifecycle + optional execute fields."""

    def test_lifecycle_noops(self, transport: RelayTransport):
        """warm/check/stop are no-ops for the stateless relay transport."""
        host = HostSpec(
            name="r",
            ssh_alias="r.example.com",
            hostnames=("r.example.com",),
        )
        assert transport.warm(host) is None
        assert transport.check(host) is False
        assert transport.stop(host) is None

    def test_execute_optional_fields(self, transport: RelayTransport):
        """execute() encodes agent/model/resume_session_id and applies shell_prefix."""
        host = HostSpec(
            name="r",
            ssh_alias="r.example.com",
            hostnames=("r.example.com",),
            shell_prefix="export K=1",
        )
        request = RunRequest(
            task="deploy",
            workdir="/srv/app",
            backend="opencode",
            agent="smol",
            model="gpt-4o-mini",
            skip_permissions=False,
            timeout=90,
        )
        captured_argv = None

        def mock_run_with_pty(argv, timeout=600):
            nonlocal captured_argv
            captured_argv = argv
            return RunResult(returncode=0, stdout="", stderr="")

        transport._run_with_pty = mock_run_with_pty

        transport.execute(request, host, "/srv/app", session_id="sess-42")

        cmd = captured_argv[2]
        assert "export K=1" in cmd
        assert "r.example.com" in cmd

        m = re.search(r"([A-Za-z0-9+/=]{40,})", cmd)
        assert m is not None, cmd
        wire = json.loads(base64.b64decode(m.group(1)).decode("utf-8"))
        assert wire["task"] == "deploy"
        assert wire["workdir"] == "/srv/app"
        assert wire["backend"] == "opencode"
        assert wire["agent"] == "smol"
        assert wire["model"] == "gpt-4o-mini"
        assert wire["resume_session_id"] == "sess-42"
        assert wire["skip_permissions"] is False
        assert wire["timeout"] == 90
