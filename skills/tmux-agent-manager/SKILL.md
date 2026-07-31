---
name: tmux-agent-manager
description: tmux-agent-manager v3 — 薄编排契约
---

# tmux-agent-manager v3 — 薄编排契约

> Worker 规则：`skill://tmux-agent-worker/` | 操作指南：`skill://tmux-agent-manager/OPERATIONS.md` | 速查：`CHEATSHEET.md`

## 1. 职责边界

本 skill **只编排**，不做领域研究或证据判断。Manager 可读 `workers.toml`、Worker 产物、`.mailbox/<session>/<agent>/status.json` 与自己的 session manager inbox；禁止用 `capture-pane` 判断状态。`status.json` 是当前状态快照，正式结论以 Worker 发往 Manager inbox 的 `REPORT` 为准。

## Manager Self-Initialization

Before dispatching or waiting for any Worker, Manager MUST initialize its own identity and notification path:

1. Re-read the current `skill://tmux-agent-manager` skill (and `skill://tmux-agent-worker` when interpreting Worker state); do not rely on restored conversation context.
2. Set the actual session identity for the current run and activate the plugin environment:

   ```bash
   export OMP_SESSION_ID=<actual-session-id>
   export OMP_WORKER_ID=manager
   ```

   Replace `<actual-session-id>` with the real session ID; never leave a placeholder or use a flat worker path.
3. Check the Manager inbox before declaring the Manager idle:

   ```bash
   mailbox peek --session <actual-session-id> --agent manager
   ```

4. If `pending` is greater than zero, process every pre-existing REPORT/NOTICE before declaring IDLE: use `mailbox read --session <actual-session-id> --agent manager --owner manager --json`, verify the report/artifact, then `mailbox finalize --session <actual-session-id> --agent manager --msg-id <id> --owner manager`; repeat until the inbox is empty. A failed peek or unreadable inbox is a startup failure, not an idle state.
5. After the inbox is drained, write Manager's own five-field status snapshot:

   ```bash
   mailbox status --session <actual-session-id> --agent manager --state IDLE --current-task "waiting for REPORT" --last-conclusion "manager initialized"
   ```

6. Start the configured mailbox plugin/watch or the documented polling loop for incoming Worker REPORTs. The plugin may notify/peek only; Manager must consume reports itself with `mailbox read` → process → `mailbox finalize`.

## 2. 当前通信模型

| Direction | Primary path | Purpose |
|---|---|---|
| Manager→Worker | standalone `mailbox` direct inbox | 正式 INIT/TASK、补充材料、需留痕的指令 |
| Manager→Worker | `tmux send-keys` | INIT 后的检查 inbox 提示或短 steering；不承载正式任务正文 |
| Worker→Manager | standalone `mailbox` direct inbox | `REPORT`、`PROGRESS`、`QUESTION`、`NOTICE` |
| Worker→Worker | standalone `mailbox` direct inbox | peer 问答、证据与复核请求；Syncthing 直接同步 |
| Worker→all observers | `.mailbox/<session>/<agent>/status.json` | `IDLE/BUSY/DONE/BLOCKED`、当前任务、最后结论 |

通知策略按部署位置区分：mailbox 才是可靠 payload，send-keys 只用于唤醒或提示。INIT 是例外的显式握手：Manager 必须先写正式 INIT，再向目标 pane 发送“检查 inbox”的提示；远程 SSH Worker 没有本地 tmux socket 时，使用其可用的 runner/交互通道发送同一提示。无论提示是否送达，Manager 都必须等待 Worker 主动 `mailbox read` 并验证五字段 `status.json`，不能把 send-keys 成功当作送达或状态证据。后续远程 Worker 若无 runner adapter，仍须在 task start/end、阶段边界和长工具返回后主动轮询自己的 inbox。启用 framework-neutral adapter 时由 standalone CLI 完成 watch/peek→inject/status_update。Manager 对所有 Worker 都轮询 `status.json` 与 inbox 计数。

