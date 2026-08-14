---
name: persist-oracle
description: 持久化多轮 Oracle review — 保留上下文。仅用 aimeshchat oracle start/ask/status/list/watch/wait/result/revive/attach/release。何时咨询、选档见 oracle-consult skill。
---

# persist-oracle — 持久化多轮 Oracle Review

> Oracle 推理较慢（30-60 分钟常见）。长任务由 gateway+tmux 监督，**没有 hard timeout**；
> 只有显式 `oracle release` 才能终止 runtime。禁止回退 OMP task 子 Agent、
> 一次性 shell 或 `communicate()`。
> 何时咨询、按困难程度路由、提问模板与追问技巧 → 见 `oracle-consult` skill。

## 原生优先原则

- **OMP（首选 full-capability）**：`omp-config.common.yml` 的 memory 配置
  （memsearch backend / autoRecall / handoffSaveToDisk）+ parked-revive 机制。
  runtime 是 tmux 监督的交互式 OMP 进程，可接收 in-loop steer/followUp。
- **OpenCode（warm-only 降级）**：原生 session DB（SQLite）+ `--session` 续接。
  已知缺陷：task_id 只在成功路径返回，失败/中断时丢失 → 走 cold snapshot 兜底。
- generic（无 warm）仅在显式指定时允许。

## 模型一致性（防漂移）

模型权威 = **ExecutionSpec 显式字段**：调用方（skill）直接传 `--model`（及
`--variant`/`--system`/`--prompt`），代码 `ExecutionSpec.from_args` 只解析：
显式 `--model` → 主 agent runtime context → execution-context → agents/*.md `model:` 兜底。

### 三档模型定义

**模型权威 = `~/.omp/agent/agents/<profile>.md` 的 `model:` 字段**（work/home 环境
provider 不同）。下表为当前本机推荐值；改模型后必须跑下方一致性校验命令。

| 档位 | provider/model（当前本机值） | variant 建议 | system 建议 | 适用场景 |
|------|----------------|---------|-------------|----------|
| `oracle`（慢思考） | `bytecat-gpt/gpt-5.6-sol` | `reasoning` | 深度分析 + 风险评估 | 架构评审、根因分析 |
| `oracle-lite`（快思考） | `Mify/deepseek/deepseek-v4-pro` | `fast` | 轻量审查 + 快速反馈 | 代码质量、日常 review |
| `oracle-opus`（最强推理） | `bytecat/claude-opus-4-8` | `balanced` | 严格形式化 + 证据链 | 安全审计、高风险变更 |

### 一致性校验命令

```bash
# 校验 agent profile 的 model: 与 skill 推荐值一致
grep -E '^model:' ~/.omp/agent/agents/oracle.md ~/.omp/agent/agents/oracle-lite.md ~/.omp/agent/agents/oracle-opus.md
```

### 优先级链

用户 CLI 显式指定（--model/--variant/--system）> 主 agent runtime context 继承 > execution-context > agents/*.md `model:` 兜底 > 报错。

## 废弃项（勿再使用）

- `--agent`：兼容占位参数，无模型语义——传了只打弃用告警，不参与模型解析。
- `config.yml` 的 `fallbackChains` / `modelRoles`：不参与模型解析（仅用于配置指纹
  哈希，检测 manifest 漂移）。模型策略已归 skill + agent profile。

## 绑定语义（A1）

`oracle start` 同步轮询 backend session id ≤60s；慢启动 oracle 超时则返回
`binding=pending`，**runtime 保持存活**。backend_session_id 后续由 gateway
`runtime.register` 异步回写 park manifest。status 显示 `binding: pending` 属正常。

## 顾问双模式

- **模式 A：CLI 主会话**（`aimeshchat oracle start/ask/result/wait`）
- **模式 B：agent-swarm worker**（`OMP_WORKER_ID=oracle` + mailbox REPORT）

两者不冲突，取决于会话是否在 swarm 编排内。

## CLI 契约

```
KEY='<project>:oracle:<domain>:<topic>[:<model_suffix>]'

# 首轮：新建 review/session/runtime
aimeshchat oracle start "$KEY" --model bytecat-gpt/gpt-5.6-sol --variant reasoning --system "..." --prompt '初始问题'

# 追加/追问：hot in-loop send（同 backend session，不新开进程）
aimeshchat oracle ask "$KEY" '追加信息'
aimeshchat oracle ask "$KEY" '首轮后追加' --wait-binding

# 状态/列表
aimeshchat oracle status "$KEY"
aimeshchat oracle list

# 进度/等待
aimeshchat oracle watch "$KEY" --exit-on ASSISTANT_PROGRESS
aimeshchat oracle wait "$KEY" --timeout 300

# 结果
aimeshchat oracle result "$KEY"          # JSON
aimeshchat oracle result "$KEY" --raw    # 纯文本

# 复活/附着/终止
aimeshchat oracle revive "$KEY" [--mode bg|pane|resume]
aimeshchat oracle attach "$KEY" '问题'
aimeshchat oracle release "$KEY" [--purge]

# 一致性检查（gateway ↔ park ↔ 会话记录 三源校验）
aimeshchat oracle doctor [--fix]

# 清理过期 released session（--dry-run 先看再删）
aimeshchat oracle gc [--dry-run] [--json]
```

## 降级策略（Hot→Warm→Cold）

1. **Hot revive**（同进程）：`hub send` 到 parked agent，上下文完整
2. **Warm resume**（原生 session）：`omp --resume <backend_session_id>` 或 `opencode --session <sid>`
3. **Cold reconstruction**（新实例 + 快照）：`oracle revive` 自动走 cold 路径

每步降级显式报告用户。`ask` 成功 JSON 含 `adopted` 字段。

## 触发条件

| 用户说 | 行为 |
|--------|------|
| "多轮 oracle review X" / "找 oracle 验收" | `oracle start` |
| "追加信息" / "追问" / 新证据 | `oracle ask` |
| "oracle 现在怎么样" | `oracle status` |
| "有哪些进行中的 oracle review" | `oracle list` |
| "oracle 回答了什么" / "取结果" | `oracle result` / `oracle wait` |
| "唤醒已释放的 review" | `oracle revive` / `oracle attach` |
| "结束 review" / "释放" | `oracle release` |

## GC 自动清理

- `oracle gc`：清理过期 released session（hard_expires_at 过期 或 last_activity_at > 2天）
- 自动触发于 `oracle start` / `oracle list` / `oracle status`（24h 节流）
- session 隔离：oracle session 存储在 `~/.omp/agent/sessions/_oracle/<safe-key>/`
