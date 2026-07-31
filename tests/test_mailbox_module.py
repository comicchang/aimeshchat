"""Acceptance gate: verify mailbox module matches original tools/mailbox behavior."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeagent.mailbox.protocol import Message, StatusSnapshot, validate_agent_id, validate_message
from codeagent.mailbox.store import MailboxStore


@pytest.fixture
def store(tmp_path):
    return MailboxStore(root=tmp_path)


class TestProtocol:
    def test_validate_message_valid(self):
        msg = {
            "session_id": "s1", "from": "mgr", "to": "w1",
            "subject": "hi", "body": "there", "kind": "TASK",
            "msg_id": "mgr_123", "created_at": "2025-01-01T00:00:00Z",
        }
        ok, reason = validate_message(msg)
        assert ok

    def test_validate_message_missing_field(self):
        msg = {"session_id": "s1", "from": "mgr"}
        ok, reason = validate_message(msg)
        assert not ok
        assert "missing fields" in reason

    def test_validate_message_invalid_kind(self):
        msg = {
            "session_id": "s1", "from": "mgr", "to": "w1",
            "subject": "hi", "body": "there", "kind": "INVALID",
            "msg_id": "mgr_123", "created_at": "2025-01-01T00:00:00Z",
        }
        ok, reason = validate_message(msg)
        assert not ok
        assert "invalid kind" in reason

    def test_validate_message_recipient_mismatch(self):
        msg = {
            "session_id": "s1", "from": "mgr", "to": "w1",
            "subject": "hi", "body": "there", "kind": "TASK",
            "msg_id": "mgr_123", "created_at": "2025-01-01T00:00:00Z",
        }
        ok, reason = validate_message(msg, expected_agent="w2")
        assert not ok
        assert "recipient mismatch" in reason

    def test_validate_message_filename_mismatch(self):
        msg = {
            "session_id": "s1", "from": "mgr", "to": "w1",
            "subject": "hi", "body": "there", "kind": "TASK",
            "msg_id": "mgr_123", "created_at": "2025-01-01T00:00:00Z",
        }
        ok, reason = validate_message(msg, filename="other.json")
        assert not ok
        assert "msg_id mismatch" in reason

    def test_validate_message_path_traversal(self):
        msg = {
            "session_id": "s1", "from": "mgr", "to": "w1",
            "subject": "hi", "body": "there", "kind": "TASK",
            "msg_id": "../escape", "created_at": "2025-01-01T00:00:00Z",
        }
        ok, reason = validate_message(msg)
        assert not ok
        assert "path separator" in reason

    def test_validate_agent_id_valid(self):
        validate_agent_id("worker-a")  # should not raise

    def test_validate_agent_id_invalid(self):
        with pytest.raises(ValueError):
            validate_agent_id("../../escape")

    def test_message_roundtrip(self):
        m = Message("s1", "mgr", "w1", "hi", "body", "TASK", "m1", "t1")
        d = m.to_dict()
        m2 = Message.from_dict(d)
        assert m2.session_id == m.session_id
        assert m2.from_id == m.from_id

    def test_status_roundtrip(self):
        s = StatusSnapshot("s1", "BUSY", "working", "prev", "t1")
        d = s.to_dict()
        s2 = StatusSnapshot.from_dict(d)
        assert s2.state == "BUSY"


class TestStore:
    def test_session_init(self, store):
        result = store.session_init("s1", "mgr", ["w1", "w2"])
        assert "created" in result
        assert (store.root / "s1" / "session.json").exists()
        assert (store.root / "s1" / "w1" / "inbox").is_dir()

    def test_session_init_duplicate(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="already exists"):
            store.session_init("s1", "mgr", ["w1"])

    def test_send_and_peek(self, store):
        store.session_init("s1", "mgr", ["w1"])
        result = store.send("s1", "mgr", "w1", "hello", "world", "TASK")
        assert "sent" in result
        peek = store.peek("s1", "w1")
        assert peek["pending"] == 1
        assert peek["messages"][0]["subject"] == "hello"

    def test_send_invalid_kind(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="invalid kind"):
            store.send("s1", "mgr", "w1", "s", "b", "INVALID")

    def test_send_to_nonexistent(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="not in session"):
            store.send("s1", "mgr", "ghost", "s", "b")

    def test_send_empty_subject(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="subject must be non-empty"):
            store.send("s1", "mgr", "w1", "", "b")

    def test_read_consumes(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK")
        msg = store.read("s1", "w1", "w1")
        assert msg is not None
        assert msg["subject"] == "t"
        assert store.peek("s1", "w1")["pending"] == 0

    def test_read_empty(self, store):
        store.session_init("s1", "mgr", ["w1"])
        assert store.read("s1", "w1", "w1") is None

    def test_finalize(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK")
        msg = store.read("s1", "w1", "w1")
        result = store.finalize("s1", "w1", msg["msg_id"], "w1")
        assert "finalized" in result

    def test_release(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK")
        msg = store.read("s1", "w1", "w1")
        result = store.release("s1", "w1", msg["msg_id"], "w1")
        assert "released" in result
        assert store.peek("s1", "w1")["pending"] == 1

    def test_status(self, store):
        store.session_init("s1", "mgr", ["w1"])
        result = store.write_status("s1", "w1", "BUSY", "working")
        assert "BUSY" in result
        status = store.read_status("s1", "w1")
        assert status is not None
        assert status.state == "BUSY"

    def test_stats(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK")
        stats = store.stats("s1", "w1")
        assert stats["inbox"] == 1
        assert stats["processing"] == 0

    def test_clear_archive(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK")
        msg = store.read("s1", "w1", "w1")
        store.finalize("s1", "w1", msg["msg_id"], "w1")
        result = store.clear("s1", "w1")
        assert "cleared 1" in result

    def test_recover_stale_empty(self, store):
        store.session_init("s1", "mgr", ["w1"])
        result = store.recover_stale("s1", "w1")
        assert "0" in result

    def test_path_traversal_blocked(self, store):
        with pytest.raises(ValueError, match="invalid agent"):
            store.session_init("../escape", "mgr", ["w1"])
