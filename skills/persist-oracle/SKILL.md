---
name: persist-oracle
description: 持久化多轮 Oracle review — 保留上下文。仅用 aimeshchat oracle start/ask/status/list/watch/wait/result/revive/attach/release。OMP 用 omp-config memory + parked-revive，OpenCode 用原生 --session 续接；oh-my-openagent 不加额外 session 字段。
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
aimeshchat oracle start "$KEY" --agent oracle --prompt '初始问题'

# 追加/追问：hot in-loop send（同 backend session，不新开进程）
aimeshchat oracle ask "$KEY" '追加信息'

# 状态：聚合 receipt / progress / park / runtime health
aimeshchat oracle status "$KEY"

# 列表：所有 park review（lifecycle / round / backend session）
aimeshchat oracle list

# 进度：cursor 可续的事件流
aimeshchat oracle watch "$KEY" --cursor <last>

# 等待：阻塞到 agent_end 事件，内联打印最终回答（超时建议转 result）
aimeshchat oracle wait "$KEY" --timeout 300

# 结果：从 session 转录 / mailbox REPORT / FS 扫描提取最新回答
aimeshchat oracle result "$KEY"          # JSON（source/confidence/messages）
aimeshchat oracle result "$KEY" --raw    # 纯文本最后一条回答

# 复活：RELEASED_SOFT / COLD_RESUMABLE → HOT_PARKED（warm 复用原生会话 / cold 快照重建）
aimeshchat oracle revive "$KEY" [--mode bg|pane|resume]

# 附着：统一入口——HOT_PARKED 走 ask（hot send），released/cold 走 revive
aimeshchat oracle attach "$KEY" '问题'

# 终止：写 terminal + 停 runtime + 释放 park（唯一终止途径）
aimeshchat oracle release "$KEY"
aimeshchat oracle release "$KEY" --purge   # 硬销毁：删 OMP session + swarm session + park 行
```

- 同 review key 默认复用同一 backend session：首轮新开、追加不新开。
- `ask` 实际方法（hot/warm/cold）由 status 报告——**绝不声称未发生的 hot revive**。
- hot 失败自动降级 warm（原生 `--resume`/`--session`）→ cold（snapshot 重建）。
- `ask` 成功 JSON 含 `adopted` 字段（runtime 是否成功注册到 gateway presence）。
- 错误统一输出 JSON 到 stderr（`{"error": ..., "review_key": ..., "detail": ...}`），可脚本化解析。

## 触发条件

| 用户说 | 行为 |
|--------|------|
| "多轮 oracle review X" / "找 oracle 验收" / "唤醒 oracle 咨询" | `oracle start`（首轮） |
| "追加信息" / "追问" / 新证据 | `oracle ask`（hot→warm→cold） |
| "oracle 现在怎么样" | `oracle status` |
| "有哪些进行中的 oracle review" / "列出 review" | `oracle list` |
| "oracle 回答了什么" / "取结果" | `oracle result`（或 `oracle wait` 阻塞等待） |
| "唤醒已释放的 review" / "恢复会话" | `oracle revive` / `oracle attach` |
| "结束 review" / "释放" | `oracle release` |

## Oracle 类型

- 识别：`agent.startswith("oracle")`（覆盖 oracle / oracle-lite / oracle-opus），
  仅用于 profile/required-capability 选择（warm_resume 必需，preferred omp→opencode），
  不硬编码 OMP runner。
- OMP 提供 full hot/in-loop；OMP 不可用时 OpenCode 明确降级为 turn 间 follow-up；
  generic 因无 warm 仅显式指定时允许。
