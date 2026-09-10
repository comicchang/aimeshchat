"""mailbox-hook — peek-only notification hook.

P1-3: 输出改 stderr，不混入 runtime stdout（OMP 插件 hook 与 oracle
runtime 共享 stdout 流，mailbox 通知会打断 oracle 产出）。
digest 含 session_id/agent_id，跨会话同形消息不互相吞掉。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from codeagent.mailbox.store import MailboxStore

# P1-3: 30s 去重窗口——相同 pending 数 + 相同消息 hash 时不重复通知
_DEDUP_FILE = Path.home() / ".cache" / "codeagent" / "mailbox-hook-dedup.json"
_DEDUP_WINDOW = 30  # seconds


def _digest(session_id: str, agent_id: str, messages: list[dict]) -> str:
    return hashlib.sha256(json.dumps(
        {"session": session_id, "agent": agent_id,
         "messages": [{"k": m["kind"], "f": m["from"], "s": m["subject"]} for m in messages]},
        sort_keys=True,
    ).encode()).hexdigest()[:16]


def _should_suppress(session_id: str, agent_id: str, pending: int, messages: list[dict]) -> bool:
    """去重：同 session/agent 且相同 pending 数 + 相同消息摘要 → suppress。"""
    try:
        data = json.loads(_DEDUP_FILE.read_text()) if _DEDUP_FILE.exists() else {}
    except Exception:
        return False  # fail-open: 读不出状态时不抑制
    entry = data.get(f"{session_id}/{agent_id}")
    if not isinstance(entry, dict):
        return False
    import time
    if (entry.get("digest") == _digest(session_id, agent_id, messages)
            and entry.get("pending") == pending
            and time.time() - entry.get("ts", 0) < _DEDUP_WINDOW):
        return True
    return False


def _save_dedup(session_id: str, agent_id: str, pending: int, messages: list[dict]) -> None:
    import time
    try:
        data = json.loads(_DEDUP_FILE.read_text()) if _DEDUP_FILE.exists() else {}
        if not isinstance(data, dict):
            data = {}
        data[f"{session_id}/{agent_id}"] = {
            "digest": _digest(session_id, agent_id, messages),
            "pending": pending,
            "ts": time.time(),
        }
        # 裁剪过期条目（2 倍窗口外），防止 map 无界增长
        cutoff = time.time() - 2 * _DEDUP_WINDOW
        for key, entry in list(data.items()):
            if not isinstance(entry, dict) or entry.get("ts", 0) < cutoff:
                del data[key]
        _DEDUP_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DEDUP_FILE.write_text(json.dumps(data))
    except Exception:
        pass


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="mailbox-hook — peek-only notification")
    p.add_argument("session_id")
    p.add_argument("agent_id")
    args = p.parse_args(argv)

    store = MailboxStore()
    result = store.peek(args.session_id, args.agent_id)
    if result["pending"] > 0:
        messages = result["messages"]
        if _should_suppress(args.session_id, args.agent_id, result["pending"], messages):
            return  # 去重：30s 内相同通知不重复输出
        print(f"📬 MAILBOX: {result['pending']} pending", file=sys.stderr)
        for m in messages:
            print(f"  [{m['kind']}] {m['from']}: {m['subject']}", file=sys.stderr)
        _save_dedup(args.session_id, args.agent_id, result["pending"], messages)
    else:
        print("📭 MAILBOX: empty", file=sys.stderr)


if __name__ == "__main__":
    main()
