"""Tests for the TCP daemon — registry, daemon, and server facade.

Covers:
  - start / stop lifecycle
  - message routing  A → daemon → B
  - session isolation (different sessions don't leak)
  - concurrent pressure (1000 messages)
  - disconnect recovery with spool replay
  - duplicate session_id handling
"""
from __future__ import annotations

import asyncio
import json
import struct
import time
import uuid
from pathlib import Path

import pytest

from codeagent.mailbox.store import MailboxStore
from codeagent.tcp.daemon import TCPConnectionDaemon
from codeagent.tcp.protocol import Frame, FrameType, decode_frame, encode_frame
from codeagent.tcp.registry import ConnectionRegistry, SessionRoutingTable
from codeagent.tcp.server import MailboxDaemon
from codeagent.tcp.spool import SpoolStore


# ── helpers ─────────────────────────────────────────────────────────────


def _make_msg_frame(
    session_id: str,
    from_id: str = "main",
    to_id: str = "worker",
    subject: str = "test",
    body: str = "hello",
    kind: str = "REPORT",
    msg_id: str | None = None,
) -> Frame:
    """Build a MESSAGE Frame with the standard envelope fields."""
    return Frame(
        type=FrameType.MESSAGE,
        session_id=session_id,
        payload={
            "session_id": session_id,
            "from": from_id,
            "to": to_id,
            "subject": subject,
            "body": body,
            "kind": kind,
            "msg_id": msg_id or f"{from_id}_{uuid.uuid4().hex[:8]}",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )


async def _connect_client(
    host: str, port: int, host_alias: str,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a TCP connection and complete the HELLO/READY handshake."""
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(encode_frame(Frame(
        type=FrameType.HELLO,
        session_id="",
        payload={"host_alias": host_alias},
    )))
    await writer.drain()

    # Read READY
    raw = await asyncio.wait_for(reader.readexactly(4), timeout=5.0)
    length = struct.unpack(">I", raw)[0]
    body = await asyncio.wait_for(reader.readexactly(length), timeout=5.0)
    frame, _ = decode_frame(raw + body)
    assert frame.type == FrameType.READY, f"expected READY, got {frame.type}"
    return reader, writer


async def _read_frame_from(
    reader: asyncio.StreamReader, timeout: float = 5.0,
) -> Frame:
    """Read and decode the next Frame from *reader*."""
    raw = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
    length = struct.unpack(">I", raw)[0]
    body = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
    frame, _ = decode_frame(raw + body)
    return frame


async def _drain_ack(reader: asyncio.StreamReader, timeout: float = 2.0) -> Frame:
    """Read the next frame and assert it's an ACK."""
    frame = await _read_frame_from(reader, timeout=timeout)
    assert frame.type == FrameType.ACK, f"expected ACK, got {frame.type}"
    return frame


# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def stores(tmp_path: Path) -> tuple[MailboxStore, SpoolStore]:
    """Create fresh mailbox + spool stores rooted under *tmp_path*."""
    return MailboxStore(tmp_path / "mailbox"), SpoolStore(tmp_path / "spool")


@pytest.fixture
async def daemon_factory(stores):
    """Return an async factory that starts a MailboxDaemon on an ephemeral port."""
    mailbox_store, spool_store = stores
    daemons: list[MailboxDaemon] = []

    async def _create(**kwargs) -> MailboxDaemon:
        d = MailboxDaemon(
            host="127.0.0.1",
            port=0,  # ephemeral
            mailbox_store=mailbox_store,
            spool_store=spool_store,
            **kwargs,
        )
        await d.start()
        daemons.append(d)
        return d

    yield _create

    # Cleanup
    for d in daemons:
        try:
            await d.stop()
        except Exception:
            pass


def _init_session(mailbox_store: MailboxStore, session_id: str,
                   manager: str = "main", agents: list[str] | None = None) -> None:
    """Create a session in the mailbox store with the given roster."""
    agents = agents or []
    all_agents = sorted(set(agents) | {manager})
    non_manager = [a for a in all_agents if a != manager]
    mailbox_store.session_init(session_id, manager, non_manager)


def _count_inbox(mailbox_store: MailboxStore, session_id: str, agent_id: str) -> int:
    """Return the number of messages in *agent_id*'s inbox."""
    inbox = mailbox_store.agent_subdir(session_id, agent_id, "inbox")
    return len(list(inbox.glob("*.json"))) if inbox.exists() else 0


def _read_inbox_bodies(mailbox_store: MailboxStore, session_id: str, agent_id: str) -> list[str]:
    """Return message bodies from *agent_id*'s inbox, sorted by mtime."""
    inbox = mailbox_store.agent_subdir(session_id, agent_id, "inbox")
    if not inbox.exists():
        return []
    files = sorted(inbox.glob("*.json"), key=lambda f: f.stat().st_mtime)
    return [json.loads(f.read_text())["body"] for f in files]


# ── 1. Lifecycle ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_stop_lifecycle(daemon_factory):
    """Daemon starts, reports status, and stops cleanly."""
    d = await daemon_factory()

    status = d.status()
    assert status["running"] is True
    assert status["num_sessions"] == 0
    assert isinstance(status["connected_hosts"], list)
    assert isinstance(status["sessions"], dict)

    await d.stop()
    status = d.status()
    assert status["running"] is False


@pytest.mark.asyncio
async def test_start_returns_ephemeral_port(daemon_factory):
    """start() returns a nonzero port when binding to port 0."""
    d = await daemon_factory()
    status = d.status()
    assert status["port"] >= 0
    assert status["running"] is True


# ── 2. Message routing  A → daemon → B ──────────────────────────────────


@pytest.mark.asyncio
async def test_message_routing(stores, daemon_factory):
    """A full round-trip: main sends, daemon routes to worker's mailbox."""
    mailbox_store, spool_store = stores
    _init_session(mailbox_store, "test-sess", manager="main", agents=["worker"])

    d = await daemon_factory()
    port = d.status()["port"]

    # Register worker in the routing table
    d._routing.add_route("test-sess", "worker")

    # Worker connects
    w_reader, w_writer = await _connect_client("127.0.0.1", port, "worker")

    # main sends a message via the server facade
    result = await d.send_message(
        session_id="test-sess",
        from_id="main",
        to_id="worker",
        msg={
            "subject": "route-test",
            "body": "ping from main",
            "kind": "REPORT",
        },
    )
    assert result["msg_id"], "msg_id should be populated"

    # Verify the message landed in the local mailbox inbox immediately
    assert _count_inbox(mailbox_store, "test-sess", "worker") == 1
    bodies = _read_inbox_bodies(mailbox_store, "test-sess", "worker")
    assert bodies == ["ping from main"]

    # Worker should also receive the forwarded MESSAGE over the wire
    frame = await _read_frame_from(w_reader, timeout=3.0)
    assert frame.type == FrameType.MESSAGE
    assert frame.payload["from"] == "main"
    assert frame.payload["to"] == "worker"
    assert frame.payload["body"] == "ping from main"
    assert frame.session_id == "test-sess"

    w_writer.close()
    await w_writer.wait_closed()


@pytest.mark.asyncio
async def test_inbound_message_writes_to_inbox(stores, daemon_factory):
    """Remote host sends a MESSAGE → daemon writes to local inbox → ACK."""
    mailbox_store, spool_store = stores
    _init_session(mailbox_store, "inbound-sess", manager="mgr", agents=["remote"])

    d = await daemon_factory()
    port = d.status()["port"]

    r_reader, r_writer = await _connect_client("127.0.0.1", port, "remote")

    # remote sends a MESSAGE to mgr
    msg_frame = _make_msg_frame(
        session_id="inbound-sess",
        from_id="remote",
        to_id="mgr",
        subject="inbound",
        body="hello from remote",
        kind="QUESTION",
    )
    r_writer.write(encode_frame(msg_frame))
    await r_writer.drain()

    # Should get ACK back
    ack = await _drain_ack(r_reader)
    assert ack.payload.get("status") == "delivered"

    # Verify message is in mgr's inbox
    assert _count_inbox(mailbox_store, "inbound-sess", "mgr") == 1
    bodies = _read_inbox_bodies(mailbox_store, "inbound-sess", "mgr")
    assert bodies == ["hello from remote"]

    r_writer.close()
    await r_writer.wait_closed()


# ── 3. Session isolation ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_isolation(stores, daemon_factory):
    """Messages for different sessions are routed independently."""
    mailbox_store, spool_store = stores
    _init_session(mailbox_store, "sess-a", manager="mgr", agents=["h1"])
    _init_session(mailbox_store, "sess-b", manager="mgr", agents=["h2"])

    d = await daemon_factory()
    port = d.status()["port"]
    d._routing.add_route("sess-a", "h1")
    d._routing.add_route("sess-b", "h2")

    h1_r, h1_w = await _connect_client("127.0.0.1", port, "h1")
    h2_r, h2_w = await _connect_client("127.0.0.1", port, "h2")

    # Send a message in sess-a to h1
    await d.send_message(
        session_id="sess-a", from_id="mgr", to_id="h1",
        msg={"subject": "a-msg", "body": "for session a", "kind": "TASK"},
    )
    # Send a message in sess-b to h2
    await d.send_message(
        session_id="sess-b", from_id="mgr", to_id="h2",
        msg={"subject": "b-msg", "body": "for session b", "kind": "REPORT"},
    )

    # Verify inboxes
    assert _count_inbox(mailbox_store, "sess-a", "h1") == 1
    assert _count_inbox(mailbox_store, "sess-b", "h2") == 1
    assert _read_inbox_bodies(mailbox_store, "sess-a", "h1") == ["for session a"]
    assert _read_inbox_bodies(mailbox_store, "sess-b", "h2") == ["for session b"]

    # Cross-check: no cross-contamination
    assert _count_inbox(mailbox_store, "sess-b", "h1") == 0
    assert _count_inbox(mailbox_store, "sess-a", "h2") == 0

    # Wire reads should also succeed
    frame_a = await _read_frame_from(h1_r, timeout=3.0)
    frame_b = await _read_frame_from(h2_r, timeout=3.0)
    assert frame_a.session_id == "sess-a"
    assert frame_a.payload["body"] == "for session a"
    assert frame_b.session_id == "sess-b"
    assert frame_b.payload["body"] == "for session b"

    h1_w.close()
    h2_w.close()
    await h1_w.wait_closed()
    await h2_w.wait_closed()


@pytest.mark.asyncio
async def test_inbound_session_isolation(stores, daemon_factory):
    """Inbound messages respect session boundaries — wrong session rejected."""
    mailbox_store, spool_store = stores
    _init_session(mailbox_store, "real-sess", manager="mgr", agents=["h1"])

    d = await daemon_factory()
    port = d.status()["port"]

    r, w = await _connect_client("127.0.0.1", port, "h1")

    # Try to send a message with a session_id that doesn't exist
    bogus_frame = _make_msg_frame(
        session_id="no-such-sess",
        from_id="h1",
        to_id="mgr",
        subject="bogus",
        body="should fail",
    )
    w.write(encode_frame(bogus_frame))
    await w.drain()

    # Should get NACK (session not found)
    resp = await _read_frame_from(r, timeout=3.0)
    assert resp.type == FrameType.NACK
    assert "session not found" in resp.payload.get("reason", "")

    w.close()
    await w.wait_closed()


# ── 4. Concurrent pressure ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_messages(stores, daemon_factory):
    """1000 messages from a single sender are all delivered and persisted."""
    mailbox_store, spool_store = stores
    _init_session(mailbox_store, "stress-sess", manager="sender", agents=["receiver"])

    d = await daemon_factory()
    port = d.status()["port"]
    d._routing.add_route("stress-sess", "receiver")

    r_r, r_w = await _connect_client("127.0.0.1", port, "receiver")

    N = 1000
    for i in range(N):
        await d.send_message(
            session_id="stress-sess",
            from_id="sender",
            to_id="receiver",
            msg={
                "subject": f"msg-{i}",
                "body": f"payload-{i}",
                "kind": "REPORT",
            },
        )

    # All messages should be in the local inbox
    assert _count_inbox(mailbox_store, "stress-sess", "receiver") == N

    # Read all frames from the wire and verify content
    received_bodies: set[str] = set()
    for _ in range(N):
        frame = await _read_frame_from(r_r, timeout=30.0)
        assert frame.type == FrameType.MESSAGE
        received_bodies.add(frame.payload["body"])

    expected = {f"payload-{i}" for i in range(N)}
    assert received_bodies == expected

    r_w.close()
    await r_w.wait_closed()


# ── 5. Disconnect recovery ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disconnect_recovery(stores, daemon_factory):
    """Messages spooled while a host is offline are replayed on reconnect."""
    mailbox_store, spool_store = stores
    _init_session(mailbox_store, "recov-sess", manager="mgr", agents=["ephemeral"])

    d = await daemon_factory()
    port = d.status()["port"]

    # Ephemeral connects briefly, then disconnects
    eph_r, eph_w = await _connect_client("127.0.0.1", port, "ephemeral")
    eph_w.close()
    await eph_w.wait_closed()
    # Wait for the daemon to process the disconnection
    await asyncio.sleep(0.2)

    # Send two messages while ephemeral is offline → spooled
    d._routing.add_route("recov-sess", "ephemeral")
    r1 = await d.send_message(
        session_id="recov-sess", from_id="mgr", to_id="ephemeral",
        msg={"subject": "offline-1", "body": "while-away-1", "kind": "TASK"},
    )
    r2 = await d.send_message(
        session_id="recov-sess", from_id="mgr", to_id="ephemeral",
        msg={"subject": "offline-2", "body": "while-away-2", "kind": "TASK"},
    )

    # Both should be spooled (host disconnected)
    assert r1["status"] == "spooled"
    assert r2["status"] == "spooled"
    pending = spool_store.replay()
    assert len(pending) == 2

    # Messages are in the local inbox (written by send_message)
    assert _count_inbox(mailbox_store, "recov-sess", "ephemeral") == 2

    # Ephemeral reconnects
    eph_r2, eph_w2 = await _connect_client("127.0.0.1", port, "ephemeral")

    # Flush the spool → pending messages should be delivered
    result = await d._daemon.flush_spool(spool_store, d._routing)
    assert result["resent"] == 2

    # Read the two replayed frames
    msgs: list[dict] = []
    for _ in range(2):
        frame = await _read_frame_from(eph_r2, timeout=5.0)
        assert frame.type == FrameType.MESSAGE
        msgs.append(frame.payload)

    bodies = {m["body"] for m in msgs}
    assert bodies == {"while-away-1", "while-away-2"}

    eph_w2.close()
    await eph_w2.wait_closed()


@pytest.mark.asyncio
async def test_reconnect_and_new_messages(stores, daemon_factory):
    """After reconnect, new messages flow normally alongside spool replay."""
    mailbox_store, spool_store = stores
    _init_session(mailbox_store, "re-sess", manager="mgr", agents=["bob"])

    d = await daemon_factory()
    port = d.status()["port"]
    d._routing.add_route("re-sess", "bob")

    # Bob connects, then disconnects
    bob_r, bob_w = await _connect_client("127.0.0.1", port, "bob")
    bob_w.close()
    await bob_w.wait_closed()
    await asyncio.sleep(0.2)

    # Message while offline → spooled
    r_offline = await d.send_message(
        session_id="re-sess", from_id="mgr", to_id="bob",
        msg={"subject": "old", "body": "before-reconnect", "kind": "REPORT"},
    )
    assert r_offline["status"] == "spooled"
    assert len(spool_store.replay()) >= 1

    # Bob reconnects
    bob_r2, bob_w2 = await _connect_client("127.0.0.1", port, "bob")

    # Flush spool
    await d._daemon.flush_spool(spool_store, d._routing)

    # New message after reconnect
    await d.send_message(
        session_id="re-sess", from_id="mgr", to_id="bob",
        msg={"subject": "new", "body": "after-reconnect", "kind": "REPORT"},
    )

    # Both messages arrive via wire
    frames: list[Frame] = []
    for _ in range(2):
        frames.append(await _read_frame_from(bob_r2, timeout=5.0))

    bodies = {f.payload["body"] for f in frames}
    assert bodies == {"before-reconnect", "after-reconnect"}

    bob_w2.close()
    await bob_w2.wait_closed()


# ── 6. Duplicate session_id ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_session_id(stores, daemon_factory):
    """Multiple messages with the same session_id are all delivered."""
    mailbox_store, spool_store = stores
    _init_session(mailbox_store, "dup-sess", manager="mgr", agents=["w1"])

    d = await daemon_factory()
    port = d.status()["port"]
    d._routing.add_route("dup-sess", "w1")

    w_r, w_w = await _connect_client("127.0.0.1", port, "w1")

    COUNT = 20
    for i in range(COUNT):
        await d.send_message(
            session_id="dup-sess",
            from_id="mgr",
            to_id="w1",
            msg={
                "subject": f"dup-{i}",
                "body": f"body-{i}",
                "kind": "REPORT",
            },
        )

    # All messages in local inbox
    assert _count_inbox(mailbox_store, "dup-sess", "w1") == COUNT

    # All messages arrive at the remote host
    received: list[Frame] = []
    for _ in range(COUNT):
        frame = await _read_frame_from(w_r, timeout=5.0)
        assert frame.type == FrameType.MESSAGE
        assert frame.session_id == "dup-sess"
        received.append(frame)

    assert len(received) == COUNT

    # Verify unique msg_ids
    msg_ids = {f.payload["msg_id"] for f in received}
    assert len(msg_ids) == COUNT

    w_w.close()
    await w_w.wait_closed()


@pytest.mark.asyncio
async def test_duplicate_session_different_hosts(stores, daemon_factory):
    """Same session routed to multiple hosts — pub/sub delivers to all."""
    mailbox_store, spool_store = stores
    _init_session(
        mailbox_store, "multi-sess",
        manager="mgr", agents=["alpha", "beta"],
    )

    d = await daemon_factory()
    port = d.status()["port"]
    d._routing.add_route("multi-sess", "alpha")
    d._routing.add_route("multi-sess", "beta")

    a_r, a_w = await _connect_client("127.0.0.1", port, "alpha")
    b_r, b_w = await _connect_client("127.0.0.1", port, "beta")

    # Send a message to alpha via the daemon
    await d.send_message(
        session_id="multi-sess", from_id="mgr", to_id="alpha",
        msg={"subject": "to-alpha", "body": "hello alpha", "kind": "NOTICE"},
    )

    # Pub/sub: both hosts in the routing table receive the frame
    fa = await _read_frame_from(a_r, timeout=5.0)
    fb = await _read_frame_from(b_r, timeout=5.0)

    # Both receive the same message payload
    assert fa.payload["body"] == "hello alpha"
    assert fa.payload["to"] == "alpha"
    assert fb.payload["body"] == "hello alpha"
    assert fb.payload["to"] == "alpha"

    # Local inbox: only the addressed recipient (alpha) has the message
    # (send_message writes to alpha's inbox via mailbox_store.send)
    assert _count_inbox(mailbox_store, "multi-sess", "alpha") == 1
    assert _read_inbox_bodies(mailbox_store, "multi-sess", "alpha") == ["hello alpha"]

    # beta's inbox is empty (message was addressed to alpha)
    assert _count_inbox(mailbox_store, "multi-sess", "beta") == 0

    a_w.close()
    b_w.close()
    await a_w.wait_closed()
    await b_w.wait_closed()


# ── Registry / Routing unit tests ───────────────────────────────────────


class TestConnectionRegistry:
    def test_register_and_get(self):
        reg = ConnectionRegistry()
        reg.register("host-a", None, None)  # type: ignore[arg-type]
        assert reg.get("host-a") == (None, None)
        assert reg.is_connected("host-a")
        assert reg.list_hosts() == ["host-a"]

    def test_remove(self):
        reg = ConnectionRegistry()
        reg.register("h1", None, None)  # type: ignore[arg-type]
        reg.remove("h1")
        assert reg.get("h1") is None
        assert not reg.is_connected("h1")
        assert reg.list_hosts() == []

    def test_remove_nonexistent_is_noop(self):
        reg = ConnectionRegistry()
        reg.remove("ghost")

    def test_clear(self):
        reg = ConnectionRegistry()
        reg.register("a", None, None)  # type: ignore[arg-type]
        reg.register("b", None, None)  # type: ignore[arg-type]
        reg.clear()
        assert reg.list_hosts() == []

    def test_overwrite(self):
        reg = ConnectionRegistry()
        reg.register("h", "r1", "w1")  # type: ignore[arg-type]
        reg.register("h", "r2", "w2")  # type: ignore[arg-type]
        assert reg.get("h") == ("r2", "w2")


class TestSessionRoutingTable:
    def test_add_and_get(self):
        rt = SessionRoutingTable()
        rt.add_route("s1", "host-a")
        rt.add_route("s1", "host-b")
        assert rt.get_hosts("s1") == {"host-a", "host-b"}

    def test_get_unknown_session(self):
        rt = SessionRoutingTable()
        assert rt.get_hosts("nope") == set()

    def test_remove_route(self):
        rt = SessionRoutingTable()
        rt.add_route("s1", "a")
        rt.add_route("s1", "b")
        rt.remove_route("s1", "a")
        assert rt.get_hosts("s1") == {"b"}

    def test_remove_last_host_cleans_session(self):
        rt = SessionRoutingTable()
        rt.add_route("s1", "a")
        rt.remove_route("s1", "a")
        assert "s1" not in rt.get_all_sessions()

    def test_remove_nonexistent_is_noop(self):
        rt = SessionRoutingTable()
        rt.remove_route("nope", "ghost")

    def test_get_all_sessions(self):
        rt = SessionRoutingTable()
        rt.add_route("s1", "a")
        rt.add_route("s2", "b")
        all_s = rt.get_all_sessions()
        assert all_s == {"s1": {"a"}, "s2": {"b"}}

    def test_clear(self):
        rt = SessionRoutingTable()
        rt.add_route("s1", "a")
        rt.clear()
        assert rt.get_all_sessions() == {}

    def test_add_route_idempotent(self):
        rt = SessionRoutingTable()
        rt.add_route("s1", "a")
        rt.add_route("s1", "a")
        assert rt.get_hosts("s1") == {"a"}
