"""Local transport — runs tasks on the current machine via subprocess."""
from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING

from codeagent.constants import DEFAULT_EXEC_TIMEOUT
from codeagent.domain import LOCAL_HOST_MARKER, RunResult
from codeagent.transport.base import Transport, TransportError
from codeagent.wire.protocol import (
    MSG_ACCEPTED,
    MSG_ERROR,
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


class LocalTransport(Transport):
    """Execute tasks on the local machine.

    Spawns ``python -m codeagent.remote_exec`` as a subprocess and
    communicates via the JSONL wire protocol over stdin/stdout.
    """

    def __init__(self, *, python_bin: str = "python3") -> None:
        self._python = python_bin

    # ── Transport interface ─────────────────────────────────────────────

    def warm(self, host: HostSpec) -> None:
        """No-op — local execution needs no pre-established connection."""
        log.debug("LocalTransport.warm: no-op for %s", host.name)

    def check(self, host: HostSpec) -> bool:
        """Always True for local transport."""
        return True

    def stop(self, host: HostSpec) -> None:
        """No-op — nothing to tear down."""
        log.debug("LocalTransport.stop: no-op for %s", host.name)

    def execute(
        self,
        request: RunRequest,
        host: HostSpec,
        workdir: str,
        session_id: str | None = None,
    ) -> RunResult:
        """Run *request* locally and return the result."""
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
            session_key=request.session_key,
            request_id=request.request_id,
            run_id=request.run_id,
            review_key=request.review_key,
            require_ack=request.require_ack,
            capabilities=request.capabilities,
        )
        return _run_wire(
            ["postmesh-remote-exec"],
            req,
            workdir=workdir,
            host_name=LOCAL_HOST_MARKER,
            backend=request.backend or "",
            timeout=request.timeout,
        )

    def mailbox(
        self,
        host: HostSpec,
        args: list[str],
        mailbox_root: str = "",
        timeout: int = 30,
    ) -> tuple[int, str, str]:
        """Run mailbox CLI args locally; return (exit_code, stdout, stderr).

        B1: 统一投递路径——本机 target 走 LocalTransport.mailbox()（内部复用
        mailbox.cli.main，无 subprocess 开销），与远程 SSH transport 共用
        同一 mailbox args 构造，消除 DeliveryEngine 里 inline store.send 的
        重复逻辑。
        """
        import contextlib
        import io

        from codeagent.mailbox import cli as mailbox_cli

        full_args: list[str] = list(args)
        if mailbox_root:
            full_args = ["--mailbox-root", mailbox_root] + full_args
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                mailbox_cli.main(full_args)
            except SystemExit as exc:
                code = exc.code or 0 if isinstance(exc.code, int) else 1
            except Exception as exc:  # noqa: BLE001 — report any failure to caller
                err.write(str(exc))
                code = 1
        return code, out.getvalue(), err.getvalue()


# ── shared wire-protocol runner ─────────────────────────────────────────


def _run_wire(
    cmd: list[str],
    request: dict,
    *,
    workdir: str,
    host_name: str,
    backend: str,
    timeout: int = DEFAULT_EXEC_TIMEOUT,
) -> RunResult:
    """Execute a remote-exec helper (local or over SSH) and collect the result.

    Uses ``Popen.communicate(timeout=deadline)`` for proper timeout handling.

    Returns a ``RunResult`` even on error (with ``returncode=-1`` for
    wire-level errors).
    """
    payload = encode_line(request)

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise TransportError(f"helper binary not found: {cmd[0]}") from exc

    session_id: str | None = None
    result_stdout = ""
    result_stderr = ""
    exit_code = -1

    try:
        stdout, stderr = proc.communicate(input=payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise TransportError(f"execution timed out after {timeout}s")
    except Exception:
        proc.kill()
        proc.wait()
        raise

    stdout_str = stdout.decode("utf-8", errors="replace") if stdout else ""
    stderr_str = stderr.decode("utf-8", errors="replace") if stderr else ""

    # Parse JSONL response lines.
    for raw_line in stdout_str.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            msg = decode_line(raw_line)
        except ValueError:
            # Non-JSON noise — treat as stdout.
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
            # Wire-level error: propagate as non-zero exit.
            result_stderr = msg.message
            exit_code = -1

    # If the helper produced no structured output, fall back to raw stderr.
    if not result_stderr and stderr_str:
        result_stderr = stderr_str

    return RunResult(
        returncode=exit_code if exit_code != -1 else proc.returncode,
        stdout=result_stdout,
        stderr=result_stderr,
        session_id=session_id,
        backend=backend,
        host=host_name,
        workdir=workdir,
    )
