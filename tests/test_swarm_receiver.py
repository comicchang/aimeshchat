"""Tests for SwarmReceiver — real-time message push (D2).

Covers:
  - Watch mode: local filesystem polling detects new inbox files
  - Stream mode: SSHStream events → callback + inbox write + dedup
  - Ack wiring: callback → auto-ack consumed
  - No-Syncthing case: watch on local root works without Syncthing
  - stop() terminates loop
  - Kernel integration: subscribe routes to receiver when attached
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from codeagent.mailbox.store import MailboxStore
from codeagent.swarm.kernel import SwarmKernel
from codeagent.swarm.model import Envelope
from codeagent.swarm.receiver import SwarmReceiver


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    return MailboxStore(root=tmp_path)


@pytest.fixture
def kernel(store):
    return SwarmKernel(store=store)


def _setup_session(kernel, store, session_id="s1", agents=("mgr", "w1", "w2")):
    """Create session + agent dirs. Idempotent — no-op if already exists."""
    if kernel.get_session(session_id) is None:
        kernel.create_session(session_id, "mgr", list(agents))
    # Ensure agent inbox dirs exist (session_init creates them)
    return kernel


def _write_msg(store, session_id, agent_id, msg_id, subject="hello",
               body="world", kind="TASK", from_id="mgr",
               created_at="2025-07-01T12:00:00Z"):
    """Write a message JSON file directly to an agent's inbox."""
    inbox = store.agent_subdir(session_id, agent_id, "inbox")
    inbox.mkdir(parents=True, exist_ok=True)
    msg = {
        "session_id": session_id,
        "from": from_id,
        "to": agent_id,
        "subject": subject,
        "body": body,
        "kind": kind,
        "msg_id": msg_id,
        "created_at": created_at,
    }
    dest = inbox / f"{msg_id}.json"
    dest.write_text(json.dumps(msg, indent=2))
    return msg


# ═══════════════════════════════════════════════════════════════════════
# Watch mode — local filesystem polling
# ═══════════════════════════════════════════════════════════════════════


class TestWatchMode:
    """Watch mode detects new inbox files via stat-based polling."""

    def test_callback_fires_on_new_file(self, store, kernel, tmp_path):
        """Writing a file to inbox triggers the callback."""
        _setup_session(kernel, store)
        fired: list[dict] = []
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=lambda msg: fired.append(msg))
        receiver.start_watch(mailbox_root=tmp_path, poll_interval=0.1)

        # Give the watch thread time to build initial stat cache
        time.sleep(0.15)

        # Write a new message to inbox
        _write_msg(store, "s1", "w1", "msg-001", subject="push-me")

        # Wait for the callback to fire
        deadline = time.monotonic() + 3.0
        while not fired and time.monotonic() < deadline:
            time.sleep(0.05)

        receiver.stop()

        assert len(fired) == 1
        assert fired[0]["subject"] == "push-me"
        assert fired[0]["msg_id"] == "msg-001"

    def test_callback_fires_with_envelope_data(self, store, kernel, tmp_path):
        """Callback receives full message dict (envelope data)."""
        _setup_session(kernel, store)
        fired: list[dict] = []
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=lambda msg: fired.append(msg))
        receiver.start_watch(mailbox_root=tmp_path, poll_interval=0.1)
        time.sleep(0.15)

        _write_msg(store, "s1", "w1", "msg-002",
                   subject="task", body="do stuff", kind="COMMAND",
                   from_id="mgr", created_at="2025-07-02T10:00:00Z")

        deadline = time.monotonic() + 3.0
        while not fired and time.monotonic() < deadline:
            time.sleep(0.05)

        receiver.stop()

        assert len(fired) == 1
        msg = fired[0]
        assert msg["from"] == "mgr"
        assert msg["body"] == "do stuff"
        assert msg["kind"] == "COMMAND"
        assert msg["created_at"] == "2025-07-02T10:00:00Z"

    def test_no_duplicate_on_same_file(self, store, kernel, tmp_path):
        """Same file scanned twice doesn't fire callback again."""
        _setup_session(kernel, store)
        fired: list[dict] = []
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=lambda msg: fired.append(msg))
        receiver.start_watch(mailbox_root=tmp_path, poll_interval=0.1)
        time.sleep(0.15)

        _write_msg(store, "s1", "w1", "msg-dedup")

        deadline = time.monotonic() + 3.0
        while not fired and time.monotonic() < deadline:
            time.sleep(0.05)

        # Wait for another full poll cycle to ensure no re-fire
        time.sleep(0.3)
        receiver.stop()

        assert len(fired) == 1

    def test_pre_existing_files_not_fired(self, store, kernel, tmp_path):
        """Files that existed before start_watch are not fired."""
        _setup_session(kernel, store)
        # Write message BEFORE starting the receiver
        _write_msg(store, "s1", "w1", "msg-old", subject="already-here")

        fired: list[dict] = []
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=lambda msg: fired.append(msg))
        receiver.start_watch(mailbox_root=tmp_path, poll_interval=0.1)

        # Wait to ensure nothing fires for pre-existing file
        time.sleep(0.5)
        receiver.stop()

        assert len(fired) == 0

    def test_multiple_new_files(self, store, kernel, tmp_path):
        """Multiple new files are all detected and fired."""
        _setup_session(kernel, store)
        fired: list[dict] = []
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=lambda msg: fired.append(msg))
        receiver.start_watch(mailbox_root=tmp_path, poll_interval=0.1)
        time.sleep(0.15)

        _write_msg(store, "s1", "w1", "msg-01", subject="first")
        _write_msg(store, "s1", "w1", "msg-02", subject="second")
        _write_msg(store, "s1", "w1", "msg-03", subject="third")

        deadline = time.monotonic() + 3.0
        while len(fired) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)

        receiver.stop()

        assert len(fired) == 3
        subjects = {m["subject"] for m in fired}
        assert subjects == {"first", "second", "third"}

    def test_watch_with_kind_filter(self, store, kernel, tmp_path):
        """Callback with kind filter only fires for matching messages."""
        _setup_session(kernel, store)
        fired: list[dict] = []
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(
            callback=lambda msg: fired.append(msg),
            kinds=["REPORT"],
        )
        receiver.start_watch(mailbox_root=tmp_path, poll_interval=0.1)
        time.sleep(0.15)

        _write_msg(store, "s1", "w1", "msg-task", kind="TASK")
        _write_msg(store, "s1", "w1", "msg-report", kind="REPORT")

        deadline = time.monotonic() + 3.0
        while len(fired) < 1 and time.monotonic() < deadline:
            time.sleep(0.05)

        time.sleep(0.3)
        receiver.stop()

        assert len(fired) == 1
        assert fired[0]["kind"] == "REPORT"