## v2 Direct Inbox

目录是共享仓库根下的 `.mailbox/`，session 隔离：

```text
.mailbox/<session_id>/
  session.json   # {manager, agents, created_at}
  manager/
    inbox/        # 其他参与者直接写入
    processing/   # read 后自动声明，finalize 后移到 archive
    archive/      # 已处理消息
    _corrupt/     # 解析或自校验失败消息
  <agent>/
    inbox/
    processing/
    archive/
    _corrupt/
    status.json   # Agent 自报状态
```

以下命令与 INIT 模板中的 `<session-id>`、`<agent-id>`、`<worker-id>`、`<role>` 和 `<artifact-root>` 都是说明性占位符；Manager 发送前必须以 session 初始化结果、workers.toml 和实际 Worker 启动信息替换它们，不能把尖括号原样发送。

```bash
# 创建 session；agents 使用 workers.toml 中的准确 ID，逗号分隔
mailbox session-init --session <session-id> --manager manager --agents <agent-id>,<another-agent-id>

# Start each Worker with a unique OS-inherited identity path; do not rely on runtime env mutation
TOKEN=$(date +%s)_$RANDOM
mkdir -p ~/.omp/mailbox-identity
OMP_MAILBOX_IDENTITY_FILE=~/.omp/mailbox-identity/${TOKEN}.json omp -c

# Manager 正式派 INIT/TASK；正文必须完整、可独立执行
mailbox send \
  --session <session-id> --from manager --to <worker-id> --kind TASK \
  --subject "INIT" \
  --body "INIT. Exact session_id=<session-id>; exact worker_id=<worker-id>; role_profile=<role>; artifact_root=<artifact-root>. Write {\"session_id\":\"<session-id>\",\"worker_id\":\"<worker-id>\"} to the launcher-injected $OMP_MAILBOX_IDENTITY_FILE path, then read this INIT, write IDLE status, and finalize it. If restored with omp -c, discard ALL prior mailbox paths, command names, protocol assumptions, and IPC mechanisms; re-read skill://tmux-agent-worker and skill://tmux-agent-manager for the CURRENT protocol. Use only standalone mailbox, session_id, read→finalize, status.json, and the launcher identity file."
```

### INIT 握手（每个 Worker 必须完成）

### Target initialization scenarios

- **Fresh Worker**：没有既有 session context，直接执行下方四步 INIT 握手。
- **Restored Worker (`omp -c`)**：RESET prompt 必须先于正式 INIT TASK。先发送：`tmux send-keys -t <target> -l -- "RESET: forget ALL prior flat mailbox paths, dotai wrappers, relay/outbox/IPC logic, workers.toml assumptions, and mailbox-v2-* names. Re-read skill://tmux-agent-worker and skill://tmux-agent-manager. Use only standalone mailbox and session-based paths. Verify with ls .mailbox/<session-id>/<worker-id>/inbox/"`，再发送回车；远程没有本地 tmux 时经可用 runner 发送同一 prompt。收到确认或按可见 inbox 验证后，才执行正式 INIT send。
- **Already-idle Worker**：若目标 `.mailbox/<session-id>/<worker-id>/status.json` 已存在且 `state=IDLE`，新的 INIT 是 **NO-OP**；不要重新发送/消费 INIT、不要要求重读 skill 或重写 IDLE，只执行 `mailbox peek --session <session-id> --agent <worker-id> [--json]`，并等待新的 TASK。


