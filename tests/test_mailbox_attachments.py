"""Tests for mailbox attachment support: CLI parsing, store persistence,
round-trip reads, validation, and remote_exec forwarding."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeagent.mailbox import cli as mailbox_cli
from codeagent.mailbox.protocol import AttachmentRef, validate_message
from codeagent.mailbox.store import MailboxStore


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path: Path) -> MailboxStore:
    return MailboxStore(root=tmp_path)


def _run_cli(argv: list[str], store: MailboxStore, capsys):
    """Run mailbox CLI with the store's root as --mailbox-root."""
    argv = ["--mailbox-root", str(store.root)] + argv
    return mailbox_cli.main(argv)


def _ref(**over) -> dict:
    """Return a valid attachment dict, with optional field overrides."""
    base = {
        "artifact_id": "art-1",
        "source_host": "worker-1",
        "remote_root": "/tmp/artifacts",
        "relative_path": "out/result.json",
        "size": 42,
        "sha256": "a" * 64,
    }
    base.update(over)
    return base


# ── CLI parsing ───────────────────────────────────────────────────────────

class TestCliAttachmentParsing:
    """The --attachment repeatable flag parses JSON into AttachmentRef."""

    def test_single_attachment(self, store, capsys):
        """One --attachment flag persists one AttachmentRef."""
        store.session_init("s1", "mgr", ["w1"])
        capsys.readouterr()

        att_json = json.dumps(_ref())
        _run_cli(
            ["send", "--session", "s1", "--from", "mgr", "--to", "w1",
             "--subject", "t", "--body", "b", "--kind", "TASK",
             "--attachment", att_json],
            store, capsys,
        )
        assert "sent" in capsys.readouterr().out.lower()

        msg = store.read("s1", "w1", "w1")
        assert len(msg["attachments"]) == 1
        assert msg["attachments"][0]["artifact_id"] == "art-1"
        assert msg["attachments"][0]["sha256"] == "a" * 64

    def test_multiple_attachments(self, store, capsys):
        """Multiple --attachment flags produce multiple refs."""
        store.session_init("s1", "mgr", ["w1"])
        capsys.readouterr()

        att1 = json.dumps(_ref())
        att2 = json.dumps(_ref(
            artifact_id="art-2", relative_path="out/log.txt",
            size=99, sha256="b" * 64, media_type="text/plain",
        ))
        _run_cli(
            ["send", "--session", "s1", "--from", "mgr", "--to", "w1",
             "--subject", "t", "--body", "b", "--kind", "EVIDENCE",
             "--attachment", att1, "--attachment", att2],
            store, capsys,
        )
        assert "sent" in capsys.readouterr().out.lower()

        msg = store.read("s1", "w1", "w1")
        assert len(msg["attachments"]) == 2
        assert msg["attachments"][1]["artifact_id"] == "art-2"
        assert msg["attachments"][1]["media_type"] == "text/plain"

    def test_no_attachments_backward_compat(self, store, capsys):
        """Omitting --attachment still works (no regression)."""
        store.session_init("s1", "mgr", ["w1"])
        capsys.readouterr()

        _run_cli(
            ["send", "--session", "s1", "--from", "mgr", "--to", "w1",
             "--subject", "t", "--body", "b", "--kind", "TASK"],
            store, capsys,
        )
        assert "sent" in capsys.readouterr().out.lower()

        msg = store.read("s1", "w1", "w1")
        assert msg.get("attachments", []) == []

    def test_invalid_json_rejected(self, store, capsys):
        """Malformed JSON in --attachment produces a clear error."""
        store.session_init("s1", "mgr", ["w1"])
        capsys.readouterr()

        with pytest.raises(SystemExit):
            _run_cli(
                ["send", "--session", "s1", "--from", "mgr", "--to", "w1",
                 "--subject", "t", "--body", "b",
                 "--attachment", "not-json"],
                store, capsys,
            )

    def test_missing_sha256_rejected(self, store, capsys):
        """Attachment missing sha256 field produces validation error."""
        store.session_init("s1", "mgr", ["w1"])
        capsys.readouterr()

        bad = _ref()
        del bad["sha256"]
        with pytest.raises(SystemExit):
            _run_cli(
                ["send", "--session", "s1", "--from", "mgr", "--to", "w1",
                 "--subject", "t", "--body", "b",
                 "--attachment", json.dumps(bad)],
                store, capsys,
            )

    def test_bad_sha256_rejected(self, store, capsys):
        """Non-hex sha256 produces validation error."""
        store.session_init("s1", "mgr", ["w1"])
        capsys.readouterr()

        with pytest.raises(SystemExit):
            _run_cli(
                ["send", "--session", "s1", "--from", "mgr", "--to", "w1",
                 "--subject", "t", "--body", "b",
                 "--attachment", json.dumps(_ref(sha256="zzzz"))],
                store, capsys,
            )

    def test_negative_size_rejected(self, store, capsys):
        """Negative size produces validation error."""
        store.session_init("s1", "mgr", ["w1"])
        capsys.readouterr()

        with pytest.raises(SystemExit):
            _run_cli(
                ["send", "--session", "s1", "--from", "mgr", "--to", "w1",
                 "--subject", "t", "--body", "b",
                 "--attachment", json.dumps(_ref(size=-1))],
                store, capsys,
            )

    def test_path_traversal_rejected(self, store, capsys):
        """Traversal in relative_path produces validation error."""
        store.session_init("s1", "mgr", ["w1"])
        capsys.readouterr()

        with pytest.raises(SystemExit):
            _run_cli(
                ["send", "--session", "s1", "--from", "mgr", "--to", "w1",
                 "--subject", "t", "--body", "b",
                 "--attachment", json.dumps(_ref(relative_path="../escape"))],
                store, capsys,
            )

    def test_broadcast_with_attachment(self, store, capsys):
        """Broadcast send propagates attachments to every recipient."""
        store.session_init("s1", "mgr", ["w1", "w2"])
        capsys.readouterr()

        att_json = json.dumps(_ref())
        _run_cli(
            ["send", "--session", "s1", "--from", "mgr", "--to", "*",
             "--subject", "t", "--body", "b", "--kind", "NOTICE",
             "--attachment", att_json],
            store, capsys,
        )
        out = capsys.readouterr().out
        assert "broadcast" in out.lower()

        for agent in ("w1", "w2"):
            msg = store.read("s1", agent, agent)
            assert len(msg["attachments"]) == 1
            assert msg["attachments"][0]["artifact_id"] == "art-1"


