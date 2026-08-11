"""Oracle CLI tests — start/ask (hot/warm/cold)/status/watch/release.

Hot/warm/cold ask paths are verified with mocked gateway + registry; the
reported method must match the ACTUAL path taken (never claim a hot
revive that did not happen).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codeagent.domain.park import Lifecycle, ParkManifest
from codeagent.oracle import cmd_oracle_ask, cmd_oracle_start, cmd_oracle_status, cmd_oracle_release
from codeagent.runtime.base import RuntimeHandle


@pytest.fixture(autouse=True)
def isolated_park(tmp_path: Path, monkeypatch):
    """Isolate park state + gateway client per test."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    yield


def _handle(runtime_id="rt-1", backend="omp", mode="interactive_plugin", backend_session_id="b1") -> RuntimeHandle:
    return RuntimeHandle(
        runtime_id=runtime_id,
        runtime=backend,
        backend_session_id=backend_session_id,
        generation=1,
        capabilities=frozenset({"stream_events", "warm_resume", "hot_resume", "in_loop_messages"}),
        supervisor="tmux",
        mode=mode,
        extra={},
    )


def _manifest(review_key="k", backend_session_id="b1", lifecycle=Lifecycle.HOT_PARKED) -> ParkManifest:
    import time

    return ParkManifest(
        review_key=review_key,
        swarm_session_id=f"ora-{review_key[:8]}-abc",
        agent_type="oracle",
        backend_session_id=backend_session_id,
        lifecycle=lifecycle,
        created_at=time.time(),
        last_activity_at=time.time(),
    )


