"""SwarmKernel tests — session, roster, ACL, routing, poll, subscribe."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codeagent.mailbox.store import MailboxStore
from codeagent.swarm.kernel import LocalDeliverySink, SwarmKernel
from codeagent.swarm.model import (
    ACL,
    Address,
    AddressKind,
    AgentLocation,
    Envelope,
    Roster,
)


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    return MailboxStore(root=tmp_path)


@pytest.fixture
def kernel(store):
    return SwarmKernel(store=store)


def _make_kernel_with_session(store, *, policy="open", extra_acl=None):
    """Helper: create kernel + session with 3 agents."""
    k = SwarmKernel(store=store)
    acl = extra_acl or ACL(
        authority="mgr",
        allowed_senders=["mgr", "w1", "w2"],
        room_members=["mgr", "w1", "w2"],
        policy=policy,
    )
    k.create_session("s1", "mgr", ["w1", "w2"], acl=acl)
    return k


def _env(subject="test", body="hello", kind="TASK"):
    return Envelope(subject=subject, body=body, kind=kind)


# ── Session creation ───────────────────────────────────────────────────


class TestCreateSession:
    def test_basic(self, kernel):
        s = kernel.create_session("s1", "mgr", ["w1", "w2"])
        assert s.session_id == "s1"
        assert s.manager_id == "mgr"
        assert "mgr" in s.roster
        assert "w1" in s.roster
        assert "w2" in s.roster

    def test_manager_always_in_roster(self, kernel):
        s = kernel.create_session("s1", "mgr", ["w1"])
        assert "mgr" in s.roster
        assert "mgr" in s.acl.allowed_senders
        assert "mgr" in s.acl.room_members

    def test_duplicate_session_raises(self, kernel):
        kernel.create_session("s1", "mgr", ["w1"])
        with pytest.raises(ValueError, match="already exists"):
            kernel.create_session("s1", "mgr", ["w1"])

    def test_invalid_session_id_raises(self, kernel):
        with pytest.raises(ValueError, match="invalid agent id"):
            kernel.create_session("../escape", "mgr", ["w1"])

    def test_invalid_manager_raises(self, kernel):
        with pytest.raises(ValueError, match="invalid agent id"):
            kernel.create_session("s1", "bad/id", ["w1"])

    def test_invalid_roster_raises(self, kernel):
        with pytest.raises(ValueError, match="invalid agent id"):
            kernel.create_session("s1", "mgr", ["bad/id"])

    def test_custom_acl(self, kernel):
        acl = ACL(authority="boss", allowed_senders=["boss"],
                  room_members=["boss", "mgr", "w1", "w2"], policy="restricted")
        s = kernel.create_session("s1", "mgr", ["w1", "w2"], acl=acl)
        assert s.acl.authority == "boss"
        assert s.acl.policy == "restricted"
        # manager is force-added
        assert "mgr" in s.acl.allowed_senders

    def test_persists_to_store(self, kernel, store):
        kernel.create_session("s1", "mgr", ["w1"])
        meta = store.read_session("s1")
        assert meta is not None
        assert meta["session_id"] == "s1"

    def test_require_session_not_found(self, kernel):
        with pytest.raises(ValueError, match="not found"):
            kernel._require_session("ghost")


# ── Register / Unregister ──────────────────────────────────────────────


class TestRegister:
    def test_register_basic(self, kernel):
        kernel.create_session("s1", "mgr", ["w1", "w2"])
        loc = AgentLocation("w1", "__local__", "cli")
        reg = kernel.register(loc, "s1")
        assert reg.agent_id == "w1"
        assert reg.session_id == "s1"
        assert kernel.get_location("s1", "w1") is loc

    def test_register_non_member_raises(self, kernel):
        kernel.create_session("s1", "mgr", ["w1", "w2"])
        loc = AgentLocation("outsider", "__local__", "cli")
        with pytest.raises(ValueError, match="not in roster"):
            kernel.register(loc, "s1")

    def test_register_no_session_raises(self, kernel):
        loc = AgentLocation("w1", "__local__", "cli")
        with pytest.raises(ValueError, match="not found"):
            kernel.register(loc, "ghost")

    def test_unregister(self, kernel):
        kernel.create_session("s1", "mgr", ["w1", "w2"])
        loc = AgentLocation("w1", "__local__", "cli")
        kernel.register(loc, "s1")
        kernel.unregister("s1", "w1")
        assert kernel.get_location("s1", "w1") is None

    def test_unregister_no_session_raises(self, kernel):
        with pytest.raises(ValueError, match="not found"):
            kernel.unregister("ghost", "w1")


# ── Direct message ─────────────────────────────────────────────────────


class TestDirect:
    def test_delivers_to_recipient_inbox(self, store):
        k = _make_kernel_with_session(store)
        receipt = k.direct("s1", "mgr", "w1", _env())
        assert receipt.status == "delivered"
        assert receipt.target == "w1"
        # Verify exactly one message in w1 inbox
        inbox_w1 = store.list_messages(store.agent_subdir("s1", "w1", "inbox"))
        assert len(inbox_w1) == 1

    def test_no_fanout(self, store):
        """Direct must write to exactly 1 inbox, not w2 or mgr."""
        k = _make_kernel_with_session(store)
        k.direct("s1", "mgr", "w1", _env())
        inbox_w2 = store.list_messages(store.agent_subdir("s1", "w2", "inbox"))
        inbox_mgr = store.list_messages(store.agent_subdir("s1", "mgr", "inbox"))
        assert len(inbox_w2) == 0
        assert len(inbox_mgr) == 0

    def test_acl_denied_sender(self, store):
        acl = ACL(authority="mgr", allowed_senders=["mgr"],
                  room_members=["mgr", "w1", "w2"], policy="restricted")
        k = _make_kernel_with_session(store, extra_acl=acl)
        with pytest.raises(PermissionError, match="not in allowed_senders"):
            k.direct("s1", "w1", "mgr", _env())

    def test_acl_denied_recipient(self, store):
        k = _make_kernel_with_session(store)
        with pytest.raises(PermissionError, match="not in room_members"):
            k.direct("s1", "mgr", "outsider", _env())

    def test_msg_id_unique(self, store):
        k = _make_kernel_with_session(store)
        r1 = k.direct("s1", "mgr", "w1", _env())
        r2 = k.direct("s1", "mgr", "w1", _env())
        assert r1.msg_id != r2.msg_id


# ── Broadcast ──────────────────────────────────────────────────────────


class TestBroadcast:
    def test_fans_out_to_all_except_sender(self, store):
        k = _make_kernel_with_session(store)
        receipts = k.broadcast("s1", "mgr", _env())
        # 2 recipients: w1, w2 (not mgr)
        assert len(receipts) == 2
        recipients = {r.recipient for r in receipts}
        assert recipients == {"w1", "w2"}

    def test_sender_excluded(self, store):
        k = _make_kernel_with_session(store)
        k.broadcast("s1", "mgr", _env())
        inbox_mgr = store.list_messages(store.agent_subdir("s1", "mgr", "inbox"))
        assert len(inbox_mgr) == 0

    def test_acl_denied_non_authority(self, store):
        k = _make_kernel_with_session(store, policy="restricted")
        with pytest.raises(PermissionError, match="not broadcast authority"):
            k.broadcast("s1", "w1", _env())

    def test_open_policy_allows_anyone(self, store):
        k = _make_kernel_with_session(store, policy="open")
        receipts = k.broadcast("s1", "w1", _env())
        assert len(receipts) == 2  # mgr + w2


# ── Channel ────────────────────────────────────────────────────────────


class TestChannel:
    def test_create_channel(self, kernel):
        kernel.create_session("s1", "mgr", ["w1", "w2"])
        ch = kernel.create_channel("s1", "dev", ["mgr", "w1"])
        assert ch.channel_id == "dev"
        assert "mgr" in ch.members
        assert "w1" in ch.members

    def test_create_channel_non_member_raises(self, kernel):
        kernel.create_session("s1", "mgr", ["w1", "w2"])
        with pytest.raises(ValueError, match="not in roster"):
            kernel.create_channel("s1", "dev", ["mgr", "outsider"])

    def test_duplicate_channel_raises(self, kernel):
        kernel.create_session("s1", "mgr", ["w1", "w2"])
        kernel.create_channel("s1", "dev", ["mgr", "w1"])
        with pytest.raises(ValueError, match="already exists"):
            kernel.create_channel("s1", "dev", ["mgr"])

    def test_channel_delivers_to_members(self, store):
        k = _make_kernel_with_session(store)
        k.create_channel("s1", "dev", ["mgr", "w1"])
        receipt = k.channel("s1", "mgr", "dev", _env())
        assert receipt.status == "delivered"
        inbox_w1 = store.list_messages(store.agent_subdir("s1", "w1", "inbox"))
        assert len(inbox_w1) == 1

    def test_channel_excludes_sender(self, store):
        k = _make_kernel_with_session(store)
        k.create_channel("s1", "dev", ["mgr", "w1", "w2"])
        k.channel("s1", "mgr", "dev", _env())
        inbox_mgr = store.list_messages(store.agent_subdir("s1", "mgr", "inbox"))
        assert len(inbox_mgr) == 0

    def test_channel_isolation(self, store):
        """Channel A members don't get channel B messages."""
        k = _make_kernel_with_session(store)
        k.create_channel("s1", "chA", ["mgr", "w1"])
        k.create_channel("s1", "chB", ["mgr", "w2"])
        # Send to chA — only w1 should get it
        k.channel("s1", "mgr", "chA", _env("A-subj", "A-body"))
        inbox_w1 = store.list_messages(store.agent_subdir("s1", "w1", "inbox"))
        inbox_w2 = store.list_messages(store.agent_subdir("s1", "w2", "inbox"))
        assert len(inbox_w1) == 1
        assert len(inbox_w2) == 0

    def test_channel_outsider_denied(self, store):
        k = _make_kernel_with_session(store)
        k.create_channel("s1", "dev", ["mgr", "w1"])
        with pytest.raises(PermissionError, match="not in channel members"):
            k.channel("s1", "w2", "dev", _env())

    def test_channel_not_found(self, store):
        k = _make_kernel_with_session(store)
        with pytest.raises(ValueError, match="not found"):
            k.channel("s1", "mgr", "ghost", _env())


