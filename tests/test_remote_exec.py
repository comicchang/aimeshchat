"""Tests for codeagent.remote_exec — the wire-protocol helper.

The helper is exercised in-process with a mocked stdin and mocked
runners, so no real backend (codex/omp) is invoked.
"""
from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

import pytest

from codeagent.constants import DEFAULT_EXEC_TIMEOUT, MAX_LINE_LENGTH
from codeagent.domain import RunResult
from codeagent.remote_exec import (
    SUPPORTED_COMMANDS,
    _handle_capabilities,
    _handle_mailbox,
    _handle_ping,
    _handle_run,
    _read_request,
    _send,
    main,
)
from codeagent.wire.protocol import WIRE_VERSION, decode_line


class TestReadRequest:
    """_read_request: stdin JSONL reading + validation."""

    def test_valid_request(self, monkeypatch):
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO('{"wire_version":1,"command":"ping"}\n'),
        )
        req = _read_request()
        assert req == {"wire_version": 1, "command": "ping"}

    def test_eof_returns_none(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        assert _read_request() is None

    def test_invalid_json_sends_error(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", io.StringIO("not json\n"))
        assert _read_request() is None
        err = capsys.readouterr().out
        assert '"type": "error"' in err

    def test_missing_command_field(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", io.StringIO('{"wire_version":1}\n'))
        assert _read_request() is None
        assert "error" in capsys.readouterr().out

    def test_line_too_long_rejected(self, monkeypatch, capsys):
        line = '{"wire_version":1,"command":"ping","x":"' + "a" * (MAX_LINE_LENGTH + 10) + '"}\n'
        monkeypatch.setattr("sys.stdin", io.StringIO(line))
        assert _read_request() is None
        out = capsys.readouterr().out
        assert "exceeds" in out


class TestSend:
    def test_send_writes_json_line(self, capsys):
        _send({"type": "pong", "k": 1})
        out = capsys.readouterr().out
        assert json.loads(out) == {"type": "pong", "k": 1}
        assert out.endswith("\n")


class TestHandlePing:
    def test_pong_shape(self, capsys):
        _handle_ping({})
        msg = json.loads(capsys.readouterr().out)
        assert msg["type"] == "pong"
        assert msg["wire_version"] == WIRE_VERSION
        assert msg["hostname"]
        assert "run" in msg["capabilities"]
        assert "mailbox" in msg["capabilities"]


class TestHandleCapabilities:
    def test_capabilities_shape(self, capsys):
        _handle_capabilities({})
        msg = json.loads(capsys.readouterr().out)
        assert msg["type"] == "capabilities"
        assert "opencode" in msg["backends"]
        assert "resume" in msg["features"]


class TestHandleRun:
    """_handle_run: runner dispatch + wire result emission."""

    @pytest.fixture
    def fake_runner(self):
        def _make(session_id="sess-1", returncode=0, stdout="done"):
            runner = MagicMock()
            runner.run.return_value = RunResult(
                returncode=returncode,
                stdout=stdout,
                stderr="",
                session_id=session_id,
            )
            return runner

        return _make

    def test_run_default_backend(self, fake_runner, tmp_path, capsys):
        runner = fake_runner()
        with (
            patch("codeagent.remote_exec.GoWrapperRunner", return_value=runner),
        ):
            _handle_run({"task": "t", "workdir": str(tmp_path)})
        lines = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
        types = [m["type"] for m in lines]
        assert types == ["accepted", "session", "result"]
        result = lines[-1]
        assert result["stdout"] == "done"
        assert result["exit_code"] == 0
        assert lines[-2]["id"] == "sess-1"

    def test_run_omp_backend(self, fake_runner, tmp_path, capsys):
        runner = fake_runner()
        with patch("codeagent.remote_exec.OMPRunner", return_value=runner):
            _handle_run({"task": "t", "workdir": str(tmp_path), "backend": "omp"})
        types = [json.loads(l)["type"] for l in capsys.readouterr().out.splitlines()]
        assert "result" in types

    def test_run_missing_workdir_errors(self, tmp_path, capsys):
        _handle_run({"task": "t", "workdir": str(tmp_path / "nope")})
        out = capsys.readouterr().out
        assert "workdir not found" in out

    def test_run_timeout_default_used(self, fake_runner, tmp_path):
        runner = fake_runner()
        from codeagent.runners.base import RunnerConfig

        captured = {}

        def _capture(config: RunnerConfig) -> MagicMock:
            captured["timeout"] = config.timeout
            return runner

        with patch("codeagent.remote_exec.GoWrapperRunner", side_effect=_capture):
            _handle_run({"task": "t", "workdir": str(tmp_path)})
        assert captured["timeout"] == DEFAULT_EXEC_TIMEOUT


class TestHandleMailbox:
    """_handle_mailbox: direct MailboxStore dispatch (primary path)."""

    def test_direct_status_dispatch(self, capsys):
        """status subcommand dispatches to MailboxStore.write_status directly."""
        with patch(
            "codeagent.remote_exec._dispatch_mailbox_direct",
            return_value=("status: IDLE", "", 0),
        ) as dm:
            _handle_mailbox({"args": ["status", "--session", "s1", "--agent", "a1", "--state", "IDLE"]})
        dm.assert_called_once()
        msg = json.loads(capsys.readouterr().out)
        assert msg["type"] == "mailbox_result"
        assert msg["exit_code"] == 0
        assert msg["stdout"] == "status: IDLE"
        assert msg["stderr"] == ""

    def test_direct_send_dispatch(self, capsys):
        """send subcommand dispatches to MailboxStore.send directly."""
        with patch(
            "codeagent.remote_exec._dispatch_mailbox_direct",
            return_value=("sent → a1/inbox/id.json", "", 0),
        ) as dm:
            _handle_mailbox({"args": ["send", "--session", "s1", "--from", "m1", "--to", "a1",
                                   "--subject", "hi", "--body", "hello"]})
        dm.assert_called_once()
        msg = json.loads(capsys.readouterr().out)
        assert msg["type"] == "mailbox_result"
        assert msg["exit_code"] == 0

    def test_direct_peek_dispatch(self, capsys):
        """peek subcommand dispatches to MailboxStore.peek directly."""
        peek_result = json.dumps({"pending": 2, "messages": []})
        with patch(
            "codeagent.remote_exec._dispatch_mailbox_direct",
            return_value=(peek_result, "", 0),
        ):
            _handle_mailbox({"args": ["peek", "--session", "s1", "--agent", "a1"]})
        msg = json.loads(capsys.readouterr().out)
        assert msg["type"] == "mailbox_result"
        assert json.loads(msg["stdout"]) == {"pending": 2, "messages": []}

    def test_direct_read_dispatch(self, capsys):
        """read subcommand dispatches to MailboxStore.read directly."""
        with patch(
            "codeagent.remote_exec._dispatch_mailbox_direct",
            return_value=("FROM: w1  KIND: REPORT\nSUBJECT: s\nBODY: b", "", 0),
        ):
            _handle_mailbox({"args": ["read", "--session", "s1", "--agent", "a1", "--owner", "o1"]})
        msg = json.loads(capsys.readouterr().out)
        assert msg["type"] == "mailbox_result"
        assert "FROM: w1" in msg["stdout"]

    def test_direct_clear_dispatch(self, capsys):
        """clear subcommand dispatches to MailboxStore.clear directly."""
        with patch(
            "codeagent.remote_exec._dispatch_mailbox_direct",
            return_value=("cleared 5", "", 0),
        ):
            _handle_mailbox({"args": ["clear", "--session", "s1", "--agent", "a1"]})
        msg = json.loads(capsys.readouterr().out)
        assert msg["type"] == "mailbox_result"
        assert msg["stdout"] == "cleared 5"

    def test_direct_error_returns_stderr(self, capsys):
        """ValueError from MailboxStore propagates as stderr + exit_code=1."""
        with patch(
            "codeagent.remote_exec._dispatch_mailbox_direct",
            return_value=("", "session not found: s1\n", 1),
        ):
            _handle_mailbox({"args": ["send"]})
        msg = json.loads(capsys.readouterr().out)
        assert msg["type"] == "mailbox_result"
        assert msg["exit_code"] == 1
        assert "session not found: s1" in msg["stderr"]

    def test_args_must_be_list(self, capsys):
        _handle_mailbox({"args": "nope"})
        msg = json.loads(capsys.readouterr().out)
        assert msg["type"] == "error"
        assert "must be a list" in msg["message"]

    def test_mailbox_root_validated(self, capsys):
        """Validated mailbox_root is forwarded to dispatch."""
        with patch(
            "codeagent.remote_exec._dispatch_mailbox_direct",
            return_value=("ok", "", 0),
        ) as dm:
            _handle_mailbox({"args": ["stats"], "mailbox_root": "/tmp/ok-root"})
        dm.assert_called_once_with(["stats"], "/tmp/ok-root")

    def test_mailbox_root_rejected_when_unsafe(self, capsys):
        _handle_mailbox({"args": ["x"], "mailbox_root": "relative/path"})
        msg = json.loads(capsys.readouterr().out)
        assert msg["type"] == "error"
        assert "invalid mailbox_root" in msg["message"]

    def test_fallback_to_cli_on_direct_failure(self, capsys):
        """Falls back to CLI when direct dispatch raises an unexpected exception."""
        with (
            patch(
                "codeagent.remote_exec._dispatch_mailbox_direct",
                side_effect=RuntimeError("direct path boom"),
            ),
            patch("codeagent.mailbox.cli.main", return_value=None),
        ):
            _handle_mailbox({"args": ["peek"]})
        msg = json.loads(capsys.readouterr().out)
        assert msg["type"] == "mailbox_result"
        assert msg["exit_code"] == 0

    def test_fallback_to_cli_on_system_exit(self, capsys):
        """Falls back to CLI when direct dispatch raises SystemExit."""
        def _exit42(argv):
            raise SystemExit(42)

        with (
            patch(
                "codeagent.remote_exec._dispatch_mailbox_direct",
                side_effect=SystemExit(42),
            ),
            patch("codeagent.mailbox.cli.main", side_effect=_exit42),
        ):
            _handle_mailbox({"args": ["x"]})
        msg = json.loads(capsys.readouterr().out)
        assert msg["type"] == "mailbox_result"
        assert msg["exit_code"] == 42

    def test_fallback_preserves_response_shape(self, capsys):
        """CLI fallback still produces the standard mailbox_result wire shape."""
        def _exit_msg(argv):
            raise SystemExit("session not found: s1")

        with (
            patch(
                "codeagent.remote_exec._dispatch_mailbox_direct",
                side_effect=Exception("unexpected"),
            ),
            patch("codeagent.mailbox.cli.main", side_effect=_exit_msg),
        ):
            _handle_mailbox({"args": ["send"]})

        msg = decode_line(capsys.readouterr().out)
        assert msg.type == "mailbox_result"
        assert msg.exit_code == 1
        assert "session not found: s1" in msg.payload["stderr"]


class TestMainLoop:
    """main(): ready banner + request dispatch."""

    def test_full_session(self, tmp_path, monkeypatch, capsys):
        reqs = (
            '{"wire_version":1,"command":"ping"}\n'
            '{"wire_version":1,"command":"capabilities"}\n'
            f'{{"wire_version":1,"command":"run","task":"t","workdir":"{tmp_path}","timeout":600}}\n'
            '{"wire_version":1,"command":"mailbox","args":["peek"]}\n'
            '{"wire_version":1,"command":"bogus"}\n'
            '{"wire_version":99,"command":"ping"}\n'
            '{}\n'
        )
        monkeypatch.setattr("sys.stdin", io.StringIO(reqs))
        runner = MagicMock()
        runner.run.return_value = RunResult(returncode=0, stdout="ok", stderr="", session_id=None)

        with (
            patch("codeagent.remote_exec.GoWrapperRunner", return_value=runner),
            patch("codeagent.remote_exec._dispatch_mailbox_direct",
                  return_value=("ok", "", 0)),
        ):
            main()

        messages = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
        assert messages[0]["type"] == "ready"
        types = [m["type"] for m in messages]
        assert types.count("pong") == 1
        assert types.count("capabilities") == 1
        assert types.count("result") == 1
        assert types.count("mailbox_result") == 1
        # unknown command, bad wire version, missing command → 3 errors
        errors = [m for m in messages if m["type"] == "error"]
        assert len(errors) == 3
        assert "unknown command" in errors[0]["message"]
        assert "wire_version" in errors[1]["message"]
        assert "command" in errors[2]["message"]

    def test_supported_commands(self):
        assert SUPPORTED_COMMANDS == {"run", "ping", "capabilities", "mailbox", "stream"}

    def test_main_help_exits_immediately(self, capsys):
        """--help must not hang the JSONL loop (dotai entrypoint check)."""
        main(["--help"])
        out = capsys.readouterr().out
        assert "usage: codeagent-remote-exec" in out

    def test_main_version_exits_immediately(self, capsys):
        """--version prints package version without entering the loop."""
        main(["--version"])
        out = capsys.readouterr().out
        assert out.strip().startswith("codeagent-remote-exec")

    def test_main_no_args_sends_ready(self, monkeypatch, capsys):
        """Wire mode (no argv) still emits the ready banner."""
        sent: list[dict] = []
        monkeypatch.setattr("codeagent.remote_exec._send", sent.append)
        monkeypatch.setattr("codeagent.remote_exec.sys.stdin", io.StringIO(""))
        main([])
        assert any(m.get("type") == "ready" for m in sent)
