#!/usr/bin/env python3
"""mailbox — session-based direct-inbox CLI (standalone, zero-dependency)

Shared root via $MAILBOX_ROOT env. Session directory layout:

  $MAILBOX_ROOT/<session_id>/
    session.json           # {manager, agents, created_at}
    manager/inbox|processing|archive/
    <agent>/inbox|processing|archive/status.json

Commands:
  session-init  --session <id> --manager <id> --agents <id,id,...>
  send          --session <id> --from <id> --to <id> --subject ... --body ...
  peek          --session <id> --agent <id>
  read          --session <id> --agent <id> --owner <id> [--json]
  finalize      --session <id> --agent <id> --msg-id <id> --owner <id>
  release       --session <id> --agent <id> --msg-id <id> --owner <id>
  recover-stale --session <id> --agent <id>
  check         --session <id> --agent <id>  (legacy: scan inbox only)
  status        --session <id> --agent <id> --state ...
  clear         --session <id> --agent <id>
  stats         --session <id> --agent <id>

Message JSON (8 required fields):
  session_id, from, to, subject, body, kind, msg_id, created_at
  [+ reply_to, run_id, request_id optional for correlation]

Kinds: TASK, REPORT, PROGRESS, EVIDENCE, QUESTION, RESPONSE, NOTICE
States: IDLE, BUSY, DONE, BLOCKED
"""

import json, os, sys, argparse, time
from pathlib import Path
from datetime import datetime, timezone

VALID_KINDS = {"TASK", "REPORT", "PROGRESS", "EVIDENCE", "QUESTION", "RESPONSE", "NOTICE"}
VALID_STATES = {"IDLE", "BUSY", "DONE", "BLOCKED"}
REQUIRED_FIELDS = {"session_id", "from", "to", "subject", "body", "kind", "msg_id", "created_at"}
LEASE_TIMEOUT_S = 300  # processing/ lease before stale recovery eligible
AGENT_ID_RE = r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,31}$"


def resolve_root():
    return Path(os.environ.get("MAILBOX_ROOT") or
                os.path.expanduser("~/Dropbox/logseq/pages/mi-docs/.mailbox"))


def _validate_agent_id(aid: str):
    import re
    if not re.match(AGENT_ID_RE, aid):
        sys.exit(f"invalid agent id: {aid}")


def session_dir(root: Path, session_id: str) -> Path:
    _validate_agent_id(session_id)
    return root / session_id


def agent_dir(root: Path, session_id: str, agent_id: str) -> Path:
    _validate_agent_id(agent_id)
    return session_dir(root, session_id) / agent_id


def agent_subdir(root: Path, session_id: str, agent_id: str, sub: str) -> Path:
    return agent_dir(root, session_id, agent_id) / sub


def _gen_msg_id(sender: str) -> str:
    """Timestamp-based message ID with random suffix for collision safety."""
    import random, string
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{sender}_{ts}_{suffix}"


def list_messages(inbox: Path) -> list[Path]:
    if not inbox.exists():
        return []
    return sorted(
        [f for f in inbox.glob("*.json")
         if f.is_file() and not f.is_symlink()
         and not f.name.startswith(".sync-conflict-")
         and not f.name.startswith(".tmp-")],
        key=lambda f: f.stat().st_mtime)


def validate_message(msg: dict, expected_agent: str, filename: str) -> tuple[bool, str]:
    if not isinstance(msg, dict):
        return False, "not a JSON object"
    missing = REQUIRED_FIELDS - set(msg.keys())
    if missing:
        return False, f"missing fields: {', '.join(sorted(missing))}"
    if msg["kind"] not in VALID_KINDS:
        return False, f"invalid kind: {msg['kind']}"
    if msg["msg_id"] + ".json" != filename:
        return False, f"msg_id mismatch: {msg['msg_id']} vs {filename}"
    if msg["to"] != expected_agent:
        return False, f"recipient mismatch: {msg['to']} vs {expected_agent}"
    if "/" in msg["msg_id"] or "\\" in msg["msg_id"]:
        return False, f"invalid msg_id (path separator): {msg['msg_id']}"
    return True, ""


