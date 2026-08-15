---
name: oracle-consult
description: 何时、如何向 Oracle 提问，并按困难程度路由到 oracle-lite / oracle。覆盖调用纪律、上下文收集、标准 prompt 模板与追问技巧。默认用 task 直接咨询，不走 persist-oracle 除非用户明确要求。
---

# oracle-consult — Oracle 咨询工作流

> 本 skill 管「该不该问、问哪个档、怎么组织问题、怎么追问」。
> 启动/持久化/追问的具体命令与模型映射见 `persist-oracle` skill。

## 调用纪律（该不该问）

用户明确要求 → 无条件执行。

Agent 主动咨询 → 需全部满足：
1. 已穷尽自身推理（2-3 种思路都已走通/证伪）
2. 是决策性的（存在 trade-off）
3. 不重复（之前没问过相同问题）
4. 上下文已就绪（见下方收集流程）

禁止：
- 每步都问「下一步」
- 同题换说法反复问
- 用 oracle / oracle-opus 做 explore / 格式审查（这类走本地工具或 oracle-lite）
- 咨询可本地 5 分钟验证的低价值问题

## 档位选择（场景驱动）

咨询时使用对应档位的 agent profile，具体模型由该 profile 按环境/provider 决定。

| 档位 | task `agent=` | agent profile | 适用场景 | 推理强度 | 成本 |
|------|---------------|---------------|----------|----------|------|
| **oracle-lite**（默认） | `oracle-lite` | `agents/oracle-lite.md` | 代码审查、文档质量、测试覆盖、格式审查、日常问题 | 中 | 低 |
| **oracle** | `oracle` | `agents/oracle.md` | 架构 trade-off、根因分析、风险评审、跨领域问题 | 高 | 中 |
| **oracle-opus** | `oracle-opus` | `agents/oracle-opus.md` | **仅用户明确要求时使用** | 最高 | 高 |

> 具体模型 = agent profile 的 `model:` 字段（如 `agents/oracle-lite.md` 的 `model:`），由各 agent 按环境决定。
> 调用者也可通过 `--model` 显式覆盖。skill 不读取、不校验、不硬编码模型值。

**默认规则**：用户说「咨询 oracle」→ 用 `oracle-lite`，除非：
- 场景命中高难度行（架构 trade-off / 同题多次失败 / 上线前风险评审 / 跨领域），或
- 用户明确说「用 oracle」「用 full oracle」
- **oracle-opus 除非用户明确说「用 opus」「用 oracle-opus」，否则禁止使用。**

## ⚠️ agent 参数必须显式传递（防模型漂移）

**`task` 工具的 `agent` 参数必须显式指定，禁止省略。**

省略 `agent` 时，task 工具继承主会话模型，**不会**自动使用 oracle 档位模型。这是咨询 oracle 唤醒错误模型的根因。

```
# ✓ 正确：显式指定 agent
task(agent="oracle-lite", task="...")
task(agent="oracle", task="...")

# ✗ 错误：省略 agent → 继承主会话模型，不是 oracle
task(task="...")
```

## 咨询方式（task 直接调用）

### 默认：task 直接咨询

用户说「咨询 oracle」「问 oracle」「oracle review」「找 oracle 验收」时：
1. 用 `task` 工具 **显式指定 `agent` 参数**（不走 persist-oracle CLI）
   - 默认：`agent="oracle-lite"`
   - 高难度：`agent="oracle"`
   - 用户要求时：`agent="oracle-opus"`
2. 在 task prompt 中包含收集好的上下文
3. 记录返回的 `agent://<id>`，告知用户已创建咨询

### 追问：复用同一 task agent

用户说「追问」「继续补充」「再问一次」「补充证据」时：
1. 用 `hub send` 向之前的 `agent://<id>` 发送增量问题
2. 不要重新 spawn——复用同一实例
3. 格式：补充新证据 + 已落实建议 + 未落实原因

### 持久化：仅用户明确要求时

用户说「persist-oracle」「持久化这个 review」「用 persist-oracle 工具」时：
1. 才使用 `persist-oracle` skill 的 CLI 命令（`aimeshchat oracle start/ask/wait`）
2. 这时走完整的持久化流程（park/revive/session-dir 隔离）

## 边界（Oracle 是什么 / 不是什么）

- **Oracle 只提供建议，不实施改动**——需要实现时另 spawn worker（源自 agent profile 约束）。
- **Oracle 建议需人工复核**——它是独立视角的输入，不是最终结论；高风险决策以验证过的证据为准。
- **上下文不足时 Oracle 会先指出缺失项**——不要替它脑补，把缺失信息补齐再问。

## 咨询前上下文收集

