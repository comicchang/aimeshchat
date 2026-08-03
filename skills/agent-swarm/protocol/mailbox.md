# Mailbox Protocol Reference

> Canonical protocol for all tmux-agent communication. Referenced by both Manager and Worker roles.

## Directory Structure

Mailbox root is `.mailbox/` under the shared repository, session-isolated:

```text
.mailbox/<session_id>/
  session.json       # {manager, agents, created_at}
  manager/
    inbox/           # other participants write directly here
    processing/      # auto-claimed by mailbox read (owner + lease)
    archive/         # finalized messages
    _corrupt/        # parse or self-validation failures
  <agent-id>/
    inbox/
    processing/
    archive/
    _corrupt/
    status.json      # agent self-reported state
```

## Message Schema

8 required fields, 3 optional association fields:

| Field | Required | Description |
|---|---|---|
| `session_id` | yes | session identifier |
| `from` | yes | sender ID (must be in roster) |
| `to` | yes | recipient ID (must be in roster) |
| `subject` | yes | short summary |
| `body` | yes | full content (self-contained for TASK) |
| `kind` | yes | message type (see kinds below) |
| `msg_id` | yes | unique message ID (matches filename) |
| `created_at` | yes | UTC timestamp |
| `reply_to` | no | references another msg_id |
| `run_id` | no | run identifier |
| `request_id` | no | request identifier |

Messages are **immutable**. Corrections require a new message with `--reply-to <msg_id>`.

## Message Kinds

| Kind | Direction | Purpose |
|---|---|---|
| `TASK` | Manager→Worker | formal task dispatch (INIT also uses TASK with subject="INIT") |
| `REPORT` | Worker→Manager | complete conclusion with artifact references |
| `PROGRESS` | Worker→Manager | intermediate progress update |
| `EVIDENCE` | Worker→Manager | evidence fragment or artifact |
| `QUESTION` | Worker→Manager or Worker→Worker | clarification request |
| `RESPONSE` | Any→Any | answer to a QUESTION |
| `NOTICE` | Any→Any | status change, error, or coordination signal |

## status.json Contract

Each agent writes only its own `.mailbox/<session>/<agent>/status.json`. Exactly 5 fields:

```json
{
  "session_id": "<session-id>",
  "state": "IDLE",
  "current_task": "waiting for TASK",
  "last_conclusion": "INIT accepted",
  "updated_at": "2026-08-02T00:00:00Z"
}
```

| Field | Description |
|---|---|
| `session_id` | must match the directory name |
| `state` | `IDLE` \| `BUSY` \| `DONE` \| `BLOCKED` — no other values |
| `current_task` | one-line task summary (not full task text) |
| `last_conclusion` | one-line latest conclusion or block reason |
| `updated_at` | UTC freshness diagnostic, not cross-machine ordering truth |

State transitions:
- **IDLE** → available for dispatch
- **BUSY** → task in progress (set at task start)
- **DONE** → task complete (set after final REPORT sent)
- **BLOCKED** → cannot proceed (set after NOTICE/REPORT with reason)

Manager reads status; Worker writes it. No other agent modifies another's status.

## Two-Phase Consumption

All message reading follows this atomic pattern:

```bash
# Phase 1: Claim — moves message from inbox to processing (owner + lease)
mailbox read --session <session-id> --agent <agent-id> --owner <agent-id> [--json]

# Phase 2: Finalize — moves message from processing to archive (validates owner)
mailbox finalize --session <session-id> --agent <agent-id> --msg-id <id> --owner <agent-id>
```

Between read and finalize, the agent processes the message. If processing fails or is abandoned, `mailbox release` returns it to inbox.

## CLI Command Reference

All commands operate on session-based paths. `<session-id>` and `<agent-id>` are placeholders — replace with actual values from INIT.

CLI resolution order:
1. PATH command `mailbox` (from `codeagent` package via `uv tool install`)
2. `codeagent mailbox` as unified cross-host entry point
3. For swarm sessions: `codeagent swarm ...` subcommands

Never route through `scripts/tmux_worker.py` or other legacy wrappers.

### Sending

