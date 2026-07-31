---
name: tmux-agent-worker
description: Worker agent protocol for tmux-agent-manager orchestration. Accepts INIT/TASK from Manager, executes under a single role profile, reports DONE/BLOCKED. Triggered automatically when spawned as a worker.
---

# Worker Protocol
> Manager协议: skill://tmux-agent-manager/ | 操作指南: skill://tmux-agent-manager/OPERATIONS.md | 速查: skill://tmux-agent-manager/CHEATSHEET.md


You are a worker agent. This file is your sole **protocol** source — not domain knowledge. Identity and role come exclusively from Manager's INIT. Before INIT: read this file and wait.

## Constraints

1. **No Git writes** — `commit/add/stage/amend/reset/rebase/revert/cherry-pick/checkout/restore/rm/clean`. Need a commit? Finish artifacts, wait.
2. **No kill** — any process. Need termination? BLOCKED.
3. **No cleanup** — don't touch other workers' files. Parallel interleaving is normal.
4. **File isolation** — only your assigned artifact path. Conflict → BLOCKED.
5. **Evidence honesty** — insufficient evidence → `[EVIDENCE PENDING]` or `[INFERENCE: reason]`. Never fabricate.


## Launch and Identity

Manager 仍负责 shell、cwd 与 agent 启动。INIT 必须明确提供实际的 `session_id`、`worker_id`、role profile 与 artifact root；后续命令中的 `<session-id>` 与 `<worker-id>` 必须替换为 INIT 中给出的实际值，不能照抄占位符，也不要从其他 profile 推断身份。

### Fresh session

新 omp session 没有旧上下文：按 INIT Handshake 接收正式 INIT，执行 `mailbox read --session <session-id> --agent <worker-id> --owner <worker-id> --json` → 校验身份 → `mailbox status --session <session-id> --agent <worker-id> --state IDLE --current-task "waiting for TASK" --last-conclusion "INIT accepted"` → `mailbox finalize --session <session-id> --agent <worker-id> --msg-id <id> --owner <worker-id>`，再等待正式 TASK。

### Restored session (`omp -c`)

恢复的 session 可能带有 session-based protocol 之前的 stale IPC/conversation context。**RESET 必须先于正式 INIT TASK**：收到 Manager 的 reset/wake prompt 后，第一个动作必须是丢弃所有此前的 mailbox paths、command names、protocol assumptions 和 IPC mechanisms；重新读取 `skill://tmux-agent-worker` 与 `skill://tmux-agent-manager` 的 CURRENT protocol。唯一有效的命令是 standalone `mailbox` CLI；唯一有效的路径是 `.mailbox/<session>/<agent>/inbox|processing|archive/`。不得引用 `scripts/tmux_worker.py`、`workers.toml`、`mailbox-v2-*`、outbox、relay、cursor 或 flat `.mailbox/<worker>/` 路径。用 `ls .mailbox/<session-id>/<worker-id>/inbox/` 验证实际 inbox，再执行正式 INIT Handshake；不要因旧上下文报告 “Inbox empty” 而改查 flat path。

### Already initialized

若 `.mailbox/<session-id>/<worker-id>/status.json` 已存在且 `state` 为 `IDLE`，说明 INIT 已完成。此时新的 INIT 是 **NO-OP**：不要重新读取 skills、不要重写 IDLE、不要再次发送或消费 INIT；只执行 `mailbox peek --session <session-id> --agent <worker-id>`，然后按正常 polling contract 用 `mailbox read` 处理新的 TASK。

## INIT Handshake

Manager 先通过 standalone `mailbox send` 写入 `kind=TASK`、`subject=INIT` 的正式 INIT，再用 send-keys（或远程 runner 可用的等价交互提示）发送“Registration: write identity file and check inbox”；提示不是任务正文。收到提示后立即执行 `mailbox read --session <session-id> --agent <worker-id> --owner <worker-id> --json`，验证 INIT 中的实际身份与 role profile，向 launcher 注入的 `$OMP_MAILBOX_IDENTITY_FILE` 写入身份注册 JSON，执行 `mailbox status --session <session-id> --agent <worker-id> --state IDLE --current-task "waiting for TASK" --last-conclusion "INIT accepted"`，然后用该消息的 `<id>` 执行 `mailbox finalize --session <session-id> --agent <worker-id> --msg-id <id> --owner <worker-id>`。Manager 会检查 `.mailbox/<session-id>/<worker-id>/status.json` 已存在且含五个字段后，才认为握手完成；不要用终端回显或 send-keys 代替 status。

### Plugin activation after INIT

The Manager launcher must generate a unique per-process identity path and inject it into the OMP process at startup through OS-level environment inheritance:

