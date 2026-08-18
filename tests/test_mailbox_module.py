"""Acceptance gate: verify mailbox module matches original tools/mailbox behavior."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

import pytest

from codeagent.constants import MAX_MAILBOX_BODY
from codeagent.mailbox.protocol import (
    AttachmentRef,
    Message,
    StatusSnapshot,
    validate_agent_id,
    validate_message,
)
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
            "run_id": "run-1", "request_id": "req-1",
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
            "run_id": "run-1", "request_id": "req-1",
        }
        ok, reason = validate_message(msg, expected_agent="w2")
        assert not ok
        assert "recipient mismatch" in reason

    def test_validate_message_filename_mismatch(self):
        msg = {
            "session_id": "s1", "from": "mgr", "to": "w1",
            "subject": "hi", "body": "there", "kind": "TASK",
            "msg_id": "mgr_123", "created_at": "2025-01-01T00:00:00Z",
            "run_id": "run-1", "request_id": "req-1",
        }
        ok, reason = validate_message(msg, filename="other.json")
        assert not ok
        assert "msg_id mismatch" in reason

    def test_validate_message_path_traversal(self):
        msg = {
            "session_id": "s1", "from": "mgr", "to": "w1",
            "subject": "hi", "body": "there", "kind": "TASK",
            "msg_id": "../escape", "created_at": "2025-01-01T00:00:00Z",
            "run_id": "run-1", "request_id": "req-1",
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
        """Idempotent: same roster → merged 0 agents, no error."""
        store.session_init("s1", "mgr", ["w1"])
        result = store.session_init("s1", "mgr", ["w1"])
        assert "merged 0 agents" in result

    def test_send_and_peek(self, store):
        store.session_init("s1", "mgr", ["w1"])
        result = store.send("s1", "mgr", "w1", "hello", "world", "TASK", run_id="run-1", request_id="req-1")
        assert "sent" in result
        peek = store.peek("s1", "w1")
        assert peek["pending"] == 1
        assert peek["messages"][0]["subject"] == "hello"

    def test_send_rejects_oversized_body(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="body exceeds"):
            store.send(
                "s1", "mgr", "w1", "subject", "x" * (MAX_MAILBOX_BODY + 1), "TASK"
            )

    def test_send_invalid_kind(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="invalid kind"):
            store.send("s1", "mgr", "w1", "s", "b", "INVALID")

    def test_send_to_nonexistent(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="recipient not in roster"):
            store.send("s1", "mgr", "ghost", "s", "b")

    def test_send_empty_subject(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="subject must be non-empty"):
            store.send("s1", "mgr", "w1", "", "b")

    def test_read_consumes(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1")
        msg = store.read("s1", "w1", "w1")
        assert msg is not None
        assert msg["subject"] == "t"
        assert store.peek("s1", "w1")["pending"] == 0

    def test_read_empty(self, store):
        store.session_init("s1", "mgr", ["w1"])
        assert store.read("s1", "w1", "w1") is None

    def test_finalize(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1")
        msg = store.read("s1", "w1", "w1")
        result = store.finalize("s1", "w1", msg["msg_id"], "w1")
        assert "finalized" in result

    def test_finalize_from_inbox(self, store):
        """finalize_from_inbox archives directly from inbox (swarm auto-ack)."""
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1")
        # Message is still in inbox (no read() called)
        peek = store.peek("s1", "w1")
        msg_id = peek["messages"][0]["msg_id"]
        result = store.finalize_from_inbox("s1", "w1", msg_id, "w1")
        assert "finalized" in result
        assert store.peek("s1", "w1")["pending"] == 0

    def test_finalize_from_inbox_processing(self, store):
        """finalize_from_inbox also archives messages already in processing."""
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1")
        msg = store.read("s1", "w1", "w1")  # inbox → processing
        assert msg is not None
        result = store.finalize_from_inbox("s1", "w1", msg["msg_id"], "w1")
        assert "finalized" in result
        assert store.peek("s1", "w1")["pending"] == 0

    def test_release(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1")
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
        store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1")
        stats = store.stats("s1", "w1")
        assert stats["inbox"] == 1
        assert stats["processing"] == 0

    def test_clear_archive(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1")
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


# ── TestResolveRoot ──────────────────────────────────────────────────────


class TestResolveRoot:
    def test_default_fallback(self, monkeypatch):
        from codeagent.mailbox.store import resolve_root

        monkeypatch.delenv("MAILBOX_ROOT", raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        assert resolve_root() == Path.home() / ".local" / "share" / "aimeshchat" / "mailbox"

    def test_env_override(self, tmp_path, monkeypatch):
        from codeagent.mailbox.store import resolve_root

        monkeypatch.setenv("MAILBOX_ROOT", str(tmp_path))
        assert resolve_root() == tmp_path

    def test_xdg_data_home_override(self, tmp_path, monkeypatch):
        from codeagent.mailbox.store import resolve_root

        monkeypatch.delenv("MAILBOX_ROOT", raising=False)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        assert resolve_root() == tmp_path / "aimeshchat" / "mailbox"


# ── TestReadSession ──────────────────────────────────────────────────────


class TestReadSession:
    def test_missing_returns_none(self, store):
        assert store.read_session("s1") is None

    def test_corrupt_returns_none(self, store):
        (store.root / "s1").mkdir()
        (store.root / "s1" / "session.json").write_text("{not json")
        assert store.read_session("s1") is None


# ── TestSendErrors ───────────────────────────────────────────────────────


class TestSendErrors:
    def test_session_not_found(self, store):
        with pytest.raises(ValueError, match="session not found"):
            store.send("nosuch", "mgr", "w1", "s", "b")

    def test_sender_not_in_roster(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="sender not in roster"):
            store.send("s1", "ghost", "w1", "s", "b")

    def test_corrupt_metadata(self, store):
        (store.root / "s1").mkdir()
        (store.root / "s1" / "session.json").write_text("{not json")
        with pytest.raises(ValueError, match="metadata not found or corrupt"):
            store.send("s1", "mgr", "w1", "s", "b")

    def test_recipient_inbox_missing(self, store):
        store.session_init("s1", "mgr", ["w1"])
        import shutil

        shutil.rmtree(store.agent_subdir("s1", "w1", "inbox"))
        with pytest.raises(ValueError, match="agent not in session"):
            store.send("s1", "mgr", "w1", "s", "b")

    def test_msg_id_collision_retries(self, store):
        store.session_init("s1", "mgr", ["w1"])
        inbox = store.agent_subdir("s1", "w1", "inbox")
        (inbox / "dup.json").write_text('{}')
        with mock.patch("codeagent.mailbox.store.gen_msg_id", side_effect=["dup", "dup2"]):
            result = store.send("s1", "mgr", "w1", "s", "b", "TASK", run_id="run-1", request_id="req-1")
        assert "dup2.json" in result
        assert (inbox / "dup.json").exists()


# ── TestPeekErrors ───────────────────────────────────────────────────────


class TestPeekErrors:
    def test_missing_inbox_dir(self, store):
        import shutil

        store.session_init("s1", "mgr", ["w1"])
        shutil.rmtree(store.agent_subdir("s1", "w1", "inbox"))
        assert store.peek("s1", "w1") == {"pending": 0, "messages": []}

    def test_unreadable_message(self, store):
        store.session_init("s1", "mgr", ["w1"])
        inbox = store.agent_subdir("s1", "w1", "inbox")
        (inbox / "mgr_1.json").write_text("{not json")
        peek = store.peek("s1", "w1")
        assert peek["pending"] == 1
        assert peek["messages"][0]["subject"] == "(unreadable)"


# ── TestReadCorruption ───────────────────────────────────────────────────


class TestReadCorruption:
    def test_corrupt_message_moved_to_corrupt(self, store):
        store.session_init("s1", "mgr", ["w1"])
        inbox = store.agent_subdir("s1", "w1", "inbox")
        (inbox / "mgr_bad.json").write_text("{not json")
        assert store.read("s1", "w1", "w1") is None
        corrupt = store.agent_subdir("s1", "w1", "_corrupt")
        assert list(corrupt.glob("*.json")) == [corrupt / "mgr_bad.json"]

    def test_invalid_message_moved_to_corrupt(self, store):
        store.session_init("s1", "mgr", ["w1"])
        inbox = store.agent_subdir("s1", "w1", "inbox")
        (inbox / "mgr_1.json").write_text(json.dumps({
            "session_id": "other-session", "from": "mgr", "to": "w1",
            "subject": "hi", "body": "there", "kind": "TASK",
            "msg_id": "mgr_1", "created_at": "2025-01-01T00:00:00Z",
        }))
        assert store.read("s1", "w1", "w1") is None
        corrupt = store.agent_subdir("s1", "w1", "_corrupt")
        assert (corrupt / "mgr_1.json").exists()

    def test_claim_collision_retries(self, store):
        store.session_init("s1", "mgr", ["w1"])
        sent = store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1")
        msg_id = sent.split("/")[-1].replace(".json", "")
        processing = store.agent_subdir("s1", "w1", "processing")
        processing.mkdir(parents=True, exist_ok=True)
        (processing / f".tmp-claim-{msg_id}-w1-1234.json").touch()

        with mock.patch("random.randint", return_value=1234):
            msg = store.read("s1", "w1", "w1")
        assert msg is not None
        assert msg["subject"] == "t"
        assert store.peek("s1", "w1")["pending"] == 0

    def test_replace_oserror_retries(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1")

        real_replace = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("race lost")
            return real_replace(src, dst)

        with mock.patch("os.replace", flaky):
            msg = store.read("s1", "w1", "w1")
        assert msg is not None
        assert calls["n"] >= 2


# ── TestFinalizeErrors ───────────────────────────────────────────────────


class TestFinalizeErrors:
    def _read_one(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1")
        return store.read("s1", "w1", "w1")

    def test_invalid_msg_id(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="invalid msg_id"):
            store.finalize("s1", "w1", "../escape", "w1")

    def test_no_claim(self, store):
        msg = self._read_one(store)
        processing = store.agent_subdir("s1", "w1", "processing")
        for cf in processing.glob(".claim-*.json"):
            cf.unlink()
        with pytest.raises(ValueError, match="no claim file"):
            store.finalize("s1", "w1", msg["msg_id"], "w1")

    def test_multiple_claims(self, store):
        msg = self._read_one(store)
        processing = store.agent_subdir("s1", "w1", "processing")
        (processing / f".claim-{msg['msg_id']}-w2.json").write_text(
            json.dumps({"owner": "w2", "msg_id": msg["msg_id"]}))
        with pytest.raises(ValueError, match="multiple claim files"):
            store.finalize("s1", "w1", msg["msg_id"], "w1")

    def test_msg_not_in_processing(self, store):
        msg = self._read_one(store)
        processing = store.agent_subdir("s1", "w1", "processing")
        (processing / f"{msg['msg_id']}.json").unlink()
        with pytest.raises(ValueError, match="msg not in processing"):
            store.finalize("s1", "w1", msg["msg_id"], "w1")

    def test_owner_mismatch(self, store):
        msg = self._read_one(store)
        processing = store.agent_subdir("s1", "w1", "processing")
        for cf in processing.glob(".claim-*.json"):
            cf.unlink()
        (processing / f".claim-{msg['msg_id']}-other.json").write_text(
            json.dumps({"owner": "other", "msg_id": msg["msg_id"]}))
        with pytest.raises(ValueError, match="owner mismatch"):
            store.finalize("s1", "w1", msg["msg_id"], "w1")


# ── TestReleaseErrors ────────────────────────────────────────────────────


class TestReleaseErrors:
    def test_msg_not_in_processing(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="msg not found in processing"):
            store.release("s1", "w1", "mgr_1", "w1")

    def test_multiple_claims(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1")
        msg = store.read("s1", "w1", "w1")
        processing = store.agent_subdir("s1", "w1", "processing")
        (processing / f".claim-{msg['msg_id']}-w2.json").write_text(
            json.dumps({"owner": "w2", "msg_id": msg["msg_id"]}))
        with pytest.raises(ValueError, match="multiple claim files"):
            store.release("s1", "w1", msg["msg_id"], "w1")

    def test_owner_mismatch(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1")
        msg = store.read("s1", "w1", "w1")
        processing = store.agent_subdir("s1", "w1", "processing")
        for cf in processing.glob(".claim-*.json"):
            cf.unlink()
        (processing / f".claim-{msg['msg_id']}-other.json").write_text(
            json.dumps({"owner": "other", "msg_id": msg["msg_id"]}))
        with pytest.raises(ValueError, match="owner mismatch"):
            store.release("s1", "w1", msg["msg_id"], "w1")


# ── TestRecoverStale ─────────────────────────────────────────────────────


class TestRecoverStale:
    def test_no_processing_dir(self, store):
        import shutil

        store.session_init("s1", "mgr", ["w1"])
        shutil.rmtree(store.agent_subdir("s1", "w1", "processing"))
        result = store.recover_stale("s1", "w1")
        assert "no processing/ directory" in result

    def test_recovers_expired_claim(self, store):
        from codeagent.constants import LEASE_TIMEOUT_S
        from datetime import datetime, timezone, timedelta

        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1")
        msg = store.read("s1", "w1", "w1")
        processing = store.agent_subdir("s1", "w1", "processing")
        old = (datetime.now(timezone.utc) - timedelta(seconds=2 * LEASE_TIMEOUT_S))
        for cf in processing.glob(".claim-*.json"):
            cf.write_text(json.dumps({
                "owner": "w1",
                "claimed_at": old.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "msg_id": msg["msg_id"],
            }))

        result = store.recover_stale("s1", "w1")
        assert "recovered 1" in result
        assert store.peek("s1", "w1")["pending"] == 1
        assert list(processing.glob(".claim-*.json")) == []

    def test_fresh_claim_not_recovered(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1")
        store.read("s1", "w1", "w1")
        result = store.recover_stale("s1", "w1")
        assert "recovered 0" in result

    def test_corrupt_claim_skipped(self, store):
        store.session_init("s1", "mgr", ["w1"])
        processing = store.agent_subdir("s1", "w1", "processing")
        processing.mkdir(parents=True, exist_ok=True)
        (processing / ".claim-mgr_1-w1.json").write_text("{bad json")
        result = store.recover_stale("s1", "w1")
        assert "recovered 0" in result

    def test_stale_claim_without_msg_file(self, store):
        from codeagent.constants import LEASE_TIMEOUT_S
        from datetime import datetime, timezone, timedelta

        store.session_init("s1", "mgr", ["w1"])
        processing = store.agent_subdir("s1", "w1", "processing")
        processing.mkdir(parents=True, exist_ok=True)
        old = (datetime.now(timezone.utc) - timedelta(seconds=2 * LEASE_TIMEOUT_S))
        (processing / ".claim-mgr_1-w1.json").write_text(json.dumps({
            "owner": "w1",
            "claimed_at": old.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "msg_id": "mgr_1",
        }))
        result = store.recover_stale("s1", "w1")
        assert "recovered 0" in result


# ── TestWriteStatusErrors ────────────────────────────────────────────────


class TestWriteStatusErrors:
    def test_invalid_state(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="invalid state"):
            store.write_status("s1", "w1", "NOPE")

    def test_session_not_found(self, store):
        with pytest.raises(ValueError, match="session not found"):
            store.write_status("s1", "w1", "BUSY")

    def test_agent_not_in_session(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="agent not in session"):
            store.write_status("s1", "ghost", "BUSY")


# ── TestReadStatusInvalid ────────────────────────────────────────────────


class TestReadStatusInvalid:
    def test_missing_file(self, store):
        store.session_init("s1", "mgr", ["w1"])
        assert store.read_status("s1", "w1") is None

    def _write(self, store, payload):
        ad = store.agent_dir("s1", "w1")
        ad.mkdir(parents=True, exist_ok=True)
        (ad / "status.json").write_text(json.dumps(payload))

    def test_wrong_keys(self, store):
        self._write(store, {"session_id": "s1", "state": "BUSY"})
        assert store.read_status("s1", "w1") is None

    def test_non_string_value(self, store):
        self._write(store, {
            "session_id": "s1", "state": "BUSY", "current_task": 5,
            "last_conclusion": "", "updated_at": "t",
        })
        assert store.read_status("s1", "w1") is None

    def test_invalid_state(self, store):
        self._write(store, {
            "session_id": "s1", "state": "NOPE", "current_task": "",
            "last_conclusion": "", "updated_at": "t",
        })
        assert store.read_status("s1", "w1") is None

    def test_wrong_session(self, store):
        self._write(store, {
            "session_id": "other", "state": "BUSY", "current_task": "",
            "last_conclusion": "", "updated_at": "t",
        })
        assert store.read_status("s1", "w1") is None

    def test_corrupt_json(self, store):
        ad = store.agent_dir("s1", "w1")
        ad.mkdir(parents=True, exist_ok=True)
        (ad / "status.json").write_text("{not json")
        assert store.read_status("s1", "w1") is None


# ── TestClearPurge ───────────────────────────────────────────────────────


class TestClearPurge:
    def test_clear_prune_stale(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1")
        msg = store.read("s1", "w1", "w1")
        store.finalize("s1", "w1", msg["msg_id"], "w1")
        result = store.clear("s1", "w1", prune_stale=True)
        assert "cleared 1" in result

    def test_purge_corrupt(self, store):
        store.session_init("s1", "mgr", ["w1"])
        corrupt = store.agent_subdir("s1", "w1", "_corrupt")
        corrupt.mkdir(parents=True, exist_ok=True)
        (corrupt / "a.json").write_text("{}")
        (corrupt / "b.json").write_text("{}")
        result = store.purge("s1", "w1")
        assert "purged 2" in result
        assert list(corrupt.glob("*.json")) == []

    def test_purge_empty(self, store):
        store.session_init("s1", "mgr", ["w1"])
        result = store.purge("s1", "w1")
        assert "purged 0" in result


# ── TestCheckLegacy ──────────────────────────────────────────────────────


class TestCheckLegacy:
    def test_no_messages(self, store):
        store.session_init("s1", "mgr", ["w1"])
        assert store.check("s1", "w1") == []

    def test_valid_message_archived(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1")
        results = store.check("s1", "w1")
        assert len(results) == 1
        assert results[0]["subject"] == "t"
        archive = store.agent_subdir("s1", "w1", "archive")
        assert len(list(archive.glob("*.json"))) == 1

    def test_max_messages_limit(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t1", "b", "TASK", run_id="run-1", request_id="req-1")
        store.send("s1", "mgr", "w1", "t2", "b", "TASK", run_id="run-1", request_id="req-1")
        results = store.check("s1", "w1", max_messages=1)
        assert len(results) == 1

    def test_invalid_message_moved_to_corrupt(self, store):
        store.session_init("s1", "mgr", ["w1"])
        inbox = store.agent_subdir("s1", "w1", "inbox")
        (inbox / "mgr_1.json").write_text(json.dumps({
            "session_id": "s1", "from": "mgr", "to": "w1",
            "subject": "hi", "body": "there", "kind": "TASK",
            "msg_id": "different-id", "created_at": "2025-01-01T00:00:00Z",
        }))
        results = store.check("s1", "w1")
        assert results == []
        corrupt = store.agent_subdir("s1", "w1", "_corrupt")
        assert (corrupt / "mgr_1.json").exists()


# ── TestConcurrentRead ───────────────────────────────────────────────────


class TestConcurrentRead:
    def test_claim_race_single_winner(self, store):
        """Two owners reading the same message — exactly one wins."""
        import threading
        from concurrent.futures import ThreadPoolExecutor

        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1")

        barrier = threading.Barrier(2)

        def reader(owner):
            barrier.wait()
            return store.read("s1", "w1", owner)

        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = [ex.submit(reader, "w1"), ex.submit(reader, "w2")]
            results = [f.result() for f in futs]

        winners = [r for r in results if r is not None]
        assert len(winners) == 1
        assert store.peek("s1", "w1")["pending"] == 0
        # The message ended up in processing under its msg_id
        processing = store.agent_subdir("s1", "w1", "processing")
        assert (processing / f"{winners[0]['msg_id']}.json").exists()


# ── TestBroadcast ────────────────────────────────────────────────────────


class TestBroadcast:
    def test_broadcast_normal(self, store):
        store.session_init("s1", "mgr", ["w1", "w2", "w3"])
        result = store.send("s1", "mgr", "*", "hi", "all", "NOTICE")
        assert "broadcast → 3 recipients" in result
        for aid in ("w1", "w2", "w3"):
            peek = store.peek("s1", aid)
            assert peek["pending"] == 1
            assert peek["messages"][0]["subject"] == "hi"
        # The sender does not receive a copy of its own broadcast
        assert store.peek("s1", "mgr")["pending"] == 0

    def test_broadcast_same_msg_id(self, store):
        store.session_init("s1", "mgr", ["w1", "w2"])
        store.send("s1", "mgr", "*", "hi", "all", "NOTICE")
        msgs1 = store.list_messages(store.agent_subdir("s1", "w1", "inbox"))
        msgs2 = store.list_messages(store.agent_subdir("s1", "w2", "inbox"))
        assert len(msgs1) == len(msgs2) == 1
        assert msgs1[0].stem == msgs2[0].stem

    def test_broadcast_empty_roster(self, store):
        store.session_init("s1", "mgr", [])
        result = store.send("s1", "mgr", "*", "hi", "all", "NOTICE")
        assert "broadcast → 0 recipients" in result

    def test_broadcast_partial_failure_writes_nothing(self, store):
        import shutil

        store.session_init("s1", "mgr", ["w1", "w2"])
        shutil.rmtree(store.agent_subdir("s1", "w2", "inbox"))
        with pytest.raises(ValueError, match="agent not in session"):
            store.send("s1", "mgr", "*", "hi", "all", "NOTICE")
        # Every recipient is validated before anything is written
        assert store.peek("s1", "w1")["pending"] == 0
        assert store.read_history("s1") == []

    def test_broadcast_from_worker(self, store):
        store.session_init("s1", "mgr", ["w1", "w2"])
        result = store.send("s1", "w1", "*", "hi", "all", "NOTICE")
        assert "broadcast → 2 recipients" in result
        assert store.peek("s1", "w1")["pending"] == 0  # sender excluded
        assert store.peek("s1", "w2")["pending"] == 1
        assert store.peek("s1", "mgr")["pending"] == 1

    def test_broadcast_readable_by_recipients(self, store):
        store.session_init("s1", "mgr", ["w1", "w2"])
        store.send("s1", "mgr", "*", "hi", "all", "NOTICE")
        msg = store.read("s1", "w2", "w2")
        assert msg is not None
        assert msg["to"] == "*"
        assert msg["subject"] == "hi"


# ── TestAttachments ──────────────────────────────────────────────────────


class TestAttachments:
    @staticmethod
    def _ref(**over):
        base = {
            "artifact_id": "art-1", "source_host": "worker-1",
            "remote_root": "/tmp/artifacts", "relative_path": "out/result.json",
            "size": 42, "sha256": "a" * 64,
        }
        base.update(over)
        return AttachmentRef(**base)

    def test_send_with_attachments(self, store):
        store.session_init("s1", "mgr", ["w1"])
        refs = [
            self._ref(),
            AttachmentRef("art-2", "worker-1", "/tmp/a", "b/c.txt", 7, "b" * 64, "text/plain"),
        ]
        store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1", attachments=refs)
        msg = store.read("s1", "w1", "w1")
        atts = msg["attachments"]
        assert len(atts) == 2
        assert atts[0]["sha256"] == "a" * 64
        assert atts[0]["media_type"] == "application/octet-stream"
        assert atts[1]["relative_path"] == "b/c.txt"
        assert atts[1]["media_type"] == "text/plain"

    def test_broadcast_with_attachments(self, store):
        store.session_init("s1", "mgr", ["w1", "w2"])
        store.send("s1", "mgr", "*", "t", "b", "TASK", run_id="run-1", request_id="req-1", attachments=[self._ref()])
        for aid in ("w1", "w2"):
            msg = store.read("s1", aid, aid)
            assert msg["attachments"][0]["artifact_id"] == "art-1"

    def test_send_rejects_bad_sha256(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="sha256"):
            store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1",
                       attachments=[self._ref(sha256="xyz")])

    def test_send_rejects_short_sha256(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="sha256"):
            store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1",
                       attachments=[self._ref(sha256="a" * 63)])

    def test_send_rejects_negative_size(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="size"):
            store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1",
                       attachments=[self._ref(size=-1)])

    def test_send_rejects_empty_artifact_id(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="artifact_id"):
            store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1",
                       attachments=[self._ref(artifact_id="")])

    def test_send_rejects_path_traversal(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="relative_path"):
            store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1",
                       attachments=[self._ref(relative_path="../escape")])

    def test_send_rejects_non_list(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="attachments must be a list"):
            store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1", attachments="nope")

    def test_send_rejects_non_dict_item(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="attachment"):
            store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1", attachments=[42])

    def test_attachment_ref_roundtrip(self):
        r = self._ref()
        assert AttachmentRef.from_dict(r.to_dict()) == r

    def test_message_attachments_roundtrip(self):
        m = Message("s1", "mgr", "w1", "s", "b", "TASK", "m1", "t1",
                    attachments=[self._ref()])
        m2 = Message.from_dict(m.to_dict())
        assert m2.attachments == m.attachments

    def test_validate_message_rejects_bad_attachments(self):
        msg = {
            "session_id": "s1", "from": "mgr", "to": "w1",
            "subject": "hi", "body": "there", "kind": "TASK",
            "msg_id": "mgr_123", "created_at": "2025-01-01T00:00:00Z",
            "run_id": "run-1", "request_id": "req-1",
            "attachments": [{"artifact_id": "", "source_host": "h",
                              "remote_root": "/r", "relative_path": "p",
                              "size": 1, "sha256": "a" * 64}],
        }
        ok, reason = validate_message(msg)
        assert not ok
        assert "artifact_id" in reason

    def test_validate_message_rejects_non_list_attachments(self):
        msg = {
            "session_id": "s1", "from": "mgr", "to": "w1",
            "subject": "hi", "body": "there", "kind": "TASK",
            "msg_id": "mgr_123", "created_at": "2025-01-01T00:00:00Z",
            "run_id": "run-1", "request_id": "req-1",
            "attachments": "not-a-list",
        }
        ok, reason = validate_message(msg)
        assert not ok
        assert "attachments must be a list" in reason


# ── TestHistory ──────────────────────────────────────────────────────────


class TestHistory:
    def test_send_appends_history(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1")
        h = store.read_history("s1")
        assert len(h) == 1
        assert h[0]["subject"] == "t"
        assert (store.history_dir("s1") / f"{h[0]['msg_id']}.json").exists()

    def test_broadcast_single_history_entry(self, store):
        store.session_init("s1", "mgr", ["w1", "w2", "w3"])
        store.send("s1", "mgr", "*", "hi", "all", "NOTICE")
        h = store.read_history("s1")
        assert len(h) == 1
        assert h[0]["to"] == "*"

    def test_history_independent_of_archive(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1")
        msg = store.read("s1", "w1", "w1")
        store.finalize("s1", "w1", msg["msg_id"], "w1")
        store.clear("s1", "w1")  # wipes the per-recipient archive only
        assert store.read_history("s1")[0]["subject"] == "t"

    def test_history_filters(self, store):
        from datetime import datetime, timedelta, timezone

        store.session_init("s1", "mgr", ["w1"])
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i, kind in enumerate(["TASK", "PROGRESS", "TASK"]):
            msg = Message("s1", "mgr", "w1", f"t{i}", "b", kind, f"m{i}",
                          (base + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                          run_id="run-1", request_id="req-1")
            store.append_history("s1", msg.to_dict())
        h = store.read_history("s1")
        assert [m["subject"] for m in h] == ["t2", "t1", "t0"]  # newest first
        assert [m["subject"] for m in store.read_history("s1", kind="TASK")] == ["t2", "t0"]
        assert [m["subject"] for m in store.read_history("s1", since="2026-01-01T00:00:01Z")] == ["t2", "t1"]
        assert [m["subject"] for m in store.read_history("s1", before="2026-01-01T00:00:01Z")] == ["t0"]
        assert len(store.read_history("s1", from_id="mgr", limit=2)) == 2
        assert store.read_history("s1", from_id="ghost") == []

    def test_history_append_only(self, store):
        store.session_init("s1", "mgr", ["w1"])
        msg = Message("s1", "mgr", "w1", "t", "b", "TASK", "m1",
                      "2026-01-01T00:00:00Z", run_id="run-1", request_id="req-1").to_dict()
        store.append_history("s1", msg)
        with pytest.raises(ValueError, match="already exists"):
            store.append_history("s1", msg)

    def test_history_session_not_found(self, store):
        with pytest.raises(ValueError, match="session not found"):
            store.append_history("nosuch", {"msg_id": "m1"})

    def test_history_rejects_invalid_message(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="validation failed"):
            store.append_history("s1", {"msg_id": "m1", "kind": "NOPE"})

    def test_history_missing_dir_returns_empty(self, store):
        store.session_init("s1", "mgr", ["w1"])
        assert store.read_history("s1") == []

    def test_history_skips_corrupt_entry(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1")
        hd = store.history_dir("s1")
        (hd / "junk.json").write_text("{not json")
        assert len(store.read_history("s1")) == 1


# ── Idempotent session-init (B3T3) ────────────────────────────────────


class TestSessionInitIdempotent:
    """B3T3: session_init merges new agents, no duplicates."""

    def test_session_init_merges_new_agents(self, store):
        """Existing session + new roster → merges new agents with subdirs."""
        store.session_init("s1", "mgr", ["w1", "w2"])
        result = store.session_init("s1", "mgr", ["w2", "w3"])
        assert "merged 1 agents" in result

        # w3 subdirs should exist
        assert (store.root / "s1" / "w3" / "inbox").is_dir()
        assert (store.root / "s1" / "w3" / "processing").is_dir()

        # session.json should contain merged roster
        meta = store.read_session("s1")
        assert meta is not None
        assert sorted(meta["agents"]) == ["w1", "w2", "w3"]

    def test_session_init_no_duplicate_agents(self, store):
        """Repeated session_init same roster → merged 0, no duplicate agents."""
        store.session_init("s1", "mgr", ["w1", "w2"])
        result = store.session_init("s1", "mgr", ["w1", "w2"])
        assert "merged 0 agents" in result

        meta = store.read_session("s1")
        assert meta is not None
        assert sorted(meta["agents"]) == ["w1", "w2"]
        # w1 and w2 subdirs still exist (not duplicated)
        assert (store.root / "s1" / "w1" / "inbox").is_dir()
        assert (store.root / "s1" / "w2" / "inbox").is_dir()

    def test_session_init_rejects_manager_conflict(self, store):
        """session_init with different manager_id MUST reject, not silently merge."""
        store.session_init("s1", "mgr", ["w1", "w2"])

        with pytest.raises(ValueError, match="already has manager.*mgr.*cannot reassign.*other"):
            store.session_init("s1", "other", ["w1", "w3"])

        # Original session unchanged — no agents from the rejected call
        meta = store.read_session("s1")
        assert meta is not None
        assert meta["manager"] == "mgr"
        assert sorted(meta["agents"]) == ["w1", "w2"]
        assert not (store.root / "s1" / "w3" / "inbox").is_dir()


# ── TestStoreUncoveredPaths ──────────────────────────────────────────────


class TestStoreUncoveredPaths:
    """Covers previously-uncovered store.py paths: helper best-effort branches,
    send/read error paths (invalid msg_id, corrupt inbox), atomic-write races
    (claim publish, history O_EXCL), archive/clean edges, two-phase claim
    failure handling, and RequestLedger edge cases."""

    @staticmethod
    def _old_ts():
        from datetime import datetime, timedelta, timezone
        from codeagent.constants import LEASE_TIMEOUT_S

        return (datetime.now(timezone.utc) - timedelta(seconds=2 * LEASE_TIMEOUT_S)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _now_ts():
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _send_one(store, session="s1", to="w1", subject="t", kind="TASK"):
        store.session_init(session, "mgr", ["w1"])
        store.send(session, "mgr", to, subject, "b", kind, run_id="run-1", request_id="req-1")
        return store.peek(session, to)["messages"][0]["msg_id"]

    @staticmethod
    def _write_status(store, payload):
        ad = store.agent_dir("s1", "w1")
        ad.mkdir(parents=True, exist_ok=True)
        (ad / "status.json").write_text(json.dumps(payload))

    @staticmethod
    def _age_session(store, sid, days):
        from datetime import datetime, timedelta, timezone

        meta = store.read_session(sid)
        meta["created_at"] = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        (store.root / sid / "session.json").write_text(json.dumps(meta))

    @staticmethod
    def _ledger(store):
        from codeagent.mailbox.store import RequestLedger

        store.session_init("s1", "mgr", ["w1"])
        return RequestLedger(store.root / "s1", "w1")

    # ── module-level helpers: best-effort branches ──

    def test_mkdir_chmod_oserror_best_effort(self, store):
        from codeagent.mailbox.store import _chmod_0600, _mkdir_0700

        p = store.root / "sub"
        with mock.patch("codeagent.mailbox.store.os.chmod", side_effect=OSError("read-only")):
            _mkdir_0700(p)
        assert p.is_dir()
        with mock.patch("codeagent.mailbox.store.os.chmod", side_effect=OSError("read-only")):
            _chmod_0600(p)

    def test_attachments_eq_normalization(self):
        from codeagent.mailbox.store import _attachments_eq

        # dict refs in different key order / list order
        a = [{"artifact_id": "a1", "size": 1}, {"artifact_id": "a2", "size": 2}]
        b = [{"size": 2, "artifact_id": "a2"}, {"artifact_id": "a1", "size": 1}]
        assert _attachments_eq(a, b)
        # AttachmentRef objects vs dicts
        ref = AttachmentRef("art-1", "worker-1", "/tmp/r", "p", 1, "a" * 64)
        assert _attachments_eq([ref], [ref.to_dict()])
        # plain (non-dict) refs
        assert _attachments_eq(["x", "y"], ["y", "x"])
        assert not _attachments_eq(["x"], ["y"])
        assert not _attachments_eq([{"artifact_id": "a1"}], [{"artifact_id": "a2"}])

    def test_park_registry_import_fallback(self):
        import importlib
        import sys

        import codeagent.mailbox.store as st

        with mock.patch.dict(sys.modules, {"codeagent.park.registry": None}):
            importlib.reload(st)
            assert st.ParkRegistry is None
        importlib.reload(st)
        assert st.ParkRegistry is not None

    def test_fsync_dir_oserror_branches(self, store):
        p = store.root / "s1"
        p.mkdir(parents=True)
        with mock.patch("codeagent.mailbox.store.os.open", side_effect=OSError("gone")):
            MailboxStore._fsync_dir(p)  # dir not openable → early return
        with mock.patch("codeagent.mailbox.store.os.fsync", side_effect=OSError("nofsync")):
            MailboxStore._fsync_dir(p)  # fsync refused → best effort
        MailboxStore._fsync_dir(p)      # success path

    def test_claim_lock_degrade_branches(self, store):
        store.session_init("s1", "mgr", ["w1"])
        processing = store.agent_subdir("s1", "w1", "processing")
        # lock file cannot be created → proceed without exclusion
        with mock.patch("codeagent.mailbox.store.os.open", side_effect=OSError("nofd")):
            with MailboxStore._claim_lock(processing, "m1", "w1"):
                pass
        # flock unavailable on acquire
        with mock.patch("codeagent.mailbox.store.fcntl.flock", side_effect=OSError("noflock")):
            with MailboxStore._claim_lock(processing, "m1", "w1"):
                pass
        # flock unavailable on release
        calls = {"n": 0}

        def flaky(*_a, **_kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("unlock failed")

        with mock.patch("codeagent.mailbox.store.fcntl.flock", side_effect=flaky):
            with MailboxStore._claim_lock(processing, "m1", "w1"):
                pass
        assert calls["n"] == 2

    def test_session_init_flock_degrade(self, store):
        with mock.patch("codeagent.mailbox.store.fcntl.flock", side_effect=OSError("noflock")):
            result = store.session_init("s1", "mgr", ["w1"])
        assert "created" in result
        assert store.read_session("s1") is not None

    def test_advance_cursor_flock_degrade(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with mock.patch("codeagent.mailbox.store.fcntl.flock", side_effect=OSError("noflock")):
            cur = store.advance_cursor("s1")
        assert "/" in cur
        with mock.patch("codeagent.mailbox.store.fcntl.flock", side_effect=OSError("noflock")):
            cur2 = store.advance_cursor("s1")
        assert "/" in cur2

    # ── park lease guard ──

    def test_check_park_lease_no_registry(self, store, monkeypatch):
        import codeagent.mailbox.store as st

        monkeypatch.setattr(st, "ParkRegistry", None)
        store._check_park_lease("s1", "w1")

    def test_check_park_lease_active_raises(self, store, monkeypatch):
        import codeagent.mailbox.store as st
        from codeagent.domain.park import Lifecycle
        from codeagent.mailbox.store import ParkLeaseActiveError

        class FakeManifest:
            lifecycle = Lifecycle.HOT_PARKED

        class FakeRegistry:
            def lookup_by_field(self, field, value):
                return FakeManifest()

        monkeypatch.setattr(st, "ParkRegistry", FakeRegistry)
        with pytest.raises(ParkLeaseActiveError, match="active park lease"):
            store._check_park_lease("s1", "w1")

    def test_check_park_lease_registry_error_ignored(self, store, monkeypatch):
        import codeagent.mailbox.store as st

        class BrokenRegistry:
            def lookup_by_field(self, field, value):
                raise RuntimeError("registry corrupt")

        monkeypatch.setattr(st, "ParkRegistry", BrokenRegistry)
        store._check_park_lease("s1", "w1")

    # ── list_messages filtering ──

    def test_list_messages_skips_symlink_tmp_conflict(self, store):
        store.session_init("s1", "mgr", ["w1"])
        inbox = store.agent_subdir("s1", "w1", "inbox")
        (inbox / "real.json").write_text("{}")
        os.symlink("real.json", inbox / "link.json")
        (inbox / ".tmp-x.json").write_text("{}")
        (inbox / ".sync-conflict-1.json").write_text("{}")
        assert [f.name for f in store.list_messages(inbox)] == ["real.json"]

    def test_list_messages_stat_oserror_skipped(self, store):
        store.session_init("s1", "mgr", ["w1"])
        inbox = store.agent_subdir("s1", "w1", "inbox")
        (inbox / "a.json").write_text("{}")
        (inbox / "b.json").write_text("{}")
        real_stat = Path.stat
        calls = {}

        def flaky(self, *a, **kw):
            # call 1 = is_file, call 2 = is_symlink (lstat→stat on 3.13),
            # call 3 = the mtime stat inside list_messages
            calls[self.name] = calls.get(self.name, 0) + 1
            if self.name == "b.json" and calls[self.name] == 3:
                raise OSError("vanished mid-scan")
            return real_stat(self, *a, **kw)

        with mock.patch.object(Path, "stat", flaky):
            files = store.list_messages(inbox)
        assert [f.name for f in files] == ["a.json"]

    # ── send: idempotent replay / error paths ──

    def test_send_replay_idempotent(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK", msg_id="mgr_1",
                   run_id="run-1", request_id="req-1")
        r2 = store.send("s1", "mgr", "w1", "t", "b", "TASK", msg_id="mgr_1",
                        run_id="run-1", request_id="req-1")
        assert "idempotent replay" in r2 and "backfilled 0" in r2

    def test_send_replay_conflicting_payload_raises(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK", msg_id="mgr_1",
                   run_id="run-1", request_id="req-1")
        with pytest.raises(ValueError, match="already exists with different payload"):
            store.send("s1", "mgr", "w1", "DIFFERENT", "b", "TASK", msg_id="mgr_1",
                       run_id="run-1", request_id="req-1")

    def test_send_replay_corrupt_inbox_payload(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK", msg_id="mgr_1",
                   run_id="run-1", request_id="req-1")
        (store.agent_subdir("s1", "w1", "inbox") / "mgr_1.json").write_text("{not json")
        with pytest.raises(ValueError, match="already exists with different payload"):
            store.send("s1", "mgr", "w1", "t", "b", "TASK", msg_id="mgr_1",
                       run_id="run-1", request_id="req-1")

    def test_send_replay_corrupt_history_payload(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK", msg_id="mgr_1",
                   run_id="run-1", request_id="req-1")
        (store.agent_subdir("s1", "w1", "inbox") / "mgr_1.json").unlink()
        (store.history_dir("s1") / "mgr_1.json").write_text("{not json")
        with pytest.raises(ValueError, match="already exists with different payload"):
            store.send("s1", "mgr", "w1", "t", "b", "TASK", msg_id="mgr_1",
                       run_id="run-1", request_id="req-1")

    def test_send_replay_backfills_missing_recipient(self, store):
        store.session_init("s1", "mgr", ["w1", "w2"])
        store.send("s1", "mgr", "*", "t", "b", "NOTICE", msg_id="mgr_1")
        inbox2 = store.agent_subdir("s1", "w2", "inbox")
        (inbox2 / "mgr_1.json").unlink()
        (store.history_dir("s1") / "mgr_1.json").unlink()
        result = store.send("s1", "mgr", "*", "t", "b", "NOTICE", msg_id="mgr_1")
        assert "backfilled 1" in result
        assert (inbox2 / "mgr_1.json").exists()
        assert (store.history_dir("s1") / "mgr_1.json").exists()

    def test_send_rejects_invalid_msg_id(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="invalid msg_id"):
            store.send("s1", "mgr", "w1", "t", "b", "TASK", msg_id="../evil",
                       run_id="run-1", request_id="req-1")

    # ── cursor / history edges ──

    def test_read_cursor_missing_and_corrupt(self, store):
        from codeagent.constants import STREAM_CURSOR_FILE, STREAM_CURSOR_INITIAL

        store.session_init("s1", "mgr", ["w1"])
        assert store.read_cursor("s1") == STREAM_CURSOR_INITIAL
        (store.root / "s1" / STREAM_CURSOR_FILE).write_text("{bad")
        assert store.read_cursor("s1") == STREAM_CURSOR_INITIAL

    def test_append_history_rejects_non_dict(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="history message must be a dict"):
            store.append_history("s1", "not-a-dict")

    def test_append_history_tmp_collision(self, store):
        store.session_init("s1", "mgr", ["w1"])
        msg = Message("s1", "mgr", "w1", "t", "b", "TASK", "m1",
                      "2026-01-01T00:00:00Z", run_id="run-1", request_id="req-1").to_dict()
        hd = store.history_dir("s1")
        hd.mkdir(parents=True, exist_ok=True)
        (hd / ".tmp-m1.json").touch()
        with pytest.raises(ValueError, match="history entry already exists"):
            store.append_history("s1", msg)

    def test_read_history_invalid_filters(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="invalid kind"):
            store.read_history("s1", kind="NOPE")
        with pytest.raises(ValueError, match="limit must be non-negative"):
            store.read_history("s1", limit=-1)

    def test_read_history_skips_invalid_entry(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1")
        hd = store.history_dir("s1")
        # Valid JSON but msg_id does not match the filename → skipped
        (hd / "foreign.json").write_text(json.dumps({
            "session_id": "s1", "from": "mgr", "to": "w1", "subject": "x",
            "body": "y", "kind": "TASK", "msg_id": "mgr_other",
            "created_at": "2026-01-01T00:00:00Z", "run_id": "run-1", "request_id": "req-1",
        }))
        assert len(store.read_history("s1")) == 1

    # ── read(): selection, vanished files, quarantine ──

    def test_read_target_msg_id_missing(self, store):
        self._send_one(store)
        assert store.read("s1", "w1", "w1", target_msg_id="nosuch") is None
        assert store.peek("s1", "w1")["pending"] == 1

    def test_read_all_skipped_returns_none(self, store):
        msg_id = self._send_one(store)
        assert store.read("s1", "w1", "w1", skip_msg_ids={msg_id}) is None
        assert store.peek("s1", "w1")["pending"] == 1

    def test_read_file_vanish_between_list_and_read(self, store):
        self._send_one(store)
        real_read_bytes = Path.read_bytes
        calls = {"n": 0}

        def flaky(self, *a, **kw):
            if self.suffix == ".json" and self.parent.name == "inbox":
                calls["n"] += 1
                if calls["n"] == 1:
                    raise FileNotFoundError("claimed by another reader")
            return real_read_bytes(self, *a, **kw)

        with mock.patch.object(Path, "read_bytes", flaky):
            msg = store.read("s1", "w1", "w1")
        assert msg is not None

    def test_read_corrupt_move_oserror_retries(self, store):
        store.session_init("s1", "mgr", ["w1"])
        inbox = store.agent_subdir("s1", "w1", "inbox")
        (inbox / "mgr_bad.json").write_text("{not json")
        real_replace = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            if "_corrupt" in str(dst) and calls["n"] == 0:
                calls["n"] += 1
                raise OSError("race lost")
            return real_replace(src, dst)

        with mock.patch("codeagent.mailbox.store.os.replace", flaky):
            assert store.read("s1", "w1", "w1") is None
        corrupt = store.agent_subdir("s1", "w1", "_corrupt")
        assert (corrupt / "mgr_bad.json").exists()

    def test_read_quarantines_non_roster_sender(self, store):
        store.session_init("s1", "mgr", ["w1"])
        inbox = store.agent_subdir("s1", "w1", "inbox")
        (inbox / "ghost_1.json").write_text(json.dumps({
            "session_id": "s1", "from": "ghost", "to": "w1", "subject": "hi",
            "body": "there", "kind": "TASK", "msg_id": "ghost_1",
            "created_at": "2026-01-01T00:00:00Z", "run_id": "run-1", "request_id": "req-1",
        }))
        assert store.read("s1", "w1", "w1") is None
        corrupt = store.agent_subdir("s1", "w1", "_corrupt")
        assert (corrupt / "ghost_1.json").exists()

    # ── read(): claim collision / reap races ──

    def test_read_reaps_expired_claim_and_retries(self, store):
        msg_id = self._send_one(store)
        processing = store.agent_subdir("s1", "w1", "processing")
        (processing / f".claim-{msg_id}-w1.json").write_text(json.dumps({
            "owner": "w1", "msg_id": msg_id, "claimed_at": self._old_ts()}))
        msg = store.read("s1", "w1", "w1")
        assert msg is not None and msg["msg_id"] == msg_id
        assert store.peek("s1", "w1")["pending"] == 0

    def test_read_fresh_claim_contested(self, store):
        msg_id = self._send_one(store)
        processing = store.agent_subdir("s1", "w1", "processing")
        # same-owner claim file blocks the reader's os.link publish
        (processing / f".claim-{msg_id}-w1.json").write_text(json.dumps({
            "owner": "w1", "msg_id": msg_id, "claimed_at": self._now_ts()}))
        assert store.read("s1", "w1", "w1") is None
        assert store.peek("s1", "w1")["pending"] == 1

    def test_read_corrupt_claim_contested(self, store):
        msg_id = self._send_one(store)
        processing = store.agent_subdir("s1", "w1", "processing")
        (processing / f".claim-{msg_id}-w1.json").write_text("{bad json")
        assert store.read("s1", "w1", "w1") is None

    def test_read_claim_no_timestamp_contested(self, store):
        msg_id = self._send_one(store)
        processing = store.agent_subdir("s1", "w1", "processing")
        (processing / f".claim-{msg_id}-w1.json").write_text(
            json.dumps({"owner": "w1", "msg_id": msg_id}))
        assert store.read("s1", "w1", "w1") is None

    def test_read_claim_link_oserror_fallback(self, store):
        self._send_one(store)
        with mock.patch("codeagent.mailbox.store.os.link", side_effect=OSError("no hardlinks")):
            msg = store.read("s1", "w1", "w1")
        assert msg is not None

    def test_read_claim_fallback_collision_contested(self, store):
        msg_id = self._send_one(store)
        processing = store.agent_subdir("s1", "w1", "processing")
        (processing / f".claim-{msg_id}-w1.json").write_text(json.dumps({
            "owner": "w1", "msg_id": msg_id, "claimed_at": self._now_ts()}))
        with mock.patch("codeagent.mailbox.store.os.link", side_effect=OSError("no hardlinks")):
            assert store.read("s1", "w1", "w1") is None

    def test_read_claim_fallback_reaps_expired(self, store):
        msg_id = self._send_one(store)
        processing = store.agent_subdir("s1", "w1", "processing")
        (processing / f".claim-{msg_id}-w1.json").write_text(json.dumps({
            "owner": "w1", "msg_id": msg_id, "claimed_at": self._old_ts()}))
        with mock.patch("codeagent.mailbox.store.os.link", side_effect=OSError("no hardlinks")):
            msg = store.read("s1", "w1", "w1")
        assert msg is not None and msg["msg_id"] == msg_id

    def test_read_reaps_claim_without_msg_id_field(self, store):
        msg_id = self._send_one(store)
        processing = store.agent_subdir("s1", "w1", "processing")
        (processing / f".claim-{msg_id}-w1.json").write_text(json.dumps({
            "owner": "w1", "claimed_at": self._old_ts()}))
        msg = store.read("s1", "w1", "w1")
        assert msg is not None and msg["msg_id"] == msg_id

    def test_read_reaps_claim_stem_fallback_odd(self, store):
        msg_id = self._send_one(store)
        processing = store.agent_subdir("s1", "w1", "processing")
        # JSON owner does not match the stem suffix → stem parse falls back
        (processing / f".claim-{msg_id}-w1.json").write_text(json.dumps({
            "owner": "w2", "claimed_at": self._old_ts()}))
        msg = store.read("s1", "w1", "w1")
        assert msg is not None and msg["msg_id"] == msg_id

    def test_read_claim_renewed_while_waiting(self, store):
        msg_id = self._send_one(store)
        processing = store.agent_subdir("s1", "w1", "processing")
        claim_path = processing / f".claim-{msg_id}-w1.json"
        old, fresh = self._old_ts(), self._now_ts()
        claim_path.write_text(json.dumps({
            "owner": "w1", "msg_id": msg_id, "claimed_at": old}))
        real_read_bytes = Path.read_bytes
        calls = {"n": 0}

        def flaky(self, *a, **kw):
            if self.name == claim_path.name:
                calls["n"] += 1
                payload = {"owner": "w1", "msg_id": msg_id,
                           "claimed_at": fresh if calls["n"] == 2 else old}
                return json.dumps(payload).encode()
            return real_read_bytes(self, *a, **kw)

        with mock.patch.object(Path, "read_bytes", flaky):
            assert store.read("s1", "w1", "w1") is None
        assert (processing / f".claim-{msg_id}-w1.json").exists()

    def test_read_claim_recheck_corrupt_uses_first_read(self, store):
        msg_id = self._send_one(store)
        processing = store.agent_subdir("s1", "w1", "processing")
        claim_path = processing / f".claim-{msg_id}-w1.json"
        old_ts = self._old_ts()
        claim_path.write_text(json.dumps({
            "owner": "w1", "msg_id": msg_id, "claimed_at": old_ts}))
        real_read_bytes = Path.read_bytes
        calls = {"n": 0}

        def flaky(self, *a, **kw):
            if self.name == claim_path.name:
                calls["n"] += 1
                if calls["n"] == 2:
                    return b""  # re-read under the lock is corrupt
                return json.dumps({"owner": "w1", "msg_id": msg_id,
                                   "claimed_at": old_ts}).encode()
            return real_read_bytes(self, *a, **kw)

        with mock.patch.object(Path, "read_bytes", flaky):
            msg = store.read("s1", "w1", "w1")
        assert msg is not None and msg["msg_id"] == msg_id

    def test_reap_expired_claim_moves_message_back(self, store):
        store.session_init("s1", "mgr", ["w1"])
        inbox = store.agent_subdir("s1", "w1", "inbox")
        processing = store.agent_subdir("s1", "w1", "processing")
        processing.mkdir(parents=True, exist_ok=True)
        (processing / "mgr_1.json").write_text("{}")
        claim_file = processing / ".claim-mgr_1-w1.json"
        claim_file.write_text(json.dumps({
            "owner": "w1", "msg_id": "mgr_1", "claimed_at": self._old_ts()}))
        assert store._reap_expired_claim(claim_file, inbox, processing) is True
        assert (inbox / "mgr_1.json").exists()
        assert not claim_file.exists()

    # ── finalize_from_inbox ──

    def test_finalize_from_inbox_missing(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="not in inbox/ or processing"):
            store.finalize_from_inbox("s1", "w1", "mgr_1", "w1")

    def test_finalize_from_inbox_foreign_claim(self, store):
        msg_id = self._send_one(store)
        store.read("s1", "w1", "w1")
        processing = store.agent_subdir("s1", "w1", "processing")
        (processing / f".claim-{msg_id}-w2.json").write_text(
            json.dumps({"owner": "w2", "msg_id": msg_id}))
        with pytest.raises(ValueError, match="active foreign claim"):
            store.finalize_from_inbox("s1", "w1", msg_id, "w1")

    def test_finalize_from_inbox_unreadable_claim_foreign(self, store):
        msg_id = self._send_one(store)
        store.read("s1", "w1", "w1")
        processing = store.agent_subdir("s1", "w1", "processing")
        (processing / f".claim-{msg_id}-w1.json").write_text("{bad json")
        with pytest.raises(ValueError, match="active foreign claim"):
            store.finalize_from_inbox("s1", "w1", msg_id, "w1")

    # ── renew_claim ──

    def test_renew_claim_success(self, store):
        msg_id = self._send_one(store)
        store.read("s1", "w1", "w1")
        assert store.renew_claim("s1", "w1", msg_id, "w1") is True
        processing = store.agent_subdir("s1", "w1", "processing")
        claim = json.loads(next(processing.glob(f".claim-{msg_id}-*.json")).read_bytes())
        assert "renewed_at" in claim

    def test_renew_claim_missing_message(self, store):
        store.session_init("s1", "mgr", ["w1"])
        assert store.renew_claim("s1", "w1", "mgr_1", "w1") is False

    def test_renew_claim_no_claim_file(self, store):
        msg_id = self._send_one(store)
        store.read("s1", "w1", "w1")
        processing = store.agent_subdir("s1", "w1", "processing")
        for cf in processing.glob(f".claim-{msg_id}-*.json"):
            cf.unlink()
        assert store.renew_claim("s1", "w1", msg_id, "w1") is False

    def test_renew_claim_two_claims(self, store):
        msg_id = self._send_one(store)
        store.read("s1", "w1", "w1")
        processing = store.agent_subdir("s1", "w1", "processing")
        (processing / f".claim-{msg_id}-w2.json").write_text(
            json.dumps({"owner": "w2", "msg_id": msg_id}))
        assert store.renew_claim("s1", "w1", msg_id, "w1") is False

    def test_renew_claim_corrupt_claim(self, store):
        msg_id = self._send_one(store)
        store.read("s1", "w1", "w1")
        processing = store.agent_subdir("s1", "w1", "processing")
        for cf in processing.glob(f".claim-{msg_id}-*.json"):
            cf.write_text("{bad json")
        assert store.renew_claim("s1", "w1", msg_id, "w1") is False

    def test_renew_claim_wrong_owner(self, store):
        msg_id = self._send_one(store)
        store.read("s1", "w1", "w1")
        processing = store.agent_subdir("s1", "w1", "processing")
        for cf in processing.glob(f".claim-{msg_id}-*.json"):
            cf.unlink()
        (processing / f".claim-{msg_id}-w2.json").write_text(
            json.dumps({"owner": "w2", "msg_id": msg_id}))
        assert store.renew_claim("s1", "w1", msg_id, "w1") is False

    def test_renew_claim_write_oserror(self, store):
        msg_id = self._send_one(store)
        store.read("s1", "w1", "w1")
        with mock.patch("codeagent.mailbox.store.os.replace", side_effect=OSError("disk full")):
            assert store.renew_claim("s1", "w1", msg_id, "w1") is False

    def test_renew_claim_target_vanished_under_lock(self, store):
        msg_id = self._send_one(store)
        store.read("s1", "w1", "w1")
        real_exists = Path.exists
        calls = {"n": 0}

        def flaky(self, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                return False  # target disappears between the two checks
            return real_exists(self, *a, **kw)

        with mock.patch.object(Path, "exists", flaky):
            assert store.renew_claim("s1", "w1", msg_id, "w1") is False

    def test_renew_claim_claim_vanished_under_lock(self, store):
        msg_id = self._send_one(store)
        store.read("s1", "w1", "w1")
        real_glob = Path.glob
        calls = {"n": 0}

        def flaky(self, pattern):
            calls["n"] += 1
            if calls["n"] == 2:
                return iter([])  # claim gone under the lock
            return real_glob(self, pattern)

        with mock.patch.object(Path, "glob", flaky):
            assert store.renew_claim("s1", "w1", msg_id, "w1") is False

    def test_renew_claim_target_gone_after_replace(self, store):
        msg_id = self._send_one(store)
        store.read("s1", "w1", "w1")
        processing = store.agent_subdir("s1", "w1", "processing")
        real_replace = os.replace

        def sneaky(src, dst):
            result = real_replace(src, dst)
            if ".tmp-renew-" in str(src):
                (processing / f"{msg_id}.json").unlink()
            return result

        with mock.patch("codeagent.mailbox.store.os.replace", sneaky):
            assert store.renew_claim("s1", "w1", msg_id, "w1") is False
        # self-heal: the resurrected claim is removed
        assert list(processing.glob(f".claim-{msg_id}-*.json")) == []

    # ── recover_stale ──

    def test_recover_stale_claim_missing_timestamp_kept(self, store):
        store.session_init("s1", "mgr", ["w1"])
        processing = store.agent_subdir("s1", "w1", "processing")
        processing.mkdir(parents=True, exist_ok=True)
        (processing / ".claim-mgr_1-w1.json").write_text(
            json.dumps({"owner": "w1", "msg_id": "mgr_1"}))
        assert "recovered 0" in store.recover_stale("s1", "w1")
        assert (processing / ".claim-mgr_1-w1.json").exists()

    def test_recover_stale_msg_id_from_stem(self, store):
        msg_id = self._send_one(store)
        store.read("s1", "w1", "w1")
        processing = store.agent_subdir("s1", "w1", "processing")
        for cf in processing.glob(f".claim-{msg_id}-*.json"):
            cf.write_text(json.dumps({"owner": "w1", "claimed_at": self._old_ts()}))
        assert "recovered 1" in store.recover_stale("s1", "w1")
        assert store.peek("s1", "w1")["pending"] == 1

    def test_recover_stale_stem_fallback_mismatch_orphan(self, store):
        store.session_init("s1", "mgr", ["w1"])
        processing = store.agent_subdir("s1", "w1", "processing")
        processing.mkdir(parents=True, exist_ok=True)
        (processing / ".claim-orphan.json").write_text(
            json.dumps({"owner": "w1", "claimed_at": self._old_ts()}))
        assert "recovered 0" in store.recover_stale("s1", "w1")
        assert not (processing / ".claim-orphan.json").exists()

    def test_recover_stale_orphan_claim_fresh_kept(self, store):
        store.session_init("s1", "mgr", ["w1"])
        processing = store.agent_subdir("s1", "w1", "processing")
        processing.mkdir(parents=True, exist_ok=True)
        (processing / ".claim-mgr_1-w1.json").write_text(json.dumps({
            "owner": "w1", "msg_id": "mgr_1", "claimed_at": self._now_ts()}))
        assert "recovered 0" in store.recover_stale("s1", "w1")
        assert (processing / ".claim-mgr_1-w1.json").exists()

    def test_recover_stale_cur_recheck_corrupt(self, store):
        msg_id = self._send_one(store)
        store.read("s1", "w1", "w1")
        processing = store.agent_subdir("s1", "w1", "processing")
        claim_file = next(processing.glob(f".claim-{msg_id}-*.json"))
        claim_file.write_text(json.dumps({
            "owner": "w1", "msg_id": msg_id, "claimed_at": self._old_ts()}))
        real_read_bytes = Path.read_bytes
        calls = {"n": 0}

        def flaky(self, *a, **kw):
            if self.name == claim_file.name:
                calls["n"] += 1
                if calls["n"] == 2:
                    return b""
            return real_read_bytes(self, *a, **kw)

        with mock.patch.object(Path, "read_bytes", flaky):
            assert "recovered 1" in store.recover_stale("s1", "w1")
        assert store.peek("s1", "w1")["pending"] == 1

    def test_recover_stale_sweeps_tmp_claims(self, store):
        import time as _time

        store.session_init("s1", "mgr", ["w1"])
        processing = store.agent_subdir("s1", "w1", "processing")
        old_mtime = _time.time() - 3600
        (processing / ".tmp-claim-old-w1-1.json").touch()
        os.utime(processing / ".tmp-claim-old-w1-1.json", (old_mtime, old_mtime))
        (processing / ".tmp-claim-fresh-w1-2.json").touch()
        os.symlink("target", processing / ".tmp-claim-link-w1-3.json")
        store.recover_stale("s1", "w1")
        assert not (processing / ".tmp-claim-old-w1-1.json").exists()
        assert (processing / ".tmp-claim-fresh-w1-2.json").exists()
        assert not (processing / ".tmp-claim-link-w1-3.json").exists()

    def test_recover_stale_tmp_stat_oserror_kept(self, store):
        store.session_init("s1", "mgr", ["w1"])
        processing = store.agent_subdir("s1", "w1", "processing")
        (processing / ".tmp-claim-x-w1-9.json").touch()
        real_stat = Path.stat

        def flaky(self, *a, **kw):
            if self.name == ".tmp-claim-x-w1-9.json":
                raise OSError("vanished")
            return real_stat(self, *a, **kw)

        with mock.patch.object(Path, "stat", flaky):
            assert "recovered 0" in store.recover_stale("s1", "w1")
        assert (processing / ".tmp-claim-x-w1-9.json").exists()

    # ── read_status strict / corrupt ──

    def test_read_status_strict_raises_variants(self, store):
        from codeagent.mailbox.store import StatusFileCorruptError

        store.session_init("s1", "mgr", ["w1"])
        base = {"session_id": "s1", "state": "BUSY", "current_task": "",
                "last_conclusion": "", "updated_at": "t"}
        cases = [
            ({"session_id": "s1"}, "missing or extra keys"),
            ({**base, "current_task": 5}, "non-string field value"),
            ({**base, "state": "NOPE"}, "invalid state"),
            ({**base, "session_id": "other"}, "session mismatch"),
            ("{not json", "corrupt status file"),
        ]
        for payload, match in cases:
            if isinstance(payload, str):
                (store.agent_dir("s1", "w1") / "status.json").write_text(payload)
            else:
                self._write_status(store, payload)
            with pytest.raises(StatusFileCorruptError, match=match):
                store.read_status("s1", "w1", strict=True)
            # non-strict keeps returning None for the same file
            assert store.read_status("s1", "w1") is None

    def test_read_status_oserror_returns_none(self, store):
        store.session_init("s1", "mgr", ["w1"])
        ad = store.agent_dir("s1", "w1")
        (ad / "status.json").mkdir()  # a directory cannot be read as status
        assert store.read_status("s1", "w1") is None
        assert store.read_status("s1", "w1", strict=True) is None

    # ── _session_has_active_park_lease ──

    def test_session_has_active_park_lease(self, store, monkeypatch):
        import codeagent.mailbox.store as st
        from codeagent.domain.park import Lifecycle

        store.session_init("s1", "mgr", ["w1"])

        class FakeManifest:
            lifecycle = Lifecycle.HOT_PARKED

        class FakeRegistry:
            def lookup_by_field(self, field, value):
                return FakeManifest()

        monkeypatch.setattr(st, "ParkRegistry", FakeRegistry)
        assert store._session_has_active_park_lease("s1") is True

    def test_session_has_active_park_lease_none_manifest(self, store, monkeypatch):
        import codeagent.mailbox.store as st

        store.session_init("s1", "mgr", ["w1"])

        class EmptyRegistry:
            def lookup_by_field(self, field, value):
                return None

        monkeypatch.setattr(st, "ParkRegistry", EmptyRegistry)
        assert store._session_has_active_park_lease("s1") is False

    def test_session_has_active_park_lease_registry_error(self, store, monkeypatch):
        import codeagent.mailbox.store as st

        store.session_init("s1", "mgr", ["w1"])

        class BrokenRegistry:
            def lookup_by_field(self, field, value):
                raise RuntimeError("boom")

        monkeypatch.setattr(st, "ParkRegistry", BrokenRegistry)
        assert store._session_has_active_park_lease("s1") is False

    def test_session_has_active_park_lease_no_registry(self, store, monkeypatch):
        import codeagent.mailbox.store as st

        store.session_init("s1", "mgr", ["w1"])
        monkeypatch.setattr(st, "ParkRegistry", None)
        assert store._session_has_active_park_lease("s1") is False

    def test_session_has_active_park_lease_empty_roster(self, store, monkeypatch):
        import codeagent.mailbox.store as st

        class EmptyRegistry:
            def lookup_by_field(self, field, value):
                raise AssertionError("no roster member should be looked up")

        monkeypatch.setattr(st, "ParkRegistry", EmptyRegistry)
        # No session.json → roster degrades to {""} → skipped
        assert store._session_has_active_park_lease("s1") is False

    # ── _session_has_unfinished_work ──

    def test_unfinished_work_none(self, store):
        store.session_init("s1", "mgr", ["w1"])
        assert store._session_has_unfinished_work("s1") is False

    def test_unfinished_work_no_session_roster(self, store):
        # No session.json → roster degrades to {""} → skipped, no work
        assert store._session_has_unfinished_work("s1") is False

    def test_unfinished_work_pending_inbox(self, store):
        self._send_one(store)
        assert store._session_has_unfinished_work("s1") is True

    def test_unfinished_work_inflight_processing(self, store):
        self._send_one(store)
        store.read("s1", "w1", "w1")
        assert store._session_has_unfinished_work("s1") is True

    def test_unfinished_work_fresh_claim_lease(self, store):
        msg_id = self._send_one(store)
        store.read("s1", "w1", "w1")
        processing = store.agent_subdir("s1", "w1", "processing")
        (processing / f"{msg_id}.json").unlink()  # message already finalized
        assert store._session_has_unfinished_work("s1") is True

    def test_unfinished_work_stale_claim_ignored(self, store):
        msg_id = self._send_one(store)
        store.read("s1", "w1", "w1")
        processing = store.agent_subdir("s1", "w1", "processing")
        (processing / f"{msg_id}.json").unlink()
        for cf in processing.glob(f".claim-{msg_id}-*.json"):
            cf.write_text(json.dumps({
                "owner": "w1", "msg_id": msg_id, "claimed_at": self._old_ts()}))
        assert store._session_has_unfinished_work("s1") is False

    def test_unfinished_work_corrupt_claim_ignored(self, store):
        msg_id = self._send_one(store)
        store.read("s1", "w1", "w1")
        processing = store.agent_subdir("s1", "w1", "processing")
        (processing / f"{msg_id}.json").unlink()
        for cf in processing.glob(f".claim-{msg_id}-*.json"):
            cf.write_text("{bad json")
        assert store._session_has_unfinished_work("s1") is False

    def test_unfinished_work_symlink_claim_ignored(self, store):
        msg_id = self._send_one(store)
        store.read("s1", "w1", "w1")
        processing = store.agent_subdir("s1", "w1", "processing")
        (processing / f"{msg_id}.json").unlink()
        for cf in processing.glob(f".claim-{msg_id}-*.json"):
            os.unlink(cf)
        os.symlink("nowhere", processing / f".claim-{msg_id}-w1.json")
        assert store._session_has_unfinished_work("s1") is False

    def test_unfinished_work_outbox_entries(self, store):
        store.session_init("s1", "mgr", ["w1"])
        outbox = store.root / "_outbox" / "s1"
        outbox.mkdir(parents=True)
        (outbox / "entry.json").touch()
        assert store._session_has_unfinished_work("s1") is True

    # ── clean_older_than ──

    def test_clean_older_than_negative_days(self, store):
        with pytest.raises(ValueError, match="non-negative"):
            store.clean_older_than(-1)

    def test_clean_older_than_missing_root(self, store):
        import shutil

        shutil.rmtree(store.root)
        assert store.clean_older_than(30) == {"removed": [], "skipped": []}

    def test_clean_older_than_removes_old_keeps_fresh(self, store):
        store.session_init("old1", "mgr", ["w1"])
        self._age_session(store, "old1", 100)
        store.session_init("fresh1", "mgr", ["w1"])
        result = store.clean_older_than(30)
        assert result["removed"] == ["old1"]
        assert result["skipped"] == []
        assert not (store.root / "old1").exists()
        assert (store.root / "fresh1").exists()

    def test_clean_older_than_removes_outbox_dead_letter(self, store):
        store.session_init("old2", "mgr", ["w1"])
        self._age_session(store, "old2", 100)
        (store.root / "_outbox" / "old2").mkdir(parents=True)
        (store.root / "_dead_letter" / "old2").mkdir(parents=True)
        result = store.clean_older_than(30)
        assert "old2" in result["removed"]
        assert not (store.root / "_outbox" / "old2").exists()
        assert not (store.root / "_dead_letter" / "old2").exists()

    def test_clean_older_than_skips_unfinished(self, store):
        store.session_init("old3", "mgr", ["w1"])
        self._age_session(store, "old3", 100)
        store.send("old3", "mgr", "w1", "t", "b", "TASK", run_id="run-1", request_id="req-1")
        result = store.clean_older_than(30)
        assert result["skipped"] == ["old3"]
        assert (store.root / "old3").exists()

    def test_clean_older_than_skips_park_leased(self, store, monkeypatch):
        import codeagent.mailbox.store as st
        from codeagent.domain.park import Lifecycle

        store.session_init("old4", "mgr", ["w1"])
        self._age_session(store, "old4", 100)

        class FakeManifest:
            lifecycle = Lifecycle.HOT_PARKED

        class FakeRegistry:
            def lookup_by_field(self, field, value):
                return FakeManifest()

        monkeypatch.setattr(st, "ParkRegistry", FakeRegistry)
        result = store.clean_older_than(30)
        assert result["skipped"] == ["old4"]

    def test_clean_older_than_skips_invalid_session_dir(self, store):
        d = store.root / "bad dir"
        d.mkdir()
        (d / "session.json").write_text("{}")
        assert store.clean_older_than(30) == {"removed": [], "skipped": []}

    def test_clean_older_than_bad_created_at_treated_fresh(self, store):
        store.session_init("weird", "mgr", ["w1"])
        meta = store.read_session("weird")
        meta["created_at"] = "not-a-date"
        (store.root / "weird" / "session.json").write_text(json.dumps(meta))
        assert store.clean_older_than(30) == {"removed": [], "skipped": []}

    def test_clean_older_than_no_session_json(self, store):
        (store.root / "empty-dir").mkdir()
        assert store.clean_older_than(30) == {"removed": [], "skipped": []}

    def test_clean_older_than_rmtree_oserror_skips(self, store):
        import shutil

        store.session_init("old5", "mgr", ["w1"])
        self._age_session(store, "old5", 100)
        with mock.patch("shutil.rmtree", side_effect=OSError("busy")):
            result = store.clean_older_than(30)
        assert result["skipped"] == ["old5"]

    # ── RequestLedger edge cases ──

    def test_apply_message_no_correlation_ids(self, store):
        ledger = self._ledger(store)
        assert ledger.apply_message({"kind": "REPORT", "msg_id": "m1"}) == ""

    def test_apply_message_notice(self, store):
        ledger = self._ledger(store)
        result = ledger.apply_message({
            "kind": "NOTICE", "msg_id": "m1", "from": "mgr",
            "run_id": "run-1", "request_id": "req-1",
        })
        assert result == "NOTICE"
        assert ledger.get_events("req-1", "run-1")[-1]["event"] == "NOTICE"

    def test_apply_message_unknown_kind(self, store):
        ledger = self._ledger(store)
        assert ledger.apply_message({
            "kind": "WHATEVER", "msg_id": "m1", "from": "mgr",
            "run_id": "run-1", "request_id": "req-1",
        }) == ""

    def test_apply_message_receipt_non_read_ignored(self, store):
        ledger = self._ledger(store)
        assert ledger.apply_message({
            "kind": "RECEIPT", "receipt_type": "WRITE", "msg_id": "m1",
            "run_id": "run-1", "request_id": "req-1",
        }) == ""

    def test_apply_message_receipt_read_acks(self, store):
        ledger = self._ledger(store)
        result = ledger.apply_message({
            "kind": "RECEIPT", "receipt_type": "READ", "msg_id": "m1",
            "reply_to": "orig-1", "run_id": "run-1", "request_id": "req-1",
        })
        assert result == "ACKED"
        assert ledger.get_terminal("req-1", "run-1") is None  # ACKED is non-terminal
        assert ledger.get_events("req-1", "run-1")[-1]["event"] == "ACKED"

    def test_apply_message_task_dispatches(self, store):
        ledger = self._ledger(store)
        assert ledger.apply_message({
            "kind": "TASK", "msg_id": "m1", "from": "mgr",
            "run_id": "run-1", "request_id": "req-1",
        }) == "DISPATCHED"

    def test_apply_message_progress_first_runs(self, store):
        ledger = self._ledger(store)
        assert ledger.apply_message({
            "kind": "PROGRESS", "msg_id": "m1", "from": "mgr",
            "run_id": "run-1", "request_id": "req-1",
        }) == "RUNNING"
        # later progress is informational only
        assert ledger.apply_message({
            "kind": "PROGRESS", "msg_id": "m2", "from": "mgr",
            "run_id": "run-1", "request_id": "req-1",
        }) == ""

    def test_apply_message_report_done(self, store):
        ledger = self._ledger(store)
        assert ledger.apply_message({
            "kind": "REPORT", "msg_id": "m1", "from": "mgr",
            "run_id": "run-1", "request_id": "req-1",
        }) == "DONE"
        assert ledger.get_terminal("req-1", "run-1") == "DONE"

    def test_get_entries_all_runs(self, store):
        ledger = self._ledger(store)
        ledger.record_event("req-1", "run-a", "DISPATCHED", {})
        ledger.record_event("req-1", "run-b", "DISPATCHED", {})
        assert set(ledger.get_entries_all_runs("req-1")) == {"run-a", "run-b"}

    def test_find_stale_no_events_dir(self, store):
        ledger = self._ledger(store)
        assert ledger.find_stale() == []

    def test_find_stale_ignores_non_dir_entries(self, store):
        ledger = self._ledger(store)
        events = store.root / "s1" / "w1" / "events"
        events.mkdir(parents=True)
        (events / "junk").write_text("x")
        assert ledger.find_stale() == []

    def test_read_all_skips_blank_and_garbage_lines(self, store):
        ledger = self._ledger(store)
        ledger.record_event("req-1", "run-1", "DISPATCHED", {})
        ef = store.root / "s1" / "w1" / "events" / "req-1" / "events.jsonl"
        with open(ef, "a") as f:
            f.write("\n")
            f.write("{garbage\n")
        entries = ledger.get_events("req-1", "run-1")
        assert len(entries) == 1
        assert entries[0]["event"] == "DISPATCHED"
