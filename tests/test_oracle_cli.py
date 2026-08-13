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
    (sessions / "ora-new").mkdir()

    unrelated = sessions / "old-proj" / "old.jsonl"
    unrelated.write_text(
        '{"type":"message","message":{"role":"assistant","content":'
        '[{"type":"text","text":"blur is a gaussian filter"}]}}\n',
        encoding="utf-8",
    )
    correct = sessions / "ora-new" / "session.jsonl"
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
    expected = tmp_path / ".omp" / "agent" / "sessions" / "ora-new" / "session.jsonl"
    assert found is not None
    assert found == expected, "full-key match（ora-* 目录）必须胜过短 tail 误匹配"


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