先用本地工具或 explore 收集，不要让 Oracle 从零探索。

### 1. 收集项目上下文（必覆盖五项）

- 项目概要（技术栈、部署、相关模块）
- 代码现状（路径、符号、调用链、最近变更）
- 问题描述（触发条件、实际 vs 期望、错误日志）
- 已尝试方案（2-3 个，含失败原因）
- 约束与风险（不能改的范围、兼容要求）

### 2. 补历史记忆（按需）

memory_search / history / git log。只追加：
- 已放弃的方案及原因
- 纠偏记录
- 用户偏好和约束来源

### 3. 拼装 prompt

用下方标准模板，将上面收集的内容填入对应槽位。一次只问**一个**决策问题；多个问题拆成多轮。

## 标准 Prompt 模板

```
你是 Oracle，资深技术架构顾问。请基于以下上下文给出可执行建议。

## 项目上下文
[技术栈、部署、路径、符号、调用链、历史决策]

## 当前问题
[唯一、明确的决策问题]

## 已知事实
[已验证证据]

## 未确认假设
[尚未验证]

## 已尝试方案
[方案、结果、失败原因；无则写暂无]

## 约束条件
[兼容、权限、网络、私有数据、不能改范围]

## 期望输出
1. Bottom line
2. Action plan
3. Effort: Quick/Short/Medium/Large
4. Confidence: high/medium/low
5. Why this approach (≤4 点)
6. Watch out for (≤3 点)

上下文不足时先指出缺失的 1-3 项，不要猜。
```

## 追问技巧

沿用同一实例（task agent），不重发全部上下文：
- 补证据：「我验证了 X，结果是 Y；这是否改变你的推荐？」
- 收敛方案：「请只在方案 A 的前提下给出最小落地步骤」
- 风险展开：「关于兼容性风险，请列出必须测试的用例」
- 反方审查：「请从反对者角度指出最可能失败在哪里」
- 拆 commit：「把方案拆成可在一个 commit 内完成的最小步骤」

## 远程验证流程

Oracle 结论需要跨主机验证/实施时，用 **mailbox 协议**派发远程任务，**禁止 `ssh + tmux send-keys`**（会打断远程 OMP 进程正在生成的回合，且无法证明送达）。具体命令与状态判定见 `skill://aimeshchat-cli/` 的「远程 Worker 管理」。

### 派发步骤（manager-pull）

```bash
SID="ora-$(date +%s)"

# 1. 建 session + 注册 worker（本地）
aimeshchat swarm create-session "$SID" --manager manager --members <w>
aimeshchat swarm register "$SID" --agent <w> --host <H> --backend omp

# 2. 远程 host 初始化 mailbox 目录
aimeshchat mailbox session-init --session "$SID" --manager manager --agents <w> --host <H>

# 3. INIT 握手 → 验证 IDLE 后派发 TASK（body 带 request_id/run_id）
aimeshchat mailbox send --session "$SID" --from manager --to <w> --kind TASK \
  --subject "验证 <结论>" --body '{"request_id":"req1","run_id":"r1","target":"..."}' --host <H>

# 4. 轮询等待 REPORT（每 5s）：read → 验证 request_id + 附件 → finalize
aimeshchat mailbox read --session "$SID" --agent manager --owner manager --host <H> --json
aimeshchat artifact pull --host <H> --artifact-id <id> --relative-path <p> \
  --size <n> --sha256 <hex> --dest <local>          # 附件验证
aimeshchat mailbox finalize --session "$SID" --agent manager --msg-id <id> --owner manager --host <H>
```

### 纪律

- 派发前确认目标 worker `state != BUSY` 且无未消费 REPORT（Dispatch Gate，见 aimeshchat-cli）
- 验证类任务要求 REPORT 附证据引用（AttachmentRef：source_host/remote_root/relative_path/size/sha256），核对一致才算验证通过
- REPORT 未经验证（request_id 匹配 + sha256/size）**不要 finalize**；校验失败 → 拒绝并要求 worker 重发
- 结论落地需要并行验证多个点 → 拆多个 worker 并行派发，不串行排队

## 成本优化

| 方案 | 说明 |
|------|------|
| 先 explore 再 Oracle | explore 收集后一次性传给 Oracle |
| 压缩上下文 | 只传相关文件/函数，不传整个目录 |
| 默认 oracle-lite | 文档/格式/测试覆盖不需要最强模型 |
| 限制轮次 | 同一 session 不超过 3-5 轮追问 |
| 持久化复用 | 多轮 review 复用同一实例，避免重复上下文 |
| 禁止 oracle-opus | 除非用户明确要求，否则不使用（太贵） |
