---
name: oracle-consult
description: 通过持久 Oracle 实例进行多轮咨询。默认使用 park/revive 保留上下文；用户明确要求时才用一次性 Oracle。自动路由 oracle-lite 与 full oracle。触发条件：用户说咨询/问/oracle review，或需要独立高阶架构、根因、风险判断。
---

# Oracle 咨询工作流

## 默认行为：持久 Oracle

**默认使用持久化 Oracle**（park/revive，上下文跨轮保留）。只有用户明确说
"一次性"、"one-shot"、"不持久化"、"新实例"时才 spawn 一次性 Oracle。

| 用户意图 | 行为 |
|----------|------|
| "咨询 oracle" / "问 oracle" / "oracle review" | **持久 Oracle**（默认） |
| "继续 review" / "追问" / "补充证据" | 持久 Oracle（revive 同一实例） |
| "一次性 oracle" / "one-shot" / "新实例" | 一次性 Oracle（不 park） |
| "独立第二意见" / "换模型" | 新持久实例（新 review key） |

## 路由

| 场景 | 路由 | 说明 |
|------|------|------|
| 代码审查/文档质量/测试覆盖 | oracle-lite | 日常审查，够用且省 |
| 架构 trade-off | oracle | 需要权衡判断 |
| 同一问题多次修复失败 | oracle | 需要跳出当前思路 |
| 上线前风险评审 | oracle | 需要独立视角 |
| 跨领域问题（并发+网络+存储） | oracle | 需要综合判断 |
| 用户明确要求 full oracle | oracle | 无条件执行 |

**用户说"咨询 oracle"时**：默认 oracle-lite，除非用户明确说"用 full oracle"。

## 调用纪律

用户明确要求 → 无条件执行。

Agent 主动咨询 → 需全部满足：
1. 已穷尽自身推理（2-3 种思路）
2. 是决策性的（trade-off）
3. 不重复（之前没问过相同问题）
4. 上下文已就绪

禁止：
- 每步都问"下一步"
- 同题换说法反复问
- 用 full oracle 做 explore/格式审查

## 持久 Oracle 工作流（默认路径）

### 首轮

1. 收集项目上下文（见下方三步流程）
2. `task` spawn oracle（oracle 系列已配置 `auto-exit: false`，任务完成后自动 park）
3. 记录返回的 `agent://<id>` 和 review key
4. 告知用户：`已创建持久 Oracle（review key: {key}），后续追问自动复用同一实例`

### 后续轮（revive）

1. `hub send` 到已保存的 `agent://<id>`，带增量问题
2. 格式：
```
继续 review key: {review_key}

增量：
- 新证据：<内容>
- 已落实的建议：<内容>
- 未落实及原因：<内容>

请：
1. 明确哪些旧结论仍成立
2. 哪些结论需修改
3. 只给出下一轮最小落地步骤
```
3. 不要重新 `task` spawn——复用同一实例

### 结束

1. 让 oracle 生成最终结论摘要
2. 告知用户：`Oracle review 已完成`

### 降级策略（Hot→Warm→Cold）

`hub send` 可能因进程重启失败。降级层级：

1. **Hot revive**（同进程）：`hub send` 到 parked agent，上下文完整
2. **Warm resume**（codeagent session-key）：`codeagent run --session-key <key> --resume`
3. **Cold reconstruction**（新实例 + 历史摘要）：`session-history-reader` 读历史注入新实例

每步降级显式报告用户。

## 一次性 Oracle（用户显式要求时）

仅当用户明确说"一次性"、"one-shot"、"不持久化"时使用：

```
task spawn oracle → 收到回答 → 结束（不 park，不 revive）
```

一次性 Oracle 不写入 ParkRegistry，不支持后续追问复用。

## Park Registry 集成

持久 Oracle 首轮 spawn 后写入 ParkRegistry：

```bash
codeagent park acquire "$REVIEW_KEY" \
  --agent-type oracle \
  --peer-id "$AGENT_ID" \
  --mailbox-id "$MAILBOX_AGENT" \
  --backend-id "$BACKEND_SESSION"
```

每轮 follow-up 后续租：
```bash
codeagent park renew "$REVIEW_KEY"
```

结束时释放：
```bash
codeagent park release "$REVIEW_KEY"
```

若 park CLI 不可用，记录 review_key 和 agent_id 到会话上下文手动管理。

## Session 复用纪律

**同一任务的所有 review 轮次必须复用同一个 oracle session**。开新 session 仅限：
- 用户明确要求"换一个 oracle / 重新 review"
- 更换模型（oracle → oracle-arch 等）
- 议题完全变化（不同 topic）

## 领域 preset 与 namespace

```json
{
  "oracle": {"backend": "codex", "model": "...", "description": "通用技术顾问"},
  "oracle-arch": {"backend": "codex", "model": "...", "description": "架构决策"},
  "oracle-perf": {"backend": "codex", "model": "...", "description": "性能分析"},
  "oracle-security": {"backend": "codex", "model": "...", "description": "安全审查"}
}
```

Session key：`<project>:oracle:<domain>:<topic>`

## 三步流程

### 1. 收集项目上下文

必须覆盖：
- 项目概要（技术栈、部署、相关模块）
- 代码现状（路径、符号、调用链、最近变更）
- 问题描述（触发条件、实际/期望、错误日志）
- 已尝试方案（2-3 个，含失败原因）
- 约束与风险（不能改的范围、兼容要求）

先用本地工具或 explore 收集，不要让 Oracle 从零探索。

### 2. 补历史记忆

按需用 memory_search、history、git log。只追加：
- 已放弃的方案及原因
- 纠偏记录
- 用户偏好和约束来源

### 3. 咨询 Oracle

持久模式（默认）：
```
task spawn oracle → 记录 agent://<id> → 回答 → park
后续轮：hub send 到 <id> → 回答 → park
```

一次性模式（用户显式要求）：
```
task spawn oracle → 收到回答 → 结束
```

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

## 追问

沿用同一实例（持久模式），不重发全部上下文：
- 补证据："我验证了 X，结果是 Y；这是否改变你的推荐？"
- 收敛方案："请只在方案 A 的前提下给出最小落地步骤"
- 风险展开："关于兼容性风险，请列出必须测试的用例"
- 反方审查："请从反对者角度指出最可能失败在哪里"
- 拆 commit："把方案拆成可在一个 commit 内完成的最小步骤"

## 成本优化

| 方案 | 说明 |
|------|------|
| 先 explore 再 Oracle | explore 收集后一次性传给 Oracle |
| 压缩上下文 | 只传相关文件/函数，不传整个目录 |
| 非关键用 oracle-lite | 文档/格式/测试覆盖不需要最强模型 |
| 限制轮次 | 同一 session 不超过 3-5 轮追问 |
| 持久化复用 | 多轮 review 复用同一实例，避免重复上下文 |