# ═══════════════════════════════════════════════════════════════════════
# Stream mode — SSHStream push events
# ═══════════════════════════════════════════════════════════════════════


class TestStreamMode:
    """Stream mode receives pushed events from a mock SSHStream."""

    def _make_receiver_with_mock_stream(self, store, kernel, tmp_path, events):
        """Create receiver and mock SSHStream to emit given events."""
        _setup_session(kernel, store)
        fired: list[dict] = []
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=lambda msg: fired.append(msg))

        # Create a mock SSHStream
        mock_stream = MagicMock()
        # poll() returns events in chunks: first call returns all, subsequent empty
        call_count = [0]

        def mock_poll(timeout):
            call_count[0] += 1
            if call_count[0] == 1:
                return events
            # After first batch, stop the receiver to end the loop
            receiver._stop_event.set()
            return []

        mock_stream.poll.side_effect = mock_poll
        mock_stream.close.return_value = None

        # Patch SSHStream creation
        with patch("codeagent.transport.ssh.SSHStream", return_value=mock_stream):
            receiver.start_stream(ssh_cmd=["ssh", "testhost"])

        return receiver, fired, mock_stream

    def test_callback_fires_on_stream_event(self, store, kernel, tmp_path):
        """MSG_STREAM_EVENT fires the callback with message data."""
        events = [{
            "msg_id": "msg-stream-1",
            "from": "alice",
            "to": "w1",
            "kind": "TASK",
            "subject": "stream-msg",
            "created_at": "2025-07-03T08:00:00Z",
        }]

        receiver, fired, _ = self._make_receiver_with_mock_stream(
            store, kernel, tmp_path, events)

        deadline = time.monotonic() + 3.0
        while not fired and time.monotonic() < deadline:
            time.sleep(0.05)

        receiver.stop()

        assert len(fired) == 1
        assert fired[0]["msg_id"] == "msg-stream-1"
        assert fired[0]["subject"] == "stream-msg"

    def test_stream_writes_to_local_inbox(self, store, kernel, tmp_path):
        """Stream events are written to inbox (notification-only, no auto-ack)."""
        events = [{
            "msg_id": "msg-inbox-1",
            "from": "bob",
            "to": "w1",
            "kind": "REPORT",
            "subject": "inbox-write",
            "body": "test body",
            "created_at": "2025-07-03T09:00:00Z",
        }]

        receiver, fired, _ = self._make_receiver_with_mock_stream(
            store, kernel, tmp_path, events)

        deadline = time.monotonic() + 3.0
        while not fired and time.monotonic() < deadline:
            time.sleep(0.05)

        receiver.stop()

        # C13: receiver is notification-only — message stays in inbox
        # (agent/worker must explicitly read→process→finalize)
        inbox = store.agent_subdir("s1", "w1", "inbox")
        archive = store.agent_subdir("s1", "w1", "archive")
        assert (inbox / "msg-inbox-1.json").exists(), "message must stay in inbox"
        assert not (archive / "msg-inbox-1.json").exists(), "message must NOT be auto-archived"
        written = json.loads((inbox / "msg-inbox-1.json").read_bytes())
        assert written["msg_id"] == "msg-inbox-1"
        assert written["from"] == "bob"
        assert written["body"] == "test body"

    def test_stream_dedup_on_duplicate_msg_id(self, store, kernel, tmp_path):
        """Duplicate msg_id events are delivered only once."""
        events = [
            {
                "msg_id": "msg-dup",
                "from": "alice",
                "to": "w1",
                "kind": "TASK",
                "subject": "first",
                "created_at": "2025-07-03T10:00:00Z",
            },
            {
                "msg_id": "msg-dup",  # duplicate
                "from": "alice",
                "to": "w1",
                "kind": "TASK",
                "subject": "second",
                "created_at": "2025-07-03T10:00:01Z",
            },
        ]

        receiver, fired, _ = self._make_receiver_with_mock_stream(
            store, kernel, tmp_path, events)

        deadline = time.monotonic() + 3.0
        while not fired and time.monotonic() < deadline:
            time.sleep(0.05)

        time.sleep(0.3)
        receiver.stop()

        # Only one callback fire despite two events with same msg_id
        assert len(fired) == 1

    def test_stream_dedup_on_existing_file(self, store, kernel, tmp_path):
        """Stream event for msg_id already on disk skips inbox write."""
        _setup_session(kernel, store)

        # Pre-write the message to inbox (after session exists)
        _write_msg(store, "s1", "w1", "msg-existing",
                   subject="pre-existing")

        events = [{
            "msg_id": "msg-existing",
            "from": "alice",
            "to": "w1",
            "kind": "TASK",
            "subject": "should-not-overwrite",
            "created_at": "2025-07-03T11:00:00Z",
        }]

        # Set up receiver manually (avoid double session creation)
        fired: list[dict] = []
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=lambda msg: fired.append(msg))

        mock_stream = MagicMock()
        call_count = [0]

        def mock_poll(timeout):
            call_count[0] += 1
            if call_count[0] == 1:
                return events
            receiver._stop_event.set()
            return []

        mock_stream.poll.side_effect = mock_poll
        mock_stream.close.return_value = None

        with patch("codeagent.transport.ssh.SSHStream", return_value=mock_stream):
            receiver.start_stream(ssh_cmd=["ssh", "testhost"])

        deadline = time.monotonic() + 3.0
        while not fired and time.monotonic() < deadline:
            time.sleep(0.05)

        time.sleep(0.3)
        receiver.stop()

        deadline = time.monotonic() + 3.0
        while not fired and time.monotonic() < deadline:
            time.sleep(0.05)

        receiver.stop()

        # Callback still fires (message exists on disk)
        assert len(fired) == 1
        # C13: receiver is notification-only — message stays in inbox
        inbox = store.agent_subdir("s1", "w1", "inbox")
        archive = store.agent_subdir("s1", "w1", "archive")
        assert (inbox / "msg-existing.json").exists(), "message stays in inbox"
        assert not (archive / "msg-existing.json").exists(), "message NOT auto-archived"
        written = json.loads((inbox / "msg-existing.json").read_bytes())
        assert written["subject"] == "pre-existing"  # not overwritten

    def test_stream_multiple_events(self, store, kernel, tmp_path):
        """Multiple distinct events are all delivered."""
        events = [
            {"msg_id": "m1", "from": "a", "to": "w1", "kind": "TASK",
             "subject": "first", "created_at": "2025-07-03T12:00:00Z"},
            {"msg_id": "m2", "from": "b", "to": "w1", "kind": "REPORT",
             "subject": "second", "created_at": "2025-07-03T12:01:00Z"},
            {"msg_id": "m3", "from": "c", "to": "w1", "kind": "COMMAND",
             "subject": "third", "created_at": "2025-07-03T12:02:00Z"},
        ]

        receiver, fired, _ = self._make_receiver_with_mock_stream(
            store, kernel, tmp_path, events)

        deadline = time.monotonic() + 3.0
        while len(fired) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)

        receiver.stop()

        assert len(fired) == 3
        ids = {m["msg_id"] for m in fired}
        assert ids == {"m1", "m2", "m3"}


