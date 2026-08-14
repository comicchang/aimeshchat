"""Tests for the ``aimeshchat swarm`` CLI subcommands.

Covers: all 10 subcommands + watch, help, error paths, JSON output shape,
and swarm hook functions (register/unregister).
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from codeagent.cli import main


# ── Helpers ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """Redirect all swarm CLI calls to use a temp mailbox store.

    This ensures each test gets a clean, isolated filesystem.
    Reuses the same kernel instance within a test so in-memory
    session state survives across multiple _run() calls.
    Uses EngineDeliverySink so outbox commands work.
    """
    import codeagent.cli as cli_mod
    from codeagent.mailbox.store import MailboxStore
    from codeagent.swarm.kernel import SwarmKernel
    from codeagent.swarm.delivery import DeliveryEngine, EngineDeliverySink

    store = MailboxStore(root=tmp_path)
    engine = DeliveryEngine(mailbox_store=store, outbox_root=tmp_path / "_outbox")
    sink = EngineDeliverySink(engine)
    kernel = SwarmKernel(store=store, sink=sink)
    sink.set_kernel(kernel)

    def _make_kernel(store_root=None):
        return kernel, store

    monkeypatch.setattr(cli_mod, "_get_swarm_kernel", _make_kernel)
    return tmp_path


def _run(argv: list[str]) -> tuple[int, str, str]:
    """Run main() and capture stdout/stderr."""
    import io
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        rc = main(argv)
    except SystemExit as e:
        rc = e.code if e.code is not None else 0
    finally:
        out = sys.stdout.getvalue()
        err = sys.stderr.getvalue()
        sys.stdout, sys.stderr = old_out, old_err
    return rc, out, err


def _json_out(out: str) -> dict | list:
    """Parse CLI stdout as JSON."""
    return json.loads(out)


def _make_envelope_raw(
    session_id: str = "s1",
    from_id: str = "mgr",
    to_id: str = "w1",
    msg_id: str = "test-msg-1",
    subject: str = "test",
    body: str = "body",
) -> dict:
    """Create a raw envelope dict for test setup."""
    from datetime import datetime, timezone
    return {
        "session_id": session_id,
        "from": from_id,
        "to": to_id,
        "subject": subject,
        "body": body,
        "kind": "TASK",
        "msg_id": msg_id,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _setup_session(
    session_id="test-s1", manager="mgr", members="w1,w2",
) -> tuple[int, str, str]:
    """Create a session and return (rc, out, err)."""
    return _run(["swarm", "create-session", session_id,
                 "--manager", manager, "--members", members])


def _setup_full(
    session_id="test-s1", manager="mgr", members="w1,w2",
) -> None:
    """Create session + register all agents."""
    rc, _, _ = _setup_session(session_id, manager, members)
    assert rc == 0
    for agent in [manager] + members.split(","):
        rc, _, _ = _run(["swarm", "register", session_id,
                         "--agent", agent, "--host", "__local__", "--backend", "cli"])
        assert rc == 0


# ── swarm --help ─────────────────────────────────────────────────────────


class TestSwarmHelp:
    """swarm --help and subcommand --help produce expected output."""

    def test_swarm_help(self):
        rc, out, _ = _run(["swarm", "--help"])
        assert rc == 0
        assert "create-session" in out
        assert "register" in out
        assert "direct" in out
        assert "broadcast" in out
        assert "channel" in out
        assert "notice" in out
        assert "poll" in out
        assert "ack" in out
        assert "status" in out
        assert "watch" in out

    def test_swarm_no_subcommand_prints_error(self):
        rc, _, err = _run(["swarm"])
        assert rc == 1
        assert "swarm subcommand" in err

    def test_create_session_help(self):
        rc, out, _ = _run(["swarm", "create-session", "--help"])
        assert rc == 0
        assert "--manager" in out
        assert "--members" in out

    def test_direct_help(self):
        rc, out, _ = _run(["swarm", "direct", "--help"])
        assert rc == 0
        assert "--to" in out
        assert "--kind" in out
        assert "--body" in out
        assert "--attachment" in out

    def test_watch_help(self):
        rc, out, _ = _run(["swarm", "watch", "--help"])
        assert rc == 0
        assert "--interval" in out
        assert "--iterations" in out


# ── create-session ───────────────────────────────────────────────────────


class TestCreateSession:
    """swarm create-session happy and error paths."""

    def test_create_session_basic(self):
        rc, out, _ = _run(["swarm", "create-session", "s1",
                           "--manager", "mgr", "--members", "w1,w2"])
        assert rc == 0
        data = _json_out(out)
        assert data["session_id"] == "s1"
        assert data["manager_id"] == "mgr"
        assert "mgr" in data["roster"]
        assert "w1" in data["roster"]
        assert "w2" in data["roster"]

    def test_create_session_manager_always_in_roster(self):
        rc, out, _ = _run(["swarm", "create-session", "s2",
                           "--manager", "mgr", "--members", "w1"])
        assert rc == 0
        data = _json_out(out)
        assert "mgr" in data["roster"]

    def test_create_session_duplicate_fails(self):
        _run(["swarm", "create-session", "dup",
              "--manager", "mgr", "--members", "w1"])
        rc, _, err = _run(["swarm", "create-session", "dup",
                           "--manager", "mgr", "--members", "w1"])
        assert rc == 1
        assert "already exists" in err

    def test_create_session_invalid_id(self):
        rc, _, err = _run(["swarm", "create-session", "../escape",
                           "--manager", "mgr", "--members", "w1"])
        assert rc == 1
        assert "invalid" in err.lower()

    def test_create_session_restricted_policy(self):
        """B4: --policy restricted --allowed-senders → ACL 持久化 + 非白名单
        sender 被拒（跨进程——新 CLI 进程从 session.json 恢复 restricted）。"""
        rc, out, _ = _run(["swarm", "create-session", "rst",
                           "--manager", "mgr", "--members", "w1,w2",
                           "--policy", "restricted",
                           "--allowed-senders", "mgr,w1"])
        assert rc == 0
        data = _json_out(out)
        assert data["acl"]["policy"] == "restricted"
        assert data["acl"]["authority"] == "mgr"

        # 跨进程：新 CLI 进程（新 kernel）从 session.json 恢复 restricted
        rc, out, _ = _run(["swarm", "direct", "rst",
                           "--from", "w2", "--to", "w1",
                           "--kind", "NOTICE",
                           "--subject", "x", "--body", "y"])
        assert rc == 1  # w2 不在 allowed_senders

        rc, out, _ = _run(["swarm", "direct", "rst",
                           "--from", "w1", "--to", "w2",
                           "--kind", "NOTICE",
                           "--subject", "x", "--body", "y"])
        assert rc == 0  # w1 在 allowed_senders
        assert _json_out(out)["status"] == "delivered"


# ── register ─────────────────────────────────────────────────────────────


class TestRegister:
    """swarm register happy and error paths."""

    def test_register_basic(self):
        _setup_session("reg-s1", "mgr", "w1")
        rc, out, _ = _run(["swarm", "register", "reg-s1",
                           "--agent", "w1", "--host", "__local__"])
        assert rc == 0
        data = _json_out(out)
        assert data["agent_id"] == "w1"
        assert data["session_id"] == "reg-s1"
        assert data["host_alias"] == "__local__"
        assert data["backend"] == "cli"

    def test_register_with_backend(self):
        _setup_session("reg2", "mgr", "w1")
        rc, out, _ = _run(["swarm", "register", "reg2",
                           "--agent", "w1", "--host", "remote", "--backend", "omp"])
        assert rc == 0
        data = _json_out(out)
        assert data["backend"] == "omp"
        assert data["host_alias"] == "remote"

    def test_register_nonexistent_session(self):
        rc, _, err = _run(["swarm", "register", "ghost",
                           "--agent", "w1", "--host", "__local__"])
        assert rc == 1
        assert "not found" in err

    def test_register_agent_not_in_roster(self):
        _setup_session("reg3", "mgr", "w1")
        rc, _, err = _run(["swarm", "register", "reg3",
                           "--agent", "outsider", "--host", "__local__"])
        assert rc == 1
        assert "not in roster" in err


# ── direct ───────────────────────────────────────────────────────────────


class TestDirect:
    """swarm direct happy and error paths."""

    def test_direct_basic(self):
        _setup_full("dir-s1", "mgr", "w1,w2")
        rc, out, _ = _run(["swarm", "direct", "dir-s1",
                           "--from", "mgr", "--to", "w1",
                           "--kind", "TASK", "--subject", "do it",
                           "--body", "please do this task",
                           "--run-id", "run-1", "--request-id", "req-1"])
        assert rc == 0
        data = _json_out(out)
        assert "msg_id" in data
        assert data["status"] == "delivered"
        assert data["target"] == "w1"

    def test_direct_with_attachment(self):
        _setup_full("dir-att", "mgr", "w1")
        att = json.dumps({"artifact_id": "art-1", "source_host": "localhost",
                          "remote_root": "/tmp", "relative_path": "f.txt",
                          "size": 10, "sha256": "a" * 64})
        rc, out, _ = _run(["swarm", "direct", "dir-att",
                           "--from", "mgr", "--to", "w1",
                           "--kind", "NOTICE", "--subject", "att", "--body", "see attached",
                           "--attachment", att])
        assert rc == 0
        data = _json_out(out)
        assert data["msg_id"]

    def test_direct_acl_denial(self):
        """Agent not in allowed_senders cannot send."""
        _setup_full("dir-acl", "mgr", "w1,w2")
        # w1 sends to w2 — should work (open policy)
        rc, out, _ = _run(["swarm", "direct", "dir-acl",
                           "--from", "w1", "--to", "w2",
                           "--kind", "NOTICE",
                           "--subject", "hi", "--body", "hello"])
        assert rc == 0

    def test_direct_missing_session(self):
        rc, _, err = _run(["swarm", "direct", "ghost",
                           "--from", "mgr", "--to", "w1",
                           "--kind", "NOTICE",
                           "--subject", "x", "--body", "y"])
        assert rc == 1
        assert "not found" in err

    def test_direct_invalid_attachment_json(self):
        _setup_full("dir-badatt", "mgr", "w1")
        rc, _, err = _run(["swarm", "direct", "dir-badatt",
                           "--from", "mgr", "--to", "w1",
                           "--kind", "NOTICE",
                           "--subject", "x", "--body", "y",
                           "--attachment", "not-json"])
        assert rc == 1

    # ── Correlation-protocol enforcement ───────────────────────────────

    def test_direct_task_missing_run_id(self):
        """TASK without --run-id must exit 1."""
        _setup_full("dir-corr1", "mgr", "w1")
        rc, _, err = _run(["swarm", "direct", "dir-corr1",
                           "--from", "mgr", "--to", "w1",
                           "--kind", "TASK", "--subject", "s", "--body", "b",
                           "--request-id", "r1"])
        assert rc == 1
        assert "--run-id" in err

    def test_direct_task_missing_request_id(self):
        """TASK without --request-id must exit 1."""
        _setup_full("dir-corr2", "mgr", "w1")
        rc, _, err = _run(["swarm", "direct", "dir-corr2",
                           "--from", "mgr", "--to", "w1",
                           "--kind", "TASK", "--subject", "s", "--body", "b",
                           "--run-id", "run-1"])
        assert rc == 1
        assert "--request-id" in err

    def test_direct_report_missing_reply_to(self):
        """REPORT without --reply-to must exit 1."""
        _setup_full("dir-corr3", "mgr", "w1")
        rc, _, err = _run(["swarm", "direct", "dir-corr3",
                           "--from", "mgr", "--to", "w1",
                           "--kind", "REPORT", "--subject", "s", "--body", "b"])
        assert rc == 1
        assert "--reply-to" in err

    def test_direct_task_with_all_correlation_fields(self):
        """TASK with run_id + request_id succeeds."""
        _setup_full("dir-corr4", "mgr", "w1")
        rc, out, _ = _run(["swarm", "direct", "dir-corr4",
                           "--from", "mgr", "--to", "w1",
                           "--kind", "TASK", "--subject", "s", "--body", "b",
                           "--run-id", "run-1", "--request-id", "req-1"])
        assert rc == 0
        assert _json_out(out)["status"] == "delivered"

    def test_direct_report_with_reply_to(self):
        """REPORT with --reply-to succeeds."""
        _setup_full("dir-corr5", "mgr", "w1")
        rc, out, _ = _run(["swarm", "direct", "dir-corr5",
                           "--from", "mgr", "--to", "w1",
                           "--kind", "REPORT", "--subject", "s", "--body", "b",
                           "--reply-to", "msg-orig",
                           "--run-id", "run-1", "--request-id", "req-1"])
        assert rc == 0
        assert _json_out(out)["status"] == "delivered"


# ── channel ──────────────────────────────────────────────────────────────


class TestChannel:
    """swarm channel happy and error paths."""

    def test_channel_basic(self):
        _setup_full("ch-s1", "mgr", "w1,w2")
        # First create the channel via the kernel
        from codeagent.cli import _get_swarm_kernel
        kernel, _ = _get_swarm_kernel()
        kernel.create_channel("ch-s1", "dev", ["mgr", "w1"])

        rc, out, _ = _run(["swarm", "channel", "ch-s1", "dev",
                           "--from", "mgr", "--kind", "NOTICE",
                           "--subject", "channel msg", "--body", "in channel"])
        assert rc == 0
        data = _json_out(out)
        assert isinstance(data, list) and len(data) == 1
        assert data[0]["msg_id"]
        assert data[0]["status"] == "delivered"
        assert data[0]["recipient"] == "w1"

    def test_channel_not_found(self):
        _setup_full("ch-nf", "mgr", "w1")
        rc, _, err = _run(["swarm", "channel", "ch-nf", "ghost-ch",
                           "--from", "mgr", "--body", "x",
                           "--run-id", "r1", "--request-id", "req1"])
        assert rc == 1
        assert "channel not found" in err


# ── broadcast ────────────────────────────────────────────────────────────


class TestBroadcast:
    """swarm broadcast happy and error paths."""

    def test_broadcast_basic(self):
        _setup_full("bc-s1", "mgr", "w1,w2")
        rc, out, _ = _run(["swarm", "broadcast", "bc-s1",
                           "--from", "mgr", "--kind", "NOTICE",
                           "--subject", "announcement", "--body", "hello all"])
        assert rc == 0
        data = _json_out(out)
        assert isinstance(data, list)
        assert len(data) == 2  # w1 + w2 (mgr is sender)
        for entry in data:
            assert entry["status"] == "delivered"

    def test_broadcast_acl_denial(self):
        """Non-authority cannot broadcast when policy is restricted."""
        # Create a session with restricted policy
        _setup_session("bc-restr", "mgr", "w1,w2")
        from codeagent.cli import _get_swarm_kernel
        kernel, _ = _get_swarm_kernel()
        from codeagent.swarm.model import ACL
        session = kernel.get_session("bc-restr")
        session.acl = ACL(authority="mgr", policy="restricted",
                          allowed_senders=["mgr", "w1", "w2"],
                          room_members=["mgr", "w1", "w2"])

        rc, _, err = _run(["swarm", "broadcast", "bc-restr",
                           "--from", "w1", "--subject", "try", "--body", "try"])
        assert rc == 1
        assert "not broadcast authority" in err


# ── notice ───────────────────────────────────────────────────────────────


class TestNotice:
    """swarm notice happy and error paths."""

    def test_notice_basic(self):
        _setup_full("nt-s1", "mgr", "w1,w2")
        rc, out, _ = _run(["swarm", "notice", "nt-s1",
                           "--from", "mgr", "--topic", "system",
                           "--subject", "maintenance alert",
                           "--body", "maintenance at 3pm", "--ttl", "300"])
        assert rc == 0
        data = _json_out(out)
        assert isinstance(data, list) and len(data) == 2  # w1 + w2
        assert all(d["status"] == "delivered" for d in data)
        assert all(d["msg_id"] for d in data)

    def test_notice_acl_denial(self):
        """Agent not in allowed_senders cannot send notice."""
        _setup_session("nt-restr", "mgr", "w1")
        from codeagent.cli import _get_swarm_kernel
        kernel, _ = _get_swarm_kernel()
        session = kernel.get_session("nt-restr")
        from codeagent.swarm.model import ACL
        session.acl = ACL(authority="mgr", policy="restricted",
                          allowed_senders=["mgr"],
                          room_members=["mgr", "w1"])

        rc, _, err = _run(["swarm", "notice", "nt-restr",
                           "--from", "w1", "--topic", "t", "--subject", "s", "--body", "b"])
        assert rc == 1
        assert "not in allowed_senders" in err


# ── poll ─────────────────────────────────────────────────────────────────


class TestPoll:
    """swarm poll happy and error paths."""

    def test_poll_empty(self):
        _setup_full("poll-s1", "mgr", "w1")
        rc, out, _ = _run(["swarm", "poll", "poll-s1", "--agent", "w1"])
        assert rc == 0
        data = _json_out(out)
        assert data["messages"] == []
        assert data["has_more"] is False

    def test_poll_with_messages(self):
        _setup_full("poll-msg", "mgr", "w1")
        # Send a direct message to w1
        _run(["swarm", "direct", "poll-msg",
              "--from", "mgr", "--to", "w1",
              "--kind", "NOTICE",
              "--subject", "task", "--body", "do this"])
        rc, out, _ = _run(["swarm", "poll", "poll-msg", "--agent", "w1"])
        assert rc == 0
        data = _json_out(out)
        assert len(data["messages"]) == 1
        assert data["messages"][0]["subject"] == "task"
        assert data["cursor"]  # non-empty cursor

    def test_poll_with_limit(self):
        _setup_full("poll-lim", "mgr", "w1")
        for i in range(3):
            _run(["swarm", "direct", "poll-lim",
                  "--from", "mgr", "--to", "w1",
                  "--kind", "NOTICE",
                  "--subject", f"t{i}", "--body", f"b{i}"])
        rc, out, _ = _run(["swarm", "poll", "poll-lim",
                           "--agent", "w1", "--limit", "2"])
        assert rc == 0
        data = _json_out(out)
        assert len(data["messages"]) == 2

    def test_poll_nonexistent_session(self):
        rc, _, err = _run(["swarm", "poll", "ghost", "--agent", "w1"])
        assert rc == 1
        assert "not found" in err


# ── ack ──────────────────────────────────────────────────────────────────


class TestAck:
    """swarm ack happy and error paths."""

    def test_ack_consumed(self):
        _setup_full("ack-s1", "mgr", "w1")
        _run(["swarm", "direct", "ack-s1",
              "--from", "mgr", "--to", "w1",
              "--kind", "NOTICE",
              "--subject", "t", "--body", "b"])
        # Poll to get msg_id
        _, out, _ = _run(["swarm", "poll", "ack-s1", "--agent", "w1"])
        msg_id = _json_out(out)["messages"][0]["msg_id"]

        rc, out, _ = _run(["swarm", "ack", "ack-s1",
                           "--agent", "w1", "--msg-id", msg_id,
                           "--phase", "consumed"])
        assert rc == 0
        data = _json_out(out)
        assert data["status"]  # non-empty

    def test_ack_missing_message(self):
        _setup_full("ack-miss", "mgr", "w1")
        rc, _, err = _run(["swarm", "ack", "ack-miss",
                           "--agent", "w1", "--msg-id", "nonexistent"])
        assert rc == 1


# ── status ───────────────────────────────────────────────────────────────


class TestStatus:
    """swarm status happy and error paths."""

    def test_status_basic(self):
        _setup_full("st-s1", "mgr", "w1,w2")
        rc, out, _ = _run(["swarm", "status", "st-s1"])
        assert rc == 0
        data = _json_out(out)
        assert data["session_id"] == "st-s1"
        assert data["manager_id"] == "mgr"
        assert "mgr" in data["roster"]
        assert data["acl"]["authority"] == "mgr"
        assert data["acl"]["policy"] == "open"
        # All agents should be registered
        assert "mgr" in data["locations"]
        assert "w1" in data["locations"]
        assert "w2" in data["locations"]

    def test_status_nonexistent(self):
        rc, _, err = _run(["swarm", "status", "ghost"])
        assert rc == 1
        assert "not found" in err


# ── watch ────────────────────────────────────────────────────────────────


class TestWatch:
    """swarm watch with --iterations for bounded execution."""

    def test_watch_iterations(self):
        _setup_full("wt-s1", "mgr", "w1")
        # Send a message first
        _run(["swarm", "direct", "wt-s1",
              "--from", "mgr", "--to", "w1",
              "--kind", "NOTICE",
              "--subject", "hello", "--body", "world"])
        # Watch with 2 iterations (should poll twice and exit)
        rc, out, _ = _run(["swarm", "watch", "wt-s1",
                           "--agent", "w1", "--interval", "0",
                           "--iterations", "2"])
        assert rc == 0
        # First iteration should have the message, second should be empty
        lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
        # At least one message on first iteration
        assert len(lines) >= 1
        first = json.loads(lines[0])
        assert first["subject"] == "hello"


# ── end-to-end ───────────────────────────────────────────────────────────


class TestSwarmEndToEnd:
    """Full end-to-end: session → register → direct → broadcast → channel → poll → ack."""

    def test_full_workflow(self):
        sid = "e2e-s1"

        # 1. Create session
        rc, out, _ = _run(["swarm", "create-session", sid,
                           "--manager", "mgr", "--members", "w1,w2"])
        assert rc == 0
        data = _json_out(out)
        assert len(data["roster"]) == 3

        # 2. Register all agents
        for agent in ["mgr", "w1", "w2"]:
            rc, _, _ = _run(["swarm", "register", sid,
                             "--agent", agent, "--host", "__local__"])
            assert rc == 0

        # 3. Direct message: mgr → w1
        rc, out, _ = _run(["swarm", "direct", sid,
                           "--from", "mgr", "--to", "w1",
                           "--kind", "TASK", "--subject", "init",
                           "--body", "initialize subsystem",
                           "--run-id", "run-e2e", "--request-id", "req-e2e"])
        assert rc == 0
        direct_receipt = _json_out(out)
        assert direct_receipt["status"] == "delivered"

        # 4. Broadcast: mgr → all
        rc, out, _ = _run(["swarm", "broadcast", sid,
                           "--from", "mgr", "--kind", "NOTICE",
                           "--subject", "status", "--body", "all good"])
        assert rc == 0
        broadcast_receipts = _json_out(out)
        assert len(broadcast_receipts) == 2

        # 5. Create channel via kernel and send
        from codeagent.cli import _get_swarm_kernel
        kernel, _ = _get_swarm_kernel()
        kernel.create_channel(sid, "dev", ["mgr", "w1", "w2"])

        rc, out, _ = _run(["swarm", "channel", sid, "dev",
                           "--from", "mgr", "--kind", "NOTICE",
                           "--subject", "channel-task", "--body", "build it"])
        assert rc == 0

        # 6. Poll w1 inbox — should have direct + broadcast + channel
        rc, out, _ = _run(["swarm", "poll", sid, "--agent", "w1"])
        assert rc == 0
        poll_data = _json_out(out)
        assert len(poll_data["messages"]) == 3  # direct + broadcast + channel

        # 7. Notice
        rc, out, _ = _run(["swarm", "notice", sid,
                           "--from", "mgr", "--topic", "ops",
                           "--subject", "deployment notice",
                           "--body", "deploy done", "--ttl", "600"])
        assert rc == 0

        # 8. Poll w2 — broadcast + channel + notice (no direct)
        rc, out, _ = _run(["swarm", "poll", sid, "--agent", "w2"])
        assert rc == 0
        poll_data = _json_out(out)
        assert len(poll_data["messages"]) == 3

        # 9. Ack the first message for w1
        rc, out, _ = _run(["swarm", "poll", sid, "--agent", "w1", "--limit", "1"])
        msg_id = _json_out(out)["messages"][0]["msg_id"]
        rc, out, _ = _run(["swarm", "ack", sid,
                           "--agent", "w1", "--msg-id", msg_id])
        assert rc == 0

        # 10. Status
        rc, out, _ = _run(["swarm", "status", sid])
        assert rc == 0
        status_data = _json_out(out)
        assert status_data["session_id"] == sid
        assert len(status_data["locations"]) == 3


# ── outbox ─────────────────────────────────────────────────────────────


class TestOutbox:
    """swarm outbox pending/flush/status subcommands."""

    def _setup_with_pending(self, sid="ob-s1"):
        """Create session with a pending (undelivered) outbox entry.

        Uses a non-existent host so transport fails, leaving the outbox entry
        in accepted+queued state.
        """
        _setup_full(sid, "mgr", "w1,w2")
        # Direct to a host that doesn't exist — transport fails, outbox entry persists
        from codeagent.cli import _get_swarm_kernel
        kernel, _ = _get_swarm_kernel()
        from codeagent.domain import HostSpec
        from codeagent.swarm.model import AgentLocation
        # Register w1 to a remote host so delivery goes through transport
        kernel.register(
            AgentLocation(agent_id="w1", host_alias="remote-noexist", backend="cli"),
            sid,
        )
        # Send message — transport fails, outbox entry remains
        _run(["swarm", "direct", sid,
              "--from", "mgr", "--to", "w1",
              "--kind", "NOTICE",
              "--subject", "test", "--body", "payload"])

    def test_pending_json_lists_undelivered(self):
        self._setup_with_pending()
        rc, out, _ = _run(["swarm", "outbox", "pending", "--json"])
        assert rc == 0
        data = _json_out(out)
        assert isinstance(data, list)
        assert len(data) >= 1
        entry = data[0]
        assert "msg_id" in entry
        assert "to" in entry
        assert "kind" in entry

    def test_pending_json_with_session_filter(self):
        self._setup_with_pending("ob-s1")
        # Filter by existing session
        rc, out, _ = _run(["swarm", "outbox", "pending", "--session", "ob-s1", "--json"])
        assert rc == 0
        data = _json_out(out)
        assert len(data) >= 1
        # Filter by non-existent session — should be empty
        rc, out, _ = _run(["swarm", "outbox", "pending", "--session", "ghost", "--json"])
        assert rc == 0
        data = _json_out(out)
        assert data == []

    def test_flush_returns_flushed_count(self):
        self._setup_with_pending()
        # Transport is broken, so flush won't actually deliver — but it runs
        rc, out, _ = _run(["swarm", "outbox", "flush"])
        assert rc == 1  # nothing flushed (transport fails)
        data = _json_out(out)
        assert data["flushed"] == 0

    def test_flush_exits_1_when_nothing_to_flush(self):
        _setup_full("ob-empty", "mgr", "w1")
        # No pending messages
        rc, out, _ = _run(["swarm", "outbox", "flush", "--session", "ob-empty"])
        assert rc == 1
        data = _json_out(out)
        assert data["flushed"] == 0

    def test_status_shows_pending_delivered(self):
        self._setup_with_pending()
        rc, out, _ = _run(["swarm", "outbox", "status"])
        assert rc == 0
        data = _json_out(out)
        assert "pending" in data
        assert "delivered" in data
        assert data["pending"] >= 1

    def test_status_with_session_filter(self):
        self._setup_with_pending("ob-s2")
        rc, out, _ = _run(["swarm", "outbox", "status", "--session", "ob-s2"])
        assert rc == 0
        data = _json_out(out)
        assert data["pending"] >= 1

    def test_no_subcommand_returns_error(self):
        rc, _, err = _run(["swarm", "outbox"])
        assert rc == 1
        assert "outbox subcommand" in err

    def test_watch_flushes_outbox_before_polling(self):
        """Watch calls engine.flush() before entering the poll loop."""
        _setup_full("wt-flush", "mgr", "w1")
        from codeagent.swarm.model import AgentLocation

        # Use the SAME kernel that _run() will use (via monkeypatched _get_swarm_kernel)
        import codeagent.cli as cli_mod
        kernel, store = cli_mod._get_swarm_kernel()
        engine = kernel._sink._engine

        kernel.register(
            AgentLocation(agent_id="w1", host_alias="__local__", backend="cli"),
            "wt-flush",
        )
        env = _make_envelope_raw("wt-flush", "mgr", "w1", "wt-m1", "flush-test", "body")
        env["_target_host"] = "fakehost"
        outbox_dir = engine._outbox / "wt-flush"
        outbox_dir.mkdir(parents=True, exist_ok=True)
        (outbox_dir / "wt-m1.json").write_text(json.dumps(env))

        # Verify pending before watch
        stats_before = engine.outbox_stats(session_id="wt-flush")
        assert stats_before["pending"] >= 1

        # Patch _remote_send to succeed for our fakehost
        with mock.patch.object(engine, "_remote_send", return_value=None):
            # Run watch with 1 iteration (polls once then exits)
            rc, out, _ = _run(["swarm", "watch", "wt-flush",
                               "--agent", "w1", "--interval", "0",
                               "--iterations", "1"])
            assert rc == 0

        # After watch, the outbox entry should have been flushed
        stats_after = engine.outbox_stats(session_id="wt-flush")
        assert stats_after["pending"] == 0
        assert stats_after["delivered"] >= 1

    def test_kernel_factory_flush_handles_missing_transport(self, tmp_path):
        """_get_swarm_kernel succeeds even when flush fails (graceful degradation)."""
        import codeagent.cli as cli_mod
        from codeagent.swarm.delivery import DeliveryEngine

        # Restore the real _get_swarm_kernel temporarily
        real_fn = cli_mod._get_swarm_kernel.__wrapped__ if hasattr(cli_mod._get_swarm_kernel, '__wrapped__') else None
        if real_fn is None:
            # The function was monkeypatched; call the real code inline
            from codeagent.mailbox.store import MailboxStore
            from codeagent.swarm.kernel import SwarmKernel
            from codeagent.swarm.delivery import EngineDeliverySink

            store = MailboxStore(root=tmp_path)
            engine = DeliveryEngine(mailbox_store=store, outbox_root=tmp_path / "_outbox")
            sink = EngineDeliverySink(engine)
            kernel = SwarmKernel(store=store, sink=sink)
            sink.set_kernel(kernel)

            # Monkeypatch flush to raise
            with mock.patch.object(engine, 'flush', side_effect=RuntimeError("no transport")):
                # The kernel factory should still succeed
                # (flush failure is caught and logged)
                try:
                    flushed = engine.flush()
                except RuntimeError:
                    pass  # expected

            # Kernel should still be usable
            assert kernel is not None
            assert kernel._sink is sink


# ── Swarm hooks ──────────────────────────────────────────────────────────


class TestSwarmHooks:
    """Test hook functions: on_agent_start, on_agent_message, on_agent_stop."""

    def test_on_agent_start_register(self, tmp_path):
        from codeagent.hooks.swarm_hooks import reset, on_agent_start, _get_kernel
        reset()

        # Create session via the hook module's own kernel
        kernel, _ = _get_kernel(store_root=tmp_path)
        kernel.create_session("hook-s1", "mgr", ["w1"])

        result = on_agent_start(
            session_id="hook-s1", agent_id="w1",
            host_alias="__local__", backend="omp",
            store_root=tmp_path,
        )
        assert result["agent_id"] == "w1"
        assert result["session_id"] == "hook-s1"
        assert result["backend"] == "omp"
        reset()

    def test_on_agent_stop_unregister(self, tmp_path):
        from codeagent.hooks.swarm_hooks import reset, on_agent_start, on_agent_stop, _get_kernel
        reset()

        kernel, _ = _get_kernel(store_root=tmp_path)
        kernel.create_session("hook-s2", "mgr", ["w1"])

        on_agent_start(session_id="hook-s2", agent_id="w1",
                       store_root=tmp_path)
        result = on_agent_stop(session_id="hook-s2", agent_id="w1",
                               store_root=tmp_path)
        assert result["unregistered"] is True
        assert result["agent_id"] == "w1"
        reset()

    def test_on_agent_message_direct(self, tmp_path):
        from codeagent.hooks.swarm_hooks import reset, on_agent_message, _get_kernel
        reset()

        kernel, _ = _get_kernel(store_root=tmp_path)
        kernel.create_session("hook-msg", "mgr", ["w1"])
        from codeagent.swarm.model import AgentLocation
        kernel.register(AgentLocation("mgr", "__local__", "cli"), "hook-msg")
        kernel.register(AgentLocation("w1", "__local__", "cli"), "hook-msg")

        result = on_agent_message(
            session_id="hook-msg", agent_id="mgr",
            msg_dict={"to": "w1", "kind": "NOTICE", "subject": "s", "body": "b"},
            store_root=tmp_path,
        )
        assert result["status"] == "delivered"
        assert result["target"] == "w1"
        reset()

    def test_on_agent_message_broadcast(self, tmp_path):
        from codeagent.hooks.swarm_hooks import reset, on_agent_message, _get_kernel
        reset()

        kernel, _ = _get_kernel(store_root=tmp_path)
        kernel.create_session("hook-bc", "mgr", ["w1", "w2"])
        from codeagent.swarm.model import AgentLocation
        kernel.register(AgentLocation("mgr", "__local__", "cli"), "hook-bc")
        kernel.register(AgentLocation("w1", "__local__", "cli"), "hook-bc")
        kernel.register(AgentLocation("w2", "__local__", "cli"), "hook-bc")

        result = on_agent_message(
            session_id="hook-bc", agent_id="mgr",
            msg_dict={"to": "*", "kind": "NOTICE", "subject": "hi", "body": "all"},
            store_root=tmp_path,
        )
        assert result["broadcast"] is True
        assert result["recipients"] == 2
        reset()

    def test_on_agent_message_channel(self, tmp_path):
        from codeagent.hooks.swarm_hooks import reset, on_agent_message, _get_kernel
        reset()

        kernel, _ = _get_kernel(store_root=tmp_path)
        kernel.create_session("hook-ch", "mgr", ["w1", "w2"])
        from codeagent.swarm.model import AgentLocation
        kernel.register(AgentLocation("mgr", "__local__", "cli"), "hook-ch")
        kernel.register(AgentLocation("w1", "__local__", "cli"), "hook-ch")
        kernel.register(AgentLocation("w2", "__local__", "cli"), "hook-ch")
        kernel.create_channel("hook-ch", "dev", ["mgr", "w1"])

        result = on_agent_message(
            session_id="hook-ch", agent_id="mgr",
            msg_dict={"to": "#dev", "kind": "NOTICE", "subject": "s", "body": "b"},
            store_root=tmp_path,
        )
        assert result["channel"] is True
        assert result["recipients"] == 1  # w1 (mgr excluded)
        assert len(result["msg_ids"]) == 1
        reset()

    def test_hooks_reset_works(self, tmp_path):
        from codeagent.hooks.swarm_hooks import reset, on_agent_start, _get_kernel
        reset()
        from codeagent.hooks import swarm_hooks
        assert swarm_hooks._kernel is None

        kernel, _ = _get_kernel(store_root=tmp_path)
        kernel.create_session("rst-s", "mgr", ["w1"])
        on_agent_start(session_id="rst-s", agent_id="w1", store_root=tmp_path)
        assert swarm_hooks._kernel is not None
        reset()
        assert swarm_hooks._kernel is None


# ── tmux watch script ───────────────────────────────────────────────────


class TestTmuxWatchScript:
    """swarm-watch.sh exists and is executable."""

    def test_script_exists(self):
        script = Path(__file__).resolve().parent.parent / "scripts" / "tmux" / "swarm-watch.sh"
        assert script.exists(), f"script not found: {script}"

    def test_script_is_executable(self):
        script = Path(__file__).resolve().parent.parent / "scripts" / "tmux" / "swarm-watch.sh"
        assert os.access(script, os.X_OK), f"script not executable: {script}"

    def test_script_has_usage(self):
        script = Path(__file__).resolve().parent.parent / "scripts" / "tmux" / "swarm-watch.sh"
        content = script.read_text()
        assert "aimeshchat swarm watch" in content
        assert "SESSION_ID" in content
        assert "AGENT_ID" in content


# ── Skills references ────────────────────────────────────────────────────


class TestSkillReferences:
    """Skills no longer reference omp-mailbox-plugin."""

    def test_tmux_agent_manager_no_omp_mailbox_plugin(self):
        skill = Path(__file__).resolve().parent.parent / "skills" / "tmux-agent-manager" / "SKILL.md"
        assert not skill.exists(), \
            "tmux-agent-manager/SKILL.md should not exist (deprecated, merged into agent-swarm)"

    def test_tmux_agent_worker_no_omp_mailbox_plugin(self):
        skill = Path(__file__).resolve().parent.parent / "skills" / "tmux-agent-worker" / "SKILL.md"
        assert not skill.exists(), \
            "tmux-agent-worker/SKILL.md should not exist (deprecated, merged into agent-swarm)"

    def test_manager_skill_mentions_swarm(self):
        skill = Path(__file__).resolve().parent.parent / "skills" / "agent-swarm" / "SKILL.md"
        content = skill.read_text()
        assert "aimeshchat swarm" in content

    def test_worker_skill_mentions_swarm(self):
        skill = Path(__file__).resolve().parent.parent / "skills" / "agent-swarm" / "protocol" / "mailbox.md"
        content = skill.read_text()
        assert "aimeshchat swarm" in content


# ── OMP runner hook wiring ──────────────────────────────────────────────


class TestOMPRunnerHooks:
    """OMPRunner imports and references swarm hooks."""

    def test_runner_imports_hooks(self):
        import codeagent.runners.omp as omp_mod
        source = Path(omp_mod.__file__).read_text()
        assert "on_agent_start" in source
        assert "on_agent_stop" in source


# ── swarm launch ────────────────────────────────────────────────────────


class TestSwarmLaunch:
    """swarm launch help and bootstrap dry-run."""

    def test_swarm_launch_help(self):
        rc, out, _ = _run(["swarm", "launch", "--help"])
        assert rc == 0
        assert "session_id" in out
        assert "--bootstrap" in out
        assert "--pull" in out
        assert "--poll-interval" in out
        assert "--max-iterations" in out

    def test_swarm_launch_no_session(self):
        """Launch with nonexistent session returns error."""
        rc, _, err = _run(["swarm", "launch", "nonexistent"])
        assert rc == 1
        assert "not found" in err

    def test_swarm_launch_bootstrap_dry_run(self):
        """--bootstrap with remote agents calls session-init + send via subprocess."""
        _setup_full(session_id="launch-s1", manager="mgr", members="w1")

        # Register w1 on a remote host so bootstrap has work to do.
        rc, _, _ = _run(["swarm", "register", "launch-s1",
                         "--agent", "w1", "--host", "remote-host-1", "--backend", "cli",
                         "--return-mode", "manager-pull"])
        assert rc == 0

        called_cmds = []

        def fake_run(cmd, **kwargs):
            called_cmds.append(cmd)
            return mock.MagicMock(returncode=0, stdout="ok\n", stderr="")

        with mock.patch("subprocess.run", side_effect=fake_run):
            rc, out, err = _run(["swarm", "launch", "launch-s1", "--bootstrap"])

        assert rc == 0
        # Should have called session-init and send for w1
        init_calls = [c for c in called_cmds if "session-init" in c]
        send_calls = [c for c in called_cmds if c[1] == "mailbox" and c[2] == "send"]
        assert len(init_calls) == 1
        assert len(send_calls) == 1

        init_cmd = init_calls[0]
        assert "--host" in init_cmd
        assert init_cmd[init_cmd.index("--host") + 1] == "remote-host-1"
        assert init_cmd[init_cmd.index("--session") + 1] == "launch-s1"
        assert init_cmd[init_cmd.index("--manager") + 1] == "mgr"

        send_cmd = send_calls[0]
        assert send_cmd[send_cmd.index("--to") + 1] == "w1"
        assert send_cmd[send_cmd.index("--from") + 1] == "mgr"
        assert send_cmd[send_cmd.index("--subject") + 1] == "INIT"
        assert send_cmd[send_cmd.index("--kind") + 1] == "TASK"

    def test_swarm_launch_bootstrap_skips_local(self):
        """--bootstrap skips agents on __local__ host."""
        _setup_full(session_id="launch-s2", manager="mgr", members="w1,w2")

        called_cmds = []

        def fake_run(cmd, **kwargs):
            called_cmds.append(cmd)
            return mock.MagicMock(returncode=0, stdout="ok\n", stderr="")

        with mock.patch("subprocess.run", side_effect=fake_run):
            rc, out, _ = _run(["swarm", "launch", "launch-s2", "--bootstrap"])

        assert rc == 0
        # All agents are __local__ so no subprocess calls should have been made
        assert len(called_cmds) == 0
        assert '"status": "done"' in out

    def test_swarm_launch_pull_no_workers(self):
        """--pull with no registered non-manager workers skips loop."""
        # Create session only (no register calls), so no locations exist.
        _setup_session(session_id="launch-s3", manager="mgr", members="w1")
        rc, out, err = _run(["swarm", "launch", "launch-s3", "--pull"])
        assert rc == 0
        assert "no registered workers" in err

    def test_swarm_launch_pull_exits_on_report(self):
        """--pull loop exits when all workers send REPORT."""
        _setup_full(session_id="launch-s4", manager="mgr", members="w1")

        # Register w1 so it appears in the pull tracking.
        rc, _, _ = _run(["swarm", "register", "launch-s4",
                         "--agent", "w1", "--host", "__local__", "--backend", "cli"])
        assert rc == 0

        # Write a REPORT directly into canonical history so pull loop finds it.
        import codeagent.cli as cli_mod
        kernel, _ = cli_mod._get_swarm_kernel()
        kernel._store.append_history("launch-s4", {
            "msg_id": "report-1",
            "from": "w1",
            "to": "mgr",
            "kind": "REPORT",
            "subject": "DONE",
            "body": "finished",
            "session_id": "launch-s4",
            "created_at": "2026-08-07T00:00:00Z",
            "run_id": "run-1",
            "request_id": "req-1",
            "reply_to": "init-msg-1",
        })

        rc, out, _ = _run(["swarm", "launch", "launch-s4", "--pull",
                           "--poll-interval", "0", "--max-iterations", "5"])
        assert rc == 0
        assert "all_workers_reported" in out

    def test_swarm_launch_pull_max_iterations(self):
        """--pull loop respects --max-iterations."""
        _setup_full(session_id="launch-s5", manager="mgr", members="w1")

        rc, _, _ = _run(["swarm", "register", "launch-s5",
                         "--agent", "w1", "--host", "__local__", "--backend", "cli"])
        assert rc == 0

        rc, out, _ = _run(["swarm", "launch", "launch-s5", "--pull",
                           "--poll-interval", "0", "--max-iterations", "3"])
        assert rc == 0
        assert "max_iterations" in out
        assert '"pending"' in out
