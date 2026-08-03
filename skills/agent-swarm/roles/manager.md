# Manager Role

> This file is loaded ONLY when `$OMP_WORKER_ID == "manager"`. Protocol reference: `skill://agent-swarm/protocol/mailbox.md`

## 1. Scope

This skill **only orchestrates** — no domain research, no evidence judgment. Manager may read `workers.toml`, Worker artifacts, `.mailbox/<session>/<agent>/status.json`, and its own session inbox. Never use `capture-pane` to infer state. `status.json` is a current-state snapshot; formal conclusions are in Worker `REPORT` messages.

## 2. Self-Initialization

Before dispatching or waiting for any Worker, Manager MUST initialize its own identity and notification path:

1. Re-read `skill://agent-swarm` (the current protocol); do not rely on restored conversation context.
2. Set session identity:

   ```bash
   export OMP_SESSION_ID=<actual-session-id>
   export OMP_WORKER_ID=manager
   ```

   Replace `<actual-session-id>` with the real session ID; never leave a placeholder.
3. Check Manager inbox before declaring idle:

   ```bash
   mailbox peek --session <actual-session-id> --agent manager
   ```

4. If pending > 0, drain every pre-existing REPORT/NOTICE before declaring IDLE:

   ```bash
   mailbox read --session <actual-session-id> --agent manager --owner manager --json
   # verify report/artifact
   mailbox finalize --session <actual-session-id> --agent manager --msg-id <id> --owner manager
   # repeat until inbox empty
   ```

   A failed peek or unreadable inbox is a startup failure, not an idle state.
5. Write Manager's own status:

   ```bash
   mailbox status --session <actual-session-id> --agent manager \
     --state IDLE --current-task "waiting for REPORT" \
     --last-conclusion "manager initialized"
   ```

6. Start the configured mailbox plugin/watch or the documented polling loop.

## 3. Communication Model

| Direction | Primary path | Purpose |
|---|---|---|
| Manager→Worker | standalone `mailbox` direct inbox | formal INIT/TASK, supplementary materials, traceable instructions |
| Manager→Worker | `tmux send-keys` | post-INIT inbox check prompt or short steering; **never** carries formal task body |
| Worker→Manager | standalone `mailbox` direct inbox | REPORT, PROGRESS, QUESTION, NOTICE |
| Worker→Worker | standalone `mailbox` direct inbox | peer Q&A, evidence and review requests; Syncthing syncs directly |
| Worker→all observers | `.mailbox/<session>/<agent>/status.json` | IDLE/BUSY/DONE/BLOCKED, current task, last conclusion |

Notification reachability:
- **Local Worker** (shared tmux socket): direct inbox is authoritative; `send-keys MAILBOX_PENDING` is optional wake.
- **Remote SSH Worker**: no local tmux socket; never use `send-keys`. Mailbox + status.json polling is the complete path.
- `send-keys` success proves neither delivery nor reading; only mailbox files and subsequent status/REPORT prove progress.

## 4. INIT Handshake

Manager must complete INIT for each Worker before dispatching tasks.

### Target Scenarios

- **Fresh Worker** — no prior session context; proceed directly with the four-step INIT.
- **Restored Worker (`omp -c`)** — RESET prompt must precede formal INIT TASK. Send via `tmux send-keys` (or available runner channel):

  ```bash
  tmux send-keys -t <target> -l -- "RESET: forget ALL prior flat mailbox paths, dotai wrappers, relay/outbox/IPC logic, workers.toml assumptions, and mailbox-v2-* names. Re-read skill://agent-swarm for the CURRENT protocol. Use only standalone mailbox and session-based paths. Verify with ls .mailbox/<session-id>/<worker-id>/inbox/"
  tmux send-keys -t <target> C-m
  ```

  Remote Worker without local tmux: send via available runner/interactive channel. After confirmation or visible inbox verification, proceed with formal INIT.

- **Already-idle Worker** — if `.mailbox/<session-id>/<worker-id>/status.json` exists with `state=IDLE`, new INIT is **NO-OP**. Do not re-send INIT, do not require skill re-read. Just `mailbox peek` and wait for new TASK.

### Four-Step INIT Sequence

1. **Write formal INIT (a)**: `mailbox send` with `kind=TASK`, `subject=INIT` into target Worker inbox. Body must contain actual `session_id`, `worker_id`, role profile, artifact root, and compatibility requirements.

2. **Send check prompt (b)**: Immediately send a short prompt to the target pane. For local Workers with tmux:

   ```bash
   tmux send-keys -t <target> -l -- "Registration: write {session_id, worker_id} to the launcher-injected \$OMP_MAILBOX_IDENTITY_FILE path, then check inbox with mailbox read --session <session-id> --agent <worker-id> --owner <worker-id> --json. If restored with omp -c, first discard ALL prior state and re-read skill://agent-swarm."
   tmux send-keys -t <target> C-m
   ```

   Remote SSH Worker: send via available runner channel; if none, rely on shared inbox + Worker active polling.

