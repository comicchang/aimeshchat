"""SSH ControlMaster socket management.

Each remote host gets an independent SSH ControlMaster socket.
Socket path: ``$XDG_RUNTIME_DIR/aimeshchat/ssh/<host-hash>.sock``
Fallback:    ``$TMPDIR/aimeshchat-<UID>/ssh/<host-hash>.sock``

The *host-hash* is a stable 12-char hex digest of the SSH alias,
so different aliases never share a socket.
"""
from __future__ import annotations

import fnmatch
import hashlib
import ipaddress
import json
import logging
import os
import shutil
import socket
import stat
import subprocess
import time
from pathlib import Path

from codeagent.transport.base import TransportError

log = logging.getLogger(__name__)


class SSHAliasError(TransportError):
    """Raised when a host alias is not defined in any ssh config.

    Distinct from network/auth/config failures: an undefined alias is a
    configuration bug that retrying can never fix, so it fails fast with
    an actionable message instead of a generic resolution error.
    """

def _known_hosts_file() -> str:
    """Explicit known_hosts path for aimeshchat SSH connections.

    P2-16: pointing UserKnownHostsFile explicitly (instead of leaving it
    to ~/.ssh/config) guarantees a config file cannot silently redirect
    host-key state to /dev/null or a per-host file that aimeshchat does not
    expect. ``AIMESHCHAT_KNOWN_HOSTS`` overrides for testing/containers.
    """
    return os.environ.get("AIMESHCHAT_KNOWN_HOSTS") or os.path.expanduser("~/.ssh/known_hosts")


# P2-16: explicit host-key verification policy. The SSH default inherits
# ~/.ssh/config, where ``StrictHostKeyChecking=no`` would silently accept a
# MITM key. accept-new keeps TOFU for first-time hosts (key is added to
# known_hosts) but REJECTS a changed key — protecting against spoofed or
# rekeyed hosts. Command-line -o beats any config value.
HOST_KEY_OPTS: list[str] = [
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "UserKnownHostsFile=" + _known_hosts_file(),
]

# SSH options applied to every master creation.
_MASTER_OPTS: list[str] = [
    "-o", "ControlMaster=yes",
    "-o", "ControlPersist=no",
    "-o", "ClearAllForwardings=yes",
    "-o", "ForwardAgent=no",
    "-o", "ForwardX11=no",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    *HOST_KEY_OPTS,
]

# ── create() retry policy ───────────────────────────────────────────────
#
# SSH name resolution / connection setup can fail transiently (network
# flap, resolver hiccup — e.g. "Temporary failure in name resolution" on a
# config alias whose HostName is an IP literal, which means ssh never
# touched DNS and the failure was a transient config/network glitch).  A
# short backoff retry clears most of those.  Config/auth failures are
# permanent by nature and must fail fast — never retried.

_CREATE_ATTEMPTS = 3                 # initial attempt + 2 retries
_CREATE_RETRY_BACKOFF_S = (1.0, 2.0)  # sleep between attempts

# stderr patterns that retrying will never fix.  Order matters: auth/config
# patterns are checked before network patterns because a "Permission denied"
# banner can be followed by server text that also matches a transient pattern.
_NONTRANSIENT_SSH_PATTERNS = (
    "permission denied",
    "authentication failed",
    "too many authentication failures",
    "host key verification failed",
    "bad configuration option",
    "unknown option",
    "could not open",
    "unable to open",
    "no such file or directory",
    "not a regular file",
    "no matching host key",
    "no matching cipher",
    "no matching key exchange",
    "invalid format",
    "configuration error",
)

# stderr patterns indicating a transient network/resolver failure.
_TRANSIENT_SSH_PATTERNS = (
    "temporary failure in name resolution",
    "could not resolve hostname",
    "nodename nor servname provided",
    "name or service not known",
    "connection refused",
    "connection timed out",
    "connection reset",
    "connection closed",
    "network is unreachable",
    "no route to host",
    "host is unreachable",
    "operation timed out",
    "resource temporarily unavailable",
    "too many open files",
    "broken pipe",
    "connection attempt failed",
)


