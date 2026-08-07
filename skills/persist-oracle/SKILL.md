---
name: persist-oracle
description: 持久化多轮 Oracle review — 保留上下文、支持 park/revive。触发条件：用户说多轮 oracle review、继续 review、持久化 oracle、结束 review。Oracle 可能需要 30-60 分钟，不设硬限时。
---

# persist-oracle — 持久化多轮 Oracle Review

> Oracle 推理较慢（30-60 分钟常见），不设硬限时。

## CLI 契约

```
KEY='<project>:oracle:<domain>:<topic>'
PROMPT='...'

# inspect
codeagent park info "$KEY" --json

# 首轮
printf '%s\n' "$PROMPT" | codeagent run --session-key "$KEY" --agent oracle --new-session

# 后续
codeagent park revive "$KEY" --prompt "$PROMPT"

# 观察
codeagent park info "$KEY" --json
codeagent park watch "$KEY" --json --interval 5

# keepalive / 终止
codeagent park renew "$KEY"
codeagent park release "$KEY"
```

不暴露 `--backend`、`--skip-permissions`、`park acquire`。agent 类型自动选择 runner。

## 触发条件

| 用户说 | 行为 |
|--------|------|
| "多轮 oracle review X" | `park info` → 有则 revive、无则 `run --new-session` |
| "继续上一轮 review" | `park revive` |
| "结束 review" | `park release` |
| "独立第二意见" | 新 key，不继承上下文 |

## 约束

- 同一 review key 单实例
- TTL 60min（`park renew` 续租），硬上限 8h
- agent 类型决定 runner（内部解析，不暴露 `--backend`）
- 进度通过 `park watch` / mailbox PROGRESS 可见
