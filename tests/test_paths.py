"""Tests for codeagent.util.paths."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from codeagent.util.paths import (
    config_dir,
    expand_path,
    normalize_workdir,
    runtime_dir,
    state_dir,
)


class TestExpandPath:
    def test_tilde(self):
        result = expand_path("~/projects")
        assert result.startswith(str(Path.home()))
        assert result.endswith("/projects")

    def test_env_var(self, monkeypatch):
        monkeypatch.setenv("MY_TEST_DIR", "/opt/test")
        assert expand_path("$MY_TEST_DIR/sub") == "/opt/test/sub"

    def test_plain_path(self):
        assert expand_path("/absolute/path") == "/absolute/path"

    def test_empty_string(self):
        # Empty string passes through — normalize_workdir handles cwd fallback
        assert expand_path("") == ""


class TestNormalizeWorkdir:
    def test_absolute_path_unchanged(self):
        assert normalize_workdir("/tmp/work") == "/tmp/work"

    def test_relative_resolved(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "sub").mkdir()
        result = normalize_workdir("sub")
        assert result == str(tmp_path / "sub")

    def test_empty_uses_cwd(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        assert normalize_workdir("") == str(tmp_path)

    def test_dot_uses_cwd(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        assert normalize_workdir(".") == str(tmp_path)

    def test_tilde_expanded(self):
        result = normalize_workdir("~/src")
        assert result.startswith(str(Path.home()))
        assert ".." not in result  # normalized

    def test_env_expanded(self, monkeypatch):
        monkeypatch.setenv("CODEAGENT_TEST_PATH", "/opt/codeagent")
        assert normalize_workdir("$CODEAGENT_TEST_PATH") == "/opt/codeagent"


class TestConfigDir:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        result = config_dir()
        assert result == Path.home() / ".config" / "codeagent"

    def test_xdg_override(self, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", "/custom/config")
        assert config_dir() == Path("/custom/config/codeagent")


class TestStateDir:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        result = state_dir()
        assert result == Path.home() / ".local" / "state" / "codeagent"

    def test_xdg_override(self, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", "/custom/state")
        assert state_dir() == Path("/custom/state/codeagent")


class TestRuntimeDir:
    def test_xdg_preferred(self, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
        assert runtime_dir() == Path("/run/user/1000/codeagent")

    def test_fallback_tmpdir(self, monkeypatch):
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        monkeypatch.setenv("TMPDIR", "/tmp/custom")
        monkeypatch.setattr(os, "getuid", lambda: 1000)
        assert runtime_dir() == Path("/tmp/custom/codeagent-1000")

    def test_fallback_no_tmpdir(self, monkeypatch):
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        monkeypatch.delenv("TMPDIR", raising=False)
        monkeypatch.setattr(os, "getuid", lambda: 501)
        assert runtime_dir() == Path("/tmp/codeagent-501")
