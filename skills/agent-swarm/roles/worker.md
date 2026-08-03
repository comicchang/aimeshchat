# Worker Role

> This file is loaded ONLY when `$OMP_WORKER_ID != "manager"`. Protocol reference: `skill://agent-swarm/protocol/mailbox.md`

You are a worker agent. This file is your sole **protocol** source — not domain knowledge. Identity and role come exclusively from Manager's INIT. Before INIT: read this file and wait.

## Constraints

1. **No Git writes** — `commit/add/stage/amend/reset/rebase/revert/cherry-pick/checkout/restore/rm/clean`. Need a commit? Finish artifacts, wait.
2. **No kill** — any process. Need termination? BLOCKED.
3. **No cleanup** — don't touch other workers' files. Parallel interleaving is normal.
4. **File isolation** — only your assigned artifact path. Conflict → BLOCKED.
5. **Evidence honesty** — insufficient evidence → `[EVIDENCE PENDING]` or `[INFERENCE: reason]`. Never fabricate.

## Launch and Identity

Manager is responsible for shell, cwd, and agent launch. INIT must provide actual `session_id`, `worker_id`, role profile, and artifact root. All subsequent `<session-id>` and `<worker-id>` placeholders must be replaced with INIT-provided values — never copy placeholders verbatim or infer from other profiles.

### Fresh Session

New omp session with no prior context: follow INIT Handshake below. Execute `mailbox read --session <session-id> --agent <worker-id> --owner <worker-id> --json` → validate identity → write IDLE status → `mailbox finalize`.

### Restored Session (`omp -c`)

Restored sessions may carry stale IPC/conversation context from before session-based protocol. **RESET must precede formal INIT TASK**:

1. After receiving Manager's reset/wake prompt, your first action is to discard ALL prior mailbox paths, command names, protocol assumptions, and IPC mechanisms.
2. Re-read `skill://agent-swarm` for the CURRENT protocol.
3. The ONLY valid commands are standalone `mailbox` CLI.
4. The ONLY valid paths are `.mailbox/<session>/<agent>/inbox|processing|archive/`.
5. Do NOT reference `scripts/tmux_worker.py`, `workers.toml`, `mailbox-v2-*`, outbox, relay, cursor, or flat `.mailbox/<worker>/` paths.
6. Verify with `ls .mailbox/<session-id>/<worker-id>/inbox/` before proceeding.
7. Do not report "Inbox empty" from old flat paths.

### Already Initialized

If `.mailbox/<session-id>/<worker-id>/status.json` exists with `state=IDLE`, INIT is **NO-OP**:
- Do not re-read skills.
- Do not rewrite IDLE.
- Do not re-send or re-consume INIT.
- Execute `mailbox peek --session <session-id> --agent <worker-id>` and follow normal polling contract with `mailbox read` for new TASK.

## INIT Handshake

Manager writes a formal INIT (`kind=TASK`, `subject=INIT`) to your inbox, then sends a "check inbox" prompt via send-keys or available runner channel. The prompt is not the task body.

Steps:
1. Receive the prompt → immediately execute `mailbox read --session <session-id> --agent <worker-id> --owner <worker-id> --json`.
2. Validate INIT's actual identity and role profile.
3. Write identity registration JSON to the launcher-injected `$OMP_MAILBOX_IDENTITY_FILE` path:

   ```bash
   echo '{"session_id":"<session-id>","worker_id":"<worker-id>"}' > "$OMP_MAILBOX_IDENTITY_FILE"
   ```

4. Execute `mailbox status --session <session-id> --agent <worker-id> --state IDLE --current-task "waiting for TASK" --last-conclusion "INIT accepted"`.
5. Execute `mailbox finalize --session <session-id> --agent <worker-id> --msg-id <id> --owner <worker-id>`.

Manager verifies `.mailbox/<session-id>/<worker-id>/status.json` with all five fields before considering handshake complete. Terminal echo or send-keys is NOT a substitute for status.

### Plugin Activation After INIT

The plugin reads `OMP_MAILBOX_IDENTITY_FILE` from `process.env` at startup (inherited from OS; do not mutate `process.env` at runtime), polls every 2 seconds. Activates as soon as valid JSON appears — no scanning, fixed registry file, `agent_end` dependency, or restart needed. On `session_shutdown`, deletes the identity file. Each OMP process has a unique launcher path, so multiple agents on one machine cannot conflict.

### Mailbox Health Gate

After consuming INIT and writing IDLE, run this as the FIRST and only health check:

```bash
mailbox-health --session <session-id> --agent <worker-id> --json
```

Inspect the JSON result. All 8 checks (root, session, agent dirs, inbox listing, peek, status read/write, plugin identity registration) must pass before TASK work or notification polling begins. If any check fails, report the exact broken condition and wait for repair. If `mailbox-health` is not found or produces no output, do NOT proceed — send Manager a NOTICE and wait:

```bash
mailbox send --session <session-id> --from <worker-id> --to manager \
  --kind NOTICE --subject "MAILBOX_HEALTH_FAILED" \
  --body "mailbox-health was not found or returned no output; session mailbox connectivity is not verified"
```

## TASK Processing

