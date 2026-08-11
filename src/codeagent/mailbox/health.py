"""mailbox-health — connectivity diagnostics for mailbox protocol.

Read-only diagnostics. Never modifies status or mailbox state.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from codeagent.mailbox.store import MailboxStore


def diagnose(store: MailboxStore, session_id: str, agent_id: str) -> dict:
    """Run 8 read-only connectivity checks."""
    checks = {}

    # 1. Root exists
    checks["root_exists"] = store.root.is_dir()

    # 2. Session dir exists
    sd = store.session_dir(session_id)
    checks["session_dir_exists"] = sd.is_dir()

    # 3. Agent dir exists
    ad = store.agent_dir(session_id, agent_id)
    checks["agent_dir_exists"] = ad.is_dir()

    # 4. Inbox exists and readable
    inbox = store.agent_subdir(session_id, agent_id, "inbox")
    checks["inbox_readable"] = inbox.is_dir()

    # 5. Status readable (read-only — never writes)
    status = store.read_status(session_id, agent_id)
    checks["status_readable"] = status is not None
    if status:
        checks["status_state"] = status.state

    # 6. Peek works
    try:
        result = store.peek(session_id, agent_id)
        checks["peek_works"] = isinstance(result, dict)
    except Exception:
        checks["peek_works"] = False

    # 7. Processing dir exists (read-only check)
    processing = store.agent_subdir(session_id, agent_id, "processing")
    checks["processing_dir_exists"] = processing.is_dir()

    # 8. Identity file (if set)
    identity_file = os.environ.get("OMP_MAILBOX_IDENTITY_FILE", "")
    if identity_file:
        checks["identity_file_set"] = True
        p = Path(identity_file)
        checks["identity_file_exists"] = p.exists()
        # 改进项2: test actual write access to the parent directory, not just
        # directory existence.  p.parent.is_dir() does not catch read-only
        # mounts, permission errors, or immutable directory flags.
        parent = p.parent
        checks["identity_file_writable"] = parent.is_dir() and os.access(parent, os.W_OK)
    else:
        checks["identity_file_set"] = False
        checks["identity_file_writable"] = None

    return checks


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="mailbox-health — read-only connectivity diagnostics")
    p.add_argument("--session", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--json", action="store_true", dest="as_json")
    args = p.parse_args(argv)

    store = MailboxStore()
    result = diagnose(store, args.session, args.agent)

    # Compute all_ok before branching
    skip_keys = {"identity_file_writable", "status_state"}
    all_ok = all(
        v for k, v in result.items()
        if k not in skip_keys and v is not None
    )

    if args.as_json:
        json.dump(result, sys.stdout, indent=2)
    else:
        for k, v in result.items():
            status = "✓" if v else "✗" if v is False else "—"
            print(f"  {status} {k}: {v}")
        print(f"\n{'ALL CHECKS PASSED' if all_ok else 'SOME CHECKS FAILED'}")

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
