"""Gateway tests — UDS permissions, RPC round-trip, EventStore cursor/replay,
stale socket handling, MailboxService bridging."""
from __future__ import annotations

import json
import os
import socket
import stat
import threading
from pathlib import Path

import pytest

from codeagent.gateway.client import GatewayClient, rpc_stdio
from codeagent.gateway.events import EventStore
from codeagent.gateway.model import (
    GATEWAY_PROTOCOL_VERSION,
    GatewayError,
    GatewayRequest,
    GatewayResponse,
    RuntimeEventDraft,
)
from codeagent.gateway.server import GatewayServer
from codeagent.gateway.service import AgentGateway
from codeagent.mailbox.store import MailboxStore


# ── fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def gateway_env(tmp_path: Path) -> dict:
    """Isolated gateway + mailbox roots."""
    gdir = tmp_path / "gateway"
    gdir.mkdir()
    sock = gdir / "control.sock"
    db = gdir / "events.sqlite3"
    return {"gdir": gdir, "sock": sock, "db": db}


def _make_gateway(tmp_path: Path) -> tuple[AgentGateway, GatewayServer, Path]:
    # pytest tmp_path is too deep for AF_UNIX (104-byte limit) — use a short
    # /tmp dir (same workaround the pre-existing socket tests use).
    import uuid as _uuid

    base = Path("/tmp") / f"gw-{_uuid.uuid4().hex[:8]}"
    gdir = base / "gateway"
    gdir.mkdir(parents=True, exist_ok=True)
    sock = gdir / "c.sock"
    db = gdir / "e.sqlite3"
    store = MailboxStore(root=base / "mailbox")
    store.root.mkdir(parents=True, exist_ok=True)
    events = EventStore(db_path=db, source_host="testhost")
    gw = AgentGateway(store=store, events=events, restore_from_park=False)
    server = GatewayServer(socket_path=sock, gateway=gw)
    return gw, server, sock


@pytest.fixture
def running_server(tmp_path: Path):
    gw, server, sock = _make_gateway(tmp_path)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    # wait for socket
    deadline = 0
    while not sock.exists() and deadline < 100:
        import time

        time.sleep(0.02)
        deadline += 1
    yield gw, server, sock
    server.stop()


# ── EventStore ─────────────────────────────────────────────────────────


