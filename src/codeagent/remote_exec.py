"""Remote execution helper — deployed via dotai setup on each host.

Usage: python -m codeagent.remote_exec
Reads JSONL requests from stdin, writes JSONL responses to stdout.
Delegates to GoWrapperRunner or OMPRunner locally on the remote machine.
"""
from __future__ import annotations

import json
import os
import select
import sys
import time
from dataclasses import dataclass, field

from codeagent import __version__
from codeagent.mailbox.store import MailboxStore
from codeagent.constants import (
    DEFAULT_EXEC_TIMEOUT,
    MAX_LINE_LENGTH,
    STREAM_HEARTBEAT_INTERVAL,
    STREAM_CURSOR_INITIAL,
)
from codeagent.domain import RunRequest
from codeagent.runners import GoWrapperRunner, OMPRunner
from codeagent.runners.base import RunnerConfig
from codeagent.wire.protocol import WIRE_VERSION, decode_request

SUPPORTED_COMMANDS = {"run", "ping", "capabilities", "mailbox", "stream"}


def _read_request() -> dict | None:
    """Read one JSON line from stdin, validate with decode_request.

    Returns ``None`` only on end-of-input.  Malformed lines (bad JSON,
    unknown commands, missing fields) produce an error response and are
    skipped — one bad request must not kill the whole helper session.
    """
    while True:
        line = sys.stdin.readline()
        if not line:
            return None
        if len(line) > MAX_LINE_LENGTH:
            _send({"type": "error", "message": f"wire line exceeds {MAX_LINE_LENGTH} bytes"})
            continue
        try:
            return decode_request(line)
        except ValueError as e:
            _send({"type": "error", "message": str(e)})


