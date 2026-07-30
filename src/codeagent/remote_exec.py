"""Remote execution helper — deployed via dotai setup on each host.

Usage: python -m codeagent.remote_exec
Reads JSONL requests from stdin, writes JSONL responses to stdout.
Delegates to GoWrapperRunner or OMPRunner locally on the remote machine.
"""
from __future__ import annotations

import json
import os
import sys

from codeagent.domain import RunRequest
from codeagent.runners import GoWrapperRunner, OMPRunner
from codeagent.runners.base import RunnerConfig

WIRE_VERSION = 1
MAX_LINE_LENGTH = 1_048_576  # 1 MiB
SUPPORTED_COMMANDS = {"run", "ping", "capabilities"}


def _read_request() -> dict | None:
    """Read one JSON line from stdin, with max-length guard."""
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if len(line) > MAX_LINE_LENGTH:
        _send({"type": "error", "message": f"request line exceeds {MAX_LINE_LENGTH} bytes"})
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as e:
        _send({"type": "error", "message": f"invalid JSON: {e}"})
        return None
    if not isinstance(obj, dict):
        _send({"type": "error", "message": "request must be a JSON object"})
        return None
    return obj


def _send(obj: dict) -> None:
    """Write one JSON line to stdout."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _handle_ping(req: dict) -> None:
    _send({
        "type": "pong",
        "wire_version": WIRE_VERSION,
        "package_version": "0.1.0",
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
    timeout = req.get("timeout", 600)

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


def main() -> None:
    """Main loop — read requests from stdin, write responses to stdout."""
    # Send ready signal
    _send({"type": "ready", "wire_version": WIRE_VERSION, "package_version": "0.1.0"})

    while True:
        req = _read_request()
        if req is None:
            break

        cmd = req.get("command")
        if not isinstance(cmd, str) or not cmd:
            _send({"type": "error", "message": "request missing or invalid 'command' field"})
            continue
        version = req.get("wire_version", 0)

        if version > WIRE_VERSION:
            _send({"type": "error", "message": f"wire_version {version} > supported {WIRE_VERSION}"})
            continue

        if cmd == "ping":
            _handle_ping(req)
        elif cmd == "capabilities":
            _handle_capabilities(req)
        elif cmd == "run":
            _handle_run(req)
        else:
            _send({"type": "error", "message": f"unknown command: {cmd}"})


if __name__ == "__main__":
    main()