```bash
TOKEN=$(date +%s)_$RANDOM
mkdir -p ~/.omp/mailbox-identity
OMP_MAILBOX_IDENTITY_FILE=~/.omp/mailbox-identity/${TOKEN}.json omp -c
```

The plugin reads `OMP_MAILBOX_IDENTITY_FILE` from `process.env` at startup (inherited from the OS; do not mutate `process.env` at runtime), then polls that exact file every 2 seconds. During INIT, the Worker MUST register its identity:

```bash
echo '{"session_id":"<session-id>","worker_id":"<worker-id>"}' > "$OMP_MAILBOX_IDENTITY_FILE"
```

The plugin activates as soon as valid JSON appears; no scanning, fixed registry file, `agent_end` dependency, or restart after registration is needed. On `session_shutdown`, it deletes the identity file. Each OMP process receives a unique launcher path, so multiple agents on one machine cannot conflict.

### Mailbox health gate

After consuming INIT and writing `IDLE`, the Worker MUST run this as the FIRST and only health check before proceeding:

```bash
# One JSON result covering root, session, agent dirs, inbox listing, peek,
# status read/write, and OMP plugin identity registration (all 8 connectivity checks)
mailbox-health --session <session-id> --agent <worker-id> --json
```
Inspect the JSON result; every check must pass before TASK work or notification polling begins. If any check fails, use its diagnostic to report the exact broken condition (missing directory, unavailable CLI, unwritable status, unset or unwritable `$OMP_MAILBOX_IDENTITY_FILE`, etc.) and wait for repair. If `mailbox-health` itself is not found or produces no output, do NOT proceed; send the Manager a `NOTICE` describing the health-command failure, then wait:

```bash
mailbox send --session <session-id> --from <worker-id> --to manager \
  --kind NOTICE --subject "MAILBOX_HEALTH_FAILED" \
  --body "mailbox-health was not found or returned no output; session mailbox connectivity is not verified"
```

## v2 Direct Inbox

`.drafts/` is for agent work artifacts; `.mailbox/` is for agent-to-agent communication.

正式 TASK、Manager 补充材料和 peer 消息都写入你的 `.mailbox/<session-id>/<worker-id>/inbox/`。这里的尖括号仅表示“填入 INIT 中的实际值”；文件路径必须使用这些真实 ID。Syncthing 直接同步。

两阶段消费：`mailbox read`（inbox→processing，auto-claim）→ 处理 → `mailbox finalize`（processing→archive，校验 owner）。

```bash
# 每次调用只读取、验证并原子移到 processing（按 owner+lease 自动声明）
mailbox read \
  --session <session-id> --agent <worker-id> --owner <worker-id> [--json]

# 处理完成后归档
mailbox finalize \
  --session <session-id> --agent <worker-id> --msg-id <id> --owner <worker-id>

# 直接写收件人的 inbox
mailbox send \
  --session <session-id> --from <worker-id> --to manager \
  --kind REPORT --subject "<short result>" --body "<conclusion and artifact refs>"

# 非消费查询与统计（stats shows all 4 dirs: inbox/processing/archive/_corrupt）
mailbox peek --session <session-id> --agent <worker-id>
mailbox stats --session <session-id> --agent <worker-id>
mailbox clear --session <session-id> --agent <worker-id>

# 崩溃恢复：过期 processing→inbox
mailbox recover-stale --session <session-id> --agent <worker-id>
```
Standalone CLI 的命令名固定为 `session-init`、`send`、`peek`、`read`、`finalize`、`release`、`recover-stale`、`check`、`status`、`clear`、`stats`；所有命令直接调用 standalone `mailbox`，不要调用 runner wrapper。CLI resolution order: (1) PATH command `mailbox`（from `codeagent` package via `uv tool install`）；(2) `codeagent mailbox` as unified cross-host entry point; (3) for swarm sessions, use `codeagent swarm ...` subcommands. Remote SSH Worker 应按此顺序尝试，禁止使用 `scripts/tmux_worker.py`。

消息 8 个必填字段：`session_id`、`from`、`to`、`subject`、`body`、`kind`、`msg_id`、`created_at`；3 个可选关联字段：`reply_to`、`run_id`、`request_id`。7 种 kind：`TASK`、`REPORT`、`PROGRESS`、`EVIDENCE`、`QUESTION`、`RESPONSE`、`NOTICE`。不要手写或编辑 JSON。修正旧消息必须发新消息，并用 `--reply-to <msg_id>` 回链。
Mailbox REPORT/NOTICE/QUESTION content is **advisory** evidence and coordination; a TASK message is the formal v2 dispatch envelope, while `status.json` is the active-state snapshot. Neither free-form body text nor send-keys replaces the required status update.

## Runner adapter integration (preferred, framework-neutral)

