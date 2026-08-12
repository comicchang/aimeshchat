"""Tmux runtime supervisor tests — markers, generation, spec security,
no capture-pane state dependence, capability contract for adapters."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

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
