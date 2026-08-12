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


def test_fallback_find_session_recursive_scan(tmp_path, monkeypatch):
    """P2-11 fallback：递归扫描子目录 .jsonl（含 __advisor）+ 精确 tail 匹配。"""
    from codeagent.oracle import _fallback_find_session_for_key
    from pathlib import Path

    # 模拟 sessions 根：一个顶层文件（含 oracle 但不含 tail）+ 一个深层 __advisor（含 tail）
    root = tmp_path / "sessions"
    top = root / "some-project"
    top.mkdir(parents=True)
    (top / "unrelated.jsonl").write_text('{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"oracle 历史"}]}}')
    sdir = top / "2026-08-11T16-36-40-491Z_019ff1ae"
    sdir.mkdir()
    (sdir / "__advisor.jsonl").write_text('{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"2 r4-closure answer"}]}}')

    monkeypatch.setattr("codeagent.oracle.Path.home", lambda: tmp_path)
    # 需要让 sessions_root 指向 tmp_path/sessions — 但 home 是 tmp_path，sessions_root=home/.omp/agent/sessions
    # 直接构造到正确位置
    real_root = tmp_path / ".omp" / "agent" / "sessions"
    real_root.mkdir(parents=True)
    (real_root / "some-project").mkdir()
    (real_root / "some-project" / "unrelated.jsonl").write_text('oracle 历史')
    sd = real_root / "some-project" / "sdir"
    sd.mkdir()
    (sd / "__advisor.jsonl").write_text('r4-closure 2 answer')

    found = _fallback_find_session_for_key("proj:oracle:review:r4-closure")
    assert found is not None
    assert "r4-closure" in found.read_text(errors="replace")


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
    rc = cmd_oracle_attach(ns)
    assert rc == 0
    got = registry.lookup("k-attach")
    assert got is not None and got.lifecycle == Lifecycle.HOT_PARKED, "attach 应 revive 回 HOT_PARKED"


# ── model fallback chain ───────────────────────────────────────────────


def test_parse_retry_fallback_chains(tmp_path):
    """retry.fallbackChains 2 级嵌套解析（default/slow/smol 链）。"""
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        "retry: \n"
        "  enabled: true\n"
        "  fallbackChains: \n"
        "    default: \n"
        "      - opencode-go/deepseek-v4-flash\n"
        "      - Mify/deepseek/deepseek-v4-pro\n"
        "    smol: \n"
        "      - opencode-go/deepseek-v4-flash\n"
        "    slow: \n"
        "      - Mify-ppio/ppio/pa/gpt-5.6-sol\n"
        "      - Mify/deepseek/deepseek-v4-pro\n"
        "compaction: \n"
        "  enabled: true\n"
        "memory: \n"
        "  backend: memsearch\n"
    , encoding="utf-8")
    from codeagent.oracle import _parse_retry_fallback_chains

    chains = _parse_retry_fallback_chains(cfg)
    assert chains["default"] == ["opencode-go/deepseek-v4-flash", "Mify/deepseek/deepseek-v4-pro"]
    assert chains["slow"] == ["Mify-ppio/ppio/pa/gpt-5.6-sol", "Mify/deepseek/deepseek-v4-pro"]
    assert chains["smol"] == ["opencode-go/deepseek-v4-flash"]
    # 不存在的 section 不得串入
    assert "compaction" not in chains and "memory" not in chains


def test_resolve_oracle_model_chain_mapping(tmp_path, monkeypatch):
    """oracle → slow（无 slow 时 default）；oracle-lite → default；显式 model 优先。"""
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        "retry: \n"
        "  fallbackChains: \n"
        "    default: \n"
        "      - model-default-a\n"
        "      - model-default-b\n"
        "    slow: \n"
        "      - model-slow-a\n"
        "      - model-slow-b\n"
    , encoding="utf-8")
    from codeagent.oracle import _resolve_oracle_model_chain

    with patch("codeagent.oracle._omp_config_paths", return_value=[cfg]):
        assert _resolve_oracle_model_chain("oracle", "") == ["model-slow-a", "model-slow-b"]
        assert _resolve_oracle_model_chain("oracle-opus", "") == ["model-slow-a", "model-slow-b"]
        assert _resolve_oracle_model_chain("oracle-lite", "") == ["model-default-a", "model-default-b"]
        # 显式指定不覆盖
        assert _resolve_oracle_model_chain("oracle", "explicit/model-x") == ["explicit/model-x"]


def test_resolve_oracle_model_chain_no_config(tmp_path, monkeypatch):
    """无配置/无 chain → 空列表（保持现状）。"""
    from codeagent.oracle import _resolve_oracle_model_chain

    with patch("codeagent.oracle._omp_config_paths", return_value=[]):
        assert _resolve_oracle_model_chain("oracle", "") == []

    cfg = tmp_path / "config.yml"
    cfg.write_text("memory: \n  backend: memsearch\n", encoding="utf-8")
    with patch("codeagent.oracle._omp_config_paths", return_value=[cfg]):
        assert _resolve_oracle_model_chain("oracle-lite", "") == []


def test_ask_cold_uses_model_chain(tmp_path, capsys, monkeypatch):
    """ask cold 分支补模型链：spawn model=primary，env 注入链，输出含 model_chain。"""
    from codeagent.park.registry import ParkRegistry
    from codeagent.oracle import _parse_flat_yaml

    cfg = tmp_path / "config.yml"
    cfg.write_text(
        "retry: \n"
        "  fallbackChains: \n"
        "    default: \n"
        "      - ask-default-a\n"
        "      - ask-default-b\n"
    , encoding="utf-8")
    monkeypatch.setattr("codeagent.oracle._omp_config_paths", lambda: [cfg])

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
    assert out["model_chain"] == ["ask-default-a", "ask-default-b"]
    req = spawn.call_args[0][1]
    assert req["model"] == "ask-default-a"
    assert req["env"]["OMP_MODEL_FALLBACK_CHAIN"] == "ask-default-a,ask-default-b"


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
