---
name: oracle-consult
description: Oracle 架构顾问咨询。触发：用户要求 Oracle、存在不可本地消歧的架构 trade-off。不触发：本地 explore、格式审查、普通 task 派发。
disable-model-invocation: false
---

# oracle-consult

## 1. 调用门

- 用户明确要求 Oracle：执行，不附加门槛。
- Agent 主动调用：必须同时满足：已完成本地分析并验证/证伪 2–3 个方向；问题是决策/trade-off；不重复已有问题；上下文清单齐全。
- 未满足门槛就继续本地分析；Oracle 不用于 explore、格式审查或可在本地 5 分钟验证的问题。

## 2. 调用前上下文交接（7 项，缺项先补，不让 Oracle 猜）

1. 项目概要：技术栈、部署、相关模块
2. 问题：触发条件、实际值、期望值
3. 代码证据：路径:行号、符号、调用链及已验证事实
4. 已尝试方案：结果与失败原因
5. 约束与风险：兼容性、权限、不能改的范围
6. 未确认假设、历史决策、已排除方向（按需查 memory/history）
7. 唯一决策问题与期望输出

一次只问一个决策问题；未确认内容明确标为假设。

## 3. Role、实例复用与并行协作

- 必须显式指定 role：oracle-gpt / oracle-opus / oracle-gemini / oracle-deepseek / oracle-glm；未指定先问用户。role→backend 由部署配置决定，本 skill 不猜模型/厂商。
- **复用现有实例**：先查当前运行及已结束的 Oracle。相关追加问题必须复用同一实例：运行中优先 `hub send`，已结束先 revive 再 send；不得新起 task。
- 只有用户明确要求换 Oracle 或问题完全无关才新建实例；切换 role/model 必须用户授权。
- **多 Oracle 并行协作**：用户明确要求并行咨询多个 Oracle 时，分别记录实例、role、key、状态；任一 Oracle 完成后，立即将其完整结论转发给尚未完成且相关的 Oracle（`hub send`），要求后者纳入并综合。最终只交付综合结论。

## 4. 等待与生命周期（硬规则）

- `hub wait` **必须无 deadline**；不得设置 job timeout/watchdog，不得 `hub cancel`/kill Oracle。
- progress、通知、session_live 不是完成；只有最终回答/`agent_end` 才算完成。
- 等待期间只能做不依赖结果的并行工作；不得因慢而重发同题、换 role/model 或自行下结论。
- 上游/transport 中断：**revive 同一实例**（保留 key、role、上下文），然后重新无限等待；禁止 cold/new task 替代。
- 配额不足、revive 失败或无法继续：报告 `BLOCKED`，说明原因并保留可恢复状态；不得伪造结论或静默降级。
- 取消、release、purge、GC 或结束咨询，均须用户明确授权。

## 5. Prompt 与交付边界

用「项目上下文｜唯一问题｜已知事实｜未确认假设｜已尝试方案｜约束｜期望输出」组织 prompt。期望输出：bottom line、action plan、effort、confidence、why（≤4 点）、risks（≤3 点）。

Oracle 只提供建议，不实施改动；高风险建议须人工复核。上下文不足时先指出缺失的 1–3 项，不得脑补。收到最终结果后记录 key、request_id、role、来源。

## 6. 追问

沿用同一实例，不重发全部上下文；只发送增量证据/问题。可要求补证据、收敛方案、展开风险、反方审查或拆成最小 commit。默认同一 session 追问不超过 3–5 轮，除非用户要求继续。