```bash
# Send a message (roster validation is automatic; fails if recipient not in session)
mailbox send \
  --session <session-id> --from <sender-id> --to <recipient-id> --kind <kind> \
  --subject "<subject>" --body "<body>" [--reply-to <msg_id>]
```

### Reading

```bash
# Non-destructive peek (count and preview, no state change)
mailbox peek --session <session-id> --agent <agent-id>

# Two-phase consumption (see above)
mailbox read --session <session-id> --agent <agent-id> --owner <agent-id> [--json]
mailbox finalize --session <session-id> --agent <agent-id> --msg-id <id> --owner <agent-id>

# Release — return message from processing back to inbox
mailbox release --session <session-id> --agent <agent-id> --msg-id <id> --owner <agent-id>
```

### Status

```bash
# Write status snapshot (auto-fills session_id and updated_at)
mailbox status --session <session-id> --agent <agent-id> \
  --state <state> --current-task "<task>" --last-conclusion "<conclusion>"
```

### Session Management

```bash
# Create session with roster
mailbox session-init --session <session-id> --manager manager --agents <id1>,<id2>

# Statistics (shows all 4 dirs: inbox/processing/archive/_corrupt)
mailbox stats --session <session-id> --agent <agent-id>

# Clear archive (only after task + receipt fully processed)
mailbox clear --session <session-id> --agent <agent-id>
```

### Health and Recovery

```bash
# Full health check (root, session, agent dirs, inbox, peek, status, identity — 8 checks)
mailbox-health --session <session-id> --agent <agent-id> --json

# Crash recovery: expired processing (>300s lease) → inbox
mailbox recover-stale --session <session-id> --agent <agent-id>
```

## Plugin / Runner Integration

The standalone CLI is the authoritative protocol boundary. A tmux/oh-my-pi plugin, opencode adapter, or another runner MAY invoke `mailbox peek` at safe boundaries for notification; the **plugin only notifies — never consumes**. The agent reads via `mailbox read`.

Requirements:
- Plugin must not depend on private runner hooks.
- Plugin must not run concurrently with a manual consumer.
- Plugin must use the same validation and atomic read→processing path.

## Error Handling

### Corrupt Messages

Parse failures, missing required fields, or `msg_id`/filename mismatch → moved to `_corrupt/` by `mailbox read`. On `mailbox stats` showing `corrupt > 0`:
1. Record the original filename.
2. Notify the sender via CLI to resend as a new message.
3. Keep the corrupt file for audit.
4. Never edit JSON and return to inbox.

### Syncthing Conflicts

`.sync-conflict-*` files are never valid messages. Compare with the original, identify the sender, request resend via CLI. Never rename conflict files to simulate delivery.

### Clock Skew

`created_at` and filename timestamps are diagnostic only. Process order by inbox mtime / actual arrival. On obvious skew, log `CLOCK_SKEW` and fix host time. Never rewrite sent timestamps.

### Missing Recipient

Sending fails if recipient not in roster or inbox directory missing. Stop and re-verify; never create directories for guessed IDs.

### Crash Recovery

`mailbox recover-stale` scans `processing/` and returns messages with leases expired (>300s) to inbox. Run on startup or when `mailbox stats` shows non-zero processing count. Never move files manually.

## Notification Reachability

| Agent type | Primary notification | Optional wake |
|---|---|---|
| Local Worker (shared tmux socket) | direct inbox | `tmux send-keys` with `MAILBOX_PENDING` |
| Remote SSH Worker | direct inbox + polling | none (no local tmux socket) |

`send-keys` is a convenience wake for local Workers only. It proves neither delivery nor reading. Only the mailbox file and subsequent status/REPORT prove progress.

## Prevention Rules

- Always use CLI for mailbox and status writes.
- Validate `--to` against roster before sending.
- Never overwrite or reuse sent messages; corrections via new message + `--reply-to`.
- Two-phase consumption always: `read` → process → `finalize`.
- Never use `capture-pane` or terminal echo to infer state.
- Never put large artifacts or sensitive full text in message body.
- Only clear archive after task and receipt processing is fully complete.
