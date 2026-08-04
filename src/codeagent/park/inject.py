"""Cold reconstruction — 上下文注入。"""
from __future__ import annotations

from codeagent.park.snapshot import latest_snapshot


def build_cold_context(review_key: str) -> str:
    """Cold reconstruction 上下文注入。

    读取最新 snapshot，生成首轮 prompt 前缀。
    新 Agent 输出三段式结论：
    ① 仍成立的结论
    ② 需重新审查的结论
    ③ 因新证据废弃的结论

    无 snapshot 时返回 fallback prompt。
    """
    snap = latest_snapshot(review_key)
    if not snap:
        return (
            f"你是 '{review_key}' 的重建实例（非原实例复活）。\n"
            f"未找到历史 snapshot，请从头开始分析。\n"
            f"请先确认你能理解以上上下文，然后列出：\n"
            f"1. 仍成立的结论\n"
            f"2. 需重新审查的结论\n"
            f"3. 因新证据废弃的结论"
        )

    def _fmt(items: list[str]) -> str:
        return "\n".join(f"- {i}" for i in items) if items else "(无)"

    return (
        f"你是 '{review_key}' 的重建实例（非原实例复活）。\n"
        f"以下是继承的上下文（来自第 {snap.round} 轮后的 snapshot）：\n\n"
        f"上次问题：{snap.last_question}\n"
        f"上次结论：{snap.last_conclusion}\n\n"
        f"仍成立的约束：\n{_fmt(snap.standing_constraints)}\n\n"
        f"证据清单：\n{_fmt(snap.evidence_list)}\n\n"
        f"已否决方案及原因：\n{_fmt(snap.rejected_approaches)}\n\n"
        f"待回答问题：\n{_fmt(snap.pending_questions)}\n\n"
        f"请先确认你能理解以上上下文，然后列出：\n"
        f"1. 仍成立的结论\n"
        f"2. 需重新审查的结论\n"
        f"3. 因新证据废弃的结论"
    )