"""Tests for DeliveryEngine — durable outbox → transport → remote inbox."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from codeagent.domain import HostSpec
from codeagent.mailbox.store import MailboxStore
from codeagent.swarm.delivery import DeliveryEngine, SendReceipt
from codeagent.swarm.kernel import SwarmKernel
from codeagent.swarm.model import AgentLocation, Envelope


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> MailboxStore:
    """MailboxStore rooted in tmp."""
    return MailboxStore(root=tmp_path)


@pytest.fixture
def outbox_root(tmp_path: Path) -> Path:
    """Dedicated outbox directory."""
    return tmp_path / "outbox"


@pytest.fixture
def engine(store: MailboxStore, outbox_root: Path) -> DeliveryEngine:
    """DeliveryEngine with no router (tests mock transport directly)."""
    return DeliveryEngine(mailbox_store=store, outbox_root=outbox_root)


@pytest.fixture
def host_a() -> HostSpec:
    return HostSpec(name="alpha", ssh_alias="alpha", hostnames=("alpha",))


@pytest.fixture
def host_b() -> HostSpec:
    return HostSpec(name="beta", ssh_alias="beta", hostnames=("beta",))


def _init_session(store: MailboxStore, sid: str = "s1") -> None:
    store.session_init(sid, "mgr", ["w1", "w2"])


def _make_envelope(
    session_id: str = "s1",
    from_id: str = "w1",
    to_id: str = "w2",
    msg_id: str = "w1_20260101T000000_abc123",
    subject: str = "test-subject",
    body: str = "test-body",
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "session_id": session_id,
        "from": from_id,
        "to": to_id,
        "subject": subject,
        "body": body,
        "kind": "TASK",
        "msg_id": msg_id,
        "created_at": now,
    }


# ── SendReceipt ────────────────────────────────────────────────────────


class TestSendReceipt:
    def test_to_dict_roundtrip(self) -> None:
        r = SendReceipt(status="delivered", msg_id="m1", queued=False)
        d = r.to_dict()
        assert d["status"] == "delivered"
        assert d["msg_id"] == "m1"
        r2 = SendReceipt.from_dict(d)
        assert r2 == r

    def test_failed_with_error(self) -> None:
        r = SendReceipt(status="failed", error="boom")
        assert r.error == "boom"
        assert r.queued is False

    def test_accepted_queued(self) -> None:
        r = SendReceipt(status="accepted", msg_id="m1", queued=True)
        assert r.queued is True


# ── Durable outbox write ───────────────────────────────────────────────


class TestDurableOutbox:
    def test_outbox_write_creates_file(self, engine: DeliveryEngine, outbox_root: Path) -> None:
        """Envelope written to outbox/<session>/<msg_id>.json."""
        _init_session(engine._store)
        envelope = _make_envelope()

        path = engine._write_outbox("s1", envelope["msg_id"], envelope)
        assert path.exists()
        data = json.loads(path.read_bytes())
        assert data["msg_id"] == envelope["msg_id"]
        assert data["subject"] == "test-subject"

    def test_outbox_write_idempotent(self, engine: DeliveryEngine) -> None:
        """Writing same msg_id twice does not raise."""
        _init_session(engine._store)
        envelope = _make_envelope()
        mid = envelope["msg_id"]

        p1 = engine._write_outbox("s1", mid, envelope)
        p2 = engine._write_outbox("s1", mid, _make_envelope(subject="updated"))
        assert p1 == p2
        # Original content preserved (not overwritten)
        assert json.loads(p1.read_bytes())["subject"] == "test-subject"

    def test_crash_sim_replay(self, store: MailboxStore, tmp_path: Path) -> None:
        """Simulate crash: write outbox entry, create fresh engine, flush replays."""
        _init_session(store)
        outbox_root = tmp_path / "outbox"
        envelope = _make_envelope()
        mid = envelope["msg_id"]
        envelope["_target_host"] = "alpha"

        # First engine writes to outbox
        engine1 = DeliveryEngine(mailbox_store=store, outbox_root=outbox_root)
        engine1._write_outbox("s1", mid, envelope)
        assert (outbox_root / "s1" / f"{mid}.json").exists()

        # "Crash" — create a brand new engine (no in-memory cache)
        call_count = {"n": 0}

        def mock_mailbox(host, args, **kw):
            # _remote_send 先做幂等 session-init（同一次远程调用），不计入 send 次数
            if "session-init" in args:
                return 0, "session exists", ""
            call_count["n"] += 1
            return 0, "sent", ""

        mock_router = MagicMock()
        mock_transport = MagicMock()
        mock_transport.mailbox = mock_mailbox
        mock_router.get.return_value = mock_transport
        mock_router.capabilities.return_value = {"mailbox"}

        engine2 = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router,
            outbox_root=outbox_root,
        )

        # flush replays the pending entry
        delivered = engine2.flush(session_id="s1")
        assert delivered == 1
        assert call_count["n"] == 1
        # Delivered marker exists
        assert (outbox_root / "s1" / f".delivered-{mid}").exists()


# ── Transport success → delivered ──────────────────────────────────────


class TestTransportSuccess:
    def test_deliver_success(
        self, store: MailboxStore, outbox_root: Path, host_a: HostSpec,
    ) -> None:
        """Transport success → delivered marker + receipt.status == 'delivered'."""
        _init_session(store)
        envelope = _make_envelope()

        mock_router = MagicMock()
        mock_transport = MagicMock()
        mock_transport.mailbox.return_value = (0, "sent → w2/inbox/m1.json", "")
        mock_router.get.return_value = mock_transport
        mock_router.capabilities.return_value = {"mailbox"}

        engine = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router,
            outbox_root=outbox_root,
        )

        receipt = engine.deliver("s1", host_a, envelope)
        assert receipt.status == "delivered"
        assert receipt.msg_id == envelope["msg_id"]
        assert receipt.queued is False

        # Delivered marker exists
        mid = envelope["msg_id"]
        assert (outbox_root / "s1" / f".delivered-{mid}").exists()

        # Outbox entry still exists (preserved for audit)
        assert (outbox_root / "s1" / f"{mid}.json").exists()
        # P1-6: successful remote delivery appends canonical history
        history = store.read_history("s1")
        assert any(h.get("msg_id") == mid for h in history)

    def test_deliver_calls_transport_with_correct_args(
        self, store: MailboxStore, outbox_root: Path, host_a: HostSpec,
    ) -> None:
        """Transport.mailbox receives correct CLI args."""
        _init_session(store)
        envelope = _make_envelope(from_id="mgr", to_id="w1", subject="go")

        mock_router = MagicMock()
        mock_transport = MagicMock()
        mock_transport.mailbox.return_value = (0, "ok", "")
        mock_router.get.return_value = mock_transport
        mock_router.capabilities.return_value = {"mailbox"}

        engine = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router,
            outbox_root=outbox_root,
        )

        engine.deliver("s1", host_a, envelope)

        args = mock_transport.mailbox.call_args[0][1]
        assert "send" in args
        assert "--session" in args
        assert "s1" in args
        assert "--from" in args
        assert "mgr" in args
        assert "--to" in args
        assert "w1" in args
        assert "--subject" in args
        assert "go" in args
        assert "--msg-id" in args
        # P0: body must be the ACTUAL message body, not the serialized envelope
        assert "--body" in args
        assert args[args.index("--body") + 1] == envelope["body"]

    def test_deliver_forwards_attachments(self, store, outbox_root, host_a):
        """P0: cross-host sends carry AttachmentRefs via --attachment flags."""
        _init_session(store)
        envelope = _make_envelope()
        envelope["attachments"] = [{
            "artifact_id": "a1", "path": "out.png", "size": 3,
            "sha256": "0" * 64, "media_type": "image/png",
        }]

        mock_router = MagicMock()
        mock_transport = MagicMock()
        mock_transport.mailbox.return_value = (0, "ok", "")
        mock_router.get.return_value = mock_transport
        mock_router.capabilities.return_value = {"mailbox"}

        engine = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router,
            outbox_root=outbox_root,
        )
        engine.deliver("s1", host_a, envelope)

        args = mock_transport.mailbox.call_args[0][1]
        assert "--attachment" in args
        att_idx = args.index("--attachment")
        import json as _json
        att = _json.loads(args[att_idx + 1])
        assert att["artifact_id"] == "a1"
        assert att["path"] == "out.png"


# ── Transport failure → pending retained ────────────────────────────────


class TestTransportFailure:
    def test_failure_returns_accepted_queued(
        self, store: MailboxStore, outbox_root: Path, host_a: HostSpec,
    ) -> None:
        """Transport failure → accepted + queued, outbox preserved."""
        _init_session(store)
        envelope = _make_envelope()

        mock_router = MagicMock()
        mock_transport = MagicMock()
        mock_transport.mailbox.return_value = (1, "", "connection refused")
        mock_router.get.return_value = mock_transport
        mock_router.capabilities.return_value = {"mailbox"}

        engine = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router,
            outbox_root=outbox_root,
        )

        receipt = engine.deliver("s1", host_a, envelope)
        assert receipt.status == "accepted"
        assert receipt.queued is True

        mid = envelope["msg_id"]
        assert (outbox_root / "s1" / f"{mid}.json").exists()
        assert not (outbox_root / "s1" / f".delivered-{mid}").exists()

    def test_flush_retries_and_succeeds(
        self, store: MailboxStore, outbox_root: Path, host_a: HostSpec,
    ) -> None:
        """Flush retries pending entries; succeeds on 2nd attempt."""
        _init_session(store)
        envelope = _make_envelope()
        envelope["_target_host"] = "alpha"
        mid = envelope["msg_id"]

        # Write outbox directly (simulate a prior failed delivery)
        sd = outbox_root / "s1"
        sd.mkdir(parents=True, exist_ok=True)
        (sd / f"{mid}.json").write_text(json.dumps(envelope))

        call_count = {"n": 0}

        def mock_mailbox(host, args, **kw):
            # _remote_send 先做幂等 session-init（同一次远程调用），不计入 send 次数
            if "session-init" in args:
                return 0, "session exists", ""
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("connection refused")
            return 0, "sent", ""

        mock_router = MagicMock()
        mock_transport = MagicMock()
        mock_transport.mailbox = mock_mailbox
        mock_router.get.return_value = mock_transport
        mock_router.capabilities.return_value = {"mailbox"}

        engine = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router,
            outbox_root=outbox_root,
        )

        # First flush: fails
        count = engine.flush(session_id="s1")
        assert count == 0
        assert call_count["n"] == 1

        # Second flush: succeeds
        count = engine.flush(session_id="s1")
        assert count == 1
        assert call_count["n"] == 2
        assert (sd / f".delivered-{mid}").exists()
        # P1: successful flush retry must also append canonical history
        history = store.read_history("s1")
        assert any(h.get("msg_id") == mid for h in history)

    def test_flush_exception_in_mailbox(
        self, store: MailboxStore, outbox_root: Path,
    ) -> None:
        """Exception in transport.mailbox is caught; entry stays pending."""
        _init_session(store)
        envelope = _make_envelope()
        envelope["_target_host"] = "alpha"
        mid = envelope["msg_id"]

        sd = outbox_root / "s1"
        sd.mkdir(parents=True, exist_ok=True)
        (sd / f"{mid}.json").write_text(json.dumps(envelope))

        mock_router = MagicMock()
        mock_transport = MagicMock()
        mock_transport.mailbox.side_effect = OSError("socket dead")
        mock_router.get.return_value = mock_transport
        mock_router.capabilities.return_value = {"mailbox"}

        engine = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router,
            outbox_root=outbox_root,
        )

        count = engine.flush(session_id="s1")
        assert count == 0
        assert not (sd / f".delivered-{mid}").exists()


# ── msg_id dedup ───────────────────────────────────────────────────────


class TestIdempotency:
    def test_same_msg_id_twice_one_outbox_entry(
        self, store: MailboxStore, outbox_root: Path, host_a: HostSpec,
    ) -> None:
        """Delivering same msg_id twice → one outbox entry, one remote write."""
        _init_session(store)
        envelope = _make_envelope()

        call_count = {"n": 0}

        mock_router = MagicMock()
        mock_transport = MagicMock()
        def counting_mailbox(host, args, **kw):
            # _remote_send 先做幂等 session-init（同一次远程调用），不计入 send 次数
            if "session-init" in args:
                return 0, "session exists", ""
            call_count["n"] += 1
            return 0, "sent", ""
        mock_transport.mailbox = counting_mailbox
        mock_router.get.return_value = mock_transport
        mock_router.capabilities.return_value = {"mailbox"}

        engine = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router,
            outbox_root=outbox_root,
        )

        r1 = engine.deliver("s1", host_a, envelope)
        assert r1.status == "delivered"
        assert call_count["n"] == 1

        # Second deliver with same msg_id — returns cached, no transport call
        r2 = engine.deliver("s1", host_a, envelope)
        assert r2.status == "delivered"
        assert r2.msg_id == r1.msg_id
        assert call_count["n"] == 1  # still 1

        # Only one outbox entry
        entries = list((outbox_root / "s1").glob("*.json"))
        assert len(entries) == 1

    def test_idempotency_across_engine_instances(
        self, store: MailboxStore, outbox_root: Path, host_a: HostSpec,
    ) -> None:
        """Idempotency persists across engine restarts (outbox check)."""
        _init_session(store)
        envelope = _make_envelope()

        # First engine delivers
        mock_router1 = MagicMock()
        mock_transport1 = MagicMock()
        mock_transport1.mailbox.return_value = (0, "sent", "")
        mock_router1.get.return_value = mock_transport1
        mock_router1.capabilities.return_value = {"mailbox"}

        engine1 = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router1,
            outbox_root=outbox_root,
        )
        r1 = engine1.deliver("s1", host_a, envelope)
        assert r1.status == "delivered"

        # Second engine (fresh start) — dedup detects outbox entry
        mock_router2 = MagicMock()
        engine2 = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router2,
            outbox_root=outbox_root,
        )
        r2 = engine2.deliver("s1", host_a, envelope)
        assert r2.status == "delivered"
        mock_router2.get.assert_not_called()  # no transport call


# ── ack phases ─────────────────────────────────────────────────────────


class TestAckPhases:
    def test_ack_lifecycle(
        self, store: MailboxStore, outbox_root: Path, host_a: HostSpec,
    ) -> None:
        """accepted → delivered → consumed via ack."""
        _init_session(store)
        envelope = _make_envelope()
        mid = envelope["msg_id"]

        mock_router = MagicMock()
        mock_transport = MagicMock()
        mock_transport.mailbox.return_value = (0, "sent", "")
        mock_router.get.return_value = mock_transport
        mock_router.capabilities.return_value = {"mailbox"}

        engine = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router,
            outbox_root=outbox_root,
        )

        receipt = engine.deliver("s1", host_a, envelope)
        assert receipt.status == "delivered"

        # ack consumed
        engine.ack("s1", "w2", mid, "consumed")

        # Status marker written
        status_dir = outbox_root / "s1" / f".status-{mid}"
        assert status_dir.exists()
        assert (status_dir / "phase").read_text().strip() == "consumed"

    def test_ack_invalid_msg_id(
        self, engine: DeliveryEngine,
    ) -> None:
        """ack raises on invalid msg_id."""
        with pytest.raises(ValueError, match="invalid msg_id"):
            engine.ack("s1", "w2", "../escape", "consumed")

    def test_ack_missing_entry(
        self, engine: DeliveryEngine,
    ) -> None:
        """ack raises on nonexistent outbox entry."""
        _init_session(engine._store)
        with pytest.raises(ValueError, match="outbox entry not found"):
            engine.ack("s1", "w2", "nonexistent_msg", "consumed")

    def test_ack_all_phases(
        self, store: MailboxStore, outbox_root: Path, host_a: HostSpec,
    ) -> None:
        """All three ack phases are trackable."""
        _init_session(store)
        envelope = _make_envelope()
        mid = envelope["msg_id"]

        mock_router = MagicMock()
        mock_transport = MagicMock()
        mock_transport.mailbox.return_value = (0, "sent", "")
        mock_router.get.return_value = mock_transport
        mock_router.capabilities.return_value = {"mailbox"}

        engine = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router,
            outbox_root=outbox_root,
        )

        engine.deliver("s1", host_a, envelope)

        for phase in ("accepted", "delivered", "consumed"):
            engine.ack("s1", "w2", mid, phase)
            status_dir = outbox_root / "s1" / f".status-{mid}"
            assert (status_dir / "phase").read_text().strip() == phase


# ── pending ────────────────────────────────────────────────────────────


class TestPending:
    def test_pending_lists_undelivered(
        self, store: MailboxStore, outbox_root: Path,
    ) -> None:
        """pending() returns undelivered entries only."""
        _init_session(store)
        env1 = _make_envelope(msg_id="m_delivered")
        env2 = _make_envelope(msg_id="m_pending")

        sd = outbox_root / "s1"
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "m_delivered.json").write_text(json.dumps(env1))
        (sd / "m_pending.json").write_text(json.dumps(env2))
        (sd / ".delivered-m_delivered").write_text("{}")

        engine = DeliveryEngine(mailbox_store=store, outbox_root=outbox_root)
        result = engine.pending(session_id="s1")
        assert len(result) == 1
        assert result[0]["msg_id"] == "m_pending"

    def test_pending_empty(self, engine: DeliveryEngine) -> None:
        assert engine.pending() == []

    def test_pending_across_sessions(
        self, store: MailboxStore, outbox_root: Path,
    ) -> None:
        """pending() with no session_id lists all sessions."""
        env_s1 = _make_envelope(session_id="s1", msg_id="m1")
        env_s2 = _make_envelope(session_id="s2", msg_id="m2")

        for sid, env in [("s1", env_s1), ("s2", env_s2)]:
            sd = outbox_root / sid
            sd.mkdir(parents=True, exist_ok=True)
            (sd / f"{env['msg_id']}.json").write_text(json.dumps(env))

        engine = DeliveryEngine(mailbox_store=store, outbox_root=outbox_root)
        result = engine.pending()
        assert len(result) == 2


# ── Stream-capable host routing ────────────────────────────────────────


class TestStreamRouting:
    def test_relay_host_uses_one_shot(
        self, store: MailboxStore, outbox_root: Path,
    ) -> None:
        """Relay host (mailbox-only cap) uses transport.mailbox() one-shot."""
        _init_session(store)
        envelope = _make_envelope()
        relay_host = HostSpec(
            name="relay", ssh_alias="relay", hostnames=("relay",),
            transport="relay-login",
        )

        mock_router = MagicMock()
        mock_transport = MagicMock()
        mock_transport.mailbox.return_value = (0, "sent", "")
        mock_router.get.return_value = mock_transport
        mock_router.capabilities.return_value = {"mailbox"}  # no stream

        engine = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router,
            outbox_root=outbox_root,
        )

        receipt = engine.deliver("s1", relay_host, envelope)
        assert receipt.status == "delivered"
        assert mock_transport.mailbox.call_count == 2  # session-init + send
        mock_router.get.assert_called_once_with(relay_host)


# ── No-fanout ──────────────────────────────────────────────────────────


class TestNoFanout:
    def test_single_recipient_single_transport_call(
        self, store: MailboxStore, outbox_root: Path, host_a: HostSpec,
    ) -> None:
        """Direct send to one host writes exactly one remote inbox call."""
        _init_session(store)
        envelope = _make_envelope(to_id="w2")

        call_count = {"n": 0}

        mock_router = MagicMock()
        mock_transport = MagicMock()
        def counting_mailbox(host, args, **kw):
            # _remote_send 先做幂等 session-init（同一次远程调用），不计入 send 次数
            if "session-init" in args:
                return 0, "session exists", ""
            call_count["n"] += 1
            return 0, "sent", ""
        mock_transport.mailbox = counting_mailbox
        mock_router.get.return_value = mock_transport
        mock_router.capabilities.return_value = {"mailbox"}

        engine = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router,
            outbox_root=outbox_root,
        )

        receipt = engine.deliver("s1", host_a, envelope)
        assert receipt.status == "delivered"
        assert call_count["n"] == 1


# ── Edge cases ─────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_non_dict_envelope(
        self, engine: DeliveryEngine, host_a: HostSpec,
    ) -> None:
        """Non-dict envelope returns failed receipt."""
        receipt = engine.deliver("s1", host_a, "not a dict")
        assert receipt.status == "failed"
        assert "dict" in receipt.error

    def test_missing_msg_id(
        self, engine: DeliveryEngine, host_a: HostSpec,
    ) -> None:
        """Envelope missing msg_id returns failed receipt."""
        receipt = engine.deliver("s1", host_a, {"subject": "hi"})
        assert receipt.status == "failed"
        assert "msg_id" in receipt.error

    def test_no_router_uses_direct_ssh(
        self, store: MailboxStore, outbox_root: Path, host_a: HostSpec,
    ) -> None:
        """Engine with no router falls back to SSHTransport."""
        _init_session(store)
        envelope = _make_envelope()

        engine = DeliveryEngine(mailbox_store=store, outbox_root=outbox_root)

        with patch("codeagent.transport.ssh.SSHTransport") as mock_ssh_cls:
            mock_ssh = MagicMock()
            mock_ssh.mailbox.return_value = (0, "sent", "")
            mock_ssh_cls.return_value = mock_ssh

            receipt = engine.deliver("s1", host_a, envelope)
            assert receipt.status == "delivered"
            assert mock_ssh.mailbox.call_count == 2  # session-init + send

    def test_no_host_alias_skips_transport(
        self, store: MailboxStore, outbox_root: Path,
    ) -> None:
        """Target with no host_alias → local delivery: inbox write, delivered.

        Regression: this used to return accepted with outbox-only (never
        reaching the recipient inbox). Now writes straight to inbox.
        """
        _init_session(store)
        envelope = _make_envelope()

        # Target with no ssh_alias or host_alias
        class NoAlias:
            pass

        engine = DeliveryEngine(mailbox_store=store, outbox_root=outbox_root)
        receipt = engine.deliver("s1", NoAlias(), envelope)
        assert receipt.status == "delivered"
        assert receipt.queued is False
        # Message actually reached the recipient inbox
        inbox = store.agent_subdir("s1", envelope["to"], "inbox")
        msgs = store.list_messages(inbox)
        assert len(msgs) == 1

    def test_flush_with_consumed_entries_skips(
        self, store: MailboxStore, outbox_root: Path,
    ) -> None:
        """Flush skips entries already marked consumed."""
        _init_session(store)
        envelope = _make_envelope(msg_id="m_consumed")
        envelope["_target_host"] = "alpha"
        mid = envelope["msg_id"]

        sd = outbox_root / "s1"
        sd.mkdir(parents=True, exist_ok=True)
        (sd / f"{mid}.json").write_text(json.dumps(envelope))
        status_dir = sd / f".status-{mid}"
        status_dir.mkdir()
        (status_dir / "phase").write_text("consumed")

        mock_router = MagicMock()
        engine = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router,
            outbox_root=outbox_root,
        )

        count = engine.flush(session_id="s1")
        assert count == 0
        mock_router.get.assert_not_called()

    def test_flush_no_target_host_skips(
        self, store: MailboxStore, outbox_root: Path,
    ) -> None:
        """Flush skips entries without _target_host."""
        _init_session(store)
        envelope = _make_envelope(msg_id="m_notarget")
        # No _target_host field

        sd = outbox_root / "s1"
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "m_notarget.json").write_text(json.dumps(envelope))

        mock_router = MagicMock()
        engine = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router,
            outbox_root=outbox_root,
        )

        count = engine.flush(session_id="s1")
        assert count == 0

    def test_flush_local_target_writes_inbox(
        self, store: MailboxStore, outbox_root: Path,
    ) -> None:
        """Bug B 回归：flush 对本机 target（resolve_is_local）直写本地 inbox，
        不走 transport（避免 SSH 本机别名 No route + 10s 超时）。"""
        _init_session(store, sid="s1")
        envelope = _make_envelope(msg_id="m_localflush", to_id="w2")
        envelope["_target_host"] = "mac"
        sd = outbox_root / "s1"
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "m_localflush.json").write_text(json.dumps(envelope))

        mock_router = MagicMock()
        engine = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router,
            outbox_root=outbox_root,
        )
        with patch("codeagent.domain.resolve_is_local", return_value=True):
            count = engine.flush(session_id="s1")

        assert count == 1
        assert (sd / ".delivered-m_localflush").exists()
        # inbox 收到消息（幂等 msg_id）
        peek = store.peek("s1", "w2", 10, 200)
        assert any(m["msg_id"] == "m_localflush" for m in peek["messages"])

    def test_deliver_local_hostspec_writes_inbox(
        self, store: MailboxStore, outbox_root: Path,
    ) -> None:
        """本机 HostSpec（resolve_is_local True）走 local delivery，
        不调用 transport.mailbox。"""
        _init_session(store, sid="s1")
        mock_router = MagicMock()
        engine = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router,
            outbox_root=outbox_root,
        )
        local_host = HostSpec(name="mac", ssh_alias="OA-MIANYIN-MAC",
                              hostnames=("OA-MIANYIN-MAC",))
        envelope = _make_envelope(msg_id="m_localhost", to_id="w2")
        with patch("codeagent.domain.resolve_is_local", return_value=True):
            receipt = engine.deliver("s1", local_host, envelope)

        assert receipt.status == "delivered"
        mock_router.mailbox.assert_not_called()
        peek = store.peek("s1", "w2", 10, 200)
        assert any(m["msg_id"] == "m_localhost" for m in peek["messages"])

    def test_ensure_session_roster_from_store(
        self, store: MailboxStore, outbox_root: Path, host_a: HostSpec,
    ) -> None:
        """Bug C 回归：无进程内 roster 缓存时，_ensure_remote_session
        从本地 store 读完整 roster + 真 manager（而非 envelope from/to）。"""
        # store 里建完整 session（kernel 行为：roster 含 manager）
        store.session_init("s1", "mgr", ["mgr", "w1", "w2", "w3"])
        mock_router = MagicMock()
        mock_router.capabilities.return_value = {"mailbox"}
        # transport.mailbox 返回成功（session-init 已存在则 OK）
        mock_router.get.return_value = MagicMock()
        engine = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router,
            outbox_root=outbox_root,
        )
        # 直接调 _remote_send：无缓存 roster，应读 store
        envelope = _make_envelope(from_id="w1", to_id="w2", msg_id="m_roster")
        transport = MagicMock()
        transport.mailbox.return_value = (0, "ok", "")
        with patch.object(engine, "_get_transport", return_value=transport):
            engine._remote_send(host_a, envelope)

        # 两次 mailbox 调用：session-init（全 roster）+ send
        init_call = transport.mailbox.call_args_list[0]
        init_args = init_call.args[1]
        assert init_args[0] == "session-init"
        agents_csv = init_args[init_args.index("--agents") + 1]
        assert set(agents_csv.split(",")) == {"mgr", "w1", "w2", "w3"}
        # manager 用 store 的权威值（mgr），不是 envelope 的 w1
        assert init_args[init_args.index("--manager") + 1] == "mgr"

    def test_resolve_target_reads_repo_map(
        self, store: MailboxStore, outbox_root: Path,
    ) -> None:
        """_resolve_target 命中 repo-map：返回带 ssh_alias/shell_prefix 的 HostSpec。"""
        engine = DeliveryEngine(
            mailbox_store=store,
            transport_router=MagicMock(),
            outbox_root=outbox_root,
        )
        spec = HostSpec(name="yellow", ssh_alias="yellow",
                        hostnames=("mcshyucs192159",),
                        shell_prefix="export PATH=$HOME/.local/bin:$PATH")
        fake_map = MagicMock()
        fake_map.hosts.get.return_value = spec
        with patch("codeagent.config.repo_map.load_repo_map", return_value=fake_map):
            target = engine._resolve_target("yellow")
        assert target.ssh_alias == "yellow"
        assert "$HOME/.local/bin" in target.shell_prefix


# ── store.send msg_id dedup ───────────────────────────────────────────


class TestStoreSendMsgIdDedup:
    def test_explicit_msg_id_used(self, store: MailboxStore) -> None:
        """store.send with explicit msg_id uses that id."""
        _init_session(store)
        result = store.send(
            "s1", "w1", "w2", "sub", "body",
            msg_id="custom_id_123",
        )
        assert "custom_id_123" in result

        # Read from inbox
        msg = store.read("s1", "w2", "reader")
        assert msg is not None
        assert msg["msg_id"] == "custom_id_123"

    def test_explicit_msg_id_duplicate_raises(self, store: MailboxStore) -> None:
        """Duplicate explicit msg_id raises ValueError."""
        _init_session(store)
        store.send("s1", "w1", "w2", "sub1", "body1", msg_id="dup_id")

        with pytest.raises(ValueError, match="msg_id already exists"):
            store.send("s1", "w1", "w2", "sub2", "body2", msg_id="dup_id")

    def test_explicit_msg_id_in_history_dedup(self, store: MailboxStore) -> None:
        """Duplicate msg_id detected even after message read (in history)."""
        _init_session(store)
        store.send("s1", "w1", "w2", "sub", "body", msg_id="hist_dup")

        # Read + archive the message
        store.read("s1", "w2", "r")
        store.finalize("s1", "w2", "hist_dup", "r")

        # Same msg_id should still be rejected (exists in history)
        with pytest.raises(ValueError, match="msg_id already exists"):
            store.send("s1", "w1", "w2", "sub2", "body2", msg_id="hist_dup")

    def test_explicit_msg_id_path_traversal_rejected(self, store: MailboxStore) -> None:
        """Path traversal in msg_id is rejected."""
        _init_session(store)
        with pytest.raises(ValueError, match="invalid msg_id"):
            store.send("s1", "w1", "w2", "sub", "body", msg_id="../escape")

    def test_none_msg_id_generates_automatically(self, store: MailboxStore) -> None:
        """msg_id=None generates an automatic id (backwards compat)."""
        _init_session(store)
        result = store.send("s1", "w1", "w2", "sub", "body")
        assert "sent" in result

    def test_no_msg_id_param_backwards_compat(self, store: MailboxStore) -> None:
        """Calling without msg_id parameter works exactly as before."""
        _init_session(store)
        result = store.send("s1", "w1", "w2", "subject", "body")
        assert "sent" in result
        msg = store.read("s1", "w2", "r")
        assert msg is not None
        assert msg["subject"] == "subject"


# ── Integration: deliver + store.send with msg_id ──────────────────────


class TestIntegrationDeliveryStore:
    def test_end_to_end_with_store_dedup(
        self, store: MailboxStore, outbox_root: Path, host_a: HostSpec,
    ) -> None:
        """Full flow: outbox write → remote store.send with msg_id → dedup."""
        _init_session(store)
        envelope = _make_envelope()
        mid = envelope["msg_id"]

        # Track what msg_id was sent remotely
        sent_msg_ids: list[str] = []

        mock_router = MagicMock()
        mock_transport = MagicMock()
        def capture_mailbox(host, args, **kw):
            # Find --msg-id in args
            for i, arg in enumerate(args):
                if arg == "--msg-id" and i + 1 < len(args):
                    sent_msg_ids.append(args[i + 1])
            return 0, "sent", ""
        mock_transport.mailbox = capture_mailbox
        mock_router.get.return_value = mock_transport
        mock_router.capabilities.return_value = {"mailbox"}

        engine = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router,
            outbox_root=outbox_root,
        )

        r = engine.deliver("s1", host_a, envelope)
        assert r.status == "delivered"
        assert len(sent_msg_ids) == 1
        assert sent_msg_ids[0] == mid


# ── E2E: kernel → DeliveryEngine → transport → remote inbox ──────────


class TestE2ECrossHostDeliveryChain:
    """End-to-end: kernel.direct() → DeliveryEngine (outbox fsync)
    → transport.mailbox() call → remote inbox → ack → finalize.

    This test exercises the full cross-host delivery chain so that
    the "zero import" failure mode (P0-1) cannot recur undetected.
    """

    def test_e2e_cross_host_delivery_chain(self, tmp_path: Path) -> None:
        # ── 1. Setup: store, engine, kernel wired with engine sink ──────
        store = MailboxStore(root=tmp_path)
        outbox_root = tmp_path / "outbox"

        mock_router = MagicMock()
        mock_transport = MagicMock()
        call_count = {"n": 0}

        def fake_mailbox(host, args, **kw):
            """Simulate remote transport: write to remote-w's inbox in the store."""
            # _remote_send 先做幂等 session-init（同一次远程调用），不计入 send 次数
            if "session-init" in args:
                return 0, "session exists", ""
            call_count["n"] += 1
            msg: dict[str, str] = {}
            for i, a in enumerate(args):
                if a.startswith("--") and i + 1 < len(args) and not args[i + 1].startswith("--"):
                    msg[a[2:].replace("-", "_")] = args[i + 1]
            mid = msg.get("msg_id", "")
            if mid:
                msg.setdefault("created_at", "2026-01-01T00:00:00Z")
                msg.setdefault("session_id", "s1")
                inbox_dir = store.agent_subdir("s1", "remote-w", "inbox")
                inbox_dir.mkdir(parents=True, exist_ok=True)
                msg_path = inbox_dir / f"{mid}.json"
                tmp_path_inner = inbox_dir / f".tmp-{mid}.json"
                tmp_path_inner.write_text(
                    json.dumps(msg, indent=2), encoding="utf-8",
                )
                os.replace(str(tmp_path_inner), str(msg_path))
            return 0, "sent", ""

        mock_transport.mailbox.side_effect = fake_mailbox
        mock_router.get.return_value = mock_transport
        mock_router.capabilities.return_value = {"mailbox"}

        engine = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router,
            outbox_root=outbox_root,
        )

        class _EngineSink:
            """Adapter: wraps engine.deliver_sink to match DeliverySink protocol."""
            def deliver(self, session_id, target_agent, envelope, msg_id, created_at, from_id):
                loc = kernel.get_location(session_id, target_agent)
                if loc and loc.host_alias and loc.host_alias != "__local__":
                    host = HostSpec(
                        name=loc.host_alias,
                        ssh_alias=loc.host_alias,
                        hostnames=(loc.host_alias,),
                    )
                    engine.cache_host(target_agent, host)
                return engine.deliver_sink(session_id, target_agent, envelope, msg_id, created_at, from_id)

        kernel = SwarmKernel(store=store, sink=_EngineSink())

        # ── 2. Create session + register remote agent ──────────────────
        kernel.create_session("s1", "mgr", ["remote-w"])
        kernel.register(
            AgentLocation(agent_id="remote-w", host_alias="yellow", backend="cli"),
            "s1",
        )

        # ── 3. Send direct message ─────────────────────────────────────
        env = Envelope(subject="deploy", body="run CI", kind="TASK")
        receipt = kernel.direct("s1", "mgr", "remote-w", env)
        assert receipt.status == "delivered"
        msg_id = receipt.msg_id

        # ── 4. Assert: outbox file exists (fsync durable write) ────────
        outbox_file = outbox_root / "s1" / f"{msg_id}.json"
        assert outbox_file.exists(), "outbox file must be written before transport"
        outbox_data = json.loads(outbox_file.read_text(encoding="utf-8"))
        assert outbox_data["kind"] == "TASK"
        assert outbox_data["subject"] == "deploy"

        # ── 5. Assert: transport.mailbox called exactly once ───────────
        assert call_count["n"] == 1, "transport.mailbox must be called once (no fanout)"
        assert mock_transport.mailbox.call_count == 2  # session-init + send

        # ── 6. Assert: remote inbox has the message ────────────────────
        remote_inbox = store.agent_subdir("s1", "remote-w", "inbox")
        remote_files = store.list_messages(remote_inbox)
        assert len(remote_files) == 1, "remote inbox must contain the delivered message"
        remote_msg = json.loads(remote_files[0].read_bytes())
        assert remote_msg["subject"] == "deploy"
        assert remote_msg["from"] == "mgr"
        assert remote_msg["to"] == "remote-w"

        # ── 7. Assert: ack updates delivery status ─────────────────────
        engine.ack("s1", "remote-w", msg_id, "consumed")
        status_dir = outbox_root / "s1" / f".status-{msg_id}"
        assert status_dir.exists()
        assert (status_dir / "phase").read_text().strip() == "consumed"

        # ── 8. Assert: claim + finalize archives the message ───────────
        # store.read() moves inbox → processing (claim)
        claimed = store.read("s1", "remote-w", owner="remote-w")
        assert claimed is not None
        assert claimed["subject"] == "deploy"
        # store.finalize() moves processing → archive
        store.finalize("s1", "remote-w", msg_id, owner="remote-w")
        archive_dir = store.agent_subdir("s1", "remote-w", "archive")
        archived = store.list_messages(archive_dir)
        assert any(f.stem == msg_id for f in archived), "message must be archived after finalize"

    def test_flush_retries_with_target_host_from_outbox(self, tmp_path: Path) -> None:
        """P0: outbox entries written via EngineDeliverySink keep _target_host,
        so flush() actually retries instead of silently skipping."""
        store = MailboxStore(root=tmp_path)
        outbox_root = tmp_path / "outbox"
        _init_session(store)

        # Simulate EngineDeliverySink path: host known, delivery fails
        router = MagicMock()
        transport = MagicMock()
        calls = {"n": 0}

        def flaky(host, args, **kw):
            if "session-init" in args:
                return 0, "ok", ""
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("network blip")
            return 0, "sent", ""

        transport.mailbox = flaky
        router.get.return_value = transport
        router.capabilities.return_value = {"mailbox"}
        engine = DeliveryEngine(
            mailbox_store=store, transport_router=router, outbox_root=outbox_root,
        )

        env = _make_envelope(msg_id="m_flush_host")
        # EngineDeliverySink records the resolved host into the envelope
        env["_target_host"] = "alpha"
        env["_target_agent"] = "w2"

        receipt = engine.deliver("s1", HostSpec("alpha", "alpha", ("alpha",)), env)
        assert receipt.status == "accepted"  # first attempt failed
        assert calls["n"] == 1

        # flush must pick up _target_host from the outbox and retry
        flushed = engine.flush("s1")
        assert flushed == 1
        assert calls["n"] == 2
        assert (outbox_root / "s1" / ".delivered-m_flush_host").exists()

    def test_relay_host_round_trips_through_resolve_target(self, tmp_path: Path) -> None:
        """relay-login HostSpec preserved via _resolve_target (P0-2 fix)."""
        store = MailboxStore(root=tmp_path)
        outbox_root = tmp_path / "outbox"

        engine = DeliveryEngine(mailbox_store=store, outbox_root=outbox_root)

        relay_host = HostSpec(
            name="jump",
            ssh_alias="jump",
            hostnames=("jump",),
            transport="relay-login",
        )

        # Pre-populate host cache as wiring would
        engine.cache_host("relay-agent", relay_host)

        # _resolve_target should return the cached relay host, not a plain SSH one
        resolved = engine._resolve_target("relay-agent")
        assert resolved.transport == "relay-login"
        assert resolved.ssh_alias == "jump"

        # Verify: an uncached host falls back to minimal SSH spec
        fallback = engine._resolve_target("unknown-agent")
        assert fallback.transport == "ssh"  # HostSpec default
        assert fallback.ssh_alias == "unknown-agent"


