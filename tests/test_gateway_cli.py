"""Unit tests for codeagent.gateway.cli — all command functions, ≥50% line coverage.

Every external dependency is mocked at the module boundary so no real tmux,
UDS, subprocess, or network calls are made.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

# Module under test — imported once; patches target this module object.
import codeagent.gateway.cli as cli_mod


# ── helpers ────────────────────────────────────────────────────────────


def _ns(**kw) -> argparse.Namespace:
    """Build a minimal args namespace with sane defaults."""
    defaults = dict(
        host=None, timeout=3.0, stdio=False, method="",
        params="", cursor=None, filters=None, session=None,
        runtime_id=None, limit=50, interval=0.1, jsonl=False,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


# ═══════════════════════════════════════════════════════════════════════
#  _gateway_running
# ═══════════════════════════════════════════════════════════════════════


class TestGatewayRunning:
    def test_true_when_call_succeeds(self):
        with mock.patch.object(cli_mod, "GatewayClient") as MockClient:
            MockClient.return_value.call.return_value = {}
            assert cli_mod._gateway_running() is True

    def test_false_when_call_raises(self):
        with mock.patch.object(cli_mod, "GatewayClient") as MockClient:
            MockClient.return_value.call.side_effect = RuntimeError("down")
            assert cli_mod._gateway_running() is False


# ═══════════════════════════════════════════════════════════════════════
#  cmd_gateway_start
# ═══════════════════════════════════════════════════════════════════════


class TestCmdGatewayStart:
    def test_already_running(self, capsys):
        with mock.patch.object(cli_mod, "_gateway_running", return_value=True):
            assert cli_mod.cmd_gateway_start(_ns()) == 0
        assert "already running" in capsys.readouterr().out

    def test_stale_socket_same_uid(self, tmp_path, capsys):
        sock = tmp_path / "control.sock"
        sock.touch()
        with (
            mock.patch.object(cli_mod, "_gateway_running", side_effect=[False, True]),
            mock.patch.object(cli_mod, "control_socket_path", return_value=sock),
            mock.patch.object(cli_mod, "_socket_owner_is_self", return_value=True),
            mock.patch.object(cli_mod, "_tmux_session_alive", return_value=False),
            mock.patch.object(cli_mod, "ensure_tmux_server", return_value=True),
            mock.patch.object(cli_mod, "_find_gateway_pane", return_value=None),
            mock.patch.object(cli_mod, "_tmux_new_gateway_pane", return_value=(0, "%1", "")),
            mock.patch.object(cli_mod.time, "monotonic", side_effect=[0.0, 1.0]),
            mock.patch.object(cli_mod.time, "sleep"),
        ):
            assert cli_mod.cmd_gateway_start(_ns()) == 0
        assert not sock.exists()
        out = capsys.readouterr().out
        assert "removing stale socket" in out
        assert "started" in out

    def test_refuse_non_same_uid_socket(self, tmp_path, capsys):
        sock = tmp_path / "control.sock"
        sock.touch()
        with (
            mock.patch.object(cli_mod, "_gateway_running", return_value=False),
            mock.patch.object(cli_mod, "control_socket_path", return_value=sock),
            mock.patch.object(cli_mod, "_socket_owner_is_self", return_value=False),
        ):
            assert cli_mod.cmd_gateway_start(_ns()) == 1
        assert "refusing" in capsys.readouterr().err

    def test_tmux_server_fails(self, capsys):
        with (
            mock.patch.object(cli_mod, "_gateway_running", return_value=False),
            mock.patch.object(cli_mod, "control_socket_path", return_value=Path("/no/sock")),
            mock.patch.object(cli_mod, "ensure_tmux_server", return_value=False),
        ):
            assert cli_mod.cmd_gateway_start(_ns()) == 1
        assert "cannot start private tmux server" in capsys.readouterr().err

    def test_pane_creation_fails(self, capsys):
        with (
            mock.patch.object(cli_mod, "_gateway_running", return_value=False),
            mock.patch.object(cli_mod, "control_socket_path", return_value=Path("/no/sock")),
            mock.patch.object(cli_mod, "ensure_tmux_server", return_value=True),
            mock.patch.object(cli_mod, "_find_gateway_pane", return_value=None),
            mock.patch.object(cli_mod, "_tmux_new_gateway_pane", return_value=(1, "", "pane err")),
        ):
            assert cli_mod.cmd_gateway_start(_ns()) == 1
        assert "pane creation failed" in capsys.readouterr().err

    def test_kills_stale_pane_then_creates_fresh(self, capsys):
        with (
            mock.patch.object(cli_mod, "_gateway_running", side_effect=[False, True]),
            mock.patch.object(cli_mod, "control_socket_path", return_value=Path("/no/sock")),
            mock.patch.object(cli_mod, "ensure_tmux_server", return_value=True),
            mock.patch.object(cli_mod, "_find_gateway_pane", return_value="%3"),
            mock.patch.object(cli_mod, "_tmux") as mock_tmux,
            mock.patch.object(cli_mod, "_tmux_new_gateway_pane", return_value=(0, "%5", "")),
            mock.patch.object(cli_mod.time, "monotonic", side_effect=[0.0, 1.0]),
            mock.patch.object(cli_mod.time, "sleep"),
        ):
            assert cli_mod.cmd_gateway_start(_ns()) == 0
        mock_tmux.assert_called_with("kill-pane", "-t", "%3")

    def test_timeout_waiting_for_uds(self, capsys):
        with (
            mock.patch.object(cli_mod, "_gateway_running", return_value=False),
            mock.patch.object(cli_mod, "control_socket_path", return_value=Path("/no/sock")),
            mock.patch.object(cli_mod, "ensure_tmux_server", return_value=True),
            mock.patch.object(cli_mod, "_find_gateway_pane", return_value=None),
            mock.patch.object(cli_mod, "_tmux_new_gateway_pane", return_value=(0, "%1", "")),
            mock.patch.object(cli_mod.time, "monotonic", side_effect=[0.0, 100.0]),
            mock.patch.object(cli_mod.time, "sleep"),
        ):
            assert cli_mod.cmd_gateway_start(_ns()) == 1
        assert "not yet responding" in capsys.readouterr().err


# ═══════════════════════════════════════════════════════════════════════
#  cmd_gateway_status
# ═══════════════════════════════════════════════════════════════════════


class TestCmdGatewayStatus:
    def test_not_running(self, capsys):
        with mock.patch.object(cli_mod, "_gateway_running", return_value=False):
            assert cli_mod.cmd_gateway_status(_ns()) == 1
        assert "not running" in capsys.readouterr().out

    def test_running_ok(self, capsys, tmp_path):
        sock = tmp_path / "control.sock"
        caps = {"version": "1.0.0", "runtimes": ["opencode"]}
        with (
            mock.patch.object(cli_mod, "_gateway_running", return_value=True),
            mock.patch.object(cli_mod, "GatewayClient") as MockClient,
            mock.patch.object(cli_mod, "control_socket_path", return_value=sock),
        ):
            MockClient.return_value.call.return_value = caps
            assert cli_mod.cmd_gateway_status(_ns()) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "running"
        assert out["version"] == "1.0.0"
        assert out["runtimes"] == ["opencode"]

    def test_gateway_error(self, capsys):
        with (
            mock.patch.object(cli_mod, "_gateway_running", return_value=True),
            mock.patch.object(cli_mod, "GatewayClient") as MockClient,
        ):
            MockClient.return_value.call.side_effect = cli_mod.GatewayError("X", "boom")
            assert cli_mod.cmd_gateway_status(_ns()) == 1
        assert "boom" in capsys.readouterr().err


# ═══════════════════════════════════════════════════════════════════════
#  cmd_gateway_stop
# ═══════════════════════════════════════════════════════════════════════


class TestCmdGatewayStop:
    def test_not_running(self, capsys):
        with mock.patch.object(cli_mod, "_gateway_running", return_value=False):
            assert cli_mod.cmd_gateway_stop(_ns()) == 0
        assert "not running" in capsys.readouterr().out

    def test_stop_success(self, capsys, tmp_path):
        sock = tmp_path / "control.sock"
        sock.touch()
        with (
            mock.patch.object(cli_mod, "_gateway_running", return_value=True),
            mock.patch.object(cli_mod, "GatewayClient") as MockClient,
            mock.patch.object(cli_mod, "_tmux", return_value=(0, "", "")),
            mock.patch.object(cli_mod, "control_socket_path", return_value=sock),
        ):
            assert cli_mod.cmd_gateway_stop(_ns()) == 0
        assert not sock.exists()
        assert "stopped" in capsys.readouterr().out

    def test_kill_window_fails_kill_session_ok(self, capsys, tmp_path):
        sock = tmp_path / "control.sock"
        with (
            mock.patch.object(cli_mod, "_gateway_running", return_value=True),
            mock.patch.object(cli_mod, "GatewayClient") as MockClient,
            mock.patch.object(cli_mod, "_tmux", side_effect=[
                (1, "", "no window"),   # kill-window fails
                (0, "", ""),            # kill-session ok
            ]),
            mock.patch.object(cli_mod, "control_socket_path", return_value=sock),
        ):
            assert cli_mod.cmd_gateway_stop(_ns()) == 0
        assert "stopped" in capsys.readouterr().out

    def test_both_kill_fail(self, capsys, tmp_path):
        with (
            mock.patch.object(cli_mod, "_gateway_running", return_value=True),
            mock.patch.object(cli_mod, "GatewayClient") as MockClient,
            mock.patch.object(cli_mod, "_tmux", side_effect=[
                (1, "", "no window"),
                (1, "", "no session"),
            ]),
            mock.patch.object(cli_mod, "control_socket_path", return_value=Path("/no/sock")),
        ):
            assert cli_mod.cmd_gateway_stop(_ns()) == 1
        assert "stop failed" in capsys.readouterr().err


# ═══════════════════════════════════════════════════════════════════════
#  cmd_gateway_serve
# ═══════════════════════════════════════════════════════════════════════


class TestCmdGatewayServe:
    def test_serve_forever(self):
        with mock.patch("codeagent.gateway.server.GatewayServer") as MockServer:
            mock_inst = MockServer.return_value
            mock_inst.serve_forever = mock.Mock()
            assert cli_mod.cmd_gateway_serve(_ns()) == 0
            mock_inst.serve_forever.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
#  cmd_gateway_rpc
# ═══════════════════════════════════════════════════════════════════════


class TestCmdGatewayRpc:
    def test_stdio_delegates(self):
        with mock.patch.object(cli_mod, "rpc_stdio", return_value=0) as mock_stdio:
            assert cli_mod.cmd_gateway_rpc(_ns(stdio=True)) == 0
            mock_stdio.assert_called_once()

    def test_non_stdio_success(self, capsys):
        with mock.patch.object(cli_mod, "GatewayClient") as MockClient:
            MockClient.return_value.call.return_value = {"answer": 42}
            assert cli_mod.cmd_gateway_rpc(_ns(method="test", params='{"a":1}')) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        assert out["result"]["answer"] == 42

    def test_non_stdio_gateway_error(self, capsys):
        with mock.patch.object(cli_mod, "GatewayClient") as MockClient:
            MockClient.return_value.call.side_effect = cli_mod.GatewayError("NOPE", "nope msg")
            assert cli_mod.cmd_gateway_rpc(_ns(method="test")) == 1
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False
        assert out["error"]["code"] == "NOPE"

    def test_non_stdio_no_params(self, capsys):
        with mock.patch.object(cli_mod, "GatewayClient") as MockClient:
            MockClient.return_value.call.return_value = {}
            assert cli_mod.cmd_gateway_rpc(_ns(method="m", params="")) == 0
        MockClient.return_value.call.assert_called_once_with("m", {})


# ═══════════════════════════════════════════════════════════════════════
#  cmd_events_watch
# ═══════════════════════════════════════════════════════════════════════


class TestCmdEventsWatch:
    def test_jsonl_output(self, capsys):
        events = [{"event_id": 1, "kind": "TASK", "runtime_id": "r1", "created_at": "now"}]
        result = {"events": events, "cursor": 10}
        with (
            mock.patch.object(cli_mod, "GatewayClient") as MockClient,
            mock.patch.object(cli_mod.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            MockClient.return_value.call.return_value = result
            assert cli_mod.cmd_events_watch(_ns(jsonl=True, interval=0)) == 0
        out_lines = capsys.readouterr().out.strip().splitlines()
        assert len(out_lines) == 1
        ev = json.loads(out_lines[0])
        assert ev["event_id"] == 1

    def test_plain_output(self, capsys):
        events = [{"event_id": 5, "kind": "ERROR", "runtime_id": "r2", "created_at": "t"}]
        result = {"events": events, "cursor": 20}
        with (
            mock.patch.object(cli_mod, "GatewayClient") as MockClient,
            mock.patch.object(cli_mod.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            MockClient.return_value.call.return_value = result
            assert cli_mod.cmd_events_watch(_ns(jsonl=False, interval=0)) == 0
        out = capsys.readouterr().out
        assert "[5]" in out
        assert "ERROR" in out

    def test_gateway_error_jsonl(self, capsys):
        with (
            mock.patch.object(cli_mod, "GatewayClient") as MockClient,
        ):
            MockClient.return_value.call.side_effect = cli_mod.GatewayError("DOWN", "down msg")
            assert cli_mod.cmd_events_watch(_ns(jsonl=True)) == 1
        out = json.loads(capsys.readouterr().out)
        assert out["type"] == "error"
        assert "down msg" in out["message"]

    def test_gateway_error_plain(self, capsys):
        with (
            mock.patch.object(cli_mod, "GatewayClient") as MockClient,
        ):
            MockClient.return_value.call.side_effect = cli_mod.GatewayError("DOWN", "down msg")
            assert cli_mod.cmd_events_watch(_ns(jsonl=False)) == 1
        assert "down msg" in capsys.readouterr().err

    def test_cursor_advances(self, capsys):
        """Second iteration uses cursor returned by first call."""
        call_count = 0

        def side_effect(method, params):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                assert params["cursor"] == 0
                return {"events": [{"event_id": 1, "kind": "K", "runtime_id": "r", "created_at": "t"}], "cursor": 42}
            # Second call: cursor should be 42
            assert params["cursor"] == 42
            return {"events": [], "cursor": 99}

        sleep_count = 0

        def sleep_side_effect(interval):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 2:
                raise KeyboardInterrupt

        with (
            mock.patch.object(cli_mod, "GatewayClient") as MockClient,
            mock.patch.object(cli_mod.time, "sleep", side_effect=sleep_side_effect),
        ):
            MockClient.return_value.call.side_effect = side_effect
            assert cli_mod.cmd_events_watch(_ns(jsonl=True, cursor=None, interval=0)) == 0
        assert call_count == 2


# ═══════════════════════════════════════════════════════════════════════
#  cmd_gateway_ensure
# ═══════════════════════════════════════════════════════════════════════


class TestCmdGatewayEnsure:
    def test_no_host(self, capsys):
        assert cli_mod.cmd_gateway_ensure(_ns(host=None)) == 1
        assert "requires --host" in capsys.readouterr().err

    def test_local_host_delegates_to_start(self):
        with (
            mock.patch("codeagent.config.repo_map.load_repo_map") as mock_map,
            mock.patch("codeagent.domain.resolve_is_local", return_value=True),
            mock.patch.object(cli_mod, "cmd_gateway_start", return_value=0) as mock_start,
        ):
            mock_map.return_value = SimpleNamespace(hosts={})
            assert cli_mod.cmd_gateway_ensure(_ns(host="localbox")) == 0
        mock_start.assert_called_once()

    def test_remote_upgrade_required(self, capsys):
        """Remote wire version < 2 yields REMOTE_UPGRADE_REQUIRED."""
        mock_cm = mock.Mock()
        mock_cm.is_alive.return_value = True
        mock_cm.ssh_cmd.return_value = ["ssh", "remote"]

        pong_line = json.dumps({"type": "pong", "wire_version": 1})
        mock_proc = mock.Mock()
        mock_proc.communicate.return_value = (pong_line.encode(), b"")

        with (
            mock.patch("codeagent.config.repo_map.load_repo_map") as mock_map,
            mock.patch("codeagent.domain.resolve_is_local", return_value=False),
            mock.patch("codeagent.transport.control_master.ControlMaster", return_value=mock_cm),
            mock.patch.object(cli_mod.subprocess, "Popen", return_value=mock_proc),
        ):
            mock_map.return_value = SimpleNamespace(hosts={})
            result = cli_mod.cmd_gateway_ensure(_ns(host="remotebox"))
        assert result == 1
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "REMOTE_UPGRADE_REQUIRED"

    def test_remote_wire_probe_fails(self, capsys):
        mock_cm = mock.Mock()
        mock_cm.is_alive.return_value = True
        mock_cm.ssh_cmd.return_value = ["ssh", "remote"]

        with (
            mock.patch("codeagent.config.repo_map.load_repo_map") as mock_map,
            mock.patch("codeagent.domain.resolve_is_local", return_value=False),
            mock.patch("codeagent.transport.control_master.ControlMaster", return_value=mock_cm),
            mock.patch.object(cli_mod.subprocess, "Popen", side_effect=OSError("no ssh")),
        ):
            mock_map.return_value = SimpleNamespace(hosts={})
            assert cli_mod.cmd_gateway_ensure(_ns(host="remotebox")) == 1
        assert "remote wire probe failed" in capsys.readouterr().err

    def test_remote_success(self, capsys):
        mock_cm = mock.Mock()
        mock_cm.is_alive.return_value = True
        mock_cm.ssh_cmd.return_value = ["ssh", "remote"]

        pong_line = json.dumps({"type": "pong", "wire_version": 3})
        mock_proc = mock.Mock()
        mock_proc.communicate.side_effect = [
            (pong_line.encode(), b""),   # wire probe
            ("gateway: started\n", ""),  # remote gateway start
        ]

        with (
            mock.patch("codeagent.config.repo_map.load_repo_map") as mock_map,
            mock.patch("codeagent.domain.resolve_is_local", return_value=False),
            mock.patch("codeagent.transport.control_master.ControlMaster", return_value=mock_cm),
            mock.patch.object(cli_mod.subprocess, "Popen", return_value=mock_proc),
            mock.patch("codeagent.gateway.remote.remote_gateway_call", return_value={"caps": True}),
        ):
            mock_map.return_value = SimpleNamespace(hosts={})
            assert cli_mod.cmd_gateway_ensure(_ns(host="remotebox")) == 0
        out = capsys.readouterr().out
        assert "ensured" in out

    def test_remote_gateway_call_fails(self, capsys):
        mock_cm = mock.Mock()
        mock_cm.is_alive.return_value = True
        mock_cm.ssh_cmd.return_value = ["ssh", "remote"]

        pong_line = json.dumps({"type": "pong", "wire_version": 2})
        mock_proc = mock.Mock()
        mock_proc.communicate.return_value = (pong_line.encode(), b"")

        with (
            mock.patch("codeagent.config.repo_map.load_repo_map") as mock_map,
            mock.patch("codeagent.domain.resolve_is_local", return_value=False),
            mock.patch("codeagent.transport.control_master.ControlMaster", return_value=mock_cm),
            mock.patch.object(cli_mod.subprocess, "Popen", return_value=mock_proc),
            mock.patch("codeagent.gateway.remote.remote_gateway_call", side_effect=RuntimeError("rpc fail")),
        ):
            mock_map.return_value = SimpleNamespace(hosts={})
            assert cli_mod.cmd_gateway_ensure(_ns(host="remotebox")) == 1
        assert "rpc fail" in capsys.readouterr().err

    def test_file_not_found_falls_back_to_ad_hoc(self, capsys):
        """load_repo_map raising FileNotFoundError still builds an ad-hoc HostSpec."""
        mock_cm = mock.Mock()
        mock_cm.is_alive.return_value = True
        mock_cm.ssh_cmd.return_value = ["ssh", "remote"]

        pong_line = json.dumps({"type": "pong", "wire_version": 3})
        mock_proc = mock.Mock()
        mock_proc.communicate.side_effect = [
            (pong_line.encode(), b""),   # wire probe
            ("gateway: started\n", ""),  # remote gateway start
        ]

        with (
            mock.patch("codeagent.config.repo_map.load_repo_map", side_effect=FileNotFoundError),
            mock.patch("codeagent.domain.resolve_is_local", return_value=False),
            mock.patch("codeagent.transport.control_master.ControlMaster", return_value=mock_cm),
            mock.patch.object(cli_mod.subprocess, "Popen", return_value=mock_proc),
            mock.patch("codeagent.gateway.remote.remote_gateway_call", return_value={"ok": True}),
        ):
            assert cli_mod.cmd_gateway_ensure(_ns(host="newhost")) == 0


# ═══════════════════════════════════════════════════════════════════════
#  _socket_owner_is_self / _tmux helpers / _find_gateway_pane
# ═══════════════════════════════════════════════════════════════════════


class TestHelpers:
    def test_socket_owner_is_self_true(self, tmp_path):
        sock = tmp_path / "s.sock"
        sock.touch()
        assert cli_mod._socket_owner_is_self(sock) is True

    def test_socket_owner_is_self_missing(self, tmp_path):
        assert cli_mod._socket_owner_is_self(tmp_path / "nope.sock") is False

    def test_tmux_session_alive_true(self):
        with mock.patch.object(cli_mod, "_tmux", return_value=(0, "", "")):
            assert cli_mod._tmux_session_alive() is True

    def test_tmux_session_alive_false(self):
        with mock.patch.object(cli_mod, "_tmux", return_value=(1, "", "")):
            assert cli_mod._tmux_session_alive() is False

    def test_find_gateway_pane_found(self):
        panes = "main|%1\ngateway|%3\n"
        with mock.patch.object(cli_mod, "_tmux", return_value=(0, panes, "")):
            assert cli_mod._find_gateway_pane() == "%3"

    def test_find_gateway_pane_not_found(self):
        panes = "main|%1\nother|%3\n"
        with mock.patch.object(cli_mod, "_tmux", return_value=(0, panes, "")):
            assert cli_mod._find_gateway_pane() is None

    def test_find_gateway_pane_list_fails(self):
        with mock.patch.object(cli_mod, "_tmux", return_value=(1, "", "")):
            assert cli_mod._find_gateway_pane() is None


# ═══════════════════════════════════════════════════════════════════════
#  main()
# ═══════════════════════════════════════════════════════════════════════


class TestMain:
    def test_no_cmd_prints_help(self, capsys):
        assert cli_mod.main([]) == 1
        assert capsys.readouterr().out  # help printed

    def test_start_dispatch(self):
        with mock.patch.object(cli_mod, "cmd_gateway_start", return_value=7) as fn:
            assert cli_mod.main(["start"]) == 7
            fn.assert_called_once()

    def test_status_dispatch(self):
        with mock.patch.object(cli_mod, "cmd_gateway_status", return_value=3) as fn:
            assert cli_mod.main(["status"]) == 3

    def test_stop_dispatch(self):
        with mock.patch.object(cli_mod, "cmd_gateway_stop", return_value=4) as fn:
            assert cli_mod.main(["stop"]) == 4

    def test_rpc_dispatch(self):
        with mock.patch.object(cli_mod, "cmd_gateway_rpc", return_value=5) as fn:
            assert cli_mod.main(["rpc", "method"]) == 5

    def test_serve_dispatch(self):
        with mock.patch.object(cli_mod, "cmd_gateway_serve", return_value=6) as fn:
            assert cli_mod.main(["serve"]) == 6


# ═══════════════════════════════════════════════════════════════════════
#  Previously-uncovered lines: start error paths, PID lock, tmux helpers,
#  health checks, cursor persistence, exit-on parsing, watch bounds
# ═══════════════════════════════════════════════════════════════════════


class TestGatewayCliUncovered:
    # ── cmd_gateway_start: socket-alive branches, PID lock, wait-loop sleep ──

    def test_start_socket_alive_then_already_running(self, tmp_path, capsys):
        """Socket connect OK and handshake re-check succeeds → already running."""
        sock = tmp_path / "control.sock"
        sock.touch()
        with (
            mock.patch.object(cli_mod, "_gateway_running", side_effect=[False, True]),
            mock.patch.object(cli_mod, "control_socket_path", return_value=sock),
            mock.patch.object(cli_mod, "_socket_owner_is_self", return_value=True),
            mock.patch.object(cli_mod, "_socket_is_alive", return_value=True),
        ):
            assert cli_mod.cmd_gateway_start(_ns()) == 0
        assert sock.exists()  # not removed — a live gateway holds it
        assert "already running" in capsys.readouterr().out

    def test_start_socket_alive_but_handshake_fails(self, tmp_path, capsys):
        """Socket alive but handshake fails → remove socket (unlink OSError
        swallowed), then start a fresh gateway."""
        sock = tmp_path / "sockdir"
        sock.mkdir()  # unlink raises IsADirectoryError (an OSError)
        with (
            mock.patch.object(cli_mod, "_gateway_running", side_effect=[False, False, True]),
            mock.patch.object(cli_mod, "control_socket_path", return_value=sock),
            mock.patch.object(cli_mod, "_socket_owner_is_self", return_value=True),
            mock.patch.object(cli_mod, "_socket_is_alive", return_value=True),
            mock.patch.object(cli_mod, "_cleanup_legacy_tmux_servers"),
            mock.patch.object(cli_mod, "ensure_tmux_server", return_value=True),
            mock.patch.object(cli_mod, "_check_pid_lock", return_value=(True, None)),
            mock.patch.object(cli_mod, "_find_gateway_pane", return_value=None),
            mock.patch.object(cli_mod, "_tmux_new_gateway_pane", return_value=(0, "%1", "")),
            mock.patch.object(cli_mod.time, "monotonic", side_effect=[0.0, 1.0]),
            mock.patch.object(cli_mod.time, "sleep"),
        ):
            assert cli_mod.cmd_gateway_start(_ns()) == 0
        out = capsys.readouterr().out
        assert "socket alive but handshake failed" in out
        assert "started" in out

    def test_start_stale_socket_unlink_oserror(self, tmp_path, capsys):
        """unlink failure while removing a stale socket is swallowed."""
        sock = tmp_path / "sockdir"
        sock.mkdir()
        with (
            mock.patch.object(cli_mod, "_gateway_running", side_effect=[False, True]),
            mock.patch.object(cli_mod, "control_socket_path", return_value=sock),
            mock.patch.object(cli_mod, "_socket_owner_is_self", return_value=True),
            mock.patch.object(cli_mod, "_socket_is_alive", return_value=False),
            mock.patch.object(cli_mod, "_cleanup_legacy_tmux_servers"),
            mock.patch.object(cli_mod, "ensure_tmux_server", return_value=True),
            mock.patch.object(cli_mod, "_check_pid_lock", return_value=(True, None)),
            mock.patch.object(cli_mod, "_find_gateway_pane", return_value=None),
            mock.patch.object(cli_mod, "_tmux_new_gateway_pane", return_value=(0, "%1", "")),
            mock.patch.object(cli_mod.time, "monotonic", side_effect=[0.0, 1.0]),
            mock.patch.object(cli_mod.time, "sleep"),
        ):
            assert cli_mod.cmd_gateway_start(_ns()) == 0
        assert "removing stale socket" in capsys.readouterr().out

    def test_start_pid_lock_held(self, capsys):
        """A live PID lock aborts the start."""
        with (
            mock.patch.object(cli_mod, "_gateway_running", return_value=False),
            mock.patch.object(cli_mod, "control_socket_path", return_value=Path("/no/sock")),
            mock.patch.object(cli_mod, "_cleanup_legacy_tmux_servers"),
            mock.patch.object(cli_mod, "ensure_tmux_server", return_value=True),
            mock.patch.object(cli_mod, "_check_pid_lock", return_value=(False, 4242)),
        ):
            assert cli_mod.cmd_gateway_start(_ns()) == 1
        assert "another gateway running (pid=4242)" in capsys.readouterr().err

    def test_start_wait_loop_sleeps_between_polls(self, capsys):
        """The UDS wait loop actually sleeps while the gateway is slow."""
        with (
            mock.patch.object(cli_mod, "_gateway_running", side_effect=[False, False, True]),
            mock.patch.object(cli_mod, "control_socket_path", return_value=Path("/no/sock")),
            mock.patch.object(cli_mod, "_cleanup_legacy_tmux_servers"),
            mock.patch.object(cli_mod, "ensure_tmux_server", return_value=True),
            mock.patch.object(cli_mod, "_check_pid_lock", return_value=(True, None)),
            mock.patch.object(cli_mod, "_find_gateway_pane", return_value=None),
            mock.patch.object(cli_mod, "_tmux_new_gateway_pane", return_value=(0, "%1", "")),
            mock.patch.object(cli_mod.time, "monotonic", side_effect=[0.0, 1.0, 2.0]),
            mock.patch.object(cli_mod.time, "sleep") as mock_sleep,
        ):
            assert cli_mod.cmd_gateway_start(_ns()) == 0
        mock_sleep.assert_called_with(0.3)
        assert "started" in capsys.readouterr().out

    # ── _cleanup_legacy_tmux_servers ──

    def test_cleanup_legacy_tmux_servers_removes_brand_dirs(self, tmp_path, caplog):
        brand_dir = tmp_path / "postmesh-tmux"
        sock = brand_dir / "codeagent.sock"
        sock.parent.mkdir(parents=True)
        sock.touch()
        with (
            mock.patch.dict(os.environ, {"TMPDIR": str(tmp_path)}),
            mock.patch.object(cli_mod.subprocess, "run", return_value=mock.Mock(returncode=0)) as mock_run,
            caplog.at_level(logging.INFO, logger="codeagent.gateway.cli"),
        ):
            cli_mod._cleanup_legacy_tmux_servers()
        mock_run.assert_called_once_with(
            ["tmux", "-S", str(sock), "kill-server"], capture_output=True, timeout=5,
        )
        assert not brand_dir.exists()
        assert any("cleaned up legacy tmux dir" in r.message for r in caplog.records)

    def test_cleanup_legacy_tmux_servers_kill_failure_logged(self, tmp_path, caplog):
        brand_dir = tmp_path / "codeagent-tmux"
        sock = brand_dir / "codeagent.sock"
        sock.parent.mkdir(parents=True)
        sock.touch()
        with (
            mock.patch.dict(os.environ, {"TMPDIR": str(tmp_path)}),
            mock.patch.object(cli_mod.subprocess, "run", side_effect=subprocess.TimeoutExpired("tmux", 5)),
            caplog.at_level(logging.DEBUG, logger="codeagent.gateway.cli"),
        ):
            cli_mod._cleanup_legacy_tmux_servers()
        assert not brand_dir.exists()  # rmtree still ran
        assert any("legacy tmux kill-server failed" in r.message for r in caplog.records)

    def test_cleanup_legacy_tmux_servers_rmtree_oserror(self, tmp_path):
        brand_dir = tmp_path / "postmesh-tmux"
        sock = brand_dir / "codeagent.sock"
        sock.parent.mkdir(parents=True)
        sock.touch()
        with (
            mock.patch.dict(os.environ, {"TMPDIR": str(tmp_path)}),
            mock.patch.object(cli_mod.subprocess, "run", return_value=mock.Mock(returncode=0)),
            mock.patch("shutil.rmtree", side_effect=OSError("busy")),
        ):
            cli_mod._cleanup_legacy_tmux_servers()  # must not raise
        assert brand_dir.exists()

    # ── _check_pid_lock / _write_pid_file / _remove_pid_file ──

    def test_pid_lock_absent(self, tmp_path):
        assert cli_mod._check_pid_lock(tmp_path / "gateway.pid") == (True, None)

    def test_pid_lock_live_process(self, tmp_path):
        pid_path = tmp_path / "gateway.pid"
        pid_path.write_text(str(os.getpid()))
        assert cli_mod._check_pid_lock(pid_path) == (False, os.getpid())

    def test_pid_lock_stale_process_removed(self, tmp_path):
        pid_path = tmp_path / "gateway.pid"
        pid_path.write_text("999999999")
        assert cli_mod._check_pid_lock(pid_path) == (True, None)
        assert not pid_path.exists()

    def test_pid_lock_stale_process_unlink_fails(self, tmp_path):
        pid_path = tmp_path / "gateway.pid"
        pid_path.write_text("999999999")
        with mock.patch.object(Path, "unlink", side_effect=OSError("busy")):
            assert cli_mod._check_pid_lock(pid_path) == (True, None)
        assert pid_path.exists()

    def test_pid_lock_corrupt_removed(self, tmp_path):
        pid_path = tmp_path / "gateway.pid"
        pid_path.write_text("not-a-pid")
        assert cli_mod._check_pid_lock(pid_path) == (True, None)
        assert not pid_path.exists()

    def test_pid_lock_corrupt_unlink_fails(self, tmp_path):
        pid_path = tmp_path / "gateway.pid"
        pid_path.write_text("not-a-pid")
        with mock.patch.object(Path, "unlink", side_effect=OSError("busy")):
            assert cli_mod._check_pid_lock(pid_path) == (True, None)
        assert pid_path.exists()

    def test_pid_lock_read_oserror(self, tmp_path):
        pid_path = tmp_path / "gateway.pid"
        pid_path.mkdir()  # read_text raises IsADirectoryError, unlink too
        assert cli_mod._check_pid_lock(pid_path) == (True, None)

    def test_write_pid_file(self, tmp_path):
        pid_path = tmp_path / "gw" / "gateway.pid"
        cli_mod._write_pid_file(pid_path)
        assert pid_path.read_text() == str(os.getpid())

    def test_write_pid_file_oserror(self, tmp_path, caplog):
        with (
            mock.patch.object(Path, "write_text", side_effect=OSError("full")),
            caplog.at_level(logging.WARNING, logger="codeagent.gateway.cli"),
        ):
            cli_mod._write_pid_file(tmp_path / "gateway.pid")
        assert any("failed to write PID file" in r.message for r in caplog.records)

    def test_remove_pid_file(self, tmp_path):
        pid_path = tmp_path / "gateway.pid"
        pid_path.write_text("1")
        cli_mod._remove_pid_file(pid_path)
        assert not pid_path.exists()

    def test_remove_pid_file_oserror(self, tmp_path):
        pid_path = tmp_path / "gateway.pid"
        pid_path.write_text("1")
        with mock.patch.object(Path, "unlink", side_effect=OSError("busy")):
            cli_mod._remove_pid_file(pid_path)  # must not raise
        assert pid_path.exists()

    # ── _tmux / _tmux_new_gateway_pane ──

    def test_tmux_success(self):
        r = mock.Mock(returncode=0, stdout="out", stderr="err")
        with mock.patch.object(cli_mod.subprocess, "run", return_value=r) as mock_run:
            assert cli_mod._tmux("has-session", "-t", "x") == (0, "out", "err")
        mock_run.assert_called_once_with(
            cli_mod.tmux_cmd("has-session", "-t", "x"),
            capture_output=True, text=True, timeout=10,
        )

    def test_tmux_timeout(self):
        exc = subprocess.TimeoutExpired("tmux", 10)
        with mock.patch.object(cli_mod.subprocess, "run", side_effect=exc):
            assert cli_mod._tmux("x") == (1, "", str(exc))

    def test_tmux_oserror(self):
        with mock.patch.object(cli_mod.subprocess, "run", side_effect=OSError("no tmux")):
            assert cli_mod._tmux("x") == (1, "", "no tmux")

    def test_tmux_new_gateway_pane_success(self):
        with mock.patch.object(cli_mod, "_tmux", side_effect=[(0, "%3\n", ""), (0, "", "")]) as mock_tmux:
            assert cli_mod._tmux_new_gateway_pane() == (0, "", "")
        assert mock_tmux.call_count == 2
        args = mock_tmux.call_args.args
        assert args[0] == "send-keys" and args[1] == "-t" and args[2] == "%3"
        assert "codeagent.gateway.cli" in args[3]
        assert args[4] == "Enter"

    def test_tmux_new_gateway_pane_window_fails(self):
        with mock.patch.object(cli_mod, "_tmux", return_value=(1, "", "no server")):
            assert cli_mod._tmux_new_gateway_pane() == (1, "", "no server")

    def test_tmux_new_gateway_pane_no_pane_id(self):
        with mock.patch.object(cli_mod, "_tmux", return_value=(0, "", "")):
            assert cli_mod._tmux_new_gateway_pane() == (1, "", "tmux returned no pane id")

    # ── cmd_gateway_health ──

    def test_health_healthy_no_pid(self, capsys, tmp_path):
        with (
            mock.patch.object(cli_mod, "_gateway_running", return_value=True),
            mock.patch.object(cli_mod, "_gateway_pid_path", return_value=tmp_path / "gateway.pid"),
            mock.patch.object(cli_mod, "control_socket_path", return_value=tmp_path / "control.sock"),
            mock.patch.object(cli_mod, "GatewayClient") as MockClient,
        ):
            MockClient.return_value.call.return_value = {"version": "1.0.0", "runtimes": ["a", "b"]}
            assert cli_mod.cmd_gateway_health(_ns(watch=False)) == 0
        out = capsys.readouterr().out
        assert "healthy" in out
        assert "pid=none" in out
        assert "version=1.0.0" in out
        assert "runtimes=2" in out

    def test_health_pid_alive(self, capsys, tmp_path):
        pid_path = tmp_path / "gateway.pid"
        pid_path.write_text(str(os.getpid()))
        with (
            mock.patch.object(cli_mod, "_gateway_running", return_value=True),
            mock.patch.object(cli_mod, "_gateway_pid_path", return_value=pid_path),
            mock.patch.object(cli_mod, "control_socket_path", return_value=tmp_path / "control.sock"),
            mock.patch.object(cli_mod, "GatewayClient") as MockClient,
        ):
            MockClient.return_value.call.return_value = {"version": "v", "runtimes": []}
            assert cli_mod.cmd_gateway_health(_ns()) == 0
        assert f"pid={os.getpid()} alive" in capsys.readouterr().out

    def test_health_pid_stale(self, capsys, tmp_path):
        pid_path = tmp_path / "gateway.pid"
        pid_path.write_text("999999999")
        with (
            mock.patch.object(cli_mod, "_gateway_running", return_value=True),
            mock.patch.object(cli_mod, "_gateway_pid_path", return_value=pid_path),
            mock.patch.object(cli_mod, "control_socket_path", return_value=tmp_path / "control.sock"),
            mock.patch.object(cli_mod, "GatewayClient") as MockClient,
        ):
            MockClient.return_value.call.return_value = {"version": "v", "runtimes": []}
            assert cli_mod.cmd_gateway_health(_ns()) == 0
        assert "pid=999999999 stale" in capsys.readouterr().out

    def test_health_pid_corrupt(self, capsys, tmp_path):
        pid_path = tmp_path / "gateway.pid"
        pid_path.write_text("garbage")
        with (
            mock.patch.object(cli_mod, "_gateway_running", return_value=True),
            mock.patch.object(cli_mod, "_gateway_pid_path", return_value=pid_path),
            mock.patch.object(cli_mod, "control_socket_path", return_value=tmp_path / "control.sock"),
            mock.patch.object(cli_mod, "GatewayClient") as MockClient,
        ):
            MockClient.return_value.call.return_value = {"version": "v", "runtimes": []}
            assert cli_mod.cmd_gateway_health(_ns()) == 0
        assert "pid=unknown(corrupt)" in capsys.readouterr().out

    def test_health_degraded(self, capsys, tmp_path):
        with (
            mock.patch.object(cli_mod, "_gateway_running", return_value=True),
            mock.patch.object(cli_mod, "_gateway_pid_path", return_value=tmp_path / "gateway.pid"),
            mock.patch.object(cli_mod, "control_socket_path", return_value=tmp_path / "control.sock"),
            mock.patch.object(cli_mod, "GatewayClient") as MockClient,
        ):
            MockClient.return_value.call.side_effect = cli_mod.GatewayError("DOWN", "boom")
            assert cli_mod.cmd_gateway_health(_ns()) == 0
        out = capsys.readouterr().out
        assert "degraded" in out
        assert "boom" in out

    def test_health_unhealthy(self, capsys, tmp_path):
        with (
            mock.patch.object(cli_mod, "_gateway_running", return_value=False),
            mock.patch.object(cli_mod, "_gateway_pid_path", return_value=tmp_path / "gateway.pid"),
            mock.patch.object(cli_mod, "control_socket_path", return_value=tmp_path / "control.sock"),
        ):
            assert cli_mod.cmd_gateway_health(_ns()) == 1
        out = capsys.readouterr().out
        assert "unhealthy" in out
        assert "socket_exists=False" in out

    def test_health_watch_two_iterations(self, capsys, tmp_path):
        with (
            mock.patch.object(cli_mod, "_gateway_running", return_value=True),
            mock.patch.object(cli_mod, "_gateway_pid_path", return_value=tmp_path / "gateway.pid"),
            mock.patch.object(cli_mod, "control_socket_path", return_value=tmp_path / "control.sock"),
            mock.patch.object(cli_mod, "GatewayClient") as MockClient,
            mock.patch.object(cli_mod.time, "sleep", side_effect=[None, KeyboardInterrupt]),
        ):
            MockClient.return_value.call.return_value = {"version": "v", "runtimes": []}
            assert cli_mod.cmd_gateway_health(_ns(watch=True, interval=5)) == 0
        assert capsys.readouterr().out.count("healthy") == 2

    # ── cmd_gateway_ensure: remaining branches ──

    def test_ensure_creates_control_master_when_not_alive(self, capsys):
        mock_cm = mock.Mock()
        mock_cm.is_alive.return_value = False
        mock_cm.ssh_cmd.return_value = ["ssh", "remote"]
        pong_line = json.dumps({"type": "pong", "wire_version": 3})
        mock_proc = mock.Mock()
        mock_proc.communicate.side_effect = [
            (pong_line.encode(), b""),   # wire probe
            ("gateway: started\n", ""),  # remote gateway start
        ]
        with (
            mock.patch("codeagent.config.repo_map.load_repo_map") as mock_map,
            mock.patch("codeagent.domain.resolve_is_local", return_value=False),
            mock.patch("codeagent.transport.control_master.ControlMaster", return_value=mock_cm),
            mock.patch.object(cli_mod.subprocess, "Popen", return_value=mock_proc),
            mock.patch("codeagent.gateway.remote.remote_gateway_call", return_value={"ok": True}),
        ):
            mock_map.return_value = SimpleNamespace(hosts={})
            assert cli_mod.cmd_gateway_ensure(_ns(host="remotebox")) == 0
        assert mock_cm.create.call_count == 2  # wire probe + gateway start
        assert "ensured" in capsys.readouterr().out

    def test_ensure_skips_bad_probe_lines(self, capsys):
        """Lines failing decode_line are skipped instead of aborting."""
        mock_cm = mock.Mock()
        mock_cm.is_alive.return_value = True
        mock_cm.ssh_cmd.return_value = ["ssh", "remote"]
        pong_line = json.dumps({"type": "pong", "wire_version": 3})
        out_b = b"not-json\n" + pong_line.encode() + b"\n"
        mock_proc = mock.Mock()
        mock_proc.communicate.side_effect = [
            (out_b, b""),                # wire probe: one garbage line + pong
            ("gateway: started\n", ""),  # remote gateway start
        ]
        with (
            mock.patch("codeagent.config.repo_map.load_repo_map") as mock_map,
            mock.patch("codeagent.domain.resolve_is_local", return_value=False),
            mock.patch("codeagent.transport.control_master.ControlMaster", return_value=mock_cm),
            mock.patch.object(cli_mod.subprocess, "Popen", return_value=mock_proc),
            mock.patch("codeagent.gateway.remote.remote_gateway_call", return_value={"ok": True}),
        ):
            mock_map.return_value = SimpleNamespace(hosts={})
            assert cli_mod.cmd_gateway_ensure(_ns(host="remotebox")) == 0
        assert "ensured" in capsys.readouterr().out

    def test_ensure_remote_start_fails(self, capsys):
        mock_cm = mock.Mock()
        mock_cm.is_alive.return_value = True
        mock_cm.ssh_cmd.return_value = ["ssh", "remote"]
        pong_line = json.dumps({"type": "pong", "wire_version": 3})
        mock_proc = mock.Mock()
        mock_proc.communicate.return_value = (pong_line.encode(), b"")
        with (
            mock.patch("codeagent.config.repo_map.load_repo_map") as mock_map,
            mock.patch("codeagent.domain.resolve_is_local", return_value=False),
            mock.patch("codeagent.transport.control_master.ControlMaster", return_value=mock_cm),
            mock.patch.object(cli_mod.subprocess, "Popen", side_effect=[mock_proc, OSError("ssh down")]),
        ):
            mock_map.return_value = SimpleNamespace(hosts={})
            assert cli_mod.cmd_gateway_ensure(_ns(host="remotebox")) == 1
        assert "remote gateway start failed" in capsys.readouterr().err

    # ── cmd_gateway_stop ──

    def test_stop_runtime_stop_gateway_error_swallowed(self, capsys, tmp_path):
        """A GatewayError from the shutdown RPC is ignored."""
        with (
            mock.patch.object(cli_mod, "_gateway_running", return_value=True),
            mock.patch.object(cli_mod, "GatewayClient") as MockClient,
            mock.patch.object(cli_mod, "_tmux", return_value=(0, "", "")),
            mock.patch.object(cli_mod, "control_socket_path", return_value=tmp_path / "control.sock"),
            mock.patch.object(cli_mod, "_gateway_pid_path", return_value=tmp_path / "gateway.pid"),
        ):
            MockClient.return_value.call.side_effect = cli_mod.GatewayError("NOPE", "nope")
            assert cli_mod.cmd_gateway_stop(_ns()) == 0
        assert "stopped" in capsys.readouterr().out

    def test_stop_socket_unlink_oserror(self, capsys, tmp_path):
        """unlink failure while removing the control socket is swallowed."""
        sock = tmp_path / "sockdir"
        sock.mkdir()
        with (
            mock.patch.object(cli_mod, "_gateway_running", return_value=True),
            mock.patch.object(cli_mod, "GatewayClient") as MockClient,
            mock.patch.object(cli_mod, "_tmux", return_value=(0, "", "")),
            mock.patch.object(cli_mod, "control_socket_path", return_value=sock),
            mock.patch.object(cli_mod, "_gateway_pid_path", return_value=tmp_path / "gateway.pid"),
        ):
            assert cli_mod.cmd_gateway_stop(_ns()) == 0
        assert "stopped" in capsys.readouterr().out

    # ── _watch_cursor_file / _load_watch_cursor / _save_watch_cursor ──

    def test_watch_cursor_file_none_without_key(self):
        assert cli_mod._watch_cursor_file("", "", None) is None

    def test_watch_cursor_file_session(self, tmp_path):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(tmp_path)}):
            path = cli_mod._watch_cursor_file("s1", "r1", None)
        assert path == tmp_path / "aimeshchat" / "watch-cursor-s1.json"

    def test_watch_cursor_file_runtime_id(self, tmp_path):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(tmp_path)}):
            path = cli_mod._watch_cursor_file("", "r1", None)
        assert path == tmp_path / "aimeshchat" / "watch-cursor-r1.json"

    def test_watch_cursor_file_with_filters(self, tmp_path):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(tmp_path)}):
            path = cli_mod._watch_cursor_file("s1", "r1", ["b", "a"])
        fhash = hashlib.sha256("a,b".encode()).hexdigest()[:12]
        assert path == tmp_path / "aimeshchat" / f"watch-cursor-s1-f{fhash}.json"

    def test_load_watch_cursor_absent(self, tmp_path):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(tmp_path)}):
            assert cli_mod._load_watch_cursor("s1", "") == 0

    def test_load_watch_cursor_valid(self, tmp_path):
        path = tmp_path / "aimeshchat" / "watch-cursor-s1.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"cursor": 9}))
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(tmp_path)}):
            assert cli_mod._load_watch_cursor("s1", "") == 9

    def test_load_watch_cursor_corrupt(self, tmp_path):
        path = tmp_path / "aimeshchat" / "watch-cursor-s1.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(tmp_path)}):
            assert cli_mod._load_watch_cursor("s1", "") == 0

    def test_load_watch_cursor_bad_cursor_type(self, tmp_path):
        path = tmp_path / "aimeshchat" / "watch-cursor-s1.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"cursor": "x"}))
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(tmp_path)}):
            assert cli_mod._load_watch_cursor("s1", "") == 0

    def test_load_watch_cursor_none_cursor(self, tmp_path):
        path = tmp_path / "aimeshchat" / "watch-cursor-s1.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"cursor": None}))
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(tmp_path)}):
            assert cli_mod._load_watch_cursor("s1", "") == 0

    def test_load_watch_cursor_read_oserror(self, tmp_path):
        path = tmp_path / "aimeshchat" / "watch-cursor-s1.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}")
        with (
            mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(tmp_path)}),
            mock.patch.object(Path, "read_text", side_effect=OSError("io")),
        ):
            assert cli_mod._load_watch_cursor("s1", "") == 0

    def test_save_watch_cursor(self, tmp_path):
        fhash = hashlib.sha256("a".encode()).hexdigest()[:12]
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(tmp_path)}):
            cli_mod._save_watch_cursor("s1", "", 5, ["a"])
        data = json.loads(
            (tmp_path / "aimeshchat" / f"watch-cursor-s1-f{fhash}.json").read_text()
        )
        assert data["cursor"] == 5
        assert data["filters"] == ["a"]
        assert data["filter_hash"] == fhash
        assert data["watcher_id"].endswith(f":{os.getpid()}")
        assert data["saved_at"]

    def test_save_watch_cursor_oserror(self, tmp_path, caplog):
        with (
            mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(tmp_path)}),
            mock.patch.object(Path, "write_text", side_effect=OSError("full")),
            caplog.at_level(logging.DEBUG, logger="codeagent.gateway.cli"),
        ):
            cli_mod._save_watch_cursor("s1", "", 5)
        assert any("cursor persist failed" in r.message for r in caplog.records)

    def test_save_watch_cursor_no_key(self, tmp_path):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(tmp_path)}):
            cli_mod._save_watch_cursor("", "", 5)
        assert not (tmp_path / "aimeshchat").exists()

    # ── _parse_exit_on ──

    def test_parse_exit_on_empty(self):
        assert cli_mod._parse_exit_on(None) == []
        assert cli_mod._parse_exit_on("") == []
        assert cli_mod._parse_exit_on(" , , ") == []

    def test_parse_exit_on_pairs(self):
        specs = cli_mod._parse_exit_on(" TASK_STATE.agent_end , RUNTIME_STATE.stopped ")
        assert specs == [("TASK_STATE", "agent_end"), ("RUNTIME_STATE", "stopped")]

    def test_parse_exit_on_kind_only(self):
        assert cli_mod._parse_exit_on("ASSISTANT_PROGRESS") == [("ASSISTANT_PROGRESS", None)]

    def test_parse_exit_on_malformed(self, capsys):
        assert cli_mod._parse_exit_on(".state,kind.,") == []
        err = capsys.readouterr().err
        assert "ignoring malformed" in err
        assert "'.state'" in err and "'kind.'" in err

    # ── cmd_events_watch: explicit cursor / exit-on / bounds ──

    def test_watch_explicit_cursor_wins(self, capsys):
        params_seen = {}

        def side_effect(method, params):
            params_seen.update(params)
            return {"events": [], "cursor": 99}

        with (
            mock.patch.object(cli_mod, "GatewayClient") as MockClient,
            mock.patch.object(cli_mod, "_load_watch_cursor") as mock_load,
            mock.patch.object(cli_mod.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            MockClient.return_value.call.side_effect = side_effect
            assert cli_mod.cmd_events_watch(_ns(jsonl=True, cursor="7", interval=0)) == 0
        assert params_seen["cursor"] == 7
        mock_load.assert_not_called()

    def test_watch_exit_on_match(self, capsys):
        ev = {
            "event_id": 1, "kind": "TASK_STATE", "runtime_id": "r",
            "created_at": "t", "payload": {"state": "agent_end"},
        }
        with (
            mock.patch.object(cli_mod, "GatewayClient") as MockClient,
            mock.patch.object(cli_mod, "_save_watch_cursor") as mock_save,
        ):
            MockClient.return_value.call.return_value = {"events": [ev], "cursor": 42}
            assert cli_mod.cmd_events_watch(_ns(jsonl=True, interval=0, exit_on="TASK_STATE.agent_end")) == 0
        mock_save.assert_called_once_with("", "", 42, None)
        assert json.loads(capsys.readouterr().out)["event_id"] == 1

    def test_watch_exit_on_kind_only(self):
        ev = {"event_id": 2, "kind": "ASSISTANT_PROGRESS", "runtime_id": "r", "created_at": "t"}
        with (
            mock.patch.object(cli_mod, "GatewayClient") as MockClient,
            mock.patch.object(cli_mod, "_save_watch_cursor"),
        ):
            MockClient.return_value.call.return_value = {"events": [ev], "cursor": 42}
            assert cli_mod.cmd_events_watch(_ns(jsonl=True, interval=0, exit_on="ASSISTANT_PROGRESS")) == 0

    def test_watch_exit_on_save_failure_swallowed(self):
        """A cursor-save TypeError inside the exit-on branch is swallowed."""
        ev = {
            "event_id": 1, "kind": "TASK_STATE", "runtime_id": "r",
            "created_at": "t", "payload": {"state": "agent_end"},
        }
        with (
            mock.patch.object(cli_mod, "GatewayClient") as MockClient,
            mock.patch.object(cli_mod, "_save_watch_cursor", side_effect=TypeError("bad cursor")),
        ):
            MockClient.return_value.call.return_value = {"events": [ev], "cursor": 42}
            assert cli_mod.cmd_events_watch(_ns(jsonl=True, interval=0, exit_on="TASK_STATE.agent_end")) == 0

    def test_watch_persist_failure_swallowed(self, capsys):
        """A TypeError from cursor persistence does not kill the watcher."""
        with (
            mock.patch.object(cli_mod, "GatewayClient") as MockClient,
            mock.patch.object(cli_mod, "_save_watch_cursor", side_effect=TypeError("bad")),
            mock.patch.object(cli_mod.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            MockClient.return_value.call.return_value = {
                "events": [{"event_id": 1, "kind": "K", "runtime_id": "r", "created_at": "t"}],
                "cursor": 5,
            }
            assert cli_mod.cmd_events_watch(_ns(jsonl=True, interval=0)) == 0

    def test_watch_max_events_bound(self):
        call_count = 0

        def side_effect(method, params):
            nonlocal call_count
            call_count += 1
            return {"events": [{"event_id": 1, "kind": "K", "runtime_id": "r", "created_at": "t"}], "cursor": 1}

        with (
            mock.patch.object(cli_mod, "GatewayClient") as MockClient,
            mock.patch.object(cli_mod, "_save_watch_cursor"),
        ):
            MockClient.return_value.call.side_effect = side_effect
            assert cli_mod.cmd_events_watch(_ns(jsonl=True, interval=0, max_events=1)) == 0
        assert call_count == 1

    def test_watch_duration_bound(self):
        with (
            mock.patch.object(cli_mod, "GatewayClient") as MockClient,
            mock.patch.object(cli_mod, "_save_watch_cursor"),
            mock.patch.object(cli_mod.time, "monotonic", side_effect=[0.0, 100.0]),
        ):
            MockClient.return_value.call.return_value = {"events": [], "cursor": 1}
            assert cli_mod.cmd_events_watch(_ns(jsonl=True, interval=0, duration=0.01)) == 0

    # ── module __main__ entry ──

    def test_main_module_entry(self, capsys):
        """Running the module as __main__ with no args prints help, exits 1."""
        import runpy
        import warnings

        with (
            warnings.catch_warnings(),
            mock.patch.object(sys, "argv", ["aimeshchat"]),
            pytest.raises(SystemExit) as exc,
        ):
            # runpy re-executes an already-imported module; ignore that warning.
            warnings.simplefilter("ignore", RuntimeWarning)
            runpy.run_module("codeagent.gateway.cli", run_name="__main__")
        assert exc.value.code == 1
        assert "usage" in capsys.readouterr().out.lower()
