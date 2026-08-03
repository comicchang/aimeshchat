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

## Session 复用纪律（同一任务多轮 Review）

**同一任务的所有 review 轮次必须复用同一个 oracle session**，让 oracle 持有完整
上下文：已审内容、已否决方案、每轮演变的理由。开新 session 仅限以下显式场景：
- 用户明确要求"换一个 oracle / 重新 review"
- 更换模型（不同 model 的 oracle preset，如 oracle → oracle-arch）
- 议题完全变化（不同 topic，如从性能评审转向安全评审）

### 自动路径选择（无需人工判断）

**第 1 步 — 检测运行 harness**：本会话能否调用 `hub`/`task` 工具（peer messaging
可用、能 spawn subagent）？
- **能 → OMP harness，走路径 B**（默认）：oracle 系列已配置 `auto-exit: false`，
  任务完成后 parked，可被 `hub send` 唤醒——会话上下文完整保留（含工具历史）。
- **不能 → 非 OMP（opencode/codex/claude 等），走路径 A**：codeagent 持久
  session-key 复用（所有 backend 通用）。

**第 2 步 — 多轮 review 固定同一 oracle 实例**：路径 B 记录首轮 `task` 返回的
`agent://<id>`，后续轮 `hub send` 到该 id；路径 A 固定同一 `--session-key`。
不要每轮重新 spawn / 新 session。

### 路径 A — codeagent 持久 session（非 OMP backend 通用）

同任务固定同一 session-key（`<project>:oracle:<domain>:<topic>`），不随轮次递增。
轮次间不重发全部上下文，只发增量并指向已有结论：
```
上一轮你推荐方案 A（理由：...）。我落地时遇到 X（新证据），是否改变推荐？
请只审查我新增的变更 Y（相对上一轮）。
```
多轮后上下文膨胀由 codeagent 侧 compaction 处理；不要因此新建 session。

### 路径 B — OMP 原生 revive（默认，已验证）

OMP 的 parked agent 可被 `hub send` 唤醒并恢复完整会话——这是唯一 resume 原语
（task 工具无 resume 参数）。
- 首轮：`task` spawn oracle，记下返回的 `agent://<id>`
- 后续轮：`hub send` 到该 `<id>`，带增量问题；不要重新 task spawn
- 已真实验证：第 1 轮回答后 agent parked，第 2 轮 `hub send` 唤醒后确认
  能看到自己第 1 轮的答案（上下文完整保留）
- 注意：parked 实例进程常驻（registry 进程级，omp 重启后丢失）；多轮后
  释放或避免堆积；并发唤醒由 bus 串行处理

### 路径 B 的可靠 Fallback（mailbox 轮询）

OMP 17.2.4 的 omp-mailbox-plugin extension 加载有竞态缺陷（`--extension` 显式
路径约 33% 成功率，自动发现 0%——详见 omp-mailbox-plugin/docs/）。依赖该插件
的"外部消息触发唤醒"不可靠。**可靠 fallback：agent 主动轮询**——prompt 引导
oracle 在等待期间定期 `codeagent mailbox peek --session <sid> --agent <id>`，
有消息即 `read` + 处理 + `finalize`。此路径 100% 可用（不依赖 OMP extension
管线），是真机验证过的唤醒方式（79140/后续会话均通过轮询完成处理）。

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
