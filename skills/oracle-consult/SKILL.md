---
name: oracle-consult
description: Oracle/顾问咨询。触发：用户明确要求咨询、存在不可本地消歧的架构 trade-off。不触发：task 派发、本地分析、代码审查。
disable-model-invocation: false
---

# oracle-consult — Oracle 咨询工作流

## 调用纪律

用户明确要求 → 无条件执行。Agent 主动咨询 → 需已穷尽自身推理 + 存在 trade-off + 上下文已就绪。

## Oracle 生命周期纪律（红线）

| 场景 | 要（MUST） | 不要（MUST NOT） |
|------|-----------|----------------|
| Oracle 运行中 | 耐心等待；推进并行工作 | **严禁** cancel/kill |
| 配额不足/连接失败 | 向用户报告 BLOCKED | **严禁**自行切换 Oracle 模型 |
| 上游中断 | `hub send` revive 同一实例 | 放弃/换模型/自行给结论 |
| 切换/取消 | **必须用户授权** | 自行决定 |

**典型违规**：遇 `403 quota` 后直接换 oracle-deepseek。正确做法：报告 BLOCKED + 等用户指示。

## 复用现有 Oracle（重要）

| 场景 | 行为 |
|------|------|
| 已有 Oracle 在运行 + 用户追加问题 | `hub send` 追加给现有实例 |
| 已有 Oracle 已结束 + 问题相关 | `hub send` revive 同一实例 |
| 问题与当前 Oracle 完全不相关 | 新起 Oracle（需说明原因） |
| 用户明确要求新 Oracle | 新起 |

**禁止**：每次提问都新起 Oracle；跳过 revive 直接新建。

## 等待纪律

1. `hub wait` **无 deadline** — 不设超时自动取消
2. **只有最终回答算完成** — progress/通知不算
3. **禁止**为「看起来卡住」重发同题/换 role/换模型
4. 上游中断 → revive 同实例 → 重新无限等待
5. 等待期间做不依赖结果的并行工作

## 顾问 role 选择（必须显式）

| Agent role | 适用场景 |
|------------|----------|
| oracle-gpt | 架构 trade-off、根因分析、风险评审 |
| oracle-opus | 复杂架构决策 |
| oracle-gemini | 长上下文、多模态 |
| oracle-deepseek / oracle-glm | 中文分析、工具调用密集 |

未指定 role 时先问用户，禁止默认选择。

## 咨询前上下文交接（强制）

当前责任人产出完整交接文档，包含：

1. **项目背景** — 技术栈、部署、关键模块、当前状态
2. **问题描述** — 触发条件、实际 vs 期望、错误日志
3. **已尝试方案** — 每个方案的细节 + 结果 + 失败原因
4. **未完成工作** — 步骤 + 阻塞点
5. **代码证据** — 文件:行号、函数签名、调用链

## 咨询方式

- 默认：直接发起，prompt 包含交接文档
- 追问：复用同一实例，不重发全部上下文
- 持久化：仅用户明确说「persist-oracle」时

## 边界

- Oracle 只提供建议，不实施改动
- 建议需人工复核
- 上下文不足时 Oracle 会先指出缺失项

## 追问技巧

沿用同一实例：补证据 / 收敛方案 / 风险展开 / 反方审查 / 拆 commit。
