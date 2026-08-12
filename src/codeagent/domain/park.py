"""Park — Agent Park/复活机制的数据模型与生命周期管理。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Lifecycle(str, Enum):
    """Agent 生命周期状态。

    P1-1: RELEASED 分裂为 RELEASED_SOFT（可 revive，暖释放）与
    RELEASED_HARD（真销毁，终态）。RELEASED 保留为别名以兼容存量代码
    和旧数据库行——迁移会将 lifecycle='released' 统一改为 'released_soft'。
    """
    HOT_PARKED = "hot_parked"
    COLD_RESUMABLE = "cold_resumable"
    RELEASED_SOFT = "released_soft"   # P1-1: 暖释放——行保留，可 revive
    RELEASED_HARD = "released_hard"   # P1-1: 硬销毁——真删除行
    BROKEN = "broken"

    # P1-1: 向后兼容别名——旧枚举成员名映射到新值。
    # 新代码应直接使用 RELEASED_SOFT / RELEASED_HARD。
    RELEASED = "released_soft"

    @classmethod
    def _missing_(cls, value: object) -> "Lifecycle | None":
        """兼容旧 lifecycle='released' 值 → RELEASED_SOFT。

        已有 DB 行和存量 JSON 中的 lifecycle='released' 能被正确解析。
        """
        if value == "released":
            return cls.RELEASED_SOFT
        return None


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
    release_mode: str = ""           # P1-1: soft / hard / 空（未 release）
    omp_session_path: str = ""       # P1-1: OMP session 文件路径（revive warm 用）