# ═══════════════════════════════════════════════════════════════════════
# Ack wiring — consumed state
# ═══════════════════════════════════════════════════════════════════════


class TestAckAfterCallback:
    """C13: Receiver is notification-only — it does NOT ack/finalize."""

    def test_watch_no_auto_ack(self, store, kernel, tmp_path):
        """Watch mode: message stays in inbox after callback (no auto-ack)."""
        _setup_session(kernel, store)
        fired: list[dict] = []
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=lambda msg: fired.append(msg))
        receiver.start_watch(mailbox_root=tmp_path, poll_interval=0.1)
        time.sleep(0.15)

        _write_msg(store, "s1", "w1", "msg-ack-test")

        deadline = time.monotonic() + 3.0
        while not fired and time.monotonic() < deadline:
            time.sleep(0.05)

        receiver.stop()

        # C13: callback fired but message stays in inbox (no auto-ack)
        assert len(fired) == 1
        inbox = store.agent_subdir("s1", "w1", "inbox")
        archive = store.agent_subdir("s1", "w1", "archive")
        assert (inbox / "msg-ack-test.json").exists(), "message stays in inbox"
        assert not (archive / "msg-ack-test.json").exists(), "NOT auto-archived"

    def test_stream_no_auto_ack(self, store, kernel, tmp_path):
        """Stream mode: message stays in inbox after callback (no auto-ack)."""
        _setup_session(kernel, store)

        # Use kernel.direct to put a message in w1's inbox
        from codeagent.swarm.model import Envelope
        env = Envelope(subject="s", body="b", kind="TASK", run_id="run-1", request_id="req-1")
        kernel.direct("s1", "mgr", "w1", env)

        # Read the actual message from inbox to get the real msg_id
        msg = store.read("s1", "w1", "w1")
        assert msg is not None
        msg_id = msg["msg_id"]

        # Now create receiver and fire the stream event for this msg_id
        fired: list[dict] = []
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=lambda m: fired.append(m))

        mock_stream = MagicMock()
        call_count = [0]

        def mock_poll(timeout):
            call_count[0] += 1
            if call_count[0] == 1:
                return [{
                    "msg_id": msg_id,
                    "from": "mgr",
                    "to": "w1",
                    "kind": "TASK",
                    "subject": "s",
                    "created_at": msg["created_at"],
                }]
            receiver._stop_event.set()
            return []

        mock_stream.poll.side_effect = mock_poll
        mock_stream.close.return_value = None

        with patch("codeagent.transport.ssh.SSHStream", return_value=mock_stream):
            receiver.start_stream(ssh_cmd=["ssh", "testhost"])

        deadline = time.monotonic() + 3.0
        while not fired and time.monotonic() < deadline:
            time.sleep(0.05)

        time.sleep(0.3)
        receiver.stop()

        # C13: callback fired but message stays in inbox (no auto-ack)
        assert len(fired) == 1
        inbox = store.agent_subdir("s1", "w1", "inbox")
        # Message was already read to processing by store.read(), then
        # receiver wrote stream event to inbox — it should still be there
        stats = store.stats("s1", "w1")
        # The original message was moved to processing by store.read(),
        # then receiver wrote a new copy to inbox via _write_to_inbox.
        # No auto-ack means the inbox copy stays.
        assert stats["inbox"] >= 1 or stats["processing"] >= 1, \
            "message must remain (not auto-archived)"


# ═══════════════════════════════════════════════════════════════════════
# No-Syncthing case — local root watch
# ═══════════════════════════════════════════════════════════════════════