# ── Store persistence ─────────────────────────────────────────────────────

class TestStoreAttachmentPersistence:
    """MailboxStore.send() persists AttachmentRef in message JSON."""

    def test_send_persists_attachments(self, store):
        store.session_init("s1", "mgr", ["w1"])
        refs = [AttachmentRef.from_dict(_ref())]
        store.send("s1", "mgr", "w1", "t", "b", "TASK", attachments=refs)

        # Read the raw JSON file to verify persistence
        inbox = store.agent_dir("s1", "w1") / "inbox"
        files = list(inbox.glob("*.json"))
        assert len(files) == 1
        raw = json.loads(files[0].read_text())
        assert "attachments" in raw
        assert len(raw["attachments"]) == 1
        assert raw["attachments"][0]["artifact_id"] == "art-1"

    def test_send_with_no_attachments_omits_key(self, store):
        """When no attachments provided, the key should not appear or be empty."""
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "t", "b", "TASK")

        inbox = store.agent_dir("s1", "w1") / "inbox"
        files = list(inbox.glob("*.json"))
        raw = json.loads(files[0].read_text())
        # Protocol omits "attachments" key when list is empty (Message.to_dict)
        assert raw.get("attachments", []) == []


# ── Round-trip ────────────────────────────────────────────────────────────

class TestAttachmentRoundTrip:
    """Attachments survive send → read round-trip."""

    def test_read_returns_attachments(self, store):
        store.session_init("s1", "mgr", ["w1"])
        refs = [
            AttachmentRef.from_dict(_ref()),
            AttachmentRef.from_dict(_ref(
                artifact_id="art-2", relative_path="b/c.txt",
                size=7, sha256="b" * 64, media_type="text/plain",
            )),
        ]
        store.send("s1", "mgr", "w1", "t", "b", "TASK", attachments=refs)

        msg = store.read("s1", "w1", "w1")
        atts = msg["attachments"]
        assert len(atts) == 2
        assert atts[0]["artifact_id"] == "art-1"
        assert atts[0]["media_type"] == "application/octet-stream"
        assert atts[1]["artifact_id"] == "art-2"
        assert atts[1]["media_type"] == "text/plain"
        assert atts[1]["relative_path"] == "b/c.txt"

    def test_peek_shows_message_exists(self, store):
        """peek confirms the message is in the inbox (attachments only visible via read)."""
        store.session_init("s1", "mgr", ["w1"])
        refs = [AttachmentRef.from_dict(_ref())]
        store.send("s1", "mgr", "w1", "t", "b", "TASK", attachments=refs)

        peek = store.peek("s1", "w1")
        assert len(peek["messages"]) == 1
        assert peek["messages"][0]["subject"] == "t"

    def test_attachment_ref_dataclass_roundtrip(self):
        """AttachmentRef.from_dict(.to_dict()) is identity."""
        d = _ref(media_type="image/png")
        r = AttachmentRef.from_dict(d)
        assert r.to_dict() == d
        assert AttachmentRef.from_dict(r.to_dict()) == r

    def test_message_attachments_roundtrip(self):
        """Message serialization preserves attachments."""
        from codeagent.mailbox.protocol import Message
        r = AttachmentRef.from_dict(_ref())
        m = Message("s1", "mgr", "w1", "s", "b", "TASK", "m1", "t1",
                    attachments=[r])
        m2 = Message.from_dict(m.to_dict())
        assert m2.attachments == m.attachments


