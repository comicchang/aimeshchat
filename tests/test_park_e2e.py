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
    db = _test_tmpdir / "state" / "codeagent" / "park" / "park.sqlite3"
    if db.exists():
        db.unlink()
    snap_dir = _test_tmpdir / "data" / "codeagent" / "park" / "snapshots"
    if snap_dir.exists():
        shutil.rmtree(snap_dir)
    metrics = _test_tmpdir / "data" / "codeagent" / "park" / "metrics.jsonl"
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
