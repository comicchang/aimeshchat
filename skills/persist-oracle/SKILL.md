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

# inspect（命中实例时输出 JSON；未命中时文本）
codeagent park info "$KEY"

# 首轮
printf '%s\n' "$PROMPT" | codeagent run --session-key "$KEY" --agent oracle --new-session

# 追加/追问（依赖 run 的 session auto-resume——不加 --new-session）
printf '%s\n' "$PROMPT" | codeagent run --session-key "$KEY" --agent oracle

# keepalive / 终止
codeagent park renew "$KEY"
codeagent park release "$KEY"
```

> **park lease 未自动建立**：当前 `codeagent run` 不会自动 park。若需要 `park info`/`renew`/`release` 有实际对象，首轮 run 完成后从输出 JSON 取 `session_id`，手动执行：
> ```
> codeagent park acquire "$KEY" --agent-type oracle --backend-id "$SESSION_ID"
> ```

> **park revive** 当前仅返回 hot/warm/cold 决策文本，不发送 prompt 或启动进程。实际追加/追问请用上述 `run`（不加 `--new-session`）依赖 auto-resume。

> **park watch** 子命令不存在。观察进度替代方案：`codeagent park info "$KEY"` 轮询 或 `mailbox stats --session <sid> --agent oracle`。

agent 类型自动选择 runner。不暴露 `--backend`。

## 触发条件

| 用户说 | 行为 |
|--------|------|
| "多轮 oracle review X" | `park info` → 有则 resume、无则 `run --new-session` |
| "继续上一轮 review" / "追加信息" | `run`（不加 `--new-session`，复用 session） |
| "结束 review" | `park release` |
| "独立第二意见" | 新 key，不继承上下文 |

## 约束

- 同一 review key 单实例
- TTL 60min（`park renew` 续租），硬上限 8h
- agent 类型决定 runner（内部解析，不暴露 `--backend`）
- 当前 `park revive` 仅打印决策，不执行
