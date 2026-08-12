---
name: agent-swarm
description: Unified agent orchestration over the aimeshchat swarm/mailbox protocol — manager dispatch or worker execution, determined by role. Progressive disclosure: loads only the relevant role file.
---

# agent-swarm — Unified Orchestration Protocol

> Role-specific rules: `skill://agent-swarm/roles/manager.md` or `skill://agent-swarm/roles/worker.md` | Protocol reference: `skill://agent-swarm/protocol/mailbox.md`

# Architecture Authority

本协议的**唯一控制面**是 SwarmKernel + SessionManifest。mailbox CLI 仅是 leaf storage/transport primitive。
Manager 和 Worker 不得绕过 manifest/routing table 直接拼远端路径或依赖 bare mailbox 完成 lifecycle。

## 关键概念

|概念|定义|来源|
|---|---|---|
|**SessionManifest**|session 的不可变权威配置：session_id、manager、agents。`swarm create-session` 产生，session.json 是其持久化形式。manifest_hash 字段已在 model 声明但 CLI 暂未输出。|`swarm/kernel.py`, `swarm/model.py`|
|**AgentLocation**|agent_id → (host_alias, backend, capabilities, execution_mode?, mailbox_root?, return_mode?) 的路由条目。model.py 已声明可选字段，`_persist_routing` 已持久化；CLI register 暂未暴露这些参数。|`swarm/kernel.py`, `swarm/model.py`|
|**execution_mode**|Worker 的执行模型：`mailbox-worker`（远端 OMP Worker）或 `local-omp-mcp`（本地 OMP + 远端 omp-execd MCP）。互斥，session 创建时选定|本协议 §Execution Mode|

Manager 的唯一入口是 `swarm` 子命令（`aimeshchat swarm direct/poll/watch/status`）；bare `mailbox send` 仅用于 bootstrap 和故障诊断。
Worker 的唯一入口是 `mailbox read` + 两阶段消费；消息到达由 OMP plugin（只通知）或主动 polling 触发。

## Execution Mode

每个 Worker agent 在 SessionManifest 中声明 `execution_mode`，二者**互斥**：

|execution_mode|描述|适用场景|
|---|---|---|
|`mailbox-worker`|远端主机运行完整 OMP Worker 进程 + plugin，通过 mailbox 协议通信|双向 SSH、Worker host 有 omp 二进制|
|`local-omp-mcp`|所有 OMP agent/model 在 Manager 主机，远端仅 `omp-execd --stdio` MCP server|单向 SSH、Worker host 仅需 MCP executor|

**`local-omp-mcp` profile 不属于传统 agent-swarm Worker**——它不参与 mailbox INIT/TASK/REPORT 生命周期。
其任务通过命名 MCP workspace 工具分发，mailbox 仅作兼容/legacy。详见 `skill://agent-swarm/operations/remote.md`。

`mailbox-worker` 和 `local-omp-mcp` 不得在同一 session 内同时用于同一 agent_id。
`session-init` 必须校验 manifest 一致性，manager ID 冲突时拒绝创建（而非静默合并）。

## Role Determination

**Role comes ONLY from explicit role/manifest** — never inferred from env presence.

| Source (precedence) | Role |
|---|---|
| CLI `--role` flag | manager / worker / oracle |
| launcher `CODEAGENT_ROLE` env | manager / worker / oracle |
| session manifest (roster/manager) | manager ↔ worker |

`$OMP_WORKER_ID` unset does **NOT** default to Worker — role is undetermined → error.
Manager role = manifest `manager`; Worker role = roster member ≠ manager.

Read **one** role file based on your role:

| Role | Load |
|---|---|
| Manager | `skill://agent-swarm/roles/manager.md` |
| Worker | `skill://agent-swarm/roles/worker.md` |

## Shared Protocol Reference

The canonical mailbox protocol (message schema, status.json contract, two-phase consumption, CLI commands, error handling) lives in `skill://agent-swarm/protocol/mailbox.md`. Both roles reference it; neither duplicates its content.
## Deployment Modes

部署模式由**拓扑可达性**和 **execution_mode** 共同决定。

### 拓扑选择

```
所有 agent host 是否共享同一 MAILBOX_ROOT 文件系统？
  │
  ├─ 是 → Mode A: Shared FS
  │       - mailbox ops 是本地文件系统操作
  │       - transport 层不参与 mailbox 通信
  │
  └─ 否 → 需要跨主机 mailbox transport（SSH/relay）
          - Manager 必须通过 SessionManifest + SwarmKernel routing 操作
          - 禁止 Manager 直接猜 host path 或手工 `mailbox --host`
```

