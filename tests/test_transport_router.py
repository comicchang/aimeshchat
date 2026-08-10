"""Tests for codeagent.transport.router — TransportRouter."""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from codeagent.domain import HostSpec
from codeagent.transport.router import TransportRouter


# ── Helpers ──────────────────────────────────────────────────────────────


def _host(**kwargs) -> HostSpec:
    """Build a HostSpec with sane defaults."""
    defaults = dict(name="h", ssh_alias="h", hostnames=("h",))
    defaults.update(kwargs)
    return HostSpec(**defaults)


# ── TransportRouter.get ─────────────────────────────────────────────────


class TestRouterGet:
    """TransportRouter.get() selects the right transport."""

    def test_default_returns_ssh(self):
        """Default transport='ssh' → SSHTransport."""
        from codeagent.transport.ssh import SSHTransport

        router = TransportRouter()
        host = _host()
        t = router.get(host)
        assert isinstance(t, SSHTransport)

    def test_relay_login_returns_relay(self, tmp_path):
        """transport='relay-login' → RelayTransport."""
        from codeagent.transport.relay import RelayTransport

        zsh = tmp_path / "relay.zsh"
        zsh.write_text("# relay script\n")
        rm = mock.MagicMock(relay_zsh=str(zsh))

        router = TransportRouter()
        host = _host(transport="relay-login")
        t = router.get(host, rm)
        assert isinstance(t, RelayTransport)

    def test_relay_login_no_zsh_raises(self):
        """relay-login without relay_zsh raises ValueError."""
        router = TransportRouter()
        host = _host(transport="relay-login")
        with pytest.raises(ValueError, match="relay_zsh"):
            router.get(host, None)

    def test_relay_login_empty_zsh_raises(self, tmp_path):
        """relay-login with empty relay_zsh raises ValueError."""
        rm = mock.MagicMock(relay_zsh="")
        router = TransportRouter()
        host = _host(transport="relay-login")
        with pytest.raises(ValueError, match="relay_zsh"):
            router.get(host, rm)

    def test_relay_login_zsh_not_found_raises(self, tmp_path):
        """relay-login with non-existent zsh file raises TransportError."""
        rm = mock.MagicMock(relay_zsh=str(tmp_path / "nonexistent.zsh"))
        router = TransportRouter()
        host = _host(transport="relay-login")
        with pytest.raises(Exception):  # TransportError from RelayTransport.__init__
            router.get(host, rm)

    def test_explicit_ssh_transport(self):
        """transport='ssh' explicitly → SSHTransport."""
        from codeagent.transport.ssh import SSHTransport

        router = TransportRouter()
        host = _host(transport="ssh")
        t = router.get(host)
        assert isinstance(t, SSHTransport)

    def test_no_repo_map_defaults_ssh(self):
        """repo_map=None with default transport → SSHTransport."""
        from codeagent.transport.ssh import SSHTransport

        router = TransportRouter()
        host = _host()
        t = router.get(host, repo_map=None)
        assert isinstance(t, SSHTransport)


# ── TransportRouter.capabilities ────────────────────────────────────────


class TestRouterCapabilities:
    """TransportRouter.capabilities() returns correct capability sets."""

    def test_ssh_capabilities(self):
        """SSH transport has full capabilities."""
        router = TransportRouter()
        host = _host(transport="ssh")
        caps = router.capabilities(host)
        assert caps == {"mailbox", "stream", "artifact"}

    def test_relay_capabilities(self):
        """Relay transport has mailbox only."""
        router = TransportRouter()
        host = _host(transport="relay-login")
        caps = router.capabilities(host)
        assert caps == {"mailbox"}

    def test_default_transport_capabilities(self):
        """Default transport (ssh) has full capabilities."""
        router = TransportRouter()
        host = _host()  # transport defaults to "ssh"
        caps = router.capabilities(host)
        assert caps == {"mailbox", "stream", "artifact"}

    def test_unknown_transport_has_full_capabilities(self):
        """Unknown transport values default to full capabilities."""
        router = TransportRouter()
        host = _host(transport="unknown-transport")
        caps = router.capabilities(host)
        assert caps == {"mailbox", "stream", "artifact"}


