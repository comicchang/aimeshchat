"""TASK C1 — opencode adapter e2e unit tests (fake binary, no real spawn).

Covers:
  1. OpenCodeRuntimeAdapter.spawn: session-id extraction from fake JSONL
  2. Warm path: --session in argv when backend_session_id provided
  3. Non-selectable stdout fallback (no fileno → direct readline)
  4. probe / resume / stop contracts
  5. Gateway integration: runtime.register → runtime.event → events.list
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from codeagent.gateway.events import EventStore
from codeagent.gateway.model import (
    ERR_NOT_AUTHORIZED,
    EVENT_KINDS,
    GatewayError,
    RuntimeEventDraft,
)
from codeagent.gateway.server import GatewayServer
from codeagent.gateway.service import AgentGateway
from codeagent.mailbox.store import MailboxStore
from codeagent.runtime.base import CAP_STREAM_EVENTS, CAP_WARM_RESUME, RUNTIME_OPENCODE
from codeagent.runtime.opencode import OpenCodeRuntimeAdapter


# ── helpers ────────────────────────────────────────────────────────────


class _FakeProcess:
    """Minimal subprocess.Popen stand-in with configurable stdout."""

    def __init__(self, stdout_lines: list[str] | None = None,
                 stdout_obj: Any = None) -> None:
        self._lines = stdout_lines or []
        self._stdout_obj = stdout_obj
        self.returncode = None
        self._idx = 0

    @property
    def stdout(self):
        if self._stdout_obj is not None:
            return self._stdout_obj
        return self

    @property
    def stderr(self):
        return io.StringIO("")

    def readline(self) -> str:
        if self._idx < len(self._lines):
            line = self._lines[self._idx]
            self._idx += 1
            return line
        return ""

    def __iter__(self):
        """adapter 用 ``for line in proc.stdout`` 迭代（PIPE 排空修复 6eef992）。"""
        return self

    def __next__(self) -> str:
        line = self.readline()
        if line == "":
            raise StopIteration
        return line

    def fileno(self) -> int:
        # Selectable — returns a valid fd.
        return 0


class _FakeProcessNoFileno(_FakeProcess):
    """Fake stdout without fileno — triggers the non-selectable fallback."""

    def fileno(self) -> int:
        raise OSError("no fileno")


def _session_line(session_id: str = "sess-abc123") -> str:
    return json.dumps({"type": "session", "id": session_id}) + "\n"


def _fake_popen(argv: list[str], stdout=None, stderr=None, text=True,
                cwd=None, **kw) -> _FakeProcess:
    """Fake Popen that returns session JSONL based on argv."""
    # Determine if warm (--session present): emit same session id.
    has_session = "--session" in argv
    if has_session:
        idx = argv.index("--session")
        sid = argv[idx + 1] if idx + 1 < len(argv) else "warm-sess"
        lines = [_session_line(sid)]
    else:
        lines = [_session_line("new-sess-42")]
    return _FakeProcess(stdout_lines=lines)


def _make_gateway(tmp_path: Path) -> tuple[AgentGateway, Path]:
    """Create an isolated AgentGateway with short /tmp socket path."""
    base = Path("/tmp") / f"oc-e2e-{uuid.uuid4().hex[:8]}"
    base.mkdir(parents=True, exist_ok=True)
    sock = base / "g.sock"
    db = base / "e.sqlite3"
    store = MailboxStore(root=base / "mailbox")
    store.root.mkdir(parents=True, exist_ok=True)
    events = EventStore(db_path=db, source_host="testhost")
    gw = AgentGateway(store=store, events=events)
    return gw, sock


def _serve_gateway(gw: AgentGateway, sock: Path) -> tuple[GatewayServer, threading.Thread]:
    """Start gateway server in background thread, wait for socket."""
    server = GatewayServer(socket_path=sock, gateway=gw)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    deadline = 0
    while not sock.exists() and deadline < 100:
        time.sleep(0.02)
        deadline += 1
    assert sock.exists(), "gateway socket not created"
    return server, t


# ── adapter tests (pure unit, no gateway) ──────────────────────────────


class TestOpenCodeSpawn:
    """OpenCodeRuntimeAdapter.spawn with mocked Popen."""

    def test_spawn_extracts_session_id(self):
        """Fake Popen emits session JSONL → backend_session_id extracted."""
        adapter = OpenCodeRuntimeAdapter()
        with patch("subprocess.Popen", side_effect=_fake_popen):
            handle = adapter.spawn({"workdir": "/tmp/test-work", "agent_id": "worker-1"})

        assert handle.backend_session_id == "new-sess-42"
        assert handle.runtime == RUNTIME_OPENCODE
        assert CAP_STREAM_EVENTS in handle.capabilities
        assert CAP_WARM_RESUME in handle.capabilities
        assert handle.mode == "first_run"
        assert handle.extra["agent"] == "worker-1"
        assert handle.extra["cwd"] == "/tmp/test-work"

    def test_spawn_warm_path_argv_contains_session(self):
        """Warm resume: backend_session_id in request → --session in argv."""
        adapter = OpenCodeRuntimeAdapter()
        captured_argv: list[str] = []

        def capture_popen(argv, **kw):
            captured_argv.extend(argv)
            return _FakeProcess(stdout_lines=[_session_line("warm-99")])

        with patch("subprocess.Popen", side_effect=capture_popen):
            handle = adapter.spawn({
                "workdir": "/tmp/w",
                "agent_id": "a",
                "backend_session_id": "warm-99",
            })

        assert "--session" in captured_argv
        sess_idx = captured_argv.index("--session")
        assert captured_argv[sess_idx + 1] == "warm-99"
        assert handle.backend_session_id == "warm-99"
        assert handle.mode == "warm"

    def test_spawn_no_session_in_output_returns_empty_id(self):
        """Popen stdout has no session event → empty backend_session_id."""
        adapter = OpenCodeRuntimeAdapter()

        def no_session_popen(argv, **kw):
            # Emit some non-session JSON, then EOF.
            lines = [
                json.dumps({"type": "log", "msg": "starting"}) + "\n",
                "",  # EOF
            ]
            return _FakeProcess(stdout_lines=lines)

        with patch("subprocess.Popen", side_effect=no_session_popen):
            handle = adapter.spawn({"workdir": "/tmp/w", "agent_id": "a"})

        assert handle.backend_session_id == ""
        assert handle.mode == "first_run"

    def test_spawn_non_selectable_stdout_fallback(self):
        """FakeStdout without fileno → direct readline path (no select)."""
        adapter = OpenCodeRuntimeAdapter()
        fake_stdout = _FakeProcessNoFileno(stdout_lines=[_session_line("nosel-1")])

        def popen_no_fileno(argv, **kw):
            proc = _FakeProcess()
            proc._stdout_obj = fake_stdout
            return proc

        with patch("subprocess.Popen", side_effect=popen_no_fileno):
            handle = adapter.spawn({"workdir": "/tmp/w", "agent_id": "a"})

        assert handle.backend_session_id == "nosel-1"

    def test_spawn_oserror_raises_runtime_error(self):
        """Popen raises OSError → RuntimeError propagated."""
        adapter = OpenCodeRuntimeAdapter()
        with patch("subprocess.Popen", side_effect=OSError("no such binary")):
            with pytest.raises(RuntimeError, match="opencode spawn failed"):
                adapter.spawn({"workdir": "/tmp/w", "agent_id": "a"})

    def test_spawn_default_workdir_falls_back_to_cwd(self):
        """Missing workdir → os.getcwd()."""
        adapter = OpenCodeRuntimeAdapter()
        captured_cwd = {}

        def capture_popen(argv, **kw):
            captured_cwd["cwd"] = kw.get("cwd")
            return _FakeProcess(stdout_lines=[_session_line("s1")])

        with patch("subprocess.Popen", side_effect=capture_popen):
            adapter.spawn({"agent_id": "a"})

        assert captured_cwd["cwd"] == os.getcwd()


class TestOpenCodeProbeResumeStop:
    """probe / resume / stop contracts."""

    def _make_handle(self, session: str = "s1") -> Any:
        from codeagent.runtime.base import RuntimeHandle
        return RuntimeHandle(
            runtime_id="opencode-test123",
            runtime=RUNTIME_OPENCODE,
            backend_session_id=session,
            capabilities=frozenset({CAP_STREAM_EVENTS, CAP_WARM_RESUME}),
            supervisor="process",
            mode="first_run",
            extra={"cwd": "/tmp/w", "agent": "a"},
        )

    def test_probe_returns_expected_keys(self):
        adapter = OpenCodeRuntimeAdapter()
        h = self._make_handle()
        result = adapter.probe(h)
        assert result["alive"] is True
        assert result["runtime"] == RUNTIME_OPENCODE
        assert result["backend_session_id"] == "s1"
        assert result["warm_resume"] is True
        assert result["in_loop_messages"] is False
        assert result["native_ui"] is False

    def test_resume_re_spawns_with_session(self):
        adapter = OpenCodeRuntimeAdapter()
        h = self._make_handle(session="old-sess")
        captured_argv: list[str] = []

        def capture_popen(argv, **kw):
            captured_argv.extend(argv)
            return _FakeProcess(stdout_lines=[_session_line("new-from-resume")])

        with patch("subprocess.Popen", side_effect=capture_popen):
            h2 = adapter.resume(h, "continue please")

        assert "--session" in captured_argv
        assert "old-sess" in captured_argv
        # Warm path: backend_session_id from request is used directly (stdout
        # not read for session extraction when session_id is already provided).
        assert h2.backend_session_id == "old-sess"
        assert h2.mode == "warm"
        assert h2.generation == h.generation + 1

    def test_stop_is_noop(self):
        adapter = OpenCodeRuntimeAdapter()
        h = self._make_handle()
        # Should not raise.
        adapter.stop(h, "test")

    def test_subscribe_raises_not_implemented(self):
        adapter = OpenCodeRuntimeAdapter()
        h = self._make_handle()
        with pytest.raises(NotImplementedError):
            list(adapter.subscribe(h))


# ── gateway integration ───────────────────────────────────────────────


class TestOpenCodeGatewayIntegration:
    """runtime.register → runtime.event → events.list via real AgentGateway."""

    def test_register_and_event_flow(self, tmp_path: Path):
        """Full flow: register runtime, report TOOL_FINISHED, verify in events.list."""
        from codeagent.gateway.client import GatewayClient

        gw, sock = _make_gateway(tmp_path)
        # Pre-create a session so roster check passes.
        gw._store.session_init("sess-1", "manager-1", ["worker-1"])

        server, _ = _serve_gateway(gw, sock)
        try:
            client = GatewayClient(socket_path=sock, timeout=5)

            # Register the runtime.
            reg = client.call("runtime.register", {
                "session_id": "sess-1",
                "agent_id": "worker-1",
                "runtime_id": "opencode-e2e-test",
                "generation": 1,
                "runtime": "opencode",
                "backend_session_id": "fake-sess-id",
                "owner_pid": os.getpid(),
                "nonce": "test-nonce",
            })
            assert reg["runtime_id"] == "opencode-e2e-test"
            assert reg["session_id"] == "sess-1"
            assert reg["agent_id"] == "worker-1"

            # Report a TOOL_FINISHED event.
            ev_result = client.call("runtime.event", {
                "event": {
                    "runtime_id": "opencode-e2e-test",
                    "generation": 1,
                    "session_id": "sess-1",
                    "agent_id": "worker-1",
                    "request_id": "",
                    "run_id": "",
                    "kind": "TOOL_FINISHED",
                    "created_at": "2026-08-09T00:00:00Z",
                    "payload": {"tool": "bash", "duration_ms": 42},
                },
            })
            assert "event_id" in ev_result
            assert ev_result["source_sequence"] >= 1

            # Query events.list for this runtime.
            ev_list = client.call("events.list", {
                "runtime_id": "opencode-e2e-test",
            })
            kinds = [e["kind"] for e in ev_list["events"]]
            assert "TOOL_FINISHED" in kinds
            # Verify payload round-trips.
            finished = [e for e in ev_list["events"] if e["kind"] == "TOOL_FINISHED"]
            assert finished[0]["payload"]["tool"] == "bash"
            assert finished[0]["payload"]["duration_ms"] == 42
        finally:
            server.stop()

    def test_register_rejects_missing_identity(self, tmp_path: Path):
        """runtime.register without session_id → ERR_NOT_AUTHORIZED."""
        from codeagent.gateway.client import GatewayClient

        gw, sock = _make_gateway(tmp_path)
        server, _ = _serve_gateway(gw, sock)
        try:
            client = GatewayClient(socket_path=sock, timeout=5)
            with pytest.raises(GatewayError) as exc_info:
                client.call("runtime.register", {
                    "agent_id": "a",
                    "runtime_id": "r1",
                })
            assert exc_info.value.code == ERR_NOT_AUTHORIZED
        finally:
            server.stop()

    def test_register_rejects_unknown_agent(self, tmp_path: Path):
        """agent not in session roster → ERR_NOT_AUTHORIZED."""
        from codeagent.gateway.client import GatewayClient

        gw, sock = _make_gateway(tmp_path)
        gw._store.session_init("sess-2", "manager-1", ["worker-1"])
        server, _ = _serve_gateway(gw, sock)
        try:
            client = GatewayClient(socket_path=sock, timeout=5)
            with pytest.raises(GatewayError) as exc_info:
                client.call("runtime.register", {
                    "session_id": "sess-2",
                    "agent_id": "unknown-agent",
                    "runtime_id": "r1",
                    "generation": 1,
                })
            assert exc_info.value.code == ERR_NOT_AUTHORIZED
        finally:
            server.stop()

    def test_events_list_filters_by_kind(self, tmp_path: Path):
        """events.list with filters returns only matching kinds."""
        from codeagent.gateway.client import GatewayClient

        gw, sock = _make_gateway(tmp_path)
        gw._store.session_init("sess-3", "mgr", ["w1"])
        server, _ = _serve_gateway(gw, sock)
        try:
            client = GatewayClient(socket_path=sock, timeout=5)
            # Register.
            client.call("runtime.register", {
                "session_id": "sess-3",
                "agent_id": "w1",
                "runtime_id": "r-filter",
                "generation": 1,
                "runtime": "opencode",
                "owner_pid": os.getpid(),
                "nonce": "n",
            })
            # Two events of different kinds.
            for kind, payload in [("TOOL_FINISHED", {"t": 1}), ("USAGE", {"tokens": 100})]:
                client.call("runtime.event", {
                    "event": {
                        "runtime_id": "r-filter",
                        "generation": 1,
                        "session_id": "sess-3",
                        "agent_id": "w1",
                        "request_id": "",
                        "run_id": "",
                        "kind": kind,
                        "created_at": "2026-08-09T00:00:00Z",
                        "payload": payload,
                    },
                })
            result = client.call("events.list", {
                "filters": ["TOOL_FINISHED"],
                "runtime_id": "r-filter",
            })
            assert all(e["kind"] == "TOOL_FINISHED" for e in result["events"])
            assert len(result["events"]) == 1
        finally:
            server.stop()


# ── adapter adapter-level (spawn with fake binary script) ──────────────


class TestSpawnWithFakeBinaryScript:
    """Use a temporary shell script as the 'opencode' binary via PATH prepend."""

    def test_spawn_via_fake_script(self, tmp_path: Path):
        """Fake opencode via mocked Popen (the conftest guard forbids real
        backend spawns, including PATH-prepended fakes — use Popen mock)."""
        import io as _io
        from unittest.mock import MagicMock as _MM

        stdout_io = _io.StringIO('{"type":"session","id":"script-sess-99"}\n')

        proc = _MM(spec=subprocess.Popen)
        proc.stdout = stdout_io
        proc.stderr = _io.StringIO()
        proc.returncode = 0

        adapter = OpenCodeRuntimeAdapter()
        with patch("subprocess.Popen", return_value=proc) as mock_popen:
            handle = adapter.spawn({
                "workdir": str(tmp_path),
                "agent_id": "script-worker",
            })
            assert handle.backend_session_id == "script-sess-99"
            assert handle.runtime == RUNTIME_OPENCODE
            assert "--agent" in mock_popen.call_args[0][0]