3. **Worker confirms (c)**: Worker reads INIT (inbox→processing), validates identity, writes IDLE status, finalizes. Manager must NOT substitute pane text or send-keys echo for this status write.

4. **Verify handshake (d)**: Manager checks `.mailbox/<session-id>/<worker-id>/status.json` exists with `session_id` matching actual session, containing all five fields. Only after verification may Manager dispatch a formal TASK.

### Identity File

The Manager launcher must generate a unique per-process identity path:

```bash
TOKEN=$(date +%s)_$RANDOM
mkdir -p ~/.omp/mailbox-identity
OMP_MAILBOX_IDENTITY_FILE=~/.omp/mailbox-identity/${TOKEN}.json omp -c
```

The Worker writes its identity JSON to this injected `$OMP_MAILBOX_IDENTITY_FILE` during INIT.

## 5. Dispatch and Polling

1. After INIT handshake, verify recipient and target status from workers.toml and `.mailbox/<session>/session.json`. Do not guess IDs.
2. Dispatch with `mailbox send --session <session-id> --from manager --to <worker-id> --kind TASK`. Formal TASK requires completed INIT; subsequent local-only `send-keys` wake is optional.
3. Poll every 5 seconds: read target `status.json` and `mailbox stats` inbox count. When status transitions from `BUSY` to `DONE/BLOCKED`, drain Manager inbox to collect the corresponding `REPORT`.
4. After each message is read and processed, `mailbox finalize` to archive. Then `mailbox clear` — only after task and receipt are fully handled.
5. Status vs REPORT inconsistency: keep both, request Worker correction. Never silently overwrite.

Status interpretation for dispatch:
- **IDLE/DONE/BLOCKED** + prior REPORT handled → may dispatch new TASK
- **BUSY** → do not dispatch
- **STALE** (`updated_at` exceeds SLA) → diagnostic only, not IDLE; check inbox, Syncthing, pane liveness

## 6. Report Verification

Manager does NOT accept `status DONE` as proof of artifact validity. Must:
1. Read the final `REPORT` from inbox.
2. Verify referenced artifact paths, sizes, hashes.
3. Perform technical review.
4. Only then proceed to next TASK or archive.

Missing or stale status does not automatically mean BLOCKED. Check inbox and Worker reachability first.

## 7. Work Modes

Work mode is determined by TASK header `# Mode` / `# Mode-Role`. All collaboration info flows via v2 direct inbox. Reviewer/verifier mode must wait for target `status.json` terminal state AND receipt of REPORT/artifact before beginning review.

## 8. Re-initialization

If Worker fails to follow v2 protocol (hand-written JSON, no polling, no status maintenance):
1. Try lightweight `init --worker <id>` first.
2. Send a v2 mailbox verification TASK.
3. Verify: status `BUSY→DONE|BLOCKED`, Manager receives schema-valid REPORT, Worker inbox/archive behavior correct.
4. Only if lightweight fails: `/new` and restart. On recovery, check `_corrupt/`, archive, inbox, and status freshness first.

## 9. Error Handling

- **Corrupt message**: `mailbox read` moves to `_corrupt/`. Record filename, notify sender to resend. Never edit the original JSON.
- **Syncthing conflict**: `.sync-conflict-*` are never valid. Compare originals, request resend. Never rename to fake delivery.
- **Clock skew**: process by inbox mtime / actual arrival. Log `CLOCK_SKEW` on obvious skew. Never rewrite timestamps.
- **Stale status / pending inbox**: `updated_at` exceeds SLA → `STALE` diagnostic (not IDLE/BLOCKED). Check inbox count, Syncthing, pane liveness. Local can re-wake; remote must wait for next Worker poll.
- **Missing recipient**: sending fails → re-verify workers.toml, session.json, inbox path. Never create misspelled directories.
- **Crash recovery**: `mailbox recover-stale` returns expired processing (>300s lease) to inbox. Run on startup or when `stats` shows non-zero processing. Never move files manually.

## 10. Prevention Rules

- **Never hand-write JSON** — use `mailbox send` and `mailbox status` only.
- Validate `--to` against roster and target inbox owner before every send.
- Do not reuse, overwrite, or edit sent messages; corrections via new message + `--reply-to`.
- Do not use filename timestamps for business priority; do not use `capture-pane` to infer state.
- Do not put large artifacts in message body; body holds summaries and artifact references only.
- `mailbox clear` only clears archive, only after full task and receipt processing.
- Remote Worker visibility relies on status/inbox polling; `send-keys` success ≠ delivery proof.
