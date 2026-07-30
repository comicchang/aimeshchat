"""End-to-end tests for the CLI facade.

Exercises main(argv) with mocked transport, registry, and _execute
to cover all subcommands without real SSH or subprocess calls.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from codeagent.domain import HostSpec, RepoEntry, RepoMap, RunResult, TopicSpec


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_repo_map(tmp_path: Path | None = None) -> RepoMap:
    """Build a deterministic RepoMap for CLI tests."""
    root = tmp_path or Path("/tmp/fake-midocs")
    hosts = {
        "devhost": HostSpec(
            name="devhost",
            ssh_alias="devhost",
            hostnames=("devhost",),
            description="dev server",
        ),
        "devbox": HostSpec(
            name="devbox",
            ssh_alias="devbox",
            hostnames=("devbox",),
            description="dev box",
            transport="relay-login",
        ),
    }
    topics = {
        "TestTopic": TopicSpec(
            name="TestTopic",
            repos=(
                RepoEntry(host="devhost", path="/src/test-repo", note="primary"),
                RepoEntry(host="devbox", path="/opt/test-repo", note="mirror"),
            ),
            description="A test topic",
        ),
        "AnotherTopic": TopicSpec(
            name="AnotherTopic",
            repos=(
                RepoEntry(host="devhost", path="/src/another", note="only repo"),
            ),
            description="Another test topic",
        ),
    }
    return RepoMap(midocs_root=root, hosts=hosts, topics=topics)


def _make_run_result(**kw) -> RunResult:
    defaults = {"returncode": 0, "stdout": "ok", "stderr": "", "session_id": "sess-123"}
    defaults.update(kw)
    return RunResult(**defaults)


# ── TestCLIHelp ──────────────────────────────────────────────────────────


class TestCLIHelp:
    """--version, --help, route --help all produce expected output."""

    def test_version(self, capsys):
        from codeagent.cli import main

        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "0.1.0" in out

    def test_help(self, capsys):
        from codeagent.cli import main

        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "route" in out
        assert "run" in out
        assert "sessions" in out
        assert "ssh" in out

    def test_route_help(self, capsys):
        from codeagent.cli import main

        with pytest.raises(SystemExit) as exc:
            main(["route", "--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "--dry-run" in out or "dry-run" in out


# ── TestRouteList ────────────────────────────────────────────────────────


class TestRouteList:
    """route list outputs topics from the repo map."""

    def test_list_outputs_topics(self, capsys, tmp_path):
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        with mock.patch("codeagent.cli.load_repo_map", return_value=rm):
            rc = main(["route", "list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "TestTopic" in out
        assert "AnotherTopic" in out

    def test_list_json(self, capsys, tmp_path):
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        with mock.patch("codeagent.cli.load_repo_map", return_value=rm):
            rc = main(["route", "list", "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "TestTopic" in data
        assert "AnotherTopic" in data
        assert data["TestTopic"]["description"] == "A test topic"
        assert len(data["TestTopic"]["repos"]) == 2


# ── TestRouteWhere ───────────────────────────────────────────────────────


class TestRouteWhere:
    """route where <topic> prints topic details or errors on missing."""

    def test_where_topic(self, capsys, tmp_path):
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        with mock.patch("codeagent.cli.load_repo_map", return_value=rm):
            rc = main(["route", "where", "TestTopic"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "TestTopic" in out
        assert "/src/test-repo" in out

    def test_where_nonexistent(self, capsys, tmp_path):
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        with mock.patch("codeagent.cli.load_repo_map", return_value=rm):
            with pytest.raises(KeyError, match="NoSuchTopic"):
                main(["route", "where", "NoSuchTopic"])


# ── TestRouteDryRun ──────────────────────────────────────────────────────


class TestRouteDryRun:
    """route <topic> <task> --dry-run prints routing info without executing."""

    def test_dry_run(self, capsys, tmp_path):
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        with mock.patch("codeagent.cli.load_repo_map", return_value=rm):
            rc = main(["route", "TestTopic", "do something", "--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Topic:" in out
        assert "TestTopic" in out
        assert "devhost" in out


# ── TestRouteExecution ───────────────────────────────────────────────────


class TestRouteExecution:
    """route <topic> <task> invokes _execute with correct request fields."""

    def test_route_calls_execute(self, capsys, tmp_path):
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        result = _make_run_result()
        with (
            mock.patch("codeagent.cli.load_repo_map", return_value=rm),
            mock.patch("codeagent.cli._execute", return_value=result) as mock_exec,
            mock.patch("codeagent.cli.SessionRegistry"),
        ):
            rc = main(["route", "TestTopic", "analyze code", "--raw"])
        assert rc == 0
        mock_exec.assert_called_once()
        call_args = mock_exec.call_args
        request = call_args.args[0] if call_args.args else call_args[1].get("request")
        assert request.task == "analyze code"
        assert request.topic == "TestTopic"

    def test_route_wraps_task_with_prompt(self, capsys, tmp_path):
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        result = _make_run_result()
        with (
            mock.patch("codeagent.cli.load_repo_map", return_value=rm),
            mock.patch("codeagent.cli._execute", return_value=result) as mock_exec,
            mock.patch("codeagent.cli.SessionRegistry"),
        ):
            rc = main(["route", "TestTopic", "analyze code"])
        assert rc == 0
        call_args = mock_exec.call_args
        request = call_args.args[0]
        # Default (no --raw) should wrap with structured prompt
        assert "你正在执行一项代码调研任务" in request.task
        assert "analyze code" in request.task
        assert request.raw is False

    def test_route_raw_skips_prompt(self, capsys, tmp_path):
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        result = _make_run_result()
        with (
            mock.patch("codeagent.cli.load_repo_map", return_value=rm),
            mock.patch("codeagent.cli._execute", return_value=result) as mock_exec,
            mock.patch("codeagent.cli.SessionRegistry"),
        ):
            rc = main(["route", "TestTopic", "analyze code", "--raw"])
        assert rc == 0
        call_args = mock_exec.call_args
        request = call_args.args[0]
        assert request.task == "analyze code"
        assert request.raw is True

    def test_route_passes_backend_agent_model(self, capsys, tmp_path):
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        result = _make_run_result()
        with (
            mock.patch("codeagent.cli.load_repo_map", return_value=rm),
            mock.patch("codeagent.cli._execute", return_value=result) as mock_exec,
            mock.patch("codeagent.cli.SessionRegistry"),
        ):
            rc = main([
                "route", "TestTopic", "task", "--raw",
                "--backend", "mybackend",
                "--agent", "myagent",
                "--model", "mymodel",
            ])
        assert rc == 0
        call_args = mock_exec.call_args
        request = call_args.args[0]
        assert request.backend == "mybackend"
        assert request.agent == "myagent"
        assert request.model == "mymodel"

    def test_route_passes_skills_and_skip_permissions(self, capsys, tmp_path):
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        result = _make_run_result()
        with (
            mock.patch("codeagent.cli.load_repo_map", return_value=rm),
            mock.patch("codeagent.cli._execute", return_value=result) as mock_exec,
            mock.patch("codeagent.cli.SessionRegistry"),
        ):
            rc = main([
                "route", "TestTopic", "task", "--raw",
                "--skills", "my-skills",
                "--skip-permissions",
            ])
        assert rc == 0
        call_args = mock_exec.call_args
        request = call_args.args[0]
        assert request.skills == "my-skills"
        assert request.skip_permissions is True

    def test_route_no_task_exits_1(self, capsys, tmp_path):
        """route <topic> with no task and empty stdin should error."""
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        with (
            mock.patch("codeagent.cli.load_repo_map", return_value=rm),
            mock.patch("sys.stdin.read", return_value=""),
        ):
            rc = main(["route", "TestTopic"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "no task" in err.lower() or "error" in err.lower()


# ── TestRunDefaults ──────────────────────────────────────────────────────


class TestRunDefaults:
    """run subcommand default values and flag propagation."""

    def test_run_backend_default(self, tmp_path):
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        result = _make_run_result()
        with (
            mock.patch("codeagent.cli.load_repo_map", return_value=rm),
            mock.patch("codeagent.cli._execute", return_value=result) as mock_exec,
            mock.patch("codeagent.cli.SessionRegistry"),
        ):
            main(["run", "do something"])
        request = mock_exec.call_args.args[0]
        assert request.backend == "opencode"

    def test_run_new_session(self, tmp_path):
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        result = _make_run_result()
        with (
            mock.patch("codeagent.cli.load_repo_map", return_value=rm),
            mock.patch("codeagent.cli._execute", return_value=result) as mock_exec,
            mock.patch("codeagent.cli.SessionRegistry"),
        ):
            main(["run", "do something", "--new-session"])
        request = mock_exec.call_args.args[0]
        assert request.new_session is True

    def test_run_with_host(self, tmp_path):
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        result = _make_run_result()
        with (
            mock.patch("codeagent.cli.load_repo_map", return_value=rm),
            mock.patch("codeagent.cli._execute", return_value=result) as mock_exec,
            mock.patch("codeagent.cli.SessionRegistry"),
        ):
            main(["run", "do something", "--host", "devhost"])
        request = mock_exec.call_args.args[0]
        assert request.host == "devhost"

    def test_run_no_task_exits_1(self, capsys):
        from codeagent.cli import main

        with mock.patch("sys.stdin.read", return_value=""):
            rc = main(["run"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "no task" in err.lower() or "error" in err.lower()

    def test_run_result_printed(self, capsys, tmp_path):
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        result = _make_run_result(returncode=0, stdout="hello world", stderr="")
        with (
            mock.patch("codeagent.cli.load_repo_map", return_value=rm),
            mock.patch("codeagent.cli._execute", return_value=result),
            mock.patch("codeagent.cli.SessionRegistry"),
        ):
            rc = main(["run", "do something"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "hello world" in out


# ── TestSessions ─────────────────────────────────────────────────────────


class TestSessions:
    """sessions subcommand: list, show, reset, bind."""

    def test_list_empty(self, capsys, tmp_path):
        from codeagent.cli import main
        from codeagent.session.registry import SessionRegistry

        db = tmp_path / "sessions.sqlite3"
        registry = SessionRegistry(db_path=db)
        with mock.patch("codeagent.cli.SessionRegistry", return_value=registry):
            rc = main(["sessions", "list"])
        assert rc == 0

    def test_show_nonexistent(self, capsys, tmp_path):
        from codeagent.cli import main
        from codeagent.session.registry import SessionRegistry

        db = tmp_path / "sessions.sqlite3"
        registry = SessionRegistry(db_path=db)
        with mock.patch("codeagent.cli.SessionRegistry", return_value=registry):
            rc = main(["sessions", "show", "nonexistent-key"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "not found" in out.lower()

    def test_reset_key(self, capsys, tmp_path):
        from codeagent.cli import main
        from codeagent.session.registry import SessionRegistry

        db = tmp_path / "sessions.sqlite3"
        registry = SessionRegistry(db_path=db)
        with mock.patch("codeagent.cli.SessionRegistry", return_value=registry):
            rc = main(["sessions", "reset", "some-key"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "reset" in out.lower()

    def test_bind_key_and_id(self, capsys, tmp_path):
        from codeagent.cli import main
        from codeagent.session.registry import SessionRegistry

        db = tmp_path / "sessions.sqlite3"
        registry = SessionRegistry(db_path=db)
        with mock.patch("codeagent.cli.SessionRegistry", return_value=registry):
            rc = main(["sessions", "bind", "--key", "my-key", "--id", "my-session-id"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "bound" in out.lower()


# ── TestSSH ──────────────────────────────────────────────────────────────


class TestSSH:
    """ssh subcommand: warm, status, stop."""

    def test_status(self, capsys):
        from codeagent.cli import main

        transport = mock.MagicMock()
        transport.list_sockets.return_value = [
            ("devhost", Path("/tmp/devhost.sock")),
        ]
        transport.check.return_value = True
        with mock.patch("codeagent.cli.SSHTransport", return_value=transport):
            rc = main(["ssh", "status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "devhost" in out

    def test_warm(self, capsys):
        from codeagent.cli import main

        transport = mock.MagicMock()
        transport.warm.return_value = None
        with mock.patch("codeagent.cli.SSHTransport", return_value=transport):
            rc = main(["ssh", "warm", "devhost"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "ok" in out.lower()

    def test_stop(self, capsys):
        from codeagent.cli import main

        transport = mock.MagicMock()
        transport.stop.return_value = None
        with mock.patch("codeagent.cli.SSHTransport", return_value=transport):
            rc = main(["ssh", "stop", "devhost"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "stopped" in out.lower()


# ── TestSkipPermissionsDefault ───────────────────────────────────────────


class TestSkipPermissionsDefault:
    """Verify skip_permissions flag propagation from CLI args."""

    def test_default_false(self, tmp_path):
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        result = _make_run_result()
        with (
            mock.patch("codeagent.cli.load_repo_map", return_value=rm),
            mock.patch("codeagent.cli._execute", return_value=result) as mock_exec,
            mock.patch("codeagent.cli.SessionRegistry"),
        ):
            main(["run", "do something"])
        request = mock_exec.call_args.args[0]
        assert request.skip_permissions is False

    def test_with_flag(self, tmp_path):
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        result = _make_run_result()
        with (
            mock.patch("codeagent.cli.load_repo_map", return_value=rm),
            mock.patch("codeagent.cli._execute", return_value=result) as mock_exec,
            mock.patch("codeagent.cli.SessionRegistry"),
        ):
            main(["run", "do something", "--skip-permissions"])
        request = mock_exec.call_args.args[0]
        assert request.skip_permissions is True

    def test_route_skip_permissions_default_false(self, tmp_path):
        """route subcommand defaults skip_permissions to False (no CLI flag)."""
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        result = _make_run_result()
        with (
            mock.patch("codeagent.cli.load_repo_map", return_value=rm),
            mock.patch("codeagent.cli._execute", return_value=result) as mock_exec,
            mock.patch("codeagent.cli.SessionRegistry"),
        ):
            main(["route", "TestTopic", "task"])
        request = mock_exec.call_args.args[0]
        assert request.skip_permissions is False


# ── TestExecuteFallback ──────────────────────────────────────────────────


class TestExecuteFallback:
    """_execute fallback on warm() failure."""

    def test_warm_failure_tries_fallback(self, tmp_path):
        """When warm() raises TransportError and host has fallback, retries with fallback alias."""
        from codeagent.cli import _execute
        from codeagent.domain import RepoEntry, RunRequest, Target
        from codeagent.transport.base import TransportError

        host = HostSpec(
            name="primary",
            ssh_alias="primary",
            hostnames=("primary",),
            transport="ssh",
            fallback_ssh_alias="fallback",
        )
        repo = RepoEntry(host="primary", path="/work")
        target = Target(host=host, repo=repo)
        request = RunRequest(task="do stuff", workdir="/work", backend="opencode")

        fallback_result = RunResult(returncode=0, stdout="ok", stderr="", host="fallback")

        registry = mock.MagicMock()
        registry.compute_key.return_value = "test-key"
        registry.lookup.return_value = None
        registry.run_with_lock.return_value = mock.MagicMock(__enter__=mock.MagicMock(), __exit__=mock.MagicMock())

        transport = mock.MagicMock()
        # warm() raises on primary, succeeds on fallback
        transport.warm.side_effect = [TransportError("master create failed"), None]
        transport.execute.return_value = fallback_result

        with mock.patch("codeagent.cli._get_transport", return_value=transport):
            result = _execute(request, target, registry)

        assert result.returncode == 0
        assert result.stdout == "ok"
        # warm called twice: primary (fails) + fallback (succeeds)
        assert transport.warm.call_count == 2
        fallback_host = transport.warm.call_args_list[1][0][0]
        assert fallback_host.ssh_alias == "fallback"
        assert fallback_host.fallback_ssh_alias == ""  # cleared to prevent loops
        # execute called with fallback host
        execute_host = transport.execute.call_args[0][1]
        assert execute_host.ssh_alias == "fallback"

    def test_warm_failure_no_fallback_propagates(self, tmp_path):
        """When warm() raises and no fallback, the error propagates as transport error."""
        from codeagent.cli import _execute
        from codeagent.domain import RepoEntry, RunRequest, Target
        from codeagent.transport.base import TransportError

        host = HostSpec(
            name="primary",
            ssh_alias="primary",
            hostnames=("primary",),
            transport="ssh",
            fallback_ssh_alias="",
        )
        repo = RepoEntry(host="primary", path="/work")
        target = Target(host=host, repo=repo)
        request = RunRequest(task="do stuff", workdir="/work", backend="opencode")

        registry = mock.MagicMock()
        registry.compute_key.return_value = "test-key"
        registry.lookup.return_value = None
        registry.run_with_lock.return_value = mock.MagicMock(__enter__=mock.MagicMock(), __exit__=mock.MagicMock())

        transport = mock.MagicMock()
        transport.warm.side_effect = TransportError("master create failed")

        with mock.patch("codeagent.cli._get_transport", return_value=transport):
            result = _execute(request, target, registry)

        # Should return transport error result
        assert result.returncode == 1
        assert "transport error" in result.stderr
        assert "master create failed" in result.stderr
        registry.mark_failed.assert_called_once()

    def test_execute_failure_tries_fallback(self, tmp_path):
        """When execute() raises TransportError and host has fallback, retries with fallback alias."""
        from codeagent.cli import _execute
        from codeagent.domain import RepoEntry, RunRequest, Target
        from codeagent.transport.base import TransportError

        host = HostSpec(
            name="primary",
            ssh_alias="primary",
            hostnames=("primary",),
            transport="ssh",
            fallback_ssh_alias="fallback",
        )
        repo = RepoEntry(host="primary", path="/work")
        target = Target(host=host, repo=repo)
        request = RunRequest(task="do stuff", workdir="/work", backend="opencode")

        fallback_result = RunResult(returncode=0, stdout="ok", stderr="", host="fallback")

        registry = mock.MagicMock()
        registry.compute_key.return_value = "test-key"
        registry.lookup.return_value = None
        registry.run_with_lock.return_value = mock.MagicMock(__enter__=mock.MagicMock(), __exit__=mock.MagicMock())

        transport = mock.MagicMock()
        # warm succeeds, execute raises, then fallback succeeds
        transport.warm.return_value = None
        transport.execute.side_effect = [TransportError("execute failed"), fallback_result]

        with mock.patch("codeagent.cli._get_transport", return_value=transport):
            result = _execute(request, target, registry)

        assert result.returncode == 0
        assert result.stdout == "ok"
        # warm called twice: primary + fallback
        assert transport.warm.call_count == 2
        # execute called twice: primary (fails) + fallback (succeeds)
        assert transport.execute.call_count == 2
        fallback_host = transport.execute.call_args_list[1][0][1]
        assert fallback_host.ssh_alias == "fallback"


# ── TestNoSubcommand ─────────────────────────────────────────────────────


class TestNoSubcommand:
    """main() with no subcommand returns 1 and prints help."""

    def test_no_command(self, capsys):
        from codeagent.cli import main

        rc = main([])
        assert rc == 1
        out = capsys.readouterr().out
        assert "route" in out or "Multi-host" in out
