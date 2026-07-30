"""Tests for codeagent.config.repo_map — repo-map.json loader."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from codeagent.config.repo_map import VALID_TRANSPORTS, expand_path, load_repo_map
from codeagent.domain import HostSpec, RepoEntry, RepoMap, TopicSpec


FIXTURES = Path(__file__).parent / "fixtures" / "repo-map"


# ── helpers ─────────────────────────────────────────────────────────────


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _load_fixture(tmp_path: Path, midocs_name: str = "mi-docs") -> tuple[Path, Path]:
    """Copy fixture tree into tmp_path and return (global_json, midocs_root)."""
    midocs = tmp_path / midocs_name
    shutil.copytree(FIXTURES / "mi-docs", midocs)
    global_json = tmp_path / "repo-map.json"
    data = json.loads((FIXTURES / "global.json").read_text())
    data["midocs_root"] = str(midocs)
    _write_json(global_json, data)
    return global_json, midocs


# ── expand_path ─────────────────────────────────────────────────────────


class TestExpandPath:
    """Tests for the expand_path helper."""

    def test_tilde_expansion(self) -> None:
        result = expand_path("~/foo")
        assert result.startswith("/")
        assert "~" not in result

    def test_env_var_expansion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_REPO_DIR", "/opt/repos")
        assert expand_path("$TEST_REPO_DIR/code") == "/opt/repos/code"

    def test_combined_tilde_and_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MY_VAR", "bar")
        result = expand_path("~/$MY_VAR")
        assert result.endswith("/bar")
        assert "~" not in result

    def test_plain_string_passthrough(self) -> None:
        assert expand_path("/usr/local/bin") == "/usr/local/bin"

    def test_empty_string(self) -> None:
        assert expand_path("") == ""


# ── VALID_TRANSPORTS ────────────────────────────────────────────────────


class TestValidTransports:
    """Verify the exported transport set."""

    def test_contains_expected_values(self) -> None:
        assert "ssh" in VALID_TRANSPORTS
        assert "relay-login" in VALID_TRANSPORTS

    def test_is_frozen(self) -> None:
        assert isinstance(VALID_TRANSPORTS, frozenset)


# ── load_repo_map: happy path ───────────────────────────────────────────


class TestLoadRepoMapHappy:
    """Normal loading from fixture files."""

    def test_basic_load(self, tmp_path: Path) -> None:
        global_json, midocs = _load_fixture(tmp_path)
        rm = load_repo_map(global_json)

        assert isinstance(rm, RepoMap)
        assert rm.midocs_root == midocs
        assert rm.relay_zsh != ""

    def test_hosts_parsed(self, tmp_path: Path) -> None:
        global_json, _ = _load_fixture(tmp_path)
        rm = load_repo_map(global_json)

        assert set(rm.hosts.keys()) == {"yellow", "devbox"}
        yellow = rm.hosts["yellow"]
        assert isinstance(yellow, HostSpec)
        assert yellow.ssh_alias == "yellow"
        assert yellow.transport == "ssh"
        assert yellow.hostnames == ("yellow", "mcshyucs192069")

    def test_topics_parsed(self, tmp_path: Path) -> None:
        global_json, _ = _load_fixture(tmp_path)
        rm = load_repo_map(global_json)

        assert set(rm.topics.keys()) == {"EmptyTopic", "TestTopic"}

    def test_topic_repos(self, tmp_path: Path) -> None:
        global_json, _ = _load_fixture(tmp_path)
        rm = load_repo_map(global_json)
        tspec = rm.topics["TestTopic"]

        assert isinstance(tspec, TopicSpec)
        assert tspec.description == "OHOS reverse engineering"
        assert len(tspec.repos) == 2
        assert tspec.repos[0].host == "yellow"
        assert tspec.repos[0].note == "OHOS 7.0"
        assert tspec.repos[1].host == "devbox"

    def test_domain_topic_method(self, tmp_path: Path) -> None:
        """The TopicSpec.repo() method from domain works on loaded data."""
        global_json, _ = _load_fixture(tmp_path)
        rm = load_repo_map(global_json)

        entry = rm.topic("TestTopic").repo(0)
        assert isinstance(entry, RepoEntry)
        assert entry.host == "yellow"

    def test_domain_repo_map_topic_method(self, tmp_path: Path) -> None:
        """RepoMap.topic() raises KeyError for missing topics."""
        global_json, _ = _load_fixture(tmp_path)
        rm = load_repo_map(global_json)

        with pytest.raises(KeyError, match="NoSuchTopic"):
            rm.topic("NoSuchTopic")

    def test_relay_zsh_expanded(self, tmp_path: Path) -> None:
        global_json, _ = _load_fixture(tmp_path)
        rm = load_repo_map(global_json)

        assert "~" not in rm.relay_zsh
        assert rm.relay_zsh.endswith("/relay.zsh")

    def test_host_shell_prefix_expanded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """shell_prefix containing env vars is expanded at load time."""
        global_json, _ = _load_fixture(tmp_path)
        monkeypatch.setenv("HOME", "/home/testuser")
        rm = load_repo_map(global_json)

        prefix = rm.hosts["yellow"].shell_prefix
        assert "~" not in prefix
        assert "/home/testuser" in prefix

    def test_host_defaults(self, tmp_path: Path) -> None:
        """A host with only required fields gets correct defaults."""
        global_json, midocs = _load_fixture(tmp_path)
        data = json.loads(global_json.read_text())
        data["hosts"]["minimal"] = {
            "ssh_alias": "mini-host",
            "hostnames": ["mini"],
        }
        _write_json(global_json, data)
        rm = load_repo_map(global_json)

        mini = rm.hosts["minimal"]
        assert mini.transport == "ssh"
        assert mini.description == ""
        assert mini.shell_prefix == ""
        assert mini.fallback_ssh_alias == ""

    def test_hostnames_default_empty_tuple(self, tmp_path: Path) -> None:
        """Omitting hostnames results in an empty tuple, not None."""
        global_json, _ = _load_fixture(tmp_path)
        data = json.loads(global_json.read_text())
        data["hosts"]["noalias"] = {
            "ssh_alias": "no-alias-host",
        }
        _write_json(global_json, data)
        rm = load_repo_map(global_json)

        assert rm.hosts["noalias"].hostnames == ()


# ── load_repo_map: path expansion in repos ──────────────────────────────


class TestLoadRepoMapPathExpansion:
    """Repo paths and midocs_root are expanded at load time."""

    def test_midocs_root_expanded(self, tmp_path: Path) -> None:
        global_json, _ = _load_fixture(tmp_path)
        data = json.loads(global_json.read_text())
        data["midocs_root"] = "~/expanded/root"
        _write_json(global_json, data)
        rm = load_repo_map(global_json)

        assert "~" not in str(rm.midocs_root)

    def test_repo_path_expanded(self, tmp_path: Path) -> None:
        global_json, midocs = _load_fixture(tmp_path)
        topic_json = midocs / "TestTopic" / ".repo-map.json"
        topic_data = json.loads(topic_json.read_text())
        topic_data["repos"] = [{"host": "yellow", "path": "~/src/code"}]
        _write_json(topic_json, topic_data)
        rm = load_repo_map(global_json)

        assert "~" not in rm.topics["TestTopic"].repos[0].path

    def test_env_var_in_repo_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODE_ROOT", "/workspace")
        global_json, midocs = _load_fixture(tmp_path)
        topic_json = midocs / "TestTopic" / ".repo-map.json"
        topic_data = json.loads(topic_json.read_text())
        topic_data["repos"] = [{"host": "yellow", "path": "$CODE_ROOT/src"}]
        _write_json(topic_json, topic_data)
        rm = load_repo_map(global_json)

        assert rm.topics["TestTopic"].repos[0].path == "/workspace/src"


# ── load_repo_map: empty / missing midocs_root ─────────────────────────


class TestLoadRepoMapEmpty:
    """Graceful handling when midocs_root is absent or has no topics."""

    def test_missing_midocs_root_dir(self, tmp_path: Path) -> None:
        """Non-existent midocs_root yields empty topics, no error."""
        global_json = tmp_path / "repo-map.json"
        _write_json(global_json, {
            "midocs_root": str(tmp_path / "nonexistent"),
            "hosts": {
                "h": {"ssh_alias": "h-host", "hostnames": ["h"]},
            },
        })
        rm = load_repo_map(global_json)

        assert rm.topics == {}

    def test_midocs_root_is_file(self, tmp_path: Path) -> None:
        """midocs_root pointing to a file (not dir) yields empty topics."""
        global_json = tmp_path / "repo-map.json"
        not_a_dir = tmp_path / "file_not_dir"
        not_a_dir.write_text("not a directory")
        _write_json(global_json, {
            "midocs_root": str(not_a_dir),
            "hosts": {
                "h": {"ssh_alias": "h-host", "hostnames": ["h"]},
            },
        })
        rm = load_repo_map(global_json)

        assert rm.topics == {}

    def test_empty_midocs_root_dir(self, tmp_path: Path) -> None:
        """Empty midocs_root directory yields empty topics."""
        midocs = tmp_path / "mi-docs"
        midocs.mkdir()
        global_json = tmp_path / "repo-map.json"
        _write_json(global_json, {
            "midocs_root": str(midocs),
            "hosts": {
                "h": {"ssh_alias": "h-host", "hostnames": ["h"]},
            },
        })
        rm = load_repo_map(global_json)

        assert rm.topics == {}

    def test_no_hosts_key(self, tmp_path: Path) -> None:
        """Global config with no 'hosts' key yields empty hosts dict."""
        global_json = tmp_path / "repo-map.json"
        midocs = tmp_path / "mi-docs"
        midocs.mkdir()
        _write_json(global_json, {"midocs_root": str(midocs)})
        rm = load_repo_map(global_json)

        assert rm.hosts == {}
        assert rm.topics == {}

    def test_dot_prefixed_dirs_skipped(self, tmp_path: Path) -> None:
        """Directories starting with '.' are not treated as topics."""
        midocs = tmp_path / "mi-docs"
        (midocs / ".drafts").mkdir(parents=True)
        _write_json(midocs / ".drafts" / ".repo-map.json", {
            "description": "should be skipped",
            "repos": [],
        })
        global_json = tmp_path / "repo-map.json"
        _write_json(global_json, {
            "midocs_root": str(midocs),
            "hosts": {
                "h": {"ssh_alias": "h-host", "hostnames": ["h"]},
            },
        })
        rm = load_repo_map(global_json)

        assert ".drafts" not in rm.topics

    def test_dirs_without_repo_map_skipped(self, tmp_path: Path) -> None:
        """Directories without .repo-map.json are silently skipped."""
        midocs = tmp_path / "mi-docs"
        (midocs / "NoConfig").mkdir(parents=True)
        global_json = tmp_path / "repo-map.json"
        _write_json(global_json, {
            "midocs_root": str(midocs),
            "hosts": {
                "h": {"ssh_alias": "h-host", "hostnames": ["h"]},
            },
        })
        rm = load_repo_map(global_json)

        assert "NoConfig" not in rm.topics

    def test_empty_repos_list(self, tmp_path: Path) -> None:
        """A topic with repos=[] yields a TopicSpec with zero repos."""
        global_json, _ = _load_fixture(tmp_path)
        rm = load_repo_map(global_json)

        empty = rm.topics["EmptyTopic"]
        assert empty.description == "empty topic with no repos"
        assert len(empty.repos) == 0


# ── load_repo_map: validation errors ───────────────────────────────────


class TestLoadRepoMapErrors:
    """Invalid configs raise ValueError with actionable messages."""

    def test_invalid_transport(self, tmp_path: Path) -> None:
        global_json = tmp_path / "repo-map.json"
        _write_json(global_json, {
            "midocs_root": str(tmp_path / "mi-docs"),
            "hosts": {
                "bad": {"ssh_alias": "b", "hostnames": ["b"], "transport": "telnet"},
            },
        })
        with pytest.raises(ValueError, match="transport invalid.*telnet"):
            load_repo_map(global_json)

    def test_missing_ssh_alias(self, tmp_path: Path) -> None:
        global_json = tmp_path / "repo-map.json"
        _write_json(global_json, {
            "midocs_root": str(tmp_path / "mi-docs"),
            "hosts": {
                "bad": {"ssh_alias": "", "hostnames": ["b"]},
            },
        })
        with pytest.raises(ValueError, match="ssh_alias must be a non-empty string"):
            load_repo_map(global_json)

    def test_empty_ssh_alias(self, tmp_path: Path) -> None:
        global_json = tmp_path / "repo-map.json"
        _write_json(global_json, {
            "midocs_root": str(tmp_path / "mi-docs"),
            "hosts": {
                "bad": {"ssh_alias": "   ", "hostnames": ["b"]},
            },
        })
        with pytest.raises(ValueError, match="ssh_alias must be a non-empty string"):
            load_repo_map(global_json)

    def test_undefined_host_in_topic(self, tmp_path: Path) -> None:
        """A topic repo referencing an undefined host raises ValueError."""
        global_json, midocs = _load_fixture(tmp_path)
        topic_json = midocs / "TestTopic" / ".repo-map.json"
        _write_json(topic_json, {
            "repos": [{"host": "nonexistent", "path": "/tmp"}],
        })
        with pytest.raises(ValueError, match="undefined host.*nonexistent"):
            load_repo_map(global_json)

    def test_fallback_equals_ssh_alias(self, tmp_path: Path) -> None:
        global_json = tmp_path / "repo-map.json"
        _write_json(global_json, {
            "midocs_root": str(tmp_path / "mi-docs"),
            "hosts": {
                "bad": {
                    "ssh_alias": "same",
                    "hostnames": ["b"],
                    "fallback_ssh_alias": "same",
                },
            },
        })
        with pytest.raises(ValueError, match="fallback_ssh_alias must differ"):
            load_repo_map(global_json)

    def test_fallback_with_relay_login(self, tmp_path: Path) -> None:
        global_json = tmp_path / "repo-map.json"
        _write_json(global_json, {
            "midocs_root": str(tmp_path / "mi-docs"),
            "hosts": {
                "bad": {
                    "ssh_alias": "relay-host",
                    "hostnames": ["b"],
                    "transport": "relay-login",
                    "fallback_ssh_alias": "backup",
                },
            },
        })
        with pytest.raises(ValueError, match="does not support fallback_ssh_alias"):
            load_repo_map(global_json)

    def test_empty_fallback_is_ok(self, tmp_path: Path) -> None:
        """Empty/None fallback_ssh_alias is silently accepted."""
        global_json = tmp_path / "repo-map.json"
        _write_json(global_json, {
            "midocs_root": str(tmp_path / "mi-docs"),
            "hosts": {
                "ok": {
                    "ssh_alias": "ok-host",
                    "hostnames": ["ok"],
                    "fallback_ssh_alias": "",
                },
            },
        })
        rm = load_repo_map(global_json)
        assert rm.hosts["ok"].fallback_ssh_alias == ""

    def test_file_not_found(self, tmp_path: Path) -> None:
        """Non-existent config path raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_repo_map(tmp_path / "nope.json")

    def test_malformed_json(self, tmp_path: Path) -> None:
        """Invalid JSON raises json.JSONDecodeError."""
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            load_repo_map(bad)

    def test_missing_path_in_repo_entry(self, tmp_path: Path) -> None:
        """A repo entry without 'path' raises KeyError."""
        global_json, midocs = _load_fixture(tmp_path)
        topic_json = midocs / "TestTopic" / ".repo-map.json"
        _write_json(topic_json, {
            "repos": [{"host": "yellow"}],
        })
        with pytest.raises(KeyError):
            load_repo_map(global_json)

    def test_missing_host_in_repo_entry(self, tmp_path: Path) -> None:
        """A repo entry without 'host' raises KeyError."""
        global_json, midocs = _load_fixture(tmp_path)
        topic_json = midocs / "TestTopic" / ".repo-map.json"
        _write_json(topic_json, {
            "repos": [{"path": "/tmp"}],
        })
        with pytest.raises(KeyError):
            load_repo_map(global_json)
