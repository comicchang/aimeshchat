---
name: persist-oracle
description: 持久化多轮顾问 review — 保留上下文。仅用 aimeshchat oracle start/ask/status/list/watch/wait/result/revive/attach/release/doctor/gc。仅在用户明确说「persist-oracle」「持久化这个 review」时使用；默认咨询走 oracle-consult 的 task 直接调用。
---

# persist-oracle — 持久化多轮 Oracle Review

> **使用时机**：仅在用户明确说「persist-oracle」「持久化这个 review」「用 persist-oracle 工具」时使用。
> 默认的 oracle 咨询走 `oracle-consult` skill 的 `task` 直接调用，不走本 skill。
>
> role 选择、提问模板与追问技巧 → 见 `oracle-consult` skill。

## 前提：显式指定 agent role

启动持久 review 前**必须明确**使用哪个 agent role；未指定时先问用户。

| Agent role | 适用场景 |
|------------|----------|
| oracle-gpt | 架构 trade-off、根因分析、风险评审 |
| oracle-opus | 复杂架构决策、根因分析 |
| oracle-gemini | 长上下文、多模态相关咨询 |
| oracle-deepseek / oracle-glm | 中文分析、工具调用密集任务 |

> role 到 runtime/backend 的映射由部署方权威配置决定；本 skill 不硬编码具体模型、厂商或价格。

## 默认行为：复用已有 Oracle

- Oracle 会话以 KEY 为唯一标识：`<project>:advisor:<domain>:<topic>[:<role_suffix>]`；`role_suffix` 由部署配置定义。
- `oracle start "$KEY"` 幂等：KEY 已存在时**复用已有 review/session/runtime**，不重复创建。
- `oracle ask "$KEY"` 一律在已有 backend session 上热投递（hot in-loop，不新开进程）；runtime 未存活时自动走 warm/cold 降级。
- 多轮 review **复用同一实例** → 上下文完整保留。同一话题不要换新 KEY 重开。
- 换话题 / 重置上下文 → 用新 KEY，或先 `oracle release "$KEY"` 再 `start`。

## task 工具使用说明（默认咨询路径）

默认的 oracle 咨询不经过本 skill 的 CLI，而是用 `task` 工具直接调用（见 `oracle-consult`）：

- 运行时没有独立 role 参数时，必须由调用方通过 runtime context 或部署配置提供 role；省略 role 仅在该上下文明确存在时允许。
- 首轮 `oracle start` 使用当前 runtime role context；本 skill 不在命令中写入具体模型或 provider。
- 追问复用同一 task 实例（见 `oracle-consult` 的「追问」），不重新 spawn。

## CLI 契约

```
KEY='<project>:advisor:<domain>:<topic>[:<role_suffix>]'

# 首轮：新建 review/session/runtime；role mapping 必须已由 runtime context 或部署配置提供
aimeshchat oracle start "$KEY" --variant reasoning --system "..." --prompt '初始问题'

# 追加/追问：hot in-loop send（同 backend session，不新开进程）
aimeshchat oracle ask "$KEY" '追加信息'
aimeshchat oracle ask "$KEY" '首轮后追加' --wait-binding

# 状态/列表
aimeshchat oracle status "$KEY"
aimeshchat oracle list

# 完成等待（主命令）：阻塞到新产出或 agent_end，内联打印最终文本
aimeshchat oracle wait "$KEY"

# 事件观测（不是完成信号）：cursor-resumable 事件流
aimeshchat oracle watch "$KEY"

# 结果
aimeshchat oracle result "$KEY"          # JSON
aimeshchat oracle result "$KEY" --raw    # 纯文本

# 复活/附着/终止
aimeshchat oracle revive "$KEY" [--mode bg|resume]
aimeshchat oracle attach "$KEY" '问题'
aimeshchat oracle release "$KEY" [--purge]

# 一致性检查（gateway ↔ park ↔ 会话记录 三源校验）
aimeshchat oracle doctor [--fix]

# 清理过期 released session（--dry-run 先看再删）
aimeshchat oracle gc [--dry-run] [--json]
```

## watch：事件观测（不是完成信号）

`oracle watch` 只用于观测运行事件（cursor-resumable 事件流），**不是完成等待**：

- 默认持续输出事件流；`--exit-on <KIND[.STATE]>` 命中首个匹配事件即退出 0。
- ⚠️ **首个 progress 即返回，不是完成信号**——只说明「有新产出」，完整回答要等 `oracle wait` / `oracle result`。
- 完成等待必须用 `oracle wait "$KEY"`（不设置有限等待参数，默认 0 = unlimited；内联打印最终文本）。

## 无限等待与无终端规则

- Oracle review 的思考时间可能超过 300 秒；正常 review 必须保持 `oracle wait "$KEY"` 运行到新产出或 `agent_end`。
- Oracle CLI 及其编排 wrapper 不得设置任何 `timeout`、命令超时或 `pty`；使用非交互进程并保持 stdout/stderr 可读。
- 外层编排器需要观察时，只查看状态或日志，不终止 wait、不把观察截止时间改写为 Oracle 失败。

## Role binding（CLI 语义）

- role 到 runtime/backend 的映射来自调用方上下文或部署配置。
- 本 skill 不硬编码具体模型名称、厂商或价格。
- 调用方显式传入的运行参数优先于默认上下文；缺少有效 role mapping 时必须报错，不静默猜测。
- `--agent` 仅保留兼容语义，不作为 skill 内的具体模型选择入口。

