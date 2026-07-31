"""TCP integration tests — gated on ``--run-integration``.

Run explicitly with::

    uv run pytest tests/test_tcp_integration.py -v --run-integration

Covers:
  1. End-to-end Manager ↔ Worker via TCP loopback
  2. Daemon crash recovery (spool replay)
  3. SSH tunnel disconnect and reconnect
  4. Multi-session concurrent routing isolation
  5. Filesystem transport degradation (daemon offline)
  6. Latency verification (<50 ms p50)
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import statistics
import struct
import time
import uuid
from pathlib import Path

import pytest

from codeagent.mailbox.store import MailboxStore
from codeagent.tcp.protocol import Frame, FrameType, decode_frame, encode_frame
from codeagent.tcp.server import MailboxDaemon
from codeagent.tcp.spool import SpoolEntry, SpoolStore

# ── gating ──────────────────────────────────────────────────────────────

requires_integration = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="integration tests disabled (use --run-integration)",
)

requires_ssh = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="SSH integration tests disabled (use --run-integration)",
)

pytestmark = requires_integration


# ── helpers ─────────────────────────────────────────────────────────────


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


def _init_session(
    mailbox_store: MailboxStore,
    session_id: str,
    manager: str = "main",
    agents: list[str] | None = None,
) -> None:
    """Create a session in the mailbox store with the given roster."""
    agents = agents or []
    all_agents = sorted(set(agents) | {manager})
    non_manager = [a for a in all_agents if a != manager]
    mailbox_store.session_init(session_id, manager, non_manager)


def _count_inbox(
    mailbox_store: MailboxStore, session_id: str, agent_id: str,
) -> int:
    """Return the number of messages in *agent_id*'s inbox."""
    inbox = mailbox_store.agent_subdir(session_id, agent_id, "inbox")
    return len(list(inbox.glob("*.json"))) if inbox.exists() else 0


def _read_inbox_bodies(
    mailbox_store: MailboxStore, session_id: str, agent_id: str,
) -> list[str]:
    """Return message bodies from *agent_id*'s inbox, sorted by mtime."""
    inbox = mailbox_store.agent_subdir(session_id, agent_id, "inbox")
    if not inbox.exists():
        return []
    files = sorted(inbox.glob("*.json"), key=lambda f: f.stat().st_mtime)
    return [json.loads(f.read_text())["body"] for f in files]


