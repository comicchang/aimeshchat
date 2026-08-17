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
ORACLE_TIMEOUT = 3600  # oracle-class agents (park=true, auto-exit=false) — covers 30–60min LLM turns
RUN_HEARTBEAT_INTERVAL = 30  # seconds between progress heartbeats during remote exec (distinguishes slow vs stuck)
SSH_IDLE_WINDOW = 180  # minimum idle window for SSH queue.get(timeout) — prevents old --timeout values from bypassing heartbeat

# ── startup / handshake timeouts (seconds) ─────────────────────────────
READY_TIMEOUT = 15  # how long to wait for the remote helper to print ``ready``
STARTUP_TIMEOUT = 15  # how long to wait for a helper subprocess to start

# ── session / lease timeouts (seconds) ─────────────────────────────────
LEASE_TIMEOUT_S = 3600  # mailbox claim lease — stale after 1 hour (covers oracle single-turn 30–60min); 300s was < oracle turn → claim expired → "no claim file" on finalize
# P2-7: cross-device clock skew tolerance.  When the claimant and reaper
# live on different hosts, their wall clocks may diverge.  The lease
# comparison uses the LOCAL filesystem mtime of the claim file (set by
# os.link) rather than the claimant's claimed_at timestamp; the tolerance
# covers the window between the claimant's write and the reaper's stat().
# NTP should keep skew < 1 s; 30 s is generous.  Operators MUST ensure NTP
# is configured on every swarm host.
LEASE_CLOCK_TOLERANCE_S = 30

# ── size limits ────────────────────────────────────────────────────────
MAX_LINE_LENGTH = 1_048_576  # 1 MiB — wire JSONL frame limit
MAX_MAILBOX_BODY = 100_000  # 100 KiB — per-message body limit
MAX_ATTACHMENT_SIZE = 100_000  # 100 KiB — per-attachment file size limit

# ── timestamp formats (single source — used by store/kernel/delivery) ──
ISO_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"  # canonical created_at/claimed_at/updated_at
MSG_ID_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S"  # compact timestamp in generated msg_ids

# ── long-lived stream settings ─────────────────────────────────────────
STREAM_HEARTBEAT_INTERVAL = 15  # seconds between heartbeat pings
STREAM_RECONNECT_MAX = 30  # max reconnect backoff in seconds
STREAM_RECONNECT_BASE = 1  # initial reconnect backoff in seconds

# ── relay PTY / receiver watch (seconds, bytes) ────────────────────────
PTY_READ_CHUNK = 4096  # bytes per PTY read
DEFAULT_WATCH_POLL_INTERVAL = 0.5  # receiver watch mode scan interval
LOOP_TICK = 0.25  # max block in receiver loop() per iteration

# ── opaque stream cursor ───────────────────────────────────────────────
# Server-side monotonic arrival counter persisted per-session.
# Format: "<epoch_ms>/<seq>" (seq = per-epoch 0-based counter, zero-padded
# to SEQ_WIDTH so the cursor string stays lexicographically ordered).
# Cursor file: <mailbox_root>/<session_id>/.stream-cursor
STREAM_CURSOR_INITIAL = "0"
STREAM_CURSOR_FILE = ".stream-cursor"
SEQ_WIDTH = 6  # zero-pad width for the seq component (P0-b: "10" must sort after "9")

