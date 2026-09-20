---
name: project-understanding
description: 结构化理解项目。触发：用户要求「理解项目」「读项目结构与历史」、新接手项目。不触发：已有上下文的会话。
disable-model-invocation: false
---

# project-understanding — 结构化理解项目
## §1 触发与 DO/DON'T

触发：理解项目、读取结构/历史/文档，或新接手目录建立可复用模型。不触发：当前会话已有可靠项目上下文且任务无需重新建模。

| DO | DON'T |
|---|---|
| 分层读取 README、AGENTS、索引，再用 glob 列结构并按需深读 | 一次读取项目根目录或无边界倾倒大文件 |
| memory 命中按 source 核对 cwd，跨项目命中标噪音；无命中就回到项目文档 | 未核对归属就把 memsearch 命中当项目事实 |
| 输出事实、证据路径、待确认、下一步，并写入 `local://<project>-model.md` | 只写叙述摘要或把推测写成事实 |
| 为每步记录完成条件和缺失文件的替代路径 | 文档缺失时停止或凭经验补全 |

## §2 执行验收

1. 定位入口：读取规则/索引并列目录；完成条件：关键入口和范围明确。
2. 深读：只读索引指向内容，分段处理大文件；完成条件：关键事实有来源。
3. 历史：检查 git、memory、session；完成条件：归属和时间范围已核对。
4. 建模：落盘结构化模型；完成条件：待确认项与下一步可直接复用。

交叉引用：OMP/OpenCode 历史分别见 `skill://omp-history-reader/`、`skill://opencode-history-reader/`；文档写作见 `skill://skill-and-agents-md-writing-guide/`。

## 何时使用

用户要求「理解项目」「读项目结构/文档目录结构/历史」，或说「通过 Memsearch、历史对话、文档理解项目」，或新接手一个项目/目录需要建立可复用的项目认知时。

## 三条铁律（历史踩坑实证）

1. **分层读取** — 禁止一次 `read` 项目根目录（实测一次灌入 32k tokens，收益极低）。
2. **memsearch 按 source 核对项目归属** — 跨项目命中是噪音，不当知识。
3. **输出结构化项目模型** — 不写叙述摘要（接真实任务即失效，需重读）。

## 流程

### 第 1 步：定位入口（分层，不灌全量）

- 读 `README.md`（项目定位）+ `AGENTS.md`（若存在，仓库规则）
- 读文档索引文件（如 `notes/INDEX.md`、`.refs/README.md`、`INDEX.md`）
- 用 `glob` 列目录结构（只列名，不读内容）

### 第 2 步：按需深读

- 只读索引指向的关键文档，不读无关文件
- 大文件（>50KB）用 `read` 的 offset/limit 分段，不整读

### 第 3 步：历史与记忆（带纪律）

- `git log --oneline -20` / `git status --short`（近期变更 + 未提交状态）
- `memory_search` 前先 `memory_stats` 确认当前 scope
  - 每条命中**必须核对 source 路径是否属于当前 cwd 项目**
  - 跨项目命中 → 标注 `[跨项目噪音，忽略]`，不当作项目知识
  - 无当前项目命中 → 跳过 memsearch，改读项目文档
- 历史对话：用 `session-history-reader` skill，或 `hub` 查 peer

### 第 4 步：输出结构化项目模型（落盘复用）

```markdown
## 项目模型
### 事实
- 技术栈、证据源、关键路径
### 证据路径
- 哪份文档 → 什么结论
### 待确认
- 明确标 [待确认]，不脑补
### 下一步
- 用户真实任务的切入点
```

写入 `local://<项目>-model.md`，后续任务直接引用，不重新读。

## 反模式

| 做法 | 问题 |
|---|---|
| `read` 项目根目录 | 32k tokens 灌入，理解成本远超收益 |
| memsearch 结果不核对 source 就当知识 | 跨项目噪音污染（实测 score 0.98 全无关） |
| 输出叙述摘要 | 接真实任务即失效，需重读文档 |
