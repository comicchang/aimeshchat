"""Tests for wire protocol validation and remote_exec fixes."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codeagent.wire.protocol import (
    MAX_LINE_LENGTH,
    WIRE_VERSION,
    WireMessage,
    CMD_CAPABILITIES,
    CMD_MAILBOX,
    CMD_PING,
    MSG_ACCEPTED,
    MSG_CAPABILITIES,
    MSG_ERROR,
    MSG_MAILBOX_RESULT,
    MSG_PONG,
    MSG_READY,
    MSG_RESULT,
    MSG_SESSION,
    decode_line,
    decode_request,
    encode_line,
    encode_request,
    make_accepted,
    make_capabilities,
    make_capabilities_request,
    make_error,
    make_mailbox_request,
    make_mailbox_result,
    make_ping,
    make_pong,
    make_ready,
    make_request,
    make_result,
    make_session,
)


# ─────────────────────────────────────────────────────────────────────────────
# decode_line — valid messages
# ─────────────────────────────────────────────────────────────────────────────


class TestDecodeLineValid:
    def test_ready(self):
        msg = decode_line('{"type":"ready","wire_version":1}')
        assert msg.type == "ready"
        assert msg.wire_version == 1

    def test_accepted(self):
        msg = decode_line('{"type":"accepted","wire_version":1}')
        assert msg.type == "accepted"
        assert msg.wire_version == 1

    def test_session(self):
        msg = decode_line('{"type":"session","id":"abc-123"}')
        assert msg.type == "session"
        assert msg.session_id == "abc-123"

    def test_result(self):
        raw = '{"type":"result","stdout":"ok","stderr":"","exit_code":0}'
        msg = decode_line(raw)
        assert msg.type == "result"
        assert msg.stdout == "ok"
        assert msg.stderr == ""
        assert msg.exit_code == 0

    def test_error(self):
        msg = decode_line('{"type":"error","message":"boom"}')
        assert msg.type == "error"
        assert msg.ok is False
        assert msg.message == "boom"

    def test_pong(self):
        msg = decode_line('{"type":"pong","wire_version":1}')
        assert msg.type == "pong"
        assert msg.wire_version == 1

    def test_capabilities(self):
        raw = '{"type":"capabilities","wire_version":1,"backends":["omp"]}'
        msg = decode_line(raw)
        assert msg.type == "capabilities"
        assert msg.payload["backends"] == ["omp"]

    def test_bytes_input(self):
        msg = decode_line(b'{"type":"pong","wire_version":1}')
        assert msg.type == "pong"

    def test_extra_fields_preserved(self):
        raw = '{"type":"pong","wire_version":1,"hostname":"h","extra":42}'
        msg = decode_line(raw)
        assert msg.payload["hostname"] == "h"
        assert msg.payload["extra"] == 42

    def test_uses_wire_message_constants(self):
        # Ensure the constants are used correctly in the schema
        from codeagent.wire.protocol import MSG_READY, MSG_RESULT
        assert MSG_READY == "ready"
        assert MSG_RESULT == "result"


# ─────────────────────────────────────────────────────────────────────────────
# decode_line — invalid messages
# ─────────────────────────────────────────────────────────────────────────────


class TestDecodeLineInvalid:
    def test_empty_string(self):
        with pytest.raises(ValueError, match="empty wire line"):
            decode_line("")

    def test_whitespace_only(self):
        with pytest.raises(ValueError, match="empty wire line"):
            decode_line("   ")

    def test_empty_bytes(self):
        with pytest.raises(ValueError, match="empty wire line"):
            decode_line(b"")

    def test_invalid_json(self):
        with pytest.raises(ValueError, match="invalid JSON"):
            decode_line("{not valid json")

    def test_json_array_not_dict(self):
        with pytest.raises(ValueError, match="must be a JSON object"):
            decode_line("[1, 2, 3]")

    def test_json_string_not_dict(self):
        with pytest.raises(ValueError, match="must be a JSON object"):
            decode_line('"hello"')

    def test_json_number_not_dict(self):
        with pytest.raises(ValueError, match="must be a JSON object"):
            decode_line("42")

    def test_missing_type_field(self):
        with pytest.raises(ValueError, match="'type' must be a non-empty string"):
            decode_line('{"wire_version": 1}')

    def test_type_is_empty_string(self):
        with pytest.raises(ValueError, match="'type' must be a non-empty string"):
            decode_line('{"type": ""}')

    def test_type_is_integer(self):
        with pytest.raises(ValueError, match="'type' must be a non-empty string"):
            decode_line('{"type": 42}')

    def test_type_is_null(self):
        with pytest.raises(ValueError, match="'type' must be a non-empty string"):
            decode_line('{"type": null}')

    def test_max_line_length_exceeded(self):
        big = '{"type":"pong","wire_version":1,"x":"' + "a" * (MAX_LINE_LENGTH + 100) + '"}'
        with pytest.raises(ValueError, match="exceeds"):
            decode_line(big)

    def test_exactly_max_line_length_ok(self):
        # Build a line that's exactly at the limit and valid
        padding_len = MAX_LINE_LENGTH - len('{"type":"pong","wire_version":1,"x":""}')
        raw = '{"type":"pong","wire_version":1,"x":"' + "a" * max(padding_len, 0) + '"}'
        if len(raw.encode("utf-8")) <= MAX_LINE_LENGTH:
            msg = decode_line(raw)
            assert msg.type == "pong"


# ─────────────────────────────────────────────────────────────────────────────
# decode_line — per-type field validation
# ─────────────────────────────────────────────────────────────────────────────


class TestDecodeLineFieldValidation:
    def test_accepted_missing_wire_version(self):
        with pytest.raises(ValueError, match="missing required field 'wire_version'"):
            decode_line('{"type":"accepted"}')

    def test_accepted_wire_version_wrong_type(self):
        with pytest.raises(ValueError, match="wire_version.*must be int"):
            decode_line('{"type":"accepted","wire_version":"1"}')

    def test_session_missing_id(self):
        with pytest.raises(ValueError, match="missing required field 'id'"):
            decode_line('{"type":"session"}')

    def test_session_id_wrong_type(self):
        with pytest.raises(ValueError, match="'id'.*must be str"):
            decode_line('{"type":"session","id":123}')

    def test_result_missing_exit_code(self):
        with pytest.raises(ValueError, match="missing required field 'exit_code'"):
            decode_line('{"type":"result","stdout":"","stderr":""}')

    def test_result_exit_code_wrong_type(self):
        with pytest.raises(ValueError, match="exit_code.*must be int"):
            decode_line('{"type":"result","stdout":"","stderr":"","exit_code":"0"}')

    def test_result_missing_stdout(self):
        with pytest.raises(ValueError, match="missing required field 'stdout'"):
            decode_line('{"type":"result","exit_code":0,"stderr":""}')

    def test_error_missing_message(self):
        with pytest.raises(ValueError, match="missing required field 'message'"):
            decode_line('{"type":"error"}')

    def test_error_message_wrong_type(self):
        with pytest.raises(ValueError, match="message.*must be str"):
            decode_line('{"type":"error","message":42}')

    def test_ready_missing_wire_version(self):
        with pytest.raises(ValueError, match="missing required field 'wire_version'"):
            decode_line('{"type":"ready"}')

    def test_pong_missing_wire_version(self):
        with pytest.raises(ValueError, match="missing required field 'wire_version'"):
            decode_line('{"type":"pong"}')

    def test_unknown_type_passes_no_schema_check(self):
        # Unknown types should not be rejected for missing fields
        msg = decode_line('{"type":"custom_thing"}')
        assert msg.type == "custom_thing"


# ─────────────────────────────────────────────────────────────────────────────
# encode_request — valid requests
# ─────────────────────────────────────────────────────────────────────────────


class TestEncodeRequest:
    def test_run_command(self):
        raw = encode_request("run", task="hello", workdir="/tmp", timeout=30)
        obj = json.loads(raw)
        assert obj["command"] == "run"
        assert obj["wire_version"] == WIRE_VERSION
        assert obj["task"] == "hello"
        assert obj["workdir"] == "/tmp"
        assert obj["timeout"] == 30

    def test_ping_command(self):
        raw = encode_request("ping")
        obj = json.loads(raw)
        assert obj == {"wire_version": WIRE_VERSION, "command": "ping"}

    def test_capabilities_command(self):
        raw = encode_request("capabilities")
        obj = json.loads(raw)
        assert obj == {"wire_version": WIRE_VERSION, "command": "capabilities"}

    def test_run_with_optional_fields(self):
        raw = encode_request(
            "run", task="x", workdir="/tmp", timeout=60,
            backend="omp", model="gpt-4"
        )
        obj = json.loads(raw)
        assert obj["backend"] == "omp"
        assert obj["model"] == "gpt-4"

    def test_unknown_command_raises(self):
        with pytest.raises(ValueError, match="unknown command"):
            encode_request("hack")

    def test_empty_command_raises(self):
        with pytest.raises(ValueError, match="command must be a non-empty string"):
            encode_request("")

    def test_non_string_command_raises(self):
        with pytest.raises(ValueError, match="command must be a non-empty string"):
            encode_request(123)  # type: ignore[arg-type]

    def test_run_missing_task(self):
        with pytest.raises(ValueError, match="requires field 'task'"):
            encode_request("run", workdir="/tmp", timeout=30)

    def test_run_missing_workdir(self):
        with pytest.raises(ValueError, match="requires field 'workdir'"):
            encode_request("run", task="x", timeout=30)

    def test_run_missing_timeout(self):
        with pytest.raises(ValueError, match="requires field 'timeout'"):
            encode_request("run", task="x", workdir="/tmp")

    def test_run_task_wrong_type(self):
        with pytest.raises(ValueError, match="task.*must be str"):
            encode_request("run", task=123, workdir="/tmp", timeout=30)  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────────────
# decode_request — valid requests
# ─────────────────────────────────────────────────────────────────────────────


class TestDecodeRequest:
    def test_valid_ping(self):
        obj = decode_request('{"wire_version":1,"command":"ping"}')
        assert obj["command"] == "ping"

    def test_valid_run(self):
        raw = '{"wire_version":1,"command":"run","task":"t","workdir":"/tmp","timeout":30}'
        obj = decode_request(raw)
        assert obj["command"] == "run"
        assert obj["task"] == "t"

    def test_bytes_input(self):
        obj = decode_request(b'{"wire_version":1,"command":"ping"}')
        assert obj["command"] == "ping"

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="empty wire line"):
            decode_request("")

    def test_non_dict_raises(self):
        with pytest.raises(ValueError, match="must be a JSON object"):
            decode_request("[1]")

    def test_missing_command_raises(self):
        with pytest.raises(ValueError, match="'command' must be a non-empty string"):
            decode_request('{"wire_version":1}')

    def test_unknown_command_raises(self):
        with pytest.raises(ValueError, match="unknown command"):
            decode_request('{"command":"hack"}')

    def test_run_missing_task_raises(self):
        with pytest.raises(ValueError, match="missing required field 'task'"):
            decode_request('{"command":"run","workdir":"/tmp","timeout":30}')

    def test_max_line_length_exceeded(self):
        big = '{"command":"ping","x":"' + "a" * (MAX_LINE_LENGTH + 100) + '"}'
        with pytest.raises(ValueError, match="exceeds"):
            decode_request(big)


# ─────────────────────────────────────────────────────────────────────────────
# encode_line / decode_line round-trip
# ─────────────────────────────────────────────────────────────────────────────


class TestRoundTrip:
    def test_encode_then_decode_result(self):
        obj = make_result(stdout="hello", stderr="", exit_code=0)
        wire = encode_line(obj)
        msg = decode_line(wire)
        assert msg.type == "result"
        assert msg.stdout == "hello"
        assert msg.exit_code == 0

    def test_encode_then_decode_error(self):
        obj = make_error("bad")
        wire = encode_line(obj)
        msg = decode_line(wire)
        assert msg.type == "error"
        assert msg.message == "bad"

    def test_encode_then_decode_session(self):
        obj = make_session("sess-42")
        wire = encode_line(obj)
        msg = decode_line(wire)
        assert msg.session_id == "sess-42"


# ─────────────────────────────────────────────────────────────────────────────
# WireMessage properties
# ─────────────────────────────────────────────────────────────────────────────


class TestWireMessage:
    def test_ok_true_for_non_error(self):
        msg = WireMessage(type="result", payload={})
        assert msg.ok is True

    def test_ok_false_for_error(self):
        msg = WireMessage(type="error", payload={"message": "x"})
        assert msg.ok is False

    def test_default_properties(self):
        msg = WireMessage(type="accepted", payload={})
        assert msg.message == ""
        assert msg.session_id is None
        assert msg.stdout == ""
        assert msg.stderr == ""
        assert msg.exit_code == 0
        assert msg.wire_version == 0

    def test_frozen(self):
        msg = WireMessage(type="pong", payload={})
        with pytest.raises(AttributeError):
            msg.type = "other"  # type: ignore[misc]


# ─────────────────────────────────────────────────────────────────────────────
# remote_exec — _read_request with max line length
# ─────────────────────────────────────────────────────────────────────────────


class TestRemoteExecReadRequest:
    def _call_read_request(self, stdin_content: str) -> tuple[subprocess.CompletedProcess, list[dict], dict | None]:
        """Run _read_request in a subprocess with controlled stdin.

        Returns (proc, sent_messages, result).
        sent_messages = JSON objects written by _send() (i.e. error responses).
        result = the return value of _read_request (last line of stdout).
        """
        code = (
            "import sys, json\n"
            "sys.path.insert(0, 'src')\n"
            "from codeagent.remote_exec import _read_request, _send\n"
            "# capture _send output separately\n"
            "sent = []\n"
            "import codeagent.remote_exec as re\n"
            "re._send = lambda obj: sent.append(obj)\n"
            "result = _read_request()\n"
            "# Print sent messages first, then result on last line\n"
            "for m in sent:\n"
            "    print(json.dumps(m))\n"
            "print('---RESULT---')\n"
            "print(json.dumps(result))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            input=stdin_content,
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        sent: list[dict] = []
        result: dict | None = None
        lines = proc.stdout.strip().split("\n")
        # Parse lines: everything before ---RESULT--- is sent messages, after is result
        try:
            sep = lines.index("---RESULT---")
            for l in lines[:sep]:
                if l:
                    sent.append(json.loads(l))
            if sep + 1 < len(lines):
                result = json.loads(lines[sep + 1])
        except (ValueError, json.JSONDecodeError):
            pass
        return proc, sent, result

    def test_valid_request(self):
        proc, sent, result = self._call_read_request('{"command":"ping"}\n')
        assert proc.returncode == 0
        assert result is not None
        assert result["command"] == "ping"
        assert sent == []  # no error messages

    def test_empty_stdin_returns_none(self):
        proc, sent, result = self._call_read_request("")
        assert proc.returncode == 0
        assert result is None
        assert sent == []  # no error on EOF

    def test_invalid_json_returns_none(self):
        proc, sent, result = self._call_read_request("{bad json}\n")
        assert proc.returncode == 0
        assert result is None
        # Should have sent an error message
        assert len(sent) == 1
        assert sent[0]["type"] == "error"
        assert "invalid JSON" in sent[0]["message"]

    def test_non_dict_returns_none(self):
        proc, sent, result = self._call_read_request("[1,2,3]\n")
        assert proc.returncode == 0
        assert result is None
        assert len(sent) == 1
        assert sent[0]["type"] == "error"
        assert "JSON object" in sent[0]["message"]

    def test_max_line_length_exceeded(self):
        big_line = '{"x":"' + "a" * (MAX_LINE_LENGTH + 100) + '"}\n'
        proc, sent, result = self._call_read_request(big_line)
        assert proc.returncode == 0
        assert result is None
        assert len(sent) == 1
        assert sent[0]["type"] == "error"
        assert "exceeds" in sent[0]["message"]


# ─────────────────────────────────────────────────────────────────────────────
# remote_exec — main() command dispatch
# ─────────────────────────────────────────────────────────────────────────────


class TestRemoteExecMain:
    def _run_main(self, stdin_lines: list[str]) -> tuple[list[dict], int]:
        """Run remote_exec.main() in a subprocess, return (output_lines, exit_code)."""
        code = (
            "import sys\n"
            "sys.path.insert(0, 'src')\n"
            "import codeagent.remote_exec as re\n"
            "# Capture stdout\n"
            "captured = []\n"
            "_orig_send = re._send\n"
            "def _mock_send(obj):\n"
            "    captured.append(obj)\n"
            "re._send = _mock_send\n"
            "# Mock stdin\n"
            "lines = sys.stdin.read().splitlines(keepends=True)\n"
            "idx = [0]\n"
            "def _mock_readline():\n"
            "    if idx[0] >= len(lines):\n"
            "        return ''\n"
            "    line = lines[idx[0]]\n"
            "    idx[0] += 1\n"
            "    return line\n"
            "import io\n"
            "re.sys = type(re.sys)('sys')\n"
            "re.sys.stdin = type('Stdin', (), {'readline': lambda self: _mock_readline()})()\n"
            "re.sys.stdout = re.sys.__stdout__ if hasattr(re.sys, '__stdout__') else sys.stdout\n"
            "re.main()\n"
            "import json\n"
            "print(json.dumps(captured))\n"
        )
        stdin_data = "\n".join(stdin_lines)
        # Use a simpler approach: just test _read_request and main dispatch logic directly
        proc = subprocess.run(
            [sys.executable, "-c", code],
            input=stdin_data,
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parents[1]),
            timeout=5,
        )
        if proc.returncode != 0:
            # If the inline approach is too complex, just check error output
            return [], proc.returncode
        try:
            output = json.loads(proc.stdout.strip())
            return output, proc.returncode
        except (json.JSONDecodeError, ValueError):
            return [], proc.returncode

    def test_missing_command_errors(self):
        """Verify that a request without 'command' produces an error, not a silent 'run' default."""
        # Test the logic directly by importing and checking main dispatch
        import codeagent.remote_exec as re

        sent: list[dict] = []
        re._send = lambda obj: sent.append(obj)

        # Simulate: read_request returns a dict with no 'command' key
        original = re._read_request
        call_count = [0]

        def mock_read():
            call_count[0] += 1
            if call_count[0] == 1:
                return {"wire_version": 1}  # missing command
            return None  # EOF

        re._read_request = mock_read
        try:
            re.main()
        except Exception:
            pass

        # Should have ready + error about missing command (not silently treated as "run")
        assert len(sent) >= 2
        assert sent[0]["type"] == "ready"
        assert sent[1]["type"] == "error"
        assert "command" in sent[1]["message"].lower()

    def test_version_too_high_rejected(self):
        """Verify that wire_version > supported produces an error."""
        import codeagent.remote_exec as re

        sent: list[dict] = []
        re._send = lambda obj: sent.append(obj)

        original = re._read_request
        call_count = [0]

        def mock_read():
            call_count[0] += 1
            if call_count[0] == 1:
                return {"wire_version": 999, "command": "ping"}
            return None

        re._read_request = mock_read
        try:
            re.main()
        except Exception:
            pass

        # ready + version error
        assert sent[0]["type"] == "ready"
        assert any(m.get("type") == "error" and "wire_version" in m.get("message", "") for m in sent[1:])


# ─────────────────────────────────────────────────────────────────────────────
# remote_exec — _run_go_wrapper tempfile safety
# ─────────────────────────────────────────────────────────────────────────────


class TestRunGoWrapperTempfile:
    def test_uses_named_tempfile_not_mktemp(self):
        """Verify the source uses NamedTemporaryFile, not mktemp."""
        src = Path(__file__).resolve().parents[1] / "src" / "codeagent" / "runners" / "go_wrapper.py"
        code = src.read_text()
        # mktemp should NOT appear (was the bug)
        assert "mktemp" not in code, "go_wrapper.py still uses unsafe mktemp()"
        # NamedTemporaryFile should appear
        assert "NamedTemporaryFile" in code

    def test_output_file_always_cleaned(self):
        """Verify output_file cleanup happens in _cleanup."""
        src = Path(__file__).resolve().parents[1] / "src" / "codeagent" / "runners" / "go_wrapper.py"
        code = src.read_text()
        # _cleanup should handle output file deletion
        assert "_cleanup" in code
        assert "_output_file" in code


# ─────────────────────────────────────────────────────────────────────────────
# remote_exec — _run_omp json.dumps fix
# ─────────────────────────────────────────────────────────────────────────────


class TestRunOmpJsonFix:
    def test_mode_is_literal_json_not_quoted(self):
        """Verify --mode json is passed as literal, not json.dumps('json') which gives '\"json\"'."""
        src = Path(__file__).resolve().parents[1] / "src" / "codeagent" / "runners" / "omp.py"
        code = src.read_text()
        # json.dumps("json") produces '"json"' — should not appear
        assert 'json.dumps("json")' not in code
        assert "json.dumps('json')" not in code
        # Should have literal "json" in the cmd list
        assert '"--mode", "json"' in code or "'--mode', 'json'" in code


# ─────────────────────────────────────────────────────────────────────────────
# remote_exec — process group isolation
# ─────────────────────────────────────────────────────────────────────────────


class TestProcessGroupIsolation:
    def test_start_new_session_in_base_runner(self):
        """Verify start_new_session=True is used in subprocess.run in BaseRunner."""
        src = Path(__file__).resolve().parents[1] / "src" / "codeagent" / "runners" / "base.py"
        code = src.read_text()
        # Should have start_new_session=True
        assert "start_new_session=True" in code

    def test_killpg_in_base_runner_timeout(self):
        """Verify os.killpg is called on TimeoutExpired in BaseRunner."""
        src = Path(__file__).resolve().parents[1] / "src" / "codeagent" / "runners" / "base.py"
        code = src.read_text()
        assert "os.killpg" in code
        assert "TimeoutExpired" in code

    def test_remote_exec_uses_runners_not_subprocess(self):
        """Verify remote_exec._handle_run delegates to runners, not raw subprocess."""
        src = Path(__file__).resolve().parents[1] / "src" / "codeagent" / "remote_exec.py"
        code = src.read_text()
        # Should import runners
        assert "GoWrapperRunner" in code
        assert "OMPRunner" in code
        # Should NOT have raw subprocess handling
        assert "_run_go_wrapper" not in code
        assert "_run_omp" not in code
        assert "subprocess.Popen" not in code


class TestMakeRequest:
    """make_request builds correct wire dicts."""

    def test_skills_included_when_set(self):
        req = make_request(command="run", task="t", workdir="/w", timeout=10, skills="my-skills")
        assert req["skills"] == "my-skills"

    def test_skills_omitted_when_none(self):
        req = make_request(command="run", task="t", workdir="/w", timeout=10)
        assert "skills" not in req

    def test_skills_omitted_when_empty(self):
        req = make_request(command="run", task="t", workdir="/w", timeout=10, skills="")
        assert "skills" not in req


class TestWireFactories:
    """Test all wire protocol factory functions."""

    def test_make_ping(self):
        req = make_ping()
        assert req["command"] == CMD_PING
        assert "wire_version" in req

    def test_make_capabilities_request(self):
        req = make_capabilities_request()
        assert req["command"] == CMD_CAPABILITIES

    def test_make_mailbox_request(self):
        req = make_mailbox_request(args=["send", "--session", "s1"])
        assert req["command"] == CMD_MAILBOX
        assert req["args"] == ["send", "--session", "s1"]

    def test_make_mailbox_request_with_root(self):
        req = make_mailbox_request(args=["send"], mailbox_root="/tmp/test")
        assert req["mailbox_root"] == "/tmp/test"

    def test_make_ready(self):
        msg = make_ready()
        assert msg["type"] == MSG_READY
        assert "wire_version" in msg
        assert "package_version" in msg

    def test_make_accepted(self):
        msg = make_accepted()
        assert msg["type"] == MSG_ACCEPTED

    def test_make_session(self):
        msg = make_session("test-id")
        assert msg["type"] == MSG_SESSION
        assert msg["id"] == "test-id"

    def test_make_result(self):
        msg = make_result(stdout="ok", exit_code=0)
        assert msg["type"] == MSG_RESULT
        assert msg["stdout"] == "ok"

    def test_make_error(self):
        msg = make_error("test error")
        assert msg["type"] == MSG_ERROR
        assert msg["message"] == "test error"

    def test_make_pong(self):
        msg = make_pong(hostname="test", capabilities=["run"])
        assert msg["type"] == MSG_PONG
        assert msg["hostname"] == "test"

    def test_make_capabilities(self):
        msg = make_capabilities(backends=["codex"], features=["resume"])
        assert msg["type"] == MSG_CAPABILITIES
        assert msg["backends"] == ["codex"]

    def test_make_mailbox_result(self):
        msg = make_mailbox_result(stdout="ok", stderr="", exit_code=0)
        assert msg["type"] == MSG_MAILBOX_RESULT
        assert msg["stdout"] == "ok"
