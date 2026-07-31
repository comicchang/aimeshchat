"""Release gate tests — comprehensive integration coverage for all topologies.

Gated on ``--run-integration`` flag (same as test_integration.py).
Tests that require real SSH skip gracefully when localhost key is absent.

Topology coverage:
    - localhost SSH: end-to-end swarm direct via SSHTransport
    - no-Syncthing: two mailbox roots on same host (LocalTransport)
    - disconnect/replay: mock transport failure → outbox pending → flush
    - duplicate delivery: same msg_id twice → one inbox write
    - private/broadcast/channel: ACL + fanout via kernel
    - artifact: attachment descriptor in envelope + verify_artifact
    - relay: TransportRouter returns RelayTransport for relay-login host
    - Tmux/OMP: script exists + hooks importable
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from codeagent.mailbox.protocol import AttachmentRef
from codeagent.mailbox.store import MailboxStore
from codeagent.swarm.kernel import LocalDeliverySink, SwarmKernel
from codeagent.swarm.model import (
    ACL,
    Address,
    AddressKind,
    AgentLocation,
    Channel,
    Envelope,
    Roster,
    Session,
)
from codeagent.transport.router import TransportRouter


# ── Integration gating ─────────────────────────────────────────────────

requires_integration = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="integration tests disabled (use --run-integration)",
)


def _localhost_ssh_available() -> bool:
    """Return True if ``ssh -o BatchMode=yes localhost`` works."""
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
             "localhost", "true"],
            capture_output=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


def _remote_exec_available() -> bool:
    """Return True if codeagent-remote-exec is accessible via localhost SSH."""
    if not _localhost_ssh_available():
        return False
    # Check if codeagent-remote-exec is on the SSH session PATH
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
             "localhost", "codeagent-remote-exec", "--help"],
            capture_output=True, timeout=10,
        )
        if r.returncode == 0:
            return True
    except Exception:
        pass
    # Fallback: try python -m via SSH
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
             "localhost", "python3", "-m", "codeagent.remote_exec", "--help"],
            capture_output=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


requires_localhost_ssh = pytest.mark.skipif(
    not _remote_exec_available(),
    reason="localhost SSH or codeagent-remote-exec not available",
)


# ── Fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path: Path) -> MailboxStore:
    return MailboxStore(root=tmp_path)


@pytest.fixture
def kernel(store: MailboxStore) -> SwarmKernel:
    return SwarmKernel(store=store)


def _env(subject: str = "test", body: str = "hello", kind: str = "TASK",
         attachments: list[AttachmentRef] | None = None) -> Envelope:
    return Envelope(subject=subject, body=body, kind=kind,
                    attachments=attachments or [])


def _setup_session(kernel: SwarmKernel,
                   sid: str = "s1") -> None:
    """Create session and register 3 agents (mgr + w1 + w2)."""
    kernel.create_session(sid, "mgr", ["w1", "w2"])
    for aid in ("mgr", "w1", "w2"):
        kernel.register(
            AgentLocation(agent_id=aid, host_alias="__local__", backend="cli"),
            sid,
        )


def _make_envelope_dict(
    msg_id: str = "gate-001",
    from_id: str = "mgr",
    to_id: str = "w1",
    subject: str = "test",
    body: str = "body",
    target_host: str = "",
) -> dict[str, Any]:
    """Build an envelope dict compatible with DeliveryEngine.deliver()."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    d: dict[str, Any] = {
        "session_id": "s1",
        "from": from_id,
        "to": to_id,
        "subject": subject,
        "body": body,
        "kind": "TASK",
        "msg_id": msg_id,
        "created_at": now,
    }
    if target_host:
        d["_target_host"] = target_host
    return d


# ══════════════════════════════════════════════════════════════════════
# 1. No-Syncthing: two mailbox roots on same host
# ══════════════════════════════════════════════════════════════════════

