# Cross-Host Mailbox Design

## Problem

tmux-agent-skills 的 mailbox 协议依赖共享文件系统（`.mailbox/` 目录）。
跨主机 Agent 无法直接通信——必须通过 Syncthing 同步或中继。

## Solution: Mailbox over SSH

将 mailbox CLI 集成到 codeagent-py，新增 `codeagent mailbox` 子命令，
底层复用 SSH transport 执行远端 mailbox 操作。

## 架构

```
┌─────────────────────────────────────────────┐
│                codeagent CLI                 │
│  codeagent mailbox send   (本地/远端)         │
│  codeagent mailbox peek   (本地/远端)         │
│  codeagent mailbox read   (本地/远端)         │
│  codeagent mailbox status (本地/远端)         │
└──────────┬──────────────────┬───────────────┘
           │                  │
     ┌─────▼─────┐    ┌──────▼──────┐
     │ Local FS   │    │ SSH Transport│
     │ mailbox    │    │ (复用已有)    │
     └───────────┘    └──────┬──────┘
                             │
                      ┌──────▼──────┐
                      │ 远端 mailbox  │
                      │ CLI          │
                      └─────────────┘
```

## 实现步骤

1. **从 tmux-agent-skills 提取 mailbox 核心**到 codeagent-py 的 `coordination/`
   - 保持现有 CLI 接口兼容
   - 作为 Python 模块可导入

2. **新增 `codeagent mailbox` 子命令**
   - 自动检测目标 host 是本地还是远端
   - 本地：直接操作文件系统
   - 远端：通过 SSH 执行 `mailbox` CLI

3. **dotai setup 同步**
   - tmux-agent-skills 的 `tools/mailbox` 安装到 PATH
   - codeagent-py 的 `codeagent mailbox` 作为统一入口

## 双向沟通示例

```bash
# Manager (host A) 发送 TASK 给 Worker (host B)
codeagent mailbox send \
  --session s1 --from manager --to worker-a \
  --kind TASK --subject "analyze rendering" --body "..." \
  --host dev-server

# Worker (host B) 发送 REPORT 给 Manager (host A)
codeagent mailbox send \
  --session s1 --from worker-a --to manager \
  --kind REPORT --subject "done" --body "..." \
  --host localhost  # 或省略 --host 表示本地
```

## 关键设计决策

1. **不合并仓库**：tmux-agent-skills 保持独立，codeagent-py 依赖其 mailbox CLI
2. **统一入口**：`codeagent mailbox` 是跨主机统一入口
3. **向后兼容**：现有 `mailbox` CLI 不受影响
4. **SSH 复用**：复用已有 ControlMaster，无额外连接开销