| 拓扑 | MAILBOX_ROOT | 通信方式 |
|---|---|---|
| Mode A (Shared FS) | 显式 `MAILBOX_ROOT=.mailbox` | 本地 FS |
| 跨主机（无共享 FS） | 默认 `resolve_root()` | `aimeshchat swarm` + SwarmKernel transport |

### 回程模式 (return_mode)

跨主机拓扑下，**Worker→Manager 的回程路径**由 Manager host 对 Worker host 的可达性决定：

| return_mode | 拓扑要求 | Manager 行为 |
|---|---|---|
| `manager-pull` (默认，推荐) | 仅 Manager→Worker SSH 可达 | Worker 写 **host-local** manager inbox；Manager 定期 `aimeshchat mailbox read --host <H>` 从远端 host 的 manager inbox 拉取 |

**单向上必须使用 `manager-pull`**。Worker 不得尝试反向 SSH 或通过 pane/send-keys 伪造回程。

### MAILBOX_ROOT Consistency

**这是最常见的模式混合错误。** 以下规则适用于所有模式：

1. **Mode A**: 所有参与者必须显式设置 `MAILBOX_ROOT=.mailbox`（env 或 `--mailbox-root`）
2. **跨主机**: Manager host 使用默认 `resolve_root()`；远端 `mailbox_root` 由 SessionManifest 声明
3. **永远不要**在 Mode A 中省略 `MAILBOX_ROOT`——CLI 默认值会指向不同路径
4. **永远不要**在跨主机模式中假设所有主机共享同一 `MAILBOX_ROOT` 路径

### Mode-Mixing Audit Checklist

在编辑任何 mailbox 或 swarm 命令前，检查：
- [ ] 命令是否通过 `swarm direct/poll` 还是 bare `mailbox`？（跨主机必须走 swarm）
- [ ] `return_mode` 是否匹配拓扑？（单向必须 `manager-pull`）
- [ ] `execution_mode` 是否已声明且不冲突？
- [ ] `send-keys` 是否仅用于本地 Worker 的 INIT check prompt？（远程不可用）

## Gateway 跨设备运行时（v2 控制面）

跨设备 Agent runtime 的权威控制面是**每设备本地 Gateway**（UDS）+ Manager 主动 SSH stdio：
远端永不反向连接 Manager，也不开放 HTTP/TCP 端口。

```
Manager(Mac)                          Remote(OA-MIANYIN-2)
  │ aimeshchat gateway ensure --host OA-MIANYIN-2
  │   ├─ wire v2 probe（REMOTE_UPGRADE_REQUIRED if <2）
  │   ├─ tmux/架构预检（缺 OMP 只关对应 capability）
  │   └─ 远端 gateway 启动（私有 tmux socket）
  │
  │ SSH ControlMaster 单次 `aimeshchat gateway rpc --stdio`（有界控制）
  ├── session.ensure   → 远端缓存只读 manifest（manager/roster/version）
  ├── runtime.spawn    → 远端 tmux supervisor 拉起 Agent（argv 无 shell）
  └── runtime.send     → 消息进远端 inbox → 插件唤醒（steer/nextTurn）
  │
  │ SSHStream（长期流，复合 cursor，断线补流）
  └── mailbox + RuntimeEvent 从远端流回 Manager → EventStore.ingest_remote
```

### 跨设备 TASK 生命周期

```
Manager 发送 --require-ack TASK
  → DELIVERED（outbox 标记）
  → READ（远端插件 claim → MailboxService 生成 READ receipt 回流）
  → RUNNING（首条 PROGRESS → RequestLedger）
  → PROGRESS…（assistant/tool 事件 → EventStore）
  → DONE（REPORT + artifact verify 通过 → terminal CAS）
```

- 任务终态以 mailbox RequestLedger 为准；runtime 只由显式 `runtime stop` / `oracle release` 终止。
- 断线期间消息进 DeliveryEngine durable outbox；重连后从最后 cursor 补流，去重消费。
- `aimeshchat events watch --session <id> --cursor <c> --jsonl` 观察事件流（只控制观察连接 timeout）。

### 跨设备写作流程

1. Manager 将文档分区建成**互不重叠**的 request，分别 dispatch 给不同远端 Worker。
2. TASK/REPORT 的 `body` 使用固定 JSON：
   ```json
   {"base_revision": "...", "target_path": "...", "artifact_id": "..."}
   ```
   artifact 仍按 AttachmentRef + sha256 验证。
