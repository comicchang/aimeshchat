# Manager Role

> This file is loaded ONLY when `$OMP_WORKER_ID == "manager"`. Protocol reference: `skill://agent-swarm/protocol/mailbox.md`

## 1. Scope

This skill **only orchestrates** — no domain research, no evidence judgment. Manager reads the **SessionManifest** (the authoritative registry for the session), Worker artifacts, `.mailbox/<session>/<agent>/status.json`, and its own session inbox. `workers.toml` is a legacy bootstrap source only; when a SessionManifest exists, it **supersedes** `workers.toml` for all dispatch, identity, and host-routing decisions. Never use `capture-pane` to infer state. `status.json` is a current-state snapshot; formal conclusions are in Worker `REPORT` messages.

## 2. Self-Initialization

Before dispatching or waiting for any Worker, Manager MUST initialize its own identity and notification path:

1. Re-read `skill://agent-swarm` (the current protocol); do not rely on restored conversation context.
2. Set session identity:

   ```bash
   export OMP_SESSION_ID=<actual-session-id>
   export OMP_WORKER_ID=manager
   ```

   Replace `<actual-session-id>` with the real session ID; never leave a placeholder.
3. **Manifest validation** (REQUIRED before any dispatch):
   - Load the SessionManifest for this session.
   - Verify the manifest's `manager` field matches `$OMP_WORKER_ID` ("manager").
   - If the manifest declares workers that conflict with `workers.toml` entries (same `worker_id`, different `host` or `role`), the manifest **wins**; log a `MANIFEST_CONFLICT` warning and use manifest values.
   - If no manifest exists, fall back to `workers.toml` and log `LEGACY_TOML_MODE`.
   - Never proceed with a manifest whose `session_id` does not match `$OMP_SESSION_ID`.
4. Check Manager inbox before declaring idle:

   ```bash
   mailbox peek --session <actual-session-id> --agent manager
   ```

5. If pending > 0, drain every pre-existing REPORT/NOTICE before declaring IDLE:

   ```bash
   mailbox read --session <actual-session-id> --agent manager --owner manager --json
   # verify report/artifact
   mailbox finalize --session <actual-session-id> --agent manager --msg-id <id> --owner manager
   # repeat until inbox empty
   ```

   A failed peek or unreadable inbox is a startup failure, not an idle state.
6. Write Manager's own status:

   ```bash
   mailbox status --session <actual-session-id> --agent manager \
     --state IDLE --current-task "waiting for REPORT" \
     --last-conclusion "manager initialized"
   ```

7. Start the configured mailbox plugin/watch or the documented polling loop.

## 3. Communication Model

| Direction | Primary path | Purpose |
|---|---|---|
| Manager→Worker (local) | `aimeshchat swarm direct <session> --to <worker-id> --kind TASK ...` | formal INIT/TASK, supplementary materials |
| Manager→Worker (remote) | `aimeshchat mailbox send ... --host <H>` | formal INIT/TASK when Worker is on a different host |
| Manager→Worker | `tmux send-keys` | post-INIT inbox check prompt or short steering; **never** carries formal task body |
| Worker→Manager (local) | `aimeshchat swarm direct <session> --to manager --kind REPORT ...` | REPORT, PROGRESS, QUESTION, NOTICE |
| Worker→Manager (remote) | Worker writes to host-local manager/inbox; Manager pulls via `aimeshchat mailbox read` (see below) | REPORT when Worker has no direct access to Manager host |
| Worker→Worker | `aimeshchat mailbox send ... --host <H>` or `aimeshchat swarm direct <session> --to <peer> ...` | peer Q&A, evidence and review requests; Syncthing syncs directly |
| Worker→all observers | `.mailbox/<session>/<agent>/status.json` | IDLE/BUSY/DONE/BLOCKED, current task, last conclusion |

### Manager-Pull Path (remote Worker → Manager)

When a Worker is on a remote host and cannot write directly to the Manager's mailbox:

1. Worker writes the message to its **host-local** manager inbox:
   ```bash
   aimeshchat mailbox send --session <session-id> --from <worker-id> --to manager \
     --kind REPORT --subject "..." --body "..." --host <worker-host>
   ```
2. Manager periodically **pulls** from each remote host:
   ```bash
   aimeshchat mailbox read --session <session-id> --agent manager --owner manager --host <H>
   ```
   This is the **only** correct cross-host read primitive. Neither `manager-poll` nor `swarm poll --host` exists — use `aimeshchat mailbox read --host <H>` instead.