def _find_free_port() -> int:
    """Return a currently-unused TCP port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ═══════════════════════════════════════════════════════════════════════
# 1. End-to-end Manager ↔ Worker via TCP loopback
# ═══════════════════════════════════════════════════════════════════════


class TestE2ELoopback:
    """Manager sends TASK, Worker receives; Worker sends REPORT, Manager receives."""

    @pytest.mark.asyncio
    async def test_manager_to_worker_task(self, tcp_daemon):
        """Manager sends a TASK → Worker receives the complete message."""
        daemon, mailbox_store, spool_store = tcp_daemon
        _init_session(mailbox_store, "e2e-sess", manager="manager", agents=["worker"])

        port = daemon.status()["port"]
        daemon._routing.add_route("e2e-sess", "worker")

        w_reader, w_writer = await _connect_client("127.0.0.1", port, "worker")

        result = await daemon.send_message(
            session_id="e2e-sess",
            from_id="manager",
            to_id="worker",
            msg={
                "subject": "implement-feature",
                "body": "Please implement the new TCP spool",
                "kind": "TASK",
            },
        )
        assert result["msg_id"], "msg_id should be populated"

        # Verify local inbox
        assert _count_inbox(mailbox_store, "e2e-sess", "worker") == 1
        bodies = _read_inbox_bodies(mailbox_store, "e2e-sess", "worker")
        assert bodies == ["Please implement the new TCP spool"]

        # Verify wire receipt
        frame = await _read_frame_from(w_reader, timeout=3.0)
        assert frame.type == FrameType.MESSAGE
        assert frame.session_id == "e2e-sess"
        assert frame.payload["from"] == "manager"
        assert frame.payload["to"] == "worker"
        assert frame.payload["kind"] == "TASK"
        assert frame.payload["body"] == "Please implement the new TCP spool"
        assert frame.payload["subject"] == "implement-feature"
        assert frame.payload["msg_id"] == result["msg_id"]

        w_writer.close()
        await w_writer.wait_closed()

    @pytest.mark.asyncio
    async def test_worker_to_manager_report(self, tcp_daemon):
        """Worker sends a REPORT → Manager receives the complete message."""
        daemon, mailbox_store, spool_store = tcp_daemon
        _init_session(mailbox_store, "e2e-report", manager="mgr", agents=["worker"])

        port = daemon.status()["port"]

        # Manager connects as "mgr" to receive forwarded messages
        m_reader, m_writer = await _connect_client("127.0.0.1", port, "mgr")
        # Worker connects as "worker" to send a report
        w_reader, w_writer = await _connect_client("127.0.0.1", port, "worker")

        # Worker sends a MESSAGE frame directly to the daemon
        msg_id = f"worker_{uuid.uuid4().hex[:8]}"
        report_frame = Frame(
            type=FrameType.MESSAGE,
            session_id="e2e-report",
            payload={
                "session_id": "e2e-report",
                "from": "worker",
                "to": "mgr",
                "subject": "task-done",
                "body": "Implementation complete, all tests pass",
                "kind": "REPORT",
                "msg_id": msg_id,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )
        w_writer.write(encode_frame(report_frame))
        await w_writer.drain()

        # Worker should get ACK back
        ack = await _read_frame_from(w_reader, timeout=3.0)
        assert ack.type == FrameType.ACK
        assert ack.payload.get("status") == "delivered"

        # Manager's local inbox should have the message
        assert _count_inbox(mailbox_store, "e2e-report", "mgr") == 1
        bodies = _read_inbox_bodies(mailbox_store, "e2e-report", "mgr")
        assert bodies == ["Implementation complete, all tests pass"]

        m_writer.close()
        w_writer.close()
        await m_writer.wait_closed()
        await w_writer.wait_closed()

    @pytest.mark.asyncio
    async def test_bidirectional_exchange(self, tcp_daemon):
        """Full bidirectional: Manager→TASK, Worker→REPORT, both verified."""
        daemon, mailbox_store, spool_store = tcp_daemon
        _init_session(
            mailbox_store, "e2e-bidir",
            manager="mgr", agents=["worker"],
        )

        port = daemon.status()["port"]
        daemon._routing.add_route("e2e-bidir", "worker")

        w_reader, w_writer = await _connect_client("127.0.0.1", port, "worker")

        # Manager sends TASK
        task_result = await daemon.send_message(
            session_id="e2e-bidir",
            from_id="mgr",
            to_id="worker",
            msg={
                "subject": "build-spool",
                "body": "Implement durable WAL spool for TCP forwarding",
                "kind": "TASK",
            },
        )

        # Worker receives TASK
        task_frame = await _read_frame_from(w_reader, timeout=3.0)
        assert task_frame.type == FrameType.MESSAGE
        assert task_frame.payload["kind"] == "TASK"
        assert task_frame.payload["body"] == "Implement durable WAL spool for TCP forwarding"

        # Worker sends REPORT back
        msg_id = f"worker_{uuid.uuid4().hex[:8]}"
        report_frame = Frame(
            type=FrameType.MESSAGE,
            session_id="e2e-bidir",
            payload={
                "session_id": "e2e-bidir",
                "from": "worker",
                "to": "mgr",
                "subject": "Re: build-spool",
                "body": "Spool implementation done. 500 lines, all tests green.",
                "kind": "REPORT",
                "msg_id": msg_id,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )
        w_writer.write(encode_frame(report_frame))
        await w_writer.drain()

        # Worker gets ACK
        ack = await _read_frame_from(w_reader, timeout=3.0)
        assert ack.type == FrameType.ACK

        # Manager inbox has the report
        assert _count_inbox(mailbox_store, "e2e-bidir", "mgr") == 1
        bodies = _read_inbox_bodies(mailbox_store, "e2e-bidir", "mgr")
        assert "Spool implementation done" in bodies[0]

        # Worker inbox has the task
        assert _count_inbox(mailbox_store, "e2e-bidir", "worker") == 1

        w_writer.close()
        await w_writer.wait_closed()


# ═══════════════════════════════════════════════════════════════════════
# 2. Daemon crash recovery (spool replay)
# ═══════════════════════════════════════════════════════════════════════


class TestDaemonCrashRecovery:
    """Simulate daemon crash → restart → verify spool replay."""

    @pytest.mark.asyncio
    async def test_spool_persists_across_daemon_restart(self, tmp_path: Path):
        """Entries written to spool survive daemon stop/start."""
        mailbox_root = tmp_path / "mailbox"
        spool_root = tmp_path / "spool"

        # ── First daemon lifecycle ────────────────────────────────────
        ms1 = MailboxStore(mailbox_root)
        ss1 = SpoolStore(spool_root)
        _init_session(ms1, "crash-sess", manager="mgr", agents=["worker"])

        d1 = MailboxDaemon("127.0.0.1", 0, ms1, ss1)
        host, port = await d1.start()
        d1._routing.add_route("crash-sess", "worker")

        # Worker connects and disconnects (simulating offline)
        w_r, w_w = await _connect_client("127.0.0.1", port, "worker")
        w_w.close()
        await w_w.wait_closed()
        await asyncio.sleep(0.2)

        # Send messages while worker offline → spooled
        r1 = await d1.send_message(
            session_id="crash-sess", from_id="mgr", to_id="worker",
            msg={"subject": "s1", "body": "msg-before-crash-1", "kind": "TASK"},
        )
        r2 = await d1.send_message(
            session_id="crash-sess", from_id="mgr", to_id="worker",
            msg={"subject": "s2", "body": "msg-before-crash-2", "kind": "TASK"},
        )
        assert r1["status"] == "spooled"
        assert r2["status"] == "spooled"

        # Verify spool has pending entries
        assert len(ss1.replay()) == 2

        # ── Simulate crash: stop daemon ──────────────────────────────
        await d1.stop()
        assert d1.status()["running"] is False

        # ── Second daemon: same spool root (crash recovery) ──────────
        ms2 = MailboxStore(mailbox_root)
        ss2 = SpoolStore(spool_root)  # same on-disk spool
        d2 = MailboxDaemon("127.0.0.1", 0, ms2, ss2)
        _, port2 = await d2.start()
        d2._routing.add_route("crash-sess", "worker")

        # Replay should find the 2 pending entries
        pending = ss2.replay()
        assert len(pending) == 2, "pending entries survived daemon restart"

        # Worker reconnects
        w_r2, w_w2 = await _connect_client("127.0.0.1", port2, "worker")

        # Flush spool → replay entries to connected worker
        result = await d2._daemon.flush_spool(ss2, d2._routing)
        assert result["resent"] == 2

        # Worker receives both replayed messages
        received_bodies: set[str] = set()
        for _ in range(2):
            frame = await _read_frame_from(w_r2, timeout=5.0)
            assert frame.type == FrameType.MESSAGE
            received_bodies.add(frame.payload["body"])

        assert received_bodies == {"msg-before-crash-1", "msg-before-crash-2"}

        w_w2.close()
        await w_w2.wait_closed()

    @pytest.mark.asyncio
    async def test_new_messages_after_crash_recovery(self, tmp_path: Path):
        """After crash recovery, new messages flow normally."""
        mailbox_root = tmp_path / "mailbox"
        spool_root = tmp_path / "spool"

        ms1 = MailboxStore(mailbox_root)
        ss1 = SpoolStore(spool_root)
        _init_session(ms1, "post-crash", manager="mgr", agents=["w"])

        d1 = MailboxDaemon("127.0.0.1", 0, ms1, ss1)
        _, port1 = await d1.start()
        d1._routing.add_route("post-crash", "w")

        # Send a message that gets spooled (worker offline)
        w_r, w_w = await _connect_client("127.0.0.1", port1, "w")
        w_w.close()
        await w_w.wait_closed()
        await asyncio.sleep(0.2)

        await d1.send_message(
            session_id="post-crash", from_id="mgr", to_id="w",
            msg={"subject": "old", "body": "before-crash", "kind": "REPORT"},
        )
        await d1.stop()

        # ── New daemon ──────────────────────────────────────────────
        ms2 = MailboxStore(mailbox_root)
        ss2 = SpoolStore(spool_root)
        d2 = MailboxDaemon("127.0.0.1", 0, ms2, ss2)
        _, port2 = await d2.start()
        d2._routing.add_route("post-crash", "w")

        w_r2, w_w2 = await _connect_client("127.0.0.1", port2, "w")
        await d2._daemon.flush_spool(ss2, d2._routing)

        # Send a NEW message after recovery
        await d2.send_message(
            session_id="post-crash", from_id="mgr", to_id="w",
            msg={"subject": "new", "body": "after-recovery", "kind": "REPORT"},
        )

        # Both old (replayed) and new messages arrive
        frames: list[Frame] = []
        for _ in range(2):
            frames.append(await _read_frame_from(w_r2, timeout=5.0))

        bodies = {f.payload["body"] for f in frames}
        assert bodies == {"before-crash", "after-recovery"}

        w_w2.close()
        await w_w2.wait_closed()


# ═══════════════════════════════════════════════════════════════════════
# 3. SSH tunnel disconnect and reconnect
# ═══════════════════════════════════════════════════════════════════════


class TestSSHTunnelReconnect:
    """Verify SSH tunnel lifecycle: establish → kill → reconnect → message delivery."""

    @pytest.mark.asyncio
    async def test_tunnel_lifecycle_and_reconnect(
        self, tcp_daemon, tcp_tunnel,
    ):
        """Full tunnel lifecycle: establish, send, kill, restart, send again."""
        daemon, mailbox_store, spool_store = tcp_daemon
        _init_session(
            mailbox_store, "tunnel-sess", manager="mgr", agents=["remote-worker"],
        )

        port = daemon.status()["port"]
        daemon._routing.add_route("tunnel-sess", "remote-worker")

        # ── Establish tunnel ──────────────────────────────────────────
        create_tunnel = tcp_tunnel
        tunnel_proc, forwarded_port = await create_tunnel(port)

        # Worker connects through the tunnel
        w_r, w_w = await _connect_client(
            "127.0.0.1", forwarded_port, "remote-worker",
        )

        # Send message through tunnel → verify delivery
        result = await daemon.send_message(
            session_id="tunnel-sess",
            from_id="mgr",
            to_id="remote-worker",
            msg={
                "subject": "via-tunnel",
                "body": "message through SSH tunnel",
                "kind": "TASK",
            },
        )
        assert result["msg_id"]

        frame = await _read_frame_from(w_r, timeout=5.0)
        assert frame.type == FrameType.MESSAGE
        assert frame.payload["body"] == "message through SSH tunnel"

        # ── Kill the tunnel ──────────────────────────────────────────
        tunnel_proc.terminate()
        await asyncio.wait_for(tunnel_proc.wait(), timeout=5.0)

        # Worker connection should break
        with pytest.raises((asyncio.IncompleteReadError, ConnectionResetError,
                            BrokenPipeError, OSError)):
            # Try reading from the broken connection
            await asyncio.wait_for(w_r.readexactly(4), timeout=3.0)

        w_w.close()
        try:
            await w_w.wait_closed()
        except Exception:
            pass

        await asyncio.sleep(0.5)

        # ── Restart tunnel (reconnect) ───────────────────────────────
        tunnel_proc2, forwarded_port2 = await create_tunnel(port)

        # Worker reconnects through the new tunnel
        w_r2, w_w2 = await _connect_client(
            "127.0.0.1", forwarded_port2, "remote-worker",
        )

        # Send another message → verify delivery through new tunnel
        result2 = await daemon.send_message(
            session_id="tunnel-sess",
            from_id="mgr",
            to_id="remote-worker",
            msg={
                "subject": "after-reconnect",
                "body": "message after tunnel reconnect",
                "kind": "REPORT",
            },
        )
        assert result2["msg_id"]

        frame2 = await _read_frame_from(w_r2, timeout=5.0)
        assert frame2.type == FrameType.MESSAGE
        assert frame2.payload["body"] == "message after tunnel reconnect"

        # ── Spool replay: message sent while tunnel was down ─────────
        # Disconnect worker, send while offline, reconnect and flush
        w_w2.close()
        await w_w2.wait_closed()
        await asyncio.sleep(0.3)

        offline_result = await daemon.send_message(
            session_id="tunnel-sess",
            from_id="mgr",
            to_id="remote-worker",
            msg={
                "subject": "while-offline",
                "body": "sent while tunnel was down",
                "kind": "TASK",
            },
        )
        assert offline_result["status"] == "spooled"

        # Reconnect through tunnel
        w_r3, w_w3 = await _connect_client(
            "127.0.0.1", forwarded_port2, "remote-worker",
        )

        # Flush spool → replay all pending (includes the first message
        # whose ACK was lost when the tunnel was killed)
        result3 = await daemon._daemon.flush_spool(
            spool_store, daemon._routing,
        )
        assert result3["resent"] >= 1

        # Read all replayed frames; our offline message must be among them
        replayed_bodies: set[str] = set()
        for _ in range(result3["resent"]):
            frame3 = await _read_frame_from(w_r3, timeout=5.0)
            assert frame3.type == FrameType.MESSAGE
            replayed_bodies.add(frame3.payload["body"])
        assert "sent while tunnel was down" in replayed_bodies

        w_w3.close()
        await w_w3.wait_closed()


# ═══════════════════════════════════════════════════════════════════════
# 4. Multi-session concurrent routing isolation
# ═══════════════════════════════════════════════════════════════════════


class TestMultiSessionConcurrent:
    """Verify routing isolation across concurrent sessions."""

    @pytest.mark.asyncio
    async def test_three_sessions_isolation(self, tcp_daemon):
        """Three concurrent sessions: messages route only to their session."""
        daemon, mailbox_store, spool_store = tcp_daemon

        sessions = ["sess-alpha", "sess-beta", "sess-gamma"]
        agents = ["agent-a", "agent-b", "agent-c"]

        for sid, agent in zip(sessions, agents):
            _init_session(mailbox_store, sid, manager="mgr", agents=[agent])
            daemon._routing.add_route(sid, agent)

        port = daemon.status()["port"]

        # All three agents connect concurrently
        readers: dict[str, asyncio.StreamReader] = {}
        writers: dict[str, asyncio.StreamWriter] = {}
        for agent in agents:
            r, w = await _connect_client("127.0.0.1", port, agent)
            readers[agent] = r
            writers[agent] = w

        # Send one message per session concurrently
        send_tasks = []
        for sid, agent in zip(sessions, agents):
            send_tasks.append(daemon.send_message(
                session_id=sid,
                from_id="mgr",
                to_id=agent,
                msg={
                    "subject": f"for-{agent}",
                    "body": f"message for {agent} in {sid}",
                    "kind": "TASK",
                },
            ))
        results = await asyncio.gather(*send_tasks)
        assert all(r["msg_id"] for r in results)

        # Verify each agent receives exactly its own message
        for agent, sid in zip(agents, sessions):
            frame = await _read_frame_from(readers[agent], timeout=5.0)
            assert frame.type == FrameType.MESSAGE
            assert frame.session_id == sid
            assert frame.payload["body"] == f"message for {agent} in {sid}"
            assert frame.payload["to"] == agent

        # Cross-check: no cross-contamination in inboxes
        for i, (sid_i, agent_i) in enumerate(zip(sessions, agents)):
            assert _count_inbox(mailbox_store, sid_i, agent_i) == 1
            for j, (sid_j, agent_j) in enumerate(zip(sessions, agents)):
                if i != j:
                    assert _count_inbox(mailbox_store, sid_j, agent_i) == 0, \
                        f"{agent_i} leaked into {sid_j}"

        # Cleanup
        for w in writers.values():
            w.close()
        for w in writers.values():
            await w.wait_closed()

    @pytest.mark.asyncio
    async def test_no_message_leak_between_sessions(self, tcp_daemon):
        """Sending 100 messages per session — no cross-session leakage."""
        daemon, mailbox_store, spool_store = tcp_daemon

        _init_session(mailbox_store, "leak-a", manager="mgr", agents=["alice"])
        _init_session(mailbox_store, "leak-b", manager="mgr", agents=["bob"])

        daemon._routing.add_route("leak-a", "alice")
        daemon._routing.add_route("leak-b", "bob")

        port = daemon.status()["port"]
        alice_r, alice_w = await _connect_client("127.0.0.1", port, "alice")
        bob_r, bob_w = await _connect_client("127.0.0.1", port, "bob")

        N = 100

        # Send N messages to alice and N to bob
        for i in range(N):
            await daemon.send_message(
                session_id="leak-a", from_id="mgr", to_id="alice",
                msg={"subject": f"a-{i}", "body": f"alice-{i}", "kind": "TASK"},
            )
            await daemon.send_message(
                session_id="leak-b", from_id="mgr", to_id="bob",
                msg={"subject": f"b-{i}", "body": f"bob-{i}", "kind": "REPORT"},
            )

        # Verify inbox counts
        assert _count_inbox(mailbox_store, "leak-a", "alice") == N
        assert _count_inbox(mailbox_store, "leak-b", "bob") == N

        # Verify wire delivery: alice gets N messages, all addressed to her
        alice_bodies: set[str] = set()
        for _ in range(N):
            frame = await _read_frame_from(alice_r, timeout=10.0)
            assert frame.session_id == "leak-a"
            assert frame.payload["to"] == "alice"
            alice_bodies.add(frame.payload["body"])

        bob_bodies: set[str] = set()
        for _ in range(N):
            frame = await _read_frame_from(bob_r, timeout=10.0)
            assert frame.session_id == "leak-b"
            assert frame.payload["to"] == "bob"
            bob_bodies.add(frame.payload["body"])

        assert len(alice_bodies) == N
        assert len(bob_bodies) == N
        assert alice_bodies.isdisjoint(bob_bodies)

        alice_w.close()
        bob_w.close()
        await alice_w.wait_closed()
        await bob_w.wait_closed()


# ═══════════════════════════════════════════════════════════════════════
# 5. Filesystem transport degradation (daemon offline)
# ═══════════════════════════════════════════════════════════════════════


class TestFilesystemDegradation:
    """Verify mailbox works without TCP daemon (direct filesystem)."""

    @pytest.mark.asyncio
    async def test_file_transport_without_daemon(self, tmp_path: Path):
        """Messages sent via filesystem land in the inbox without a daemon."""
        mailbox_store = MailboxStore(tmp_path / "mailbox")
        _init_session(mailbox_store, "fs-sess", manager="mgr", agents=["worker"])

        # Direct filesystem send (no TCP daemon running)
        result = mailbox_store.send(
            session_id="fs-sess",
            from_id="mgr",
            to_id="worker",
            subject="file-mode",
            body="sent via filesystem transport",
            kind="TASK",
        )
        assert "sent" in result

        # Verify message landed in the inbox
        assert _count_inbox(mailbox_store, "fs-sess", "worker") == 1
        bodies = _read_inbox_bodies(mailbox_store, "fs-sess", "worker")
        assert bodies == ["sent via filesystem transport"]

        # Verify message content is valid JSON with all required fields
        inbox = mailbox_store.agent_subdir("fs-sess", "worker", "inbox")
        msg_file = next(inbox.glob("*.json"))
        msg = json.loads(msg_file.read_text())
        assert msg["session_id"] == "fs-sess"
        assert msg["from"] == "mgr"
        assert msg["to"] == "worker"
        assert msg["kind"] == "TASK"
        assert msg["msg_id"]

    @pytest.mark.asyncio
    async def test_daemon_stopped_file_still_works(self, tmp_path: Path):
        """After daemon stops, filesystem transport continues to work."""
        mailbox_store = MailboxStore(tmp_path / "mailbox")
        spool_store = SpoolStore(tmp_path / "spool")
        _init_session(mailbox_store, "degrade-sess", manager="mgr", agents=["w"])

        # Start daemon, send a message via TCP
        d = MailboxDaemon("127.0.0.1", 0, mailbox_store, spool_store)
        _, port = await d.start()
        d._routing.add_route("degrade-sess", "w")

        w_r, w_w = await _connect_client("127.0.0.1", port, "w")
        await d.send_message(
            session_id="degrade-sess", from_id="mgr", to_id="w",
            msg={"subject": "tcp", "body": "via-tcp", "kind": "TASK"},
        )
        frame = await _read_frame_from(w_r, timeout=3.0)
        assert frame.payload["body"] == "via-tcp"

        w_w.close()
        await w_w.wait_closed()

        # Stop daemon
        await d.stop()
        assert d.status()["running"] is False

        # Filesystem transport still works
        mailbox_store.send(
            session_id="degrade-sess",
            from_id="mgr",
            to_id="w",
            subject="file-mode",
            body="after-daemon-stopped",
            kind="REPORT",
        )
        assert _count_inbox(mailbox_store, "degrade-sess", "w") == 2
        bodies = _read_inbox_bodies(mailbox_store, "degrade-sess", "w")
        assert "via-tcp" in bodies
        assert "after-daemon-stopped" in bodies

    @pytest.mark.asyncio
    async def test_file_transport_multiple_messages(self, tmp_path: Path):
        """Filesystem transport handles multiple concurrent messages."""
        mailbox_store = MailboxStore(tmp_path / "mailbox")
        _init_session(mailbox_store, "fs-multi", manager="mgr", agents=["w"])

        N = 50
        for i in range(N):
            mailbox_store.send(
                session_id="fs-multi",
                from_id="mgr",
                to_id="w",
                subject=f"msg-{i}",
                body=f"filesystem-message-{i}",
                kind="REPORT",
            )

        assert _count_inbox(mailbox_store, "fs-multi", "w") == N
        bodies = _read_inbox_bodies(mailbox_store, "fs-multi", "w")
        expected = {f"filesystem-message-{i}" for i in range(N)}
        assert set(bodies) == expected


# ═══════════════════════════════════════════════════════════════════════
# 6. Latency verification  (<50 ms p50)
# ═══════════════════════════════════════════════════════════════════════


class TestLatency:
    """TCP loopback p50 latency must be under 50 ms."""

    @pytest.mark.asyncio
    async def test_tcp_latency_p50_under_50ms(self, tcp_daemon):
        """100 round-trips over TCP loopback: p50 < 50 ms."""
        daemon, mailbox_store, spool_store = tcp_daemon
        _init_session(
            mailbox_store, "latency-sess", manager="mgr", agents=["lat-worker"],
        )

        port = daemon.status()["port"]
        daemon._routing.add_route("latency-sess", "lat-worker")

        w_r, w_w = await _connect_client("127.0.0.1", port, "lat-worker")

        latencies: list[float] = []
        N = 100

        for i in range(N):
            t0 = time.monotonic()
            await daemon.send_message(
                session_id="latency-sess",
                from_id="mgr",
                to_id="lat-worker",
                msg={
                    "subject": f"lat-{i}",
                    "body": f"latency-probe-{i}",
                    "kind": "REPORT",
                },
            )
            frame = await _read_frame_from(w_r, timeout=5.0)
            t1 = time.monotonic()
            assert frame.type == FrameType.MESSAGE
            assert frame.payload["body"] == f"latency-probe-{i}"
            latencies.append((t1 - t0) * 1000)  # ms

        p50 = statistics.median(latencies)
        p99 = sorted(latencies)[int(N * 0.99)]
        avg = statistics.mean(latencies)

        # Log for diagnostics
        print(f"\n  Latency: p50={p50:.1f}ms  p99={p99:.1f}ms  avg={avg:.1f}ms")

        assert p50 < 50.0, f"p50 latency {p50:.1f}ms exceeds 50 ms threshold"
        assert avg < 100.0, f"avg latency {avg:.1f}ms unexpectedly high"

        w_w.close()
        await w_w.wait_closed()

    @pytest.mark.asyncio
    async def test_spool_write_latency(self, tmp_path: Path):
        """Spool write latency: 100 writes should complete quickly."""
        spool = SpoolStore(tmp_path / "spool")

        latencies: list[float] = []
        N = 100

        for i in range(N):
            entry = SpoolEntry(
                uuid=str(uuid.uuid4()),
                session_id="lat-sess",
                from_id="mgr",
                to_id="worker",
                msg_id=f"msg-{i}",
                payload={"body": f"spool-probe-{i}"},
                created_at=time.time(),
                host_alias="worker",
            )
            t0 = time.monotonic()
            spool.write(entry)
            t1 = time.monotonic()
            latencies.append((t1 - t0) * 1000)

        p50 = statistics.median(latencies)
        avg = statistics.mean(latencies)

        print(f"\n  Spool write: p50={p50:.2f}ms  avg={avg:.2f}ms")

        # Spool writes should be sub-millisecond for in-memory + fsync
        assert p50 < 10.0, f"spool write p50 {p50:.2f}ms unexpectedly high"
