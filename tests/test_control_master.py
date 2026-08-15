"""Tests for SSH ControlMaster alias pre-validation and transient retry.

Covers:
- ``classify_ssh_error`` — network vs auth vs config vs unknown.
- ``Host`` pattern matching / config scanning (incl. ``Include``), and the
  rule that a bare ``Host *`` defaults block does not define an alias.
- ``resolve_alias`` — ``ssh -G`` pre-validation: configured alias, literal
  IP, unconfigured bare name (``SSHAliasError``), ssh failures.
- ``ControlMaster.create`` — transient network errors retried with backoff;
  auth/config errors fail fast; unconfigured alias fails before any master
  connection attempt.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import codeagent.transport.control_master as cm_mod
from codeagent.transport.control_master import (
    ControlMaster,
    SSHAliasError,
    TransportError,
    _alias_configured,
    _host_hash,
    _host_line_defines,
    _host_line_matches,
    _looks_like_ip,
    _scan_config_host_matches,
    classify_ssh_error,
    resolve_alias,
)


class _Proc:
    """Fake subprocess.CompletedProcess."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ── classify_ssh_error ─────────────────────────────────────────────────


class TestClassifySshError:
    def test_auth(self):
        assert classify_ssh_error(
            "mianyin@10.0.0.1: Permission denied (publickey,password).", 255
        ) == "auth"

    def test_auth_too_many(self):
        assert classify_ssh_error(
            "Too many authentication failures for mianyin.", 255
        ) == "auth"

    def test_config_bad_option(self):
        assert classify_ssh_error("Bad configuration option: FooBar", 255) == "config"

    def test_config_cannot_open(self):
        assert classify_ssh_error(
            "/Users/x/.ssh/config line 3: unable to open file", 255
        ) == "config"

    def test_network_resolution(self):
        assert classify_ssh_error(
            "ssh: Could not resolve hostname yellow: Temporary failure in name resolution",
            255,
        ) == "network"

    def test_network_macos_wording(self):
        assert classify_ssh_error(
            "ssh: Could not resolve hostname yellow: nodename nor servname provided, "
            "or not known",
            255,
        ) == "network"

    def test_network_connection_refused(self):
        assert classify_ssh_error(
            "ssh: connect to host 10.0.0.1 port 22: Connection refused", 255
        ) == "network"

    def test_network_wins_over_noise_but_loses_to_auth(self):
        # Auth pattern takes precedence even when transient-looking text follows.
        assert classify_ssh_error(
            "Permission denied (publickey).\nConnection closed by remote host.", 255
        ) == "auth"

    def test_unknown(self):
        assert classify_ssh_error("something completely different", 255) == "unknown"

    def test_empty(self):
        assert classify_ssh_error("", 0) == "unknown"


# ── Host pattern matching / config scan ────────────────────────────────


class TestHostPatternMatching:
    def test_simple_match(self):
        assert _host_line_matches(["yellow"], "yellow")
        assert not _host_line_matches(["yellow"], "other")

    def test_case_insensitive(self):
        assert _host_line_matches(["Yellow"], "yellow")

    def test_glob(self):
        assert _host_line_matches(["*.internal"], "web.internal")
        assert not _host_line_matches(["*.internal"], "web.example.com")

    def test_negation_excludes(self):
        # Verified against real ssh -G: `!foo` excludes hosts whose body
        # matches; later patterns still apply to other hosts.
        assert not _host_line_matches(["!foo", "*"], "foo")
        assert _host_line_matches(["!foo", "*"], "bar")

    def test_first_match_wins(self):
        assert _host_line_matches(["*.internal", "!secret.internal"], "x.internal")
        # `!secret.internal` excludes secret.internal; `!other.internal`
        # does not (body does not match), so `*.internal` applies.
        assert not _host_line_matches(["!secret.internal", "*.internal"], "secret.internal")
        assert _host_line_matches(["!secret.internal", "*.internal"], "other.internal")

    def test_star_defaults_not_a_definition(self):
        # `Host *` sets defaults; it must not count as defining an alias.
        assert not _host_line_defines(["*"], "yellow")
        assert _host_line_defines(["yellow"], "yellow")
        assert _host_line_defines(["*", "yellow"], "yellow")

    def test_negated_defaults_block(self):
        assert not _host_line_defines(["!foo", "*"], "bar")

    def test_scan_config_host_matches(self, tmp_path: Path):
        cfg = tmp_path / "config"
        cfg.write_text(
            "Host yellow\n"
            "    HostName 10.227.192.69\n"
            "Host *.internal\n"
            "    User mianyin\n"
        )
        assert _scan_config_host_matches(cfg, "yellow")
        assert _scan_config_host_matches(cfg, "web.internal")
        assert not _scan_config_host_matches(cfg, "other")

    def test_scan_include_recursion(self, tmp_path: Path):
        included = tmp_path / "extra.conf"
        included.write_text("Host box\n    HostName 10.0.0.9\n")
        main = tmp_path / "config"
        main.write_text(f"Include {included.name}\nHost *\n    ControlMaster auto\n")
        assert _scan_config_host_matches(main, "box")
        # `Host *` defaults block alone does not define a bare name.
        assert not _scan_config_host_matches(main, "bare-name")

    def test_scan_include_glob(self, tmp_path: Path):
        (tmp_path / "conf.d").mkdir()
        (tmp_path / "conf.d" / "a.conf").write_text("Host g1\n")
        (tmp_path / "conf.d" / "b.conf").write_text("Host g2\n")
        main = tmp_path / "config"
        main.write_text("Include conf.d/*.conf\n")
        assert _scan_config_host_matches(main, "g1")
        assert _scan_config_host_matches(main, "g2")

    def test_alias_configured_with_config_path(self, tmp_path: Path):
        cfg = tmp_path / "config"
        cfg.write_text("Host yellow\n    HostName 10.0.0.1\n")
        assert _alias_configured("yellow", config_path=str(cfg))
        assert not _alias_configured("other", config_path=str(cfg))


