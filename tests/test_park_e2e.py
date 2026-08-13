"""Park 机制端到端验证脚本。

运行方式:
  python3 -m pytest tests/test_park_e2e.py -v

注意：需要已安装的 codeagent CLI（非 pip install -e . 也可）
某些测试需要 OMP 环境（如 hot revive），跳过条件标注。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import tempfile
from pathlib import Path

# 隔离测试数据：使用临时 XDG 目录（短路径——XDG_RUNTIME_DIR 泄漏会
# 影响 ControlMaster socket，超 104 字节 AF_UNIX 限制则其他测试失败）
_test_tmpdir = Path(tempfile.mkdtemp(prefix="park_e2e_"))
os.environ["XDG_STATE_HOME"] = str(_test_tmpdir / "state")
os.environ["XDG_DATA_HOME"] = str(_test_tmpdir / "data")
_short_runtime = Path("/tmp") / f"parkrt-{os.getpid()}"
os.environ["XDG_RUNTIME_DIR"] = str(_short_runtime)
_SAVED_XDG = {"state": os.environ.get("XDG_STATE_HOME"), "data": os.environ.get("XDG_DATA_HOME"), "runtime": os.environ.get("XDG_RUNTIME_DIR")}

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from codeagent.park.registry import ParkRegistry
from codeagent.domain.park import ParkManifest, Lifecycle
from codeagent.park.snapshot import save_snapshot, ReviewSnapshot
from codeagent.park.router import park_revive
from codeagent.park.metrics import log_event, read_metrics, compute_stats


def _clean():
    db = _test_tmpdir / "state" / "aimeshchat" / "park" / "park.sqlite3"
    if db.exists():
        db.unlink()
    snap_dir = _test_tmpdir / "data" / "aimeshchat" / "park" / "snapshots"
    if snap_dir.exists():
        shutil.rmtree(snap_dir)
    metrics = _test_tmpdir / "data" / "aimeshchat" / "park" / "metrics.jsonl"
    if metrics.exists():
        metrics.unlink()


def setup_module():
    _test_tmpdir.mkdir(parents=True, exist_ok=True)


def teardown_module():
    shutil.rmtree(_test_tmpdir, ignore_errors=True)


def test_1_same_process_two_round_hot_revive():
    _clean()
    pr = ParkRegistry()
    m = ParkManifest(review_key="e2e-1", lifecycle=Lifecycle.HOT_PARKED, agent_type="oracle")
    assert pr.acquire("e2e-1", m)

    lm = pr.lookup("e2e-1")
    assert lm and lm.lifecycle == Lifecycle.HOT_PARKED

    snap = ReviewSnapshot(review_key="e2e-1", round=1, last_conclusion="方案 A 可行")
    save_snapshot(snap)

    result = park_revive("e2e-1")
    assert result.method == "hot" and result.success
    assert result.manifest and result.manifest.review_key == "e2e-1"

    log_event("revive_hot", review_key="e2e-1", agent_type="oracle", method="hot", success=True)
    pr.release("e2e-1")


def test_2_concurrent_followup_serialization():
    _clean()
    pr = ParkRegistry()
    m = ParkManifest(review_key="e2e-2", lifecycle=Lifecycle.HOT_PARKED)
    assert pr.acquire("e2e-2", m)
    assert not pr.acquire("e2e-2", m)
    pr.release("e2e-2")


def test_3_ttl_eviction_lru():
    _clean()
    pr = ParkRegistry()
    m = ParkManifest(review_key="e2e-3", lifecycle=Lifecycle.HOT_PARKED, soft_expires_at=time.time() - 10)
    pr.acquire("e2e-3", m)
    evicted = pr.sweep()
    assert "e2e-3" in evicted
    log_event("evict_ttl", review_key="e2e-3", method="sweep")


def test_4_cold_recovery_after_release():
    _clean()
    pr = ParkRegistry()
    m = ParkManifest(review_key="e2e-4", lifecycle=Lifecycle.HOT_PARKED)
    pr.acquire("e2e-4", m)
    snap = ReviewSnapshot(
        review_key="e2e-4", round=1, last_conclusion="首次分析结论",
        standing_constraints=["不能改公共 API"],
    )
    save_snapshot(snap)
    pr.release("e2e-4")

    result = park_revive("e2e-4")
    assert result.method == "cold" and result.success
    assert "e2e-4" in result.context
    assert "仍成立的结论" in result.context


def test_5_resume_failure_cold_fallback():
    _clean()
    pr = ParkRegistry()
    m = ParkManifest(review_key="e2e-5", lifecycle=Lifecycle.COLD_RESUMABLE, backend_session_id="")
    pr.acquire("e2e-5", m)
    result = park_revive("e2e-5")
    assert result.method == "cold"


def test_6_mailbox_archive_second_opinion():
    _clean()
    pr = ParkRegistry()
    m1 = ParkManifest(review_key="proj:oracle:arch:storage", lifecycle=Lifecycle.HOT_PARKED)
    m2 = ParkManifest(review_key="proj:oracle:arch:storage:second-opinion", lifecycle=Lifecycle.HOT_PARKED)
    assert pr.acquire("proj:oracle:arch:storage", m1)
    assert pr.acquire("proj:oracle:arch:storage:second-opinion", m2)
    active = pr.list_active()
    assert len(active) == 2
    pr.release("proj:oracle:arch:storage")
    pr.release("proj:oracle:arch:storage:second-opinion")


if __name__ == "__main__":
    test_1_same_process_two_round_hot_revive()
    test_2_concurrent_followup_serialization()
    test_3_ttl_eviction_lru()
    test_4_cold_recovery_after_release()
    test_5_resume_failure_cold_fallback()
    test_6_mailbox_archive_second_opinion()
    print("\n🎉 ALL E2E TESTS PASSED")

def teardown_module(module):
    """Restore the XDG env vars this module hijacked at import time —
    otherwise the deep/short XDG_RUNTIME_DIR leaks into other test files
    and breaks ControlMaster socket paths (AF_UNIX 104-byte limit)."""
    for name, val in _SAVED_XDG.items():
        key = f"XDG_{name.upper()}_HOME" if name != "runtime" else "XDG_RUNTIME_DIR"
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


# ── ControlStore（控制面存储：runtime_generations / commands）──────────
# runtime_generations 持久化 runtime.context model_context（重启恢复）；
# commands 以 request_id 为主键幂等（重复入队返回 False，不产生重复行）。


def test_7_control_store_runtime_generations_model_context_roundtrip(tmp_path):
    """runtime_generations：model_context 持久化 + 读回（JSON 解析）。"""
    from codeagent.gateway.control_store import ControlStore

    store = ControlStore(db_path=tmp_path / "control.sqlite3")
    try:
        store.upsert_generation(
            runtime_id="rt-ctx-1", current_generation=3, owner_nonce="nonce-a",
            presence="alive", binding="bound", backend_session_id="b1",
            binding_epoch=2, agent_state="agent_running",
            model_context=json.dumps({
                "provider": "prov-x", "model": "mdl-y",
                "variant": "thinking", "epoch": 7,
            }, ensure_ascii=False),
        )
        got = store.get_generation("rt-ctx-1")
        assert got is not None
        assert got["current_generation"] == 3
        assert got["binding_epoch"] == 2
        assert got["binding"] == "bound"
        assert got["presence"] == "alive"
        assert got["model_context"] == {
            "provider": "prov-x", "model": "mdl-y",
            "variant": "thinking", "epoch": 7,
        }
    finally:
        store.close()


def test_8_control_store_model_context_empty_upsert_preserves(tmp_path):
    """空 model_context 的归约不覆盖已持久化的上下文（保留逻辑）。"""
    from codeagent.gateway.control_store import ControlStore

    store = ControlStore(db_path=tmp_path / "control.sqlite3")
    try:
        store.upsert_generation(
            runtime_id="rt-ctx-2", current_generation=1,
            model_context=json.dumps({"model": "m1", "provider": "p1", "variant": "", "epoch": 1}),
        )
        # 无上下文的状态归约（如 heartbeat）→ 不应抹掉已上报的 model_context
        store.upsert_generation(
            runtime_id="rt-ctx-2", current_generation=2,
            presence="stale", model_context="",
        )
        got = store.get_generation("rt-ctx-2")
        assert got["current_generation"] == 2
        assert got["presence"] == "stale"
        assert got["model_context"] == {"model": "m1", "provider": "p1", "variant": "", "epoch": 1}
        # 显式传入新上下文 → 覆盖
        store.upsert_generation(
            runtime_id="rt-ctx-2", current_generation=3,
            model_context=json.dumps({"model": "m2", "provider": "p1", "variant": "", "epoch": 2}),
        )
        assert store.get_generation("rt-ctx-2")["model_context"]["model"] == "m2"
    finally:
        store.close()


def test_9_control_store_last_state_seq_monotonic(tmp_path):
    """last_state_seq 单调递增（每次归约 +1，恢复顺序可判定）。"""
    from codeagent.gateway.control_store import ControlStore

    store = ControlStore(db_path=tmp_path / "control.sqlite3")
    try:
        store.upsert_generation("rt-seq", current_generation=1)
        store.upsert_generation("rt-seq", current_generation=1)
        store.upsert_generation("rt-seq", current_generation=2)
        assert store.get_generation("rt-seq")["last_state_seq"] == 3
        store.delete_generation("rt-seq")
        assert store.get_generation("rt-seq") is None
    finally:
        store.close()


def test_10_control_store_commands_enqueue_idempotent(tmp_path):
    """commands 幂等：同 request_id 二次入队 → False 且只有一行。"""
    from codeagent.gateway.control_store import ControlStore

    store = ControlStore(db_path=tmp_path / "control.sqlite3")
    try:
        created = store.enqueue_command(
            request_id="req-1", command_id="cmd-1", msg_id="m1",
            runtime_id="rt-c", generation=1, payload_hash="hash-a",
            state="QUEUED", binding_epoch=0, backend_session_id="b1",
        )
        assert created is True
        # 同 request_id 再次入队（不同 command_id/payload）→ 幂等拒绝
        again = store.enqueue_command(
            request_id="req-1", command_id="cmd-DUP", msg_id="m2",
            runtime_id="rt-c", generation=1, payload_hash="hash-B",
            state="QUEUED",
        )
        assert again is False
        row = store.get_command("req-1")
        assert row["command_id"] == "cmd-1"          # 首条保留
        assert row["payload_hash"] == "hash-a"
        cmds = store.list_commands(runtime_id="rt-c")
        assert len(cmds) == 1, "幂等入队不得产生重复行"
    finally:
        store.close()


def test_11_control_store_commands_update_and_list(tmp_path):
    """commands：update_command 推进状态 + list 过滤/分页。"""
    from codeagent.gateway.control_store import ControlStore

    store = ControlStore(db_path=tmp_path / "control.sqlite3")
    try:
        store.enqueue_command(
            request_id="r-a", command_id="c-a", runtime_id="rt-u",
            generation=1, payload_hash="h-a", state="QUEUED",
            created_at="2026-08-13T00:00:01Z",
        )
        store.enqueue_command(
            request_id="r-b", command_id="c-b", runtime_id="rt-u",
            generation=1, payload_hash="h-b", state="QUEUED",
            created_at="2026-08-13T00:00:02Z",
        )
        store.enqueue_command(
            request_id="r-other", command_id="c-o", runtime_id="rt-other",
            generation=1, payload_hash="h-o", state="QUEUED",
            created_at="2026-08-13T00:00:03Z",
        )
        updated = store.update_command("r-a", state="CLAIMED", msg_id="msg-a")
        assert updated["state"] == "CLAIMED"
        assert updated["msg_id"] == "msg-a"
        # 按 runtime 过滤 + 最新在前（created_at DESC）
        cmds = store.list_commands(runtime_id="rt-u")
        assert [c["request_id"] for c in cmds] == ["r-b", "r-a"]
        # state 过滤
        claimed = store.list_commands(runtime_id="rt-u", state="CLAIMED")
        assert [c["request_id"] for c in claimed] == ["r-a"]
        # 分页：limit=1 offset=1 → 第二页
        page2 = store.list_commands(runtime_id="rt-u", limit=1, offset=1)
        assert [c["request_id"] for c in page2] == ["r-a"]
    finally:
        store.close()


def test_12_control_store_persistence_across_instances(tmp_path):
    """跨实例持久化：close 后新 ControlStore 同库读回数据（重启恢复语义）。"""
    from codeagent.gateway.control_store import ControlStore

    db = tmp_path / "control.sqlite3"
    store = ControlStore(db_path=db)
    store.upsert_generation(
        runtime_id="rt-persist", current_generation=5,
        model_context=json.dumps({"model": "mdl", "provider": "prov", "variant": "", "epoch": 3}),
    )
    store.enqueue_command(
        request_id="req-persist", command_id="cmd-p", runtime_id="rt-persist",
        generation=5, payload_hash="hash-p", state="TURN_TRIGGERED",
    )
    store.close()

    reopened = ControlStore(db_path=db)
    try:
        gen = reopened.get_generation("rt-persist")
        assert gen is not None
        assert gen["current_generation"] == 5
        assert gen["model_context"] == {"model": "mdl", "provider": "prov", "variant": "", "epoch": 3}
        cmd = reopened.get_command("req-persist")
        assert cmd is not None
        assert cmd["state"] == "TURN_TRIGGERED"
        assert cmd["payload_hash"] == "hash-p"
    finally:
        reopened.close()


def test_13_control_store_update_command_full_fields(tmp_path):
    """update_command：turn_id/binding_epoch/backend_session_id/detail 分支 + 空 set 透传。"""
    from codeagent.gateway.control_store import ControlStore

    store = ControlStore(db_path=tmp_path / "control.sqlite3")
    try:
        store.enqueue_command(
            request_id="r-full", command_id="c-full", runtime_id="rt-f",
            generation=1, payload_hash="h-full", state="QUEUED",
        )
        # 一次性推进全部字段（None 字段保持不动）
        updated = store.update_command(
            "r-full", state="TRIGGERING", turn_id="t-9",
            binding_epoch=3, backend_session_id="b-new",
            detail={"gate": "hot"},
        )
        assert updated["state"] == "TRIGGERING"
        assert updated["turn_id"] == "t-9"
        assert updated["binding_epoch"] == 3
        assert updated["backend_session_id"] == "b-new"
        assert updated["detail"] == {"gate": "hot"}
        # 无任何字段 → 直接读回（透传 get_command，不写库）
        assert store.update_command("r-full")["state"] == "TRIGGERING"
    finally:
        store.close()


def test_14_control_store_get_generation_corrupt_context_fallback(tmp_path):
    """get_generation：model_context 列损坏（非 JSON）→ 容错回退空 dict。"""
    import sqlite3 as _sqlite3

    from codeagent.gateway.control_store import ControlStore

    store = ControlStore(db_path=tmp_path / "control.sqlite3")
    try:
        store.upsert_generation("rt-bad", current_generation=1, model_context='{"model": "ok"}')
        # 直接 SQL 灌入非法 JSON（模拟旧库/手工损坏）
        with store._connect() as conn:
            conn.execute(
                "UPDATE runtime_generations SET model_context = ? WHERE runtime_id = ?",
                ("{not-json", "rt-bad"),
            )
        got = store.get_generation("rt-bad")
        assert got is not None
        assert got["model_context"] == {}  # 解析失败 → 空 dict，不抛
    finally:
        store.close()
