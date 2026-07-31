"""SSH transport — runs tasks on remote hosts via SSH ControlMaster.

Each host gets an independent ControlMaster socket managed by
``ControlMaster``.  Execution reuses the existing master to run
``python -m codeagent.remote_exec`` on the remote side, communicating
via the JSONL wire protocol over stdin/stdout.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from codeagent.domain import RunResult
from codeagent.transport.base import Transport, TransportError
from codeagent.transport.control_master import ControlMaster, list_sockets, stop_by_alias, stop_all
from codeagent.wire.protocol import (
    MSG_ACCEPTED,
    MSG_ERROR,
    MSG_MAILBOX_RESULT,
    MSG_READY,
    MSG_RESULT,
    MSG_SESSION,
    WIRE_VERSION,
    decode_line,
    encode_line,
    make_request,
)

if TYPE_CHECKING:
    from codeagent.domain import HostSpec, RunRequest

log = logging.getLogger(__name__)

# How long to wait for the remote helper to print ``ready`` (seconds).
_READY_TIMEOUT = 15

# Default per-request timeout passed to the remote helper.
_DEFAULT_TIMEOUT = 600

# SSH error patterns that indicate a connection-level failure (exit 255).
_SSH_ERROR_PATTERNS = (
    "Connection refused",
    "Connection timed out",
    "Connection reset by peer",
    "No route to host",
    "Network is unreachable",
    "Name or service not known",
    "Could not resolve hostname",
    "Permission denied",
    "Host key verification failed",
    "Connection closed by remote host",
)


class SSHTransport(Transport):
    """Execute tasks on remote hosts over SSH.

    Lifecycle::

        transport = SSHTransport()
        transport.warm(host)          # establish ControlMaster
        result = transport.execute(req, host, workdir)
        transport.stop(host)          # tear down

    The ControlMaster socket is shared across all ``execute`` calls to the
    same host, so ``warm`` only needs to be called once.
    """

    def __init__(self, *, ssh_bin: str = "ssh", python_bin: str = "python3") -> None:
        self._ssh = ssh_bin
        self._python = python_bin
        # One ControlMaster per host alias.
        self._masters: dict[str, ControlMaster] = {}

    # ── Transport interface ─────────────────────────────────────────────

    def warm(self, host: HostSpec) -> None:
        """Pre-establish a ControlMaster for *host*.

        Idempotent — no-op if the master is already alive.
        """
        alias = host.ssh_alias
        cm = self._masters.get(alias)
        if cm is None:
            cm = ControlMaster(alias, ssh_bin=self._ssh)
            self._masters[alias] = cm
        cm.create()

    def check(self, host: HostSpec) -> bool:
        """Return True if the ControlMaster for *host* is alive.

        Works cross-process: if the socket is not in the local ``_masters``
        dict, falls back to looking up the socket via ``.meta`` files.
        """
        alias = host.ssh_alias
        cm = self._masters.get(alias)
        if cm is not None:
            return cm.is_alive()
        # Cross-process: find socket by alias via .meta files.
        for sock_alias, sock_path in list_sockets():
            if sock_alias == alias:
                cm = ControlMaster(alias, ssh_bin=self._ssh)
                cm._socket = sock_path
                return cm.is_alive()
        return False

    def stop(self, host: HostSpec) -> None:
        """Tear down the ControlMaster for *host*.

        Works cross-process: uses ``stop_by_alias`` which reads ``.meta``
        files to locate the socket, so it works even when ``_masters`` is
        empty (new CLI process).
        """
        alias = host.ssh_alias
        # Remove from local cache if present.
        self._masters.pop(alias, None)
        # Always delegate to the cross-process stop.
        stop_by_alias(alias, ssh_bin=self._ssh)

    def stop_all(self) -> None:
        """Tear down every open ControlMaster.

        Uses the cross-process ``stop_all`` from control_master which
        iterates over ``.meta`` files.
        """
        self._masters.clear()
        stop_all(ssh_bin=self._ssh)

    def list_sockets(self) -> list[tuple[str, Path]]:
        """Return all managed sockets as ``(alias, socket_path)`` tuples.

        Needed by the CLI for ``ssh status`` display.
        """
        return list_sockets()

    def execute(
        self,
        request: RunRequest,
        host: HostSpec,
        workdir: str,
        session_id: str | None = None,
    ) -> RunResult:
        """Run *request* on *host* in *workdir* via SSH.

        Ensures the ControlMaster is alive before executing.

        On connection errors (exit 255 + SSH error patterns) **or**
        ControlMaster creation failures, retries with
        ``host.fallback_ssh_alias`` if available.
        """
        alias = host.ssh_alias
        cm = self._masters.get(alias)
        if cm is None:
            cm = ControlMaster(alias, ssh_bin=self._ssh)
            self._masters[alias] = cm

        # Ensure master is alive (lazy warm) — catch failure for fallback.
        try:
            if not cm.is_alive():
                cm.create()
        except TransportError:
            if not host.fallback_ssh_alias:
                raise
            log.warning(
                "ControlMaster create for %s failed, retrying with fallback %s",
                alias,
                host.fallback_ssh_alias,
            )
            return self._execute_on_fallback(request, host, workdir, session_id)

        result = self._run_on_host(request, host, cm, alias, workdir, session_id)

        # On connection error (exit 255 + SSH error patterns), retry with fallback.
        if (
            result.returncode == 255
            and host.fallback_ssh_alias
            and _is_ssh_error(result.stderr)
        ):
            log.warning(
                "SSH connection to %s failed (exit 255), retrying with fallback %s",
                alias,
                host.fallback_ssh_alias,
            )
            return self._execute_on_fallback(request, host, workdir, session_id)

        return result

    # ── Internal helpers ────────────────────────────────────────────────

    def _execute_on_fallback(
        self,
        request: RunRequest,
        host: HostSpec,
        workdir: str,
        session_id: str | None = None,
    ) -> RunResult:
        """Execute using ``host.fallback_ssh_alias``.

        Called when the primary ControlMaster fails to create **or** the
        primary execution returns exit 255 with an SSH error pattern.
        """
        fallback_alias = host.fallback_ssh_alias
        fallback_cm = self._masters.get(fallback_alias)
        if fallback_cm is None:
            fallback_cm = ControlMaster(fallback_alias, ssh_bin=self._ssh)
            self._masters[fallback_alias] = fallback_cm

        if not fallback_cm.is_alive():
            fallback_cm.create()

        return self._run_on_host(
            request, host, fallback_cm, fallback_alias, workdir, session_id
        )

    def _run_on_host(
        self,
        request: RunRequest,
        host: HostSpec,
        cm: ControlMaster,
        host_name: str,
        workdir: str,
        session_id: str | None = None,
    ) -> RunResult:
        """Build remote command and execute on *cm*."""
        # shell_prefix must be expanded on the REMOTE host, not locally.
        remote_exec = "codeagent-remote-exec"
        if host.shell_prefix:
            remote_cmd_str = f"{host.shell_prefix}; {remote_exec}"
            remote_cmd = ["sh", "-c", remote_cmd_str]
        else:
            remote_cmd = ["codeagent-remote-exec"]

        req = make_request(
            command="run",
            task=request.task,
            workdir=workdir or request.workdir,
            backend=request.backend or "",
            agent=request.agent,
            model=request.model,
            skills=request.skills,
            session_id=session_id,
            skip_permissions=request.skip_permissions,
            timeout=request.timeout,
        )

        ssh_cmd = cm.ssh_cmd(*remote_cmd)
        log.debug("SSH execute: %s", " ".join(ssh_cmd))

        return _run_ssh_wire(
            ssh_cmd,
            req,
            workdir=workdir,
            host_name=host_name,
            backend=request.backend or "",
            timeout=request.timeout,
        )


# ── SSH wire-protocol runner ────────────────────────────────────────────


def _run_ssh_wire(
    ssh_cmd: list[str],
    request: dict,
    *,
    workdir: str,
    host_name: str,
    backend: str,
    timeout: int = _DEFAULT_TIMEOUT,
) -> RunResult:
    """Run an SSH command that hosts ``codeagent.remote_exec`` and exchange
    a single JSONL request/response cycle.

    Uses ``Popen.communicate(timeout=deadline)`` for proper timeout handling.
    Stderr from the SSH process is captured and included in RunResult.stderr
    for diagnostics.
    """
    payload = encode_line(request)

    try:
        proc = subprocess.Popen(
            ssh_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise TransportError(f"ssh binary not found: {ssh_cmd[0]}") from exc

    session_id: str | None = None
    result_stdout = ""
    result_stderr = ""
    exit_code = -1

    try:
        stdout, stderr = proc.communicate(input=payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise TransportError(f"SSH execution timed out after {timeout}s")
    except Exception:
        proc.kill()
        proc.wait()
        raise

    stdout_str = stdout.decode("utf-8", errors="replace") if stdout else ""
    ssh_stderr = stderr.decode("utf-8", errors="replace") if stderr else ""

    # Parse JSONL response lines.
    for raw_line in stdout_str.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            msg = decode_line(raw_line)
        except ValueError:
            # Non-JSON noise — skip.
            continue

        if msg.type == MSG_READY:
            # Check wire version compatibility
            remote_ver = msg.payload.get("wire_version", 0)
            if remote_ver != WIRE_VERSION:
                raise TransportError(
                    f"wire version mismatch: remote={remote_ver}, local={WIRE_VERSION}. "
                    f"Update codeagent-py on the remote host."
                )
            continue
        if msg.type == MSG_ACCEPTED:
            continue
        if msg.type == MSG_SESSION:
            session_id = msg.session_id
        elif msg.type == MSG_RESULT:
            result_stdout = msg.stdout
            result_stderr = msg.stderr
            exit_code = msg.exit_code
        elif msg.type == MSG_ERROR:
            result_stderr = msg.message
            exit_code = -1

    # If we got no structured result, use the SSH stderr as a diagnostic.
    if exit_code == -1 and not result_stderr and ssh_stderr:
        result_stderr = ssh_stderr

    # If the process exited non-zero and we have no wire-level exit code,
    # propagate the SSH exit code.
    if exit_code == -1 and proc.returncode is not None and proc.returncode != 0:
        exit_code = proc.returncode

    return RunResult(
        returncode=exit_code if exit_code != -1 else proc.returncode,
        stdout=result_stdout,
        stderr=result_stderr,
        session_id=session_id,
        backend=backend,
        host=host_name,
        workdir=workdir,
    )


def _run_ssh_mailbox(
    ssh_cmd: list[str],
    request: dict,
    *,
    timeout: int = 60,
) -> tuple[int, str, str]:
    """Run a mailbox wire request over SSH. Returns (exit_code, stdout, stderr).

    Follows the same stdin-JSONL pattern as ``_run_ssh_wire``, but parses
    ``mailbox_result`` response messages instead of ``result``.
    """
    payload = encode_line(request)

    try:
        proc = subprocess.Popen(
            ssh_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise TransportError(f"ssh binary not found: {ssh_cmd[0]}") from exc

    result_stdout = ""
    result_stderr = ""
    exit_code = -1

    try:
        stdout, stderr = proc.communicate(input=payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise TransportError(f"SSH mailbox execution timed out after {timeout}s")
    except Exception:
        proc.kill()
        proc.wait()
        raise

    stdout_str = stdout.decode("utf-8", errors="replace") if stdout else ""

    for raw_line in stdout_str.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            msg = decode_line(raw_line)
        except ValueError:
            continue

        if msg.type == MSG_READY:
            remote_ver = msg.payload.get("wire_version", 0)
            if remote_ver != WIRE_VERSION:
                raise TransportError(
                    f"wire version mismatch: remote={remote_ver}, local={WIRE_VERSION}. "
                    f"Update codeagent-py on the remote host."
                )
            continue
        if msg.type == MSG_MAILBOX_RESULT:
            result_stdout = msg.payload.get("stdout", "")
            result_stderr = msg.payload.get("stderr", "")
            exit_code = msg.payload.get("exit_code", 0)
        elif msg.type == MSG_ERROR:
            result_stderr = msg.message
            exit_code = 1

    # Fallback: use SSH process exit code if no structured result
    if exit_code == -1:
        ssh_stderr = stderr.decode("utf-8", errors="replace") if stderr else ""
        if ssh_stderr:
            result_stderr = ssh_stderr
        exit_code = proc.returncode if proc.returncode is not None else 1

    return exit_code, result_stdout, result_stderr


def _is_ssh_error(stderr: str) -> bool:
    """Check if stderr contains SSH connection error patterns."""
    return any(pattern in stderr for pattern in _SSH_ERROR_PATTERNS)