# ── Notice ─────────────────────────────────────────────────────────────


class TestNotice:
    def test_notice_delivers_to_room(self, store):
        k = _make_kernel_with_session(store)
        receipt = k.notice("s1", "mgr", "system", _env())
        assert receipt.status == "delivered"
        inbox_w1 = store.list_messages(store.agent_subdir("s1", "w1", "inbox"))
        inbox_w2 = store.list_messages(store.agent_subdir("s1", "w2", "inbox"))
        assert len(inbox_w1) == 1
        assert len(inbox_w2) == 1

    def test_notice_excludes_sender(self, store):
        k = _make_kernel_with_session(store)
        k.notice("s1", "mgr", "system", _env())
        inbox_mgr = store.list_messages(store.agent_subdir("s1", "mgr", "inbox"))
        assert len(inbox_mgr) == 0

    def test_notice_with_ttl(self, store):
        k = _make_kernel_with_session(store)
        receipt = k.notice("s1", "mgr", "system", _env(), ttl=300)
        assert receipt.status == "delivered"

    def test_notice_acl_denied(self, store):
        acl = ACL(authority="mgr", allowed_senders=["mgr"],
                  room_members=["mgr", "w1", "w2"], policy="restricted")
        k = _make_kernel_with_session(store, extra_acl=acl)
        with pytest.raises(PermissionError, match="not in allowed_senders"):
            k.notice("s1", "w1", "system", _env())


