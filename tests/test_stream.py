"""Tests for long-lived JSONL stream: protocol, remote_exec serve, SSHStream.

Covers:
  - Protocol encode/decode for stream frames
  - remote_exec serve mode: stream registration, mailbox polling, events
  - SSHStream: open/write/read/heartbeat/reconnect-with-cursor/dedup
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from codeagent.constants import (
    STREAM_CURSOR_DEFAULT,
    STREAM_HEARTBEAT_INTERVAL,
    STREAM_RECONNECT_BASE,
    STREAM_RECONNECT_MAX,
)
from codeagent.domain import RunResult
from codeagent.wire.protocol import (
    CMD_STREAM,
    MSG_ACCEPTED,
    MSG_ERROR,
    MSG_PONG,
    MSG_READY,
    MSG_STREAM_EVENT,
    WIRE_VERSION,
    WireMessage,
    decode_line,
    decode_request,
    encode_line,
    encode_request,
    make_stream_event,
    make_stream_request,
)


# ═══════════════════════════════════════════════════════════════════════════
# Protocol — stream request encoding/decoding
# ═══════════════════════════════════════════════════════════════════════════


class TestStreamRequestProtocol:
    """make_stream_request / encode / decode round-trip."""

    def test_make_stream_request_shape(self):
        req = make_stream_request(session_id="s1", cursor="0", request_id="abc")
        assert req["wire_version"] == WIRE_VERSION
        assert req["command"] == CMD_STREAM
        assert req["session_id"] == "s1"
        assert req["cursor"] == "0"
        assert req["request_id"] == "abc"
        assert "timeout" in req

    def test_make_stream_request_auto_request_id(self):
        req = make_stream_request(session_id="s1")
        assert len(req["request_id"]) == 12  # uuid hex[:12]

    def test_encode_stream_request(self):
        line = encode_request("stream", session_id="s1", cursor="0")
        obj = json.loads(line)
        assert obj["command"] == "stream"
        assert obj["session_id"] == "s1"

    def test_decode_stream_request(self):
        raw = json.dumps({
            "wire_version": 1, "command": "stream",
            "session_id": "s1", "cursor": "0",
        })
        req = decode_request(raw)
        assert req["command"] == "stream"
        assert req["session_id"] == "s1"
        assert req["cursor"] == "0"

    def test_decode_stream_request_missing_session_id(self):
        raw = json.dumps({"wire_version": 1, "command": "stream", "cursor": "0"})
        with pytest.raises(ValueError, match="session_id"):
            decode_request(raw)

    def test_decode_stream_request_missing_cursor(self):
        raw = json.dumps({"wire_version": 1, "command": "stream", "session_id": "s1"})
        with pytest.raises(ValueError, match="cursor"):
            decode_request(raw)


# ═══════════════════════════════════════════════════════════════════════════
# Protocol — stream event encoding/decoding
# ═══════════════════════════════════════════════════════════════════════════


class TestStreamEventProtocol:
    """make_stream_event / encode / decode round-trip."""

    def test_make_stream_event_shape(self):
        evt = make_stream_event(
            request_id="abc",
            session_id="s1",
            cursor="2025-01-01T00:00:00Z",
            payload={"msg_id": "m1", "from": "agent1"},
        )
        assert evt["type"] == MSG_STREAM_EVENT
        assert evt["request_id"] == "abc"
        assert evt["session_id"] == "s1"
        assert evt["cursor"] == "2025-01-01T00:00:00Z"
        assert evt["payload"]["msg_id"] == "m1"

    def test_decode_stream_event(self):
        obj = {
            "type": MSG_STREAM_EVENT,
            "request_id": "abc",
            "session_id": "s1",
            "cursor": "2025-01-01T00:00:00Z",
            "payload": {"msg_id": "m1"},
        }
        line = json.dumps(obj)
        msg = decode_line(line)
        assert msg.type == MSG_STREAM_EVENT
        assert msg.request_id == "abc"
        assert msg.cursor == "2025-01-01T00:00:00Z"
        assert msg.payload["payload"]["msg_id"] == "m1"

    def test_decode_stream_event_missing_request_id(self):
        obj = {
            "type": MSG_STREAM_EVENT,
            "session_id": "s1",
            "cursor": "0",
            "payload": {},
        }
        with pytest.raises(ValueError, match="request_id"):
            decode_line(json.dumps(obj))

    def test_decode_stream_event_missing_session_id(self):
        obj = {
            "type": MSG_STREAM_EVENT,
            "request_id": "abc",
            "cursor": "0",
            "payload": {},
        }
        with pytest.raises(ValueError, match="session_id"):
            decode_line(json.dumps(obj))

    def test_decode_stream_event_missing_cursor(self):
        obj = {
            "type": MSG_STREAM_EVENT,
            "request_id": "abc",
            "session_id": "s1",
            "payload": {},
        }
        with pytest.raises(ValueError, match="cursor"):
            decode_line(json.dumps(obj))

    def test_decode_stream_event_missing_payload(self):
        obj = {
            "type": MSG_STREAM_EVENT,
            "request_id": "abc",
            "session_id": "s1",
            "cursor": "0",
        }
        with pytest.raises(ValueError, match="payload"):
            decode_line(json.dumps(obj))

    def test_decode_stream_event_payload_not_dict(self):
        obj = {
            "type": MSG_STREAM_EVENT,
            "request_id": "abc",
            "session_id": "s1",
            "cursor": "0",
            "payload": "not a dict",
        }
        with pytest.raises(ValueError, match="dict"):
            decode_line(json.dumps(obj))

    def test_encode_then_decode_roundtrip(self):
        evt = make_stream_event(
            request_id="req-1",
            session_id="sess-1",
            cursor="2025-07-01T12:00:00Z",
            payload={"msg_id": "msg-42", "from": "alice", "subject": "hello"},
        )
        encoded = encode_line(evt)
        msg = decode_line(encoded)
        assert msg.type == MSG_STREAM_EVENT
        assert msg.request_id == "req-1"
        assert msg.cursor == "2025-07-01T12:00:00Z"
        assert msg.payload["payload"]["msg_id"] == "msg-42"


# ═══════════════════════════════════════════════════════════════════════════
# WireMessage — stream properties
# ═══════════════════════════════════════════════════════════════════════════


class TestWireMessageStreamProps:
    def test_request_id_property(self):
        msg = WireMessage(type=MSG_STREAM_EVENT, payload={"request_id": "abc"})
        assert msg.request_id == "abc"

    def test_request_id_default_empty(self):
        msg = WireMessage(type="other", payload={})
        assert msg.request_id == ""

    def test_cursor_property(self):
        msg = WireMessage(type=MSG_STREAM_EVENT, payload={"cursor": "tok123"})
        assert msg.cursor == "tok123"

    def test_cursor_default_empty(self):
        msg = WireMessage(type="other", payload={})
        assert msg.cursor == ""


# ═══════════════════════════════════════════════════════════════════════════
# Protocol — constants exist
# ═══════════════════════════════════════════════════════════════════════════


class TestStreamConstantsExist:
    def test_msg_stream_event_value(self):
        assert MSG_STREAM_EVENT == "stream_event"

    def test_cmd_stream_value(self):
        assert CMD_STREAM == "stream"

    def test_stream_types_contains_event(self):
        from codeagent.wire.protocol import STREAM_TYPES
        assert MSG_STREAM_EVENT in STREAM_TYPES

    def test_constants_module(self):
        assert STREAM_HEARTBEAT_INTERVAL == 15
        assert STREAM_RECONNECT_MAX == 30
        assert STREAM_RECONNECT_BASE == 1
        assert STREAM_CURSOR_DEFAULT == "0"


# ═══════════════════════════════════════════════════════════════════════════
# remote_exec — stream command in supported commands
# ═══════════════════════════════════════════════════════════════════════════


class TestRemoteExecStreamSupport:
    def test_stream_in_supported_commands(self):
        from codeagent.remote_exec import SUPPORTED_COMMANDS
        assert "stream" in SUPPORTED_COMMANDS


# ═══════════════════════════════════════════════════════════════════════════
# remote_exec — _StreamSubscription
# ═══════════════════════════════════════════════════════════════════════════


class TestStreamSubscription:
    def test_dataclass_fields(self):
        from codeagent.remote_exec import _StreamSubscription
        sub = _StreamSubscription(
            request_id="r1", session_id="s1", agent_id="a1", cursor="0",
        )
        assert sub.request_id == "r1"
        assert sub.session_id == "s1"
        assert sub.agent_id == "a1"
        assert sub.cursor == "0"
        assert sub.last_heartbeat > 0


# ═══════════════════════════════════════════════════════════════════════════
# remote_exec — serve mode stream registration + event emission
# ═══════════════════════════════════════════════════════════════════════════


class TestServeModeStream:
    """Test the main loop's stream command handling and event emission."""

    def _run_main_with_stdin(self, stdin_lines: list[str], mailbox_dir: Path | None = None) -> list[dict]:
        """Run main() with given stdin lines, return all output dicts."""
        from codeagent.remote_exec import main

        stdin_content = "\n".join(stdin_lines) + "\n"
        sent: list[dict] = []

        with patch("codeagent.remote_exec._send", side_effect=sent.append):
            with patch("codeagent.remote_exec.sys.stdin", io.StringIO(stdin_content)):
                if mailbox_dir:
                    with patch("codeagent.remote_exec.MailboxStore") as MockStore:
                        store = MagicMock()
                        MockStore.return_value = store
                        store.agent_subdir.return_value = MagicMock()
                        store.list_messages.return_value = []
                        try:
                            main()
                        except (SystemExit, StopIteration):
                            pass
                else:
                    try:
                        main()
                    except (SystemExit, StopIteration):
                        pass

        return sent

    def test_stream_command_accepted(self):
        """A stream command produces an accepted response."""
        lines = [
            json.dumps({"wire_version": 1, "command": "stream", "session_id": "s1", "agent_id": "a1", "cursor": "0", "request_id": "r1"}),
        ]
        sent = self._run_main_with_stdin(lines, mailbox_dir=Path("/tmp"))

        types = [m.get("type") for m in sent]
        assert "ready" in types
        assert "accepted" in types

        accepted = [m for m in sent if m.get("type") == "accepted"][0]
        assert accepted.get("request_id") == "r1"

    def test_stream_command_requires_session_id(self):
        """Stream without session_id is rejected."""
        lines = [
            json.dumps({"wire_version": 1, "command": "stream", "agent_id": "a1", "cursor": "0", "request_id": "r1"}),
        ]
        sent = self._run_main_with_stdin(lines, mailbox_dir=Path("/tmp"))

        errors = [m for m in sent if m.get("type") == "error"]
        assert any("session_id" in m.get("message", "") for m in errors)

    def test_stream_command_requires_agent_id(self):
        """Stream without agent_id is rejected."""
        lines = [
            json.dumps({"wire_version": 1, "command": "stream", "session_id": "s1", "cursor": "0", "request_id": "r1"}),
        ]
        sent = self._run_main_with_stdin(lines, mailbox_dir=Path("/tmp"))

        errors = [m for m in sent if m.get("type") == "error"]
        assert any("agent_id" in m.get("message", "") for m in errors)

    def test_stream_emits_events_for_mailbox_messages(self, tmp_path: Path):
        """Stream subscription emits events for messages already in inbox."""
        from codeagent.remote_exec import _poll_streams, _StreamSubscription

        # Set up a mailbox store with one message in the inbox
        session_dir = tmp_path / "s1" / "a1" / "inbox"
        session_dir.mkdir(parents=True)
        msg = {
            "msg_id": "msg-001",
            "from": "alice",
            "to": "a1",
            "kind": "REPORT",
            "subject": "Hello",
            "body": "World",
            "created_at": "2025-07-01T12:00:00Z",
        }
        (session_dir / "msg-001.json").write_text(json.dumps(msg, indent=2))

        sub = _StreamSubscription(
            request_id="r1", session_id="s1", agent_id="a1", cursor="0",
        )

        sent: list[dict] = []
        with patch("codeagent.remote_exec._send", side_effect=sent.append):
            with patch("codeagent.remote_exec.MailboxStore") as MockStore:
                from codeagent.mailbox.store import MailboxStore
                real_store = MailboxStore(root=tmp_path)
                MockStore.return_value = real_store
                _poll_streams([sub])

        events = [m for m in sent if m.get("type") == "stream_event"]
        assert len(events) == 1
        evt = events[0]
        assert evt["payload"]["msg_id"] == "msg-001"
        assert evt["payload"]["from"] == "alice"
        assert evt["cursor"] == "2025-07-01T12:00:00Z"

    def test_stream_cursor_advances(self, tmp_path: Path):
        """After polling, the cursor advances so same message isn't re-emitted."""
        from codeagent.remote_exec import _poll_streams, _StreamSubscription

        session_dir = tmp_path / "s1" / "a1" / "inbox"
        session_dir.mkdir(parents=True)
        msg = {
            "msg_id": "msg-001",
            "from": "alice",
            "to": "a1",
            "kind": "REPORT",
            "subject": "Hello",
            "body": "World",
            "created_at": "2025-07-01T12:00:00Z",
        }
        (session_dir / "msg-001.json").write_text(json.dumps(msg, indent=2))

        sub = _StreamSubscription(
            request_id="r1", session_id="s1", agent_id="a1", cursor="0",
        )

        sent: list[dict] = []
        with patch("codeagent.remote_exec._send", side_effect=sent.append):
            with patch("codeagent.remote_exec.MailboxStore") as MockStore:
                from codeagent.mailbox.store import MailboxStore
                real_store = MailboxStore(root=tmp_path)
                MockStore.return_value = real_store
                _poll_streams([sub])

        # Cursor should have advanced
        assert sub.cursor == "2025-07-01T12:00:00Z"

        # Second poll should not emit the same message again
        sent2: list[dict] = []
        with patch("codeagent.remote_exec._send", side_effect=sent2.append):
            with patch("codeagent.remote_exec.MailboxStore") as MockStore:
                from codeagent.mailbox.store import MailboxStore
                real_store = MailboxStore(root=tmp_path)
                MockStore.return_value = real_store
                _poll_streams([sub])

        events2 = [m for m in sent2 if m.get("type") == "stream_event"]
        assert len(events2) == 0

    def test_stream_new_message_after_cursor(self, tmp_path: Path):
        """New messages arriving after cursor are emitted."""
        from codeagent.remote_exec import _poll_streams, _StreamSubscription

        session_dir = tmp_path / "s1" / "a1" / "inbox"
        session_dir.mkdir(parents=True)
        msg1 = {
            "msg_id": "msg-001",
            "from": "alice", "to": "a1", "kind": "REPORT",
            "subject": "First", "body": "B1",
            "created_at": "2025-07-01T12:00:00Z",
        }
        (session_dir / "msg-001.json").write_text(json.dumps(msg1, indent=2))

        sub = _StreamSubscription(
            request_id="r1", session_id="s1", agent_id="a1", cursor="0",
        )

        # First poll: emit msg-001
        sent: list[dict] = []
        with patch("codeagent.remote_exec._send", side_effect=sent.append):
            with patch("codeagent.remote_exec.MailboxStore") as MockStore:
                from codeagent.mailbox.store import MailboxStore
                real_store = MailboxStore(root=tmp_path)
                MockStore.return_value = real_store
                _poll_streams([sub])

        assert len([m for m in sent if m.get("type") == "stream_event"]) == 1
        assert sub.cursor == "2025-07-01T12:00:00Z"

        # Add a new message with a later timestamp
        msg2 = {
            "msg_id": "msg-002",
            "from": "bob", "to": "a1", "kind": "COMMAND",
            "subject": "Second", "body": "B2",
            "created_at": "2025-07-01T12:05:00Z",
        }
        (session_dir / "msg-002.json").write_text(json.dumps(msg2, indent=2))

        # Second poll: emit only msg-002
        sent2: list[dict] = []
        with patch("codeagent.remote_exec._send", side_effect=sent2.append):
            with patch("codeagent.remote_exec.MailboxStore") as MockStore:
                from codeagent.mailbox.store import MailboxStore
                real_store = MailboxStore(root=tmp_path)
                MockStore.return_value = real_store
                _poll_streams([sub])

        events2 = [m for m in sent2 if m.get("type") == "stream_event"]
        assert len(events2) == 1
        assert events2[0]["payload"]["msg_id"] == "msg-002"

    def test_stream_heartbeat_emitted(self):
        """Heartbeat pong is emitted when enough time has passed."""
        from codeagent.remote_exec import _poll_streams, _StreamSubscription

        sub = _StreamSubscription(
            request_id="r1", session_id="s1", agent_id="a1", cursor="0",
        )
        # Simulate time passing
        sub.last_heartbeat = time.monotonic() - STREAM_HEARTBEAT_INTERVAL - 1

        sent: list[dict] = []
        with patch("codeagent.remote_exec._send", side_effect=sent.append):
            with patch("codeagent.remote_exec.MailboxStore") as MockStore:
                store = MagicMock()
                MockStore.return_value = store
                store.agent_subdir.return_value = MagicMock()
                store.list_messages.return_value = []
                _poll_streams([sub])

        pongs = [m for m in sent if m.get("type") == "pong" and m.get("heartbeat")]
        assert len(pongs) == 1

    def test_serve_polls_streams_on_select_timeout(self, tmp_path: Path):
        """select timeout ([] on a real pipe) must poll streams, not block.

        Regression: `if ready is False:` treated select's [] (timeout) as
        False, so serve mode fell into a blocking stdin read and never
        flushed mailbox events — real-time push silently broken.
        """
        import threading

        from codeagent.mailbox.store import MailboxStore
        from codeagent.remote_exec import main

        r_fd, w_fd = os.pipe()
        real_stdin = os.fdopen(r_fd, "r")
        sent: list[dict[str, Any]] = []
        store = MailboxStore(root=tmp_path)

        try:
            with (
                patch("codeagent.remote_exec._send", side_effect=sent.append),
                patch("codeagent.remote_exec.MailboxStore", return_value=store),
                patch("codeagent.remote_exec.sys.stdin", real_stdin),
                patch("codeagent.remote_exec.STREAM_HEARTBEAT_INTERVAL", 1.0),
            ):
                t = threading.Thread(target=main, daemon=True)
                t.start()

                # Register a stream subscription
                os.write(w_fd, json.dumps({
                    "wire_version": 1,
                    "command": "stream",
                    "session_id": "s1",
                    "agent_id": "a1",
                    "cursor": "0",
                    "request_id": "r1",
                }).encode() + b"\n")

                # Wait for subscription registration + first select cycle
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    if any(m.get("type") == "accepted" for m in sent):
                        break
                    time.sleep(0.05)

                # Drop a new message into the inbox while the serve loop
                # is blocked in select (real pipe, no further stdin input)
                inbox = store.agent_subdir("s1", "a1", "inbox")
                inbox.mkdir(parents=True, exist_ok=True)
                msg = {
                    "msg_id": "m1",
                    "from": "alice",
                    "to": "a1",
                    "kind": "TASK",
                    "subject": "hi",
                    "created_at": "2026-01-01T00:00:00Z",
                }
                (inbox / "m1.json").write_text(json.dumps(msg))

                # Give the select-timeout → poll → emit cycle time to run
                deadline = time.monotonic() + 4.0
                while time.monotonic() < deadline:
                    if any(m.get("type") == "stream_event" for m in sent):
                        break
                    time.sleep(0.1)

                events = [m for m in sent if m.get("type") == "stream_event"]
                assert len(events) == 1
                assert events[0]["payload"]["msg_id"] == "m1"
        finally:
            os.close(w_fd)
            try:
                real_stdin.close()
            except OSError:
                pass
            if "t" in locals():
                t.join(timeout=5)


