# Worker Role

> This file is loaded ONLY when `$OMP_WORKER_ID != "manager"`. Protocol reference: `skill://agent-swarm/protocol/mailbox.md`

You are a worker agent. This file is your sole **protocol** source — not domain knowledge. Identity and role come exclusively from Manager's INIT. Before INIT: read this file and wait.

## Constraints

1. **No Git writes** — `commit/add/stage/amend/reset/rebase/revert/cherry-pick/checkout/restore/rm/clean`. Need a commit? Finish artifacts, wait.
2. **No kill** — any process. Need termination? BLOCKED.
3. **No cleanup** — don't touch other workers' files. Parallel interleaving is normal.
4. **File isolation** — only your assigned artifact path. Conflict → BLOCKED.
5. **Evidence honesty** — insufficient evidence → `[EVIDENCE PENDING]` or `[INFERENCE: reason]`. Never fabricate.

## Channel Decision（最先执行，防双收件箱错配）

收到任何 "check inbox"/wake 提示后，**先判定你是哪种 worker**，再决定通道。把
mailbox（mailbox CLI）与 OMP hub inbox（进程内 agent 消息）混用 = 领不到任务、空转。

```text
$OMP_MAILBOX_IDENTITY_FILE 存在 → 你是 mailbox worker：唯一通道 = mailbox CLI
                                  （mailbox read → process → finalize）
不存在                          → 你是 OMP task worker：唯一通道 = hub inbox
```

- 收到 "📬 MAILBOX: N pending..." 通知 → **一律走 `mailbox read`**（mailbox CLI），
  与 hub inbox 无关。不要用 `hub inbox` 领 mailbox 投递的任务（会返回空）。
- OMP `hub inbox` 是进程内 agent 消息箱，不是 mailbox——两者不可互替。
- 永远用 mailbox CLI 读/领任务；**手动 read inbox JSON 文件不产生 consumption ACK**，
  TASK 会持续悬挂（oracle-init 一直挂着），必须 `mailbox read → finalize` 才算消费。

## Launch and Identity

Manager is responsible for shell, cwd, and agent launch. INIT must provide actual `session_id`, `worker_id`, role profile, and artifact root. All subsequent `<session-id>` and `<worker-id>` placeholders must be replaced with INIT-provided values — never copy placeholders verbatim or infer from other profiles.

### Fresh Session

New omp session with no prior context: follow INIT Handshake below. Execute `mailbox read --session <session-id> --agent <worker-id> --owner <worker-id> --json` → validate identity → write IDLE status → `mailbox finalize`.

### Restored Session (`omp -c`)

Restored sessions may carry stale IPC/conversation context from before session-based protocol. **RESET must precede formal INIT TASK**:

1. After receiving Manager's reset/wake prompt, your first action is to discard ALL prior mailbox paths, command names, protocol assumptions, and IPC mechanisms.
2. Re-read `skill://agent-swarm` for the CURRENT protocol.
3. The ONLY valid commands are standalone `mailbox` CLI.
4. The ONLY valid paths are under the mailbox root. **MAILBOX_ROOT 澄清**：root 由
   `store.resolve_root()` 决定（源码）——`$MAILBOX_ROOT` 环境变量优先；未设则
   `$XDG_DATA_HOME/aimeshchat/mailbox`（`XDG_DATA_HOME` 默认 `~/.local/share`，
   即 `~/.local/share/aimeshchat/mailbox`）。**不要假设 root 在 cwd 的 `.mailbox/`**；
   `.mailbox/` 不存在不代表 mailbox 不可用，先查 `~/.local/share/aimeshchat/mailbox`
   与 `$MAILBOX_ROOT`。本文件其余 `.mailbox/<session>/<agent>/` 硬编码路径均为
   **同机 Mode A 特例**（Manager 显式以 `.mailbox` 为 root 时）；root 不同则路径前缀
   相应替换。
5. Do NOT reference `scripts/tmux_worker.py`, `workers.toml`, `mailbox-v2-*`, outbox, relay, cursor, or flat `.mailbox/<worker>/` paths.
6. Verify with `ls .mailbox/<session-id>/<worker-id>/inbox/` before proceeding.
7. Do not report "Inbox empty" from old flat paths.

