"""mailbox-hook — peek-only notification hook.

P1-3: 输出改 stderr，不混入 runtime stdout（OMP 插件 hook 与 oracle
runtime 共享 stdout 流，mailbox 通知会打断 oracle 产出）。
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


def _should_suppress(pending: int, messages: list[dict]) -> bool:
    """去重：相同 pending 数 + 相同消息摘要 → suppress。"""
    try:
        digest = hashlib.sha256(json.dumps(
            [{"k": m["kind"], "f": m["from"], "s": m["subject"]} for m in messages],
            sort_keys=True,
        ).encode()).hexdigest()[:16]
        data = json.loads(_DEDUP_FILE.read_text()) if _DEDUP_FILE.exists() else {}
        if data.get("digest") == digest and data.get("pending") == pending:
            import time
            if time.time() - data.get("ts", 0) < _DEDUP_WINDOW:
                return True
        return False
    except Exception:
        return False


def _save_dedup(pending: int, messages: list[dict]) -> None:
    import time
    try:
        _DEDUP_FILE.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(json.dumps(
            [{"k": m["kind"], "f": m["from"], "s": m["subject"]} for m in messages],
            sort_keys=True,
        ).encode()).hexdigest()[:16]
        _DEDUP_FILE.write_text(json.dumps({
            "digest": digest, "pending": pending, "ts": time.time(),
        }))
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
        if _should_suppress(result["pending"], messages):
            return  # 去重：30s 内相同通知不重复输出
        print(f"📬 MAILBOX: {result['pending']} pending", file=sys.stderr)
        for m in messages:
            print(f"  [{m['kind']}] {m['from']}: {m['subject']}", file=sys.stderr)
        _save_dedup(result["pending"], messages)
    else:
        print("📭 MAILBOX: empty", file=sys.stderr)


if __name__ == "__main__":
    main()