class TestNoSyncthingLocalDelivery:
    """Two separate mailbox roots simulate hosts without shared filesystem.

    Each store has its own independent session; messages written to one
    are invisible to the other — demonstrating filesystem-level isolation.
    """

    def test_two_roots_isolated(self, tmp_path: Path) -> None:
        root_a = tmp_path / "host_a"
        root_b = tmp_path / "host_b"
        store_a = MailboxStore(root=root_a)
        store_b = MailboxStore(root=root_b)

        # Both stores create the same session
        store_a.session_init("s1", "mgr", ["w1"])
        store_b.session_init("s1", "mgr", ["w1"])

        # Send a message on store_a
        store_a.send("s1", "mgr", "w1", "cross-host", "no syncthing",
                     msg_id="ns-001")

        # w1 on store_a sees the message
        peek_a = store_a.peek("s1", "w1")
        assert peek_a["pending"] == 1
        assert peek_a["messages"][0]["subject"] == "cross-host"

        # w1 on store_b has nothing — independent root
        peek_b = store_b.peek("s1", "w1")
        assert peek_b["pending"] == 0

    def test_kernel_delivery_stays_local(self, tmp_path: Path) -> None:
        """SwarmKernel with LocalDeliverySink writes to the store's root."""
        root_a = tmp_path / "mbox"
        store = MailboxStore(root=root_a)
        kernel = SwarmKernel(store=store)
        _setup_session(kernel)

        receipt = kernel.direct("s1", "mgr", "w1", _env(subject="local-only"))
        assert receipt.status == "delivered"

        peek = store.peek("s1", "w1")
        assert peek["pending"] == 1
        assert peek["messages"][0]["subject"] == "local-only"


# ══════════════════════════════════════════════════════════════════════
# 2. Disconnect / Replay
# ══════════════════════════════════════════════════════════════════════

class TestDisconnectReplay:
    """Transport failure → outbox retains pending → flush succeeds on retry."""

    def test_outbox_pending_after_failure(self, tmp_path: Path) -> None:
        from codeagent.swarm.delivery import DeliveryEngine
        from codeagent.domain import HostSpec

        store = MailboxStore(root=tmp_path / "mbox")
        store.session_init("s1", "mgr", ["w1"])
        outbox = tmp_path / "outbox"

        # Mock router that fails
        mock_router = MagicMock()
        failing_transport = MagicMock()
        failing_transport.mailbox.side_effect = ConnectionError("SSH disconnected")
        mock_router.get.return_value = failing_transport

        host = HostSpec(name="remote", ssh_alias="remote",
                        hostnames=("remote",))
        engine = DeliveryEngine(
            mailbox_store=store,
            transport_router=mock_router,
            outbox_root=outbox,
        )

        envelope = _make_envelope_dict(msg_id="dr-001", subject="will-fail",
                                      target_host="remote")
        receipt = engine.deliver("s1", host, envelope)

        # Accepted and queued (transport failure doesn't reject)
        assert receipt.status == "accepted"
        assert receipt.queued

        # Pending list has the failed message
        pending = engine.pending("s1")
        assert len(pending) >= 1
        msg_ids = [p.get("msg_id", "") for p in pending]
        assert "dr-001" in msg_ids

    def test_flush_after_recovery(self, tmp_path: Path) -> None:
        from codeagent.swarm.delivery import DeliveryEngine
        from codeagent.domain import HostSpec

        store = MailboxStore(root=tmp_path / "mbox")
        store.session_init("s1", "mgr", ["w1"])
        outbox = tmp_path / "outbox"

        host = HostSpec(name="remote", ssh_alias="remote",
                        hostnames=("remote",))

        # Phase 1: fail
        mock_router = MagicMock()
        failing = MagicMock()
        failing.mailbox.side_effect = ConnectionError("down")
        mock_router.get.return_value = failing

        engine = DeliveryEngine(
            mailbox_store=store, transport_router=mock_router,
            outbox_root=outbox,
        )
        envelope = _make_envelope_dict(msg_id="dr-002", subject="retry-me",
                                      target_host="remote")
        r1 = engine.deliver("s1", host, envelope)
        assert r1.queued
        assert len(engine.pending("s1")) >= 1

        # Phase 2: fix transport, flush
        working = MagicMock()
        mock_router.get.return_value = working
        working.mailbox.return_value = (0, "ok", "")

        flushed = engine.flush("s1")
        assert flushed >= 1


# ══════════════════════════════════════════════════════════════════════
# 3. Duplicate delivery
# ══════════════════════════════════════════════════════════════════════

