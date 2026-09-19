---
name: oracle-consult
description: Oracle 架构顾问咨询。触发：用户要求 Oracle、存在不可本地消歧的架构 trade-off。不触发：本地 explore、格式审查、普通 task 派发。
disable-model-invocation: false
---

# oracle-consult

## 1. 调用门

- 用户明确要求 Oracle：执行，不附加门槛。
- Agent 主动调用：仅当 (a) 已完成本地分析并验证/证伪 2–3 个方向，(b) 问题是决策/trade-off，(c) 不重复已有问题，(d) 上下文清单齐全。
- 未满足门槛：继续本地分析；不要把 Oracle 当 explore、格式审查或低价值验证器。

## 2. 调用前交接（缺项先补，不让 Oracle 猜）

必须提供：
1. 项目/技术栈/部署与相关模块
2. 问题、触发条件、实际值与期望值
3. 代码证据（路径:行号、符号、调用链）与已验证事实
4. 已尝试方案及结果/失败原因
5. 约束、兼容性、权限、风险与不能改的范围
6. 未确认假设、历史决策/已排除方向（按需查 memory/history）
7. 唯一决策问题与期望输出

一次只问一个决策问题；未确认内容明确标为假设。

## 3. Role 与实例

- 必须显式指定 role：oracle-gpt / oracle-opus / oracle-gemini / oracle-deepseek / oracle-glm；未指定先问用户。
- **复用现有实例**：先查当前运行/已结束的 Oracle。相关追加问题必须复用同一实例：优先 `hub send`；已结束先 revive 再 send。不要新起 task。
- 只有用户明确要求换 Oracle 或问题完全无关才新建实例；切换 role/model 必须用户授权。

## 4. 等待与生命周期（硬规则）

- `hub wait` **无 deadline**；不得设置 job timeout/watchdog，不得 `hub cancel`/kill。
- progress、通知、session_live 不是完成；只有最终回答/agent_end 才算完成。
- 等待期间只做不依赖结果的并行工作；不因慢而重发、换 role/model 或自行下结论。
- 上游/transport 中断：**revive 同一实例**（保留 key、role、上下文），然后重新无限等待。
- revive 失败：报告 `BLOCKED`、保留可恢复状态；不 cold/new 替代、不伪造结论。
- 取消/release/清理仅用户明确授权。

## 5. Prompt 与交付

使用：项目上下文｜唯一问题｜已知事实｜未确认假设｜已尝试方案｜约束｜期望输出（bottom line、action plan、effort、confidence、why、risks）。

Oracle 只提供建议，不实施改动；高风险建议须人工复核。收到最终结果后记录 key、request_id、role、来源，再向用户交付。
