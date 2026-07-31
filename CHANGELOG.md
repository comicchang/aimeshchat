# Changelog

All notable changes to **codeagent** are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Round 13 — Final polish

- **Coverage**: 603 tests, 96% overall. `transport.relay` raised from 82% to
  100% with edge-path tests for the PTY select loop (preexec controlling-TTY
  setup, slave-fd close failure, stdin probe/EOF/read-error handling, deadline
  break, blank-line skip, unhandled wire types, reap timeout, kill-escalation
  exhaustion, and cleanup-close failures).
- **Dead code removed**: the unreachable `handler is None` fallback in
  `cli.main` (argparse already restricts `args.command` to the registered
  subcommands, and `None` is handled earlier).
- **Test hygiene**: every `pytest.skip` / `@pytest.mark.skipif` now carries an
  explicit `reason` string.

### Round 12 — Robustness + integration

- **Relay transport hardening** (`transport.relay`): bounded select loop with an
  iteration cap, output-buffer cap, escalating SIGTERM→SIGKILL termination with
  a grace period, and parse-state transition logging for diagnostics.
- **Constants module** (`codeagent.constants`): centralized shared timeouts and
  limits (`DEFAULT_RELAY_TIMEOUT`, `MAX_LINE_LENGTH`, termination grace).
- **Integration suite**: 8 SSH error-path integration tests; integration tests
  are gated behind `--run-integration` with explicit skip reasons when disabled.

### Added

- Coverage gate raised from 75% to 85% (`--cov-fail-under=85`).
- Test coverage for error paths across `cli`, `transport.control_master`, and
  `mailbox.store` (100% on all three modules as of this release).

## [0.1.0] — 2026-07-31

Initial public release of `codeagent-py` — multi-host code agent orchestration.

### Added

- **CLI facade** (`codeagent.cli`): unified entry point with `run`, `route`,
  `sessions`, `ssh`, `mailbox`, and `artifact` subcommands, plus `--version`/`--help`.
- **Routing** (`codeagent.routing`): resolve `RunRequest`s against the repo map —
  explicit host, topic-based, or local fallback — with `resolve_is_local` detection.
- **Session registry** (`codeagent.session`): SQLite-backed session persistence,
  per-key locking, resume, `mark_starting/observed/active/failed` lifecycle,
  and `list/show/reset/bind` CLI verbs.
- **Transports**:
  - `LocalTransport` — in-process wire execution via `codeagent.remote_exec`.
  - `SSHTransport` — ControlMaster-backed SSH wire protocol, warm/check/stop,
    fallback alias retry on warm/execute failure.
  - `RelayTransport` — relay-login bastion host execution with PTY + expect.
  - SSH **ControlMaster** socket management: stable per-alias sockets,
    `.meta` companion files for cross-process alias discovery, `stop_by_alias`,
    `stop_all`, and cleanup on stop.
- **Mailbox subsystem** (`codeagent.mailbox`): filesystem-backed direct-inbox
  protocol with two-phase consumption (inbox → processing → archive),
  claim-file concurrency protection, stale-claim recovery, status snapshots,
  corrupt-message quarantine, and a CLI 100% compatible with the original
  `tools/mailbox`. Cross-host mailbox dispatch runs over the SSH wire protocol.
- **Artifact transport** (`codeagent.artifact`): descriptor validation
  (path traversal, absolute-path, `..` checks), scp-based pull over
  ControlMaster, and post-transfer SHA-256 + size verification.
- **Wire protocol** (`codeagent.wire`): versioned JSONL protocol with
  `ready`/`accepted`/`session`/`result`/`error`/`mailbox_request` messages,
  max line length enforcement, and version-mismatch fail-closed.
- **Entry points**: `codeagent`, `codeagent-remote-exec`, `mailbox`,
  `mailbox-hook`, `mailbox-health`.

### Fixed

- `send()` fails closed when `session.json` is missing or corrupt.
- Mailbox `read()`/`finalize()`/`release()` enforce owner identity via claim files.
- Wire protocol rejects oversized lines and unknown message types.
- SSH error paths (`ssh: ...` detection) surface user-readable messages instead
  of raw exit codes.
- Artifact pull verifies size and SHA-256 after transfer, not just before.

### Security

- Socket dirs created with 0700 permissions.
- Agent/session IDs validated against path traversal; message filenames checked
  against `msg_id` to prevent smuggling.
- `MAX_LINE_LENGTH` / `MAX_MAILBOX_BODY` caps on wire and mailbox payloads.
