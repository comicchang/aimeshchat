"""Relay transport — for relay-login (bastion host / PTY + expect) hosts.

Uses the same wire protocol as SSH transport, but:
- Task is base64-encoded in the remote command (stdin reserved for expect/QR)
- PTY is allocated for /dev/tty access
- shell_prefix is prepended to remote command
- No ControlMaster (relay sessions are stateful expect interactions)

Based on code_route.py's relay-login implementation.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import pty
import select
import shlex
import subprocess
import time
from typing import Optional

from codeagent.domain import HostSpec, RunRequest, RunResult
from codeagent.transport.base import Transport, TransportError
from codeagent.wire.protocol import (
    MSG_ACCEPTED,
    MSG_ERROR,
    MSG_READY,
    MSG_RESULT,
    MSG_SESSION,
    decode_line,
    encode_line,
)

log = logging.getLogger(__name__)

_READY_TIMEOUT = 15
_EXEC_TIMEOUT = 600


class RelayTransport(Transport):
    """Execute tasks via relay-login (bastion host with PTY + expect).

    The relay host uses expect-based authentication (e.g., QR code scan).
    stdin is reserved for expect, so task data is base64-encoded in the
    remote command string.
    """

    def __init__(self, relay_zsh: str) -> None:
        if not relay_zsh:
            raise TransportError("relay_zsh is required for relay-login transport")
        self._relay_zsh = os.path.expanduser(relay_zsh)
        if not os.path.isfile(self._relay_zsh):
            raise TransportError(f"relay_zsh not found: {self._relay_zsh}")

    def warm(self, host: HostSpec) -> None:
        """No-op for relay — connections are per-execution."""
        pass

    def check(self, host: HostSpec) -> bool:
        """Cannot check relay connections without executing."""
        return False

    def stop(self, host: HostSpec) -> None:
        """No-op for relay."""
        pass

    def execute(
        self,
        request: RunRequest,
        host: HostSpec,
        workdir: str,
        session_id: Optional[str] = None,
    ) -> RunResult:
        """Execute via relay-login with base64-encoded wire request."""
        # Build wire request
        wire_req = {
            "wire_version": 1,
            "command": "run",
            "task": request.task,
            "workdir": workdir,
            "backend": request.backend or "opencode",
            "skip_permissions": request.skip_permissions,
            "timeout": request.timeout if hasattr(request, 'timeout') else _EXEC_TIMEOUT,
        }
        if request.agent:
            wire_req["agent"] = request.agent
        if request.model:
            wire_req["model"] = request.model
        if session_id:
            wire_req["resume_session_id"] = session_id

        wire_line = json.dumps(wire_req, ensure_ascii=False)
        wire_b64 = base64.b64encode(wire_line.encode("utf-8")).decode("ascii")

        # Build remote command
        remote_cmd = f"printf '%s' {wire_b64} | base64 -d | python3 -m codeagent.remote_exec"
        if host.shell_prefix:
            remote_cmd = f"{host.shell_prefix}; {remote_cmd}"

        # Build relay command: zsh -c "source <relay_zsh> && relay-login <target> <remote_cmd>"
        target = host.ssh_alias  # relay-login target (e.g., clouddev-user.android.xiaomi.com)
        relay_cmd = (
            f"source {shlex.quote(self._relay_zsh)} && "
            f"relay-login {shlex.quote(target)} {shlex.quote(remote_cmd)}"
        )
        argv = ["zsh", "-c", relay_cmd]

        return self._run_with_pty(argv)

    def _run_with_pty(self, argv: list[str]) -> RunResult:
        """Execute with PTY allocation for relay expect/QR code interaction.

        Adapted from code_route.py's _run_with_pty.
        """
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        session_id = None
        exit_code = 0

        try:
            master_fd, slave_fd = pty.openpty()
            proc = subprocess.Popen(
                argv,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
            os.close(slave_fd)

            deadline = time.time() + _EXEC_TIMEOUT
            buffer = ""

            while time.time() < deadline:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break

                ready, _, _ = select.select([master_fd], [], [], min(remaining, 1.0))
                if not ready:
                    continue

                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break

                if not data:
                    break

                text = data.decode("utf-8", errors="replace")
                buffer += text

                # Process complete lines
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue

                    # Try to parse as wire protocol JSON
                    try:
                        msg = json.loads(line)
                        msg_type = msg.get("type", "")

                        if msg_type == MSG_SESSION:
                            session_id = msg.get("id")
                        elif msg_type == MSG_RESULT:
                            stdout_chunks.append(msg.get("stdout", ""))
                            exit_code = msg.get("exit_code", 0)
                        elif msg_type == MSG_ERROR:
                            stderr_chunks.append(msg.get("message", ""))
                            exit_code = msg.get("exit_code", 1)
                        elif msg_type in (MSG_READY, MSG_ACCEPTED):
                            pass  # protocol handshake
                        else:
                            # Non-wire output (relay UI, QR codes, etc.) → stderr
                            stderr_chunks.append(line)
                    except (json.JSONDecodeError, AttributeError):
                        # Non-JSON output → likely relay UI → stderr
                        stderr_chunks.append(line)

            # Timeout
            if time.time() >= deadline:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                stderr_chunks.append(f"timeout after {_EXEC_TIMEOUT}s")
                exit_code = -1

            proc.wait(timeout=5)

        except Exception as e:
            stderr_chunks.append(f"relay execution error: {e}")
            exit_code = 1
        finally:
            try:
                os.close(master_fd)
            except OSError:
                pass

        return RunResult(
            returncode=exit_code or proc.returncode,
            stdout="\n".join(stdout_chunks),
            stderr="\n".join(stderr_chunks),
            session_id=session_id,
            host="relay",
            workdir="",
        )