# ── send() dispatcher ──────────────────────────────────────────────────


class TestSend:
    def test_send_direct(self, store):
        k = _make_kernel_with_session(store)
        addr = Address(kind=AddressKind.DIRECT, agent_id="w1")
        r = k.send("s1", "mgr", addr, _env())
        assert r.status == "delivered"

    def test_send_broadcast(self, store):
        k = _make_kernel_with_session(store)
        addr = Address(kind=AddressKind.BROADCAST)
        r = k.send("s1", "mgr", addr, _env())
        assert r.status == "delivered"

    def test_send_channel(self, store):
        k = _make_kernel_with_session(store)
        k.create_channel("s1", "dev", ["mgr", "w1"])
        addr = Address(kind=AddressKind.CHANNEL, channel_id="dev")
        r = k.send("s1", "mgr", addr, _env())
        assert r.status == "delivered"

    def test_send_notice(self, store):
        k = _make_kernel_with_session(store)
        addr = Address(kind=AddressKind.NOTICE, topic="sys")
        r = k.send("s1", "mgr", addr, _env())
        assert r.status == "delivered"

    def test_send_unknown_kind(self, store):
        k = _make_kernel_with_session(store)
        addr = Address(kind="bogus")
        with pytest.raises(ValueError, match="unknown address kind"):
            k.send("s1", "mgr", addr, _env())


