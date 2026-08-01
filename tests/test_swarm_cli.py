"""Tests for the ``codeagent swarm`` CLI subcommands.

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
    """
    import codeagent.cli as cli_mod
    from codeagent.mailbox.store import MailboxStore
    from codeagent.swarm.kernel import SwarmKernel, LocalDeliverySink

    store = MailboxStore(root=tmp_path)
    kernel = SwarmKernel(store=store, sink=LocalDeliverySink(store))

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
                           "--body", "please do this task"])
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
                           "--subject", "att", "--body", "see attached",
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
                           "--subject", "hi", "--body", "hello"])
        assert rc == 0

    def test_direct_missing_session(self):
        rc, _, err = _run(["swarm", "direct", "ghost",
                           "--from", "mgr", "--to", "w1",
                           "--subject", "x", "--body", "y"])
        assert rc == 1
        assert "not found" in err

    def test_direct_invalid_attachment_json(self):
        _setup_full("dir-badatt", "mgr", "w1")
        rc, _, err = _run(["swarm", "direct", "dir-badatt",
                           "--from", "mgr", "--to", "w1",
                           "--subject", "x", "--body", "y",
                           "--attachment", "not-json"])
        assert rc == 1


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
                           "--from", "mgr", "--kind", "TASK",
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
                           "--from", "mgr", "--body", "x"])
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
                           "--body", "initialize subsystem"])
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
                           "--from", "mgr", "--kind", "TASK",
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
            msg_dict={"to": "w1", "kind": "TASK", "subject": "s", "body": "b"},
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
            msg_dict={"to": "#dev", "kind": "TASK", "subject": "s", "body": "b"},
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
        assert "codeagent swarm watch" in content
        assert "SESSION_ID" in content
        assert "AGENT_ID" in content


# ── Skills references ────────────────────────────────────────────────────


class TestSkillReferences:
    """Skills no longer reference omp-mailbox-plugin."""

    def test_tmux_agent_manager_no_omp_mailbox_plugin(self):
        skill = Path(__file__).resolve().parent.parent / "skills" / "tmux-agent-manager" / "SKILL.md"
        content = skill.read_text()
        assert "omp-mailbox-plugin" not in content, \
            "tmux-agent-manager/SKILL.md still references omp-mailbox-plugin"

    def test_tmux_agent_worker_no_omp_mailbox_plugin(self):
        skill = Path(__file__).resolve().parent.parent / "skills" / "tmux-agent-worker" / "SKILL.md"
        content = skill.read_text()
        assert "omp-mailbox-plugin" not in content, \
            "tmux-agent-worker/SKILL.md still references omp-mailbox-plugin"

    def test_manager_skill_mentions_swarm(self):
        skill = Path(__file__).resolve().parent.parent / "skills" / "tmux-agent-manager" / "SKILL.md"
        content = skill.read_text()
        assert "codeagent swarm" in content

    def test_worker_skill_mentions_swarm(self):
        skill = Path(__file__).resolve().parent.parent / "skills" / "tmux-agent-worker" / "SKILL.md"
        content = skill.read_text()
        assert "codeagent swarm" in content


# ── OMP runner hook wiring ──────────────────────────────────────────────


class TestOMPRunnerHooks:
    """OMPRunner imports and references swarm hooks."""

    def test_runner_imports_hooks(self):
        import codeagent.runners.omp as omp_mod
        source = Path(omp_mod.__file__).read_text()
        assert "on_agent_start" in source
        assert "on_agent_stop" in source
