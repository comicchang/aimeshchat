---
name: codeagent-oracle-consult
description: 通过 codeagent 持久 session 咨询 Oracle。自动路由 oracle-lite 与 full oracle；覆盖上下文收集、历史补充、领域隔离、追问和结果整合。触发条件：用户明确说咨询/问/oracle review，或需要独立高阶架构、根因、风险判断。
---

# codeagent Oracle 咨询工作流

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
2. 问题是决策性的（trade-off）
3. 不重复（之前没问过相同问题）
4. 上下文已就绪

禁止：
- 每步都问"下一步"
- 同题换说法反复问
- 用 full oracle 做 explore/格式审查

## 领域 preset 与 namespace

建议 models.json 定义多个 oracle preset：

```json
{
  "oracle": {"backend": "codex", "model": "...", "description": "通用技术顾问", "yolo": true},
  "oracle-arch": {"backend": "codex", "model": "...", "description": "架构决策"},
  "oracle-perf": {"backend": "codex", "model": "...", "description": "性能分析"},
  "oracle-security": {"backend": "codex", "model": "...", "description": "安全审查"}
}
```

Session key 推荐：`<project>:oracle:<domain>:<topic>`

```bash
# 同项目同领域自动 resume
codeagent run "$PROMPT" "$PWD" --agent oracle-lite \
  --session-key "myproj:oracle:review:module-x"

# 远程 topic 架构咨询
printf '%s\n' "$PROMPT" | codeagent route MyTopic --repo 0 \
  --agent oracle-arch \
  --session-key "myproj:oracle:arch:storage-boundary"

# 新议题
codeagent run "$PROMPT" "$PWD" --agent oracle \
  --session-key "myproj:oracle:security:auth-v2" --new-session
```

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

```bash
# 本地仓
codeagent run "$PROMPT" "$PWD" --agent oracle \
  --session-key "myproj:oracle:arch:question"

# 远程仓
printf '%s\n' "$PROMPT" | codeagent route MyTopic \
  --agent oracle --session-key "myproj:oracle:arch:question"
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

沿用同一 session key，不重发全部上下文：
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