# ═══════════════════════════════════════════════════════════════════════════
# SSHStream — mock Popen tests
# ═══════════════════════════════════════════════════════════════════════════

from codeagent.transport.base import TransportError


class TestSSHStream:
    """SSHStream with a fake subprocess (mock Popen)."""

    def _make_mock_proc(
        self,
        response_lines: list[bytes] | None = None,
        returncode: int | None = None,
    ) -> MagicMock:
        """Build a mock Popen with controllable stdout/stdin."""
        if response_lines is None:
            response_lines = [
                encode_line({"type": MSG_READY, "wire_version": WIRE_VERSION, "package_version": "0.1.0"}),
                encode_line({"type": MSG_ACCEPTED, "wire_version": WIRE_VERSION}),
            ]

        stdout_data = b"".join(response_lines)
        stdout_io = io.BytesIO(stdout_data)
        stdin_io = io.BytesIO()
        stderr_io = io.BytesIO()

        proc = MagicMock(spec=subprocess.Popen)
        proc.stdout = stdout_io
        proc.stdin = stdin_io
        proc.stderr = stderr_io
        proc.returncode = returncode
        proc.poll.return_value = returncode
        proc.wait.return_value = returncode or 0

        return proc

    def test_open_and_close(self):
        """SSHStream opens (spawns SSH) and closes cleanly."""
        from codeagent.transport.ssh import SSHStream

        proc = self._make_mock_proc()

        with patch("subprocess.Popen", return_value=proc) as mock_popen:
            stream = SSHStream(ssh_cmd=["ssh", "testhost"])
            stream.open(session_id="s1", agent_id="a1")

            assert stream.is_alive is True or proc.poll() is None
            mock_popen.assert_called_once()

            # Verify a stream request was written to stdin
            stdin_bytes = proc.stdin.getvalue()
            assert b"stream" in stdin_bytes
            assert b"s1" in stdin_bytes

            stream.close()

    def test_poll_returns_stream_events(self):
        """poll() parses stream_event frames from stdout."""
        from codeagent.transport.ssh import SSHStream

        ready_line = encode_line({"type": MSG_READY, "wire_version": WIRE_VERSION, "package_version": "0.1.0"})
        accepted_line = encode_line({"type": MSG_ACCEPTED, "wire_version": WIRE_VERSION})
        event_line = encode_line({
            "type": MSG_STREAM_EVENT,
            "request_id": "r1",
            "session_id": "s1",
            "cursor": "2025-07-01T12:00:00Z",
            "payload": {"msg_id": "m1", "from": "alice", "subject": "hello"},
        })

        stdout_io = io.BytesIO(ready_line + accepted_line + event_line)
        stdin_io = io.BytesIO()
        stderr_io = io.BytesIO()

        proc = MagicMock(spec=subprocess.Popen)
        proc.stdout = stdout_io
        proc.stdin = stdin_io
        proc.stderr = stderr_io
        proc.returncode = None
        proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=proc):
            stream = SSHStream(ssh_cmd=["ssh", "testhost"])
            stream.open(session_id="s1", agent_id="a1")

            events = stream.poll(timeout=0.1)
            assert len(events) == 1
            assert events[0]["msg_id"] == "m1"
            assert events[0]["from"] == "alice"
            assert stream.cursor == "2025-07-01T12:00:00Z"

            stream.close()

    def test_poll_dedup_by_msg_id(self):
        """Duplicate msg_id events are filtered."""
        from codeagent.transport.ssh import SSHStream

        event_line = encode_line({
            "type": MSG_STREAM_EVENT,
            "request_id": "r1",
            "session_id": "s1",
            "cursor": "tok1",
            "payload": {"msg_id": "m1", "from": "alice"},
        })
        # Same event twice
        ready_line = encode_line({"type": MSG_READY, "wire_version": 1, "package_version": "0.1.0"})
        accepted_line = encode_line({"type": MSG_ACCEPTED, "wire_version": 1})
        stdout_io = io.BytesIO(ready_line + accepted_line + event_line + event_line)
        stdin_io = io.BytesIO()

        proc = MagicMock(spec=subprocess.Popen)
        proc.stdout = stdout_io
        proc.stdin = stdin_io
        proc.stderr = io.BytesIO()
        proc.returncode = None
        proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=proc):
            stream = SSHStream(ssh_cmd=["ssh", "testhost"])
            stream.open(session_id="s1", agent_id="a1")

            events = stream.poll(timeout=0.1)
            # Should see exactly one event (second is deduped)
            assert len(events) == 1
            assert events[0]["msg_id"] == "m1"

            stream.close()

    def test_poll_cursor_resume_after_reconnect(self):
        """After reconnect, stream re-issues with the last cursor."""
        from codeagent.transport.ssh import SSHStream

        # First connection: sends event with cursor "tok1"
        event1 = encode_line({
            "type": MSG_STREAM_EVENT,
            "request_id": "r1",
            "session_id": "s1",
            "cursor": "tok1",
            "payload": {"msg_id": "m1", "from": "alice"},
        })
        ready1 = encode_line({"type": MSG_READY, "wire_version": 1, "package_version": "0.1.0"})
        acc1 = encode_line({"type": MSG_ACCEPTED, "wire_version": 1})
        stdout1 = io.BytesIO(ready1 + acc1 + event1)

        # Second connection (reconnect): accepts and sends another event
        event2 = encode_line({
            "type": MSG_STREAM_EVENT,
            "request_id": "r1",
            "session_id": "s1",
            "cursor": "tok2",
            "payload": {"msg_id": "m2", "from": "bob"},
        })
        ready2 = encode_line({"type": MSG_READY, "wire_version": 1, "package_version": "0.1.0"})
        acc2 = encode_line({"type": MSG_ACCEPTED, "wire_version": 1})
        stdout2 = io.BytesIO(ready2 + acc2 + event2)

        stdin1 = io.BytesIO()
        stdin2 = io.BytesIO()

        proc1 = MagicMock(spec=subprocess.Popen)
        proc1.stdout = stdout1
        proc1.stdin = stdin1
        proc1.stderr = io.BytesIO()
        proc1.returncode = None
        # First poll's EOF check sees alive (None); second poll's EOF
        # check sees the process has exited (1) → triggers reconnect.
        poll_calls = [0]

        def proc1_poll():
            poll_calls[0] += 1
            return 1 if poll_calls[0] >= 2 else None

        proc1.poll.side_effect = proc1_poll

        proc2 = MagicMock(spec=subprocess.Popen)
        proc2.stdout = stdout2
        proc2.stdin = stdin2
        proc2.stderr = io.BytesIO()
        proc2.returncode = None
        proc2.poll.return_value = None

        call_count = 0

        def mock_popen(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return proc1
            return proc2

        with patch("subprocess.Popen", side_effect=mock_popen):
            stream = SSHStream(ssh_cmd=["ssh", "testhost"])
            stream.open(session_id="s1", agent_id="a1")

            # First poll: get event from first connection
            events1 = stream.poll(timeout=0.1)
            assert len(events1) == 1
            assert events1[0]["msg_id"] == "m1"
            assert stream.cursor == "tok1"

            # Simulate connection death: proc.poll() returns non-None
            # Force reconnect by calling poll again
            # The mock will return exit code on the next poll check
            events2 = stream.poll(timeout=0.1)
            assert len(events2) == 1
            assert events2[0]["msg_id"] == "m2"
            assert stream.cursor == "tok2"

            # Verify the second connection received cursor resume
            stdin2_bytes = stdin2.getvalue()
            req = json.loads(stdin2_bytes)
            assert req["cursor"] == "tok1"  # resumed from last cursor

            stream.close()

    def test_reconnect_exponential_backoff(self):
        """Reconnect uses exponential backoff up to max."""
        from codeagent.transport.ssh import SSHStream

        stream = SSHStream(
            ssh_cmd=["ssh", "testhost"],
            reconnect_base=0.01,  # fast for testing
            reconnect_max=0.05,
        )

        # Track sleep calls
        sleep_calls: list[float] = []

        def mock_sleep(duration):
            sleep_calls.append(duration)

        # First spawn fails, second succeeds
        spawn_count = 0

        def mock_spawn():
            nonlocal spawn_count
            spawn_count += 1
            if spawn_count <= 2:
                raise TransportError("connection refused")

        stream._session_id = "s1"
        stream._agent_id = "a1"

        with patch("time.sleep", side_effect=mock_sleep):
            with patch.object(stream, "_spawn_and_subscribe", side_effect=mock_spawn):
                stream._reconnect()

        # Should have retried with increasing backoff
        assert len(sleep_calls) >= 2
        assert sleep_calls[0] == 0.01  # base
        # Second call is min(base*2, max) = min(0.02, 0.05) = 0.02
        assert sleep_calls[1] == 0.02

    def test_reconnect_capped_at_max(self):
        """Reconnect backoff doesn't exceed max."""
        from codeagent.transport.ssh import SSHStream

        stream = SSHStream(
            ssh_cmd=["ssh", "testhost"],
            reconnect_base=0.04,
            reconnect_max=0.05,
        )

        sleep_calls: list[float] = []

        def mock_sleep(duration):
            sleep_calls.append(duration)

        # All spawns fail (would go on forever, but _reconnect checks _closed)
        spawn_count = [0]
        max_retries = 5

        def mock_spawn():
            spawn_count[0] += 1
            if spawn_count[0] >= max_retries:
                stream._closed = True  # stop the loop
            raise TransportError("connection refused")

        stream._session_id = "s1"
        stream._agent_id = "a1"

        with patch("time.sleep", side_effect=mock_sleep):
            with patch.object(stream, "_spawn_and_subscribe", side_effect=mock_spawn):
                stream._reconnect()

        # All backoffs should be <= reconnect_max
        for call in sleep_calls:
            assert call 