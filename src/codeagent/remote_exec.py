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

SUPPORTED_COMMANDS = {"run", "ping", "capabilities", "mailbox", "mailbox-daemon"}


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
        "capabilities": ["run", "ping", "capabilities"],
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


def _handle_mailbox(req: dict) -> None:
    """Execute mailbox subcommand locally on the remote host.

    Reads ``mailbox_root`` from the wire request and sets ``MAILBOX_ROOT``
    in the subprocess environment before invoking the mailbox CLI.
    """
    import io
    args = req.get("args", [])
    if not isinstance(args, list):
        _send({"type": "error", "message": "mailbox 'args' must be a list"})
        return

    # Propagate mailbox_root from wire request (explicit, not global env)
    mailbox_root = req.get("mailbox_root", "")
    if mailbox_root and isinstance(mailbox_root, str):
        # Validate: must be absolute path, no shell metacharacters
        import re
        if not re.match(r"^/[a-zA-Z0-9/_.-]+$", mailbox_root):
            _send({"type": "error", "message": f"invalid mailbox_root: {mailbox_root}"})
            return
        # Pass as CLI arg, not global env
        args = ["--mailbox-root", mailbox_root] + args

    # Capture stdout/stderr from mailbox CLI
    old_stdout, old_stderr = sys.stdout, sys.stderr
    buf_out, buf_err = io.StringIO(), io.StringIO()
    try:
        sys.stdout = buf_out
        sys.stderr = buf_err
        from codeagent.mailbox.cli import main as mailbox_main
        mailbox_main(args)
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


def _handle_mailbox_daemon(req: dict) -> None:
    """Execute a mailbox-daemon CLI subcommand on the remote host.

    Mirrors the structure of ``_handle_mailbox`` but delegates to
    :mod:`codeagent.tcp.cli`.
    """
    import io
    args = req.get("args", [])
    if not isinstance(args, list):
        _send({"type": "error", "message": "mailbox-daemon 'args' must be a list"})
        return

    old_stdout, old_stderr = sys.stdout, sys.stderr
    buf_out, buf_err = io.StringIO(), io.StringIO()
    try:
        sys.stdout = buf_out
        sys.stderr = buf_err
        from codeagent.tcp.cli import main as daemon_main
        daemon_main(args)
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
        elif cmd == "mailbox-daemon":
            _handle_mailbox_daemon(req)
        else:
            _send({"type": "error", "message": f"unknown command: {cmd}"})


if __name__ == "__main__":
    main()
