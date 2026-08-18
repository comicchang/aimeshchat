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
from codeagent.oracle import (
    _detect_oracle_stuck,
    _is_interrupt_skip,
    _parse_iso_ts,
    cmd_oracle_ask,
    cmd_oracle_start,
    cmd_oracle_status,
    cmd_oracle_release,
)
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
        swarm_session_id=f"postmesh-{review_key[:8]}-abc",
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
                 workdir=str(tmp_path), model="m/x", prompt="hi")
        from codeagent.park.registry import ParkRegistry

        with patch("codeagent.oracle._ensure_gateway_or_hint", return_value=True), \
             patch("codeagent.oracle.RuntimeRegistry.spawn", return_value=_handle()) as spawn, \
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
                 workdir=str(tmp_path), model="m/x", prompt="")
        with patch("codeagent.oracle._ensure_gateway_or_hint", return_value=True), \
             patch("codeagent.oracle.RuntimeRegistry.spawn", return_value=_handle()), \
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
        assert out["method"] == "hot_pending_ack"
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


def test_fallback_find_session_recursive_scan(tmp_path, monkeypatch):
    """P2-11 fallback：递归扫描子目录 .jsonl（主会话）+ 精确 tail 匹配；__advisor 排除。"""
    from codeagent.oracle import _fallback_find_session_for_key

    real_root = tmp_path / ".omp" / "agent" / "sessions"
    real_root.mkdir(parents=True)
    (real_root / "some-project").mkdir()
    (real_root / "some-project" / "unrelated.jsonl").write_text('oracle 历史')
    sd = real_root / "some-project" / "sdir"
    sd.mkdir()
    # 主会话文件（应被选中）
    (sd / "main.jsonl").write_text('r4-closure 2 answer')
    # __advisor 监控会话（含 tail 但必须被排除——根因2修复）
    (sd / "__advisor.jsonl").write_text('r4-closure advisor meta-comment')

    monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)

    found = _fallback_find_session_for_key("proj:oracle:review:r4-closure")
    assert found is not None
    assert "r4-closure" in found.read_text(errors="replace")
    # 根因2修复：__advisor（独立监控会话）不得被选为主回答来源
    assert "__advisor" not in found.name, "__advisor 会话不应被选作主回答"


def test_release_soft_preserves_session_and_manifest(tmp_path):
    """release（默认 soft）保留 manifest 行 + backend_session_id，lifecycle=RELEASED_SOFT。"""
    from codeagent.park.registry import ParkRegistry
    from codeagent.domain.park import ParkManifest, Lifecycle
    registry = ParkRegistry()
    m = ParkManifest(review_key="k-soft", swarm_session_id="s-soft",
                     backend_session_id="sid-1", lifecycle=Lifecycle.HOT_PARKED)
    registry.acquire("k-soft", m)
    registry.release("k-soft")  # 默认 soft
    got = registry.lookup("k-soft")
    assert got is not None, "soft release 应保留行"
    assert got.lifecycle == Lifecycle.RELEASED_SOFT
    assert got.backend_session_id == "sid-1"


def test_release_purge_deletes_manifest(tmp_path):
    """release --purge（hard）删 park 行。"""
    from codeagent.park.registry import ParkRegistry
    from codeagent.domain.park import ParkManifest, Lifecycle
    registry = ParkRegistry()
    registry.acquire("k-hard", ParkManifest(
        review_key="k-hard", swarm_session_id="s-hard", lifecycle=Lifecycle.HOT_PARKED))
    registry.release("k-hard", mode="hard")
    assert registry.lookup("k-hard") is None, "hard release 应删行"


def test_revive_warm_from_released_soft(tmp_path):
    """RELEASED_SOFT + backend_session_id → revive 路由 warm。"""
    from codeagent.park.registry import ParkRegistry
    from codeagent.domain.park import ParkManifest, Lifecycle
    from codeagent.park.router import revive_or_spawn
    registry = ParkRegistry()
    registry.acquire("k-revive", ParkManifest(
        review_key="k-revive", swarm_session_id="s-revive",
        backend_session_id="sid-2", lifecycle=Lifecycle.HOT_PARKED))
    registry.release("k-revive")  # → RELEASED_SOFT，保留 sid-2
    rv = revive_or_spawn("k-revive")
    assert rv.method == "warm", f"RELEASED_SOFT+sid 应 warm，got {rv.method}"


def test_attach_routes_to_revive_when_released(tmp_path):
    """attach 对 RELEASED_SOFT 走 revive（而非 ask）。"""
    from codeagent.park.registry import ParkRegistry
    from codeagent.domain.park import ParkManifest, Lifecycle
    from codeagent.oracle import cmd_oracle_attach
    from argparse import Namespace
    registry = ParkRegistry()
    registry.acquire("k-attach", ParkManifest(
        review_key="k-attach", swarm_session_id="s-attach",
        backend_session_id="sid-a", lifecycle=Lifecycle.HOT_PARKED))
    registry.release("k-attach")  # → RELEASED_SOFT
    ns = Namespace(review_key="k-attach", mode="bg", prompt="")
    with patch("codeagent.oracle._ensure_gateway_or_hint", return_value=True):
        rc = cmd_oracle_attach(ns)
    assert rc == 0
    got = registry.lookup("k-attach")
    assert got is not None and got.lifecycle == Lifecycle.HOT_PARKED, "attach 应 revive 回 HOT_PARKED"


# ── model resolve ──────────────────────────────────────────────────────


def test_resolve_oracle_model_chain_mapping(monkeypatch):
    """M-model: agent profile model: 为唯一权威源；显式 model 优先；
    profile 缺失 → 空列表；空 agent 归一为 oracle。"""
    from codeagent.oracle import _resolve_oracle_model_chain

    profiles = {"oracle": "prof/gpt-5.6-sol",
                "oracle-opus": "prof/claude-opus",
                "oracle-lite": "prof/v4-pro"}

    # 1) profile 存在 → profile model 为单一权威源
    with patch("codeagent.oracle._read_agent_model", side_effect=lambda t: profiles.get(t, "")):
        assert _resolve_oracle_model_chain("oracle", "") == ["prof/gpt-5.6-sol"]
        assert _resolve_oracle_model_chain("oracle-opus", "") == ["prof/claude-opus"]
        assert _resolve_oracle_model_chain("oracle-lite", "") == ["prof/v4-pro"]
    # 2) 显式 model 永远优先（不被 profile 覆盖）
    with patch("codeagent.oracle._read_agent_model", return_value="prof/gpt-5.6-sol"):
        assert _resolve_oracle_model_chain("oracle", "explicit/model-x") == ["explicit/model-x"]
    # 3) profile 缺失 → 空列表（不再回退 config chain）
    with patch("codeagent.oracle._read_agent_model", return_value=""):
        assert _resolve_oracle_model_chain("oracle", "") == []
        assert _resolve_oracle_model_chain("oracle-opus", "") == []
        assert _resolve_oracle_model_chain("oracle-lite", "") == []
    # 4) 空/未显式 agent → 明确默认 oracle（不静默回落 default 链）
    with patch("codeagent.oracle._read_agent_model", return_value="prof/gpt-5.6-sol"):
        assert _resolve_oracle_model_chain("", "") == ["prof/gpt-5.6-sol"]
        assert _resolve_oracle_model_chain("default", "") == ["prof/gpt-5.6-sol"]


def test_resolve_oracle_model_chain_no_config(monkeypatch):
    """profile 缺失 + 无配置 → 空列表（不静默降级）。"""
    from codeagent.oracle import _resolve_oracle_model_chain

    with patch("codeagent.oracle._read_agent_model", return_value=""):
        assert _resolve_oracle_model_chain("oracle", "") == []
        assert _resolve_oracle_model_chain("", "") == []
        assert _resolve_oracle_model_chain("oracle-lite", "") == []


def test_model_chain_from_manifest_prefers_persisted():
    """M-model: revive/ask 从 manifest 读已落盘模型，不再重推导。"""
    from dataclasses import replace

    from codeagent.oracle import _model_chain_from_manifest

    m = _manifest("k1")
    m = replace(m, model="explicit/x", primary_model="prof/gpt-5.6-sol")
    # 1) manifest.model（start 显式 --model 持久化）优先于 primary_model
    assert _model_chain_from_manifest("oracle", m) == ["explicit/x"]
    # 2) primary_model（start 落盘的 chain[0]）→ 直接读，不重推导
    m2 = replace(m, model="")
    assert _model_chain_from_manifest("oracle", m2) == ["prof/gpt-5.6-sol"]
    # 3) 调用方本次 --model（explicit_override）优先
    assert _model_chain_from_manifest("oracle", m2, explicit_override="ask/y") == ["ask/y"]
    # 4) 旧 manifest 无 primary_model → 迁移解析一次（不写回）
    m3 = replace(m2, primary_model="")
    with patch("codeagent.oracle._resolve_oracle_model_chain",
               return_value=["migrated/gpt-5.6-sol"]) as resolve:
        assert _model_chain_from_manifest("oracle-lite", m3) == ["migrated/gpt-5.6-sol"]
        resolve.assert_called_once_with("oracle-lite", "")
    # 5) 无 manifest（ask 冷启动无实例）→ 现场解析
    with patch("codeagent.oracle._resolve_oracle_model_chain",
               return_value=["fresh/gpt-5.6-sol"]) as resolve:
        assert _model_chain_from_manifest("oracle", None) == ["fresh/gpt-5.6-sol"]
        resolve.assert_called_once_with("oracle", "")


def test_ask_cold_uses_model_chain(tmp_path, capsys, monkeypatch):
    """ask cold 分支补模型链：spawn model=primary，env 注入链，输出含 model_chain。

    M-model: agent profile model: 为唯一权威源（见 test_model_chain_from_manifest_*）。
    """
    from codeagent.park.registry import ParkRegistry

    # profile 存在 → agent profile model 为单一权威源
    monkeypatch.setattr("codeagent.oracle._read_agent_model",
                        lambda t: "ask-prof/v4-pro" if t == "oracle-lite" else "")

    ns = _NS(review_key="k1", prompt="cold ask", agent="oracle-lite", backend="omp")
    gw = MagicMock()
    gw.call.side_effect = _raise(Exception("GATEWAY_DOWN: socket not found"))
    with patch("codeagent.oracle._gateway", return_value=gw), \
         patch("codeagent.oracle.RuntimeRegistry.spawn",
               return_value=_handle("rt-c", backend_session_id="bc")) as spawn, \
         patch("codeagent.oracle.build_cold_context", return_value="snapshot"):
        code = cmd_oracle_ask(ns)

    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["method"] == "cold"
    assert out["model_chain"] == ["ask-prof/v4-pro"]
    req = spawn.call_args[0][1]
    assert req["model"] == "ask-prof/v4-pro"
    assert req["env"]["OMP_MODEL_FALLBACK_CHAIN"] == "ask-prof/v4-pro"


def test_ask_hot_surfaces_quota_warning(tmp_path, capsys):
    """运行时死于 quota → ask 显式告警（insufficient_quota + degrade_hint），
    而非静默降级成 transport 超时。"""
    from codeagent.park.registry import ParkRegistry

    ParkRegistry().acquire("k1", _manifest("k1", backend_session_id="native-1"))
    ns = _NS(review_key="k1", prompt="p", agent="oracle", backend="omp")
    info = {
        "runtime_id": "rt-1", "status": "offline",
        "runtime_health": {"alive": False, "quota_error": "insufficient_quota for model X"},
    }
    gw = MagicMock()
    gw.call.side_effect = lambda m, p=None: info if m == "runtime.info" else {}
    with patch("codeagent.oracle._gateway", return_value=gw), \
         patch("codeagent.oracle.RuntimeRegistry.spawn",
               return_value=_handle("rt-4", backend_session_id="b4")):
        code = cmd_oracle_ask(ns)
    assert code == 0
    err = capsys.readouterr().err
    assert "insufficient_quota" in err
    assert "oracle-lite" in err


def test_looks_like_quota():
    from codeagent.oracle import _looks_like_quota

    assert _looks_like_quota("insufficient_quota on model X")
    assert _looks_like_quota("quota exceeded (402)")
    assert _looks_like_quota("rate limit reached")
    assert not _looks_like_quota("transport timed out after 30s")
    assert not _looks_like_quota("connection refused")


def test_fallback_session_scoring_avoids_short_tail_mismatch(tmp_path, monkeypatch):
    """短 tail（< 5 字符）不再误匹配旧会话；full key 命中优先。"""
    from codeagent.oracle import _fallback_find_session_for_key

    sessions = tmp_path / "sessions"
    (sessions / "old-proj").mkdir(parents=True)
    (sessions / "postmesh-new").mkdir()

    unrelated = sessions / "old-proj" / "old.jsonl"
    unrelated.write_text(
        '{"type":"message","message":{"role":"assistant","content":'
        '[{"type":"text","text":"blur is a gaussian filter"}]}}\n',
        encoding="utf-8",
    )
    correct = sessions / "postmesh-new" / "session.jsonl"
    correct.write_text(
        '{"type":"message","message":{"role":"assistant","content":'
        '[{"type":"text","text":"proj:oracle:gfx:blur discussion here"}]}}\n',
        encoding="utf-8",
    )
    # unrelated is newest — naive first-hit would pick it
    import os as _os
    _os.utime(unrelated, (1, 1))
    _os.utime(correct, (2, 2))

    monkeypatch.setattr("codeagent.oracle.Path.home",
                        lambda: tmp_path)
    monkeypatch.setenv("OMP_CONFIG", "")
    # 旧实现用 sessions_root = ~/.omp/agent/sessions，这里 monkeypatch home
    # 后路径不对——直接测核心逻辑：通过 _find_session_file 不可行，改用
    # 对 sessions_root 的间接依赖：把 sessions 目录放到 home 下
    (tmp_path / ".omp" / "agent").mkdir(parents=True, exist_ok=True)
    import shutil as _sh
    if (tmp_path / ".omp" / "agent" / "sessions").exists():
        _sh.rmtree(tmp_path / ".omp" / "agent" / "sessions")
    _sh.copytree(sessions, tmp_path / ".omp" / "agent" / "sessions")

    found = _fallback_find_session_for_key("proj:oracle:gfx:blur")
    expected = tmp_path / ".omp" / "agent" / "sessions" / "postmesh-new" / "session.jsonl"
    assert found is not None
    assert found == expected, "full-key match（postmesh-* 目录）必须胜过短 tail 误匹配"


# ── B1: CLI 强制显式 ExecutionSpec（去 role）──────────────────────────


def test_cli_warns_deprecated_agent(capsys):
    """传 --agent → 弃用告警引导 --model/--variant；未传则不告警。"""
    from codeagent.cli import _warn_deprecated_agent

    _warn_deprecated_agent(_NS(agent="oracle"))
    err = capsys.readouterr().err
    assert "--agent" in err and "--model" in err and "--variant" in err
    _warn_deprecated_agent(_NS(agent=""))
    assert capsys.readouterr().err == ""



def test_cmd_oracle_start_explicit_model_dispatches(capsys, tmp_path):
    """oracle start 带显式 --model（无 --agent）→ 放行并派发 handler。"""
    from codeagent.cli import _cmd_oracle

    ns = _NS(ora_cmd="start", review_key="k-b1", agent="", model="m/x",
             backend="omp", workdir=str(tmp_path), prompt="")
    with patch("codeagent.oracle.cmd_oracle_start", return_value=0) as handler:
        assert _cmd_oracle(ns) == 0
        handler.assert_called_once_with(ns)
    assert capsys.readouterr().err == ""


def test_cmd_oracle_ask_deprecation_warning_still_dispatches(capsys):
    """ask 传 --agent → 打弃用告警但仍派发（向后兼容，不破坏现有调用）。"""
    from codeagent.cli import _cmd_oracle

    ns = _NS(ora_cmd="ask", review_key="k1", prompt="p", agent="oracle",
             backend="omp", model="")
    with patch("codeagent.oracle.cmd_oracle_ask", return_value=0) as handler:
        assert _cmd_oracle(ns) == 0
        handler.assert_called_once_with(ns)
    err = capsys.readouterr().err
    assert "--agent" in err and "--model" in err


def test_start_explicit_model_no_agent(tmp_path):
    """B1: 显式 --model（无 --agent）→ model_source=explicit，manifest 落盘 primary_model。"""
    from codeagent.park.registry import ParkRegistry

    ns = _NS(review_key="k-exp", agent="", backend="omp", workdir=str(tmp_path),
             model="explicit/gpt-x", variant="thinking", system="", prompt="hi")
    with patch("codeagent.oracle._ensure_gateway_or_hint", return_value=True), \
         patch("codeagent.oracle.RuntimeRegistry.spawn", return_value=_handle()) as spawn, \
         patch("codeagent.cli._get_swarm_kernel") as mock_kernel:
        kernel = MagicMock()
        store = MagicMock()
        store.root = tmp_path / "mb"
        mock_kernel.return_value = (kernel, store)
        assert cmd_oracle_start(ns) == 0

    m = ParkRegistry().lookup("k-exp")
    assert m is not None
    assert m.model == "explicit/gpt-x"          # 显式 --model 持久化
    assert m.primary_model == "explicit/gpt-x"  # chain[0] = 显式模型（不被 profile 覆盖）
    assert m.variant == "thinking"
    req = spawn.call_args[0][1]
    assert req["model"] == "explicit/gpt-x"
    assert req["variant"] == "thinking"



# ── 卡死/停滞检测（_detect_oracle_stuck 操作层防御）─────────────────────
# 强信号：同 generation 连续 ≥3 个 interrupt_skipped 且期间无进度事件；
# 弱信号：非终态 + runtime alive + ≥15min 无 work 事件 + 无 in-flight tool。
# 只告警不自动 recover —— 返回 {"detected": True, "signal": ...} 或 None。


