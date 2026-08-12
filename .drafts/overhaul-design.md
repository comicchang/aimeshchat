# persist-oracle 彻底修复设计（zombie-overhaul oracle 2026-08-12）

> 供实现 subagent 读取。核心：控制面缺"Agent 能否接收 turn"权威状态。局部重建 Gateway↔插件↔Oracle 控制面 + 模型单一权威。不全量推倒。

## 一、三维状态正交（Gateway，不再单一 active）

Gateway 维护三维状态：

| 维度 | 状态 | 权威事件 |
|---|---|---|
| `presence` | alive / stale / dead | heartbeat、进程退出 |
| `binding` | pending / bound / lost | backend session 注册、解绑 |
| `agent_state` | agent_running / idle / ended | OMP lifecycle/registry 事件 |

路由条件（hot 投递）：
```
presence=alive AND binding=bound
AND agent_state ∈ {agent_running, idle, ended}
AND plugin capability 包含 park_revive + correlated_turn_ack
```
- agent_running：允许 steer
- idle：直接启动下一 turn
- ended：必须先 park-revive 再启动 turn
- binding=pending：禁止投递
- presence=stale/dead：禁止 hot 投递

状态归约：
```
session_ready → idle
agent_start / turn_start → agent_running
正常 agent_end → idle
session_shutdown / registry park / registry removed / process exit → ended
heartbeat → 只更新 presence
```
注意：不能只靠 agent_end 推断 parked/ended。若 OMP agent loop 结束后自动 park，须另上报 registry_parked 或 session_shutdown，否则 agent_end 归约为 idle。

## 二、FC-2 真正的 park-revive（插件）

插件与 Gateway 握手上报：
```
omp_agent_id, backend_session_id, generation,
capabilities: [park_revive_v1, correlated_turn_ack_v1]
```

投递链：
```
Gateway durable command
  → Plugin exact claim(command_id/msg_id)
  → ensureLive(exact omp_agent_id)
  → 校验返回 session 与 binding_epoch
  → session.prompt(body, {streamingBehavior: "steer"})
  → OMP turn_start
  → TURN_TRIGGERED(command_id, turn_id, generation)
  → Gateway 持久化 ack
  → runtime.send 返回成功
```
禁止把以下当成功：mailbox 文件写入、plugin claim、ensureLive 返回、sendUserMessage/prompt 未抛异常。

若 OMP 无稳定公开 ensureLiveAndPrompt API，先补 OMP 核心能力；不从 plugin 导入私有 lifecycle 冒充稳定契约。

## 三、runtime.send 持久命令状态机

状态：
```
QUEUED → CLAIMED → REVIVING(仅 ended/parked) → TRIGGERING → TURN_TRIGGERED
失败旁路 → FAILED_SAFE / AMBIGUOUS / TRIGGER_UNKNOWN
```
关联字段：request_id(幂等键), command_id, msg_id, turn_id, runtime_id, generation, backend_session_id, binding_epoch, payload_hash

返回语义：
| 状态 | 成功 | 含义 |
|---|---|---|
| mailbox_persisted | 否 | 只写入持久队列 |
| claimed | 否 | 插件已领取 |
| session_live | 否 | revive/binding 完成 |
| turn_triggered | 是 | OMP 已建立关联 turn |
| binding_pending | 否 | 未注入，允许稍后重试 |
| failed_safe | 否 | 明确未触发，可安全重试 |
| ambiguous | 否 | 可能已触发，禁止自动重投 |

幂等：同一 request_id+payload_hash 必须返回原 command/turn，不得重复注入；同 request_id 不同 payload 报 IDEMPOTENCY_CONFLICT。

不承诺虚假 exactly-once：TRIGGERING 崩溃 → AMBIGUOUS，走 transcript/native turn metadata reconcile。禁止：自动新 request_id、自动 warm/cold fallback、重执行相同 prompt、把 timeout 报成 success。

## 四、ControlStore（Gateway，SQLite WAL）

关键控制事务 synchronous=FULL。表：
```
reviews: review_key PK, swarm_session_id UNIQUE, runtime_id, profile_id, mailbox_agent_id
runtime_generations: runtime_id, current_generation, owner_nonce, presence, binding,
                     backend_session_id, binding_epoch, agent_state, last_state_seq
commands: request_id UNIQUE, command_id, msg_id, turn_id, runtime_id, generation, state...
```

## 五、模型单一权威链路（oracle.py）

- agent profile（agents/oracle*.md 的 model:）为唯一权威源，删除/弱化 retry.fallbackChains YAML 子集解析依赖
- 解析出的 primary chain[0] 落盘 manifest，revive/ask 不再重推导
- 未显式 agent 时行为明确（不静默回落 mimo）

## 实现顺序（P0 优先）

1. Gateway 三维状态 + ControlStore（service.py + 新 ControlStore）
2. runtime.send 持久命令状态机（gateway）
3. 插件 park-revive + turn ack（TS 插件）
4. 模型单一权威链路（oracle.py）
