"""Remote execution helper — deployed via dotai setup on each host.

Usage: python -m codeagent.remote_exec
Reads JSONL requests from stdin, writes JSONL responses to stdout.
Delegates to GoWrapperRunner or OMPRunner locally on the remote machine.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

WIRE_VERSION = 1
SUPPORTED_COMMANDS = {"run", "ping", "capabilities"}


def _read_request() -> dict | None:
    """Read one JSON line from stdin."""
    line = sys.stdin.readline()
    if not line:
        return None
    try:
        return json.loads(line.strip())
    except json.JSONDecodeError as e:
        _send({"type": "error", "message": f"invalid JSON: {e}"})
        return None


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
    session_id = req.get("resume_session_id")
    skip_permissions = req.get("skip_permissions", True)
    timeout = req.get("timeout", 600)

    _send({"type": "accepted", "wire_version": WIRE_VERSION})

    # Expand workdir
    workdir = os.path.expanduser(workdir)
    if not os.path.isdir(workdir):
        _send({"type": "error", "message": f"workdir not found: {workdir}"})
        return

    if backend == "omp":
        _run_omp(task, workdir, model, session_id, skip_permissions, timeout)
    else:
        _run_go_wrapper(task, workdir, backend, agent, model, session_id, skip_permissions, timeout)


def _run_go_wrapper(task, workdir, backend, agent, model, session_id, skip_permissions, timeout):
    """Execute via Go codeagent-wrapper binary."""
    import subprocess
    import tempfile

    cmd = [os.path.expanduser("~/.claude/bin/codeagent-wrapper")]
    if session_id:
        cmd += ["resume", session_id, task, workdir]
    else:
        if backend:
            cmd += ["--backend", backend]
        if agent:
            cmd += ["--agent", agent]
        if model:
            cmd += ["--model", model]
        if skip_permissions:
            cmd.append("--skip-permissions")

        # Use --output for structured result
        output_file = tempfile.mktemp(suffix=".json", prefix="codeagent-")
        cmd += ["--output", output_file, "-", workdir]

    try:
        proc = subprocess.run(
            cmd,
            input=task if not session_id else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=workdir,
        )

        # Extract session_id from stderr
        sid = None
        for line in (proc.stderr or "").splitlines():
            if line.startswith("SESSION_ID:"):
                sid = line.split(":", 1)[1].strip()
                break

        # Try structured output
        if not sid and "output_file" in dir():
            try:
                with open(output_file) as f:
                    data = json.loads(f.read())
                    sid = data.get("session_id")
                os.unlink(output_file)
            except (FileNotFoundError, json.JSONDecodeError):
                pass

        if sid:
            _send({"type": "session", "id": sid})

        _send({
            "type": "result",
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
            "exit_code": proc.returncode,
        })
    except subprocess.TimeoutExpired:
        _send({"type": "error", "message": f"timeout after {timeout}s"})
    except FileNotFoundError:
        _send({"type": "error", "message": "codeagent-wrapper not found"})
    except Exception as e:
        _send({"type": "error", "message": str(e)})


def _run_omp(task, workdir, model, session_id, skip_permissions, timeout):
    """Execute via omp CLI."""
    import subprocess
    import tempfile

    # Write task to temp file (omp doesn't read stdin)
    prompt_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", prefix="codeagent-omp-",
        delete=False, dir=tempfile.gettempdir()
    )
    prompt_file.write(task)
    prompt_file.close()
    os.chmod(prompt_file.name, 0o600)

    cmd = ["omp", "--print", "--mode", json.dumps("json"), "--cwd", workdir]
    if session_id:
        cmd += ["--resume", session_id]
    if model:
        cmd += ["--model", model]
    if skip_permissions:
        cmd.append("--auto-approve")
    cmd += [f"@{prompt_file.name}"]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=workdir,
        )

        # Parse JSONL output
        sid = None
        final_message = ""
        for line in (proc.stdout or "").splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "session":
                sid = obj.get("id")
                if sid:
                    _send({"type": "session", "id": sid})
            elif obj.get("type") == "assistant":
                msg_end = obj.get("message_end", {})
                final_message = msg_end.get("message", final_message)

        _send({
            "type": "result",
            "stdout": final_message,
            "stderr": proc.stderr or "",
            "exit_code": proc.returncode,
        })
    except subprocess.TimeoutExpired:
        _send({"type": "error", "message": f"timeout after {timeout}s"})
    except FileNotFoundError:
        _send({"type": "error", "message": "omp not found"})
    except Exception as e:
        _send({"type": "error", "message": str(e)})
    finally:
        try:
            os.unlink(prompt_file.name)
        except OSError:
            pass


def main() -> None:
    """Main loop — read requests from stdin, write responses to stdout."""
    # Send ready signal
    _send({"type": "ready", "wire_version": WIRE_VERSION, "package_version": "0.1.0"})

    while True:
        req = _read_request()
        if req is None:
            break

        cmd = req.get("command", "run")
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