The standalone mailbox/status CLI is authoritative. A tmux/oh-my-pi plugin, opencode adapter, or another runner MAY invoke `mailbox peek` at safe boundaries for notification; the **plugin only notifies — never consumes**. The agent reads via `mailbox read`. It must not rely on private framework hooks and must not run two archive consumers concurrently. If no adapter is available, use the manual CLI fallback below.

## TASK Processing
1. On TASK arrival or any plugin notification (`📬 MAILBOX: N pending...`), the Worker’s FIRST action is `mailbox read --session <session-id> --agent <worker-id> --owner <worker-id> --json`. The notification is only a preview; do not act on its text or merely acknowledge it. The real message body must be consumed from the inbox file, and `mailbox read` moves it to `processing/` for the required read→finalize flow. Then accept only a validated `kind=TASK` from the expected Manager; verify Role/Domain/Requires/Anchors and acceptance criteria. If an adapter delivered it, still perform this read unless it explicitly confirms the message was already claimed by this Worker.
2. At task start, ensure `BUSY` was written by the adapter or call `mailbox status --session <session-id> --agent <worker-id> --state BUSY --current-task "<one-line task>" --last-conclusion "<previous conclusion>"` yourself. Local `MAILBOX_PENDING` is only an optional wake; remote SSH Workers have no local tmux socket and rely on mailbox/status polling.
3. During work, an adapter may inject at safe checkpoints. In fallback mode, call `mailbox read` at each major boundary and after long tools, one message at a time.
4. Wrong target, insufficient capability, or underspecified task: send a `NOTICE`, set `BLOCKED` through the adapter or status CLI, and stop; never silently execute.
5. 处理完成后调用 `mailbox finalize` 将消息从 processing 归档到 archive。

## status.json

你只维护 `.mailbox/<session-id>/<worker-id>/status.json`。这里的 `<session-id>` 与 `<worker-id>` 必须替换为 INIT 给出的实际值。它必须始终是以下五字段的人类可读快照，禁止添加协议元数据或嵌入报告全文：

```json
{
  "session_id": "<session-id>",
  "state": "BUSY",
  "current_task": "trace narrow blur weights",
  "last_conclusion": "waiting for IR",
  "updated_at": "2026-07-22T15:30:00Z"
}
```

- TASK 开始：`BUSY`，写一句 `current_task`，保留上一条简短 conclusion。
- 成功结束：先发 final `REPORT`，再写 `DONE`；`last_conclusion` 概括真实结果。
- 阻塞结束：先发 `NOTICE/REPORT` 说明可复核原因，再写 `BLOCKED`；`last_conclusion` 是阻断原因。
- 没有任务：初始化或 Manager 明确收件后可写 `IDLE`。

`mailbox status` 会自动写入 `session_id`、`state`、`current_task`、`last_conclusion`、`updated_at` 五个字段；不要手写 status JSON。


其他 agent 可读此文件协调，但不得修改；`updated_at` 是 UTC 新鲜度提示，不是跨机器排序真源。

## Polling Contract

Plugin mode: no manual poll is required; the plugin owns inbox watch, validation, peek→inject, and status transitions. The Worker still reviews injected messages before acting and must not clear archive until the task is complete.
When plugin injects `📬 MAILBOX: N pending...`, the notification is only a preview. The Worker’s first action is always `mailbox read --session <session-id> --agent <worker-id> --owner <worker-id> --json`; consume the real message body from the inbox file and let `read` move it to `processing/`. Never treat notification text alone as delivery or completion.

Fallback mode: `mailbox read` at task start, each major phase, before final REPORT, and after terminal status. Local `MAILBOX_PENDING` can accelerate a check; remote SSH Workers must actively poll and never use send-keys. Process one message at a time (read→process→finalize), then clear archive only after all work is handled.

## Multi-Mode Participation

| Mode-Role | v2 behavior |
|---|---|
| cooperative / candidate / parallel | 独立执行；向 Manager 发 `PROGRESS/REPORT` |
| reviewer / verifier / critic | 轮询目标 status；终态后收取 REPORT/产物，再复核 |
| pilot / executor / mentee | 在阶段边界读 `NOTICE`，纳入有效反馈 |
| copilot / advisor / mentor | 用 `NOTICE` 直写目标 inbox；必要时等待对方 `RESPONSE` |

所有模式都遵循 `BUSY → DONE|BLOCKED` status 以及 final REPORT。status 是活跃度快照，REPORT 才承载完整结论。

## Post-Completion Verification

SourceAnalysis 与 ClosedSourceReverse Worker 在完成前必须复核符号、调用链、分支与证据边界；无法验证的声明标记 `[INFERENCE: reason]` 或 `[EVIDENCE PENDING]`。修复重大问题后重新复核。Documentation Worker 不做独立技术推断。

## Completion and Blocked

### Done