class TestDuplicateDelivery:
    """Same msg_id twice → one inbox entry (idempotent)."""

    def test_store_send_rejects_duplicate_msg_id(self, store: MailboxStore) -> None:
        """MailboxStore.send raises on pre-existing msg_id (strict)."""
        store.session_init("s1", "mgr", ["w1"])
        store.send("s1", "mgr", "w1", "first", "body1", msg_id="dup-001")
        with pytest.raises(ValueError, match="msg_id already exists"):
            store.send("s1", "mgr", "w1", "second", "body2", msg_id="dup-001")

        peek = store.peek("s1", "w1")
        assert peek["pending"] == 1
        assert peek["messages"][0]["subject"] == "first"

    def test_delivery_engine_dedup(self, tmp_path: Path) -> None:
        """DeliveryEngine.deliver is idempotent — returns cached receipt on retry."""
        from codeagent.swarm.delivery import DeliveryEngine

        store = MailboxStore(root=tmp_path / "mbox")
        store.session_init("s1", "mgr", ["w1"])
        outbox = tmp_path / "outbox"
        engine = DeliveryEngine(mailbox_store=store, outbox_root=outbox)

        envelope = _make_envelope_dict(msg_id="idem-001")
        r1 = engine.deliver("s1", "w1", envelope)
        r2 = engine.deliver("s1", "w1", envelope)

        assert r1.msg_id == "idem-001"
        assert r2.msg_id == "idem-001"
        # Second call returns cached receipt — no duplicate outbox write
        outbox_files = list((outbox / "s1").glob("*.json"))
        assert len(outbox_files) == 1


# ══════════════════════════════════════════════════════════════════════
# 4. Private / Broadcast / Channel — ACL + fanout
# ══════════════════════════════════════════════════════════════════════

class TestPrivateBroadcastChannel:
    """Kernel routing: direct (private), broadcast, channel with ACL."""

    def test_private_direct(self, kernel: SwarmKernel, store: MailboxStore) -> None:
        _setup_session(kernel)
        receipt = kernel.direct("s1", "mgr", "w1", _env())
        assert receipt.msg_id
        assert receipt.target == "w1"
        peek_w1 = store.peek("s1", "w1")
        peek_w2 = store.peek("s1", "w2")
        assert peek_w1["pending"] == 1
        assert peek_w2["pending"] == 0

    def test_broadcast_fanout(self, kernel: SwarmKernel, store: MailboxStore) -> None:
        _setup_session(kernel)
        receipts = kernel.broadcast("s1", "mgr", _env())
        # Broadcast to all except sender: w1 + w2 = 2
        assert len(receipts) == 2
        targets = {r.recipient for r in receipts}
        assert targets == {"w1", "w2"}
        # Sender's inbox is empty
        peek_mgr = store.peek("s1", "mgr")
        assert peek_mgr["pending"] == 0
        # Recipients each got one
        assert store.peek("s1", "w1")["pending"] == 1
        assert store.peek("s1", "w2")["pending"] == 1

    def test_channel_with_acl(self, kernel: SwarmKernel, store: MailboxStore) -> None:
        _setup_session(kernel)
        kernel.create_channel("s1", "secret-room", members=["mgr", "w1"])
        receipt = kernel.channel("s1", "mgr", "secret-room", _env())
        assert receipt.msg_id
        # w2 is not in the channel
        assert store.peek("s1", "w2")["pending"] == 0
        # w1 is in the channel
        assert store.peek("s1", "w1")["pending"] == 1

    def test_acl_blocks_non_member(self, kernel: SwarmKernel) -> None:
        _setup_session(kernel)
        kernel.create_channel("s1", "vip", members=["mgr"])
        with pytest.raises(PermissionError):
            kernel.channel("s1", "w1", "vip", _env())

    def test_notice_requires_authority(self, kernel: SwarmKernel) -> None:
        """Non-allowed sender cannot send notice."""
        _setup_session(kernel)
        # Create a restricted session: only mgr in allowed_senders
        kernel.create_session("restricted", "mgr", ["w1", "w2"],
                              acl=ACL(authority="mgr",
                                      allowed_senders=["mgr"],
                                      room_members=["mgr", "w1", "w2"],
                                      policy="closed"))
        for aid in ("mgr", "w1", "w2"):
            kernel.register(
                AgentLocation(agent_id=aid, host_alias="__local__",
                              backend="cli"),
                "restricted",
            )
        with pytest.raises(PermissionError):
            kernel.notice("restricted", "w1", "system", _env())

    def test_broadcast_requires_authority_closed_policy(
        self, kernel: SwarmKernel,
    ) -> None:
        """In closed policy, only authority can broadcast."""
        kernel.create_session("closed", "mgr", ["w1"],
                              acl=ACL(authority="mgr",
                                      allowed_senders=["mgr", "w1"],
                                      room_members=["mgr", "w1"],
                                      policy="closed"))
        for aid in ("mgr", "w1"):
            kernel.register(
                AgentLocation(agent_id=aid, host_alias="__local__",
                              backend="cli"),
                "closed",
            )
        with pytest.raises(PermissionError):
            kernel.broadcast("closed", "w1", _env())