def _send(obj: dict) -> None:
    """Write one JSON line to stdout."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _handle_ping(req: dict) -> None:
    _send({
        "type": "pong",
        "wire_version": WIRE_VERSION,
        "package_version": __version__,
        "capabilities": ["run", "ping", "capabilities", "mailbox"],
        "hostname": os.uname().nodename,
    })


def _handle_capabilities(req: dict) -> None:
    _send({
        "type": "capabilities",
        "wire_version": WIRE_VERSION,
        "backends": ["codex", "claude", "gemini", "opencode", "omp"],
        "features": ["resume", "session", "timeout"],
    })


def _handle_run(req: dict) -> None:
    """Execute a task locally using the appropriate runner."""
    task = req.get("task", "")
    workdir = req.get("workdir", ".")
    backend = req.get("backend", "opencode")
    agent = req.get("agent")
    model = req.get("model")
    resume_session_id = req.get("resume_session_id")
    skip_permissions = req.get("skip_permissions", True)
    skills = req.get("skills")
    timeout = req.get("timeout", DEFAULT_EXEC_TIMEOUT)

    _send({"type": "accepted", "wire_version": WIRE_VERSION})

    # Expand workdir here (NOT in config loader)
    workdir = os.path.expanduser(workdir)
    if not os.path.isdir(workdir):
        _send({"type": "error", "message": f"workdir not found: {workdir}"})
        return

    # Build RunRequest from wire fields
    request = RunRequest(
        task=task,
        workdir=workdir,
        backend=backend,
        agent=agent,
        model=model,
        skills=skills,
        skip_permissions=skip_permissions,
        timeout=timeout,
        resume_session_id=resume_session_id,
    )

    # Select runner by backend
    config = RunnerConfig(timeout=timeout)
    if backend == "omp":
        runner = OMPRunner(config=config)
    else:
        runner = GoWrapperRunner(config=config)

    # Run via tested runner implementation
    result = runner.run(request)

    # Send session ID if available
    if result.session_id:
        _send({"type": "session", "id": result.session_id})

    # Send result
    _send({
        "type": "result",
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.returncode,
    })


def _dispatch_mailbox_direct(args: list[str], mailbox_root: str | None = None) -> tuple[str, str, int]:
    """Dispatch a mailbox subcommand directly via MailboxStore.

    Returns ``(stdout, stderr, exit_code)``.  Raises ``_DirectUnsupported``
    for subcommands we haven't mapped yet so the caller can fall back.
    """
    from pathlib import Path

    from codeagent.mailbox.store import MailboxStore

    import argparse as _ap

    root = Path(mailbox_root) if mailbox_root else None
    store = MailboxStore(root=root)

    def _parse() -> tuple[str, _ap.Namespace]:
        p = _ap.ArgumentParser(description="mailbox", add_help=False)
        sub = p.add_subparsers(dest="cmd")

        si = sub.add_parser("session-init")
        si.add_argument("--session", required=True)
        si.add_argument("--manager", required=True)
        si.add_argument("--agents", required=True)

        s = sub.add_parser("send")
        s.add_argument("--session", required=True)
        s.add_argument("--from", required=True, dest="from_worker")
        s.add_argument("--to", required=True)
        s.add_argument("--subject", required=True)
        s.add_argument("--body", required=True)
        s.add_argument("--kind", default="TASK")
        s.add_argument("--reply-to", default="")
        s.add_argument("--run-id", default="")
        s.add_argument("--request-id", default="")
        s.add_argument("--msg-id", default=None)
        s.add_argument("--attachment", action="append", default=[])

        pk = sub.add_parser("peek")
        pk.add_argument("--session", required=True)
        pk.add_argument("--agent", required=True)
        pk.add_argument("--max-messages", type=int, default=5)
        pk.add_argument("--max-subject", type=int, default=80)

        rd = sub.add_parser("read")
        rd.add_argument("--session", required=True)
        rd.add_argument("--agent", required=True)
        rd.add_argument("--owner", required=True)
        rd.add_argument("--json", action="store_true")

        fn = sub.add_parser("finalize")
        fn.add_argument("--session", required=True)
        fn.add_argument("--agent", required=True)
        fn.add_argument("--msg-id", required=True)
        fn.add_argument("--owner", required=True)

        rl = sub.add_parser("release")
        rl.add_argument("--session", required=True)
        rl.add_argument("--agent", required=True)
        rl.add_argument("--msg-id", required=True)
        rl.add_argument("--owner", required=True)

        rs = sub.add_parser("recover-stale")
        rs.add_argument("--session", required=True)
        rs.add_argument("--agent", required=True)

        st = sub.add_parser("status")
        st.add_argument("--session", required=True)
        st.add_argument("--agent", required=True)
        st.add_argument("--state", required=True)
        st.add_argument("--current-task", default="")
        st.add_argument("--last-conclusion", default="")

        clr = sub.add_parser("clear")
        clr.add_argument("--session", required=True)
        clr.add_argument("--agent", required=True)
        clr.add_argument("--prune-stale", action="store_true")

        ss = sub.add_parser("stats")
        ss.add_argument("--session", required=True)
        ss.add_argument("--agent", required=True)

        parsed = p.parse_args(args)
        return parsed.cmd, parsed

    cmd, ns = _parse()
    out, err = "", ""
    exit_code = 0

    try:
        if cmd == "session-init":
            out = store.session_init(ns.session, ns.manager, ns.agents.split(","))
        elif cmd == "send":
            from codeagent.mailbox.protocol import AttachmentRef
            attachments: list[AttachmentRef] = []
            for item in ns.attachment:
                try:
                    d = json.loads(item)
                except json.JSONDecodeError as e:
                    raise ValueError(f"--attachment is not valid JSON: {e}") from e
                if not isinstance(d, dict):
                    raise ValueError(f"--attachment must be a JSON object")
                attachments.append(AttachmentRef.from_dict(d))
            out = store.send(
                ns.session, ns.from_worker, ns.to,
                ns.subject, ns.body, ns.kind,
                ns.reply_to, ns.run_id, ns.request_id,
                attachments=attachments or None,
                msg_id=getattr(ns, 'msg_id', None),
            )
        elif cmd == "peek":
            import json as _json
            out = _json.dumps(
                store.peek(ns.session, ns.agent, ns.max_messages, ns.max_subject),
                ensure_ascii=False,
            )
        elif cmd == "read":
            msg = store.read(ns.session, ns.agent, ns.owner)
            if msg:
                if ns.json:
                    import json as _json
                    out = _json.dumps(msg, ensure_ascii=False)
                else:
                    out = f"FROM: {msg['from']}  KIND: {msg.get('kind', '?')}\n"
                    out += f"SUBJECT: {msg['subject']}\n"
                    out += f"BODY: {msg['body']}"
        elif cmd == "finalize":
            out = store.finalize(ns.session, ns.agent, ns.msg_id, ns.owner)
        elif cmd == "release":
            out = store.release(ns.session, ns.agent, ns.msg_id, ns.owner)
        elif cmd == "recover-stale":
            out = store.recover_stale(ns.session, ns.agent)
        elif cmd == "status":
            out = store.write_status(ns.session, ns.agent, ns.state, ns.current_task, ns.last_conclusion)
        elif cmd == "clear":
            out = store.clear(ns.session, ns.agent, prune_stale=ns.prune_stale)
        elif cmd == "stats":
            lines = [f"{d}: {c}" for d, c in store.stats(ns.session, ns.agent).items()]
            out = "\n".join(lines)
        else:
            raise _DirectUnsupported(f"unmapped subcommand: {cmd}")
    except ValueError as e:
        err = str(e) + "\n"
        exit_code = 1
    except _DirectUnsupported:
        raise
    except Exception as e:
        err = f"error: {e}\n"
        exit_code = 1

    return out, err, exit_code


class _DirectUnsupported(Exception):
    """Raised when a subcommand isn't mapped for direct dispatch."""


