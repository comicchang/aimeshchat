"""Gateway tests — UDS permissions, RPC round-trip, EventStore cursor/replay,
stale socket handling, MailboxService bridging."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from codeagent.domain.park import Lifecycle, ParkManifest
from codeagent.gateway.client import GatewayClient, rpc_stdio
from codeagent.gateway.events import EventStore
from codeagent.gateway.model import (
    ERR_GENERATION_STALE,
    ERR_NOT_AUTHORIZED,
    ERR_NOT_FOUND,
    ERR_OWNER_MISMATCH,
    ERR_PROTOCOL,
    ERR_PROTOCOL_CONFLICT,
    ERR_UNSUPPORTED_RUNTIME,
    GATEWAY_PROTOCOL_VERSION,
    GatewayError,
    GatewayRequest,
    GatewayResponse,
    RuntimeEventDraft,
)
from codeagent.gateway.server import GatewayServer
from codeagent.gateway.service import (
    AGENT_ENDED,
    AGENT_IDLE,
    AGENT_RUNNING,
    BINDING_BOUND,
    BINDING_LOST,
    BINDING_PENDING,
    CMD_ACK_CHAIN,
    CMD_AMBIGUOUS,
    CMD_CLAIMED,
    CMD_FAILED_SAFE,
    CMD_QUEUED,
    CMD_REVIVING,
    CMD_TRANSITIONS,
    CMD_TRIGGERING,
    CMD_TRIGGER_UNKNOWN,
    CMD_TURN_TRIGGERED,
    ERR_IDEMPOTENCY_CONFLICT,
    PRESENCE_ALIVE,
    PRESENCE_DEAD,
    PRESENCE_STALE,
    _advance_ack_chain,
    _normalize_capability,
    _sha256,
    AgentGateway,
)
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



# ── _advance_ack_chain（P1-2 统一 ack 协议状态机）───────────────────────
# 规范链 QUEUED→CLAIMED→REVIVING→TRIGGERING→TURN_TRIGGERED；插件可跳级
# 上报，gateway 沿链原子补齐中间态；非法迁移（反向/自环/终态/链外）拒绝。

class TestAdvanceAckChain:
    """_advance_ack_chain：ack 规范链推进/跳级补齐/非法迁移拒绝。"""

    def test_single_step_progression(self):
        """逐级推进：每一步返回 (目标态, 无中间态)。"""
        assert _advance_ack_chain(CMD_QUEUED, CMD_CLAIMED) == (CMD_CLAIMED, [])
        assert _advance_ack_chain(CMD_CLAIMED, CMD_REVIVING) == (CMD_REVIVING, [])
        assert _advance_ack_chain(CMD_REVIVING, CMD_TRIGGERING) == (CMD_TRIGGERING, [])
        assert _advance_ack_chain(CMD_TRIGGERING, CMD_TURN_TRIGGERED) == (CMD_TURN_TRIGGERED, [])

    def test_skip_level_fills_intermediates(self):
        """跳级上报：返回终态 + 补齐的中间态列表（按链序）。"""
        got = _advance_ack_chain(CMD_QUEUED, CMD_TURN_TRIGGERED)
        assert got == (CMD_TURN_TRIGGERED, [CMD_CLAIMED, CMD_REVIVING, CMD_TRIGGERING])
        # 中间跳级：CLAIMED 直接 TURN_TRIGGERED → 补 REVIVING/TRIGGERING
        got = _advance_ack_chain(CMD_CLAIMED, CMD_TURN_TRIGGERED)
        assert got == (CMD_TURN_TRIGGERED, [CMD_REVIVING, CMD_TRIGGERING])
        got = _advance_ack_chain(CMD_QUEUED, CMD_TRIGGERING)
        assert got == (CMD_TRIGGERING, [CMD_CLAIMED, CMD_REVIVING])

    def test_exhaustive_chain_pairs_advance(self):
        """链上任意 i<j 对都可推进（跳级补齐 = 链中间段）——验证整条链连通。"""
        for i in range(len(CMD_ACK_CHAIN)):
            for j in range(i + 1, len(CMD_ACK_CHAIN)):
                got = _advance_ack_chain(CMD_ACK_CHAIN[i], CMD_ACK_CHAIN[j])
                assert got is not None, f"{CMD_ACK_CHAIN[i]}→{CMD_ACK_CHAIN[j]} 应可推进"
                assert got[0] == CMD_ACK_CHAIN[j]
                assert got[1] == list(CMD_ACK_CHAIN[i + 1 : j])

    def test_reverse_rejected(self):
        """反向迁移拒绝（状态机不可回退）。"""
        assert _advance_ack_chain(CMD_CLAIMED, CMD_QUEUED) is None
        assert _advance_ack_chain(CMD_TURN_TRIGGERED, CMD_TRIGGERING) is None
        assert _advance_ack_chain(CMD_TRIGGERING, CMD_REVIVING) is None

    def test_self_loop_rejected(self):
        """自环迁移拒绝。"""
        for state in CMD_ACK_CHAIN:
            assert _advance_ack_chain(state, state) is None, f"{state}→{state} 应拒绝"

    def test_terminal_state_rejected(self):
        """终态（TURN_TRIGGERED）不可再迁移。"""
        assert _advance_ack_chain(CMD_TURN_TRIGGERED, CMD_QUEUED) is None
        assert _advance_ack_chain(CMD_TURN_TRIGGERED, CMD_CLAIMED) is None
        assert _advance_ack_chain(CMD_TURN_TRIGGERED, CMD_TURN_TRIGGERED) is None

    def test_off_chain_states_rejected(self):
        """链外状态（terminal 旁路态）不参与 ack 链：作起点或终点均拒绝。"""
        for off in (CMD_FAILED_SAFE, CMD_AMBIGUOUS, CMD_TRIGGER_UNKNOWN):
            assert _advance_ack_chain(off, CMD_CLAIMED) is None
            assert _advance_ack_chain(CMD_QUEUED, off) is None

    def test_unknown_states_rejected(self):
        """完全未知的状态名 → None（fail-closed）。"""
        assert _advance_ack_chain("NOPE", CMD_CLAIMED) is None
        assert _advance_ack_chain(CMD_QUEUED, "NOPE") is None
        assert _advance_ack_chain("", "") is None

    def test_consecutive_chain_hops_are_legal_transitions(self):
        """链上相邻跳必须都在 CMD_TRANSITIONS 合法迁移表内（契约一致性）。"""
        from codeagent.gateway.service import CMD_TRANSITIONS

        for i in range(len(CMD_ACK_CHAIN) - 1):
            src, dst = CMD_ACK_CHAIN[i], CMD_ACK_CHAIN[i + 1]
            assert dst in CMD_TRANSITIONS[src], f"合法链 {src}→{dst} 缺迁移表条目"


# ── runtime.send 幂等（IDEMPOTENCY_CONFLICT，设计 §3）─────────────────
# 幂等键 = request_id + payload_hash：同键重放返回原 command（不重复注入）；
# 同 request_id 不同 payload → IDEMPOTENCY_CONFLICT。

def _register_test_runtime(gw: AgentGateway, runtime_id: str = "rt-idem",
                           session_id: str = "s-idem", agent_id: str = "worker",
                           generation: int = 1, review_key: str = "rk-idem") -> None:
    """注册一个可用的测试 runtime（session.ensure + runtime.register）。"""
    gw.session_ensure({
        "session_id": session_id, "manager_id": "manager",
        "roster": [agent_id],
    })
    gw.runtime_register({
        "session_id": session_id, "agent_id": agent_id,
        "runtime_id": runtime_id, "generation": generation,
        "review_key": review_key, "runtime": "omp",
        "owner_pid": 1234, "nonce": "n-idem",
        "capabilities": ["park_revive", "correlated_turn_ack"],
    })


class TestRuntimeSendIdempotency:
    """runtime.send：request_id+payload_hash 幂等键 + 冲突拒绝。"""

    def test_same_request_id_same_payload_replays_cached(self, tmp_path: Path):
        """同 request_id+payload → 第二次返回原 command/state，不重复注入。"""
        gw, _server, _sock = _make_gateway(tmp_path)
        _register_test_runtime(gw)
        first = gw.runtime_send({
            "runtime_id": "rt-idem", "request_id": "req-1",
            "body": "review the diff", "from": "manager",
        })
        assert first["request_id"] == "req-1"
        second = gw.runtime_send({
            "runtime_id": "rt-idem", "request_id": "req-1",
            "body": "review the diff", "from": "manager",
        })
        # 幂等重放：同一 command_id，同一 state（不产生第二条命令）
        assert second["command_id"] == first["command_id"]
        assert second["state"] == first["state"]
        commands = gw._control.list_commands(runtime_id="rt-idem")
        assert len(commands) == 1, "幂等重放不得重复入队"

    def test_same_request_id_different_payload_conflict(self, tmp_path: Path):
        """同 request_id 不同 payload → IDEMPOTENCY_CONFLICT（带 state 上下文）。"""
        gw, _server, _sock = _make_gateway(tmp_path)
        _register_test_runtime(gw)
        gw.runtime_send({
            "runtime_id": "rt-idem", "request_id": "req-c",
            "body": "payload v1", "from": "manager",
        })
        with pytest.raises(GatewayError) as ei:
            gw.runtime_send({
                "runtime_id": "rt-idem", "request_id": "req-c",
                "body": "payload v2 DIFFERENT", "from": "manager",
            })
        assert ei.value.code == ERR_IDEMPOTENCY_CONFLICT
        assert ei.value.context.get("request_id") == "req-c"
        assert "state" in ei.value.context

    def test_payload_hash_auto_derived_from_body(self, tmp_path: Path):
        """未显式传 payload_hash → 由 body 的 sha256 自动派生并落库。"""
        gw, _server, _sock = _make_gateway(tmp_path)
        _register_test_runtime(gw)
        gw.runtime_send({
            "runtime_id": "rt-idem", "request_id": "req-h",
            "body": "same body", "from": "manager",
        })
        row = gw._control.get_command("req-h")
        assert row["payload_hash"] == _sha256("same body")
        assert row["payload_hash"] != _sha256("different body")

    def test_explicit_payload_hash_wins(self, tmp_path: Path):
        """显式 payload_hash 优先于 body 派生。"""
        gw, _server, _sock = _make_gateway(tmp_path)
        _register_test_runtime(gw)
        explicit = _sha256("explicitly hashed")
        gw.runtime_send({
            "runtime_id": "rt-idem", "request_id": "req-x",
            "body": "raw body", "payload_hash": explicit, "from": "manager",
        })
        row = gw._control.get_command("req-x")
        assert row["payload_hash"] == explicit

    def test_sha256_stable_and_derives_from_empty(self):
        """_sha256：确定性 + 空文本也产出稳定 64 位 hex。"""
        assert _sha256("a") == _sha256("a")
        assert _sha256("a") != _sha256("b")
        h = _sha256("")
        assert len(h) == 64

    def test_send_requires_request_id(self, tmp_path: Path):
        """缺 request_id/runtime_id → PROTOCOL（fail-closed）。"""
        gw, _server, _sock = _make_gateway(tmp_path)
        _register_test_runtime(gw)
        with pytest.raises(GatewayError) as ei:
            gw.runtime_send({"runtime_id": "rt-idem", "body": "x"})
        assert ei.value.code == ERR_PROTOCOL


# ── runtime.context set/get（Q5b：主 agent 模型上下文）─────────────────
# 插件在 model_change/thinking_level 时经 runtime.context_set 原子上报
# provider/model/variant/epoch；oracle CLI 经 runtime.context_get 继承。
# 带 generation 校验（防陈旧代际覆盖）；stopped 记录视为不存在。

class TestRuntimeContext:
    """runtime.context：model_context 原子读写 + 持久化。"""

    def _gw_with_runtime(self, tmp_path: Path, runtime_id: str = "rt-ctx",
                         review_key: str = "rk-ctx", generation: int = 1):
        gw, _server, _sock = _make_gateway(tmp_path)
        _register_test_runtime(gw, runtime_id=runtime_id, generation=generation,
                               review_key=review_key)
        return gw

    def test_set_get_roundtrip(self, tmp_path: Path):
        """context_set → context_get 原样返回 model_context 快照。"""
        gw = self._gw_with_runtime(tmp_path)
        out = gw.runtime_context_set({
            "runtime_id": "rt-ctx", "provider": "prov-a",
            "model": "model-b", "variant": "thinking", "epoch": 2,
        })
        assert out["model_context"] == {
            "provider": "prov-a", "model": "model-b",
            "variant": "thinking", "epoch": 2,
        }
        got = gw.runtime_context_get({"runtime_id": "rt-ctx"})
        assert got["model_context"] == {
            "provider": "prov-a", "model": "model-b",
            "variant": "thinking", "epoch": 2,
        }
        assert got["runtime_id"] == "rt-ctx"

    def test_get_by_review_key(self, tmp_path: Path):
        """context_get 支持 review_key 定位（多个候选取最近活跃）。"""
        gw = self._gw_with_runtime(tmp_path, runtime_id="rt-ctx", review_key="rk-ctx")
        gw.runtime_context_set({
            "runtime_id": "rt-ctx", "provider": "p", "model": "m", "variant": "", "epoch": 1,
        })
        got = gw.runtime_context_get({"review_key": "rk-ctx"})
        assert got["model_context"]["model"] == "m"

    def test_set_unknown_runtime_not_found(self, tmp_path: Path):
        """context_set 未知 runtime → NOT_FOUND。"""
        gw = self._gw_with_runtime(tmp_path)
        with pytest.raises(GatewayError) as ei:
            gw.runtime_context_set({"runtime_id": "ghost", "model": "m"})
        assert ei.value.code == ERR_NOT_FOUND

    def test_set_stale_generation_rejected(self, tmp_path: Path):
        """context_set 陈旧 generation → GENERATION_STALE（防代际覆盖）。"""
        gw = self._gw_with_runtime(tmp_path, generation=3)
        with pytest.raises(GatewayError) as ei:
            gw.runtime_context_set({
                "runtime_id": "rt-ctx", "model": "m", "generation": 1,
            })
        assert ei.value.code == ERR_GENERATION_STALE
        # 匹配 generation 则放行
        ok = gw.runtime_context_set({
            "runtime_id": "rt-ctx", "model": "m", "generation": 3,
        })
        assert ok["model_context"]["model"] == "m"

    def test_set_bad_epoch_rejected(self, tmp_path: Path):
        """epoch 非 int → PROTOCOL。"""
        gw = self._gw_with_runtime(tmp_path)
        with pytest.raises(GatewayError) as ei:
            gw.runtime_context_set({"runtime_id": "rt-ctx", "epoch": "abc"})
        assert ei.value.code == ERR_PROTOCOL

    def test_get_stopped_runtime_not_found(self, tmp_path: Path):
        """stopped 记录视为不存在（sweep 前不报旧答案）。"""
        gw = self._gw_with_runtime(tmp_path)
        gw.runtime_context_set({
            "runtime_id": "rt-ctx", "provider": "p", "model": "m", "epoch": 1,
        })
        with gw._runtimes_lock:
            gw._runtimes["rt-ctx"].status = "stopped"
        with pytest.raises(GatewayError) as ei:
            gw.runtime_context_get({"runtime_id": "rt-ctx"})
        assert ei.value.code == ERR_NOT_FOUND

    def test_get_without_report_returns_empty(self, tmp_path: Path):
        """已注册但未上报 model_context → 空 dict（由调用方决定降级）。"""
        gw = self._gw_with_runtime(tmp_path)
        got = gw.runtime_context_get({"runtime_id": "rt-ctx"})
        assert got["model_context"] == {}

    def test_set_persists_to_control_store(self, tmp_path: Path):
        """context_set 同步持久化到 ControlStore.runtime_generations。"""
        gw = self._gw_with_runtime(tmp_path)
        gw.runtime_context_set({
            "runtime_id": "rt-ctx", "provider": "prov", "model": "mdl",
            "variant": "v1", "epoch": 5,
        })
        stored = gw._control.get_generation("rt-ctx")
        assert stored is not None
        assert stored["model_context"] == {
            "provider": "prov", "model": "mdl", "variant": "v1", "epoch": 5,
        }


class TestRuntimeSendIdempotencyGuards:
    """runtime.send 前置校验（fail-closed 分支）。"""

    def test_send_empty_body_still_derives_hash(self, tmp_path: Path):
        """body 为空且未显式 payload_hash → 由空文本派生 sha256（_sha256 恒非空）。"""
        gw, _server, _sock = _make_gateway(tmp_path)
        _register_test_runtime(gw)
        out = gw.runtime_send({
            "runtime_id": "rt-idem", "request_id": "req-e", "body": "",
        })
        assert out["request_id"] == "req-e"
        assert gw._control.get_command("req-e")["payload_hash"] == _sha256("")


class TestRuntimeContextGuards:
    """runtime.context 前置校验分支。"""

    def test_set_missing_runtime_id(self, tmp_path: Path):
        """context_set 缺 runtime_id → PROTOCOL。"""
        gw, _server, _sock = _make_gateway(tmp_path)
        with pytest.raises(GatewayError) as ei:
            gw.runtime_context_set({"model": "m"})
        assert ei.value.code == ERR_PROTOCOL

    def test_set_non_int_generation(self, tmp_path: Path):
        """generation 非 int → PROTOCOL（防类型混乱）。"""
        gw = TestRuntimeContext()._gw_with_runtime(tmp_path)
        with pytest.raises(GatewayError) as ei:
            gw.runtime_context_set({"runtime_id": "rt-ctx", "model": "m", "generation": "abc"})
        assert ei.value.code == ERR_PROTOCOL


# ── events.py 补充覆盖（TestEventsUncovered）───────────────────────────
# 覆盖 EventStore 错误路径 / 边界：init chmod 失败、_connect 回滚、close、
# append/ingest 回滚、ingest 校验、未知 event type、空 payload、
# defence-in-depth 竞态重查、list_after 过滤、kind_stats、sweep 总量上限、
# _fetch_by_id、hostname 回退。

class TestEventsUncovered:
    """events.py 未覆盖路径：错误路径、schema 验证、空 payload、未知 event type。"""

    @staticmethod
    def _draft(**over):
        base = dict(
            runtime_id="r1", generation=1, session_id="s1", agent_id="a1",
            request_id="", run_id="", kind="ERROR",
            created_at="2026-01-01T00:00:00Z", payload={},
        )
        base.update(over)
        return RuntimeEventDraft(**base)

    def test_init_tolerates_chmod_failure(self, tmp_path: Path, monkeypatch):
        """os.chmod 抛 OSError（权限受限目录）→ __init__ 继续（except OSError: pass）。"""
        import codeagent.gateway.events as ev

        def _boom(*_a, **_k):
            raise OSError("permission denied")

        monkeypatch.setattr(ev.os, "chmod", _boom)
        store = EventStore(db_path=tmp_path / "e.sqlite3", source_host="h")
        assert store._db_path == tmp_path / "e.sqlite3"
        e = store.append_local(self._draft())
        assert e.source_sequence == 1

    def test_connect_rolls_back_and_reraises(self, tmp_path: Path):
        """_connect 内语句抛异常 → rollback 后重新抛出（117-119）。"""
        import sqlite3

        store = EventStore(db_path=tmp_path / "e.sqlite3", source_host="h")
        with pytest.raises(sqlite3.OperationalError):
            with store._connect() as conn:
                conn.execute("SELECT * FROM no_such_table")
        # rollback 未破坏连接状态——后续 append 仍可用
        e = store.append_local(self._draft())
        assert e.event_id > 0

    def test_close_is_idempotent_and_swallows_errors(self, tmp_path: Path):
        """close()：正常关闭可重复；连接 close 抛异常被吞掉（123-127）。"""
        store = EventStore(db_path=tmp_path / "e.sqlite3", source_host="h")
        store.close()
        store.close()  # 幂等

        class _BoomClose:
            def close(self):
                raise RuntimeError("already closed")

        store2 = EventStore(db_path=tmp_path / "e2.sqlite3", source_host="h")
        store2._conn = _BoomClose()
        store2.close()  # except Exception: pass

    def test_append_local_rolls_back_on_bad_payload(self, tmp_path: Path):
        """payload 含不可 JSON 序列化对象 → 事务回滚，无残留行（173-175）。"""
        store = EventStore(db_path=tmp_path / "e.sqlite3", source_host="h")
        with pytest.raises(TypeError):
            store.append_local(self._draft(payload={"bad": {1, 2}}))
        events, _ = store.list_after(0)
        assert events == []

    def test_append_local_schema_validation(self, tmp_path: Path):
        """event schema 验证：未知 kind / 非 dict payload / 缺 session_id 拒绝。"""
        store = EventStore(db_path=tmp_path / "e.sqlite3", source_host="h")
        with pytest.raises(GatewayError) as ei:
            store.append_local(self._draft(kind="NO_SUCH_KIND"))
        assert ei.value.code == ERR_PROTOCOL
        with pytest.raises(GatewayError) as ei:
            store.append_local(self._draft(payload="not-a-dict"))
        assert ei.value.code == ERR_PROTOCOL
        with pytest.raises(GatewayError) as ei:
            store.append_local(self._draft(session_id=""))
        assert ei.value.code == ERR_PROTOCOL
        events, _ = store.list_after(0)
        assert events == []

    def test_ingest_remote_requires_host_and_runtime(self, tmp_path: Path):
        """ingest_remote 缺 source_host/runtime_id → ValueError（208）。"""
        store = EventStore(db_path=tmp_path / "e.sqlite3", source_host="h")
        with pytest.raises(ValueError, match="source_host"):
            store.ingest_remote({
                "generation": 1, "source_sequence": 1, "kind": "ERROR",
            })
        with pytest.raises(ValueError, match="source_host"):
            store.ingest_remote({
                "source_host": "", "runtime_id": "", "generation": 1,
                "source_sequence": 1, "kind": "ERROR",
            })

    def test_ingest_remote_unknown_kind_skipped(self, tmp_path: Path, caplog):
        """未知 event type → log warning + 返回 None，不落库（216-221）。"""
        import logging

        store = EventStore(db_path=tmp_path / "e.sqlite3", source_host="h")
        with caplog.at_level(logging.WARNING, logger="codeagent.gateway.events"):
            out = store.ingest_remote({
                "source_host": "remote", "runtime_id": "r1", "generation": 1,
                "source_sequence": 1, "kind": "MYSTERY_KIND",
                "created_at": "", "payload": {},
            })
        assert out is None
        assert "MYSTERY_KIND" in caplog.text
        events, _ = store.list_after(0)
        assert events == []

    def test_ingest_remote_empty_payload(self, tmp_path: Path):
        """空 payload 边界：显式 {} 或缺 payload 键 → 持久化为空对象。"""
        store = EventStore(db_path=tmp_path / "e.sqlite3", source_host="h")
        ev = {
            "source_host": "remote", "runtime_id": "r1", "generation": 1,
            "source_sequence": 1, "kind": "ERROR", "created_at": "",
            "payload": {},
        }
        e1 = store.ingest_remote(ev)
        assert e1.payload == {}
        no_payload = {k: v for k, v in ev.items() if k != "payload"}
        no_payload["source_sequence"] = 2
        e2 = store.ingest_remote(no_payload)
        assert e2.payload == {}
        events, _ = store.list_after(0)
        assert len(events) == 2
        assert all(e.payload == {} for e in events)

    def test_ingest_remote_rolls_back_on_bad_payload(self, tmp_path: Path):
        """ingest payload 不可序列化 → 回滚 + 重抛（274-276）。"""
        store = EventStore(db_path=tmp_path / "e.sqlite3", source_host="h")
        with pytest.raises(TypeError):
            store.ingest_remote({
                "source_host": "remote", "runtime_id": "r1", "generation": 1,
                "source_sequence": 5, "kind": "ERROR", "created_at": "",
                "payload": {"bad": {1, 2}},
            })
        events, _ = store.list_after(0)
        assert events == []

    def test_ingest_remote_defence_in_depth_requery(self, tmp_path: Path):
        """竞态 defence-in-depth：existing 检查漏看已提交行 → INSERT OR IGNORE
        冲突被吞（fresh 连接 lastrowid=0）→ 重查拿回 event_id（261-266, 272）。"""
        ev = {
            "source_host": "remote", "runtime_id": "r1", "generation": 1,
            "source_sequence": 7, "kind": "TOOL_STARTED", "created_at": "",
            "payload": {"tool": "x"},
        }
        first = EventStore(db_path=tmp_path / "e.sqlite3", source_host="local").ingest_remote(ev)
        second_store = EventStore(db_path=tmp_path / "e.sqlite3", source_host="local")
        second_store._conn = _HideExistingRow(second_store._conn)
        again = second_store.ingest_remote(ev)
        assert again.event_id == first.event_id
        events, _ = second_store.list_after(0)
        assert len(events) == 1

    def test_ingest_remote_defence_in_depth_raise(self, tmp_path: Path):
        """竞态 defence-in-depth：INSERT OR IGNORE 被吞且重查无行 → ValueError（267-271）。"""
        store = EventStore(db_path=tmp_path / "e.sqlite3", source_host="h")
        store._conn = _NoOpInsertConn(store._conn)
        with pytest.raises(ValueError, match="failed to persist"):
            store.ingest_remote({
                "source_host": "remote", "runtime_id": "r1", "generation": 1,
                "source_sequence": 3, "kind": "ERROR", "created_at": "",
                "payload": {},
            })
        events, _ = store.list_after(0)
        assert events == []

    def test_list_after_invalid_filter_rejected(self, tmp_path: Path):
        """未知 kind 过滤 → ValueError（311）。"""
        store = EventStore(db_path=tmp_path / "e.sqlite3", source_host="h")
        with pytest.raises(ValueError, match="invalid event kind filter"):
            store.list_after(0, filters=["BOGUS_KIND"])

    def test_list_after_runtime_filter(self, tmp_path: Path):
        """list_after runtime_id 过滤分支（323-324）。"""
        store = EventStore(db_path=tmp_path / "e.sqlite3", source_host="h")
        store.append_local(self._draft(runtime_id="r1"))
        store.append_local(self._draft(runtime_id="r2"))
        events, _ = store.list_after(0, runtime_id="r1")
        assert len(events) == 1
        assert events[0].runtime_id == "r1"

    def test_kind_stats_counts_and_newest(self, tmp_path: Path):
        """kind_stats：per-kind 计数 + 最新时间戳；generation 作用域（389-410）。"""
        store = EventStore(db_path=tmp_path / "e.sqlite3", source_host="h")
        assert store.kind_stats("ghost") == {"counts": {}, "newest": {}}
        store.append_local(self._draft(kind="TOOL_STARTED", created_at="2026-01-01T00:00:01Z"))
        store.append_local(self._draft(kind="TOOL_STARTED", created_at="2026-01-01T00:00:02Z"))
        store.append_local(self._draft(kind="ERROR", created_at="2026-01-01T00:00:03Z"))
        stats = store.kind_stats("r1")
        assert stats["counts"] == {"TOOL_STARTED": 2, "ERROR": 1}
        assert stats["newest"]["TOOL_STARTED"] == "2026-01-01T00:00:02Z"
        assert stats["newest"]["ERROR"] == "2026-01-01T00:00:03Z"
        store.append_local(self._draft(generation=2, kind="ASSISTANT_PROGRESS",
                                       created_at="2026-01-01T00:00:04Z"))
        g2 = store.kind_stats("r1", generation=2)
        assert g2["counts"] == {"ASSISTANT_PROGRESS": 1}

    def test_sweep_caps_total_events(self, tmp_path: Path, monkeypatch):
        """总量上限：超出 MAX_TOTAL_EVENTS 驱逐最旧行（451-458）。"""
        import datetime as dt

        import codeagent.gateway.events as ev

        monkeypatch.setattr(ev, "MAX_TOTAL_EVENTS", 3)
        store = EventStore(db_path=tmp_path / "e.sqlite3", source_host="h")
        now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for i in range(5):
            store.append_local(self._draft(kind="RUNTIME_STATE", created_at=now, payload={"i": i}))
        removed = store.sweep()
        assert removed == 2
        events, _ = store.list_after(0)
        assert [e.payload["i"] for e in events] == [2, 3, 4]

    def test_fetch_by_id(self, tmp_path: Path):
        """_fetch_by_id：命中返回行 / 未命中 ValueError（464-473）。"""
        store = EventStore(db_path=tmp_path / "e.sqlite3", source_host="h")
        e = store.append_local(self._draft(kind="ERROR", payload={"code": 1}))
        row = store._fetch_by_id(e.event_id)
        assert row[0] == e.event_id
        assert row[9] == "ERROR"
        assert json.loads(row[11]) == {"code": 1}
        with pytest.raises(ValueError, match="event not found"):
            store._fetch_by_id(e.event_id + 9999)

    def test_local_hostname_fallback(self, monkeypatch):
        """os.uname 失败 → hostname 回退 'localhost'（496-497）。"""
        import codeagent.gateway.events as ev

        def _boom(*_a, **_k):
            raise OSError("no uname")

        monkeypatch.setattr(ev.os, "uname", _boom)
        assert ev._local_hostname() == "localhost"


class _FakeCursor:
    """Minimal sqlite3 cursor stand-in for simulating swallowed inserts."""

    def __init__(self, rows=None) -> None:
        self._rows = list(rows or [])
        self.lastrowid = 0

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _HideExistingRow:
    """Simulate the lost-update race: the existing-check SELECT misses a row
    committed by another connection, so INSERT OR IGNORE is swallowed (fresh
    connection → lastrowid stays 0) and the defence-in-depth re-query runs."""

    def __init__(self, real) -> None:
        self._real = real
        self._hid = False

    def execute(self, sql, params=()):
        sql = sql.strip()
        if (not self._hid and sql.startswith("SELECT event_id FROM runtime_events")
                and "WHERE source_host = ?" in sql):
            self._hid = True
            return _FakeCursor()
        return self._real.execute(sql, params)

    def commit(self):
        return self._real.commit()

    def rollback(self):
        return self._real.rollback()

    def close(self):
        return self._real.close()


class _NoOpInsertConn:
    """Simulate INSERT OR IGNORE being swallowed with no row surviving the
    re-query (defence-in-depth ValueError path)."""

    def __init__(self, real) -> None:
        self._real = real

    def execute(self, sql, params=()):
        sql = sql.strip()
        if sql.startswith("INSERT OR IGNORE INTO runtime_events"):
            return _FakeCursor()
        return self._real.execute(sql, params)

    def commit(self):
        return self._real.commit()

    def rollback(self):
        return self._real.rollback()

    def close(self):
        return self._real.close()


# ── gateway/service.py 未覆盖路径（TestServiceUncoveredPaths）──────────
# 覆盖：handshake 错误路径（OWNER_MISMATCH / GENERATION_STALE / roster /
# session-not-found）、runtime lifecycle 错误（spawn、heartbeat 恢复、
# stop/purge、probe 回退）、message routing 错误（send 失败、ack 迁移
# 冲突、hub offline/未知 peer）、persistence（park restore、peers/merges
# 持久化与损坏恢复、control 落盘失败）、cleanup（sweep 超时/stopped 清理、
# claims 过期）、并发幂等（同 request_id 并发 send 只入队一次）。


def _mk_gw(tmp_path: Path, **kw) -> AgentGateway:
    """Isolated gateway without a server — direct RPC calls."""
    store = MailboxStore(root=tmp_path / "mailbox")
    store.root.mkdir(parents=True, exist_ok=True)
    events = EventStore(db_path=tmp_path / "events.sqlite3", source_host="testhost")
    kw.setdefault("restore_from_park", False)
    kw.setdefault("peers_file", tmp_path / "peers.json")
    return AgentGateway(store=store, events=events, **kw)


def _reg(gw: AgentGateway, rid: str, sid: str, aid: str = "worker", rk: str = "",
         bs: str = "bs-1", caps=None, runtime: str = "omp", gen: int = 1,
         pid: int = 1234, nonce: str = "n-test") -> None:
    """session.ensure + runtime.register for a usable test runtime."""
    gw.session_ensure({"session_id": sid, "manager_id": "manager", "roster": [aid]})
    gw.runtime_register({
        "session_id": sid, "agent_id": aid, "runtime_id": rid, "generation": gen,
        "review_key": rk or rid, "backend_session_id": bs, "runtime": runtime,
        "owner_pid": pid, "nonce": nonce,
        "capabilities": list(caps) if caps is not None
        else ["park_revive", "correlated_turn_ack"],
    })


class _FakeParkRegistry:
    """Stand-in for ParkRegistry with a configurable manifest table."""

    def __init__(self, manifests=None, fail_list=False, fail_update=False):
        self._manifests = dict(manifests or {})
        self.fail_list = fail_list
        self.fail_update = fail_update
        self.updates: list = []
        self.renews: list = []
        self.released: list = []

    def lookup(self, review_key: str):
        return self._manifests.get(review_key)

    def list_active(self):
        if self.fail_list:
            raise RuntimeError("park db locked")
        return [m for m in self._manifests.values()
                if getattr(m, "lifecycle", Lifecycle.HOT_PARKED) == Lifecycle.HOT_PARKED]

    def update(self, review_key: str, manifest) -> None:
        if self.fail_update:
            raise RuntimeError("park update failed")
        self.updates.append((review_key, manifest))
        self._manifests[review_key] = manifest

    def renew(self, review_key: str) -> None:
        self.renews.append(review_key)

    def release(self, review_key: str) -> None:
        self.released.append(review_key)


class TestServiceUncoveredPaths:
    """service.py 未覆盖路径：handshake/lifecycle/routing 错误 + persistence + cleanup。"""

    # ── 工具函数 ──────────────────────────────────────────────────────

    def test_normalize_capability_variants(self):
        """_normalize_capability：_vN 后缀剥离；基础名/无后缀原样返回。"""
        assert _normalize_capability("park_revive_v1") == "park_revive"
        assert _normalize_capability("correlated_turn_ack_v3") == "correlated_turn_ack"
        assert _normalize_capability("park_revive") == "park_revive"
        assert _normalize_capability("no_suffix") == "no_suffix"
        assert _normalize_capability("v99") == "v99"  # 前缀式 _vN 不剥离

    def test_advance_ack_chain_illegal_hop(self, monkeypatch):
        """ack 链中段非法迁移 → None（CMD_TRANSITIONS 缺失 hop 时 fail-closed）。"""
        import codeagent.gateway.service as svc

        transitions = {k: set(v) for k, v in svc.CMD_TRANSITIONS.items()}
        transitions[CMD_QUEUED].discard(CMD_CLAIMED)
        monkeypatch.setattr(svc, "CMD_TRANSITIONS", transitions)
        assert _advance_ack_chain(CMD_QUEUED, CMD_CLAIMED) is None

    # ── 构造 / 生命周期 ───────────────────────────────────────────────

    def test_init_accepts_kernel_and_restores_from_park(self, tmp_path, monkeypatch):
        """__init__：kernel 直传 + restore_from_park 恢复 HOT_PARKED 占位记录
        （含 model_context 持久化恢复）。"""
        import codeagent.park.registry as parkmod

        fake_kernel = SimpleNamespace()
        # 预置 ControlStore 行（events db 同目录 control.sqlite3）——恢复路径会读 model_context。
        gw0 = _mk_gw(tmp_path)
        slug = "rk-" + "y" * 26  # ≥24 chars → 确定性 runtime_id
        runtime_id = "park-" + slug[-24:]
        gw0._control.upsert_generation(
            runtime_id=runtime_id, current_generation=2, owner_nonce="",
            presence="stale", binding="bound", backend_session_id="bs-park",
            binding_epoch=3, agent_state="ended",
            model_context=json.dumps({"provider": "prov", "model": "mdl"}),
        )
        gw0.stop()

        manifest = SimpleNamespace(
            review_key=slug, swarm_session_id="s-park", backend_session_id="bs-park",
            mailbox_agent_id="oracle", round=2, host="host-x", agent_type="omp",
            created_at=time.time() - 100, lifecycle=Lifecycle.HOT_PARKED,
        )
        fake = _FakeParkRegistry({"rk-any": manifest})
        monkeypatch.setattr(parkmod, "ParkRegistry", lambda: fake)
        gw = AgentGateway(
            store=MailboxStore(root=tmp_path / "mb2"),
            events=EventStore(db_path=tmp_path / "events.sqlite3", source_host="h"),
            kernel=fake_kernel, restore_from_park=True,
            peers_file=tmp_path / "peers2.json",
        )
        try:
            assert gw._kernel is fake_kernel
            rec = gw._runtimes[runtime_id]
            assert rec.status == "unknown"          # A2: 占位非活 runtime
            assert rec.presence == PRESENCE_STALE
            assert rec.binding == BINDING_BOUND
            assert rec.agent_state == AGENT_ENDED
            assert rec.generation == 3              # round 2 + 1
            assert rec.model_context == {"provider": "prov", "model": "mdl"}
        finally:
            gw.stop()  # stop()：idempotent + join sweep thread

    def test_init_restore_park_unavailable(self, tmp_path, monkeypatch):
        """park registry 不可用 → 恢复跳过（不阻断 gateway 启动）。"""
        import codeagent.park.registry as parkmod

        monkeypatch.setattr(parkmod, "ParkRegistry",
                            lambda: (_ for _ in ()).throw(RuntimeError("no park")))
        gw = AgentGateway(
            store=MailboxStore(root=tmp_path / "mb-un"),
            events=EventStore(db_path=tmp_path / "e-un.sqlite3", source_host="h"),
            restore_from_park=True, peers_file=tmp_path / "peers-un.json",
        )
        assert gw._runtimes == {}
        gw.stop()

    def test_sweep_loop_iteration_and_retention_throttle(self, tmp_path):
        """sweep 循环体 + retention 节流：首轮执行完整 retention，后续被 3600s 节流。"""
        gw = _mk_gw(tmp_path)
        gw.stop()  # 停掉 30s 间隔线程
        gw._sweep_stop = threading.Event()
        gw._sweep_interval = 0.02
        gw._start_sweep_loop()
        time.sleep(0.12)  # 让后台循环跑几轮
        assert gw._last_retention_sweep > 0
        before = gw._last_retention_sweep
        gw._retention_sweep()  # 节流早退
        assert gw._last_retention_sweep == before
        gw.stop()

    def test_sweep_loop_tolerates_iteration_error(self, tmp_path, monkeypatch, caplog):
        """sweep 迭代抛异常 → 记 warning，线程不退出。"""
        import logging

        def _boom(self):
            raise RuntimeError("sweep boom")

        monkeypatch.setattr(AgentGateway, "_sweep_once", _boom)
        gw = _mk_gw(tmp_path)
        gw.stop()
        gw._sweep_stop = threading.Event()
        gw._sweep_interval = 0.02
        with caplog.at_level(logging.WARNING, logger="codeagent.gateway.service"):
            gw._start_sweep_loop()
            time.sleep(0.12)
        assert "sweep iteration failed" in caplog.text
        gw.stop()

    def test_sweep_once_offline_dead_cleanup(self, tmp_path, monkeypatch, caplog):
        """_sweep_once：active→offline（含无效 created_at 容错）、stale→dead、
        stopped 记录清除 + ControlStore 同步、过期 claim 剪除、hub peer 联动 offline。"""
        import logging

        gw = _mk_gw(tmp_path)
        _reg(gw, "r-stale", "s-stale", rk="rk-stale")
        _reg(gw, "r-badts", "s-badts", rk="rk-badts")
        _reg(gw, "r-stop", "s-stop", rk="rk-stop")
        _reg(gw, "r-fresh", "s-fresh", rk="rk-fresh")  # 活跃 → sweep 跳过
        from datetime import datetime, timedelta, timezone

        with gw._runtimes_lock:
            for rid, created in (("r-stale", datetime.now(timezone.utc) - timedelta(hours=2)),
                                 ("r-badts", "not-a-date")):
                rec = gw._runtimes[rid]
                rec.created_at = created.strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(created, datetime) else created
                rec.last_activity = time.time() - 1000
            gw._runtimes["r-stop"].status = "stopped"
        # 过期 claim
        gw._claims["s-stale:worker"] = {"owner": "x", "claimed_at": 0, "expires_at": time.time() - 5}
        # hub peer 与 r-stale 同 session/agent → 联动 offline + 持久化
        gw.hub_register({"peer_id": "p-stale", "session_id": "s-stale",
                         "agent_id": "worker", "host_alias": "h-stale"})
        # offline 事件 append 失败 → 告警但状态仍推进
        real_append = gw._events.append_local

        def _flaky_append(draft, **kw):
            if draft.payload.get("reason") == "heartbeat_timeout":
                raise RuntimeError("events db full")
            return real_append(draft, **kw)

        monkeypatch.setattr(gw._events, "append_local", _flaky_append)
        with caplog.at_level(logging.WARNING, logger="codeagent.gateway.service"):
            offline = gw._sweep_once()
        assert set(offline) == {"r-stale", "r-badts"}
        assert "offline event append failed" in caplog.text
        assert gw._runtimes["r-stale"].status == "offline"
        assert gw._runtimes["r-stale"].presence == PRESENCE_DEAD  # dead 衰变
        assert gw._runtimes["r-badts"].status == "offline"
        assert gw._runtimes["r-fresh"].status == "active"  # 活跃记录不被误判 offline
        assert "r-stop" not in gw._runtimes                 # stopped 记录清除
        assert gw._claims == {}                              # 过期 claim 剪除
        assert gw._peers["p-stale"].status == "offline"      # peer 联动
        assert (tmp_path / "peers.json").exists()            # _save_peers 落盘

    def test_sweep_once_delete_generation_failure(self, tmp_path, monkeypatch, caplog):
        """stopped 记录清除时 ControlStore 删除失败 → 告警但不阻断。"""
        import logging

        gw = _mk_gw(tmp_path)
        _reg(gw, "r-g1", "s-g1", rk="rk-g1")
        with gw._runtimes_lock:
            gw._runtimes["r-g1"].status = "stopped"

        def _boom(*a, **k):
            raise RuntimeError("db locked")

        monkeypatch.setattr(gw._control, "delete_generation", _boom)
        with caplog.at_level(logging.WARNING, logger="codeagent.gateway.service"):
            gw._sweep_once()
        assert "control generation delete failed" in caplog.text
        assert "r-g1" not in gw._runtimes

    # ── capabilities / session ────────────────────────────────────────

    def test_capabilities_fallback_on_registry_error(self, tmp_path, monkeypatch, caplog):
        """RuntimeRegistry 不可用 → capabilities 降级 runtimes=['omp'] 并告警。"""
        import logging

        import codeagent.runtime.registry as rtmod

        class _Boom:
            def __init__(self, *a, **k):
                raise RuntimeError("no registry")

        monkeypatch.setattr(rtmod, "RuntimeRegistry", _Boom)
        gw = _mk_gw(tmp_path)
        with caplog.at_level(logging.WARNING, logger="codeagent.gateway.service"):
            caps = gw.capabilities({})
        assert caps["runtimes"] == ["omp"]
        assert "listing failed" in caplog.text
        gw.stop()

    def test_session_ensure_errors(self, tmp_path):
        """session.ensure：缺参数 → PROTOCOL；manager 变更 → MANIFEST_CONFLICT。"""
        gw = _mk_gw(tmp_path)
        with pytest.raises(GatewayError) as ei:
            gw.session_ensure({"session_id": ""})
        assert ei.value.code == "PROTOCOL"
        gw.session_ensure({"session_id": "s-mc", "manager_id": "m1", "roster": ["a1"]})
        with pytest.raises(GatewayError) as ei:
            gw.session_ensure({"session_id": "s-mc", "manager_id": "m2", "roster": ["a1"]})
        assert ei.value.code == "MANIFEST_CONFLICT"

    # ── runtime.register（handshake）──────────────────────────────────

    def test_register_identity_errors(self, tmp_path):
        """handshake fail-closed：缺身份 / 代际陈旧 / owner 不匹配 / session 缺失 /
        roster 外成员。"""
        gw = _mk_gw(tmp_path)
        with pytest.raises(GatewayError) as ei:
            gw.runtime_register({"session_id": "", "agent_id": "", "runtime_id": ""})
        assert ei.value.code == ERR_NOT_AUTHORIZED
        gw.session_ensure({"session_id": "s-id", "manager_id": "manager", "roster": ["worker"]})
        gw.runtime_register({"session_id": "s-id", "agent_id": "worker", "runtime_id": "r1",
                             "generation": 2, "owner_pid": 1, "nonce": "n1"})
        with pytest.raises(GatewayError) as ei:
            gw.runtime_register({"session_id": "s-id", "agent_id": "worker",
                                 "runtime_id": "r1", "generation": 1})
        assert ei.value.code == ERR_GENERATION_STALE
        with pytest.raises(GatewayError) as ei:
            gw.runtime_register({"session_id": "s-id", "agent_id": "worker", "runtime_id": "r1",
                                 "generation": 2, "owner_pid": 999, "nonce": "n1"})
        assert ei.value.code == ERR_OWNER_MISMATCH
        with pytest.raises(GatewayError) as ei:
            gw.runtime_register({"session_id": "no-such", "agent_id": "worker",
                                 "runtime_id": "rx", "generation": 1})
        assert ei.value.code == ERR_NOT_AUTHORIZED
        with pytest.raises(GatewayError) as ei:
            gw.runtime_register({"session_id": "s-id", "agent_id": "ghost",
                                 "runtime_id": "rx", "generation": 1})
        assert ei.value.code == ERR_NOT_AUTHORIZED

    def test_register_binding_epoch_and_preserve(self, tmp_path):
        """binding_epoch：新绑定=1、更换 backend session +1；重注册保留 spec/initial_task。"""
        gw = _mk_gw(tmp_path)
        _reg(gw, "r-b", "s-b", bs="bs-1")
        assert gw._runtimes["r-b"].binding_epoch == 1
        _reg(gw, "r-b", "s-b", bs="bs-2")
        assert gw._runtimes["r-b"].binding_epoch == 2
        with gw._runtimes_lock:
            gw._runtimes["r-b"].spec = {"spawn": 1}
        _reg(gw, "r-b", "s-b", bs="bs-3")
        assert gw._runtimes["r-b"].binding_epoch == 3
        assert gw._runtimes["r-b"].spec == {"spawn": 1}  # 先前的 spec 保留

    def test_register_review_mirror_failure(self, tmp_path, monkeypatch, caplog):
        """review 镜像 upsert 失败 → 告警，注册本身成功。"""
        import logging

        gw = _mk_gw(tmp_path)

        def _boom(*a, **k):
            raise RuntimeError("review db locked")

        monkeypatch.setattr(gw._control, "upsert_review", _boom)
        with caplog.at_level(logging.WARNING, logger="codeagent.gateway.service"):
            _reg(gw, "r-mir", "s-mir")
        assert "review mirror failed" in caplog.text
        assert gw._runtimes["r-mir"].status == "active"

    def test_register_park_backend_sync(self, tmp_path, monkeypatch):
        """注册时把 backend_session_id 同步进 HOT_PARKED manifest；
        RELEASED manifest 不复活；update 失败仅告警。"""
        import codeagent.park.registry as parkmod

        hot = ParkManifest(review_key="rk-sync", swarm_session_id="s-sync",
                           backend_session_id="old-bs", lifecycle=Lifecycle.HOT_PARKED)
        fake = _FakeParkRegistry({"rk-sync": hot})
        monkeypatch.setattr(parkmod, "ParkRegistry", lambda: fake)
        gw = _mk_gw(tmp_path)
        gw.session_ensure({"session_id": "s-sync", "manager_id": "manager", "roster": ["worker"]})
        gw.runtime_register({"session_id": "s-sync", "agent_id": "worker", "runtime_id": "r-sync",
                             "generation": 1, "review_key": "rk-sync", "backend_session_id": "new-bs"})
        assert len(fake.updates) == 1
        assert fake.updates[0][0] == "rk-sync"
        assert fake.updates[0][1].backend_session_id == "new-bs"

        released = ParkManifest(review_key="rk-rel", swarm_session_id="s-rel",
                                lifecycle=Lifecycle.RELEASED_SOFT)
        fake2 = _FakeParkRegistry({"rk-rel": released})
        monkeypatch.setattr(parkmod, "ParkRegistry", lambda: fake2)
        gw.session_ensure({"session_id": "s-rel", "manager_id": "manager", "roster": ["worker"]})
        gw.runtime_register({"session_id": "s-rel", "agent_id": "worker", "runtime_id": "r-rel",
                             "generation": 1, "review_key": "rk-rel", "backend_session_id": "b"})
        assert fake2.updates == []  # A1: RELEASED 不得复活

        fake3 = _FakeParkRegistry(
            {"rk-fail": ParkManifest(review_key="rk-fail", swarm_session_id="s-fail",
                                     lifecycle=Lifecycle.HOT_PARKED)},
            fail_update=True,
        )
        monkeypatch.setattr(parkmod, "ParkRegistry", lambda: fake3)
        gw.session_ensure({"session_id": "s-fail", "manager_id": "manager", "roster": ["worker"]})
        gw.runtime_register({"session_id": "s-fail", "agent_id": "worker", "runtime_id": "r-fail",
                             "generation": 1, "review_key": "rk-fail", "backend_session_id": "b"})
        assert gw._runtimes["r-fail"].status == "active"  # 同步失败不影响注册

    def test_register_initial_task_scan(self, tmp_path, monkeypatch):
        """注册时扫描 inbox：取最旧 TASK（含 command_id）；损坏消息跳过；扫描失败容错。"""
        gw = _mk_gw(tmp_path)
        gw.session_ensure({"session_id": "s-task", "manager_id": "manager", "roster": ["worker"]})
        inbox = gw._store.agent_subdir("s-task", "worker", "inbox")
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "junk.json").write_text("{corrupt")
        (inbox / "m1.json").write_text(json.dumps(
            {"kind": "TASK", "body": "first task", "msg_id": "m1", "command_id": "cmd-1"}))
        (inbox / "m2.json").write_text(json.dumps({"kind": "REPORT", "body": "not a task"}))
        out = gw.runtime_register({"session_id": "s-task", "agent_id": "worker",
                                   "runtime_id": "r-task", "generation": 1})
        assert out["initial_task"] == "first task"
        assert out["initial_task_msg_id"] == "m1"
        assert out["initial_task_command_id"] == "cmd-1"

        def _boom(*a, **k):
            raise OSError("inbox gone")

        monkeypatch.setattr(gw._store, "list_messages", _boom)
        out2 = gw.runtime_register({"session_id": "s-task", "agent_id": "worker",
                                    "runtime_id": "r-task2", "generation": 1})
        assert out2["initial_task"] == ""

    # ── runtime.declare / spawn / heartbeat ───────────────────────────

    def test_declare_paths(self, tmp_path, monkeypatch):
        """runtime.declare：弱身份门控 + 派生 runtime_id + 幂等重声明。"""
        import codeagent.park.registry as parkmod

        m = ParkManifest(review_key="rk-decl", swarm_session_id="s-decl")
        fake = _FakeParkRegistry({"rk-decl": m})
        monkeypatch.setattr(parkmod, "ParkRegistry", lambda: fake)
        gw = _mk_gw(tmp_path)
        with pytest.raises(GatewayError) as ei:
            gw.runtime_declare({"backend_session_id": "b"})
        assert ei.value.code == ERR_NOT_AUTHORIZED
        with pytest.raises(GatewayError) as ei:
            gw.runtime_declare({"review_key": "rk-decl"})
        assert ei.value.code == ERR_NOT_AUTHORIZED
        with pytest.raises(GatewayError) as ei:
            gw.runtime_declare({"review_key": "ghost", "backend_session_id": "b"})
        assert ei.value.code == ERR_NOT_FOUND
        out = gw.runtime_declare({"review_key": "rk-decl", "backend_session_id": "bs-decl"})
        assert out["runtime_id"].startswith("native-")
        assert out["status"] == "active"
        rec = gw._runtimes[out["runtime_id"]]
        assert rec.runtime == "native"
        assert rec.binding == BINDING_BOUND
        assert rec.binding_epoch == 1
        # 幂等：同 backend_session_id 重声明 → 复用同记录
        out2 = gw.runtime_declare({"review_key": "rk-decl", "backend_session_id": "bs-decl"})
        assert out2["runtime_id"] == out["runtime_id"]
        # 显式 runtime_id 重声明 → 刷新活跃度
        out3 = gw.runtime_declare({"review_key": "rk-decl", "backend_session_id": "bs-decl",
                                   "runtime_id": out["runtime_id"]})
        assert out3["runtime_id"] == out["runtime_id"]

    def test_spawn_and_heartbeat_restore(self, tmp_path, monkeypatch):
        """runtime.spawn 委托 registry（spec 保留）→ 离线 runtime 经 heartbeat 恢复
        （含 peer 联动 online + park renew）；未知 runtime → NOT_FOUND。"""
        import codeagent.park.registry as parkmod
        import codeagent.runtime.registry as rtmod

        handle = SimpleNamespace(runtime_id="sp-1", generation=3, backend_session_id="bs-sp",
                                 host_alias="host-sp", mode="hot",
                                 capabilities=["park_revive", "correlated_turn_ack"])

        class _FakeRT:
            def __init__(self):
                pass

            def spawn(self, name, request):
                return handle

            def names(self):
                return ["omp"]

            def probe(self, rid):
                raise RuntimeError("no handle")

            def stop(self, rid, reason):
                raise RuntimeError("no handle")

        monkeypatch.setattr(rtmod, "RuntimeRegistry", _FakeRT)
        fake_park = _FakeParkRegistry()
        monkeypatch.setattr(parkmod, "ParkRegistry", lambda: fake_park)
        gw = _mk_gw(tmp_path)
        out = gw.runtime_spawn({"runtime": "omp", "session_id": "s-sp", "agent_id": "worker",
                                "review_key": "rk-sp", "workdir": "/tmp", "task": "t0",
                                "model": "m", "short_task": True})
        assert out["runtime_id"] == "sp-1"
        assert out["generation"] == 3
        assert out["mode"] == "hot"
        rec = gw._runtimes["sp-1"]
        assert rec.binding == BINDING_PENDING
        assert rec.spec["task"] == "t0"
        gw.hub_register({"peer_id": "p-sp", "session_id": "s-sp", "agent_id": "worker",
                         "host_alias": "h-sp"})
        with gw._runtimes_lock:
            rec.status = "offline"
            rec.presence = PRESENCE_STALE
        hb = gw.runtime_heartbeat({"runtime_id": "sp-1"})
        assert hb["status"] == "active"
        assert gw._runtimes["sp-1"].presence == PRESENCE_ALIVE
        assert gw._peers["p-sp"].status == "online"      # 联动恢复
        assert fake_park.renews == ["rk-sp"]             # park lease renew
        with pytest.raises(GatewayError) as ei:
            gw.runtime_heartbeat({"runtime_id": "ghost"})
        assert ei.value.code == ERR_NOT_FOUND

    # ── status / list ─────────────────────────────────────────────────

    def test_status_and_list(self, tmp_path):
        """runtime.status + runtime.list：session 过滤 / 全量快照。"""
        gw = _mk_gw(tmp_path)
        _reg(gw, "r-a", "s-a", rk="rk-a")
        _reg(gw, "r-b", "s-b", rk="rk-b")
        st = gw.runtime_status({"runtime_id": "r-a"})
        assert st["status"] == "active"
        assert st["presence"] == PRESENCE_ALIVE
        assert st["binding"] == BINDING_BOUND
        all_rt = gw.runtimes_list({})["runtimes"]
        assert {r["runtime_id"] for r in all_rt} == {"r-a", "r-b"}
        only_a = gw.runtimes_list({"session_id": "s-a"})["runtimes"]
        assert [r["runtime_id"] for r in only_a] == ["r-a"]
        assert gw.runtimes_list({"session_id": "nope"})["runtimes"] == []

    # ── runtime.event 归约 ────────────────────────────────────────────

    def test_event_reduction_and_errors(self, tmp_path):
        """runtime.event：未知 runtime / 代际陈旧拒绝；TURN_STARTED → agent_running；
        payload 显式 agent_state/binding 跳转并落盘。"""
        gw = _mk_gw(tmp_path)
        with pytest.raises(GatewayError) as ei:
            gw.runtime_event({"event": {"runtime_id": "ghost", "generation": 1,
                                        "kind": "ERROR", "session_id": "s"}})
        assert ei.value.code == ERR_NOT_FOUND
        _reg(gw, "r-ev", "s-ev")
        with pytest.raises(GatewayError) as ei:
            gw.runtime_event({"event": {"runtime_id": "r-ev", "generation": 9,
                                        "kind": "ERROR", "session_id": "s-ev",
                                        "agent_id": "worker"}})
        assert ei.value.code == ERR_GENERATION_STALE
        gw.runtime_event({"event": {"runtime_id": "r-ev", "generation": 1, "kind": "TURN_STARTED",
                                    "session_id": "s-ev", "agent_id": "worker", "payload": {}}})
        assert gw._runtimes["r-ev"].agent_state == AGENT_RUNNING
        gw.runtime_event({"event": {"runtime_id": "r-ev", "generation": 1, "kind": "RUNTIME_STATE",
                                    "session_id": "s-ev", "agent_id": "worker",
                                    "payload": {"agent_state": "idle", "binding": "lost"}}})
        assert gw._runtimes["r-ev"].agent_state == AGENT_IDLE
        assert gw._runtimes["r-ev"].binding == BINDING_LOST
        stored = gw._control.get_generation("r-ev")
        assert stored["agent_state"] == "idle"  # 变化已镜像控制面
        assert stored["binding"] == "lost"

    # ── runtime.send 门控与状态机 ─────────────────────────────────────

    def test_send_hot_and_gates(self, tmp_path):
        """runtime.send：hot 投递（mailbox_persisted）、binding_pending（wait_binding
        超时）、not_hot（presence 非 alive）、非 omp backend → FAILED_SAFE + unsupported。"""
        gw = _mk_gw(tmp_path)
        _reg(gw, "r-hot", "s-hot")
        out = gw.runtime_send({"runtime_id": "r-hot", "request_id": "req-1",
                               "body": "go", "run_id": "run-1"})
        assert out["status"] == "mailbox_persisted"
        assert out["state"] == CMD_QUEUED
        assert out["msg_id"]
        # binding 未建立 → binding_pending（wait_binding 窗口内未绑定）
        _reg(gw, "r-pend", "s-pend", bs="")
        out = gw.runtime_send({"runtime_id": "r-pend", "request_id": "req-2", "body": "x",
                               "run_id": "run-1", "wait_binding": True,
                               "wait_binding_timeout": 0.05})
        assert out["status"] == "binding_pending"
        assert out["detail"]["gate"] == "binding_pending"
        # bound 但 presence 非 alive → 仅持久队列
        _reg(gw, "r-st", "s-st")
        with gw._runtimes_lock:
            gw._runtimes["r-st"].presence = PRESENCE_STALE
        out = gw.runtime_send({"runtime_id": "r-st", "request_id": "req-3", "body": "x",
                               "run_id": "run-1"})
        assert out["status"] == "mailbox_persisted"
        assert out["detail"]["gate"] == "not_hot"
        # 非 omp backend：先 FAILED_SAFE 再拒绝（无插件 consumer）
        _reg(gw, "r-nat", "s-nat", runtime="native")
        with pytest.raises(GatewayError) as ei:
            gw.runtime_send({"runtime_id": "r-nat", "request_id": "req-4", "body": "x",
                             "run_id": "run-1"})
        assert ei.value.code == ERR_UNSUPPORTED_RUNTIME
        assert gw._control.get_command("req-4")["state"] == CMD_FAILED_SAFE

    def test_send_revive_paths(self, tmp_path, monkeypatch):
        """ended/parked runtime → park-revive：hot/warm → session_live；cold →
        binding_pending；失败/异常 → failed_safe。"""
        import codeagent.park.router as router

        gw = _mk_gw(tmp_path)
        _reg(gw, "r-rev", "s-rev")
        gw.runtime_lifecycle({"runtime_id": "r-rev", "event": "registry_parked"})
        assert gw._runtimes["r-rev"].agent_state == AGENT_ENDED

        class _RV:
            def __init__(self, success, method="", context=None):
                self.success = success
                self.method = method
                self.context = context or {}

        calls = []

        def _revive_hot(key, body):
            calls.append((key, body))
            return _RV(True, "hot")

        monkeypatch.setattr(router, "park_revive", _revive_hot)
        out = gw.runtime_send({"runtime_id": "r-rev", "request_id": "req-r1",
                               "body": "revive me", "run_id": "run-1"})
        assert out["status"] == "session_live"
        assert calls == [("r-rev", "revive me")]
        monkeypatch.setattr(router, "park_revive", lambda k, b: _RV(True, "cold"))
        out = gw.runtime_send({"runtime_id": "r-rev", "request_id": "req-r2",
                               "body": "x", "run_id": "run-1"})
        assert out["status"] == "binding_pending"
        assert out["detail"]["revive"] == "cold"
        monkeypatch.setattr(router, "park_revive", lambda k, b: _RV(False, "error"))
        out = gw.runtime_send({"runtime_id": "r-rev", "request_id": "req-r3",
                               "body": "x", "run_id": "run-1"})
        assert out["status"] == "failed_safe"
        assert out["detail"]["revive_failed"] == "error"

        def _revive_boom(key, body):
            raise RuntimeError("router down")

        monkeypatch.setattr(router, "park_revive", _revive_boom)
        out = gw.runtime_send({"runtime_id": "r-rev", "request_id": "req-r4",
                               "body": "x", "run_id": "run-1"})
        assert out["status"] == "failed_safe"
        assert "revive_error" in out["detail"]

    def test_send_concurrent_enqueue_replay(self, tmp_path):
        """并发入队失败（另一线程先赢）→ 走幂等重放，不重复注入。"""
        gw = _mk_gw(tmp_path)
        _reg(gw, "r-race", "s-race")
        real_enqueue = gw._control.enqueue_command

        def _lost_race(*a, **k):
            real_enqueue(*a, **k)  # 模拟另一线程先入队
            return False

        gw._control.enqueue_command = _lost_race
        out = gw.runtime_send({"runtime_id": "r-race", "request_id": "req-race",
                               "body": "x", "run_id": "run-1"})
        assert out["request_id"] == "req-race"
        assert len(gw._control.list_commands(runtime_id="r-race")) == 1

    def test_send_mailbox_failed_receipt(self, tmp_path, monkeypatch):
        """mailbox 写入失败 receipt → FAILED_SAFE（可安全重试，不假报触发）。"""
        gw = _mk_gw(tmp_path)
        _reg(gw, "r-fail", "s-fail")
        monkeypatch.setattr(gw._svc, "send",
                            lambda **k: SimpleNamespace(status="failed",
                                                        error="no route to agent",
                                                        msg_id=""))
        out = gw.runtime_send({"runtime_id": "r-fail", "request_id": "req-f1",
                               "body": "x", "run_id": "run-1"})
        assert out["status"] == "failed_safe"
        assert "no route to agent" in out["detail"]["reason"]

    # ── runtime.lifecycle ─────────────────────────────────────────────

    def test_lifecycle_paths(self, tmp_path):
        """runtime.lifecycle：协议校验 + 全部权威事件归约 + 未知事件 no-op。"""
        gw = _mk_gw(tmp_path)
        _reg(gw, "r-lc", "s-lc")
        with pytest.raises(GatewayError) as ei:
            gw.runtime_lifecycle({})
        assert ei.value.code == ERR_PROTOCOL
        with pytest.raises(GatewayError) as ei:
            gw.runtime_lifecycle({"runtime_id": "r-lc", "event": "mystery"})
        assert ei.value.code == ERR_PROTOCOL
        with pytest.raises(GatewayError) as ei:
            gw.runtime_lifecycle({"runtime_id": "ghost", "event": "turn_start"})
        assert ei.value.code == ERR_NOT_FOUND
        assert gw.runtime_lifecycle({"runtime_id": "r-lc", "event": "turn_start"})["agent_state"] == AGENT_RUNNING
        assert gw.runtime_lifecycle({"runtime_id": "r-lc", "event": "turn_end"})["agent_state"] == AGENT_IDLE
        assert gw.runtime_lifecycle({"runtime_id": "r-lc", "event": "agent_start"})["agent_state"] == AGENT_RUNNING
        assert gw.runtime_lifecycle({"runtime_id": "r-lc", "event": "agent_end"})["agent_state"] == AGENT_IDLE
        assert gw.runtime_lifecycle({"runtime_id": "r-lc", "event": "session_ready"})["agent_state"] == AGENT_IDLE
        assert gw.runtime_lifecycle({"runtime_id": "r-lc", "event": "session_shutdown"})["agent_state"] == AGENT_ENDED
        assert gw.runtime_lifecycle({"runtime_id": "r-lc", "event": "registry_parked"})["agent_state"] == AGENT_ENDED
        assert gw.runtime_lifecycle({"runtime_id": "r-lc", "event": "registry_removed"})["agent_state"] == AGENT_ENDED
        assert gw.runtime_lifecycle({"runtime_id": "r-lc", "event": "process_exit"})["agent_state"] == AGENT_ENDED
        out = gw.runtime_lifecycle({"runtime_id": "r-lc", "event": "heartbeat"})
        assert out["presence"] == PRESENCE_ALIVE
        assert out["status"] == "active"
        # 未知事件经静态归约 → fail-closed 保持状态
        gw._reduce_lifecycle(gw._runtimes["r-lc"], "mystery_event")
        assert gw._runtimes["r-lc"].agent_state == AGENT_ENDED

    def test_lifecycle_event_append_failure(self, tmp_path, monkeypatch, caplog):
        """lifecycle 事件落库失败 → 告警，状态归约结果照常返回。"""
        import logging

        gw = _mk_gw(tmp_path)
        _reg(gw, "r-lc2", "s-lc2")

        def _boom(*a, **k):
            raise RuntimeError("events db full")

        monkeypatch.setattr(gw._events, "append_local", _boom)
        with caplog.at_level(logging.WARNING, logger="codeagent.gateway.service"):
            out = gw.runtime_lifecycle({"runtime_id": "r-lc2", "event": "turn_start"})
        assert "lifecycle event append failed" in caplog.text
        assert out["agent_state"] == AGENT_RUNNING

    # ── runtime.command_ack / command_status ──────────────────────────

    def test_command_ack_paths(self, tmp_path):
        """command_ack：协议/身份/代际校验 + 直接迁移 + 跳级补齐 + 终态拒绝。"""
        gw = _mk_gw(tmp_path)
        _reg(gw, "r-ack", "s-ack")
        gw.runtime_send({"runtime_id": "r-ack", "request_id": "req-a1",
                         "body": "x", "run_id": "run-1"})
        with pytest.raises(GatewayError) as ei:
            gw.runtime_command_ack({"request_id": "req-a1"})
        assert ei.value.code == ERR_PROTOCOL
        with pytest.raises(GatewayError) as ei:
            gw.runtime_command_ack({"request_id": "req-a1", "state": "BOGUS"})
        assert ei.value.code == ERR_PROTOCOL
        with pytest.raises(GatewayError) as ei:
            gw.runtime_command_ack({"request_id": "ghost", "state": "CLAIMED"})
        assert ei.value.code == ERR_NOT_FOUND
        with pytest.raises(GatewayError) as ei:
            gw.runtime_command_ack({"request_id": "req-a1", "state": "CLAIMED",
                                    "runtime_id": "other"})
        assert ei.value.code == ERR_NOT_AUTHORIZED
        with pytest.raises(GatewayError) as ei:
            gw.runtime_command_ack({"request_id": "req-a1", "state": "CLAIMED",
                                    "generation": "abc"})
        assert ei.value.code == ERR_PROTOCOL
        with pytest.raises(GatewayError) as ei:
            gw.runtime_command_ack({"request_id": "req-a1", "state": "CLAIMED",
                                    "generation": 0})
        assert ei.value.code == ERR_GENERATION_STALE
        # 直接合法迁移
        out = gw.runtime_command_ack({"request_id": "req-a1", "state": "CLAIMED"})
        assert out["status"] == "claimed"
        # 跳级：CLAIMED → TURN_TRIGGERED，补齐 REVIVING/TRIGGERING 中间态
        out = gw.runtime_command_ack({"request_id": "req-a1", "state": "TURN_TRIGGERED",
                                      "turn_id": "t-9"})
        assert out["state"] == CMD_TURN_TRIGGERED
        assert out["status"] == "turn_triggered"
        assert out["turn_id"] == "t-9"
        assert out["detail"]["advanced_through"] == [CMD_REVIVING, CMD_TRIGGERING]
        # 终态不可再迁移
        with pytest.raises(GatewayError) as ei:
            gw.runtime_command_ack({"request_id": "req-a1", "state": "CLAIMED"})
        assert ei.value.code == ERR_PROTOCOL_CONFLICT
        # 链外旁路态（FAILED_SAFE）→ 拒绝
        _reg(gw, "r-ack2", "s-ack2", runtime="native")
        with pytest.raises(GatewayError):
            gw.runtime_send({"runtime_id": "r-ack2", "request_id": "req-a2",
                             "body": "x", "run_id": "run-1"})
        with pytest.raises(GatewayError) as ei:
            gw.runtime_command_ack({"request_id": "req-a2", "state": "CLAIMED"})
        assert ei.value.code == ERR_PROTOCOL_CONFLICT

    def test_command_status_paths(self, tmp_path):
        """command_status：缺参 / 未知命令 / 查询现有命令。"""
        gw = _mk_gw(tmp_path)
        with pytest.raises(GatewayError) as ei:
            gw.runtime_command_status({})
        assert ei.value.code == ERR_PROTOCOL
        with pytest.raises(GatewayError) as ei:
            gw.runtime_command_status({"request_id": "ghost"})
        assert ei.value.code == ERR_NOT_FOUND
        _reg(gw, "r-cs", "s-cs")
        gw.runtime_send({"runtime_id": "r-cs", "request_id": "req-cs",
                         "body": "x", "run_id": "run-1"})
        out = gw.runtime_command_status({"request_id": "req-cs"})
        assert out["request_id"] == "req-cs"
        assert out["state"] == CMD_QUEUED

    # ── runtime.probe / info / event_stats ────────────────────────────

    def test_probe_paths(self, tmp_path, monkeypatch):
        """runtime.probe：registry 失败 → 回退 liveness 判定；registry 成功 → 直通。"""
        import codeagent.runtime.registry as rtmod

        gw = _mk_gw(tmp_path)
        _reg(gw, "r-pr", "s-pr")
        out = gw.runtime_probe({"runtime_id": "r-pr"})
        assert out["health"]["alive"] is True
        assert "probe unavailable" in out["health"]["reason"]

        class _FakeRT:
            def __init__(self):
                pass

            def probe(self, rid):
                return {"alive": True, "status": "ok", "detail": "up"}

        monkeypatch.setattr(rtmod, "RuntimeRegistry", _FakeRT)
        out = gw.runtime_probe({"runtime_id": "r-pr"})
        assert out["health"] == {"alive": True, "status": "ok", "detail": "up"}

        class _RaiseRT:
            def __init__(self):
                pass

            def probe(self, rid):
                raise RuntimeError("no handle")

        # 回退路径：陈旧 last_activity → alive=False
        monkeypatch.setattr(rtmod, "RuntimeRegistry", _RaiseRT)
        with gw._runtimes_lock:
            gw._runtimes["r-pr"].last_activity = time.time() - 1000
        out = gw.runtime_probe({"runtime_id": "r-pr"})
        assert out["health"]["alive"] is False

    def test_info_paths(self, tmp_path, monkeypatch):
        """runtime.info：by id / by review_key（多候选取最新）、probe 回退、
        stopped 与未知 → NOT_FOUND。"""
        import codeagent.runtime.registry as rtmod

        gw = _mk_gw(tmp_path)
        _reg(gw, "r-inf", "s-inf", rk="rk-inf")
        _reg(gw, "r-inf2", "s-inf2", rk="rk-inf")
        info = gw.runtime_info({"review_key": "rk-inf"})
        assert info["runtime_id"] in ("r-inf", "r-inf2")
        info = gw.runtime_info({"runtime_id": "r-inf"})
        assert info["status"] == "active"
        assert info["presence"] == PRESENCE_ALIVE
        assert info["idle_s"] is not None
        assert info["elapsed"] >= 0
        assert info["runtime_health"]["alive"] is True  # 回退 liveness

        class _FakeRT:
            def __init__(self):
                pass

            def probe(self, rid):
                return {"alive": True, "status": "ok"}

        monkeypatch.setattr(rtmod, "RuntimeRegistry", _FakeRT)
        info = gw.runtime_info({"runtime_id": "r-inf"})
        assert info["runtime_health"] == {"alive": True, "status": "ok"}
        # stopped 记录视为不存在（sweep 前不报旧答案）
        with gw._runtimes_lock:
            gw._runtimes["r-inf"].status = "stopped"
        with pytest.raises(GatewayError) as ei:
            gw.runtime_info({"runtime_id": "r-inf"})
        assert ei.value.code == ERR_NOT_FOUND
        with pytest.raises(GatewayError) as ei:
            gw.runtime_info({"runtime_id": "ghost"})
        assert ei.value.code == ERR_NOT_FOUND

    def test_event_stats_paths(self, tmp_path):
        """runtime.event_stats：缺 runtime_id → PROTOCOL；按代聚合。"""
        gw = _mk_gw(tmp_path)
        with pytest.raises(GatewayError) as ei:
            gw.runtime_event_stats({})
        assert ei.value.code == ERR_PROTOCOL
        _reg(gw, "r-es", "s-es")
        stats = gw.runtime_event_stats({"runtime_id": "r-es"})
        assert stats["counts"].get("RUNTIME_STATE") == 1
        stats_g = gw.runtime_event_stats({"runtime_id": "r-es", "generation": 1})
        assert stats_g["counts"].get("RUNTIME_STATE") == 1

    # ── runtime.stop / purge ──────────────────────────────────────────

    def test_stop_and_purge(self, tmp_path, caplog):
        """runtime.stop：registry 失败仍标记 stopped（dead/ended），同 review_key
        旧 stopped 记录清除；purge_stopped 清残留；缺 review_key → PROTOCOL。"""
        import logging

        gw = _mk_gw(tmp_path)
        _reg(gw, "r-old", "s-old", rk="rk-purge")
        _reg(gw, "r-new", "s-new", rk="rk-purge")
        gw.runtime_stop({"runtime_id": "r-old"})  # r-old 先停止（成为待清理记录）
        with caplog.at_level(logging.WARNING, logger="codeagent.gateway.service"):
            out = gw.runtime_stop({"runtime_id": "r-new", "reason": "done"})
        assert out["status"] == "stopped"
        assert "runtime stop failed" in caplog.text
        rec = gw._runtimes["r-new"]
        assert rec.status == "stopped"
        assert rec.presence == PRESENCE_DEAD
        assert rec.agent_state == AGENT_ENDED
        assert "r-old" not in gw._runtimes  # 同 key 旧 stopped 记录已清
        purged = gw.runtime_purge_stopped({"review_key": "rk-purge"})
        assert purged["purged"] == ["r-new"]
        assert "r-new" not in gw._runtimes
        with pytest.raises(GatewayError) as ei:
            gw.runtime_purge_stopped({})
        assert ei.value.code == ERR_PROTOCOL

    def test_purge_tolerates_generation_delete_failure(self, tmp_path, monkeypatch, caplog):
        """purge 时 ControlStore 删除失败 → 告警，内存记录仍清除。"""
        import logging

        gw = _mk_gw(tmp_path)
        _reg(gw, "r-x", "s-x", rk="rk-x")
        _reg(gw, "r-y", "s-y", rk="rk-x")
        with gw._runtimes_lock:
            gw._runtimes["r-x"].status = "stopped"

        def _boom(*a, **k):
            raise RuntimeError("db locked")

        monkeypatch.setattr(gw._control, "delete_generation", _boom)
        with caplog.at_level(logging.WARNING, logger="codeagent.gateway.service"):
            purged = gw.runtime_purge_stopped({"review_key": "rk-x"})
        assert purged["purged"] == ["r-x"]
        assert "control generation delete failed" in caplog.text
        assert "r-x" not in gw._runtimes
        assert "r-y" in gw._runtimes  # 未停止记录不受影响

    # ── message ───────────────────────────────────────────────────────

    def test_message_send_failure_and_release(self, tmp_path, monkeypatch):
        """message.send 失败 receipt → NOT_FOUND；read → release 回退 inbox。"""
        gw = _mk_gw(tmp_path)
        gw.session_ensure({"session_id": "s-msg", "manager_id": "manager", "roster": ["worker"]})
        real_send = gw._svc.send
        monkeypatch.setattr(gw._svc, "send",
                            lambda **k: SimpleNamespace(status="failed",
                                                        error="no route", msg_id=""))
        with pytest.raises(GatewayError) as ei:
            gw.message_send({"session_id": "s-msg", "from": "manager",
                             "to": "worker", "body": "x"})
        assert ei.value.code == ERR_NOT_FOUND
        monkeypatch.setattr(gw._svc, "send", real_send)
        sent = gw.message_send({"session_id": "s-msg", "from": "manager", "to": "worker",
                                "subject": "t", "body": "x", "kind": "TASK",
                                "run_id": "r1", "request_id": "req-msg"})
        assert sent["status"] == "delivered"
        read = gw.message_read({"session_id": "s-msg", "agent": "worker",
                                "owner": "worker", "msg_id": sent["msg_id"]})
        assert read["status"] == "ok"
        rel = gw.message_release({"session_id": "s-msg", "agent": "worker",
                                  "msg_id": sent["msg_id"], "owner": "worker"})
        assert "released" in rel["status"]

    # ── artifact.verify ───────────────────────────────────────────────

    def test_artifact_verify_paths(self, tmp_path):
        """artifact.verify：参数校验、agent 自动定位、hash/size 校验、幂等 EXISTS、
        not-a-file 传播。"""
        gw = _mk_gw(tmp_path)
        gw.session_ensure({"session_id": "s-art", "manager_id": "manager", "roster": ["a1"]})
        with pytest.raises(GatewayError) as ei:
            gw.artifact_verify({"session_id": "s-art", "request_id": "r"})
        assert ei.value.code == ERR_PROTOCOL
        artifact = tmp_path / "artifact.txt"
        artifact.write_bytes(b"hello world")
        digest = hashlib.sha256(b"hello world").hexdigest()
        out = gw.artifact_verify({"session_id": "s-art", "request_id": "req-1",
                                  "run_id": "run-1", "path": str(artifact),
                                  "sha256": digest, "size": 11, "agent_id": "a1"})
        assert out["verified"] is True
        assert out["terminal"] == "DONE"
        # 幂等：终态已存在 → EXISTS
        out2 = gw.artifact_verify({"session_id": "s-art", "request_id": "req-1",
                                   "run_id": "run-1", "path": str(artifact),
                                   "sha256": digest, "size": 11, "agent_id": "a1"})
        assert out2["status"] == "EXISTS"
        # size 不匹配 → BLOCKED（记入 ledger）
        out3 = gw.artifact_verify({"session_id": "s-art", "request_id": "req-2",
                                   "run_id": "run-2", "path": str(artifact),
                                   "sha256": digest, "size": 999, "agent_id": "a1"})
        assert out3["verified"] is False
        assert out3["terminal"] == "BLOCKED"
        # not a file → ValueError 传播（不记 BLOCKED）
        with pytest.raises(ValueError, match="not a file"):
            gw.artifact_verify({"session_id": "s-art", "request_id": "req-3",
                                "run_id": "run-3", "path": str(tmp_path / "missing.txt"),
                                "sha256": digest, "size": 11, "agent_id": "a1"})
        # 省略 agent_id → 扫描 events 目录定位
        evdir = gw._store.session_dir("s-art") / "a2" / "events" / "req-9"
        evdir.mkdir(parents=True)
        out4 = gw.artifact_verify({"session_id": "s-art", "request_id": "req-9",
                                   "run_id": "run-9", "path": str(artifact),
                                   "sha256": digest, "size": 11})
        assert out4["verified"] is True
        # 定位不到 agent → NOT_FOUND
        with pytest.raises(GatewayError) as ei:
            gw.artifact_verify({"session_id": "s-art", "request_id": "req-42",
                                "run_id": "run-42", "path": str(artifact),
                                "sha256": digest, "size": 11})
        assert ei.value.code == ERR_NOT_FOUND

    # ── park bridge ───────────────────────────────────────────────────

    def test_park_bridge(self, tmp_path, monkeypatch):
        """park.revive / park.release：委托 park router / registry。"""
        import codeagent.park.registry as parkmod
        import codeagent.park.router as router

        monkeypatch.setattr(router, "park_revive",
                            lambda key, prompt: SimpleNamespace(method="hot", success=True,
                                                                context={"key": key}))
        gw = _mk_gw(tmp_path)
        out = gw.park_revive({"review_key": "rk-br", "prompt": "go"})
        assert out == {"method": "hot", "success": True, "context": {"key": "rk-br"}}
        fake = _FakeParkRegistry()
        monkeypatch.setattr(parkmod, "ParkRegistry", lambda: fake)
        out = gw.park_release({"review_key": "rk-br"})
        assert out == {"released": "rk-br"}
        assert fake.released == ["rk-br"]

    # ── hub 跨设备路由 ────────────────────────────────────────────────

    def test_hub_register_status_send_unregister(self, tmp_path):
        """hub：注册/状态（单个+全量）/send（真实 kernel 投递）/unregister 全链路。"""
        gw = _mk_gw(tmp_path)
        with pytest.raises(GatewayError) as ei:
            gw.hub_register({"peer_id": "p"})
        assert ei.value.code == "PROTOCOL"
        out = gw.hub_register({"peer_id": "p1", "session_id": "s-hub",
                               "agent_id": "worker", "host_alias": "host-a",
                               "mailbox_root": "/m"})
        assert out["status"] == "online"
        gw.hub_register({"peer_id": "p2", "session_id": "s-hub2",
                         "agent_id": "worker2", "host_alias": "host-b"})
        st = gw.hub_status({"peer_id": "p1"})
        assert st["host_alias"] == "host-a"
        peers = gw.hub_status({})["peers"]
        assert {p["peer_id"] for p in peers} == {"p1", "p2"}
        with pytest.raises(GatewayError) as ei:
            gw.hub_status({"peer_id": "ghost"})
        assert ei.value.code == ERR_NOT_FOUND
        # hub send：经 kernel direct → outbox（status accepted）
        out = gw.hub_send({"peer_id": "p1", "from": "hub", "content": "do it",
                           "subject": "s", "kind": "TASK", "run_id": "run-h"})
        assert out["msg_id"]
        assert out["status"] == "accepted"
        with pytest.raises(GatewayError) as ei:
            gw.hub_send({"peer_id": "ghost", "content": "x"})
        assert ei.value.code == ERR_NOT_FOUND
        with gw._peers_lock:
            gw._peers["p1"].status = "offline"
        with pytest.raises(GatewayError) as ei:
            gw.hub_send({"peer_id": "p1", "content": "x"})
        assert ei.value.code == ERR_NOT_FOUND
        with gw._peers_lock:
            gw._peers["p1"].status = "online"
        out = gw.hub_unregister({"peer_id": "p1"})
        assert out["unregistered"] is True
        with pytest.raises(GatewayError) as ei:
            gw.hub_unregister({"peer_id": "ghost"})
        assert ei.value.code == ERR_NOT_FOUND
        # peers 持久化 → 新 gateway 恢复 p2
        gw2 = _mk_gw(tmp_path)
        assert "p2" in gw2._peers
        assert gw2._peers["p2"].host_alias == "host-b"
        gw2.stop()

    def test_hub_routing_failures_tolerated(self, tmp_path, monkeypatch, caplog):
        """hub：kernel 路由注册/注销失败 → 告警但 peer 操作成功。"""
        import logging

        gw = _mk_gw(tmp_path)
        gw.session_ensure({"session_id": "s-hf", "manager_id": "manager", "roster": ["worker"]})
        gw._kernel.create_session("s-hf", "manager", ["worker", "manager"])
        with caplog.at_level(logging.WARNING, logger="codeagent.gateway.service"):
            out = gw.hub_register({"peer_id": "p-f", "session_id": "s-hf",
                                   "agent_id": "ghost-agent", "host_alias": "h"})
        assert "routing registration failed" in caplog.text
        assert out["status"] == "online"  # peer 记录仍生效

        def _boom(session_id, agent_id):
            raise RuntimeError("kernel gone")

        monkeypatch.setattr(gw._kernel, "unregister", _boom)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="codeagent.gateway.service"):
            out = gw.hub_unregister({"peer_id": "p-f"})
        assert out["unregistered"] is True
        assert "kernel unregister failed" in caplog.text

    def test_hub_send_kernel_error(self, tmp_path, monkeypatch):
        """hub.send：kernel.direct 抛 ValueError/PermissionError → NOT_FOUND。"""
        gw = _mk_gw(tmp_path)
        gw.hub_register({"peer_id": "p1", "session_id": "s-h", "agent_id": "a1",
                         "host_alias": "h"})

        def _boom(*a, **k):
            raise ValueError("no session")

        monkeypatch.setattr(gw._kernel, "direct", _boom)
        with pytest.raises(GatewayError) as ei:
            gw.hub_send({"peer_id": "p1", "content": "x"})
        assert ei.value.code == ERR_NOT_FOUND

    def test_peer_merge_persist_failures(self, tmp_path, monkeypatch, caplog):
        """peers/merges 落盘失败（目录不可写）→ 告警，内存操作不受影响。"""
        import logging

        gw = _mk_gw(tmp_path)
        gw._peers_file = Path("/dev/null/peers.json")  # mkdir 必然失败
        with caplog.at_level(logging.WARNING, logger="codeagent.gateway.service"):
            gw.hub_register({"peer_id": "p1", "session_id": "s", "agent_id": "a",
                             "host_alias": "h"})
        assert "hub peers persist failed" in caplog.text
        caplog.clear()
        gw.write_merge({"session_id": "s", "target_path": "t", "artifact_sha256": "h1"})
        assert "merges persist failed" in caplog.text
        assert gw._merges == {("s", "t"): "h1"}

    def test_peer_merge_restore_corrupt(self, tmp_path):
        """损坏的 peers.json / merges.json → 恢复跳过并告警（不崩溃）。"""
        (tmp_path / "peers.json").write_text("{corrupt")
        (tmp_path / "merges.json").write_text("{corrupt")
        gw = _mk_gw(tmp_path)
        assert gw._peers == {}
        assert gw._merges == {}
        gw.stop()

    # ── session.claim / release ───────────────────────────────────────

    def test_claim_release_paths(self, tmp_path):
        """session.claim：独占冲突 / TTL 边界 / 过期接管；release：无 claim /
        owner 不符 / 正常释放。"""
        gw = _mk_gw(tmp_path)
        with pytest.raises(GatewayError) as ei:
            gw.session_claim({"session_id": "", "agent_id": "", "owner": ""})
        assert ei.value.code == "PROTOCOL"
        with pytest.raises(GatewayError) as ei:
            gw.session_claim({"session_id": "s-c", "agent_id": "worker",
                              "owner": "rt1", "ttl": 0})
        assert ei.value.code == ERR_PROTOCOL
        with pytest.raises(GatewayError) as ei:
            gw.session_claim({"session_id": "s-c", "agent_id": "worker",
                              "owner": "rt1", "ttl": 999999})
        assert ei.value.code == ERR_PROTOCOL
        c = gw.session_claim({"session_id": "s-c", "agent_id": "worker", "owner": "rt1"})
        assert c["expires_at"] > time.time()
        # 同 owner 幂等续期
        c2 = gw.session_claim({"session_id": "s-c", "agent_id": "worker", "owner": "rt1"})
        assert c2["expires_at"] >= c["expires_at"]
        with pytest.raises(GatewayError) as ei:
            gw.session_claim({"session_id": "s-c", "agent_id": "worker", "owner": "rt2"})
        assert ei.value.code == ERR_PROTOCOL_CONFLICT
        # 过期 claim → 新 owner 接管
        with gw._runtimes_lock:
            gw._claims["s-c:worker"]["expires_at"] = time.time() - 1
        c3 = gw.session_claim({"session_id": "s-c", "agent_id": "worker", "owner": "rt2"})
        assert c3["owner"] == "rt2"
        with pytest.raises(GatewayError) as ei:
            gw.session_release({"session_id": "s-c", "agent_id": "worker", "owner": "rt3"})
        assert ei.value.code == ERR_NOT_AUTHORIZED
        out = gw.session_release({"session_id": "s-c", "agent_id": "worker", "owner": "rt2"})
        assert out["released"] is True
        out = gw.session_release({"session_id": "s-c", "agent_id": "worker"})
        assert out["released"] is False

    # ── write merge ───────────────────────────────────────────────────

    def test_merge_paths(self, tmp_path):
        """write.merge：参数校验 / 冲突拒绝 / 记录落盘；merge_reset 单条与全量。"""
        gw = _mk_gw(tmp_path)
        assert AgentGateway.write_parse_body(
            '{"target_path": "a.txt", "base_revision": "r1", "artifact_id": "art"}'
        ) == {"target_path": "a.txt", "base_revision": "r1", "artifact_id": "art"}
        assert AgentGateway.write_parse_body("not json") == {}
        assert AgentGateway.write_parse_body("[1,2]") == {}
        assert AgentGateway.write_parse_body('{"target_path": 5}') == {}
        assert AgentGateway.write_parse_body("") == {}
        with pytest.raises(GatewayError) as ei:
            gw.write_merge({"session_id": "s-m", "target_path": "a.txt"})
        assert ei.value.code == ERR_NOT_FOUND
        assert gw.write_merge({"session_id": "s-m", "target_path": "a.txt",
                               "artifact_sha256": "h1"}) == {"merged": True}
        with pytest.raises(GatewayError) as ei:
            gw.write_merge({"session_id": "s-m", "target_path": "a.txt",
                            "artifact_sha256": "h2"})
        assert ei.value.code == ERR_PROTOCOL_CONFLICT
        # 同 sha256 重复合并 → 幂等
        assert gw.write_merge({"session_id": "s-m", "target_path": "a.txt",
                               "artifact_sha256": "h1"}) == {"merged": True}
        gw.write_merge({"session_id": "s-m", "target_path": "b.txt", "artifact_sha256": "h3"})
        gw.write_merge({"session_id": "s-m", "target_path": "c.txt", "artifact_sha256": "h4"})
        assert gw.merge_reset({"session_id": "s-m", "target_path": "a.txt"}) == {"reset": 1}
        assert gw.merge_reset({"session_id": "s-m"}) == {"reset": 2}
        assert gw.merge_reset({"session_id": "s-m"}) == {"reset": 0}
        with pytest.raises(GatewayError) as ei:
            gw.merge_reset({})
        assert ei.value.code == ERR_NOT_FOUND

    def test_merge_persistence_restore(self, tmp_path):
        """merges 持久化 → 新 gateway 恢复（冲突检测跨重启存活）。"""
        gw = _mk_gw(tmp_path)
        gw.write_merge({"session_id": "s-m", "target_path": "a.txt", "artifact_sha256": "h1"})
        gw.write_merge({"session_id": "s-m", "target_path": "b.txt", "artifact_sha256": "h2"})
        gw2 = _mk_gw(tmp_path)  # 同 peers.json → merges.json 兄弟文件
        assert gw2._merges == {("s-m", "a.txt"): "h1", ("s-m", "b.txt"): "h2"}
        gw2.stop()

    # ── 并发 ──────────────────────────────────────────────────────────

    def test_concurrent_send_same_request_id_single_command(self, tmp_path):
        """并发同 request_id send → 只入队一条命令（幂等键防重复注入）。"""
        gw = _mk_gw(tmp_path)
        _reg(gw, "r-con", "s-con")
        errors: list = []
        results: list = []

        def _send(i):
            try:
                out = gw.runtime_send({"runtime_id": "r-con", "request_id": "req-con",
                                       "body": "x", "run_id": "run-1", "from": f"m{i}"})
                results.append(out["command_id"])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_send, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert len(set(results)) == 1
        assert len(gw._control.list_commands(runtime_id="r-con")) == 1

    def test_concurrent_register_heartbeat_lifecycle(self, tmp_path, monkeypatch):
        """并发 register/heartbeat/lifecycle 无异常、无状态撕裂（锁保护）。"""
        import codeagent.park.registry as parkmod

        monkeypatch.setattr(parkmod, "ParkRegistry", lambda: _FakeParkRegistry())
        gw = _mk_gw(tmp_path)
        gw.session_ensure({"session_id": "s-con2", "manager_id": "manager", "roster": ["worker"]})
        errors: list = []

        def _worker():
            try:
                for _ in range(15):
                    gw.runtime_register({"session_id": "s-con2", "agent_id": "worker",
                                         "runtime_id": "r-c2", "generation": 1,
                                         "owner_pid": 1, "nonce": "n"})
                    gw.runtime_heartbeat({"runtime_id": "r-c2"})
                    gw.runtime_lifecycle({"runtime_id": "r-c2", "event": "turn_start"})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        rec = gw._runtimes["r-c2"]
        assert rec.status == "active"
        assert rec.presence == PRESENCE_ALIVE
        assert rec.agent_state == AGENT_RUNNING

    # ── 补充分支：park restore 跳过 / sweep 冷启动 / retention 日志 / _is_hot 降级 ──

    def test_restore_park_skip_branches(self, tmp_path, monkeypatch):
        """park restore：缺 session/backend 跳过、短 key 派生、重复 runtime_id 跳过、
        model_context 读取失败容错。"""
        import codeagent.park.registry as parkmod
        import codeagent.gateway.service as svc

        slug_long = "rk-" + "z" * 26
        rid_long = "park-" + slug_long[-24:]
        manifest_no_sid = SimpleNamespace(
            review_key="rk-" + "a" * 26, swarm_session_id="", backend_session_id="bs",
            mailbox_agent_id="oracle", round=1, host="h", agent_type="omp",
            created_at=time.time(), lifecycle=Lifecycle.HOT_PARKED,
        )
        manifest_short = SimpleNamespace(
            review_key="short", swarm_session_id="s-short", backend_session_id="bs-short",
            mailbox_agent_id="oracle", round=0, host="h", agent_type="omp",
            created_at=time.time(), lifecycle=Lifecycle.HOT_PARKED,
        )
        manifest_dup = SimpleNamespace(
            review_key=slug_long, swarm_session_id="s-dup", backend_session_id="bs-dup",
            mailbox_agent_id="oracle", round=2, host="h", agent_type="omp",
            created_at=time.time(), lifecycle=Lifecycle.HOT_PARKED,
        )
        manifest_dup2 = SimpleNamespace(
            review_key=slug_long, swarm_session_id="s-dup2", backend_session_id="bs-dup2",
            mailbox_agent_id="oracle", round=3, host="h", agent_type="omp",
            created_at=time.time(), lifecycle=Lifecycle.HOT_PARKED,
        )
        fake = _FakeParkRegistry({
            "skip": manifest_no_sid, "short": manifest_short,
            "dup": manifest_dup, "dup2": manifest_dup2,
        })

        # model_context 读取抛异常 → debug 日志跳过（395-396）
        orig_control = svc.ControlStore

        class _FlakyGenStore(orig_control):
            def get_generation(self, runtime_id):
                if runtime_id.startswith("park-"):
                    raise RuntimeError("corrupt generation row")
                return super().get_generation(runtime_id)

        monkeypatch.setattr(svc, "ControlStore", _FlakyGenStore)
        monkeypatch.setattr(parkmod, "ParkRegistry", lambda: fake)
        gw = AgentGateway(
            store=MailboxStore(root=tmp_path / "mb-r"),
            events=EventStore(db_path=tmp_path / "e-r.sqlite3", source_host="h"),
            restore_from_park=True, peers_file=tmp_path / "peers-r.json",
        )
        try:
            assert rid_long in gw._runtimes
            assert gw._runtimes[rid_long].session_id == "s-dup"  # dup2 同 key 被跳过
            short_ids = [rid for rid in gw._runtimes if rid.startswith("park-short-")]
            assert len(short_ids) == 1                    # 短 key 走 token_hex 派生
            assert gw._runtimes[short_ids[0]].session_id == "s-short"
        finally:
            gw.stop()

    def test_sweep_cold_start_grace(self, tmp_path):
        """冷启动宽限：created_at < 3min 的 runtime 用双倍超时判 offline。"""
        from datetime import datetime, timedelta, timezone

        gw = _mk_gw(tmp_path)
        _reg(gw, "r-cold", "s-cold", rk="rk-cold")
        with gw._runtimes_lock:
            rec = gw._runtimes["r-cold"]
            rec.created_at = (datetime.now(timezone.utc) - timedelta(seconds=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
            rec.last_activity = time.time() - 1000  # 超过双倍超时（600s）→ 仍 offline
        offline = gw._sweep_once()
        assert "r-cold" in offline
        assert gw._runtimes["r-cold"].status == "offline"

    def test_retention_sweep_removes_rows(self, tmp_path, monkeypatch, caplog):
        """retention sweep：清旧 TOOL_* 行 + outbox 清理，均有日志；失败 → 告警。"""
        import datetime as dt
        import logging

        gw = _mk_gw(tmp_path)
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
        gw._events.append_local(RuntimeEventDraft(
            runtime_id="r-ret", generation=1, session_id="s-ret", agent_id="a",
            request_id="", run_id="", kind="TOOL_UPDATED", created_at=old,
            payload={"detail": "old"},
        ))
        monkeypatch.setattr(gw._engine, "sweep", lambda retention_days=7: 3)
        with caplog.at_level(logging.INFO, logger="codeagent.gateway.service"):
            gw._retention_sweep()
        assert "events retention sweep removed 1 row" in caplog.text
        assert "outbox retention sweep removed 3 entry" in caplog.text

        # events.sweep 失败 → 告警
        def _boom_sweep(*a, **k):
            raise RuntimeError("events db corrupt")

        monkeypatch.setattr(gw._events, "sweep", _boom_sweep)
        gw._last_retention_sweep = 0
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="codeagent.gateway.service"):
            gw._retention_sweep()
        assert "events retention sweep failed" in caplog.text

        # engine.sweep 失败 → 告警（恢复 events.sweep）
        monkeypatch.setattr(gw._events, "sweep", lambda: 0)
        monkeypatch.setattr(gw._engine, "sweep",
                            lambda retention_days=7: (_ for _ in ()).throw(RuntimeError("outbox db corrupt")))
        gw._last_retention_sweep = 0
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="codeagent.gateway.service"):
            gw._retention_sweep()
        assert "outbox retention sweep failed" in caplog.text

    def test_declare_spawn_review_mirror_failure(self, tmp_path, monkeypatch, caplog):
        """declare/spawn 的 review 镜像 upsert 失败 → 告警，主流程成功。"""
        import logging

        import codeagent.park.registry as parkmod
        import codeagent.runtime.registry as rtmod

        m = ParkManifest(review_key="rk-dm", swarm_session_id="s-dm")
        monkeypatch.setattr(parkmod, "ParkRegistry", lambda: _FakeParkRegistry({"rk-dm": m}))

        class _FakeRT:
            def __init__(self):
                pass

            def spawn(self, name, request):
                return SimpleNamespace(runtime_id="sp-m", generation=1,
                                       backend_session_id="bs", host_alias="h",
                                       mode="", capabilities=[])

            def names(self):
                return ["omp"]

            def probe(self, rid):
                raise RuntimeError("no handle")

            def stop(self, rid, reason):
                raise RuntimeError("no handle")

        monkeypatch.setattr(rtmod, "RuntimeRegistry", _FakeRT)
        gw = _mk_gw(tmp_path)

        def _boom(*a, **k):
            raise RuntimeError("review db locked")

        monkeypatch.setattr(gw._control, "upsert_review", _boom)
        with caplog.at_level(logging.WARNING, logger="codeagent.gateway.service"):
            gw.runtime_declare({"review_key": "rk-dm", "backend_session_id": "bs-dm"})
            gw.runtime_spawn({"runtime": "omp", "session_id": "s-sp-m",
                              "agent_id": "worker", "review_key": "rk-sp-m"})
        assert caplog.text.count("review mirror failed") == 2
        assert "sp-m" in gw._runtimes

    def test_heartbeat_append_and_renew_failures(self, tmp_path, monkeypatch, caplog):
        """heartbeat 恢复：事件落库失败 / park renew 失败 → 告警，状态照常恢复。"""
        import logging

        import codeagent.park.registry as parkmod
        import codeagent.runtime.registry as rtmod

        class _FakeRT:
            def __init__(self):
                pass

            def spawn(self, name, request):
                return SimpleNamespace(runtime_id="sp-hb", generation=1,
                                       backend_session_id="bs", host_alias="h",
                                       mode="", capabilities=[])

            def names(self):
                return ["omp"]

            def probe(self, rid):
                raise RuntimeError("no handle")

            def stop(self, rid, reason):
                raise RuntimeError("no handle")

        monkeypatch.setattr(rtmod, "RuntimeRegistry", _FakeRT)
        fake_park = _FakeParkRegistry()
        monkeypatch.setattr(parkmod, "ParkRegistry", lambda: fake_park)
        gw = _mk_gw(tmp_path)
        gw.runtime_spawn({"runtime": "omp", "session_id": "s-hb", "agent_id": "worker",
                          "review_key": "rk-hb"})
        with gw._runtimes_lock:
            rec = gw._runtimes["sp-hb"]
            rec.status = "offline"
            rec.presence = PRESENCE_STALE

        def _boom(*a, **k):
            raise RuntimeError("events db full")

        monkeypatch.setattr(gw._events, "append_local", _boom)
        with caplog.at_level(logging.WARNING, logger="codeagent.gateway.service"):
            hb = gw.runtime_heartbeat({"runtime_id": "sp-hb"})
        assert hb["status"] == "active"
        assert "restore event append failed" in caplog.text

        # park renew 失败
        caplog.clear()
        with gw._runtimes_lock:
            rec.status = "offline"
            rec.presence = PRESENCE_STALE

        def _renew_boom(key):
            raise RuntimeError("park db locked")

        monkeypatch.setattr(fake_park, "renew", _renew_boom)
        with caplog.at_level(logging.WARNING, logger="codeagent.gateway.service"):
            hb = gw.runtime_heartbeat({"runtime_id": "sp-hb"})
        assert hb["status"] == "active"
        assert "park renew failed" in caplog.text

    def test_event_turn_triggered_command_advance(self, tmp_path, monkeypatch, caplog):
        """runtime.event TURN_TRIGGERED 双通道：推进命令状态机（跳级补齐）、未知命令
        告警、终态跳过、非法迁移告警、推进异常告警。"""
        import logging

        import codeagent.gateway.model as modelmod

        # TURN_TRIGGERED 是双通道冗余的预留 kind：注入合法集以走通推进分支。
        monkeypatch.setattr(
            modelmod, "EVENT_KINDS",
            frozenset(set(modelmod.EVENT_KINDS) | {"TURN_TRIGGERED"}),
        )
        gw = _mk_gw(tmp_path)
        _reg(gw, "r-tt", "s-tt")
        gw.runtime_send({"runtime_id": "r-tt", "request_id": "req-tt",
                         "body": "x", "run_id": "run-1"})
        # QUEUED → TURN_TRIGGERED（补齐 CLAIMED/REVIVING/TRIGGERING）
        gw.runtime_event({"event": {"runtime_id": "r-tt", "generation": 1,
                                    "kind": "TURN_TRIGGERED", "session_id": "s-tt",
                                    "agent_id": "worker",
                                    "request_id": "req-tt",
                                    "payload": {"command_id": "req-tt",
                                                "turn_id": "turn-x"}}})
        row = gw._control.get_command("req-tt")
        assert row["state"] == CMD_TURN_TRIGGERED
        assert row["turn_id"] == "turn-x"
        assert row["detail"]["advanced_through"] == [CMD_CLAIMED, CMD_REVIVING, CMD_TRIGGERING]
        assert row["detail"]["via"] == "runtime_event"
        # 已终态 → 跳过（无告警）
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="codeagent.gateway.service"):
            gw.runtime_event({"event": {"runtime_id": "r-tt", "generation": 1,
                                        "kind": "TURN_TRIGGERED", "session_id": "s-tt",
                                        "agent_id": "worker",
                                        "payload": {"command_id": "req-tt"}}})
        assert caplog.text == ""
        # 未知命令 → 告警
        with caplog.at_level(logging.WARNING, logger="codeagent.gateway.service"):
            gw.runtime_event({"event": {"runtime_id": "r-tt", "generation": 1,
                                        "kind": "TURN_TRIGGERED", "session_id": "s-tt",
                                        "agent_id": "worker",
                                        "payload": {"command_id": "ghost-cmd"}}})
        assert "unknown command ghost-cmd" in caplog.text
        # 终态旁路（FAILED_SAFE）无法推进 → 告警
        _reg(gw, "r-tt2", "s-tt2", runtime="native")
        with pytest.raises(GatewayError):
            gw.runtime_send({"runtime_id": "r-tt2", "request_id": "req-tt2",
                             "body": "x", "run_id": "run-1"})
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="codeagent.gateway.service"):
            gw.runtime_event({"event": {"runtime_id": "r-tt2", "generation": 1,
                                        "kind": "TURN_TRIGGERED", "session_id": "s-tt2",
                                        "agent_id": "worker",
                                        "payload": {"command_id": "req-tt2"}}})
        assert "cannot advance" in caplog.text
        # 推进时 update_command 异常 → 告警
        gw.runtime_send({"runtime_id": "r-tt", "request_id": "req-tt3",
                         "body": "x", "run_id": "run-1"})

        def _boom(*a, **k):
            raise RuntimeError("control db full")

        monkeypatch.setattr(gw._control, "update_command", _boom)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="codeagent.gateway.service"):
            gw.runtime_event({"event": {"runtime_id": "r-tt", "generation": 1,
                                        "kind": "TURN_TRIGGERED", "session_id": "s-tt",
                                        "agent_id": "worker",
                                        "payload": {"command_id": "req-tt3"}}})
        assert "advance failed" in caplog.text

    def test_purge_skips_other_keys(self, tmp_path):
        """purge_stopped 只清理指定 review_key（其他 key 记录不受影响）。"""
        gw = _mk_gw(tmp_path)
        _reg(gw, "r-x", "s-x", rk="rk-x")
        _reg(gw, "r-other", "s-other", rk="rk-other")
        with gw._runtimes_lock:
            gw._runtimes["r-x"].status = "stopped"
        purged = gw.runtime_purge_stopped({"review_key": "rk-x"})
        assert purged["purged"] == ["r-x"]
        assert "r-other" in gw._runtimes  # 不同 key 的 stopped/active 记录跳过

    def test_hub_register_merge_paths(self, tmp_path, monkeypatch, caplog):
        """hub_register：kernel 会话并发创建 ValueError 容忍、session 合并失败告警、
        manager 并入 in-memory roster。"""
        import logging

        gw = _mk_gw(tmp_path)
        gw.session_ensure({"session_id": "s-mrg", "manager_id": "manager",
                           "roster": ["worker"]})
        gw._kernel.create_session("s-mrg", "manager", ["worker"])

        def _merge_boom(*a, **k):
            raise OSError("session.json write failed")

        monkeypatch.setattr(gw._store, "session_init", _merge_boom)
        with caplog.at_level(logging.WARNING, logger="codeagent.gateway.service"):
            out = gw.hub_register({"peer_id": "p-mrg", "session_id": "s-mrg",
                                   "agent_id": "worker", "host_alias": "h-m"})
        assert out["status"] == "online"
        assert "session manifest merge failed" in caplog.text
        # manager 已并入 in-memory roster
        session = gw._kernel.get_session("s-mrg")
        assert "manager" in session.roster.members

        # kernel.create_session ValueError（并发创建）→ 容忍继续
        def _create_boom(session_id, authority, members):
            raise ValueError("concurrent creation")

        monkeypatch.setattr(gw._kernel, "create_session", _create_boom)
        caplog.clear()
        out = gw.hub_register({"peer_id": "p-cr", "session_id": "s-cr",
                               "agent_id": "worker", "host_alias": "h-c"})
        assert out["peer_id"] == "p-cr"

        # 存量 kernel 会话 roster 无 manager → 就地并入（1921 分支）
        class _FakeRoster:
            def __init__(self, members):
                self.members = members

            def __contains__(self, item):
                return item in self.members

        fake_session = SimpleNamespace(
            roster=_FakeRoster(["worker"]),
            acl=SimpleNamespace(allowed_senders=[], room_members=[]),
        )
        monkeypatch.setattr(gw._kernel, "get_session", lambda sid: fake_session)
        out = gw.hub_register({"peer_id": "p-m2", "session_id": "s-m2",
                               "agent_id": "worker", "host_alias": "h-m2"})
        assert out["peer_id"] == "p-m2"
        assert "manager" in fake_session.roster.members

    def test_is_hot_degrades(self, tmp_path, caplog):
        """_is_hot：非 omp backend / 未上报能力 / 缺必需能力 → 降级；全能力 → hot。"""
        import logging

        gw = _mk_gw(tmp_path)
        _reg(gw, "r-hot", "s-hot")
        with gw._runtimes_lock:
            rec = gw._runtimes["r-hot"]
            rec.runtime = "native"          # 非 omp → 降级
        with caplog.at_level(logging.WARNING, logger="codeagent.gateway.service"):
            assert gw._is_hot(rec) is False
        assert "无插件 consumer" in caplog.text
        caplog.clear()
        with gw._runtimes_lock:
            rec.runtime = "omp"
            rec.capabilities = []           # 未上报 → 兼容放行
        with caplog.at_level(logging.WARNING, logger="codeagent.gateway.service"):
            assert gw._is_hot(rec) is True
        assert "按兼容放行" in caplog.text
        caplog.clear()
        with gw._runtimes_lock:
            rec.capabilities = ["park_revive_v1"]  # 缺 correlated_turn_ack → 降级
        with caplog.at_level(logging.WARNING, logger="codeagent.gateway.service"):
            assert gw._is_hot(rec) is False
        assert "缺 hot 必需能力" in caplog.text
        caplog.clear()
        with gw._runtimes_lock:
            rec.capabilities = ["park_revive_v1", "correlated_turn_ack"]
            rec.presence = PRESENCE_STALE   # presence 非 alive → 不 hot
        assert gw._is_hot(rec) is False
        with gw._runtimes_lock:
            rec.presence = PRESENCE_ALIVE
            rec.binding = BINDING_PENDING   # binding 未建立 → 不 hot
        assert gw._is_hot(rec) is False
        with gw._runtimes_lock:
            rec.binding = BINDING_BOUND
        assert gw._is_hot(rec) is True      # 全能力 + 三维齐 → hot

    def test_persist_control_failure_tolerated(self, tmp_path, monkeypatch, caplog):
        """_persist_control_state 落盘失败 → 告警，内存记录仍是操作权威。"""
        import logging

        gw = _mk_gw(tmp_path)

        def _boom(*a, **k):
            raise RuntimeError("control db full")

        monkeypatch.setattr(gw._control, "upsert_generation", _boom)
        with caplog.at_level(logging.WARNING, logger="codeagent.gateway.service"):
            _reg(gw, "r-pc", "s-pc")
        assert "control state persist failed" in caplog.text
        assert gw._runtimes["r-pc"].status == "active"

    def test_heartbeat_runtime_disappears_race(self, tmp_path):
        """heartbeat 竞态：锁内重查发现记录已消失 → NOT_FOUND（fail-closed）。"""
        gw = _mk_gw(tmp_path)
        _reg(gw, "r-die", "s-die", rk="rk-die")

        class _Disappearing(dict):
            """第二次 get(r-die) 返回 None——模拟并发 runtime 记录被替换后消失。"""

            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self._armed = False

            def get(self, key, *a, **k):
                if key == "r-die":
                    if not self._armed:
                        self._armed = True
                        return super().get(key, *a, **k)
                    return None
                return super().get(key, *a, **k)

        with gw._runtimes_lock:
            gw._runtimes = _Disappearing(gw._runtimes)
        with pytest.raises(GatewayError) as ei:
            gw.runtime_heartbeat({"runtime_id": "r-die"})
        assert ei.value.code == ERR_NOT_FOUND
        assert "disappeared" in ei.value.message
