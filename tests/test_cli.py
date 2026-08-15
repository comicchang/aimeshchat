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
