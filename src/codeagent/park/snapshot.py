"""Snapshot — 多轮 Review 上下文结构化保留与冷恢复。"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from codeagent.park.constants import park_data_dir

log = logging.getLogger(__name__)

# P2-14: review_key becomes a filesystem directory under snapshots/ — a
# crafted key ("../", "/abs", "a/../../b", glob) could read/write snapshot
# JSON outside the snapshots root. Whitelist before pathing; topic-style
# keys like "proj:oracle:arch:storage" (colons) are legal.
_REVIEW_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


@dataclass
class ReviewSnapshot:
    """单轮 review 的结构化摘要。"""
    review_key: str
    round: int
    last_question: str = ""
    last_conclusion: str = ""
    standing_constraints: list[str] = field(default_factory=list)
    evidence_list: list[str] = field(default_factory=list)
    rejected_approaches: list[str] = field(default_factory=list)
    unconfirmed_assumptions: list[str] = field(default_factory=list)
    pending_questions: list[str] = field(default_factory=list)
    recent_changes: list[str] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    generated_at: float = 0.0


def _snapshot_dir(review_key: str) -> Path:
    # P2-14: reject traversal/glob keys before they become a path component.
    # Exact "."/".." are rejected too: as a single component they resolve to
    # the snapshots root / its parent, escaping the per-review subdir.
    if not isinstance(review_key, str) or review_key in (".", "..") or not _REVIEW_KEY_RE.match(review_key):
        raise ValueError(f"invalid review_key: {review_key!r}")
    d = park_data_dir() / "snapshots" / review_key
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_snapshot(snapshot: ReviewSnapshot) -> Path:
    """持久化 snapshot (atomic: tmp → fsync → rename)."""
    path = _snapshot_dir(snapshot.review_key) / f"round_{snapshot.round}.json"
    data = {
        "review_key": snapshot.review_key,
        "round": snapshot.round,
        "last_question": snapshot.last_question,
        "last_conclusion": snapshot.last_conclusion,
        "standing_constraints": snapshot.standing_constraints,
        "evidence_list": snapshot.evidence_list,
        "rejected_approaches": snapshot.rejected_approaches,
        "unconfirmed_assumptions": snapshot.unconfirmed_assumptions,
        "pending_questions": snapshot.pending_questions,
        "recent_changes": snapshot.recent_changes,
        "artifact_refs": snapshot.artifact_refs,
        "generated_at": snapshot.generated_at or time.time(),
    }
    # P3-10: atomic write — tmp+fsync+rename prevents half-written JSON on crash.
    tmp_path = path.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    tmp_path.rename(path)
    return path


def load_snapshot(review_key: str, round_num: int) -> Optional[ReviewSnapshot]:
    """按轮次加载 snapshot。"""
    try:
        path = _snapshot_dir(review_key) / f"round_{round_num}.json"
    except ValueError as exc:
        # P2-14: invalid key → no valid snapshot (registry sweep calls this
        # with DB-stored keys and must not crash).
        log.warning("load_snapshot: %s", exc)
        return None
    if not path.exists():
        return None
    with open(path) as f:
        return ReviewSnapshot(**json.load(f))


def latest_snapshot(review_key: str) -> Optional[ReviewSnapshot]:
    """加载最新轮次的 snapshot。"""
    try:
        d = _snapshot_dir(review_key)
    except ValueError as exc:
        # P2-14: invalid key → no valid snapshot (registry sweep path).
        log.warning("latest_snapshot: %s", exc)
        return None
    # P0-1: 按整数轮次排序（int(stem)），而非文件名字典序——
    # P3-10: tolerate non-integer stems (e.g. round_backup.json) by filtering.
    rounds = sorted(
        (p for p in d.glob("round_*.json") if p.stem.split("_", 1)[1].isdigit()),
        key=lambda p: int(p.stem.split("_", 1)[1]),
    )
    if not rounds:
        return None
    with open(rounds[-1]) as f:
        return ReviewSnapshot(**json.load(f))