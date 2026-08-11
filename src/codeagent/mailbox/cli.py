"""Standalone mailbox CLI — 100% compatible with original tools/mailbox.

Usage: mailbox <subcommand> [args]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from codeagent.mailbox.protocol import VALID_KINDS, VALID_STATES, AttachmentRef
from codeagent.mailbox.store import MailboxStore


def _parse_attachment_args(raw: list[str]) -> list[AttachmentRef]:
    """Parse repeatable ``--attachment`` JSON strings into AttachmentRef list.

    Raises ``ValueError`` with a clear message on malformed input.
    """
    refs: list[AttachmentRef] = []
    for item in raw:
        try:
            d = json.loads(item)
        except json.JSONDecodeError as e:
            raise ValueError(f"--attachment is not valid JSON: {e}") from e
        if not isinstance(d, dict):
            raise ValueError(f"--attachment must be a JSON object, got {type(d).__name__}")
        refs.append(AttachmentRef.from_dict(d))
    return refs


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="mailbox — session-based direct-inbox CLI")
    sub = p.add_subparsers(dest="cmd")

    # session-init
    si = sub.add_parser("session-init")
    si.add_argument("--session", required=True)
    si.add_argument("--manager", required=True)
    si.add_argument("--agents", required=True, help="comma-separated agent IDs")
    si.add_argument("--acl", default=None, help="B4-Manifest: ACL JSON {authority,allowed_senders,room_members,policy} (optional)")

    # send
    s = sub.add_parser("send")
    s.add_argument("--session", required=True)
    s.add_argument("--from", required=True, dest="from_worker")
    s.add_argument("--to", required=True, help="recipient agent ID, or '*' to broadcast to all except the sender")
    s.add_argument("--subject", required=True)
    s.add_argument("--body", required=True)
    s.add_argument("--kind", default="TASK", choices=sorted(VALID_KINDS))
    s.add_argument("--reply-to", default="")
    s.add_argument("--run-id", default="")
    s.add_argument("--request-id", default="")
    s.add_argument("--causation-id", default="", help="parent msg_id for forward chains (Top4)")
    s.add_argument("--trace-id", default="", help="cross-host trace id (B2)")
    s.add_argument("--msg-id", default=None, help="caller-provided msg_id for idempotent send")
    s.add_argument(
        "--require-ack", action="store_true", default=False,
        help="v2: demand a RECEIPT(READ) from the recipient when consumed",
    )
    s.add_argument(
        "--receipt-type", default="",
        help="v2: receipt_type for RECEIPT-kind messages (e.g. READ)",
    )
    s.add_argument(
        "--attachment", action="append", default=[],
        help=(
            'JSON object with artifact_id, source_host, remote_root, '
            'relative_path, size, sha256 (required); media_type (optional). '
            'Repeat for multiple attachments.'
        ),
    )

    # peek
    pk = sub.add_parser("peek")
    pk.add_argument("--session", required=True)
    pk.add_argument("--agent", required=True)
    pk.add_argument("--max-messages", type=int, default=5)
    pk.add_argument("--max-subject", type=int, default=80)

    # read
    rd = sub.add_parser("read")
    rd.add_argument("--session", required=True)
    rd.add_argument("--agent", required=True)
    rd.add_argument("--owner", required=True)
    rd.add_argument("--json", action="store_true", help="output full JSON")

    # finalize
    fn = sub.add_parser("finalize")
    fn.add_argument("--session", required=True)
    fn.add_argument("--agent", required=True)
    fn.add_argument("--msg-id", required=True)
    fn.add_argument("--owner", required=True)

    # release
    rl = sub.add_parser("release")
    rl.add_argument("--session", required=True)
    rl.add_argument("--agent", required=True)
    rl.add_argument("--msg-id", required=True)
    rl.add_argument("--owner", required=True)

    # renew (P2-10: lease renewal for long-running claims)
    rn = sub.add_parser("renew")
    rn.add_argument("--session", required=True)
    rn.add_argument("--agent", required=True)
    rn.add_argument("--msg-id", required=True)
    rn.add_argument("--owner", required=True)

    # recover-stale
    rs = sub.add_parser("recover-stale")
    rs.add_argument("--session", required=True)
    rs.add_argument("--agent", required=True)

    # status
    st = sub.add_parser("status")
    st.add_argument("--session", required=True)
    st.add_argument("--agent", required=True)
    st.add_argument("--state", required=True, choices=sorted(VALID_STATES))
    st.add_argument("--current-task", default="")
    st.add_argument("--last-conclusion", default="")

    # clear
    clr = sub.add_parser("clear")
    clr.add_argument("--session", required=True)
    clr.add_argument("--agent", required=True)
    clr.add_argument("--prune-stale", action="store_true")

    # A6: session-clean — whole-session retention cleanup
    sc = sub.add_parser("session-clean", help="Delete whole sessions older than N days "
                                              "(history/archive/events/outbox)")
    sc.add_argument("--older-than", type=int, required=True,
                    help="Delete sessions older than this many days")
    sc.add_argument("--json", action="store_true", help="JSON output")

    # stats
    ss = sub.add_parser("stats")
    ss.add_argument("--session", required=True)
    ss.add_argument("--agent", required=True)

    # history
    hs = sub.add_parser("history", help="read canonical session history (newest first)")
    hs.add_argument("--session", required=True)
    hs.add_argument("--since", default=None, help="only messages with created_at >= this timestamp (ISO-8601)")
    hs.add_argument("--before", default=None, help="only messages with created_at < this timestamp (ISO-8601)")
    hs.add_argument("--limit", type=int, default=None)
    hs.add_argument("--from", default=None, dest="from_worker", help="only messages from this sender")
    hs.add_argument("--kind", default=None, choices=sorted(VALID_KINDS))
    hs.add_argument("--json", action="store_true", help="output full JSON array")

    # check (legacy)
    ck = sub.add_parser("check")
    ck.add_argument("--session", required=True)
    ck.add_argument("--agent", required=True)
    ck.add_argument("--json", action="store_true")
    ck.add_argument("--max-messages", type=int, default=0)

    # Global options
    p.add_argument("--mailbox-root", help="Override MAILBOX_ROOT")

    args = p.parse_args(argv)
    root = Path(args.mailbox_root) if args.mailbox_root else None
    store = MailboxStore(root=root)

    try:
        if args.cmd == "session-init":
            acl = json.loads(args.acl) if args.acl else None
            print(store.session_init(args.session, args.manager, args.agents.split(","), acl=acl))
        elif args.cmd == "send":
            attachments = _parse_attachment_args(args.attachment)
            # v2: send through MailboxService so require_ack / receipt_type
            # are honored and the JSON field is exactly `require_ack`.
            from codeagent.mailbox.service import MailboxService
            svc = MailboxService(store=store)
            receipt = svc.send(
                args.session, args.from_worker, args.to,
                args.subject, args.body, args.kind,
                args.reply_to, args.run_id, args.request_id,
                trace_id=args.trace_id, causation_id=args.causation_id,
                attachments=attachments or None,
                msg_id=args.msg_id,
                require_ack=args.require_ack,
                receipt_type=args.receipt_type,
            )
            if receipt.status == "failed":
                raise ValueError(receipt.error or "send failed")
            if receipt.detail:
                print(receipt.detail)
            else:
                print(f"sent → {args.to}/inbox/{receipt.msg_id}.json")
        elif args.cmd == "peek":
            result = store.peek(args.session, args.agent, args.max_messages, args.max_subject)
            json.dump(result, sys.stdout, ensure_ascii=False)
        elif args.cmd == "read":
            # v2: claim via MailboxService — emits RECEIPT(READ) for
            # require_ack messages; never consumes when the ack route is
            # unresolved.
            from codeagent.mailbox.service import ACK_ROUTE_UNRESOLVED, MailboxService
            svc = MailboxService(store=store)
            outcome = svc.read(args.session, args.agent, args.owner)
            if outcome.status == ACK_ROUTE_UNRESOLVED:
                print(outcome.error, file=sys.stderr)
                sys.exit(2)  # terminal — retry is pointless (roster missing)
            msg = outcome.message
            if msg:
                if getattr(args, "json", False):
                    json.dump(msg, sys.stdout, ensure_ascii=False)
                else:
                    print(f"FROM: {msg['from']}  KIND: {msg.get('kind', '?')}")
                    print(f"SUBJECT: {msg['subject']}")
                    print(f"BODY: {msg['body']}")
        elif args.cmd == "finalize":
            print(store.finalize(args.session, args.agent, args.msg_id, args.owner))
        elif args.cmd == "release":
            print(store.release(args.session, args.agent, args.msg_id, args.owner))
        elif args.cmd == "recover-stale":
            print(store.recover_stale(args.session, args.agent))
        elif args.cmd == "renew":
            if not store.renew_claim(args.session, args.agent, args.msg_id, args.owner):
                raise ValueError(
                    f"claim not renewable: {args.msg_id} (missing / owner mismatch)"
                )
            print(f"renewed claim {args.msg_id}")
        elif args.cmd == "status":
            print(store.write_status(args.session, args.agent, args.state, args.current_task, args.last_conclusion))
        elif args.cmd == "clear":
            print(store.clear(args.session, args.agent, prune_stale=args.prune_stale))
        elif args.cmd == "session-clean":
            result = store.clean_older_than(args.older_than)
            if args.json:
                json.dump(result, sys.stdout, ensure_ascii=False)
            else:
                for sid in result["removed"]:
                    print(f"removed {sid}")
                for sid in result["skipped"]:
                    print(f"skipped {sid} (active park lease / locked)")
                print(f"session-clean: removed {len(result['removed'])}, "
                      f"skipped {len(result['skipped'])}")
        elif args.cmd == "stats":
            for d, c in store.stats(args.session, args.agent).items():
                print(f"{d}: {c}")
        elif args.cmd == "history":
            msgs = store.read_history(
                args.session,
                since=args.since, before=args.before, limit=args.limit,
                from_id=args.from_worker, kind=args.kind,
            )
            if args.json:
                json.dump(msgs, sys.stdout, ensure_ascii=False)
            else:
                for msg in msgs:
                    print(f"FROM: {msg['from']}  KIND: {msg.get('kind', '?')}")
                    print(f"SUBJECT: {msg['subject']}")
                    print(f"BODY: {msg['body']}")
                    print("---")
        elif args.cmd == "check":
            results = store.check(args.session, args.agent, args.max_messages)
            for msg in results:
                if args.json:
                    print(json.dumps(msg, ensure_ascii=False))
                else:
                    print(f"FROM: {msg['from']}  KIND: {msg.get('kind', '?')}")
                    print(f"SUBJECT: {msg['subject']}")
                    print(f"BODY: {msg['body']}")
                    print("---")
        else:
            p.print_help()
    except ValueError as e:
        # P2 (oracle-lite): terminal 错误用 exit 2（校验/roster/幂等冲突——
        # 重试无意义），delivery 侧据此分类，不再只靠关键字匹配。
        print(str(e), file=sys.stderr)
        sys.exit(2)  # terminal
    except Exception as e:
        # exit 1 = retryable（未知/环境错误，重试可能成功）
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