class _NS:
    """argparse-like namespace."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


# ── start ──────────────────────────────────────────────────────────────


class TestOracleStart:
    def test_start_creates_runtime_and_park(self, tmp_path: Path, monkeypatch):
        ns = _NS(review_key="proj:oracle:gfx:blur", agent="oracle", backend="omp",
                 workdir=str(tmp_path), model="", prompt="hi")
        from codeagent.park.registry import ParkRegistry

        with patch("codeagent.oracle.RuntimeRegistry.spawn", return_value=_handle()) as spawn, \
             patch("codeagent.cli._get_swarm_kernel") as mock_kernel:
            kernel = MagicMock()
            store = MagicMock()
            store.root = tmp_path / "mb"
            mock_kernel.return_value = (kernel, store)
            code = cmd_oracle_start(ns)

        assert code == 0
        spawn.assert_called_once()
        m = ParkRegistry().lookup(ns.review_key)
        assert m is not None
        assert m.backend_session_id == "b1"

    def test_start_is_idempotent(self, tmp_path: Path):
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        registry.acquire("k1", _manifest("k1"))
        ns = _NS(review_key="k1", agent="oracle", backend="omp",
                 workdir=str(tmp_path), model="", prompt="")
        with patch("codeagent.oracle.RuntimeRegistry.spawn", return_value=_handle()), \
             patch("codeagent.cli._get_swarm_kernel") as mock_kernel:
            kernel = MagicMock()
            store = MagicMock()
            store.root = tmp_path / "mb"
            mock_kernel.return_value = (kernel, store)
            assert cmd_oracle_start(ns) == 0
        # manifest survives (not re-created with empty backend)
        m = ParkRegistry().lookup("k1")
        assert m is not None


def _raise(exc: Exception):
    def _fn(*_a, **_k):
        raise exc
    return _fn


# ── ask ────────────────────────────────────────────────────────────────


class TestOracleAsk:
    def test_ask_hot_in_loop(self, tmp_path: Path, capsys):
        """Live runtime → in-loop send, method=hot, receipt msg_id returned."""
        ns = _NS(review_key="k1", prompt="follow up", agent="oracle", backend="omp")
        info = {
            "runtime_id": "rt-1", "status": "active", "backend_session_id": "b1",
            "runtime_health": {"alive": True},
        }
        gw = MagicMock()
        gw.call.side_effect = lambda m, p=None: (
            info if m == "runtime.info" else
            {"msg_id": "m-123", "status": "delivered"} if m == "runtime.send" else {}
        )
        with patch("codeagent.oracle._gateway", return_value=gw):
            code = cmd_oracle_ask(ns)

        assert code == 0
        out = json.loads(capsys.readouterr().out)
        assert out["method"] == "hot"
        assert out["msg_id"] == "m-123"
        # send went to the live runtime with require_ack
        send_params = gw.call.call_args_list[1][0][1]
        assert send_params["runtime_id"] == "rt-1"
        assert send_params["require_ack"] is True

    def test_ask_warm_resume_native_session(self, tmp_path: Path, capsys):
        """No live runtime, but park has backend_session_id → native resume."""
        from codeagent.park.registry import ParkRegistry

        ParkRegistry().acquire("k1", _manifest("k1", backend_session_id="native-1"))
        ns = _NS(review_key="k1", prompt="resume me", agent="oracle", backend="omp")

        gw = MagicMock()
        gw.call.side_effect = _raise(
            Exception("NOT_FOUND: no runtime"))

        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle.RuntimeRegistry.spawn",
                   return_value=_handle("rt-2", backend_session_id="native-2")) as spawn:
            code = cmd_oracle_ask(ns)

        assert code == 0
        out = json.loads(capsys.readouterr().out)
        assert out["method"] == "warm"
        assert out["old_backend_session_id"] == "native-1"
        assert out["new_backend_session_id"] == "native-2"
        # native resume: backend_session_id passed to the adapter
        req = spawn.call_args[0][1]
        assert req["backend_session_id"] == "native-1"

    def test_ask_cold_snapshot(self, tmp_path: Path, capsys):
        """No park backend → cold reconstruction with snapshot context."""
        ns = _NS(review_key="k1", prompt="start fresh", agent="oracle", backend="omp")
        gw = MagicMock()
        gw.call.side_effect = _raise(
            Exception("GATEWAY_DOWN: socket not found"))

        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle.RuntimeRegistry.spawn",
                   return_value=_handle("rt-3", backend_session_id="b3")) as spawn, \
             patch("codeagent.oracle.build_cold_context",
                   return_value="snapshot ctx") as bcc:
            code = cmd_oracle_ask(ns)

        assert code == 0
        out = json.loads(capsys.readouterr().out)
        assert out["method"] == "cold"
        # P1-7: cold 上下文直接来自 build_cold_context（snapshot 注入），
        # 不经过 park_revive 的决策层 context（stale HOT_PARKED 会返回
        # 路由提示而非 snapshot 上下文）。
        bcc.assert_called_once_with("k1")
        # cold prompt = snapshot context + user prompt
        req = spawn.call_args[0][1]
        assert "snapshot ctx" in req["task"]
        assert "start fresh" in req["task"]

    def test_ask_reports_actual_method_no_fake_hot(self, tmp_path: Path, capsys):
        """Dead runtime (not alive) must NOT claim hot — falls to warm/cold."""
        from codeagent.park.registry import ParkRegistry

        ParkRegistry().acquire("k1", _manifest("k1", backend_session_id="native-1"))
        ns = _NS(review_key="k1", prompt="p", agent="oracle", backend="omp")
        info = {"runtime_id": "rt-1", "status": "stopped", "runtime_health": {"alive": False}}
        gw = MagicMock()
        gw.call.side_effect = lambda m, p=None: info if m == "runtime.info" else {}
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle.RuntimeRegistry.spawn",
                   return_value=_handle("rt-4", backend_session_id="b4")):
            code = cmd_oracle_ask(ns)
        assert code == 0
        out = json.loads(capsys.readouterr().out)
        assert out["method"] == "warm"
        assert "hot" not in out["method"]


# ── status / release ───────────────────────────────────────────────────


class TestOracleStatusRelease:
    def test_status_aggregates_park_runtime(self, tmp_path: Path, capsys):
        from codeagent.park.registry import ParkRegistry

        ParkRegistry().acquire("k1", _manifest("k1", backend_session_id="b1"))
        ns = _NS(review_key="k1")
        gw = MagicMock()
        gw.call.return_value = {
            "runtime_id": "rt-1", "status": "active", "elapsed": 42,
            "backend_session_id": "b1", "generation": 1,
            "last_event": {"tool_count": 3, "error_count": 0},
            "tool_stats": {"tool_count": 3, "error_count": 0},
            "runtime_health": {"alive": True},
        }
        with patch("codeagent.oracle._gateway", return_value=gw):
            code = cmd_oracle_status(ns)
        assert code == 0
        out = json.loads(capsys.readouterr().out)
        assert out["park"]["lifecycle"] == "hot_parked"
        assert out["runtime"]["status"] == "active"
        assert out["runtime"]["tool_stats"]["tool_count"] == 3

    def test_status_gateway_down_still_reports_park(self, tmp_path: Path, capsys):
        from codeagent.park.registry import ParkRegistry

        ParkRegistry().acquire("k1", _manifest("k1", backend_session_id="b1"))
        ns = _NS(review_key="k1")
        gw = MagicMock()
        gw.call.side_effect = Exception("gateway down")
        with patch("codeagent.oracle._gateway", return_value=gw):
            code = cmd_oracle_status(ns)
        assert code == 0
        out = json.loads(capsys.readouterr().out)
        assert out["park"]["lifecycle"] == "hot_parked"
        assert "error" in out["runtime"]

    def test_release_stops_runtime_and_releases_park(self, tmp_path: Path, capsys):
        from codeagent.park.registry import ParkRegistry

        ParkRegistry().acquire("k1", _manifest("k1", backend_session_id="b1"))
        ns = _NS(review_key="k1")
        gw = MagicMock()
        info = {"runtime_id": "rt-1"}
        gw.call.side_effect = lambda m, p=None: info if m == "runtime.info" else {}
        with patch("codeagent.oracle._gateway", return_value=gw):
            code = cmd_oracle_release(ns)
        assert code == 0
        # park released
        assert ParkRegistry().lookup("k1").lifecycle == Lifecycle.RELEASED
        # runtime.stop called
        stop_params = [c[0][1] for c in gw.call.call_args_list if c[0][0] == "runtime.stop"]
        assert stop_params and stop_params[0]["runtime_id"] == "rt-1"
        out = json.loads(capsys.readouterr().out)
        assert out["runtime_stopped"] is True