# ── Validation rejection ──────────────────────────────────────────────────

class TestAttachmentValidation:
    """Malformed attachments are rejected with explicit errors."""

    def test_missing_sha256(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="sha256"):
            store.send("s1", "mgr", "w1", "t", "b", "TASK",
                       attachments=[AttachmentRef.from_dict(_ref(sha256=""))])

    def test_invalid_sha256_format(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="sha256"):
            store.send("s1", "mgr", "w1", "t", "b", "TASK",
                       attachments=[AttachmentRef.from_dict(_ref(sha256="xyz"))])

    def test_short_sha256(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="sha256"):
            store.send("s1", "mgr", "w1", "t", "b", "TASK",
                       attachments=[AttachmentRef.from_dict(_ref(sha256="a" * 63))])

    def test_negative_size(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="size"):
            store.send("s1", "mgr", "w1", "t", "b", "TASK",
                       attachments=[AttachmentRef.from_dict(_ref(size=-1))])

    def test_oversize_attachment_rejected(self, store):
        """Attachment size beyond MAX_ATTACHMENT_SIZE is rejected."""
        from codeagent.constants import MAX_ATTACHMENT_SIZE
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="exceeds.*byte limit"):
            store.send("s1", "mgr", "w1", "t", "b", "TASK",
                       attachments=[AttachmentRef.from_dict(_ref(size=MAX_ATTACHMENT_SIZE + 1))])

    def test_empty_artifact_id(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="artifact_id"):
            store.send("s1", "mgr", "w1", "t", "b", "TASK",
                       attachments=[AttachmentRef.from_dict(_ref(artifact_id=""))])

    def test_path_traversal(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="relative_path"):
            store.send("s1", "mgr", "w1", "t", "b", "TASK",
                       attachments=[AttachmentRef.from_dict(_ref(relative_path="../escape"))])

    def test_non_list_rejected(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="attachments must be a list"):
            store.send("s1", "mgr", "w1", "t", "b", "TASK", attachments="nope")

    def test_non_dict_item_rejected(self, store):
        store.session_init("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="attachment"):
            store.send("s1", "mgr", "w1", "t", "b", "TASK", attachments=[42])

    def test_validate_message_rejects_bad_attachment(self):
        """validate_message() catches bad attachments in the dict."""
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


# ── remote_exec forwarding ────────────────────────────────────────────────

class TestRemoteExecAttachmentForwarding:
    """_dispatch_mailbox_direct forwards --attachment to store.send()."""

    def test_dispatch_send_with_attachment(self, store):
        """Direct dispatch passes attachments through to MailboxStore."""
        from codeagent.remote_exec import _dispatch_mailbox_direct

        store.session_init("s1", "mgr", ["w1"])

        att_json = json.dumps(_ref())
        out, err, exit_code = _dispatch_mailbox_direct(
            ["send", "--session", "s1", "--from", "mgr", "--to", "w1",
             "--subject", "t", "--body", "b", "--kind", "TASK",
             "--attachment", att_json],
            mailbox_root=str(store.root),
        )
        assert exit_code == 0
        assert "sent" in out.lower()

        msg = store.read("s1", "w1", "w1")
        assert len(msg["attachments"]) == 1
        assert msg["attachments"][0]["artifact_id"] == "art-1"

    def test_dispatch_send_multiple_attachments(self, store):
        from codeagent.remote_exec import _dispatch_mailbox_direct

        store.session_init("s1", "mgr", ["w1"])

        att1 = json.dumps(_ref())
        att2 = json.dumps(_ref(
            artifact_id="art-2", sha256="b" * 64, size=99,
        ))
        out, err, exit_code = _dispatch_mailbox_direct(
            ["send", "--session", "s1", "--from", "mgr", "--to", "w1",
             "--subject", "t", "--body", "b",
             "--attachment", att1, "--attachment", att2],
            mailbox_root=str(store.root),
        )
        assert exit_code == 0

        msg = store.read("s1", "w1", "w1")
        assert len(msg["attachments"]) == 2

    def test_dispatch_send_invalid_attachment_exits_nonzero(self, store):
        from codeagent.remote_exec import _dispatch_mailbox_direct

        store.session_init("s1", "mgr", ["w1"])

        bad = _ref()
        del bad["sha256"]
        out, err, exit_code = _dispatch_mailbox_direct(
            ["send", "--session", "s1", "--from", "mgr", "--to", "w1",
             "--subject", "t", "--body", "b",
             "--attachment", json.dumps(bad)],
            mailbox_root=str(store.root),
        )
        assert exit_code == 1
        assert "sha256" in err
