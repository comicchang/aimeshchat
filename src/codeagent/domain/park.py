"""Park — Agent Park/复活机制的数据模型与生命周期管理。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Lifecycle(str, Enum):
    """Agent 生命周期状态。"""
    HOT_PARKED = "hot_parked"
    COLD_RESUMABLE = "cold_resumable"
    RELEASED = "released"
    BROKEN = "broken"


class ParkClass(str, Enum):
    """Park 分类——决定保留策略。"""
    ADVISOR = "advisor"        # oracle 系列：高上下文价值，长 TTL
    SHORT_TERM = "short-term"  # prometheus：短时 park，计划定稿后释放
    CONDITIONAL = "conditional"  # reviewer：仅当含"修复后复审"时 park
    NEVER = "never"            # 一次性 agent：完成后销毁


@dataclass(frozen=True)
class ParkManifest:
    """Park 实例的完整元数据。"""
    review_key: str
    swarm_session_id: str = ""
    agent_type: str = ""
    model: str = ""
    host: str = ""
    workdir: str = ""
    lifecycle: Lifecycle = Lifecycle.HOT_PARKED
    peer_agent_id: str = ""
    mailbox_agent_id: str = ""
    backend_session_id: str = ""
    parent_process_generation: str = ""
    created_at: float = 0.0
    last_activity_at: float = 0.0
    soft_expires_at: float = 0.0
    hard_expires_at: float = 0.0
    round: int = 0
    last_msg_id: str = ""
    summary_uri: str = ""
    transcript_uri: str = ""
    artifact_refs: list[str] = field(default_factory=list)
    config_fingerprint: str = ""
    schema_version: int = 1