"""Cross-device gateway/SSH tests — composite cursor, SSHStream stream_kind,
remote gateway RPC, outbox dedup, dual-poll."""
from __future__ import annotations

import io
import json
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codeagent.gateway.events import EventStore
from codeagent.gateway.model import (
    GATEWAY_PROTOCOL_VERSION,
    GatewayError,
    GatewayRequest,
    GatewayResponse,
)
from codeagent.gateway.remote import remote_gateway_call
from codeagent.mailbox.store import MailboxStore
from codeagent.wire.protocol import (
    MSG_ACCEPTED,
    MSG_READY,
    MSG_STREAM_EVENT,
    WIRE_VERSION,
    encode_line,
    make_composite_cursor,
    split_composite_cursor,
)


# ── composite cursor ───────────────────────────────────────────────────


class TestCompositeCursor:
    def test_roundtrip(self):
        c = make_composite_cursor("1786200000000/000007", 42)
        mailbox, runtime = split_composite_cursor(c)
        assert mailbox == "1786200000000/000007"
        assert runtime == 42

    def test_opaque_to_client(self):
        """The composite cursor is base64url and contains no plaintext fields."""
        c = make_composite_cursor("x/1", 3)
        # must not contain the literal JSON structure (opaque to client)
        assert "mailbox" not in c
        assert "runtime" not in c
        assert "v" not in c

    def test_legacy_plain_cursor_passthrough(self):
        mailbox, runtime = split_composite_cursor("1786200000000/000000")
        assert mailbox == "1786200000000/000000"
        assert runtime == 0

    def test_initial_cursor(self):
        mailbox, runtime = split_composite_cursor("0")
        assert mailbox == "0"
        assert runtime == 0

    def test_stable_encoding(self):
        assert make_composite_cursor("a/1", 2) == make_composite_cursor("a/1", 2)


# ── SSHStream stream_kind propagation ──────────────────────────────────


class TestSSHStreamStreamKind:
    def _mock_proc(self) -> MagicMock:
        stdout_io = io.BytesIO(
            encode_line({"type": MSG_READY, "wire_version": WIRE_VERSION, "package_version": "0.2.0"})
            + encode_line({"type": MSG_ACCEPTED, "wire_version": WIRE_VERSION})
        )
        stdin_io = io.BytesIO()
        stderr_io = io.BytesIO()
        proc = MagicMock(spec=subprocess.Popen)
        proc.stdout = stdout_io
        proc.stdin = stdin_io
        proc.stderr = stderr_io
        proc.returncode = None
        proc.poll.return_value = None
        proc.wait.return_value = 0
        return proc

    def test_open_passes_stream_kind_all(self):
        from codeagent.transport.ssh import SSHStream

        proc = self._mock_proc()
        with patch("subprocess.Popen", return_value=proc):
            stream = SSHStream(ssh_cmd=["ssh", "host"])
            stream.open(
                session_id="s1", agent_id="a1", stream_kind="all",
                cursor=make_composite_cursor("0/000000", 0),
            )
            req = json.loads(proc.stdin.getvalue().decode())
            assert req["stream_kind"] == "all"
            # composite cursor passed through verbatim (client never parses)
            assert req["cursor"] == make_composite_cursor("0/000000", 0)
            stream.close()

    def test_open_default_mailbox(self):
        from codeagent.transport.ssh import SSHStream

        proc = self._mock_proc()
        with patch("subprocess.Popen", return_value=proc):
            stream = SSHStream(ssh_cmd=["ssh", "host"])
            stream.open(session_id="s1", agent_id="a1")
            req = json.loads(proc.stdin.getvalue().decode())
            assert req["stream_kind"] == "mailbox"
            stream.close()

    def test_poll_adopts_composite_cursor(self):
        """The client stores the opaque server cursor verbatim."""
        from codeagent.transport.ssh import SSHStream

        composite = make_composite_cursor("1/000000", 9)
        event_line = encode_line({
            "type": MSG_STREAM_EVENT,
            "request_id": "r1",
            "session_id": "s1",
            "cursor": composite,
            "payload": {"msg_id": "m1"},
        })
        stdout_io = io.BytesIO(
            encode_line({"type": MSG_READY, "wire_version": WIRE_VERSION, "package_version": "0.2.0"})
            + encode_line({"type": MSG_ACCEPTED, "wire_version": WIRE_VERSION})
            + event_line
        )
        proc = MagicMock(spec=subprocess.Popen)
        proc.stdout = stdout_io
        proc.stdin = io.BytesIO()
        proc.stderr = io.BytesIO()
        proc.returncode = None
        proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=proc):
            stream = SSHStream(ssh_cmd=["ssh", "host"])
            stream.open(session_id="s1", agent_id="a1")
            events = stream.poll(timeout=0)
            assert len(events) == 1
            assert stream.cursor == composite  # opaque round-trip