class TestEventStore:
    def test_append_local_assigns_sequence(self, tmp_path: Path):
        store = EventStore(db_path=tmp_path / "e.sqlite3", source_host="h1")
        e1 = store.append_local(RuntimeEventDraft(
            runtime_id="r1", generation=1, session_id="s1", agent_id="a1",
            request_id="", run_id="", kind="RUNTIME_STATE",
            created_at="2026-01-01T00:00:00Z", payload={"state": "starting"},
        ))
        e2 = store.append_local(RuntimeEventDraft(
            runtime_id="r1", generation=1, session_id="s1", agent_id="a1",
            request_id="", run_id="", kind="ASSISTANT_PROGRESS",
            created_at="2026-01-01T00:00:01Z", payload={"text": "hi"},
        ))
        assert e1.event_id < e2.event_id
        assert e1.source_sequence == 1
        assert e2.source_sequence == 2
        assert e1.source_host == "h1"

    def test_append_local_per_generation_sequence(self, tmp_path: Path):
        store = EventStore(db_path=tmp_path / "e.sqlite3", source_host="h1")
        store.append_local(RuntimeEventDraft(
            runtime_id="r1", generation=1, session_id="s1", agent_id="a1",
            request_id="", run_id="", kind="RUNTIME_STATE",
            created_at="2026-01-01T00:00:00Z", payload={},
        ))
        # same runtime, generation 2 → sequence restarts
        e = store.append_local(RuntimeEventDraft(
            runtime_id="r1", generation=2, session_id="s1", agent_id="a1",
            request_id="", run_id="", kind="RUNTIME_STATE",
            created_at="2026-01-01T00:00:02Z", payload={},
        ))
        assert e.source_sequence == 1

    def test_ingest_remote_idempotent(self, tmp_path: Path):
        store = EventStore(db_path=tmp_path / "e.sqlite3", source_host="local")
        ev = {
            "source_host": "remote1", "runtime_id": "r9", "generation": 1,
            "source_sequence": 7, "session_id": "s1", "agent_id": "a1",
            "request_id": "", "run_id": "", "kind": "TOOL_STARTED",
            "created_at": "2026-01-01T00:00:00Z",
            "payload": {"tool": "bash", "name": "cmd"},
        }
        e1 = store.ingest_remote(ev)
        e2 = store.ingest_remote(ev)  # duplicate → idempotent no-op
        assert e1.event_id == e2.event_id
        assert e1.source_sequence == 7  # remote sequence preserved
        events, cursor = store.list_after(0)
        assert len(events) == 1

    def test_ingest_remote_requires_sequence(self, tmp_path: Path):
        store = EventStore(db_path=tmp_path / "e.sqlite3", source_host="local")
        with pytest.raises(ValueError, match="source_sequence"):
            store.ingest_remote({
                "source_host": "h", "runtime_id": "r", "generation": 1,
                "source_sequence": 0, "kind": "ERROR", "created_at": "",
            })

    def test_list_after_cursor_and_filters(self, tmp_path: Path):
        store = EventStore(db_path=tmp_path / "e.sqlite3", source_host="h")
        for i in range(5):
            store.append_local(RuntimeEventDraft(
                runtime_id="r1", generation=1, session_id="s1", agent_id="a1",
                request_id="", run_id="", kind="TOOL_STARTED" if i % 2 else "RUNTIME_STATE",
                created_at=f"2026-01-01T00:00:0{i}Z", payload={"i": i},
            ))
        events, cursor = store.list_after(0, filters=["TOOL_STARTED"])
        assert len(events) == 2
        assert all(e.kind == "TOOL_STARTED" for e in events)
        # resume from cursor
        events2, cursor2 = store.list_after(cursor, filters=["TOOL_STARTED"])
        assert events2 == []
        # session filter
        events3, _ = store.list_after(0, session_id="s1")
        assert len(events3) == 5

    def test_aggregate(self, tmp_path: Path):
        store = EventStore(db_path=tmp_path / "e.sqlite3", source_host="h")
        for kind in ("TOOL_STARTED", "TOOL_FINISHED", "ERROR", "ASSISTANT_PROGRESS"):
            store.append_local(RuntimeEventDraft(
                runtime_id="r1", generation=1, session_id="s1", agent_id="a1",
                request_id="", run_id="", kind=kind,
                created_at="2026-01-01T00:00:00Z", payload={},
            ))
        agg = store.aggregate("r1")
        assert agg["tool_count"] == 1
        assert agg["error_count"] == 1
        assert agg["last_event_kind"] == "ASSISTANT_PROGRESS"

    def test_sweep_prunes_old_tool_updates(self, tmp_path: Path):
        store = EventStore(db_path=tmp_path / "e.sqlite3", source_host="h")
        import datetime as dt

        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
        store.append_local(RuntimeEventDraft(
            runtime_id="r1", generation=1, session_id="s1", agent_id="a1",
            request_id="", run_id="", kind="TOOL_UPDATED",
            created_at=old, payload={"detail": "old"},
        ))
        store.append_local(RuntimeEventDraft(
            runtime_id="r1", generation=1, session_id="s1", agent_id="a1",
            request_id="", run_id="", kind="ERROR",
            created_at=old, payload={"error": "keep"},
        ))
        removed = store.sweep()
        assert removed == 1
        events, _ = store.list_after(0)
        assert [e.kind for e in events] == ["ERROR"]


# ── Gateway server / client ────────────────────────────────────────────


