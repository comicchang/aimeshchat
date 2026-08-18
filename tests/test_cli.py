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
        from codeagent import __version__
        from codeagent.cli import main

        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert __version__ in out

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
            rc = main(["route", "where", "NoSuchTopic"])
            assert rc == 1
            captured = capsys.readouterr()
            assert "NoSuchTopic" in captured.err


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
        assert request.backend == "omp"

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


# ── TestGetTransport ─────────────────────────────────────────────────────


class TestGetTransport:
    """_get_transport selects transports by host.transport field."""

    def test_default_ssh(self):
        from codeagent.cli import _get_transport

        host = HostSpec(name="h", ssh_alias="h", hostnames=("h",))
        with mock.patch("codeagent.cli._router") as router:
            sentinel = mock.MagicMock()
            router.get.return_value = sentinel
            t = _get_transport(host)
        assert t is sentinel
        router.get.assert_called_once_with(host, None)

    def test_relay_login_with_zsh(self, tmp_path):
        from codeagent.cli import _get_transport
        from codeagent.transport.relay import RelayTransport

        zsh = tmp_path / "fake.zsh"
        zsh.write_text("# fake relay script\n")
        host = HostSpec(name="h", ssh_alias="h", hostnames=("h",), transport="relay-login")
        rm = mock.MagicMock(relay_zsh=str(zsh))
        t = _get_transport(host, rm)
        assert isinstance(t, RelayTransport)

    def test_relay_login_missing_zsh(self):
        from codeagent.cli import _get_transport

        host = HostSpec(name="h", ssh_alias="h", hostnames=("h",), transport="relay-login")
        with pytest.raises(ValueError, match="relay_zsh"):
            _get_transport(host, None)


# ── TestExecuteLocal ─────────────────────────────────────────────────────


class TestExecuteLocal:
    """_execute local path uses LocalTransport and updates session state."""

    def _execute_local(self):
        from codeagent.cli import _execute
        from codeagent.domain import RunRequest, Target

        host = HostSpec(name="__local__", ssh_alias="__local__", hostnames=())
        repo = RepoEntry(host="__local__", path="/work")
        target = Target(host=host, repo=repo, is_local=True)
        request = RunRequest(task="t", workdir="/work", backend="opencode")

        registry = mock.MagicMock()
        registry.compute_key.return_value = "k"
        registry.lookup.return_value = None
        registry.run_with_lock.return_value = mock.MagicMock(
            __enter__=mock.MagicMock(), __exit__=mock.MagicMock())
        return _execute, request, target, registry

    def test_local_execute_marks_observed_and_upserts(self):
        from codeagent.domain import RunResult

        _execute, request, target, registry = self._execute_local()
        transport = mock.MagicMock()
        transport.execute.return_value = RunResult(
            returncode=0, stdout="ok", stderr="", session_id="sess-1", host="__local__")
        with mock.patch("codeagent.cli.LocalTransport", return_value=transport):
            result = _execute(request, target, registry)

        assert result.returncode == 0
        registry.mark_starting.assert_called_once()
        registry.mark_observed.assert_called_once_with("k", "sess-1")
        registry.upsert.assert_called_once()
        registry.mark_failed.assert_not_called()

    def test_local_execute_failure_marks_failed(self):
        from codeagent.domain import RunResult

        _execute, request, target, registry = self._execute_local()
        transport = mock.MagicMock()
        transport.execute.return_value = RunResult(returncode=2, stdout="", stderr="boom")
        with mock.patch("codeagent.cli.LocalTransport", return_value=transport):
            result = _execute(request, target, registry)

        assert result.returncode == 2
        registry.mark_failed.assert_called_once_with("k")
        registry.mark_observed.assert_not_called()

    def test_local_execute_resumes_active_session(self, capsys):
        from codeagent.domain import RunResult

        _execute, request, target, registry = self._execute_local()
        record = mock.MagicMock(status="active", session_id="sess-abcdef123456")
        registry.lookup.return_value = record
        transport = mock.MagicMock()
        transport.execute.return_value = RunResult(returncode=0, stdout="ok", stderr="")
        with mock.patch("codeagent.cli.LocalTransport", return_value=transport):
            result = _execute(request, target, registry)

        assert result.returncode == 0
        err = capsys.readouterr().err
        assert "resuming session" in err
        assert transport.execute.call_args.kwargs["session_id"] == "sess-abcdef123456"


# ── TestRunErrors ────────────────────────────────────────────────────────


