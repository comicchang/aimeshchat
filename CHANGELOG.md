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

## [0.2.0] — UNRELEASED

Major release: Oracle NO-GO → 4 P0 + 9 P1 + 7 P2 fixed across 5 batches
(B1–B5). Delivery receipts, stream cursor, callback safety, routing TOCTOU,
outbox CLI, session-ensure, plugin identity, hooks lifecycle, tmux-agent skill
merge, and deployment modes.

### Fixed

- **P0-1 Stream cursor monotonic** (B1-T2): opaque `epoch_ms/seq` cursor persisted to
  `.stream-cursor`, compared lexicographically — same-second message loss
  eliminated. Emits full `Message` payload (body, attachments, reply_to, run_id,
  request_id). `SSHStream` uses `STREAM_CURSOR_INITIAL`.
- **P0-2 Delivery receipt propagation** (B1-T1): `DeliverySink` contract returns
  `SendReceipt` everywhere; `EngineDeliverySink`, `LocalDeliverySink` propagate
  real status (`accepted`+`queued` on transport failure, `delivered` on local
  success) instead of hardcoded 'delivered'. `SendReceipt.queued` added to model.
- **P0-3 Callback failure safety** (B1-T3): receiver only acks on callback success;
  `_fire_callbacks` returns `False` if zero matched or any raised — failed/uncalled
  callbacks leave message in inbox for retry, never archive.
- **P0-4 Routing TOCTOU** (B2-T1): `_persist_meta` locks a separate `.swarm-meta.lock`
  (stable inode, no lock-invalidation race), reads/writes under lock, fail-closed
  (no unlocked fallback). `_persist_routing`/`_persist_channels` merge entries per
  session instead of full-overwrite. `unregister` explicitly deletes agent from disk.
- **P1 Outbox silent permanent-queued** (B3-T1/T2): `swarm outbox flush` exits
  non-zero when nothing flushed — no more silent permanent-queued with 'delivered'
  display.
- **P1 Flush retry appends history** (B3): flush retries append to message history
  instead of overwriting.
- **P1 Receiver ack archives** (B3): receiver ack properly archives processed
  messages.
- **P1 Channel/notice receipts** (B3): channel and notice delivery returns real
  receipt status.
- **P1 Session meta lock** (B3): session metadata writes use locked path.

### Added

- **Outbox CLI** (B3-T1/T2): `swarm outbox pending|flush|status` subcommands for
  durable outbox management. `DeliveryEngine.outbox_stats()`.
- **Opportunistic flush** (B3): `swarm watch` + kernel factory attempt flush on
  startup (try/except, no crash when transport absent).
- **Session-ensure full roster** (B3-T3): `_remote_send` now does capability check
  (fail-closed, cached per host) → idempotent session-init with FULL roster (cached
  per session+host pair) → send. `MailboxStore.session_init` merges new agents into
  existing session (no duplicate agents).
- **Capability check fail-closed** (B3-T3): `_check_capability(host)` fails closed on
  transport errors, cached per host.
- **Launcher-injected identity env** (B4-T2): `OMPRunner._extra_env()` injects
  namespaced mailbox identity (`SWARM_SESSION_ID`, `OMP_MAILBOX_SESSION_ID`,
  `OMP_MAILBOX_AGENT_ID`, `OMP_MAILBOX_IDENTITY_FILE`, `MAILBOX_ROOT`) into OMP
  subprocess env via `BaseRunner._extra_env()` hook. Identity file (per-run token)
  written before spawn, removed in cleanup.
- **Plugin type gate** (B4): `scripts/check-plugin-types.sh` — typecheck + test gate
  for omp-mailbox-plugin.
- **Unified tmux-agent skill** (B5-T2): `skills/tmux-agent/` with progressive
  disclosure — `SKILL.md` (role determination, shared invariants) +
  `roles/{manager,worker}.md` + `protocol/mailbox.md`. Old `tmux-agent-manager/`
  and `tmux-agent-worker/` become deprecation redirect stubs.
- **Deployment modes documented** (B5-T3): Mode A Shared FS (Syncthing, explicit
  `MAILBOX_ROOT=.mailbox`) vs Mode B Remote Transport (SSH/relay, default) with
  decision tree; `operations/{local,remote}.md`.

### Changed

- **`bin/mailbox` removed from plugin** (B5): PATH canonical CLI (`mailbox`,
  `mailbox-hook`, `mailbox-health`) replaces plugin-local bin stubs.
- **`swarm_hooks` lifecycle fixed** (B4-T3): tracks `_registered` pairs + `_store_root`;
  `on_agent_stop` no-ops safely when never registered, unregisters when it was;
  `reset()` clears all. OMP `_parse_output`/`_cleanup` hook failures log WARNING
  instead of silent pass.
- **`LocalDeliverySink` preserves kernel `msg_id`** (B1): local delivery uses
  kernel-assigned message ID, not sink-generated.
- **`mailbox-hook`/`mailbox-health` status** (B5): read-only diagnostics and
  peek-only notification hook — canonical entry points documented in skill protocol.

### Security

- **Dotai fail-closed plugin install** (B4): plugin install fails closed on
  transport/setup errors — no silent partial installs.
- **Registry-driven `node_modules` cleanup** (B5): dotai `components.json` registry
  controls plugin installation and deprecation.
- **Pinned codeagent install ref** (B5): standalone install guide references pinned
  codeagent version, no dotai dependency.

### Test

- Concurrent routing tests (B2-T2): real subprocess concurrency — two processes
  register different agents → both survive; register+create_channel cross-op;
  unregister removes only own.
- Identity injection tests (B4-T2): +4 tests for `_extra_env()` and identity file
  lifecycle.
- Hooks lifecycle tests (B4-T3): +6 tests for registration/unregistration/reset.
- Outbox + session-ensure tests (B3): +5 tests.
- 1016 tests passed, 10 skipped (as of Batch-5).

---

Source of truth: 7-batch execution plan (B1–B7). B1–B5 merged; B6 (version,
README, changelog, dotai trigger) in progress; B7 (release gate, tag) pending.

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
