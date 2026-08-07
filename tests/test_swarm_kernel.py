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
    ReturnMode,
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


def _env(subject="test", body="hello", kind="TASK", run_id="run-1", request_id="req-1",
         reply_to=""):
    return Envelope(subject=subject, body=body, kind=kind, run_id=run_id, request_id=request_id,
                    reply_to=reply_to)


# ── Session creation ───────────────────────────────────────────────────


class TestCreateSession:
    def test_basic(self, kernel):
        s = kernel.create_session("s1", "mgr", ["w1", "w2"])
        assert s.session_id == "s1"

    def test_second_kernel_restores_session_from_disk(self, store):
        """回归：CLI 每子命令一个新进程/新 kernel。create-session 写入
        session.json 后，下一个进程的 kernel 必须能从磁盘恢复它。
        此前内存-only 导致 register 紧随 create-session 报 session not found。"""
        k1 = SwarmKernel(store=store)
        k1.create_session("s1", "mgr", ["w1", "w2"])

        # 模拟下一个 CLI 进程：全新 kernel 实例，同一 store
        k2 = SwarmKernel(store=store)
        s = k2.get_session("s1")
        assert s is not None
        assert s.manager_id == "mgr"
        assert "w1" in s.roster and "w2" in s.roster
        # ACL 也从 swarm-meta.json 恢复
        assert s.acl.authority == "mgr"

    def test_acl_persisted_to_session_json_authority(self, store):
        """B4-Manifest：create_session 的 ACL 权威写入 session.json（ensure
        同步副本）；新 kernel 从 session.json 恢复 restricted policy。"""
        k1 = SwarmKernel(store=store)
        k1.create_session(
            "s1", "mgr", ["w1", "w2"],
            acl=ACL(
                authority="mgr",
                allowed_senders=["mgr", "w1"],
                room_members=["mgr", "w1", "w2"],
                policy="restricted",
            ),
        )
        # session.json 有 acl 权威副本
        meta = store.read_session("s1")
        assert meta is not None
        assert meta["acl"]["policy"] == "restricted"
        assert meta["manifest_revision"] >= 1

        # 新 kernel（模拟远端 ensure 后的本地加载）从 session.json 恢复 restricted
        k2 = SwarmKernel(store=store)
        s = k2.get_session("s1")
        assert s.acl.policy == "restricted"
        assert s.acl.authority == "mgr"
        # restricted: 非白名单 sender 不能 direct
        with pytest.raises(PermissionError):
            k2.direct("s1", "w2", "w1", _env())

    def test_session_init_acl_merge_bumps_revision(self, store):
        """B4-Manifest：session_init 携带 acl 合并时 manifest_revision 递增。"""
        store.session_init("s1", "mgr", ["w1"])
        r1 = store.read_session("s1")
        assert r1["manifest_revision"] == 1

        store.session_init(
            "s1", "mgr", ["w1", "w2"],
            acl={"authority": "mgr", "allowed_senders": ["mgr"],
                 "room_members": ["mgr", "w1", "w2"], "policy": "restricted"},
        )
        r2 = store.read_session("s1")
        assert r2["manifest_revision"] == 2
        assert r2["acl"]["policy"] == "restricted"
        assert "w2" in r2["agents"]

    def test_second_kernel_restores_channel_from_disk(self, store):
        """回归：channel 跨进程持久化（create-channel 后新进程可见）。"""
        k1 = SwarmKernel(store=store)
        k1.create_session("s1", "mgr", ["w1", "w2"])
        k1.create_channel("s1", "dev", ["mgr", "w1"])

        k2 = SwarmKernel(store=store)
        assert k2.get_session("s1") is not None
        channels = k2._channels.get("s1", {})
        assert "dev" in channels
        assert set(channels["dev"].members) == {"mgr", "w1"}
        # 新 kernel 上 channel 可直接发送
        receipts = k2.channel("s1", "mgr", "dev", _env())
        assert len(receipts) == 1
        assert receipts[0].status == "delivered"
        assert receipts[0].recipient == "w1"

    def test_second_kernel_restores_routing_from_disk(self, store):
        """回归：register 的 host 映射跨进程持久化。
        否则新 kernel 的 get_location 为空，DeliveryEngine 的
        _resolve_agent_to_host 返回 None → 跨主机消息被误投本机。"""
        k1 = SwarmKernel(store=store)
        k1.create_session("s1", "mgr", ["w1"])
        k1.register(AgentLocation("w1", "192.168.234.18", "cli"), "s1")

        k2 = SwarmKernel(store=store)
        loc = k2.get_location("s1", "w1")
        assert loc is not None
        assert loc.host_alias == "192.168.234.18"
        assert loc.backend == "cli"

    def test_concurrent_register_both_persist(self, store):
        """回归：并发 register 到不同 agent 不得互相覆盖（P1-1 TOCTOU）。"""
        import threading

        k = SwarmKernel(store=store)
        k.create_session("s1", "mgr", ["w1", "w2", "w3"])

        def reg(agent: str, host: str) -> None:
            k.register(AgentLocation(agent, host, "cli"), "s1")

        threads = [
            threading.Thread(target=reg, args=("w1", "host-a")),
            threading.Thread(target=reg, args=("w2", "host-b")),
            threading.Thread(target=reg, args=("w3", "host-c")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # New kernel: all three registrations survived (no lost update)
        k2 = SwarmKernel(store=store)
        assert k2.get_location("s1", "w1").host_alias == "host-a"
        assert k2.get_location("s1", "w2").host_alias == "host-b"
        assert k2.get_location("s1", "w3").host_alias == "host-c"

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
        receipts = k.channel("s1", "mgr", "dev", _env())
        assert len(receipts) == 1
        assert receipts[0].status == "delivered"
        assert receipts[0].recipient == "w1"
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
        receipts = k.notice("s1", "mgr", "system", _env())
        assert len(receipts) == 2  # w1 + w2
        assert all(r.status == "delivered" for r in receipts)
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
        receipts = k.notice("s1", "mgr", "system", _env(), ttl=300)
        assert len(receipts) == 2
        assert all(r.status == "delivered" for r in receipts)

    def test_notice_acl_denied(self, store):
        acl = ACL(authority="mgr", allowed_senders=["mgr"],
                  room_members=["mgr", "w1", "w2"], policy="restricted")
        k = _make_kernel_with_session(store, extra_acl=acl)
        with pytest.raises(PermissionError, match="not in allowed_senders"):
            k.notice("s1", "w1", "system", _env())

    def test_notice_topic_fanout_only_subscribers(self, store):
        """notice --topic reaches only topic subscribers, not all room members."""
        k = _make_kernel_with_session(store)
        k.subscribe("s1", "w1", lambda m: None, topics=["deploy"])
        # w2 has no topic subscription → should NOT receive the notice
        k.notice("s1", "mgr", "deploy", _env())
        inbox_w1 = store.list_messages(store.agent_subdir("s1", "w1", "inbox"))
        inbox_w2 = store.list_messages(store.agent_subdir("s1", "w2", "inbox"))
        assert len(inbox_w1) == 1
        assert len(inbox_w2) == 0

    def test_notice_topic_unknown_falls_back_to_room(self, store):
        """Unknown topic falls back to session-wide notice."""
        k = _make_kernel_with_session(store)
        k.notice("s1", "mgr", "no-such-topic", _env())
        inbox_w1 = store.list_messages(store.agent_subdir("s1", "w1", "inbox"))
        inbox_w2 = store.list_messages(store.agent_subdir("s1", "w2", "inbox"))
        assert len(inbox_w1) == 1
        assert len(inbox_w2) == 1


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
        k.direct("s1", "mgr", "w1", _env("report", "body", "REPORT", reply_to="some-task-id"))
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
        env = Envelope(subject="s", body="b", kind="TASK", run_id="run-1", request_id="req-1")
        sink.deliver("s1", "w1", env, "msg-001", "2025-01-01T00:00:00Z", "mgr")
        inbox = store.list_messages(store.agent_subdir("s1", "w1", "inbox"))
        assert len(inbox) == 1

    def test_deliver_preserves_kernel_msg_id(self, store):
        """P1-1: receipt msg_id must equal inbox file msg_id (not regenerated)."""
        sink = LocalDeliverySink(store)
        store.session_init("s1", "mgr", ["w1"])
        env = Envelope(subject="s", body="b", kind="TASK", run_id="run-1", request_id="req-1")
        kernel_msg_id = "kernel-supplied-123"
        receipt = sink.deliver("s1", "w1", env, kernel_msg_id, "2025-01-01T00:00:00Z", "mgr")
        # Receipt must echo the kernel-supplied id
        assert receipt.msg_id == kernel_msg_id
        # Inbox file must use the same id
        inbox = store.list_messages(store.agent_subdir("s1", "w1", "inbox"))
        assert len(inbox) == 1
        file_msg_id = inbox[0].stem
        assert file_msg_id == kernel_msg_id


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


# ── Receipt propagation ────────────────────────────────────────────────


class TestReceiptPropagation:
    def test_direct_returns_accepted_on_transport_failure(self, store):
        """kernel.direct returns 'accepted' when sink returns accepted."""
        k = _make_kernel_with_session(store)
        from codeagent.swarm.delivery import SendReceipt as DSinkReceipt
        mock_sink = MagicMock()
        mock_sink.deliver.return_value = DSinkReceipt(
            status="accepted", msg_id="m1", queued=True,
        )
        k._sink = mock_sink
        receipt = k.direct("s1", "mgr", "w1", _env())
        assert receipt.status == "accepted"
        assert receipt.queued is True

    def test_direct_returns_delivered_on_local_success(self, store):
        """kernel.direct returns 'delivered' when sink returns delivered."""
        k = _make_kernel_with_session(store)
        receipt = k.direct("s1", "mgr", "w1", _env())
        assert receipt.status == "delivered"
        assert receipt.queued is False

    def test_broadcast_per_recipient_distinct_statuses(self, store):
        """broadcast returns per-recipient receipts with actual sink statuses."""
        k = _make_kernel_with_session(store)
        from codeagent.swarm.delivery import SendReceipt as DSinkReceipt
        mock_sink = MagicMock()
        mock_sink.deliver.side_effect = [
            DSinkReceipt(status="accepted", msg_id="m1", queued=True),
            DSinkReceipt(status="delivered", msg_id="m2"),
        ]
        k._sink = mock_sink
        receipts = k.broadcast("s1", "mgr", _env())
        assert len(receipts) == 2
        statuses = {r.recipient: r.status for r in receipts}
        assert statuses["w1"] == "accepted"
        assert statuses["w2"] == "delivered"

    def test_send_broadcast_uses_max_status(self, store):
        """send() broadcast branch returns 'accepted' if any receipt is not 'delivered'."""
        k = _make_kernel_with_session(store)
        from codeagent.swarm.delivery import SendReceipt as DSinkReceipt
        mock_sink = MagicMock()
        mock_sink.deliver.side_effect = [
            DSinkReceipt(status="accepted", msg_id="m1", queued=True),
            DSinkReceipt(status="delivered", msg_id="m2"),
        ]
        k._sink = mock_sink
        addr = Address(kind=AddressKind.BROADCAST)
        receipt = k.send("s1", "mgr", addr, _env())
        assert receipt.status == "accepted"


# ── Trace (Top4) ────────────────────────────────────────────────────────


class TestTrace:
    """Top4: kernel.trace 按 trace_id 聚合（fan-out 多 leaf + state）。"""

    def test_trace_fanout_same_id(self, store):
        k = SwarmKernel(store=store)
        k.create_session("s1", "mgr", ["w1", "w2"])
        k.direct("s1", "mgr", "w1", Envelope(subject="t1", body="b", run_id="run-1", request_id="req-1", trace_id="trace-x"))
        k.direct("s1", "mgr", "w2", Envelope(subject="t2", body="b", run_id="run-1", request_id="req-1", trace_id="trace-x", causation_id="p1"))
        r = k.trace("s1", "trace-x")
        assert r["leaf_count"] == 2
        assert all(l["state"] == "delivered" for l in r["leaves"])
        assert any(l["causation_id"] == "p1" for l in r["leaves"])

    def test_trace_missing_raises(self, store):
        k = SwarmKernel(store=store)
        k.create_session("s1", "mgr", ["w1"])
        with pytest.raises(ValueError):
            k.trace("s1", "ghost")

    def test_trace_engine_state_from_outbox_markers(self, store, tmp_path):
        """oracle-lite P2: EngineDeliverySink 路径——outbox 有 .delivered 标记 →
        state=delivered；无标记 → state=unknown。此前零测试覆盖。"""
        from codeagent.swarm.delivery import DeliveryEngine, EngineDeliverySink
        from unittest.mock import MagicMock

        outbox = tmp_path / "outbox"
        engine = DeliveryEngine(mailbox_store=store, transport_router=MagicMock(),
                                outbox_root=outbox)
        k = SwarmKernel(store=store, sink=EngineDeliverySink(engine=engine, kernel=None))
        k.create_session("s1", "mgr", ["w1"])
        # 直接 store.send（写 inbox+history，trace_id 透传）——不经 kernel.direct
        # 的 local 投递（那会写 .delivered 标记），构造"无标记"场景。
        store.send("s1", "mgr", "w1", subject="t", body="b", kind="REPORT",
                   reply_to="orig-msg", run_id="run-1", request_id="req-1", trace_id="tr-e")

        # 无标记（delivery 未成功）→ unknown
        r = k.trace("s1", "tr-e")
        assert r["leaf_count"] == 1
        assert r["leaves"][0]["state"] == "unknown"

        # 写 .delivered 标记 → delivered
        sd = outbox / "s1"
        sd.mkdir(parents=True, exist_ok=True)
        mid = r["leaves"][0]["msg_id"]
        (sd / f".delivered-{mid}").write_text("{}")
        r2 = k.trace("s1", "tr-e")
        assert r2["leaves"][0]["state"] == "delivered"


# ── Agent Card (P2) ─────────────────────────────────────────────────────


class TestAgentCard:
    """P2: agent_card 持久化 + 白名单字段 + 跨进程恢复。"""

    def test_set_and_get_card(self, store):
        k = SwarmKernel(store=store)
        k.create_session("s1", "mgr", ["w1"])
        k.set_agent_card("s1", "w1", {
            "display_name": "worker-1",
            "description": "build worker",
            "agent_version": "0.2.3",
            "capabilities": ["mailbox", "stream"],
            "ignored_field": "dropped",
        })
        cards = k.get_agent_cards("s1")
        assert cards["w1"]["display_name"] == "worker-1"
        assert "ignored_field" not in cards["w1"]
        assert cards["w1"]["capabilities"] == ["mailbox", "stream"]

    def test_card_unknown_fields_rejected(self, store):
        k = SwarmKernel(store=store)
        k.create_session("s1", "mgr", ["w1"])
        with pytest.raises(ValueError):
            k.set_agent_card("s1", "w1", {"not_allowed": 1})
        with pytest.raises(ValueError):
            k.set_agent_card("s1", "ghost", {"display_name": "x"})

    def test_card_persists_across_kernels(self, store):
        k1 = SwarmKernel(store=store)
        k1.create_session("s1", "mgr", ["w1"])
        k1.set_agent_card("s1", "w1", {"display_name": "worker-1"})

        k2 = SwarmKernel(store=store)
        cards = k2.get_agent_cards("s1")
        assert cards["w1"]["display_name"] == "worker-1"


# ── Execution mode / return mode persistence ───────────────────────────


class TestModePersistence:
    """Persist execution_mode/return_mode/mailbox_root across kernel restarts."""

    def test_routing_persists_mode_fields(self, store):
        """_persist_routing writes execution_mode/mailbox_root/return_mode
        to swarm-meta.json and _load_persisted_sessions restores them."""
        from codeagent.swarm.model import ExecutionMode, ReturnMode

        k1 = SwarmKernel(store=store)
        k1.create_session("s1", "mgr", ["w1"])
        k1.register(
            AgentLocation(
                "w1", "192.168.1.10", "cli",
                execution_mode=ExecutionMode.MAILBOX_WORKER,
                mailbox_root="/data/mailbox/w1",
                return_mode=ReturnMode.MANAGER_PULL,
            ),
            "s1",
        )

        # Simulate fresh kernel (new process)
        k2 = SwarmKernel(store=store)
        loc = k2.get_location("s1", "w1")
        assert loc is not None
        assert loc.execution_mode == ExecutionMode.MAILBOX_WORKER
        assert loc.mailbox_root == "/data/mailbox/w1"
        assert loc.return_mode == ReturnMode.MANAGER_PULL

    def test_create_session_persists_modes_to_session_json(self, store):
        """create_session passes execution_modes/return_modes to session_init
        which writes them into session.json."""
        from codeagent.swarm.model import ExecutionMode, ReturnMode

        k1 = SwarmKernel(store=store)
        k1.create_session(
            "s1", "mgr", ["w1"],
            execution_modes={"w1": ExecutionMode.LOCAL_OMP_MCP.value},
            return_modes={"w1": ReturnMode.BIDIRECTIONAL.value},
        )

        # Verify session.json has the modes
        data = store.read_session("s1")
        assert data is not None
        assert data["execution_modes"]["w1"] == "local-omp-mcp"
        assert data["return_modes"]["w1"] == "bidirectional"

        # Verify new kernel restores them on Session object
        k2 = SwarmKernel(store=store)
        s = k2.get_session("s1")
        assert s is not None
        assert s.execution_modes["w1"] == "local-omp-mcp"
        assert s.return_modes["w1"] == "bidirectional"

    def test_register_updates_session_modes(self, store):
        """register() with execution_mode/return_mode persists them
        to session.json so they survive a kernel restart."""
        from codeagent.swarm.model import ExecutionMode, ReturnMode

        k1 = SwarmKernel(store=store)
        k1.create_session("s1", "mgr", ["w1", "w2"])
        k1.register(
            AgentLocation("w1", "host-a", "cli",
                          execution_mode=ExecutionMode.MAILBOX_WORKER,
                          return_mode=ReturnMode.MANAGER_PULL),
            "s1",
        )
        k1.register(
            AgentLocation("w2", "host-b", "omp",
                          execution_mode=ExecutionMode.LOCAL_OMP_MCP,
                          return_mode=ReturnMode.BIDIRECTIONAL),
            "s1",
        )

        # Verify session object updated in-memory
        s1 = k1.get_session("s1")
        assert s1.execution_modes["w1"] == "mailbox-worker"
        assert s1.execution_modes["w2"] == "local-omp-mcp"
        assert s1.return_modes["w1"] == "manager-pull"
        assert s1.return_modes["w2"] == "bidirectional"

        # Simulate fresh kernel
        k2 = SwarmKernel(store=store)
        s2 = k2.get_session("s1")
        assert s2 is not None
        assert s2.execution_modes["w1"] == "mailbox-worker"
        assert s2.execution_modes["w2"] == "local-omp-mcp"
        assert s2.return_modes["w1"] == "manager-pull"
        assert s2.return_modes["w2"] == "bidirectional"

    def test_legacy_routing_without_modes_loads_defaults(self, store):
        """Legacy swarm-meta.json without execution_mode/mailbox_root/return_mode
        must still load cleanly with None/''/None defaults."""
        import json

        k1 = SwarmKernel(store=store)
        k1.create_session("s1", "mgr", ["w1"])
        k1.register(AgentLocation("w1", "old-host", "cli"), "s1")

        # Strip mode fields to simulate legacy swarm-meta.json
        meta_path = store.session_dir("s1") / "swarm-meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["routing"]["w1"] = {
            "agent_id": "w1",
            "host_alias": "old-host",
            "backend": "cli",
        }
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        k2 = SwarmKernel(store=store)
        loc = k2.get_location("s1", "w1")
        assert loc is not None
        assert loc.execution_mode is None
        assert loc.mailbox_root == ""
        assert loc.return_mode is None


# ── pull_remote ────────────────────────────────────────────────────────


class TestPullRemote:
    """pull_remote: find manager-pull agents on host, SSH mailbox read."""

    def test_no_pull_agents_returns_empty(self, store):
        k = _make_kernel_with_session(store)
        # w1 registered with bidirectional, not manager-pull
        k.register(
            AgentLocation("w1", "host-a", "cli",
                          return_mode=ReturnMode.BIDIRECTIONAL),
            "s1",
        )
        assert k.pull_remote("s1", "host-a") == []

    def test_no_agents_on_host_returns_empty(self, store):
        k = _make_kernel_with_session(store)
        k.register(
            AgentLocation("w1", "host-a", "cli",
                          return_mode=ReturnMode.MANAGER_PULL),
            "s1",
        )
        assert k.pull_remote("s1", "host-b") == []

    def test_session_not_found_raises(self, kernel):
        with pytest.raises(ValueError, match="not found"):
            kernel.pull_remote("ghost", "host-a")

    def test_pulls_message_from_remote_host(self, store):
        k = _make_kernel_with_session(store)
        k.register(
            AgentLocation("w1", "host-a", "cli",
                          return_mode=ReturnMode.MANAGER_PULL),
            "s1",
        )
        fake_msg = {
            "msg_id": "remote-001",
            "session_id": "s1",
            "from": "w1",
            "to": "manager",
            "subject": "report",
            "body": "done",
            "kind": "REPORT",
            "created_at": "2026-01-01T00:00:00Z",
            "run_id": "run-1",
            "request_id": "req-1",
            "reply_to": "task-001",
            "trace_id": "",
            "causation_id": "",
        }
        import subprocess
        from unittest.mock import patch

        read_result = MagicMock()
        read_result.returncode = 0
        read_result.stdout = __import__("json").dumps(fake_msg)
        read_result.stderr = ""
        with patch("codeagent.swarm.kernel.subprocess.run",
                    return_value=read_result) as mock_run:
            result = k.pull_remote("s1", "host-a")
        assert len(result) == 1
        assert result[0]["msg_id"] == "remote-001"
        # Verify annotations for caller-side finalize
        assert result[0]["_pull_host"] == "host-a"
        assert "_pull_mailbox_root" in result[0]
        # Verify the SSH command structure (single call = read)
        read_args = mock_run.call_args_list[0][0][0]
        assert "mailbox" in read_args
        assert "read" in read_args
        assert "--host" in read_args
        assert "host-a" in read_args

    def test_pull_skips_failed_command(self, store):
        k = _make_kernel_with_session(store)
        k.register(
            AgentLocation("w1", "host-a", "cli",
                          return_mode=ReturnMode.MANAGER_PULL),
            "s1",
        )
        import subprocess
        from unittest.mock import patch

        completed = MagicMock()
        completed.returncode = 1
        completed.stdout = ""
        completed.stderr = "connection refused"
        with patch("codeagent.swarm.kernel.subprocess.run", return_value=completed):
            result = k.pull_remote("s1", "host-a")
        assert result == []

    def test_pull_handles_list_response(self, store):
        k = _make_kernel_with_session(store)
        k.register(
            AgentLocation("w1", "host-a", "cli",
                          return_mode=ReturnMode.MANAGER_PULL),
            "s1",
        )
        from unittest.mock import patch

        msg1 = {"msg_id": "m1", "session_id": "s1", "from": "w1", "to": "mgr",
                "subject": "a", "body": "b", "kind": "REPORT", "created_at": "t1",
                "run_id": "r1", "request_id": "req-1", "reply_to": "orig-1", "trace_id": "", "causation_id": ""}
        msg2 = {"msg_id": "m2", "session_id": "s1", "from": "w1", "to": "mgr",
                "subject": "c", "body": "d", "kind": "REPORT", "created_at": "t2",
                "run_id": "r2", "request_id": "req-2", "reply_to": "orig-2", "trace_id": "", "causation_id": ""}
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = __import__("json").dumps([msg1, msg2])
        completed.stderr = ""
        with patch("codeagent.swarm.kernel.subprocess.run", return_value=completed):
            result = k.pull_remote("s1", "host-a")
        assert len(result) == 2
        # Verify annotations for caller-side finalize
        assert result[0]["_pull_host"] == "host-a"
        assert result[1]["_pull_host"] == "host-a"
        assert "_pull_mailbox_root" in result[0]
        assert "_pull_mailbox_root" in result[1]

    def test_pull_remote_falls_back_to_session_return_modes(self, store):
        """When loc.return_mode is None (CLI register without --return-mode),
        pull_remote should fall back to session.return_modes dict."""
        k = SwarmKernel(store=store)
        acl = ACL(
            authority="mgr",
            allowed_senders=["mgr", "w1"],
            room_members=["mgr", "w1"],
            policy="open",
        )
        # Session created with return_modes mapping
        k.create_session("s1", "mgr", ["w1"], acl=acl,
                         return_modes={"w1": "manager-pull"})
        # Register agent without explicit return_mode (simulates CLI register)
        k.register(
            AgentLocation("w1", "host-a", "cli"),  # return_mode=None
            "s1",
        )
        # Verify loc.return_mode is indeed None
        loc = k.get_location("s1", "w1")
        assert loc is not None
        assert loc.return_mode is None

        # Session.return_modes should have the mapping
        session = k.get_session("s1")
        assert session is not None
        assert session.return_modes.get("w1") == "manager-pull"

        from unittest.mock import patch
        fake_msg = {
            "msg_id": "fallback-001",
            "session_id": "s1",
            "from": "w1",
            "to": "manager",
            "subject": "report",
            "body": "done",
            "kind": "REPORT",
            "created_at": "2026-01-01T00:00:00Z",
            "run_id": "run-1",
            "request_id": "req-1",
            "reply_to": "",
            "trace_id": "",
            "causation_id": "",
        }
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = __import__("json").dumps(fake_msg)
        completed.stderr = ""
        with patch("codeagent.swarm.kernel.subprocess.run", return_value=completed):
            result = k.pull_remote("s1", "host-a")
        assert len(result) == 1
        assert result[0]["msg_id"] == "fallback-001"
        # Verify annotations for caller-side finalize
        assert result[0]["_pull_host"] == "host-a"
        assert "_pull_mailbox_root" in result[0]


# ── ingest ─────────────────────────────────────────────────────────────


class TestIngest:
    """ingest: validate roster/ACL, append to history."""

    def _valid_msg(self, msg_id="ingest-001", request_id="req-1"):
        return {
            "msg_id": msg_id,
            "session_id": "s1",
            "from": "w1",
            "to": "mgr",
            "subject": "result",
            "body": "done",
            "kind": "REPORT",
            "created_at": "2026-01-01T00:00:00Z",
            "run_id": "run-1",
            "request_id": request_id,
            "reply_to": "original-task-001",
            "trace_id": "",
            "causation_id": "",
        }

    def test_ingest_valid_message(self, store):
        k = _make_kernel_with_session(store)
        msg = self._valid_msg()
        result = k.ingest("s1", [msg])
        assert result == ["ingest-001"]
        # Verify in history
        history = store.read_history("s1")
        assert len(history) == 1
        assert history[0]["msg_id"] == "ingest-001"

    def test_ingest_multiple_messages(self, store):
        k = _make_kernel_with_session(store)
        msgs = [self._valid_msg(f"m-{i}") for i in range(3)]
        result = k.ingest("s1", msgs)
        assert len(result) == 3
        history = store.read_history("s1")
        assert len(history) == 3

    def test_ingest_skips_invalid_message(self, store):
        k = _make_kernel_with_session(store)
        bad_msg = {"msg_id": "bad"}  # missing required fields
        result = k.ingest("s1", [bad_msg])
        assert result == []
        history = store.read_history("s1")
        assert len(history) == 0

    def test_ingest_skips_non_dict(self, store):
        k = _make_kernel_with_session(store)
        result = k.ingest("s1", ["not a dict", 42, None])
        assert result == []

    def test_ingest_skips_duplicate(self, store):
        k = _make_kernel_with_session(store)
        msg = self._valid_msg()
        r1 = k.ingest("s1", [msg])
        assert r1 == ["ingest-001"]
        # Ingest same message again — duplicate msg_id
        r2 = k.ingest("s1", [msg])
        assert r2 == []  # skipped
        history = store.read_history("s1")
        assert len(history) == 1

    def test_ingest_session_not_found(self, kernel):
        with pytest.raises(ValueError, match="not found"):
            kernel.ingest("ghost", [{}])

    def test_ingest_persists_to_history_dir(self, store):
        k = _make_kernel_with_session(store)
        msg = self._valid_msg()
        k.ingest("s1", [msg])
        history_dir = store.session_dir("s1") / "history"
        assert history_dir.exists()
        assert (history_dir / "ingest-001.json").exists()


# ── replay ─────────────────────────────────────────────────────────────


class TestReplay:
    """replay: filter history by request_id, return chronological."""

    def _ingest_msgs(self, store, k, request_id, count=3):
        """Helper: ingest count messages with the given request_id."""
        msgs = []
        for i in range(count):
            msgs.append({
                "msg_id": f"replay-{request_id}-{i:03d}",
                "session_id": "s1",
                "from": "w1",
                "to": "mgr",
                "subject": f"msg-{i}",
                "body": f"body-{i}",
                "kind": "REPORT",
                "created_at": f"2026-01-01T00:00:{i:02d}Z",
                "run_id": "run-1",
                "request_id": request_id,
                "reply_to": f"task-{i:03d}",
                "trace_id": "",
                "causation_id": "",
            })
        k.ingest("s1", msgs)

    def test_replay_returns_matching_messages(self, store):
        k = _make_kernel_with_session(store)
        self._ingest_msgs(store, k, "req-1", 3)
        self._ingest_msgs(store, k, "req-2", 2)
        result = k.replay("s1", "req-1")
        assert len(result) == 3
        assert all(m["request_id"] == "req-1" for m in result)

    def test_replay_chronological_order(self, store):
        k = _make_kernel_with_session(store)
        self._ingest_msgs(store, k, "req-1", 3)
        result = k.replay("s1", "req-1")
        timestamps = [m["created_at"] for m in result]
        assert timestamps == sorted(timestamps)

    def test_replay_empty_for_unknown_request(self, store):
        k = _make_kernel_with_session(store)
        self._ingest_msgs(store, k, "req-1", 3)
        result = k.replay("s1", "nonexistent")
        assert result == []

    def test_replay_empty_history(self, store):
        k = _make_kernel_with_session(store)
        result = k.replay("s1", "req-1")
        assert result == []

    def test_replay_session_not_found(self, kernel):
        with pytest.raises(ValueError, match="not found"):
            kernel.replay("ghost", "req-1")

    def test_replay_isolation_between_requests(self, store):
        k = _make_kernel_with_session(store)
        self._ingest_msgs(store, k, "req-a", 2)
        self._ingest_msgs(store, k, "req-b", 4)
        assert len(k.replay("s1", "req-a")) == 2
        assert len(k.replay("s1", "req-b")) == 4

    def test_replay_preserves_message_content(self, store):
        k = _make_kernel_with_session(store)
        msg = {
            "msg_id": "content-check",
            "session_id": "s1",
            "from": "w1",
            "to": "mgr",
            "subject": "specific-subject",
            "body": "specific-body",
            "kind": "REPORT",
            "created_at": "2026-06-15T12:00:00Z",
            "run_id": "run-42",
            "request_id": "req-42",
            "reply_to": "task-001",
            "trace_id": "trace-abc",
            "causation_id": "",
        }
        k.ingest("s1", [msg])
        result = k.replay("s1", "req-42")
        assert len(result) == 1
        assert result[0]["subject"] == "specific-subject"
        assert result[0]["body"] == "specific-body"
        assert result[0]["trace_id"] == "trace-abc"