### Already Initialized

If `.mailbox/<session-id>/<worker-id>/status.json` exists with `state=IDLE`, INIT is **NO-OP**:
- Do not re-read skills.
- Do not rewrite IDLE.
- Do not re-send or re-consume INIT.
- Execute `mailbox peek --session <session-id> --agent <worker-id>` and follow normal polling contract with `mailbox read` for new TASK.

> **Note**: When the registration CLI becomes available, this check will also require a valid `registration.json` with matching generation and active lease. Until then, status.json alone is the NO-OP gate.

## Worker Registration

> **[DESIGN ONLY — requires mailbox registration CLI not yet implemented]**
>
> The registration lifecycle below is specified but cannot be executed today.
> The `mailbox` CLI has no `register`/`registration` subcommand.
> Until a `mailbox register` command exists, Workers skip registration and
> rely on the INIT handshake + status.json alone. The fields and validity
> rules below describe the target protocol; they are NOT enforceable without CLI support.

Before the INIT handshake, the Worker MUST write a registration record so the Manager and plugin can validate its identity and liveness. When the registration CLI becomes available:

```bash
mailbox register --session <session-id> --worker <worker-id> \
  --generation <init-generation> --nonce <random-hex> \
  --host-alias <hostname-alias> --backend <omp|ssh|tmux> \
  --execution-mode <mode from INIT>
```

| Field | Description |
|---|---|
| `generation` | Monotonic counter from INIT. Each Manager restart increments generation. |
| `nonce` | Random 16-hex-char value generated at Worker launch; prevents stale registration replay. |
| `host_alias` | Short hostname identifying this machine in the swarm. |
| `backend` | Transport the Worker was launched with. |
| `execution_mode` | The mode assigned by INIT (e.g. `cooperative`, `reviewer`, `pilot`). |
| `last_seen` | ISO-8601 timestamp; Worker updates this on each heartbeat or status write. |

**Validity rule**: An already-idle Worker (status `IDLE` already present) is considered valid only if its registration `generation` matches the current INIT generation AND its `last_seen` is within the active lease window (default 120s). Stale or mismatched-generation registrations MUST be treated as uninitialized — the Worker must re-register before accepting tasks.

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

## ACK Semantics

There are two distinct acknowledgments in the mailbox protocol. Workers MUST NOT conflate them.

| ACK type | What it means | Who sends it | When |
|---|---|---|---|
| **Delivery ACK** | The transport layer received and persisted the message to the target inbox file. | `mailbox send` / transport adapter | Immediately on successful file write — no business logic involved. |
| **Task Consumption ACK** | The Worker read, validated, and finalized the TASK message (inbox → processing → archive). | Worker via `mailbox finalize` | After the Worker has processed the message content and produced a result. |

**REPORT correlation**: Every REPORT and EVIDENCE message MUST include the `request_id` field set to the originating TASK's `msg_id`. This lets the Manager correlate which TASK produced which result, even when multiple TASKs are in flight. A REPORT without `request_id` is a protocol violation.

**Delivery ACK ≠ completion**: Receiving a delivery ACK for a REPORT only means the Manager's inbox file was written. The Worker MUST NOT consider its task complete until it has also:
2. Written DONE or BLOCKED status.
3. Verified the Manager's consumption (via `mailbox peek` or next-read confirming the REPORT was finalized on the Manager side), OR waited for the lease timeout to expire.

   **Manager-pull exception**: In manager-pull mode, the Worker cannot SSH to the Manager host to verify consumption. The Worker MUST wait for the lease timeout (or next Manager pull receipt) instead of attempting cross-host `mailbox peek`. Writing DONE/BLOCKED status is sufficient; Manager will pull the REPORT on its next poll cycle.

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

**Plugin mode**: no manual poll required. Plugin ONLY watches the inbox filesystem, peeks at new message filenames, and notifies the Worker process (via `process.send` or injected IPC). Plugin MUST NOT call `mailbox read`, `mailbox finalize`, `mailbox release`, or write business-level status. Worker is the sole consumer: `read` (inbox→processing) → validate → process → `finalize` (processing→archive) → write status. Plugin notification is a hint, not a delivery guarantee — Worker still reviews the actual message body before acting and must not clear archive until task is complete.

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

