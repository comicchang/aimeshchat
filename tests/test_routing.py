"""Tests for codeagent.routing.resolver."""
from __future__ import annotations

import socket

import pytest

from codeagent.domain import HostSpec, RepoEntry, RepoMap, RunRequest, TopicSpec, resolve_is_local
from codeagent.routing.resolver import resolve_target


class TestResolveByHost:
    """request.host is set → direct host resolution."""

    def test_host_in_map(self, repo_map: RepoMap):
        req = RunRequest(task="do stuff", host="remote-build")
        target = resolve_target(req, repo_map)

        assert target.host.name == "remote-build"
        assert target.host.ssh_alias == "build-box"
        assert target.repo.path == "."
        assert target.topic is None
        assert target.repo_index == 0

    def test_host_in_map_with_workdir(self, repo_map: RepoMap):
        req = RunRequest(task="do stuff", host="remote-build", workdir="/opt/project")
        target = resolve_target(req, repo_map)

        assert target.repo.path == "/opt/project"

    def test_adhoc_host_not_in_map(self, repo_map: RepoMap):
        req = RunRequest(task="do stuff", host="unknown-box")
        target = resolve_target(req, repo_map)

        assert target.host.name == "unknown-box"
        assert target.host.ssh_alias == "unknown-box"
        assert target.is_local is False  # unknown host ≠ local

    def test_local_host_detected(self, repo_map: RepoMap, local_host: HostSpec):
        req = RunRequest(task="do stuff", host=local_host.name)
        target = resolve_target(req, repo_map)

        assert target.is_local is True


class TestResolveByTopic:
    """request.topic is set → topic → repo_index → host lookup."""

    def test_topic_first_repo(self, repo_map: RepoMap):
        req = RunRequest(task="analyze", topic="codeagent")
        target = resolve_target(req, repo_map)

        assert target.topic is not None
        assert target.topic.name == "codeagent"
        assert target.repo_index == 0
        assert target.repo.path == "/src/codeagent"
        assert target.host.name == "local-dev"

    def test_topic_second_repo(self, repo_map: RepoMap):
        req = RunRequest(task="build", topic="codeagent", repo_index=1)
        target = resolve_target(req, repo_map)

        assert target.repo_index == 1
        assert target.repo.path == "/opt/codeagent"
        assert target.host.ssh_alias == "build-box"
        assert target.is_local is False

    def test_topic_single_repo(self, repo_map: RepoMap):
        req = RunRequest(task="edit docs", topic="mi-docs")
        target = resolve_target(req, repo_map)

        assert target.topic.name == "mi-docs"
        assert target.repo.path == "/docs/mi-docs"

    def test_topic_not_found(self, repo_map: RepoMap):
        req = RunRequest(task="noop", topic="nonexistent")
        with pytest.raises(KeyError, match="nonexistent"):
            resolve_target(req, repo_map)

    def test_topic_repo_index_oob(self, repo_map: RepoMap):
        req = RunRequest(task="noop", topic="mi-docs", repo_index=5)
        with pytest.raises(IndexError, match="越界"):
            resolve_target(req, repo_map)

    def test_topic_with_unmapped_host(self, repo_map: RepoMap, tmp_path):
        """Topic references a host not in the hosts dict → synthetic HostSpec."""
        topics = {
            "edge": TopicSpec(
                name="edge",
                repos=(RepoEntry(host="orphan-host", path="/data"),),
            ),
        }
        rm = RepoMap(midocs_root=tmp_path, hosts={}, topics=topics)
        req = RunRequest(task="x", topic="edge")
        target = resolve_target(req, rm)

        assert target.host.name == "orphan-host"
        assert target.is_local is False  # orphan → not recognized as local


class TestResolveLocalFallback:
    """Neither host nor topic → local execution."""

    def test_with_workdir(self, repo_map: RepoMap):
        req = RunRequest(task="quick fix", workdir="/Users/me/project")
        target = resolve_target(req, repo_map)

        assert target.is_local is True
        assert target.host.transport == "local"
        assert target.repo.path == "/Users/me/project"
        assert target.topic is None

    def test_empty_workdir(self, repo_map: RepoMap):
        req = RunRequest(task="noop")
        target = resolve_target(req, repo_map)

        assert target.is_local is True
        assert target.repo.path == "."

    def test_host_takes_priority_over_topic(self, repo_map: RepoMap):
        """When both host and topic are set, host wins."""
        req = RunRequest(task="x", host="remote-build", topic="codeagent")
        target = resolve_target(req, repo_map)

        assert target.host.name == "remote-build"
        assert target.topic is None  # topic ignored


class TestResolveIsLocal:
    """Unit tests for the domain helper."""

    def test_hostname_match(self):
        host = HostSpec(
            name="x", ssh_alias="x",
            hostnames=(socket.gethostname().split(".", 1)[0],),
        )
        assert resolve_is_local(host) is True

    def test_no_match(self):
        host = HostSpec(name="x", ssh_alias="x", hostnames=("definitely-not-a-real-host",))
        assert resolve_is_local(host) is False

    def test_empty_hostnames(self):
        host = HostSpec(name="x", ssh_alias="x", hostnames=())
        assert resolve_is_local(host) is False

    def test_substring_match(self):
        """resolve_is_local uses substring matching."""
        host = HostSpec(name="x", ssh_alias="x", hostnames=("mac",))
        # will match if hostname contains "mac" (case-insensitive)
        actual = socket.gethostname().split(".", 1)[0].lower()
        expected = "mac" in actual
        assert resolve_is_local(host) is expected
