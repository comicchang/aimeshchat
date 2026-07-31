"""mailbox-health — connectivity diagnostics for mailbox protocol."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from codeagent.mailbox.store import MailboxStore


def diagnose(store: MailboxStore, session_id: str, agent_id: str) -> dict:
    """Run 8 connectivity checks."""
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

    # 5. Status writable
    try:
        store.write_status(session_id, agent_id, "IDLE", "health check", "")
        checks["status_writable"] = True
    except Exception:
        checks["status_writable"] = False

    # 6. Status readable
    status = store.read_status(session_id, agent_id)
    checks["status_readable"] = status is not None

    # 7. Peek works
    try:
        result = store.peek(session_id, agent_id)
        checks["peek_works"] = isinstance(result, dict)
    except Exception:
        checks["peek_works"] = False

    # 8. Identity file (if set)
    import os
    identity_file = os.environ.get("OMP_MAILBOX_IDENTITY_FILE", "")
    if identity_file:
        checks["identity_file_set"] = True
        checks["identity_file_writable"] = Path(identity_file).parent.is_dir()
    else:
        checks["identity_file_set"] = False
        checks["identity_file_writable"] = None

    return checks


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="mailbox-health — connectivity diagnostics")
    p.add_argument("--session", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--json", action="store_true", dest="as_json")
    args = p.parse_args(argv)

    store = MailboxStore()
    result = diagnose(store, args.session, args.agent)

    if args.as_json:
        json.dump(result, sys.stdout, indent=2)
    else:
        all_ok = all(v for k, v in result.items() if k != "identity_file_writable" or v is not None)
        for k, v in result.items():
            status = "✓" if v else "✗" if v is False else "—"
            print(f"  {status} {k}: {v}")
        print(f"\n{'ALL CHECKS PASSED' if all_ok else 'SOME CHECKS FAILED'}")

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