# ══════════════════════════════════════════════════════════════════════
# 5. Artifact: attachment descriptor + verify
# ══════════════════════════════════════════════════════════════════════

class TestArtifactDescriptor:
    """Attachment descriptor in envelope + SHA256 verification."""

    def test_attachment_in_envelope(self, kernel: SwarmKernel,
                                    store: MailboxStore,
                                    tmp_path: Path) -> None:
        _setup_session(kernel)

        content = b'{"score": 0.95}'
        sha = hashlib.sha256(content).hexdigest()

        att = AttachmentRef(
            artifact_id="art-gate-1",
            source_host="__local__",
            remote_root=str(tmp_path),
            relative_path="result.json",
            size=len(content),
            sha256=sha,
        )
        receipt = kernel.direct("s1", "mgr", "w1",
                                _env(subject="results",
                                     attachments=[att]))
        assert receipt.msg_id

        # Read the raw message from the inbox to check attachments
        inbox_dir = store.agent_subdir("s1", "w1", "inbox")
        files = store.list_messages(inbox_dir)
        assert len(files) == 1
        msg = json.loads(files[0].read_bytes())
        assert "attachments" in msg
        assert len(msg["attachments"]) == 1
        assert msg["attachments"][0]["artifact_id"] == "art-gate-1"
        assert msg["attachments"][0]["sha256"] == sha

    def test_verify_artifact(self, tmp_path: Path) -> None:
        from codeagent.artifact import verify_artifact

        content = b"hello world"
        p = tmp_path / "test.bin"
        p.write_bytes(content)
        sha = hashlib.sha256(content).hexdigest()

        assert verify_artifact(p, sha, len(content)) is True
        # Mismatch raises ValueError
        with pytest.raises(ValueError, match="size mismatch"):
            verify_artifact(p, sha, len(content) + 1)
        with pytest.raises(ValueError, match="sha256 mismatch"):
            verify_artifact(p, "0" * 64, len(content))


# ══════════════════════════════════════════════════════════════════════
# 6. Relay: TransportRouter returns RelayTransport
# ══════════════════════════════════════════════════════════════════════

class TestRelayTransportRouting:
    """TransportRouter returns RelayTransport for relay-login host."""

    def test_relay_host_returns_relay_transport(self, tmp_path: Path) -> None:
        from codeagent.domain import HostSpec
        from codeagent.transport.relay import RelayTransport

        # Create a fake relay_zsh script
        zsh = tmp_path / "relay.zsh"
        zsh.write_text("# relay script\n")

        host = HostSpec(
            name="cf-gateway", ssh_alias="cf-gw", hostnames=("cf-gw",),
            transport="relay-login",
        )
        repo_map = MagicMock()
        repo_map.relay_zsh = str(zsh)

        router = TransportRouter()
        transport = router.get(host, repo_map)
        assert isinstance(transport, RelayTransport)

    def test_relay_capabilities_mailbox_only(self) -> None:
        from codeagent.domain import HostSpec

        host = HostSpec(
            name="cf-gateway", ssh_alias="cf-gw", hostnames=("cf-gw",),
            transport="relay-login",
        )
        router = TransportRouter()
        caps = router.capabilities(host)
        assert caps == {"mailbox"}
        assert "stream" not in caps
        assert "artifact" not in caps

    def test_ssh_capabilities_full(self) -> None:
        from codeagent.domain import HostSpec

        host = HostSpec(name="dev", ssh_alias="dev", hostnames=("dev",))
        router = TransportRouter()
        caps = router.capabilities(host)
        assert caps == {"mailbox", "stream", "artifact"}