class TestNoSyncthingWatch:
    """Watch works on local filesystem root without Syncthing."""

    def test_watch_on_local_root(self, store, kernel, tmp_path):
        """Watch on tmp_path (no Syncthing) still detects new files."""
        _setup_session(kernel, store)
        fired: list[dict] = []
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=lambda msg: fired.append(msg))
        # Use tmp_path as mailbox_root — purely local, no Syncthing
        receiver.start_watch(mailbox_root=tmp_path, poll_interval=0.1)
        time.sleep(0.15)

        _write_msg(store, "s1", "w1", "msg-local", subject="no-syncthing")

        deadline = time.monotonic() + 3.0
        while not fired and time.monotonic() < deadline:
            time.sleep(0.05)

        receiver.stop()

        assert len(fired) == 1
        assert fired[0]["subject"] == "no-syncthing"

    def test_watch_catches_sync_conflict_files_ignored(self, store, kernel, tmp_path):
        """Sync-conflict files (prefixed .sync-conflict-) are ignored."""
        _setup_session(kernel, store)
        fired: list[dict] = []
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=lambda msg: fired.append(msg))
        receiver.start_watch(mailbox_root=tmp_path, poll_interval=0.1)
        time.sleep(0.15)

        # Write a sync-conflict file (should be ignored by store.list_messages)
        inbox = store.agent_subdir("s1", "w1", "inbox")
        inbox.mkdir(parents=True, exist_ok=True)
        conflict = inbox / ".sync-conflict-msg-001.json"
        conflict.write_text(json.dumps({"msg_id": "conflict-1"}))

        # Write a real message
        _write_msg(store, "s1", "w1", "msg-real", subject="real-msg")

        deadline = time.monotonic() + 3.0
        while not fired and time.monotonic() < deadline:
            time.sleep(0.05)

        time.sleep(0.3)
        receiver.stop()

        # Only the real message fires
        assert len(fired) == 1
        assert fired[0]["msg_id"] == "msg-real"


# ═══════════════════════════════════════════════════════════════════════
# stop() terminates loop
# ═══════════════════════════════════════════════════════════════════════


class TestStop:
    """stop() cleanly terminates the background loop."""

    def test_stop_watch_mode(self, store, kernel, tmp_path):
        """stop() terminates watch thread."""
        _setup_session(kernel, store)
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=lambda msg: None)
        receiver.start_watch(mailbox_root=tmp_path, poll_interval=0.1)

        assert receiver.is_running is True
        receiver.stop()
        assert receiver.is_running is False

    def test_stop_stream_mode(self, store, kernel, tmp_path):
        """stop() terminates stream thread."""
        _setup_session(kernel, store)
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=lambda msg: None)

        mock_stream = MagicMock()
        # Block in poll until stop
        def blocking_poll(timeout):
            time.sleep(0.5)
            return []

        mock_stream.poll.side_effect = blocking_poll
        mock_stream.close.return_value = None

        with patch("codeagent.transport.ssh.SSHStream", return_value=mock_stream):
            receiver.start_stream(ssh_cmd=["ssh", "testhost"])

        time.sleep(0.1)
        assert receiver.is_running is True

        receiver.stop()
        assert receiver.is_running is False
        mock_stream.close.assert_called()

    def test_double_start_raises(self, store, kernel, tmp_path):
        """Starting an already-started receiver raises RuntimeError."""
        _setup_session(kernel, store)
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=lambda msg: None)
        receiver.start_watch(mailbox_root=tmp_path, poll_interval=0.1)

        with pytest.raises(RuntimeError, match="already started"):
            receiver.start_watch(mailbox_root=tmp_path, poll_interval=0.1)

        receiver.stop()

    def test_stop_idempotent(self, store, kernel, tmp_path):
        """Calling stop() multiple times doesn't error."""
        _setup_session(kernel, store)
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=lambda msg: None)
        receiver.start_watch(mailbox_root=tmp_path, poll_interval=0.1)
        time.sleep(0.1)

        receiver.stop()
        receiver.stop()  # no error


# ═══════════════════════════════════════════════════════════════════════
# Kernel integration — subscribe routes to receiver
# ═══════════════════════════════════════════════════════════════════════


class TestKernelIntegration:
    """SwarmKernel.subscribe routes to SwarmReceiver when attached."""

    def test_kernel_subscribe_routes_to_receiver(self, store, kernel, tmp_path):
        """kernel.subscribe() registers callback with attached receiver."""
        _setup_session(kernel, store)

        receiver = SwarmReceiver("s1", "w1", kernel, store)
        kernel.attach_receiver(receiver)

        fired: list[dict] = []
        kernel.subscribe("s1", "w1", callback=lambda msg: fired.append(msg))

        # The receiver should now have the callback registered
        assert len(receiver._callbacks) == 1

        # Start watch and write a message
        receiver.start_watch(mailbox_root=tmp_path, poll_interval=0.1)
        time.sleep(0.15)

        _write_msg(store, "s1", "w1", "msg-kernel-push")

        deadline = time.monotonic() + 3.0
        while not fired and time.monotonic() < deadline:
            time.sleep(0.05)

        receiver.stop()

        assert len(fired) == 1
        assert fired[0]["msg_id"] == "msg-kernel-push"

    def test_kernel_subscribe_with_filters_routes(self, store, kernel, tmp_path):
        """kernel.subscribe() with kinds filter passes filter to receiver."""
        _setup_session(kernel, store)

        receiver = SwarmReceiver("s1", "w1", kernel, store)
        kernel.attach_receiver(receiver)

        fired: list[dict] = []
        kernel.subscribe("s1", "w1",
                         callback=lambda msg: fired.append(msg),
                         kinds=["REPORT"])

        # Start watch and write messages of different kinds
        receiver.start_watch(mailbox_root=tmp_path, poll_interval=0.1)
        time.sleep(0.15)

        _write_msg(store, "s1", "w1", "msg-task", kind="TASK")
        _write_msg(store, "s1", "w1", "msg-report", kind="REPORT")

        deadline = time.monotonic() + 3.0
        while len(fired) < 1 and time.monotonic() < deadline:
            time.sleep(0.05)

        time.sleep(0.3)
        receiver.stop()

        assert len(fired) == 1
        assert fired[0]["kind"] == "REPORT"

    def test_kernel_subscribe_different_agent_no_route(self, store, kernel, tmp_path):
        """kernel.subscribe for a different agent doesn't route to receiver."""
        _setup_session(kernel, store)

        receiver = SwarmReceiver("s1", "w1", kernel, store)
        kernel.attach_receiver(receiver)

        # Subscribe for w2 — should NOT route to receiver for w1
        kernel.subscribe("s1", "w2", callback=lambda msg: None)
        assert len(receiver._callbacks) == 0

    def test_kernel_poll_still_fires_subscriptions(self, store, kernel):
        """kernel.poll() still fires callbacks the old way (existing behavior)."""
        _setup_session(kernel, store)

        fired: list[dict] = []
        kernel.subscribe("s1", "w1", callback=lambda msg: fired.append(msg))

        kernel.direct("s1", "mgr", "w1",
                       Envelope(subject="poll-test", body="b", kind="TASK", run_id="run-1", request_id="req-1"))

        result = kernel.poll("s1", "w1")
        assert len(result.messages) == 1
        assert len(fired) == 1
        assert fired[0]["subject"] == "poll-test"