3. 仅 Manager 的**单一 merge request** 串行应用——Worker 不得同时修改 Manager working tree。
4. 相同目标路径不同 hash 的并发 REPORT → `PROTOCOL_CONFLICT`，不覆盖既有 artifact。
5. QUESTION/RESPONSE 使用 reply_to；重要消息设置 `require_ack=true`。


## Shared Invariants

These rules apply to **every** agent regardless of role:

1. **Evidence honesty** — insufficient evidence → `[EVIDENCE PENDING]` or `[INFERENCE: reason]`. Never fabricate.
2. **msg_id correlation** — every reply references the original `msg_id` via `--reply-to`. Never reuse or overwrite a sent message.
3. **Never fabricate** — do not invent protocol fields, status values, inbox paths, or worker IDs. Verify from INIT, session.json, or workers.toml.
4. **CLI-only writes** — all mailbox and status.json mutations go through the standalone `mailbox` CLI. Never hand-write JSON.
5. **No capture-pane** — do not use terminal text to infer agent state. Use status.json and inbox polling.
6. **Two-phase consumption** — `mailbox read` (inbox→processing) → process → `mailbox finalize` (processing→archive). No shortcuts.
7. **status.json is a snapshot** — five fields only. Full conclusions belong in REPORT messages and artifacts, not in status.
8. **Lifecycle vs mailbox status 正交** — `mailbox status` 仅描述工作状态（IDLE/BUSY/DONE/BLOCKED）。Park 是独立的 lifecycle 概念（由 `aimeshchat park registry` 管理），不在 status.json 表达。Park 期间 agent 保持 IDLE 且 archive 受保护（禁止 `mailbox clear`）。

## Initialization Flow

1. **Determine role** from explicit role/manifest (`CODEAGENT_ROLE` / session manifest / CLI `--role`) — never infer from env presence.
2. **Load your role file** — `roles/manager.md` or `roles/worker.md`.
3. **Read the protocol** — `protocol/mailbox.md` for the canonical CLI schema and state machine.
4. **Load deployment mode** — `operations/local.md` or `operations/remote.md` based on session topology.
5. **跨设备 runtime** — Manager `aimeshchat gateway ensure --host <H>` 预检 + 启动远端 gateway（见 §Gateway 跨设备运行时）。
6. **Follow role-specific initialization** — Manager self-init or Worker INIT handshake.

## CLI Resolution Order

跨主机通信的权威入口是 `aimeshchat swarm` 子命令 + `aimeshchat gateway` 控制面，而非 bare `mailbox`：

1. `aimeshchat gateway ensure/start/status/rpc` — 每设备本地控制面（session.ensure/runtime.spawn/register/send/stop/events.list）
2. `aimeshchat swarm direct/poll/watch/status` — SessionManifest-aware routing + delivery（`poll`/`status` 为 local-only，跨主机聚合由 manager pull / gateway events 补足）
3. `aimeshchat mailbox ... --host <H>` — 跨主机 leaf transport primitive（read/peek/stats/send/status 均可通过 SSH 路由到远端 host 的本地 mailbox CLI）
4. PATH command `mailbox` — 本地 FS mailbox 操作

跨主机 manager-pull 回程：Manager 使用 `aimeshchat mailbox read --session <id> --agent manager --owner manager --host <H>` 从远端 host 的 manager inbox 拉取 REPORT。
禁止 `scripts/tmux_worker.py` 或其他 legacy wrapper。

## Protocol Version

本协议 active design version: **v2 (session-based)**。
同目录内 v1 legacy 命令仅用于 unmigrated Worker；OMP Remote v2 (`omp-execd` MCP) 属于独立架构，
不继承本协议的 mailbox lifecycle。详见 §Execution Mode。

## Legacy (v1)

v1 concepts deprecated by this protocol:

- **control envelope** (A-plane) → replaced by `mailbox read`
- **B-plane** (event-emit) → replaced by `status.json`
- **mailbox/outbox → relay → mailbox/inbox** → replaced by direct inbox
- **cursor / unread / mark-read** → replaced by `mailbox read` / `mailbox finalize` two-phase

Legacy commands (`request`, `request-role`, `batch-request`, `event-emit`, `event-wait`, `mailbox-send`, `mailbox-check`, `mailbox-relay`) are for unmigrated Workers only.
跨主机 manager pull 使用 `aimeshchat mailbox ... --host <H>`（非 legacy，是当前唯一可用的跨主机 mailbox 传输原语），详见 `roles/manager.md`。
