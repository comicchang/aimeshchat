"""Tests for the runner layer — GoWrapperRunner and OMPRunner."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from codeagent.domain import RunRequest, RunResult
from codeagent.runners.base import BaseRunner, RunnerConfig
from codeagent.runners.go_wrapper import GoWrapperRunner
from codeagent.runners.omp import OMPRunner


# ── helpers ──────────────────────────────────────────────────────────────


def _completed(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> mock.MagicMock:
    """Create a mock proc that behaves like a Popen process object."""
    proc = mock.MagicMock()
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    proc.pid = 12345
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


# ── GoWrapperRunner ─────────────────────────────────────────────────────


class TestGoWrapperRunner:
    """Tests for GoWrapperRunner._build_cmd and _parse_output."""

    def _runner(self, **config_kw) -> GoWrapperRunner:
        return GoWrapperRunner(config=RunnerConfig(**config_kw))

    # -- _build_cmd -------------------------------------------------------

    def test_basic_cmd(self, tmp_path: Path) -> None:
        r = self._runner(binary="/usr/local/bin/wrapper")
        req = RunRequest(task="fix the bug", workdir="/src")
        cmd = r._build_cmd(req)
        assert cmd[0] == "/usr/local/bin/wrapper"
        assert "--skip-permissions" in cmd
        assert "fix the bug" in cmd
        assert "/src" in cmd

    def test_resume_cmd(self) -> None:
        r = self._runner(binary="/usr/bin/wrapper")
        req = RunRequest(task="continue", resume_session_id="backend-sess-42")
        cmd = r._build_cmd(req)
        assert cmd[1] == "resume"
        assert "backend-sess-42" in cmd
        # session_key (namespace) should NOT be used
        assert req.session_key is None

    def test_resume_uses_session_id_not_session_key(self) -> None:
        r = self._runner(binary="/usr/bin/wrapper")
        req = RunRequest(
            task="continue",
            session_key="namespace-key",
            resume_session_id="backend-id-99",
        )
        cmd = r._build_cmd(req)
        assert "backend-id-99" in cmd
        assert "namespace-key" not in cmd

    def test_new_session_overrides_resume(self) -> None:
        r = self._runner(binary="/usr/bin/wrapper")
        req = RunRequest(task="start fresh", resume_session_id="backend-sess-42", new_session=True)
        cmd = r._build_cmd(req)
        # "resume" should NOT appear when new_session=True
        assert "resume" not in cmd

    def test_backend_flag(self) -> None:
        r = self._runner(binary="/usr/bin/wrapper")
        req = RunRequest(task="do it", backend="claude")
        cmd = r._build_cmd(req)
        assert "--backend" in cmd
        idx = cmd.index("--backend")
        assert cmd[idx + 1] == "claude"

    def test_agent_flag(self) -> None:
        r = self._runner(binary="/usr/bin/wrapper")
        req = RunRequest(task="do it", agent="coder")
        cmd = r._build_cmd(req)
        assert "--agent" in cmd
        assert cmd[cmd.index("--agent") + 1] == "coder"

    def test_model_flag(self) -> None:
        r = self._runner(binary="/usr/bin/wrapper")
        req = RunRequest(task="do it", model="opus")
        cmd = r._build_cmd(req)
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "opus"

    def test_skills_flag(self) -> None:
        r = self._runner(binary="/usr/bin/wrapper")
        req = RunRequest(task="do it", skills="refactor,test")
        cmd = r._build_cmd(req)
        assert "--skills" in cmd
        assert cmd[cmd.index("--skills") + 1] == "refactor,test"

    def test_output_file_created(self, tmp_path: Path) -> None:
        r = self._runner(binary="/usr/bin/wrapper", output_dir=tmp_path)
        req = RunRequest(task="do it")
        cmd = r._build_cmd(req)
        assert "--output" in cmd
        idx = cmd.index("--output")
        output_path = Path(cmd[idx + 1])
        assert output_path.parent == tmp_path

    def test_no_workdir_omitted(self) -> None:
        r = self._runner(binary="/usr/bin/wrapper")
        req = RunRequest(task="do it", workdir="")
        cmd = r._build_cmd(req)
        # task is the last positional
        assert cmd[-1] == "do it"
        # workdir should not be appended
        assert cmd.count("do it") == 1

    # -- _parse_output ----------------------------------------------------

    def test_parse_json_output(self, tmp_path: Path) -> None:
        r = self._runner(binary="/usr/bin/wrapper")
        # Simulate the output file
        out_file = tmp_path / "go_out_123.json"
        out_file.write_text(
            json.dumps(
                {
                    "session_id": "sess-42",
                    "task_id": "task-1",
                    "pid": 9999,
                    "backend": "claude",
                    "status": "ok",
                    "error": "",
                }
            )
        )
        r._output_file = out_file

        proc = _completed(returncode=0, stdout="done", stderr="")
        result = r._parse_output(proc, RunRequest(task="x"))
        assert result.session_id == "sess-42"
        assert result.backend == "claude"
        assert result.returncode == 0

    def test_parse_stderr_fallback(self) -> None:
        r = self._runner(binary="/usr/bin/wrapper")
        # No output file
        r._output_file = Path("/nonexistent")

        stderr = "SESSION_ID: sess-99\nSelected backend: codex\n"
        proc = _completed(returncode=0, stdout="output", stderr=stderr)
        result = r._parse_output(proc, RunRequest(task="x"))
        assert result.session_id == "sess-99"
        assert result.backend == "codex"

    def test_parse_error_status(self, tmp_path: Path) -> None:
        r = self._runner(binary="/usr/bin/wrapper")
        out_file = tmp_path / "go_out_err.json"
        out_file.write_text(
            json.dumps(
                {
                    "session_id": "sess-err",
                    "backend": "claude",
                    "status": "error",
                    "error": "backend crashed",
                }
            )
        )
        r._output_file = out_file

        proc = _completed(returncode=1, stdout="", stderr="")
        result = r._parse_output(proc, RunRequest(task="x"))
        assert result.returncode == 1
        assert result.stderr == "backend crashed"

    def test_parse_malformed_json_ignored(self, tmp_path: Path) -> None:
        r = self._runner(binary="/usr/bin/wrapper")
        out_file = tmp_path / "go_out_bad.json"
        out_file.write_text("NOT JSON")
        r._output_file = out_file

        stderr = "SESSION_ID: fallback-id\n"
        proc = _completed(returncode=0, stdout="", stderr=stderr)
        result = r._parse_output(proc, RunRequest(task="x"))
        assert result.session_id == "fallback-id"

    def test_output_file_cleaned_up(self, tmp_path: Path) -> None:
        r = self._runner(binary="/usr/bin/wrapper")
        out_file = tmp_path / "go_out_clean.json"
        out_file.write_text(json.dumps({"session_id": "s1", "backend": "b1"}))
        r._output_file = out_file

        proc = _completed(returncode=0, stdout="", stderr="")
        r._parse_output(proc, RunRequest(task="x"))
        assert not out_file.exists()

    # -- run() integration (mocked subprocess) ----------------------------

    def test_run_success(self, tmp_path: Path) -> None:
        out_file = tmp_path / "go_out_run.json"
        out_file.write_text(
            json.dumps({"session_id": "s1", "backend": "claude", "status": "ok"})
        )

        mock_popen = mock.MagicMock(return_value=_completed(0, "ok", ""))
        with mock.patch("subprocess.Popen", mock_popen):
            r = self._runner(binary="/usr/bin/wrapper", output_dir=tmp_path)
            # Patch _build_cmd to set _output_file to our known path
            with mock.patch.object(
                r,
                "_build_cmd",
                wraps=r._build_cmd,
            ) as m:
                # We need to manually set _output_file since the mock wraps
                orig_build = r._build_cmd

                def patched_build(req):
                    cmd = orig_build(req)
                    r._output_file = out_file
                    return cmd

                with mock.patch.object(r, "_build_cmd", side_effect=patched_build):
                    result = r.run(RunRequest(task="fix it"))
                    assert result.returncode == 0
                    assert result.session_id == "s1"
                    assert result.backend == "claude"

    def test_run_binary_not_found(self) -> None:
        r = self._runner(binary="/nonexistent/binary")
        result = r.run(RunRequest(task="hello"))
        assert result.returncode == 127
        assert "No such file" in result.stderr or "not found" in result.stderr.lower()

    def test_run_timeout(self) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.pid = 9999
        exc = subprocess.TimeoutExpired(cmd="wrapper", timeout=5)
        exc.proc = mock_proc

        mock_popen = mock.MagicMock()
        mock_popen.return_value = mock_proc
        mock_proc.communicate.side_effect = exc

        with mock.patch("subprocess.Popen", mock_popen):
            r = self._runner(binary="/usr/bin/wrapper")
            r.config.timeout = 5
            result = r.run(RunRequest(task="long task"))
            assert result.returncode == -1
            assert "timeout" in result.stderr.lower()

    def test_run_os_error(self) -> None:
        mock_popen = mock.MagicMock(side_effect=OSError("permission denied"))
        with mock.patch("subprocess.Popen", mock_popen):
            r = self._runner(binary="/usr/bin/wrapper")
            result = r.run(RunRequest(task="hello"))
            assert result.returncode == 1
            assert "permission denied" in result.stderr

    def test_output_file_cleanup_on_success(self, tmp_path: Path) -> None:
        """Output temp file should be cleaned up after successful run."""
        out_file = tmp_path / "go_out_clean.json"
        out_file.write_text(json.dumps({"session_id": "s1", "backend": "claude", "status": "ok"}))

        mock_popen = mock.MagicMock(return_value=_completed(0, "ok", ""))
        with mock.patch("subprocess.Popen", mock_popen):
            r = self._runner(binary="/usr/bin/wrapper", output_dir=tmp_path)
            orig_build = r._build_cmd

            def patched_build(req):
                cmd = orig_build(req)
                r._output_file = out_file
                return cmd

            with mock.patch.object(r, "_build_cmd", side_effect=patched_build):
                result = r.run(RunRequest(task="fix it"))
                assert result.returncode == 0
                # Output file should be cleaned up by _cleanup
                assert not out_file.exists()

    def test_output_file_cleanup_on_failure(self, tmp_path: Path) -> None:
        """Output temp file should be cleaned up even when process fails."""
        out_file = tmp_path / "go_out_fail.json"
        out_file.write_text(json.dumps({"session_id": "s1", "backend": "claude", "status": "error", "error": "crash"}))

        mock_popen = mock.MagicMock(return_value=_completed(1, "", "error"))
        with mock.patch("subprocess.Popen", mock_popen):
            r = self._runner(binary="/usr/bin/wrapper", output_dir=tmp_path)
            orig_build = r._build_cmd

            def patched_build(req):
                cmd = orig_build(req)
                r._output_file = out_file
                return cmd

            with mock.patch.object(r, "_build_cmd", side_effect=patched_build):
                result = r.run(RunRequest(task="fix it"))
                assert result.returncode == 1
                # Output file should be cleaned up by _cleanup
                assert not out_file.exists()

    def test_output_file_cleanup_on_timeout(self, tmp_path: Path) -> None:
        """Output temp file should be cleaned up on timeout."""
        out_file = tmp_path / "go_out_timeout.json"
        out_file.write_text("{}")

        mock_proc = mock.MagicMock()
        mock_proc.pid = 9999
        exc = subprocess.TimeoutExpired(cmd="wrapper", timeout=5)
        exc.proc = mock_proc

        mock_popen = mock.MagicMock()
        mock_popen.return_value = mock_proc
        mock_proc.communicate.side_effect = exc

        with mock.patch("subprocess.Popen", mock_popen):
            r = self._runner(binary="/usr/bin/wrapper", output_dir=tmp_path)
            orig_build = r._build_cmd

            def patched_build(req):
                cmd = orig_build(req)
                r._output_file = out_file
                return cmd

            with mock.patch.object(r, "_build_cmd", side_effect=patched_build):
                result = r.run(RunRequest(task="long task"))
                assert result.returncode == -1
                # Output file should be cleaned up by _cleanup
                assert not out_file.exists()


# ── OMPRunner ────────────────────────────────────────────────────────────


class TestOMPRunner:
    """Tests for OMPRunner._build_cmd, _parse_output, and prompt file handling."""

    def _runner(self, **config_kw) -> OMPRunner:
        return OMPRunner(config=RunnerConfig(**config_kw))

    # -- _build_cmd -------------------------------------------------------

    def test_basic_cmd(self) -> None:
        r = self._runner(binary="/usr/local/bin/omp")
        req = RunRequest(task="explain this", workdir="/src")
        cmd = r._build_cmd(req)
        assert cmd[0] == "/usr/local/bin/omp"
        assert "--print" in cmd
        assert "--mode" in cmd
        # Mode must be literal "json", not json.dumps("json")
        mode_idx = cmd.index("--mode")
        assert cmd[mode_idx + 1] == "json"
        assert "--cwd" in cmd
        assert "/src" in cmd
        assert "--auto-approve" in cmd
        # Prompt via @-file
        assert any(arg.startswith("@") for arg in cmd)
        # Clean up
        r._cleanup_prompt_file()

    def test_mode_is_literal_json(self) -> None:
        """Verify --mode is literal 'json', not json.dumps('json') which would be '"json"'."""
        r = self._runner(binary="/usr/bin/omp")
        req = RunRequest(task="test")
        cmd = r._build_cmd(req)
        mode_idx = cmd.index("--mode")
        mode_value = cmd[mode_idx + 1]
        assert mode_value == "json", f"expected literal 'json', got: {mode_value!r}"
        assert not mode_value.startswith('"'), "should not be json.dumps output"
        r._cleanup_prompt_file()

    def test_resume_cmd(self) -> None:
        r = self._runner(binary="/usr/bin/omp")
        req = RunRequest(task="continue", workdir="/src", resume_session_id="backend-sess-1")
        cmd = r._build_cmd(req)
        assert "--resume" in cmd
        assert cmd[cmd.index("--resume") + 1] == "backend-sess-1"
        r._cleanup_prompt_file()

    def test_resume_uses_session_id_not_session_key(self) -> None:
        r = self._runner(binary="/usr/bin/omp")
        req = RunRequest(
            task="continue",
            session_key="namespace-key",
            resume_session_id="backend-id-99",
        )
        cmd = r._build_cmd(req)
        assert "backend-id-99" in cmd
        assert "namespace-key" not in cmd
        r._cleanup_prompt_file()

    def test_no_resume_when_new_session(self) -> None:
        r = self._runner(binary="/usr/bin/omp")
        req = RunRequest(
            task="fresh start", resume_session_id="old-sess", new_session=True
        )
        cmd = r._build_cmd(req)
        assert "--resume" not in cmd
        r._cleanup_prompt_file()

    def test_model_flag(self) -> None:
        r = self._runner(binary="/usr/bin/omp")
        req = RunRequest(task="do it", model="gpt-5")
        cmd = r._build_cmd(req)
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "gpt-5"
        r._cleanup_prompt_file()

    def test_auto_approve_when_skip_permissions(self) -> None:
        """--auto-approve should be present when skip_permissions=True (default)."""
        r = self._runner(binary="/usr/bin/omp")
        req = RunRequest(task="do it")  # skip_permissions defaults to True
        cmd = r._build_cmd(req)
        assert "--auto-approve" in cmd
        r._cleanup_prompt_file()

    def test_no_auto_approve_when_not_skip_permissions(self) -> None:
        """--auto-approve should be absent when skip_permissions=False."""
        r = self._runner(binary="/usr/bin/omp")
        req = RunRequest(task="do it", skip_permissions=False)
        cmd = r._build_cmd(req)
        assert "--auto-approve" not in cmd
        r._cleanup_prompt_file()

    def test_prompt_file_is_0600(self) -> None:
        r = self._runner(binary="/usr/bin/omp")
        req = RunRequest(task="secret task")
        r._build_cmd(req)
        path = r._prompt_file
        try:
            st = os.stat(path)
            mode = stat.S_IMODE(st.st_mode)
            assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
        finally:
            r._cleanup_prompt_file()

    def test_prompt_file_contains_task(self) -> None:
        r = self._runner(binary="/usr/bin/omp")
        task_text = "Fix the authentication bug\n\nLine 42 is wrong."
        req = RunRequest(task=task_text)
        r._build_cmd(req)
        path = r._prompt_file
        try:
            content = Path(path).read_text(encoding="utf-8")
            assert content == task_text
        finally:
            r._cleanup_prompt_file()

    def test_prompt_file_cleaned_after_parse(self) -> None:
        r = self._runner(binary="/usr/bin/omp")
        req = RunRequest(task="test cleanup")
        r._build_cmd(req)
        path = r._prompt_file
        assert os.path.exists(path)

        proc = _completed(0, '{"type":"agent_end"}\n', "")
        r._parse_output(proc, req)
        assert not os.path.exists(path)

    def test_no_workdir_omits_cwd(self) -> None:
        r = self._runner(binary="/usr/bin/omp")
        req = RunRequest(task="do it", workdir="")
        cmd = r._build_cmd(req)
        assert "--cwd" not in cmd
        r._cleanup_prompt_file()

    # -- _parse_output ----------------------------------------------------

    def test_parse_session_and_end(self) -> None:
        r = self._runner(binary="/usr/bin/omp")
        stdout = (
            '{"type": "session", "id": "sess-77"}\n'
            '{"type": "assistant", "message_end": {"message": "Hello world"}}\n'
            '{"type": "agent_end"}\n'
        )
        proc = _completed(0, stdout, "")
        result = r._parse_output(proc, RunRequest(task="x"))
        assert result.session_id == "sess-77"
        assert result.stdout == "Hello world"
        assert result.returncode == 0
        r._cleanup_prompt_file()

    def test_parse_agent_end_stops_early(self) -> None:
        r = self._runner(binary="/usr/bin/omp")
        stdout = (
            '{"type": "session", "id": "s1"}\n'
            '{"type": "agent_end"}\n'
            '{"type": "assistant", "message_end": {"message": "ignored"}}\n'
        )
        proc = _completed(0, stdout, "")
        result = r._parse_output(proc, RunRequest(task="x"))
        assert result.session_id == "s1"
        # Message after agent_end should be ignored
        assert result.stdout != "ignored"
        r._cleanup_prompt_file()

    def test_parse_nonzero_returncode(self) -> None:
        r = self._runner(binary="/usr/bin/omp")
        proc = _completed(1, "", "error: model not found")
        result = r._parse_output(proc, RunRequest(task="x"))
        assert result.returncode == 1
        assert "model not found" in result.stderr
        r._cleanup_prompt_file()

    def test_parse_malformed_json_lines_skipped(self) -> None:
        r = self._runner(binary="/usr/bin/omp")
        stdout = (
            "not json\n"
            '{"type": "session", "id": "s2"}\n'
            "also not json\n"
            '{"type": "agent_end"}\n'
        )
        proc = _completed(0, stdout, "")
        result = r._parse_output(proc, RunRequest(task="x"))
        assert result.session_id == "s2"
        r._cleanup_prompt_file()

    def test_parse_empty_output(self) -> None:
        r = self._runner(binary="/usr/bin/omp")
        proc = _completed(0, "", "")
        result = r._parse_output(proc, RunRequest(task="x"))
        assert result.session_id is None
        assert result.stdout == ""
        r._cleanup_prompt_file()

    def test_parse_preserves_stdout_when_no_message_end(self) -> None:
        """If no message_end is found, keep original stdout."""
        r = self._runner(binary="/usr/bin/omp")
        stdout = '{"type": "session", "id": "s3"}\n'
        proc = _completed(0, stdout, "some raw output")
        result = r._parse_output(proc, RunRequest(task="x"))
        assert result.session_id == "s3"
        # stdout should remain the original since no message_end was found
        assert result.stdout == stdout
        r._cleanup_prompt_file()

    # -- run() integration (mocked subprocess) ----------------------------

    def test_run_success(self) -> None:
        stdout = (
            '{"type": "session", "id": "s-run"}\n'
            '{"type": "assistant", "message_end": {"message": "Done!"}}\n'
            '{"type": "agent_end"}\n'
        )
        mock_popen = mock.MagicMock(return_value=_completed(0, stdout, ""))
        with mock.patch("subprocess.Popen", mock_popen):
            r = self._runner(binary="/usr/bin/omp")
            result = r.run(RunRequest(task="fix it", workdir="/code"))
            assert result.returncode == 0
            assert result.session_id == "s-run"
            assert result.stdout == "Done!"

    def test_run_binary_not_found(self) -> None:
        r = self._runner(binary="/nonexistent/omp")
        result = r.run(RunRequest(task="hello"))
        assert result.returncode == 127

    def test_run_timeout(self) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.pid = 9999
        exc = subprocess.TimeoutExpired(cmd="omp", timeout=30)
        exc.proc = mock_proc

        mock_popen = mock.MagicMock()
        mock_popen.return_value = mock_proc
        mock_proc.communicate.side_effect = exc

        with mock.patch("subprocess.Popen", mock_popen):
            r = self._runner(binary="/usr/bin/omp")
            r.config.timeout = 30
            result = r.run(RunRequest(task="long task"))
            assert result.returncode == -1
            assert "timeout" in result.stderr.lower()

    def test_run_os_error(self) -> None:
        mock_popen = mock.MagicMock(side_effect=OSError("disk full"))
        with mock.patch("subprocess.Popen", mock_popen):
            r = self._runner(binary="/usr/bin/omp")
            result = r.run(RunRequest(task="hello"))
            assert result.returncode == 1
            assert "disk full" in result.stderr

    # -- prompt file lifecycle in run() -----------------------------------

    def test_run_cleans_prompt_on_success(self) -> None:
        """Prompt temp file should be deleted after a successful run."""
        stdout = '{"type": "agent_end"}\n'
        mock_popen = mock.MagicMock(return_value=_completed(0, stdout, ""))
        with mock.patch("subprocess.Popen", mock_popen):
            r = self._runner(binary="/usr/bin/omp")
            # Manually call _build_cmd to capture the prompt file path
            req = RunRequest(task="test")
            cmd = r._build_cmd(req)
            prompt_path = r._prompt_file
            assert os.path.exists(prompt_path)

            # Now run for real (subprocess is mocked)
            r2 = self._runner(binary="/usr/bin/omp")
            r2.run(req)
            # Prompt file from r2 should be cleaned up
            # (r2._prompt_file was set inside _build_cmd and cleaned in _parse_output)
            assert r2._prompt_file is None

        # Clean up the manually created one
        r._cleanup_prompt_file()

    def test_run_cleans_prompt_on_error(self) -> None:
        """Prompt temp file should be deleted even when the process fails."""
        mock_popen = mock.MagicMock(return_value=_completed(1, "", "oops"))
        with mock.patch("subprocess.Popen", mock_popen):
            r = self._runner(binary="/usr/bin/omp")
            result = r.run(RunRequest(task="test"))
            assert result.returncode == 1
            # Prompt file should be cleaned up (set to None in _parse_output)
            assert r._prompt_file is None


# ── BaseRunner contract ─────────────────────────────────────────────────


class TestBaseRunnerContract:
    """Verify that BaseRunner is properly abstract."""

    def test_cannot_instantiate(self) -> None:
        with pytest.raises(TypeError):
            BaseRunner()  # type: ignore[abstract]

    def test_concrete_subclass_works(self) -> None:
        class Dummy(BaseRunner):
            def _build_cmd(self, request):
                return ["echo", request.task]

            def _parse_output(self, proc, request):
                return RunResult(returncode=proc.returncode, stdout=proc.stdout)

        d = Dummy()
        result = d.run(RunRequest(task="hello"))
        assert result.returncode == 0
        assert result.stdout.strip() == "hello"

    def test_timeout_calls_killpg(self) -> None:
        """BaseRunner should call os.killpg on timeout."""
        mock_proc = mock.MagicMock()
        mock_proc.pid = 12345

        class Dummy(BaseRunner):
            def _build_cmd(self, request):
                return ["echo", request.task]
            def _parse_output(self, proc, request):
                return RunResult(returncode=0)

        timeout_exc = subprocess.TimeoutExpired(cmd="echo", timeout=5)
        timeout_exc.proc = mock_proc

        mock_popen = mock.MagicMock()
        mock_popen.return_value = mock_proc
        mock_proc.communicate.side_effect = timeout_exc

        with mock.patch("subprocess.Popen", mock_popen), \
             mock.patch("os.killpg") as mock_killpg, \
             mock.patch("os.getpgid", return_value=12345):
            d = Dummy(config=RunnerConfig(timeout=5))
            result = d.run(RunRequest(task="long"))
            assert result.returncode == -1
            mock_killpg.assert_called_once_with(12345, 9)
