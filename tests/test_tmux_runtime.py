"""Tmux runtime supervisor tests — markers, generation, spec security,
no capture-pane state dependence, capability contract for adapters."""
from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

import pytest

from codeagent.launchers import tmux as tmux_mod
from codeagent.runtime.base import (
    CAP_HOT_RESUME,
    CAP_IN_LOOP_MESSAGES,
    CAP_NATIVE_UI,
    CAP_STREAM_EVENTS,
    CAP_TOOL_STATS,
    CAP_WARM_RESUME,
)
from codeagent.runtime.registry import RuntimeRegistry, RuntimeErrorCode
from codeagent.runtime.supervisor import (
    MARKER_AGENT_STARTED,
    MARKER_CWD_VERIFIED,
    MARKER_SHELL_READY,
    RuntimeSpec,
    main as supervisor_main,
    write_spec,
)


# ── RuntimeSpec / spec security ────────────────────────────────────────


class TestRuntimeSpec:
    def test_spec_roundtrip(self, tmp_path: Path):
        spec = RuntimeSpec(
            runtime_id="r1", session_id="s1", agent_id="a1", runtime="omp",
            review_key="k", generation=2, backend_session_id="b1",
            workdir="/tmp", task="do it", model="m", gateway_socket="/tmp/sock",
            owner_pid=123, nonce="n", mode="interactive_plugin",
        )
        d = spec.to_dict()
        back = RuntimeSpec.from_dict(d)
        assert back == spec

    def test_write_spec_0600(self, tmp_path: Path):
        spec = RuntimeSpec(runtime_id="r1", session_id="s1", agent_id="a1")
        path = write_spec(spec, dir=tmp_path)
        assert path.exists()
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode & 0o777 == 0o600
        # spec dir is 0700
        dirmode = stat.S_IMODE(tmp_path.stat().st_mode)
        assert dirmode & 0o777 == 0o700


# ── supervisor main (no tmux dependency — process-level) ───────────────


class TestSupervisorMain:
    def test_missing_spec_returns_2(self, tmp_path: Path, capsys):
        code = supervisor_main([str(tmp_path / "nope.json")])
        assert code == 2

    def test_bad_spec_returns_2(self, tmp_path: Path, capsys):
        p = tmp_path / "bad.json"
        p.write_text("not json")
        code = supervisor_main([str(p)])
        assert code == 2

    def test_missing_workdir_fails_fast(self, tmp_path: Path, capsys):
        spec = RuntimeSpec(
            runtime_id="r1", session_id="s1", agent_id="a1",
            workdir=str(tmp_path / "missing-dir"),
        )
        path = write_spec(spec, dir=tmp_path / "runtime")
        code = supervisor_main([str(path)])
        assert code == 1
        marker = tmp_path / "runtime" / "r1.CWD_VERIFIED"
        assert marker.exists()
        assert "missing" in marker.read_text()

    def test_marker_sequence_and_pid(self, tmp_path: Path):
        """A real child process gets SHELL_READY/CWD_VERIFIED/AGENT_STARTED
        markers + pid file; exit marker on completion."""
        script = tmp_path / "agent.sh"
        script.write_text("#!/bin/sh\necho running\nsleep 0.2\nexit 0\n")
        script.chmod(0o755)
        spec = RuntimeSpec(
            runtime_id="r1", session_id="s1", agent_id="a1",
            runtime="generic",
            workdir=str(tmp_path),
            profile_args=[str(script)],
        )
        d = tmp_path / "runtime"
        path = write_spec(spec, dir=d)
        code = supervisor_main([str(path)])
        assert code == 0
        for marker in (MARKER_SHELL_READY, MARKER_CWD_VERIFIED, MARKER_AGENT_STARTED, "AGENT_EXITED"):
            p = d / f"r1.{marker}"
            assert p.exists(), f"missing marker {marker}"
        pid_file = d / "r1.pid"
        assert not pid_file.exists()  # cleaned up after exit

    def test_generation_field_preserved(self, tmp_path: Path):
        spec = RuntimeSpec(
            runtime_id="r1", session_id="s1", agent_id="a1",
            generation=7, workdir=str(tmp_path),
        )
        d = tmp_path / "runtime"
        path = write_spec(spec, dir=d)
        reread = RuntimeSpec.from_dict(json.loads(path.read_text()))
        assert reread.generation == 7

    def test_unsupported_runtime_reports_error(self, tmp_path: Path):
        spec = RuntimeSpec(
            runtime_id="r1", session_id="s1", agent_id="a1",
            runtime="nonexistent", workdir=str(tmp_path),
        )
        d = tmp_path / "runtime"
        path = write_spec(spec, dir=d)
        code = supervisor_main([str(path)])
        assert code == 1
        marker = d / "r1.AGENT_EXITED"
        assert marker.exists()


