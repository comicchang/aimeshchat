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

8 required fields, 5 optional correlation fields (kind-conditional):

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
| `reply_to` | REQUIRED for REPORT, QUESTION, RESPONSE, TASK/INIT if responding to a prior message | references another msg_id |
| `run_id` | REQUIRED for TASK, INIT, REPORT | run identifier (immutable once assigned) |
| `request_id` | REQUIRED for TASK, INIT, REPORT | request identifier (immutable once assigned) |
| `trace_id` | no | distributed trace correlation ID |
| `causation_id` | no | parent msg_id that caused this message |

> **⚠ [DESIGN ONLY — PERMISSIVE EDGE CASE]**: `validate_message` enforces kind-conditional required fields for TASK/INIT/REPORT (kIND_CONDITIONAL_REQUIRED) at both the protocol layer and CLI (swarm direct exits 1 for missing --run-id). However, `reply_to` is NOT enforced for QUESTION/RESPONSE, and `attachments` is NOT enforced for REPORT/EVIDENCE. These edge cases remain permissive until protocol layer is updated. See `codeagent/mailbox/protocol.py:20-25`.
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

## Request Lifecycle

Every TASK/INIT message defines a request lifecycle. The lifecycle is scoped to `(session_id, request_id, run_id)` and governed by the following rules.

### Identity Fields

- `request_id` + `run_id` are **REQUIRED** on every TASK/INIT and on every REPORT that concludes the request.
- Both are **immutable** once assigned by the Manager at dispatch time. Workers MUST copy them verbatim into all correlated messages (REPORT, PROGRESS, EVIDENCE, QUESTION).
- A Worker that loses or mutates `request_id`/`run_id` produces an orphaned message. The Manager MUST reject it.

### Terminal State Machine

Per-request states progress monotonically:

```
DISPATCHED → ACKED → RUNNING → DONE
                      ↓
                      BLOCKED
                      ↓→ CANCELLED
                      ↓→ EXPIRED
```

Note: DONE, BLOCKED, CANCELLED, and EXPIRED are **all** terminal states. A request reaches exactly one of them (see Terminal Exclusivity below). The diagram does not imply transitions between terminal states — CANCELLED and EXPIRED branch independently from RUNNING, just like DONE and BLOCKED.

| State | Author | Meaning |
|---|---|---|
| `DISPATCHED` | Manager | TASK/INIT written to inbox |
| `ACKED` | Worker | Worker read the TASK (mailbox read claim) |
| `RUNNING` | Worker | Worker actively executing (set via PROGRESS) |
| `DONE` | Worker | Terminal — task completed successfully |
| `BLOCKED` | Worker | Terminal — cannot proceed (includes reason) |
| `CANCELLED` | Manager | Terminal — Manager rescinds the request |
| `EXPIRED` | Manager | Terminal — SLA exceeded, Manager declares stale |
| `UNKNOWN/STALE` | Manager | Terminal — watchdog fired, no terminal within SLA |

### Terminal Exclusivity (CAS)

A request may reach exactly **ONE** terminal state. The first terminal write wins; all subsequent attempts are **PROTOCOL_CONFLICT**.

```bash
# Manager or Worker writes terminal state:
# - If no prior terminal exists for (session_id, request_id, run_id) → accepted
# - If a terminal already exists → REJECTED with PROTOCOL_CONFLICT
```

Implementation: terminal writes MUST be compare-and-swap (CAS) — check that no terminal record exists before writing. If a second terminal arrives, the request is **frozen**: no further dispatch or state change is permitted.

### ACK_ONLY Watchdog

If a request reaches ACKED but no terminal state (DONE/BLOCKED/CANCELLED/EXPIRED) appears within the configured SLA:

1. Manager marks the request `UNKNOWN/STALE`.
2. Manager MUST trigger one of: retry (new run_id), recover (check-in with Worker), or escalate (alert operator).
3. The stale Worker's status.json remains `BUSY` until it self-corrects or the session terminates.

The SLA is a deployment-time configuration. The watchdog reads the request event ledger, not status.json.

---

## Artifact Attachment

REPORT and EVIDENCE messages that claim task completion or provide evidence MUST include an `attachments` array. Each element is an `AttachmentRef`.

### AttachmentRef Schema

| Field | Required | Description |
|---|---|---|
| `artifact_id` | yes | content-addressed identifier (`<session>/<request>/<run>/<agent>/<sha256>`) |
| `source_host` | yes | hostname where the artifact was produced |
| `remote_root` | yes | artifact root path on the source host |
| `relative_path` | yes | path relative to the artifact root |
| `size` | yes | byte count (decimal integer) |
| `sha256` | yes | SHA-256 hex digest of the artifact content |
| `media_type` | yes | MIME type (e.g. `application/json`, `text/plain`, `image/png`; default `application/octet-stream`) |

> **Note**: `request_id` is a message-level correlation field (on the REPORT envelope), not part of the attachment itself.

### Manager Verification

Before reducing a request to DONE, the Manager MUST:

1. Fetch each artifact via `codeagent artifact pull --host <source_host> --artifact-id <artifact_id> --remote-root <remote_root> --relative-path <relative_path> --size <size> --sha256 <sha256> --dest <dest>` (the real cross-host transport).
2. Verify `size` matches the actual byte count.
3. Verify `sha256` matches the actual content digest.
4. If either check fails → reject the REPORT; request remains RUNNING.

### Content-Addressed Namespace

Artifact identity is the tuple `(session, request, run, agent, sha256)`. Two artifacts with the same content hash but different source contexts are distinct entries. The namespace prevents ambiguity when multiple Workers produce files with the same relative path.

### ARTIFACT_CONFLICT

If a REPORT references a `relative_path` that already exists for the same `(session_id, request_id)` but with a **different** `sha256`:

- The existing artifact is **immutable**. Overwrite is **REJECTED**.
- The conflicting REPORT MUST be treated as a protocol violation.
- Resolution: the Worker MUST use a distinct `relative_path` or re-derive the content with a new hash.

---

## status.json Contract

> **⚠ AVAILABILITY SNAPSHOT ONLY**
>
> `status.json` is a 5-field availability diagnostic. It tells the Manager whether a Worker is IDLE/BUSY/DONE/BLOCKED **right now**.
>
> **It is NOT a task terminal state ledger.** Terminal states (DONE, BLOCKED, CANCELLED, EXPIRED, UNKNOWN/STALE) are authoritative only in the **request event ledger** — the sequence of REPORT/NOTICE messages with `request_id` + `run_id`.
>
> A Worker may self-report `DONE` in status.json before the Manager has verified artifacts and accepted the REPORT. Conversely, status.json may lag behind the event ledger if the Worker crashes after writing the REPORT but before updating status.
>
> **Manager logic MUST derive request outcomes from the mailbox event stream, not from status.json.**

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

**Hard boundary — plugin MUST NOT:**
- Call `mailbox read` or `mailbox finalize` or `mailbox release` — those are agent-only operations.
- Write `status.json` or any business-logic status. Status is owned by the agent process.
- Infer request lifecycle state from status.json or terminal output. State truth lives in the mailbox event ledger.
- Mutate, reorder, or replay messages. The mailbox is an append-only log.

**Plugin MAY:**
- Call `mailbox peek` to discover pending count and notify the agent.
- Call `mailbox stats` for dashboard/health display.
- Invoke `send-keys` with `MAILBOX_PENDING` as a convenience wake for local Workers.

Requirements:
- Plugin must not depend on private runner hooks.
- Plugin must not run concurrently with a manual consumer.
- Plugin must use only validate non-consumption properties (msg_id, kind, from).

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
