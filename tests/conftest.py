"""Shared fixtures for codeagent tests."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from codeagent.domain import HostSpec, RepoEntry, RepoMap, TopicSpec


# ---------------------------------------------------------------------------
# Integration test gating
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the ``--run-integration`` flag."""
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="run integration tests (require a working localhost SSH setup)",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Mirror the flag into the environment for collection-time skips.

    ``pytest.config`` was removed in pytest 7, so module-level
    ``skipif`` markers cannot read CLI options at import time.  Mirroring
    the option into an env var keeps ``tests/test_integration.py`` simple
    and works on every pytest version.
    """
    os.environ["RUN_INTEGRATION"] = (
        "1" if config.getoption("--run-integration", default=False) else "0"
    )
    config.addinivalue_line(
        "markers",
        "integration: integration tests gated on --run-integration",
    )


# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------

@pytest.fixture
def local_host() -> HostSpec:
    """A host that matches the current machine."""
    import socket
    return HostSpec(
        name="local-dev",
        ssh_alias="local-dev",
        hostnames=(socket.gethostname().split(".", 1)[0], "localhost"),
        description="local dev machine",
        transport="local",
    )


@pytest.fixture
def remote_host() -> HostSpec:
    return HostSpec(
        name="remote-build",
        ssh_alias="build-box",
        hostnames=("build-box",),
        description="remote build server",
    )


@pytest.fixture
def hosts_dict(local_host: HostSpec, remote_host: HostSpec) -> dict[str, HostSpec]:
    return {
        local_host.name: local_host,
        remote_host.name: remote_host,
    }


# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_topics(local_host: HostSpec, remote_host: HostSpec) -> dict[str, TopicSpec]:
    return {
        "codeagent": TopicSpec(
            name="codeagent",
            repos=(
                RepoEntry(host=local_host.name, path="/src/codeagent", note="primary"),
                RepoEntry(host=remote_host.name, path="/opt/codeagent", note="build mirror"),
            ),
            description="codeagent orchestration repo",
        ),
        "mi-docs": TopicSpec(
            name="mi-docs",
            repos=(
                RepoEntry(host=local_host.name, path="/docs/mi-docs", note="docs"),
            ),
            description="MI documentation",
        ),
    }


# ---------------------------------------------------------------------------
# RepoMap
# ---------------------------------------------------------------------------

@pytest.fixture
def repo_map(hosts_dict, sample_topics, tmp_path: Path) -> RepoMap:
    return RepoMap(
        midocs_root=tmp_path,
        hosts=hosts_dict,
        topics=sample_topics,
    )


# ---------------------------------------------------------------------------
# TCP integration fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def tcp_daemon(tmp_path: Path):
    """Start a temporary MailboxDaemon on an ephemeral port.

    Yields ``(daemon, mailbox_store, spool_store)`` and tears down on exit.
    """
    from codeagent.mailbox.store import MailboxStore
    from codeagent.tcp.server import MailboxDaemon
    from codeagent.tcp.spool import SpoolStore

    mailbox_store = MailboxStore(tmp_path / "mailbox")
    spool_store = SpoolStore(tmp_path / "spool")
    daemon = MailboxDaemon(
        host="127.0.0.1",
        port=0,
        mailbox_store=mailbox_store,
        spool_store=spool_store,
    )
    await daemon.start()
    yield daemon, mailbox_store, spool_store
    try:
        await daemon.stop()
    except Exception:
        pass


@pytest.fixture
async def tcp_tunnel(tmp_path: Path):
    """Establish a localhost SSH tunnel for integration testing.

    Yields a factory ``(daemon_port, forwarded_port) → proc`` that spawns
    an SSH ``-L`` tunnel forwarding *forwarded_port* → *daemon_port* via
    localhost.

    Skips the test if localhost SSH with BatchMode is unavailable.
    """
    import asyncio
    import socket
    import subprocess

    probe = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
         "localhost", "true"],
        capture_output=True, text=True, timeout=15,
    )
    if probe.returncode != 0:
        pytest.skip(
            "localhost SSH with BatchMode unavailable: "
            f"{probe.stderr.strip() or probe.returncode}"
        )

    procs: list[asyncio.subprocess.Process] = []

    def _find_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    async def _create(daemon_port: int, forwarded_port: int | None = None):
        lp = forwarded_port or _find_free_port()
        fwd = f"127.0.0.1:{lp}:127.0.0.1:{daemon_port}"
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-L", fwd,
            "-o", "ControlMaster=no",
            "-o", "ControlPath=none",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=5",
            "-o", "BatchMode=yes",
            "-N", "-T", "localhost",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        # Poll until the tunnel port accepts connections (up to 10s)
        for _ in range(100):
            await asyncio.sleep(0.1)
            if proc.returncode is not None:
                stderr = b""
                if proc.stderr:
                    stderr = await proc.stderr.read()
                pytest.skip(
                    f"SSH tunnel exited early (rc={proc.returncode}): "
                    f"{stderr.decode(errors='replace')[:200]}"
                )
            try:
                r, w = await asyncio.open_connection("127.0.0.1", lp)
                w.close()
                await w.wait_closed()
                break
            except (ConnectionRefusedError, OSError):
                continue
        else:
            proc.terminate()
            pytest.skip(f"SSH tunnel port {lp} never became reachable")
        procs.append(proc)
        return proc, lp

    yield _create

    for p in procs:
        try:
            p.terminate()
            await asyncio.wait_for(p.wait(), timeout=5.0)
        except Exception:
            try:
                p.kill()
            except ProcessLookupError:
                pass
