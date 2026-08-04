# Park Terminology

> 术语冻结 — Agent Park／复活机制的核心概念定义。

## 三层恢复层级

### Hot Park（热保留）
同进程同 generation 内，Agent 任务完成后保持 parked 状态。进程内上下文完整保留（含工具调用历史、模型会话、peer registry 条目）。通过 `hub send` 唤醒，Agent 可继续上一轮对话。

**约束**：OMP harness 进程级（registry 进程内存），OMP 重启后 peer 不可寻址。限定同 `parent_process_generation`。

### Warm Resume（温恢复）
跨 CLI 调用，通过 `codeagent run --session-key <key>` 复用同一 backend session。backend（如 OMP 的 `--resume`）恢复对话历史。

**约束**：能否恢复全部工具状态取决于 backend 实现。首个 turn 必须做上下文校验（"请列出你上一轮的 3 个要点"），确认失败则降级 cold。

### Cold Reconstruction（冷重建）
新建 Agent 实例，注入 curated snapshot（结构化摘要 + artifact 引用）。不声称"原实例复活"，首轮输出三段式结论：
1. 仍成立的结论
2. 需重新审查的结论
3. 因新证据废弃的结论

## 降级矩阵

| 当前层级 | 下一级 | 降级条件 |
|----------|--------|----------|
| Hot | Warm | hub send 失败（peer 不可达 / generation 不匹配 / 超时） |
| Warm | Cold | resume 失败 / 上下文校验不通过 |
| Cold | — | 最终 fallback（无进一步降级） |

## 身份命名空间

| 名称 | 用途 | 作用域 |
|------|------|--------|
| `review_key` | 用户意图的逻辑标识（`<project>:oracle:<domain>:<topic>`） | 跨 session / 跨主机 |
| `peer_agent_id` | OMP harness 的 peer registry 条目 ID | 同进程同 generation |
| `mailbox_agent_id` | mailbox inbox 目录名（`<root>/<session>/<agent>/inbox/`） | 同 mailbox 根 |
| `backend_session_id` | OMP / 其他 backend 的 session ID | 跨 CLI 调用 |

## 生命周期状态

```
SPAWNING → ACTIVE → HOT_PARKED → REVIVING → ACTIVE
                    │
                    ├─ TTL／进程退出／资源驱逐 → COLD_RESUMABLE
                    ├─ snapshot 失败           → BROKEN
                    └─ 用户结束／硬上限         → RELEASED

COLD_RESUMABLE → backend resume 成功 → ACTIVE
               → resume 失败         → 新实例 + curated context → ACTIVE
```

## 资源策略

| 项 | 默认值 | 说明 |
|----|--------|------|
| max_hot_parked | 3 | 每个 manager 最多 hot parked 顾问型 Agent |
| TTL | 60 min | 滑动 TTL，每次 follow-up 后续租 |
| hard_limit | 8 h | 硬上限，超时强制释放 |
| max_rounds | 5 | 同一 review key 最大轮次 |
| eviction_order | LRU | 最久未使用优先驱逐 |
| snapshot_on_evict | true | 驱逐前强制生成最后一次 snapshot |