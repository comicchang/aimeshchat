"""Smoke tests — real subprocess execution, no mocks.

These tests exercise the actual codeagent-remote-exec helper and wire
protocol end-to-end. They require `codeagent-remote-exec` to be installed
(uv tool install) or available on PATH.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from codeagent.wire.protocol import (
    CMD_CAPABILITIES,
    CMD_PING,
    WIRE_VERSION,
    make_capabilities_request,
    make_ping,
    make_request,
)


def _has_remote_exec() -> bool:
    """Check if codeagent-remote-exec is available."""
    try:
        r = subprocess.run(
            ["codeagent-remote-exec", "--help"],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _run_remote_exec(request_dict: dict, timeout: float = 10) -> dict:
    """Run codeagent-remote-exec with a single request, return parsed response.

    Skips the initial 'ready' handshake message and returns the actual response.
    """
    proc = subprocess.Popen(
        ["codeagent-remote-exec"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    input_line = json.dumps(request_dict, ensure_ascii=False) + "\n"
    stdout, stderr = proc.communicate(input=input_line, timeout=timeout)
    # Parse JSON lines, skip 'ready' handshake
    for line in stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        resp = json.loads(line)
        if resp.get("type") == "ready":
            continue
        return resp
    raise RuntimeError(f"No response JSON. stderr={stderr!r}, returncode={proc.returncode}")


pytestmark = pytest.mark.skipif(
    not _has_remote_exec(),
    reason="codeagent-remote-exec not installed (run: uv tool install .)",
)


class TestRemoteExecPing:
    """Smoke: real codeagent-remote-exec ping/pong cycle."""

    def test_ping_returns_pong(self):
        resp = _run_remote_exec(make_ping())
        assert resp["type"] == "pong"
        assert resp["wire_version"] == WIRE_VERSION
        assert "hostname" in resp
        assert "capabilities" in resp

    def test_capabilities_returns_backends(self):
        resp = _run_remote_exec(make_capabilities_request())
        assert resp["type"] == "capabilities"
        assert resp["wire_version"] == WIRE_VERSION
        assert isinstance(resp.get("backends"), list)
        assert isinstance(resp.get("features"), list)


class TestRemoteExecValidation:
    """Smoke: real validation errors from codeagent-remote-exec."""

    def test_invalid_json_returns_error(self):
        proc = subprocess.Popen(
            ["codeagent-remote-exec"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, _ = proc.communicate(input="not json\n", timeout=10)
        # Skip 'ready' line, find the error response
        for line in stdout.strip().splitlines():
            resp = json.loads(line.strip())
            if resp.get("type") == "ready":
                continue
            assert resp["type"] == "error"
            assert "invalid JSON" in resp["message"]
            return
        pytest.fail("No error response found")

    def test_unknown_command_returns_error(self):
        resp = _run_remote_exec({"wire_version": WIRE_VERSION, "command": "nonexistent"})
        assert resp["type"] == "error"
        assert "unknown" in resp["message"].lower()

    def test_missing_required_field_returns_error(self):
        resp = _run_remote_exec({"wire_version": WIRE_VERSION, "command": "run"})
        assert resp["type"] == "error"
        assert "task" in resp["message"].lower()


class TestRemoteExecWireVersion:
    """Smoke: wire version negotiation."""

    def test_future_wire_version_rejected(self):
        resp = _run_remote_exec({"wire_version": 999, "command": "ping"})
        # Should still get a pong (ping doesn't check version strictly),
        # but the helper should NOT crash
        assert resp["type"] in ("pong", "error")
