"""mailbox-hook — peek-only notification hook."""
from __future__ import annotations

import argparse
import sys

from codeagent.mailbox.store import MailboxStore


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="mailbox-hook — peek-only notification")
    p.add_argument("session_id")
    p.add_argument("agent_id")
    args = p.parse_args(argv)

    store = MailboxStore()
    result = store.peek(args.session_id, args.agent_id)
    if result["pending"] > 0:
        print(f"📬 MAILBOX: {result['pending']} pending")
        for m in result["messages"]:
            print(f"  [{m['kind']}] {m['from']}: {m['subject']}")
    else:
        print("📭 MAILBOX: empty")


if __name__ == "__main__":
    main()