def _stuck_jsonl(tmp_path: Path, events: list[tuple[str, str]], name: str = "ses.jsonl") -> Path:
    """写一条 OMP 会话 JSONL：events = [(kind, iso_ts)]，kind ∈ skip/tool_ok/output。"""
    path = tmp_path / name
    lines = []
    for kind, ts in events:
        if kind == "skip":
            msg = {"role": "toolResult", "content": [
                {"type": "text", "text": "Skipped due to pending system advisory"},
            ]}
        elif kind == "tool_ok":
            msg = {"role": "toolResult", "content": [
                {"type": "text", "text": "command output: ok"},
            ]}
        else:  # output
            msg = {"role": "assistant", "content": [
                {"type": "text", "text": "working on it..."},
            ]}
        lines.append(json.dumps({"type": "message", "timestamp": ts, "message": msg}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _stuck_info(runtime_id: str = "rt-1", generation: int = 1,
                agent_state: str = "agent_running", status: str = "active",
                alive: bool = True, backend_session_id: str = "",
                last_kind: str = "TOOL_FINISHED", last_payload: dict | None = None,
                first_seen_at: str = "2026-08-13T00:00:00Z") -> dict:
    """构造 _detect_oracle_stuck 的 info 参数（默认工作态、无卡死信号）。"""
    return {
        "runtime_id": runtime_id,
        "generation": generation,
        "agent_state": agent_state,
        "status": status,
        "runtime_health": {"alive": alive},
        "backend_session_id": backend_session_id,
        "last_event": {
            "last_event_kind": last_kind,
            "last_event_payload": last_payload or {},
            "first_seen_at": first_seen_at,
        },
    }


def _iso(delta_s: float) -> str:
    """now + delta_s 秒的 ISO-8601 Z 时间戳（弱信号窗口计算用）。"""
    from datetime import datetime, timezone, timedelta

    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


class TestDetectOracleStuck:
    """_detect_oracle_stuck：强信号（连续 skip ≥3）/ 弱信号（≥15min 无 work）。"""

    def test_strong_signal_three_consecutive_skips(self, tmp_path, monkeypatch):
        """连续 3 个 interrupt_skipped（最新事件向前数）→ 强信号。"""
        session = _stuck_jsonl(tmp_path, [
            ("skip", _iso(-100)), ("skip", _iso(-80)), ("skip", _iso(-60)),
        ])
        monkeypatch.setattr("codeagent.oracle._find_session_file", lambda _sid, session_dir='': session)
        info = _stuck_info(backend_session_id="b-strong")
        out = _detect_oracle_stuck("rk-strong", info, session_dir="")
        assert out is not None
        assert out["detected"] is True
        assert out["signal"] == "strong"
        assert "interrupt_skipped" in out["detail"]
        assert "3" in out["detail"]

    def test_strong_signal_more_than_three(self, tmp_path, monkeypatch):
        """连续 5 个 skip（含更早的中间进度被后续 skip 覆盖）→ 仍强信号。"""
        session = _stuck_jsonl(tmp_path, [
            ("output", _iso(-500)), ("skip", _iso(-100)), ("skip", _iso(-80)),
            ("skip", _iso(-60)), ("skip", _iso(-40)), ("skip", _iso(-20)),
        ])
        monkeypatch.setattr("codeagent.oracle._find_session_file", lambda _sid, session_dir='': session)
        out = _detect_oracle_stuck("rk-multi", _stuck_info(backend_session_id="b-multi"), session_dir="")
        assert out is not None and out["signal"] == "strong"

    def test_two_skips_not_enough(self, tmp_path, monkeypatch):
        """仅 2 个连续 skip → 不达阈值（<3）→ 无强信号。"""
        session = _stuck_jsonl(tmp_path, [
            ("skip", _iso(-100)), ("skip", _iso(-60)),
        ])
        monkeypatch.setattr("codeagent.oracle._find_session_file", lambda _sid, session_dir='': session)
        out = _detect_oracle_stuck("rk-two", _stuck_info(backend_session_id="b-two"), session_dir="")
        assert out is None

    def test_progress_event_breaks_skip_run(self, tmp_path, monkeypatch):
        """最新事件是进度（assistant 文本）→ 连续段断掉 → 无强信号。"""
        session = _stuck_jsonl(tmp_path, [
            ("skip", _iso(-200)), ("skip", _iso(-160)), ("skip", _iso(-120)),
            ("output", _iso(-60)),
        ])
        monkeypatch.setattr("codeagent.oracle._find_session_file", lambda _sid, session_dir='': session)
        out = _detect_oracle_stuck("rk-brk", _stuck_info(backend_session_id="b-brk"), session_dir="")
        assert out is None, "进度事件应打断连续 skip 段"

    def test_weak_signal_stall_over_15min(self, tmp_path, monkeypatch):
        """alive + 非终态 + 最新 work 事件 ≥15min 前 + 无 in-flight → 弱信号。"""
        newest_work = _iso(-20 * 60)  # 20 分钟前
        mock_gw = MagicMock()
        mock_gw.call.return_value = {"newest": {"TOOL_FINISHED": newest_work}}
        monkeypatch.setattr("codeagent.oracle._gateway", lambda: mock_gw)
        info = _stuck_info(last_kind="TOOL_FINISHED")  # 非 TOOL_STARTED → 无 in-flight
        out = _detect_oracle_stuck("rk-weak", info)
        assert out is not None
        assert out["detected"] is True
        assert out["signal"] == "weak"
        assert "20 分钟" in out["detail"]

    def test_recent_work_no_stall(self, tmp_path, monkeypatch):
        """1 分钟内有 work 事件 → 无弱信号。"""
        mock_gw = MagicMock()
        mock_gw.call.return_value = {"newest": {"ASSISTANT_PROGRESS": _iso(-60)}}
        monkeypatch.setattr("codeagent.oracle._gateway", lambda: mock_gw)
        out = _detect_oracle_stuck("rk-fresh", _stuck_info())
        assert out is None

    def test_in_flight_tool_suppresses_weak(self, tmp_path, monkeypatch):
        """最新事件是 TOOL_STARTED（in-flight tool）→ 即使久无 work 也不告警。"""
        mock_gw = MagicMock()
        mock_gw.call.return_value = {"newest": {"TOOL_STARTED": _iso(-30 * 60)}}
        monkeypatch.setattr("codeagent.oracle._gateway", lambda: mock_gw)
        info = _stuck_info(last_kind="TOOL_STARTED")
        out = _detect_oracle_stuck("rk-inflight", info)
        assert out is None, "in-flight tool 不算停滞"

    def test_terminal_state_no_weak_signal(self, tmp_path, monkeypatch):
        """agent_state 非 running（等 ask 的正常态）→ 终态不告警。"""
        mock_gw = MagicMock()
        mock_gw.call.return_value = {"newest": {"TOOL_FINISHED": _iso(-60 * 60)}}
        monkeypatch.setattr("codeagent.oracle._gateway", lambda: mock_gw)
        info = _stuck_info(agent_state="idle", last_kind="TOOL_FINISHED")
        out = _detect_oracle_stuck("rk-idle", info)
        assert out is None

    def test_stopped_runtime_no_weak_signal(self, tmp_path, monkeypatch):
        """status=stopped → 终态：弱信号不触发（停滞只告警活着的 runtime）。"""
        mock_gw = MagicMock()
        mock_gw.call.return_value = {"newest": {"TOOL_FINISHED": _iso(-60 * 60)}}
        monkeypatch.setattr("codeagent.oracle._gateway", lambda: mock_gw)
        info = _stuck_info(status="stopped", last_kind="TOOL_FINISHED")
        out = _detect_oracle_stuck("rk-stop", info)
        assert out is None

    def test_missing_runtime_or_generation_no_signal(self):
        """缺 runtime_id 或 generation → 直接 None（无法界定代际窗口）。"""
        assert _detect_oracle_stuck("rk-none", {"runtime_id": "rt-1"}) is None
        assert _detect_oracle_stuck("rk-none", {"generation": 1}) is None

    def test_dead_runtime_no_weak_signal(self, tmp_path, monkeypatch):
        """runtime 不 alive → 弱信号不触发（停滞仅针对活着的 runtime）。"""
        mock_gw = MagicMock()
        mock_gw.call.return_value = {"newest": {"TOOL_FINISHED": _iso(-30 * 60)}}
        monkeypatch.setattr("codeagent.oracle._gateway", lambda: mock_gw)
        info = _stuck_info(alive=False, last_kind="TOOL_FINISHED")
        assert _detect_oracle_stuck("rk-dead", info) is None


# ── 卡死检测辅助函数（_is_interrupt_skip / _parse_iso_ts / 事件扫描）────

class TestStuckHelpers:
    """卡死检测底层原语：skip 载荷识别 / 时间戳解析 / JSONL 事件扫描。"""

    def test_interrupt_skip_fixed_marker(self):
        """OMP createSkippedToolResult 固定文案 → skip。"""
        assert _is_interrupt_skip("Skipped due to pending system advisory") is True
        assert _is_interrupt_skip("  Skipped due to pending system advisory\n") is True

    def test_interrupt_skip_json_payload(self):
        """JSON 载荷（__synthetic/__interrupted + source）→ skip。"""
        assert _is_interrupt_skip(
            '{"__synthetic": true, "source": "interrupt_skipped"}'
        ) is True
        assert _is_interrupt_skip(
            '{"__interrupted": true, "source": "interrupt_skipped"}'
        ) is True

    def test_interrupt_skip_rejects_non_payload(self):
        """非载荷文本（含 marker 但不以其开头 / 无 source 字段）→ 非 skip。"""
        assert _is_interrupt_skip("grep output: Skipped due to pending system advisory") is False
        assert _is_interrupt_skip('{"__synthetic": true}') is False
        assert _is_interrupt_skip("normal tool result") is False
        assert _is_interrupt_skip("") is False

    def test_parse_iso_ts_both_precisions(self):
        """gateway 秒级（无毫秒）与 OMP 毫秒级时间戳统一解析为 epoch。"""
        import time as _t

        s = _parse_iso_ts("2026-08-13T00:00:00Z")
        ms = _parse_iso_ts("2026-08-13T00:00:00.548Z")
        assert s is not None and ms is not None
        assert abs(ms - s - 0.548) < 0.001
        assert _parse_iso_ts("") is None
        assert _parse_iso_ts("not-a-date") is None
        assert _parse_iso_ts(None) is None

    def test_scan_session_stuck_events_classification(self, tmp_path):
        """事件扫描：skip/tool_ok/output 分类 + 时间序 + since 窗口过滤 + 脏行容忍。"""
        from codeagent.oracle import _scan_session_stuck_events

        session = _stuck_jsonl(tmp_path, [
            ("skip", "2026-08-13T00:00:02Z"),
            ("tool_ok", "2026-08-13T00:00:01Z"),
            ("output", "2026-08-13T00:00:03Z"),
            ("skip", "2026-08-13T00:00:00Z"),  # 早于 since → 窗口外
        ])
        # 追加脏行：空行 / 非法 JSON / 非 message 类型 —— 扫描须跳过不崩溃
        session.write_text(session.read_text(encoding="utf-8") + "\n\n{not-json\n", encoding="utf-8")
        with session.open("a", encoding="utf-8") as f:
            f.write('{"type": "event", "timestamp": "2026-08-13T00:00:04Z"}\n')
        since = _parse_iso_ts("2026-08-13T00:00:00.500Z")
        events = _scan_session_stuck_events(session, since)
        # 返回的 ts 已是 epoch 浮点（内部已解析）；窗口过滤掉最早的 skip，
        # 脏行被跳过，剩余按时间序排列
        assert [(k, round(ts, 3)) for k, ts in events] == [
            ("tool_ok", _parse_iso_ts("2026-08-13T00:00:01Z")),
            ("skip", _parse_iso_ts("2026-08-13T00:00:02Z")),
            ("output", _parse_iso_ts("2026-08-13T00:00:03Z")),
        ]

    def test_scan_session_stuck_events_missing_file(self, tmp_path):
        """会话文件不存在/不可读 → 返回 []（不抛异常，检测静默跳过）。"""
        from codeagent.oracle import _scan_session_stuck_events

        assert _scan_session_stuck_events(tmp_path / "no-such.jsonl", 0.0) == []


class TestReadTailLines:
    """_read_tail_lines：尾部 N 行读取（大文件截断行丢弃 + 缺失文件容错）。"""

    def test_returns_last_n_lines_in_order(self, tmp_path):
        """常规文件：返回最后 n 行，保持原顺序。"""
        from codeagent.oracle import _read_tail_lines

        p = tmp_path / "tail.jsonl"
        p.write_text("\n".join(f"line-{i}" for i in range(10)) + "\n", encoding="utf-8")
        assert _read_tail_lines(p, 3) == ["line-7", "line-8", "line-9"]
        assert _read_tail_lines(p, 200) == [f"line-{i}" for i in range(10)]

    def test_big_file_drops_truncated_first_line(self, tmp_path):
        """超过读取块的文件：首行被截断 → 丢弃后返回最后 n 行（内容完整）。"""
        from codeagent.oracle import _read_tail_lines

        p = tmp_path / "big.jsonl"
        # 每行 ~300B，共 8 行（~2.4KB）> n*1KB（n=2 → 2KB）且行数 > n
        lines = [json.dumps({"type": "message", "timestamp": "2026-08-13T00:00:00Z",
                             "message": {"role": "toolResult", "content": [
                                 {"type": "text", "text": "x" * 280}]}})
                 for _ in range(8)]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        got = _read_tail_lines(p, 2)
        assert len(got) == 2
        # 尾部两行必须是完整 JSON（首行截断被丢弃）
        for line in got:
            assert json.loads(line)["type"] == "message"

    def test_missing_file_returns_empty(self, tmp_path):
        """文件不存在 → []（不抛异常）。"""
        from codeagent.oracle import _read_tail_lines

        assert _read_tail_lines(tmp_path / "absent.jsonl", 10) == []


class TestSidConsistency:
    """Bug 2 回归：确定性 sid——bootstrap/start/冷路径必须用同一 sid，消除 session 碎片。"""

    def test_review_sid_is_deterministic_and_collision_safe(self):
        from codeagent.oracle import _review_sid

        rk = "mi-docs:oracle:review:gateway-sid-bugs"
        s1 = _review_sid(rk)
        s2 = _review_sid(rk)
        # 同一 review_key 每次相同（确定性）
        assert s1 == s2
        # postmesh-{sha256[:16]} 格式
        assert s1.startswith("postmesh-")
        assert len(s1) == len("postmesh-") + 16
        # 不同 review_key 不同 sid（截断碰撞安全）
        assert s1 != _review_sid("other:review:key")

    def test_bootstrap_uses_review_sid_not_random_run_id(self, tmp_path, monkeypatch):
        """_bootstrap_oracle_swarm 必须用确定性 _review_sid，而非每次 uuid4 的 run_id。"""
        import codeagent.cli as cli_mod
        from codeagent.oracle import _review_sid

        created = {}

        class FakeKernel:
            def __init__(self, *_a, **_k): pass
            def create_session(self, session_id, *a, **k):
                created["sid"] = session_id
            def register(self, *a, **k): pass
            def direct(self, *a, **k): pass

        monkeypatch.setattr(cli_mod, "_get_swarm_kernel", lambda **k: (FakeKernel(), object()))
        monkeypatch.setattr(cli_mod, "resolve_root", lambda: tmp_path / "root")

        req = MagicMock()
        req.review_key = "mi-docs:oracle:review:gateway-sid-bugs"
        req.request_id = "req1"
        req.session_key = None
        req.host = None
        req.workdir = str(tmp_path)

        # RunContext 需要的基础字段
        from codeagent.cli import RunContext
        from types import SimpleNamespace

        with patch.object(cli_mod, "RunContext", lambda **k: SimpleNamespace(**k)):
            cli_mod._bootstrap_oracle_swarm(req, "mi-docs:oracle:review:gateway-sid-bugs")

        # bootstrap 创建的 session 必须 = 确定性 _review_sid（与 start/冷路径一致）
        assert created.get("sid") == _review_sid("mi-docs:oracle:review:gateway-sid-bugs")
        # 绝不能是 ora-{key}-{random} 形态
        assert not created.get("sid", "").startswith("ora-")


class TestSupervisorGeneration:
    """Bug 1 回归：supervisor 重启后必须递增 generation + 轮换 nonce，
    否则 gateway A7 检查（同 generation 必须同 owner_pid+nonce）拒绝注册。"""

    def _write_spec(self, runtime_dir, runtime_id="rt-g1", generation=1, nonce="oldnonce"):
        from codeagent.runtime.supervisor import RuntimeSpec
        spec = RuntimeSpec(
            runtime_id=runtime_id,
            session_id="postmesh-abc",
            agent_id="oracle",
            runtime="omp",
            review_key="mi-docs:oracle:review:gateway-sid-bugs",
            generation=generation,
            gateway_socket="/tmp/fake.sock",
            nonce=nonce,
            workdir="/tmp",
            mode="interactive_plugin",
            spec_path=str(runtime_dir / "spec.json"),
        )
        p = runtime_dir / "spec.json"
        p.write_text(json.dumps(spec.to_dict()), encoding="utf-8")
        return spec

    def _run_supervisor(self, spec_path, monkeypatch):
        """跑 supervisor.main 到 identity 写入点（mock Popen 防真启动）。"""
        import codeagent.runtime.supervisor as sup_mod

        # mock Popen：不真启动 agent 进程
        fake_proc = MagicMock()
        monkeypatch.setattr(sup_mod.subprocess, "Popen", lambda *a, **k: fake_proc)
        # mock _report（gateway socket 不存在）
        monkeypatch.setattr(sup_mod, "_report", lambda *a, **k: None)
        # 返回前 kill 掉 Popen 后返回码 0
        fake_proc.wait.return_value = 0
        fake_proc.returncode = 0

        import codeagent.runtime.supervisor as m
        return m.main([str(spec_path)])

    def test_restart_bumps_generation_when_old_owner_dead(self, tmp_path, monkeypatch):
        """旧 identity.json 的 owner_pid 已死 → generation+1 + nonce 轮换。"""
        from codeagent.runtime import supervisor as sup_mod
        from codeagent.runtime.supervisor import _runtime_dir

        runtime_id = "rt-restart-1"
        d = _runtime_dir(runtime_id)
        d.mkdir(parents=True, exist_ok=True)

        # 写一个"旧进程已死"的 identity（owner_pid=999999 不存在）
        dead_pid = 999999  # 不可能存在的 pid
        (d / "identity.json").write_text(json.dumps({
            "runtime_id": runtime_id, "generation": 1,
            "owner_pid": dead_pid, "nonce": "oldnonce123456",
        }), encoding="utf-8")

        spec = self._write_spec(d, runtime_id=runtime_id, generation=1, nonce="oldnonce123456")
        # 预写旧 spec 目录下的 identity 由 spec_path 定位

        rc = self._run_supervisor(d / "spec.json", monkeypatch)
        # 跑完应生成新 identity
        new_identity = json.loads((d / "identity.json").read_text(encoding="utf-8"))
        assert rc == 0
        # generation 递增
        assert new_identity["generation"] == 2
        # nonce 轮换（≠ 旧值）
        assert new_identity["nonce"] != "oldnonce123456"

    def test_same_generation_keeps_identity_when_no_restart(self, tmp_path, monkeypatch):
        """无旧 identity（首次启动）→ 保持 spec.generation + spec.nonce。"""
        from codeagent.runtime.supervisor import _runtime_dir

        runtime_id = "rt-fresh-1"
        d = _runtime_dir(runtime_id)
        d.mkdir(parents=True, exist_ok=True)

        spec = self._write_spec(d, runtime_id=runtime_id, generation=3, nonce="keptnonce")
        rc = self._run_supervisor(d / "spec.json", monkeypatch)
        new_identity = json.loads((d / "identity.json").read_text(encoding="utf-8"))
        assert rc == 0
        assert new_identity["generation"] == 3
        assert new_identity["nonce"] == "keptnonce"


# ── uncovered paths: pure helpers ─────────────────────────────────────


class TestOracleUncoveredPaths:
    """Coverage expansion for src/codeagent/oracle.py (pure helpers first)."""

    # ── _ensure_gateway_or_hint ──────────────────────────────────────

    def test_ensure_gateway_hint_fast_path(self, capsys):
        from codeagent.oracle import _ensure_gateway_or_hint

        gw = MagicMock()
        gw.call.return_value = {"capabilities": []}
        with patch("codeagent.oracle.GatewayClient", return_value=gw), \
             patch("codeagent.oracle.subprocess.run") as m_run:
            assert _ensure_gateway_or_hint() is True
        m_run.assert_not_called()

    def test_ensure_gateway_hint_autostart_success(self, capsys):
        from codeagent.oracle import _ensure_gateway_or_hint

        gw = MagicMock()
        gw.call.side_effect = [Exception("socket not found"), {"capabilities": []}]
        with patch("codeagent.oracle.GatewayClient", return_value=gw), \
             patch("codeagent.oracle.subprocess.run") as m_run, \
             patch("codeagent.oracle.time.sleep"):
            m_run.return_value.returncode = 0
            assert _ensure_gateway_or_hint() is True
        m_run.assert_called_once()
        argv = m_run.call_args[0][0]
        assert argv[1:] == ["-m", "codeagent.gateway.cli", "start"]
        assert capsys.readouterr().err == ""

    def test_ensure_gateway_hint_autostart_fail(self, capsys):
        from codeagent.oracle import _ensure_gateway_or_hint

        gw = MagicMock()
        gw.call.side_effect = Exception("down")
        with patch("codeagent.oracle.GatewayClient", return_value=gw), \
             patch("codeagent.oracle.subprocess.run") as m_run, \
             patch("codeagent.oracle.time.sleep"):
            m_run.return_value.returncode = 1
            m_run.return_value.stderr = "boom\nlast line"
            assert _ensure_gateway_or_hint() is False
        err = capsys.readouterr().err
        assert "gateway not running" in err
        assert "aimeshchat gateway start" in err

    def test_ensure_gateway_hint_timeout(self, capsys):
        import subprocess as _subprocess

        from codeagent.oracle import _ensure_gateway_or_hint

        gw = MagicMock()
        gw.call.side_effect = Exception("down")
        with patch("codeagent.oracle.GatewayClient", return_value=gw), \
             patch("codeagent.oracle.subprocess.run",
                   side_effect=_subprocess.TimeoutExpired("x", 20)), \
             patch("codeagent.oracle.time.sleep"):
            assert _ensure_gateway_or_hint() is False
        assert "gateway not running" in capsys.readouterr().err

    def test_ensure_gateway_hint_filenotfound(self, capsys):
        from codeagent.oracle import _ensure_gateway_or_hint

        gw = MagicMock()
        gw.call.side_effect = Exception("down")
        with patch("codeagent.oracle.GatewayClient", return_value=gw), \
             patch("codeagent.oracle.subprocess.run",
                   side_effect=FileNotFoundError("no python")), \
             patch("codeagent.oracle.time.sleep"):
            assert _ensure_gateway_or_hint() is False
        assert "gateway not running" in capsys.readouterr().err

    def test_ensure_gateway_hint_unknown_exception(self, capsys):
        from codeagent.oracle import _ensure_gateway_or_hint

        gw = MagicMock()
        gw.call.side_effect = Exception("down")
        with patch("codeagent.oracle.GatewayClient", return_value=gw), \
             patch("codeagent.oracle.subprocess.run", side_effect=RuntimeError("weird")), \
             patch("codeagent.oracle.time.sleep"):
            assert _ensure_gateway_or_hint() is False

    # ── _adopt_runtime ───────────────────────────────────────────────

    def test_adopt_runtime_skipped_omp(self):
        from codeagent.oracle import _adopt_runtime

        assert _adopt_runtime("k", "sid", _handle(), "omp") == "skipped"

    def test_adopt_runtime_register_ok(self):
        from codeagent.oracle import _adopt_runtime

        gw = MagicMock()
        gw.call.return_value = {}
        with patch("codeagent.gateway.client.GatewayClient", return_value=gw):
            assert _adopt_runtime("k1", "sid-1", _handle(), "opencode") is True
        params = gw.call.call_args[0][1]
        assert params["session_id"] == "sid-1"
        assert params["runtime_id"] == "rt-1"
        assert params["review_key"] == "k1"

    def test_adopt_runtime_register_fail(self, capsys):
        from codeagent.oracle import _adopt_runtime

        gw = MagicMock()
        gw.call.side_effect = Exception("register refused")
        with patch("codeagent.gateway.client.GatewayClient", return_value=gw):
            assert _adopt_runtime("k1", "sid-1", _handle(), "opencode") is False
        assert "gateway runtime adoption failed" in capsys.readouterr().err

    # ── omp config paths / flat yaml / agent model ───────────────────

    def test_omp_config_paths_env_and_home(self, tmp_path, monkeypatch):
        from codeagent.oracle import _omp_config_paths

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        monkeypatch.delenv("OMP_CONFIG", raising=False)
        assert _omp_config_paths() == []
        cfg = tmp_path / ".omp" / "agent" / "config.yml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("a: b\n", encoding="utf-8")
        assert _omp_config_paths() == [cfg]
        # env var wins
        env_cfg = tmp_path / "env.yml"
        env_cfg.write_text("x: 1\n", encoding="utf-8")
        monkeypatch.setenv("OMP_CONFIG", str(env_cfg))
        assert _omp_config_paths()[0] == env_cfg

    def test_parse_flat_yaml_sections_and_noise(self, tmp_path):
        from codeagent.oracle import _parse_flat_yaml

        p = tmp_path / "config.yml"
        p.write_text(
            "# comment\n\n"
            "memory:\n"
            "  backend: memsearch\n"
            "memsearch:\n"
            "  autoRecall: true\n"
            "not-a-section\n"
            "top: bare\n",
            encoding="utf-8",
        )
        parsed = _parse_flat_yaml(p)
        assert parsed["memory"]["backend"] == "memsearch"
        assert parsed["memsearch"]["autoRecall"] == "true"
        assert parsed["top"] == {}

    def test_parse_flat_yaml_missing_and_oserror(self, tmp_path):
        from codeagent.oracle import _parse_flat_yaml

        assert _parse_flat_yaml(tmp_path / "absent.yml") == {}
        d = tmp_path / "adir"
        d.mkdir()
        assert _parse_flat_yaml(d) == {}  # IsADirectoryError → OSError → {}

    def test_read_agent_model_forms(self, tmp_path, monkeypatch):
        from codeagent.oracle import _read_agent_model

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        agents = tmp_path / ".omp" / "agent" / "agents"
        agents.mkdir(parents=True)
        assert _read_agent_model("oracle") == ""
        (agents / "oracle.md").write_text("model: prof/gpt-5.6-sol\n", encoding="utf-8")
        assert _read_agent_model("oracle") == "prof/gpt-5.6-sol"
        (agents / "lite.md").write_text("model=prof/v4-pro\n", encoding="utf-8")
        assert _read_agent_model("lite") == "prof/v4-pro"
        # profile without model line → ""
        (agents / "none.md").write_text("description: hi\n", encoding="utf-8")
        assert _read_agent_model("none") == ""
        # profile is a directory → read OSError → ""
        (agents / "dir.md").mkdir()
        assert _read_agent_model("dir") == ""

    # ── _config_fingerprint ──────────────────────────────────────────

    def test_config_fingerprint_profile(self, tmp_path, monkeypatch):
        import hashlib as _h

        from codeagent.oracle import _config_fingerprint

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        agents = tmp_path / ".omp" / "agent" / "agents"
        agents.mkdir(parents=True)
        (agents / "oracle.md").write_text("model: prof/x\n", encoding="utf-8")
        content = "model: prof/x"
        assert _config_fingerprint("oracle") == _h.sha256(content.encode()).hexdigest()[:16]

    def test_config_fingerprint_fallback_and_none(self, tmp_path, monkeypatch):
        import hashlib as _h

        from codeagent.oracle import _config_fingerprint

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        assert _config_fingerprint("") == ""
        cfg = tmp_path / ".omp" / "agent" / "config.yml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("modelRoles:\n  oracle: m/1\nretry:\n  n: 2\nother:\n  x: 1\n",
                       encoding="utf-8")
        raw = json.dumps({"modelRoles": {"oracle": "m/1"}, "retry": {"n": "2"}},
                         sort_keys=True, ensure_ascii=True)
        assert _config_fingerprint("") == _h.sha256(raw.encode()).hexdigest()[:16]

    def test_config_fingerprint_empty_profile_falls_back(self, tmp_path, monkeypatch):
        import hashlib as _h

        from codeagent.oracle import _config_fingerprint

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        agents = tmp_path / ".omp" / "agent" / "agents"
        agents.mkdir(parents=True)
        (agents / "oracle.md").write_text("", encoding="utf-8")
        cfg = tmp_path / ".omp" / "agent" / "config.yml"
        cfg.write_text("modelRoles:\n  oracle: m/1\n", encoding="utf-8")
        raw = json.dumps({"modelRoles": {"oracle": "m/1"}}, sort_keys=True, ensure_ascii=True)
        assert _config_fingerprint("oracle") == _h.sha256(raw.encode()).hexdigest()[:16]

    # ── _runtime_context_model ───────────────────────────────────────

    def test_runtime_context_model_no_env(self, monkeypatch):
        from codeagent.oracle import _runtime_context_model

        monkeypatch.delenv("AIMESHCHAT_RUNTIME_ID", raising=False)
        assert _runtime_context_model("oracle") is None

    def test_runtime_context_model_ctx(self, monkeypatch):
        from codeagent.oracle import _runtime_context_model

        monkeypatch.setenv("AIMESHCHAT_RUNTIME_ID", "rt-1")
        gw = MagicMock()
        gw.call.return_value = {"model_context": {"model": "m/x", "variant": "v1", "provider": "p1"}}
        with patch("codeagent.oracle._gateway", return_value=gw):
            assert _runtime_context_model("oracle") == ("m/x", "v1", "p1")
        gw.call.assert_called_once_with("runtime.context_get", {"runtime_id": "rt-1"})

    def test_runtime_context_model_gateway_error(self, monkeypatch):
        from codeagent.domain import ModelContextUnavailable
        from codeagent.oracle import _runtime_context_model

        monkeypatch.setenv("AIMESHCHAT_RUNTIME_ID", "rt-1")
        gw = MagicMock()
        gw.call.side_effect = Exception("socket not found")
        with patch("codeagent.oracle._gateway", return_value=gw):
            with pytest.raises(ModelContextUnavailable):
                _runtime_context_model("oracle")

    def test_runtime_context_model_empty_model(self, monkeypatch):
        from codeagent.domain import ModelContextUnavailable
        from codeagent.oracle import _runtime_context_model

        monkeypatch.setenv("AIMESHCHAT_RUNTIME_ID", "rt-1")
        gw = MagicMock()
        gw.call.return_value = {"model_context": {}}
        with patch("codeagent.oracle._gateway", return_value=gw):
            with pytest.raises(ModelContextUnavailable):
                _runtime_context_model("oracle")

    # ── _ask_model_chain_realtime ────────────────────────────────────

    def test_ask_model_chain_realtime_explicit(self):
        from dataclasses import replace

        from codeagent.oracle import _ask_model_chain_realtime

        m = replace(_manifest("k1"), model="explicit/x")
        assert _ask_model_chain_realtime("oracle", m) == ["explicit/x"]
        # manifest.model 优先于 explicit_override；仅当 manifest 无 model 时
        # explicit_override 生效
        m2 = replace(_manifest("k1"), model="")
        assert _ask_model_chain_realtime("oracle", m2, explicit_override="ask/y") == ["ask/y"]

    def test_ask_model_chain_realtime_ctx_priority(self):
        from dataclasses import replace

        from codeagent.oracle import _ask_model_chain_realtime

        m = replace(_manifest("k1"), model="", primary_model="persisted/x")
        with patch("codeagent.oracle._runtime_context_model",
                   return_value=("ctx/x", "", "")):
            assert _ask_model_chain_realtime("oracle", m) == ["ctx/x"]

    def test_ask_model_chain_realtime_ctx_unavailable_falls_back(self):
        from dataclasses import replace

        from codeagent.domain import ModelContextUnavailable
        from codeagent.oracle import _ask_model_chain_realtime

        m = replace(_manifest("k1"), model="", primary_model="persisted/x")
        with patch("codeagent.oracle._runtime_context_model",
                   side_effect=ModelContextUnavailable("no ctx")):
            assert _ask_model_chain_realtime("oracle", m) == ["persisted/x"]

    def test_ask_model_chain_realtime_fingerprint_change(self):
        from dataclasses import replace

        from codeagent.oracle import _ask_model_chain_realtime

        m = replace(_manifest("k1"), model="", primary_model="persisted/x",
                    config_fingerprint="oldfp")
        with patch("codeagent.oracle._runtime_context_model", return_value=None), \
             patch("codeagent.oracle._config_fingerprint", return_value="newfp"), \
             patch("codeagent.oracle._resolve_oracle_model_chain",
                   return_value=["derived/x"]) as resolve:
            assert _ask_model_chain_realtime("oracle", m) == ["derived/x"]
            resolve.assert_called_once_with("oracle", "")

    def test_ask_model_chain_realtime_old_manifest(self):
        from dataclasses import replace

        from codeagent.oracle import _ask_model_chain_realtime

        m = replace(_manifest("k1"), model="", primary_model="")
        with patch("codeagent.oracle._runtime_context_model", return_value=None), \
             patch("codeagent.oracle._config_fingerprint", return_value=""), \
             patch("codeagent.oracle._resolve_oracle_model_chain",
                   return_value=["migrated/x"]):
            assert _ask_model_chain_realtime("oracle-lite", m) == ["migrated/x"]

    # ── _merge_flat_yaml / ensure_omp_memory_config ──────────────────

    def test_merge_flat_yaml_existing_section(self, tmp_path):
        from codeagent.oracle import _merge_flat_yaml

        p = tmp_path / "config.yml"
        p.write_text("memsearch:\n  backend: memsearch\ncompaction:\n  x: 1\n",
                     encoding="utf-8")
        assert _merge_flat_yaml(p, {"memsearch": {"autoRecall": "true"}}) is True
        out = p.read_text(encoding="utf-8")
        assert "  autoRecall: true" in out
        # inserted at the END of the memsearch section, before compaction
        assert out.index("autoRecall") < out.index("compaction")
        backups = list(tmp_path.glob("config.yml.bak-*"))
        assert len(backups) == 1

    def test_merge_flat_yaml_new_section(self, tmp_path):
        from codeagent.oracle import _merge_flat_yaml

        p = tmp_path / "config.yml"
        p.write_text("memsearch:\n  autoRecall: true\n", encoding="utf-8")
        assert _merge_flat_yaml(p, {"memory": {"backend": "memsearch"}}) is True
        out = p.read_text(encoding="utf-8")
        assert "memory:" in out and "  backend: memsearch" in out

    def test_merge_flat_yaml_noop(self, tmp_path):
        from codeagent.oracle import _merge_flat_yaml

        p = tmp_path / "config.yml"
        p.write_text("memsearch:\n  autoRecall: true\n", encoding="utf-8")
        assert _merge_flat_yaml(p, {"memsearch": {"autoRecall": "true"}}) is False
        assert not list(tmp_path.glob("config.yml.bak-*"))

    def test_merge_flat_yaml_creates_missing_file(self, tmp_path):
        from codeagent.oracle import _merge_flat_yaml

        p = tmp_path / "config.yml"
        assert _merge_flat_yaml(p, {"memory": {"backend": "memsearch"}}) is True
        assert "backend: memsearch" in p.read_text(encoding="utf-8")

    def test_ensure_omp_memory_config_no_paths(self, tmp_path, monkeypatch):
        from codeagent.oracle import ensure_omp_memory_config

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        monkeypatch.delenv("OMP_CONFIG", raising=False)
        r = ensure_omp_memory_config()
        assert r["config_path"] == ""
        assert any("omp config file not found" in m for m in r["missing"])

    def test_ensure_omp_memory_config_all_set(self, tmp_path, monkeypatch):
        from codeagent.oracle import ensure_omp_memory_config

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        monkeypatch.delenv("OMP_CONFIG", raising=False)
        cfg = tmp_path / ".omp" / "agent" / "config.yml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(
            "memory:\n  backend: memsearch\nmemsearch:\n  autoRecall: true\n"
            "compaction:\n  handoffSaveToDisk: true\n",
            encoding="utf-8",
        )
        r = ensure_omp_memory_config()
        assert r["auto_recall"] is True
        assert r["handoff_save_to_disk"] is True
        assert r["backend"] == "memsearch"
        assert r["missing"] == []

    def test_ensure_omp_memory_config_missing_no_apply(self, tmp_path, monkeypatch):
        from codeagent.oracle import ensure_omp_memory_config

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        monkeypatch.delenv("OMP_CONFIG", raising=False)
        cfg = tmp_path / ".omp" / "agent" / "config.yml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("memory:\n  backend: memsearch\n", encoding="utf-8")
        r = ensure_omp_memory_config()
        assert r["merged"] is False
        assert "memsearch.autoRecall=true" in r["missing"]
        assert "compaction.handoffSaveToDisk=true" in r["missing"]

    def test_ensure_omp_memory_config_apply_merges(self, tmp_path, monkeypatch):
        from codeagent.oracle import ensure_omp_memory_config

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        monkeypatch.delenv("OMP_CONFIG", raising=False)
        cfg = tmp_path / ".omp" / "agent" / "config.yml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("memsearch:\n  autoRecall: true\n", encoding="utf-8")
        r = ensure_omp_memory_config(apply=True)
        assert r["merged"] is True
        out = cfg.read_text(encoding="utf-8")
        assert "handoffSaveToDisk: true" in out
        assert "backend: memsearch" in out

    def test_ensure_omp_memory_config_merge_error(self, tmp_path, monkeypatch):
        from codeagent.oracle import ensure_omp_memory_config

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        monkeypatch.delenv("OMP_CONFIG", raising=False)
        cfg = tmp_path / ".omp" / "agent" / "config.yml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("memsearch:\n  autoRecall: true\n", encoding="utf-8")
        with patch("codeagent.oracle._merge_flat_yaml", side_effect=OSError("disk full")):
            r = ensure_omp_memory_config(apply=True)
        assert "disk full" in r["merge_error"]

    # ── _ensure_oracle_overlay / _check_mailbox_plugin ───────────────

    def test_ensure_oracle_overlay_idempotent(self, tmp_path, monkeypatch):
        from codeagent.oracle import _ensure_oracle_overlay

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        overlay = _ensure_oracle_overlay()
        assert overlay.exists()
        assert "advisor:" in overlay.read_text(encoding="utf-8")
        # second call must NOT rewrite
        monkeypatch.setattr(Path, "write_text", lambda *a, **k: (_ for _ in ()).throw(AssertionError("rewrote")))
        assert _ensure_oracle_overlay() == overlay

    def test_check_mailbox_plugin_missing(self, tmp_path, monkeypatch):
        from codeagent.oracle import _check_mailbox_plugin

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        err = _check_mailbox_plugin()
        assert err is not None and "not found" in err

    def test_check_mailbox_plugin_predates(self, tmp_path, monkeypatch):
        from codeagent.oracle import _check_mailbox_plugin

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        plugin = (tmp_path / ".omp" / "plugins" / "node_modules"
                  / "omp-mailbox-plugin" / "src" / "index.ts")
        plugin.parent.mkdir(parents=True)
        plugin.write_text("export {};\n", encoding="utf-8")
        err = _check_mailbox_plugin()
        assert err is not None and "predates" in err

    def test_check_mailbox_plugin_ok(self, tmp_path, monkeypatch):
        from codeagent.oracle import _check_mailbox_plugin

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        plugin = (tmp_path / ".omp" / "plugins" / "node_modules"
                  / "omp-mailbox-plugin" / "src" / "index.ts")
        plugin.parent.mkdir(parents=True)
        plugin.write_text("class RuntimeEventReporter {}\n", encoding="utf-8")
        assert _check_mailbox_plugin() is None

    # ── meta.json ────────────────────────────────────────────────────

    def test_oracle_meta_path_and_session_dir_sanitize(self, tmp_path, monkeypatch):
        from codeagent.oracle import _oracle_meta_path, _oracle_session_dir

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        assert _oracle_meta_path("a:b/c\\d") == tmp_path / ".omp" / "oracle" / "a-b-c-d" / "meta.json"
        assert _oracle_session_dir("a:b/c\\d") == str(
            tmp_path / ".omp" / "agent" / "sessions" / "_oracle" / "a-b-c-d")

    def test_read_write_oracle_meta_roundtrip(self, tmp_path, monkeypatch):
        from codeagent.oracle import _read_oracle_meta, _write_oracle_meta

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        meta = _write_oracle_meta("k1", "sid-1", "bound", swarm_session_id="sw-1")
        assert meta["backend_session_id"] == "sid-1"
        assert meta["status"] == "bound"
        got = _read_oracle_meta("k1")
        assert got["backend_session_id"] == "sid-1"
        assert got["swarm_session_id"] == "sw-1"

    def test_read_oracle_meta_corrupt(self, tmp_path, monkeypatch):
        from codeagent.oracle import _read_oracle_meta

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        assert _read_oracle_meta("k1") == {}
        p = tmp_path / ".omp" / "oracle" / "k1" / "meta.json"
        p.parent.mkdir(parents=True)
        p.write_text("{not-json", encoding="utf-8")
        assert _read_oracle_meta("k1") == {}
        p.write_text("[1, 2]", encoding="utf-8")
        assert _read_oracle_meta("k1") == {}

    def test_write_oracle_meta_oserror_warns(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import _write_oracle_meta

        blocker = tmp_path / "blocker"
        blocker.write_text("file", encoding="utf-8")
        monkeypatch.setattr("codeagent.oracle._oracle_meta_path",
                            lambda _k: blocker / "meta.json")
        meta = _write_oracle_meta("k1", "sid-1", "bound")
        assert meta["backend_session_id"] == "sid-1"
        assert "oracle meta write failed" in capsys.readouterr().err

    # ── runtime log / session-id binding ─────────────────────────────

    def test_runtime_log_path_spec_and_fallback(self, tmp_path, monkeypatch):
        from dataclasses import replace

        from codeagent.oracle import _runtime_log_path

        rt_dir = tmp_path / "rt"
        rt_dir.mkdir()
        h = replace(_handle("rt-x"), extra={"spec_path": str(rt_dir / "spec.json")})
        assert _runtime_log_path(h) == rt_dir / "rt-x.log"
        # spec parent missing → supervisor fallback
        h2 = replace(_handle("rt-y"), extra={"spec_path": str(tmp_path / "nope" / "spec.json")})
        monkeypatch.setattr("codeagent.runtime.supervisor._runtime_dir",
                            lambda _rid: tmp_path / "sup")
        assert _runtime_log_path(h2) == tmp_path / "sup" / "rt-y.log"

    def test_runtime_log_path_exception(self, tmp_path, monkeypatch):
        from dataclasses import replace

        from codeagent.oracle import _runtime_log_path

        h = replace(_handle("rt-z"), extra={})
        monkeypatch.setattr("codeagent.runtime.supervisor._runtime_dir",
                            lambda _rid: (_ for _ in ()).throw(Exception("boom")))
        assert _runtime_log_path(h) is None

    def test_scan_runtime_log_backend_wins(self, tmp_path):
        from codeagent.oracle import _scan_runtime_log_for_session_id

        logp = tmp_path / "rt.log"
        logp.write_text("session_id=postmesh-x backend_session=ses_999\n"
                        "backend_session_id=ses_777 session_id=ora-1\n", encoding="utf-8")
        assert _scan_runtime_log_for_session_id(logp) == "ses_777"

    def test_scan_runtime_log_session_id_filter(self, tmp_path):
        from codeagent.oracle import _scan_runtime_log_for_session_id

        logp = tmp_path / "rt.log"
        logp.write_text("session_id=postmesh-x\nsession_id=ora-9\n", encoding="utf-8")
        assert _scan_runtime_log_for_session_id(logp) == ""
        logp.write_text("session_id=ses_1\nsession_id=ses_2\n", encoding="utf-8")
        assert _scan_runtime_log_for_session_id(logp) == "ses_2"

    def test_scan_runtime_log_missing_and_oserror(self, tmp_path):
        from codeagent.oracle import _scan_runtime_log_for_session_id

        assert _scan_runtime_log_for_session_id(None) == ""
        assert _scan_runtime_log_for_session_id(tmp_path / "absent.log") == ""
        d = tmp_path / "logdir"
        d.mkdir()
        assert _scan_runtime_log_for_session_id(d) == ""

    def test_poll_backend_session_id_direct(self):
        from codeagent.oracle import _poll_backend_session_id

        with patch("codeagent.oracle.time.sleep",
                   side_effect=AssertionError("must not sleep")):
            assert _poll_backend_session_id(_handle()) == "b1"

    def test_poll_backend_session_id_from_log(self, tmp_path, monkeypatch):
        from dataclasses import replace

        from codeagent.oracle import _poll_backend_session_id

        logp = tmp_path / "rt.log"
        logp.write_text("backend_session=ses_abc\n", encoding="utf-8")
        h = replace(_handle("rt-l"), backend_session_id="")
        monkeypatch.setattr("codeagent.oracle._runtime_log_path", lambda _h: logp)
        with patch("codeagent.oracle.time.sleep",
                   side_effect=AssertionError("must not sleep")):
            assert _poll_backend_session_id(h, timeout=1.0) == "ses_abc"

    def test_poll_backend_session_id_timeout(self, monkeypatch):
        from dataclasses import replace

        from codeagent.oracle import _poll_backend_session_id

        h = replace(_handle("rt-t"), backend_session_id="")
        monkeypatch.setattr("codeagent.oracle._runtime_log_path", lambda _h: None)
        assert _poll_backend_session_id(h, timeout=0.0) == ""

    # ── _resolve_bound_session_id / lazy sync ────────────────────────

    def test_resolve_bound_session_id_manifest(self, tmp_path, monkeypatch):
        from codeagent.oracle import _resolve_bound_session_id

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        assert _resolve_bound_session_id("k1", _manifest("k1", backend_session_id="b1")) == "b1"

    def test_resolve_bound_session_id_meta(self, tmp_path, monkeypatch):
        from codeagent.oracle import _resolve_bound_session_id, _write_oracle_meta

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        _write_oracle_meta("k1", "m-1", "bound")
        m = _manifest("k1", backend_session_id="")
        assert _resolve_bound_session_id("k1", m) == "m-1"

    def test_resolve_bound_session_id_lazy_sync(self, tmp_path, monkeypatch):
        from codeagent.oracle import _read_oracle_meta, _resolve_bound_session_id
        from codeagent.park.registry import ParkRegistry

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        reg = ParkRegistry()
        reg.acquire("k1", _manifest("k1", backend_session_id=""))
        gw = MagicMock()
        gw.call.return_value = {"backend_session_id": "g-1"}
        with patch("codeagent.oracle._gateway", return_value=gw):
            assert _resolve_bound_session_id("k1", reg.lookup("k1")) == "g-1"
        # backfilled into both manifest and meta
        assert reg.lookup("k1").backend_session_id == "g-1"
        assert _read_oracle_meta("k1")["backend_session_id"] == "g-1"

    def test_resolve_bound_session_id_gateway_error(self, tmp_path, monkeypatch):
        from codeagent.oracle import _resolve_bound_session_id

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        gw = MagicMock()
        gw.call.side_effect = Exception("down")
        with patch("codeagent.oracle._gateway", return_value=gw):
            assert _resolve_bound_session_id("k1", _manifest("k1", backend_session_id="")) == ""

    def test_resolve_bound_session_id_gateway_empty(self, tmp_path, monkeypatch):
        from codeagent.oracle import _resolve_bound_session_id

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        gw = MagicMock()
        gw.call.return_value = {}
        with patch("codeagent.oracle._gateway", return_value=gw):
            assert _resolve_bound_session_id("k1", _manifest("k1", backend_session_id="")) == ""

    # ── cmd_oracle_start paths ───────────────────────────────────────

    def _start_ctx(self, tmp_path, monkeypatch, spawn_return=None):
        """Deterministic start context: gateway up, plugin ok, no gc thread.

        Returns (ExitStack, kernel, store, spawn_mock) — caller enters st.
        """
        from contextlib import ExitStack

        st = ExitStack()
        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        st.enter_context(patch("codeagent.oracle._ensure_gateway_or_hint", return_value=True))
        st.enter_context(patch("codeagent.oracle._check_mailbox_plugin", return_value=None))
        st.enter_context(patch("codeagent.oracle._gc_throttle", return_value=False))
        kernel = MagicMock()
        store = MagicMock()
        store.root = tmp_path / "mb"
        st.enter_context(patch("codeagent.cli._get_swarm_kernel", return_value=(kernel, store)))
        spawn = st.enter_context(patch(
            "codeagent.oracle.RuntimeRegistry.spawn",
            return_value=spawn_return if spawn_return is not None else _handle()))
        return st, kernel, store, spawn

    def test_start_gateway_down_returns_1(self, tmp_path, capsys):
        from codeagent.oracle import cmd_oracle_start

        ns = _NS(review_key="k1", agent="oracle", backend="omp",
                 workdir=str(tmp_path), model="m/x", prompt="hi")
        with patch("codeagent.oracle._ensure_gateway_or_hint", return_value=False), \
             patch("codeagent.oracle.RuntimeRegistry.spawn") as spawn:
            assert cmd_oracle_start(ns) == 1
        spawn.assert_not_called()
        assert capsys.readouterr().out == ""

    def test_start_plugin_missing_returns_1(self, tmp_path, capsys):
        from codeagent.oracle import cmd_oracle_start

        ns = _NS(review_key="k1", agent="oracle", backend="omp",
                 workdir=str(tmp_path), model="m/x", prompt="hi")
        with patch("codeagent.oracle._ensure_gateway_or_hint", return_value=True), \
             patch("codeagent.oracle._check_mailbox_plugin",
                   return_value="plugin missing"), \
             patch("codeagent.oracle.RuntimeRegistry.spawn") as spawn:
            assert cmd_oracle_start(ns) == 1
        spawn.assert_not_called()
        err = json.loads(capsys.readouterr().err)
        assert err["error"] == "mailbox_plugin_unavailable"
        assert err["review_key"] == "k1"

    def test_start_model_strict_unavailable(self, tmp_path, monkeypatch, capsys):
        from codeagent.domain import ModelContextUnavailable
        from codeagent.oracle import cmd_oracle_start

        ns = _NS(review_key="k1", agent="oracle", backend="omp",
                 workdir=str(tmp_path), model="", prompt="hi", model_strict=True)
        st, kernel, store, spawn = self._start_ctx(tmp_path, monkeypatch)
        st.enter_context(patch(
            "codeagent.oracle.ExecutionSpec.from_args",
            side_effect=ModelContextUnavailable("no runtime context")))
        with st:
            assert cmd_oracle_start(ns) == 1
        spawn.assert_not_called()
        assert "MODEL_CONTEXT_UNAVAILABLE" in capsys.readouterr().err

    def test_start_no_model_returns_1(self, tmp_path, monkeypatch, capsys):
        from codeagent.domain import ExecutionSpec
        from codeagent.oracle import cmd_oracle_start

        ns = _NS(review_key="k1", agent="oracle", backend="omp",
                 workdir=str(tmp_path), model="", prompt="hi")
        spec = ExecutionSpec(provider="", model="", variant="", system_prompt="",
                             full_prompt="hi", model_source="")
        st, kernel, store, spawn = self._start_ctx(tmp_path, monkeypatch)
        st.enter_context(patch("codeagent.oracle.ExecutionSpec.from_args", return_value=spec))
        with st:
            assert cmd_oracle_start(ns) == 1
        spawn.assert_not_called()
        assert "请显式 --model" in capsys.readouterr().err

    def test_start_success_path(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import cmd_oracle_start
        from codeagent.park.registry import ParkRegistry

        ns = _NS(review_key="proj:oracle:gfx:blur", agent="", backend="omp",
                 workdir=str(tmp_path), model="m/x", prompt="hi")
        st, kernel, store, spawn = self._start_ctx(tmp_path, monkeypatch)
        with st:
            assert cmd_oracle_start(ns) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["bound"] is True
        assert out["backend_session_id"] == "b1"
        assert out["adopted"] == "skipped"
        assert out["model_chain"] == ["m/x"]
        assert out["spec"]["model_source"] == "explicit"
        req = spawn.call_args[0][1]
        assert req["model"] == "m/x"
        assert req["session_id"] == out["session_id"]
        assert req["env"]["OMP_MODEL_FALLBACK_CHAIN"] == "m/x"
        m = ParkRegistry().lookup(ns.review_key)
        assert m is not None
        assert m.lifecycle == Lifecycle.HOT_PARKED
        assert m.backend_session_id == "b1"
        assert m.primary_model == "m/x"
        assert m.omp_session_dir == str(
            tmp_path / ".omp" / "agent" / "sessions" / "_oracle" / "proj-oracle-gfx-blur")

    def test_start_binding_pending(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import _read_oracle_meta, cmd_oracle_start

        ns = _NS(review_key="k-pend", agent="oracle", backend="omp",
                 workdir=str(tmp_path), model="m/x", prompt="hi")
        st, kernel, store, spawn = self._start_ctx(tmp_path, monkeypatch)
        st.enter_context(patch("codeagent.oracle._poll_backend_session_id", return_value=""))
        with st:
            assert cmd_oracle_start(ns) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["bound"] is False
        assert out["binding"] == "pending"
        assert _read_oracle_meta("k-pend")["status"] == "pending"

    def test_start_idempotent_session_register(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import cmd_oracle_start
        from codeagent.park.registry import ParkRegistry

        ns = _NS(review_key="k-idem", agent="oracle", backend="omp",
                 workdir=str(tmp_path), model="m/x", prompt="hi")
        st, kernel, store, spawn = self._start_ctx(tmp_path, monkeypatch)
        kernel.create_session.side_effect = ValueError("session already exists")
        kernel.register.side_effect = ValueError("agent already registered")
        with st:
            assert cmd_oracle_start(ns) == 0
        assert ParkRegistry().lookup("k-idem") is not None

    def test_start_init_task_dispatch_warning(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import cmd_oracle_start

        ns = _NS(review_key="k-disp", agent="oracle", backend="omp",
                 workdir=str(tmp_path), model="m/x", prompt="hi")
        st, kernel, store, spawn = self._start_ctx(tmp_path, monkeypatch)
        st.enter_context(patch("codeagent.mailbox.service.MailboxService.send",
                               side_effect=Exception("mailbox busy")))
        with st:
            assert cmd_oracle_start(ns) == 0
        assert "initial task dispatch failed" in capsys.readouterr().err

    def test_start_opencode_adopts_runtime(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import cmd_oracle_start

        ns = _NS(review_key="k-oc", agent="oracle", backend="opencode",
                 workdir=str(tmp_path), model="m/x", prompt="hi")
        gw = MagicMock()
        gw.call.return_value = {}
        st, kernel, store, spawn = self._start_ctx(tmp_path, monkeypatch)
        st.enter_context(patch("codeagent.gateway.client.GatewayClient", return_value=gw))
        with st:
            assert cmd_oracle_start(ns) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["backend"] == "opencode"
        assert out["adopted"] is True
        req = spawn.call_args[0][1]
        # non-omp: prompt travels via spawn task; no overlay; no session dir
        assert req["task"] == "hi"
        assert req["profile_args"] == []
        assert req["session_dir"] == ""

    def test_start_restart_preserves_manifest_fields(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import cmd_oracle_start
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        registry.acquire("k-rst", _manifest("k-rst", backend_session_id="old-sid"))
        ns = _NS(review_key="k-rst", agent="oracle", backend="omp",
                 workdir=str(tmp_path), model="m/x", prompt="hi")
        st, kernel, store, spawn = self._start_ctx(tmp_path, monkeypatch)
        with st:
            assert cmd_oracle_start(ns) == 0
        m = registry.lookup("k-rst")
        assert m.backend_session_id == "b1"  # updated
        assert m.lifecycle == Lifecycle.HOT_PARKED
        assert m.release_mode == ""

    # ── cmd_oracle_ask paths ─────────────────────────────────────────

    def test_ask_no_prompt_returns_1(self, tmp_path, capsys):
        from codeagent.oracle import cmd_oracle_ask

        ns = _NS(review_key="k1", prompt="   ", agent="oracle", backend="omp")
        assert cmd_oracle_ask(ns) == 1
        err = json.loads(capsys.readouterr().err)
        assert err["error"] == "no_prompt"

    def test_ask_hot_blocked_binding_pending(self, tmp_path, capsys):
        from codeagent.oracle import cmd_oracle_ask

        ns = _NS(review_key="k1", prompt="p", agent="oracle", backend="omp")
        info = {"runtime_id": "rt-1", "status": "active",
                "backend_session_id": "", "runtime_health": {"alive": True}}
        gw = MagicMock()
        gw.call.return_value = info
        with patch("codeagent.oracle._gateway", return_value=gw):
            assert cmd_oracle_ask(ns) == 1
        err = json.loads(capsys.readouterr().err)
        assert err["status"] == "binding_pending"
        assert err["method"] == "blocked"

    def test_ask_hot_wait_binding_success(self, tmp_path, capsys):
        from codeagent.oracle import cmd_oracle_ask

        ns = _NS(review_key="k1", prompt="p", agent="oracle", backend="omp",
                 wait_binding=True)
        no_sid = {"runtime_id": "rt-1", "status": "active",
                  "backend_session_id": "", "runtime_health": {"alive": True}}
        with_sid = {"runtime_id": "rt-1", "status": "active",
                    "backend_session_id": "b1", "runtime_health": {"alive": True}}
        gw = MagicMock()
        gw.call.side_effect = [no_sid, with_sid,
                               {"msg_id": "m-1", "status": "delivered"}]
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle.time.sleep"):
            assert cmd_oracle_ask(ns) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["method"] == "hot_pending_ack"
        assert out["msg_id"] == "m-1"

    def test_ask_hot_wait_binding_timeout(self, tmp_path, capsys):
        from codeagent.oracle import cmd_oracle_ask

        ns = _NS(review_key="k1", prompt="p", agent="oracle", backend="omp",
                 wait_binding=True)
        no_sid = {"runtime_id": "rt-1", "status": "active",
                  "backend_session_id": "", "runtime_health": {"alive": True}}
        gw = MagicMock()
        gw.call.return_value = no_sid
        mono_counter = iter(range(0, 100))
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle.time.sleep"), \
             patch("codeagent.oracle.time.monotonic",
                   side_effect=lambda: float(next(mono_counter))):
            assert cmd_oracle_ask(ns) == 1
        err = json.loads(capsys.readouterr().err)
        assert err["status"] == "binding_pending"
        assert "did not bind within" in err["detail"]

    def test_ask_warm_spawn_quota_failure(self, tmp_path, capsys):
        from codeagent.oracle import cmd_oracle_ask
        from codeagent.park.registry import ParkRegistry

        ParkRegistry().acquire("k1", _manifest("k1", backend_session_id="native-1"))
        ns = _NS(review_key="k1", prompt="p", agent="oracle", backend="omp")
        gw = MagicMock()
        gw.call.side_effect = _raise(Exception("GATEWAY_DOWN: socket not found"))
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._ask_model_chain_realtime", return_value=["q/x"]), \
             patch("codeagent.oracle.RuntimeRegistry.spawn",
                   side_effect=Exception("insufficient_quota (402)")):
            assert cmd_oracle_ask(ns) == 1
        err = capsys.readouterr().err
        assert '"method": "warm_failed"' in err
        assert "insufficient_quota" in err
        assert '"method": "cold_failed"' in err

    def test_ask_warm_sid_pending_warning(self, tmp_path, capsys):
        from codeagent.oracle import cmd_oracle_ask
        from codeagent.park.registry import ParkRegistry

        ParkRegistry().acquire("k1", _manifest("k1", backend_session_id="native-1"))
        ns = _NS(review_key="k1", prompt="p", agent="oracle", backend="omp")
        gw = MagicMock()
        gw.call.side_effect = _raise(Exception("GATEWAY_DOWN: socket not found"))
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._ask_model_chain_realtime", return_value=["q/x"]), \
             patch("codeagent.oracle.RuntimeRegistry.spawn",
                   return_value=_handle("rt-w", backend_session_id="")) as spawn:
            assert cmd_oracle_ask(ns) == 0
        captured = capsys.readouterr()  # pytest 9: single read, resets buffer
        err, out = captured.err, captured.out
        assert "preserving previous id" in err
        out = json.loads(out)
        assert out["method"] == "warm"
        assert out["new_backend_session_id"] == "native-1"
        # manifest round advanced + backend id preserved
        m = ParkRegistry().lookup("k1")
        assert m.round == 1
        assert m.backend_session_id == "native-1"
        assert m.lifecycle == Lifecycle.HOT_PARKED

    def test_ask_warm_enqueue_failure_continues(self, tmp_path, capsys):
        from codeagent.oracle import cmd_oracle_ask
        from codeagent.park.registry import ParkRegistry

        ParkRegistry().acquire("k1", _manifest("k1", backend_session_id="native-1"))
        ns = _NS(review_key="k1", prompt="p", agent="oracle", backend="omp")
        gw = MagicMock()
        gw.call.side_effect = _raise(Exception("GATEWAY_DOWN: socket not found"))
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._ask_model_chain_realtime", return_value=["q/x"]), \
             patch("codeagent.mailbox.service.MailboxService.send",
                   side_effect=Exception("enqueue boom")), \
             patch("codeagent.oracle.RuntimeRegistry.spawn",
                   return_value=_handle("rt-w2", backend_session_id="b2")):
            assert cmd_oracle_ask(ns) == 0
        assert "warm task enqueue failed" in capsys.readouterr().err

    def test_ask_cold_with_manifest_persists(self, tmp_path, capsys):
        from dataclasses import replace

        from codeagent.oracle import cmd_oracle_ask
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        registry.acquire("k-cold", replace(
            _manifest("k-cold", backend_session_id="", lifecycle=Lifecycle.HOT_PARKED),
            primary_model="prof/gpt-5.6-sol"))
        ns = _NS(review_key="k-cold", prompt="fresh", agent="oracle", backend="omp")
        gw = MagicMock()
        gw.call.side_effect = _raise(Exception("GATEWAY_DOWN: socket not found"))
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle.build_cold_context", return_value="snapshot"), \
             patch("codeagent.oracle.RuntimeRegistry.spawn",
                   return_value=_handle("rt-c", backend_session_id="bc")) as spawn:
            assert cmd_oracle_ask(ns) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["method"] == "cold"
        assert out["model_chain"] == ["prof/gpt-5.6-sol"]
        m = registry.lookup("k-cold")
        assert m.round == 1
        assert m.lifecycle == Lifecycle.HOT_PARKED
        assert m.backend_session_id == "bc"
        req = spawn.call_args[0][1]
        assert "snapshot" in req["task"] and "fresh" in req["task"]

    def test_ask_cold_wait_forwards(self, tmp_path, capsys):
        from codeagent.oracle import cmd_oracle_ask

        ns = _NS(review_key="k1", prompt="p", agent="oracle", backend="omp", wait=True)
        gw = MagicMock()
        gw.call.side_effect = _raise(Exception("GATEWAY_DOWN: socket not found"))
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle.build_cold_context", return_value="ctx"), \
             patch("codeagent.oracle.RuntimeRegistry.spawn",
                   return_value=_handle("rt-w3", backend_session_id="b3")), \
             patch("codeagent.oracle._wait_for_new_output", return_value=0) as wait:
            assert cmd_oracle_ask(ns) == 0
        wait.assert_called_once()
        assert wait.call_args[0][0] == "k1"

    def test_ask_hot_wait_forwards(self, tmp_path, capsys):
        from codeagent.oracle import cmd_oracle_ask

        ns = _NS(review_key="k1", prompt="p", agent="oracle", backend="omp", wait=True)
        info = {"runtime_id": "rt-1", "status": "active", "session_id": "ses-1",
                "backend_session_id": "b1", "runtime_health": {"alive": True}}
        gw = MagicMock()
        gw.call.side_effect = lambda m, p=None: (
            info if m == "runtime.info" else
            {"msg_id": "m-9", "status": "delivered"})
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._wait_for_new_output", return_value=0) as wait:
            assert cmd_oracle_ask(ns) == 0
        wait.assert_called_once_with("k1", "rt-1", "ses-1", session_dir="")

    # ── cmd_oracle_status paths ──────────────────────────────────────

    def test_status_quota_error(self, tmp_path, capsys):
        from codeagent.oracle import cmd_oracle_status

        ns = _NS(review_key="k1")
        gw = MagicMock()
        gw.call.return_value = {
            "runtime_id": "rt-1", "status": "active",
            "runtime_health": {"alive": True, "quota_error": "insufficient_quota for X"},
        }
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._detect_oracle_stuck", return_value=None), \
             patch("codeagent.oracle._gc_throttle", return_value=False):
            assert cmd_oracle_status(ns) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["runtime"]["quota_error"] == "insufficient_quota for X"
        assert "degrade_hint" in out["runtime"]

    def test_status_gateway_down_structured(self, tmp_path, capsys):
        from codeagent.gateway.model import GatewayError
        from codeagent.oracle import cmd_oracle_status

        ns = _NS(review_key="k1")
        gw = MagicMock()
        gw.call.side_effect = GatewayError("GATEWAY_DOWN", "socket not found")
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._gc_throttle", return_value=False):
            assert cmd_oracle_status(ns) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["runtime"]["status"] == "gateway_down"
        assert "aimeshchat gateway start" in out["runtime"]["hint"]

    def test_status_other_gateway_error(self, tmp_path, capsys):
        from codeagent.gateway.model import GatewayError
        from codeagent.oracle import cmd_oracle_status

        ns = _NS(review_key="k1")
        gw = MagicMock()
        gw.call.side_effect = GatewayError("SOMETHING_ELSE", "boom")
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._gc_throttle", return_value=False):
            assert cmd_oracle_status(ns) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["runtime"] == {"status": "unavailable", "error": "boom"}

    def test_status_request_ledger(self, tmp_path, capsys):
        from codeagent.oracle import cmd_oracle_status
        from codeagent.mailbox.store import MailboxStore
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        registry.acquire("k1", _manifest("k1", backend_session_id="b1"))
        m = registry.lookup("k1")
        req_dir = (MailboxStore().session_dir(m.swarm_session_id)
                   / "oracle" / "events" / "req-1")
        req_dir.mkdir(parents=True)
        (req_dir / "events.jsonl").write_text(
            json.dumps({"request_id": "req-1", "run_id": "run-1",
                        "event": "DONE", "ts": 1.0, "meta": {}}) + "\n",
            encoding="utf-8")
        gw = MagicMock()
        gw.call.return_value = {"runtime_id": "rt-1", "status": "active",
                                "runtime_health": {"alive": True}}
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._detect_oracle_stuck", return_value=None), \
             patch("codeagent.oracle._gc_throttle", return_value=False):
            assert cmd_oracle_status(_NS(review_key="k1")) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["requests"] == [{
            "request_id": "req-1", "run_id": "run-1",
            "states": ["DONE"], "terminal": "DONE",
        }]
        assert out["mailbox"]["unread"] == 0

    def test_status_receipts_and_mailbox_unread(self, tmp_path, capsys):
        from codeagent.oracle import cmd_oracle_status
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        registry.acquire("k1", _manifest("k1", backend_session_id="b1"))
        m = registry.lookup("k1")
        mock_store = MagicMock()
        mock_store.read_history.return_value = [
            {"kind": "RECEIPT", "msg_id": "m-1", "reply_to": "r-1", "from": "oracle"},
            {"kind": "PROGRESS", "msg_id": "m-0", "reply_to": "r-0", "from": "oracle"},
        ]
        report_file = tmp_path / "report.json"
        report_file.write_text(json.dumps({
            "kind": "REPORT", "msg_id": "m-2", "from": "oracle",
            "created_at": "2026-08-13T00:00:00Z", "subject": "done",
            "body": "result body",
        }), encoding="utf-8")
        mock_store.list_messages.return_value = [report_file]
        gw = MagicMock()
        gw.call.return_value = {"runtime_id": "rt-1", "status": "active",
                                "runtime_health": {"alive": True}}
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._detect_oracle_stuck", return_value=None), \
             patch("codeagent.oracle.MailboxStore", return_value=mock_store), \
             patch("codeagent.oracle._gc_throttle", return_value=False):
            assert cmd_oracle_status(_NS(review_key="k1")) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["receipts"] == [
            {"msg_id": "m-1", "reply_to": "r-1", "from": "oracle"},
        ]
        assert out["mailbox"]["unread"] == 1
        assert out["mailbox"]["latest_report"]["msg_id"] == "m-2"
        assert out["mailbox"]["recommendation"] == "read or ack REPORT before release"

    def test_status_stuck_reported(self, tmp_path, capsys):
        from codeagent.oracle import cmd_oracle_status

        gw = MagicMock()
        gw.call.return_value = {"runtime_id": "rt-1", "status": "active",
                                "runtime_health": {"alive": True}}
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._detect_oracle_stuck",
                   return_value={"detected": True, "signal": "strong",
                                 "detail": "stuck detail", "hint": "release+revive"}), \
             patch("codeagent.oracle._gc_throttle", return_value=False):
            assert cmd_oracle_status(_NS(review_key="k1")) == 0
        captured = capsys.readouterr()
        out = json.loads(captured.out)
        assert out["stuck"]["signal"] == "strong"
        assert "stuck detail" in captured.err

    def test_status_stuck_detection_error(self, tmp_path, capsys):
        from codeagent.oracle import cmd_oracle_status

        gw = MagicMock()
        gw.call.return_value = {"runtime_id": "rt-1", "status": "active",
                                "runtime_health": {"alive": True}}
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._detect_oracle_stuck",
                   side_effect=Exception("probe failed")), \
             patch("codeagent.oracle._gc_throttle", return_value=False):
            assert cmd_oracle_status(_NS(review_key="k1")) == 0
        assert "stuck" not in json.loads(capsys.readouterr().out)

    def test_status_snapshot_age(self, tmp_path, capsys):
        from codeagent.oracle import cmd_oracle_status
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        registry.acquire("k1", _manifest("k1", backend_session_id="b1"))
        gw = MagicMock()
        gw.call.side_effect = Exception("down")
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._snapshot_age_days", return_value=9.5), \
             patch("codeagent.oracle._gc_throttle", return_value=False):
            assert cmd_oracle_status(_NS(review_key="k1")) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["snapshot"] == {"age_days": 9.5, "stale": True,
                                   "threshold_days": 7}

    # ── cmd_oracle_list ─────────────────────────────────────────────

    def test_list_reviews_and_empty(self, tmp_path, capsys):
        from codeagent.oracle import cmd_oracle_list
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        registry.acquire("k1", _manifest("k1", backend_session_id="b1"))
        registry.acquire("k2", _manifest("k2", backend_session_id="b2"))
        with patch("codeagent.oracle._gc_throttle", return_value=False):
            assert cmd_oracle_list(_NS()) == 0
        out = json.loads(capsys.readouterr().out)
        assert [r["review_key"] for r in out["reviews"]] == ["k1", "k2"]
        assert out["reviews"][0]["lifecycle"] == "hot_parked"
        # empty registry
        registry.delete("k1")
        registry.delete("k2")
        with patch("codeagent.oracle._gc_throttle", return_value=False):
            assert cmd_oracle_list(_NS()) == 0
        assert json.loads(capsys.readouterr().out)["reviews"] == []

    # ── session-file / message extraction helpers ────────────────────

    def test_get_session_dir(self):
        from dataclasses import replace

        from codeagent.oracle import _get_session_dir

        assert _get_session_dir(_manifest("k1")) == ""
        m = replace(_manifest("k1"), omp_session_path="/a/b/c.jsonl")
        assert _get_session_dir(m) == "/a/b"

    def test_find_session_file_primary_and_fallback(self, tmp_path, monkeypatch):
        from codeagent.oracle import _find_session_file

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        root = tmp_path / ".omp" / "agent" / "sessions"
        assert _find_session_file("sid-1") is None
        (root / "proj-a").mkdir(parents=True)
        f1 = root / "proj-a" / "x_sid-1.jsonl"
        f1.write_text("a", encoding="utf-8")
        os.utime(f1, (1, 1))
        (root / "_oracle" / "legacy").mkdir(parents=True)
        f2 = root / "_oracle" / "legacy" / "y_sid-1.jsonl"
        f2.write_text("b", encoding="utf-8")
        os.utime(f2, (2, 2))
        # primary root wins when it has a match
        assert _find_session_file("sid-1") == f1
        # session_dir override: only that dir searched (same dir-as-root shape)
        custom = tmp_path / "custom-root"
        (custom / "proj").mkdir(parents=True)
        f3 = custom / "proj" / "z_sid-1.jsonl"
        f3.write_text("c", encoding="utf-8")
        assert _find_session_file("sid-1", session_dir=str(custom)) == f3
        assert _find_session_file("sid-2", session_dir=str(custom)) is None
        assert _find_session_file("sid-1", session_dir=str(tmp_path / "missing")) is None

    def test_find_session_file_oracle_fallback_only(self, tmp_path, monkeypatch):
        from codeagent.oracle import _find_session_file

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        root = tmp_path / ".omp" / "agent" / "sessions"
        (root / "_oracle" / "legacy").mkdir(parents=True)
        f = root / "_oracle" / "legacy" / "y_sid-9.jsonl"
        f.write_text("b", encoding="utf-8")
        assert _find_session_file("sid-9") == f

    def test_extract_assistant_messages(self, tmp_path):
        from codeagent.oracle import _extract_assistant_messages

        p = tmp_path / "s.jsonl"
        p.write_text("\n".join([
            json.dumps({"type": "message", "message": {"role": "user",
                        "content": [{"type": "text", "text": "u"}]}}),
            "{bad-json",
            json.dumps({"type": "event"}),
            json.dumps({"type": "message", "message": {"role": "assistant",
                        "content": [{"type": "text", "text": "a1"}]}}),
            json.dumps({"type": "message", "message": {"role": "assistant",
                        "content": [{"type": "text", "text": "a2"}]}}),
            json.dumps({"type": "message", "message": {"role": "assistant",
                        "content": [{"type": "text", "text": "   "}]}}),
        ]) + "\n", encoding="utf-8")
        assert _extract_assistant_messages(p) == ["a2"]
        assert _extract_assistant_messages(p, max_messages=5) == ["a1", "a2"]
        assert _extract_assistant_messages(tmp_path / "absent.jsonl") == []
        d = tmp_path / "adir"
        d.mkdir()
        assert _extract_assistant_messages(d) == []

    def test_review_start_ts_sources(self, tmp_path, monkeypatch):
        from codeagent.oracle import _review_start_ts, _write_oracle_meta

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        m = _manifest("k1", backend_session_id="b1")
        assert _review_start_ts("k1", m) == float(m.created_at)
        _write_oracle_meta("k1", "b1", "bound")
        ts = _review_start_ts("k1", m)
        assert ts is not None and ts > 1e9
        meta = tmp_path / ".omp" / "oracle" / "k1" / "meta.json"
        meta.write_text(json.dumps({"bound_at": "not-a-date"}), encoding="utf-8")
        assert _review_start_ts("k1", m) == float(m.created_at)
        assert _review_start_ts("k2", None) is None

    def test_review_reply_to_candidates(self):
        import hashlib as _h

        from codeagent.oracle import _review_reply_to_candidates

        rk = "proj:oracle:gfx:blur"
        keys = _review_reply_to_candidates(rk)
        assert rk in keys
        assert "proj-oracle-gfx-blur" in keys
        assert rk.replace(":", "-")[-12:] in keys
        assert _h.sha256(rk.encode()).hexdigest()[:12] in keys

    def test_scan_mailbox_report_known_session(self, tmp_path, monkeypatch):
        from codeagent.oracle import _scan_mailbox_report

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        m = _manifest("k1", backend_session_id="b1")
        mock_store = MagicMock()
        mock_store.read_history.return_value = [
            {"kind": "REPORT", "reply_to": "k1", "body": "report-body-1"},
            {"kind": "REPORT", "reply_to": "other", "body": "wrong"},
            {"kind": "PROGRESS", "reply_to": "k1", "body": "not-report"},
        ]
        with patch("codeagent.oracle.MailboxStore", return_value=mock_store):
            assert _scan_mailbox_report("k1", m) == "report-body-1"
        mock_store.read_history.assert_called_once_with(m.swarm_session_id, kind="REPORT")

    def test_scan_mailbox_report_fallback_walk(self, tmp_path, monkeypatch):
        from codeagent.oracle import _scan_mailbox_report
        from codeagent.mailbox.store import resolve_root

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        hist = resolve_root() / "some-session" / "oracle" / "history"
        hist.mkdir(parents=True)
        (hist / "1.json").write_text(json.dumps({
            "kind": "REPORT", "reply_to": "proj-oracle-gfx-blur", "body": "fb-body",
        }), encoding="utf-8")
        mock_store = MagicMock()
        mock_store.read_history.side_effect = Exception("no sessions")
        with patch("codeagent.oracle.MailboxStore", return_value=mock_store):
            assert _scan_mailbox_report("proj:oracle:gfx:blur", None) == "fb-body"

    def test_scan_mailbox_report_none(self, tmp_path, monkeypatch):
        from codeagent.oracle import _scan_mailbox_report

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        mock_store = MagicMock()
        mock_store.read_history.return_value = []
        with patch("codeagent.oracle.MailboxStore", return_value=mock_store):
            assert _scan_mailbox_report("k1", _manifest("k1")) is None

    def test_truncate_result_boundaries(self):
        from codeagent.oracle import _truncate_result

        text = "x" * 100
        out, was, tb, total = _truncate_result(text, 50)
        assert was is True and total == 100
        assert len(out.encode("utf-8")) == tb <= 50
        # line boundary preferred in last 30%
        t2 = "a" * 80 + "\n" + "b" * 80
        out2, was2, _, total2 = _truncate_result(t2, 100)
        assert was2 and total2 == 161
        assert out2 == "a" * 80
        # whitespace fallback
        t3 = "a" * 80 + " " + "b" * 80
        out3, was3, _, _ = _truncate_result(t3, 100)
        assert was3 and out3 == "a" * 80
        # multibyte-safe: total counts bytes
        t4 = "é" * 100
        out4, was4, tb4, total4 = _truncate_result(t4, 150)
        assert was4 and total4 == 200
        assert len(out4.encode("utf-8")) == tb4
        # no truncation when within budget / max_bytes <= 0
        assert _truncate_result("short", 100) == ("short", False, 0, 5)
        assert _truncate_result("short", 0) == ("short", False, 0, 5)
        assert _truncate_result("short", -1) == ("short", False, 0, 5)

    def test_trunc_notice(self):
        from codeagent.oracle import _trunc_notice

        assert _trunc_notice("t", True, 10, 20) == "t\n\n…[truncated 10/20 bytes]"
        assert _trunc_notice("t", False, 0, 5) == "t"

    # ── cmd_oracle_result paths ──────────────────────────────────────

    def test_result_session_transcript(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import cmd_oracle_result

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        session = tmp_path / "session.jsonl"
        session.write_text(json.dumps({"type": "message", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "final answer"}]}}) + "\n",
            encoding="utf-8")
        ns = _NS(review_key="k1", strict=False, all=False, raw=False,
                 include_digest=False)
        with patch("codeagent.oracle._resolve_bound_session_id",
                   return_value="ses_123"), \
             patch("codeagent.oracle._find_session_file", return_value=session), \
             patch("codeagent.oracle._strip_running_session") as strip:
            assert cmd_oracle_result(ns) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["source"] == "session_transcript"
        assert out["confidence"] == 0.95
        assert out["messages"] == ["final answer"]
        assert out["truncated"] is False
        assert out["meta"]["session_id"] == "ses_123"
        strip.assert_called_once_with("ses_123")

    def test_result_transcript_trunc_env(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import cmd_oracle_result

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        monkeypatch.setenv("ORACLE_RESULT_MAX_BYTES", "10")
        session = tmp_path / "session.jsonl"
        session.write_text(json.dumps({"type": "message", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "x" * 50}]}}) + "\n",
            encoding="utf-8")
        ns = _NS(review_key="k1", strict=False, all=False, raw=False,
                 include_digest=False)
        with patch("codeagent.oracle._resolve_bound_session_id",
                   return_value="ses_123"), \
             patch("codeagent.oracle._find_session_file", return_value=session):
            assert cmd_oracle_result(ns) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["truncated"] is True
        assert out["hint"] == "use --all for full result"
        assert out["total_bytes"] == 50

    def test_result_raw_mode(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import cmd_oracle_result

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        session = tmp_path / "session.jsonl"
        session.write_text(json.dumps({"type": "message", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "raw text"}]}}) + "\n",
            encoding="utf-8")
        ns = _NS(review_key="k1", strict=False, all=False, raw=True,
                 include_digest=False)
        with patch("codeagent.oracle._resolve_bound_session_id",
                   return_value="ses_123"), \
             patch("codeagent.oracle._find_session_file", return_value=session):
            assert cmd_oracle_result(ns) == 0
        assert capsys.readouterr().out == "raw text\n"

    def test_result_all_and_include_digest(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import cmd_oracle_result

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        session = tmp_path / "session.jsonl"
        lines = []
        for t in ("first", "second"):
            lines.append(json.dumps({"type": "message", "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": t}]}}))
        session.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ns = _NS(review_key="k1", strict=False, all=True, raw=False,
                 include_digest=True)
        with patch("codeagent.oracle._resolve_bound_session_id",
                   return_value="ses_123"), \
             patch("codeagent.oracle._find_session_file", return_value=session), \
             patch("codeagent.oracle._load_advisor_digest",
                   return_value={"conclusion": "digest-c"}):
            assert cmd_oracle_result(ns) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["messages"] == ["first\nsecond"]
        assert out["truncated"] is False
        assert out["advisor_digest"] == {"conclusion": "digest-c"}

    def test_result_mailbox_report(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import cmd_oracle_result

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        ns = _NS(review_key="k1", strict=False, all=False, raw=False,
                 include_digest=False)
        with patch("codeagent.oracle._resolve_bound_session_id", return_value=""), \
             patch("codeagent.oracle._scan_mailbox_report", return_value="report body"):
            assert cmd_oracle_result(ns) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["source"] == "mailbox_report"
        assert out["confidence"] == 0.9
        assert out["messages"] == ["report body"]

    def test_result_filesystem_fallback(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import cmd_oracle_result

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        session = tmp_path / "fs.jsonl"
        session.write_text(json.dumps({"type": "message", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "fs answer"}]}}) + "\n",
            encoding="utf-8")
        ns = _NS(review_key="k1", strict=False, all=False, raw=False,
                 include_digest=False)
        with patch("codeagent.oracle._resolve_bound_session_id", return_value=""), \
             patch("codeagent.oracle._scan_mailbox_report", return_value=None), \
             patch("codeagent.oracle._fallback_find_session_for_key",
                   return_value=session):
            assert cmd_oracle_result(ns) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["source"] == "filesystem"
        assert out["confidence"] == 0.7
        assert out["messages"] == ["fs answer"]

    def test_result_strict_nothing_returns_1(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import cmd_oracle_result

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        ns = _NS(review_key="k1", strict=True, all=False, raw=False,
                 include_digest=False)
        with patch("codeagent.oracle._resolve_bound_session_id", return_value="ses_x"), \
             patch("codeagent.oracle._scan_mailbox_report", return_value=None):
            assert cmd_oracle_result(ns) == 1
        err = json.loads(capsys.readouterr().err)
        assert err["error"] == "no_result"
        assert "strict mode" in err["detail"]

    def test_result_no_bound_sid_detail(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import cmd_oracle_result

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        ns = _NS(review_key="k1", strict=False, all=False, raw=False,
                 include_digest=False)
        with patch("codeagent.oracle._resolve_bound_session_id", return_value=""), \
             patch("codeagent.oracle._scan_mailbox_report", return_value=None):
            assert cmd_oracle_result(ns) == 1
        err = json.loads(capsys.readouterr().err)
        assert "oracle start 后才有" in err["detail"]

    # ── cmd_oracle_watch ─────────────────────────────────────────────

    def test_watch_delegates(self, tmp_path, capsys):
        from codeagent.oracle import cmd_oracle_watch

        ns = _NS(review_key="k1", cursor="c-1", interval=2, timeout=30)
        gw = MagicMock()
        gw.call.return_value = {"runtime_id": "rt-1", "session_id": "ses-1"}
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.gateway.cli.cmd_events_watch", return_value=7) as cew:
            assert cmd_oracle_watch(ns) == 7
        sub = cew.call_args[0][0]
        assert sub.runtime_id == "rt-1"
        assert sub.session == "ses-1"
        assert sub.cursor == "c-1"
        assert sub.limit == 200

    def test_watch_gateway_error(self, tmp_path, capsys):
        from codeagent.gateway.model import GatewayError
        from codeagent.oracle import cmd_oracle_watch

        ns = _NS(review_key="k1", cursor="", interval=2, timeout=30)
        gw = MagicMock()
        gw.call.side_effect = GatewayError("GATEWAY_DOWN", "socket")
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.gateway.cli.cmd_events_watch") as cew:
            assert cmd_oracle_watch(ns) == 1
        cew.assert_not_called()
        err = json.loads(capsys.readouterr().err)
        assert err["error"] == "GATEWAY_DOWN"

    def test_snapshot_age_days_missing(self):
        from codeagent.oracle import _snapshot_age_days

        assert _snapshot_age_days(_manifest("k1")) == -1.0

    # ── _wait_for_new_output / cmd_oracle_wait ───────────────────────

    def test_wait_new_output_boot_agent_end(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import _wait_for_new_output

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        gw = MagicMock()
        gw.call.return_value = {"events": [
            {"kind": "TASK_STATE", "payload": {"state": "agent_end"}},
        ], "cursor": 9}
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._wait_final_text", return_value="final text"):
            assert _wait_for_new_output("k1", "rt-1", "ses-1", timeout=0,
                                        info={}) == 0
        assert capsys.readouterr().out == "final text\n"

    def test_wait_new_output_assistant_progress(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import _wait_for_new_output

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        gw = MagicMock()
        gw.call.return_value = {"events": [
            {"kind": "ASSISTANT_PROGRESS", "payload": {"text": "thinking"}},
        ], "cursor": 3}
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._wait_final_text", return_value="new output"):
            assert _wait_for_new_output("k1", "rt-1", "ses-1", timeout=0,
                                        info={}) == 0
        assert capsys.readouterr().out == "new output\n"

    def test_wait_new_output_agent_end_no_text(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import _wait_for_new_output

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        gw = MagicMock()
        gw.call.return_value = {"events": [
            {"kind": "TASK_STATE", "payload": {"state": "agent_end"}},
        ], "cursor": 1}
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._wait_final_text", return_value=None):
            assert _wait_for_new_output("k1", "rt-1", "ses-1", timeout=0,
                                        info={}) == 0
        assert capsys.readouterr().out == ""

    def test_wait_new_output_gateway_error(self, tmp_path, monkeypatch, capsys):
        from codeagent.gateway.model import GatewayError
        from codeagent.oracle import _wait_for_new_output

        gw = MagicMock()
        gw.call.side_effect = GatewayError("GATEWAY_DOWN", "socket")
        with patch("codeagent.oracle._gateway", return_value=gw):
            assert _wait_for_new_output("k1", "rt-1", "ses-1", timeout=0,
                                        info={}) == 1
        err = json.loads(capsys.readouterr().out)
        assert err["status"] == "error"
        assert err["error"] == "GATEWAY_DOWN"

    def test_wait_new_output_timeout(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import _wait_for_new_output

        gw = MagicMock()
        gw.call.return_value = {"events": [], "cursor": 0}
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle.time.sleep"):
            assert _wait_for_new_output("k1", "rt-1", "ses-1", timeout=0,
                                        info={}) == 1
        err = json.loads(capsys.readouterr().out)
        assert err["status"] == "timeout"
        assert "use oracle result" in err["suggestion"]

    def test_wait_new_output_stuck_returns_1(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import _wait_for_new_output

        gw = MagicMock()
        gw.call.side_effect = [
            {"events": [], "cursor": 0},  # boot
            {"events": [], "cursor": 0},  # poll 1
            {"events": [], "cursor": 0},  # poll 2
            {"events": [], "cursor": 0},  # poll 3
            {"runtime_id": "rt-1", "status": "active"},  # info refetch
        ]
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle.time.sleep"), \
             patch("codeagent.oracle._detect_oracle_stuck",
                   return_value={"detected": True, "signal": "strong",
                                 "hint": "release+revive"}):
            assert _wait_for_new_output("k1", "rt-1", "ses-1", timeout=300,
                                        info={}) == 1
        err = json.loads(capsys.readouterr().out)
        assert err["status"] == "stuck"

    def test_wait_new_output_keyboard_interrupt(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import _wait_for_new_output

        gw = MagicMock()
        gw.call.return_value = {"events": [], "cursor": 0}
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle.time.sleep", side_effect=KeyboardInterrupt):
            assert _wait_for_new_output("k1", "rt-1", "ses-1", timeout=300,
                                        info={}) == 130

    def test_wait_new_output_auto_recover_post_revive_fail(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import _wait_for_new_output
        from codeagent.park.registry import ParkRegistry

        ParkRegistry().acquire("k1", _manifest("k1", backend_session_id="b1"))
        gw = MagicMock()
        gw.call.side_effect = [
            {"events": [], "cursor": 0},  # boot
            {"events": [], "cursor": 0},  # poll 1
            {"events": [], "cursor": 0},  # poll 2
            {"events": [], "cursor": 0},  # poll 3
            {"runtime_id": "rt-1", "status": "active"},  # info refetch
            {"runtime_id": "rt-1", "status": "active"},  # release step info
            {},  # runtime.stop
            {},  # runtime.purge_stopped
            {"runtime_id": "rt-1", "status": "stopped"},  # post-revive refresh
        ]
        decision = MagicMock()
        decision.method = "warm"
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle.time.sleep"), \
             patch("codeagent.oracle._detect_oracle_stuck",
                   return_value={"detected": True, "signal": "strong",
                                 "hint": "release+revive"}), \
             patch("codeagent.park.router.revive_or_spawn", return_value=decision), \
             patch("codeagent.oracle._revive_warm", return_value=("rt-9", "b9", ["m"])):
            assert _wait_for_new_output("k1", "rt-1", "ses-1", timeout=300,
                                        info={}, auto_recover=True) == 1
        out_text = capsys.readouterr().out
        assert '"status": "recovering"' in out_text
        assert '"status": "recover_failed"' in out_text
        assert '"phase": "post_revive"' in out_text

    def test_cmd_wait_routes_to_shared(self, tmp_path, capsys):
        from codeagent.oracle import cmd_oracle_wait

        info = {"runtime_id": "rt-1", "session_id": "ses-1", "status": "active",
                "backend_session_id": "b1", "runtime_health": {"alive": True}}
        gw = MagicMock()
        gw.call.return_value = info
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._wait_for_new_output", return_value=0) as w:
            assert cmd_oracle_wait(_NS(review_key="k1", interval=1, timeout=30,
                                       all=False, auto_recover=True)) == 0
        w.assert_called_once_with("k1", "rt-1", "ses-1", timeout=30.0,
                                  interval=1.0, max_bytes=32768, info=info,
                                  auto_recover=True, session_dir="")

    def test_cmd_wait_gateway_error(self, tmp_path, capsys):
        from codeagent.gateway.model import GatewayError
        from codeagent.oracle import cmd_oracle_wait

        gw = MagicMock()
        gw.call.side_effect = GatewayError("GATEWAY_DOWN", "socket")
        with patch("codeagent.oracle._gateway", return_value=gw):
            assert cmd_oracle_wait(_NS(review_key="k1", interval=1, timeout=30,
                                       all=False)) == 1
        err = json.loads(capsys.readouterr().out)
        assert err["error"] == "GATEWAY_DOWN"

    def test_cmd_wait_no_runtime(self, tmp_path, capsys):
        from codeagent.oracle import cmd_oracle_wait

        gw = MagicMock()
        gw.call.return_value = {"runtime_id": ""}
        with patch("codeagent.oracle._gateway", return_value=gw):
            assert cmd_oracle_wait(_NS(review_key="k1", interval=1, timeout=30,
                                       all=False)) == 1
        err = json.loads(capsys.readouterr().out)
        assert err["error"] == "NO_RUNTIME"

    def test_cmd_wait_not_active(self, tmp_path, capsys):
        from codeagent.oracle import cmd_oracle_wait

        gw = MagicMock()
        gw.call.return_value = {"runtime_id": "rt-1",
                                "runtime_health": {"alive": False}}
        with patch("codeagent.oracle._gateway", return_value=gw):
            assert cmd_oracle_wait(_NS(review_key="k1", interval=1, timeout=30,
                                       all=False)) == 1
        err = json.loads(capsys.readouterr().out)
        assert err["error"] == "NOT_ACTIVE"

    def test_cmd_wait_binding_pending_continues(self, tmp_path, capsys):
        from codeagent.oracle import cmd_oracle_wait

        info = {"runtime_id": "rt-1", "session_id": "ses-1", "status": "active",
                "backend_session_id": "", "runtime_health": {"alive": True}}
        gw = MagicMock()
        gw.call.return_value = info
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._wait_for_new_output", return_value=5) as w:
            assert cmd_oracle_wait(_NS(review_key="k1", interval=1, timeout=30,
                                       all=False)) == 5
        w.assert_called_once()
        assert "binding_pending" in capsys.readouterr().out

    # ── _tmux_kill_oracle_runtime ────────────────────────────────────

    def test_tmux_kill_pid_and_pane(self, tmp_path, monkeypatch):
        from codeagent.oracle import _review_sid, _tmux_kill_oracle_runtime

        runtime_root = (Path(os.environ["XDG_STATE_HOME"])
                        / "aimeshchat" / "runtime" / "rt-1")
        runtime_root.mkdir(parents=True)
        (runtime_root / "spec.json").write_text(json.dumps({
            "review_key": "k1", "runtime_id": "rt-1"}), encoding="utf-8")
        (runtime_root / "rt-1.pid").write_text("4242\n", encoding="utf-8")

        def fake_kill(pid, sig):
            if sig == 0:
                raise ProcessLookupError(3, "no process")
            return None

        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = f"{_review_sid('k1')}|%1\nora-k1-legacy|%2\n"
        monkeypatch.setattr("codeagent.launchers.tmux.tmux_cmd",
                            lambda *a: ["tmux", *a])
        with patch("codeagent.oracle.os.kill", side_effect=fake_kill), \
             patch("codeagent.oracle.subprocess.run", return_value=proc), \
             patch("codeagent.launchers.tmux.kill_pane", return_value=True) as kp:
            killed, targets = _tmux_kill_oracle_runtime("k1")
        assert killed is True
        assert "pid:4242" in targets
        assert any(t.startswith("pane:") for t in targets)
        assert kp.call_count == 2

    def test_tmux_kill_no_targets(self, tmp_path, monkeypatch):
        from codeagent.oracle import _tmux_kill_oracle_runtime

        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "unrelated|%1\n"
        monkeypatch.setattr("codeagent.launchers.tmux.tmux_cmd",
                            lambda *a: ["tmux", *a])
        with patch("codeagent.oracle.subprocess.run", return_value=proc), \
             patch("codeagent.launchers.tmux.kill_pane", return_value=True):
            killed, targets = _tmux_kill_oracle_runtime("k1")
        assert killed is False
        assert targets == []

    def test_tmux_kill_scan_error(self, tmp_path, monkeypatch):
        from codeagent.oracle import _tmux_kill_oracle_runtime

        monkeypatch.setattr("codeagent.launchers.tmux.tmux_cmd",
                            lambda *a: ["tmux", *a])
        with patch("codeagent.oracle.subprocess.run",
                   side_effect=FileNotFoundError("no tmux")), \
             patch("codeagent.launchers.tmux.kill_pane", return_value=True):
            killed, targets = _tmux_kill_oracle_runtime("k1")
        assert killed is False
        assert targets == []

    # ── opencode.db purge / strip ────────────────────────────────────

    def _make_opencode_db(self, tmp_path, monkeypatch, sid="ses_111",
                          events=((1, "session.created"), (5, "session.next.agent.switched.1"))):
        import sqlite3 as _sqlite

        db = tmp_path / "opencode.db"
        conn = _sqlite.connect(db)
        conn.execute("CREATE TABLE session (id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE event_sequence (aggregate_id TEXT PRIMARY KEY, seq INTEGER)")
        conn.execute("CREATE TABLE event (aggregate_id TEXT, seq INTEGER, type TEXT)")
        conn.execute("INSERT INTO session VALUES (?)", (sid,))
        conn.execute("INSERT INTO event_sequence VALUES (?, 5)", (sid,))
        for seq, typ in events:
            conn.execute("INSERT INTO event VALUES (?, ?, ?)", (sid, seq, typ))
        conn.commit()
        conn.close()
        monkeypatch.setattr("codeagent.oracle._OPENCODE_DB_PATH", db)
        return db

    def test_purge_opencode_hard(self, tmp_path, monkeypatch):
        import sqlite3 as _sqlite

        from codeagent.oracle import _purge_opencode_session

        db = self._make_opencode_db(tmp_path, monkeypatch)
        r = _purge_opencode_session("ses_111")
        assert r["deleted_session"] is True
        assert r["deleted_events"] is True
        assert r["error"] is None
        assert Path(r["backup"]).exists()
        conn = _sqlite.connect(db)
        assert conn.execute("SELECT 1 FROM session WHERE id='ses_111'").fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM event_sequence WHERE aggregate_id='ses_111'").fetchone() is None
        conn.close()

    def test_purge_opencode_strip_only(self, tmp_path, monkeypatch):
        import sqlite3 as _sqlite

        from codeagent.oracle import _purge_opencode_session

        db = self._make_opencode_db(tmp_path, monkeypatch)
        r = _purge_opencode_session("ses_111", strip_only=True)
        assert r["deleted_session"] is False
        assert r["deleted_events"] is True
        conn = _sqlite.connect(db)
        assert conn.execute("SELECT 1 FROM session WHERE id='ses_111'").fetchone() is not None
        assert conn.execute(
            "SELECT 1 FROM event_sequence WHERE aggregate_id='ses_111'").fetchone() is None
        conn.close()

    def test_purge_opencode_missing_db_and_empty_sid(self, tmp_path, monkeypatch):
        from codeagent.oracle import _purge_opencode_session

        monkeypatch.setattr("codeagent.oracle._OPENCODE_DB_PATH",
                            tmp_path / "nope.db")
        r = _purge_opencode_session("ses_1")
        assert r["error"] == "opencode.db not found"
        self._make_opencode_db(tmp_path, monkeypatch)
        r2 = _purge_opencode_session("")
        assert r2["error"] == "empty backend_session_id"

    def test_purge_opencode_backup_failure_continues(self, tmp_path, monkeypatch):
        from codeagent.oracle import _purge_opencode_session

        self._make_opencode_db(tmp_path, monkeypatch)
        with patch("codeagent.oracle.shutil.copy2",
                   side_effect=OSError("disk full")):
            r = _purge_opencode_session("ses_111")
        assert r["deleted_session"] is True
        assert r["backup"].startswith("failed:")

    def test_is_turn_completed(self):
        from codeagent.oracle import _is_turn_completed

        events = [
            {"seq": 1, "type": "session.created"},
            {"seq": 2, "type": "tool.a"},
            {"seq": 6, "type": "session.next.agent.switched.1"},
        ]
        assert _is_turn_completed(events, 0) is True
        assert _is_turn_completed(events, 1) is True
        assert _is_turn_completed(events, 2) is False
        assert _is_turn_completed(events, 9) is False
        assert _is_turn_completed([], 0) is False

    def test_strip_running_session_trims(self, tmp_path, monkeypatch):
        import sqlite3 as _sqlite

        from codeagent.oracle import _strip_running_session

        events = (
            (1, "session.created"),
            (2, "tool.a"),
            (3, "tool.b"),
            (6, "session.next.agent.switched.1"),
            (7, "tool.c"),
            (12, "session.next.model.switched.1"),
            (13, "tool.d"),
            (14, "tool.e"),
        )
        db = self._make_opencode_db(tmp_path, monkeypatch, events=events)
        r = _strip_running_session("ses_111")  # keep_recent=2
        assert r["error"] is None
        assert r["trimmed"] == 3  # seq 1,2,3 removed
        assert r["kept"] == 5
        conn = _sqlite.connect(db)
        rows = conn.execute(
            "SELECT seq FROM event WHERE aggregate_id='ses_111'").fetchall()
        conn.close()
        assert [x[0] for x in rows] == [6, 7, 12, 13, 14]

    def test_strip_running_session_keeps_all(self, tmp_path, monkeypatch):
        from codeagent.oracle import _strip_running_session

        self._make_opencode_db(tmp_path, monkeypatch)
        r = _strip_running_session("ses_111")
        assert r["trimmed"] == 0
        assert r["kept"] == 2

    def test_strip_running_session_errors(self, tmp_path, monkeypatch):
        from codeagent.oracle import _strip_running_session

        monkeypatch.setattr("codeagent.oracle._OPENCODE_DB_PATH",
                            tmp_path / "nope.db")
        assert _strip_running_session("ses_1")["error"] == "opencode.db not found"
        self._make_opencode_db(tmp_path, monkeypatch)
        assert _strip_running_session("")["error"] == "empty backend_session_id"

    def test_lazy_db_cleanup_guards(self, tmp_path, monkeypatch):
        from codeagent.oracle import _lazy_db_cleanup

        monkeypatch.setattr("codeagent.oracle._OPENCODE_DB_PATH",
                            tmp_path / "nope.db")
        assert _lazy_db_cleanup() == 0
        small = tmp_path / "small.db"
        small.write_bytes(b"x" * 1024)
        monkeypatch.setattr("codeagent.oracle._OPENCODE_DB_PATH", small)
        assert _lazy_db_cleanup() == 0
        big = tmp_path / "big.db"
        with open(big, "wb") as f:
            f.truncate(101 * 1024 * 1024)  # sparse 101MB
        monkeypatch.setattr("codeagent.oracle._OPENCODE_DB_PATH", big)
        assert _lazy_db_cleanup() == 0  # query error (no released_at col) → 0

    def test_cleanup_opencode_on_release_sources(self, tmp_path, monkeypatch):
        from codeagent.oracle import (_cleanup_opencode_session_on_release,
                                      _write_oracle_meta)

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        self._make_opencode_db(tmp_path, monkeypatch, sid="ses_222")
        # no sid anywhere → skipped
        r = _cleanup_opencode_session_on_release("k1", _manifest("k1", backend_session_id=""),
                                                 strip_only=True)
        assert r == {"skipped": "no backend_session_id"}
        # meta sid
        _write_oracle_meta("k1", "ses_222", "bound")
        r2 = _cleanup_opencode_session_on_release("k1", _manifest("k1", backend_session_id=""),
                                                  strip_only=False)
        assert r2["backend_session_id"] == "ses_222"
        assert r2["deleted_session"] is True
        assert r2["strip_only"] is False
        # non-opencode sid → skipped
        r3 = _cleanup_opencode_session_on_release("k1", _manifest("k1", backend_session_id="native-1"),
                                                  strip_only=True)
        assert "not an opencode session" in r3["skipped"]

    def test_cleanup_opencode_gateway_fallback(self, tmp_path, monkeypatch):
        from codeagent.oracle import _cleanup_opencode_session_on_release

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        self._make_opencode_db(tmp_path, monkeypatch, sid="ses_333")
        gw = MagicMock()
        gw.call.return_value = {"backend_session_id": "ses_333"}
        with patch("codeagent.oracle._gateway", return_value=gw):
            r = _cleanup_opencode_session_on_release(
                "k1", _manifest("k1", backend_session_id=""), strip_only=True)
        assert r["backend_session_id"] == "ses_333"
        assert r["strip_only"] is True

    # ── GC ───────────────────────────────────────────────────────────

    def test_gc_meta_roundtrip_and_throttle(self, tmp_path, monkeypatch):
        from codeagent.oracle import (_gc_meta_path, _gc_read_meta,
                                      _gc_throttle, _gc_update_timestamp,
                                      _gc_write_meta)

        assert _gc_throttle() is True
        _gc_update_timestamp()
        assert _gc_throttle() is False
        assert "last_gc_at" in _gc_read_meta()
        # corrupt meta → treated as empty
        _gc_meta_path().write_text("{bad", encoding="utf-8")
        assert _gc_read_meta() == {}
        assert _gc_throttle() is True
        # write failure tolerated (p.write_text OSError is caught)
        d = tmp_path / "gcdir"
        d.mkdir()
        monkeypatch.setattr("codeagent.oracle._gc_meta_path",
                            lambda: d / "gc.json")
        d2 = tmp_path / "gc.json"
        d2.mkdir()
        monkeypatch.setattr("codeagent.oracle._gc_meta_path", lambda: d2)
        _gc_write_meta({"a": 1})  # must not raise (write to dir → OSError)

    def test_cmd_oracle_gc_empty(self, capsys):
        from codeagent.oracle import cmd_oracle_gc

        assert cmd_oracle_gc(_NS(dry_run=False, json=False)) == 0
        assert "no released sessions" in capsys.readouterr().err

    def test_cmd_oracle_gc_expired_cleans(self, tmp_path, monkeypatch, capsys):
        import time
        from dataclasses import replace

        from codeagent.oracle import cmd_oracle_gc
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        m = replace(_manifest("k-gc", backend_session_id=""),
                    hard_expires_at=time.time() - 100,
                    soft_expires_at=time.time() - 100)
        registry.acquire("k-gc", m)
        registry.release("k-gc")  # released_soft
        with patch("codeagent.oracle._purge_omp_session",
                   return_value=["/sessions/x"]) as po, \
             patch("codeagent.oracle._purge_opencode_session",
                   return_value={"error": None}):
            assert cmd_oracle_gc(_NS(dry_run=False, json=False)) == 0
        po.assert_called_once()
        assert registry.lookup("k-gc") is None  # row deleted
        assert "cleaned=1" in capsys.readouterr().err

    def test_cmd_oracle_gc_dry_run_json(self, tmp_path, monkeypatch, capsys):
        import time
        from dataclasses import replace

        from codeagent.oracle import cmd_oracle_gc
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        m = replace(_manifest("k-gc2", backend_session_id=""),
                    hard_expires_at=time.time() - 100)
        registry.acquire("k-gc2", m)
        registry.release("k-gc2")
        with patch("codeagent.oracle._purge_omp_session", return_value=[]):
            assert cmd_oracle_gc(_NS(dry_run=True, json=True)) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["cleaned"] == 1
        assert registry.lookup("k-gc2") is not None  # dry-run keeps row

    def test_cmd_oracle_gc_stale_by_activity(self, tmp_path, monkeypatch, capsys):
        import time
        from dataclasses import replace

        from codeagent.oracle import cmd_oracle_gc
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        m = replace(_manifest("k-stale", backend_session_id=""),
                    hard_expires_at=0, soft_expires_at=0,
                    last_activity_at=time.time() - 3 * 86400)
        registry.acquire("k-stale", m)
        registry.release("k-stale")
        assert cmd_oracle_gc(_NS(dry_run=False, json=False)) == 0
        assert registry.lookup("k-stale") is None
        assert "cleaned=1" in capsys.readouterr().err

    def test_cmd_oracle_gc_skips_fresh(self, tmp_path, capsys):
        from codeagent.oracle import cmd_oracle_gc
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        registry.acquire("k-fresh", _manifest("k-fresh", backend_session_id="b1"))
        registry.release("k-fresh")
        assert cmd_oracle_gc(_NS(dry_run=False, json=False)) == 0
        assert registry.lookup("k-fresh") is not None
        assert "no expired sessions found" in capsys.readouterr().err

    def test_cmd_oracle_gc_corrupt_manifest(self, capsys):
        from codeagent.oracle import cmd_oracle_gc
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        with registry._connect() as conn:
            conn.execute(
                "INSERT INTO park_leases (key, manifest_json, lifecycle) "
                "VALUES ('k-bad', '{not-json', 'released_soft')")
            conn.commit()
        assert cmd_oracle_gc(_NS(dry_run=False, json=False)) == 1
        assert "corrupt manifest" in capsys.readouterr().err

    def test_cmd_oracle_gc_scan_error_json(self, capsys):
        from codeagent.oracle import cmd_oracle_gc

        with patch("codeagent.oracle.ParkRegistry._connect",
                   side_effect=Exception("db locked")):
            assert cmd_oracle_gc(_NS(dry_run=False, json=True)) == 1
        out = json.loads(capsys.readouterr().out)
        assert out["failed"] == 1
        assert "db locked" in out["errors"][0]

    def test_run_gc_silent(self):
        from codeagent.oracle import _run_gc_silent

        with patch("codeagent.oracle.cmd_oracle_gc", return_value=0) as gc:
            _run_gc_silent()
        args = gc.call_args[0][0]
        assert args.dry_run is False and args.json is False
        with patch("codeagent.oracle.cmd_oracle_gc", side_effect=Exception("boom")):
            _run_gc_silent()  # must not raise

    # ── advisor digest ───────────────────────────────────────────────

    def test_find_advisor_session_file(self, tmp_path, monkeypatch):
        from codeagent.oracle import _find_advisor_session_file

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        root = tmp_path / ".omp" / "agent" / "sessions"
        (root / "proj").mkdir(parents=True)
        main = root / "proj" / "x_ses_1.jsonl"
        main.write_text("m", encoding="utf-8")
        adv = root / "proj" / "__advisor_ses_1.jsonl"
        adv.write_text("a", encoding="utf-8")
        assert _find_advisor_session_file("") is None
        assert _find_advisor_session_file("ses_1") == adv
        assert _find_advisor_session_file("ses_1", session_dir=str(root / "proj")) == adv
        # fallback recursive scan when main file absent
        (root / "other").mkdir()
        adv2 = root / "other" / "__advisor_ses_2.jsonl"
        adv2.write_text("x", encoding="utf-8")
        assert _find_advisor_session_file("ses_2") == adv2

    def test_extract_advisor_digest(self, tmp_path):
        from codeagent.oracle import _extract_advisor_digest

        p = tmp_path / "advisor.jsonl"
        lines = [
            json.dumps({"type": "message", "message": {"role": "assistant",
                        "content": [{"type": "text", "text": "conclusion text"}]}}),
            json.dumps({"type": "message", "message": {"role": "toolResult",
                        "content": [{"type": "text", "text": "x" * 250}]}}),
            json.dumps({"type": "message", "message": {"role": "toolResult",
                        "content": [{"type": "text", "text": "short"}]}}),
            json.dumps({"type": "tool_result", "text": "y" * 250}),
            "{bad-json",
        ]
        p.write_text("\n".join(lines), encoding="utf-8")
        d = _extract_advisor_digest(p)
        assert d["conclusion"] == "conclusion text"
        assert d["evidence_count"] == 2
        assert d["token_estimate"] > 0
        assert d["source"] == str(p)
        # OSError → error digest
        err = _extract_advisor_digest(tmp_path / "absent.jsonl")
        assert err["error"]
        # truncation to max_bytes
        long = tmp_path / "long.jsonl"
        long.write_text(json.dumps({"type": "message", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "z" * 100}]}}), encoding="utf-8")
        t = _extract_advisor_digest(long, max_bytes=10)
        assert len(t["conclusion"].encode("utf-8")) <= 10

    def test_save_load_advisor_digest(self, tmp_path, monkeypatch):
        from dataclasses import replace

        from codeagent.oracle import (_load_advisor_digest,
                                      _save_advisor_digest)

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        assert _save_advisor_digest("k1", None) is None
        assert _save_advisor_digest("k1", _manifest("k1", backend_session_id="")) is None
        assert _save_advisor_digest("k1", _manifest("k1", backend_session_id="ses_1")) is None
        root = tmp_path / ".omp" / "agent" / "sessions"
        (root / "proj").mkdir(parents=True)
        (root / "proj" / "x_ses_1.jsonl").write_text("m", encoding="utf-8")
        adv = root / "proj" / "__advisor_ses_1.jsonl"
        adv.write_text(json.dumps({"type": "message", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "adv conclusion"}]}}),
            encoding="utf-8")
        m = replace(_manifest("k1", backend_session_id="ses_1"),
                    omp_session_path=str(root / "proj" / "x_ses_1.jsonl"))
        d = _save_advisor_digest("k1", m)
        assert d is not None
        assert d["conclusion"] == "adv conclusion"
        assert _load_advisor_digest("k1") == d
        digest_path = tmp_path / ".omp" / "oracle" / "k1" / "digest.json"
        digest_path.write_text("{bad", encoding="utf-8")
        assert _load_advisor_digest("k1") is None
        assert _load_advisor_digest("k-absent") is None
        # write failure → None
        blocker = tmp_path / "blocker"
        blocker.write_text("file", encoding="utf-8")
        monkeypatch.setattr("codeagent.oracle._oracle_digest_path",
                            lambda _k: blocker / "digest.json")
        assert _save_advisor_digest("k1", m) is None

    # ── cmd_oracle_release paths ─────────────────────────────────────

    def test_release_unread_reports_guard(self, tmp_path, capsys):
        from codeagent.mailbox.store import MailboxStore
        from codeagent.oracle import cmd_oracle_release
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        registry.acquire("k1", _manifest("k1", backend_session_id="b1"))
        m = registry.lookup("k1")
        inbox = MailboxStore().agent_subdir(m.swarm_session_id, "oracle", "inbox")
        inbox.mkdir(parents=True)
        (inbox / "r1.json").write_text(json.dumps({
            "kind": "REPORT", "msg_id": "m1"}), encoding="utf-8")
        ns = _NS(review_key="k1", force=False, purge=False, keep_advisor=False,
                 prompt="")
        gw = MagicMock()
        gw.call.side_effect = _raise(Exception("no gateway"))
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("builtins.input", return_value="n"):
            assert cmd_oracle_release(ns) == 1
        out = json.loads(capsys.readouterr().out)
        assert out["release_aborted"] is True
        assert out["unread_reports"] == 1
        # guard confirmation "y" proceeds
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("builtins.input", return_value="y"), \
             patch("codeagent.oracle._strip_oracle_transcript",
                   return_value={"removed": []}), \
             patch("codeagent.oracle._save_advisor_digest", return_value=None), \
             patch("codeagent.oracle.Path.home", lambda: tmp_path):
            assert cmd_oracle_release(ns) == 0
        assert registry.lookup("k1").lifecycle == Lifecycle.RELEASED_SOFT

    def test_release_gateway_not_found_warning(self, tmp_path, monkeypatch, capsys):
        from codeagent.gateway.model import GatewayError
        from codeagent.oracle import cmd_oracle_release
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        registry.acquire("k1", _manifest("k1", backend_session_id="b1"))
        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        gw = MagicMock()
        gw.call.side_effect = GatewayError("NOT_FOUND", "no runtime")
        ns = _NS(review_key="k1", force=False, purge=False, keep_advisor=False,
                 prompt="")
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._strip_oracle_transcript",
                   return_value={"removed": []}), \
             patch("codeagent.oracle._save_advisor_digest", return_value=None):
            assert cmd_oracle_release(ns) == 0
        captured = capsys.readouterr()
        out = json.loads(captured.out)
        assert out["runtime_stopped"] is False
        assert out["runtime_leaked"] is False
        assert "runtime stop failed" in captured.err

    def test_release_gateway_down_tmux_fallback(self, tmp_path, monkeypatch, capsys):
        from codeagent.gateway.model import GatewayError
        from codeagent.oracle import cmd_oracle_release
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        registry.acquire("k1", _manifest("k1", backend_session_id="b1"))
        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        gw = MagicMock()
        gw.call.side_effect = GatewayError("GATEWAY_DOWN", "socket")
        ns = _NS(review_key="k1", force=False, purge=False, keep_advisor=False,
                 prompt="")
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._strip_oracle_transcript",
                   return_value={"removed": []}), \
             patch("codeagent.oracle._save_advisor_digest", return_value=None), \
             patch("codeagent.oracle._tmux_kill_oracle_runtime",
                   return_value=(True, ["pid:123"])) as tmux_kill:
            assert cmd_oracle_release(ns) == 0
        tmux_kill.assert_called_once_with("k1")
        out = json.loads(capsys.readouterr().out)
        assert out["runtime_stopped"] is True
        assert out["runtime_leaked"] is False

    def test_release_gateway_down_no_targets_leak(self, tmp_path, monkeypatch, capsys):
        from codeagent.gateway.model import GatewayError
        from codeagent.oracle import cmd_oracle_release
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        registry.acquire("k1", _manifest("k1", backend_session_id="b1"))
        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        gw = MagicMock()
        gw.call.side_effect = GatewayError("GATEWAY_CONNECT_FAILED", "refused")
        ns = _NS(review_key="k1", force=False, purge=False, keep_advisor=False,
                 prompt="")
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._strip_oracle_transcript",
                   return_value={"removed": []}), \
             patch("codeagent.oracle._save_advisor_digest", return_value=None), \
             patch("codeagent.oracle._tmux_kill_oracle_runtime",
                   return_value=(False, [])):
            assert cmd_oracle_release(ns) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["runtime_stopped"] is False
        assert out["runtime_leaked"] is True

    def test_release_no_manifest(self, capsys):
        from codeagent.oracle import cmd_oracle_release

        ns = _NS(review_key="k-nope", force=False, purge=False,
                 keep_advisor=False, prompt="")
        assert cmd_oracle_release(ns) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["park_released"] is False
        assert out["release_mode"] == "soft"

    def test_release_purge_path(self, tmp_path, monkeypatch, capsys):
        from codeagent.gateway.model import GatewayError
        from codeagent.oracle import cmd_oracle_release
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        registry.acquire("k1", _manifest("k1", backend_session_id="b1"))
        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        gw = MagicMock()
        gw.call.side_effect = GatewayError("NOT_FOUND", "no runtime")
        ns = _NS(review_key="k1", force=False, purge=True, keep_advisor=False,
                 prompt="")
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._save_advisor_digest", return_value=None), \
             patch("codeagent.oracle._purge_omp_session",
                   return_value=["/x.jsonl"]) as po, \
             patch("codeagent.oracle._cleanup_opencode_session_on_release",
                   return_value={"deleted": True}) as cl:
            assert cmd_oracle_release(ns) == 0
        po.assert_called_once()
        assert po.call_args.kwargs["skip_advisor"] is False
        cl.assert_called_once()
        assert cl.call_args.kwargs["strip_only"] is False
        out = json.loads(capsys.readouterr().out)
        assert out["session_purged"] is True
        assert out["release_mode"] == "hard"
        assert registry.lookup("k1") is None

    def test_release_keep_advisor(self, tmp_path, monkeypatch, capsys):
        from codeagent.gateway.model import GatewayError
        from codeagent.oracle import cmd_oracle_release
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        registry.acquire("k1", _manifest("k1", backend_session_id="b1"))
        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        gw = MagicMock()
        gw.call.side_effect = GatewayError("NOT_FOUND", "no runtime")
        # soft + keep_advisor: transcript strip skipped
        ns = _NS(review_key="k1", force=False, purge=False, keep_advisor=True,
                 prompt="")
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._save_advisor_digest", return_value=None), \
             patch("codeagent.oracle._strip_oracle_transcript") as strip:
            assert cmd_oracle_release(ns) == 0
        strip.assert_not_called()
        # purge + keep_advisor: skip_advisor=True
        ns2 = _NS(review_key="k1", force=False, purge=True, keep_advisor=True,
                  prompt="")
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._save_advisor_digest", return_value=None), \
             patch("codeagent.oracle._purge_omp_session", return_value=[]) as po, \
             patch("codeagent.oracle._cleanup_opencode_session_on_release",
                   return_value={}):
            assert cmd_oracle_release(ns2) == 0
        assert po.call_args.kwargs["skip_advisor"] is True
        out_text = capsys.readouterr().out
        assert '"keep_advisor": true' in out_text

    # ── doctor ───────────────────────────────────────────────────────

    def test_opencode_session_exists(self, tmp_path, monkeypatch):
        from codeagent.oracle import _opencode_session_exists

        self._make_opencode_db(tmp_path, monkeypatch, sid="ses_777")
        assert _opencode_session_exists("ses_777") is True
        assert _opencode_session_exists("ses_999") is False
        assert _opencode_session_exists("") is False
        monkeypatch.setattr("codeagent.oracle._OPENCODE_DB_PATH",
                            tmp_path / "nope.db")
        assert _opencode_session_exists("ses_777") is False

    def test_doctor_list_all_park_entries(self, tmp_path):
        from codeagent.oracle import _doctor_list_all_park_entries
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        registry.acquire("k1", _manifest("k1", backend_session_id="b1"))
        registry.acquire("k2", _manifest("k2", backend_session_id="b2"))
        registry.release("k2")  # released_soft — still listed
        entries = _doctor_list_all_park_entries()
        assert {e["review_key"] for e in entries} == {"k1", "k2"}
        with registry._connect() as conn:
            conn.execute(
                "INSERT INTO park_leases (key, manifest_json, lifecycle) "
                "VALUES ('k-bad', '{bad', 'released_soft')")
            conn.commit()
        entries2 = _doctor_list_all_park_entries()  # corrupt row skipped
        assert "k-bad" not in {e["review_key"] for e in entries2}

    def test_cmd_oracle_doctor_healthy(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import cmd_oracle_doctor
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        registry.acquire("k1", _manifest("k1", backend_session_id="b1"))
        gw = MagicMock()
        gw.call.side_effect = lambda m, p=None: (
            {"ok": True} if m == "capabilities.get" else {"status": "active"})
        proc = MagicMock()
        proc.returncode = 1
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._OPENCODE_DB_PATH",
                   tmp_path / "nope.db"), \
             patch("codeagent.oracle.subprocess.run", return_value=proc):
            assert cmd_oracle_doctor(_NS(fix=False)) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["healthy"] == 1
        assert out["issues"] == []

    def test_cmd_oracle_doctor_stale_runtime_fix(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import cmd_oracle_doctor
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        registry.acquire("k1", _manifest("k1", backend_session_id="b1"))
        calls = {"n": 0}

        def gw_call(m, p=None):
            if m == "capabilities.get":
                return {"ok": True}
            if m == "runtime.info":
                calls["n"] += 1
                return {"status": "stopped", "runtime_id": "rt-1"} if calls["n"] >= 2 \
                    else {"status": "stopped"}
            return {}

        gw = MagicMock()
        gw.call.side_effect = gw_call
        proc = MagicMock()
        proc.returncode = 1
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._OPENCODE_DB_PATH",
                   tmp_path / "nope.db"), \
             patch("codeagent.oracle.subprocess.run", return_value=proc):
            assert cmd_oracle_doctor(_NS(fix=True)) == 1
        out = json.loads(capsys.readouterr().out)
        assert out["issues"][0]["type"] == "STALE_RUNTIME"
        assert out["fix_results"][0] == {
            "type": "STALE_RUNTIME", "review_key": "k1",
            "action": "runtime.stop", "success": True,
        }

    def test_cmd_oracle_doctor_gateway_unreachable(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import cmd_oracle_doctor

        gw = MagicMock()
        gw.call.side_effect = Exception("no gateway")
        proc = MagicMock()
        proc.returncode = 1
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._OPENCODE_DB_PATH",
                   tmp_path / "nope.db"), \
             patch("codeagent.oracle.subprocess.run", return_value=proc):
            assert cmd_oracle_doctor(_NS(fix=False)) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["issues"][0]["type"] == "GATEWAY_UNREACHABLE"
        assert "aimeshchat gateway start" in out["issues"][0]["fix_action"]

    def test_cmd_oracle_doctor_orphan_db_fix(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import cmd_oracle_doctor

        self._make_opencode_db(tmp_path, monkeypatch, sid="ses_999")
        gw = MagicMock()
        gw.call.return_value = {"ok": True}
        proc = MagicMock()
        proc.returncode = 1
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle.subprocess.run", return_value=proc):
            assert cmd_oracle_doctor(_NS(fix=True)) == 1
        out = json.loads(capsys.readouterr().out)
        types = [i["type"] for i in out["issues"]]
        assert "ORPHAN_DB" in types
        orphan = next(i for i in out["issues"] if i["type"] == "ORPHAN_DB")
        assert orphan["detail"].startswith("session ")

    # ── _purge_omp_session ───────────────────────────────────────────

    def test_purge_omp_session_targets(self, tmp_path, monkeypatch):
        from dataclasses import replace

        from codeagent.oracle import _purge_omp_session

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        sessions = tmp_path / ".omp" / "agent" / "sessions"
        proj = sessions / "proj"
        proj.mkdir(parents=True)
        main = proj / "x_ses_1.jsonl"
        main.write_text("m", encoding="utf-8")
        sub = proj / "123_ses_1"
        sub.mkdir()
        (sub / "__advisor.jsonl").write_text("adv", encoding="utf-8")
        m = replace(_manifest("k1", backend_session_id="ses_1"),
                    swarm_session_id="postmesh-k1-abc",
                    omp_session_dir=str(proj))
        removed = _purge_omp_session(m)
        assert str(main) in removed
        assert str(sub) in removed
        assert not main.exists() and not sub.exists()
        # non-existent targets are skipped silently
        m2 = replace(m, backend_session_id="ses_absent")
        assert _purge_omp_session(m2) == []

    def test_purge_omp_session_skip_advisor(self, tmp_path, monkeypatch):
        from dataclasses import replace

        from codeagent.oracle import _purge_omp_session

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        sessions = tmp_path / ".omp" / "agent" / "sessions"
        proj = sessions / "proj"
        proj.mkdir(parents=True)
        main = proj / "x_ses_1.jsonl"
        main.write_text("m", encoding="utf-8")
        sub = proj / "123_ses_1"
        sub.mkdir()
        (sub / "__advisor.jsonl").write_text("adv", encoding="utf-8")
        (sub / "other.log").write_text("o", encoding="utf-8")
        m = replace(_manifest("k1", backend_session_id="ses_1"),
                    swarm_session_id="postmesh-k1-abc")
        removed = _purge_omp_session(m, skip_advisor=True)
        assert not main.exists()
        assert (sub / "__advisor.jsonl").exists()  # preserved
        assert not (sub / "other.log").exists()
        assert sub.exists()  # dir kept (preserved advisor)
        # advisor-named file target preserved
        m2 = replace(m, backend_session_id="", omp_session_path=str(sub / "__advisor.jsonl"))
        assert _purge_omp_session(m2, skip_advisor=True) == []

    def test_purge_omp_session_oserror_warns(self, tmp_path, monkeypatch, capsys):
        from dataclasses import replace

        from codeagent.oracle import _purge_omp_session

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        proj = tmp_path / ".omp" / "agent" / "sessions" / "proj"
        proj.mkdir(parents=True)
        m = replace(_manifest("k1", backend_session_id=""),
                    omp_session_path=str(proj),
                    swarm_session_id="")
        with patch("codeagent.oracle.shutil.rmtree",
                   side_effect=OSError("permission denied")):
            removed = _purge_omp_session(m)
        assert removed == []
        assert "warning: purge session file failed" in capsys.readouterr().err

    # ── _is_runtime_alive / revive / attach ──────────────────────────

    def test_is_runtime_alive(self):
        from codeagent.oracle import _is_runtime_alive

        m = _manifest("k1", backend_session_id="b1")
        gw = MagicMock()
        gw.call.return_value = {"status": "active",
                                "runtime_health": {"alive": True}}
        with patch("codeagent.oracle._gateway", return_value=gw):
            assert _is_runtime_alive(m) is True
        gw.call.return_value = {"status": "active",
                                "runtime_health": {"alive": False}}
        with patch("codeagent.oracle._gateway", return_value=gw):
            assert _is_runtime_alive(m) is False
        gw.call.side_effect = Exception("down")
        with patch("codeagent.oracle._gateway", return_value=gw):
            assert _is_runtime_alive(m) is False

    def test_revive_gateway_down_returns_1(self, tmp_path, capsys):
        from codeagent.oracle import cmd_oracle_revive

        with patch("codeagent.oracle._ensure_gateway_or_hint", return_value=False):
            assert cmd_oracle_revive(_NS(review_key="k1", mode="bg")) == 1
        assert capsys.readouterr().out == ""

    def test_revive_not_found_and_purged_and_broken(self, tmp_path, monkeypatch, capsys):
        from dataclasses import replace
        from codeagent.oracle import cmd_oracle_revive
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        with patch("codeagent.oracle._ensure_gateway_or_hint", return_value=True):
            assert cmd_oracle_revive(_NS(review_key="k-nope", mode="bg")) == 1
        assert json.loads(capsys.readouterr().err)["error"] == "not_found"

        for lc, err_key in ((Lifecycle.RELEASED_HARD, "purged"),
                            (Lifecycle.BROKEN, "broken")):
            m = _manifest(f"k-{lc.value}", backend_session_id="b1")
            m = replace(m, lifecycle=lc)
            with registry._connect() as conn:
                conn.execute(
                    "INSERT INTO park_leases (key, manifest_json, lifecycle) "
                    "VALUES (?, ?, ?)",
                    (m.review_key, json.dumps(registry._manifest_to_dict(m)),
                     lc.value))
                conn.commit()
            with patch("codeagent.oracle._ensure_gateway_or_hint",
                       return_value=True):
                assert cmd_oracle_revive(
                    _NS(review_key=m.review_key, mode="bg")) == 1
            assert json.loads(capsys.readouterr().err)["error"] == err_key

    def test_revive_hot_already_active(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import cmd_oracle_revive
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        registry.acquire("k1", _manifest("k1", backend_session_id="b1"))
        decision = MagicMock()
        decision.method = "hot"
        with patch("codeagent.oracle._ensure_gateway_or_hint", return_value=True), \
             patch("codeagent.park.router.revive_or_spawn",
                   return_value=decision):
            assert cmd_oracle_revive(_NS(review_key="k1", mode="bg")) == 1
        err = json.loads(capsys.readouterr().err)
        assert err["error"] == "already_active"
        assert "oracle ask" in err["hint"]

    def test_revive_warm_success(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import cmd_oracle_revive
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        registry.acquire("k1", _manifest("k1", backend_session_id="b1"))
        registry.release("k1")
        decision = MagicMock()
        decision.method = "warm"
        with patch("codeagent.oracle._ensure_gateway_or_hint", return_value=True), \
             patch("codeagent.park.router.revive_or_spawn", return_value=decision), \
             patch("codeagent.oracle._revive_warm",
                   return_value=("rt-1", "b1", ["m/x"])) as rw, \
             patch("codeagent.oracle._write_oracle_meta") as wm, \
             patch("codeagent.oracle._lazy_db_cleanup", return_value=0):
            assert cmd_oracle_revive(_NS(review_key="k1", mode="bg")) == 0
        manifest = registry.lookup("k1")
        rw.assert_called_once_with("k1", manifest, "bg")
        wm.assert_called_once()
        out = json.loads(capsys.readouterr().out)
        assert out["method"] == "warm"
        assert out["declared"] is True
        assert out["backend_session_id"] == "b1"

    def test_revive_cold_success(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import cmd_oracle_revive
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        registry.acquire("k1", _manifest("k1", backend_session_id=""))
        registry.release("k1")
        decision = MagicMock()
        decision.method = "cold"
        with patch("codeagent.oracle._ensure_gateway_or_hint", return_value=True), \
             patch("codeagent.park.router.revive_or_spawn", return_value=decision), \
             patch("codeagent.oracle._revive_cold",
                   return_value=("rt-2", "", [])) as rc, \
             patch("codeagent.oracle._lazy_db_cleanup", return_value=0):
            assert cmd_oracle_revive(_NS(review_key="k1", mode="bg")) == 0
        rc.assert_called_once()
        out = json.loads(capsys.readouterr().out)
        assert out["method"] == "cold"
        assert out["declared"] is True

    def test_revive_resume_declare_failed(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import cmd_oracle_revive
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        registry.acquire("k1", _manifest("k1", backend_session_id="b1"))
        registry.release("k1")
        decision = MagicMock()
        decision.method = "warm"
        with patch("codeagent.oracle._ensure_gateway_or_hint", return_value=True), \
             patch("codeagent.park.router.revive_or_spawn", return_value=decision), \
             patch("codeagent.oracle._revive_warm",
                   side_effect=RuntimeError("gateway declare failed: A7: owner mismatch")):
            assert cmd_oracle_revive(_NS(review_key="k1", mode="resume")) == 1
        err = json.loads(capsys.readouterr().err)
        assert err["error"] == "declare_failed"
        assert err["declared"] is False

    def test_revive_generic_failure(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import cmd_oracle_revive
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        registry.acquire("k1", _manifest("k1", backend_session_id=""))
        registry.release("k1")
        decision = MagicMock()
        decision.method = "cold"
        with patch("codeagent.oracle._ensure_gateway_or_hint", return_value=True), \
             patch("codeagent.park.router.revive_or_spawn", return_value=decision), \
             patch("codeagent.oracle._revive_cold", side_effect=ValueError("boom")):
            assert cmd_oracle_revive(_NS(review_key="k1", mode="bg")) == 1
        err = json.loads(capsys.readouterr().err)
        assert err["error"] == "cold_failed"
        assert err["declared"] is False

    def test_revive_warm_bg_spawn_and_flip(self, tmp_path, monkeypatch):
        from dataclasses import replace
        from codeagent.oracle import _revive_warm
        from codeagent.park.registry import ParkRegistry

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        registry = ParkRegistry()
        m = replace(_manifest("k1", backend_session_id="native-1"),
                    primary_model="prof/x")
        registry.acquire("k1", m)
        with patch("codeagent.oracle.RuntimeRegistry.spawn",
                   return_value=_handle("rt-w", backend_session_id="b2")) as spawn, \
             patch("codeagent.oracle._adopt_runtime", return_value="skipped"):
            rid, sid, chain = _revive_warm("k1", registry.lookup("k1"), "bg")
        assert rid == "rt-w"
        assert sid == "b2"
        assert chain == ["prof/x"]
        req = spawn.call_args[0][1]
        assert req["backend_session_id"] == "native-1"
        assert req["session_dir"] == ""  # manifest has no omp_session_dir
        got = registry.lookup("k1")
        assert got.lifecycle == Lifecycle.HOT_PARKED
        assert got.backend_session_id == "b2"
        assert got.round == 1

    def test_revive_warm_sid_pending_warning(self, tmp_path, monkeypatch, capsys):
        from dataclasses import replace
        from codeagent.oracle import _revive_warm
        from codeagent.park.registry import ParkRegistry

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        registry = ParkRegistry()
        m = replace(_manifest("k1", backend_session_id="native-1"),
                    primary_model="prof/x")
        registry.acquire("k1", m)
        with patch("codeagent.oracle.RuntimeRegistry.spawn",
                   return_value=_handle("rt-w", backend_session_id="")) as spawn, \
             patch("codeagent.oracle._adopt_runtime", return_value="skipped"):
            rid, sid, chain = _revive_warm("k1", registry.lookup("k1"), "bg")
        assert sid == "native-1"  # preserved
        assert "preserving previous id" in capsys.readouterr().err

    def test_revive_warm_spawn_fail_cleans_up(self, tmp_path, monkeypatch):
        from dataclasses import replace

        from codeagent.oracle import _revive_warm
        from codeagent.park.registry import ParkRegistry

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        registry = ParkRegistry()
        m = replace(_manifest("k1", backend_session_id="native-1"),
                    primary_model="prof/x")
        registry.acquire("k1", m)
        reg_mock = MagicMock()
        reg_mock.spawn.return_value = _handle("rt-w", backend_session_id="b2")
        with patch("codeagent.oracle.RuntimeRegistry",
                   return_value=reg_mock), \
             patch("codeagent.oracle._flip_to_hot",
                   side_effect=Exception("flip exploded")):
            with pytest.raises(Exception, match="flip exploded"):
                _revive_warm("k1", registry.lookup("k1"), "bg")
        reg_mock.stop.assert_called_once_with("rt-w", "revive-warm-failed")

    def test_revive_warm_resume_ok_and_error(self, tmp_path, monkeypatch):
        from codeagent.oracle import _revive_warm

        m = _manifest("k1", backend_session_id="native-1")
        with patch("codeagent.oracle._attach_omp_session", return_value=None) as attach:
            rid, sid, chain = _revive_warm("k1", m, "resume")
        attach.assert_called_once_with("native-1", "k1", m.workdir)
        assert rid == "" and sid == "native-1" and chain == []
        with patch("codeagent.oracle._attach_omp_session",
                   return_value="gateway declare failed: A7: x"):
            with pytest.raises(RuntimeError, match="A7"):
                _revive_warm("k1", m, "resume")

    def test_revive_cold_resume_falls_back_bg(self, tmp_path, monkeypatch):
        from dataclasses import replace
        from codeagent.oracle import _revive_cold, _review_sid

        m = replace(_manifest("k1", backend_session_id=""), primary_model="m/x")
        with patch("codeagent.oracle.build_cold_context", return_value="ctx"), \
             patch("codeagent.oracle.RuntimeRegistry.spawn",
                   return_value=_handle("rt-c", backend_session_id="bc")) as spawn, \
             patch("codeagent.oracle._flip_to_hot") as flip, \
             patch("codeagent.oracle._adopt_runtime", return_value=True):
            rid, sid, chain = _revive_cold("k1", m, "resume")
        assert rid == "rt-c"
        assert sid == "bc"
        assert chain == ["m/x"]
        assert spawn.call_args[0][0] == "omp"
        assert flip.call_args.kwargs["sid"] == _review_sid("k1")
        req = spawn.call_args[0][1]
        assert "ctx" in req["task"]

    def test_flip_to_hot_updates(self, tmp_path, monkeypatch):
        from codeagent.oracle import _flip_to_hot
        from codeagent.park.registry import ParkRegistry

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        registry = ParkRegistry()
        m = _manifest("k1", backend_session_id="",
                      lifecycle=Lifecycle.COLD_RESUMABLE)
        registry.acquire("k1", m)
        _flip_to_hot("k1", registry.lookup("k1"), sid="postmesh-x",
                     backend_session_id="b9")
        got = registry.lookup("k1")
        assert got.lifecycle == Lifecycle.HOT_PARKED
        assert got.backend_session_id == "b9"
        assert got.round == 1
        assert got.swarm_session_id == "postmesh-x"
        assert got.release_mode == ""

    def test_attach_omp_session_paths(self, tmp_path, monkeypatch, capsys):
        from codeagent.gateway.model import GatewayError
        from codeagent.oracle import _attach_omp_session

        gw = MagicMock()
        gw.call.return_value = {}
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle.subprocess.Popen") as popen:
            assert _attach_omp_session("ses-1", "k1", str(tmp_path)) is None
        argv = popen.call_args[0][0]
        assert argv == ["omp", "--resume", "ses-1"]
        gw.call.assert_called_once_with("runtime.declare", {
            "review_key": "k1", "backend_session_id": "ses-1",
            "mode": "native_resume", "agent_id": "oracle",
        })
        # Popen OSError → warning, declare still attempted
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle.subprocess.Popen",
                   side_effect=OSError("no omp binary")):
            assert _attach_omp_session("ses-1", "k1") is None
        assert "omp --resume attach failed" in capsys.readouterr().err
        # gateway unreachable → silent None
        gw2 = MagicMock()
        gw2.call.side_effect = ConnectionError("no socket")
        with patch("codeagent.oracle._gateway", return_value=gw2), \
             patch("codeagent.oracle.subprocess.Popen"):
            assert _attach_omp_session("ses-1", "k1") is None
        # structured GatewayError → error string
        gw3 = MagicMock()
        gw3.call.side_effect = GatewayError("A7", "owner mismatch")
        with patch("codeagent.oracle._gateway", return_value=gw3), \
             patch("codeagent.oracle.subprocess.Popen"):
            assert _attach_omp_session("ses-1", "k1") == \
                "gateway declare failed: A7: owner mismatch"
        # unexpected exception → error string
        gw4 = MagicMock()
        gw4.call.side_effect = ValueError("weird")
        with patch("codeagent.oracle._gateway", return_value=gw4), \
             patch("codeagent.oracle.subprocess.Popen"):
            assert _attach_omp_session("ses-1", "k1") == \
                "gateway declare failed: weird"

    def test_attach_absent_returns_1(self, capsys):
        from codeagent.oracle import cmd_oracle_attach

        assert cmd_oracle_attach(_NS(review_key="k-nope", mode="bg",
                                     prompt="")) == 1
        err = json.loads(capsys.readouterr().err)
        assert err["error"] == "not_found"

    def test_attach_hot_routes_to_ask(self, tmp_path, capsys):
        from codeagent.oracle import cmd_oracle_attach
        from codeagent.park.registry import ParkRegistry

        ParkRegistry().acquire("k1", _manifest("k1", backend_session_id="b1"))
        with patch("codeagent.oracle.cmd_oracle_ask", return_value=0) as ask:
            assert cmd_oracle_attach(_NS(review_key="k1", mode="bg",
                                         prompt="p")) == 0
        ask.assert_called_once()

    def test_attach_released_routes_to_revive(self, tmp_path, capsys):
        from codeagent.oracle import cmd_oracle_attach
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        registry.acquire("k2", _manifest("k2", backend_session_id="b2"))
        registry.release("k2")
        with patch("codeagent.oracle.cmd_oracle_revive", return_value=0) as rev:
            assert cmd_oracle_attach(_NS(review_key="k2", mode="bg",
                                         prompt="")) == 0
        rev.assert_called_once()

    # ── residual branches ────────────────────────────────────────────

    def test_purge_opencode_vacuum_large_db(self, tmp_path, monkeypatch):
        from codeagent.oracle import _purge_opencode_session

        db = self._make_opencode_db(tmp_path, monkeypatch)
        with open(db, "ab") as f:
            f.truncate(101 * 1024 * 1024)  # sparse > 100MB → VACUUM path
        r = _purge_opencode_session("ses_111")
        assert r["error"] is None
        assert r["vacuum"] is True
        assert r["vacuum_before_mb"] >= 100.0

    def test_cmd_oracle_gc_park_delete_failed(self, tmp_path, monkeypatch, capsys):
        import time

        from dataclasses import replace

        from codeagent.oracle import cmd_oracle_gc
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        m = replace(_manifest("k-gc", backend_session_id=""),
                    hard_expires_at=time.time() - 100)
        registry.acquire("k-gc", m)
        registry.release("k-gc")
        with patch("codeagent.oracle._purge_omp_session", return_value=[]), \
             patch("codeagent.oracle.ParkRegistry.delete",
                   side_effect=Exception("row locked")):
            assert cmd_oracle_gc(_NS(dry_run=False, json=True)) == 1
        out = json.loads(capsys.readouterr().out)
        assert out["failed"] == 1
        assert any("park delete failed" in e for e in out["errors"])

    def test_purge_omp_session_swarm_dirs(self, tmp_path, monkeypatch):
        from dataclasses import replace

        from codeagent.oracle import _purge_omp_session
        from codeagent.mailbox.store import MailboxStore

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        m = replace(_manifest("k1", backend_session_id="ses_1"),
                    swarm_session_id="postmesh-k1-abc")
        store = MailboxStore()
        swarm_dirs = [
            store.session_dir("postmesh-k1-abc"),
            store.root / "_outbox" / "postmesh-k1-abc",
            store.root / "_dead_letter" / "postmesh-k1-abc",
        ]
        for d in swarm_dirs:
            d.mkdir(parents=True)
        sessions = tmp_path / ".omp" / "agent" / "sessions"
        (sessions / "proj").mkdir(parents=True)
        main = sessions / "proj" / "x_ses_1.jsonl"
        main.write_text("m", encoding="utf-8")
        removed = _purge_omp_session(m)
        assert str(main) in removed
        for d in swarm_dirs:
            assert not d.exists()

    def test_purge_omp_session_skip_advisor_empty_child(self, tmp_path, monkeypatch):
        from dataclasses import replace

        from codeagent.oracle import _purge_omp_session

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        sessions = tmp_path / ".omp" / "agent" / "sessions"
        proj = sessions / "proj"
        proj.mkdir(parents=True)
        (proj / "x_ses_1.jsonl").write_text("m", encoding="utf-8")
        sub = proj / "123_ses_1"
        (sub / "empty-dir").mkdir(parents=True)
        m = replace(_manifest("k1", backend_session_id="ses_1"),
                    swarm_session_id="postmesh-k1-abc")
        removed = _purge_omp_session(m, skip_advisor=True)
        assert str(sub / "empty-dir") in removed  # rmdir success path

    def test_cmd_oracle_doctor_orphan_tmux(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import _review_sid, cmd_oracle_doctor
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        registry.acquire("k1", _manifest("k1", backend_session_id="b1"))
        gw = MagicMock()
        gw.call.return_value = {"ok": True}
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = (f"{_review_sid('k1')}|%1\n"
                       "postmesh-unmatched|%2\n"
                       "ora-other-legacy|%3\n")
        monkeypatch.setattr("codeagent.launchers.tmux.tmux_cmd",
                            lambda *a: ["tmux", *a])
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._OPENCODE_DB_PATH",
                   tmp_path / "nope.db"), \
             patch("codeagent.oracle.subprocess.run", return_value=proc):
            assert cmd_oracle_doctor(_NS(fix=False)) == 1
        out = json.loads(capsys.readouterr().out)
        types = [i["type"] for i in out["issues"]]
        assert types.count("ORPHAN_TMUX") == 2  # unmatched windows only

    def test_cmd_oracle_doctor_orphan_db_scan_error(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import cmd_oracle_doctor

        db = tmp_path / "opencode.db"
        db.write_bytes(b"this is not a sqlite database" * 100)
        monkeypatch.setattr("codeagent.oracle._OPENCODE_DB_PATH", db)
        gw = MagicMock()
        gw.call.return_value = {"ok": True}
        proc = MagicMock()
        proc.returncode = 1
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle.subprocess.run", return_value=proc):
            assert cmd_oracle_doctor(_NS(fix=False)) == 0
        assert json.loads(capsys.readouterr().out)["issues"] == []

    def test_cmd_oracle_doctor_stale_runtime_stop_fails(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import cmd_oracle_doctor
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        registry.acquire("k1", _manifest("k1", backend_session_id="b1"))
        calls = {"n": 0}

        def gw_call(m, p=None):
            if m == "capabilities.get":
                return {"ok": True}
            if m == "runtime.info":
                calls["n"] += 1
                return {"status": "stopped", "runtime_id": "rt-1"} if calls["n"] >= 2 \
                    else {"status": "stopped"}
            if m == "runtime.stop":
                raise Exception("stop refused")
            return {}

        gw = MagicMock()
        gw.call.side_effect = gw_call
        proc = MagicMock()
        proc.returncode = 1
        with patch("codeagent.oracle._gateway", return_value=gw), \
             patch("codeagent.oracle._OPENCODE_DB_PATH",
                   tmp_path / "nope.db"), \
             patch("codeagent.oracle.subprocess.run", return_value=proc):
            assert cmd_oracle_doctor(_NS(fix=True)) == 2
        out = json.loads(capsys.readouterr().out)
        assert out["fix_results"][0]["success"] is False

    def test_revive_cold_spawn_fail_cleanup(self, tmp_path, monkeypatch):
        from dataclasses import replace
        from codeagent.oracle import _revive_cold

        m = replace(_manifest("k1", backend_session_id=""), primary_model="m/x")
        reg_mock = MagicMock()
        reg_mock.spawn.return_value = _handle("rt-c", backend_session_id="bc")
        with patch("codeagent.oracle.RuntimeRegistry", return_value=reg_mock), \
             patch("codeagent.oracle.build_cold_context", return_value="ctx"), \
             patch("codeagent.oracle._flip_to_hot",
                   side_effect=Exception("flip exploded")):
            with pytest.raises(Exception, match="flip exploded"):
                _revive_cold("k1", m, "bg")
        reg_mock.stop.assert_called_once_with("rt-c", "revive-cold-failed")

    def test_revive_cold_stale_snapshot_warning(self, tmp_path, monkeypatch, capsys):
        from dataclasses import replace
        from codeagent.oracle import _revive_cold

        m = replace(_manifest("k1", backend_session_id=""), primary_model="m/x")
        with patch("codeagent.oracle._snapshot_age_days", return_value=9.5), \
             patch("codeagent.oracle.build_cold_context", return_value="ctx"), \
             patch("codeagent.oracle.RuntimeRegistry.spawn",
                   return_value=_handle("rt-c", backend_session_id="bc")), \
             patch("codeagent.oracle._flip_to_hot"), \
             patch("codeagent.oracle._adopt_runtime", return_value=True):
            rid, _sid, _chain = _revive_cold("k1", m, "bg")
        assert rid == "rt-c"

    def test_revive_lazy_db_cleanup_error_still_succeeds(self, tmp_path, monkeypatch, capsys):
        from codeagent.oracle import cmd_oracle_revive
        from codeagent.park.registry import ParkRegistry

        registry = ParkRegistry()
        registry.acquire("k1", _manifest("k1", backend_session_id="b1"))
        registry.release("k1")
        decision = MagicMock()
        decision.method = "warm"
        with patch("codeagent.oracle._ensure_gateway_or_hint", return_value=True), \
             patch("codeagent.park.router.revive_or_spawn", return_value=decision), \
             patch("codeagent.oracle._revive_warm",
                   return_value=("rt-1", "b1", ["m"])), \
             patch("codeagent.oracle._write_oracle_meta"), \
             patch("codeagent.oracle._lazy_db_cleanup",
                   side_effect=Exception("db busy")):
            assert cmd_oracle_revive(_NS(review_key="k1", mode="bg")) == 0
        assert json.loads(capsys.readouterr().out)["method"] == "warm"

    def test_purge_omp_session_raw_path_target(self, tmp_path, monkeypatch):
        from dataclasses import replace

        from codeagent.oracle import _purge_omp_session

        monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
        raw = tmp_path / "session-dir" / "s.jsonl"
        raw.parent.mkdir(parents=True)
        raw.write_text("m", encoding="utf-8")
        m = replace(_manifest("k1", backend_session_id=""),
                    omp_session_path=str(raw))
        removed = _purge_omp_session(m)
        assert str(raw) in removed
        assert not raw.exists()
