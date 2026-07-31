"""Tests for launchers (tmux pane management) and adapters (OMP identity)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


class TestLaunchers:
    """Tests for codeagent.launchers tmux helpers."""

    def test_send_keys(self):
        from codeagent.launchers import send_keys
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = send_keys("session:0.0", "echo hello")
            assert result is True
            cmd = mock_run.call_args[0][0]
            assert "send-keys" in cmd
            assert "Enter" in cmd

    def test_send_keys_no_enter(self):
        from codeagent.launchers import send_keys
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = send_keys("session:0.0", "text", enter=False)
            assert result is True
            cmd = mock_run.call_args[0][0]
            assert "Enter" not in cmd

    def test_capture_pane(self):
        from codeagent.launchers import capture_pane
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="line1\nline2\n")
            result = capture_pane("session:0.0", lines=10)
            assert "line1" in result

    def test_kill_pane(self):
        from codeagent.launchers import kill_pane
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = kill_pane("session:0.0")
            assert result is True

    def test_create_pane(self):
        from codeagent.launchers import create_pane, PaneConfig
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="%5\n")
            result = create_pane(PaneConfig(session="test"))
            assert result == "%5"

    def test_create_pane_failure(self):
        from codeagent.launchers import create_pane, PaneConfig
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            result = create_pane(PaneConfig())
            assert result is None


class TestAdapters:
    """Tests for codeagent.adapters OMP identity registration."""

    def test_register_identity_with_env(self, tmp_path):
        from codeagent.adapters import register_identity
        identity_file = str(tmp_path / "identity.json")
        env = {**os.environ, "OMP_MAILBOX_IDENTITY_FILE": identity_file}
        with patch.dict(os.environ, env, clear=False):
            result = register_identity("sess-1", "worker-1")
            assert result.exists()
            data = json.loads(result.read_text())
            assert data["session_id"] == "sess-1"
            assert data["worker_id"] == "worker-1"

    def test_register_identity_explicit_path(self, tmp_path):
        from codeagent.adapters import register_identity
        identity_file = str(tmp_path / "custom" / "id.json")
        result = register_identity("sess-2", "worker-2", identity_file=identity_file)
        assert result.exists()
        assert str(result) == identity_file

    def test_unregister_identity(self, tmp_path):
        from codeagent.adapters import register_identity, unregister_identity
        identity_file = str(tmp_path / "id.json")
        register_identity("s", "w", identity_file=identity_file)
        assert Path(identity_file).exists()
        unregister_identity(identity_file)
        assert not Path(identity_file).exists()

    def test_unregister_identity_missing(self, tmp_path):
        from codeagent.adapters import unregister_identity
        # Should not raise
        unregister_identity(str(tmp_path / "nonexistent.json"))
