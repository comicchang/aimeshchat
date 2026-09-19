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

## 3. Role、实例复用与多 Oracle 协作

- 必须显式指定 role：`oracle-gpt`、`oracle-opus`、`oracle-gemini`、`oracle-deepseek` 或 `oracle-glm`；未指定先询问用户。role 到 backend 的映射由部署配置决定，本 skill 不猜模型或厂商。
- 调用前先检查当前运行及已结束的 Oracle。相关追加问题必须复用同一实例：运行中优先 `hub send`，已结束先 revive 同一实例再 send；不得每次新起 task，也不重复发送完整上下文。
- 只有用户明确要求换 Oracle，或问题完全无关，才新建实例；切换 role/model 必须用户明确授权，禁止自行切换。
- 用户明确要求同时咨询多个 Oracle 时，分别记录实例、role、key、状态。Oracle 完成有先后：先完成者的完整结论必须立即转发给尚未完成的每个 Oracle（用 `hub send`），要求后者纳入并综合分析。最终只交付综合结论，不交付单个 Oracle 的孤立结论。

## 4. 等待与生命周期（硬规则）

- `hub wait` 必须无 deadline；不得设置 job timeout 或 watchdog，不得 `hub cancel` 或 kill Oracle。
- progress、通知、`session_live` 都不是完成信号；只有最终回答或 `agent_end` 才算完成。
- 等待期间只能做不依赖结果的并行工作；不得因等待时间长而重发同题、换 role/model 或自行下结论。
- 上游或 transport 中断：revive 同一实例，保留 key、role、上下文，然后重新无限等待；禁止用 cold/new task 替代。
- Oracle 配额不足、revive 失败或无法继续：报告 `BLOCKED`，说明原因并保留可恢复状态；不得伪造结论或静默降级。
- 取消、release、purge、GC 或结束咨询均须用户明确授权。收到最终结果后记录 key、request_id、role 与结果来源。

## 5. Prompt 与交付边界

按"项目上下文｜唯一问题｜已知事实｜未确认假设｜已尝试方案｜约束｜期望输出"组织 prompt；期望输出包含 bottom line、action plan、effort、confidence、why（≤4 点）和 risks（≤3 点）。

Oracle 只提供建议，不实施改动；高风险建议须人工复核，并以可验证证据为准。上下文不足时先指出缺失的 1–3 项，不得脑补。

## 6. 追问

沿用同一实例，不重发全部上下文，只发送增量证据或问题。可要求补证据、收敛方案、展开风险、反方审查或拆成最小 commit。默认同一 session 追问不超过 3–5 轮，除非用户要求继续。

## 7. `disable-model-invocation`

Oracle 需由 agent 自主触发时删除该字段（默认 false）；`true` 仅适合用户手动触发。无论取值，直接 `task(agent=oracle-*)` 都可能绕过 skill；确定性执行必须在始终加载的 `system-prompt.md` 或全局 `AGENTS.md` 写入上述门槛，并由 oracle/task wrapper 或 hook 做 preflight gate：缺 role、7 项上下文、实例复用检查、等待约束或取消约束任一失败，即拒绝创建或取消。

项目 `AGENTS.md` 只能补充指针，不能替代全局 gate。若暂保留 `true`，必须把关键硬规则同步写入 system prompt，不能只写"请读取 skill"。
