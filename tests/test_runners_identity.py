"""Tests for OMPRunner identity injection (Oracle P1-3)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from codeagent.domain import RunRequest
from codeagent.runners.omp import OMPRunner


def _request(**kw) -> RunRequest:
    base = dict(
        task="hello",
        workdir="/tmp",
        backend="omp",
        agent="test-agent",
        model="",
        skills=[],
        session_key="k",
    )
    base.update(kw)
    return RunRequest(**base)


class TestIdentityInjection:
    def test_no_swarm_session_no_injection(self):
        runner = OMPRunner()
        with patch.dict(os.environ, {}, clear=True):
            env = runner._extra_env()
        assert env is None

    def test_injects_namespaced_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SWARM_SESSION_ID", "s1")
        monkeypatch.setenv("OMP_WORKER_ID", "worker-a")
        monkeypatch.setenv("MAILBOX_ROOT", str(tmp_path / "mb"))

        runner = OMPRunner()
        env = runner._extra_env()

        assert env is not None
        assert env["SWARM_SESSION_ID"] == "s1"
        assert env["OMP_MAILBOX_SESSION_ID"] == "s1"
        assert env["OMP_MAILBOX_AGENT_ID"] == "worker-a"
        assert env["MAILBOX_ROOT"] == str(tmp_path / "mb")
        assert env["OMP_MAILBOX_IDENTITY_FILE"]

    def test_identity_file_written_before_spawn(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SWARM_SESSION_ID", "s1")
        runner = OMPRunner()
        env = runner._extra_env()
        assert env is not None

        identity_path = Path(env["OMP_MAILBOX_IDENTITY_FILE"])
        assert identity_path.exists()
        data = json.loads(identity_path.read_text())
        assert data["session_id"] == "s1"

        # cleanup removes it
        runner._cleanup()
        assert not identity_path.exists()

    def test_subprocess_receives_env(self, tmp_path, monkeypatch):
        """BaseRunner.run passes extra_env to the subprocess."""
        monkeypatch.setenv("SWARM_SESSION_ID", "s1")
        runner = OMPRunner()
        request = _request()

        env_seen = {}

        class _FakeProc:
            def __init__(self, cmd, **kwargs):
                env_seen["env"] = kwargs.get("env")
                self.returncode = 0
                self.stdout = ""
                self.stderr = ""
                self.cmd = cmd

            def communicate(self, input=None, timeout=None):
                return self.stdout, self.stderr

        with patch("subprocess.Popen", side_effect=_FakeProc):
            runner._build_cmd = lambda r: ["omp", "--print", "--mode", "json", "@x"]
            runner._parse_output = lambda p, r: __import__(
                "codeagent.domain", fromlist=["RunResult"]
            ).RunResult(returncode=0, stdout="", stderr="")
            runner.run(request)

        assert env_seen.get("env") is not None
        assert env_seen["env"]["OMP_MAILBOX_SESSION_ID"] == "s1"
