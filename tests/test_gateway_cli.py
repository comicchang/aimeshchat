"""Unit tests for codeagent.gateway.cli — all command functions, ≥50% line coverage.

Every external dependency is mocked at the module boundary so no real tmux,
UDS, subprocess, or network calls are made.
"""
from __future__ import annotations

import argparse
import json
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
