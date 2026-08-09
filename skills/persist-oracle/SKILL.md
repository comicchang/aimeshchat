---
name: persist-oracle
description: 持久化多轮 Oracle review — 保留上下文。仅用 meshkit oracle start/ask/status/watch/release。OMP 用 omp-config memory + parked-revive，OpenCode 用原生 --session 续接；oh-my-openagent 不加额外 session 字段。
---

# persist-oracle — 持久化多轮 Oracle Review

> Oracle 推理较慢（30-60 分钟常见）。长任务由 gateway+tmux 监督，**没有 hard timeout**；
> 只有显式 `oracle release` 才能终止 runtime。禁止回退 OMP task 子 Agent、
> 一次性 shell 或 `communicate()`。

## 原生优先原则（用户明确指示）

- **OMP（首选 full-capability）**：`omp-config.common.yml` 的 memory 配置
  （memsearch backend / autoRecall / handoffSaveToDisk）+ parked-revive 机制。
  runtime 是 tmux 监督的交互式 OMP 进程，可接收 in-loop steer/followUp。
- **OpenCode（warm-only 降级）**：原生 session DB（SQLite）+ `--session` 续接——
  即 subagent `task_id` 续接机制（"continues with its previous messages and
  tool outputs"）。oh-my-openagent 侧**不加任何额外 session 字段**，只透传原生
  backend session id。已知缺陷：task_id 只在成功路径返回，失败/中断时丢失
  （opencode issue #13910/#35222）→ 走 cold snapshot 兜底。
- generic（无 warm）仅在显式指定时允许。

## CLI 契约（唯一工作流）

```
KEY='<project>:oracle:<domain>:<topic>[:<model_suffix>]'

# 首轮：新建 review/session/runtime（hot 交互式，初始 prompt 即首轮任务）
meshkit oracle start "$KEY" --agent oracle --prompt '初始问题'

# 追加/追问：hot in-loop send（同 backend session，不新开进程）
meshkit oracle ask "$KEY" '追加信息'

# 状态：聚合 receipt / progress / park / runtime health
meshkit oracle status "$KEY"

# 进度：cursor 可续的事件流
meshkit oracle watch "$KEY" --cursor <last>

# 终止：写 terminal + 停 runtime + 释放 park（唯一终止途径）
meshkit oracle release "$KEY"
```

- 同 review key 默认复用同一 backend session：首轮新开、追加不新开。
- `ask` 实际方法（hot/warm/cold）由 status 报告——**绝不声称未发生的 hot revive**。
- hot 失败自动降级 warm（原生 `--resume`/`--session`）→ cold（snapshot 重建）。

## 触发条件

| 用户说 | 行为 |
|--------|------|
| "多轮 oracle review X" / "找 oracle 验收" / "唤醒 oracle 咨询" | `oracle start`（首轮） |
| "追加信息" / "追问" / 新证据 | `oracle ask`（hot→warm→cold） |
| "oracle 现在怎么样" | `oracle status` |
| "结束 review" / "释放" | `oracle release` |

## Oracle 类型

- 识别：`agent.startswith("oracle")`（覆盖 oracle / oracle-lite / oracle-opus），
  仅用于 profile/required-capability 选择（warm_resume 必需，preferred omp→opencode），
  不硬编码 OMP runner。
- OMP 提供 full hot/in-loop；OMP 不可用时 OpenCode 明确降级为 turn 间 follow-up；
  generic 因无 warm 仅显式指定时允许。
