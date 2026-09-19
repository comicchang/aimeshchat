---
name: oracle-consult
description: >
  Oracle 架构顾问咨询流程：用户要求 Oracle，或本地分析无法消歧且存在真实架构 trade-off 时，先加载本 skill 再调用顾问。覆盖上下文交接、实例复用、多 Oracle 协作、无限等待与追问；不用于本地 explore、格式审查或普通 task 派发。
---

# oracle-consult

## 1. 调用门

- 用户明确要求 Oracle：执行，不附加门槛。
- Agent 主动调用：必须同时满足：已完成本地分析并验证或证伪 2–3 个方向；问题是决策性 trade-off；不重复已有问题；上下文清单齐全。
- 未满足门槛就继续本地分析；Oracle 不用于 explore、格式审查或可在本地 5 分钟验证的问题。

## 2. 调用前上下文交接（7 项）

缺项先补，不让 Oracle 猜：
1. 项目概要：技术栈、部署、相关模块。
2. 问题：触发条件、实际值、期望值。
3. 代码证据：路径:行号、符号、调用链及已验证事实。
4. 已尝试方案：结果与失败原因。
5. 约束与风险：兼容性、权限、不能改的范围。
6. 未确认假设、历史决策、已排除方向（按需查 memory/history）。
7. 唯一决策问题与期望输出。

一次只问一个决策问题；未确认内容明确标为"假设"。

## 3. Role、实例复用与等待纪律

### Role 选择

| DO | DON'T |
|---|---|
| 显式指定 role（`oracle-gpt`、`oracle-opus`、`oracle-gemini`、`oracle-deepseek` 或 `oracle-glm`） | 未指定就默认某个 role |
| 未指定时先询问用户 | 自行猜模型或厂商 |
| 让部署配置决定 role 到 backend 的映射 | 在 skill 中猜测模型或厂商映射 |

### 实例复用

| DO | DON'T |
|---|---|
| 追加问题复用同一实例：运行中用 `hub send`，已结束先 revive | 每次追问都新起 task |
| running/刚 parked（<5min）→ `revive`；已 parked 很久（>10min 无响应）或 revive 后上下文丢失 → 起新实例并重发关键上下文 | 盲目 revive 不检查实例状态/运行时长；同 topic 同时起多个 Oracle |
| 多 Oracle 并行时，将先完成者的完整结论转发给尚未完成的每个实例 | 只交付某一个 Oracle 的孤立结论 |
| 切换 role/model 前先取得用户授权 | 自行切换 role/model |

### 等待与错误恢复

| DO | DON'T |
|---|---|
| 一次性发送完整上下文后用 `hub wait` 等待最终回答或 `agent_end`，不设 deadline | 设置 timeout/watchdog、调用 `hub cancel`/kill，或反复 `hub jobs`/`hub wait`/sleep N 秒轮询、发送催促消息 |
| 等待期间只做不依赖 Oracle 结果的并行工作；Oracle 返回后读取完整结果再行动 | 因等待时间长重发同题、切换 role/model、自行下结论，或抢先产出“临时版本” |
| 方向相关的多个问题 append 给同一 Oracle 实例（`hub send` 追加） | 为方向相关问题新起 Oracle agent，丢失上下文连续性 |
| transport/SSL 中断或卡住时 revive 同一实例并保留 key、role、上下文；revive 失败就报告 `BLOCKED` | 用 cold/new task 替代，或伪造结论、静默降级；取消/release/purge/结束咨询前不取得用户授权 |

> **硬规则**：违反本节任一 DON'T 即停止自行推进，按对应 DO 回退；Oracle 负责的工作不得由本 agent 代做。收到最终结果后记录 key、request_id、role 与结果来源。

## 4. Prompt 与交付边界

按“项目上下文｜唯一问题｜已知事实｜未确认假设｜已尝试方案｜约束｜期望输出”组织 prompt；期望输出包含 bottom line、action plan、effort、confidence、why（≤4 点）和 risks（≤3 点）。

Oracle 只提供建议，不实施改动；高风险建议须人工复核，并以可验证证据为准。上下文不足时先指出缺失的 1–3 项，不得脑补。Oracle 返回前，agent 不得把等待中的咨询结果包装成自己的结论。

## 5. 追问

沿用同一实例，不重发全部上下文，只发送增量证据或问题。可要求补证据、收敛方案、展开风险、反方审查或拆成最小 commit。默认同一 session 追问不超过 3–5 轮，除非用户要求继续。

## 6. `disable-model-invocation`

本 skill 面向“用户明确要求 Oracle”或“本地分析无法消歧且存在真实 trade-off”的场景，默认允许模型自动触发，因此 front matter 不应设置 `disable-model-invocation: true`；若部署需要仅用户手动触发，才设置为 `true`，并明确这会关闭上述自动触发路径。该字段只控制 skill 是否可被模型自动调用，不是 Oracle agent 的安全闸门。

无论字段取值，直接 `task(agent=oracle-*)` 都可能绕过本 skill。确定性执行必须在始终加载的 `system-prompt.md` 或全局 `AGENTS.md` 写入调用门、上下文交接、实例复用、等待和取消约束，并由 oracle/task wrapper 或 hook 做 preflight gate：缺 role、7 项上下文、实例复用检查、等待约束或取消约束任一失败，即拒绝创建或取消。项目 `AGENTS.md` 只能补充指针，不能替代全局 gate；若保留 `true`，关键硬规则不能只写“请读取 skill”。
