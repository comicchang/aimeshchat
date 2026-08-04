"""Park 默认配置与路径工具。"""
from __future__ import annotations

import os
from pathlib import Path

PARK_DEFAULTS: dict[str, object] = {
    "max_hot_parked": 3,
    "ttl_seconds": 3600,
    "hard_limit_seconds": 28800,
    "max_rounds": 5,
    "eviction_order": "lru",
    "snapshot_on_evict": True,
}


def park_state_dir() -> Path:
    """SQLite 状态数据库目录。"""
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "codeagent" / "park"


def park_data_dir() -> Path:
    """Snapshot/metrcis 数据目录。"""
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "codeagent" / "park"