# ── quota / rate-limit detection ───────────────────────────────────────


def test_scan_quota_error_detects_provider_markers(tmp_path):
    """insufficient_quota / quota exceeded / rate limit in the log tail are
    detected — never disguised as a generic transport timeout."""
    from codeagent.runtime.supervisor import _scan_quota_error

    log = tmp_path / "runtime.log"
    log.write_text(
        "2026-08-12T10:00:01 [info] omp started\n"
        "2026-08-12T10:00:02 [error] provider error: insufficient_quota for "
        "model Mify-ppio/ppio/pa/gpt-5.6-sol\n",
        encoding="utf-8",
    )
    hit = _scan_quota_error(log, "some-model")
    assert "insufficient_quota" in hit

    log.write_text("rate limit exceeded (429) on request", encoding="utf-8")
    assert "rate limit" in _scan_quota_error(log, "m")

    log.write_text("all good, nothing to see", encoding="utf-8")
    assert _scan_quota_error(log, "m") == ""


def test_scan_quota_error_missing_log(tmp_path):
    from codeagent.runtime.supervisor import _scan_quota_error

    assert _scan_quota_error(tmp_path / "nope.log", "m") == ""


# ── capture-pane is never used for state ───────────────────────────────


class TestNoCapturePaneDependence:
    def test_supervisor_argv_built_without_shell(self):
        """The supervisor's agent argv is a list — never a shell string."""
        spec = RuntimeSpec(
            runtime_id="r", session_id="s", agent_id="a",
            runtime="omp", mode="interactive_plugin", workdir="/w",
            model="m", profile_args=["--thinking", "low"],
        )
        from codeagent.runtime.supervisor import _build_agent_argv

        argv = _build_agent_argv(spec)
        assert isinstance(argv, list)
        assert argv[0] == "omp"
        assert "--cwd" in argv
        # NO -c / --print for interactive_plugin
        assert "-c" not in argv
        assert "--print" not in argv

    def test_short_task_uses_print(self):
        spec = RuntimeSpec(
            runtime_id="r", session_id="s", agent_id="a",
            runtime="omp", mode="short_task", workdir="/w", task="t",
        )
        from codeagent.runtime.supervisor import _build_agent_argv

        argv = _build_agent_argv(spec)
        assert "--print" in argv
        assert "--mode" in argv

    def test_warm_uses_resume(self):
        spec = RuntimeSpec(
            runtime_id="r", session_id="s", agent_id="a",
            runtime="omp", mode="interactive_plugin", workdir="/w",
            backend_session_id="b123",
        )
        from codeagent.runtime.supervisor import _build_agent_argv

        argv = _build_agent_argv(spec)
        assert "--resume" in argv
        assert "b123" in argv


# ── RuntimeRegistry capability contract ────────────────────────────────


