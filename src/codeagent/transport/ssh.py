"""SSH transport — runs tasks on remote hosts via SSH ControlMaster.

Each host gets an independent ControlMaster socket managed by
``ControlMaster``.  Execution reuses the existing master to run
``python -m codeagent.remote_exec`` on the remote side, communicating
via the JSONL wire protocol over stdin/stdout.
"""
from __future__ import annotations

import logging
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

from codeagent.constants import (
    DEFAULT_EXEC_TIMEOUT,
    DEFAULT_MAILBOX_TIMEOUT,
    DEFAULT_SSH_TIMEOUT,
    STREAM_CURSOR_INITIAL,
    STREAM_HEARTBEAT_INTERVAL,
    STREAM_RECONNECT_BASE,
    STREAM_RECONNECT_MAX,
)
from codeagent.domain import RunResult
from codeagent.transport.base import Transport, TransportError
from codeagent.transport.control_master import (
    HOST_KEY_OPTS,
    ControlMaster,
    list_sockets,
    stop_by_alias,
    stop_all,
)
from codeagent.wire.protocol import (
    MAX_CONSECUTIVE_BAD_FRAMES,
    MSG_ACCEPTED,
    MSG_ERROR,
    MSG_MAILBOX_RESULT,
    MSG_PONG,
    MSG_READY,
    MSG_RESULT,
    MSG_SESSION,
    MSG_STREAM_EVENT,
    NO_TERMINAL_FRAME_MSG,
    WIRE_VERSION,
    decode_line,
    encode_line,
    make_mailbox_request,
    make_request,
)

if TYPE_CHECKING:
    from codeagent.domain import HostSpec, RunRequest

log = logging.getLogger(__name__)

# P1-6: how long to wait for the SSHStream ready banner before giving up.
SSHSTREAM_READY_TIMEOUT = 10.0

# P3-15: cap on _seen_msg_ids — the dedup set must not grow unbounded.
# When it exceeds this many entries, the oldest half is dropped.  Cursor
# resume on reconnect makes this safe (server replays from cursor, so
# dropping old ids cannot lose dedup coverage for messages we re-receive).
_SEEN_IDS_MAX_SIZE = 10_000

