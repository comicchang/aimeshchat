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
# F3: forbid accidental real-backend spawns (they hang the suite)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_real_backend_spawn(monkeypatch):
    """Block accidental real `omp`/`opencode` subprocess spawns.

    These binaries run indefinitely (waiting on a model) and hang the suite
    (test_runtime_registry hung for 90s+ before this guard). Tests that
    INTENTIONALLY exercise a backend must patch ``subprocess.Popen`` (or the
    runner/adapter) explicitly — the explicit patch overrides this guard.

    Implemented as a Popen SUBCLASS (not a function wrapper) so
    ``MagicMock(spec=subprocess.Popen)`` still exposes the real attribute
    surface (poll/communicate/wait/…).
    """
    import subprocess as _sp

    _BLOCKED = frozenset({"omp", "opencode"})

    class _GuardedPopen(_sp.Popen):
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            argv = args[0] if args else kwargs.get("args", [])
            if isinstance(argv, (list, tuple)) and argv and isinstance(argv[0], str):
                binary = argv[0]
                head = binary.rsplit("/", 1)[-1]
                # Block bare names (PATH-resolved real backends) and EXISTING
                # absolute paths. Allow nonexistent paths (e.g. /nonexistent/omp)
                # — Popen raises FileNotFoundError naturally, no hang.
                if head in _BLOCKED and ("/" not in binary or Path(binary).exists()):
                    raise RuntimeError(
                        f"tests must not spawn real backend binary: {argv!r} "
                        "(patch subprocess.Popen / the adapter to mock it)"
                    )
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(_sp, "Popen", _GuardedPopen)