# ── EngineDeliverySink receipt propagation ─────────────────────────────


class TestEngineDeliverySinkReceipt:
    def test_deliver_returns_receipt_local(self, store: MailboxStore, outbox_root: Path) -> None:
        """EngineDeliverySink returns receipt with 'delivered' for local target."""
        from codeagent.swarm.delivery import EngineDeliverySink

        _init_session(store)
        engine = DeliveryEngine(mailbox_store=store, outbox_root=outbox_root)
        sink = EngineDeliverySink(engine=engine, kernel=None)
        envelope = _make_envelope()
        receipt = sink.deliver("s1", "w1", envelope, "mid-1", "2026-01-01T00:00:00Z", "mgr")
        assert receipt.status == "delivered"
        assert receipt.queued is False

    def test_deliver_returns_receipt_transport_failure(
        self, store: MailboxStore, outbox_root: Path, host_a: HostSpec,
    ) -> None:
        """EngineDeliverySink returns 'accepted' when transport fails."""
        from codeagent.swarm.delivery import EngineDeliverySink

        _init_session(store)
        mock_router = MagicMock()
        mock_transport = MagicMock()
        mock_transport.mailbox.side_effect = ConnectionError("refused")
        mock_router.get.return_value = mock_transport
        mock_router.capabilities.return_value = {"mailbox"}

        engine = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router,
            outbox_root=outbox_root,
        )
        sink = EngineDeliverySink(engine=engine, kernel=None)
        engine.cache_host("remote-agent", host_a)
        receipt = sink.deliver(
            "s1", "remote-agent", _make_envelope(msg_id="mid-transport-fail"),
            "mid-transport-fail", "2026-01-01T00:00:00Z", "mgr",
        )
        assert receipt.status == "accepted"
        assert receipt.queued is True