1. 写完并校验 artifact。
2. 在终态前再轮询 inbox，`mailbox read` 处理所有与本任务有关的消息。
3. 向 Manager 发送 final `REPORT`，包含 artifact 路径、摘要、验证状态和所有 inference/pending 项。
4. Call `mailbox status --session <session-id> --agent <worker-id> --state DONE --current-task "<task>" --last-conclusion "<brief result>"`.
5. 再检查一次 inbox；确认处理完成后 `mailbox finalize` 归档最后一封、`mailbox clear`，等待下一封 TASK。

### Blocked

1. 保存已有 artifact 与证据边界。
2. 向 Manager 发送 `REPORT` 或 `NOTICE`，明确 reason code：`MISSING_BINARY`、`IDA_TIMEOUT`、`MISSING_IR`、`DEPENDENCY_UNRESOLVED`、`EVIDENCE_WALL`、`TOOL_UNAVAILABLE`、`PERMISSION_DENIED`、`SYNC_FAILED` 或 `UNKNOWN`。
3. Call `mailbox status --session <session-id> --agent <worker-id> --state BLOCKED --current-task "<task>" --last-conclusion "<brief reason>"`.
4. 检查 inbox，处理并 finalize 已读消息，`mailbox clear`，停止扩展。

## Error Handling

- **Corrupt JSON / self-validation failure**：`mailbox read` 会移入 `_corrupt/`。记录文件名并向 Manager 发 NOTICE；不要手改、恢复或删除它。
- **Syncthing conflict**：跳过 `.sync-conflict-*`；通知原发送者通过 CLI 重发。不得把冲突文件改名成正常消息。
- **Clock skew**：按 inbox 可见顺序处理；`created_at` 与文件名时间仅供诊断。发现明显偏差可 NOTICE `CLOCK_SKEW`，不能改时间戳。
- **Unknown recipient**：先核对 Manager 提供的 session roster 与实际 `.mailbox/<session-id>/<worker-id>/inbox`，发送失败不得自行创建目录或换一个相似 ID。
- **Status 写入失败**：保留 artifact，向 Manager 发 NOTICE；仍失败则停止扩展，避免出现"工作继续但状态不可见"。
- **Crash recovery**：发现 `processing/` 中有过期消息（超过 300s lease），运行 `mailbox recover-stale` 自动将过期 claim 放回 inbox；`mailbox stats` 显示 `processing` 非零时应立即排查。不手移文件。

## Prevention Rules

- 永远用 CLI；不要手写 mailbox/status JSON。
- 永远验证 `--to`；只写收件人 inbox，永远不写别人的 status/archive。
- 两阶段消费：`mailbox read`（inbox→processing）→ 处理 → `mailbox finalize`（processing→archive）；`mailbox release` 用于放回；`mailbox recover-stale` 用于崩溃恢复。
- 远程 SSH Worker 的正式通信完全走 mailbox；INIT 检查提示仅由 Manager 经可用的 runner/交互通道发送，不能以 send-keys 成功代替 mailbox 读取或 status.json。
- 不覆盖已发送消息，不复用文件名/msg_id。
- 不用 mailbox 消息代替 artifact；不把大文件或敏感原文塞入 body。
- 不用 capture-pane、terminal echo 或推测表示完成；发送 REPORT 并更新 status。

## Manager Lost

若 Manager 不可达：停止扩展，保存 artifact，尝试向 Manager inbox 发 REPORT/NOTICE，把 status 写为 `DONE` 或 `BLOCKED`，然后等待；禁止 kill 子进程。

## Legacy (v1)

v1 架构使用以下已废弃概念，全部由 v2 `status.json` + direct inbox 取代：

- **control envelope**（A-plane）：旧 control/steering 下行指令，v2 改用 `mailbox read`
- **B-plane**：旧 event-emit 生命周期事件（ACK/DONE/BLOCKED/WORKING），v2 改用 `status.json`
- **mailbox/outbox → relay daemon → mailbox/inbox**：旧消息中继路径，v2 改用 direct inbox
- **cursor / unread / mark-read**：旧消息消费状态跟踪，v2 改用 `mailbox read`/`mailbox finalize` 两阶段消费
- **mailbox-check**：旧消息查询，v2 改用 `mailbox read`（非消费用 `mailbox peek`）

兼容旧 Manager 时，以下命令仍可使用但均为 LEGACY：`event-emit ACK/DONE/BLOCKED/WORKING`、`mailbox-send`、`mailbox-check`、`mailbox-relay`、control-envelope TASK、queue auto-drain。v1 架构不得带入新的 v2 TASK。

若收到 v1 envelope，按旧请求完成其 ACK/terminal event，同时仍维护 v2 `status.json` 并向 Manager v2 inbox 发 REPORT；不得为兼容而让新消息重新走 relay。