class TestGatewayServer:
    def test_socket_permissions(self, running_server):
        gw, server, sock = running_server
        assert sock.exists()
        mode = stat.S_IMODE(sock.stat().st_mode)
        assert mode & 0o777 == 0o600  # socket 0600

    def test_db_dir_permissions(self, tmp_path: Path):
        import uuid as _uuid

        base = Path("/tmp") / f"gwdb-{_uuid.uuid4().hex[:8]}"
        gdir = base / "gateway"
        gdir.mkdir(parents=True)
        sock = gdir / "c.sock"
        db = gdir / "e.sqlite3"
        store = MailboxStore(root=tmp_path / "mailbox")
        store.root.mkdir(parents=True, exist_ok=True)
        events = EventStore(db_path=db, source_host="h")
        gw = AgentGateway(store=store, events=events, restore_from_park=False)
        server = GatewayServer(socket_path=sock, gateway=gw)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        import time

        for _ in range(100):
            if sock.exists():
                break
            time.sleep(0.02)
        # WAL mode
        import sqlite3

        conn = sqlite3.connect(str(db))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"
        server.stop()

    def test_capabilities_rpc(self, running_server):
        gw, server, sock = running_server
        client = GatewayClient(socket_path=sock)
        caps = client.call("capabilities.get")
        assert caps["version"] == GATEWAY_PROTOCOL_VERSION
        assert "runtimes" in caps

    def test_unknown_method_fails_closed(self, running_server):
        gw, server, sock = running_server
        client = GatewayClient(socket_path=sock)
        with pytest.raises(GatewayError) as ei:
            client.call("no.such.method")
        assert ei.value.code == "PROTOCOL"

    def test_version_mismatch_rejected(self, running_server):
        gw, server, sock = running_server
        req = GatewayRequest(v=99, id="x", method="capabilities.get", params={})
        client = GatewayClient(socket_path=sock)
        resp = client._roundtrip(req)
        assert not resp.ok
        assert resp.error["code"] == "VERSION_INCOMPATIBLE"

    def test_stale_socket_replaced(self, tmp_path: Path):
        gw, server, sock = _make_gateway(tmp_path)
        # Simulate a stale socket (created but nothing listening)
        sock.write_bytes(b"")
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        import time

        for _ in range(100):
            try:
                GatewayClient(socket_path=sock, timeout=1).call("capabilities.get")
                break
            except GatewayError:
                time.sleep(0.05)
        # server replaced the stale socket and is now serving
        caps = GatewayClient(socket_path=sock, timeout=2).call("capabilities.get")
        assert caps["version"] == GATEWAY_PROTOCOL_VERSION
        server.stop()

    def test_already_running_detected(self, running_server):
        gw, server, sock = running_server
        # Second server on the same socket → ALREADY_RUNNING
        store = MailboxStore(root=Path("/tmp") / f"mb-{os.getpid()}")
        store.root.mkdir(parents=True, exist_ok=True)
        gw2 = AgentGateway(store=store, restore_from_park=False)
        server2 = GatewayServer(socket_path=sock, gateway=gw2)
        with pytest.raises(GatewayError) as ei:
            server2.serve_forever()
        assert ei.value.code == "ALREADY_RUNNING"

    def test_frame_too_large(self, running_server):
        gw, server, sock = running_server
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(str(sock))
        big = "x" * (1_048_600)
        s.sendall(
            ('{"v":1,"id":"big","method":"capabilities.get","params":{"pad":"' + big + '"}}\n').encode()
        )
        buf = b""
        while b"\n" not in buf:
            buf += s.recv(65536)
        line = buf.split(b"\n", 1)[0].decode()
        resp = json.loads(line)
        assert resp["ok"] is False
        assert resp["error"]["code"] == "FRAME_TOO_LARGE"
        s.close()


# ── Mailbox bridging through the gateway ───────────────────────────────


