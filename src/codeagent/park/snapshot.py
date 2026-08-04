"""Snapshot — 多轮 Review 上下文结构化保留与冷恢复。"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from codeagent.park.constants import park_data_dir


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
    d = park_data_dir() / "snapshots" / review_key
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_snapshot(snapshot: ReviewSnapshot) -> Path:
    """持久化 snapshot。"""
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
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def load_snapshot(review_key: str, round_num: int) -> Optional[ReviewSnapshot]:
    """按轮次加载 snapshot。"""
    path = _snapshot_dir(review_key) / f"round_{round_num}.json"
    if not path.exists():
        return None
    with open(path) as f:
        return ReviewSnapshot(**json.load(f))


def latest_snapshot(review_key: str) -> Optional[ReviewSnapshot]:
    """加载最新轮次的 snapshot。"""
    d = _snapshot_dir(review_key)
    rounds = sorted(d.glob("round_*.json"))
    if not rounds:
        return None
    with open(rounds[-1]) as f:
        return ReviewSnapshot(**json.load(f))