1. **写入正式 INIT（a）**：Manager 用上面的 `mailbox send` 将 `kind=TASK`、`subject=INIT` 写入目标 Worker inbox；body 必须包含该 Worker 的实际 `session_id`、`worker_id`、role profile、artifact root 和兼容握手要求。
2. **发送检查提示（b）**：紧接着向目标 pane 发同一身份信息的短提示，明确要求立即检查 inbox。目标支持 tmux 时使用：

   ```bash
   tmux send-keys -t <target> -l -- "Registration: write {session_id, worker_id} with exact session_id=<session-id> and worker_id=<worker-id> to the launcher-injected \$OMP_MAILBOX_IDENTITY_FILE path, then check inbox with mailbox read --session <session-id> --agent <worker-id> --owner <worker-id> --json. If restored with omp -c, first discard ALL prior mailbox/relay/outbox/IPC logic and re-read skill://tmux-agent-worker and skill://tmux-agent-manager; use only the CURRENT standalone mailbox protocol and session-based paths."
   tmux send-keys -t <target> C-m
   ```

   `<session-id>` 与 `<worker-id>` 在发送前必须替换为实际值；send-keys 只是唤醒/提示，不承载正式 INIT 正文。远程 SSH Worker 没有本地 tmux socket 时，通过其可用 runner/交互通道发送完全相同的提示；若没有该通道，仍以共享 inbox 为 payload，并要求 Worker 主动轮询。
3. **Worker 读取并确认（c）**：Worker 执行 `mailbox read`（inbox→processing），校验 INIT 的实际身份，随后执行 `mailbox status --session <session-id> --agent <worker-id> --state IDLE --current-task "waiting for TASK" --last-conclusion "INIT accepted"`，再以读到的消息 ID 执行 `mailbox finalize`（processing→archive）；Manager 不得以 pane 文本或 send-keys 回显代替这个状态写入。
4. **验证握手（d）**：Manager 检查 `.mailbox/<session-id>/<worker-id>/status.json` 已存在、`session_id` 等于实际 session ID，并含五个字段 `session_id`、`state`、`current_task`、`last_conclusion`、`updated_at`；验证通过后才能派正式 TASK。Worker 若没有后台 polling，也必须由上述提示触发这次主动 `mailbox read`。

```bash

# Manager 读取自己的 inbox：read (inbox→processing, auto-claim) → 处理 → finalize (processing→archive)
mailbox read \
  --session <session-id> --agent manager --owner manager [--json]
mailbox finalize \
  --session <session-id> --agent manager --msg-id <id> --owner manager

# 非消费查看、统计与清理
mailbox peek --session <session-id> --agent manager
mailbox stats --session <session-id> --agent manager  # shows all 4 dirs: inbox/processing/archive/_corrupt
mailbox clear --session <session-id> --agent manager

# 崩溃恢复：将过期 processing 消息放回 inbox
mailbox recover-stale --session <session-id> --agent manager
```

远程 SSH Worker 的可靠通知路径是共享 inbox + Worker 主动轮询；send-keys 提示不构成送达证据。

## Plugin and runner integration (preferred, framework-neutral)

The authoritative standalone CLI command set is `session-init`, `send`, `peek`, `read`, `finalize`, `release`, `recover-stale`, `check`, `status`, `clear`, and `stats`; invoke these names directly, never through a runner wrapper. A tmux/oh-my-pi plugin, opencode adapter, or another runner MAY invoke `mailbox peek` at safe boundaries for notification; the **plugin only notifies — never consumes**. The agent reads via `mailbox read`. No skill depends on private runner hooks.
CLI resolution order is: (1) bundled plugin `~/.omp/plugins/node_modules/omp-mailbox-plugin/bin/mailbox`; (2) PATH command `mailbox` (from `codeagent` package via `uv tool install`); (3) `codeagent mailbox` as unified cross-host entry point. Try these locations in order; never route commands through `scripts/tmux_worker.py`.

Manager still polls each `status.json` and inbox statistics for observability. Adapter injection is not a substitute for final REPORT or artifact verification. If no adapter is available, the runner calls the standalone CLI at the documented boundaries. Remote SSH Worker formal communication uses the plugin/direct Syncthing path; an available runner may carry the INIT check prompt, but send-keys is never the payload or proof of delivery.

