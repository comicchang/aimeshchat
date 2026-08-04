# Park Capability Matrix

> OMP harness 对 Agent Park/复活的支持契约。真机实验记录。

## 实验 1：同进程 Hot Revive

| 项 | 结果 |
|----|------|
| 场景 | oracle task → 回答后 parked → hub send 唤醒 |
| 状态 | ✅ 已验证（codeagent-oracle-consult skill 记录） |
| 上下文完整 | ✅ Agent 确认能看到自己第 1 轮的答案 |
| 失败条件 | peer 进程不存在 / generation 不匹配 |

## 实验 2：进程重启后 peer 不可寻址

| 项 | 结果 |
|----|------|
| 场景 | parked → kill OMP → hub send 到旧 peer id |
| 预期 | reachable = false |
| 状态 | ⚠️ 未验证（从 OMP 源码分析：registry 是进程内存） |
| 降级路径 | Warm resume 或 Cold reconstruction |

## 实验 3：Parked Agent 资源占用

| 项 | 结果 |
|----|------|
| 场景 | 测量 parked agent 的 RSS / FD / timer 占用 |
| 状态 | ⚠️ 未测量（需真机实验） |
| 建议 | 设置 max_hot_parked=3 硬限制，避免 OMP 并发槽耗尽 |

## 未确认项

| 项 | 策略 |
|----|------|
| OMP 重启后 hub send 的精确行为 | 标记 capability-dependent，hot revive 限定同 generation |
| Parked agent 是否计入 OMP 并发槽 | 需要测量，暂设 max_hot_parked=3 |
| plugin 的 fs.watch 在 OMP 重启后是否存活 | 否（进程级），降级到 Warm/Cold |

## 依赖 OMP 的公开契约

| 契约 | 说明 |
|------|------|
| `auto-exit: false` | Agent 完成后不退出，保持 parked |
| `hub send` | 唤醒 parked agent，上下文完整保留 |
| `session_shutdown` | 清理 watcher/timer/identity 文件 |
| peer registry 进程级 | 重启后丢失，不可 hot revive |