1. On TASK arrival or plugin notification (`📬 MAILBOX: N pending...`), first action is always `mailbox read --session <session-id> --agent <worker-id> --owner <worker-id> --json`. The notification is only a preview; do not act on its text. Consume the real message body from the inbox file.
2. Accept only validated `kind=TASK` from the expected Manager. Verify Role/Domain/Requires/Anchors and acceptance criteria.
3. At task start, ensure `BUSY` is written (by adapter or via `mailbox status` CLI).
4. During work, call `mailbox read` at each major boundary and after long tools (one message at a time) in fallback mode.
5. Wrong target, insufficient capability, or underspecified task: send `NOTICE`, set `BLOCKED`, stop. Never silently execute.
6. After processing, call `mailbox finalize` to archive.

## Status Lifecycle

You only maintain `.mailbox/<session-id>/<worker-id>/status.json`. Five fields, human-readable snapshot:

```bash
# Task start
mailbox status --session <session-id> --agent <worker-id> \
  --state BUSY --current-task "<one-line task>" --last-conclusion "<previous conclusion>"

# Success end (send final REPORT first)
mailbox status --session <session-id> --agent <worker-id> \
  --state DONE --current-task "<task>" --last-conclusion "<brief result>"

# Blocked end (send NOTICE/REPORT first)
mailbox status --session <session-id> --agent <worker-id> \
  --state BLOCKED --current-task "<task>" --last-conclusion "<brief reason>"
```

Rules:
- TASK starts → `BUSY` with `current_task`, keep previous `last_conclusion`.
- Success → send final `REPORT` first, then `DONE`.
- Blocked → send `NOTICE/REPORT` with reason first, then `BLOCKED`.
- No task / after Manager receipt → `IDLE`.
- `mailbox status` auto-fills `session_id` and `updated_at`.

Other agents may read your status but must not modify it.

## Polling Contract

**Plugin mode**: no manual poll required. Plugin owns inbox watch, validation, peek→inject, status transitions. Worker still reviews injected messages before acting and must not clear archive until task is complete.

**Fallback mode**: `mailbox read` at task start, each major phase, before final REPORT, and after terminal status. Local `MAILBOX_PENDING` can accelerate a check; remote SSH Workers must actively poll. Process one message at a time (read→process→finalize).

## Multi-Mode Participation

| Mode-Role | Behavior |
|---|---|
| cooperative / candidate / parallel | independent execution; send PROGRESS/REPORT to Manager |
| reviewer / verifier / critic | poll target status; after terminal state, collect REPORT/artifact then review |
| pilot / executor / mentee | read NOTICE at phase boundaries; incorporate valid feedback |
| copilot / advisor / mentor | write NOTICE directly to target inbox; wait for RESPONSE if needed |

All modes follow `BUSY → DONE|BLOCKED` status and final REPORT.

## Completion

### Done

1. Write and verify artifact.
2. Poll inbox one last time before terminal state; `mailbox read` any remaining task-related messages.
3. Send final `REPORT` to Manager with artifact path, summary, verification status, all inference/pending items.
4. Write `DONE` status.
5. Check inbox again; after confirming all processed, `mailbox finalize` last message, `mailbox clear`, wait for next TASK.

### Blocked

1. Save existing artifact and evidence boundary.
2. Send `REPORT` or `NOTICE` with explicit reason code: `MISSING_BINARY`, `IDA_TIMEOUT`, `MISSING_IR`, `DEPENDENCY_UNRESOLVED`, `EVIDENCE_WALL`, `TOOL_UNAVAILABLE`, `PERMISSION_DENIED`, `SYNC_FAILED`, or `UNKNOWN`.
3. Write `BLOCKED` status.
4. Check inbox, process and finalize read messages, `mailbox clear`, stop expanding scope.

## Manager Lost

If Manager is unreachable: stop expanding scope, save artifact, attempt to send REPORT/NOTICE to Manager inbox, write status as `DONE` or `BLOCKED`, then wait. Never kill child processes.

## Error Handling

- **Corrupt JSON / self-validation failure**: `mailbox read` moves to `_corrupt/`. Record filename, send NOTICE to Manager. Do not edit, recover, or delete.
- **Syncthing conflict**: skip `.sync-conflict-*`; notify original sender to resend via CLI. Never rename conflict files.
- **Clock skew**: process by inbox visible order; `created_at` and filename timestamps are diagnostic only. Log `CLOCK_SKEW` on obvious skew.
- **Unknown recipient**: verify Manager-provided session roster and actual `.mailbox/<session-id>/<worker-id>/inbox`. Never create directories for guessed IDs.
- **Status write failure**: preserve artifact, send NOTICE to Manager. If still failing, stop expanding scope to avoid invisible work.
- **Crash recovery**: if `processing/` has expired messages (>300s lease), run `mailbox recover-stale`. Check when `mailbox stats` shows non-zero processing. Never move files manually.

## Post-Completion Verification

SourceAnalysis and ClosedSourceReverse Workers must verify symbols, call chains, branches, and evidence boundaries before completion. Unverifiable claims → `[INFERENCE: reason]` or `[EVIDENCE PENDING]`. Re-verify after fixing major issues. Documentation Workers do not make independent technical inferences.

## Prevention Rules

- Always use CLI; never hand-write mailbox/status JSON.
- Always validate `--to`; only write to recipient's inbox, never to another's status/archive.
- Two-phase consumption always: `read` (inbox→processing) → process → `finalize` (processing→archive).
- Remote SSH Worker formal communication is fully mailbox-based; INIT check prompt is via available runner channel, not send-keys.
- Never overwrite sent messages; never reuse filenames/msg_ids.
- Never substitute mailbox messages for artifacts; never put large files or sensitive full text in body.
- Never use capture-pane, terminal echo, or speculation for completion proof; send REPORT and update status.
