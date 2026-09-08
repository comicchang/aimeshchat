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
aimeshchat oracle wait "$KEY" --timeout 300

# 事件观测（不是完成信号）：cursor-resumable 事件流
aimeshchat oracle watch "$KEY"

# 结果
aimeshchat oracle result "$KEY"          # JSON
aimeshchat oracle result "$KEY" --raw    # 纯文本

# 复活/附着/终止
aimeshchat oracle revive "$KEY" [--mode bg|pane|resume]
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
- 完成等待请用 `oracle wait "$KEY" --timeout 300`（阻塞到新产出或 agent_end，内联打印最终文本）。

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
