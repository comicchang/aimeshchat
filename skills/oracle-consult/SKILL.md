---
name: oracle-consult
description: 何时、如何向 Oracle 提问，并按困难程度路由到 oracle-lite / oracle / oracle-opus。覆盖调用纪律、上下文收集、标准 prompt 模板与追问技巧。实际启动/持久化/追问命令与模型映射见 persist-oracle。
---

# oracle-consult — Oracle 咨询工作流

> 本 skill 管「该不该问、问哪个档、怎么组织问题」。启动/持久化/追问/降级的具体命令
> 与三档模型映射见 `persist-oracle` skill。

## 默认行为：持久 Oracle

默认使用持久化 Oracle（上下文跨轮保留）。只有用户明确说「一次性」「one-shot」
「不持久化」「新实例」时才 spawn 一次性 Oracle。

| 用户意图 | 行为 |
|----------|------|
| "咨询 oracle" / "问 oracle" / "oracle review" / "找 oracle 验收" | 持久 Oracle（默认） |
| "继续 review" / "追问" / "补充证据" | 持久 Oracle（复用同一实例） |
| "一次性 oracle" / "one-shot" / "新实例" | 一次性 Oracle（不 park） |
| "独立第二意见" / "换模型" | 新持久实例（新 review key） |

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
| 安全审计 / 合规检查 / 高风险变更 | `oracle-opus` | 需证据链与严格形式化 |
| 用户明确指定档位（oracle / oracle-opus） | 按用户指定 | 无条件执行 |

**用户说「咨询 oracle」时**：默认 `oracle-lite`，除非明确指定档位
（「用 oracle」「用 oracle-opus」）或场景命中上表高难度行。

三档对应的 model/variant/system 见 `persist-oracle` 的 ExecutionSpec 推荐表。

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

沿用同一实例（持久模式），不重发全部上下文：
- 补证据：「我验证了 X，结果是 Y；这是否改变你的推荐？」
- 收敛方案：「请只在方案 A 的前提下给出最小落地步骤」
- 风险展开：「关于兼容性风险，请列出必须测试的用例」
- 反方审查：「请从反对者角度指出最可能失败在哪里」
- 拆 commit：「把方案拆成可在一个 commit 内完成的最小步骤」

## 成本优化

| 方案 | 说明 |
|------|------|
| 先 explore 再 Oracle | explore 收集后一次性传给 Oracle |
| 压缩上下文 | 只传相关文件/函数，不传整个目录 |
| 非关键用 oracle-lite | 文档/格式/测试覆盖不需要最强模型 |
| 限制轮次 | 同一 session 不超过 3-5 轮追问 |
| 持久化复用 | 多轮 review 复用同一实例，避免重复上下文 |

## 交付模式

- 持久模式（默认）：首轮启动后记录 review key，后续轮复用；命令见 `persist-oracle`。
- 一次性模式（用户显式要求）：spawn → 收回答 → 结束（不 park，不支持后续追问）。
- 降级（hot→warm→cold）由 `persist-oracle` 的 CLI 自动处理——本 skill 只声明
  「追问自动走 hot→warm→cold」，不重复其机制。