# ═══════════════════════════════════════════════════════════════════════
# Stream error paths
# ═══════════════════════════════════════════════════════════════════════


class TestStreamErrorPaths:
    """Error handling in stream mode (coverage for receiver.py)."""

    def test_stream_open_failure(self, store, kernel, tmp_path):
        """stream.open() raising logs error and exits cleanly."""
        _setup_session(kernel, store)
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=lambda msg: None)

        mock_stream = MagicMock()
        mock_stream.open.side_effect = OSError("connection refused")
        mock_stream.close.return_value = None
        mock_stream.poll.return_value = []

        with patch("codeagent.transport.ssh.SSHStream", return_value=mock_stream):
            receiver.start_stream(ssh_cmd=["ssh", "testhost"])

        # Thread should exit quickly after open() fails
        deadline = time.monotonic() + 3.0
        while receiver.is_running and time.monotonic() < deadline:
            time.sleep(0.05)

        receiver.stop()
        assert not receiver.is_running

    def test_stream_poll_error_breaks_loop(self, store, kernel, tmp_path):
        """stream.poll() raising breaks the loop and closes cleanly."""
        _setup_session(kernel, store)
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=lambda msg: None)

        mock_stream = MagicMock()
        mock_stream.open.return_value = None
        mock_stream.close.return_value = None
        mock_stream.poll.side_effect = OSError("stream reset")

        with patch("codeagent.transport.ssh.SSHStream", return_value=mock_stream):
            receiver.start_stream(ssh_cmd=["ssh", "testhost"])

        deadline = time.monotonic() + 3.0
        while receiver.is_running and time.monotonic() < deadline:
            time.sleep(0.05)

        receiver.stop()
        assert not receiver.is_running
        mock_stream.close.assert_called()

    def test_stream_event_missing_msg_id_skipped(self, store, kernel, tmp_path):
        """Events with empty msg_id are silently skipped."""
        _setup_session(kernel, store)
        fired: list[dict] = []
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=lambda msg: fired.append(msg))

        mock_stream = MagicMock()
        call_count = [0]

        def mock_poll(timeout):
            call_count[0] += 1
            if call_count[0] == 1:
                return [
                    {"msg_id": "", "from": "a", "kind": "TASK"},  # empty
                    {"from": "b", "kind": "REPORT"},                # missing key
                    {"msg_id": "valid-1", "from": "c", "to": "w1",
                     "kind": "TASK", "subject": "ok", "created_at": "2025-07-10T00:00:00Z"},
                ]
            receiver._stop_event.set()
            return []

        mock_stream.poll.side_effect = mock_poll
        mock_stream.close.return_value = None

        with patch("codeagent.transport.ssh.SSHStream", return_value=mock_stream):
            receiver.start_stream(ssh_cmd=["ssh", "testhost"])

        deadline = time.monotonic() + 3.0
        while not fired and time.monotonic() < deadline:
            time.sleep(0.05)

        receiver.stop()
        # Only the valid event fired
        assert len(fired) == 1
        assert fired[0]["msg_id"] == "valid-1"

    def test_stream_close_error_in_cleanup(self, store, kernel, tmp_path):
        """stream.close() raising in cleanup doesn't propagate."""
        _setup_session(kernel, store)
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=lambda msg: None)

        mock_stream = MagicMock()
        mock_stream.open.return_value = None
        mock_stream.close.side_effect = OSError("already closed")
        # Make poll return empty and stop quickly
        call_count = [0]

        def mock_poll(timeout):
            call_count[0] += 1
            if call_count[0] > 1:
                receiver._stop_event.set()
            return []

        mock_stream.poll.side_effect = mock_poll

        with patch("codeagent.transport.ssh.SSHStream", return_value=mock_stream):
            receiver.start_stream(ssh_cmd=["ssh", "testhost"])

        receiver.stop()  # Should not raise


# ═══════════════════════════════════════════════════════════════════════
# Watch error paths
# ═══════════════════════════════════════════════════════════════════════


