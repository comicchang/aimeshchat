# codeagent-py 综合检视上下文

## 项目背景

codeagent-py 是一个跨主机代码 Agent 编排 CLI，用于在多台机器（macOS、Linux、WSL）上执行 AI 代码 Agent（codex、claude、gemini、opencode、omp）。

### 起源

- 用户有 3 天 SSHFS/FUSE 调试失败的经历（macOS 27 beta 完全不支持）
- 不用 rsync（SSD 不够）、不用 Samba（WSL 冲突）、不用 ControlPersist 1h（破坏端口转发）
- 最终方案：远程执行（SSH + wire protocol），不复制源码

### 设计约束

- 每台机器运行 `dotai setup`，不手动 pip install
- 远端部署通过 dotai setup 自动完成
- Relay-login 主机需要 PTY + expect（QR 码扫描认证）
- 部分主机在 ProxyJump、Cloudflare tunnel 或 bastion relay 后面

## 架构演进

### Phase 1: Route Parity

从 dotai 的 `code_route.py` 迁移路由功能到 codeagent-py：
- topic routing（repo-map.json → host → path）
- relay PTY 支持（setsid+TIOCSCTTY+stdin forwarding）
- SSH fallback on warm failure

### Phase 2: tmux-agent-skills 合并

tmux-agent-skills 原本是独立仓库，包含：
- Manager/Worker 编排协议（SKILL.md 文档）
- mailbox CLI（文件系统 inbox/processing/archive）
- mailbox-health、mailbox-hook 工具

合并原因：
- mailbox 协议需要与 SSH transport 深度集成（跨主机通信）
- 单一事实来源，避免协议漂移
- 统一 `uv tool install` 部署

### Phase 3: 跨主机 mailbox

`codeagent mailbox` 子命令支持 `--host` 参数：
- 本地：直接 Python 调用（零进程开销）
- 远端：wire protocol（base64 over stdin，无 shell 注入）

## 技术栈

- Python 3.10+，零外部依赖
- SSH transport：ControlMaster per host
- Wire protocol：JSONL over stdin/stdout
- Session：SQLite-backed registry
- Mailbox：文件系统 inbox/processing/archive

## 三仓现状

### codeagent-py (33b306f) — 388 tests
```
src/codeagent/
├── cli.py                    # 主 CLI
├── remote_exec.py            # 远端 helper
├── domain/                   # 数据模型
├── config/                   # repo-map.json
├── routing/                  # topic → host → path
├── runners/                  # GoWrapperRunner, OMPRunner
├── session/                  # SQLite session registry
├── transport/                # local, ssh, relay
├── wire/                     # JSONL wire protocol
├── mailbox/                  # ★ 新：mailbox 协议
│   ├── protocol.py           # Message 类型 + 校验
│   ├── store.py              # MailboxStore I/O
│   ├── cli.py                # standalone CLI
│   ├── hook.py               # peek-only notification
│   └── health.py             # 8-check diagnostics
├── coordination/             # mailbox_bridge (跨主机)
└── util/                     # paths, expand_path
skills/
├── codeagent/
├── codeagent-oracle-consult/
├── tmux-agent-manager/       # ★ 从 tmux-agent-skills 迁入
└── tmux-agent-worker/        # ★ 从 tmux-agent-skills 迁入
```

### tmux-agent-skills (bbe53bf) — 21 tests
tools/ 现在是 shim（委托到 codeagent.mailbox），原始代码保留在 mailbox.original。

### dotai (3115860)
components.json 已更新：tmux-agent-manager/worker 的 remote_source 指向 codeagent-py。

## 关键设计决策

1. **合并 vs 依赖**：合并仓库，因为 mailbox 协议与 SSH transport 有深度耦合
2. **3 模块 vs 8 模块**：protocol.py + store.py + cli.py，不过度拆分
3. **wire protocol 扩展**：新增 `mailbox` command，通过 base64 over stdin 传输
4. **shell 注入防护**：远端调用走 wire protocol，不走 shell 字符串拼接
5. **向后兼容**：standalone `mailbox` CLI 100% 兼容原版

## 已知风险

1. 远端 `codeagent mailbox` 依赖 `codeagent` 已安装
2. Syncthing + 300s lease 的软契约
3. MAILBOX_ROOT 默认值在不同机器可能不同
4. Manager/Worker SKILL.md 文档可能有遗漏的路径引用

## 方案演进历史

### 方案 1: dispatch-only（已放弃）

最初方案：`codeagent mailbox` 子命令通过 subprocess 调用远端 `mailbox` CLI。
- 优点：零协议复制，实现简单
- 缺点：远端 shell 注入风险（body 中的 `$`、反引号会被远端 shell 解析）
- oracle-lite 否决理由：过浅的集成，无法解决跨主机通信的根本问题

### 方案 2: 模块提取 + 合并仓库（当前方案）

oracle-lite 推荐：将 mailbox 协议提取为 Python 模块，合并 tmux-agent-skills 到 codeagent-py。
- 优点：单一事实来源，wire protocol 原生支持，无 shell 注入
- 缺点：需要重构，合并两个仓库
- 实现：3 模块（protocol.py + store.py + cli.py），跨主机走 wire protocol

### 方案 3: mailbox 网关服务（未采用）

每台机器运行轻量 HTTP 服务代理 mailbox 操作。
- 优点：解耦最强
- 缺点：引入额外依赖，部署复杂
- 未采用理由：对于内部工具过度工程化

## 许可证决策

使用 Enlightened Public License (EPL-1.0)，源自 comicchang/iitc-mcp：
- 允许：非 Ingress 玩家 + Enlightened 阵营玩家
- 禁止：Resistance 阵营 + Machina（AI 阵营）
- 理由：用户自己的 Ingress 主题许可证，用于 codeagent-py 的自嘲式条款
- 文件：LICENSE（已提交）

## codeagent-wrapper 致谢

用户要求在 README 中对 codeagent-wrapper 给予合适的引用和致敬：
- codeagent-wrapper 是 Go 实现的 CLI，由 github.com/stellarlinkco/myclaude 分发
- `.codeagent/` 目录和 `~/.codeagent/models.json` 来自 myclaude 体系
- codeagent-py 的 GoWrapperRunner 包装了这个二进制
- ACKNOWLEDGEMENTS 段已写入 README（待验证）

## Oracle 历史评审

### Round 1 (3/10)
- P0: dotai install order broken
- P0: remote helper entrypoint
- P0: mailbox/tmux authority
- P0: tmux-agent docs
- P0: mailbox CLI security
- P0: execution core bugs

### Round 2 (8/10)
- P2: duplicate expand_path
- P2: mailbox cmd_stats raw glob
- P2: adapters/launchers zero tests
- P2: relay wire parser
- P2: commands/ dead code
- P2: registry positional indices
- P2: orchestrator warn-not-fail

### Round 3 (7.5/10)
- P1 回归: relay catch ValueError
- P2: coordination __version__ duplicate
- P2: config expand_path re-export

### 当前预估: 9/10
