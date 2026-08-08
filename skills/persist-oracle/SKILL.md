---
name: persist-oracle
description: 持久化多轮 Oracle review — 保留上下文。Oracle 可能需要 30-60 分钟，不设硬限时。首轮 run --new-session（自动 park），追加 run（auto-resume）。
---

# persist-oracle — 持久化多轮 Oracle Review

> Oracle 推理较慢（30-60 分钟常见），不设硬限时。

## CLI 契约

```
KEY='<project>:oracle:<domain>:<topic>'
PROMPT='...'

# inspect（命中实例输出 JSON；未命中文本。lease 由 run 自动建立）
codeagent park info "$KEY"

# 首轮（自动 park acquire，无需手动步骤）
printf '%s\n' "$PROMPT" | codeagent run --session-key "$KEY" --agent oracle --new-session

# 追加/追问 — 消息排队在 mailbox，NEXT run 时自动追加到 prompt
printf '%s\n' "$PROMPT" | codeagent run --session-key "$KEY" --agent oracle

# keepalive / 终止
codeagent park renew "$KEY"
codeagent park release "$KEY"

# 状态快速检查
codeagent park info "$KEY"  # lifecycle + last_message

# 进度实时监控
tail -f ~/.omp/park/progress/$KEY.txt  # 实时输出
```

> **park lease 由 `run` 自动建立**（首次 oracle run 成功后自动 `park acquire`）。无需手动步骤。若 lease 丢失可手动 `codeagent park acquire "$KEY" --agent-type oracle --backend-id "$SESSION_ID"`。

> **park revive** 报告 hot/warm/cold 决策但执行待完善——当前推荐使用上述 `run`（不加 `--new-session`）auto-resume 作为追加/追问路径。

> **park watch** 不存在。进度观察：`codeagent park info "$KEY"` 或 `mailbox stats --session <sid> --agent oracle`。

> **oracle 执行中不会读取 mailbox；等当前轮结束再 run 即可。**

agent 类型自动选择 runner。不暴露 `--backend`。

## 触发条件

| 用户说 | 行为 |
|--------|------|
| "多轮 oracle review X" / "找 oracle 验收" | `run --new-session` |
| "追加信息" / "追问" | `run`（不加 --new-session） |
| "结束 review" | `park release` |
