"""Tests for the mailbox CLI, health diagnostics, and hook entry points."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeagent.mailbox import cli as mailbox_cli
from codeagent.mailbox import health as mailbox_health
from codeagent.mailbox import hook as mailbox_hook
from codeagent.mailbox.store import MailboxStore


@pytest.fixture
def store(tmp_path: Path) -> MailboxStore:
    return MailboxStore(root=tmp_path)


def _run_cli(argv: list[str], store: MailboxStore, capsys):
    # Global option must precede the subcommand.
    argv = ["--mailbox-root", str(store.root)] + argv
    return mailbox_cli.main(argv)


class TestMailboxCli:
    def test_session_init(self, store, capsys):
        _run_cli(["session-init", "--session", "s1", "--manager", "mgr", "--agents", "w1,w2"], store, capsys)
        assert "created" in capsys.readouterr().out
        assert (store.root / "s1" / "session.json").exists()

    def test_send_peek(self, store, capsys):
        _run_cli(["session-init", "--session", "s1", "--manager", "mgr", "--agents", "w1"], store, capsys)
        capsys.readouterr()
        _run_cli(
            ["send", "--session", "s1", "--from", "mgr", "--to", "w1",
             "--subject", "hello", "--body", "world", "--kind", "TASK", "--run-id", "run-1", "--request-id", "req-1"],
            store, capsys,
        )
        capsys.readouterr()
        _run_cli(["peek", "--session", "s1", "--agent", "w1"], store, capsys)
        peek = json.loads(capsys.readouterr().out)
        assert peek["pending"] == 1
        assert peek["messages"][0]["subject"] == "hello"

    def test_read_finalize(self, store, capsys):
        _run_cli(["session-init", "--session", "s1", "--manager", "mgr", "--agents", "w1"], store, capsys)
        capsys.readouterr()
        _run_cli(
            ["send", "--session", "s1", "--from", "mgr", "--to", "w1",
             "--subject", "s", "--body", "b", "--kind", "TASK", "--run-id", "run-1", "--request-id", "req-1"],
            store, capsys,
        )
        capsys.readouterr()
        _run_cli(["read", "--session", "s1", "--agent", "w1", "--owner", "w1", "--json"], store, capsys)
        msg = json.loads(capsys.readouterr().out)
        assert msg["subject"] == "s"
        _run_cli(["finalize", "--session", "s1", "--agent", "w1", "--msg-id", msg["msg_id"], "--owner", "w1"], store, capsys)
        assert "finalized" in capsys.readouterr().out

    def test_read_text_mode(self, store, capsys):
        _run_cli(["session-init", "--session", "s1", "--manager", "mgr", "--agents", "w1"], store, capsys)
        capsys.readouterr()
        _run_cli(
            ["send", "--session", "s1", "--from", "mgr", "--to", "w1",
             "--subject", "s", "--body", "b", "--kind", "TASK", "--run-id", "run-1", "--request-id", "req-1"],
            store, capsys,
        )
        capsys.readouterr()
        _run_cli(["read", "--session", "s1", "--agent", "w1", "--owner", "w1"], store, capsys)
        out = capsys.readouterr().out
        assert "FROM: mgr" in out
        assert "SUBJECT: s" in out

    def test_send_broadcast(self, store, capsys):
        _run_cli(["session-init", "--session", "s1", "--manager", "mgr", "--agents", "w1,w2"], store, capsys)
        capsys.readouterr()
        _run_cli(
            ["send", "--session", "s1", "--from", "mgr", "--to", "*",
             "--subject", "hello", "--body", "world", "--kind", "NOTICE"],
            store, capsys,
        )
        assert "broadcast → 2 recipients" in capsys.readouterr().out
        for agent in ("w1", "w2"):
            _run_cli(["peek", "--session", "s1", "--agent", agent], store, capsys)
            peek = json.loads(capsys.readouterr().out)
            assert peek["pending"] == 1
            assert peek["messages"][0]["subject"] == "hello"

    def test_history_cli(self, store, capsys):
        _run_cli(["session-init", "--session", "s1", "--manager", "mgr", "--agents", "w1"], store, capsys)
        capsys.readouterr()
        _run_cli(
            ["send", "--session", "s1", "--from", "mgr", "--to", "w1",
             "--subject", "s", "--body", "b", "--kind", "TASK", "--run-id", "run-1", "--request-id", "req-1"],
            store, capsys,
        )
        capsys.readouterr()
        _run_cli(["history", "--session", "s1"], store, capsys)
        out = capsys.readouterr().out
        assert "SUBJECT: s" in out
        _run_cli(["history", "--session", "s1", "--json", "--kind", "TASK"], store, capsys)
        msgs = json.loads(capsys.readouterr().out)
        assert len(msgs) == 1
        assert msgs[0]["subject"] == "s"

    def test_release(self, store, capsys):
        _run_cli(["session-init", "--session", "s1", "--manager", "mgr", "--agents", "w1"], store, capsys)
        capsys.readouterr()
        _run_cli(
            ["send", "--session", "s1", "--from", "mgr", "--to", "w1",
             "--subject", "s", "--body", "b", "--kind", "TASK", "--run-id", "run-1", "--request-id", "req-1"],
            store, capsys,
        )
        capsys.readouterr()
        _run_cli(["read", "--session", "s1", "--agent", "w1", "--owner", "w1", "--json"], store, capsys)
        msg = json.loads(capsys.readouterr().out)
        _run_cli(["release", "--session", "s1", "--agent", "w1", "--msg-id", msg["msg_id"], "--owner", "w1"], store, capsys)
        assert "released" in capsys.readouterr().out

    def test_status_stats_clear(self, store, capsys):
        _run_cli(["session-init", "--session", "s1", "--manager", "mgr", "--agents", "w1"], store, capsys)
        capsys.readouterr()
        _run_cli(["status", "--session", "s1", "--agent", "w1", "--state", "BUSY", "--current-task", "work"], store, capsys)
        assert "BUSY" in capsys.readouterr().out
        _run_cli(["stats", "--session", "s1", "--agent", "w1"], store, capsys)
        out = capsys.readouterr().out
        assert "inbox: 0" in out
        _run_cli(["clear", "--session", "s1", "--agent", "w1"], store, capsys)
        assert "cleared 0" in capsys.readouterr().out

    def test_recover_stale(self, store, capsys):
        _run_cli(["session-init", "--session", "s1", "--manager", "mgr", "--agents", "w1"], store, capsys)
        capsys.readouterr()
        _run_cli(["recover-stale", "--session", "s1", "--agent", "w1"], store, capsys)
        assert "recovered 0" in capsys.readouterr().out

    def test_check_legacy(self, store, capsys):
        _run_cli(["session-init", "--session", "s1", "--manager", "mgr", "--agents", "w1"], store, capsys)
        capsys.readouterr()
        _run_cli(
            ["send", "--session", "s1", "--from", "mgr", "--to", "w1",
             "--subject", "s", "--body", "b", "--kind", "TASK", "--run-id", "run-1", "--request-id", "req-1"],
            store, capsys,
        )
        capsys.readouterr()
        _run_cli(["check", "--session", "s1", "--agent", "w1", "--json"], store, capsys)
        out = capsys.readouterr().out
        msgs = [json.loads(l) for l in out.splitlines() if l.strip()]
        assert len(msgs) == 1
        assert msgs[0]["subject"] == "s"

    def test_value_error_exits_nonzero(self, store, capsys):
        with pytest.raises(SystemExit) as exc:
            _run_cli(["send", "--session", "s1", "--from", "mgr", "--to", "w1",
                      "--subject", "s", "--body", "b"], store, capsys)
        # P2 (oracle-lite): ValueError（terminal）→ exit 2 + stderr 带 message
        assert exc.value.code == 2
        assert "session not found: s1" in capsys.readouterr().err

    def test_no_command_prints_help(self, store, capsys):
        _run_cli([], store, capsys)
        assert "usage" in capsys.readouterr().out.lower()


class TestMailboxHealth:
    def _healthy_store(self, tmp_path: Path, monkeypatch=None) -> MailboxStore:
        store = MailboxStore(root=tmp_path)
        store.session_init("s1", "mgr", ["w1"])
        store.write_status("s1", "w1", "IDLE")
        # An identity file is part of a fully healthy configuration.
        identity = tmp_path / "identity.json"
        identity.write_text('{"session_id": "s1"}')
        if monkeypatch is not None:
            monkeypatch.setenv("OMP_MAILBOX_IDENTITY_FILE", str(identity))
        return store

    def test_diagnose_healthy(self, tmp_path, monkeypatch):
        # 隔离宿主环境变量泄漏：健康配置下不应误判 identity 已设置
        monkeypatch.delenv("OMP_MAILBOX_IDENTITY_FILE", raising=False)
        store = self._healthy_store(tmp_path)
        checks = mailbox_health.diagnose(store, "s1", "w1")
        assert checks["root_exists"] is True
        assert checks["session_dir_exists"] is True
        assert checks["agent_dir_exists"] is True
        assert checks["inbox_readable"] is True
        assert checks["status_readable"] is True
        assert checks["status_state"] == "IDLE"
        assert checks["peek_works"] is True
        assert checks["processing_dir_exists"] is True
        assert checks["identity_file_set"] is False

    def test_diagnose_missing_session(self, tmp_path):
        store = MailboxStore(root=tmp_path)
        checks = mailbox_health.diagnose(store, "s1", "w1")
        assert checks["session_dir_exists"] is False
        assert checks["status_readable"] is False

    def test_main_healthy_exits_zero(self, tmp_path, monkeypatch, capsys):
        self._healthy_store(tmp_path, monkeypatch)
        monkeypatch.setenv("MAILBOX_ROOT", str(tmp_path))
        with pytest.raises(SystemExit) as exc:
            mailbox_health.main(["--session", "s1", "--agent", "w1"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "ALL CHECKS PASSED" in out

    def test_main_broken_exits_one(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("MAILBOX_ROOT", str(tmp_path))
        with pytest.raises(SystemExit) as exc:
            mailbox_health.main(["--session", "s1", "--agent", "w1"])
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "SOME CHECKS FAILED" in out

    def test_main_json(self, tmp_path, monkeypatch, capsys):
        self._healthy_store(tmp_path, monkeypatch)
        monkeypatch.setenv("MAILBOX_ROOT", str(tmp_path))
        with pytest.raises(SystemExit) as exc:
            mailbox_health.main(["--session", "s1", "--agent", "w1", "--json"])
        assert exc.value.code == 0
        result = json.loads(capsys.readouterr().out)
        assert result["status_state"] == "IDLE"

    def test_identity_file_checks(self, tmp_path, monkeypatch):
        store = self._healthy_store(tmp_path)  # writes identity.json
        identity = tmp_path / "identity.json"
        monkeypatch.setenv("OMP_MAILBOX_IDENTITY_FILE", str(identity))
        checks = mailbox_health.diagnose(store, "s1", "w1")
        assert checks["identity_file_set"] is True
        assert checks["identity_file_exists"] is True
        assert checks["identity_file_writable"] is True

    def test_identity_file_missing(self, tmp_path, monkeypatch):
        store = self._healthy_store(tmp_path)
        monkeypatch.setenv("OMP_MAILBOX_IDENTITY_FILE", str(tmp_path / "ghost.json"))
        checks = mailbox_health.diagnose(store, "s1", "w1")
        assert checks["identity_file_set"] is True
        assert checks["identity_file_exists"] is False

    def test_identity_file_not_writable_dir(self, tmp_path, monkeypatch):
        # 改进项2: verify that a read-only parent directory is detected as
        # non-writable, even though the directory itself exists.
        store = self._healthy_store(tmp_path)
        ro_dir = tmp_path / "ro"
        ro_dir.mkdir()
        identity = ro_dir / "identity.json"
        monkeypatch.setenv("OMP_MAILBOX_IDENTITY_FILE", str(identity))
        # Simulate read-only directory by revoking write permission.
        try:
            ro_dir.chmod(0o555)
        except (OSError, PermissionError):
            pytest.skip("cannot revoke write permission in this environment")
        try:
            checks = mailbox_health.diagnose(store, "s1", "w1")
            assert checks["identity_file_set"] is True
            assert checks["identity_file_writable"] is False
        finally:
            # Restore permissions for cleanup.
            ro_dir.chmod(0o755)


class TestMailboxHook:
    def test_hook_empty(self, tmp_path, monkeypatch, capsys):
        store = MailboxStore(root=tmp_path)
        store.session_init("s1", "mgr", ["w1"])
        monkeypatch.setenv("MAILBOX_ROOT", str(tmp_path))
        mailbox_hook.main(["s1", "w1"])
        assert "empty" in capsys.readouterr().out

    def test_hook_pending(self, tmp_path, monkeypatch, capsys):
        store = MailboxStore(root=tmp_path)
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "subject here", "body", "TASK", run_id="run-1", request_id="req-1")
        monkeypatch.setenv("MAILBOX_ROOT", str(tmp_path))
        mailbox_hook.main(["s1", "w1"])
        out = capsys.readouterr().out
        assert "1 pending" in out
        assert "subject here" in out
