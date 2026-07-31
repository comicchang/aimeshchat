"""Remote execution helper — deployed via dotai setup on each host.

Usage: python -m codeagent.remote_exec
Reads JSONL requests from stdin, writes JSONL responses to stdout.
Delegates to GoWrapperRunner or OMPRunner locally on the remote machine.
"""
from __future__ import annotations

import json
import os
import sys

from codeagent import __version__
from codeagent.constants import DEFAULT_EXEC_TIMEOUT, MAX_LINE_LENGTH
from codeagent.domain import RunRequest
from codeagent.runners import GoWrapperRunner, OMPRunner
from codeagent.runners.base import RunnerConfig
from codeagent.wire.protocol import WIRE_VERSION, decode_request

SUPPORTED_COMMANDS = {"run", "ping", "capabilities", "mailbox"}


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
        s.add_argument("--kind", default="REPORT")
        s.add_argument("--reply-to", default="")
        s.add_argument("--run-id", default="")
        s.add_argument("--request-id", default="")

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
            out = store.send(
                ns.session, ns.from_worker, ns.to,
                ns.subject, ns.body, ns.kind,
                ns.reply_to, ns.run_id, ns.request_id,
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


def main() -> None:
    """Main loop — read requests from stdin, write responses to stdout."""
    # Send ready signal
    _send({"type": "ready", "wire_version": WIRE_VERSION, "package_version": __version__})

    while True:
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
        else:
            _send({"type": "error", "message": f"unknown command: {cmd}"})


if __name__ == "__main__":
    main()
