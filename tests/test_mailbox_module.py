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


# ── TestResolveRoot ──────────────────────────────────────────────────────


class TestResolveRoot:
    def test_default_fallback(self, monkeypatch):
        from codeagent.mailbox.store import resolve_root

        monkeypatch.delenv("MAILBOX_ROOT", raising=False)
        assert resolve_root() == Path.home() / "Dropbox" / "logseq" / "pages" / "mi-docs" / ".mailbox"

    def test_env_override(self, tmp_path, monkeypatch):
        from codeagent.mailbox.store import resolve_root

        monkeypatch.setenv("MAILBOX_ROOT", str(tmp_path))
        assert resolve_root() == tmp_path


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
            result = store.send("s1", "mgr", "w1", "s", "b", "TASK")
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
        sent = store.send("s1", "mgr", "w1", "t", "b", "TASK")
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
        store.send("s1", "mgr", "w1", "t", "b", "TASK")

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
        store.send("s1", "mgr", "w1", "t", "b", "TASK")
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
        store.send("s1", "mgr", "w1", "t", "b", "TASK")
        msg = store.read("s1", "w1", "w1")
        processing = store.agent_subdir("s1", "w1", "processing")
        (processing / f".claim-{msg['msg_id']}-w2.json").write_text(
            json.dumps({"owner": "w2", "msg_id": msg["msg_id"]}))
        with pytest.raises(ValueError, match="multiple claim files"):
            store.release("s1", "w1", msg["msg_id"], "w1")

    def test_owner_mismatch(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK")
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
        store.send("s1", "mgr", "w1", "t", "b", "TASK")
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
        store.send("s1", "mgr", "w1", "t", "b", "TASK")
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
        store.send("s1", "mgr", "w1", "t", "b", "TASK")
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
        store.send("s1", "mgr", "w1", "t", "b", "TASK")
        results = store.check("s1", "w1")
        assert len(results) == 1
        assert results[0]["subject"] == "t"
        archive = store.agent_subdir("s1", "w1", "archive")
        assert len(list(archive.glob("*.json"))) == 1

    def test_max_messages_limit(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t1", "b", "TASK")
        store.send("s1", "mgr", "w1", "t2", "b", "TASK")
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
        store.send("s1", "mgr", "w1", "t", "b", "TASK")

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
        store.send("s1", "mgr", "w1", "t", "b", "TASK", attachments=refs)
        msg = store.read("s1", "w1", "w1")
        atts = msg["attachments"]
        assert len(atts) == 2
        assert atts[0]["sha256"] == "a" * 64
        assert atts[0]["media_type"] == "application/octet-stream"
        assert atts[1]["relative_path"] == "b/c.txt"
        assert atts[1]["media_type"] == "text/plain"

    def test_broadcast_with_attachments(self, store):
        store.session_init("s1", "mgr", ["w1", "w2"])
        store.send("s1", "mgr", "*", "t", "b", "TASK", attachments=[self._ref()])
        for aid in ("w1", "w2"):
            msg = store.read("s1", aid, aid)
            assert msg["attachments"][0]["artifact_id"] == "art-1"

    def test_send_rejects_bad_sha256(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="sha256"):
            store.send("s1", "mgr", "w1", "t", "b", "TASK",
                       attachments=[self._ref(sha256="xyz")])

    def test_send_rejects_short_sha256(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="sha256"):
            store.send("s1", "mgr", "w1", "t", "b", "TASK",
                       attachments=[self._ref(sha256="a" * 63)])

    def test_send_rejects_negative_size(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="size"):
            store.send("s1", "mgr", "w1", "t", "b", "TASK",
                       attachments=[self._ref(size=-1)])

    def test_send_rejects_empty_artifact_id(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="artifact_id"):
            store.send("s1", "mgr", "w1", "t", "b", "TASK",
                       attachments=[self._ref(artifact_id="")])

    def test_send_rejects_path_traversal(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="relative_path"):
            store.send("s1", "mgr", "w1", "t", "b", "TASK",
                       attachments=[self._ref(relative_path="../escape")])

    def test_send_rejects_non_list(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="attachments must be a list"):
            store.send("s1", "mgr", "w1", "t", "b", "TASK", attachments="nope")

    def test_send_rejects_non_dict_item(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="attachment"):
            store.send("s1", "mgr", "w1", "t", "b", "TASK", attachments=[42])

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
            "attachments": "not-a-list",
        }
        ok, reason = validate_message(msg)
        assert not ok
        assert "attachments must be a list" in reason


# ── TestHistory ──────────────────────────────────────────────────────────


class TestHistory:
    def test_send_appends_history(self, store):
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK")
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
        store.send("s1", "mgr", "w1", "t", "b", "TASK")
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
                          (base + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ"))
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
                      "2026-01-01T00:00:00Z").to_dict()
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
        store.send("s1", "mgr", "w1", "t", "b", "TASK")
        hd = store.history_dir("s1")
        (hd / "junk.json").write_text("{not json")
        assert len(store.read_history("s1")) == 1
