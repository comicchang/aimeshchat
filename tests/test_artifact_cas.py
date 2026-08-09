"""Tests for artifact.verify RPC — terminal CAS in RequestLedger.

Covers: verify success → DONE, sha256 mismatch → BLOCKED,
second verify → terminal CAS rejection (EXISTS), file-not-found → error.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from codeagent.gateway.model import GatewayError
from codeagent.gateway.service import AgentGateway
from codeagent.mailbox.store import MailboxStore, RequestLedger


# ── helpers ────────────────────────────────────────────────────────────


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_artifact(tmp_path: Path, name: str = "out.bin", content: bytes = b"hello") -> tuple[Path, str, int]:
    """Write a file and return (path, sha256, size)."""
    p = tmp_path / name
    p.write_bytes(content)
    return p, _sha256(content), len(content)


def _setup_session(tmp_path: Path) -> tuple[MailboxStore, str, str, str, str]:
    """Return (store, session_id, agent_id, request_id, run_id) with dirs created."""
    root = tmp_path / "mailbox"
    root.mkdir()
    store = MailboxStore(root=root)
    session_id = "sess-001"
    agent_id = "agent-001"
    request_id = "req-abc"
    run_id = "run-1"
    # Pre-create the agent events dir so artifact_verify can discover it.
    (root / session_id / agent_id / "events" / request_id).mkdir(parents=True)
    return store, session_id, agent_id, request_id, run_id


# ════════════════════════════════════════════════════════════════════════
# RequestLedger.record_artifact_verdict  (unit)
# ════════════════════════════════════════════════════════════════════════


class TestRecordArtifactVerdict:
    """Unit tests for the ledger method itself."""

    def test_verified_true_records_DONE(self, tmp_path: Path):
        store, sid, aid, rid, run_id = _setup_session(tmp_path)
        ledger = RequestLedger(store.session_dir(sid), aid)
        result = ledger.record_artifact_verdict(rid, run_id, verified=True)
        assert result == {"terminal": "DONE", "cas": True}
        assert ledger.get_terminal(rid, run_id) == "DONE"

    def test_verified_false_records_BLOCKED(self, tmp_path: Path):
        store, sid, aid, rid, run_id = _setup_session(tmp_path)
        ledger = RequestLedger(store.session_dir(sid), aid)
        result = ledger.record_artifact_verdict(rid, run_id, verified=False)
        assert result == {"terminal": "BLOCKED", "cas": True}
        assert ledger.get_terminal(rid, run_id) == "BLOCKED"

    def test_second_verdict_cas_conflict(self, tmp_path: Path):
        store, sid, aid, rid, run_id = _setup_session(tmp_path)
        ledger = RequestLedger(store.session_dir(sid), aid)
        ledger.record_artifact_verdict(rid, run_id, verified=True)
        # Second verdict — terminal CAS should reject.
        result = ledger.record_artifact_verdict(rid, run_id, verified=False)
        assert result["cas"] is False
        assert result["terminal"] == "DONE"  # original terminal preserved

    def test_second_verdict_same_state_cas_conflict(self, tmp_path: Path):
        """Even the same terminal state triggers CAS conflict (idempotent-safe)."""
        store, sid, aid, rid, run_id = _setup_session(tmp_path)
        ledger = RequestLedger(store.session_dir(sid), aid)
        ledger.record_artifact_verdict(rid, run_id, verified=True)
        result = ledger.record_artifact_verdict(rid, run_id, verified=True)
        assert result["cas"] is False
        assert result["terminal"] == "DONE"


# ════════════════════════════════════════════════════════════════════════
# AgentGateway.dispatch("artifact.verify")  (integration)
# ════════════════════════════════════════════════════════════════════════


class TestArtifactVerifyRPC:
    """Integration tests through the gateway dispatch table."""

    def _gw(self, store: MailboxStore) -> AgentGateway:
        return AgentGateway(store=store)

    def test_verify_success_returns_DONE(self, tmp_path: Path):
        store, sid, aid, rid, run_id = _setup_session(tmp_path)
        path, sha, size = _make_artifact(tmp_path)
        gw = self._gw(store)
        result = gw.dispatch("artifact.verify", {
            "session_id": sid,
            "request_id": rid,
            "run_id": run_id,
            "path": str(path),
            "sha256": sha,
            "size": size,
            "agent_id": aid,
        })
        assert result["verified"] is True
        assert result["terminal"] == "DONE"
        assert "status" not in result  # no CAS conflict

    def test_verify_sha_mismatch_returns_BLOCKED(self, tmp_path: Path):
        store, sid, aid, rid, run_id = _setup_session(tmp_path)
        path, _, size = _make_artifact(tmp_path)
        gw = self._gw(store)
        result = gw.dispatch("artifact.verify", {
            "session_id": sid,
            "request_id": rid,
            "run_id": run_id,
            "path": str(path),
            "sha256": "0" * 64,  # wrong hash
            "size": size,
            "agent_id": aid,
        })
        assert result["verified"] is False
        assert result["terminal"] == "BLOCKED"

    def test_second_verify_returns_EXISTS(self, tmp_path: Path):
        store, sid, aid, rid, run_id = _setup_session(tmp_path)
        path, sha, size = _make_artifact(tmp_path)
        gw = self._gw(store)
        params = {
            "session_id": sid,
            "request_id": rid,
            "run_id": run_id,
            "path": str(path),
            "sha256": sha,
            "size": size,
            "agent_id": aid,
        }
        gw.dispatch("artifact.verify", params)
        # Second call — terminal CAS rejects.
        result = gw.dispatch("artifact.verify", params)
        assert result["terminal"] == "DONE"
        assert result["status"] == "EXISTS"

    def test_file_not_found_raises(self, tmp_path: Path):
        store, sid, aid, rid, run_id = _setup_session(tmp_path)
        gw = self._gw(store)
        with pytest.raises(ValueError, match="not a file"):
            gw.dispatch("artifact.verify", {
                "session_id": sid,
                "request_id": rid,
                "run_id": run_id,
                "path": str(tmp_path / "nonexistent.bin"),
                "sha256": "a" * 64,
                "size": 100,
                "agent_id": aid,
            })

    def test_missing_params_raises_PROTOCOL(self, tmp_path: Path):
        store, sid, aid, rid, run_id = _setup_session(tmp_path)
        gw = self._gw(store)
        with pytest.raises(GatewayError) as exc_info:
            gw.dispatch("artifact.verify", {
                "session_id": sid,
                # request_id missing
                "run_id": run_id,
                "path": "/tmp/x",
                "sha256": "a" * 64,
                "size": 10,
            })
        assert exc_info.value.code == "PROTOCOL"

    def test_agent_id_inferred_from_session_dir(self, tmp_path: Path):
        """When agent_id is omitted, the service scans session_dir to find it."""
        store, sid, aid, rid, run_id = _setup_session(tmp_path)
        path, sha, size = _make_artifact(tmp_path)
        gw = self._gw(store)
        result = gw.dispatch("artifact.verify", {
            "session_id": sid,
            "request_id": rid,
            "run_id": run_id,
            "path": str(path),
            "sha256": sha,
            "size": size,
            # agent_id omitted — should be inferred
        })
        assert result["verified"] is True
        assert result["terminal"] == "DONE"

    def test_no_agent_for_request_raises_NOT_FOUND(self, tmp_path: Path):
        """When no agent directory contains the request_id, ERR_NOT_FOUND."""
        root = tmp_path / "mailbox"
        root.mkdir()
        store = MailboxStore(root=root)
        sid = "sess-002"
        # Create session dir but no agent with matching events.
        (root / sid).mkdir()
        gw = self._gw(store)
        with pytest.raises(GatewayError) as exc_info:
            gw.dispatch("artifact.verify", {
                "session_id": sid,
                "request_id": "req-ghost",
                "run_id": "run-1",
                "path": "/tmp/x",
                "sha256": "a" * 64,
                "size": 10,
            })
        assert exc_info.value.code == "NOT_FOUND"