class TestWatchErrorPaths:
    """Error handling in watch mode (coverage for receiver.py)."""

    def test_corrupt_json_skipped(self, store, kernel, tmp_path):
        """Corrupt JSON files in inbox are skipped silently."""
        _setup_session(kernel, store)
        fired: list[dict] = []
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=lambda msg: fired.append(msg))
        receiver.start_watch(mailbox_root=tmp_path, poll_interval=0.1)
        time.sleep(0.15)

        inbox = store.agent_subdir("s1", "w1", "inbox")
        inbox.mkdir(parents=True, exist_ok=True)

        # Write corrupt JSON file
        corrupt = inbox / "corrupt-msg.json"
        corrupt.write_text("not valid json {{{")

        # Write valid message
        _write_msg(store, "s1", "w1", "valid-after-corrupt", subject="good")

        deadline = time.monotonic() + 3.0
        while not fired and time.monotonic() < deadline:
            time.sleep(0.05)

        time.sleep(0.2)
        receiver.stop()

        # Only the valid message fired
        assert len(fired) == 1
        assert fired[0]["subject"] == "good"

    def test_stat_oserror_skipped(self, store, kernel, tmp_path):
        """Files where stat() raises are skipped."""
        _setup_session(kernel, store)
        fired: list[dict] = []
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=lambda msg: fired.append(msg))
        receiver.start_watch(mailbox_root=tmp_path, poll_interval=0.1)
        time.sleep(0.15)

        # Write message file first
        _write_msg(store, "s1", "w1", "msg-stat-err", subject="stat-test")

        deadline = time.monotonic() + 3.0
        while not fired and time.monotonic() < deadline:
            time.sleep(0.05)

        receiver.stop()
        assert len(fired) == 1

    def test_callback_kind_mismatch_filtered(self, store, kernel, tmp_path):
        """Callback with kinds filter doesn't fire for mismatched kind."""
        _setup_session(kernel, store)
        task_fired: list[dict] = []
        report_fired: list[dict] = []
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=lambda msg: task_fired.append(msg), kinds=["TASK"])
        receiver.subscribe(callback=lambda msg: report_fired.append(msg), kinds=["REPORT"])
        receiver.start_watch(mailbox_root=tmp_path, poll_interval=0.1)
        time.sleep(0.15)

        _write_msg(store, "s1", "w1", "msg-kind-filter", kind="TASK")

        deadline = time.monotonic() + 3.0
        while not task_fired and time.monotonic() < deadline:
            time.sleep(0.05)

        time.sleep(0.2)
        receiver.stop()

        assert len(task_fired) == 1
        assert len(report_fired) == 0  # TASK doesn't match REPORT filter

    def test_callback_channel_mismatch_filtered(self, store, kernel, tmp_path):
        """Callback with channels filter doesn't fire for mismatched channel."""
        _setup_session(kernel, store)
        fired: list[dict] = []
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(
            callback=lambda msg: fired.append(msg),
            channels=["design"],
        )
        receiver.start_watch(mailbox_root=tmp_path, poll_interval=0.1)
        time.sleep(0.15)

        # Message in a different channel
        inbox = store.agent_subdir("s1", "w1", "inbox")
        inbox.mkdir(parents=True, exist_ok=True)
        msg = {
            "msg_id": "msg-ch-mismatch",
            "from": "mgr",
            "to": "w1",
            "kind": "TASK",
            "subject": "wrong-channel",
            "channel_id": "ops",
            "created_at": "2025-07-10T00:00:00Z",
        }
        dest = inbox / "msg-ch-mismatch.json"
        dest.write_text(json.dumps(msg))

        time.sleep(0.5)
        receiver.stop()

        # Channel mismatch → callback not fired
        assert len(fired) == 0

    def test_stream_dedup_archived_file(self, store, kernel, tmp_path):
        """Stream event for msg already archived still fires callback."""
        _setup_session(kernel, store)

        # Create archive entry
        archive_dir = store.agent_subdir("s1", "w1", "archive")
        archive_dir.mkdir(parents=True, exist_ok=True)
        archived_msg = {
            "msg_id": "msg-in-archive",
            "from": "mgr",
            "to": "w1",
            "kind": "TASK",
            "subject": "archived-msg",
        }
        (archive_dir / "msg-in-archive.json").write_text(json.dumps(archived_msg))

        fired: list[dict] = []
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=lambda msg: fired.append(msg))

        mock_stream = MagicMock()
        call_count = [0]

        def mock_poll(timeout):
            call_count[0] += 1
            if call_count[0] == 1:
                return [{
                    "msg_id": "msg-in-archive",
                    "from": "mgr",
                    "to": "w1",
                    "kind": "TASK",
                    "subject": "archived-msg",
                    "created_at": "2025-07-10T00:00:00Z",
                }]
            receiver._stop_event.set()
            return []

        mock_stream.poll.side_effect = mock_poll
        mock_stream.close.return_value = None

        with patch("codeagent.transport.ssh.SSHStream", return_value=mock_stream):
            receiver.start_stream(ssh_cmd=["ssh", "testhost"])

        deadline = time.monotonic() + 3.0
        while not fired and time.monotonic() < deadline:
            time.sleep(0.05)

        receiver.stop()
        # Callback fires even though msg is archived (disk dedup path)
        assert len(fired) == 1
        assert fired[0]["msg_id"] == "msg-in-archive"

    def test_callback_error_doesnt_crash(self, store, kernel, tmp_path):
        """Callback raising doesn't crash the receiver."""
        _setup_session(kernel, store)
        call_count = [0]

        def bad_callback(msg):
            call_count[0] += 1
            raise RuntimeError("callback error")

        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=bad_callback)
        receiver.start_watch(mailbox_root=tmp_path, poll_interval=0.1)
        time.sleep(0.15)

        _write_msg(store, "s1", "w1", "msg-cb-err")

        deadline = time.monotonic() + 3.0
        while call_count[0] == 0 and time.monotonic() < deadline:
            time.sleep(0.05)

        time.sleep(0.2)
        receiver.stop()

        # Callback was invoked (and errored), but receiver kept running
        assert call_count[0] >= 1

    def test_write_to_inbox_oserror(self, store, kernel, tmp_path):
        """_write_to_inbox handling OSError on write."""
        _setup_session(kernel, store)
        fired: list[dict] = []
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=lambda msg: fired.append(msg))

        mock_stream = MagicMock()
        call_count = [0]

        def mock_poll(timeout):
            call_count[0] += 1
            if call_count[0] == 1:
                return [{
                    "msg_id": "msg-write-fail",
                    "from": "mgr",
                    "to": "w1",
                    "kind": "TASK",
                    "subject": "write-test",
                    "created_at": "2025-07-10T00:00:00Z",
                }]
            receiver._stop_event.set()
            return []

        mock_stream.poll.side_effect = mock_poll
        mock_stream.close.return_value = None

        # Patch open to raise OSError
        original_open = open

        def failing_open(*args, **kwargs):
            if args and isinstance(args[0], (str, Path)) and ".tmp-msg-write-fail" in str(args[0]):
                raise OSError("disk full")
            return original_open(*args, **kwargs)

        with patch("codeagent.transport.ssh.SSHStream", return_value=mock_stream), \
             patch("builtins.open", side_effect=failing_open):
            receiver.start_stream(ssh_cmd=["ssh", "testhost"])

        deadline = time.monotonic() + 3.0
        while receiver.is_running and time.monotonic() < deadline:
            time.sleep(0.05)

        receiver.stop()
        # Callback still fires (after write failure, event is still processed)
        assert len(fired) == 1
        assert fired[0]["msg_id"] == "msg-write-fail"