def classify_ssh_error(stderr: str, returncode: int = 255) -> str:
    """Classify an ssh failure into ``auth`` | ``config`` | ``network`` | ``unknown``.

    Used to decide whether ``create()`` may retry (only ``network``) and to
    produce an actionable error message.  ``returncode`` is accepted for
    interface completeness; classification is driven by stderr text.
    """
    text = (stderr or "").strip().lower()
    if not text:
        return "unknown"
    for pat in _NONTRANSIENT_SSH_PATTERNS:
        if pat in text:
            if (
                "permission denied" in text
                or "authentication" in text
                or "too many authentication" in text
            ):
                return "auth"
            return "config"
    for pat in _TRANSIENT_SSH_PATTERNS:
        if pat in text:
            return "network"
    return "unknown"


def _looks_like_ip(alias: str) -> bool:
    """True if *alias* is a literal IPv4/IPv6 address (no DNS involved)."""
    try:
        ipaddress.ip_address(alias.strip("[]"))
        return True
    except ValueError:
        return False


def _host_pattern_matches(pattern: str, alias: str) -> bool:
    """Match one OpenSSH ``Host`` pattern against *alias*.

    OpenSSH host patterns support ``*``, ``?`` and ``[...]`` (fnmatch
    semantics) and are matched case-insensitively.
    """
    return fnmatch.fnmatchcase(alias.lower(), pattern.lower())


def _host_line_matches(patterns: list[str], alias: str) -> bool:
    """Evaluate a ``Host`` line's pattern list against *alias* (OpenSSH rules).

    Verified against real ``ssh -G`` behavior: patterns are tested left to
    right; a ``!``-prefixed pattern *excludes* the host when its body
    matches (``Host !secret.internal *.internal`` excludes
    ``secret.internal`` but includes ``other.internal``); the first
    positive match includes the host.
    """
    for pat in patterns:
        p = pat.strip()
        if not p:
            continue
        if p.startswith("!"):
            if _host_pattern_matches(p[1:].lstrip(), alias):
                return False
        elif _host_pattern_matches(p, alias):
            return True
    return False


def _host_line_defines(patterns: list[str], alias: str) -> bool:
    """True if a ``Host`` line *defines* *alias* (as opposed to a defaults block).

    Same semantics as ``_host_line_matches``, except a bare ``*`` pattern
    is treated as a catch-all defaults block (``Host *`` with
    ``User``/``ControlPath``/… sets defaults, not an alias entry) and does
    not count as defining the alias.  Without this, a ubiquitous
    ``Host *`` block would make every bare name look "configured" and the
    unconfigured-alias check could never fire.
    """
    for pat in patterns:
        p = pat.strip()
        if not p:
            continue
        if p.startswith("!"):
            if _host_pattern_matches(p[1:].lstrip(), alias):
                return False
        elif p == "*":
            continue
        elif _host_pattern_matches(p, alias):
            return True
    return False


def _scan_config_host_matches(path: Path, alias: str, _depth: int = 0) -> bool:
    """Recursively scan an ssh config file for a ``Host`` line matching *alias*.

    Handles ``Include`` (relative to the including file's directory, glob
    expansion, missing files ignored) and ``Host`` pattern lists.  Only
    ``Host`` lines matter for alias existence — ``Match`` blocks never
    define new host entries, and HostName overrides are already visible
    via ``ssh -G`` (``resolve_alias`` compares the resolved hostname).
    """
    if _depth > 8 or not path.is_file():
        return False
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return False
    base = path.parent
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        key = parts[0].lower()
        value = parts[1].strip() if len(parts) > 1 else ""
        if key == "include" and value:
            for pat in value.split():
                expanded = os.path.expanduser(pat)
                if any(ch in pat for ch in "*?["):
                    if pat.startswith("/") or pat.startswith("~"):
                        p = Path(expanded)
                        matches = sorted(p.parent.glob(p.name))
                    else:
                        matches = sorted(base.glob(pat))
                else:
                    p = Path(expanded)
                    matches = [p if p.is_absolute() else base / p]
                for inc in matches:
                    if _scan_config_host_matches(inc, alias, _depth + 1):
                        return True
        elif key == "host" and value:
            patterns = [p.strip() for p in value.replace(",", " ").split()]
            if _host_line_defines(patterns, alias):
                return True
    return False


