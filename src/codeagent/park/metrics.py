"""Park 指标采集。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from codeagent.park.constants import park_data_dir


def _metrics_path() -> Path:
    d = park_data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / "metrics.jsonl"


def log_event(
    event: str,
    review_key: str = "",
    agent_type: str = "",
    method: str = "",
    success: bool = True,
    latency_ms: float = 0.0,
    round_num: int = 0,
    extra: Optional[dict] = None,
) -> None:
    """记录 park 事件到 metrics.jsonl。

    event: revive_hot | revive_warm | revive_cold | evict_ttl | evict_lru | release
    """
    record = {
        "timestamp": time.time(),
        "event": event,
        "review_key": review_key,
        "agent_type": agent_type,
        "method": method,
        "success": success,
        "latency_ms": latency_ms,
        "round": round_num,
    }
    if extra:
        record.update(extra)
    path = _metrics_path()
    with open(path, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_metrics(limit: int = 100) -> list[dict]:
    """读取最近 N 条指标记录。"""
    path = _metrics_path()
    if not path.exists():
        return []
    with open(path) as f:
        lines = f.readlines()
    return [json.loads(l) for l in lines[-limit:]]


def compute_stats() -> dict:
    """计算指标统计。

    Returns: {revive_count, success_rate, hot_count, warm_count, cold_count,
              evict_count, release_count, avg_latency_ms}
    """
    records = read_metrics()
    if not records:
        return {}
    revives = [r for r in records if r["event"].startswith("revive_")]
    if not revives:
        return {}
    success = sum(1 for r in revives if r.get("success"))
    return {
        "revive_count": len(revives),
        "success_rate": round(success / len(revives), 2),
        "hot_count": sum(1 for r in revives if r["event"] == "revive_hot"),
        "warm_count": sum(1 for r in revives if r["event"] == "revive_warm"),
        "cold_count": sum(1 for r in revives if r["event"] == "revive_cold"),
        "evict_count": sum(1 for r in records if r["event"].startswith("evict_")),
        "release_count": sum(1 for r in records if r["event"] == "release"),
        "avg_latency_ms": round(
            sum(r.get("latency_ms", 0) for r in revives) / len(revives), 1
        ),
    }