class TestGatewayMailboxBridge:
    def test_session_ensure_and_message_flow(self, tmp_path: Path):
        gw, server, sock = _make_gateway(tmp_path)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        import time

        for _ in range(100):
            if sock.exists():
                break
            time.sleep(0.02)
        client = GatewayClient(socket_path=sock)

        result = client.call("session.ensure", {
            "session_id": "s1", "manager_id": "manager", "roster": ["worker", "oracle"],
        })
        assert result["session_id"] == "s1"
        assert "worker" in result["roster"]

        send = client.call("message.send", {
            "session_id": "s1", "from": "manager", "to": "worker",
            "subject": "t", "body": "do it", "kind": "TASK",
            "run_id": "r1", "request_id": "q1", "require_ack": True,
        })
        assert send["status"] == "delivered"

        peek = client.call("message.peek", {"session_id": "s1", "agent": "worker"})
        assert peek["pending"] == 1

        # read → READ receipt emitted (require_ack)
        outcome = client.call("message.read", {"session_id": "s1", "agent": "worker", "owner": "worker"})
        assert outcome["status"] == "ok"
        assert outcome["receipt"]["status"] == "delivered"
        assert outcome["message"]["require_ack"] is True

        # finalize
        fin = client.call("message.finalize", {
            "session_id": "s1", "agent": "worker", "msg_id": outcome["message"]["msg_id"], "owner": "worker",
        })
        assert "archive" in fin["status"]

        # ack-route-unresolved: require_ack from a non-roster sender
        inbox = gw._store.agent_subdir("s1", "worker", "inbox")
        inbox.mkdir(parents=True, exist_ok=True)
        spoof = {
            "session_id": "s1", "from": "ghost", "to": "worker", "subject": "s",
            "body": "no route", "kind": "TASK", "msg_id": "spoof1",
            "created_at": "2026-01-01T00:00:00Z", "protocol_version": 2,
            "require_ack": True, "run_id": "r9", "request_id": "q9",
        }
        (inbox / "spoof1.json").write_text(json.dumps(spoof))
        with pytest.raises(GatewayError) as ei:
            client.call("message.read", {"session_id": "s1", "agent": "worker", "owner": "worker"})
        assert "NOT_AUTHORIZED" in ei.value.code or "ack route" in ei.value.message
        server.stop()

    def test_runtime_event_via_gateway(self, tmp_path: Path):
        gw, server, sock = _make_gateway(tmp_path)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        import time

        for _ in range(100):
            if sock.exists():
                break
            time.sleep(0.02)
        client = GatewayClient(socket_path=sock)
        client.call("session.ensure", {
            "session_id": "s1", "manager_id": "manager", "roster": ["worker"],
        })
        # A7: runtime.event requires a REGISTERED runtime with matching
        # generation — register r1 first (identity check is fail-closed).
        client.call("runtime.register", {
            "session_id": "s1", "agent_id": "worker", "runtime_id": "r1",
            "generation": 1, "owner_pid": 1111, "nonce": "n1",
        })
        result = client.call("runtime.event", {
            "event": {
                "runtime_id": "r1", "generation": 1, "session_id": "s1",
                "agent_id": "worker", "request_id": "", "run_id": "",
                "kind": "TOOL_STARTED", "created_at": "2026-01-01T00:00:00Z",
                "payload": {"tool": "bash"},
            }
        })
        # register itself appends a RUNTIME_STATE (seq 1) — the event is seq 2.
        assert result["source_sequence"] >= 1
        events = client.call("events.list", {"cursor": 0, "filters": ["TOOL_STARTED"]})
        assert len(events["events"]) == 1
        assert events["cursor"] >= 1
        server.stop()


# ── rpc_stdio (SSH-bounded) ────────────────────────────────────────────


class TestRpcStdio:
    def test_stdio_roundtrip(self, running_server, monkeypatch, capsys):
        gw, server, sock = running_server
        req = GatewayRequest(v=GATEWAY_PROTOCOL_VERSION, id="abc", method="capabilities.get", params={})
        monkeypatch.setattr("sys.stdin", _LineReader(req.to_json() + "\n"))
        import codeagent.gateway.client as gw_client

        code = gw_client.rpc_stdio(socket_path=sock)
        out = capsys.readouterr().out
        resp = json.loads(out)
        assert resp["id"] == "abc"
        assert resp["ok"] is True
        assert code == 0

    def test_stdio_gateway_down(self, tmp_path: Path, monkeypatch, capsys):
        req = GatewayRequest(v=GATEWAY_PROTOCOL_VERSION, id="x", method="capabilities.get", params={})
        monkeypatch.setattr("sys.stdin", _LineReader(req.to_json() + "\n"))
        import codeagent.gateway.client as gw_client

        code = gw_client.rpc_stdio(socket_path=tmp_path / "none.sock")
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False
        assert "GATEWAY_DOWN" in out["error"]["code"] or "GATEWAY_CONNECT_FAILED" in out["error"]["code"]


class _LineReader:
    """Minimal stdin stand-in: one line, then EOF."""

    def __init__(self, line: str) -> None:
        self._line = line
        self._done = False

    def readline(self) -> str:
        if not self._done:
            self._done = True
            return self._line
        return ""