## 绑定语义（A1）

`oracle start` 同步轮询 backend session id ≤120s；慢启动 oracle 超时则返回
`binding=pending`，**runtime 保持存活**。backend_session_id 后续异步回写。
status 显示 `binding: pending` 属正常。`ask` 在绑定未完成时默认快速失败并提示
`--wait-binding`（阻塞到绑定完成后再投递）。

## 投递状态表（ask receipt 的 status 语义）

`oracle ask` 返回的 `status` 是**投递状态**，不是回答完成信号：

| status | 含义 | 允许的下一步 |
|--------|------|--------------|
| `mailbox_persisted` | 仅写入持久队列（state=QUEUED），**只证明入队**，未证明 backend turn | 用 `oracle wait` / `oracle result` 轮询；不得当作已回答 |
| `binding_pending` / `claimed` / `session_live` | 命令推进中（待绑定 / 已领取 / 会话恢复） | 继续等待或稍后重试 ask（binding_pending 可安全重投） |
| `turn_triggered` | OMP 已建立关联 turn——**唯一证明 backend 开始处理** | `oracle wait` 直到产出，然后 `oracle result` |
| `failed_safe` | 明确未触发（未投递） | 可安全原样重发 |
| `ambiguous` | 可能已触发，无法确认 | **禁止自动重发**；先 `oracle status` 或人工确认 |

**最新 ask 锚点（receipt/freshness contract）**：

- 每次 ask 成功入队后，CLI 把 `last_ask_at` / `last_request_id` / `last_command_state` / `last_generation` 写入 review meta，并在 ask 输出中原样回显 gateway receipt 的 `request_id` / `command_id` / `state` / `generation` / `detail`。调用方必须记录返回的 `request_id` 与 `generation`。
- `oracle result` / `oracle wait` **只承认 latest ask 之后**的输出：anchor 之前的 transcript 消息、旧 mailbox REPORT 与旧会话文件一律排除（返回 `no_result` 或继续等待），绝不会把上一轮的旧回答冒充本轮结果。
- 输出 `meta.last_ask`（result）/ `last_ask`（status）暴露当前锚点；`state` 字段语义见上表。

## 异步 reply 通知

- `oracle ask` 返回 receipt 后即可结束当前命令；持久化队列中的 Oracle `REPORT` 到达 manager inbox 后，启用的 manager-mode `omp-mailbox-plugin` 会发送一次关联通知并唤醒 OMP turn。
- 通知只携带 `request_id` / `generation` / `msg_id` 等关联元数据，不消费或 finalize mailbox 消息；它是唤醒提示，不是回答完成或 freshness 证明。
- 收到通知后必须运行 `aimeshchat oracle result "$KEY"`，按 `last_ask`、`request_id`、`generation` 校验本轮结果；没有插件通知时仍可直接使用 `oracle result` 轮询。

## 顾问双模式

- **模式 A：CLI 主会话**（`aimeshchat oracle start/ask/result/wait`）
- **模式 B：agent-swarm worker**（mailbox REPORT 回报模式，worker 身份由 CLI 自动注入）

两者不冲突，取决于会话是否在 swarm 编排内。

## 降级策略（Hot→Warm→Cold）

1. **Hot**（同进程）：复用已存活 runtime 的 backend session，上下文完整
2. **Warm**（原生 session 续接）：复用已落盘的 backend session id 恢复
3. **Cold**（新实例 + 快照）：`oracle revive` 自动走 cold 路径重建

由 CLI 自动选择；每步降级显式报告用户。`ask` 成功 JSON 含 `adopted` 字段。

## 触发条件

> **前提**：以下命令仅在已进入持久 review 会话时使用。
> 首次咨询走 `oracle-consult` 的 `task` 直接调用，不走本表。

| 用户说 | 行为 |
|--------|------|
| "persist-oracle" / "持久化这个 review" / "用 persist-oracle 工具" | `oracle start`（进入持久会话） |
| （已在持久会话内）"追加信息" / "追问" / 新证据 | `oracle ask` |
| （已在持久会话内）"oracle 现在怎么样" | `oracle status` |
| （已在持久会话内）"有哪些进行中的 oracle review" | `oracle list` |
| （已在持久会话内）"oracle 回答了什么" / "取结果" | `oracle wait` / `oracle result` |
| （已在持久会话内）"唤醒已释放的 review" | `oracle revive` / `oracle attach` |
| （已在持久会话内）"结束 review" / "释放" | `oracle release` |

## GC 自动清理

- `oracle gc`：清理过期 released session（hard_expires_at 过期 或 last_activity_at > 2天）
- 自动触发于 `oracle start` / `oracle list` / `oracle status`（24h 节流）
- session 隔离：oracle session 按 review key 隔离存储，互不串扰

## 输出过滤禁令

**调用 aimeshchat oracle 命令时，禁止使用提前退出的管道（`| head`/`| tail`/`| grep`）过滤输出。**

原因：
- oracle 命令的输出可能包含关键状态信息（如 runtime ID、session ID）
- 提前退出消费者会触发 SIGPIPE 杀死命令

允许的管道：
```bash
# ✓ 结构化转换
aimeshchat oracle result "$KEY" | python3 -c "import sys,json; ..."
aimeshchat oracle gc --json | jq '.cleaned'
```

禁止的管道：
```bash
# ✗ 提前退出过滤
aimeshchat oracle status "$KEY" | grep "runtime_id"
aimeshchat oracle list | head -5
```