class TestRuntimeRegistryContract:
    def test_names(self):
        reg = RuntimeRegistry()
        names = reg.names()
        assert "omp" in names
        assert "opencode" in names
        assert "generic" in names

    def test_explicit_unknown_runtime(self):
        reg = RuntimeRegistry()
        with pytest.raises(RuntimeErrorCode) as ei:
            reg.get("bogus")
        assert ei.value.code == "UNSUPPORTED_RUNTIME"

    def test_omp_full_capabilities(self):
        from codeagent.runtime.omp import OMPRuntimeAdapter

        adapter = OMPRuntimeAdapter()
        assert CAP_HOT_RESUME in adapter.capabilities
        assert CAP_IN_LOOP_MESSAGES in adapter.capabilities
        assert CAP_NATIVE_UI in adapter.capabilities
        assert CAP_TOOL_STATS in adapter.capabilities
        assert CAP_STREAM_EVENTS in adapter.capabilities
        assert CAP_WARM_RESUME in adapter.capabilities

    def test_opencode_capabilities_honest(self):
        from codeagent.runtime.opencode import OpenCodeRuntimeAdapter

        adapter = OpenCodeRuntimeAdapter()
        assert CAP_STREAM_EVENTS in adapter.capabilities
        assert CAP_WARM_RESUME in adapter.capabilities
        # explicitly unsupported — never faked
        assert CAP_IN_LOOP_MESSAGES not in adapter.capabilities
        assert CAP_TOOL_STATS not in adapter.capabilities
        assert CAP_NATIVE_UI not in adapter.capabilities
        assert CAP_HOT_RESUME not in adapter.capabilities

    def test_generic_cold_only(self):
        from codeagent.runtime.generic import GenericRuntimeAdapter

        adapter = GenericRuntimeAdapter()
        assert adapter.capabilities == frozenset({CAP_STREAM_EVENTS})

    def test_required_capability_selection(self):
        """hot_resume requires OMP; OpenCode/generic cannot satisfy it."""
        reg = RuntimeRegistry()
        # hot_resume is OMP-only
        adapter = reg.get(required_capabilities=frozenset({CAP_HOT_RESUME}))
        assert adapter.name == "omp"
        # warm_resume → OMP preferred (full caps), OpenCode as fallback
        adapter2 = reg.get(required_capabilities=frozenset({CAP_WARM_RESUME}))
        assert adapter2.name in ("omp", "opencode")

    def test_impossible_capability_rejected(self):
        reg = RuntimeRegistry()
        with pytest.raises(RuntimeErrorCode) as ei:
            reg.get(required_capabilities=frozenset({"no_such_cap"}))
        assert ei.value.code == "UNSUPPORTED_CAPABILITY"

    def test_generic_requires_argv(self):
        reg = RuntimeRegistry()
        with pytest.raises(ValueError, match="argv"):
            reg.spawn("generic", {"task": "t"})


# ── launchers/tmux.py uncovered branches ───────────────────────────────