# ── TransportRouter.supports_mailbox / supports_stream ──────────────────


class TestRouterSupportsHelpers:
    """supports_mailbox() and supports_stream() helpers."""

    def test_ssh_supports_all(self):
        router = TransportRouter()
        host = _host(transport="ssh")
        assert router.supports_mailbox(host) is True
        assert router.supports_stream(host) is True

    def test_relay_supports_mailbox_only(self):
        router = TransportRouter()
        host = _host(transport="relay-login")
        assert router.supports_mailbox(host) is True
        assert router.supports_stream(host) is False


# ── Transport.mailbox (base class default) ──────────────────────────────


class TestTransportMailboxBase:
    """Transport.mailbox() base class raises NotImplementedError."""

    def test_local_transport_mailbox_executes_locally(self, tmp_path):
        """B1: LocalTransport.mailbox 本地执行 mailbox CLI（不再 raise）。
        通过 mailbox-root 指向 tmp，session-init 实际建目录。"""
        from codeagent.transport.local import LocalTransport

        transport = LocalTransport()
        host = _host(name="__local__", ssh_alias="__local__", hostnames=())
        code, out, err = transport.mailbox(
            host,
            ["session-init", "--session", "s1", "--manager", "mgr", "--agents", "w1"],
            mailbox_root=str(tmp_path),
        )
        assert code == 0
        assert "created" in out or "ok" in out, f"out={out!r} err={err!r}"
        assert (tmp_path / "s1" / "session.json").exists()


# ── SSHTransport.mailbox ────────────────────────────────────────────────


class TestSSHTransportMailbox:
    """SSHTransport.mailbox() dispatches through ControlMaster."""

    def test_mailbox_creates_controlmaster_and_calls_run(self):
        """mailbox() creates ControlMaster if needed, builds request, calls _run_ssh_mailbox."""
        from codeagent.transport.ssh import SSHTransport

        transport = SSHTransport()
        host = _host()

        with (
            mock.patch("codeagent.transport.ssh.ControlMaster") as cm_cls,
            mock.patch("codeagent.transport.ssh._run_ssh_mailbox",
                       return_value=(0, "out", "err")) as run_mbox,
            mock.patch("codeagent.transport.ssh.make_mailbox_request",
                       return_value={"type": "mailbox_request"}) as make_req,
        ):
            cm_cls.return_value.is_alive.return_value = False
            exit_code, stdout, stderr = transport.mailbox(
                host, ["stats", "--session", "s1"], mailbox_root="/tmp/mbox",
            )

        assert exit_code == 0
        assert stdout == "out"
        assert stderr == "err"
        make_req.assert_called_once_with(
            args=["stats", "--session", "s1"], mailbox_root="/tmp/mbox",
        )
        cm = cm_cls.return_value
        cm.is_alive.assert_called_once()
        cm.create.assert_called_once()
        run_mbox.assert_called_once()

    def test_mailbox_reuses_alive_controlmaster(self):
        """mailbox() skips create() when ControlMaster is already alive."""
        from codeagent.transport.ssh import SSHTransport

        transport = SSHTransport()
        host = _host()

        with (
            mock.patch("codeagent.transport.ssh.ControlMaster") as cm_cls,
            mock.patch("codeagent.transport.ssh._run_ssh_mailbox",
                       return_value=(0, "", "")),
        ):
            cm_cls.return_value.is_alive.return_value = True
            transport.mailbox(host, ["peek", "--session", "s", "--agent", "a"])

        cm = cm_cls.return_value
        cm.is_alive.assert_called_once()
        cm.create.assert_not_called()

    def test_mailbox_uses_cached_controlmaster(self):
        """mailbox() reuses cached ControlMaster on second call."""
        from codeagent.transport.ssh import SSHTransport

        transport = SSHTransport()
        host = _host()

        with (
            mock.patch("codeagent.transport.ssh.ControlMaster") as cm_cls,
            mock.patch("codeagent.transport.ssh._run_ssh_mailbox",
                       return_value=(0, "", "")),
        ):
            cm_cls.return_value.is_alive.return_value = True
            transport.mailbox(host, ["stats", "--session", "s1", "--agent", "a1"])
            transport.mailbox(host, ["stats", "--session", "s1", "--agent", "a1"])

        # ControlMaster constructor called once, check twice
        assert cm_cls.call_count == 1
        assert cm_cls.return_value.is_alive.call_count == 2