def _validate_msg_schema(msg: dict, expected_session_id: str = None) -> tuple[bool, str]:
    """Full schema validation of a message dict for all 8 required fields + kinds."""
    if not isinstance(msg, dict):
        return False, "not a JSON object"
    missing = REQUIRED_FIELDS - set(msg.keys())
    if missing:
        return False, f"missing fields: {', '.join(sorted(missing))}"
    if msg["kind"] not in VALID_KINDS:
        return False, f"invalid kind: {msg['kind']}"
    if not isinstance(msg["subject"], str):
        return False, f"subject must be string"
    if not msg["subject"].strip():
        return False, "subject must be non-empty"
    if not isinstance(msg["body"], str):
        return False, f"body must be string"
    if not msg["body"].strip():
        return False, "body must be non-empty"
    if not isinstance(msg["session_id"], str):
        return False, f"session_id must be string"
    if expected_session_id is not None and msg["session_id"] != expected_session_id:
        return False, f"session_id mismatch: {msg['session_id']} vs {expected_session_id}"
    if not isinstance(msg["from"], str):
        return False, f"from must be string"
    if not isinstance(msg["to"], str):
        return False, f"to must be string"
    if not isinstance(msg["msg_id"], str):
        return False, f"msg_id must be string"
    if not isinstance(msg["created_at"], str):
        return False, f"created_at must be string"
    if "/" in msg["msg_id"] or "\\" in msg["msg_id"]:
        return False, f"invalid msg_id (path separator): {msg['msg_id']}"
    return True, ""


# ═══════════════════════════════════════════════════════════════════════
# Commands
# ═══════════════════════════════════════════════════════════════════════

def cmd_session_init(args):
    root = resolve_root()
    sd = session_dir(root, args.session)
    if sd.exists():
        sys.exit(f"session already exists: {args.session}")
    sd.mkdir(parents=True)

    meta = {
        "protocol_version": "2",
        "session_id": args.session,
        "manager": args.manager,
        "agents": sorted(set(args.agents.split(","))),
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    tmp = sd / ".tmp-session.json"
    with open(tmp, "w") as f:
        f.write(json.dumps(meta, indent=2, ensure_ascii=False))
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp), str(sd / "session.json"))

    # Pre-create all agent directories
    for aid in meta["agents"]:
        for sub in ["inbox", "processing", "archive"]:
            agent_subdir(root, args.session, aid, sub).mkdir(parents=True, exist_ok=True)
    # Manager directory
    for sub in ["inbox", "processing", "archive"]:
        agent_subdir(root, args.session, args.manager, sub).mkdir(parents=True, exist_ok=True)

    print(f"session {args.session} created: manager={args.manager}, agents={meta['agents']}")


def cmd_send(args):
    root = resolve_root()
    sd = session_dir(root, args.session)
    if not sd.exists():
        sys.exit(f"session not found: {args.session}")

    inbox = agent_subdir(root, args.session, args.to, "inbox")
    if not inbox.exists():
        sys.exit(f"agent not in session: {args.to}")
    sender_inbox = agent_subdir(root, args.session, args.from_worker, "inbox")
    if not sender_inbox.exists():
        sys.exit(f"sender not in roster: {args.from_worker}")

    if args.kind not in VALID_KINDS:
        sys.exit(f"invalid kind: {args.kind}")

    msg_id = _gen_msg_id(args.from_worker)
    while (inbox / f"{msg_id}.json").exists():
        msg_id = _gen_msg_id(args.from_worker)

    msg = {
        "session_id": args.session,
        "from": args.from_worker,
        "to": args.to,
        "subject": args.subject,
        "body": args.body,
        "kind": args.kind,
        "msg_id": msg_id,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if args.reply_to:
        msg["reply_to"] = args.reply_to
    if args.run_id:
        msg["run_id"] = args.run_id
    if args.request_id:
        msg["request_id"] = args.request_id

    # P0 Fix 6: validate all 8 fields + schema before writing
    ok, reason = _validate_msg_schema(msg)
    if not ok:
        sys.exit(f"send validation failed: {reason}")
    dest = inbox / f"{msg_id}.json"
    tmp = inbox / f".tmp-{msg_id}.json"
    with open(tmp, "w") as f:
        f.write(json.dumps(msg, indent=2, ensure_ascii=False))
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp), str(dest))
    print(f"sent → {args.to}/inbox/{msg_id}.json")