def _handle_mailbox(req: dict) -> None:
    """Execute mailbox subcommand locally on the remote host.

    Primary path: call MailboxStore directly (no sys.stdout monkey-patch).
    Fallback: invoke the CLI via :mod:`codeagent.mailbox.cli` if the direct
    path fails with an unexpected error.
    """
    args = req.get("args", [])
    if not isinstance(args, list):
        _send({"type": "error", "message": "mailbox 'args' must be a list"})
        return

    # Propagate mailbox_root from wire request (explicit, not global env)
    import re
    mailbox_root: str | None = None
    root_raw = req.get("mailbox_root", "")
    if root_raw and isinstance(root_raw, str):
        if not re.match(r"^/[a-zA-Z0-9/_.-]+$", root_raw):
            _send({"type": "error", "message": f"invalid mailbox_root: {root_raw}"})
            return
        mailbox_root = root_raw

    # Primary path: direct MailboxStore dispatch
    try:
        stdout, stderr, exit_code = _dispatch_mailbox_direct(args, mailbox_root)
        _send({
            "type": "mailbox_result",
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
        })
        return
    except _DirectUnsupported:
        pass  # fall through to CLI
    except SystemExit:
        pass  # fall through to CLI
    except Exception:
        pass  # fall through to CLI

    # Fallback: invoke CLI (original path, sys.stdout capture)
    import io
    cli_args = list(args)
    if mailbox_root:
        cli_args = ["--mailbox-root", mailbox_root] + cli_args

    old_stdout, old_stderr = sys.stdout, sys.stderr
    buf_out, buf_err = io.StringIO(), io.StringIO()
    try:
        sys.stdout = buf_out
        sys.stderr = buf_err
        from codeagent.mailbox.cli import main as mailbox_main
        mailbox_main(cli_args)
        exit_code = 0
    except SystemExit as e:
        code = e.code
        if code is None:
            exit_code = 0
        elif isinstance(code, int):
            exit_code = code
        else:
            buf_err.write(f"{code}\n")
            exit_code = 1
    except Exception as e:
        buf_err.write(f"error: {e}\n")
        exit_code = 1
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr

    _send({
        "type": "mailbox_result",
        "stdout": buf_out.getvalue(),
        "stderr": buf_err.getvalue(),
        "exit_code": exit_code,
    })


# ── stream subscription ────────────────────────────────────────────────


@dataclass
class _StreamSubscription:
    """Tracks an active stream subscription from a client."""

    request_id: str
    session_id: str
    agent_id: str
    cursor: str  # opaque server cursor ("epoch_ms/seq"), or STREAM_CURSOR_INITIAL
    last_heartbeat: float = field(default_factory=time.monotonic)


def _cursor_gt(a: str, b: str) -> bool:
    """Numeric cursor comparison: ``a`` strictly after ``b``?

    P0-b: cursors are "epoch_ms/seq" — comparing the strings directly breaks
    when seq is unpadded ("epoch/10" sorts before "epoch/9") or when legacy
    unpadded and new zero-padded cursors mix. Parse both into int tuples.
    """
    def _parse(cur: str) -> tuple[int, int]:
        if not cur or cur == STREAM_CURSOR_INITIAL:
            return (0, 0)
        parts = cur.split("/", 1)
        try:
            return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
        except ValueError:
            return (0, 0)

    return _parse(a) > _parse(b)


