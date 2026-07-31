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
import signal
import subprocess
import sys
import time
from typing import Optional

from codeagent.constants import DEFAULT_MAILBOX_TIMEOUT, DEFAULT_RELAY_TIMEOUT, MAX_LINE_LENGTH
from codeagent.domain import HostSpec, RunRequest, RunResult
from codeagent.transport.base import Transport, TransportError
from codeagent.wire.protocol import (
    MSG_ACCEPTED,
    MSG_ERROR,
    MSG_MAILBOX_RESULT,
    MSG_READY,
    MSG_RESULT,
    MSG_SESSION,
    WIRE_VERSION,
    decode_line,
    make_mailbox_request,
)

log = logging.getLogger(__name__)

# Hard cap on select-loop iterations — guards against a busy-loop that
# never makes progress (infinite-loop protection).
_MAX_PTY_ITERATIONS = 100_000

# SIGTERM → SIGKILL grace period used when terminating the PTY child
# after a timeout or parse abort (seconds).
_TERM_GRACE_S = 5


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
            "wire_version": WIRE_VERSION,
            "command": "run",
            "task": request.task,
            "workdir": workdir,
            "backend": request.backend or "opencode",
            "skip_permissions": request.skip_permissions,
            "timeout": request.timeout,
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
        remote_cmd = f"printf '%s' {wire_b64} | base64 -d | codeagent-remote-exec"
        if host.shell_prefix:
            remote_cmd = f"{host.shell_prefix}; {remote_cmd}"

        # Build relay command: zsh -c "source <relay_zsh> && relay-login <target> <remote_cmd>"
        target = host.ssh_alias
        relay_cmd = (
            f"source {shlex.quote(self._relay_zsh)} && "
            f"relay-login {shlex.quote(target)} {shlex.quote(remote_cmd)}"
        )
        argv = ["zsh", "-c", relay_cmd]

        return self._run_with_pty(argv, timeout=request.timeout)

    def mailbox(
        self,
        host: HostSpec,
        args: list[str],
        mailbox_root: str = "",
        timeout: int = DEFAULT_MAILBOX_TIMEOUT,
    ) -> tuple[int, str, str]:
        """Run a mailbox wire request via relay-login PTY.

        Returns ``(exit_code, stdout, stderr)``.
        The mailbox request is base64-encoded and piped to
        ``codeagent-remote-exec`` through the relay, just like
        regular execute requests.
        """
        req = make_mailbox_request(args=args, mailbox_root=mailbox_root)
        wire_line = json.dumps(req, ensure_ascii=False)
        wire_b64 = base64.b64encode(wire_line.encode("utf-8")).decode("ascii")

        remote_cmd = f"printf '%s' {wire_b64} | base64 -d | codeagent-remote-exec"
        if host.shell_prefix:
            remote_cmd = f"{host.shell_prefix}; {remote_cmd}"

        target = host.ssh_alias
        relay_cmd = (
            f"source {shlex.quote(self._relay_zsh)} && "
            f"relay-login {shlex.quote(target)} {shlex.quote(remote_cmd)}"
        )
        argv = ["zsh", "-c", relay_cmd]

        result = self._run_with_pty(argv, timeout=timeout)
        return result.returncode, result.stdout, result.stderr

    def _run_with_pty(self, argv: list[str], timeout: int = DEFAULT_RELAY_TIMEOUT) -> RunResult:
        """Execute with PTY allocation for relay expect/QR code interaction.

        Adapted from code_route.py's _run_with_pty:
        - os.setsid() + TIOCSCTTY for controlling TTY
        - Bidirectional stdin↔PTY master forwarding (for QR/expect)
        - Wire JSON parsing with non-JSON forwarded to stderr
        - Bounded select loop: iteration cap + output buffer cap (no
          infinite loops, no unbounded memory growth)
        - Escalating termination on timeout: SIGTERM, brief wait, SIGKILL
        - Parse state transition logging for diagnostics
        """
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        session_id: Optional[str] = None
        exit_code: Optional[int] = None  # None = no wire result received yet
        version_mismatch = False
        master_fd: Optional[int] = None
        slave_fd: Optional[int] = None
        parse_state = "init"

        def _set_state(new_state: str) -> None:
            """Log parse-state transitions (diagnostics for PTY parsing)."""
            nonlocal parse_state
            if new_state != parse_state:
                log.debug("relay pty parse state: %s -> %s", parse_state, new_state)
                parse_state = new_state

        def _terminate_group(grace: float = _TERM_GRACE_S) -> None:
            """SIGTERM the child's process group, wait *grace* seconds, then
            escalate to SIGKILL.  Bounded — never blocks indefinitely."""
            for sig, wait_s in ((signal.SIGTERM, grace), (signal.SIGKILL, _TERM_GRACE_S)):
                try:
                    os.killpg(proc.pid, sig)
                except (ProcessLookupError, OSError):
                    pass
                try:
                    proc.wait(timeout=wait_s)
                    return
                except subprocess.TimeoutExpired:
                    continue
            log.warning(
                "relay pty process group %d did not exit after SIGTERM/SIGKILL",
                proc.pid,
            )

        try:
            master_fd, slave_fd = pty.openpty()

            def _preexec() -> None:
                """Set up controlling TTY for child process."""
                os.setsid()
                try:
                    import fcntl
                    import termios

                    fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
                except (ImportError, OSError):
                    pass

            proc = subprocess.Popen(
                argv,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                preexec_fn=_preexec,
            )
            try:
                os.close(slave_fd)
            except OSError:
                # Child owns the fd now — a failed close must not abort the run.
                log.warning("relay pty: failed to close slave fd %d", slave_fd)
            finally:
                slave_fd = None

            deadline = time.time() + timeout
            buffer = ""
            iterations = 0
            abort_reason: Optional[str] = None

            # Get stdin fd for forwarding (QR/expect interaction)
            stdin_fd: Optional[int] = None
            try:
                if sys.stdin.isatty():
                    stdin_fd = sys.stdin.fileno()
            except (AttributeError, ValueError):
                pass

            _set_state("streaming")
            while time.time() < deadline:
                # Infinite-loop guard: hard cap on select iterations.
                if iterations >= _MAX_PTY_ITERATIONS:
                    log.warning(
                        "relay pty iteration cap (%d) reached after %d iterations",
                        _MAX_PTY_ITERATIONS,
                        iterations,
                    )
                    stderr_chunks.append(
                        f"relay pty iteration cap reached after {iterations} iterations"
                    )
                    exit_code = -1
                    abort_reason = "iteration-cap"
                    _set_state("abort:iteration-cap")
                    break
                iterations += 1

                remaining = deadline - time.time()
                if remaining <= 0:
                    break

                # Memory guard: refuse to accumulate an unbounded buffer.
                # Matches the wire protocol's own MAX_LINE_LENGTH limit.
                if len(buffer) > MAX_LINE_LENGTH:
                    log.warning(
                        "relay pty output buffer exceeded %d bytes; aborting",
                        MAX_LINE_LENGTH,
                    )
                    stderr_chunks.append("relay output buffer exceeded limit; aborting")
                    exit_code = -1
                    abort_reason = "buffer-overflow"
                    _set_state("abort:buffer-overflow")
                    break

                read_fds = [master_fd]
                if stdin_fd is not None:
                    read_fds.append(stdin_fd)

                try:
                    ready, _, _ = select.select(read_fds, [], [], min(remaining, 1.0))
                except (ValueError, OSError):
                    # Select failed — can no longer monitor the child.
                    abort_reason = "select-error"
                    _set_state("abort:select-error")
                    break

                # Forward stdin → PTY master (for QR code / expect interaction)
                if stdin_fd is not None and stdin_fd in ready:
                    try:
                        chunk = os.read(stdin_fd, 4096)
                        if chunk:
                            os.write(master_fd, chunk)
                        else:
                            stdin_fd = None  # stdin closed
                            _set_state("stdin-closed")
                    except OSError:
                        stdin_fd = None
                        _set_state("stdin-closed")

                # Read PTY master output
                if master_fd in ready:
                    try:
                        data = os.read(master_fd, 4096)
                    except OSError:
                        abort_reason = "read-error"
                        _set_state("abort:read-error")
                        break

                    if not data:
                        # EOF on the PTY master — child closed its side.
                        _set_state("eof")
                        break

                    text = data.decode("utf-8", errors="replace")
                    buffer += text
                    _set_state("collecting")

                    # Process complete lines
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue

                        # Try to parse as wire protocol JSON
                        try:
                            msg = decode_line(line)
                            msg_type = msg.type
                            payload = msg.payload

                            if msg_type == MSG_SESSION:
                                _set_state("msg:session")
                                session_id = payload.get("id")
                            elif msg_type == MSG_RESULT:
                                _set_state("msg:result")
                                if not version_mismatch:
                                    stdout_chunks.append(payload.get("stdout", ""))
                                    exit_code = payload.get("exit_code", 0)
                                else:
                                    stderr_chunks.append("ignoring result after wire version mismatch")
                            elif msg_type == MSG_MAILBOX_RESULT:
                                _set_state("msg:mailbox_result")
                                if not version_mismatch:
                                    stdout_chunks.append(payload.get("stdout", ""))
                                    stderr_chunks.append(payload.get("stderr", ""))
                                    exit_code = payload.get("exit_code", 0)
                                else:
                                    stderr_chunks.append("ignoring mailbox_result after wire version mismatch")
                            elif msg_type == MSG_ERROR:
                                _set_state("msg:error")
                                stderr_chunks.append(payload.get("message", ""))
                                exit_code = payload.get("exit_code", 1)
                            elif msg_type == MSG_READY:
                                # Check wire version compatibility
                                remote_ver = payload.get("wire_version", 0)
                                if remote_ver != WIRE_VERSION:
                                    stderr_chunks.append(
                                        f"wire version mismatch: remote={remote_ver}, local={WIRE_VERSION}"
                                    )
                                    exit_code = 1
                                    version_mismatch = True
                                _set_state("msg:ready")
                            elif msg_type == MSG_ACCEPTED:
                                _set_state("msg:accepted")
                                pass  # protocol handshake
                            else:
                                # Non-wire output (relay UI, QR codes, etc.) → stderr
                                _set_state("msg:other")
                                stderr_chunks.append(line)
                        except (ValueError, AttributeError):
                            # Non-JSON output → likely relay UI → stderr
                            _set_state("non-wire")
                            stderr_chunks.append(line)

            # ── post-loop termination ──────────────────────────────────
            timed_out = time.time() >= deadline
            if timed_out or abort_reason is not None:
                if timed_out:
                    stderr_chunks.append(f"timeout after {timeout}s")
                    exit_code = -1
                    _set_state("timeout")
                # Escalating kill: SIGTERM → brief wait → SIGKILL.
                _terminate_group()
            else:
                # Normal EOF — reap the child with a bounded wait.
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

            # No wire result received — use process exit code or default to error
            if exit_code is None:
                exit_code = proc.returncode if proc.returncode is not None else 1

        except Exception as e:
            stderr_chunks.append(f"relay execution error: {e}")
            exit_code = 1
        finally:
            if master_fd is not None:
                try:
                    os.close(master_fd)
                except OSError:
                    pass
            if slave_fd is not None:
                try:
                    os.close(slave_fd)
                except OSError:
                    pass

        return RunResult(
            returncode=exit_code,
            stdout="\n".join(stdout_chunks),
            stderr="\n".join(stderr_chunks),
            session_id=session_id,
            host="relay",
            workdir="",
        )