# ── resolve_alias (ssh -G pre-validation) ──────────────────────────────


class TestResolveAlias:
    def test_configured_hostname_override(self):
        with patch("codeagent.transport.control_master.shutil.which", return_value="/usr/bin/ssh"), patch(
            "codeagent.transport.control_master.subprocess.run",
            return_value=_Proc(0, "user mianyin\nhostname 10.227.192.69\nport 22\n"),
        ):
            assert resolve_alias("yellow") == "10.227.192.69"

    def test_host_block_without_hostname_override(self):
        # Host entry exists but no HostName: ssh connects to the alias itself.
        with patch("codeagent.transport.control_master.shutil.which", return_value="/usr/bin/ssh"), patch(
            "codeagent.transport.control_master.subprocess.run",
            return_value=_Proc(0, "hostname myserver\nuser mianyin\n"),
        ), patch("codeagent.transport.control_master._alias_configured", return_value=True):
            assert resolve_alias("myserver") == "myserver"

    def test_unconfigured_bare_name_raises(self):
        # Not in config, not an IP, and DNS cannot resolve it → misconfigured alias.
        with patch("codeagent.transport.control_master.shutil.which", return_value="/usr/bin/ssh"), patch(
            "codeagent.transport.control_master.subprocess.run",
            return_value=_Proc(0, "hostname definitely-not-an-alias\n"),
        ), patch("codeagent.transport.control_master._alias_configured", return_value=False), patch(
            "codeagent.transport.control_master._dns_resolve_error",
            return_value="nodename nor servname provided, or not known",
        ):
            with pytest.raises(SSHAliasError, match="未配置 SSH alias"):
                resolve_alias("definitely-not-an-alias")

    def test_bare_name_resolving_via_dns_is_allowed(self):
        # `localhost` has no config entry but resolves — legitimate direct target.
        with patch("codeagent.transport.control_master.shutil.which", return_value="/usr/bin/ssh"), patch(
            "codeagent.transport.control_master.subprocess.run",
            return_value=_Proc(0, "hostname localhost\n"),
        ), patch("codeagent.transport.control_master._alias_configured", return_value=False), patch(
            "codeagent.transport.control_master._dns_resolve_error", return_value=None
        ):
            assert resolve_alias("localhost") == "localhost"

    def test_ip_literal_is_allowed_without_config(self):
        with patch("codeagent.transport.control_master.shutil.which", return_value="/usr/bin/ssh"), patch(
            "codeagent.transport.control_master.subprocess.run",
            return_value=_Proc(0, "hostname 10.227.192.69\n"),
        ), patch("codeagent.transport.control_master._alias_configured", return_value=False):
            assert resolve_alias("10.227.192.69") == "10.227.192.69"

    def test_ssh_g_failure_raises_transport_error(self):
        with patch("codeagent.transport.control_master.shutil.which", return_value="/usr/bin/ssh"), patch(
            "codeagent.transport.control_master.subprocess.run",
            return_value=_Proc(1, "", "Bad configuration option: FooBar"),
        ):
            with pytest.raises(TransportError, match="ssh -G failed"):
                resolve_alias("yellow")

    def test_missing_ssh_bin_raises(self):
        with patch("codeagent.transport.control_master.shutil.which", return_value=None):
            with pytest.raises(TransportError, match="ssh binary not found"):
                resolve_alias("yellow")


# ── ControlMaster.create: pre-validation + transient retry ─────────────