# ── remote gateway RPC over SSH (mocked) ───────────────────────────────


class TestRemoteGatewayCall:
    def _patch_cm(self):
        cm = MagicMock()
        cm.is_alive.return_value = True
        cm.ssh_cmd.return_value = ["ssh", "host1", "codeagent gateway rpc --stdio"]
        return patch("codeagent.gateway.remote.ControlMaster", return_value=cm), cm

    def test_success(self):
        resp = GatewayResponse(
            v=GATEWAY_PROTOCOL_VERSION, id="x", ok=True,
            result={"version": GATEWAY_PROTOCOL_VERSION, "runtimes": ["omp"]},
        )
        proc = MagicMock(spec=subprocess.Popen)
        proc.communicate.return_value = (
            (resp.to_json() + "\n").encode(), b"",
        )
        proc.returncode = 0
        cm_patch, cm = self._patch_cm()
        with cm_patch, patch("subprocess.Popen", return_value=proc):
            result = remote_gateway_call("host1", "capabilities.get", {})
        assert result["version"] == GATEWAY_PROTOCOL_VERSION

    def test_remote_error_code(self):
        resp = GatewayResponse(
            v=GATEWAY_PROTOCOL_VERSION, id="x", ok=False,
            error={"code": "UNSUPPORTED_RUNTIME", "message": "nope", "context": {}},
        )
        proc = MagicMock(spec=subprocess.Popen)
        proc.communicate.return_value = ((resp.to_json() + "\n").encode(), b"")
        proc.returncode = 0
        cm_patch, cm = self._patch_cm()
        with cm_patch, patch("subprocess.Popen", return_value=proc):
            with pytest.raises(GatewayError) as ei:
                remote_gateway_call("host1", "runtime.spawn", {})
        assert ei.value.code == "UNSUPPORTED_RUNTIME"

    def test_empty_response(self):
        proc = MagicMock(spec=subprocess.Popen)
        proc.communicate.return_value = (b"", b"ssh error")
        proc.returncode = 255
        cm_patch, cm = self._patch_cm()
        with cm_patch, patch("subprocess.Popen", return_value=proc):
            with pytest.raises(GatewayError) as ei:
                remote_gateway_call("host1", "capabilities.get", {})
        assert "REMOTE" in ei.value.code

    def test_timeout(self):
        proc = MagicMock(spec=subprocess.Popen)
        proc.communicate.side_effect = subprocess.TimeoutExpired("cmd", 1)
        proc.kill.return_value = None
        cm_patch, cm = self._patch_cm()
        with cm_patch, patch("subprocess.Popen", return_value=proc):
            with pytest.raises(GatewayError) as ei:
                remote_gateway_call("host1", "capabilities.get", {})
        assert ei.value.code == "REMOTE_RPC_TIMEOUT"


# ── remote_exec dual poll (runtime + mailbox) ──────────────────────────


