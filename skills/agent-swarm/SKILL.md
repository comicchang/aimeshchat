---
name: agent-swarm
description: Swarm/mailbox 协议编排。触发：aimeshchat swarm 子命令、manager/worker 角色声明、跨主机 mailbox 通信。不触发：OMP task()、本地 subagent、hub 协作。
disable-model-invocation: true
---

# agent-swarm — Unified Orchestration Protocol

> Role-specific rules: `skill://agent-swarm/roles/manager.md` or `skill://agent-swarm/roles/worker.md` | Protocol reference: `skill://agent-swarm/protocol/mailbox.md`
## §1 触发与执行契约

触发：使用 `aimeshchat swarm`、声明 manager/worker、或跨主机 mailbox 通信。不触发：同一 OMP 进程内的 `task()`、本地 subagent、`hub` 协作。

| DO | DON'T |
|---|---|
| 先读取本文件，再按角色加载 `roles/manager.md` 或 `roles/worker.md`，并按拓扑加载 `operations/local.md`/`remote.md` | 未判定角色、拓扑或 execution mode 就派发任务 |
| 以 SessionManifest/SwarmKernel 为控制面，使用 `swarm` 路由；用 protocol 文档核对消息状态 | 直接拼远端路径、绕过 manifest，或用 hub 跨进程投递 |
| 用 `mailbox read → process → finalize` 两阶段消费，并以匹配 `request_id` 的 REPORT 作为完成证据 | 以 status.json、capture-pane 或 send-keys 推断完成 |
| 遇证据不足标记 `[EVIDENCE PENDING]`/`BLOCKED`，并报告缺失证据 | 猜测 worker、字段、路径或静默降级 |

## §2 导航

角色细则：`skill://agent-swarm/roles/manager.md`、`skill://agent-swarm/roles/worker.md`；消息协议：`skill://agent-swarm/protocol/mailbox.md`；跨主机 CLI：`skill://aimeshchat-cli/`；持久顾问：`skill://persist-oracle/`。

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

## Interactive-Peer Mode

共享 FS（Mode A）下**手工多开对等 omp 实例**的协作形态。两个实例都由用户手工启动，
没有 launcher 注入身份——必须先 attach 再开工。

### 标准流程

```bash
# ① 编排者建 session 并注册 roster（幂等容错）
aimeshchat swarm create-session <sid> --manager manager --members manager,w1,w2

# ② 每个 peer 启动 omp 之前生成并 source 身份（缺此步骤 = 聋哑节点）
python3 scripts/swarm_attach.py <sid> --agent w1 --out /tmp/attach-w1.sh
source /tmp/attach-w1.sh && omp        # 身份 env 随进程注入，插件轮询激活
```

### 聋哑节点禁令

**无身份 env 的实例视为聋哑节点：禁止承担 worker 角色、禁止派发 TASK 给它。**
判据：`echo $OMP_MAILBOX_IDENTITY_FILE` 为空 ⇒ 该实例收不到任何 mailbox 唤醒。
旁证：session 全程零次 `skill-prompt`/`irc:incoming` 注入。

### 消费与等待纪律（Interactive-Peer 同样适用）

- 启动后第一动作：drain-inbox（`mailbox read` → 处理 → `finalize`）清掉历史积压再开工。
- read 后回合被打断 → processing 租约悬挂；恢复后第一动作 = finalize 该 request
  或执行 `aimeshchat mailbox recover-stale`。
- 其余 Dispatch Gate / 等待纪律 / 关轮护栏 / 单写者锁见对应角色文件。

## Role Determination

**Role comes ONLY from explicit role/manifest** — never inferred from env presence.

| Source (precedence) | Role |
|---|---|
| launcher `CODEAGENT_ROLE` env | manager / worker / oracle |
| worker launcher `OMP_WORKER_ID` env（gateway spawn 注入） | worker / oracle |
| session manifest (roster/manager) | manager ↔ worker |

`$OMP_WORKER_ID` unset does **NOT** default to Worker — role is undetermined → error.
Manager role = manifest `manager`; Worker role = roster member ≠ manager.

**Oracle/顾问任务分派必须显式指定 agent role**；role 到 runtime/backend 的映射由部署配置负责，skill 不列出具体模型、厂商或价格。未指定 role 时先向用户询问，禁止 Manager 自行选择默认顾问。

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

## §5 远程运行时参考

跨设备 Gateway、TASK 生命周期、写作边界和 v1/v2 差异：详见 `references/remote-operations.md`；完整协议细节见 `operations/remote.md`。


## §6 Shared invariants and initialization

| DO | DON'T |
|---|---|
| Verify INIT/session.json/workers.toml; correlate replies with `msg_id`/`--reply-to`; mark weak evidence `[EVIDENCE PENDING]` or `[INFERENCE]` | Fabricate fields, IDs, paths, or conclusions |
| Mutate mailbox/status only through CLI; consume `read → process → finalize`; use status/inbox/REPORT evidence | Hand-write JSON, use capture-pane, or skip finalize |
| Treat status.json as a five-field availability snapshot; use RequestLedger/REPORT for terminal state; keep Park lifecycle separate | Treat `DONE`/file presence as final proof or clear Park-protected archive |
| Use `aimeshchat mailbox/swarm` across processes; load role, protocol and topology before work | Use hub or filesystem side channels across OMP instances |

**Initialization:** (1) determine explicit role/manifest; (2) load role file; (3) read `protocol/mailbox.md`; (4) load `operations/local.md` or `operations/remote.md`; (5) Manager ensures Gateway when needed; (6) complete Manager/Worker handshake.

**CLI resolution:** cross-host use `gateway` control plane → `swarm` routing → `mailbox --host`; local shared FS may use PATH `mailbox`. Current protocol is v2; v1 commands are only for unmigrated Workers. Legacy details and remote lifecycle: `references/remote-operations.md` and `operations/remote.md`.
