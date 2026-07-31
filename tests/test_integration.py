"""Integration tests — gated on --run-integration flag.

These tests exercise real external services (localhost SSH daemon) and
are skipped by default.  Run them explicitly with::

    uv run pytest --run-integration tests/test_integration.py

``pytest.config`` was removed in pytest 7, so ``conftest.pytest_configure``
mirrors the ``--run-integration`` flag into ``RUN_INTEGRATION`` for
collection-time evaluation of the marker below.

Every test talks to the local OpenSSH daemon over ``ssh localhost`` in
BatchMode (no interactive prompts).  A module-scoped autouse probe skips
the whole class when BatchMode-auth to localhost is unavailable.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from codeagent.domain import HostSpec
from codeagent.transport.ssh import SSHTransport, _is_ssh_error, _run_ssh_wire
from codeagent.wire.protocol import (
    MAX_LINE_LENGTH,
    WIRE_VERSION,
    decode_line,
    encode_line,
    make_ping,
    make_request,
)

requires_integration = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="integration tests disabled (use --run-integration)",
)


@pytest.fixture(scope="module", autouse=True)
def localhost_ssh() -> None:
    """Probe that localhost SSH with BatchMode works; skip the module otherwise.

    The probe itself is the skip condition: everything below depends on
    a working non-interactive ``ssh localhost``.
    """
    probe = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "localhost", "true"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if probe.returncode != 0:
        pytest.skip(
            "localhost SSH with BatchMode unavailable: "
            f"{probe.stderr.strip() or probe.stdout.strip() or probe.returncode}"
        )


def _remote_exec_argv() -> list[str]:
    """Resolve the remote-exec helper for a localhost SSH session.

    Prefers the installed ``codeagent-remote-exec`` entry point (its
    shebang is absolute, so it works under the minimal PATH ssh
    provides); falls back to ``python -m codeagent.remote_exec``.
    """
    entry = shutil.which("codeagent-remote-exec")
    if entry:
        return [entry]
    return [sys.executable, "-m", "codeagent.remote_exec"]


def _ssh_argv(*extra: str) -> list[str]:
    """Build a fresh (non-multiplexed) ssh argv for error-path probes.

    ``-o ControlMaster=no -o ControlPath=none`` force a real connection
    so the probe is not silently satisfied by a stale multiplexed master
    left behind by the user's own ``Host *`` ssh config.
    """
    return ["ssh", "-o", "ControlMaster=no", "-o", "ControlPath=none", *extra]


def _run(
    ssh_cmd: list[str],
    payload: bytes,
    *,
    timeout: int = 60,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Spawn *ssh_cmd*, feed *payload* on stdin, return (rc, stdout, stderr)."""
    proc = subprocess.Popen(
        ssh_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    stdout, stderr = proc.communicate(input=payload, timeout=timeout)
    return (
        proc.returncode,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def _decode_stream(stdout: str) -> list:
    """Decode every JSONL response line, skipping non-JSON garbage.

    Mirrors the transport's own tolerant parse loop (``_run_ssh_wire``
    catches the ``ValueError`` from ``decode_line`` and continues).
    """
    responses = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            responses.append(decode_line(line))
        except ValueError:
            continue
    return responses


def _pong(responses: list) -> list:
    """Filter a decoded stream down to pong messages."""
    return [m for m in responses if m.type == "pong"]


def _closed_port() -> int:
    """Return a TCP port on 127.0.0.1 that currently has no listener."""
    for _ in range(5):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        # Port released — confirm nothing grabbed it before returning.
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                continue  # something took the port; try again
        except OSError:
            return port
    raise RuntimeError("could not find a closed TCP port")


def _localhost_host(*, name: str, alias: str = "localhost") -> HostSpec:
    """HostSpec pointing at the local OpenSSH daemon."""
    return HostSpec(
        name=name,
        ssh_alias=alias,
        hostnames=("localhost",),
        description="integration test host",
    )


@requires_integration
class TestLocalhostSSH:
    """End-to-end SSH ControlMaster + wire protocol against localhost."""

    # ── happy path ─────────────────────────────────────────────────────

    def test_warm_ping_verify_stop(self, tmp_path: pytest.TempPathFactory) -> None:
        """warm ControlMaster → run ping → verify response → stop."""
        host = HostSpec(
            name="localhost-it",
            ssh_alias="localhost",
            hostnames=("localhost",),
            description="integration test host",
        )
        transport = SSHTransport()
        try:
            # 1. Warm: establish ControlMaster.
            transport.warm(host)
            assert transport.check(host), "ControlMaster not alive after warm()"

            cm = transport._masters[host.ssh_alias]

            # 2. Run ping over the warm socket.
            ssh_cmd = cm.ssh_cmd(*_remote_exec_argv())
            proc = subprocess.Popen(
                ssh_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = proc.communicate(
                input=encode_line(make_ping()), timeout=30
            )
            assert proc.returncode == 0, f"ssh failed: {stderr.decode(errors='replace')}"

            # 3. Verify the pong response.
            responses = [
                decode_line(line)
                for line in stdout.decode("utf-8", errors="replace").splitlines()
                if line.strip()
            ]
            pongs = [m for m in responses if m.type == "pong"]
            assert pongs, f"no pong received; got: {responses}"
            assert pongs[0].payload.get("wire_version") == 1
            assert pongs[0].payload.get("hostname"), "pong missing hostname"

            # 4. Stop: tear down the ControlMaster.
            transport.stop(host)
            assert not transport.check(host), "ControlMaster still alive after stop()"
        finally:
            transport.stop(host)

    # ── error paths ────────────────────────────────────────────────────

    def test_connection_refused_reports_error(self) -> None:
        """A refused connection (dead port) surfaces as exit 255 + pattern.

        Exercises the transport's own wire runner against a real refused
        TCP connection and checks the stderr is classified as an SSH
        connection error (the hook the fallback-alias retry uses).
        """
        port = _closed_port()
        ssh_cmd = _ssh_argv(
            "-p", str(port),
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5",
            "127.0.0.1",
            "true",
        )
        result = _run_ssh_wire(
            ssh_cmd, make_ping(), workdir=".", host_name="127.0.0.1", backend=""
        )
        assert result.returncode == 255, f"expected ssh exit 255; got {result.returncode}"
        assert "Connection refused" in result.stderr, f"unexpected stderr: {result.stderr}"
        assert _is_ssh_error(result.stderr), "refused connection not classified as SSH error"

    def test_auth_failure_reports_error(self) -> None:
        """An unusable key is rejected with Permission denied (exit 255).

        The user's ssh-agent is isolated (SSH_AUTH_SOCK) and identity
        files are restricted (IdentitiesOnly + a nonexistent key) so no
        valid credential can be offered.
        """
        env = dict(os.environ)
        env["SSH_AUTH_SOCK"] = "/nonexistent"
        ssh_cmd = _ssh_argv(
            "-i", "/nonexistent-integration-key",
            "-o", "IdentitiesOnly=yes",
            "-o", "PreferredAuthentications=publickey",
            "-o", "BatchMode=yes",
            "localhost",
            "true",
        )
        rc, stdout, stderr = _run(ssh_cmd, b"", env=env)
        assert rc == 255, f"expected ssh exit 255; got {rc} (stderr: {stderr})"
        assert "Permission denied" in stderr, f"unexpected stderr: {stderr}"
        assert _is_ssh_error(stderr), "auth failure not classified as SSH error"

    def test_connect_timeout_bounds_wait(self) -> None:
        """ConnectTimeout=1 makes a blackholed connect fail fast.

        ``192.0.2.1`` (RFC 5737 TEST-NET-1) is never routed, so the TCP
        connect hangs until the timeout.  Without ``ConnectTimeout=1``
        the OS default would take 75s+; the test asserts the whole ssh
        round trip returns within a small budget and reports a
        connection-level error.
        """
        ssh_cmd = _ssh_argv(
            "-o", "ConnectTimeout=1",
            "-o", "BatchMode=yes",
            "192.0.2.1",
            "true",
        )
        start = time.monotonic()
        result = _run_ssh_wire(
            ssh_cmd, make_ping(), workdir=".", host_name="192.0.2.1", backend="",
            timeout=60,
        )
        elapsed = time.monotonic() - start

        assert result.returncode == 255, f"expected ssh exit 255; got {result.returncode}"
        assert "timed out" in result.stderr.lower(), f"unexpected stderr: {result.stderr}"
        assert _is_ssh_error(result.stderr), "connect timeout not classified as SSH error"
        assert elapsed < 20, f"ConnectTimeout=1 not honored; connect took {elapsed:.1f}s"

    def test_wire_version_mismatch_rejected(self) -> None:
        """A request with the wrong wire_version is rejected with an error.

        The remote helper answers ``error`` (never ``pong``) for a bad
        version and stays alive for the next, correctly-versioned
        request.
        """
        transport = SSHTransport()
        host = _localhost_host(name="localhost-it-version")
        try:
            transport.warm(host)
            cm = transport._masters[host.ssh_alias]
            ssh_cmd = cm.ssh_cmd(*_remote_exec_argv())

            bad = {"wire_version": WIRE_VERSION + 5, "command": "ping"}
            rc, stdout, stderr = _run(ssh_cmd, encode_line(bad))
            responses = _decode_stream(stdout)
            assert rc == 0, f"ssh failed: {stderr}"
            assert not _pong(responses), "wrong version must not be answered with pong"
            errors = [m for m in responses if m.type == "error"]
            assert errors, f"expected an error response; got {responses}"
            assert "wire_version" in errors[0].message, errors[0].message

            # Helper survives: a valid ping still gets a pong.
            rc, stdout, stderr = _run(ssh_cmd, encode_line(make_ping()))
            pongs = _pong(_decode_stream(stdout))
            assert pongs, f"helper did not recover after version mismatch: {stderr}"
            assert pongs[0].payload.get("wire_version") == WIRE_VERSION
        finally:
            transport.stop(host)

    def test_malformed_response_handled_gracefully(self) -> None:
        """Garbage in either direction is tolerated without crashing.

        Client→remote: garbage lines draw an ``error`` reply and the
        helper keeps serving subsequent valid requests.  Remote→client:
        the transport skips non-JSON lines and still delivers the
        structured result.
        """
        transport = SSHTransport()
        host = _localhost_host(name="localhost-it-garbage")
        try:
            transport.warm(host)
            cm = transport._masters[host.ssh_alias]
            ssh_cmd = cm.ssh_cmd(*_remote_exec_argv())

            # (a) client sends garbage → error response, then recovery.
            rc, stdout, stderr = _run(ssh_cmd, b"this is not json\n" + encode_line(make_ping()))
            responses = _decode_stream(stdout)
            errors = [m for m in responses if m.type == "error"]
            assert errors, f"expected error for garbage input; got {responses}"
            assert "invalid JSON" in errors[0].message, errors[0].message
            assert _pong(responses), "helper must still answer the valid ping after garbage"

            # (b) remote spews garbage before protocol lines → skipped.
            entry = _remote_exec_argv()[0]
            script = f"printf '%s\\n' 'garbage-a' 'garbage-b' ; exec {entry}"
            ssh_cmd = cm.ssh_cmd("sh", "-c", script)
            req = make_request(
                command="run", task="probe",
                workdir="/nonexistent-garbage-probe", timeout=30,
            )
            result = _run_ssh_wire(
                ssh_cmd, req,
                workdir="/nonexistent-garbage-probe", host_name="localhost", backend="",
            )
            assert result.stderr == "workdir not found: /nonexistent-garbage-probe", (
                f"structured error lost among garbage: {result.stderr!r}"
            )
        finally:
            transport.stop(host)

    def test_large_payload_round_trip(self) -> None:
        """A near-1 MiB task is transported intact over the wire.

        The request line stays under ``MAX_LINE_LENGTH`` (1 MiB); the
        helper accepts it and answers with a structured error (bogus
        workdir) — proving the full SSH + JSONL round trip without
        executing a real task.
        """
        task = "a" * 1_000_000
        req = make_request(
            command="run", task=task, workdir="/nonexistent-large-payload", timeout=30,
        )
        line = encode_line(req)
        assert len(line) <= MAX_LINE_LENGTH, (
            f"payload {len(line)} exceeds the {MAX_LINE_LENGTH}-byte wire limit"
        )
        assert len(line) > 950_000, f"payload too small to be near 1 MiB: {len(line)}"

        transport = SSHTransport()
        host = _localhost_host(name="localhost-it-large")
        try:
            transport.warm(host)
            cm = transport._masters[host.ssh_alias]
            ssh_cmd = cm.ssh_cmd(*_remote_exec_argv())

            rc, stdout, stderr = _run(ssh_cmd, line, timeout=120)
            responses = _decode_stream(stdout)
            assert rc == 0, f"ssh failed: {stderr}"
            types = [m.type for m in responses]
            assert "accepted" in types, f"large request not accepted; got {responses}"
            errors = [m for m in responses if m.type == "error"]
            assert errors and errors[0].message == "workdir not found: /nonexistent-large-payload", (
                f"unexpected helper response: {responses}"
            )
        finally:
            transport.stop(host)

    def test_concurrent_sessions_no_conflict(self) -> None:
        """8 simultaneous wire sessions over one ControlMaster don't interfere.

        Half the workers ping; the other half run requests carrying a
        unique workdir marker that round-trips back in the error message.
        Each worker must receive its own response, so any cross-session
        mix-up fails the assertions.
        """
        transport = SSHTransport()
        host = _localhost_host(name="localhost-it-concurrent")
        try:
            transport.warm(host)
            cm = transport._masters[host.ssh_alias]
            ssh_cmd = cm.ssh_cmd(*_remote_exec_argv())

            def worker(i: int) -> tuple[int, str | None, list]:
                if i % 2 == 0:
                    marker = f"/nonexistent-marker-{i}"
                    req = make_request(
                        command="run", task=f"task-{i}", workdir=marker, timeout=30,
                    )
                else:
                    marker = None
                    req = make_ping()
                rc, stdout, stderr = _run(ssh_cmd, encode_line(req), timeout=60)
                return rc, marker, _decode_stream(stdout)

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(worker, range(8)))

            assert len(results) == 8
            for i, (rc, marker, responses) in enumerate(results):
                assert rc == 0, f"worker {i} ssh failed"
                if marker is None:  # ping worker
                    pongs = _pong(responses)
                    assert len(pongs) == 1, f"worker {i} expected one pong; got {responses}"
                    assert pongs[0].payload.get("wire_version") == WIRE_VERSION
                    assert pongs[0].payload.get("hostname"), "pong missing hostname"
                else:  # run worker: its own marker must come back
                    errors = [m for m in responses if m.type == "error"]
                    assert errors and errors[0].message == f"workdir not found: {marker}", (
                        f"worker {i} got a foreign response: {responses}"
                    )
        finally:
            transport.stop(host)

    def test_controlmaster_lifecycle_warm_stop_warm(self) -> None:
        """stop() fully tears down; warm() after stop() recreates a working master."""
        transport = SSHTransport()
        host = _localhost_host(name="localhost-it-lifecycle")
        try:
            transport.warm(host)
            assert transport.check(host), "master not alive after first warm()"

            transport.stop(host)
            assert not transport.check(host), "master still alive after stop()"

            transport.warm(host)
            assert transport.check(host), "master not alive after second warm()"

            # The recreated master must be functional, not just present.
            cm = transport._masters[host.ssh_alias]
            ssh_cmd = cm.ssh_cmd(*_remote_exec_argv())
            rc, stdout, stderr = _run(ssh_cmd, encode_line(make_ping()))
            pongs = _pong(_decode_stream(stdout))
            assert rc == 0 and pongs, f"ping over recreated master failed: {stderr}"
        finally:
            transport.stop(host)
