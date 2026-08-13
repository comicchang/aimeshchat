---
name: persist-oracle
description: 持久化多轮 Oracle review — 保留上下文。仅用 aimeshchat oracle start/ask/status/list/watch/wait/result/revive/attach/release。显式 --model/--variant/--system/--prompt 传参（去 role）。OMP 用 omp-config memory + parked-revive，OpenCode 用原生 --session 续接；oh-my-openagent 不加额外 session 字段。
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

## 模型一致性（防漂移，2026-08 design 结论）

模型单一决定源 = **agents/*.md 的 `model:` 字段**（代码 `_read_agent_model()` 读取
`agents/<agent_type>.md`，不解析 `config.yml` 的 `fallbackChains` / `modelRoles`）。
**oracle 语义（review/验收/咨询）禁止依赖默认 `modelRoles.task`（= mimo）**，
调用方必须显式传 `--model`（及 `--variant`/`--system`），由 skill 按档位提供默认值。

改模型需修改 agents/*.md 的 `model:` 字段 + 校验一致性：
```bash
# 校验 agent profile 的 model: 与 skill 推荐值一致（不再 grep config.yml）
grep -E '^model:' ~/.omp/agents/oracle.md ~/.omp/agents/oracle-lite.md ~/.omp/agents/oracle-opus.md
```
- oracle / oracle-opus / oracle-lite 三档模型由 `agents/*.md` 的 `model:` 字段统一管理。
- 显式 `--model` 优先级 > 主 agent runtime context 继承 > agents/*.md `model:` 兜底 > 报错。
- 默认未指定 `--model` → 继承主 agent runtime context（不回退 mimo）。

## 绑定语义（A1，2026-08-12 修复）

`oracle start` 同步轮询 backend session id ≤60s；慢启动 oracle（full/opus 读配置、
长首轮）超时则返回 `binding=pending`，**runtime 保持存活**（不 SIGTERM——那曾误杀
健康 advisor，exit 143）。backend_session_id 后续由 gateway `runtime.register`
异步回写 park manifest，warm resume 仍收敛。status 显示 `binding: pending` 属正常，
不等同失败。

## 顾问双模式（CLI 主会话 或 worker mailbox，2026-08-12 澄清）

Oracle 顾问**可以与 agent-swarm worker/mailbox 共存**——两种交付模式都合法，按使用场景选择：

- **模式 A：CLI 主会话**（`aimeshchat oracle start/ask/result/wait`）——顾问问答流走主会话文本，
  `oracle result` 取回复。适用：独立多轮咨询，无 swarm 编排。
- **模式 B：agent-swarm worker**（`OMP_WORKER_ID=oracle` + mailbox）——oracle 以 worker 身份参与
  swarm，答案经 mailbox `REPORT` 交付给 manager。适用：swarm 编排下由 manager 派发/回收顾问结果。
  这是合法模式，**不是错配**。

两者不冲突，取决于会话是否在 swarm 编排内。**不要强行隔离** worker 身份——顾问在 swarm 内就该是
worker 角色。若用 CLI 主会话（模式 A）且不希望继承 worker 身份，则启动前 `unset OMP_WORKER_ID`；
若用 swarm 编排（模式 B），保留 `OMP_WORKER_ID` 并走 mailbox REPORT。

```bash
# 模式 A（CLI 主会话，不参与 swarm）：
unset OMP_WORKER_ID
aimeshchat oracle start "$KEY" --model gpt-5.6-sol --variant reasoning --system "你是一位资深架构顾问..." --prompt '...'
# 模式 B（swarm worker，走 mailbox REPORT）：保留 OMP_WORKER_ID，由 manager 派发
```

## CLI 契约（唯一工作流）

```
KEY='<project>:oracle:<domain>:<topic>[:<model_suffix>]'

# 首轮：新建 review/session/runtime（hot 交互式，初始 prompt 即首轮任务）
# skill 按档位生成 ExecutionSpec，通过 --model/--variant/--system/--prompt 显式透传
aimeshchat oracle start "$KEY" --model gpt-5.6-sol --variant reasoning --system "你是一位资深架构顾问..." --prompt '初始问题'

# 追加/追问：hot in-loop send（同 backend session，不新开进程）。
#   binding pending（sid 空）时 ask 返回 status=binding_pending 退出码 1（静默丢弃保护）；
#   慢启动首轮后立即 ask 用 --wait-binding 阻塞至绑定（≤60s）再投递。
aimeshchat oracle ask "$KEY" '追加信息'
aimeshchat oracle ask "$KEY" '首轮后立即追加' --wait-binding

# 状态：聚合 receipt / progress / park / runtime health
aimeshchat oracle status "$KEY"

# 列表：所有 park review（lifecycle / round / backend session）
aimeshchat oracle list

# 进度：cursor 可续的事件流；--exit-on ASSISTANT_PROGRESS 在产出时退出
aimeshchat oracle watch "$KEY" --cursor <last>
aimeshchat oracle watch "$KEY" --exit-on ASSISTANT_PROGRESS

# 等待：阻塞到【新的 ASSISTANT_PROGRESS 产出】（parked oracle 产出后 runtime 保持
#   active，agent_end 不触发）或 agent_end 兜底，内联打印最终回答。
#   生命周期：hot/warm 统一等新产出；cold(无 runtime) → 报错提示 revive；
#   binding pending → 提示但不阻塞。
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

- 档位选择：`oracle` / `oracle-lite` / `oracle-opus`——由 skill ExecutionSpec 模板决定
  模型（`--model`）、变种（`--variant`）、系统提示词（`--system`），
  调用时显式传参，agent profile `model:` 仅作兜底。
- OMP 提供 full hot/in-loop；OMP 不可用时 OpenCode 明确降级为 turn 间 follow-up；
  generic 因无 warm 仅显式指定时允许。

## ExecutionSpec 承载（去 role 化，模型/提示词策略归 skill）

### 设计原则

aimeshchat CLI 去 role 化后，**模型与工作负载提示词策略由 skill 决定**，aimeshchat 只保留
执行/路由/会话/mailbox 能力。oracle 顾问会话启动时，skill 生成 `ExecutionSpec`（含
provider/model/variant/system/full_prompt），通过 CLI 参数显式透传给 aimeshchat。

这确保：
- 模型选择由 skill 按顾问档位/场景自主决策，不在 aimeshchat 硬编码。
- 提示词策略（系统提示词、完整提示词模板）由 skill 维护，aimeshchat 不干预。
- **不使用 `--agent role`**——调用方直接传 `--model`/`--variant`/`--system`/`--prompt`。

### ExecutionSpec 结构

```yaml
ExecutionSpec:
  provider: <provider>       # 模型提供方（如 ppio/pa、anthropic、deepseek）
  model: <model>             # 模型标识（如 gpt-5.6-sol、claude-opus-4-8、v4-pro）
  variant: <variant>         # 变种标识（如 reasoning、fast、balanced）
  system: <system_prompt>    # 系统提示词（skill 定义的顾问角色/约束/格式）
  full_prompt: <prompt>      # 完整提示词（首轮任务 + 上下文拼装）
```

### 各档位 ExecutionSpec 推荐

| 档位 | provider/model | variant | system 建议 | 适用场景 |
|------|----------------|---------|-------------|----------|
| `oracle`（慢思考） | `ppio/pa/gpt-5.6-sol` | `reasoning` | 深度分析 + 风险评估 + 多角度论证 | 架构评审、根因分析、复杂决策 |
| `oracle-lite`（快思考） | `deepseek/v4-pro` | `fast` | 轻量审查 + 快速反馈 + 关键问题聚焦 | 代码质量、文档覆盖、日常 review |
| `oracle-opus`（最强推理） | `anthropic/claude-opus-4-8` | `balanced` | 严格形式化 + 证据链 + 可执行建议 | 安全审计、合规检查、高风险变更 |

> **skill 实现者**：以上为推荐默认值，可按项目/团队偏好覆盖。`variant` 和 `system` 字段
> 为 skill 内部约定，aimeshchat 不解析语义，仅透传给 backend。

### CLI 用法

```bash
# skill 生成 ExecutionSpec 后，通过 CLI 参数透传
aimeshchat oracle start "$KEY" \
  --model gpt-5.6-sol \
  --variant reasoning \
  --system "你是一位资深架构顾问，专注于..." \
  --prompt "请评审以下架构方案..."

# oracle-lite 档位
aimeshchat oracle start "$KEY" \
  --model v4-pro \
  --variant fast \
  --system "你是代码审查专家，快速识别关键问题..." \
  --prompt "请审查 PR #123 的变更..."

# oracle-opus 档位
aimeshchat oracle start "$KEY" \
  --model claude-opus-4-8 \
  --variant balanced \
  --system "你是安全审计专家，需提供证据链..." \
  --prompt "请审计以下安全相关变更..."
```

### 默认继承规则

**未显式指定时，default 继承主 agent 当前模型（runtime context）**，不在 skill 硬编码 mimo。

```bash
# 场景 1：skill 未指定 model → 继承主 agent 当前模型
aimeshchat oracle start "$KEY" --prompt '...'
# 实际使用主 agent 的 model（如主 agent 正在用 claude-sonnet → oracle 也用 claude-sonnet）

# 场景 2：skill 显式指定 model（推荐） → 使用 skill ExecutionSpec 的模型
aimeshchat oracle start "$KEY" --model gpt-5.6-sol --variant reasoning --system "你是一位资深架构顾问..." --prompt '...'
# 实际使用 gpt-5.6-sol

# 场景 3：用户 CLI 显式指定 model → 优先级最高
aimeshchat oracle start "$KEY" --model v4-pro --prompt '...'
# 实际使用 v4-pro，覆盖 skill 默认值
```

优先级链：**用户 CLI 显式指定 > skill ExecutionSpec > 主 agent runtime context > agents/*.md `model:` 兜底 > 报错**

### 迁移提示（完全去 role）

- `--agent oracle | oracle-lite | oracle-opus` **已废弃**——不再作为默认用法。
- 所有启动/追加/复活命令均通过 `--model`/`--variant`/`--system`/`--prompt` 显式传参。
- skill 按档位提供 ExecutionSpec 模板（见上表），调用时展开为 CLI 参数。
- 模型决定优先级链不变：**用户 CLI 显式指定 > skill ExecutionSpec > 主 agent runtime context > agents/*.md `model:` 兜底 > 报错**。