1. Write artifact and verify it (size + SHA256 — see Artifact Verification below).
2. Poll inbox one last time before terminal state; `mailbox read` any remaining task-related messages.
3. Send final `REPORT` to Manager. The REPORT envelope MUST carry `attachments` as a top-level `--attachment` CLI argument (one per attachment) — **no bare paths in the body**. Each AttachmentRef carries:

   | Field | Description |
   |---|---|
   | `artifact_id` | Unique artifact identifier (e.g. `art-<session>-<worker>-<n>`) |
   | `source_host` | Host alias where the artifact was produced |
   | `remote_root` | Artifact root path on the source host |
   | `relative_path` | Path relative to the artifact root |
   | `size` | Exact byte count of the final artifact file |
   | `sha256` | Lowercase hex SHA256 digest of the final artifact file |
   | `media_type` | MIME type (e.g. `text/plain`, `image/png`; default `application/octet-stream`) |

   The REPORT envelope MUST carry `request_id` set to the originating TASK's `msg_id` for correlation.

   EVIDENCE messages that reference files MUST also use AttachmentRef. Empty `attachments` is valid only when the task produced no file artifact.

4. Write `DONE` status.
5. Check inbox again; after confirming all processed, `mailbox finalize` last message, `mailbox clear`, wait for next TASK.

#### Artifact Verification

Before sending REPORT, the Worker MUST verify every attachment:

```bash
# size check
actual_size=$(stat -f%z "$artifact_path")   # macOS; use stat -c%s on Linux
[ "$actual_size" = "$expected_size" ] || { echo "SIZE_MISMATCH"; exit 1; }

# SHA256 check
actual_hash=$(shasum -a 256 "$artifact_path" | cut -d' ' -f1)
[ "$actual_hash" = "$expected_sha256" ] || { echo "HASH_MISMATCH"; exit 1; }
```

If verification fails, do NOT send REPORT. Write BLOCKED with reason `MISSING_ARTIFACT` and notify Manager. Hook into the request lifecycle: verification is part of task completion, not a post-hoc check.

### Blocked

1. Save existing artifact and evidence boundary.
2. Send `REPORT` or `NOTICE` with explicit reason code: `MISSING_BINARY`, `IDA_TIMEOUT`, `MISSING_IR`, `DEPENDENCY_UNRESOLVED`, `EVIDENCE_WALL`, `TOOL_UNAVAILABLE`, `PERMISSION_DENIED`, `SYNC_FAILED`, `PROTOCOL_CONFLICT`, `MISSING_ARTIFACT`, or `UNKNOWN`.

   - `PROTOCOL_CONFLICT` — Worker received a message that violates the mailbox protocol (e.g. malformed kind, missing required fields, generation mismatch, conflicting state transitions). Do not attempt to process it; report the violation and stop.
   - `MISSING_ARTIFACT` — Artifact verification (size or SHA256) failed, or the expected artifact file does not exist at the declared path. Do not send REPORT until the artifact is recovered or the task is re-assigned.
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
- **手动 read inbox JSON = 违规**：直接读/`cat` inbox 文件不产生 consumption ACK，
  通知持续悬挂（oracle-init 一直挂着）。必须 `mailbox read`（inbox→processing）
  + `mailbox finalize`（→archive）才算消费。调试排障除外，但不得替代消费流程。
- Always validate `--to`; only write to recipient's inbox, never to another's status/archive.
- Two-phase consumption always: `read` (inbox→processing) → process → `finalize` (processing→archive).
- Remote SSH Worker formal communication is fully mailbox-based; INIT check prompt is via available runner channel, not send-keys.
- Never overwrite sent messages; never reuse filenames/msg_ids.
- Never substitute mailbox messages for artifacts; never put large files or sensitive full text in body.
- Never use capture-pane, terminal echo, or speculation for completion proof; send REPORT and update status.