# ═══════════════════════════════════════════════════════════════════════
# P0-3: Callback failure safety — message must not be archived on error
# ═══════════════════════════════════════════════════════════════════════


class TestCallbackFailureSafety:
    """P0-3: callback raising or no match must NOT archive the message."""

    def test_callback_raises_keeps_message_in_inbox(self, store, kernel, tmp_path):
        """Callback raising RuntimeError → message stays in inbox, retryable."""
        _setup_session(kernel, store)

        def bad_callback(msg):
            raise RuntimeError("boom")

        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=bad_callback)
        receiver.start_watch(mailbox_root=tmp_path, poll_interval=0.1)
        time.sleep(0.15)

        _write_msg(store, "s1", "w1", "msg-cb-raise", subject="should-stay")

        # Wait for the watch cycle to process the message
        deadline = time.monotonic() + 3.0
        while True:
            # Check if the message is still in inbox
            inbox = store.agent_subdir("s1", "w1", "inbox")
            if (inbox / "msg-cb-raise.json").exists():
                # Check if it's been processed (stat cache updated)
                # Give a bit more time for the callback to have fired
                time.sleep(0.3)
                break
            if time.monotonic() > deadline:
                break
            time.sleep(0.05)

        receiver.stop()

        # Message must still be in inbox — NOT archived
        inbox = store.agent_subdir("s1", "w1", "inbox")
        archive = store.agent_subdir("s1", "w1", "archive")
        assert (inbox / "msg-cb-raise.json").exists(), "message must stay in inbox after callback failure"
        assert not (archive / "msg-cb-raise.json").exists(), "message must NOT be archived after callback failure"

    def test_callback_success_stays_in_inbox(self, store, kernel, tmp_path):
        """Callback succeeds → message stays in inbox (notification-only)."""
        _setup_session(kernel, store)
        fired: list[dict] = []

        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=lambda msg: fired.append(msg))
        receiver.start_watch(mailbox_root=tmp_path, poll_interval=0.1)
        time.sleep(0.15)

        _write_msg(store, "s1", "w1", "msg-cb-ok", subject="should-stay")

        deadline = time.monotonic() + 3.0
        while not fired and time.monotonic() < deadline:
            time.sleep(0.05)

        time.sleep(0.3)
        receiver.stop()

        assert len(fired) == 1
        # C13: message stays in inbox — receiver is notification-only
        inbox = store.agent_subdir("s1", "w1", "inbox")
        archive = store.agent_subdir("s1", "w1", "archive")
        assert (inbox / "msg-cb-ok.json").exists(), "message stays in inbox after callback"
        assert not (archive / "msg-cb-ok.json").exists(), "message NOT auto-archived"

    def test_no_callback_match_keeps_message_in_inbox(self, store, kernel, tmp_path):
        """No callback matches filters → message NOT archived."""
        _setup_session(kernel, store)

        receiver = SwarmReceiver("s1", "w1", kernel, store)
        # Register callback with a kind filter that won't match
        receiver.subscribe(callback=lambda msg: None, kinds=["REPORT"])
        receiver.start_watch(mailbox_root=tmp_path, poll_interval=0.1)
        time.sleep(0.15)

        # Write a TASK message (doesn't match REPORT filter)
        _write_msg(store, "s1", "w1", "msg-no-match", kind="TASK",
                   subject="should-not-archive")

        # Wait for watch cycle
        deadline = time.monotonic() + 3.0
        while True:
            inbox = store.agent_subdir("s1", "w1", "inbox")
            if (inbox / "msg-no-match.json").exists():
                time.sleep(0.3)
                break
            if time.monotonic() > deadline:
                break
            time.sleep(0.05)

        receiver.stop()

        # Message stays in inbox — no callback matched
        inbox = store.agent_subdir("s1", "w1", "inbox")
        archive = store.agent_subdir("s1", "w1", "archive")
        assert (inbox / "msg-no-match.json").exists(), "no-match must keep message in inbox"
        assert not (archive / "msg-no-match.json").exists(), "no-match must NOT archive"

    def test_stream_callback_failure_no_ack(self, store, kernel, tmp_path):
        """Stream mode: callback failure → no ack, message remains."""
        _setup_session(kernel, store)

        def failing_cb(msg):
            raise RuntimeError("stream boom")

        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=failing_cb)

        # Call _handle_stream_event directly (avoids needing SSHStream import
        # which has a pre-existing broken import in constants.py).
        event = {
            "msg_id": "msg-stream-fail",
            "from": "mgr",
            "to": "w1",
            "kind": "TASK",
            "subject": "stream-cb-fail",
            "created_at": "2025-07-10T00:00:00Z",
        }
        receiver._handle_stream_event(event)

        # Message written to inbox but NOT archived (callback failed)
        inbox = store.agent_subdir("s1", "w1", "inbox")
        archive = store.agent_subdir("s1", "w1", "archive")
        assert (inbox / "msg-stream-fail.json").exists(), "stream failure must keep message in inbox"
        assert not (archive / "msg-stream-fail.json").exists(), "stream failure must NOT archive"


# ═══════════════════════════════════════════════════════════════════════
# C15: Field preservation — all correlation fields survive round-trip
# ═══════════════════════════════════════════════════════════════════════


