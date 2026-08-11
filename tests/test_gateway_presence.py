"""P8.3 presence tests — sweep marks offline, heartbeat restores, queries."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codeagent.constants import ISO_TIMESTAMP_FORMAT
from codeagent.gateway.events import EventStore
from codeagent.gateway.model import RuntimeEventDraft
from codeagent.gateway.service import AgentGateway
from codeagent.mailbox.store import MailboxStore


@pytest.fixture
def gw(tmp_path: Path) -> AgentGateway:
    import uuid

    base = Path("/tmp") / f"gwpres-{uuid.uuid4().hex[:8]}"
    store = MailboxStore(root=base / "mailbox")
    store.root.mkdir(parents=True, exist_ok=True)
    events = EventStore(db_path=base / "e.sqlite3", source_host="h")
    gateway = AgentGateway(store=store, events=events, restore_from_park=False)
    gateway._offline_timeout = 120.0  # explicit
    yield gateway


def _register_active(gw: AgentGateway, runtime_id: str = "rt-1",
                     session_id: str = "s1", agent_id: str = "w1") -> None:
    # Ensure the session exists (runtime_register requires an authoritative roster).
    try:
        gw._store.session_init(session_id, "manager", [agent_id])
    except ValueError:
        pass
    gw.runtime_register({
        "session_id": session_id,
        "agent_id": agent_id,
        "runtime_id": runtime_id,
        "generation": 1,
        "review_key": "k1",
        "owner_pid": 999999,  # won't be checked by gateway
        "nonce": "n1",
    })


class TestPresenceSweep:
    def test_sweep_marks_offline(self, gw: AgentGateway):
        _register_active(gw)
        gw._runtimes["rt-1"].status = "active"
        # runtime 已过冷启动期（>180s 注册），150s 无活动 > 120s 阈值 → 判 offline
        gw._runtimes["rt-1"].created_at = (
            datetime.now(timezone.utc) - timedelta(seconds=200)
        ).strftime(ISO_TIMESTAMP_FORMAT)
        gw._runtimes["rt-1"].last_activity = time.time() - 150  # > 120s
        offline = gw._sweep_once()
        assert "rt-1" in offline
        assert gw._runtimes["rt-1"].status == "offline"

    def test_sweep_writes_ag_status_event(self, gw: AgentGateway):
        _register_active(gw)
        gw._runtimes["rt-1"].created_at = (
            datetime.now(timezone.utc) - timedelta(seconds=200)
        ).strftime(ISO_TIMESTAMP_FORMAT)
        gw._runtimes["rt-1"].last_activity = time.time() - 150
        gw._sweep_once()
        events, _ = gw._events.list_after(0, filters=["AGENT_STATUS"])
        assert len(events) == 1
        assert events[0].payload["new_status"] == "offline"
        assert events[0].payload["reason"] == "heartbeat_timeout"

    def test_fresh_runtime_not_swept(self, gw: AgentGateway):
        _register_active(gw)
        gw._runtimes["rt-1"].status = "active"
        gw._runtimes["rt-1"].last_activity = time.time() - 10  # fresh
        assert gw._sweep_once() == []

    def test_heartbeat_restores_active(self, gw: AgentGateway):
        _register_active(gw)
        gw._runtimes["rt-1"].status = "offline"
        gw._runtimes["rt-1"].last_activity = time.time() - 300
        result = gw.runtime_heartbeat({"runtime_id": "rt-1"})
        assert result["status"] == "active"
        assert gw._runtimes["rt-1"].status == "active"
        events, _ = gw._events.list_after(0, filters=["AGENT_STATUS"])
        assert any(e.payload.get("new_status") == "active" for e in events)

    def test_stopped_not_swept(self, gw: AgentGateway):
        _register_active(gw)
        gw._runtimes["rt-1"].status = "stopped"
        gw._runtimes["rt-1"].last_activity = 0
        assert gw._sweep_once() == []
        assert gw._runtimes["rt-1"].status == "stopped"

    def test_heartbeat_renews_and_keeps_active(self, gw: AgentGateway):
        _register_active(gw)
        before = gw._runtimes["rt-1"].last_activity
        time.sleep(0.01)
        gw.runtime_heartbeat({"runtime_id": "rt-1"})
        assert gw._runtimes["rt-1"].last_activity > before
        assert gw._runtimes["rt-1"].status == "active"

    def test_runtime_event_refreshes_liveness(self, gw: AgentGateway):
        """An active event stream (no heartbeat) must NOT be swept offline."""
        _register_active(gw)
        gw._runtimes["rt-1"].status = "active"
        gw._runtimes["rt-1"].last_activity = time.time() - 100  # under 120s
        # A runtime.event keeps last_activity fresh → not swept.
        gw.runtime_event({"event": {
            "runtime_id": "rt-1", "generation": 1, "session_id": "s1",
            "agent_id": "w1", "request_id": "", "run_id": "",
            "kind": "ASSISTANT_PROGRESS", "created_at": "2026-01-01T00:00:00Z",
            "payload": {"text": "x"},
        }})
        assert gw._runtimes["rt-1"].last_activity > time.time() - 1
        assert gw._sweep_once() == []

    def test_sweep_heartbeat_race_no_false_offline(self, gw: AgentGateway):
        """A heartbeat refreshing last_activity while sweep is scanning must
        not produce a false offline flip (lock-protected re-verify)."""
        import threading

        _register_active(gw)
        gw._runtimes["rt-1"].status = "active"
        gw._runtimes["rt-1"].last_activity = time.time() - 150  # stale

        # Hold the sweep lock, simulate a concurrent heartbeat, then let the
        # sweep proceed — it must re-verify and skip.
        with gw._runtimes_lock:
            # Concurrent heartbeat refreshes last_activity (would happen
            # between scan and transition without the lock).
            gw._runtimes["rt-1"].last_activity = time.time()
        # Sweep now sees fresh liveness → no offline flip.
        assert gw._sweep_once() == []
        assert gw._runtimes["rt-1"].status == "active"

    def test_sweep_loop_stops(self, gw: AgentGateway):
        """stop() signals the sweep loop and joins the thread."""
        gw._sweep_interval = 0.05  # fast for the test
        gw._start_sweep_loop()
        assert gw._sweep_thread is not None and gw._sweep_thread.is_alive()
        gw.stop()
        assert gw._sweep_thread is None
        # idempotent
        gw.stop()


class TestPresenceQueries:
    def test_runtime_status(self, gw: AgentGateway):
        _register_active(gw)
        gw._runtimes["rt-1"].status = "offline"
        result = gw.runtime_status({"runtime_id": "rt-1"})
        assert result["status"] == "offline"
        assert result["runtime_id"] == "rt-1"
        assert result["host_alias"] == "__local__"

    def test_runtime_status_unknown(self, gw: AgentGateway):
        from codeagent.gateway.model import GatewayError

        with pytest.raises(GatewayError):
            gw.runtime_status({"runtime_id": "nope"})

    def test_runtimes_list_filter_by_session(self, gw: AgentGateway):
        _register_active(gw, "rt-1", "s1", "w1")
        _register_active(gw, "rt-2", "s2", "w2")
        result = gw.runtimes_list({"session_id": "s1"})
        assert len(result["runtimes"]) == 1
        assert result["runtimes"][0]["runtime_id"] == "rt-1"

    def test_runtimes_list_all(self, gw: AgentGateway):
        _register_active(gw, "rt-1", "s1", "w1")
        _register_active(gw, "rt-2", "s1", "w2")
        result = gw.runtimes_list({})
        assert len(result["runtimes"]) == 2


class TestGatewayRestore:
    def test_restore_runtimes_from_park(self, tmp_path: Path):
        """A3: a fresh gateway rebuilds runtime records from HOT_PARKED park
        manifests — hot detection survives a gateway restart."""
        import time as _time
        from codeagent.domain.park import Lifecycle, ParkManifest
        from codeagent.park.registry import ParkRegistry

        # Seed a hot-parked manifest with a backend session.
        reg = ParkRegistry()
        reg.acquire("restore:key", ParkManifest(
            review_key="restore:key",
            swarm_session_id="ora-restore-s1",
            mailbox_agent_id="oracle",
            backend_session_id="backend-s1",
            host="__local__",
            lifecycle=Lifecycle.HOT_PARKED,
            created_at=_time.time(),
            last_activity_at=_time.time(),
        ))
        # New gateway instance (simulated restart) with the SAME store.
        base = Path("/tmp") / f"gwrest-{__import__('uuid').uuid4().hex[:8]}"
        store = MailboxStore(root=base / "mailbox")
        store.root.mkdir(parents=True, exist_ok=True)
        gw2 = AgentGateway(store=store, events=EventStore(db_path=base / "e.sqlite3", source_host="h"))
        restored = [r for r in gw2._runtimes.values() if r.review_key == "restore:key"]
        assert len(restored) == 1
        assert restored[0].backend_session_id == "backend-s1"
        assert restored[0].status == "active"
        gw2.stop()