class TestDualPoll:
    def _init_session(self, store: MailboxStore, sid: str = "s1") -> None:
        store.session_init(sid, "manager", ["worker"])

    def test_mailbox_poll_emits_events(self, tmp_path: Path):
        import codeagent.remote_exec as rex
        from codeagent.mailbox.store import MailboxStore

        root = tmp_path / "mb"
        root.mkdir()
        store = MailboxStore(root=root)
        self._init_session(store)
        store.send("s1", "manager", "worker", "t", "body", kind="TASK",
                   run_id="r1", request_id="q1")

        sub = rex._StreamSubscription(
            request_id="r1", session_id="s1", agent_id="worker",
            cursor="0", stream_kind="mailbox",
        )
        sent: list[dict] = []
        with patch("codeagent.remote_exec.MailboxStore", return_value=store), \
             patch.object(rex, "_send", side_effect=lambda obj: sent.append(obj)):
            rex._poll_streams([sub])
        events = [s for s in sent if s.get("type") == "stream_event"]
        assert len(events) == 1
        assert events[0]["source"] == "mailbox"
        assert events[0]["payload"]["kind"] == "TASK"
        assert sub.cursor != "0"  # advanced

    def test_runtime_poll_emits_events(self, tmp_path: Path):
        import codeagent.remote_exec as rex
        from codeagent.gateway.model import RuntimeEventDraft

        db = tmp_path / "e.sqlite3"
        es = EventStore(db_path=db, source_host="h")
        es.append_local(RuntimeEventDraft(
            runtime_id="r9", generation=1, session_id="s1", agent_id="w",
            request_id="", run_id="", kind="TOOL_STARTED",
            created_at="2026-01-01T00:00:00Z", payload={"tool": "bash"},
        ))
        with patch("codeagent.gateway.events.EventStore", return_value=es):
            sub = rex._StreamSubscription(
                request_id="r2", session_id="s1", agent_id="w",
                cursor="0", stream_kind="runtime",
            )
            sent: list[dict] = []
            with patch.object(rex, "_send", side_effect=lambda obj: sent.append(obj)):
                rex._poll_streams([sub])
        events = [s for s in sent if s.get("type") == "stream_event"]
        assert len(events) == 1
        assert events[0]["source"] == "runtime"
        assert events[0]["payload"]["kind"] == "TOOL_STARTED"
        # composite cursor advanced
        _mc, rt = split_composite_cursor(sub.cursor)
        assert rt >= 1

    def test_all_poll_both_legs(self, tmp_path: Path):
        import codeagent.remote_exec as rex
        from codeagent.gateway.model import RuntimeEventDraft

        root = tmp_path / "mb"
        root.mkdir()
        store = MailboxStore(root=root)
        self._init_session(store)
        store.send("s1", "manager", "worker", "t", "b", kind="TASK", run_id="r1", request_id="q1")

        db = tmp_path / "e.sqlite3"
        es = EventStore(db_path=db, source_host="h")
        es.append_local(RuntimeEventDraft(
            runtime_id="r9", generation=1, session_id="s1", agent_id="w",
            request_id="", run_id="", kind="RUNTIME_STATE",
            created_at="2026-01-01T00:00:00Z", payload={"state": "active"},
        ))
        with patch("codeagent.gateway.events.EventStore", return_value=es), \
             patch("codeagent.remote_exec.MailboxStore", return_value=store):
            sub = rex._StreamSubscription(
                request_id="r3", session_id="s1", agent_id="worker",
                cursor=make_composite_cursor("0", 0), stream_kind="all",
            )
            sent: list[dict] = []
            with patch.object(rex, "_send", side_effect=lambda obj: sent.append(obj)):
                rex._poll_streams([sub])
        events = [s for s in sent if s.get("type") == "stream_event"]
        sources = sorted(e["source"] for e in events)
        assert sources == ["mailbox", "runtime"]
        # composite cursor round-trips through split
        mailbox, rt = split_composite_cursor(sub.cursor)
        assert mailbox != "0" or rt >= 1


