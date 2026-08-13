"""Oracle 会话事后精简脚本——release 后运行，杀日志爆炸。

功能（5 步）：
  1. 定位 sid 目录（backend_session_id → ~/.omp/agent/sessions/**/<sid>）。
  2. 删除 <N>.bash-original.log（全量 artifact，45–293MB）。
  3. <N>.bash.log / .eval.log / .read.* 只保留前 2KB（truncate 到 head）。
  4. <sid>.jsonl 逐行过滤：保留 role in {user, assistant} 消息 +
     title/session 元数据头行（≈0.5KB，保留 session identity）。
     丢弃 toolResult / developer / tool_call / tool_result / tool_execution_start / end。
  5. __advisor.jsonl 删除（若 advisor.enabled 漏关导致的冗余转录）。

安全约束：
  - **禁止 live 运行**——卡死检测依赖 TOOL_FINISHED/ASSISTANT_PROGRESS 事件，
    仅 release 后运行。live_guard 检测终端标记（session_exit 等），
    无终端标记时拒绝运行（force=True 覆盖）。
  - 不破坏 oracle result 提取（assistant 文本完整保留）。
  - 不破坏进度走 gateway 事件流（不依赖 JSONL）。
  - 幂等：重复运行结果相同（已删除的文件跳过，已过滤的 jsonl 不重写）。

用法：
  CLI:  python -m codeagent.scripts.oracle_transcript_strip <sid> [--head-bytes N] [--force] [--dry-run]
  模块: from codeagent.scripts.oracle_transcript_strip import strip_oracle_session, strip_for_manifest

Release 调用点：cmd_oracle_release → _strip_oracle_transcript(manifest) → strip_for_manifest
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

# ── 常量 ──────────────────────────────────────────────────────────────

# 默认 head 字节数（bash.log / eval.log / read.* 保留前 N 字节）
_DEFAULT_HEAD_BYTES = 2048

# OMP 会话根目录
_SESSIONS_ROOT = Path.home() / ".omp" / "agent" / "sessions"

# JSONL 中应保留的 message role 集合
_KEEP_ROLES = frozenset({"user", "assistant", "toolResult"})

# JSONL 中应保留的元数据行 type 集合（session identity，≈0.5KB）
_KEEP_HEADER_TYPES = frozenset({"title", "session"})

# 终端标记 customType——session 已结束的强信号
_TERMINAL_CUSTOM_TYPES = frozenset({
    "session_exit", "agent_end", "agent_stop",
    "session_shutdown", "process_exit",
})


# ── 定位 ──────────────────────────────────────────────────────────────

def _locate_session_dir(sid: str) -> Optional[Path]:
    """定位 sid 的会话目录。

    递归扫描 ``~/.omp/agent/sessions/**`` 找 ``*_<sid>.jsonl``，
    返回其父目录（mtime 最新的匹配）。
    """
    if not _SESSIONS_ROOT.is_dir():
        return None

    best: Optional[Path] = None
    for dirpath, _dirnames, filenames in os.walk(_SESSIONS_ROOT):
        for name in filenames:
            if not name.endswith(f"_{sid}.jsonl"):
                continue
            p = Path(dirpath) / name
            if best is None or p.stat().st_mtime > best.parent.stat().st_mtime:
                best = p
    return best.parent if best is not None else None


def _locate_session_jsonl(sid: str, session_dir: Path) -> Optional[Path]:
    """在 session_dir 中找 ``*_<sid>.jsonl``（mtime 最新）。"""
    candidates = sorted(session_dir.glob(f"*_{sid}.jsonl"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


# ── live guard ────────────────────────────────────────────────────────

def _is_session_terminal(jsonl_path: Path) -> bool:
    """检查 jsonl 最后一行是否为终端标记（session 已结束）。

    读文件尾部 4KB（覆盖任意长度 JSONL 行），取最后一个非空行解析。
    返回 True 表示 session 已正常结束（安全可 strip）。
    """
    try:
        with open(jsonl_path, "rb") as f:
            f.seek(0, 2)
            fsize = f.tell()
            if fsize == 0:
                return True  # 空文件视为已结束
            # 读尾部 4KB（JSONL 单行极少超过 4KB）
            tail_bytes = 4096
            f.seek(max(fsize - tail_bytes, 0))
            tail = f.read(tail_bytes)
        # 取最后一个非空行
        for line in reversed(tail.split(b"\n")):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            ctype = obj.get("customType", "")
            return ctype in _TERMINAL_CUSTOM_TYPES
        return False
    except (OSError, json.JSONDecodeError):
        return False


# ── 步骤 2：删除 bash-original.log ───────────────────────────────────

def _delete_bash_original(session_dir: Path, sid: str) -> list[str]:
    """删除 session_dir 及 ``*_<sid>/`` 子目录中的 ``*.bash-original.log``。"""
    removed: list[str] = []
    targets = [session_dir]
    targets.extend(d for d in session_dir.glob(f"*_{sid}") if d.is_dir())

    for d in targets:
        for p in d.glob("*.bash-original.log"):
            try:
                p.unlink()
                removed.append(str(p))
            except OSError as exc:
                print(f"warning: delete bash-original failed: {p}: {exc}",
                      file=sys.stderr)
    return removed


# ── 步骤 3：truncate bash.log / eval.log / read.* ────────────────────

def _truncate_sidecar_logs(session_dir: Path, sid: str,
                           head_bytes: int = _DEFAULT_HEAD_BYTES) -> list[str]:
    """truncate ``*.bash.log`` / ``*.eval.log`` / ``*.read.*`` 到前 head_bytes 字节。

    已经 ≤ head_bytes 的文件不重写（幂等）。返回被 truncate 的文件列表。
    """
    truncated: list[str] = []
    targets = [session_dir]
    targets.extend(d for d in session_dir.glob(f"*_{sid}") if d.is_dir())

    for d in targets:
        patterns = ["*.bash.log", "*.eval.log", "*.read.*"]
        for pat in patterns:
            for p in d.glob(pat):
                try:
                    size = p.stat().st_size
                    if size <= head_bytes:
                        continue  # 已经足够小，不重写（幂等）
                    # 读前 N 字节后覆盖写回
                    with open(p, "rb") as f:
                        head = f.read(head_bytes)
                    # 添加截断标记注释（如果文件有内容）
                    with open(p, "wb") as f:
                        f.write(head)
                    truncated.append(f"{p} ({size}→{head_bytes})")
                except OSError as exc:
                    print(f"warning: truncate failed: {p}: {exc}",
                          file=sys.stderr)
    return truncated


# ── 步骤 4：过滤 JSONL ───────────────────────────────────────────────

def _filter_session_jsonl(jsonl_path: Path,
                          dry_run: bool = False) -> dict:
    """逐行过滤 ``<sid>.jsonl``：保留 user/assistant + title/session 头行。

    幂等：若所有行已满足过滤条件则不重写（避免 mtime 膨胀）。
    返回 {"kept": N, "dropped": M, "rewritten": bool, "original_bytes": int, "filtered_bytes": int}。
    """
    if not jsonl_path.exists():
        return {"kept": 0, "dropped": 0, "rewritten": False,
                "original_bytes": 0, "filtered_bytes": 0}

    kept_lines: list[str] = []
    kept = 0
    dropped = 0
    original_bytes = jsonl_path.stat().st_size

    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            raw = line.rstrip("\n")
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                dropped += 1
                continue

            t = obj.get("type", "")
            # 保留：title/session 元数据头行
            if t in _KEEP_HEADER_TYPES:
                kept_lines.append(raw)
                kept += 1
                continue
            # 保留：message 且 role in {user, assistant, toolResult}
            if t == "message":
                role = obj.get("message", {}).get("role", "")
                if role in _KEEP_ROLES:
                    kept_lines.append(raw)
                    kept += 1
                    continue
            # 保留：tool_execution_start（tool call 参数/命令——审计"做了什么"）
            if t == "custom" and obj.get("customType", "") == "tool_execution_start":
                kept_lines.append(raw)
                kept += 1
                continue
            # 丢弃：developer / custom_message / 其他 custom 等
            dropped += 1

    # 计算过滤后大小（dry_run 也需要报告）
    rewritten = False
    new_content = "\n".join(kept_lines) + "\n" if kept_lines else ""
    filtered_bytes = len(new_content.encode("utf-8"))
    if dropped > 0 and not dry_run:
        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        rewritten = True

    return {"kept": kept, "dropped": dropped, "rewritten": rewritten,
            "original_bytes": original_bytes, "filtered_bytes": filtered_bytes}


# ── 步骤 5：删除 __advisor.jsonl ─────────────────────────────────────

def _delete_advisor_files(session_dir: Path, sid: str) -> list[str]:
    """删除 ``__advisor*`` 文件（advisor 二评转录，可达 499MB）。

    扫描 session_dir 及 ``*_<sid>/`` 子目录。
    """
    removed: list[str] = []
    targets = [session_dir]
    targets.extend(d for d in session_dir.glob(f"*_{sid}") if d.is_dir())

    for d in targets:
        for p in d.glob("__advisor*"):
            try:
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                removed.append(str(p))
            except OSError as exc:
                print(f"warning: delete advisor file failed: {p}: {exc}",
                      file=sys.stderr)
    return removed


# ── 主入口 ────────────────────────────────────────────────────────────

def strip_oracle_session(
    sid: str,
    *,
    head_bytes: int = _DEFAULT_HEAD_BYTES,
    force: bool = False,
    dry_run: bool = False,
) -> dict:
    """精简 oracle 会话——删除 bash-original、truncate sidecar、过滤 jsonl、删 advisor。

    参数：
      sid:        backend_session_id（OMP 原生 session id）。
      head_bytes: bash.log/eval.log/read.* 保留前 N 字节（默认 2048）。
      force:      跳过 live guard（仅 release 路径使用——调用者保证非 live）。
      dry_run:    仅计算变更，不实际修改文件。

    返回报告 dict（幂等：重复运行返回相同或更少的变更）。
    """
    report: dict = {
        "sid": sid,
        "session_dir": None,
        "removed": [],
        "truncated": [],
        "jsonl": None,
        "advisors_removed": [],
        "live_guard": "skipped",  # force 时
    }
    if not sid:
        report["error"] = "empty_sid"
        return report

    session_dir = _locate_session_dir(sid)
    if session_dir is None:
        report["error"] = "session_not_found"
        return report
    report["session_dir"] = str(session_dir)

    # ── live guard ────────────────────────────────────────────────────
    jsonl_path = _locate_session_jsonl(sid, session_dir)
    if jsonl_path is not None and not force:
        if not _is_session_terminal(jsonl_path):
            report["live_guard"] = "refused"
            report["error"] = "session_looks_live"
            report["hint"] = (
                "会话无终端标记（session_exit 等），可能仍在运行。"
                "仅 release 后运行，或使用 --force 覆盖。"
            )
            return report
        report["live_guard"] = "passed"
    elif force:
        report["live_guard"] = "forced"

    # ── 步骤 2：删除 bash-original.log ────────────────────────────────
    report["removed"] = _delete_bash_original(session_dir, sid)

    # ── 步骤 3：truncate sidecar logs ─────────────────────────────────
    report["truncated"] = _truncate_sidecar_logs(session_dir, sid,
                                                 head_bytes=head_bytes)

    # ── 步骤 4：过滤 JSONL ───────────────────────────────────────────
    if jsonl_path is not None:
        report["jsonl"] = _filter_session_jsonl(jsonl_path, dry_run=dry_run)
    else:
        report["jsonl"] = {"kept": 0, "dropped": 0, "rewritten": False,
                           "original_bytes": 0, "filtered_bytes": 0,
                           "note": "jsonl_not_found"}

    # ── 步骤 5：删除 __advisor.jsonl ──────────────────────────────────
    report["advisors_removed"] = _delete_advisor_files(session_dir, sid)

    return report


def strip_for_manifest(manifest, *, head_bytes: int = _DEFAULT_HEAD_BYTES,
                       force: bool = True, dry_run: bool = False) -> dict:
    """manifest-aware 包装——从 manifest.backend_session_id 提取 sid。

    ``force=True`` 默认：release 路径调用时 runtime 已停止，允许跳过 live guard。
    """
    sid = getattr(manifest, "backend_session_id", "") or ""
    return strip_oracle_session(sid, head_bytes=head_bytes,
                                force=force, dry_run=dry_run)


# ── CLI ───────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    """CLI 入口：``python -m codeagent.scripts.oracle_transcript_strip <sid>``"""
    parser = argparse.ArgumentParser(
        description="Oracle 会话事后精简（release 后运行）——杀日志爆炸",
    )
    parser.add_argument("sid", help="backend_session_id（OMP 原生 session id）")
    parser.add_argument("--head-bytes", type=int, default=_DEFAULT_HEAD_BYTES,
                        help=f"bash.log/eval.log/read.* 保留前 N 字节（默认 {_DEFAULT_HEAD_BYTES}）")
    parser.add_argument("--force", action="store_true",
                        help="跳过 live guard（仅确认 session 已停止后使用）")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅计算变更，不实际修改文件")

    args = parser.parse_args(argv)
    report = strip_oracle_session(
        args.sid,
        head_bytes=args.head_bytes,
        force=args.force,
        dry_run=args.dry_run,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if "error" not in report else 1


if __name__ == "__main__":
    raise SystemExit(main())
