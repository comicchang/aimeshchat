"""Tests for cross-device write merge + PROTOCOL_CONFLICT (TASK C2)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeagent.gateway.model import ERR_NOT_FOUND, ERR_PROTOCOL_CONFLICT, GatewayError
from codeagent.gateway.service import AgentGateway
from codeagent.mailbox.store import MailboxStore


# ── write_parse_body ───────────────────────────────────────────────────


class TestWriteParseBody:
    """Test the static ``write_parse_body`` helper."""

    def test_valid_body(self):
        body = json.dumps({
            "base_revision": "abc123",
            "target_path": "src/foo.py",
            "artifact_id": "art-001",
        })
        result = AgentGateway.write_parse_body(body)
        assert result == {
            "base_revision": "abc123",
            "target_path": "src/foo.py",
            "artifact_id": "art-001",
        }

    def test_partial_body(self):
        body = json.dumps({"target_path": "src/bar.py", "artifact_id": "art-002"})
        result = AgentGateway.write_parse_body(body)
        assert result == {"target_path": "src/bar.py", "artifact_id": "art-002"}
        assert "base_revision" not in result

    def test_extra_fields_ignored(self):
        body = json.dumps({
            "base_revision": "r1",
            "target_path": "x",
            "artifact_id": "y",
            "noise": 42,
        })
        result = AgentGateway.write_parse_body(body)
        assert "noise" not in result
        assert result["base_revision"] == "r1"

    def test_empty_string_returns_empty(self):
        assert AgentGateway.write_parse_body("") == {}

    def test_invalid_json_returns_empty(self):
        assert AgentGateway.write_parse_body("{not json") == {}

    def test_non_dict_json_returns_empty(self):
        assert AgentGateway.write_parse_body('"just a string"') == {}
        assert AgentGateway.write_parse_body("[1, 2]") == {}

    def test_empty_fields_ignored(self):
        body = json.dumps({"base_revision": "", "target_path": "", "artifact_id": ""})
        assert AgentGateway.write_parse_body(body) == {}

    def test_non_string_fields_ignored(self):
        body = json.dumps({"base_revision": 123, "target_path": True, "artifact_id": []})
        assert AgentGateway.write_parse_body(body) == {}


# ── write_merge RPC ────────────────────────────────────────────────────


@pytest.fixture
def gw(tmp_path: Path) -> AgentGateway:
    """Fresh AgentGateway with isolated stores (no UDS, tmp peers/merges)."""
    import uuid as _uuid

    base = Path("/tmp") / f"gwmerge-{_uuid.uuid4().hex[:8]}"
    store = MailboxStore(root=base / "mailbox")
    store.root.mkdir(parents=True, exist_ok=True)
    return AgentGateway(
        store=store,
        restore_from_park=False,
        peers_file=base / "peers.json",
    )


class TestWriteMerge:
    """Test the ``write.merge`` RPC method."""

    def test_merge_success(self, gw: AgentGateway):
        params = {
            "session_id": "sess-1",
            "request_id": "req-1",
            "run_id": "run-1",
            "target_path": "src/foo.py",
            "base_revision": "abc",
            "artifact_sha256": "sha-aaa",
            "body": "{}",
        }
        result = gw.write_merge(params)
        assert result == {"merged": True}

    def test_merge_idempotent_same_sha(self, gw: AgentGateway):
        params = {
            "session_id": "sess-1",
            "target_path": "src/foo.py",
            "artifact_sha256": "sha-aaa",
        }
        gw.write_merge(params)
        result = gw.write_merge(params)
        assert result == {"merged": True}

    def test_merge_conflict_different_sha(self, gw: AgentGateway):
        params_a = {
            "session_id": "sess-1",
            "target_path": "src/foo.py",
            "artifact_sha256": "sha-aaa",
        }
        gw.write_merge(params_a)
        params_b = {
            "session_id": "sess-1",
            "target_path": "src/foo.py",
            "artifact_sha256": "sha-bbb",
        }
        with pytest.raises(GatewayError) as exc_info:
            gw.write_merge(params_b)
        assert exc_info.value.code == ERR_PROTOCOL_CONFLICT
        assert "sha-aaa" in exc_info.value.message
        assert "sha-bbb" in exc_info.value.message

    def test_merge_different_sessions_no_conflict(self, gw: AgentGateway):
        gw.write_merge({
            "session_id": "sess-1",
            "target_path": "src/foo.py",
            "artifact_sha256": "sha-aaa",
        })
        result = gw.write_merge({
            "session_id": "sess-2",
            "target_path": "src/foo.py",
            "artifact_sha256": "sha-bbb",
        })
        assert result == {"merged": True}

    def test_merge_different_paths_no_conflict(self, gw: AgentGateway):
        gw.write_merge({
            "session_id": "sess-1",
            "target_path": "src/foo.py",
            "artifact_sha256": "sha-aaa",
        })
        result = gw.write_merge({
            "session_id": "sess-1",
            "target_path": "src/bar.py",
            "artifact_sha256": "sha-bbb",
        })
        assert result == {"merged": True}

    def test_merge_missing_session_id(self, gw: AgentGateway):
        with pytest.raises(GatewayError) as exc_info:
            gw.write_merge({
                "target_path": "src/foo.py",
                "artifact_sha256": "sha-aaa",
            })
        assert exc_info.value.code == ERR_NOT_FOUND

    def test_merge_missing_target_path(self, gw: AgentGateway):
        with pytest.raises(GatewayError) as exc_info:
            gw.write_merge({
                "session_id": "sess-1",
                "artifact_sha256": "sha-aaa",
            })
        assert exc_info.value.code == ERR_NOT_FOUND

    def test_merge_missing_artifact_sha256(self, gw: AgentGateway):
        with pytest.raises(GatewayError) as exc_info:
            gw.write_merge({
                "session_id": "sess-1",
                "target_path": "src/foo.py",
            })
        assert exc_info.value.code == ERR_NOT_FOUND

    def test_dispatch_routes_write_merge(self, gw: AgentGateway):
        result = gw.dispatch("write.merge", {
            "session_id": "s1",
            "target_path": "t.py",
            "artifact_sha256": "sha-ok",
        })
        assert result == {"merged": True}

    def test_dispatch_conflict_via_dispatch(self, gw: AgentGateway):
        gw.dispatch("write.merge", {
            "session_id": "s1",
            "target_path": "t.py",
            "artifact_sha256": "sha-1",
        })
        with pytest.raises(GatewayError) as exc_info:
            gw.dispatch("write.merge", {
                "session_id": "s1",
                "target_path": "t.py",
                "artifact_sha256": "sha-2",
            })
        assert exc_info.value.code == ERR_PROTOCOL_CONFLICT
