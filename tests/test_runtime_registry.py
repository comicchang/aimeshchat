"""RuntimeRegistry tests — OMP→OpenCode→generic capability selection,
generic NDJSON fake agent, cold-only rejection, explicit runtime errors."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from codeagent.runtime.base import (
    CAP_HOT_RESUME,
    CAP_IN_LOOP_MESSAGES,
    CAP_STREAM_EVENTS,
    CAP_WARM_RESUME,
    RUNTIME_GENERIC,
    RUNTIME_OMP,
    RUNTIME_OPENCODE,
    RuntimeErrorCode,
)
from codeagent.runtime.registry import RuntimeRegistry


class TestSelection:
    def test_implicit_prefers_omp(self):
        reg = RuntimeRegistry()
        adapter = reg.get()
        assert adapter.name == RUNTIME_OMP

    def test_explicit_runtime_honored(self):
        reg = RuntimeRegistry()
        assert reg.get(RUNTIME_OPENCODE).name == RUNTIME_OPENCODE
        assert reg.get(RUNTIME_GENERIC).name == RUNTIME_GENERIC

    def test_explicit_unknown_returns_registry_contents(self):
        reg = RuntimeRegistry()
        with pytest.raises(RuntimeErrorCode) as ei:
            reg.get("bogus")
        assert ei.value.code == "UNSUPPORTED_RUNTIME"
        assert "registered" in ei.value.message
        assert RUNTIME_OMP in ei.value.message

    def test_hot_resume_requires_omp(self):
        reg = RuntimeRegistry()
        adapter = reg.get(required_capabilities=frozenset({CAP_HOT_RESUME}))
        assert adapter.name == RUNTIME_OMP

    def test_warm_resume_selects_omp_then_opencode(self):
        reg = RuntimeRegistry()
        adapter = reg.get(required_capabilities=frozenset({CAP_WARM_RESUME}))
        assert adapter.name in (RUNTIME_OMP, RUNTIME_OPENCODE)

    def test_in_loop_messages_only_omp(self):
        reg = RuntimeRegistry()
        adapter = reg.get(required_capabilities=frozenset({CAP_IN_LOOP_MESSAGES}))
        assert adapter.name == RUNTIME_OMP

    def test_impossible_capability_rejected(self):
        reg = RuntimeRegistry()
        with pytest.raises(RuntimeErrorCode) as ei:
            reg.get(required_capabilities=frozenset({"no_such_cap"}))
        assert ei.value.code == "UNSUPPORTED_CAPABILITY"

    def test_opencode_with_omp_removed(self):
        """When OMP is unavailable, warm_resume falls back to OpenCode."""
        reg = RuntimeRegistry()
        # Simulate no OMP adapter: unregister it.
        reg._adapters.pop(RUNTIME_OMP, None)
        adapter = reg.get(required_capabilities=frozenset({CAP_WARM_RESUME}))
        assert adapter.name == RUNTIME_OPENCODE

    def test_generic_cannot_satisfy_warm_resume(self):
        reg = RuntimeRegistry()
        reg._adapters.pop(RUNTIME_OMP, None)
        reg._adapters.pop(RUNTIME_OPENCODE, None)
        with pytest.raises(RuntimeErrorCode) as ei:
            reg.get(required_capabilities=frozenset({CAP_WARM_RESUME}))
        assert ei.value.code == "UNSUPPORTED_CAPABILITY"


class TestGenericNDJSON:
    def _fake_agent(self, tmp_path: Path) -> Path:
        """A fake NDJSON agent: reads task, emits progress + result, exits 0."""
        script = tmp_path / "agent.py"
        script.write_text(
            "import sys, json\n"
            "for line in sys.stdin:\n"
            "    obj = json.loads(line)\n"
            "    if obj.get('type') == 'task':\n"
            "        print(json.dumps({'type': 'progress', 'step': 1}), flush=True)\n"
            "        print(json.dumps({'type': 'progress', 'step': 2}), flush=True)\n"
            "        print(json.dumps({'type': 'result', 'ok': True, 'summary': 'done'}), flush=True)\n"
            "        break\n"
        )
        script.chmod(0o755)
        return script

    def test_progress_and_result(self, tmp_path: Path):
        script = self._fake_agent(tmp_path)
        reg = RuntimeRegistry()
        handle = reg.spawn(RUNTIME_GENERIC, {
            "argv": ["python3", str(script)],
            "task": "analyze x",
            "workdir": str(tmp_path),
        })
        result = handle.extra["result"]
        assert result["ok"] is True
        assert result["summary"] == "done"
        progress = handle.extra["progress"]
        assert len(progress) == 2
        assert handle.mode == "cold"
        assert handle.backend_session_id == ""

    def test_error_frame(self, tmp_path: Path):
        script = tmp_path / "agent_err.py"
        script.write_text(
            "import sys, json\n"
            "for line in sys.stdin:\n"
            "    obj = json.loads(line)\n"
            "    if obj.get('type') == 'task':\n"
            "        print(json.dumps({'type': 'error', 'error': 'boom'}), flush=True)\n"
            "        break\n"
        )
        script.chmod(0o755)
        reg = RuntimeRegistry()
        handle = reg.spawn(RUNTIME_GENERIC, {
            "argv": ["python3", str(script)],
            "task": "t",
        })
        assert handle.extra["result"]["error"] == "boom"

    def test_cold_only_rejects_warm_resume(self):
        reg = RuntimeRegistry()
        reg._adapters.pop(RUNTIME_OMP, None)
        reg._adapters.pop(RUNTIME_OPENCODE, None)
        with pytest.raises(RuntimeErrorCode) as ei:
            reg.spawn(RUNTIME_GENERIC, {"task": "t"},
                      required_capabilities=frozenset({CAP_WARM_RESUME}))
        assert ei.value.code == "UNSUPPORTED_CAPABILITY"

    def test_no_argv_rejected(self):
        reg = RuntimeRegistry()
        with pytest.raises(ValueError, match="argv"):
            reg.spawn(RUNTIME_GENERIC, {"task": "t"})


class TestOpenCodeAdapter:
    def test_first_run_extracts_session(self):
        """A fake opencode process emitting a session event yields the id."""
        from codeagent.runtime.opencode import OpenCodeRuntimeAdapter

        class FakeStdout:
            def __init__(self):
                self._lines = [
                    '{"type":"session","id":"oc-sess-123"}\n',
                    '{"type":"assistant","message_end":{"message":"hi"}}\n',
                ]
                self._i = 0

            def readline(self):
                if self._i >= len(self._lines):
                    return ""
                line = self._lines[self._i]
                self._i += 1
                return line

            def __iter__(self):
                return iter(self._lines)

        proc = subprocess.Popen.__new__(subprocess.Popen)  # type: ignore[attr-defined]
        proc.stdout = FakeStdout()
        proc.stderr = None
        proc.returncode = 0

        def _fake_popen(argv, **kwargs):
            proc.args = argv
            return proc

        with patch("subprocess.Popen", side_effect=_fake_popen):
            adapter = OpenCodeRuntimeAdapter()
            handle = adapter.spawn({"workdir": "/tmp", "agent_id": "a1"})
        assert handle.backend_session_id == "oc-sess-123"
        assert handle.mode == "first_run"
        assert "--dir" in proc.args and "/tmp" in proc.args

    def test_warm_uses_session_flag(self):
        from codeagent.runtime.opencode import OpenCodeRuntimeAdapter

        proc = subprocess.Popen.__new__(subprocess.Popen)  # type: ignore[attr-defined]
        proc.stdout = None
        proc.stderr = None
        proc.returncode = 0

        def _fake_popen(argv, **kwargs):
            proc.args = argv
            return proc

        with patch("subprocess.Popen", side_effect=_fake_popen):
            adapter = OpenCodeRuntimeAdapter()
            handle = adapter.spawn({
                "workdir": "/tmp", "agent_id": "a1",
                "backend_session_id": "oc-existing",
            })
        assert handle.backend_session_id == "oc-existing"
        assert "--session" in proc.args

    def test_adapter_capabilities_honest(self):
        from codeagent.runtime.opencode import OpenCodeRuntimeAdapter

        adapter = OpenCodeRuntimeAdapter()
        assert CAP_STREAM_EVENTS in adapter.capabilities
        assert CAP_WARM_RESUME in adapter.capabilities
        assert CAP_IN_LOOP_MESSAGES not in adapter.capabilities
        assert CAP_HOT_RESUME not in adapter.capabilities


class TestOMPAdapter:
    def test_short_task_handle_caps(self):
        """short_task handles only claim stream_events/warm_resume."""
        from codeagent.runtime.omp import OMPRuntimeAdapter
        from codeagent.domain import RunResult

        adapter = OMPRuntimeAdapter()
        with patch("codeagent.runners.omp.OMPRunner") as mock_cls:
            fake = mock_cls.return_value
            fake.run.return_value = RunResult(returncode=0, stdout="ok", session_id="s9")
            handle = adapter._spawn_short_task(
                {"task": "t", "workdir": "/tmp", "timeout": 60},
                "rt-short",
            )
        assert "hot_resume" not in handle.capabilities
        assert "in_loop_messages" not in handle.capabilities
        assert "native_ui" not in handle.capabilities
        assert "tool_stats" not in handle.capabilities
        assert "stream_events" in handle.capabilities
        assert "warm_resume" in handle.capabilities

    def test_full_capabilities_default(self):
        from codeagent.runtime.omp import OMPRuntimeAdapter

        adapter = OMPRuntimeAdapter()
        assert adapter.capabilities == frozenset({
            "stream_events", "in_loop_messages", "tool_stats", "native_ui",
            "hot_resume", "warm_resume",
        })

    def test_short_task_send_rejected(self):
        from codeagent.runtime.omp import OMPRuntimeAdapter
        from codeagent.domain import RunResult

        adapter = OMPRuntimeAdapter()
        with patch("codeagent.runners.omp.OMPRunner") as mock_cls:
            fake = mock_cls.return_value
            fake.run.return_value = RunResult(returncode=0, stdout="ok", session_id="s9")
            handle = adapter._spawn_short_task(
                {"task": "t", "workdir": "/tmp", "timeout": 60}, "rt-short",
            )
        with pytest.raises(RuntimeErrorCode):
            adapter.send(handle, {"body": "steer"})


class TestRegistrySpawnErrors:
    def test_unknown_runtime_in_spawn(self):
        reg = RuntimeRegistry()
        with pytest.raises(RuntimeErrorCode) as ei:
            reg.spawn("not-a-runtime", {"task": "t"})
        assert ei.value.code == "UNSUPPORTED_RUNTIME"

    def test_names_returns_sorted(self):
        reg = RuntimeRegistry()
        assert reg.names() == sorted(reg.names())
        assert set(reg.names()) == {RUNTIME_OMP, RUNTIME_OPENCODE, RUNTIME_GENERIC}
