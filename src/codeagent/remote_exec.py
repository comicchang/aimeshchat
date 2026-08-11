"""Remote execution helper — deployed via dotai setup on each host.

Usage: python -m codeagent.remote_exec
Reads JSONL requests from stdin, writes JSONL responses to stdout.
Delegates to OMPRunner locally on the remote machine.
"""
from __future__ import annotations

import json
import logging
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
    from codeagent.runtime.registry import RuntimeRegistry

    backends = RuntimeRegistry().names()
    _send({
        "type": "capabilities",
        "wire_version": WIRE_VERSION,
        "backends": backends,
        "features": ["resume", "session", "timeout"],
    })


def _handle_run(req: dict) -> None:
    """Execute a task locally using the appropriate runner."""
    task = req.get("task", "")
    workdir = req.get("workdir", ".")
    backend = req.get("backend", "omp")
    agent = req.get("agent")
    model = req.get("model")
    resume_session_id = req.get("resume_session_id")
    skip_permissions = req.get("skip_permissions", True)
    skills = req.get("skills")
    timeout = req.get("timeout", DEFAULT_EXEC_TIMEOUT)

    # v2 correlation fields — round-trip verbatim (session_key must NOT
    # collapse to None; it was a historical bug).
    session_key = req.get("session_key") or None
    request_id = req.get("request_id", "")
    run_id = req.get("run_id", "")
    review_key = req.get("review_key", "")
    require_ack = bool(req.get("require_ack", False))
    caps_raw = req.get("capabilities") or []
    capabilities = tuple(str(c) for c in caps_raw if isinstance(c, str))

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
        session_key=session_key,
        request_id=request_id,
        run_id=run_id,
        review_key=review_key,
        require_ack=require_ack,
        capabilities=capabilities,
    )

    # Select runner via the RuntimeRegistry (bounded short-task path).
    from codeagent.runtime.registry import RuntimeRegistry, RuntimeErrorCode

    try:
        adapter = RuntimeRegistry().get(backend)
    except RuntimeErrorCode as exc:
        _send({"type": "error", "message": f"{exc.code}: {exc.message}"})
        return

    try:
        handle = adapter.spawn({
            "task": task,
            "workdir": workdir,
            "agent_id": agent or "",
            "model": model or "",
            "session_id": session_key or "",
            "review_key": review_key,
            "request_id": request_id,
            "run_id": run_id,
            "backend_session_id": resume_session_id,
            "short_task": True,
            "timeout": timeout,
            "profile_args": [],
        })
    except Exception as exc:
        _send({"type": "error", "message": f"run failed: {exc}"})
        return

    # Bounded adapters put their result in handle.extra["result"].
    result = (handle.extra or {}).get("result") or {}
    returncode = int(result.get("returncode", 0))
    stdout = result.get("stdout", "") or ""
    stderr = result.get("stderr", "") or ""

    # Send session ID if available
    if handle.backend_session_id:
        _send({"type": "session", "id": handle.backend_session_id})
    elif result.get("session_id"):
        _send({"type": "session", "id": result["session_id"]})

    # Send result
    _send({
        "type": "result",
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": returncode,
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
        s.add_argument("--require-ack", action="store_true", default=False)
        s.add_argument("--receipt-type", default="")

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

        rn = sub.add_parser("renew")  # P2-10: lease renewal for long claims
        rn.add_argument("--session", required=True)
        rn.add_argument("--agent", required=True)
        rn.add_argument("--msg-id", required=True)
        rn.add_argument("--owner", required=True)

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
            # v2: route through MailboxService (require_ack / receipt_type).
            from codeagent.mailbox.service import MailboxService
            svc = MailboxService(store=store)
            receipt = svc.send(
                ns.session, ns.from_worker, ns.to,
                ns.subject, ns.body, ns.kind,
                ns.reply_to, ns.run_id, ns.request_id,
                attachments=attachments or None,
                msg_id=getattr(ns, 'msg_id', None),
                require_ack=getattr(ns, 'require_ack', False),
                receipt_type=getattr(ns, 'receipt_type', ''),
            )
            if receipt.status == "failed":
                raise ValueError(receipt.error or "send failed")
            out = receipt.detail or f"sent → {ns.to}/inbox/{receipt.msg_id}.json"
        elif cmd == "peek":
            import json as _json
            out = _json.dumps(
                store.peek(ns.session, ns.agent, ns.max_messages, ns.max_subject),
                ensure_ascii=False,
            )
        elif cmd == "read":
            # v2: claim via MailboxService — emits RECEIPT(READ) for
            # require_ack messages on the remote host too.
            from codeagent.mailbox.service import ACK_ROUTE_UNRESOLVED, MailboxService
            svc = MailboxService(store=store)
            outcome = svc.read(ns.session, ns.agent, ns.owner)
            if outcome.status == ACK_ROUTE_UNRESOLVED:
                raise ValueError(outcome.error or "ack route unresolved")
            msg = outcome.message
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
        elif cmd == "renew":
            if not store.renew_claim(ns.session, ns.agent, ns.msg_id, ns.owner):
                raise ValueError(
                    f"claim not renewable: {ns.msg_id} (missing / owner mismatch)"
                )
            out = f"renewed claim {ns.msg_id}"
        elif cmd == "status":
            out = store.write_status(ns.session, ns.agent, ns.state, ns.current_task, ns.last_conclusion)
        elif cmd == "clear":
            out = store.clear(ns.session, ns.agent, prune_stale=ns.prune_stale)
        elif cmd == "stats":
            lines = [f"{d}: {c}" for d, c in store.stats(ns.session, ns.agent).items()]
            out = "\n".join(lines)
        else:
            raise _DirectUnsupported(f"unmapped subcommand: {cmd}")
    except (ValueError, KeyError, TypeError) as e:
        # P2 (oracle-lite): terminal error → exit 2 (schema/validation/roster —
        # retry is pointless).  Matches cli.py which sys.exit(2) for ValueError.
        # DeliveryEngine._is_terminal_error recognizes "exit 2" and dead-letters.
        # KeyError covers missing required fields (e.g. AttachmentRef.from_dict);
        # TypeError covers wrong payload shapes.
        err = str(e) + "\n"
        exit_code = 2
    except _DirectUnsupported:
        raise
    except Exception as e:
        # exit 1 = retryable (transient/environment errors, retry may succeed)
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
    stream_kind: str = "mailbox"  # mailbox | runtime | all
    runtime_id: str = ""


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
    """Poll mailbox stores + local Gateway EventStore; emit stream_event frames.

    For each subscription:
      - stream_kind mailbox: poll the agent's inbox (plain epoch/seq cursor)
      - stream_kind runtime: poll the local EventStore (composite cursor)
      - stream_kind all: poll BOTH, advancing a composite cursor

    Emits one ``stream_event`` per new message/event with the full payload
    and advances the subscription cursor. Consumers dedupe by msg_id
    (mailbox) or (source_host, runtime_id, generation, source_sequence).
    """
    if not subs:
        return

    store = MailboxStore()
    now = time.monotonic()

    for sub in subs:
        if sub.stream_kind in ("mailbox", "all"):
            _poll_mailbox(sub, store)
        if sub.stream_kind in ("runtime", "all"):
            _poll_runtime_events(sub)

        # Heartbeat: emit a pong if we haven't sent anything recently
        if now - sub.last_heartbeat >= STREAM_HEARTBEAT_INTERVAL:
            _send({
                "type": "pong",
                "wire_version": WIRE_VERSION,
                "heartbeat": True,
            })
            sub.last_heartbeat = now


def _poll_mailbox(sub: _StreamSubscription, store: MailboxStore) -> None:
    """Emit inbox messages newer than the mailbox cursor component.

    Polls BOTH the subscribed agent's inbox AND the session manager's inbox
    on this host — the manager inbox is the return path for READ receipts /
    REPORPs generated by local claims (cross-host flow is one-way SSH: the
    Manager's stream carries them back).
    """
    last_cursor = ""
    inboxes: list[str] = [sub.agent_id]
    try:
        meta = store.read_session(sub.session_id)
        if meta:
            members = {meta.get("manager", "")} | set(meta.get("agents", []) or [])
            for member in sorted(m for m in members if m):
                if member not in inboxes:
                    inboxes.append(member)
    except Exception:
        pass
    for agent_id in inboxes:
        try:
            inbox = store.agent_subdir(sub.session_id, agent_id, "inbox")
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
                if not msg_cursor:
                    continue
                mailbox_cursor, _rt = _split_cursor(sub.cursor)
                if not _cursor_gt(msg_cursor, mailbox_cursor):
                    continue
                event = {
                    'type': 'stream_event',
                    'request_id': sub.request_id,
                    'session_id': sub.session_id,
                    'cursor': _advance_cursor(sub, msg_cursor=msg_cursor),
                    'payload': {
                        k: msg.get(k, '')
                        for k in ('msg_id', 'from', 'to', 'kind', 'subject',
                                  'body', 'created_at', 'reply_to', 'run_id',
                                  'request_id')
                    },
                    'source': 'mailbox',
                    'inbox_agent': agent_id,
                }
                if msg.get('attachments'):
                    event['payload']['attachments'] = msg['attachments']
                if msg.get('trace_id'):
                    event['payload']['trace_id'] = msg['trace_id']
                _send(event)
                last_cursor = msg_cursor
        except Exception:
            # Don't let one subscription's error kill the loop
            pass
    if last_cursor:
        if sub.stream_kind == "mailbox":
            sub.cursor = last_cursor
        else:
            sub.cursor = _advance_cursor(sub, msg_cursor=last_cursor)


def _poll_runtime_events(sub: _StreamSubscription) -> None:
    """Emit local Gateway EventStore events newer than the runtime cursor."""
    last_event_id = 0
    try:
        from codeagent.gateway.events import EventStore

        _mc, runtime_event_id = _split_cursor(sub.cursor)
        store = EventStore()
        events, _next_cursor = store.list_after(
            cursor=runtime_event_id,
            limit=200,
            session_id=sub.session_id if sub.session_id else "",
            runtime_id=sub.runtime_id,
        )
        for ev in events:
            _send({
                'type': 'stream_event',
                'request_id': sub.request_id,
                'session_id': sub.session_id,
                'cursor': _advance_cursor(sub, runtime_event_id=ev.event_id),
                'payload': ev.to_dict(),
                'source': 'runtime',
            })
            last_event_id = ev.event_id
    except Exception:
        # EventStore unavailable — the runtime leg simply yields nothing.
        pass
    if last_event_id and sub.stream_kind in ("runtime", "all"):
        sub.cursor = _advance_cursor(sub, runtime_event_id=last_event_id)


def _split_cursor(cursor: str) -> tuple[str, int]:
    """Split an opaque cursor into (mailbox_cursor, runtime_event_id)."""
    try:
        from codeagent.wire.protocol import split_composite_cursor
        return split_composite_cursor(cursor)
    except ImportError:
        return cursor, 0
    except Exception:
        # P3-n: warn on unexpected parse failure (ImportError is expected
        # if wire module is unavailable in minimal deployments).
        logging.getLogger(__name__).warning(
            "_split_cursor: failed to parse cursor %r, falling back", cursor[:64]
        )
        return cursor, 0


def _advance_cursor(sub: _StreamSubscription, msg_cursor: str = "", runtime_event_id: int = 0) -> str:
    """Advance the subscription cursor after emitting an event.

    mailbox → plain epoch/seq cursor; runtime/all → composite base64url.
    """
    if sub.stream_kind == "mailbox":
        return msg_cursor or sub.cursor
    from codeagent.wire.protocol import make_composite_cursor
    mailbox_cursor, rt = _split_cursor(sub.cursor)
    if msg_cursor:
        mailbox_cursor = msg_cursor
    if runtime_event_id:
        rt = runtime_event_id
    return make_composite_cursor(mailbox_cursor, rt)


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
            "usage: postmesh-remote-exec [--version]\n\n"
            "Remote execution helper — reads JSONL requests from stdin, "
            "writes JSONL responses to stdout (wire protocol).\n"
            "Commands over stdin: ping, capabilities, run, mailbox, stream."
        )
        return
    if "--version" in argv:
        print(f"postmesh-remote-exec {__version__}")
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
            stream_kind = req.get("stream_kind", "mailbox")
            if stream_kind not in ("mailbox", "runtime", "all"):
                _send({"type": "error", "message": f"invalid stream_kind: {stream_kind}"})
                continue
            runtime_id = req.get("runtime_id", "")
            # Replace existing sub for same request_id
            active_subs[:] = [s for s in active_subs if s.request_id != request_id]
            sub = _StreamSubscription(
                request_id=request_id,
                session_id=session_id,
                agent_id=agent_id,
                cursor=cursor,
                stream_kind=stream_kind,
                runtime_id=runtime_id,
            )
            active_subs.append(sub)
            _send({
                "type": "accepted",
                "wire_version": WIRE_VERSION,
                "request_id": request_id,
                "stream_kind": stream_kind,
            })
            # Immediately poll to deliver any messages already in the inbox
            _poll_streams(active_subs)
        else:
            _send({"type": "error", "message": f"unknown command: {cmd}"})


if __name__ == "__main__":
    main()