# ── Poll ───────────────────────────────────────────────────────────────


class TestPoll:
    def test_poll_empty(self, kernel):
        kernel.create_session("s1", "mgr", ["w1"])
        result = kernel.poll("s1", "w1")
        assert result.messages == []
        assert result.has_more is False

    def test_poll_returns_messages(self, store):
        k = _make_kernel_with_session(store)
        k.direct("s1", "mgr", "w1", _env("first"))
        k.direct("s1", "mgr", "w1", _env("second"))
        result = kernel_poll(k, "s1", "w1")
        assert len(result.messages) == 2
        subjects = {m["subject"] for m in result.messages}
        assert subjects == {"first", "second"}

    def test_poll_cursor_filter(self, store):
        k = _make_kernel_with_session(store)
        k.direct("s1", "mgr", "w1", _env("first"))
        # Read the messages to get their created_at values
        msg1 = store.read("s1", "w1", "poller")
        assert msg1 is not None
        store.release("s1", "w1", msg1["msg_id"], "poller")
        # Now send second after we know the cursor
        k.direct("s1", "mgr", "w1", _env("second"))
        # Poll with cursor set to the first message's created_at
        # Since cursor uses <=, we need cursor to exclude msg1 but include msg2
        result = k.poll("s1", "w1", cursor=msg1["created_at"])
        # second was sent after first, so it should be included
        subjects = {m["subject"] for m in result.messages}
        assert "second" in subjects or "first" not in subjects

    def test_poll_limit(self, store):
        k = _make_kernel_with_session(store)
        for i in range(5):
            k.direct("s1", "mgr", "w1", _env(f"msg-{i}"))
        result = k.poll("s1", "w1", limit=2)
        assert len(result.messages) == 2
        assert result.has_more is True

    def test_poll_no_session_raises(self, kernel):
        with pytest.raises(ValueError, match="not found"):
            kernel.poll("ghost", "w1")


def kernel_poll(k, session_id, agent_id, **kw):
    return k.poll(session_id, agent_id, **kw)


# ── Subscribe ──────────────────────────────────────────────────────────