v2 消息固定 8 个必填字段：`session_id`、`from`、`to`、`subject`、`body`、`kind`、`msg_id`、`created_at`。3 个可选关联字段：`reply_to`、`run_id`、`request_id`。7 种 kind：`TASK`、`REPORT`、`PROGRESS`、`EVIDENCE`、`QUESTION`、`RESPONSE`、`NOTICE`。文件名与 `msg_id` 一致；消息不可原地修改，纠正内容必须发新消息并用 `--reply-to <msg_id>` 回链。

消费流程：`mailbox read`（inbox→processing，按 owner+lease 自动声明）→ agent 处理 → `mailbox finalize`（processing→archive，校验 owner）；`mailbox release` 可放回 inbox；`mailbox recover-stale` 将过期 processing（300s lease）自动恢复到 inbox。

## 3. status.json 状态快照

每个 Agent 只写自己的 `.mailbox/<session>/<agent>/status.json`，且文件固定为五个字段：`session_id`、`state`、`current_task`、`last_conclusion`、`updated_at`。

```json
{
  "session_id": "<session-id>",
  "state": "BUSY",
  "current_task": "verify shadow paths",
  "last_conclusion": "traditional and SDF paths are separate",
  "updated_at": "2026-07-22T15:30:00Z"
}
```

- `session_id`: 当前 session 的实际 ID，必须与目录名一致，由 `mailbox status` 自动写入。
- `state`: `IDLE | BUSY | DONE | BLOCKED`。
- `current_task`: 一句话任务摘要；不得塞任务全文。
- `last_conclusion`: 一句话最新结论或阻断原因。
- `updated_at`: UTC，仅用于新鲜度诊断，不用于跨机器消息排序。


Manager 派发前必须读取目标 status：只有 `IDLE/DONE/BLOCKED` 且已处理上一条 REPORT 才可派新任务。`BUSY` 禁止并发派发。状态过旧时先检查 inbox、Syncthing 与 Worker 可达性，不能凭旧值宣告空闲。

## 4. 派发、轮询与收件

1. INIT 握手完成后，用 workers.toml 与 `.mailbox/<session>/session.json` 中的准确 ID 验证收件人和目标 status；不要猜 ID。
2. 用 `mailbox send --session <session-id> --from manager --to <worker-id> --kind TASK` 直写目标 inbox。正式 TASK 之前必须完成上面的 INIT 四步握手；后续仅可对支持交互通道的 Worker 发短 `send-keys` 提示，远程 Worker 不依赖该提示。
3. 每 5 秒轮询目标 `status.json` 和 `mailbox stats` 的 inbox 数量；值由 `BUSY` 转为 `DONE/BLOCKED` 后，轮询 Manager inbox 收取对应 `REPORT`。
4. Worker 无论本地还是远程都在 task start/end、阶段边界和长工具返回后主动 `mailbox read`。Manager 自己在派发前、等待期间每个检查周期、处理完一封消息后再次 `mailbox read` Manager inbox，直到为空。
5. 读完 artifact/REPORT、安排后续后，再 `mailbox finalize` 归档，最后 `mailbox clear`；不得在消息尚未处理时 clear。

状态快照与 REPORT 不一致时：以 REPORT 作为结论内容，以最新可验证的 `status.json.updated_at` 作为活跃度快照；保留冲突并要求 Worker 发纠正消息，禁止静默覆盖。

## 5. 工作模式

工作模式仍由 TASK 头部 `# Mode` / `# Mode-Role` 决定。candidate/pilot/mentor 等所有协作信息均走 v2 direct inbox。Reviewer/verifier 必须等目标 `status.json` 终态且收到 REPORT/产物后再开始；不能只看到 `DONE` 字样就越过报告验收。

## 6. 错误处理与恢复

