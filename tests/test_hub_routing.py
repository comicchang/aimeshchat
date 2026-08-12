"""P8.1 hub routing tests — peer register/send/status/unregister + presence sync."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codeagent.gateway.events import EventStore
from codeagent.gateway.model import GatewayError
from codeagent.gateway.service import AgentGateway
from codeagent.mailbox.store import MailboxStore


@pytest.fixture
def gw(tmp_path: Path) -> AgentGateway:
    import uuid

    base = Path("/tmp") / f"gwhub-{uuid.uuid4().hex[:8]}"
    store = MailboxStore(root=base / "mailbox")
    store.root.mkdir(parents=True, exist_ok=True)
    events = EventStore(db_path=base / "e.sqlite3", source_host="h")
    gateway = AgentGateway(
        store=store, events=events, restore_from_park=False,
        peers_file=base / "peers.json",
    )
    gateway._offline_timeout = 120.0  # explicit (A4: default relaxed to 300s)
    yield gateway


def _register_peer(gw: AgentGateway, peer_id: str = "remote-dev",
                   session_id: str = "s1", agent_id: str = "w1",
                   host_alias: str = "remote-dev") -> None:
    gw.hub_register({
        "peer_id": peer_id,
        "session_id": session_id,
        "agent_id": agent_id,
        "host_alias": host_alias,
    })


class TestHubRegister:
    def test_register_creates_peer(self, gw: AgentGateway):
        result = gw.hub_register({
            "peer_id": "remote-dev", "session_id": "s1",
            "agent_id": "w1", "host_alias": "remote-dev",
        })
        assert result["status"] == "online"
        assert gw._peers["remote-dev"].agent_id == "w1"

    def test_register_requires_fields(self, gw: AgentGateway):
        with pytest.raises(GatewayError):
            gw.hub_register({"peer_id": "x"})

    def test_register_creates_session_and_routing(self, gw: AgentGateway):
        _register_peer(gw)
        loc = gw._kernel.get_location("s1", "w1")
        assert loc is not None
        assert loc.host_alias == "remote-dev"

    def test_duplicate_register_overwrites(self, gw: AgentGateway):
        """Re-registering a peer id overwrites the mapping (documented)."""
        _register_peer(gw, "p", "s1", "w1", "remote-dev")
        gw.hub_register({"peer_id": "p", "session_id": "s1", "agent_id": "w2", "host_alias": "__local__"})
        assert gw._peers["p"].agent_id == "w2"
        assert gw._peers["p"].host_alias == "__local__"

    def test_unregister_cleans_kernel_routing(self, gw: AgentGateway):
        _register_peer(gw)
        gw.hub_unregister({"peer_id": "remote-dev"})
        assert gw._kernel.get_location("s1", "w1") is None

    def test_existing_session_authority_preserved(self, gw: AgentGateway):
        """hub.register on a manager-owned session must NOT hijack authority."""
        gw._store.session_init("s2", "real-manager", ["worker"])
        gw.hub_register({
            "peer_id": "p2", "session_id": "s2",
            "agent_id": "worker", "host_alias": "__local__",
        })
        session = gw._kernel.get_session("s2")
        assert session is not None
        assert session.manager_id == "real-manager"
        # manager (hub sender) still added to allowed senders
        assert "manager" in session.acl.allowed_senders

    def test_peers_recover_after_gateway_restart(self, gw: AgentGateway, tmp_path: Path):
        """A fresh gateway re-registering peers on the persisted session works."""
        _register_peer(gw, "p1", "s1", "w1", "remote-dev")
        # Simulate gateway restart: new gateway instance, same store.
        gw2 = AgentGateway(store=gw._store, restore_from_park=False, peers_file=gw._peers_file)
        gw2.hub_register({
            "peer_id": "p1", "session_id": "s1",
            "agent_id": "w1", "host_alias": "remote-dev",
        })
        result = gw2.hub_send({"peer_id": "p1", "from": "hub", "content": "hi"})
        assert result["status"] in ("delivered", "accepted", "queued")

    def test_sweep_and_hub_send_concurrent(self, gw: AgentGateway):
        """sweep + hub_send racing on the same peer must not crash or corrupt."""
        import threading

        _register_peer(gw, "p1", "s1", "w1", "__local__")
        gw.runtime_register({
            "session_id": "s1", "agent_id": "w1", "runtime_id": "rt-1",
            "generation": 1, "review_key": "k1", "owner_pid": 999999, "nonce": "n",
        })
        errors: list[Exception] = []

        def _sweep():
            try:
                gw._sweep_once()
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def _send():
            try:
                gw.hub_send({"peer_id": "p1", "from": "hub", "content": "x"})
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=_sweep), threading.Thread(target=_send)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert errors == []


class TestHubSend:
    def test_send_delivers_to_agent_inbox(self, gw: AgentGateway):
        """Local peer (__local__) delivers straight to the inbox."""
        _register_peer(gw, "local", "s1", "w1", host_alias="__local__")
        result = gw.hub_send({
            "peer_id": "local", "from": "hub", "content": "hello",
        })
        assert result["status"] == "delivered"
        assert result["msg_id"]
        inbox = gw._store.list_messages(gw._store.agent_subdir("s1", "w1", "inbox"))
        assert len(inbox) == 1
        msg = inbox[0].read_bytes()
        assert b"hello" in msg

    def test_send_uses_require_ack(self, gw: AgentGateway):
        _register_peer(gw, "local", "s1", "w1", host_alias="__local__")
        gw.hub_send({"peer_id": "local", "from": "hub", "content": "hi"})
        inbox = gw._store.list_messages(gw._store.agent_subdir("s1", "w1", "inbox"))
        import json

        payload = json.loads(inbox[0].read_bytes())
        assert payload["require_ack"] is True
        assert payload["protocol_version"] == 2

    def test_send_offline_peer_fails_fast(self, gw: AgentGateway):
        _register_peer(gw)
        gw._peers["remote-dev"].status = "offline"
        # P3-f: offline peer → fail-closed raise（不假返回 ok=True 无 msg_id）
        with pytest.raises(GatewayError) as ei:
            gw.hub_send({"peer_id": "remote-dev", "from": "hub", "content": "hi"})
        assert ei.value.code == "NOT_FOUND"
        # nothing delivered
        inbox = gw._store.list_messages(gw._store.agent_subdir("s1", "w1", "inbox"))
        assert len(inbox) == 0

    def test_send_unknown_peer(self, gw: AgentGateway):
        with pytest.raises(GatewayError) as ei:
            gw.hub_send({"peer_id": "nope", "from": "hub", "content": "hi"})
        assert ei.value.code == "NOT_FOUND"

    def test_send_routes_remote_host(self, gw: AgentGateway):
        """Peer with a remote host alias → durable outbox (accepted/queued),
        resolved through the kernel routing table."""
        _register_peer(gw)
        loc = gw._kernel.get_location("s1", "w1")
        assert loc.host_alias == "remote-dev"
        result = gw.hub_send({"peer_id": "remote-dev", "from": "hub", "content": "x"})
        assert result["status"] in ("accepted", "queued")
        # durable outbox entry written
        outbox = gw._engine._outbox / "s1"
        assert len(list(outbox.glob("*.json"))) >= 1

    def test_remote_delivery_uses_ssh_transport(self, gw: AgentGateway, tmp_path):
        """Remote peer delivery invokes the SSH transport mailbox path."""
        import uuid as _uuid

        base = Path("/tmp") / f"hubssh-{_uuid.uuid4().hex[:8]}"
        # A second gateway with a mocked transport router wired in.
        from codeagent.config.repo_map import load_repo_map
        from codeagent.transport.router import TransportRouter
        from codeagent.transport.ssh import SSHTransport
        from unittest.mock import MagicMock

        store = gw._store
        transport = MagicMock(spec=SSHTransport)
        transport.mailbox.return_value = (0, "sent → w1/inbox/x.json", "")

        router = TransportRouter()
        gw2 = AgentGateway(store=store, restore_from_park=False)
        gw2._engine._router = router  # engine now routes through router

        gw2.hub_register({
            "peer_id": "remote-dev", "session_id": "s1",
            "agent_id": "w1", "host_alias": "remote-dev",
        })
        # Route remote-dev through the mocked SSH transport.
        with patch.object(router, "get", return_value=transport):
            result = gw2.hub_send({"peer_id": "remote-dev", "from": "hub", "content": "hello"})
        assert result["status"] in ("delivered", "queued")
        assert transport.mailbox.called


class TestHubStatus:
    def test_status_list(self, gw: AgentGateway):
        _register_peer(gw, "a", "s1", "w1")
        _register_peer(gw, "b", "s1", "w2")
        result = gw.hub_status({})
        assert len(result["peers"]) == 2
        ids = {p["peer_id"] for p in result["peers"]}
        assert ids == {"a", "b"}

    def test_status_one(self, gw: AgentGateway):
        _register_peer(gw)
        result = gw.hub_status({"peer_id": "remote-dev"})
        assert result["status"] == "online"
        assert result["host_alias"] == "remote-dev"

    def test_unregister(self, gw: AgentGateway):
        _register_peer(gw)
        result = gw.hub_unregister({"peer_id": "remote-dev"})
        assert result["unregistered"] is True
        assert "remote-dev" not in gw._peers

    def test_unregister_unknown(self, gw: AgentGateway):
        with pytest.raises(GatewayError):
            gw.hub_unregister({"peer_id": "nope"})


class TestHubPresenceSync:
    def test_sweep_marks_peer_offline(self, gw: AgentGateway):
        _register_peer(gw, "remote-dev", "s1", "w1")
        gw.runtime_register({
            "session_id": "s1", "agent_id": "w1", "runtime_id": "rt-1",
            "generation": 1, "review_key": "k1", "owner_pid": 999999, "nonce": "n",
        })
        gw._runtimes["rt-1"].status = "active"
        # runtime 已过冷启动期（>180s），150s 无活动 > 120s 阈值 → 判 offline
        from datetime import datetime, timedelta, timezone
        gw._runtimes["rt-1"].created_at = (
            datetime.now(timezone.utc) - timedelta(seconds=200)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        gw._runtimes["rt-1"].last_activity = time.time() - 150
        gw._sweep_once()
        assert gw._runtimes["rt-1"].status == "offline"
        assert gw._peers["remote-dev"].status == "offline"

    def test_heartbeat_restores_peer_online(self, gw: AgentGateway):
        _register_peer(gw, "remote-dev", "s1", "w1")
        gw.runtime_register({
            "session_id": "s1", "agent_id": "w1", "runtime_id": "rt-1",
            "generation": 1, "review_key": "k1", "owner_pid": 999999, "nonce": "n",
        })
        gw._runtimes["rt-1"].status = "offline"
        gw._peers["remote-dev"].status = "offline"
        gw.runtime_heartbeat({"runtime_id": "rt-1"})
        assert gw._peers["remote-dev"].status == "online"


class TestHubPeerPersistence:
    def test_peers_persist_and_restore(self, gw: AgentGateway, tmp_path: Path):
        """F2: hub peers survive a gateway restart (persisted file restore)."""
        _register_peer(gw, "p1", "s1", "w1", "remote-dev")
        assert gw._peers_file.exists()
        # Simulated restart: new gateway, same peers file.
        gw2 = AgentGateway(store=gw._store, restore_from_park=False, peers_file=gw._peers_file)
        assert "p1" in gw2._peers
        assert gw2._peers["p1"].host_alias == "remote-dev"
        gw2.stop()

    def test_unregister_persists_removal(self, gw: AgentGateway):
        _register_peer(gw, "p1", "s1", "w1", "remote-dev")
        gw.hub_unregister({"peer_id": "p1"})
        assert "p1" not in gw._peers
        gw2 = AgentGateway(store=gw._store, restore_from_park=False, peers_file=gw._peers_file)
        assert "p1" not in gw2._peers
        gw2.stop()


class TestSessionClaim:
    def test_claim_success_and_idempotent(self, gw: AgentGateway):
        r1 = gw.session_claim({"session_id": "s1", "agent_id": "w1", "owner": "rt-1"})
        assert r1["owner"] == "rt-1"
        # same owner re-claim extends (idempotent)
        r2 = gw.session_claim({"session_id": "s1", "agent_id": "w1", "owner": "rt-1"})
        assert r2["owner"] == "rt-1"

    def test_claim_conflict(self, gw: AgentGateway):
        gw.session_claim({"session_id": "s1", "agent_id": "w1", "owner": "rt-1"})
        with pytest.raises(GatewayError) as ei:
            gw.session_claim({"session_id": "s1", "agent_id": "w1", "owner": "rt-2"})
        assert ei.value.code == "PROTOCOL_CONFLICT"

    def test_claim_expired_takeover(self, gw: AgentGateway):
        gw.session_claim({"session_id": "s1", "agent_id": "w1", "owner": "rt-1", "ttl": 0.01})
        import time as _t

        _t.sleep(0.02)
        r = gw.session_claim({"session_id": "s1", "agent_id": "w1", "owner": "rt-2"})
        assert r["owner"] == "rt-2"

    def test_release(self, gw: AgentGateway):
        gw.session_claim({"session_id": "s1", "agent_id": "w1", "owner": "rt-1"})
        r = gw.session_release({"session_id": "s1", "agent_id": "w1", "owner": "rt-1"})
        assert r["released"] is True
        # claimable again
        gw.session_claim({"session_id": "s1", "agent_id": "w1", "owner": "rt-2"})


class TestMergePersistence:
    def test_merges_persist_and_restore(self, gw: AgentGateway, tmp_path: Path):
        """merge records survive a gateway restart (conflict detection)."""
        gw.write_merge({
            "session_id": "s1", "target_path": "docs/a.md",
            "artifact_sha256": "abc123", "base_revision": "r1",
        })
        assert gw._peers_file.with_name("merges.json").exists()
        gw2 = AgentGateway(store=gw._store, restore_from_park=False, peers_file=gw._peers_file)
        # restored merge blocks a conflicting write
        with pytest.raises(GatewayError) as ei:
            gw2.write_merge({
                "session_id": "s1", "target_path": "docs/a.md",
                "artifact_sha256": "different", "base_revision": "r1",
            })
        assert ei.value.code == "PROTOCOL_CONFLICT"
        gw2.stop()


def test_runtime_declare_registers_native_presence(gw):
    """runtime.declare 注册非 postmesh 管理的 native runtime（weak presence）。"""
    from codeagent.domain.park import ParkManifest, Lifecycle
    from codeagent.park.registry import ParkRegistry
    ParkRegistry().acquire("k-declare", ParkManifest(
        review_key="k-declare", swarm_session_id="s-declare", lifecycle=Lifecycle.HOT_PARKED))
    result = gw.runtime_declare({
        "review_key": "k-declare", "backend_session_id": "native-sid-1", "mode": "native_resume"})
    assert result.get("runtime_id", "").startswith("native-")
    assert result.get("status") == "active"
    again = gw.runtime_declare({
        "review_key": "k-declare", "backend_session_id": "native-sid-1", "mode": "native_resume"})
    assert again["runtime_id"] == result["runtime_id"]
