"""SSH transport — runs tasks on remote hosts via SSH ControlMaster.

Each host gets an independent ControlMaster socket managed by
``ControlMaster``.  Execution reuses the existing master to run
``python -m codeagent.remote_exec`` on the remote side, communicating
via the JSONL wire protocol over stdin/stdout.
"""
from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING

from codeagent.domain import RunResult
from codeagent.transport.base import Transport, TransportError
from codeagent.transport.control_master import ControlMaster
from codeagent.wire.protocol import (
    MSG_ERROR,
    MSG_READY,
    MSG_RESULT,
    MSG_SESSION,
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
        """Return True if the ControlMaster for *host* is alive."""
        cm = self._masters.get(host.ssh_alias)
        if cm is None:
            return False
        return cm.is_alive()

    def stop(self, host: HostSpec) -> None:
        """Tear down the ControlMaster for *host*.

        Idempotent — no-op if already stopped.
        """
        alias = host.ssh_alias
        cm = self._masters.pop(alias, None)
        if cm is not None:
            cm.stop()

    def stop_all(self) -> None:
        """Tear down every open ControlMaster."""
        for alias, cm in list(self._masters.items()):
            cm.stop()
        self._masters.clear()

    def execute(self, request: RunRequest, host: HostSpec, workdir: str) -> RunResult:
        """Run *request* on *host* in *workdir* via SSH.

        Ensures the ControlMaster is alive before executing.
        """
        alias = host.ssh_alias
        cm = self._masters.get(alias)
        if cm is None:
            cm = ControlMaster(alias, ssh_bin=self._ssh)
            self._masters[alias] = cm

        # Ensure master is alive (lazy warm).
        if not cm.is_alive():
            cm.create()

        req = make_request(
            command="run",
            task=request.task,
            workdir=workdir or request.workdir,
            backend=request.backend or "",
            agent=request.agent,
            model=request.model,
            session_id=request.session_key if not request.new_session else None,
            skip_permissions=request.skip_permissions,
            timeout=_DEFAULT_TIMEOUT,
        )

        ssh_cmd = cm.ssh_cmd(self._python, "-m", "codeagent.remote_exec")
        log.debug("SSH execute: %s", " ".join(ssh_cmd))

        return _run_ssh_wire(
            ssh_cmd,
            req,
            workdir=workdir,
            host_name=alias,
            backend=request.backend or "",
            timeout=_DEFAULT_TIMEOUT,
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

    Unlike the local transport (which uses ``subprocess.run`` with input),
    we use ``Popen`` so we can stream the request on stdin while reading
    the response lines from stdout in real time.  This lets us capture
    ``session`` messages that may arrive *before* the final ``result``.
    """
    try:
        proc = subprocess.Popen(
            ssh_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise TransportError(f"ssh binary not found: {ssh_cmd[0]}") from exc

    payload = encode_line(request)
    session_id: str | None = None
    result_stdout = ""
    result_stderr = ""
    exit_code = -1

    try:
        # Write the request and close stdin so the remote helper knows
        # we're done sending.
        assert proc.stdin is not None
        proc.stdin.write(payload)
        proc.stdin.flush()
        proc.stdin.close()

        # Read lines until we get a terminal message or EOF.
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = decode_line(line)
            except ValueError:
                # Non-JSON noise — skip.
                continue

            if msg.type == MSG_READY:
                # Helper sent ready before processing — expected.
                continue
            if msg.type == MSG_SESSION:
                session_id = msg.session_id
            elif msg.type == MSG_RESULT:
                result_stdout = msg.stdout
                result_stderr = msg.stderr
                exit_code = msg.exit_code
                break
            elif msg.type == MSG_ERROR:
                result_stderr = msg.message
                exit_code = -1
                break

        # Collect any remaining stderr from SSH itself.
        assert proc.stderr is not None
        ssh_stderr = proc.stderr.read().decode("utf-8", errors="replace")
        proc.wait(timeout=30)

    except subprocess.TimeoutExpired:
        proc.kill()
        raise TransportError(f"SSH execution timed out after {timeout}s")
    except Exception:
        proc.kill()
        raise

    # If we got no structured result, use the SSH stderr as a diagnostic.
    if exit_code == -1 and not result_stderr and ssh_stderr:
        result_stderr = ssh_stderr

    # If the process exited non-zero and we have no wire-level exit code,
    # propagate the SSH exit code.
    if exit_code == -1 and proc.returncode != 0:
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