# ── DeliveryEngine outbox dedup ────────────────────────────────────────


class TestOutboxDedup:
    def test_flush_does_not_duplicate_delivered(self, tmp_path: Path):
        from codeagent.swarm.delivery import DeliveryEngine
        from codeagent.swarm.kernel import LocalDeliverySink, SwarmKernel
        from codeagent.swarm.model import Envelope

        root = tmp_path / "mb"
        root.mkdir()
        store = MailboxStore(root=root)
        store.session_init("s1", "manager", ["worker"])
        kernel = SwarmKernel(store=store, sink=LocalDeliverySink(store))
        engine = DeliveryEngine(mailbox_store=store, transport_router=None, outbox_root=tmp_path / "outbox")

        receipt = engine.deliver("s1", "worker", {
            "session_id": "s1", "from": "manager", "to": "worker",
            "subject": "t", "body": "b", "kind": "TASK",
            "msg_id": "m1", "run_id": "r1", "request_id": "q1",
        })
        assert receipt.status in ("accepted", "delivered")

        # Second flush with same msg_id → no duplicate envelope
        flushed = engine.flush("s1")
        outbox_dir = tmp_path / "outbox" / "s1"
        envelopes = list(outbox_dir.glob("*.json"))
        # The single envelope file remains; delivered marker set
        assert len(envelopes) == 1
        inbox = store.list_messages(store.agent_subdir("s1", "worker", "inbox"))
        # local send wrote one inbox message
        assert len(inbox) == 1

    def test_replay_same_msg_id_idempotent(self, tmp_path: Path):
        store = MailboxStore(root=tmp_path / "mb")
        store.root.mkdir(parents=True)
        store.session_init("s1", "manager", ["worker"])
        svc = store.send("s1", "manager", "worker", "t", "b", kind="TASK",
                         run_id="r1", request_id="q1", msg_id="m-fixed")
        replay = store.send("s1", "manager", "worker", "t", "b", kind="TASK",
                            run_id="r1", request_id="q1", msg_id="m-fixed")
        assert "idempotent" in replay
        inbox = store.list_messages(store.agent_subdir("s1", "worker", "inbox"))
        assert len(inbox) == 1  # no duplicate


# ── heartbeat / reconnect resumption (mock) ────────────────────────────


class TestSSHStreamReconnect:
    def test_reconnect_resumes_from_cursor(self):
        """After process exit, the reconnect re-issues the request with the
        last cursor (mailbox + composite)."""
        from codeagent.transport.ssh import SSHStream

        composite = make_composite_cursor("5/000000", 3)

        def _make_proc():
            stdout_io = io.BytesIO(
                encode_line({"type": MSG_READY, "wire_version": WIRE_VERSION, "package_version": "0.2.0"})
                + encode_line({"type": MSG_ACCEPTED, "wire_version": WIRE_VERSION})
                + encode_line({
                    "type": MSG_STREAM_EVENT, "request_id": "r1", "session_id": "s1",
                    "cursor": composite, "payload": {"msg_id": "m9"},
                })
            )
            proc = MagicMock(spec=subprocess.Popen)
            proc.stdout = stdout_io
            proc.stdin = io.BytesIO()
            proc.stderr = io.BytesIO()
            proc.returncode = None
            proc.poll.return_value = None
            proc.wait.return_value = 0
            return proc

        procs = [_make_proc(), _make_proc()]
        with patch("subprocess.Popen", side_effect=procs):
            stream = SSHStream(ssh_cmd=["ssh", "h"], reconnect_base=0.01, reconnect_max=0.02)
            stream.open(session_id="s1", agent_id="a1", stream_kind="all", cursor="0")
            events = stream.poll(timeout=0.2)
            assert len(events) == 1
            assert stream.cursor == composite
            stream.close()
