"""Router — Hot→Warm→Cold 决策树。"""
from __future__ import annotations

from typing import Callable, Optional

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


def revive_or_spawn(
    review_key: str,
    prompt: str = "",
    is_alive: Optional[Callable[[ParkManifest], bool]] = None,
) -> ReviveResult:
    """Hot→Warm→Cold 决策树（纯决策层，不执行操作）。

    返回值是决策结果，实际执行由调用方（manager/CLI）负责：
    - method="hot": 调用方应 `hub send` 到 manifest.peer_agent_id
    - method="warm": 调用方应 `codeagent run --session-key <key> --resume`
    - method="cold": 调用方应 spawn 新 agent + 注入 context（作为首轮 prompt）
    - method="failed": 调用方应报告错误并走降级

    1. Hot revive: 同进程 hub send（由调用方执行，本函数返回 manifest）
    2. Warm resume: 同 session-key 恢复 backend session
    3. Cold reconstruction: 新实例 + curated snapshot

    D1: *is_alive* 可选回调注入 runtime alive 检查。当 lifecycle==HOT_PARKED
    但 is_alive 返回 False 时，降级为 warm/cold（避免将已死 runtime 当作 hot）。
    """
    registry = ParkRegistry()
    manifest = registry.lookup(review_key)

    if manifest and manifest.lifecycle == Lifecycle.HOT_PARKED:
        # D1: hot 判断须验证 runtime 真正 alive（不仅仅是 lifecycle 标记）。
        alive = is_alive(manifest) if is_alive is not None else True
        if alive:
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
        # Runtime 标记 HOT_PARKED 但已死 — 降级到 warm/cold 路径。
        # 若有 backend_session_id 则 warm，否则 fall-through 到 cold。

    # P1-2: COLD_RESUMABLE 或 RELEASED_SOFT 且有 backend_session_id → warm。
    #   - COLD_RESUMABLE: agent 因进程退出而失去 peer，但 backend session 仍在。
    #   - RELEASED_SOFT:  agent 被显式 release，但 backend session 尚未被清理。
    #   - HOT_PARKED 但 runtime 已死（D1 降级）也走此路径。
    #     无 backend_session_id 则 fall-through 到 cold reconstruction。
    if manifest and manifest.backend_session_id:
        if manifest.lifecycle in (Lifecycle.COLD_RESUMABLE, Lifecycle.RELEASED_SOFT):
            return ReviveResult(
                success=True,
                method="warm",
                context=(
                    f"Warm resume available ({manifest.lifecycle.value}): "
                    f"backend_session_id={manifest.backend_session_id}"
                ),
                manifest=manifest,
                prompt=prompt,
            )
        # D1: HOT_PARKED 但 runtime 已死 + 有 backend_session_id → warm
        if manifest.lifecycle == Lifecycle.HOT_PARKED and is_alive is not None:
            return ReviveResult(
                success=True,
                method="warm",
                context=(
                    f"Warm resume (HOT_PARKED but runtime dead): "
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