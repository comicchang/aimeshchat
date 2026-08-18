"""Domain contracts + runtime adapter tests.

Covers previously-uncovered lines in:
- ``codeagent/domain/__init__.py``: ExecutionSpec.from_args model-resolution
  chain (runtime_context / execution_context / agent_profile), the OMP 0600
  execution-context file reader, frozen dataclass contracts, and
  resolve_is_local.
- ``codeagent/runtime/generic.py`` / ``omp.py`` / ``opencode.py`` /
  ``registry.py``: spawn/detect/probe/send/resume/stop paths with
  subprocess/runner/tmux/mailbox mocked.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from codeagent.constants import DEFAULT_EXEC_TIMEOUT
from codeagent.domain import (
    ExecutionSpec,
    HostSpec,
    ModelContextUnavailable,
    RepoEntry,
    RepoMap,
    RunRequest,
    RunResult,
    SessionRecord,
    Target,
    TopicSpec,
    current_hostname,
    resolve_is_local,
)
from codeagent.runtime.base import (
    CAP_HOT_RESUME,
    CAP_IN_LOOP_MESSAGES,
    CAP_NATIVE_UI,
    CAP_STREAM_EVENTS,
    CAP_TOOL_STATS,
    CAP_WARM_RESUME,
    RUNTIME_GENERIC,
    RUNTIME_OMP,
    RUNTIME_OPENCODE,
    RuntimeAdapter,
    RuntimeErrorCode,
    RuntimeHandle,
)
from codeagent.runtime.generic import GenericRuntimeAdapter
from codeagent.runtime.omp import OMPRuntimeAdapter
from codeagent.runtime.opencode import OpenCodeRuntimeAdapter
from codeagent.runtime.registry import RuntimeRegistry
from codeagent.runtime.supervisor import RuntimeSpec


def _args(**kw) -> SimpleNamespace:
    """Minimal CLI-args namespace consumed by ExecutionSpec.from_args."""
    defaults = dict(model="", variant="", system="", prompt="", agent="")
    defaults.update(kw)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# ExecutionSpec
# ---------------------------------------------------------------------------


class TestExecutionSpec:
    def test_field_access_and_frozen(self):
        spec = ExecutionSpec(
            provider="openai", model="openai/gpt-4o", variant="reasoning",
            system_prompt="sys", full_prompt="sys\n\nhi", model_source="explicit",
        )
        assert spec.provider == "openai"
        assert spec.model == "openai/gpt-4o"
        assert spec.variant == "reasoning"
        assert spec.system_prompt == "sys"
        assert spec.full_prompt == "sys\n\nhi"
        assert spec.model_source == "explicit"
        with pytest.raises(FrozenInstanceError):
            spec.model = "other"

    def test_from_args_explicit_wins(self):
        spec = ExecutionSpec.from_args(_args(
            model="ollama/llama3", variant="thinking", system="sys", prompt="hello", agent="a1",
        ))
        assert spec.model == "ollama/llama3"
        assert spec.variant == "thinking"
        assert spec.provider == "ollama"
        assert spec.model_source == "explicit"
        assert spec.full_prompt == "sys\n\nhello"

    def test_from_args_system_only_prompt(self):
        spec = ExecutionSpec.from_args(_args(system="sys", prompt=""))
        assert spec.full_prompt == "sys"

    def test_from_args_prompt_only(self):
        spec = ExecutionSpec.from_args(_args(prompt="hello"))
        assert spec.full_prompt == "hello"

    def test_from_args_model_without_provider_prefix(self):
        spec = ExecutionSpec.from_args(_args(model="gpt-4"))
        assert spec.model == "gpt-4"
        assert spec.provider == ""  # _extract_provider returns "" for no-slash model

    def test_from_args_runtime_context_inherits(self):
        spec = ExecutionSpec.from_args(
            _args(agent="a1"),
            resolve_runtime_context=lambda agent: ("openai/gpt-4o", "reasoning", "openai"),
        )
        assert spec.model == "openai/gpt-4o"
        assert spec.variant == "reasoning"
        assert spec.provider == "openai"
        assert spec.model_source == "runtime_context"

    def test_from_args_runtime_context_short_tuple_keeps_explicit_variant(self):
        spec = ExecutionSpec.from_args(
            _args(agent="a1", variant="fixed"),
            resolve_runtime_context=lambda agent: ("ollama/llama3",),
        )
        assert spec.model == "ollama/llama3"
        assert spec.variant == "fixed"  # explicit variant is not overwritten
        assert spec.provider == "ollama"  # provider extracted from model prefix

    def test_from_args_runtime_context_strict_raises(self):
        def boom(agent):
            raise ModelContextUnavailable("no runtime context")

        with pytest.raises(ModelContextUnavailable):
            ExecutionSpec.from_args(
                _args(agent="a1"), resolve_runtime_context=boom, runtime_context_strict=True,
            )

    def test_from_args_runtime_context_non_strict_falls_back(self, monkeypatch):
        def boom(agent):
            raise ModelContextUnavailable("no runtime context")

        monkeypatch.setattr(
            ExecutionSpec, "_read_omp_execution_context",
            lambda: ("openai/gpt-4o", "thinking", "openai"),
        )
        spec = ExecutionSpec.from_args(_args(agent="a1"), resolve_runtime_context=boom)
        assert spec.model_source == "execution_context"
        assert spec.model == "openai/gpt-4o"
        assert spec.variant == "thinking"

    def test_from_args_execution_context_file(self, monkeypatch):
        monkeypatch.setattr(
            ExecutionSpec, "_read_omp_execution_context",
            lambda: ("openai/gpt-4o", "thinking", "openai"),
        )
        spec = ExecutionSpec.from_args(_args(agent="a1"))
        assert spec.model_source == "execution_context"
        assert spec.model == "openai/gpt-4o"
        assert spec.variant == "thinking"
        assert spec.provider == "openai"

    def test_from_args_agent_profile_fallback(self):
        spec = ExecutionSpec.from_args(
            _args(agent="a1"), resolve_agent_model=lambda agent: "claude-3-5-sonnet",
        )
        assert spec.model_source == "agent_profile"
        assert spec.model == "claude-3-5-sonnet"
        assert spec.provider == ""

    def test_from_args_nothing_resolves(self):
        spec = ExecutionSpec.from_args(_args(agent="a1"))
        assert spec.model == ""
        assert spec.model_source == ""
        assert spec.provider == ""
        assert spec.full_prompt == ""

    def test_from_args_runtime_context_none_skips_to_profile(self):
        spec = ExecutionSpec.from_args(
            _args(agent="a1"),
            resolve_runtime_context=lambda agent: None,
            resolve_agent_model=lambda agent: "openai/gpt-4o",
        )
        assert spec.model_source == "agent_profile"

    def test_extract_provider(self):
        assert ExecutionSpec._extract_provider("openai/gpt-4o") == "openai"
        assert ExecutionSpec._extract_provider("gpt-4o") == ""


class TestReadOmpExecutionContext:
    def test_env_path_valid(self, tmp_path, monkeypatch):
        p = tmp_path / "exec-ctx.json"
        p.write_text(json.dumps({
            "provider": "openai", "model": "openai/gpt-4o", "variant": "thinking", "epoch": 1,
        }))
        monkeypatch.setenv("AIMESHCHAT_EXECUTION_CONTEXT", str(p))
        assert ExecutionSpec._read_omp_execution_context() == ("openai/gpt-4o", "thinking", "openai")

    def test_default_path_missing(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AIMESHCHAT_EXECUTION_CONTEXT", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fakehome"))
        assert ExecutionSpec._read_omp_execution_context() is None

    def test_env_path_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AIMESHCHAT_EXECUTION_CONTEXT", str(tmp_path / "nope.json"))
        assert ExecutionSpec._read_omp_execution_context() is None

    def test_invalid_json(self, tmp_path, monkeypatch):
        p = tmp_path / "bad.json"
        p.write_text("{not json")
        monkeypatch.setenv("AIMESHCHAT_EXECUTION_CONTEXT", str(p))
        assert ExecutionSpec._read_omp_execution_context() is None

    def test_missing_model_key(self, tmp_path, monkeypatch):
        p = tmp_path / "no-model.json"
        p.write_text(json.dumps({"variant": "thinking"}))
        monkeypatch.setenv("AIMESHCHAT_EXECUTION_CONTEXT", str(p))
        assert ExecutionSpec._read_omp_execution_context() is None

    def test_empty_model_ignored(self, tmp_path, monkeypatch):
        p = tmp_path / "empty-model.json"
        p.write_text(json.dumps({"model": "   "}))
        monkeypatch.setenv("AIMESHCHAT_EXECUTION_CONTEXT", str(p))
        assert ExecutionSpec._read_omp_execution_context() is None

    def test_invalid_model_schema_rejected(self, tmp_path, monkeypatch):
        p = tmp_path / "bad-model.json"
        p.write_text(json.dumps({"model": "bad model with spaces!"}))
        monkeypatch.setenv("AIMESHCHAT_EXECUTION_CONTEXT", str(p))
        assert ExecutionSpec._read_omp_execution_context() is None

    def test_read_oserror_returns_none(self, tmp_path, monkeypatch):
        # A directory passes exists() but read_text raises IsADirectoryError.
        d = tmp_path / "ctx-dir"
        d.mkdir()
        monkeypatch.setenv("AIMESHCHAT_EXECUTION_CONTEXT", str(d))
        assert ExecutionSpec._read_omp_execution_context() is None


# ---------------------------------------------------------------------------
# HostSpec / RepoEntry / TopicSpec / RepoMap / Target
# ---------------------------------------------------------------------------


class TestHostRepoMapDataclasses:
    def test_host_spec_fields_and_frozen(self):
        h = HostSpec(
            name="dev", ssh_alias="dev-alias", hostnames=("dev", "dev.local"),
            description="d", shell_prefix="zsh", transport="ssh",
        )
        assert (h.name, h.ssh_alias, h.hostnames, h.description, h.shell_prefix, h.transport) == (
            "dev", "dev-alias", ("dev", "dev.local"), "d", "zsh", "ssh",
        )
        with pytest.raises(FrozenInstanceError):
            h.name = "other"

    def test_host_spec_defaults(self):
        h = HostSpec(name="x", ssh_alias="x", hostnames=("x",))
        assert h.description == ""
        assert h.shell_prefix == ""
        assert h.transport == "ssh"
        assert h.fallback_ssh_alias == ""

    def test_repo_entry_fields_and_frozen(self):
        r = RepoEntry(host="dev", path="/src/a", note="primary")
        assert r.host == "dev"
        assert r.path == "/src/a"
        assert r.note == "primary"
        with pytest.raises(FrozenInstanceError):
            r.path = "/other"

    def test_topic_spec_repo_valid_and_bounds(self):
        t = TopicSpec(
            name="t1",
            repos=(RepoEntry("dev", "/a"), RepoEntry("dev", "/b")),
            description="d",
        )
        assert t.repo(0).path == "/a"
        assert t.repo(1).path == "/b"
        with pytest.raises(IndexError):
            t.repo(2)
        with pytest.raises(IndexError):
            t.repo(-1)

    def test_repo_map_topic_lookup(self, tmp_path):
        t = TopicSpec(name="t1", repos=(RepoEntry("dev", "/a"),))
        m = RepoMap(midocs_root=tmp_path, hosts={}, topics={"t1": t}, relay_zsh="zsh")
        assert m.topic("t1") is t
        with pytest.raises(KeyError):
            m.topic("nope")

    def test_target_properties(self):
        host = HostSpec(name="dev", ssh_alias="dev-alias", hostnames=("dev",))
        repo = RepoEntry(host="dev", path="/src/a")
        t = Target(host=host, repo=repo)
        assert t.workdir == "/src/a"
        assert t.ssh_alias == "dev-alias"
        assert t.topic is None
        assert t.repo_index == 0
        assert t.is_local is False
        with pytest.raises(FrozenInstanceError):
            t.repo_index = 1


# ---------------------------------------------------------------------------
# RunRequest / RunResult / SessionRecord
# ---------------------------------------------------------------------------


class TestRunRecords:
    def test_run_request_defaults(self):
        req = RunRequest(task="do it")
        assert req.workdir == ""
        assert req.backend is None
        assert req.agent is None
        assert req.model is None
        assert req.skills is None
        assert req.skip_permissions is True
        assert req.session_key is None
        assert req.new_session is False
        assert req.no_auto_resume is False
        assert req.topic is None
        assert req.repo_index == 0
        assert req.host is None
        assert req.raw is False
        assert req.timeout == DEFAULT_EXEC_TIMEOUT
        assert req.resume_session_id is None
        assert req.request_id == ""
        assert req.run_id == ""
        assert req.review_key == ""
        assert req.require_ack is False
        assert req.capabilities == ()

    def test_run_request_mutable(self):
        req = RunRequest(task="t")
        req.backend = "omp"
        req.workdir = "/tmp"
        req.capabilities = ("stream_events",)
        assert req.backend == "omp"
        assert req.workdir == "/tmp"
        assert req.capabilities == ("stream_events",)

    def test_run_result_defaults_and_fields(self):
        r = RunResult(returncode=0)
        assert r.stdout == ""
        assert r.stderr == ""
        assert r.session_id is None
        assert r.backend == ""
        assert r.host == ""
        assert r.workdir == ""
        r2 = RunResult(returncode=1, stdout="out", stderr="err", session_id="s1",
                       backend="omp", host="dev", workdir="/w")
        assert r2.returncode == 1
        assert r2.stdout == "out"
        assert r2.stderr == "err"
        assert r2.session_id == "s1"
        assert r2.backend == "omp"
        assert r2.host == "dev"
        assert r2.workdir == "/w"

    def test_session_record_defaults_and_fields(self):
        rec = SessionRecord(key="k1", session_id="s1", backend="omp", host="dev", workdir="/w")
        assert rec.agent == ""
        assert rec.model == ""
        assert rec.topic == ""
        assert rec.status == "active"
        assert rec.created_at == 0.0
        assert rec.updated_at == 0.0
        rec2 = SessionRecord(key="k", session_id="s", backend="b", host="h", workdir="w",
                             agent="a", model="m", topic="t", status="failed",
                             created_at=1.0, updated_at=2.0)
        assert rec2.status == "failed"
        assert rec2.created_at == 1.0
        assert rec2.updated_at == 2.0

    def test_current_hostname_short(self):
        import socket
        name = current_hostname()
        assert "." not in name
        assert name == socket.gethostname().split(".", 1)[0]


# ---------------------------------------------------------------------------
# resolve_is_local
# ---------------------------------------------------------------------------


class TestResolveIsLocal:
    def test_exact_short_match(self):
        host = HostSpec(name="dev", ssh_alias="dev", hostnames=("dev",))
        assert resolve_is_local(host, hostname="dev") is True

    def test_exact_fqdn_match(self):
        host = HostSpec(name="dev", ssh_alias="dev", hostnames=("dev.example.com",))
        assert resolve_is_local(host, hostname="dev.example.com") is True

    def test_short_vs_fqdn_cross_match(self):
        host = HostSpec(name="dev", ssh_alias="dev", hostnames=("dev.example.com",))
        assert resolve_is_local(host, hostname="dev") is True

    def test_case_insensitive(self):
        host = HostSpec(name="dev", ssh_alias="dev", hostnames=("DEV",))
        assert resolve_is_local(host, hostname="dev") is True

    def test_no_match(self):
        host = HostSpec(name="dev", ssh_alias="dev", hostnames=("other",))
        assert resolve_is_local(host, hostname="dev") is False

    def test_empty_candidate_skipped(self):
        host = HostSpec(name="dev", ssh_alias="dev", hostnames=("", "dev"))
        assert resolve_is_local(host, hostname="dev") is True

    def test_no_substring_false_positive(self):
        host = HostSpec(name="dev", ssh_alias="dev", hostnames=("devbox",))
        assert resolve_is_local(host, hostname="dev") is False


# ---------------------------------------------------------------------------
# GenericRuntimeAdapter
# ---------------------------------------------------------------------------


def _write_agent(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "agent.py"
    script.write_text("import sys, json\n" + body)
    script.chmod(0o755)
    return script


class TestGenericAdapter:
    def test_spawn_skips_blank_and_bad_json_lines(self, tmp_path):
        script = _write_agent(
            tmp_path,
            "for line in sys.stdin:\n"
            "    obj = json.loads(line)\n"
            "    if obj.get('type') == 'task':\n"
            "        print('', flush=True)\n"
            "        print('not-json', flush=True)\n"
            "        print(json.dumps({'type': 'progress', 'step': 1}), flush=True)\n"
            "        print(json.dumps({'type': 'result', 'ok': True}), flush=True)\n"
            "        break\n",
        )
        adapter = GenericRuntimeAdapter()
        handle = adapter.spawn({
            "argv": ["python3", str(script)], "task": "t",
            "host_alias": "h1", "generation": 3,
        })
        assert handle.extra["result"]["ok"] is True
        assert handle.extra["progress"] == [{"type": "progress", "step": 1}]
        assert handle.runtime_id.startswith("generic-")
        assert handle.runtime == RUNTIME_GENERIC
        assert handle.host_alias == "h1"
        assert handle.generation == 3
        assert handle.mode == "cold"
        assert handle.supervisor == "process"
        assert CAP_STREAM_EVENTS in handle.capabilities

    def test_spawn_missing_binary_raises(self):
        adapter = GenericRuntimeAdapter()
        with pytest.raises(RuntimeError, match="generic spawn failed"):
            adapter.spawn({"argv": ["/nonexistent/binary", "x"], "task": "t"})

    def test_spawn_protocol_failure_kills_proc(self):
        fake = MagicMock()
        fake.stdin.write.side_effect = OSError("pipe closed")
        with patch("codeagent.runtime.generic.subprocess.Popen", return_value=fake):
            adapter = GenericRuntimeAdapter()
            with pytest.raises(RuntimeError, match="generic protocol failure"):
                adapter.spawn({"argv": ["python3", "-c", "pass"], "task": "t"})
        fake.kill.assert_called_once()
        fake.wait.assert_called_once()

    def test_no_argv_rejected(self):
        adapter = GenericRuntimeAdapter()
        with pytest.raises(ValueError, match="argv"):
            adapter.spawn({"task": "t"})

    def test_send_subscribe_resume_not_implemented(self):
        adapter = GenericRuntimeAdapter()
        handle = RuntimeHandle(runtime_id="g1", runtime=RUNTIME_GENERIC)
        with pytest.raises(NotImplementedError):
            adapter.send(handle, {})
        with pytest.raises(NotImplementedError):
            adapter.subscribe(handle)
        with pytest.raises(NotImplementedError):
            adapter.resume(handle, "p")

    def test_probe_alive_and_dead(self):
        adapter = GenericRuntimeAdapter()
        alive = RuntimeHandle(runtime_id="g1", runtime=RUNTIME_GENERIC,
                              extra={"result": {"ok": True}})
        out = adapter.probe(alive)
        assert out["alive"] is True
        assert out["runtime"] == RUNTIME_GENERIC
        assert out["backend_session_id"] == ""
        assert out["cold_only"] is True
        dead = RuntimeHandle(runtime_id="g2", runtime=RUNTIME_GENERIC, extra={})
        assert adapter.probe(dead)["alive"] is False

    def test_stop_is_noop(self):
        adapter = GenericRuntimeAdapter()
        handle = RuntimeHandle(runtime_id="g1", runtime=RUNTIME_GENERIC)
        assert adapter.stop(handle, "done") is None


# ---------------------------------------------------------------------------
# OMPRuntimeAdapter
# ---------------------------------------------------------------------------


class TestOMPAdapter:
    def _short_task_handle(self):
        from codeagent.domain import RunResult

        with patch("codeagent.runners.omp.OMPRunner") as mock_cls:
            fake = mock_cls.return_value
            fake.run.return_value = RunResult(returncode=0, stdout="ok", stderr="", session_id="s9")
            handle = OMPRuntimeAdapter().spawn({
                "short_task": True, "task": "t", "workdir": "/tmp", "timeout": 30,
                "agent_id": "a1", "model": "m", "review_key": "rk",
                "request_id": "r1", "run_id": "r2", "generation": 2,
            })
        return handle

    def test_spawn_short_task_via_spawn(self):
        handle = self._short_task_handle()
        assert handle.runtime_id.startswith("omp-")
        assert handle.mode == "short_task"
        assert handle.supervisor == "process"
        assert handle.backend_session_id == "s9"
        assert handle.generation == 2
        assert handle.extra["result"] == {"returncode": 0, "stdout": "ok", "stderr": ""}
        assert CAP_STREAM_EVENTS in handle.capabilities
        assert CAP_HOT_RESUME not in handle.capabilities

    def test_spawn_interactive_plugin_builds_spec(self):
        with patch("codeagent.runtime.omp.spawn_runtime",
                   return_value=MagicMock(runtime_id="ignored")) as mock_spawn:
            handle = OMPRuntimeAdapter().spawn({
                "workdir": "/w", "session_id": "sess", "agent_id": "a1",
                "review_key": "rk", "generation": 2, "backend_session_id": "bs1",
                "model": "m", "profile_args": ["--profile"], "gateway_socket": "/tmp/gw.sock",
                "owner_pid": 123, "nonce": "n1", "host_alias": "dev",
                "env": {"A": "1"}, "session_dir": "/sd",
            })
        assert mock_spawn.call_count == 1
        spec = mock_spawn.call_args[0][0]
        assert isinstance(spec, RuntimeSpec)
        assert spec.mode == "interactive_plugin"
        assert spec.workdir == "/w"
        assert spec.session_id == "sess"
        assert spec.agent_id == "a1"
        assert spec.review_key == "rk"
        assert spec.generation == 2
        assert spec.backend_session_id == "bs1"
        assert spec.model == "m"
        assert spec.profile_args == ["--profile"]
        assert spec.gateway_socket == "/tmp/gw.sock"
        assert spec.owner_pid == 123
        assert spec.nonce == "n1"
        assert spec.host_alias == "dev"
        assert spec.env == {"A": "1"}
        assert spec.session_dir == "/sd"
        assert set(spec.capabilities) == {
            CAP_STREAM_EVENTS, CAP_IN_LOOP_MESSAGES, CAP_TOOL_STATS,
            CAP_NATIVE_UI, CAP_HOT_RESUME, CAP_WARM_RESUME,
        }
        assert handle.runtime_id.startswith("omp-")
        assert handle.supervisor == "tmux"
        assert handle.mode == "interactive_plugin"
        assert handle.backend_session_id == "bs1"
        assert handle.host_alias == "dev"
        assert CAP_HOT_RESUME in handle.capabilities

    def test_send_enqueues_mailbox(self):
        receipt = MagicMock()
        receipt.msg_id = "m1"
        receipt.status = "delivered"
        with patch("codeagent.mailbox.service.MailboxService") as MockSvc:
            MockSvc.return_value.send.return_value = receipt
            handle = RuntimeHandle(
                runtime_id="rt1", runtime=RUNTIME_OMP, mode="interactive_plugin",
                extra={"session_id": "sess", "agent_id": "w1"},
            )
            out = OMPRuntimeAdapter().send(handle, {
                "body": "steer", "require_ack": True, "run_id": "r1", "request_id": "q1",
            })
        assert out == {"msg_id": "m1", "status": "delivered"}
        MockSvc.return_value.send.assert_called_once_with(
            session_id="sess", from_id="manager", to_id="w1", subject="steer",
            body="steer", kind="TASK", run_id="r1", request_id="q1", require_ack=True,
        )

    def test_send_short_task_rejected(self):
        handle = self._short_task_handle()
        with pytest.raises(RuntimeErrorCode) as ei:
            OMPRuntimeAdapter().send(handle, {})
        assert ei.value.code == "UNSUPPORTED_RUNTIME"

    def test_subscribe_not_implemented(self):
        handle = RuntimeHandle(runtime_id="rt1", runtime=RUNTIME_OMP)
        with pytest.raises(NotImplementedError):
            OMPRuntimeAdapter().subscribe(handle)

    def test_probe_short_task(self):
        handle = self._short_task_handle()
        assert OMPRuntimeAdapter().probe(handle) == {
            "alive": False, "reason": "short_task is bounded — no persistent runtime",
        }

    def test_probe_tmux_handle(self, tmp_path):
        spec = RuntimeSpec(runtime_id="rt1", session_id="s", agent_id="a")
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(spec.to_dict()))
        handle = RuntimeHandle(runtime_id="rt1", runtime=RUNTIME_OMP,
                               extra={"spec_path": str(spec_path)})
        with patch("codeagent.launchers.tmux.probe_runtime",
                   return_value={"alive": True, "pane_alive": True}) as mock_probe:
            out = OMPRuntimeAdapter().probe(handle)
        assert out == {"alive": True, "pane_alive": True}
        mock_probe.assert_called_once()

    def test_probe_missing_spec_reports_dead(self):
        handle = RuntimeHandle(runtime_id="rt1", runtime=RUNTIME_OMP,
                               extra={"spec_path": "/nonexistent/spec.json"})
        out = OMPRuntimeAdapter().probe(handle)
        assert out["alive"] is False
        assert out["reason"]

    def test_resume_warm_bumps_generation(self):
        orig = RuntimeHandle(
            runtime_id="rt1", runtime=RUNTIME_OMP, backend_session_id="bs1",
            host_alias="dev", generation=2, capabilities=frozenset({CAP_STREAM_EVENTS}),
            extra={"session_id": "sess", "agent_id": "a1", "review_key": "rk",
                   "workdir": "/w", "gateway_socket": "/tmp/gw.sock", "session_dir": "/sd"},
        )
        with patch("codeagent.runtime.supervisor.spawn_runtime",
                   return_value=MagicMock(runtime_id="rt2")) as mock_spawn:
            new_handle = OMPRuntimeAdapter().resume(orig, "follow up")
        spec = mock_spawn.call_args[0][0]
        assert isinstance(spec, RuntimeSpec)
        assert spec.mode == "interactive_plugin"
        assert spec.generation == 3
        assert spec.backend_session_id == "bs1"
        assert spec.task == "follow up"
        assert spec.owner_pid == os.getpid()
        assert spec.nonce
        assert new_handle.runtime_id == "rt2"
        assert new_handle.mode == "warm"
        assert new_handle.generation == 3
        assert new_handle.backend_session_id == "bs1"
        assert new_handle.host_alias == "dev"
        assert new_handle.extra["session_id"] == "sess"
        assert new_handle.extra["agent_id"] == "a1"
        assert CAP_HOT_RESUME in new_handle.capabilities

    def test_stop_no_spec_path_is_noop(self):
        handle = RuntimeHandle(runtime_id="rt1", runtime=RUNTIME_OMP, extra={})
        assert OMPRuntimeAdapter().stop(handle, "done") is None

    def test_stop_with_spec_path(self, tmp_path):
        spec_path = tmp_path / "spec.json"
        spec_path.write_text("{}")
        handle = RuntimeHandle(runtime_id="rt1", runtime=RUNTIME_OMP,
                               extra={"spec_path": str(spec_path)})
        with patch("codeagent.runtime.supervisor.stop_runtime") as mock_stop:
            OMPRuntimeAdapter().stop(handle, "done")
        mock_stop.assert_called_once()

    def test_stop_exception_swallowed(self, tmp_path):
        spec_path = tmp_path / "spec.json"
        spec_path.write_text("{}")
        handle = RuntimeHandle(runtime_id="rt1", runtime=RUNTIME_OMP,
                               extra={"spec_path": str(spec_path)})
        with patch("codeagent.runtime.supervisor.stop_runtime",
                   side_effect=RuntimeError("tmux gone")):
            OMPRuntimeAdapter().stop(handle, "done")  # must not raise


# ---------------------------------------------------------------------------
# OpenCodeRuntimeAdapter
# ---------------------------------------------------------------------------


class _FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)
        self._i = 0

    def readline(self):
        if self._i >= len(self._lines):
            return ""
        line = self._lines[self._i]
        self._i += 1
        return line

    def __iter__(self):
        return iter(self._lines)


class TestOpenCodeAdapter:
    @staticmethod
    def _fake_proc(stdout_lines):
        proc = subprocess.Popen.__new__(subprocess.Popen)  # type: ignore[attr-defined]
        proc.stdout = _FakeStdout(stdout_lines)
        proc.stderr = None
        proc.returncode = 0
        proc.args = []
        return proc

    def test_spawn_full_argv_env_and_session_capture(self):
        captured = {}
        proc = self._fake_proc([
            "garbage line\n",
            '{"sessionID": "oc-sess-42", "type": "assistant"}\n',
            '{"type": "result", "body": "done"}\n',
        ])

        def _fake_popen(argv, **kwargs):
            captured["argv"] = list(argv)
            captured["env"] = kwargs.get("env")
            return proc

        with patch("codeagent.runtime.opencode.subprocess.Popen", side_effect=_fake_popen):
            handle = OpenCodeRuntimeAdapter().spawn({
                "workdir": "/w", "agent_id": "a1", "model": "openai/gpt-4o",
                "variant": "thinking", "task": "do the thing",
                "env": {"OMP_MODEL_FALLBACK_CHAIN": "x"},
                "host_alias": "dev", "generation": 5,
            })
        argv = captured["argv"]
        assert argv[:6] == ["opencode", "run", "--format", "json", "--dir", "/w"]
        assert argv[argv.index("--agent") + 1] == "a1"
        assert argv[argv.index("--model") + 1] == "openai/gpt-4o"
        assert argv[argv.index("--variant") + 1] == "thinking"
        assert argv[-1] == "do the thing"
        env = captured["env"]
        assert env["OMP_MODEL_FALLBACK_CHAIN"] == "x"
        assert env["PATH"] == os.environ["PATH"]
        assert handle.backend_session_id == "oc-sess-42"
        assert handle.mode == "first_run"
        assert handle.host_alias == "dev"
        assert handle.generation == 5
        assert handle.supervisor == "process"
        assert handle.extra["cwd"] == "/w"
        assert "garbage line" in handle.extra["result"]["stdout"]

    def test_spawn_popen_failure_raises(self):
        with patch("codeagent.runtime.opencode.subprocess.Popen",
                   side_effect=FileNotFoundError("no opencode")):
            with pytest.raises(RuntimeError, match="opencode spawn failed"):
                OpenCodeRuntimeAdapter().spawn({"workdir": "/tmp", "agent_id": "a1"})

    def test_send_degraded_mailbox(self):
        receipt = MagicMock()
        receipt.msg_id = "m1"
        receipt.status = "delivered"
        with patch("codeagent.mailbox.service.MailboxService") as MockSvc:
            MockSvc.return_value.send.return_value = receipt
            handle = RuntimeHandle(runtime_id="rt1", runtime=RUNTIME_OPENCODE,
                                   extra={"session_id": "sess", "agent_id": "w1"})
            out = OpenCodeRuntimeAdapter().send(handle, {"body": "hi"})
        assert out["msg_id"] == "m1"
        assert out["status"] == "delivered"
        assert out["degraded"] is True
        MockSvc.return_value.send.assert_called_once_with(
            session_id="sess", from_id="manager", to_id="w1", subject="next-turn",
            body="hi", kind="TASK", run_id="", request_id="",
        )

    def test_probe_alive_dead_and_legacy(self):
        adapter = OpenCodeRuntimeAdapter()
        alive_proc = MagicMock()
        alive_proc.poll.return_value = None
        alive = RuntimeHandle(runtime_id="rt1", runtime=RUNTIME_OPENCODE,
                              backend_session_id="bs1", extra={"proc": alive_proc})
        out = adapter.probe(alive)
        assert out["alive"] is True
        assert out["backend_session_id"] == "bs1"
        assert out["in_loop_messages"] is False
        assert out["tool_stats"] is False
        assert out["native_ui"] is False
        assert out["hot_resume"] is False
        assert out["warm_resume"] is True

        dead_proc = MagicMock()
        dead_proc.poll.return_value = 0
        dead = RuntimeHandle(runtime_id="rt2", runtime=RUNTIME_OPENCODE,
                             extra={"proc": dead_proc})
        assert adapter.probe(dead)["alive"] is False

        legacy = RuntimeHandle(runtime_id="rt3", runtime=RUNTIME_OPENCODE, extra={})
        assert adapter.probe(legacy)["alive"] is True

    def test_resume_reuses_handle_state(self):
        captured = {}
        proc = self._fake_proc(['{"sessionID": "oc-new"}\n'])

        def _fake_popen(argv, **kwargs):
            captured["argv"] = list(argv)
            return proc

        handle = RuntimeHandle(
            runtime_id="rt1", runtime=RUNTIME_OPENCODE, backend_session_id="oc-old",
            host_alias="dev", generation=2,
            extra={"cwd": "/w", "agent": "a1", "model": "m1", "variant": "v1", "env": {"K": "V"}},
        )
        with patch("codeagent.runtime.opencode.subprocess.Popen", side_effect=_fake_popen):
            new_handle = OpenCodeRuntimeAdapter().resume(handle, "continue")
        argv = captured["argv"]
        assert argv[argv.index("--session") + 1] == "oc-old"
        assert argv[argv.index("--agent") + 1] == "a1"
        assert new_handle.mode == "warm"
        assert new_handle.generation == 3
        assert new_handle.host_alias == "dev"
        assert new_handle.backend_session_id == "oc-old"

    def test_stop_terminate_then_kill(self):
        proc = MagicMock()
        proc.poll.return_value = None
        proc.terminate = MagicMock()
        proc.wait.side_effect = TimeoutError("grace elapsed")
        proc.kill = MagicMock()
        handle = RuntimeHandle(runtime_id="rt1", runtime=RUNTIME_OPENCODE,
                               extra={"proc": proc})
        OpenCodeRuntimeAdapter().stop(handle, "done")
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()

    def test_stop_kill_failure_swallowed(self):
        proc = MagicMock()
        proc.poll.return_value = None
        proc.terminate = MagicMock()
        proc.wait.side_effect = TimeoutError("grace elapsed")
        proc.kill.side_effect = Exception("already dead")
        handle = RuntimeHandle(runtime_id="rt1", runtime=RUNTIME_OPENCODE,
                               extra={"proc": proc})
        OpenCodeRuntimeAdapter().stop(handle, "done")  # must not raise

    def test_stop_noop_when_dead(self):
        proc = MagicMock()
        proc.poll.return_value = 0
        handle = RuntimeHandle(runtime_id="rt1", runtime=RUNTIME_OPENCODE,
                               extra={"proc": proc})
        OpenCodeRuntimeAdapter().stop(handle, "done")
        proc.terminate.assert_not_called()


# ---------------------------------------------------------------------------
# RuntimeRegistry
# ---------------------------------------------------------------------------


class TestRegistryBuild:
    def test_build_defaults_omp_failure_swallowed(self):
        with patch("codeagent.runtime.omp.OMPRuntimeAdapter",
                   side_effect=RuntimeError("no tmux")):
            reg = RuntimeRegistry()
        assert RUNTIME_OMP not in reg.names()
        assert RUNTIME_OPENCODE in reg.names()
        assert RUNTIME_GENERIC in reg.names()

    def test_build_defaults_opencode_failure_swallowed(self):
        with patch("codeagent.runtime.opencode.OpenCodeRuntimeAdapter",
                   side_effect=RuntimeError("no opencode")):
            reg = RuntimeRegistry()
        assert RUNTIME_OPENCODE not in reg.names()
        assert RUNTIME_GENERIC in reg.names()

    def test_build_defaults_generic_failure_swallowed(self):
        with patch("codeagent.runtime.generic.GenericRuntimeAdapter",
                   side_effect=RuntimeError("no generic")):
            reg = RuntimeRegistry()
        assert RUNTIME_GENERIC not in reg.names()
        assert RUNTIME_OMP in reg.names()

    def test_get_no_adapters_raises_no_runtime(self):
        reg = RuntimeRegistry()
        reg._adapters.clear()
        with pytest.raises(RuntimeErrorCode) as ei:
            reg.get()
        assert ei.value.code == "UNSUPPORTED_RUNTIME"
        assert "no runtime adapter registered" in ei.value.message

    def test_adapter_capabilities_static_fallback(self):
        reg = RuntimeRegistry()

        class NoCaps(RuntimeAdapter):
            name = RUNTIME_GENERIC

        assert reg._adapter_capabilities(NoCaps()) == frozenset({CAP_STREAM_EVENTS})

        class Unknown(RuntimeAdapter):
            name = "weird"

        assert reg._adapter_capabilities(Unknown()) == frozenset()

    def test_handle_unknown_returns_none(self):
        reg = RuntimeRegistry()
        assert reg.handle("nope") is None


class TestRegistryHandleLifecycle:
    @staticmethod
    def _spawn_generic(reg, tmp_path):
        script = _write_agent(
            tmp_path,
            "for line in sys.stdin:\n"
            "    obj = json.loads(line)\n"
            "    if obj.get('type') == 'task':\n"
            "        print(json.dumps({'type': 'result', 'ok': True}), flush=True)\n"
            "        break\n",
        )
        return reg.spawn(RUNTIME_GENERIC, {"argv": ["python3", str(script)], "task": "t"})

    def test_spawn_registers_handle(self, tmp_path):
        reg = RuntimeRegistry()
        handle = self._spawn_generic(reg, tmp_path)
        assert reg.handle(handle.runtime_id) is handle

    def test_probe_handle(self, tmp_path):
        reg = RuntimeRegistry()
        handle = self._spawn_generic(reg, tmp_path)
        out = reg.probe(handle.runtime_id)
        assert out["runtime"] == RUNTIME_GENERIC
        assert out["alive"] is True

    def test_probe_unknown_runtime(self):
        reg = RuntimeRegistry()
        with pytest.raises(RuntimeErrorCode) as ei:
            reg.probe("missing")
        assert ei.value.code == "UNSUPPORTED_RUNTIME"
        assert "unknown runtime" in ei.value.message

    def test_probe_no_adapter_for_handle(self, tmp_path):
        reg = RuntimeRegistry()
        handle = self._spawn_generic(reg, tmp_path)
        reg._adapters.pop(RUNTIME_GENERIC, None)
        with pytest.raises(RuntimeErrorCode) as ei:
            reg.probe(handle.runtime_id)
        assert "no adapter for 'generic'" in ei.value.message

    def test_stop_removes_handle(self, tmp_path):
        reg = RuntimeRegistry()
        handle = self._spawn_generic(reg, tmp_path)
        reg.stop(handle.runtime_id, "done")
        assert reg.handle(handle.runtime_id) is None

    def test_stop_no_adapter_still_removes(self, tmp_path):
        reg = RuntimeRegistry()
        handle = self._spawn_generic(reg, tmp_path)
        reg._adapters.pop(RUNTIME_GENERIC, None)
        reg.stop(handle.runtime_id, "done")  # must not raise
        assert reg.handle(handle.runtime_id) is None

    def test_stop_unknown_runtime(self):
        reg = RuntimeRegistry()
        with pytest.raises(RuntimeErrorCode) as ei:
            reg.stop("missing", "done")
        assert ei.value.code == "UNSUPPORTED_RUNTIME"

    def test_send_delegates_to_adapter(self, tmp_path):
        reg = RuntimeRegistry()
        handle = self._spawn_generic(reg, tmp_path)
        with pytest.raises(NotImplementedError):
            reg.send(handle.runtime_id, {})  # generic adapter is cold-only

    def test_send_no_adapter_for_handle(self, tmp_path):
        reg = RuntimeRegistry()
        handle = self._spawn_generic(reg, tmp_path)
        reg._adapters.pop(RUNTIME_GENERIC, None)
        with pytest.raises(RuntimeErrorCode) as ei:
            reg.send(handle.runtime_id, {})
        assert "no adapter for 'generic'" in ei.value.message

    def test_send_unknown_runtime(self):
        reg = RuntimeRegistry()
        with pytest.raises(RuntimeErrorCode) as ei:
            reg.send("missing", {})
        assert ei.value.code == "UNSUPPORTED_RUNTIME"
