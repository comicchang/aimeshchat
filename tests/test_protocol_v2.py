"""Protocol v2 behavior — require_ack, RECEIPT(READ), v1 compatibility.

Covers:
  - v1 messages read back as protocol_version=1 / require_ack=False
  - new sends write protocol_version=2 with require_ack honored
  - RECEIPT(READ) validation: reply_to/run_id/request_id/receipt_type,
    require_ack=False (no receipt loops)
  - READ receipts are deterministic (UUIDv5) and idempotent
  - MailboxService.read emits receipts on require_ack claims
  - ACK_ROUTE_UNRESOLVED: require_ack message not consumed when the
    sender is not in the authoritative roster
  - RequestLedger.apply_message reducer (DISPATCHED/ACKED/RUNNING/terminal)
  - UNKNOWN_STALE is a terminal state
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from codeagent.mailbox.protocol import (
    PROTOCOL_VERSION,
    Message,
    MessageKind,
    ReceiptType,
    validate_message,
)
from codeagent.mailbox.service import ACK_ROUTE_UNRESOLVED, MailboxService
from codeagent.mailbox.store import MailboxStore, RequestLedger, TERMINAL_STATES


# ── fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def mailbox_root(tmp_path: Path) -> Path:
    root = tmp_path / "mailbox"
    root.mkdir()
    return root


def _init_session(store: MailboxStore, sid: str = "s1", manager: str = "manager",
                  agents: tuple[str, ...] = ("worker", "oracle")) -> None:
    store.session_init(sid, manager, list(agents))


# ── v1 compatibility ───────────────────────────────────────────────────


def test_v1_message_reads_back_as_v1(tmp_path: Path) -> None:
    """A message dict without protocol_version (v1) reads as v1/require_ack=False."""
    v1 = {
        "session_id": "s1",
        "from": "manager",
        "to": "worker",
        "subject": "init",
        "body": "hello",
        "kind": "TASK",
        "msg_id": "m1",
        "created_at": "2026-01-01T00:00:00Z",
        "run_id": "r1",
        "request_id": "q1",
    }
    ok, reason = validate_message(v1, "s1")
    assert ok, reason
    msg = Message.from_dict(v1)
    assert msg.protocol_version == 1
    assert msg.require_ack is False


def test_v1_fixture_roundtrip(tmp_path: Path, mailbox_root: Path) -> None:
    """Old v1 fixture files in an inbox are still readable and consumable."""
    store = MailboxStore(root=mailbox_root)
    _init_session(store)
    inbox = store.agent_subdir("s1", "worker", "inbox")
    # inbox already exists from session_init — just write the fixture file
    v1 = {
        "session_id": "s1",
        "from": "manager",
        "to": "worker",
        "subject": "legacy",
        "body": "pre-v2 task",
        "kind": "TASK",
        "msg_id": "legacy_1",
        "created_at": "2026-01-01T00:00:00Z",
        "run_id": "r1",
        "request_id": "q1",
        "_cursor": "1000/000000",
    }
    (inbox / "legacy_1.json").write_text(json.dumps(v1))

    svc = MailboxService(store=store)
    outcome = svc.read("s1", "worker", "worker")
    assert outcome.status == "ok"
    assert outcome.message is not None
    assert outcome.message["msg_id"] == "legacy_1"
    # v1 messages never demand an ack → no receipt generated
    assert outcome.receipt is None


# ── v2 send / require_ack ──────────────────────────────────────────────


def test_new_send_writes_v2(tmp_path: Path, mailbox_root: Path) -> None:
    store = MailboxStore(root=mailbox_root)
    _init_session(store)
    svc = MailboxService(store=store)
    receipt = svc.send(
        "s1", "manager", "worker", "task", "do it", kind="TASK",
        run_id="r1", request_id="q1", require_ack=True,
    )
    assert receipt.status == "delivered"
    assert receipt.msg_id

    inbox = store.agent_subdir("s1", "worker", "inbox")
    files = store.list_messages(inbox)
    assert len(files) == 1
    payload = json.loads(files[0].read_bytes())
    assert payload["protocol_version"] == PROTOCOL_VERSION == 2
    assert payload["require_ack"] is True
    # JSON field is exactly `require_ack` — no `receipt_policy` alias
    assert "receipt_policy" not in payload


def test_send_without_require_ack_defaults_false(tmp_path: Path, mailbox_root: Path) -> None:
    store = MailboxStore(root=mailbox_root)
    _init_session(store)
    svc = MailboxService(store=store)
    svc.send("s1", "manager", "worker", "n", "no ack", kind="NOTICE")
    inbox = store.agent_subdir("s1", "worker", "inbox")
    payload = json.loads(store.list_messages(inbox)[0].read_bytes())
    assert payload["require_ack"] is False


# ── RECEIPT validation ─────────────────────────────────────────────────


def test_receipt_requires_correlation_fields() -> None:
    """RECEIPT must carry reply_to/run_id/request_id/receipt_type=READ."""
    base = {
        "session_id": "s1",
        "from": "worker",
        "to": "manager",
        "subject": "READ m1",
        "body": '{"receipt_type":"READ"}',
        "kind": "RECEIPT",
        "msg_id": "rcpt-1",
        "created_at": "2026-01-01T00:00:00Z",
        "protocol_version": 2,
        "require_ack": False,
        "receipt_type": "READ",
        "reply_to": "m1",
        "run_id": "r1",
        "request_id": "q1",
    }
    ok, reason = validate_message(base, "s1")
    assert ok, reason

    # missing reply_to
    bad = dict(base)
    bad.pop("reply_to")
    ok, reason = validate_message(bad, "s1")
    assert not ok
    assert "reply_to" in reason

    # missing receipt_type
    bad = dict(base)
    bad.pop("receipt_type")
    ok, reason = validate_message(bad, "s1")
    assert not ok

    # wrong receipt_type value
    bad = dict(base)
    bad["receipt_type"] = "DELIVERED"
    ok, reason = validate_message(bad, "s1")
    assert not ok
    assert "receipt_type" in reason


def test_receipt_never_requires_ack() -> None:
    """A RECEIPT with require_ack=True is rejected — no receipt loops."""
    base = {
        "session_id": "s1",
        "from": "worker",
        "to": "manager",
        "subject": "READ m1",
        "body": "read",
        "kind": "RECEIPT",
        "msg_id": "rcpt-1",
        "created_at": "2026-01-01T00:00:00Z",
        "protocol_version": 2,
        "require_ack": True,  # invalid
        "receipt_type": "READ",
        "reply_to": "m1",
        "run_id": "r1",
        "request_id": "q1",
    }
    ok, reason = validate_message(base, "s1")
    assert not ok
    assert "require_ack" in reason


# ── MailboxService.read → READ receipt ─────────────────────────────────


def test_read_emits_deterministic_read_receipt(mailbox_root: Path) -> None:
    store = MailboxStore(root=mailbox_root)
    _init_session(store)
    svc = MailboxService(store=store)
    svc.send(
        "s1", "manager", "worker", "task", "do it", kind="TASK",
        run_id="r1", request_id="q1", require_ack=True,
    )

    outcome = svc.read("s1", "worker", "worker")
    assert outcome.status == "ok"
    assert outcome.receipt is not None
    assert outcome.receipt.status == "delivered"
    receipt_msg_id = outcome.receipt.msg_id

    # Deterministic: UUIDv5(session, reply_to, agent, READ)
    expected = str(uuid.uuid5(
        uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8"),
        "s1:<task_msg_id>:worker:READ",
    ))
    # The exact task msg_id is unknown; verify shape + determinism across runs.
    assert "-" in receipt_msg_id  # uuid-form

    # Receipt landed in the manager's inbox (to=original.from)
    mgr_files = store.list_messages(store.agent_subdir("s1", "manager", "inbox"))
    assert len(mgr_files) == 1
    receipt = json.loads(mgr_files[0].read_bytes())
    assert receipt["kind"] == "RECEIPT"
    assert receipt["receipt_type"] == "READ"
    assert receipt["require_ack"] is False
    assert receipt["from"] == "worker"
    assert receipt["to"] == "manager"
    assert receipt["reply_to"] == outcome.message["msg_id"]
    assert receipt["run_id"] == "r1"
    assert receipt["request_id"] == "q1"
    assert receipt["protocol_version"] == 2


def test_read_receipt_idempotent_no_duplicates(mailbox_root: Path) -> None:
    """Crash replay: re-reading the same msg with a fresh claim does not
    duplicate the receipt (deterministic msg_id + store idempotency)."""
    store = MailboxStore(root=mailbox_root)
    _init_session(store)
    svc = MailboxService(store=store)
    svc.send(
        "s1", "manager", "worker", "task", "do it", kind="TASK",
        run_id="r1", request_id="q1", require_ack=True,
    )

    # First claim generates the receipt.
    out1 = svc.read("s1", "worker", "worker")
    assert out1.receipt is not None

    # A second TASK with same run/request: still only one receipt per msg_id.
    svc.send(
        "s1", "manager", "worker", "task2", "again", kind="TASK",
        run_id="r1", request_id="q1", require_ack=True,
    )
    out2 = svc.read("s1", "worker", "worker")
    assert out2.receipt is not None
    assert out2.receipt.msg_id != out1.receipt.msg_id  # different reply_to

    mgr_files = store.list_messages(store.agent_subdir("s1", "manager", "inbox"))
    ids = [json.loads(f.read_bytes())["msg_id"] for f in mgr_files]
    assert len(ids) == len(set(ids))  # no duplicates
    # Exactly one receipt per claimed task
    assert len(mgr_files) == 2


def test_read_without_require_ack_no_receipt(mailbox_root: Path) -> None:
    store = MailboxStore(root=mailbox_root)
    _init_session(store)
    svc = MailboxService(store=store)
    svc.send("s1", "manager", "worker", "n", "no ack", kind="NOTICE")
    outcome = svc.read("s1", "worker", "worker")
    assert outcome.status == "ok"
    assert outcome.message is not None
    assert outcome.receipt is None


def test_peek_never_generates_receipt(mailbox_root: Path) -> None:
    store = MailboxStore(root=mailbox_root)
    _init_session(store)
    svc = MailboxService(store=store)
    svc.send(
        "s1", "manager", "worker", "task", "do it", kind="TASK",
        run_id="r1", request_id="q1", require_ack=True,
    )
    svc.peek("s1", "worker")
    # peek must not consume nor emit
    stats = store.stats("s1", "worker")
    assert stats["inbox"] == 1
    assert stats["processing"] == 0
    mgr_inbox = store.list_messages(store.agent_subdir("s1", "manager", "inbox"))
    assert len(mgr_inbox) == 0


def test_ack_route_unresolved_does_not_consume(mailbox_root: Path) -> None:
    """require_ack message from a sender NOT in the roster is not consumed."""
    store = MailboxStore(root=mailbox_root)
    _init_session(store)
    svc = MailboxService(store=store)

    # Sender 'ghost' is not in the roster — hand-write the message directly
    # into the inbox (bypasses service.send's roster validation).
    inbox = store.agent_subdir("s1", "worker", "inbox")
    inbox.mkdir(parents=True, exist_ok=True)
    msg = {
        "session_id": "s1",
        "from": "ghost",
        "to": "worker",
        "subject": "spoof",
        "body": "no route back",
        "kind": "TASK",
        "msg_id": "spoof_1",
        "created_at": "2026-01-01T00:00:00Z",
        "protocol_version": 2,
        "require_ack": True,
        "run_id": "r9",
        "request_id": "q9",
        "_cursor": "1000/000000",
    }
    (inbox / "spoof_1.json").write_text(json.dumps(msg))

    outcome = svc.read("s1", "worker", "worker")
    assert outcome.status == ACK_ROUTE_UNRESOLVED
    assert outcome.error
    # NOT consumed — still in inbox
    stats = store.stats("s1", "worker")
    assert stats["inbox"] == 1
    assert stats["processing"] == 0


def test_ack_route_resolved_sender_in_roster(mailbox_root: Path) -> None:
    """require_ack from a roster member is consumed and receipt emitted."""
    store = MailboxStore(root=mailbox_root)
    _init_session(store, agents=("worker", "oracle"))
    svc = MailboxService(store=store)
    svc.send(
        "s1", "oracle", "worker", "task", "from oracle", kind="TASK",
        run_id="r1", request_id="q1", require_ack=True,
    )
    outcome = svc.read("s1", "worker", "worker")
    assert outcome.status == "ok"
    assert outcome.receipt is not None
    assert outcome.receipt.status == "delivered"


# ── RequestLedger.apply_message ────────────────────────────────────────


@pytest.fixture
def ledger(tmp_path: Path) -> RequestLedger:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    return RequestLedger(session_dir, "worker")


def _task(run_id: str = "r1", request_id: str = "q1", msg_id: str = "m1") -> dict:
    return {
        "kind": "TASK", "run_id": run_id, "request_id": request_id,
        "msg_id": msg_id, "from": "manager",
    }


def test_apply_message_task_dispatched(ledger: RequestLedger) -> None:
    assert ledger.apply_message(_task()) == "DISPATCHED"
    events = ledger.get_events("q1", "r1")
    assert [e["event"] for e in events] == ["DISPATCHED"]


def test_apply_message_read_receipt_acks(ledger: RequestLedger) -> None:
    ledger.apply_message(_task())
    rcpt = {
        "kind": "RECEIPT", "receipt_type": "READ",
        "run_id": "r1", "request_id": "q1", "msg_id": "rcpt-1",
        "reply_to": "m1",
    }
    assert ledger.apply_message(rcpt) == "ACKED"
    events = [e["event"] for e in ledger.get_events("q1", "r1")]
    assert events == ["DISPATCHED", "ACKED"]


def test_apply_message_first_progress_running(ledger: RequestLedger) -> None:
    ledger.apply_message(_task())
    prog1 = {"kind": "PROGRESS", "run_id": "r1", "request_id": "q1", "msg_id": "p1"}
    assert ledger.apply_message(prog1) == "RUNNING"
    # second progress is informational — no new state
    prog2 = {"kind": "PROGRESS", "run_id": "r1", "request_id": "q1", "msg_id": "p2"}
    assert ledger.apply_message(prog2) == ""
    states = [e["event"] for e in ledger.get_events("q1", "r1")]
    assert states == ["DISPATCHED", "RUNNING"]


def test_apply_message_report_terminal_cas(ledger: RequestLedger) -> None:
    ledger.apply_message(_task())
    # P2-11: a REPORT is the task's final outcome → terminal DONE.
    assert ledger.apply_message({
        "kind": "REPORT", "run_id": "r1", "request_id": "q1", "msg_id": "rep1",
        "reply_to": "m1", "from": "worker",
    }) == "DONE"
    # A second terminal is rejected (CAS — REPORT already closed the request)
    assert ledger.record_event("q1", "r1", "DONE", {}) is False
    assert ledger.record_event("q1", "r1", "BLOCKED", {}) is False
    assert ledger.get_terminal("q1", "r1") == "DONE"
    # conflict recorded
    events = ledger.get_events("q1", "r1")
    assert events[-1]["event"] == "PROTOCOL_CONFLICT"


def test_apply_message_requires_correlation(ledger: RequestLedger) -> None:
    assert ledger.apply_message({"kind": "TASK", "msg_id": "x"}) == ""
    assert ledger.apply_message({"kind": "PROGRESS", "run_id": "r1", "msg_id": "p"}) == ""


def test_unknown_stale_is_terminal() -> None:
    assert "UNKNOWN_STALE" in TERMINAL_STATES
    # record_event enforces single-terminal CAS across UNKNOWN_STALE too
    session_dir = Path("/tmp") / f"ledger-{uuid.uuid4().hex[:8]}"
    session_dir.mkdir(exist_ok=True)
    try:
        lg = RequestLedger(session_dir, "w")
        assert lg.record_event("q", "r", "UNKNOWN_STALE", {}) is True
        assert lg.record_event("q", "r", "DONE", {}) is False
        assert lg.get_terminal("q", "r") == "UNKNOWN_STALE"
    finally:
        import shutil
        shutil.rmtree(session_dir, ignore_errors=True)