# ══════════════════════════════════════════════════════════════════════
# 7. Tmux / OMP: script exists + hooks importable
# ══════════════════════════════════════════════════════════════════════

class TestTmuxOMPHooks:
    """Tmux watch script exists; OMP hooks are importable."""

    def test_swarm_watch_script_exists(self) -> None:
        script = (Path(__file__).resolve().parent.parent
                  / "scripts" / "tmux" / "swarm-watch.sh")
        assert script.exists(), f"tmux watch script not found: {script}"
        assert os.access(script, os.X_OK), f"script not executable: {script}"

    def test_swarm_watch_script_has_usage(self) -> None:
        script = (Path(__file__).resolve().parent.parent
                  / "scripts" / "tmux" / "swarm-watch.sh")
        content = script.read_text()
        assert "codeagent swarm watch" in content
        assert "SESSION_ID" in content

    def test_swarm_hooks_importable(self) -> None:
        from codeagent.hooks import swarm_hooks
        assert callable(swarm_hooks.on_agent_start)
        assert callable(swarm_hooks.on_agent_message)
        assert callable(swarm_hooks.on_agent_stop)

    def test_swarm_hooks_lifecycle(self, tmp_path: Path) -> None:
        from codeagent.hooks import swarm_hooks
        swarm_hooks.reset()

        # We need to create the session in the kernel first.
        # The hooks use a module-level singleton kernel, so we need to
        # get it, create the session, then use the hooks.
        kernel, store = swarm_hooks._get_kernel(tmp_path)
        kernel.create_session("hook-s1", "mgr", ["w1", "w2"])

        # Register via hook
        result = swarm_hooks.on_agent_start(
            session_id="hook-s1", agent_id="w1",
            host_alias="__local__", backend="omp",
            store_root=tmp_path,
        )
        assert result["agent_id"] == "w1"
        assert result["session_id"] == "hook-s1"

        # Register mgr too (needed as sender)
        swarm_hooks.on_agent_start(
            session_id="hook-s1", agent_id="mgr",
            host_alias="__local__", backend="omp",
            store_root=tmp_path,
        )

        # Send via hook
        msg_result = swarm_hooks.on_agent_message(
            session_id="hook-s1", agent_id="mgr",
            msg_dict={
                "to": "w1", "kind": "TASK", "subject": "from-hook",
                "body": "via OMP",
            },
            store_root=tmp_path,
        )
        assert "msg_id" in msg_result
        assert msg_result["target"] == "w1"

        # Unregister via hook
        stop_result = swarm_hooks.on_agent_stop(
            session_id="hook-s1", agent_id="w1",
            store_root=tmp_path,
        )
        assert stop_result["unregistered"]

        swarm_hooks.reset()


# ══════════════════════════════════════════════════════════════════════
# 8. Localhost SSH: end-to-end swarm via SSH wire
# ══════════════════════════════════════════════════════════════════════

@requires_integration
@requires_localhost_ssh
class TestLocalhostSSHSwarm:
    """End-to-end swarm delivery over localhost SSH.

    Requires both --run-integration and a working localhost SSH key.
    """

    def test_ssh_mailbox_send_peek(self) -> None:
        """Send a mailbox message to localhost via SSH, then peek it."""
        from codeagent.domain import HostSpec
        from codeagent.transport.ssh import SSHTransport

        host = HostSpec(
            name="localhost", ssh_alias="localhost",
            hostnames=("localhost",),
        )
        transport = SSHTransport()

        try:
            transport.warm(host)

            # session-init
            code, out, err = transport.mailbox(
                host,
                ["session-init", "--session", "ssh-gate",
                 "--manager", "mgr", "--agents", "w1"],
            )
            assert code == 0, f"session-init failed: {err}"

            # send
            code, out, err = transport.mailbox(
                host,
                ["send", "--session", "ssh-gate", "--from", "mgr",
                 "--to", "w1", "--kind", "TASK", "--subject", "ssh-test",
                 "--body", "via CM"],
            )
            assert code == 0, f"send failed: {err}"

            # peek
            code, out, err = transport.mailbox(
                host,
                ["peek", "--session", "ssh-gate", "--agent", "w1"],
            )
            assert code == 0, f"peek failed: {err}"
            assert "ssh-test" in out or "via CM" in out
        finally:
            transport.stop(host)
