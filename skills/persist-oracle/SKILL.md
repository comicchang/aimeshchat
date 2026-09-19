---
name: persist-oracle
description: 持久化多轮 Oracle review。触发：用户明确说「persist-oracle」或「持久化 review」。不触发：单次咨询、普通 task 派发。
disable-model-invocation: true
---

# persist-oracle — 持久化多轮 Oracle Review
## §1 触发、边界与 DO/DON'T

触发：用户明确说 `persist-oracle`、持久化 review 或使用 persist-oracle 工具。不触发：单次咨询、普通 task 派发；后者见 `skill://oracle-consult/`。

| DO | DON'T |
|---|---|
| 首轮确认 role、KEY、项目/主题边界，再执行 `oracle start` | 未指定 role 时猜模型，或用同一 KEY 混入不同主题 |
| 同一主题复用 KEY 和 backend session，追加问题使用 `oracle ask` | 每轮新建实例、把 `mailbox_persisted` 当作回答完成 |
| 用 `oracle wait`/`result` 等最终产出，按 latest ask 的 request_id/generation 校验 freshness | 用 watch/progress 当完成信号、旧 transcript 冒充本轮结果 |
| 失败时 revive 同一实例；无法恢复就明确 `BLOCKED` | 静默 cold 降级、自动重发 ambiguous 请求或用提前退出管道过滤 |

## §2 执行验收

1. `start`：记录 KEY、role、binding 状态；完成条件：session 可查询。
2. `ask`：记录 receipt 的 request_id/generation/state；完成条件：允许继续等待或明确失败。
3. `wait/result`：校验 latest ask 锚点、完整输出和终态；完成条件：结果非旧回答。
4. 交付：报告结论、来源、阻塞和能力降级；完成条件：用户可复核。

交叉引用：通用调用门见 `skill://oracle-consult/`；swarm worker 回报见 `skill://agent-swarm/`。

> **使用时机**：仅在用户明确说「persist-oracle」「持久化这个 review」「用 persist-oracle 工具」时使用。
> 默认的 oracle 咨询走 `oracle-consult` skill 的 `task` 直接调用，不走本 skill。
>
> role 选择、提问模板与追问技巧 → 见 `oracle-consult` skill。

## §3 CLI 参考

完整 CLI 命令、receipt 状态、freshness anchor、绑定/降级、GC 与输出管道规则：详见 `references/cli-contracts.md`。

主文件硬边界：`watch` 只观测，`wait/result` 才能验收；每轮记录 `request_id`/`generation`；`ambiguous` 不自动重发；旧结果不作本轮结果。