def _alias_configured(alias: str, config_path: str | None = None) -> bool:
    """True if any user ssh config ``Host`` pattern matches *alias*.

    ``config_path`` overrides ``~/.ssh/config`` for tests.
    """
    if config_path is not None:
        return _scan_config_host_matches(Path(config_path), alias)
    return _scan_config_host_matches(Path.home() / ".ssh" / "config", alias)


def _dns_resolve_error(hostname: str) -> str | None:
    """Return a DNS error description if *hostname* does not resolve, else None."""
    try:
        socket.getaddrinfo(hostname, None)
        return None
    except socket.gaierror as exc:
        return str(exc)


def resolve_alias(alias: str, ssh_bin: str = "ssh") -> str:
    """Resolve *alias* to its effective HostName via ``ssh -G``.

    Returns the ``hostname`` field of ``ssh -G <alias>`` — the address ssh
    will actually connect to (config HostName override, or the alias itself
    when no override exists).

    Raises:
        TransportError: ssh binary missing, or ``ssh -G`` itself failed
            (e.g. broken config — ``ssh -G`` exits non-zero).
        SSHAliasError: *alias* has no HostName override, no literal IP, no
            ``Host`` match in any user config, and does not resolve via
            DNS — i.e. it is a misconfigured alias (typo).  Fail fast
            instead of surfacing an ambiguous ``Temporary failure in name
            resolution`` later.  Bare names that DO resolve (``localhost``,
            a public hostname) are legitimate direct targets and pass.
    """
    ssh = shutil.which(ssh_bin)
    if not ssh:
        raise TransportError(f"ssh binary not found: {ssh_bin}")
    proc = subprocess.run(
        [ssh, "-G", alias], capture_output=True, text=True, timeout=10
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise TransportError(f"ssh -G failed for {alias}: {detail}")
    hostname = alias
    for line in proc.stdout.splitlines():
        if line.lower().startswith("hostname "):
            val = line.split(None, 1)[1].strip()
            if val:
                hostname = val
            break
    if hostname != alias or _looks_like_ip(alias) or _alias_configured(alias):
        return hostname
    # No config entry and not an IP: ssh would connect to the bare name via
    # DNS.  A resolvable bare name (e.g. `localhost`) is a valid direct
    # target; only a name that DNS cannot resolve is a misconfigured alias.
    dns_error = _dns_resolve_error(alias)
    if dns_error is None:
        return hostname
    raise SSHAliasError(
        f"未配置 SSH alias: {alias!r} — ~/.ssh/config 中没有匹配的 Host 条目，"
        f"且 DNS 解析失败: {dns_error}"
        f"（若目标是直连主机名，请检查拼写；直连 IP 可直接使用 IP）"
    )


def _host_hash(alias: str) -> str:
    """Stable short hash for a host alias."""
    return hashlib.sha256(alias.encode()).hexdigest()[:12]


def _socket_dir() -> Path:
    """Return the directory for ControlMaster sockets, creating it if needed.

    Uses 0700 permissions for security.
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        d = Path(xdg) / "aimeshchat" / "ssh"
    else:
        uid = os.getuid()
        d = Path(os.environ.get("TMPDIR", "/tmp")) / f"aimeshchat-{uid}" / "ssh"
    d.mkdir(parents=True, exist_ok=True)
    # Ensure restrictive permissions (0700) even if directory existed before.
    try:
        d.chmod(stat.S_IRWXU)
    except OSError:
        pass
    return d


def socket_path(alias: str) -> Path:
    """Return the ControlMaster socket path for *alias*."""
    return _socket_dir() / f"{_host_hash(alias)}.sock"


def list_sockets() -> list[tuple[str, Path]]:
    """List all managed sockets as ``(alias, socket_path)`` tuples.

    Reads the companion ``.meta`` file written alongside each socket
    to recover the human-readable alias.
    """
    d = _socket_dir()
    if not d.exists():
        return []
    result: list[tuple[str, Path]] = []
    for sock in sorted(d.glob("*.sock")):
        meta = sock.with_suffix(".meta")
        if meta.exists():
            try:
                info = json.loads(meta.read_text())
                alias = info.get("alias", sock.stem)
            except (json.JSONDecodeError, OSError):
                alias = sock.stem
        else:
            alias = sock.stem
        result.append((alias, sock))
    return result


def stop_by_alias(alias: str, ssh_bin: str = "ssh") -> bool:
    """Stop a ControlMaster socket by alias.

    Looks up the socket via ``.meta`` files, sends ``ssh -O exit``,
    and cleans up the socket and metadata files.  Returns True if a
    matching socket was found and stop was attempted.
    """
    d = _socket_dir()
    if not d.exists():
        return False
    for meta in d.glob("*.meta"):
        try:
            info = json.loads(meta.read_text())
            if info.get("alias") != alias:
                continue
        except (json.JSONDecodeError, OSError):
            continue

        sock = meta.with_suffix(".sock")
        ssh = shutil.which(ssh_bin)
        if ssh and sock.exists():
            cmd = [ssh, "-O", "exit", "-S", str(sock), alias]
            log.debug("stopping master: %s", " ".join(cmd))
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if proc.returncode != 0:
                log.warning("master exit for %s returned %d: %s",
                            alias, proc.returncode, proc.stderr.strip())
        # Clean up socket and metadata files.
        try:
            sock.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            meta.unlink(missing_ok=True)
        except OSError:
            pass
        log.info("master stopped for %s", alias)
        return True
    return False


def stop_all(ssh_bin: str = "ssh") -> None:
    """Stop all managed ControlMaster sockets.

    Iterates over all ``.meta`` files and stops each one.
    """
    d = _socket_dir()
    if not d.exists():
        return
    for meta in list(d.glob("*.meta")):
        try:
            info = json.loads(meta.read_text())
            alias = info.get("alias", "")
        except (json.JSONDecodeError, OSError):
            continue
        if alias:
            stop_by_alias(alias, ssh_bin=ssh_bin)


class ControlMaster:
    """Manages a single SSH ControlMaster socket for one host.

    Usage::

        cm = ControlMaster(alias="myhost")
        cm.create()       # establish master
        cm.is_alive()     # True
        cm.stop()         # tear down
    """

    def __init__(self, alias: str, *, ssh_bin: str = "ssh") -> None:
        self.alias = alias
        self._ssh = ssh_bin
        self._socket = socket_path(alias)

    @property
    def socket(self) -> Path:
        return self._socket

    # ── lifecycle ───────────────────────────────────────────────────────

    def create(self) -> None:
        """Open a ControlMaster connection.

        Idempotent — if the socket is already alive, this is a no-op.

        Flow: alias pre-validation (``ssh -G``) → stale-socket cleanup →
        master creation with transient-network retry.

        - Alias not defined in any ssh config (and not a literal IP) →
          ``SSHAliasError`` immediately; retrying cannot fix a missing
          config entry.
        - Network-class create failures (e.g. ``Could not resolve
          hostname``, ``Connection refused``) are retried with backoff
          (``_CREATE_ATTEMPTS`` / ``_CREATE_RETRY_BACKOFF_S``).
        - Auth/config failures raise immediately — never retried.
        """
        if self.is_alive():
            log.debug("master already alive for %s", self.alias)
            return

        ssh = shutil.which(self._ssh)
        if not ssh:
            raise TransportError(f"ssh binary not found: {self._ssh}")

        # Alias pre-validation: catch an unconfigured alias before any
        # connection attempt, so the failure is actionable instead of an
        # ambiguous "Could not resolve hostname".  Transient resolution
        # failures on a *configured* alias fall through to the retry loop.
        resolve_alias(self.alias, ssh_bin=self._ssh)

        # P2-13: 清理陈旧 socket——is_alive() 为 False 但 socket 文件残留时
        # （master 被 kill/crash 后未走 stop() 清理），ssh -M -N -f 会因
        # ControlPath 已存在而失败（"ControlPath ... already exists"），
        # 造成永久 TransportError。创建前 unlink 残留 socket + companion .meta。
        if self._socket.exists():
            log.info(
                "removing stale socket %s before create for %s",
                self._socket, self.alias,
            )
            try:
                self._socket.unlink(missing_ok=True)
            except OSError as exc:
                raise TransportError(
                    f"failed to remove stale socket {self._socket}: {exc}"
                ) from exc
            meta = self._socket.with_suffix(".meta")
            try:
                meta.unlink(missing_ok=True)
            except OSError:
                pass

        cmd = [
            ssh,
            "-M", "-N", "-f",
            "-S", str(self._socket),
            *_MASTER_OPTS,
            self.alias,
        ]
        last_detail = ""
        last_cls = "unknown"
        for attempt in range(_CREATE_ATTEMPTS):
            log.debug(
                "creating master (attempt %d/%d): %s",
                attempt + 1, _CREATE_ATTEMPTS, " ".join(cmd),
            )
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if proc.returncode == 0:
                log.info("master created for %s (socket %s)", self.alias, self._socket)

                # Write companion metadata so other processes can map socket → alias.
                meta = self._socket.with_suffix(".meta")
                try:
                    meta.write_text(
                        json.dumps({"alias": self.alias, "created": time.time()})
                    )
                except OSError as exc:
                    log.warning("failed to write .meta for %s: %s", self.alias, exc)
                return

            last_detail = (
                proc.stderr.strip()
                or proc.stdout.strip()
                or f"exit code {proc.returncode}"
            )
            last_cls = classify_ssh_error(last_detail, proc.returncode)
            if last_cls != "network" or attempt == _CREATE_ATTEMPTS - 1:
                break
            backoff = _CREATE_RETRY_BACKOFF_S[attempt]
            log.warning(
                "transient SSH error creating master for %s (attempt %d/%d): %s "
                "— retrying in %.1fs",
                self.alias, attempt + 1, _CREATE_ATTEMPTS, last_detail, backoff,
            )
            time.sleep(backoff)

        labels = {
            "auth": "authentication failed",
            "config": "SSH configuration error",
            "network": "network error (transient; retries exhausted)",
            "unknown": "unknown error",
        }
        raise TransportError(
            f"failed to create master for {self.alias}: "
            f"{labels.get(last_cls, last_cls)}: {last_detail}"
        )

    def is_alive(self) -> bool:
        """Check if the ControlMaster socket is active."""
        return self._check() == 0

    def check(self) -> bool:
        """Alias for ``is_alive()``."""
        return self.is_alive()

    def stop(self) -> None:
        """Shut down the ControlMaster.

        Idempotent — no-op if already stopped.
        """
        if not self._socket.exists():
            return

        ssh = shutil.which(self._ssh)
        if not ssh:
            # Socket file exists but ssh gone — just clean up.
            self._cleanup_socket()
            return

        cmd = [ssh, "-O", "exit", "-S", str(self._socket), self.alias]
        log.debug("stopping master: %s", " ".join(cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            log.warning("master exit for %s returned %d: %s",
                        self.alias, proc.returncode, proc.stderr.strip())
        self._cleanup_socket()
        log.info("master stopped for %s", self.alias)

    # ── ssh command builder ─────────────────────────────────────────────

    def ssh_cmd(self, *remote_args: str) -> list[str]:
        """Build an ``ssh`` command that reuses this master socket.

        Returns the full argv (ssh binary + options + alias + remote args).
        """
        ssh = shutil.which(self._ssh)
        if not ssh:
            raise TransportError(f"ssh binary not found: {self._ssh}")
        return [ssh, "-S", str(self._socket), self.alias, *remote_args]

    # ── internals ───────────────────────────────────────────────────────

    def _check(self) -> int:
        """Run ``ssh -O check`` and return the exit code."""
        if not self._socket.exists():
            return 1

        ssh = shutil.which(self._ssh)
        if not ssh:
            return 1

        cmd = [ssh, "-O", "check", "-S", str(self._socket), self.alias]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return proc.returncode

    def _cleanup_socket(self) -> None:
        """Remove the socket file and its companion .meta file."""
        try:
            self._socket.unlink(missing_ok=True)
        except OSError:
            pass
        meta = self._socket.with_suffix(".meta")
        try:
            meta.unlink(missing_ok=True)
        except OSError:
            pass