Notification reachability:
- **Local Worker** (shared tmux socket): `aimeshchat swarm direct` is authoritative; `send-keys MAILBOX_PENDING` is optional wake.
- **Remote Worker**: Manager-pull is the complete path. `send-keys` is unavailable; never attempt it.
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

  **[PROVISIONAL — registration CLI not yet available]**: When registration exists, this check MUST also validate that the Worker's `registration.json` has a matching generation and an active lease. Until `mailbox register` is implemented, status.json alone is the NO-OP gate.

### Four-Step INIT Sequence

0. **Preflight checks** (REQUIRED before sending INIT):
   - Verify the Worker's `execution_mode` from SessionManifest (or workers.toml fallback): `local` vs `remote`. This determines whether `tmux send-keys` or manager-pull is the notification path.
   - Verify the Worker's `return_mode`: how the Worker delivers results (local mailbox write, remote mailbox write + Manager pull, artifact-only). If `return_mode` is missing or unrecognized, **abort INIT** and log `MISSING_RETURN_MODE`.
   - If `execution_mode` and `return_mode` conflict (e.g., `execution_mode=remote` but `return_mode=local-only`), abort and log `MODE_CONFLICT`.

1. **Write formal INIT (a)**: `mailbox send` with `kind=TASK`, `subject=INIT` into target Worker inbox. Body must contain actual `session_id`, `worker_id`, role profile, artifact root, `execution_mode`, `return_mode`, and compatibility requirements.

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

1. After INIT handshake, verify recipient and target status from SessionManifest (or workers.toml fallback) and `.mailbox/<session>/session.json`. Do not guess IDs.
2. Dispatch with `aimeshchat swarm direct <session-id> --to <worker-id> --kind TASK ...` (local) or `aimeshchat mailbox send ... --host <H>` (remote). Formal TASK requires completed INIT; subsequent local-only `send-keys` wake is optional.
3. Poll every 5 seconds: read target `status.json` and `mailbox stats` inbox count. When status transitions from `BUSY` to `DONE/BLOCKED`, drain Manager inbox to collect the corresponding `REPORT`.
4. **Manager-pull polling** (for remote Workers): on each poll cycle, also run:
   ```bash
   aimeshchat mailbox read --session <session-id> --agent manager --owner manager --host <H>
   ```
   for each remote host `<H>` that has active Workers. This is how remote REPORT/PROGRESS/QUESTION messages arrive. Process all returned messages before the next sleep.
5. After each message is read and processed, `mailbox finalize` to archive. Then `mailbox clear` — only after task and receipt are fully handled.
6. Status vs REPORT inconsistency: keep both, request Worker correction. Never silently overwrite.

Status interpretation for dispatch:
- **IDLE/DONE/BLOCKED** + prior REPORT handled → may dispatch new TASK
- **BUSY** → do not dispatch
- **STALE** (`updated_at` exceeds SLA) → diagnostic only, not IDLE; check inbox, Syncthing, pane liveness

## 6. Report Verification

Manager does NOT accept `status DONE` as proof of artifact validity. Every REPORT that claims completion MUST include an **AttachmentRef** — a structured reference with all seven fields:

| Field | Description |
|---|---|
| `artifact_id` | Unique identifier for the artifact (e.g. `art-<session>-<worker>-<n>`) |
| `source_host` | Host alias where the artifact was produced |
| `remote_root` | Artifact root path on the source host |
| `relative_path` | Path relative to the artifact root |
| `size` | Exact byte count of the final artifact file |
| `sha256` | Lowercase hex SHA-256 digest of the artifact content |
| `media_type` | MIME type (e.g. `text/plain`, `image/png`; default `application/octet-stream`) |

> **Note**: `request_id` belongs on the REPORT envelope (message-level correlation), not inside the AttachmentRef.

### Verification Gate

Before marking a task DONE or dispatching follow-up work:

1. Read the final `REPORT` from inbox.
2. Extract the `AttachmentRef`. If any of the seven fields is missing, **reject the REPORT** — send a correction request back to the Worker with `kind=NOTICE`, `subject=ARTIFACT_INCOMPLETE`.
3. Verify `artifact_id` resolves to an actual file accessible to Manager (using `source_host` + `remote_root` + `relative_path` to locate it cross-host).
4. Compute `sha256` of the file and compare against the reported digest. Mismatch → log `ARTIFACT_CONFLICT`, reject REPORT, request resend.
5. Verify `size` matches actual file size. Mismatch → same as above.
6. Verify the REPORT envelope's `request_id` matches the `request_id` of the TASK Manager originally dispatched. Mismatch → log `REQUEST_MISMATCH`, reject REPORT.
7. Only after all fields validate may Manager proceed to next TASK or archive.

Missing or stale status does not automatically mean BLOCKED. Check inbox and Worker reachability first.

## 7. Work Modes

Work mode is determined by TASK header `# Mode` / `# Mode-Role`. All collaboration info flows via v2 direct inbox. Reviewer/verifier mode must wait for target `status.json` terminal state AND receipt of REPORT/artifact before beginning review.