# ── outbox_stats ──────────────────────────────────────────────────────


class TestOutboxStats:
    """DeliveryEngine.outbox_stats() returns pending/delivered counts."""

    def test_empty_outbox(self, engine: DeliveryEngine) -> None:
        stats = engine.outbox_stats()
        assert stats == {"pending": 0, "delivered": 0}

    def test_pending_and_delivered_counts(
        self, engine: DeliveryEngine, outbox_root: Path,
    ) -> None:
        """Counts reflect actual outbox state."""
        sd = outbox_root / "s1"
        sd.mkdir(parents=True, exist_ok=True)

        # Two pending envelopes (no .delivered marker)
        (sd / "msg-1.json").write_text(json.dumps(_make_envelope(msg_id="msg-1")))
        (sd / "msg-2.json").write_text(json.dumps(_make_envelope(msg_id="msg-2")))
        # One delivered envelope
        (sd / "msg-3.json").write_text(json.dumps(_make_envelope(msg_id="msg-3")))
        (sd / ".delivered-msg-3").write_text("{}")

        stats = engine.outbox_stats()
        assert stats["pending"] == 2
        assert stats["delivered"] == 1

    def test_session_filter(self, engine: DeliveryEngine, outbox_root: Path) -> None:
        """--session filter scopes counts to one session."""
        for sid in ("s1", "s2"):
            sd = outbox_root / sid
            sd.mkdir(parents=True, exist_ok=True)
            (sd / "m1.json").write_text(json.dumps(_make_envelope(msg_id="m1")))
            (sd / ".delivered-m1").write_text("{}")

        # s1 has 0 pending, 1 delivered; same for s2
        stats = engine.outbox_stats(session_id="s1")
        assert stats == {"pending": 0, "delivered": 1}

    def test_no_sessions(self, engine: DeliveryEngine) -> None:
        """No outbox dir at all."""
        stats = engine.outbox_stats()
        assert stats == {"pending": 0, "delivered": 0}


