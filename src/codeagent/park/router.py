"""Router — Hot→Warm→Cold 决策树。"""
from __future__ import annotations

from typing import Optional

from codeagent.domain.park import Lifecycle, ParkManifest
from codeagent.park.registry import ParkRegistry
from codeagent.park.inject import build_cold_context


class ReviveResult:
    """复活结果。"""

    def __init__(
        self,
        success: bool,
        method: str,
        context: str = "",
        manifest: Optional[ParkManifest] = None,
        prompt: str = "",
    ) -> None:
        self.success = success
        self.method = method  # "hot", "warm", "cold", "failed"
        self.context = context
        self.manifest = manifest
        self.prompt = prompt


def revive_or_spawn(review_key: str, prompt: str = "") -> ReviveResult:
    """Hot→Warm→Cold 决策树（纯决策层，不执行操作）。

    返回值是决策结果，实际执行由调用方（manager/CLI）负责：
    - method="hot": 调用方应 `hub send` 到 manifest.peer_agent_id
    - method="warm": 调用方应 `codeagent run --session-key <key> --resume`
    - method="cold": 调用方应 spawn 新 agent + 注入 context（作为首轮 prompt）
    - method="failed": 调用方应报告错误并走降级

    1. Hot revive: 同进程 hub send（由调用方执行，本函数返回 manifest）
    2. Warm resume: 同 session-key 恢复 backend session
    3. Cold reconstruction: 新实例 + curated snapshot
    """
    registry = ParkRegistry()
    manifest = registry.lookup(review_key)

    if manifest and manifest.lifecycle == Lifecycle.HOT_PARKED:
        return ReviveResult(
            success=True,
            method="hot",
            context=(
                f"Found HOT_PARKED instance for '{review_key}', "
                f"use hub send to peer_agent_id={manifest.peer_agent_id}"
            ),
            manifest=manifest,
            prompt=prompt,
        )

    # P0-4: 只有 COLD_RESUMABLE 可 warm-revivable；RELEASED 是终态，
    # 不应再恢复 backend session（终态过滤），一律走 cold reconstruction。
    if manifest and manifest.lifecycle == Lifecycle.COLD_RESUMABLE:
        if manifest.backend_session_id:
            return ReviveResult(
                success=True,
                method="warm",
                context=(
                    f"Warm resume available: "
                    f"backend_session_id={manifest.backend_session_id}"
                ),
                manifest=manifest,
                prompt=prompt,
            )

    # Cold reconstruction
    cold_context = build_cold_context(review_key)
    return ReviveResult(
        success=True,
        method="cold",
        context=cold_context,
        manifest=manifest,
        prompt=prompt,
    )


def park_revive(review_key: str, prompt: str = "") -> ReviveResult:
    """Public API: revive or spawn a park instance."""
    return revive_or_spawn(review_key, prompt)