class TestTmuxLauncherUncovered:
    """Coverage for launchers/tmux.py branches missing from the suite:
    socket-dir chmod fallback, detect/spawn-in-current-tmux, _tmux error
    paths, ensure_tmux_server failures, spawn_runtime error paths,
    probe_runtime health, stop_runtime termination ladder."""

    @staticmethod
    def _run_ok(returncode=0, stdout="", stderr=""):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    @staticmethod
    def _write_spec(tmp_path, runtime_id="r1"):
        spec = tmp_path / "spec.json"
        spec.write_text(json.dumps({
            "runtime_id": runtime_id,
            "runtime": "omp",
            "review_key": "rk:12",
            "generation": 3,
            "host_alias": "host-1",
        }))
        return spec

    @staticmethod
    def _handle(tmp_path, runtime_id="r1"):
        return tmux_mod.TmuxRuntimeHandle(
            runtime_id=runtime_id,
            socket_path=Path("/tmp/sock/codeagent.sock"),
            session="aimeshchat-gateway",
            window="w1",
            pane_id="%1",
            host_alias="__local__",
            runtime="omp",
            pid=None,
            started_at="2026-08-18T00:00:00Z",
            generation=1,
            diagnostic_log=tmp_path / f"{runtime_id}.log",
        )

    # -- tmux_socket_dir -------------------------------------------------

    def test_socket_dir_chmod_oserror_ignored(self, tmp_path, monkeypatch):
        """chmod failure on the private socket dir is swallowed."""
        sock_dir = tmp_path / "sockets"
        monkeypatch.setenv(tmux_mod.TMUX_SOCKET_DIR_ENV, str(sock_dir))
        with patch("os.chmod", side_effect=OSError("read-only fs")):
            base = tmux_mod.tmux_socket_dir()
        assert base == sock_dir
        assert sock_dir.is_dir()

    # -- TmuxRuntimeHandle.to_dict ---------------------------------------

    def test_handle_to_dict(self, tmp_path):
        handle = self._handle(tmp_path)
        handle.pid = 4242
        handle.started_at = "2026-08-18T00:00:00Z"
        handle.generation = 2
        d = handle.to_dict()
        assert d == {
            "runtime_id": "r1",
            "socket_path": "/tmp/sock/codeagent.sock",
            "session": "aimeshchat-gateway",
            "window": "w1",
            "pane_id": "%1",
            "host_alias": "__local__",
            "runtime": "omp",
            "pid": 4242,
            "started_at": "2026-08-18T00:00:00Z",
            "generation": 2,
            "diagnostic_log": str(tmp_path / "r1.log"),
        }

    # -- detect_current_tmux ---------------------------------------------

    def test_detect_current_tmux_no_env(self, monkeypatch):
        monkeypatch.delenv("TMUX", raising=False)
        assert tmux_mod.detect_current_tmux() is None

    def test_detect_current_tmux_no_binary(self, monkeypatch):
        monkeypatch.setenv("TMUX", "/tmp/sock,1234,0")
        with patch.object(tmux_mod, "_tmux_ok", return_value=False):
            assert tmux_mod.detect_current_tmux() is None

    def test_detect_current_tmux_display_fails(self, monkeypatch):
        monkeypatch.setenv("TMUX", "/tmp/sock,1234,0")
        with patch.object(tmux_mod, "_tmux_ok", return_value=True), \
             patch("subprocess.run", return_value=self._run_ok(returncode=1, stderr="err")):
            assert tmux_mod.detect_current_tmux() is None

    def test_detect_current_tmux_empty_session_name(self, monkeypatch):
        monkeypatch.setenv("TMUX", "/tmp/sock,1234,0")
        with patch.object(tmux_mod, "_tmux_ok", return_value=True), \
             patch("subprocess.run", return_value=self._run_ok(stdout="\n")):
            assert tmux_mod.detect_current_tmux() is None

    def test_detect_current_tmux_success(self, monkeypatch):
        monkeypatch.setenv("TMUX", "/tmp/sock,1234,0")
        with patch.object(tmux_mod, "_tmux_ok", return_value=True), \
             patch("subprocess.run", return_value=self._run_ok(stdout="mysession\n")):
            assert tmux_mod.detect_current_tmux() == ("mysession", "/tmp/sock")

    # -- spawn_in_current_tmux -------------------------------------------

    def test_spawn_in_current_tmux_not_in_tmux(self):
        with patch.object(tmux_mod, "detect_current_tmux", return_value=None):
            with pytest.raises(RuntimeError, match="not inside a tmux session"):
                tmux_mod.spawn_in_current_tmux(["echo", "hi"])

    def test_spawn_in_current_tmux_new_window_ok(self):
        effects = [
            self._run_ok(stdout="%42\n"),  # new-window
            self._run_ok(),                # set-option @codeagent_managed
            self._run_ok(),                # set-option @codeagent_history_safe
            self._run_ok(),                # send-keys setopt HIST_IGNORE_SPACE
            self._run_ok(),                # send-keys command
        ]
        with patch.object(tmux_mod, "detect_current_tmux", return_value=("sess1", "/tmp/sock")), \
             patch("subprocess.run", side_effect=effects) as mock_run:
            pane_id = tmux_mod.spawn_in_current_tmux(
                ["echo", "hi"], label="mylabel", cwd="/work",
            )
        assert pane_id == "%42"
        argv = mock_run.call_args_list[0][0][0]
        assert argv[1] == "new-window"
        assert "sess1:" in argv
        assert "mylabel" in argv
        assert argv[argv.index("-c") + 1] == "/work"
        # pane marked as managed via set-option
        assert "set-option" in mock_run.call_args_list[1][0][0]

    def test_spawn_in_current_tmux_split_default_cwd(self):
        effects = [self._run_ok(stdout="%43\n")] + [self._run_ok()] * 4
        with patch.object(tmux_mod, "detect_current_tmux", return_value=("sess1", "/tmp/sock")), \
             patch("subprocess.run", side_effect=effects) as mock_run:
            pane_id = tmux_mod.spawn_in_current_tmux(["cmd"], split=True)
        assert pane_id == "%43"
        argv = mock_run.call_args_list[0][0][0]
        assert argv[1] == "split-window"
        assert "-T" in argv
        assert argv[argv.index("-c") + 1] == os.getcwd()

    def test_spawn_in_current_tmux_window_fails(self):
        with patch.object(tmux_mod, "detect_current_tmux", return_value=("sess1", "/tmp/sock")), \
             patch("subprocess.run", return_value=self._run_ok(returncode=1, stderr="boom")):
            with pytest.raises(RuntimeError, match="new-window failed: boom"):
                tmux_mod.spawn_in_current_tmux(["echo"])

    def test_spawn_in_current_tmux_no_pane_id(self):
        with patch.object(tmux_mod, "detect_current_tmux", return_value=("sess1", "/tmp/sock")), \
             patch("subprocess.run", return_value=self._run_ok(stdout="")):
            with pytest.raises(RuntimeError, match="tmux returned no pane id"):
                tmux_mod.spawn_in_current_tmux(["echo"])

    def test_spawn_in_current_tmux_send_keys_fails(self):
        effects = [
            self._run_ok(stdout="%42\n"),  # new-window
            self._run_ok(),                # set-option x2
            self._run_ok(),
            self._run_ok(),                # setopt HIST_IGNORE_SPACE
            self._run_ok(returncode=1),    # send-keys command fails
            self._run_ok(),                # kill-pane cleanup
        ]
        with patch.object(tmux_mod, "detect_current_tmux", return_value=("sess1", "/tmp/sock")), \
             patch("subprocess.run", side_effect=effects) as mock_run:
            with pytest.raises(RuntimeError, match="tmux send-keys failed"):
                tmux_mod.spawn_in_current_tmux(["echo", "hi"])
        # cleanup kill-pane ran after the failed send
        assert "kill-pane" in mock_run.call_args_list[-1][0][0]

    # -- _tmux_ok ---------------------------------------------------------

    def test_tmux_ok_true(self):
        with patch("shutil.which", return_value="/usr/bin/tmux"):
            assert tmux_mod._tmux_ok() is True

    def test_tmux_ok_false(self):
        with patch("shutil.which", return_value=None):
            assert tmux_mod._tmux_ok() is False

    # -- _tmux exception paths -------------------------------------------

    def test_tmux_helper_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("tmux", 10)):
            rc, out, err = tmux_mod._tmux("has-session")
        assert rc == 1 and out == ""
        assert "timed out" in err

    def test_tmux_helper_oserror(self):
        with patch("subprocess.run", side_effect=OSError("no such file")):
            rc, out, err = tmux_mod._tmux("has-session")
        assert rc == 1 and out == ""
        assert "no such file" in err

    # -- ensure_tmux_server ----------------------------------------------

    def test_ensure_tmux_server_no_binary(self):
        with patch.object(tmux_mod, "_tmux_ok", return_value=False):
            assert tmux_mod.ensure_tmux_server() is False

    def test_ensure_tmux_server_session_exists(self):
        with patch.object(tmux_mod, "_tmux_ok", return_value=True), \
             patch.object(tmux_mod, "_tmux", return_value=(0, "", "")):
            assert tmux_mod.ensure_tmux_server() is True

    def test_ensure_tmux_server_new_session_fails(self):
        with patch.object(tmux_mod, "_tmux_ok", return_value=True), \
             patch.object(tmux_mod, "_tmux",
                          side_effect=[(1, "", "no session"), (1, "", "no server")]):
            assert tmux_mod.ensure_tmux_server() is False

    def test_ensure_tmux_server_new_session_ok(self):
        with patch.object(tmux_mod, "_tmux_ok", return_value=True), \
             patch.object(tmux_mod, "_tmux",
                          side_effect=[(1, "", "no session"), (0, "", "")]):
            assert tmux_mod.ensure_tmux_server() is True

    # -- spawn_runtime ---------------------------------------------------

    def test_spawn_runtime_no_tmux_binary(self, tmp_path):
        spec = self._write_spec(tmp_path)
        with patch.object(tmux_mod, "_tmux_ok", return_value=False):
            with pytest.raises(RuntimeError, match="tmux binary not found"):
                tmux_mod.spawn_runtime(spec)

    def test_spawn_runtime_server_unavailable(self, tmp_path):
        spec = self._write_spec(tmp_path)
        with patch.object(tmux_mod, "_tmux_ok", return_value=True), \
             patch.object(tmux_mod, "ensure_tmux_server", return_value=False):
            with pytest.raises(RuntimeError, match="cannot start private tmux server"):
                tmux_mod.spawn_runtime(spec)

    def test_spawn_runtime_new_window_fails(self, tmp_path):
        spec = self._write_spec(tmp_path)
        with patch.object(tmux_mod, "_tmux_ok", return_value=True), \
             patch.object(tmux_mod, "ensure_tmux_server", return_value=True), \
             patch.object(tmux_mod, "_tmux", return_value=(1, "", "pane err")):
            with pytest.raises(RuntimeError, match="tmux new-window failed: pane err"):
                tmux_mod.spawn_runtime(spec)

    def test_spawn_runtime_no_pane_id(self, tmp_path):
        spec = self._write_spec(tmp_path)
        with patch.object(tmux_mod, "_tmux_ok", return_value=True), \
             patch.object(tmux_mod, "ensure_tmux_server", return_value=True), \
             patch.object(tmux_mod, "_tmux", return_value=(0, "\n", "")):
            with pytest.raises(RuntimeError, match="tmux new-window returned no pane id"):
                tmux_mod.spawn_runtime(spec)

    def test_spawn_runtime_send_keys_fails_cleans_pane(self, tmp_path):
        spec = self._write_spec(tmp_path)
        effects = [
            (0, "%77\n", ""),     # new-window
            (0, "", ""),          # set-option @codeagent_managed
            (0, "", ""),          # set-option @codeagent_history_safe
            (0, "", ""),          # send-keys setopt HIST_IGNORE_SPACE
            (1, "", "send err"),  # send-keys supervisor fails
            (0, "", ""),          # kill-pane cleanup
        ]
        with patch.object(tmux_mod, "_tmux_ok", return_value=True), \
             patch.object(tmux_mod, "ensure_tmux_server", return_value=True), \
             patch.object(tmux_mod, "_tmux", side_effect=effects) as mock_tmux:
            with pytest.raises(RuntimeError, match="tmux send-keys failed: send err"):
                tmux_mod.spawn_runtime(spec)
        assert mock_tmux.call_args_list[-1][0] == ("kill-pane", "-t", "%77")

    def test_spawn_runtime_success(self, tmp_path):
        spec = self._write_spec(tmp_path)
        effects = [(0, "%77\n", ""), (0, "", ""), (0, "", ""), (0, "", ""), (0, "", "")]
        with patch.object(tmux_mod, "_tmux_ok", return_value=True), \
             patch.object(tmux_mod, "ensure_tmux_server", return_value=True), \
             patch.object(tmux_mod, "_tmux", side_effect=effects):
            handle = tmux_mod.spawn_runtime(spec)
        assert handle.runtime_id == "r1"
        assert handle.runtime == "omp"
        assert handle.pane_id == "%77"
        assert handle.window.startswith("ora-rk-12-")
        assert handle.generation == 3
        assert handle.host_alias == "host-1"
        assert handle.diagnostic_log == tmp_path / "r1.log"

    # -- probe_runtime ---------------------------------------------------

    def test_probe_runtime_alive_with_pid_and_markers(self, tmp_path):
        handle = self._handle(tmp_path)
        (tmp_path / "r1.pid").write_text("4242")
        (tmp_path / "r1.SHELL_READY").write_text("ready")
        (tmp_path / "r1.AGENT_EXITED").write_text("0")
        (tmp_path / "r1.QUOTA_ERROR").write_text("quota exceeded")
        with patch.object(tmux_mod, "_tmux", return_value=(0, "", "")), \
             patch("os.kill") as mock_kill:
            health = tmux_mod.probe_runtime(handle)
        assert health["pane_alive"] is True
        assert health["pid_alive"] is True
        assert health["pid"] == 4242
        assert health["alive"] is True
        assert health["markers"] == {"SHELL_READY": "ready", "AGENT_EXITED": "0"}
        assert health["quota_error"] == "quota exceeded"
        mock_kill.assert_called_once_with(4242, 0)

    def test_probe_runtime_pid_gone(self, tmp_path):
        handle = self._handle(tmp_path)
        (tmp_path / "r1.pid").write_text("4242")
        with patch.object(tmux_mod, "_tmux", return_value=(0, "", "")), \
             patch("os.kill", side_effect=ProcessLookupError):
            health = tmux_mod.probe_runtime(handle)
        assert health["pid_alive"] is False
        assert health["alive"] is False

    def test_probe_runtime_bad_pid_file(self, tmp_path):
        handle = self._handle(tmp_path)
        (tmp_path / "r1.pid").write_text("not-a-pid")
        with patch.object(tmux_mod, "_tmux", return_value=(0, "", "")), \
             patch("os.kill") as mock_kill:
            health = tmux_mod.probe_runtime(handle)
        assert health["pid_alive"] is False
        assert health["alive"] is False
        mock_kill.assert_not_called()

    def test_probe_runtime_no_pid_file(self, tmp_path):
        handle = self._handle(tmp_path)
        with patch.object(tmux_mod, "_tmux", return_value=(0, "", "")):
            health = tmux_mod.probe_runtime(handle)
        assert health == {"alive": False, "pane_alive": True, "pid_alive": False, "markers": {}}

    def test_probe_runtime_pane_dead(self, tmp_path):
        handle = self._handle(tmp_path)
        (tmp_path / "r1.pid").write_text("4242")
        with patch.object(tmux_mod, "_tmux", return_value=(1, "", "")), \
             patch("os.kill"):
            health = tmux_mod.probe_runtime(handle)
        assert health["pane_alive"] is False
        assert health["alive"] is False

    # -- stop_runtime ----------------------------------------------------

    def test_stop_runtime_no_pid_file(self, tmp_path):
        handle = self._handle(tmp_path)
        with patch.object(tmux_mod, "_tmux", return_value=(0, "", "")), \
             patch("os.kill") as mock_kill:
            assert tmux_mod.stop_runtime(handle) is True
        mock_kill.assert_not_called()

    def test_stop_runtime_graceful_exit(self, tmp_path):
        """SIGTERM, then process gone → break grace loop, no SIGKILL."""
        handle = self._handle(tmp_path)
        (tmp_path / "r1.pid").write_text("4242")

        def fake_kill(pid, sig):
            if sig == 0:
                raise ProcessLookupError()

        with patch.object(tmux_mod, "_tmux", return_value=(0, "", "")), \
             patch("os.kill", side_effect=fake_kill) as mock_kill:
            assert tmux_mod.stop_runtime(handle) is True
        sigs = [c.args[1] for c in mock_kill.call_args_list]
        assert sigs == [15, 0, 0]  # SIGTERM + probes; no SIGKILL
        assert 9 not in sigs

    def test_stop_runtime_sigkill_after_grace(self, tmp_path):
        """Process survives grace window → SIGKILL."""
        handle = self._handle(tmp_path)
        (tmp_path / "r1.pid").write_text("4242")
        with patch.object(tmux_mod, "_tmux", return_value=(0, "", "")), \
             patch("os.kill") as mock_kill, \
             patch.object(tmux_mod.time, "monotonic", side_effect=[0.0, 0.0, 10.1]), \
             patch.object(tmux_mod.time, "sleep") as mock_sleep:
            assert tmux_mod.stop_runtime(handle, grace_seconds=10) is True
        sigs = [c.args[1] for c in mock_kill.call_args_list]
        assert sigs[-1] == 9  # SIGKILL after grace expired
        assert mock_sleep.called

    def test_stop_runtime_bad_pid_file(self, tmp_path):
        handle = self._handle(tmp_path)
        (tmp_path / "r1.pid").write_text("garbage")
        with patch.object(tmux_mod, "_tmux", return_value=(0, "", "")), \
             patch("os.kill") as mock_kill:
            assert tmux_mod.stop_runtime(handle) is True
        mock_kill.assert_not_called()

    def test_stop_runtime_sigterm_process_gone(self, tmp_path):
        """SIGTERM raises ProcessLookupError → skip grace ladder, still True."""
        handle = self._handle(tmp_path)
        (tmp_path / "r1.pid").write_text("4242")

        def fake_kill(pid, sig):
            raise ProcessLookupError()

        with patch.object(tmux_mod, "_tmux", return_value=(0, "", "")), \
             patch("os.kill", side_effect=fake_kill) as mock_kill:
            assert tmux_mod.stop_runtime(handle) is True
        assert mock_kill.call_args_list[0].args == (4242, 15)
        assert 9 not in [c.args[1] for c in mock_kill.call_args_list]

    def test_stop_runtime_kill_pane_failure_warns(self, tmp_path, caplog):
        handle = self._handle(tmp_path)
        (tmp_path / "r1.pid").write_text("4242")

        def fake_kill(pid, sig):
            if sig == 0:
                raise ProcessLookupError()

        with patch.object(tmux_mod, "_tmux", return_value=(1, "", "no pane")), \
             patch("os.kill", side_effect=fake_kill), \
             caplog.at_level(logging.WARNING, logger="codeagent.launchers.tmux"):
            assert tmux_mod.stop_runtime(handle) is True
        assert "tmux kill-pane failed: no pane" in caplog.text