class TestControlMasterCreate:
    def test_network_error_retried_then_success(self, tmp_path: Path):
        procs = [
            _Proc(255, "", "ssh: Could not resolve hostname yellow: Temporary failure in name resolution"),
            _Proc(0),
        ]
        with patch("codeagent.transport.control_master.subprocess.run", side_effect=procs) as m_run, patch(
            "codeagent.transport.control_master.time.sleep"
        ) as m_sleep, patch.object(ControlMaster, "is_alive", return_value=False), patch(
            "codeagent.transport.control_master.shutil.which", return_value="/usr/bin/ssh"
        ), patch(
            "codeagent.transport.control_master._socket_dir", return_value=tmp_path
        ), patch(
            "codeagent.transport.control_master.resolve_alias", return_value="10.0.0.1"
        ):
            cm = ControlMaster("yellow")
            cm.create()

        assert m_run.call_count == 2
        m_sleep.assert_called_once_with(1.0)
        # Companion metadata written on success (with_suffix replaces .sock).
        meta = tmp_path / f"{_host_hash('yellow')}.meta"
        assert meta.exists()
        assert "yellow" in meta.read_text()

    def test_network_error_retries_exhausted(self, tmp_path: Path):
        err = _Proc(255, "", "ssh: Could not resolve hostname yellow: Connection timed out")
        with patch("codeagent.transport.control_master.subprocess.run", side_effect=[err, err, err]) as m_run, patch(
            "codeagent.transport.control_master.time.sleep"
        ) as m_sleep, patch.object(ControlMaster, "is_alive", return_value=False), patch(
            "codeagent.transport.control_master.shutil.which", return_value="/usr/bin/ssh"
        ), patch(
            "codeagent.transport.control_master._socket_dir", return_value=tmp_path
        ), patch(
            "codeagent.transport.control_master.resolve_alias", return_value="10.0.0.1"
        ):
            cm = ControlMaster("yellow")
            with pytest.raises(TransportError, match="network error \\(transient; retries exhausted\\)"):
                cm.create()

        assert m_run.call_count == 3
        assert m_sleep.call_args_list[0].args == (1.0,)
        assert m_sleep.call_args_list[1].args == (2.0,)

    def test_auth_failure_fails_fast(self, tmp_path: Path):
        err = _Proc(255, "", "mianyin@10.0.0.1: Permission denied (publickey).")
        with patch("codeagent.transport.control_master.subprocess.run", side_effect=[err]) as m_run, patch(
            "codeagent.transport.control_master.time.sleep"
        ) as m_sleep, patch.object(ControlMaster, "is_alive", return_value=False), patch(
            "codeagent.transport.control_master.shutil.which", return_value="/usr/bin/ssh"
        ), patch(
            "codeagent.transport.control_master._socket_dir", return_value=tmp_path
        ), patch(
            "codeagent.transport.control_master.resolve_alias", return_value="10.0.0.1"
        ):
            cm = ControlMaster("yellow")
            with pytest.raises(TransportError, match="authentication failed"):
                cm.create()

        assert m_run.call_count == 1
        m_sleep.assert_not_called()

    def test_config_failure_fails_fast(self, tmp_path: Path):
        err = _Proc(255, "", "Bad configuration option: FooBar")
        with patch("codeagent.transport.control_master.subprocess.run", side_effect=[err]) as m_run, patch(
            "codeagent.transport.control_master.time.sleep"
        ), patch.object(ControlMaster, "is_alive", return_value=False), patch(
            "codeagent.transport.control_master.shutil.which", return_value="/usr/bin/ssh"
        ), patch(
            "codeagent.transport.control_master._socket_dir", return_value=tmp_path
        ), patch(
            "codeagent.transport.control_master.resolve_alias", return_value="10.0.0.1"
        ):
            cm = ControlMaster("yellow")
            with pytest.raises(TransportError, match="SSH configuration error"):
                cm.create()

        assert m_run.call_count == 1

    def test_unconfigured_alias_fails_before_connection(self, tmp_path: Path):
        with patch(
            "codeagent.transport.control_master.resolve_alias",
            side_effect=SSHAliasError("未配置 SSH alias: 'xyz'"),
        ), patch("codeagent.transport.control_master.subprocess.run") as m_run, patch.object(
            ControlMaster, "is_alive", return_value=False
        ), patch(
            "codeagent.transport.control_master._socket_dir", return_value=tmp_path
        ):
            cm = ControlMaster("xyz")
            with pytest.raises(SSHAliasError, match="未配置 SSH alias"):
                cm.create()

        # No master-creation attempt happened at all.
        m_run.assert_not_called()

    def test_idempotent_when_alive(self, tmp_path: Path):
        with patch.object(ControlMaster, "is_alive", return_value=True), patch(
            "codeagent.transport.control_master.subprocess.run"
        ) as m_run:
            ControlMaster("yellow").create()
        m_run.assert_not_called()


# ── misc helpers ───────────────────────────────────────────────────────


class TestMisc:
    def test_looks_like_ip(self):
        assert _looks_like_ip("10.227.192.69")
        assert _looks_like_ip("2001:db8::1")
        assert not _looks_like_ip("yellow")
        assert not _looks_like_ip("10.227.192")

    def test_host_hash_stable(self):
        assert _host_hash("yellow") == _host_hash("yellow")
        assert _host_hash("yellow") != _host_hash("other")
