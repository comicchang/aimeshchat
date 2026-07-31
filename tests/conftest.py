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