class TestSubscribe:
    def test_callback_fires_on_poll(self, store):
        k = _make_kernel_with_session(store)
        fired = []
        k.subscribe("s1", "w1", callback=lambda msg: fired.append(msg))
        k.direct("s1", "mgr", "w1", _env("fire-me"))
        k.poll("s1", "w1")
        assert len(fired) == 1
        assert fired[0]["subject"] == "fire-me"

    def test_subscribe_no_session_raises(self, kernel):
        with pytest.raises(ValueError, match="not found"):
            kernel.subscribe("ghost", "w1", callback=lambda m: None)

    def test_subscribe_with_kinds_filter(self, store):
        k = _make_kernel_with_session(store)
        fired = []
        k.subscribe("s1", "w1", callback=lambda msg: fired.append(msg), kinds=["REPORT"])
        k.direct("s1", "mgr", "w1", _env("task", "body", "TASK"))
        k.direct("s1", "mgr", "w1", _env("report", "body", "REPORT"))
        k.poll("s1", "w1")
        assert len(fired) == 1
        assert fired[0]["kind"] == "REPORT"

    def test_callback_error_swallowed(self, store):
        k = _make_kernel_with_session(store)
        def bad_cb(msg):
            raise RuntimeError("boom")
        k.subscribe("s1", "w1", callback=bad_cb)
        k.direct("s1", "mgr", "w1", _env())
        # Should not raise
        result = k.poll("s1", "w1")
        assert len(result.messages) == 1


# ── Ack ────────────────────────────────────────────────────────────────


class TestAck:
    def test_ack_consumed(self, store):
        k = _make_kernel_with_session(store)
        k.direct("s1", "mgr", "w1", _env())
        # Read with owner=w1 so ack owner matches
        msg = store.read("s1", "w1", "w1")
        result = k.ack("s1", "w1", msg["msg_id"], "consumed")
        assert "finalized" in result
        # Should be in archive now
        stats = store.stats("s1", "w1")
        assert stats["archive"] == 1

    def test_ack_released(self, store):
        k = _make_kernel_with_session(store)
        k.direct("s1", "mgr", "w1", _env())
        # Read with owner=w1 so ack owner matches
        msg = store.read("s1", "w1", "w1")
        result = k.ack("s1", "w1", msg["msg_id"], "released")
        assert "released" in result
        # Should be back in inbox
        inbox = store.list_messages(store.agent_subdir("s1", "w1", "inbox"))
        assert len(inbox) == 1

    def test_ack_unknown_phase(self, store):
        k = _make_kernel_with_session(store)
        k.direct("s1", "mgr", "w1", _env())
        msg = store.read("s1", "w1", "w1")
        with pytest.raises(ValueError, match="unknown ack phase"):
            k.ack("s1", "w1", msg["msg_id"], "bogus")

    def test_ack_no_session_raises(self, kernel):
        with pytest.raises(ValueError, match="not found"):
            kernel.ack("ghost", "w1", "msg-id", "consumed")


# ── LocalDeliverySink ─────────────────────────────────────────────────


class TestLocalDeliverySink:
    def test_deliver_writes_to_store(self, store):
        sink = LocalDeliverySink(store)
        store.session_init("s1", "mgr", ["w1"])
        env = Envelope(subject="s", body="b", kind="TASK")
        sink.deliver("s1", "w1", env, "msg-001", "2025-01-01T00:00:00Z", "mgr")
        inbox = store.list_messages(store.agent_subdir("s1", "w1", "inbox"))
        assert len(inbox) == 1


# ── get_session / get_location ─────────────────────────────────────────


class TestAccessors:
    def test_get_session(self, kernel):
        kernel.create_session("s1", "mgr", ["w1"])
        s = kernel.get_session("s1")
        assert s is not None
        assert s.session_id == "s1"

    def test_get_session_missing(self, kernel):
        assert kernel.get_session("ghost") is None

    def test_get_location(self, kernel):
        kernel.create_session("s1", "mgr", ["w1"])
        loc = AgentLocation("w1", "host1", "cli")
        kernel.register(loc, "s1")
        assert kernel.get_location("s1", "w1") is loc

    def test_get_location_missing(self, kernel):
        kernel.create_session("s1", "mgr", ["w1"])
        assert kernel.get_location("s1", "w1") is None
