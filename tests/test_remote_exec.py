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
from codeagent.wire.protocol import WIRE_VERSION


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
    def test_mailbox_result(self, capsys):
        with (
            patch("codeagent.mailbox.cli.main", return_value=None) as mb_main,
        ):
            _handle_mailbox({"args": ["status"]})
        msg = json.loads(capsys.readouterr().out)
        assert msg["type"] == "mailbox_result"
        assert msg["exit_code"] == 0

    def test_mailbox_args_must_be_list(self, capsys):
        _handle_mailbox({"args": "nope"})
        msg = json.loads(capsys.readouterr().out)
        assert msg["type"] == "error"
        assert "must be a list" in msg["message"]

    def test_mailbox_root_validated(self, capsys):
        with patch("codeagent.mailbox.cli.main", return_value=None) as mb_main:
            _handle_mailbox({"args": ["x"], "mailbox_root": "/tmp/ok-root"})
        assert mb_main.call_args[0][0][:2] == ["--mailbox-root", "/tmp/ok-root"]

    def test_mailbox_root_rejected_when_unsafe(self, capsys):
        _handle_mailbox({"args": ["x"], "mailbox_root": "relative/path"})
        msg = json.loads(capsys.readouterr().out)
        assert msg["type"] == "error"
        assert "invalid mailbox_root" in msg["message"]

    def test_mailbox_cli_exception_reported(self, capsys):
        def _boom(argv):
            raise RuntimeError("mailbox exploded")

        with patch("codeagent.mailbox.cli.main", side_effect=_boom):
            _handle_mailbox({"args": ["x"]})
        msg = json.loads(capsys.readouterr().out)
        assert msg["type"] == "mailbox_result"
        assert msg["exit_code"] == 1
        assert "mailbox exploded" in msg["stderr"]

    def test_mailbox_system_exit_code(self, capsys):
        def _exit42(argv):
            raise SystemExit(42)

        with patch("codeagent.mailbox.cli.main", side_effect=_exit42):
            _handle_mailbox({"args": ["x"]})
        msg = json.loads(capsys.readouterr().out)
        assert msg["type"] == "mailbox_result"
        assert msg["exit_code"] == 42


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
            patch("codeagent.mailbox.cli.main", return_value=None),
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
        assert SUPPORTED_COMMANDS == {"run", "ping", "capabilities", "mailbox"}