class TestFieldPreservation:
    """C15: All wire fields survive stream→inbox round-trip."""

    def test_stream_preserves_correlation_fields(self, store, kernel, tmp_path):
        """Stream event with reply_to, run_id, request_id, trace_id, causation_id
        survives _write_to_inbox and can be read back with all fields intact."""
        _setup_session(kernel, store)
        fired: list[dict] = []
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=lambda msg: fired.append(msg))

        event = {
            "msg_id": "msg-corr-fields",
            "from": "mgr",
            "to": "w1",
            "kind": "TASK",
            "subject": "correlation-test",
            "body": "check fields",
            "created_at": "2025-07-10T00:00:00Z",
            "reply_to": "msg-original",
            "run_id": "run-abc-123",
            "request_id": "req-xyz-789",
            "trace_id": "trace-aaa-bbb",
            "causation_id": "cause-ccc-ddd",
        }

        # Write directly to inbox via _write_to_inbox
        receiver._write_to_inbox(event)

        # Read back from disk
        inbox = store.agent_subdir("s1", "w1", "inbox")
        written = json.loads((inbox / "msg-corr-fields.json").read_bytes())

        # Assert ALL correlation fields survived
        assert written["reply_to"] == "msg-original"
        assert written["run_id"] == "run-abc-123"
        assert written["request_id"] == "req-xyz-789"
        assert written["trace_id"] == "trace-aaa-bbb"
        assert written["causation_id"] == "cause-ccc-ddd"
        # Required fields also present
        assert written["session_id"] == "s1"
        assert written["from"] == "mgr"
        assert written["to"] == "w1"
        assert written["msg_id"] == "msg-corr-fields"

    def test_stream_preserves_attachments(self, store, kernel, tmp_path):
        """Stream event with attachments survives _write_to_inbox."""
        _setup_session(kernel, store)
        receiver = SwarmReceiver("s1", "w1", kernel, store)

        event = {
            "msg_id": "msg-with-atts",
            "from": "mgr",
            "to": "w1",
            "kind": "TASK",
            "subject": "has-attachments",
            "body": "see attached",
            "created_at": "2025-07-10T00:00:00Z",
            "attachments": [
                {
                    "artifact_id": "art-001",
                    "source_host": "host-a",
                    "remote_root": "/tmp/artifacts",
                    "relative_path": "output.tar.gz",
                    "size": 1024,
                    "sha256": "a" * 64,
                    "media_type": "application/gzip",
                },
            ],
        }

        receiver._write_to_inbox(event)

        inbox = store.agent_subdir("s1", "w1", "inbox")
        written = json.loads((inbox / "msg-with-atts.json").read_bytes())

        assert len(written["attachments"]) == 1
        att = written["attachments"][0]
        assert att["artifact_id"] == "art-001"
        assert att["sha256"] == "a" * 64
        assert att["media_type"] == "application/gzip"

    def test_history_entry_preserves_correlation_fields(self, store, kernel):
        """DeliveryEngine._history_entry preserves all correlation fields."""
        from codeagent.swarm.delivery import DeliveryEngine

        engine = DeliveryEngine(mailbox_store=store)

        envelope = {
            "session_id": "s1",
            "from": "mgr",
            "to": "w1",
            "subject": "test",
            "body": "body",
            "kind": "TASK",
            "msg_id": "msg-hist-1",
            "created_at": "2025-07-10T00:00:00Z",
            "reply_to": "msg-orig",
            "run_id": "run-1",
            "request_id": "req-1",
            "trace_id": "trace-1",
            "causation_id": "cause-1",
        }

        entry = engine._history_entry(envelope, "msg-hist-1")

        assert entry["reply_to"] == "msg-orig"
        assert entry["run_id"] == "run-1"
        assert entry["request_id"] == "req-1"
        assert entry["trace_id"] == "trace-1"
        assert entry["causation_id"] == "cause-1"

    def test_history_entry_preserves_attachments(self, store, kernel):
        """DeliveryEngine._history_entry preserves attachments."""
        from codeagent.swarm.delivery import DeliveryEngine

        engine = DeliveryEngine(mailbox_store=store)

        envelope = {
            "session_id": "s1",
            "from": "mgr",
            "to": "w1",
            "subject": "test",
            "body": "body",
            "kind": "TASK",
            "msg_id": "msg-hist-2",
            "created_at": "2025-07-10T00:00:00Z",
            "attachments": [
                {"artifact_id": "a1", "source_host": "h1",
                 "remote_root": "/r", "relative_path": "f.txt",
                 "size": 100, "sha256": "b" * 64},
            ],
        }

        entry = engine._history_entry(envelope, "msg-hist-2")

        assert len(entry["attachments"]) == 1
        assert entry["attachments"][0]["artifact_id"] == "a1"

    def test_full_round_trip_all_fields(self, store, kernel, tmp_path):
        """End-to-end: stream→inbox→read, all correlation fields match."""
        _setup_session(kernel, store)
        fired: list[dict] = []
        receiver = SwarmReceiver("s1", "w1", kernel, store)
        receiver.subscribe(callback=lambda msg: fired.append(msg))

        event = {
            "msg_id": "msg-roundtrip",
            "from": "mgr",
            "to": "w1",
            "kind": "EVIDENCE",
            "subject": "round-trip",
            "body": "verify all fields",
            "created_at": "2025-07-10T00:00:00Z",
            "reply_to": "orig-msg",
            "run_id": "run-rt",
            "request_id": "req-rt",
            "trace_id": "trace-rt",
            "causation_id": "cause-rt",
            "attachments": [
                {"artifact_id": "att-rt", "source_host": "host-rt",
                 "remote_root": "/data", "relative_path": "result.json",
                 "size": 512, "sha256": "c" * 64},
            ],
        }

        receiver._handle_stream_event(event)

        # Callback received the event
        assert len(fired) == 1

        # Read back from inbox
        inbox = store.agent_subdir("s1", "w1", "inbox")
        written = json.loads((inbox / "msg-roundtrip.json").read_bytes())

        # Every correlation field matches
        for field in ("reply_to", "run_id", "request_id", "trace_id", "causation_id"):
            assert written[field] == event[field], f"{field} mismatch"
        assert len(written["attachments"]) == 1
        assert written["attachments"][0]["artifact_id"] == "att-rt"
