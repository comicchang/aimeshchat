---
name: oracle-consult
description: 何时、如何向 Oracle 提问，并按困难程度路由到 oracle-lite / oracle。覆盖调用纪律、上下文收集、标准 prompt 模板与追问技巧。默认用 task 直接咨询，不走 persist-oracle 除非用户明确要求。
---

# oracle-consult — Oracle 咨询工作流

> 本 skill 管「该不该问、问哪个档、怎么组织问题、怎么追问」。
> 启动/持久化/追问的具体命令与模型映射见 `persist-oracle` skill。

## 模型选择（成本优先）

| 档位 | 模型（provider/model） | 适用场景 | 成本 |
|------|------------------------|----------|------|
| **oracle-lite**（默认） | `Mify/deepseek/deepseek-v4-pro` | 代码审查、文档质量、测试覆盖、格式审查、日常问题 | 低 |
| **oracle** | `bytecat-gpt/gpt-5.6-sol` | 架构 trade-off、根因分析、风险评审、跨领域问题 | 中 |
| **oracle-opus** | `bytecat/claude-opus-4-8` | **仅用户明确要求时使用**（太贵） | 高 |

> 实际模型以 `~/.omp/agent/agents/<profile>.md` 的 `model:` 为准。
> 改模型后跑 `grep -E '^model:' ~/.omp/agent/agents/oracle*.md` 校验一致性。

**默认规则**：用户说「咨询 oracle」→ 用 `oracle-lite`。
只有场景命中高难度行或用户明确说「用 oracle」「用 full oracle」时才升级。
**oracle-opus 除非用户明确说「用 opus」「用 oracle-opus」，否则禁止使用。**

### task 调用时的档位映射

用 `task` 工具直接咨询时，`agent` 参数决定模型：
- `agent="oracle-lite"` → `Mify/deepseek/deepseek-v4-pro`
- `agent="oracle"` → `bytecat-gpt/gpt-5.6-sol`
- `agent="oracle-opus"` → `bytecat/claude-opus-4-8`

未指定 `agent` 时默认 `oracle-lite`。

## 咨询方式（task 直接调用）

### 默认：task 直接咨询

用户说「咨询 oracle」「问 oracle」「oracle review」「找 oracle 验收」时：
1. 用 `task` 工具直接 spawn oracle agent（不走 persist-oracle CLI）
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

## 调用纪律

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

## 边界（Oracle 是什么 / 不是什么）

- **Oracle 只提供建议，不实施改动**——需要实现时另 spawn worker（源自 agent profile 约束）。
- **Oracle 建议需人工复核**——它是独立视角的输入，不是最终结论；高风险决策以验证过的证据为准。
- **上下文不足时 Oracle 会先指出缺失项**——不要替它脑补，把缺失信息补齐再问。

## 路由决策树

| 场景 | 路由 | 说明 |
|------|------|------|
| 代码审查 / 文档质量 / 测试覆盖 / 格式审查 | `oracle-lite` | 日常审查，够用且省 |
| 架构 trade-off | `oracle` | 需要权衡判断 |
| 同一问题多次修复失败 | `oracle` | 需要跳出当前思路 |
| 上线前风险评审 | `oracle` | 需要独立视角 |
| 跨领域问题（并发+网络+存储） | `oracle` | 需要综合判断 |
| 用户明确指定档位（oracle / oracle-lite） | 按用户指定 | 无条件执行 |
| 用户说「用 opus」「用 oracle-opus」 | `oracle-opus` | 无条件执行（贵，需明确要求） |

**用户说「咨询 oracle」时**：默认 `oracle-lite`，除非明确指定档位
（「用 oracle」「用 full oracle」）或场景命中上表高难度行。

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

用下方标准模板，将上面收集的内容填入对应槽位。

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
  --subject "验证 <结论>" --body '<{"request_id":"req1","run_id":"r1",...}>' --host <H>

# 4. 轮询等待 REPORT（每 5s）→ 验证 request_id 匹配 + 证据/产物 → finalize
aimeshchat mailbox read --session "$SID" --agent manager --owner manager --host <H> --json
```

### 纪律

- 派发前确认目标 worker `state != BUSY` 且无未消费 REPORT（Dispatch Gate，见 aimeshchat-cli）
- 验证类任务要求 REPORT 附证据引用（AttachmentRef：source_host/remote_root/relative_path/size/sha256），核对一致才算验证通过
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