class TestRunErrors:
    """run subcommand error paths."""

    def test_run_repo_map_missing_no_host(self, capsys):
        """Missing repo-map with no --host propagates FileNotFoundError to main()."""
        from codeagent.cli import main

        with (
            mock.patch("codeagent.cli.load_repo_map", side_effect=FileNotFoundError("no repo-map")),
            mock.patch("codeagent.cli.SessionRegistry"),
        ):
            rc = main(["run", "do something"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "no repo-map" in err

    def test_run_repo_map_missing_with_host(self, tmp_path):
        """Missing repo-map with --host falls back to an empty ad-hoc repo-map."""
        from codeagent.cli import main

        result = _make_run_result()
        with (
            mock.patch("codeagent.cli.load_repo_map", side_effect=FileNotFoundError("no repo-map")),
            mock.patch("codeagent.cli._execute", return_value=result) as mock_exec,
            mock.patch("codeagent.cli.SessionRegistry"),
        ):
            rc = main(["run", "do something", "--host", "adhoc-host"])
        assert rc == 0
        request = mock_exec.call_args.args[0]
        assert request.host == "adhoc-host"

    def test_run_prints_stderr(self, capsys, tmp_path):
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        result = _make_run_result(stdout="", stderr="something went wrong")
        with (
            mock.patch("codeagent.cli.load_repo_map", return_value=rm),
            mock.patch("codeagent.cli._execute", return_value=result),
            mock.patch("codeagent.cli.SessionRegistry"),
        ):
            rc = main(["run", "do something"])
        assert rc == 0
        err = capsys.readouterr().err
        assert "something went wrong" in err


# ── TestRunOutput ────────────────────────────────────────────────────────


class TestRunOutput:
    """run/route --output writes structured JSON to a file."""

    def test_run_output_json(self, tmp_path):
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        out_file = tmp_path / "out.json"
        result = _make_run_result(stdout="", stderr="", session_id="sess-123")
        with (
            mock.patch("codeagent.cli.load_repo_map", return_value=rm),
            mock.patch("codeagent.cli._execute", return_value=result),
            mock.patch("codeagent.cli.SessionRegistry"),
        ):
            rc = main(["run", "do something", "--output", str(out_file)])
        assert rc == 0
        data = json.loads(out_file.read_text())
        assert data["session_id"] == "sess-123"
        assert data["exit_code"] == 0

    def test_route_output_json(self, tmp_path):
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        out_file = tmp_path / "route-out.json"
        result = _make_run_result(stdout="", stderr="", session_id="sess-456")
        with (
            mock.patch("codeagent.cli.load_repo_map", return_value=rm),
            mock.patch("codeagent.cli._execute", return_value=result),
            mock.patch("codeagent.cli.SessionRegistry"),
        ):
            rc = main(["route", "TestTopic", "task", "--raw", "--output", str(out_file)])
        assert rc == 0
        data = json.loads(out_file.read_text())
        assert data["session_id"] == "sess-456"

    def test_route_prints_stderr(self, capsys, tmp_path):
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        result = _make_run_result(stdout="", stderr="route-level warning")
        with (
            mock.patch("codeagent.cli.load_repo_map", return_value=rm),
            mock.patch("codeagent.cli._execute", return_value=result),
            mock.patch("codeagent.cli.SessionRegistry"),
        ):
            rc = main(["route", "TestTopic", "task", "--raw"])
        assert rc == 0
        err = capsys.readouterr().err
        assert "route-level warning" in err


# ── TestRouteErrors ──────────────────────────────────────────────────────


class TestRouteErrors:
    """route subcommand error paths."""

    def test_where_missing_topic_arg(self, capsys, tmp_path):
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        with mock.patch("codeagent.cli.load_repo_map", return_value=rm):
            rc = main(["route", "where"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "where <topic>" in err

    def test_topic_not_found(self, capsys, tmp_path):
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        with mock.patch("codeagent.cli.load_repo_map", return_value=rm):
            rc = main(["route", "NoSuchTopic", "do the thing"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "topic not found" in err

    def test_where_json(self, capsys, tmp_path):
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        with mock.patch("codeagent.cli.load_repo_map", return_value=rm):
            rc = main(["route", "where", "TestTopic", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["name"] == "TestTopic"
        assert len(data["repos"]) == 2
        assert data["repos"][0]["local"] is False

    def test_dry_run_json(self, capsys, tmp_path):
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        with mock.patch("codeagent.cli.load_repo_map", return_value=rm):
            rc = main(["route", "TestTopic", "task", "--dry-run", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["dry_run"] is True
        assert data["topic"] == "TestTopic"


# ── TestSessionsEdge ─────────────────────────────────────────────────────


class TestSessionsEdge:
    """sessions subcommand edge paths."""

    def test_list_with_records(self, capsys):
        from codeagent.cli import main

        registry = mock.MagicMock()
        registry.list_all.return_value = [
            mock.MagicMock(key="some-key", session_id="sess-abc", status="active",
                           host="devhost", workdir="/work")
        ]
        with mock.patch("codeagent.cli.SessionRegistry", return_value=registry):
            rc = main(["sessions", "list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "some-key" in out

    def test_show_found(self, capsys):
        import types

        from codeagent.cli import main

        registry = mock.MagicMock()
        record = types.SimpleNamespace(key="k", session_id="s", status="active", host="h", workdir="w")
        registry.lookup.return_value = record
        with mock.patch("codeagent.cli.SessionRegistry", return_value=registry):
            rc = main(["sessions", "show", "k"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["key"] == "k"

    def test_no_subcommand_returns_zero(self, capsys):
        from codeagent.cli import main

        with mock.patch("codeagent.cli.SessionRegistry", return_value=mock.MagicMock()):
            rc = main(["sessions"])
        assert rc == 0


class TestSSHEdge:
    """ssh subcommand edge paths."""

    def test_no_subcommand_returns_zero(self, capsys):
        from codeagent.cli import main

        with mock.patch("codeagent.cli.SSHTransport", return_value=mock.MagicMock()):
            rc = main(["ssh"])
        assert rc == 0


# ── TestMailbox ──────────────────────────────────────────────────────────


class TestMailbox:
    """mailbox subcommand local and remote dispatch."""

    def test_no_args_prints_help(self):
        from codeagent.cli import main

        with mock.patch("codeagent.mailbox.cli.main") as mailbox_main:
            rc = main(["mailbox"])
        assert rc == 0
        mailbox_main.assert_called_once_with(["--help"])

    def test_local_dispatch(self, tmp_path):
        """Local mailbox run: --mailbox-root is extracted and passed through."""
        from codeagent.cli import main

        with mock.patch("codeagent.mailbox.cli.main") as mailbox_main:
            rc = main(["mailbox", "stats", "--session", "s1", "--agent", "w1",
                       "--mailbox-root", str(tmp_path)])
        assert rc == 0
        args = mailbox_main.call_args.args[0]
        assert args == ["--mailbox-root", str(tmp_path),
                        "stats", "--session", "s1", "--agent", "w1"]

    def test_local_dispatch_system_exit_code(self, tmp_path):
        from codeagent.cli import main

        with mock.patch("codeagent.mailbox.cli.main", side_effect=SystemExit(5)):
            rc = main(["mailbox", "stats", "--session", "s1", "--agent", "w1",
                       "--mailbox-root", str(tmp_path)])
        assert rc == 5

    def test_remote_ad_hoc_host(self, capsys):
        """Remote host not in repo-map → ad-hoc HostSpec → router dispatches transport.mailbox()."""
        from codeagent.cli import main

        rm = _make_repo_map()
        transport = mock.MagicMock()
        transport.mailbox.return_value = (0, "stdout-line", "stderr-line")
        with (
            mock.patch("codeagent.cli.load_repo_map", return_value=rm),
            mock.patch("codeagent.domain.resolve_is_local", return_value=False),
            mock.patch("codeagent.cli._router.get", return_value=transport),
        ):
            rc = main(["mailbox", "--host", "remotehost",
                       "stats", "--session", "s1", "--agent", "w1"])
        assert rc == 0
        transport.mailbox.assert_called_once()
        call_args = transport.mailbox.call_args
        host_spec = call_args.args[0]
        assert host_spec.ssh_alias == "remotehost"
        assert "stats" in call_args.args[1]
        captured = capsys.readouterr()
        assert "stdout-line" in captured.out
        assert "stderr-line" in captured.err

    def test_local_dispatch_mailbox_root_double_dash(self, tmp_path):
        """--mailbox-root=... form is extracted from the REMAINDER args."""
        from codeagent.cli import main

        with mock.patch("codeagent.mailbox.cli.main") as mailbox_main:
            rc = main(["mailbox", "stats", "--session", "s1", "--agent", "w1",
                       f"--mailbox-root={tmp_path}"])
        assert rc == 0
        args = mailbox_main.call_args.args[0]
        assert args[0:2] == ["--mailbox-root", str(tmp_path)]

    def test_host_flag_inside_remainder(self, capsys):
        """--host inside the REMAINDER args is extracted (space form)."""
        from codeagent.cli import main

        rm = _make_repo_map()
        transport = mock.MagicMock()
        transport.mailbox.return_value = (0, "", "")
        with (
            mock.patch("codeagent.cli.load_repo_map", return_value=rm),
            mock.patch("codeagent.domain.resolve_is_local", return_value=False),
            mock.patch("codeagent.cli._router.get", return_value=transport),
        ):
            rc = main(["mailbox", "stats", "--session", "s1", "--agent", "w1",
                       "--host", "remotehost"])
        assert rc == 0

    def test_host_flag_inside_remainder_equals(self, capsys):
        """--host=... inside the REMAINDER args is extracted (equals form)."""
        from codeagent.cli import main

        rm = _make_repo_map()
        transport = mock.MagicMock()
        transport.mailbox.return_value = (0, "", "")
        with (
            mock.patch("codeagent.cli.load_repo_map", return_value=rm),
            mock.patch("codeagent.domain.resolve_is_local", return_value=False),
            mock.patch("codeagent.cli._router.get", return_value=transport),
        ):
            rc = main(["mailbox", "stats", "--host=remotehost",
                       "--session", "s1", "--agent", "w1"])
        assert rc == 0

    def test_remote_repo_map_load_failure(self, capsys):
        """Remote path with repo-map FileNotFoundError → ad-hoc host fallback."""
        from codeagent.cli import main

        transport = mock.MagicMock()
        transport.mailbox.return_value = (0, "ok-out", "")
        with (
            mock.patch("codeagent.config.repo_map.load_repo_map",
                       side_effect=FileNotFoundError("no repo-map")),
            mock.patch("codeagent.domain.resolve_is_local", return_value=False),
            mock.patch("codeagent.cli._router.get", return_value=transport),
        ):
            rc = main(["mailbox", "--host", "adhoc",
                       "stats", "--session", "s1", "--agent", "w1"])
        assert rc == 0

    def test_remote_host_is_local_direct_call(self, tmp_path):
        """Host that resolves local → mailbox CLI called in-process."""
        from codeagent.cli import main

        rm = _make_repo_map()
        with (
            mock.patch("codeagent.cli.load_repo_map", return_value=rm),
            mock.patch("codeagent.domain.resolve_is_local", return_value=True),
            mock.patch("codeagent.mailbox.cli.main") as mailbox_main,
        ):
            rc = main(["mailbox", "--host", "devhost", "--mailbox-root", str(tmp_path),
                       "stats", "--session", "s1", "--agent", "w1"])
        assert rc == 0
        mailbox_main.assert_called_once()
        args = mailbox_main.call_args.args[0]
        assert args[0:2] == ["--mailbox-root", str(tmp_path)]
        assert "stats" in args


# ── TestArtifactCLIPull ──────────────────────────────────────────────────


class TestArtifactCLIPull:
    """artifact pull subcommand."""

    def test_pull_success(self, capsys, tmp_path):
        from codeagent.cli import main

        dest = tmp_path / "out.bin"
        with mock.patch("codeagent.cli.pull_artifact", return_value=dest):
            rc = main([
                "artifact", "pull",
                "--host", "devhost",
                "--artifact-id", "art-1",
                "--relative-path", "out/report.json",
                "--size", "5",
                "--sha256", "a" * 64,
                "--dest", str(dest),
            ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "pulled art-1" in out

    def test_pull_error(self, capsys, tmp_path):
        from codeagent.cli import main
        from codeagent.transport.base import TransportError

        dest = tmp_path / "out.bin"
        with mock.patch("codeagent.cli.pull_artifact", side_effect=TransportError("ssh failed")):
            rc = main([
                "artifact", "pull",
                "--host", "devhost",
                "--artifact-id", "art-1",
                "--relative-path", "out/report.json",
                "--size", "5",
                "--sha256", "a" * 64,
                "--dest", str(dest),
            ])
        assert rc == 1
        err = capsys.readouterr().err
        assert "ssh failed" in err

    def test_unknown_subcommand(self):
        from codeagent.cli import main

        rc = main(["artifact"])
        assert rc == 1


# ── TestMainErrorHandlers ────────────────────────────────────────────────


class TestMainErrorHandlers:
    """main() top-level error handlers."""

    def test_keyboard_interrupt(self, capsys):
        from codeagent.cli import main

        with mock.patch("codeagent.cli._cmd_run", side_effect=KeyboardInterrupt):
            rc = main(["run", "task"])
        assert rc == 130
        err = capsys.readouterr().err
        assert "interrupted" in err

    def test_generic_exception(self, capsys):
        from codeagent.cli import main

        with mock.patch("codeagent.cli._cmd_run", side_effect=RuntimeError("boom")):
            rc = main(["run", "task"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "RuntimeError: boom" in err

    def test_file_not_found(self, capsys):
        from codeagent.cli import main

        with mock.patch("codeagent.cli._cmd_run", side_effect=FileNotFoundError("missing file")):
            rc = main(["run", "task"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "missing file" in err


# ── TestMainEntrypoint ───────────────────────────────────────────────────


class TestMainEntrypoint:
    """python -m codeagent.cli entrypoint."""

    def test_main_guard_subprocess(self):
        """Running the module directly exits with --help."""
        import subprocess
        import sys
        import os

        env = dict(os.environ)
        # src/ 布局：模块需从仓库 src 导入
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent / "src")
        proc = subprocess.run(
            [sys.executable, "-m", "codeagent.cli", "--help"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
            env=env,
        )
        assert proc.returncode == 0
        assert "route" in proc.stdout

    def test_main_guard(self):
        """The ``if __name__ == "__main__"`` guard executes main()."""
        import runpy
        import sys

        old_argv = sys.argv
        sys.argv = ["codeagent.cli"]
        try:
            with pytest.raises(SystemExit) as exc:
                runpy.run_module("codeagent.cli", run_name="__main__")
            assert exc.value.code == 1
        finally:
            sys.argv = old_argv


class TestPositiveInt:
    """_positive_int rejects non-positive values."""

    def test_positive_value(self):
        from codeagent.cli import _positive_int
        assert _positive_int("600") == 600
        assert _positive_int("1") == 1

    def test_zero_rejected(self):
        import argparse
        from codeagent.cli import _positive_int
        with pytest.raises(argparse.ArgumentTypeError, match="must be >0"):
            _positive_int("0")

    def test_negative_rejected(self):
        import argparse
        from codeagent.cli import _positive_int
        with pytest.raises(argparse.ArgumentTypeError, match="must be >0"):
            _positive_int("-5")


class TestRemoteTimeoutClamp:
    """Remote targets clamp --timeout below SSH_IDLE_WINDOW."""

    def test_clamp_remote_small_timeout(self):
        """--timeout 30 for remote target is clamped to SSH_IDLE_WINDOW (180)."""
        from unittest.mock import patch, MagicMock
        from codeagent.cli import _run_sync_result
        from codeagent.constants import SSH_IDLE_WINDOW

        args = MagicMock()
        args.task = "test"
        args.workdir = ""
        args.host = "yellow"
        args.backend = "omp"
        args.agent = None
        args.model = "test/model"
        args.skills = None
        args.skip_permissions = False
        args.session_key = None
        args.new_session = False
        args.no_auto_resume = False
        args.timeout = 30  # below SSH_IDLE_WINDOW
        args.output = None

        with patch("codeagent.cli.load_repo_map") as mock_rm, \
             patch("codeagent.cli.resolve_target") as mock_rt, \
             patch("codeagent.cli._execute") as mock_exec:
            # Target is remote
            target = MagicMock()
            target.is_local = False
            target.host.name = "yellow"
            mock_rt.return_value = target
            mock_rm.return_value = MagicMock()
            mock_exec.return_value = MagicMock(returncode=0, session_id=None, backend="", host="", workdir="", stdout="", stderr="")

            _run_sync_result(args, "test task")
            # The request passed to _execute should have timeout clamped
            call_args = mock_exec.call_args
            request = call_args[0][0]
            assert request.timeout == SSH_IDLE_WINDOW, (
                f"Expected timeout clamped to {SSH_IDLE_WINDOW}, got {request.timeout}"
            )

    def test_no_clamp_local(self):
        """Local targets do NOT clamp --timeout."""
        from unittest.mock import patch, MagicMock
        from codeagent.cli import _run_sync_result

        args = MagicMock()
        args.task = "test"
        args.workdir = ""
        args.host = None
        args.backend = "omp"
        args.agent = None
        args.model = "test/model"
        args.skills = None
        args.skip_permissions = False
        args.session_key = None
        args.new_session = False
        args.no_auto_resume = False
        args.timeout = 30
        args.output = None

        with patch("codeagent.cli.load_repo_map") as mock_rm, \
             patch("codeagent.cli.resolve_target") as mock_rt, \
             patch("codeagent.cli._execute") as mock_exec:
            target = MagicMock()
            target.is_local = True
            mock_rt.return_value = target
            mock_rm.return_value = MagicMock()
            mock_exec.return_value = MagicMock(returncode=0, session_id=None, backend="", host="", workdir="", stdout="", stderr="")

            _run_sync_result(args, "test task")
            call_args = mock_exec.call_args
            request = call_args[0][0]
            assert request.timeout == 30, f"Local timeout should stay 30, got {request.timeout}"


class TestIsOracleAgent:
    """_is_oracle_agent unifies name + profile determination."""

    def test_oracle_name(self):
        from codeagent.cli import _is_oracle_agent
        assert _is_oracle_agent("oracle") is True
        assert _is_oracle_agent("oracle-lite") is True

    def test_none_returns_false(self):
        from codeagent.cli import _is_oracle_agent
        assert _is_oracle_agent(None) is False
        assert _is_oracle_agent("") is False


# ── TestRunBgChild ───────────────────────────────────────────────────────


class TestRunBgChild:
    """_run_bg_child runs sync and persists the full RunResult via mark_done."""

    @staticmethod
    def _make_args(**kw):
        """Run-subcommand args with the fields _run_sync_result reads."""
        from unittest.mock import MagicMock

        args = MagicMock()
        args.task = "test"
        args.workdir = ""
        args.host = None
        args.backend = "omp"
        args.agent = None
        args.model = "test/model"
        args.skills = None
        args.skip_permissions = False
        args.session_key = None
        args.new_session = False
        args.no_auto_resume = False
        args.timeout = 30
        args.output = None
        for name, value in kw.items():
            setattr(args, name, value)
        return args

    def test_mark_done_receives_full_result(self):
        """Successful bg child persists the complete RunResult, not just exit code."""
        from unittest.mock import patch, MagicMock
        from codeagent.cli import _run_bg_child

        args = self._make_args()
        result = _make_run_result(
            returncode=0,
            stdout="out-1",
            stderr="err-1",
            session_id="sess-bg-1",
            backend="omp",
        )

        with patch("codeagent.cli.load_repo_map") as mock_rm, \
             patch("codeagent.cli.resolve_target") as mock_rt, \
             patch("codeagent.cli._execute", return_value=result), \
             patch("codeagent.job.get_manager") as mock_get_mgr:
            target = MagicMock()
            target.is_local = True
            mock_rt.return_value = target
            mock_rm.return_value = MagicMock()
            mgr = mock_get_mgr.return_value

            rc = _run_bg_child(args, "do something", "job-123")

        assert rc == result.returncode
        mgr.mark_done.assert_called_once_with("job-123", result)
        persisted = mgr.mark_done.call_args.args[1]
        # The exact result object is persisted — stdout/stderr/session_id/backend all kept.
        assert persisted is result
        assert persisted.stdout == "out-1"
        assert persisted.stderr == "err-1"
        assert persisted.session_id == "sess-bg-1"
        assert persisted.backend == "omp"

    def test_returns_result_returncode(self):
        """bg child returns the result's returncode (non-zero included)."""
        from unittest.mock import patch, MagicMock
        from codeagent.cli import _run_bg_child

        args = self._make_args()
        result = _make_run_result(returncode=7, stdout="boom out", stderr="boom err")

        with patch("codeagent.cli.load_repo_map") as mock_rm, \
             patch("codeagent.cli.resolve_target") as mock_rt, \
             patch("codeagent.cli._execute", return_value=result), \
             patch("codeagent.job.get_manager") as mock_get_mgr:
            target = MagicMock()
            target.is_local = True
            mock_rt.return_value = target
            mock_rm.return_value = MagicMock()

            rc = _run_bg_child(args, "do something", "job-456")

        assert rc == 7

    def test_mark_done_exception_does_not_crash(self):
        """mark_done failure is logged, not raised — returncode still returned."""
        from unittest.mock import patch, MagicMock
        from codeagent.cli import _run_bg_child

        args = self._make_args()
        result = _make_run_result(returncode=3, stdout="partial", stderr="")

        with patch("codeagent.cli.load_repo_map") as mock_rm, \
             patch("codeagent.cli.resolve_target") as mock_rt, \
             patch("codeagent.cli._execute", return_value=result), \
             patch("codeagent.job.get_manager") as mock_get_mgr:
            target = MagicMock()
            target.is_local = True
            mock_rt.return_value = target
            mock_rm.return_value = MagicMock()
            mock_get_mgr.return_value.mark_done.side_effect = RuntimeError("disk full")

            rc = _run_bg_child(args, "do something", "job-789")

        assert rc == 3
        mock_get_mgr.return_value.mark_done.assert_called_once()

    def test_execution_exception_persists_error_result(self):
        """If _run_sync_result throws, mark_done still called with error RunResult."""
        from unittest.mock import patch, MagicMock
        from codeagent.cli import _run_bg_child

        args = self._make_args()

        with patch("codeagent.cli._run_sync_result", side_effect=FileNotFoundError("no repo map")), \
             patch("codeagent.job.get_manager") as mock_get_mgr:
            mgr = mock_get_mgr.return_value

            rc = _run_bg_child(args, "do something", "job-err")

        assert rc == 1
        mgr.mark_done.assert_called_once()
        persisted = mgr.mark_done.call_args.args[1]
        assert persisted.returncode == 1
        assert "no repo map" in persisted.stderr


# ── TestRouteBgChild ─────────────────────────────────────────────────────


class TestRouteBgChild:
    """_cmd_route --_bg-job-id persists the full RunResult and returns its code."""

    @staticmethod
    def _make_args(**kw):
        """Route-subcommand args with _bg_job_id set (background child mode)."""
        from unittest.mock import MagicMock

        args = MagicMock()
        args.args = ["TestTopic", "inspect login flow"]
        args.repo = None
        args.backend = "omp"
        args.agent = None
        args.model = None
        args.skills = None
        args.session_key = None
        args.new_session = False
        args.no_auto_resume = False
        args.raw = False
        args.skip_permissions = False
        args.timeout = None
        args.dry_run = False
        args.background = False
        args.json_output = False
        args._bg_job_id = "job-route-1"
        for name, value in kw.items():
            setattr(args, name, value)
        return args

    def test_bg_child_persists_full_result(self, tmp_path):
        """Route bg child calls mark_done with the full result, not a bare exit code."""
        from unittest.mock import patch, MagicMock
        from codeagent.cli import _cmd_route

        rm = _make_repo_map(tmp_path)
        result = _make_run_result(
            returncode=0,
            stdout="route out",
            stderr="route err",
            session_id="sess-route-bg",
            backend="omp",
        )
        args = self._make_args()

        with patch("codeagent.cli.load_repo_map", return_value=rm), \
             patch("codeagent.cli.resolve_target") as mock_rt, \
             patch("codeagent.cli._execute", return_value=result), \
             patch("codeagent.cli.SessionRegistry"), \
             patch("codeagent.job.get_manager") as mock_get_mgr:
            target = MagicMock()
            target.is_local = True
            target.host.name = "devhost"
            mock_rt.return_value = target
            mgr = mock_get_mgr.return_value

            rc = _cmd_route(args)

        assert rc == result.returncode
        mgr.mark_done.assert_called_once_with("job-route-1", result)
        persisted = mgr.mark_done.call_args.args[1]
        assert persisted is result
        assert persisted.stdout == "route out"
        assert persisted.stderr == "route err"
        assert persisted.session_id == "sess-route-bg"
        assert persisted.backend == "omp"

    def test_bg_child_returns_result_returncode(self, tmp_path):
        """Route bg child returns result.returncode (non-zero included)."""
        from unittest.mock import patch, MagicMock
        from codeagent.cli import _cmd_route

        rm = _make_repo_map(tmp_path)
        result = _make_run_result(returncode=7, stdout="route boom", stderr="route err")
        args = self._make_args(_bg_job_id="job-route-7")

        with patch("codeagent.cli.load_repo_map", return_value=rm), \
             patch("codeagent.cli.resolve_target") as mock_rt, \
             patch("codeagent.cli._execute", return_value=result), \
             patch("codeagent.cli.SessionRegistry"), \
             patch("codeagent.job.get_manager") as mock_get_mgr:
            target = MagicMock()
            target.is_local = True
            target.host.name = "devhost"
            mock_rt.return_value = target

            rc = _cmd_route(args)

        assert rc == 7


# ── TestCliUncoveredPaths ───────────────────────────────────────────────


class TestCliUncoveredPaths:
    """Coverage gap closure for src/codeagent/cli.py.

    Targets branches the main suite never reached: _cmd_swarm dispatch for
    whoami/create-channel, swarm correlation/ack/status/watch/outbox/launch
    error paths, _get_swarm_kernel, oracle pre-spawn bootstrap, tmux and
    background run modes, park/gateway/events/runtime/oracle/job/session
    dispatch, and _cmd_route oracle/remote timeout handling.  External
    dependencies (kernel, registry, gateway, subprocess) are mocked.
    """

    # ── _is_oracle_agent profile semantics ──────────────────────────────

    def test_is_oracle_agent_profile_park_semantics(self):
        from codeagent.cli import _is_oracle_agent

        profile = mock.MagicMock(park=True, auto_exit=False)
        with mock.patch("codeagent.runners.omp.resolve_agent_profile", return_value=profile):
            assert _is_oracle_agent("deepseek-v3") is True

        profile.park = False
        with mock.patch("codeagent.runners.omp.resolve_agent_profile", return_value=profile):
            assert _is_oracle_agent("deepseek-v3") is False

        profile.park = True
        profile.auto_exit = True
        with mock.patch("codeagent.runners.omp.resolve_agent_profile", return_value=profile):
            assert _is_oracle_agent("deepseek-v3") is False

    def test_is_oracle_agent_profile_exception_falls_back(self):
        from codeagent.cli import _is_oracle_agent

        with mock.patch(
            "codeagent.runners.omp.resolve_agent_profile",
            side_effect=RuntimeError("no profile"),
        ):
            assert _is_oracle_agent("deepseek-v3") is False

    # ── _get_swarm_kernel ────────────────────────────────────────────────

    def test_get_swarm_kernel_creates_engine_sink(self, tmp_path):
        from codeagent.cli import _get_swarm_kernel

        engine = mock.MagicMock()
        engine.flush.return_value = 0
        sink = mock.MagicMock()
        kernel = mock.MagicMock()
        store = mock.MagicMock()
        with (
            mock.patch("codeagent.swarm.delivery.DeliveryEngine", return_value=engine),
            mock.patch("codeagent.swarm.delivery.EngineDeliverySink", return_value=sink),
            mock.patch("codeagent.cli.SwarmKernel", return_value=kernel),
            mock.patch("codeagent.cli.MailboxStore", return_value=store),
        ):
            k, s = _get_swarm_kernel(store_root=tmp_path)
        assert k is kernel
        assert s is store
        engine.flush.assert_called_once_with()
        sink.set_kernel.assert_called_once_with(kernel)

    def test_get_swarm_kernel_flush_logs(self, tmp_path):
        from codeagent.cli import _get_swarm_kernel

        engine = mock.MagicMock()
        engine.flush.return_value = 3  # truthy → info log path
        with (
            mock.patch("codeagent.swarm.delivery.DeliveryEngine", return_value=engine),
            mock.patch("codeagent.swarm.delivery.EngineDeliverySink"),
            mock.patch("codeagent.cli.SwarmKernel"),
            mock.patch("codeagent.cli.MailboxStore"),
        ):
            _get_swarm_kernel(store_root=tmp_path)

    def test_get_swarm_kernel_flush_exception_tolerated(self, tmp_path):
        from codeagent.cli import _get_swarm_kernel

        engine = mock.MagicMock()
        engine.flush.side_effect = RuntimeError("flush boom")
        with (
            mock.patch("codeagent.swarm.delivery.DeliveryEngine", return_value=engine),
            mock.patch("codeagent.swarm.delivery.EngineDeliverySink"),
            mock.patch("codeagent.cli.SwarmKernel"),
            mock.patch("codeagent.cli.MailboxStore"),
        ):
            k, s = _get_swarm_kernel(store_root=tmp_path)  # must not raise
        assert k is not None

    # ── _cmd_swarm dispatch ──────────────────────────────────────────────

    def test_swarm_whoami_dispatch(self, capsys):
        from codeagent.cli import main

        kernel = mock.MagicMock()
        loc = mock.MagicMock(host_alias="remote-1", backend="omp")
        kernel.get_location.return_value = loc
        kernel.get_agent_cards.return_value = {"w1": {"display_name": "Worker 1"}}
        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
        ):
            rc = main(["swarm", "whoami", "s1", "--agent", "w1"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["host_alias"] == "remote-1"
        assert data["backend"] == "omp"
        assert data["agent_card"]["display_name"] == "Worker 1"
        assert "mailbox" in data["transport_capabilities"]

    def test_swarm_create_channel_dispatch(self, capsys):
        from codeagent.cli import main

        kernel = mock.MagicMock()
        kernel.create_channel.return_value = mock.MagicMock(
            channel_id="chan-1", members=["a", "b"]
        )
        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
        ):
            rc = main(["swarm", "create-channel", "s1", "chan-1", "--members", "a,b"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["channel_id"] == "chan-1"
        assert data["members"] == ["a", "b"]
        kernel.create_channel.assert_called_once_with("s1", "chan-1", ["a", "b"])

    def test_swarm_unknown_command(self, capsys):
        from types import SimpleNamespace

        from codeagent.cli import _cmd_swarm

        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(mock.MagicMock(), mock.MagicMock())
        ):
            rc = _cmd_swarm(SimpleNamespace(swarm_cmd="bogus"))
        assert rc == 1
        assert "unknown swarm command: bogus" in capsys.readouterr().err

    def test_swarm_create_session_invalid_allowed_senders(self, capsys):
        from codeagent.cli import main

        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(mock.MagicMock(), mock.MagicMock())
        ):
            rc = main([
                "swarm", "create-session", "s1",
                "--manager", "mgr", "--members", "w1,w2",
                "--policy", "restricted", "--allowed-senders", "outsider",
            ])
        assert rc == 1
        assert "非 roster" in capsys.readouterr().err

    # ── _swarm_register extended output / card errors ────────────────────

    def test_swarm_register_extended_output(self, capsys):
        from codeagent.cli import main

        kernel = mock.MagicMock()
        reg = mock.MagicMock(agent_id="w1", session_id="s1")
        reg.location.host_alias = "remote-1"
        reg.location.backend = "omp"
        kernel.register.return_value = reg
        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
        ):
            rc = main([
                "swarm", "register", "s1",
                "--agent", "w1", "--host", "remote-1",
                "--execution-mode", "mailbox-worker",
                "--return-mode", "manager-pull",
                "--mailbox-root", "/mr",
            ])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["execution_mode"] == "mailbox-worker"
        assert data["return_mode"] == "manager-pull"
        assert data["mailbox_root"] == "/mr"

    def test_swarm_register_invalid_card_json(self, capsys):
        from codeagent.cli import main

        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(mock.MagicMock(), mock.MagicMock())
        ):
            rc = main([
                "swarm", "register", "s1",
                "--agent", "w1", "--host", "h", "--card", "{not json",
            ])
        assert rc == 1
        assert "invalid --card JSON" in capsys.readouterr().err

    def test_swarm_register_valid_card_sets_agent_card(self, capsys):
        from codeagent.cli import main

        kernel = mock.MagicMock()
        reg = mock.MagicMock(agent_id="w1", session_id="s1")
        reg.location.host_alias = "h"
        reg.location.backend = "omp"
        kernel.register.return_value = reg
        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
        ):
            rc = main([
                "swarm", "register", "s1",
                "--agent", "w1", "--host", "h",
                "--card", '{"display_name": "Worker One"}',
            ])
        assert rc == 0
        kernel.set_agent_card.assert_called_once_with(
            "s1", "w1", {"display_name": "Worker One"}
        )

    # ── _require_kind_correlation REPORT paths ───────────────────────────

    def test_require_kind_correlation_report_errors(self, capsys):
        from codeagent.cli import _require_kind_correlation

        assert _require_kind_correlation("REPORT", "", "", "") == 1
        assert "--reply-to is required" in capsys.readouterr().err
        assert _require_kind_correlation("REPORT", "", "rq", "rt") == 1
        assert "--run-id is required" in capsys.readouterr().err
        assert _require_kind_correlation("REPORT", "rn", "", "rt") == 1
        assert "--request-id is required" in capsys.readouterr().err
        assert _require_kind_correlation("REPORT", "rn", "rq", "rt") == 0
        assert _require_kind_correlation("TASK", "rn", "rq", "") == 0
        assert _require_kind_correlation("NOTICE", "", "", "") == 0

    # ── channel/broadcast/notice correlation early-returns ───────────────

    def test_swarm_channel_correlation_error(self, capsys):
        from codeagent.cli import main

        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(mock.MagicMock(), mock.MagicMock())
        ):
            rc = main([
                "swarm", "channel", "s1", "chan",
                "--from", "a", "--kind", "TASK", "--subject", "x", "--body", "y",
            ])
        assert rc == 1
        assert "--run-id is required" in capsys.readouterr().err

    def test_swarm_broadcast_correlation_error(self, capsys):
        from codeagent.cli import main

        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(mock.MagicMock(), mock.MagicMock())
        ):
            rc = main([
                "swarm", "broadcast", "s1",
                "--from", "a", "--kind", "TASK", "--subject", "x", "--body", "y",
            ])
        assert rc == 1
        assert "--run-id is required" in capsys.readouterr().err

    def test_swarm_notice_correlation_error(self, capsys):
        from codeagent.cli import main

        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(mock.MagicMock(), mock.MagicMock())
        ):
            rc = main([
                "swarm", "notice", "s1",
                "--from", "a", "--topic", "t", "--subject", "s",
                "--body", "y", "--kind", "TASK",
            ])
        assert rc == 1
        assert "--run-id is required" in capsys.readouterr().err

    # ── _swarm_ack consumed error paths ──────────────────────────────────

    def test_swarm_ack_consumed_no_message(self, capsys):
        from codeagent.cli import main

        kernel = mock.MagicMock()
        svc = mock.MagicMock()
        svc.read.return_value = mock.MagicMock(message=None, status="no-such-msg")
        with (
            mock.patch(
                "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
            ),
            mock.patch("codeagent.mailbox.service.MailboxService", return_value=svc),
        ):
            rc = main([
                "swarm", "ack", "s1", "--agent", "w1",
                "--msg-id", "m1", "--phase", "consumed",
            ])
        assert rc == 1
        err = capsys.readouterr().err
        assert "no message to ack: m1" in err
        svc.read.assert_called_once_with("s1", "w1", owner="w1", msg_id="m1")

    def test_swarm_ack_consumed_msg_id_mismatch(self, capsys):
        from codeagent.cli import main

        kernel = mock.MagicMock()
        svc = mock.MagicMock()
        svc.read.return_value = mock.MagicMock(message={"msg_id": "actual-1"}, status="ok")
        with (
            mock.patch(
                "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
            ),
            mock.patch("codeagent.mailbox.service.MailboxService", return_value=svc),
        ):
            rc = main([
                "swarm", "ack", "s1", "--agent", "w1",
                "--msg-id", "m1", "--phase", "consumed",
            ])
        assert rc == 1
        err = capsys.readouterr().err
        assert "msg_id mismatch" in err
        assert "released back to inbox" in err
        kernel._store.release.assert_called_once_with("s1", "w1", "actual-1", owner="w1")

    # ── _swarm_status ────────────────────────────────────────────────────

    def test_swarm_status_session_not_found(self, capsys):
        from codeagent.cli import main

        kernel = mock.MagicMock()
        kernel.get_session.return_value = None
        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
        ):
            rc = main(["swarm", "status", "ghost"])
        assert rc == 1
        assert "session not found: ghost" in capsys.readouterr().err

    def test_swarm_status_trace_success(self, capsys):
        from codeagent.cli import main

        kernel = mock.MagicMock()
        kernel.get_session.return_value = mock.MagicMock()
        kernel.trace.return_value = {"trace": [{"msg_id": "m1"}]}
        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
        ):
            rc = main(["swarm", "status", "s1", "--trace", "tr-1"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["trace"][0]["msg_id"] == "m1"

    def test_swarm_status_trace_value_error(self, capsys):
        from codeagent.cli import main

        kernel = mock.MagicMock()
        kernel.get_session.return_value = mock.MagicMock()
        kernel.trace.side_effect = ValueError("no such trace")
        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
        ):
            rc = main(["swarm", "status", "s1", "--trace", "tr-1"])
        assert rc == 1
        assert "no such trace" in capsys.readouterr().err

    # ── _swarm_watch ─────────────────────────────────────────────────────

    def test_swarm_watch_flushes_and_polls_once(self, capsys):
        from codeagent.cli import main

        kernel = mock.MagicMock()
        engine = mock.MagicMock()
        engine.flush.return_value = 2
        kernel._sink = mock.MagicMock(_engine=engine)
        kernel.poll.return_value = mock.MagicMock(
            messages=[{"msg_id": "m1", "body": "hello"}],
            cursor="c1",
            has_more=False,
        )
        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
        ):
            rc = main(["swarm", "watch", "s1", "--agent", "w1", "--iterations", "1"])
        assert rc == 0
        out = capsys.readouterr().out
        assert '"msg_id": "m1"' in out
        engine.flush.assert_called_once_with(session_id="s1")
        kernel.poll.assert_called_once()

    def test_swarm_watch_flush_exception_tolerated(self, capsys):
        from codeagent.cli import main

        kernel = mock.MagicMock()
        engine = mock.MagicMock()
        engine.flush.side_effect = RuntimeError("flush failed")
        kernel._sink = mock.MagicMock(_engine=engine)
        kernel.poll.return_value = mock.MagicMock(messages=[], cursor="", has_more=False)
        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
        ):
            rc = main(["swarm", "watch", "s1", "--agent", "w1", "--iterations", "1"])
        assert rc == 0  # flush failure is logged, not fatal

    # ── _swarm_outbox ────────────────────────────────────────────────────

    def test_swarm_outbox_no_subcommand(self, capsys):
        from codeagent.cli import main

        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(mock.MagicMock(), mock.MagicMock())
        ):
            rc = main(["swarm", "outbox"])
        assert rc == 1
        assert "specify an outbox subcommand" in capsys.readouterr().err

    def test_swarm_outbox_no_engine_sink(self, capsys):
        from types import SimpleNamespace

        from codeagent.cli import main

        kernel = mock.MagicMock()
        kernel._sink = SimpleNamespace(_engine=None)
        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
        ):
            rc = main(["swarm", "outbox", "pending"])
        assert rc == 1
        assert "no DeliveryEngine sink" in capsys.readouterr().err

    def test_swarm_outbox_pending_json_and_text(self, capsys):
        from codeagent.cli import main

        kernel = mock.MagicMock()
        engine = mock.MagicMock()
        engine.pending.return_value = [{"msg_id": "m1", "to": "w1", "kind": "TASK"}]
        kernel._sink = mock.MagicMock(_engine=engine)
        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
        ):
            rc = main(["swarm", "outbox", "pending", "--json"])
            assert rc == 0
            data = json.loads(capsys.readouterr().out)
            assert data[0]["msg_id"] == "m1"

            rc = main(["swarm", "outbox", "pending"])
            assert rc == 0
            out = capsys.readouterr().out
            assert "m1" in out
            assert "w1" in out

    def test_swarm_outbox_flush(self, capsys):
        from codeagent.cli import main

        kernel = mock.MagicMock()
        engine = mock.MagicMock()
        engine.flush.return_value = 2
        kernel._sink = mock.MagicMock(_engine=engine)
        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
        ):
            rc = main(["swarm", "outbox", "flush"])
        assert rc == 0
        assert json.loads(capsys.readouterr().out) == {"flushed": 2}

        engine.flush.return_value = 0
        rc = main(["swarm", "outbox", "flush"])
        assert rc == 1

    def test_swarm_outbox_status(self, capsys):
        from codeagent.cli import main

        kernel = mock.MagicMock()
        engine = mock.MagicMock()
        engine.outbox_stats.return_value = {"pending": 5, "dead": 1}
        kernel._sink = mock.MagicMock(_engine=engine)
        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
        ):
            rc = main(["swarm", "outbox", "status"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["pending"] == 5

    def test_swarm_outbox_dead_empty_and_entries(self, capsys):
        from codeagent.cli import main

        kernel = mock.MagicMock()
        engine = mock.MagicMock()
        kernel._sink = mock.MagicMock(_engine=engine)
        engine.dead_letter_list.return_value = []
        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
        ):
            rc = main(["swarm", "outbox", "dead"])
            assert rc == 0
            assert "(no dead-lettered messages)" in capsys.readouterr().out

            engine.dead_letter_list.return_value = [
                {"msg_id": "dl-1", "to": "w1", "reason": "unroutable"}
            ]
            rc = main(["swarm", "outbox", "dead"])
            assert rc == 0
            out = capsys.readouterr().out
            assert "dl-1" in out
            assert "unroutable" in out

    def test_swarm_outbox_requeue(self, capsys):
        from codeagent.cli import main

        kernel = mock.MagicMock()
        engine = mock.MagicMock()
        engine.dead_letter_requeue.return_value = True
        kernel._sink = mock.MagicMock(_engine=engine)
        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
        ):
            rc = main(["swarm", "outbox", "requeue", "dl-1", "--session", "s1"])
        assert rc == 0
        assert json.loads(capsys.readouterr().out) == {"requeued": "dl-1"}
        engine.dead_letter_requeue.assert_called_once_with("s1", "dl-1")

        engine.dead_letter_requeue.return_value = False
        rc = main(["swarm", "outbox", "requeue", "dl-1", "--session", "s1"])
        assert rc == 1
        assert "dead-letter entry not found: dl-1" in capsys.readouterr().err

    def test_swarm_outbox_purge(self, capsys):
        from codeagent.cli import main

        kernel = mock.MagicMock()
        engine = mock.MagicMock()
        engine.dead_letter_purge.return_value = 3
        kernel._sink = mock.MagicMock(_engine=engine)
        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
        ):
            rc = main(["swarm", "outbox", "purge", "--session", "s1"])
        assert rc == 0
        assert json.loads(capsys.readouterr().out) == {"purged": 3}
        engine.dead_letter_purge.assert_called_once_with(session_id="s1")

    def test_swarm_outbox_unknown_command_direct(self, capsys):
        from types import SimpleNamespace

        from codeagent.cli import _swarm_outbox

        kernel = mock.MagicMock()
        engine = mock.MagicMock()
        kernel._sink = mock.MagicMock(_engine=engine)
        rc = _swarm_outbox(kernel, SimpleNamespace(outbox_cmd="bogus"))
        assert rc == 1
        assert "unknown outbox command: bogus" in capsys.readouterr().err

    # ── _swarm_launch bootstrap ──────────────────────────────────────────

    @staticmethod
    def _launch_session(roster):
        return mock.MagicMock(manager_id="mgr", roster=list(roster))

    @staticmethod
    def _loc(host_alias="remote-1", mailbox_root="", execution_mode=None, return_mode=None):
        return mock.MagicMock(
            host_alias=host_alias,
            mailbox_root=mailbox_root,
            execution_mode=execution_mode,
            return_mode=return_mode,
        )

    def test_swarm_launch_bootstrap_remote_with_mailbox_root(self, capsys):
        from codeagent.cli import main
        from codeagent.swarm.model import ExecutionMode

        kernel = mock.MagicMock()
        kernel.get_session.return_value = self._launch_session(["mgr", "w1"])
        loc = self._loc(host_alias="remote-1", mailbox_root="/mr",
                        execution_mode=ExecutionMode.MAILBOX_WORKER)
        kernel.get_location.side_effect = lambda sid, agent: loc if agent == "w1" else None
        fake_run = mock.MagicMock(returncode=0, stdout="init out\n", stderr="")
        with (
            mock.patch(
                "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
            ),
            mock.patch("codeagent.cli.subprocess.run", return_value=fake_run) as run_mock,
        ):
            rc = main(["swarm", "launch", "s1", "--bootstrap"])
        assert rc == 0
        out = capsys.readouterr().out
        assert '"status": "done"' in out
        assert "init out" in out
        assert run_mock.call_count == 2
        for call in run_mock.call_args_list:
            cmd = call.args[0]
            assert "--mailbox-root" in cmd

    def test_swarm_launch_bootstrap_skips_missing_location(self, capsys):
        from codeagent.cli import main

        kernel = mock.MagicMock()
        kernel.get_session.return_value = self._launch_session(["mgr", "w2"])
        kernel.get_location.side_effect = lambda sid, agent: None
        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
        ):
            rc = main(["swarm", "launch", "s1", "--bootstrap"])
        assert rc == 0
        assert '"remote_agents": []' in capsys.readouterr().out

    def test_swarm_launch_bootstrap_skips_non_worker_exec_mode(self, capsys):
        from codeagent.cli import main
        from codeagent.swarm.model import ExecutionMode

        kernel = mock.MagicMock()
        kernel.get_session.return_value = self._launch_session(["mgr", "w1"])
        loc = self._loc(host_alias="remote-1", execution_mode=ExecutionMode.LOCAL_OMP_MCP)
        kernel.get_location.side_effect = lambda sid, agent: loc if agent == "w1" else None
        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
        ):
            rc = main(["swarm", "launch", "s1", "--bootstrap"])
        assert rc == 0
        assert '"remote_agents": []' in capsys.readouterr().out

    def test_swarm_launch_bootstrap_session_init_failure(self, capsys):
        from codeagent.cli import main
        from codeagent.swarm.model import ExecutionMode

        kernel = mock.MagicMock()
        kernel.get_session.return_value = self._launch_session(["mgr", "w1"])
        loc = self._loc(host_alias="remote-1", execution_mode=ExecutionMode.MAILBOX_WORKER)
        kernel.get_location.side_effect = lambda sid, agent: loc if agent == "w1" else None
        bad = mock.MagicMock(returncode=1, stdout="", stderr="init boom")
        with (
            mock.patch(
                "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
            ),
            mock.patch("codeagent.cli.subprocess.run", return_value=bad),
        ):
            rc = main(["swarm", "launch", "s1", "--bootstrap"])
        assert rc == 1
        assert "session-init failed for w1@remote-1: init boom" in capsys.readouterr().err

    def test_swarm_launch_bootstrap_session_init_exception(self, capsys):
        import subprocess

        from codeagent.cli import main
        from codeagent.swarm.model import ExecutionMode

        kernel = mock.MagicMock()
        kernel.get_session.return_value = self._launch_session(["mgr", "w1"])
        loc = self._loc(host_alias="remote-1", execution_mode=ExecutionMode.MAILBOX_WORKER)
        kernel.get_location.side_effect = lambda sid, agent: loc if agent == "w1" else None
        with (
            mock.patch(
                "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
            ),
            mock.patch(
                "codeagent.cli.subprocess.run",
                side_effect=subprocess.TimeoutExpired("cmd", 30),
            ),
        ):
            rc = main(["swarm", "launch", "s1", "--bootstrap"])
        assert rc == 1
        assert "session-init exception" in capsys.readouterr().err

    def test_swarm_launch_bootstrap_send_init_failure(self, capsys):
        from codeagent.cli import main
        from codeagent.swarm.model import ExecutionMode

        kernel = mock.MagicMock()
        kernel.get_session.return_value = self._launch_session(["mgr", "w1"])
        loc = self._loc(host_alias="remote-1", execution_mode=ExecutionMode.MAILBOX_WORKER)
        kernel.get_location.side_effect = lambda sid, agent: loc if agent == "w1" else None
        ok = mock.MagicMock(returncode=0, stdout="ok\n", stderr="")
        bad = mock.MagicMock(returncode=1, stdout="", stderr="send boom")
        with (
            mock.patch(
                "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
            ),
            mock.patch("codeagent.cli.subprocess.run", side_effect=[ok, bad]),
        ):
            rc = main(["swarm", "launch", "s1", "--bootstrap"])
        assert rc == 1
        assert "send INIT failed for w1@remote-1: send boom" in capsys.readouterr().err

    def test_swarm_launch_bootstrap_send_init_exception(self, capsys):
        import subprocess

        from codeagent.cli import main
        from codeagent.swarm.model import ExecutionMode

        kernel = mock.MagicMock()
        kernel.get_session.return_value = self._launch_session(["mgr", "w1"])
        loc = self._loc(host_alias="remote-1", execution_mode=ExecutionMode.MAILBOX_WORKER)
        kernel.get_location.side_effect = lambda sid, agent: loc if agent == "w1" else None
        ok = mock.MagicMock(returncode=0, stdout="ok\n", stderr="")
        with (
            mock.patch(
                "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
            ),
            mock.patch(
                "codeagent.cli.subprocess.run",
                side_effect=[ok, subprocess.TimeoutExpired("cmd", 30)],
            ),
        ):
            rc = main(["swarm", "launch", "s1", "--bootstrap"])
        assert rc == 1
        assert "send INIT exception" in capsys.readouterr().err

    # ── _swarm_launch pull loop ──────────────────────────────────────────

    def test_swarm_launch_pull_skips_non_workers(self, capsys):
        from codeagent.cli import main
        from codeagent.swarm.model import ExecutionMode, ReturnMode

        kernel = mock.MagicMock()
        kernel.get_session.return_value = self._launch_session(["mgr", "w1", "w2"])
        loc_w1 = self._loc(host_alias="remote-1", execution_mode=ExecutionMode.LOCAL_OMP_MCP)
        loc_w2 = self._loc(host_alias="remote-2", execution_mode=ExecutionMode.MAILBOX_WORKER,
                           return_mode=ReturnMode.BIDIRECTIONAL)
        kernel.get_location.side_effect = lambda sid, agent: {
            "w1": loc_w1, "w2": loc_w2,
        }.get(agent)
        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
        ):
            rc = main(["swarm", "launch", "s1", "--pull"])
        assert rc == 0
        assert "no registered workers found" in capsys.readouterr().err

    def test_swarm_launch_pull_finalize_and_release(self, capsys):
        from codeagent.cli import main

        kernel = mock.MagicMock()
        kernel.get_session.return_value = self._launch_session(["mgr", "w1"])
        loc = self._loc(host_alias="remote-1", mailbox_root="/mr")
        kernel.get_location.side_effect = lambda sid, agent: loc if agent == "w1" else None
        kernel.pull_remote.return_value = [
            {"msg_id": "m1", "_pull_host": "remote-1", "_pull_mailbox_root": "/mr"},
            {"msg_id": "m2", "_pull_host": "remote-1", "_pull_mailbox_root": "/mr"},
        ]
        kernel.ingest.return_value = ["m1"]
        kernel._store = mock.MagicMock()
        kernel._store.read_history.return_value = [
            {"kind": "REPORT", "from": "w1", "body": "done"}
        ]
        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
        ):
            rc = main(["swarm", "launch", "s1", "--pull", "--max-iterations", "5"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "all_workers_reported" in out
        kernel.pull_remote.assert_called_once_with("s1", "remote-1")
        kernel.ingest.assert_called_once()
        kernel.finalize_remote.assert_called_once()
        kernel.release_remote.assert_called_once()

    def test_swarm_launch_pull_exception_then_max_iterations(self, capsys):
        from codeagent.cli import main

        kernel = mock.MagicMock()
        kernel.get_session.return_value = self._launch_session(["mgr", "w1"])
        loc = self._loc(host_alias="remote-1")
        kernel.get_location.side_effect = lambda sid, agent: loc if agent == "w1" else None
        kernel.ingest.side_effect = ValueError("bad message")
        kernel._store = mock.MagicMock()
        kernel._store.read_history.return_value = []
        with mock.patch(
            "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
        ):
            rc = main(["swarm", "launch", "s1", "--pull", "--max-iterations", "1"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "max_iterations" in out

    # ── _execute with run_context ────────────────────────────────────────

    def test_execute_with_run_context_logs(self):
        from codeagent.cli import _execute
        from codeagent.domain import RepoEntry, RunContext, RunRequest, RunResult, Target

        host = HostSpec(name="__local__", ssh_alias="__local__", hostnames=())
        target = Target(host=host, repo=RepoEntry(host="__local__", path="/work"), is_local=True)
        request = RunRequest(task="t", workdir="/work", backend="opencode")
        rc = RunContext(review_key="rk", generation=1, run_id="ri",
                        request_id="rq", swarm_session_id="sid", mailbox_root="/mr")
        registry = mock.MagicMock()
        registry.compute_key.return_value = "k"
        registry.lookup.return_value = None
        registry.run_with_lock.return_value = mock.MagicMock(
            __enter__=mock.MagicMock(), __exit__=mock.MagicMock()
        )
        transport = mock.MagicMock()
        transport.execute.return_value = RunResult(returncode=0, stdout="ok", stderr="")
        with mock.patch("codeagent.cli.LocalTransport", return_value=transport):
            result = _execute(request, target, registry, run_context=rc)
        assert result.returncode == 0
        registry.mark_active.assert_called_once_with("k")

    def test_execute_auto_park_acquire_for_oracle(self):
        from codeagent.cli import _execute
        from codeagent.domain import RepoEntry, RunRequest, RunResult, Target

        host = HostSpec(name="__local__", ssh_alias="__local__", hostnames=())
        target = Target(host=host, repo=RepoEntry(host="__local__", path="/work"), is_local=True)
        request = RunRequest(task="t", workdir="/work", backend="omp", agent="oracle")
        registry = mock.MagicMock()
        registry.compute_key.return_value = "k"
        registry.lookup.return_value = None
        registry.run_with_lock.return_value = mock.MagicMock(
            __enter__=mock.MagicMock(), __exit__=mock.MagicMock()
        )
        transport = mock.MagicMock()
        transport.execute.return_value = RunResult(
            returncode=0, stdout="ok", stderr="", session_id="sess-oracle-1"
        )
        with (
            mock.patch("codeagent.cli.LocalTransport", return_value=transport),
            mock.patch("codeagent.cli.subprocess.run") as run_mock,
        ):
            result = _execute(request, target, registry)
        assert result.returncode == 0
        run_mock.assert_called_once()
        cmd = run_mock.call_args.args[0]
        assert cmd[:3] == ["aimeshchat", "park", "acquire"]
        assert cmd[cmd.index("--backend-id") + 1] == "sess-oracle-1"
        registry.mark_observed.assert_called_once_with("k", "sess-oracle-1")

    def test_execute_auto_park_acquire_failure_tolerated(self):
        from codeagent.cli import _execute
        from codeagent.domain import RepoEntry, RunRequest, RunResult, Target

        host = HostSpec(name="__local__", ssh_alias="__local__", hostnames=())
        target = Target(host=host, repo=RepoEntry(host="__local__", path="/work"), is_local=True)
        request = RunRequest(task="t", workdir="/work", backend="omp", agent="oracle")
        registry = mock.MagicMock()
        registry.compute_key.return_value = "k"
        registry.lookup.return_value = None
        registry.run_with_lock.return_value = mock.MagicMock(
            __enter__=mock.MagicMock(), __exit__=mock.MagicMock()
        )
        transport = mock.MagicMock()
        transport.execute.return_value = RunResult(
            returncode=0, stdout="ok", stderr="", session_id="sess-oracle-2"
        )
        with (
            mock.patch("codeagent.cli.LocalTransport", return_value=transport),
            mock.patch(
                "codeagent.cli.subprocess.run", side_effect=RuntimeError("park acquire failed")
            ),
        ):
            result = _execute(request, target, registry)
        assert result.returncode == 0  # park acquire failure is non-fatal

    # ── _resolve_agent_backend ───────────────────────────────────────────

    def test_resolve_agent_backend_oracle_primary(self):
        from codeagent.cli import _resolve_agent_backend

        reg = mock.MagicMock()
        rt = mock.MagicMock()
        rt.name = "omp"
        reg.get.return_value = rt
        with mock.patch("codeagent.runtime.registry.RuntimeRegistry", return_value=reg):
            assert _resolve_agent_backend("oracle", "omp") == "omp"
        reg.get.assert_called_once()

    def test_resolve_agent_backend_oracle_fallback(self):
        from codeagent.cli import _resolve_agent_backend

        reg = mock.MagicMock()

        def _get(name, required_capabilities=None):
            if name == "opencode":
                rt = mock.MagicMock()
                rt.name = "opencode"
                return rt
            raise RuntimeError("no warm-resume runtime")

        reg.get.side_effect = _get
        with mock.patch("codeagent.runtime.registry.RuntimeRegistry", return_value=reg):
            assert _resolve_agent_backend("oracle", None) == "opencode"

    def test_resolve_agent_backend_oracle_all_fail(self):
        from codeagent.cli import _resolve_agent_backend

        reg = mock.MagicMock()
        reg.get.side_effect = RuntimeError("registry down")
        with mock.patch("codeagent.runtime.registry.RuntimeRegistry", return_value=reg):
            assert _resolve_agent_backend("oracle", "omp") == "omp"

    def test_resolve_agent_backend_non_oracle_passthrough(self):
        from codeagent.cli import _resolve_agent_backend

        assert _resolve_agent_backend("my-agent", "opencode") == "opencode"

    # ── _cmd_run dispatch ────────────────────────────────────────────────

    def test_run_bg_job_dispatch(self):
        from codeagent.cli import main

        with mock.patch("codeagent.cli._run_bg_child", return_value=42) as bg:
            rc = main(["run", "task", "--_bg-job-id", "job-1"])
        assert rc == 42
        bg.assert_called_once()

    def test_run_tmux_no_tmux_session(self, capsys):
        from codeagent.cli import main

        with mock.patch("codeagent.launchers.tmux.detect_current_tmux", return_value=None):
            rc = main(["run", "task", "--tmux"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "requires running inside a tmux session" in err

    def test_run_tmux_spawn(self, capsys):
        from codeagent.cli import main

        with (
            mock.patch("codeagent.launchers.tmux.detect_current_tmux", return_value="sess"),
            mock.patch(
                "codeagent.launchers.tmux.spawn_in_current_tmux", return_value="pane-1"
            ) as spawn,
        ):
            rc = main([
                "run", "task", "/wd", "--tmux",
                "--host", "h", "--backend", "omp", "--agent", "a",
                "--model", "m", "--skills", "s", "--session-key", "k",
                "--new-session", "--no-auto-resume", "--skip-permissions",
                "--output", "/tmp/o.json", "--timeout", "30",
            ])
        assert rc == 0
        err = capsys.readouterr().err
        assert "[tmux] agent spawned in pane pane-1" in err
        argv = spawn.call_args.args[0]
        assert argv[:4] == ["aimeshchat", "run", "task", "/wd"]
        assert argv[argv.index("--timeout") + 1] == "30"
        assert "--split" not in argv

    def test_run_tmux_spawn_error(self, capsys):
        from codeagent.cli import main

        with (
            mock.patch("codeagent.launchers.tmux.detect_current_tmux", return_value="sess"),
            mock.patch(
                "codeagent.launchers.tmux.spawn_in_current_tmux",
                side_effect=RuntimeError("no tmux server"),
            ),
        ):
            rc = main(["run", "task", "--tmux"])
        assert rc == 1
        assert "no tmux server" in capsys.readouterr().err

    def test_run_background_submits_job(self, capsys):
        import sys

        from codeagent.cli import main

        mgr = mock.MagicMock()
        mgr.create_placeholder.return_value = "job-1"
        proc = mock.MagicMock()
        proc.pid = 1234
        with (
            mock.patch("codeagent.job.get_manager", return_value=mgr),
            mock.patch("codeagent.cli.subprocess.Popen", return_value=proc) as popen,
        ):
            rc = main([
                "run", "task", "/wd", "--background",
                "--host", "h", "--backend", "omp", "--agent", "a",
                "--model", "m", "--skills", "s", "--session-key", "k",
                "--new-session", "--no-auto-resume", "--skip-permissions",
                "--output", "/tmp/o.json", "--timeout", "30",
            ])
        assert rc == 0
        err = capsys.readouterr().err
        assert "[background] job submitted: job-1  (pid=1234)" in err
        mgr.create_placeholder.assert_called_once_with(task="task", host="h", workdir="/wd")
        mgr.mark_running.assert_called_once_with("job-1", pid=1234)
        argv = popen.call_args.args[0]
        assert argv[:6] == [sys.executable, "-m", "codeagent.cli", "run", "task", "/wd"]
        assert argv[-2:] == ["--_bg-job-id", "job-1"]
        assert popen.call_args.kwargs["start_new_session"] is True

    # ── _route_in_background ─────────────────────────────────────────────

    def test_route_background_submits_job(self, capsys, tmp_path):
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        mgr = mock.MagicMock()
        mgr.create_placeholder.return_value = "job-route-1"
        proc = mock.MagicMock()
        proc.pid = 99
        with (
            mock.patch("codeagent.cli.load_repo_map", return_value=rm),
            mock.patch("codeagent.job.get_manager", return_value=mgr),
            mock.patch("codeagent.cli.subprocess.Popen", return_value=proc) as popen,
        ):
            rc = main([
                "route", "TestTopic", "inspect login", "--background",
                "--repo", "1", "--backend", "omp", "--agent", "a", "--model", "m",
                "--raw", "--json", "--new-session", "--no-auto-resume",
                "--skip-permissions", "--skills", "s", "--session-key", "k",
                "--output", "/tmp/o.json", "--timeout", "120",
            ])
        assert rc == 0
        err = capsys.readouterr().err
        assert "route job submitted: job-route-1  (pid=99)" in err
        assert "topic: TestTopic" in err
        mgr.create_placeholder.assert_called_once_with(
            task="inspect login", host="route:TestTopic", workdir=""
        )
        mgr.mark_running.assert_called_once_with("job-route-1", pid=99)
        argv = popen.call_args.args[0]
        assert argv[argv.index("--repo") + 1] == "1"
        assert argv[-2:] == ["--_bg-job-id", "job-route-1"]
        assert "--raw" in argv
        assert "--json" in argv
        assert argv[argv.index("--timeout") + 1] == "120"

    # ── _cmd_route oracle / remote-timeout / bg-child ────────────────────

    def test_route_oracle_timeout_clamped(self, tmp_path):
        from codeagent.cli import main
        from codeagent.constants import ORACLE_TIMEOUT

        rm = _make_repo_map(tmp_path)
        result = _make_run_result()
        with (
            mock.patch("codeagent.cli.load_repo_map", return_value=rm),
            mock.patch("codeagent.cli._execute", return_value=result) as mock_exec,
            mock.patch("codeagent.cli.SessionRegistry"),
        ):
            rc = main(["route", "TestTopic", "task", "--raw", "--agent", "oracle"])
        assert rc == 0
        request = mock_exec.call_args.args[0]
        assert request.timeout == ORACLE_TIMEOUT

    def test_route_remote_timeout_warning_and_clamp(self, tmp_path, caplog):
        from codeagent.cli import main
        from codeagent.constants import SSH_IDLE_WINDOW

        rm = _make_repo_map(tmp_path)
        result = _make_run_result()
        with (
            mock.patch("codeagent.cli.load_repo_map", return_value=rm),
            mock.patch("codeagent.cli._execute", return_value=result) as mock_exec,
            mock.patch("codeagent.cli.SessionRegistry"),
        ):
            rc = main(["route", "TestTopic", "task", "--raw", "--timeout", "30"])
        assert rc == 0
        request = mock_exec.call_args.args[0]
        assert request.timeout == SSH_IDLE_WINDOW
        assert any("SSH_IDLE_WINDOW" in r.message for r in caplog.records)

    def test_route_bg_child_mark_done_failure(self, tmp_path, capsys):
        from codeagent.cli import main

        rm = _make_repo_map(tmp_path)
        result = _make_run_result(returncode=7, stdout="", stderr="")
        with (
            mock.patch("codeagent.cli.load_repo_map", return_value=rm),
            mock.patch("codeagent.cli._execute", return_value=result),
            mock.patch("codeagent.cli.SessionRegistry"),
            mock.patch("codeagent.job.get_manager") as mock_mgr,
        ):
            mock_mgr.return_value.mark_done.side_effect = RuntimeError("disk full")
            rc = main(["route", "TestTopic", "task", "--raw", "--_bg-job-id", "job-r"])
        assert rc == 7  # mark_done failure is logged, not fatal

    # ── _run_sync_result oracle bootstrap / crash recovery ───────────────

    @staticmethod
    def _run_args(agent="oracle", new_session=False, timeout=60, task="oracle task"):
        args = mock.MagicMock()
        args.task = task
        args.workdir = ""
        args.host = None
        args.backend = "omp"
        args.agent = agent
        args.model = None
        args.skills = None
        args.skip_permissions = False
        args.session_key = None
        args.new_session = new_session
        args.no_auto_resume = False
        args.timeout = timeout
        args.output = None
        return args

    def test_run_sync_result_oracle_bootstrap_and_warm_resume(self, tmp_path, capsys):
        from codeagent.cli import _run_sync_result
        from codeagent.constants import ORACLE_TIMEOUT
        from codeagent.domain import RunContext

        args = self._run_args()
        rc = RunContext(review_key="rk", generation=1, run_id="ri", request_id="rq",
                        swarm_session_id="sid", mailbox_root=str(tmp_path))
        kernel = mock.MagicMock()
        kernel.poll.return_value = mock.MagicMock(
            messages=[{"from": "manager", "body": "pending ping"}],
            cursor="", has_more=False,
        )
        result = _make_run_result(returncode=0, stdout="", stderr="")
        target = mock.MagicMock()
        target.is_local = True
        with (
            mock.patch("codeagent.cli._resolve_agent_backend", return_value="omp"),
            mock.patch("codeagent.cli.load_repo_map"),
            mock.patch("codeagent.cli.resolve_target", return_value=target),
            mock.patch("codeagent.cli._bootstrap_oracle_swarm", return_value=rc),
            mock.patch(
                "codeagent.cli._get_swarm_kernel", return_value=(kernel, mock.MagicMock())
            ),
            mock.patch("codeagent.cli._execute", return_value=result) as mock_exec,
            mock.patch("codeagent.cli.SessionRegistry"),
        ):
            _run_sync_result(args, "oracle task")
        request = mock_exec.call_args.args[0]
        assert request.timeout == ORACLE_TIMEOUT
        assert "PENDING MAILBOX MESSAGES" in request.task
        assert "pending ping" in request.task
        assert "--- END PENDING ---" in request.task
        kernel.poll.assert_called_once_with("sid", "oracle")

    def test_run_sync_result_oracle_new_session_skips_resume(self, tmp_path):
        from codeagent.cli import _run_sync_result
        from codeagent.constants import ORACLE_TIMEOUT
        from codeagent.domain import RunContext

        args = self._run_args(new_session=True)
        rc = RunContext(review_key="rk", generation=1, run_id="ri", request_id="rq",
                        swarm_session_id="sid", mailbox_root=str(tmp_path))
        result = _make_run_result(returncode=0, stdout="", stderr="")
        target = mock.MagicMock()
        target.is_local = True
        with (
            mock.patch("codeagent.cli._resolve_agent_backend", return_value="omp"),
            mock.patch("codeagent.cli.load_repo_map"),
            mock.patch("codeagent.cli.resolve_target", return_value=target),
            mock.patch("codeagent.cli._bootstrap_oracle_swarm", return_value=rc),
            mock.patch("codeagent.cli._execute", return_value=result) as mock_exec,
            mock.patch("codeagent.cli.SessionRegistry"),
        ):
            _run_sync_result(args, "oracle task")
        request = mock_exec.call_args.args[0]
        assert request.timeout == ORACLE_TIMEOUT
        assert "PENDING MAILBOX MESSAGES" not in request.task

    def test_run_sync_result_oracle_crash_recovery(self, tmp_path, capsys):
        from codeagent.cli import _run_sync_result
        from codeagent.domain import RunContext

        args = self._run_args(new_session=True)
        rc = RunContext(review_key="rk", generation=1, run_id="ri", request_id="rq",
                        swarm_session_id="sid", mailbox_root=str(tmp_path))
        store = mock.MagicMock()
        store.read_history.return_value = [
            {"kind": "REPORT", "from": "oracle", "body": "recovered answer"},
            {"kind": "TASK", "from": "manager", "body": "irrelevant"},
        ]
        target = mock.MagicMock()
        target.is_local = True
        with (
            mock.patch("codeagent.cli._resolve_agent_backend", return_value="omp"),
            mock.patch("codeagent.cli.load_repo_map"),
            mock.patch("codeagent.cli.resolve_target", return_value=target),
            mock.patch("codeagent.cli._bootstrap_oracle_swarm", return_value=rc),
            mock.patch("codeagent.cli._execute", side_effect=RuntimeError("boom")),
            mock.patch("codeagent.cli.MailboxStore", return_value=store),
            mock.patch("codeagent.cli.SessionRegistry"),
        ):
            result = _run_sync_result(args, "oracle task")
        assert result.returncode == 1
        err = capsys.readouterr().err
        assert "[recovered from mailbox]: recovered answer" in err
        assert "[oracle error - output may be incomplete]: boom" in err

    def test_run_sync_result_oracle_crash_no_recovery(self, tmp_path, capsys):
        from codeagent.cli import _run_sync_result
        from codeagent.domain import RunContext

        args = self._run_args(new_session=True)
        rc = RunContext(review_key="rk", generation=1, run_id="ri", request_id="rq",
                        swarm_session_id="sid", mailbox_root=str(tmp_path))
        store = mock.MagicMock()
        store.read_history.return_value = [
            {"kind": "TASK", "from": "manager", "body": "no report here"}
        ]
        target = mock.MagicMock()
        target.is_local = True
        with (
            mock.patch("codeagent.cli._resolve_agent_backend", return_value="omp"),
            mock.patch("codeagent.cli.load_repo_map"),
            mock.patch("codeagent.cli.resolve_target", return_value=target),
            mock.patch("codeagent.cli._bootstrap_oracle_swarm", return_value=rc),
            mock.patch("codeagent.cli._execute", side_effect=RuntimeError("boom")),
            mock.patch("codeagent.cli.MailboxStore", return_value=store),
            mock.patch("codeagent.cli.SessionRegistry"),
        ):
            result = _run_sync_result(args, "oracle task")
        assert result.returncode == 1
        err = capsys.readouterr().err
        assert "[recovered from mailbox]" not in err
        assert "boom" in err

    def test_run_sync_result_crash_history_read_exception(self, tmp_path, capsys):
        from codeagent.cli import _run_sync_result
        from codeagent.domain import RunContext

        args = self._run_args(new_session=True)
        rc = RunContext(review_key="rk", generation=1, run_id="ri", request_id="rq",
                        swarm_session_id="sid", mailbox_root=str(tmp_path))
        store = mock.MagicMock()
        store.read_history.side_effect = RuntimeError("corrupt history")
        target = mock.MagicMock()
        target.is_local = True
        with (
            mock.patch("codeagent.cli._resolve_agent_backend", return_value="omp"),
            mock.patch("codeagent.cli.load_repo_map"),
            mock.patch("codeagent.cli.resolve_target", return_value=target),
            mock.patch("codeagent.cli._bootstrap_oracle_swarm", return_value=rc),
            mock.patch("codeagent.cli._execute", side_effect=RuntimeError("boom")),
            mock.patch("codeagent.cli.MailboxStore", return_value=store),
            mock.patch("codeagent.cli.SessionRegistry"),
        ):
            result = _run_sync_result(args, "oracle task")
        assert result.returncode == 1
        err = capsys.readouterr().err
        assert "[recovered from mailbox]" not in err
        assert "[oracle error - output may be incomplete]: boom" in err

    # ── _cmd_park ────────────────────────────────────────────────────────

    def test_park_no_subcommand(self, capsys):
        from codeagent.cli import main

        with mock.patch("codeagent.park.registry.ParkRegistry"):
            rc = main(["park"])
        assert rc == 1
        assert "missing subcommand" in capsys.readouterr().out

    def test_park_list_all_sql(self, capsys):
        from codeagent.cli import main
        from codeagent.domain.park import Lifecycle

        registry = mock.MagicMock()
        conn = mock.MagicMock()
        conn.execute.return_value.fetchall.return_value = [
            (json.dumps({"review_key": "k1", "lifecycle": "hot_parked",
                         "round": 1, "agent_type": "oracle"}),),
        ]
        registry._connect.return_value.__enter__.return_value = conn
        registry._dict_to_manifest.side_effect = lambda d: mock.MagicMock(
            review_key=d["review_key"],
            lifecycle=Lifecycle(d["lifecycle"]),
            round=d["round"],
            agent_type=d["agent_type"],
        )
        with mock.patch("codeagent.park.registry.ParkRegistry", return_value=registry):
            rc = main(["park", "list", "--all"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "k1" in out
        assert "hot_parked" in out
        conn.execute.assert_called_once()

    def test_park_list_active_and_lifecycle_filter(self, capsys):
        from codeagent.cli import main
        from codeagent.domain.park import Lifecycle

        registry = mock.MagicMock()
        m1 = mock.MagicMock(review_key="k1", lifecycle=Lifecycle.HOT_PARKED,
                            round=1, agent_type="oracle")
        m2 = mock.MagicMock(review_key="k2", lifecycle=Lifecycle.COLD_RESUMABLE,
                            round=2, agent_type="oracle-lite")
        registry.list_active.return_value = [m1, m2]
        with mock.patch("codeagent.park.registry.ParkRegistry", return_value=registry):
            rc = main(["park", "list"])
            assert rc == 0
            out = capsys.readouterr().out
            assert "k1" in out and "k2" in out

            rc = main(["park", "list", "--lifecycle", "hot_parked"])
            assert rc == 0
            out = capsys.readouterr().out
            assert "k1" in out
            assert "k2" not in out

    def test_park_list_empty(self, capsys):
        from codeagent.cli import main

        registry = mock.MagicMock()
        registry.list_active.return_value = []
        with mock.patch("codeagent.park.registry.ParkRegistry", return_value=registry):
            rc = main(["park", "list"])
        assert rc == 0
        assert "(no park instances)" in capsys.readouterr().out

    def test_park_info_with_runtime(self, capsys):
        from codeagent.cli import main
        from codeagent.domain.park import Lifecycle

        registry = mock.MagicMock()
        m = mock.MagicMock(review_key="k1", lifecycle=Lifecycle.HOT_PARKED,
                           agent_type="oracle", model="m1", backend_session_id="bs",
                           peer_agent_id="p", round=1, created_at=1.0,
                           last_activity_at=2.0, soft_expires_at=3.0)
        registry.lookup.return_value = m
        client = mock.MagicMock()
        client.call.return_value = {"health": "ok"}
        with (
            mock.patch("codeagent.park.registry.ParkRegistry", return_value=registry),
            mock.patch("codeagent.gateway.client.GatewayClient", return_value=client),
        ):
            rc = main(["park", "info", "k1"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["review_key"] == "k1"
        assert data["runtime"]["health"] == "ok"
        client.call.assert_called_once_with("runtime.info", {"review_key": "k1"})

    def test_park_info_gateway_error(self, capsys):
        from codeagent.cli import main
        from codeagent.domain.park import Lifecycle
        from codeagent.gateway.model import GatewayError

        registry = mock.MagicMock()
        m = mock.MagicMock(review_key="k1", lifecycle=Lifecycle.HOT_PARKED,
                           agent_type="oracle", model="", backend_session_id="",
                           peer_agent_id="", round=1, created_at=1.0,
                           last_activity_at=2.0, soft_expires_at=3.0)
        registry.lookup.return_value = m
        client = mock.MagicMock()
        client.call.side_effect = GatewayError("E_CONNECT", "gateway down")
        with (
            mock.patch("codeagent.park.registry.ParkRegistry", return_value=registry),
            mock.patch("codeagent.gateway.client.GatewayClient", return_value=client),
        ):
            rc = main(["park", "info", "k1"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["runtime_error"] == "E_CONNECT: gateway down"

    def test_park_info_generic_error(self, capsys):
        from codeagent.cli import main
        from codeagent.domain.park import Lifecycle

        registry = mock.MagicMock()
        m = mock.MagicMock(review_key="k1", lifecycle=Lifecycle.HOT_PARKED,
                           agent_type="oracle", model="", backend_session_id="",
                           peer_agent_id="", round=1, created_at=1.0,
                           last_activity_at=2.0, soft_expires_at=3.0)
        registry.lookup.return_value = m
        client = mock.MagicMock()
        client.call.side_effect = RuntimeError("boom")
        with (
            mock.patch("codeagent.park.registry.ParkRegistry", return_value=registry),
            mock.patch("codeagent.gateway.client.GatewayClient", return_value=client),
        ):
            rc = main(["park", "info", "k1"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["runtime_error"] == "boom"

    def test_park_info_not_found(self, capsys):
        from codeagent.cli import main

        registry = mock.MagicMock()
        registry.lookup.return_value = None
        with mock.patch("codeagent.park.registry.ParkRegistry", return_value=registry):
            rc = main(["park", "info", "k1"])
        assert rc == 0
        assert "(no instance for 'k1')" in capsys.readouterr().out

    def test_park_revive_with_prompt(self, capsys):
        from types import SimpleNamespace

        from codeagent.cli import main

        rv = mock.MagicMock(method="hot", success=True, context="ctx")
        r = SimpleNamespace(stdout="agent out", returncode=0)
        with (
            mock.patch("codeagent.park.registry.ParkRegistry", return_value=mock.MagicMock()),
            mock.patch("codeagent.park.router.park_revive", return_value=rv),
            mock.patch("codeagent.cli.subprocess.run", return_value=r) as run_mock,
        ):
            rc = main(["park", "revive", "k1", "--prompt", "continue"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["method"] == "hot"
        assert data["prompt"] == "continue"
        assert data["revive_returncode"] == 0
        argv = run_mock.call_args.args[0]
        assert argv[argv.index("--session-key") + 1] == "k1"
        assert argv[argv.index("--agent") + 1] == "oracle"

    def test_park_revive_cold_inserts_new_session(self, capsys):
        from types import SimpleNamespace

        from codeagent.cli import main

        rv = mock.MagicMock(method="cold", success=True, context="ctx")
        r = SimpleNamespace(stdout="cold out", returncode=0)
        with (
            mock.patch("codeagent.park.registry.ParkRegistry", return_value=mock.MagicMock()),
            mock.patch("codeagent.park.router.park_revive", return_value=rv),
            mock.patch("codeagent.cli.subprocess.run", return_value=r) as run_mock,
        ):
            rc = main(["park", "revive", "k1", "--prompt", "go"])
        assert rc == 0
        argv = run_mock.call_args.args[0]
        assert "--new-session" in argv

    def test_park_revive_subprocess_exception(self, capsys):
        from codeagent.cli import main

        class _FakeSubprocessError(Exception):
            """Exception carrying str stdout (like a subprocess error would)."""

            stdout = "partial output"
            stderr = ""

        rv = mock.MagicMock(method="hot", success=True, context="ctx")
        with (
            mock.patch("codeagent.park.registry.ParkRegistry", return_value=mock.MagicMock()),
            mock.patch("codeagent.park.router.park_revive", return_value=rv),
            mock.patch(
                "codeagent.cli.subprocess.run",
                side_effect=_FakeSubprocessError("spawn failed"),
            ),
        ):
            rc = main(["park", "revive", "k1", "--prompt", "go"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["revive_error"].startswith("_FakeSubprocessError")
        assert data["revive_output"] == "partial output"
        assert data["revive_returncode"] == -1

    def test_park_acquire_success_and_exists(self, capsys):
        from codeagent.cli import main

        registry = mock.MagicMock()
        registry.acquire.return_value = True
        with mock.patch("codeagent.park.registry.ParkRegistry", return_value=registry):
            rc = main([
                "park", "acquire", "k1", "--agent-type", "oracle",
                "--peer-id", "p1", "--mailbox-id", "m1", "--backend-id", "b1",
            ])
            assert rc == 0
            assert "Acquired: k1 (agent=oracle)" in capsys.readouterr().out

            registry.acquire.return_value = False
            rc = main(["park", "acquire", "k1"])
            assert rc == 1
            assert "Already exists: k1" in capsys.readouterr().out

    def test_park_renew_and_release(self, capsys):
        from codeagent.cli import main

        registry = mock.MagicMock()
        with mock.patch("codeagent.park.registry.ParkRegistry", return_value=registry):
            rc = main(["park", "renew", "k1"])
            assert rc == 0
            assert "Renewed: k1" in capsys.readouterr().out
            registry.renew.assert_called_once_with("k1")

            rc = main(["park", "release", "k1"])
            assert rc == 0
            assert "Released: k1" in capsys.readouterr().out
            registry.release.assert_called_once_with("k1")

    def test_park_sweep(self, capsys):
        from codeagent.cli import main

        registry = mock.MagicMock()
        registry.sweep.return_value = ["k1", "k2"]
        with mock.patch("codeagent.park.registry.ParkRegistry", return_value=registry):
            rc = main(["park", "sweep"])
            assert rc == 0
            out = capsys.readouterr().out
            assert "Evicted: k1" in out and "Evicted: k2" in out

            registry.sweep.return_value = []
            rc = main(["park", "sweep"])
            assert rc == 0
            assert "(no expired instances)" in capsys.readouterr().out

            rc = main(["park", "sweep", "--dry-run"])
            assert rc == 0
            assert "Dry run: would sweep expired instances" in capsys.readouterr().out

    # ── _cmd_gateway / _cmd_events / _cmd_runtime ────────────────────────

    def test_gateway_no_subcommand(self, capsys):
        from codeagent.cli import main

        rc = main(["gateway"])
        assert rc == 1
        assert "missing subcommand" in capsys.readouterr().err

    def test_gateway_dispatch_to_handlers(self):
        from codeagent.cli import main

        for cmd in ("start", "ensure", "status", "stop", "serve", "rpc", "health"):
            handler = mock.MagicMock(return_value=7)
            with mock.patch(f"codeagent.gateway.cli.cmd_gateway_{cmd}", handler):
                args = ["gateway", cmd]
                if cmd == "ensure":
                    args += ["--host", "h"]
                rc = main(args)
            assert rc == 7, f"gateway {cmd} did not dispatch"
            handler.assert_called_once()

    def test_events_no_subcommand(self, capsys):
        from codeagent.cli import main

        rc = main(["events"])
        assert rc == 1
        assert "missing subcommand" in capsys.readouterr().err

    def test_events_watch_dispatch(self):
        from codeagent.cli import main

        with mock.patch("codeagent.gateway.cli.cmd_events_watch", return_value=3) as handler:
            rc = main(["events", "watch", "--session", "s1"])
        assert rc == 3
        handler.assert_called_once()

    def test_runtime_no_subcommand(self, capsys):
        from codeagent.cli import main

        rc = main(["runtime"])
        assert rc == 1
        assert "missing subcommand" in capsys.readouterr().err

    def test_runtime_status_alive(self, capsys):
        from codeagent.cli import main

        client = mock.MagicMock()
        client.call.return_value = {"health": {"alive": True}}
        with mock.patch("codeagent.gateway.client.GatewayClient", return_value=client):
            rc = main(["runtime", "status", "rt-1"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["health"]["alive"] is True
        client.call.assert_called_once_with("runtime.probe", {"runtime_id": "rt-1"})

    def test_runtime_status_dead(self, capsys):
        from codeagent.cli import main

        client = mock.MagicMock()
        client.call.return_value = {"health": {"alive": False}}
        with mock.patch("codeagent.gateway.client.GatewayClient", return_value=client):
            rc = main(["runtime", "status", "rt-1"])
        assert rc == 1

    def test_runtime_stop(self, capsys):
        from codeagent.cli import main

        client = mock.MagicMock()
        client.call.return_value = {"stopped": True}
        with mock.patch("codeagent.gateway.client.GatewayClient", return_value=client):
            rc = main(["runtime", "stop", "rt-1", "--reason", "maintenance"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["stopped"] is True
        client.call.assert_called_once_with(
            "runtime.stop", {"runtime_id": "rt-1", "reason": "maintenance"}
        )

    def test_runtime_gateway_error(self, capsys):
        from codeagent.cli import main
        from codeagent.gateway.model import GatewayError

        client = mock.MagicMock()
        client.call.side_effect = GatewayError("EC", "connection refused")
        with mock.patch("codeagent.gateway.client.GatewayClient", return_value=client):
            rc = main(["runtime", "status", "rt-1"])
        assert rc == 1
        assert "connection refused" in capsys.readouterr().err

    # ── _cmd_oracle missing subcommand ───────────────────────────────────

    def test_oracle_no_subcommand(self, capsys):
        from codeagent.cli import main

        rc = main(["oracle"])
        assert rc == 1
        assert "oracle: missing subcommand" in capsys.readouterr().err

    # ── _cmd_job ─────────────────────────────────────────────────────────

    def test_job_no_subcommand(self, capsys):
        from codeagent.cli import main

        rc = main(["job"])
        assert rc == 1
        assert "specify a job subcommand" in capsys.readouterr().err

    def test_job_list_empty(self, capsys):
        from codeagent.cli import main

        mgr = mock.MagicMock()
        mgr.list_jobs.return_value = []
        with mock.patch("codeagent.job.get_manager", return_value=mgr):
            rc = main(["job", "list"])
        assert rc == 0
        assert "no jobs found" in capsys.readouterr().out

    def test_job_list_rows(self, capsys):
        from codeagent.cli import main

        j = mock.MagicMock(job_id="j1", status="running",
                           created_at="2026-01-01T00:00:00Z",
                           task="do the thing that is quite long indeed")
        mgr = mock.MagicMock()
        mgr.list_jobs.return_value = [j]
        with mock.patch("codeagent.job.get_manager", return_value=mgr):
            rc = main(["job", "list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "j1" in out and "running" in out

    def test_job_status_ok(self, capsys):
        from codeagent.cli import main

        info = mock.MagicMock()
        info.to_dict.return_value = {"job_id": "j1", "status": "done"}
        mgr = mock.MagicMock()
        mgr.status.return_value = info
        with mock.patch("codeagent.job.get_manager", return_value=mgr):
            rc = main(["job", "status", "j1"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["job_id"] == "j1"
        mgr.status.assert_called_once_with("j1")

    def test_job_status_not_found(self, capsys):
        from codeagent.cli import main

        mgr = mock.MagicMock()
        mgr.status.side_effect = FileNotFoundError("no such job")
        with mock.patch("codeagent.job.get_manager", return_value=mgr):
            rc = main(["job", "status", "j1"])
        assert rc == 1
        assert "no such job" in capsys.readouterr().err

    def test_job_wait_returncode(self, capsys):
        from codeagent.cli import main

        info = mock.MagicMock()
        info.to_dict.return_value = {"job_id": "j1", "status": "done"}
        info.returncode = 3
        mgr = mock.MagicMock()
        mgr.wait.return_value = info
        with mock.patch("codeagent.job.get_manager", return_value=mgr):
            rc = main(["job", "wait", "j1", "--timeout", "5"])
        assert rc == 3
        mgr.wait.assert_called_once_with("j1", timeout=5.0)

    def test_job_wait_no_returncode(self, capsys):
        from codeagent.cli import main

        info = mock.MagicMock()
        info.to_dict.return_value = {"job_id": "j1"}
        info.returncode = None
        mgr = mock.MagicMock()
        mgr.wait.return_value = info
        with mock.patch("codeagent.job.get_manager", return_value=mgr):
            rc = main(["job", "wait", "j1"])
        assert rc == 0

    def test_job_wait_not_found(self, capsys):
        from codeagent.cli import main

        mgr = mock.MagicMock()
        mgr.wait.side_effect = FileNotFoundError("gone")
        with mock.patch("codeagent.job.get_manager", return_value=mgr):
            rc = main(["job", "wait", "j1"])
        assert rc == 1
        assert "gone" in capsys.readouterr().err

    def test_job_unknown_subcommand_direct(self, capsys):
        from types import SimpleNamespace

        from codeagent.cli import _cmd_job

        with mock.patch("codeagent.job.get_manager"):
            rc = _cmd_job(SimpleNamespace(job_cmd="bogus"))
        assert rc == 1
        assert "unknown subcommand" in capsys.readouterr().err

    # ── _cmd_session ─────────────────────────────────────────────────────

    def test_session_no_subcommand(self, capsys):
        from codeagent.cli import main

        rc = main(["session"])
        assert rc == 1
        assert "specify a session subcommand" in capsys.readouterr().err

    def test_session_clean_text(self, capsys):
        from codeagent.cli import main

        store = mock.MagicMock()
        store.clean_older_than.return_value = {"removed": ["s1", "s2"], "skipped": ["s3"]}
        with mock.patch("codeagent.cli.MailboxStore", return_value=store):
            rc = main(["session", "clean", "--older-than", "30"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "removed s1" in out and "removed s2" in out
        assert "skipped s3 (active park lease / locked)" in out
        assert "clean: removed 2, skipped 1" in out
        store.clean_older_than.assert_called_once_with(30)

    def test_session_clean_json(self, capsys):
        from codeagent.cli import main

        store = mock.MagicMock()
        store.clean_older_than.return_value = {"removed": ["s1"], "skipped": []}
        with mock.patch("codeagent.cli.MailboxStore", return_value=store):
            rc = main(["session", "clean", "--older-than", "30", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["removed"] == ["s1"]

    def test_session_unknown_subcommand_direct(self, capsys):
        from types import SimpleNamespace

        from codeagent.cli import _cmd_session

        rc = _cmd_session(SimpleNamespace(session_cmd="bogus"))
        assert rc == 1
        assert "unknown subcommand" in capsys.readouterr().err