def cmd_peek(args):
    root = resolve_root()
    inbox = agent_subdir(root, args.session, args.agent, "inbox")
    files = list_messages(inbox)
    if not files:
        json.dump({"pending": 0, "messages": []}, sys.stdout)
        return

    summaries = []
    limit = min(len(files), args.max_messages or 5)
    for f in files[:limit]:
        try:
            msg = json.loads(f.read_bytes())
            summaries.append({
                "from": msg.get("from", "?"),
                "kind": msg.get("kind", "?"),
                "subject": msg.get("subject", "")[:args.max_subject or 80],
                "msg_id": msg.get("msg_id", f.stem),
            })
        except (json.JSONDecodeError, UnicodeDecodeError):
            summaries.append({"from": "?", "kind": "?", "subject": "(unreadable)", "msg_id": f.stem})

    result = {"pending": len(files), "messages": summaries}
    json.dump(result, sys.stdout, ensure_ascii=False)


def cmd_read(args):
    """Read oldest unprocessed message + auto-claim to processing/.

    Outputs the full message JSON to stdout. If no messages, prints nothing.
    This is the primary consumption command — replaces claim+peek with one step.

    P0 Fix 1: verifies msg.session_id == args.session before claiming.
    P0 Fix 4: if os.replace fails (concurrent race), tries next inbox file.
    """
    root = resolve_root()
    inbox = agent_subdir(root, args.session, args.agent, "inbox")
    processing = agent_subdir(root, args.session, args.agent, "processing")
    corrupt_dir = agent_subdir(root, args.session, args.agent, "_corrupt")

    while True:
        files = list_messages(inbox)
        if not files:
            return

        target = files[0]  # oldest message
        msg = None
        try:
            msg = json.loads(target.read_bytes())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            corrupt_dir.mkdir(parents=True, exist_ok=True)
            os.replace(str(target), str(corrupt_dir / target.name))
            print(f"CORRUPT: {target.name} ({e})", file=sys.stderr)
            continue  # P0 Fix 4: try next file on JSON parse failure too

        # P1 Fix 3: full schema validation before claiming (types, sizes, kind, session_id)
        ok, reason = _validate_msg_schema(msg, args.session)
        if not ok:
            corrupt_dir.mkdir(parents=True, exist_ok=True)
            os.replace(str(target), str(corrupt_dir / target.name))
            print(f"CORRUPT: {target.name} (schema: {reason})", file=sys.stderr)
            continue  # try next file

        processing.mkdir(parents=True, exist_ok=True)
        dest = processing / target.name

        # Write claim metadata
        claim_meta = {
            "owner": args.owner,
            "claimed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "msg_id": target.stem,
        }
        claim_file = processing / f".claim-{target.stem}-{args.owner}.json"
        tmp_claim = processing / f".tmp-claim-{target.stem}-{args.owner}.json"
        with open(tmp_claim, "w") as fc:
            fc.write(json.dumps(claim_meta))
            fc.flush()
            os.fsync(fc.fileno())
        os.replace(str(tmp_claim), str(claim_file))

        try:
            os.replace(str(target), str(dest))
        except OSError as e:
            # P0 Fix 4: concurrent race — clean up and try next file
            claim_file.unlink(missing_ok=True)
            print(f"read (claim) conflict on {target.name}: {e}, trying next", file=sys.stderr)
            continue

        # Output message to stdout for agent consumption
        if args.json:
            json.dump(msg, sys.stdout, ensure_ascii=False)
        else:
            print(f"FROM: {msg['from']}  KIND: {msg.get('kind','?')}")
            print(f"SUBJECT: {msg['subject']}")
            print(f"BODY: {msg['body']}")
        return  # success — done


