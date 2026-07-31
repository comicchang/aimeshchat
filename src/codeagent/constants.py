"""Centralized timeouts and limits.

Single source of truth for every timeout/limit in the codebase — no
magic numbers scattered across transports, runners, or stores.
"""
from __future__ import annotations

# ── execution / transport timeouts (seconds) ───────────────────────────
DEFAULT_EXEC_TIMEOUT = 600  # default per-request task timeout
DEFAULT_SSH_TIMEOUT = 600  # default timeout for an SSH wire round-trip
DEFAULT_RELAY_TIMEOUT = 600  # default timeout for a relay-login PTY session
DEFAULT_MAILBOX_TIMEOUT = 60  # default timeout for mailbox wire requests
DEFAULT_PULL_TIMEOUT = 60  # default timeout for an scp artifact pull

# ── startup / handshake timeouts (seconds) ─────────────────────────────
READY_TIMEOUT = 15  # how long to wait for the remote helper to print ``ready``
STARTUP_TIMEOUT = 15  # how long to wait for a helper subprocess to start

# ── session / lease timeouts (seconds) ─────────────────────────────────
LEASE_TIMEOUT_S = 300  # mailbox claim lease — stale after 5 minutes

# ── size limits ────────────────────────────────────────────────────────
MAX_LINE_LENGTH = 1_048_576  # 1 MiB
MAX_MAILBOX_BODY = 100_000  # 100 KiB