# ── Session-ensure (B3T3) ─────────────────────────────────────────────


class TestSessionEnsure:
    """B3T3: _ensure_remote_session topology + idempotent session-init."""

    def test_session_ensure_called_once_per_pair(
        self, store: MailboxStore, outbox_root: Path, host_a: HostSpec,
    ) -> None:
        """Second message to same (session, host) skips remote session-init."""
        _init_session(store)
        env1 = _make_envelope(msg_id="m1")
        env2 = _make_envelope(msg_id="m2", from_id="mgr", to_id="w1")

        init_calls = {"n": 0}

        def mock_mailbox(host, args, **kw):
            if "session-init" in args:
                init_calls["n"] += 1
                return 0, "session s1 created", ""
            return 0, "sent", ""

        mock_router = MagicMock()
        mock_transport = MagicMock()
        mock_transport.mailbox = mock_mailbox
        mock_router.get.return_value = mock_transport
        mock_router.capabilities.return_value = {"mailbox"}

        engine = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router,
            outbox_root=outbox_root,
        )
        engine.cache_roster("s1", ["w1", "w2", "mgr"])

        r1 = engine.deliver("s1", host_a, env1)
        assert r1.status == "delivered"
        assert init_calls["n"] == 1

        # Second message to same host — session-init should NOT be called
        r2 = engine.deliver("s1", host_a, env2)
        assert r2.status == "delivered"
        assert init_calls["n"] == 1  # still 1

    def test_session_ensure_full_roster(
        self, store: MailboxStore, outbox_root: Path, host_a: HostSpec,
    ) -> None:
        """session-init carries full roster (all agents), not just target."""
        _init_session(store)
        envelope = _make_envelope()
        captured_args: list[list[str]] = []

        def mock_mailbox(host, args, **kw):
            if "session-init" in args:
                captured_args.append(list(args))
                return 0, "ok", ""
            return 0, "sent", ""

        mock_router = MagicMock()
        mock_transport = MagicMock()
        mock_transport.mailbox = mock_mailbox
        mock_router.get.return_value = mock_transport
        mock_router.capabilities.return_value = {"mailbox"}

        engine = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router,
            outbox_root=outbox_root,
        )
        engine.cache_roster("s1", ["w1", "w2", "mgr"])

        engine.deliver("s1", host_a, envelope)

        assert len(captured_args) == 1
        args = captured_args[0]
        agents_idx = args.index("--agents")
        agents_csv = args[agents_idx + 1]
        assert agents_csv == "mgr,w1,w2"  # full sorted roster

    def test_capability_check_fails_closed(
        self, store: MailboxStore, outbox_root: Path, host_a: HostSpec,
    ) -> None:
        """RuntimeError when remote host lacks 'mailbox' capability."""
        _init_session(store)
        envelope = _make_envelope()

        mock_router = MagicMock()
        mock_router.capabilities.return_value = {"stream"}  # no mailbox!
        mock_transport = MagicMock()
        mock_router.get.return_value = mock_transport

        engine = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router,
            outbox_root=outbox_root,
        )

        # Capability check happens inside deliver → _remote_send → raises
        # RuntimeError, caught by deliver() → accepted+queued
        receipt = engine.deliver("s1", host_a, envelope)
        # Transport failure → accepted+queued (outbox preserved)
        assert receipt.status == "accepted"
        assert receipt.queued is True

    def test_ensure_syncs_acl_from_session_json(
        self, store: MailboxStore, outbox_root: Path, host_a: HostSpec,
    ) -> None:
        """B4-Manifest：session-init 携带 --acl（本地 session.json 权威副本），
        远端 ensure 不再只复制 roster。"""
        store.session_init(
            "s1", "mgr", ["mgr", "w1", "w2"],
            acl={"authority": "mgr", "allowed_senders": ["mgr"],
                 "room_members": ["mgr", "w1", "w2"], "policy": "restricted"},
        )
        envelope = _make_envelope()
        captured: list[list[str]] = []

        def mock_mailbox(host, args, **kw):
            if "session-init" in args:
                captured.append(list(args))
                return 0, "ok", ""
            return 0, "sent", ""

        mock_router = MagicMock()
        mock_transport = MagicMock()
        mock_transport.mailbox = mock_mailbox
        mock_router.get.return_value = mock_transport
        mock_router.capabilities.return_value = {"mailbox"}
        engine = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router,
            outbox_root=outbox_root,
        )
        engine.deliver("s1", host_a, envelope)

        assert len(captured) == 1
        args = captured[0]
        assert "--acl" in args
        acl = json.loads(args[args.index("--acl") + 1])
        assert acl["policy"] == "restricted"
        assert acl["authority"] == "mgr"

    def test_local_session_acl_none_for_legacy(
        self, store: MailboxStore, outbox_root: Path,
    ) -> None:
        """B4-Manifest：无 acl 的旧 session → _local_session_acl 返回 None
        （ensure 跳过 --acl，向后兼容）。"""
        store.session_init("s1", "mgr", ["mgr", "w1"])
        engine = DeliveryEngine(
            mailbox_store=store,
            transport_router=MagicMock(),
            outbox_root=outbox_root,
        )
        assert engine._local_session_acl("s1") is None
        assert engine._local_session_acl("ghost") is None

    def test_flush_remote_success_marks_delivered(
        self, store: MailboxStore, outbox_root: Path, host_a: HostSpec,
    ) -> None:
        """flush 的 remote 分支：transport 成功后 mark delivered + history。"""
        _init_session(store, sid="s1")
        envelope = _make_envelope(msg_id="m_remoteflush", to_id="w2")
        envelope["_target_host"] = "alpha"
        sd = outbox_root / "s1"
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "m_remoteflush.json").write_text(json.dumps(envelope))

        mock_router = MagicMock()
        mock_router.capabilities.return_value = {"mailbox"}
        mock_transport = MagicMock()
        mock_transport.mailbox.return_value = (0, "sent", "")
        mock_router.get.return_value = mock_transport
        engine = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router,
            outbox_root=outbox_root,
        )
        with patch("codeagent.domain.resolve_is_local", return_value=False):
            count = engine.flush(session_id="s1")

        assert count == 1
        assert (sd / ".delivered-m_remoteflush").exists()
        mock_transport.mailbox.assert_called()