- **Corrupt message**：`mailbox read` 将坏 JSON 或 `msg_id`/文件名不一致的文件移到 `_corrupt/`。Manager 看到 `corrupt > 0` 必须记录文件名、通知发送方重发新消息；不得手改原 JSON。
- **Syncthing conflict**：任何 `.sync-conflict-*` 文件都不是有效消息。保留原件，比较双方内容，由发送方通过 CLI 发一封新消息；不得 rename 成正常消息伪造投递。
- **Clock skew**：处理顺序以 inbox 文件 mtime/实际可见顺序为准，`created_at` 和文件名时间只做诊断。明显偏差时记录 `CLOCK_SKEW`，不要重写时间戳。
- **Stale status / pending inbox**：`updated_at` 超过预期 SLA 时状态为 `STALE` 诊断，不等同 `IDLE` 或 `BLOCKED`。Manager 先检查 inbox count、Syncthing 和 pane liveness；本地可再发 wake，远程只能等待 Worker 的下一次主动 poll。
- **Missing recipient**：发送失败即重新核对 workers.toml、`session.json` 与 `.mailbox/<session>/<agent>/inbox`，不创建拼错的目录。
- **Crash recovery**：发现 `processing/` 中有过期消息（超过 300s lease），运行 `mailbox recover-stale --session <session-id> --agent <agent-id>` 自动放回 inbox；不手移文件。

## 7. 预防规则

- **永远不要手写 JSON**；只能用 `mailbox send` 和 `mailbox status`，二者负责原子写入。
- 每次发送前验证 `--to` 在 roster 中且与目标 inbox owner 一致。
- 不复用、覆盖或编辑已发送消息；更正用新消息 + `--reply-to`。
- 不用文件名时间排序业务优先级，不用 capture-pane 推断状态。
- 不把大产物塞进 body；body 只放摘要和可定位的 artifact 引用。
- `mailbox clear` 只清 archive，且只在任务/收件处理完整结束后执行。
- 远程 Worker 的可见性以 status/inbox 轮询为准；send-keys 成功返回不代表消息已被 agent 看到。

## 8. 启动与故障边界

### Launch and Identity

For a restored session (`omp -c`), the first action after INIT is to discard ALL prior mailbox paths, command names, protocol assumptions, and IPC mechanisms; re-read `skill://tmux-agent-worker` and `skill://tmux-agent-manager` for the CURRENT protocol. The ONLY valid commands are the standalone `mailbox` CLI, and the ONLY valid paths are `.mailbox/<session>/<agent>/inbox|processing|archive/`. Do not reference `scripts/tmux_worker.py`, `workers.toml`, `mailbox-v2-*`, outbox, relay, cursor, or flat `.mailbox/<worker>/` paths.

Marker 与 pane liveness 仍用于启动：`PANE_ALIVE → SHELL_READY → CWD_VERIFIED → AGENT_STARTED`。启动完成后 Worker 通过 status 写 `IDLE`。Manager 失联、同步失败或工具不可用时，Worker 保存产物、发送 `REPORT/NOTICE`，并将 status 更新为 `BLOCKED`；禁止硬 kill。

## Legacy (v1)

v1 架构使用以下已废弃概念，全部由 v2 `status.json` + direct inbox 取代：

- **control envelope**（A-plane）：旧 control/steering 下行指令封装
- **B-plane**：旧 event-emit 生命周期事件（ACK/DONE/BLOCKED/WORKING），曾是 Manager 获取 Worker 状态的唯一来源
- **C-plane**：旧消息通道，被 v2 direct inbox 取代
- **mailbox/outbox → relay → mailbox/inbox**：旧消息中继路径
- **cursor / unread / mark-read**：旧消息消费状态跟踪

下列命令仅供尚未迁移的 Worker 过渡使用，不得用于新的 v2 工作流：`request`、`request-role`、`batch-request`、`event-emit`、`event-wait`、`mailbox-send`、`mailbox-check`、`mailbox-relay`、`manager-poll`。

兼容期若 v1 Worker 仍发事件，Manager 可审计事件并读取其旧 outbox，但不得把 v1 relay daemon 当成 v2 前提；迁移完成后由 v2 `status.json` + direct inbox 接管。
---

- 操作指南：`skill://tmux-agent-manager/OPERATIONS.md`
- Worker 协议：`skill://tmux-agent-worker/`
- 速查：`CHEATSHEET.md`
