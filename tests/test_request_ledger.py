"""Tests for RequestLedger — append-only event ledger with terminal CAS."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from codeagent.mailbox.store import RequestLedger


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def ledger(tmp_path: Path) -> RequestLedger:
    """Fresh ledger rooted in a temp directory."""
    return RequestLedger(session_dir=tmp_path, agent_id="test-agent")


# ═══════════════════════════════════════════════════════════════════════════
# Terminal CAS
# ═══════════════════════════════════════════════════════════════════════════


class TestTerminalCAS:
    """Exactly one terminal state per (request_id, run_id)."""

    def test_first_terminal_accepted(self, ledger: RequestLedger) -> None:
        """First terminal event (DONE) is accepted."""
        ok = ledger.record_event("r1", "run1", "DONE", {"result": "ok"})
        assert ok is True
        assert ledger.get_terminal("r1", "run1") == "DONE"

    def test_second_terminal_rejected_protocol_conflict(self, ledger: RequestLedger) -> None:
        """Second terminal event is frozen as PROTOCOL_CONFLICT."""
        ledger.record_event("r1", "run1", "DONE", {"result": "ok"})
        ok = ledger.record_event("r1", "run1", "BLOCKED", {"reason": "late"})
        assert ok is False  # PROTOCOL_CONFLICT — first terminal wins
        # Terminal state unchanged
        assert ledger.get_terminal("r1", "run1") == "DONE"


# ═══════════════════════════════════════════════════════════════════════════
# Non-terminal events
# ═══════════════════════════════════════════════════════════════════════════


class TestNonTerminalEvents:
    """Non-terminal events are always accepted regardless of prior state."""

    def test_non_terminal_events_always_accepted(self, ledger: RequestLedger) -> None:
        """DISPATCHED, ACKED, RUNNING are always appended."""
        assert ledger.record_event("r1", "run1", "DISPATCHED", {}) is True
        assert ledger.record_event("r1", "run1", "ACKED", {}) is True
        assert ledger.record_event("r1", "run1", "RUNNING", {}) is True
        events = ledger.get_events("r1", "run1")
        assert len(events) == 3
        assert [e["event"] for e in events] == ["DISPATCHED", "ACKED", "RUNNING"]

    def test_non_terminal_after_terminal_accepted(self, ledger: RequestLedger) -> None:
        """Non-terminal events still append after a terminal — ledger is append-only."""
        ledger.record_event("r1", "run1", "DONE", {})
        assert ledger.record_event("r1", "run1", "RUNNING", {"late": True}) is True
        events = ledger.get_events("r1", "run1")
        assert len(events) == 2


# ═══════════════════════════════════════════════════════════════════════════
# Watchdog — find stale ACKED requests
# ═══════════════════════════════════════════════════════════════════════════


class TestFindStale:
    """Watchdog trigger: ACKED without terminal within SLA."""

    def test_find_stale_returns_expired_acked(self, ledger: RequestLedger) -> None:
        """Requests stuck in ACKED beyond SLA are reported."""
        # Record an old ACKED event (backdate via the JSONL file)
        ledger.record_event("r-old", "run1", "ACKED", {})
        # Manually backdate the event timestamp in the ledger file
        ledger_dir = ledger._events_dir("r-old")
        events_file = ledger_dir / "events.jsonl"
        lines = events_file.read_text().strip().splitlines()
        event = json.loads(lines[0])
        event["ts"] = time.time() - 600  # 10 minutes ago
        events_file.write_text(json.dumps(event) + "\n")

        # Fresh ACKED — should not appear
        ledger.record_event("r-fresh", "run1", "ACKED", {})

        stale = ledger.find_stale(sla_seconds=300)
        stale_ids = [s["request_id"] for s in stale]
        assert "r-old" in stale_ids
        assert "r-fresh" not in stale_ids

    def test_find_stale_skips_terminal(self, ledger: RequestLedger) -> None:
        """Requests that already reached terminal are not stale."""
        ledger.record_event("r-done", "run1", "ACKED", {})
        ledger.record_event("r-done", "run1", "DONE", {})
        stale = ledger.find_stale(sla_seconds=0)
        stale_ids = [s["request_id"] for s in stale]
        assert "r-done" not in stale_ids


# ═══════════════════════════════════════════════════════════════════════════
# Terminal retrieval
# ═══════════════════════════════════════════════════════════════════════════


class TestGetTerminal:
    """Terminal state lookup."""

    def test_get_terminal_returns_correct_state(self, ledger: RequestLedger) -> None:
        """Returns the first terminal event type recorded."""
        ledger.record_event("r1", "run1", "DISPATCHED", {})
        assert ledger.get_terminal("r1", "run1") is None
        ledger.record_event("r1", "run1", "BLOCKED", {"reason": "no-worker"})
        assert ledger.get_terminal("r1", "run1") == "BLOCKED"

    def test_get_terminal_none_for_unknown(self, ledger: RequestLedger) -> None:
        """Unknown request returns None."""
        assert ledger.get_terminal("nonexistent", "run1") is None