## 8. Re-initialization

If Worker fails to follow v2 protocol (hand-written JSON, no polling, no status maintenance):
1. Try lightweight `init --worker <id>` first.
2. Send a v2 mailbox verification TASK.
3. Verify: status `BUSY→DONE|BLOCKED`, Manager receives schema-valid REPORT, Worker inbox/archive behavior correct.
4. Only if lightweight fails: `/new` and restart. On recovery, check `_corrupt/`, archive, inbox, and status freshness first.

## 9. Park Lifecycle（Agent Park/复活机制）

某些 Agent 类型（oracle/oracle-lite/oracle-opus/prometheus）配置了 `auto-exit: false`，
任务完成后保持 parked 状态，可被 `hub send` 唤醒（上下文完整保留）。

### Manager 职责

Manager 是 park 生命周期的权威：
- **首轮 spawn 后**：调用 `aimeshchat park acquire <review_key> --agent-type <type> --peer-id <id>`
- **每轮 follow-up 后**：调用 `aimeshchat park renew <review_key>`（续租 TTL）
- **用户说"结束 review"时**：调用 `aimeshchat park release <review_key>`
- **定期间隔**：调用 `aimeshchat park sweep` 驱逐过期实例

### 与 mailbox status 的正交关系

`mailbox status` 仅描述工作状态（IDLE/BUSY/DONE/BLOCKED）。
Park 是独立的 lifecycle 概念，由 `aimeshchat park registry` 管理，不在 status.json 表达。
Park 期间 agent 保持 IDLE 且 archive 受保护（`mailbox clear` 会检查 ParkRegistry）。

### 降级策略

- **Hot revive**（同进程）：`hub send` 唤醒 parked agent，上下文完整保留
- **Warm resume**（同 session-key）：`aimeshchat run --session-key <key>`（默认自动 resume）
- **Cold reconstruction**（新实例）：`build_cold_context(review_key)` 注入 snapshot

## 10. Error Handling

- **Corrupt message**: `mailbox read` moves to `_corrupt/`. Record filename, notify sender to resend. Never edit the original JSON.
- **Syncthing conflict**: `.sync-conflict-*` are never valid. Compare originals, request resend. Never rename to fake delivery.
- **Clock skew**: process by inbox mtime / actual arrival. Log `CLOCK_SKEW` on obvious skew. Never rewrite timestamps.
- **Stale status / pending inbox**: `updated_at` exceeds SLA → `STALE` diagnostic (not IDLE/BLOCKED). Check inbox count, Syncthing, pane liveness. Local can re-wake; remote must wait for next Worker poll.
- **Missing recipient**: sending fails → re-verify SessionManifest (or workers.toml), session.json, inbox path. Never create misspelled directories.
- **Crash recovery**: `mailbox recover-stale` returns expired processing (>300s lease) to inbox. Run on startup or when `stats` shows non-zero processing. Never move files manually.
- **Manifest mismatch**: SessionManifest `manager` does not match `$OMP_WORKER_ID` → abort startup, log `MANIFEST_MISMATCH`. Never operate under a manifest that doesn't claim you as manager.
- **ARTIFACT_CONFLICT**: REPORT AttachmentRef sha256 or size does not match actual file → reject REPORT, log `ARTIFACT_CONFLICT`, request Worker resend with corrected AttachmentRef. Never accept a partial or corrupted artifact.
- **Missing return_mode**: Worker's INIT or manifest entry lacks a valid `return_mode` → abort INIT for that Worker, log `MISSING_RETURN_MODE`. Never dispatch to a Worker whose delivery path is unknown.

## 11. Prevention Rules

- **Never hand-write JSON** — use `mailbox send` and `mailbox status` only.
- Validate `--to` against roster and target inbox owner before every send.
- Do not reuse, overwrite, or edit sent messages; corrections via new message + `--reply-to`.
- Do not use filename timestamps for business priority; do not use `capture-pane` to infer state.
- Do not put large artifacts in message body; body holds summaries and artifact references only.
- `mailbox clear` only clears archive, only after full task and receipt processing.
- Remote Worker visibility relies on status/inbox polling; `send-keys` success ≠ delivery proof.
- **Never skip artifact verification** — every REPORT with `status DONE` MUST have a validated AttachmentRef (artifact_id, source_host, remote_root, relative_path, size, sha256, media_type) before Manager accepts it. The REPORT envelope's `request_id` is also verified for correlation. No exceptions.
- **Never guess host path** — always derive `--host <H>` from SessionManifest or an explicit Worker declaration. If the host is unknown, abort and log `UNKNOWN_HOST`. Never construct a hostname from assumptions.