# SSH error patterns that indicate a connection-level failure (exit 255).
_SSH_ERROR_PATTERNS = (
    "Connection refused",
    "Connection timed out",
    "Operation timed out",  # macOS OpenSSH wording for a connect timeout
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

    def mailbox(
        self,
        host: HostSpec,
        args: list[str],
        mailbox_root: str = "",
        timeout: int = DEFAULT_MAILBOX_TIMEOUT,
    ) -> tuple[int, str, str]:
        """Run a mailbox wire request on *host* via SSH ControlMaster.

        Returns ``(exit_code, stdout, stderr)``.
        """
        alias = host.ssh_alias
        cm = self._masters.get(alias)
        if cm is None:
            cm = ControlMaster(alias, ssh_bin=self._ssh)
            self._masters[alias] = cm
        if not cm.is_alive():
            cm.create()
        req = make_mailbox_request(args=args, mailbox_root=mailbox_root)
        remote_exec = "postmesh-remote-exec"
        if host.shell_prefix:
            # Non-interactive SSH omits ~/.local/bin etc. — same gap
            # dotai setup's shell_prefix fills for ``run``; mailbox must
            # honor it too or remote mailbox fails with command-not-found.
            prefix = host.shell_prefix
        else:
            # uv tool installs to ~/.local/bin by default; prepend it so
            # remote mailbox works even without repo-map shell_prefix.
            prefix = "export PATH=$HOME/.local/bin:$PATH"
        remote_cmd_str = f"{prefix}; {shlex.quote(remote_exec)}"
        # Single string arg: OpenSSH joins argv[4:] with spaces, so passing
        # ["sh","-c",cmd] loses quotes and the remote shell only sees the
        # first word after -c. A single string is executed as-is remotely.
        ssh_cmd = cm.ssh_cmd(remote_cmd_str)
        return _run_ssh_mailbox(ssh_cmd, req, timeout=timeout)

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
        remote_exec = "postmesh-remote-exec"
        if host.shell_prefix:
            # shell_prefix is trusted config (repo-map.json, same trust domain
            # as SSH aliases), but quote the fixed segment so a compromised
            # prefix cannot smuggle extra commands via the appended part.
            remote_cmd_str = f"{host.shell_prefix}; {shlex.quote(remote_exec)}"
            # P1-5: single string arg — OpenSSH joins argv[4:] with spaces,
            # so ["sh","-c",cmd] loses the env from shell_prefix (the remote
            # shell only sees the first word after -c) and the appended
            # remote_exec runs without the prefix → command not found.
            # A single string is executed as-is remotely (same as mailbox()).
            remote_cmd = [remote_cmd_str]
        else:
            remote_cmd = ["postmesh-remote-exec"]

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
    timeout: int = DEFAULT_SSH_TIMEOUT,
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
    # P1-3: got_terminal invariant — a one-shot exchange MUST end with a
    # terminal frame (result/error/mailbox_result); EOF without one is a
    # failure, never a silent success.
    got_terminal = False
    # P1-4: consecutive unparseable frames counter (abort on N).
    consecutive_bad = 0

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
            # P1-4: strict=False — a v1 remote's bare {"type":"ready"}
            # decodes with wire_version 0 so the version check below
            # reports a mismatch instead of the frame being skipped.
            msg = decode_line(raw_line, strict=False)
        except ValueError:
            # P1-4: N consecutive bad frames → give up loudly instead of
            # skipping garbage (which could hide a dropped terminal frame).
            consecutive_bad += 1
            if consecutive_bad >= MAX_CONSECUTIVE_BAD_FRAMES:
                log.warning(
                    "_run_ssh_wire: %d consecutive bad frames from remote; aborting parse",
                    consecutive_bad,
                )
                break
            continue
        consecutive_bad = 0

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
            got_terminal = True
            result_stdout = msg.stdout
            result_stderr = msg.stderr
            exit_code = msg.exit_code
        elif msg.type == MSG_ERROR:
            got_terminal = True
            result_stderr = msg.message
            exit_code = -1

    # P1-3: got_terminal invariant — EOF without a terminal frame (frame
    # dropped by the >1MiB guard, truncated output, garbage-only stream)
    # must not fall back to the SSH exit code when that code is 0: zero +
    # no terminal is a wire-level failure, with a diagnostic.
    if not got_terminal and exit_code == -1 and (proc.returncode is None or proc.returncode == 0):
        if not result_stderr and ssh_stderr:
            result_stderr = ssh_stderr
        if result_stderr:
            result_stderr += "\n"
        result_stderr += NO_TERMINAL_FRAME_MSG
        exit_code = 1

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
    timeout: int = DEFAULT_MAILBOX_TIMEOUT,
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
    invalid_mailbox_result = False
    # P1-3: got_terminal invariant — a one-shot mailbox exchange MUST end
    # with a terminal frame (mailbox_result/error); EOF without one is a
    # failure, never a silent success.
    got_terminal = False
    # P1-4: consecutive unparseable frames counter (abort on N).
    consecutive_bad = 0

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
            # P1-4: strict=False — a v1 remote's bare {"type":"ready"}
            # decodes with wire_version 0 so the version check below
            # reports a mismatch instead of the frame being skipped.
            msg = decode_line(raw_line, strict=False)
        except ValueError:
            if "mailbox_result" in raw_line:
                invalid_mailbox_result = True
            # P1-4: N consecutive bad frames → give up loudly instead of
            # skipping garbage (which could hide a dropped terminal frame).
            consecutive_bad += 1
            if consecutive_bad >= MAX_CONSECUTIVE_BAD_FRAMES:
                log.warning(
                    "_run_ssh_mailbox: %d consecutive bad frames from remote; aborting parse",
                    consecutive_bad,
                )
                break
            continue
        consecutive_bad = 0

        if msg.type == MSG_READY:
            remote_ver = msg.payload.get("wire_version", 0)
            if remote_ver != WIRE_VERSION:
                raise TransportError(
                    f"wire version mismatch: remote={remote_ver}, local={WIRE_VERSION}. "
                    f"Update codeagent-py on the remote host."
                )
            continue
        if msg.type == MSG_MAILBOX_RESULT:
            got_terminal = True
            stdout_value = msg.payload.get("stdout")
            stderr_value = msg.payload.get("stderr")
            exit_value = msg.payload.get("exit_code")
            if (
                not isinstance(stdout_value, str)
                or not isinstance(stderr_value, str)
                or type(exit_value) is not int
            ):
                invalid_mailbox_result = True
                continue
            result_stdout = stdout_value
            result_stderr = stderr_value
            exit_code = exit_value
        elif msg.type == MSG_ERROR:
            got_terminal = True
            result_stderr = msg.message
            exit_code = 1

    if invalid_mailbox_result:
        if not result_stderr:
            result_stderr = "invalid mailbox_result response from remote helper"
        if exit_code in (-1, 0):
            exit_code = 1

    # P1-3: got_terminal invariant — EOF without a terminal frame must not
    # fall back to the SSH exit code when that code is 0: zero + no
    # terminal is a wire-level failure, with a diagnostic.
    if not got_terminal and exit_code == -1 and (proc.returncode is None or proc.returncode == 0):
        ssh_stderr = stderr.decode("utf-8", errors="replace") if stderr else ""
        if not result_stderr and ssh_stderr:
            result_stderr = ssh_stderr
        if result_stderr:
            result_stderr += "\n"
        result_stderr += NO_TERMINAL_FRAME_MSG
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


# ── SSHStream — long-lived bidirectional JSONL stream ──────────────────


class SSHStream:
    """Bidirectional JSONL stream to a remote ``postmesh remote-exec serve``.

    Spawns ``ssh <host> postmesh-remote-exec`` (which enters serve mode
    automatically when ``stream`` is requested), keeps stdin/stdout open,
    and provides ``poll()`` to wait for stream events with cursor-based
    resumable delivery.

    Reconnect: on process exit, exponential backoff 1s/2s/4s … max 30s,
    then re-issues the last request with cursor to resume (at-least-once
    delivery; consumers deduplicate by ``msg_id``).

    Lifecycle::

        stream = SSHStream(ssh_cmd=["ssh", "host"])
        stream.open(session_id="s1", agent_id="a1")
        for event in stream.poll(timeout=300):
            process(event)
        stream.close()
    """

    def __init__(
        self,
        *,
        ssh_cmd: list[str],
        heartbeat_interval: int = STREAM_HEARTBEAT_INTERVAL,
        reconnect_base: float = float(STREAM_RECONNECT_BASE),
        reconnect_max: float = float(STREAM_RECONNECT_MAX),
    ) -> None:
        self._ssh_cmd = list(ssh_cmd)
        self._heartbeat_interval = heartbeat_interval
        self._reconnect_base = reconnect_base
        self._reconnect_max = reconnect_max

        self._proc: subprocess.Popen[bytes] | None = None
        self._session_id = ""
        self._agent_id = ""
        self._request_id = ""
        self._cursor = STREAM_CURSOR_INITIAL
        self._timeout = DEFAULT_EXEC_TIMEOUT

        self._last_event_time: float = 0.0
        self._closed = False
        self._seen_msg_ids: set[str] = set()
        # P3-15: insertion order of seen msg_ids — used to prune the
        # oldest entries when the set exceeds _SEEN_IDS_MAX_SIZE.
        self._seen_ids_order: list[str] = []

    # ── public API ─────────────────────────────────────────────────────

    def open(
        self,
        *,
        session_id: str,
        agent_id: str,
        cursor: str = STREAM_CURSOR_INITIAL,
        timeout: int = DEFAULT_EXEC_TIMEOUT,
        stream_kind: str = "mailbox",
        runtime_id: str = "",
    ) -> None:
        """Open the stream — spawn the SSH process and issue a stream request.

        ``stream_kind`` selects the event source: "mailbox" (inbox cursor),
        "runtime" (remote Gateway EventStore), or "all" (composite cursor).
        The composite cursor is opaque to the client — stored and returned
        verbatim on reconnect.
        """
        self._session_id = session_id
        self._agent_id = agent_id
        self._cursor = cursor
        self._timeout = timeout
        self._stream_kind = stream_kind
        self._runtime_id = runtime_id
        self._closed = False
        self._spawn_and_subscribe()

    def poll(self, timeout: float = 0.0) -> list[dict[str, Any]]:
        """Read available stream events, blocking up to *timeout* seconds.

        Returns a list of event payload dicts (empty if timeout elapses
        with no events).  Automatically reconnects on process exit with
        cursor resume.

        Each event payload includes ``msg_id`` for consumer-side dedup.
        """
        if self._closed:
            return []

        events: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout if timeout > 0 else 0.0

        while True:
            remaining = deadline - time.monotonic() if deadline else float(self._heartbeat_interval)
            if remaining <= 0:
                break

            line = self._readline(min(remaining, self._heartbeat_interval))
            if line is None:
                # Timeout or EOF
                if self._proc is not None and self._proc.poll() is not None:
                    # Process exited — reconnect.  The reconnect sleep is
                    # not idle wait; reset the deadline so events arriving
                    # right after reconnection are still delivered.
                    log.warning("SSHStream: process exited (rc=%s), reconnecting", self._proc.returncode)
                    self._reconnect()
                    if deadline:
                        deadline = time.monotonic() + timeout
                    continue
                # P1-5: heartbeat watchdog — if the remote has sent NOTHING
                # (no events, no heartbeat pong) for > 3× the heartbeat
                # interval, the connection is stale (hung remote / dead
                # network path).  Kill and reconnect with cursor resume
                # instead of waiting forever; _reconnect() does the kill +
                # backoff + resubscribe.
                stale_since = time.monotonic() - self._last_event_time
                if (
                    self._proc is not None
                    and stale_since > 3 * self._heartbeat_interval
                ):
                    log.warning(
                        "SSHStream: stale — no frames for %.0fs (> %.0fs), "
                        "killing and reconnecting (cursor=%s)",
                        stale_since,
                        3 * self._heartbeat_interval,
                        self._cursor,
                    )
                    self._reconnect()
                    if deadline:
                        deadline = time.monotonic() + timeout
                    continue
                break

            try:
                msg = decode_line(line)
            except ValueError:
                continue

            self._last_event_time = time.monotonic()

            if msg.type == MSG_READY:
                continue
            if msg.type == MSG_ACCEPTED:
                continue
            if msg.type == MSG_PONG:
                # Heartbeat from server — connection alive
                continue
            if msg.type == MSG_ERROR:
                log.error("SSHStream: server error: %s", msg.message)
                continue
            if msg.type == MSG_STREAM_EVENT:
                payload = msg.payload.get("payload", {})
                msg_id = payload.get("msg_id", "")
                # At-least-once delivery with msg_id dedup
                if msg_id and msg_id in self._seen_msg_ids:
                    continue
                if msg_id:
                    self._seen_msg_ids.add(msg_id)
                    self._seen_ids_order.append(msg_id)
                    self._prune_seen_ids()
                # Adopt the server cursor (opaque — mailbox "epoch/seq" OR
                # base64url composite for stream_kind="all"). The client
                # stores and returns it verbatim; it never parses it.
                if isinstance(msg.cursor, str) and msg.cursor:
                    self._cursor = msg.cursor
                events.append(payload)

        return events

    def close(self) -> None:
        """Shut down the stream gracefully."""
        self._closed = True
        if self._proc is not None:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
                self._proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                self._proc.kill()
                self._proc.wait()
            self._proc = None

    def _prune_seen_ids(self) -> None:
        """P3-15: bound the dedup set to _SEEN_IDS_MAX_SIZE entries.

        Drops the oldest half of seen msg_ids.  Safe because the server
        resumes from the cursor on reconnect — old ids beyond the window
        are either consumed (cursor advanced past them) or replayed from
        cursor (re-added on receipt).
        """
        if len(self._seen_ids_order) <= _SEEN_IDS_MAX_SIZE:
            return
        drop_count = len(self._seen_ids_order) // 2
        dropped = self._seen_ids_order[:drop_count]
        del self._seen_ids_order[:drop_count]
        for mid in dropped:
            self._seen_msg_ids.discard(mid)

    @property
    def cursor(self) -> str:
        """Current resume cursor."""
        return self._cursor

    @property
    def is_alive(self) -> bool:
        """True if the underlying SSH process is running."""
        return self._proc is not None and self._proc.poll() is None

    # ── internal ───────────────────────────────────────────────────────

    def _spawn_and_subscribe(self) -> None:
        """Spawn SSH subprocess and issue a stream subscription request."""
        import uuid

        self._request_id = uuid.uuid4().hex[:12]
        # Non-interactive SSH omits ~/.local/bin (uv tool default) — same
        # gap mailbox()/run fix via shell_prefix.  Prepending PATH here
        # keeps remote serve mode reachable on hosts without repo-map
        # shell_prefix.  Single-string remote command (OpenSSH joins
        # argv[4:] with spaces, so multi-arg would lose quoting).
        remote_cmd = [
            "export PATH=$HOME/.local/bin:$PATH; postmesh-remote-exec",
        ]
        # P2-16: SSHStream spawns a raw `ssh` process — splice in the same
        # explicit host-key policy ControlMaster uses, so a direct stream
        # cannot inherit a ~/.ssh/config that disables host-key checking.
        # Options are placed before the host (after the binary) so they win.
        cmd = [self._ssh_cmd[0], *HOST_KEY_OPTS, *self._ssh_cmd[1:]] + remote_cmd

        log.debug("SSHStream: spawning %s", " ".join(cmd))
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise TransportError(f"ssh binary not found: {cmd[0]}") from exc

        # P1-6: drain stderr on a daemon thread so a full stderr pipe can't
        # deadlock the ready-banner handshake below (ssh -v noise or remote
        # banner spam can fill the pipe buffer while we wait on stdout).
        assert self._proc.stderr is not None
        threading.Thread(
            target=self._drain_stderr,
            args=(self._proc.stderr,),
            daemon=True,
            name="sshstream-stderr-drain",
        ).start()

        # P1-6: ready banner read with a timeout — a hung ssh must fail
        # fast instead of blocking open() forever on readline().
        assert self._proc.stdout is not None
        ready_line = self._readline(SSHSTREAM_READY_TIMEOUT)
        if ready_line is None:
            if self._proc.poll() is None:
                # Process still alive → timed out waiting for the banner.
                self._proc.kill()
                rc = self._proc.wait()
                raise TransportError(
                    f"SSHStream: no ready banner within {SSHSTREAM_READY_TIMEOUT:.0f}s "
                    f"(exit {rc})"
                )
            rc = self._proc.returncode
            raise TransportError(f"SSHStream: no ready banner (exit {rc})")
        try:
            # P1-4: strict=False — a v1 remote's bare {"type":"ready"}
            # decodes with wire_version 0 so the version check below
            # reports a mismatch instead of a generic banner error.
            ready_msg = decode_line(ready_line, strict=False)
            if ready_msg.type != MSG_READY:
                raise TransportError(
                    f"SSHStream: expected ready, got {ready_msg.type}"
                )
            # P1-6: validate wire version (same check as _run_ssh_wire /
            # _run_ssh_mailbox) — a stale remote would desync the stream.
            remote_ver = ready_msg.payload.get("wire_version", 0)
            if remote_ver != WIRE_VERSION:
                raise TransportError(
                    f"wire version mismatch: remote={remote_ver}, local={WIRE_VERSION}. "
                    f"Update codeagent-py on the remote host."
                )
        except ValueError as exc:
            raise TransportError(f"SSHStream: bad ready banner: {exc}") from exc

        # Send stream subscription request
        from codeagent.wire.protocol import make_stream_request

        req = make_stream_request(
            session_id=self._session_id,
            cursor=self._cursor,
            timeout=self._timeout,
            request_id=self._request_id,
            stream_kind=getattr(self, "_stream_kind", "mailbox"),
            agent_id=self._agent_id,
            runtime_id=getattr(self, "_runtime_id", ""),
        )
        payload = encode_line(req)
        assert self._proc.stdin is not None
        self._proc.stdin.write(payload)
        self._proc.stdin.flush()

        self._last_event_time = time.monotonic()

    @staticmethod
    def _drain_stderr(stream: Any) -> None:
        """Read stderr to EOF so the pipe buffer never fills (P1-6).

        Runs on a daemon thread; exits as soon as the remote closes stderr
        (process exit) or the read side is torn down (close/reconnect).
        """
        try:
            while stream.read(4096):
                pass
        except (OSError, ValueError):
            pass

    def _readline(self, timeout: float) -> str | None:
        """Read one line from stdout with timeout. Returns None on timeout/EOF."""
        import select

        if self._proc is None or self._proc.stdout is None:
            return None

        try:
            ready, _, _ = select.select([self._proc.stdout], [], [], timeout)
        except (ValueError, OSError, TypeError, AttributeError):
            # Non-selectable stream (e.g. io.BytesIO in tests, or a
            # stream without a fileno) — degrade to a direct read.
            ready = None

        if ready is not None and not ready:
            return None  # select timeout

        try:
            line = self._proc.stdout.readline()
        except (ValueError, OSError):
            return None
        if not line:
            return None  # EOF

        return line.decode("utf-8", errors="replace")

    def _reconnect(self) -> None:
        """Reconnect with exponential backoff and cursor resume."""
        import time as _time

        # Clean up old process
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait()
            except OSError:
                pass
            self._proc = None

        backoff = self._reconnect_base
        while not self._closed:
            log.info("SSHStream: reconnecting in %.1fs (cursor=%s)", backoff, self._cursor)
            _time.sleep(backoff)
            try:
                self._spawn_and_subscribe()
                log.info("SSHStream: reconnected")
                return
            except (TransportError, OSError) as exc:
                log.warning("SSHStream: reconnect failed: %s", exc)
                backoff = min(backoff * 2, self._reconnect_max)