def _poll_streams(subs: list[_StreamSubscription]) -> None:
    """Poll mailbox stores for new messages and emit stream_event frames.

    For each subscription, checks the agent's inbox for messages with
    ``_cursor`` lexicographically greater than the subscription's cursor.
    Emits one ``stream_event`` per new message with the full Message
    payload and advances the cursor.
    """
    if not subs:
        return

    store = MailboxStore()
    now = time.monotonic()

    for sub in subs:
        try:
            inbox = store.agent_subdir(sub.session_id, sub.agent_id, "inbox")
            files = store.list_messages(inbox)
            for f in files:
                try:
                    msg = json.loads(f.read_bytes())
                except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                    continue
                msg_cursor = msg.get('_cursor', '')
                msg_id = msg.get("msg_id", f.stem)
                # Opaque cursor discipline: only messages carrying the
                # server-generated _cursor participate in stream delivery.
                # Legacy pre-0.2.0 messages (no _cursor) are skipped —
                # mixing msg_id fallbacks with opaque cursors breaks the
                # ordering and can silently drop new mail.
                if not msg_cursor:
                    continue
                # P0-b: compare numerically, not lexicographically. The seq
                # component is zero-padded now, but legacy cursors written
                # before the padding ("epoch/10") would sort before
                # "epoch/9" as strings — silent stream skip. Parse both
                # sides into (epoch_ms, seq) ints.
                if not _cursor_gt(msg_cursor, sub.cursor):
                    continue
                # Emit event with full Message payload
                event = {
                    'type': 'stream_event',
                    'request_id': sub.request_id,
                    'session_id': sub.session_id,
                    'cursor': msg_cursor,
                    'payload': {
                        k: msg.get(k, '')
                        for k in ('msg_id', 'from', 'to', 'kind', 'subject',
                                  'body', 'created_at', 'reply_to', 'run_id',
                                  'request_id')
                    },
                }
                if msg.get('attachments'):
                    event['payload']['attachments'] = msg['attachments']
                if msg.get('trace_id'):
                    event['payload']['trace_id'] = msg['trace_id']
                _send(event)
                sub.cursor = msg_cursor
        except Exception:
            # Don't let one subscription's error kill the loop
            pass

        # Heartbeat: emit a pong if we haven't sent anything recently
        if now - sub.last_heartbeat >= STREAM_HEARTBEAT_INTERVAL:
            _send({
                "type": "pong",
                "wire_version": WIRE_VERSION,
                "heartbeat": True,
            })
            sub.last_heartbeat = now


def main(argv: list[str] | None = None) -> None:
    """Main loop — read requests from stdin, write responses to stdout.

    Supports both one-shot and long-lived serve modes.  When a ``stream``
    command is received the subscription is registered and the loop
    continues to poll for mailbox events between stdin reads.

    ``--help`` / ``--version`` are handled for dotai setup entrypoint
    verification (the wire protocol itself is JSONL over stdin).
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--help" in argv or "-h" in argv:
        print(
            "usage: codeagent-remote-exec [--version]\n\n"
            "Remote execution helper — reads JSONL requests from stdin, "
            "writes JSONL responses to stdout (wire protocol).\n"
            "Commands over stdin: ping, capabilities, run, mailbox, stream."
        )
        return
    if "--version" in argv:
        print(f"codeagent-remote-exec {__version__}")
        return

    _send({"type": "ready", "wire_version": WIRE_VERSION, "package_version": __version__})

    active_subs: list[_StreamSubscription] = []
    poll_interval = max(STREAM_HEARTBEAT_INTERVAL / 2, 1.0)

    while True:
        # If we have active subscriptions, use a non-blocking stdin read
        # so we can poll streams in the background.
        if active_subs:
            try:
                ready, _, _ = select.select([sys.stdin], [], [], poll_interval)
            except (ValueError, OSError, TypeError, AttributeError):
                # Non-selectable stdin (e.g. io.StringIO in tests) —
                # fall through to a blocking read below.
                ready = True
            if not ready:
                # select timeout ([]): no pending request — poll streams
                # and continue.  (Degraded stdin sets ready=True so the
                # blocking read below still runs.)
                _poll_streams(active_subs)
                continue

        req = _read_request()
        if req is None:
            break

        cmd = req.get("command")
        if not isinstance(cmd, str) or not cmd:
            _send({"type": "error", "message": "request missing or invalid 'command' field"})
            continue
        version = req.get("wire_version", 0)

        if version != WIRE_VERSION:
            _send({"type": "error", "message": f"wire_version {version} != required {WIRE_VERSION}"})
            continue

        if cmd == "ping":
            _handle_ping(req)
        elif cmd == "capabilities":
            _handle_capabilities(req)
        elif cmd == "run":
            _handle_run(req)
        elif cmd == "mailbox":
            _handle_mailbox(req)
        elif cmd == "stream":
            # Register a new stream subscription
            session_id = req.get("session_id", "")
            agent_id = req.get("agent_id", "")
            cursor = req.get("cursor", STREAM_CURSOR_INITIAL)
            request_id = req.get("request_id", "")
            if not session_id:
                _send({"type": "error", "message": "stream requires session_id"})
                continue
            if not agent_id:
                _send({"type": "error", "message": "stream requires agent_id"})
                continue
            # Replace existing sub for same request_id
            active_subs[:] = [s for s in active_subs if s.request_id != request_id]
            sub = _StreamSubscription(
                request_id=request_id,
                session_id=session_id,
                agent_id=agent_id,
                cursor=cursor,
            )
            active_subs.append(sub)
            _send({
                "type": "accepted",
                "wire_version": WIRE_VERSION,
                "request_id": request_id,
            })
            # Immediately poll to deliver any messages already in the inbox
            _poll_streams(active_subs)
        else:
            _send({"type": "error", "message": f"unknown command: {cmd}"})


if __name__ == "__main__":
    main()
