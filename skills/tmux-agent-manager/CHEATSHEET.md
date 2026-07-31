# tmux-agent-manager v2 Direct Inbox 速查
> Manager → `skill://tmux-agent-manager/` | Worker → `skill://tmux-agent-worker/`

## v2 Direct Inbox

所有命令使用 `--session` + `--agent`（不再用 `--worker`）。

```bash
# 先验证 ID
python3 scripts/tmux_worker.py mailbox-roster --config workers.toml

# Manager → Worker 正式 TASK（直写目标 inbox）
python3 scripts/tmux_worker.py mailbox send \
  --session <session-id> --from manager --to <agent-id> --kind TASK \
  --subject "<task>" --body "<full task + acceptance>"

# 或直接用 standalone CLI（zero-dependency）
MAILBOX_ROOT=.mailbox mailbox send \
  --session <session-id> --from manager --to <agent-id> --kind TASK \
  --subject "<task>" --body "<full task>"

# 非消费查询
mailbox peek --session <session-id> --agent <agent-id>

# 消费消息：inbox→processing（auto-claim，按 owner+lease）
mailbox read --session <session-id> --agent <agent-id> --owner <agent-id> [--json]

# 处理完成：processing→archive（校验 owner）
mailbox finalize --session <session-id> --agent <agent-id> --msg-id <id> --owner <agent-id>

# 放回 inbox（不处理时）
mailbox release --session <session-id> --agent <agent-id> --msg-id <id> --owner <agent-id>

# 崩溃恢复：过期 processing→inbox
mailbox recover-stale --session <session-id> --agent <agent-id>

# 计数与清理（stats shows all4 dirs: inbox/processing/archive/_corrupt）
mailbox stats --session <session-id> --agent <agent-id>
mailbox clear --session <session-id> --agent <agent-id>

# send-keys 只唤醒/steering（仅本地 Worker）
tmux send-keys -t <target> -l -- "MAILBOX_PENDING; check v2 inbox"
tmux send-keys -t <target> C-m
```

目录：`.mailbox/<session_id>/<agent>/{inbox,processing,archive,_corrupt}/`；外加 `.mailbox/<session_id>/session.json`。Worker→Manager、Worker→Worker、Manager→Worker 正式内容全部 direct inbox。
8 个必填字段：`session_id`、`from`、`to`、`subject`、`body`、`kind`、`msg_id`、`created_at`；3 个可选关联字段：`reply_to`、`run_id`、`request_id`。
7 种 kind：TASK, REPORT, PROGRESS, EVIDENCE, QUESTION, RESPONSE, NOTICE。
竞态保护：`mailbox read`（inbox→processing，auto-claim）→ 处理 → `mailbox finalize`（processing→archive）；`mailbox release` 回放；`mailbox recover-stale` 崩溃恢复。

## Notification reachability

- Local Worker (`ios-re`, `ios-shader`, `ohos-bin`): direct inbox is authoritative; `send-keys MAILBOX_PENDING` is an optional wake-up.
- Remote SSH Worker (`aosp`, `hyperos`, `ohos`): no local tmux socket; never use `send-keys` for Manager or peer communication. Mailbox v2 + status.json polling is the complete communication path.
- Manager polls each `status.json` plus `mailbox stats` inbox count every 5 seconds. A growing count is pending work, not a new status field. `BUSY` remains busy until the Worker updates status.
- A successful local `send-keys` call proves neither delivery nor reading; only the mailbox file and later status/REPORT prove progress.

## Plugin integration

- `$OMP_SESSION_ID` and `$OMP_WORKER_ID` must be set by the Worker launcher before OMP starts.
- Plugin uses `mailbox peek` + watch (30s timer fallback), dedups via `msg_id`.
- **Plugin only notifies — never consumes.** Agent reads via `mailbox read`.

## Runner adapter mode

- Standalone CLI is authoritative; tmux/oh-my-pi, opencode, or another runner MAY invoke it at task start/end/idle/checkpoint without private hook coupling.
- Adapter may peek, inject, and call `mailbox status`; never run a second manual read/finalize consumer.
- If no adapter is available, use `mailbox read` + `mailbox finalize` + `mailbox status`. Remote SSH uses mailbox/status only; local send-keys is optional wake.

## status.json

```bash
# TASK 开始
mailbox status --session <id> --agent <id> \
  --state BUSY --current-task "<one-line task>" --last-conclusion "<previous>"

# TASK 成功/阻塞结束（先发 REPORT）
mailbox status --session <id> --agent <id> \
  --state DONE --current-task "<task>" --last-conclusion "<brief result>"
mailbox status --session <id> --agent <id> \
  --state BLOCKED --current-task "<task>" --last-conclusion "<brief reason>"
```

`.mailbox/<session>/<agent>/status.json` **恰好五字段**：`session_id`、`state`、`current_task`、`last_conclusion`、`updated_at`。Manager 每 5 秒读 status；`BUSY` 不派新任务，`DONE/BLOCKED` 立即收取 Manager inbox 的 REPORT。`STALE` 只做诊断，不等于 IDLE。

## Task headers

```text
# Role: SourceAnalysis
# Domain: aosp
# Requires: source-analysis
# Anchors: visibleRegion, occlusion
# Mode: cooperative

<task body and acceptance criteria>
```

## Polling

- Worker：任务前、主要阶段后、final REPORT 前、终态后；本地看到 `MAILBOX_PENDING` 可立即检查，远程即使无提示也必须主动 poll。
- Manager：派发前、等待循环、状态终态、每封消息处理后；同时看 status 和 inbox count。
- 每次处理一封（read→process→finalize），直到 inbox 为空；处理完成才 clear archive。

## Errors / Prevention

- `_corrupt/` 非空：记录并要求 sender 用 CLI 重发；不修 JSON。
- `.sync-conflict-*`：跳过并人工审计；不改名成正常消息。
- clock skew：按 mtime/实际到达处理；不信文件名时间排序。
- crash recovery：`mailbox recover-stale` 恢复过期 processing 消息。
- 永远用 CLI，永远先校验收件人，永远不覆盖已发送消息。
- capture-pane 不用于状态；status 是快照，REPORT 是完整结论。

## Legacy (v1)

v1 使用以下已废弃概念，全部由 v2 `status.json` + direct inbox 取代：
- **control envelope**（A-plane）：旧 control/steering 下行指令封装
- **B-plane**：旧 event-emit 生命周期事件（ACK/DONE/BLOCKED/WORKING）
- **mailbox/outbox → relay daemon → mailbox/inbox**：旧消息中继路径
- **cursor / unread / mark-read**：旧消息消费状态跟踪

```bash
python3 scripts/tmux_worker.py request --worker <id> --task-file t.txt --yes
python3 scripts/tmux_worker.py request-role --role <role> --task-file t.txt --yes
python3 scripts/tmux_worker.py batch-request --batch b.json --yes
python3 scripts/tmux_worker.py event-emit ...
python3 scripts/tmux_worker.py event-wait ...
python3 scripts/tmux_worker.py mailbox-send ...
python3 scripts/tmux_worker.py mailbox-check ...
python3 scripts/tmux_worker.py mailbox-relay ...
```

仅供尚未迁移 Worker 审计。v1 架构不得用于新任务。