def cmd_finalize(args):
    """Move msg from processing/ → archive/ — validates ownership from claim file.

    P0 Fix 2: fail closed — sys.exit if claim file is missing.
    """
    root = resolve_root()
    processing = agent_subdir(root, args.session, args.agent, "processing")
    archive = agent_subdir(root, args.session, args.agent, "archive")
    target = processing / f"{args.msg_id}.json"

    # Scan for claim file matching this msg_id (supports per-owner naming: .claim-{msg_id}-{owner}.json)
    claim_files = sorted(processing.glob(f".claim-{args.msg_id}-*.json"))
    if not claim_files:
        sys.exit(f"no claim file for {args.msg_id} — refusing to finalize")
    if len(claim_files) > 1:
        sys.exit(f"multiple claim files for {args.msg_id} — refusing to finalize")
    claim_file = claim_files[0]

    if not target.exists():
        sys.exit(f"msg not in processing/: {args.msg_id}")

    # Verify ownership via claim file
    try:
        claim = json.loads(claim_file.read_bytes())
        if claim.get("owner") != args.owner:
            sys.exit(f"owner mismatch: claim={claim.get('owner')} vs {args.owner}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        sys.exit(f"corrupt claim file for {args.msg_id}")

    archive.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(str(target), str(archive / target.name))
    except OSError as e:
        sys.exit(f"finalize failed: {e}")

    # Clean up claim metadata
    claim_file.unlink(missing_ok=True)
    print(f"finalized → archive/{target.name}")


def cmd_release(args):
    """Move msg from processing/ back to inbox.

    P0 Fix 3: verify .claim file owner matches --owner, reject mismatch.
    """
    root = resolve_root()
    inbox = agent_subdir(root, args.session, args.agent, "inbox")
    processing = agent_subdir(root, args.session, args.agent, "processing")
    target = processing / f"{args.msg_id}.json"

    # Scan for claim file matching this msg_id (supports per-owner naming: .claim-{msg_id}-{owner}.json)
    claim_files = sorted(processing.glob(f".claim-{args.msg_id}-*.json"))

    if not target.exists():
        sys.exit(f"msg not found in processing/: {args.msg_id}")

    if len(claim_files) > 1:
        sys.exit(f"multiple claim files for {args.msg_id} — refusing to release")

    claim_file = claim_files[0] if claim_files else None

    # P0 Fix 3: verify claim file owner
    if claim_file and claim_file.exists():
        try:
            claim = json.loads(claim_file.read_bytes())
            if claim.get("owner") != args.owner:
                sys.exit(f"owner mismatch on release: claim={claim.get('owner')} vs {args.owner}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            sys.exit(f"corrupt claim file for {args.msg_id}")
    elif args.owner:
        # If --owner was provided but no claim file exists, reject
        sys.exit(f"no claim file for {args.msg_id} — refusing to release")

    try:
        os.replace(str(target), str(inbox / target.name))
    except OSError as e:
        sys.exit(f"release failed: {e}")

    if claim_file:
        claim_file.unlink(missing_ok=True)
    print(f"released → inbox/{target.name}")


def cmd_recover_stale(args):
    """Recover stale claims: processing/ messages with expired leases → inbox."""
    root = resolve_root()
    processing_dir = agent_subdir(root, args.session, args.agent, "processing")
    inbox = agent_subdir(root, args.session, args.agent, "inbox")

    if not processing_dir.exists():
        print("no processing/ directory")
        return

    recovered = 0
    cutoff = datetime.now(timezone.utc).timestamp() - LEASE_TIMEOUT_S
    for cf in sorted(processing_dir.glob(".claim-*.json")):
        try:
            claim = json.loads(cf.read_bytes())
            claimed_at_s = claim.get("claimed_at", "")
            if claimed_at_s:
                claimed_ts = datetime.fromisoformat(claimed_at_s).timestamp()
                if claimed_ts < cutoff:
                    msg_id = claim.get("msg_id") or cf.stem.replace(".claim-", "", 1).split("-", 1)[0]
                    msg_file = processing_dir / f"{msg_id}.json"
                    if msg_file.exists():
                        os.replace(str(msg_file), str(inbox / msg_file.name))
                        cf.unlink()
                        recovered += 1
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            pass

    print(f"recovered {recovered} stale claim(s)")


def cmd_check(args):
    """Legacy: validate + archive from inbox only (no processing/ awareness)."""
    root = resolve_root()
    inbox = agent_subdir(root, args.session, args.agent, "inbox")
    archive = agent_subdir(root, args.session, args.agent, "archive")
    corrupt = agent_subdir(root, args.session, args.agent, "_corrupt")

    files = list_messages(inbox)
    if not files:
        return

    count = 0
    limit = args.max_messages or len(files)
    for entry in files[:limit]:
        filename = entry.name
        try:
            msg = json.loads(entry.read_bytes())
            ok, reason = validate_message(msg, args.agent, filename)
            if not ok:
                raise ValueError(reason)
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as e:
            corrupt.mkdir(parents=True, exist_ok=True)
            os.replace(str(entry), str(corrupt / filename))
            print(f"CORRUPT: {filename} ({e})", file=sys.stderr)
            continue

        archive.mkdir(parents=True, exist_ok=True)
        os.replace(str(entry), str(archive / filename))
        count += 1

        if args.json:
            print(json.dumps(msg, ensure_ascii=False))
        else:
            print(f"FROM: {msg['from']}  KIND: {msg.get('kind', '?')}")
            print(f"SUBJECT: {msg['subject']}")
            print(f"BODY: {msg['body']}")
            print("---")

    if args.max_messages and count >= args.max_messages:
        remaining = len(list(inbox.glob("*.json")))
        print(f"[+{remaining} more in inbox]", file=sys.stderr)


def cmd_status(args):
    """P0 Fix 5: includes session_id in status.json."""
    root = resolve_root()
    if args.state not in VALID_STATES:
        sys.exit(f"invalid state: {args.state}")

    ad = agent_dir(root, args.session, args.agent)
    ad.mkdir(parents=True, exist_ok=True)

    status = {
        "session_id": args.session,
        "state": args.state,
        "current_task": args.current_task or "",
        "last_conclusion": args.last_conclusion or "",
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    dest = ad / "status.json"
    tmp = ad / ".tmp-status.json"
    with open(tmp, "w") as f:
        f.write(json.dumps(status, indent=2, ensure_ascii=False))
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp), str(dest))
    print(f"status: {args.state}")


def cmd_clear(args):
    root = resolve_root()
    ad = agent_dir(root, args.session, args.agent)
    total = 0
    for sub in ["archive", "_corrupt"]:
        d = ad / sub
        if d.exists():
            for f in d.glob("*.json"):
                f.unlink()
                total += 1
    if args.prune_stale:
        # Also recover stale processing claims
        cmd_recover_stale(args)
    print(f"cleared {total}")


def cmd_stats(args):
    root = resolve_root()
    ad = agent_dir(root, args.session, args.agent)
    for d in ["inbox", "processing", "archive", "_corrupt"]:
        p = ad / d
        c = len(list(p.glob("*.json"))) if p.exists() else 0
        print(f"{d}: {c}")


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(description="mailbox — session-based direct-inbox CLI")
    sub = p.add_subparsers(dest="cmd")

    # session-init
    si = sub.add_parser("session-init")
    si.add_argument("--session", required=True)
    si.add_argument("--manager", required=True)
    si.add_argument("--agents", required=True, help="comma-separated agent IDs")

    # send
    s = sub.add_parser("send")
    s.add_argument("--session", required=True)
    s.add_argument("--from", required=True, dest="from_worker")
    s.add_argument("--to", required=True)
    s.add_argument("--subject", required=True)
    s.add_argument("--body", required=True)
    s.add_argument("--kind", default="REPORT", choices=sorted(VALID_KINDS))
    s.add_argument("--reply-to", default="")
    s.add_argument("--run-id", default="")
    s.add_argument("--request-id", default="")

    # peek
    pk = sub.add_parser("peek")
    pk.add_argument("--session", required=True)
    pk.add_argument("--agent", required=True)
    pk.add_argument("--max-messages", type=int, default=5)
    pk.add_argument("--max-subject", type=int, default=80)

    # read (auto-claim on read)
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

    # release (P0 Fix 3: added --owner)
    rl = sub.add_parser("release")
    rl.add_argument("--session", required=True)
    rl.add_argument("--agent", required=True)
    rl.add_argument("--msg-id", required=True)
    rl.add_argument("--owner", required=True)

    # recover-stale
    rs = sub.add_parser("recover-stale")
    rs.add_argument("--session", required=True)
    rs.add_argument("--agent", required=True)

    # check
    c = sub.add_parser("check")
    c.add_argument("--session", required=True)
    c.add_argument("--agent", required=True)
    c.add_argument("--json", action="store_true")
    c.add_argument("--max-messages", type=int, default=0)

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

    # stats
    ss = sub.add_parser("stats")
    ss.add_argument("--session", required=True)
    ss.add_argument("--agent", required=True)

    args = p.parse_args()
    if args.cmd == "session-init": cmd_session_init(args)
    elif args.cmd == "send": cmd_send(args)
    elif args.cmd == "peek": cmd_peek(args)
    elif args.cmd == "read": cmd_read(args)
    elif args.cmd == "finalize": cmd_finalize(args)
    elif args.cmd == "release": cmd_release(args)
    elif args.cmd == "recover-stale": cmd_recover_stale(args)
    elif args.cmd == "check": cmd_check(args)
    elif args.cmd == "status": cmd_status(args)
    elif args.cmd == "clear": cmd_clear(args)
    elif args.cmd == "stats": cmd_stats(args)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
