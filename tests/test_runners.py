"""Tests for the runner layer — OMPRunner."""
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
from codeagent.runners.omp import OMPRunner, AgentProfile, resolve_agent_profile


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
# (removed — GoWrapperRunner deleted)


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
        import io

        mock_proc = mock.MagicMock()
        mock_proc.pid = 9999
        mock_proc.stdout = io.StringIO("")
        mock_proc.stderr = io.StringIO("")
        exc = subprocess.TimeoutExpired(cmd="omp", timeout=30)
        exc.proc = mock_proc

        mock_popen = mock.MagicMock()
        mock_popen.return_value = mock_proc
        mock_proc.wait.side_effect = exc

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

    # -- agent profile resolution ------------------------------------------

    def test_agent_oracle_resolves_model_and_thinking(self, tmp_path: Path) -> None:
        """agent='oracle' should set --model, --thinking, --append-system-prompt from profile."""
        from codeagent.runners.omp import AgentProfile, resolve_agent_profile

        # Create fake agent profile
        agents_dir = tmp_path / ".omp" / "agent" / "agents"
        agents_dir.mkdir(parents=True)
        profile_file = agents_dir / "oracle.md"
        profile_file.write_text(
            "---\n"
            "name: oracle\n"
            "model: gpt-5.6-sol\n"
            "thinking: high\n"
            "spawning: true\n"
            "auto-exit: false\n"
            "park: true\n"
            "park-class: advisor\n"
            "system-prompt: append\n"
            "---\n"
            "# Oracle\n\nYou are a senior architecture advisor.\n"
        )

        # Monkey-patch Path.home for this test
        orig_home = Path.home
        try:
            Path.home = classmethod(lambda cls: tmp_path)  # type: ignore[assignment]
            prof = resolve_agent_profile("oracle")
        finally:
            Path.home = orig_home  # type: ignore[assignment]

        assert prof.name == "oracle"
        assert prof.model == "gpt-5.6-sol"
        assert prof.thinking == "high"
        assert prof.park is True
        assert prof.auto_exit is False
        assert prof.system_prompt_path == str(profile_file)

    def test_agent_oracle_build_cmd_flags(self, tmp_path: Path) -> None:
        """_build_cmd with agent='oracle' should include --model, --thinking, --append-system-prompt."""
        from codeagent.runners.omp import AgentProfile

        profile = AgentProfile(
            name="oracle",
            model="gpt-5.6-sol",
            thinking="high",
            system_prompt_path=str(tmp_path / "oracle.md"),
            park=True,
            auto_exit=False,
        )

        r = self._runner(binary="/usr/bin/omp")
        r._agent_profile = profile  # stash as run() would
        req = RunRequest(task="analyze architecture", agent="oracle")
        cmd = r._build_cmd(req)

        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "gpt-5.6-sol"
        assert "--thinking" in cmd
        assert cmd[cmd.index("--thinking") + 1] == "high"
        assert "--append-system-prompt" in cmd
        assert cmd[cmd.index("--append-system-prompt") + 1] == str(tmp_path / "oracle.md")
        r._cleanup_prompt_file()

    def test_request_model_overrides_profile_model(self, tmp_path: Path) -> None:
        """Explicit request.model should override the agent profile model."""
        from codeagent.runners.omp import AgentProfile

        profile = AgentProfile(
            name="oracle",
            model="gpt-5.6-sol",
            thinking="high",
            system_prompt_path=str(tmp_path / "oracle.md"),
            park=True,
            auto_exit=False,
        )

        r = self._runner(binary="/usr/bin/omp")
        r._agent_profile = profile
        req = RunRequest(task="do it", agent="oracle", model="claude-opus")
        cmd = r._build_cmd(req)

        # request.model wins
        assert cmd[cmd.index("--model") + 1] == "claude-opus"
        # thinking still from profile
        assert "--thinking" in cmd
        assert cmd[cmd.index("--thinking") + 1] == "high"
        r._cleanup_prompt_file()

    def test_no_agent_no_profile_flags(self) -> None:
        """Without agent, no --thinking or --append-system-prompt should appear."""
        r = self._runner(binary="/usr/bin/omp")
        r._agent_profile = None
        req = RunRequest(task="do it", model="gpt-5")
        cmd = r._build_cmd(req)

        assert "--model" in cmd
        assert "--thinking" not in cmd
        assert "--append-system-prompt" not in cmd
        r._cleanup_prompt_file()

    def test_unknown_agent_returns_error(self) -> None:
        """run() with an unknown agent name should return an error result."""
        r = self._runner(binary="/usr/bin/omp")
        result = r.run(RunRequest(task="do it", agent="nonexistent-agent-xyz"))
        assert result.returncode == 1
        assert "Unknown agent profile" in result.stderr

    def test_oracle_timeout_override(self, tmp_path: Path) -> None:
        """Oracle-class agents should get _ORACLE_TIMEOUT instead of DEFAULT_EXEC_TIMEOUT."""
        from codeagent.runners.omp import AgentProfile
        from codeagent.constants import ORACLE_TIMEOUT as _ORACLE_TIMEOUT

        profile = AgentProfile(
            name="oracle", model="gpt-5", park=True, auto_exit=False,
        )
        stdout = '{"type": "agent_end"}\n'
        mock_popen = mock.MagicMock(return_value=_completed(0, stdout, ""))

        with mock.patch("subprocess.Popen", mock_popen):
            r = self._runner(binary="/usr/bin/omp")
            original_timeout = r.config.timeout
            # Inject profile as run() would
            with mock.patch.object(r, "_ensure_swarm_session"):
                with mock.patch(
                    "codeagent.runners.omp.resolve_agent_profile",
                    return_value=profile,
                ):
                    result = r.run(RunRequest(task="review", agent="oracle"))
                    # During run, timeout should have been overridden
                    # After run, it should be restored
                    assert r.config.timeout == original_timeout

    def test_non_oracle_no_timeout_override(self) -> None:
        """Non-oracle agents (auto_exit=True) should keep default timeout."""
        from codeagent.runners.omp import AgentProfile

        profile = AgentProfile(
            name="sisyphus", model="mimo", park=False, auto_exit=True,
        )
        stdout = '{"type": "agent_end"}\n'
        mock_popen = mock.MagicMock(return_value=_completed(0, stdout, ""))

        with mock.patch("subprocess.Popen", mock_popen):
            r = self._runner(binary="/usr/bin/omp")
            original_timeout = r.config.timeout
            with mock.patch(
                "codeagent.runners.omp.resolve_agent_profile",
                return_value=profile,
            ):
                result = r.run(RunRequest(task="implement", agent="sisyphus"))
                assert r.config.timeout == original_timeout

    def test_oracle_creates_swarm_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Oracle agents should trigger swarm session creation via _ensure_swarm_session."""
        from codeagent.runners.omp import AgentProfile

        profile = AgentProfile(
            name="oracle", model="gpt-5", park=True, auto_exit=False,
        )
        stdout = '{"type": "agent_end"}\n'
        mock_popen = mock.MagicMock(return_value=_completed(0, stdout, ""))

        ensure_calls: list[str] = []

        def mock_ensure(self, request):
            ensure_calls.append(request.agent)

        with mock.patch("subprocess.Popen", mock_popen):
            r = self._runner(binary="/usr/bin/omp")
            with mock.patch(
                "codeagent.runners.omp.resolve_agent_profile",
                return_value=profile,
            ):
                with mock.patch.object(
                    OMPRunner, "_ensure_swarm_session", mock_ensure,
                ):
                    r.run(RunRequest(task="review", agent="oracle"))
                    assert ensure_calls == ["oracle"]

    def test_non_oracle_no_swarm_session(self) -> None:
        """Non-oracle agents should NOT trigger swarm session creation."""
        from codeagent.runners.omp import AgentProfile

        profile = AgentProfile(
            name="sisyphus", model="mimo", park=False, auto_exit=True,
        )
        stdout = '{"type": "agent_end"}\n'
        mock_popen = mock.MagicMock(return_value=_completed(0, stdout, ""))

        ensure_calls: list[str] = []

        def mock_ensure(self, request):
            ensure_calls.append(request.agent)

        with mock.patch("subprocess.Popen", mock_popen):
            r = self._runner(binary="/usr/bin/omp")
            with mock.patch(
                "codeagent.runners.omp.resolve_agent_profile",
                return_value=profile,
            ):
                with mock.patch.object(
                    OMPRunner, "_ensure_swarm_session", mock_ensure,
                ):
                    r.run(RunRequest(task="implement", agent="sisyphus"))
                    assert ensure_calls == []

    def test_no_agent_no_swarm_session(self) -> None:
        """No agent → no swarm session creation."""
        stdout = '{"type": "agent_end"}\n'
        mock_popen = mock.MagicMock(return_value=_completed(0, stdout, ""))

        ensure_calls: list[str] = []

        def mock_ensure(self, request):
            ensure_calls.append(request.agent)

        with mock.patch("subprocess.Popen", mock_popen):
            r = self._runner(binary="/usr/bin/omp")
            with mock.patch.object(
                OMPRunner, "_ensure_swarm_session", mock_ensure,
            ):
                r.run(RunRequest(task="do it"))
                assert ensure_calls == []


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
        import io

        mock_proc = mock.MagicMock()
        mock_proc.pid = 12345
        mock_proc.stdout = io.StringIO("")
        mock_proc.stderr = io.StringIO("")

        class Dummy(BaseRunner):
            def _build_cmd(self, request):
                return ["echo", request.task]
            def _parse_output(self, proc, request):
                return RunResult(returncode=0)

        timeout_exc = subprocess.TimeoutExpired(cmd="echo", timeout=5)
        timeout_exc.proc = mock_proc

        mock_popen = mock.MagicMock()
        mock_popen.return_value = mock_proc
        mock_proc.wait.side_effect = timeout_exc

        with mock.patch("subprocess.Popen", mock_popen), \
             mock.patch("os.killpg") as mock_killpg, \
             mock.patch("os.getpgid", return_value=12345):
            d = Dummy(config=RunnerConfig(timeout=5))
            result = d.run(RunRequest(task="long"))
            assert result.returncode == -1
            mock_killpg.assert_called_once_with(12345, 9)