# ── RelayTransport.mailbox ──────────────────────────────────────────────


class TestRelayTransportMailbox:
    """RelayTransport.mailbox() base64-encodes request for relay-login."""

    def test_mailbox_builds_base64_command(self, tmp_path):
        """mailbox() builds base64-encoded wire request for relay."""
        from codeagent.transport.relay import RelayTransport

        zsh = tmp_path / "relay.zsh"
        zsh.write_text("# relay\n")
        transport = RelayTransport(str(zsh))
        host = _host(transport="relay-login")

        with mock.patch.object(transport, "_run_with_pty") as run_pty:
            run_pty.return_value = mock.MagicMock(
                returncode=0, stdout="result-out", stderr="",
            )
            exit_code, stdout, stderr = transport.mailbox(
                host, ["stats", "--session", "s1", "--agent", "a1"],
                mailbox_root="/tmp/mbox",
            )

        assert exit_code == 0
        assert stdout == "result-out"
        assert stderr == ""
        # Verify the command was built with base64 encoding
        run_pty.assert_called_once()
        argv = run_pty.call_args.args[0]
        assert argv[0] == "zsh"
        assert "base64 -d" in argv[2]
        assert "postmesh-remote-exec" in argv[2]

    def test_mailbox_relay_with_shell_prefix(self, tmp_path):
        """mailbox() includes shell_prefix in remote command."""
        from codeagent.transport.relay import RelayTransport

        zsh = tmp_path / "relay.zsh"
        zsh.write_text("# relay\n")
        transport = RelayTransport(str(zsh))
        host = _host(transport="relay-login", shell_prefix="source /opt/env.sh")

        with mock.patch.object(transport, "_run_with_pty") as run_pty:
            run_pty.return_value = mock.MagicMock(
                returncode=0, stdout="", stderr="",
            )
            transport.mailbox(host, ["peek", "--session", "s", "--agent", "a"])

        argv = run_pty.call_args.args[0]
        remote_cmd = argv[2]
        assert "source /opt/env.sh" in remote_cmd

    def test_mailbox_relay_error_propagates(self, tmp_path):
        """mailbox() propagates non-zero exit from _run_with_pty."""
        from codeagent.transport.relay import RelayTransport

        zsh = tmp_path / "relay.zsh"
        zsh.write_text("# relay\n")
        transport = RelayTransport(str(zsh))
        host = _host(transport="relay-login")

        with mock.patch.object(transport, "_run_with_pty") as run_pty:
            run_pty.return_value = mock.MagicMock(
                returncode=1, stdout="", stderr="relay error",
            )
            exit_code, stdout, stderr = transport.mailbox(
                host, ["send", "--session", "s", "--from", "w1", "--to", "mgr",
                        "--subject", "hi", "--body", "hello"],
            )

        assert exit_code == 1
        assert "relay error" in stderr


# ── CLI delegates to router ─────────────────────────────────────────────


class TestCLIDelegatesToRouter:
    """CLI _get_transport delegates to TransportRouter.get()."""

    def test_get_transport_delegates(self):
        """_get_transport calls _router.get with host and repo_map."""
        from codeagent.cli import _get_transport

        host = _host()
        rm = mock.MagicMock()
        with mock.patch("codeagent.cli._router") as router:
            sentinel = mock.MagicMock()
            router.get.return_value = sentinel
            result = _get_transport(host, rm)

        assert result is sentinel
        router.get.assert_called_once_with(host, rm)

    def test_get_transport_default_repo_map(self):
        """_get_transport defaults repo_map to None."""
        from codeagent.cli import _get_transport

        host = _host()
        with mock.patch("codeagent.cli._router") as router:
            router.get.return_value = mock.MagicMock()
            _get_transport(host)

        router.get.assert_called_once_with(host, None)
