---
name: postmesh
description: 使用 postmesh CLI 在本机或 repo-map 注册的远程仓执行代码 Agent。触发条件：目标源码不在当前机器、跨机器/多仓调研、需要持久 session、SSH/relay-login 路由。
requires:
  online: true
---

# postmesh — 多主机代码任务唯一入口

> **编排协议**: `skill://agent-swarm/` — tmux-agent 编排协议（Manager/Worker 角色、INIT 握手、v2 direct inbox）
> **部署模式**: `skill://agent-swarm/SKILL.md#deployment-modes` — Shared FS vs Remote Transport 决策树
> **默认模式**: B (Remote Transport) — 无共享文件系统，跨主机通信走 SSH wire protocol

## 何时使用

满足任一条件时使用 `postmesh route`：
- 目标仓不在当前 CWD
- topic 有 `.repo-map.json`
- 目标 host 与本机 hostname 不匹配
- 需要在 SSH/relay-login 主机运行工具
- 需要持久 session

不要路由：
- CWD 已位于目标仓且本机可直接 Read/Grep/LSP
- 纯文档 topic 无 repo-map
- 只需处理当前本地文件

## 必做判断树

```
1. CWD 是否已在目标仓内？
   是 → 直接本地工具
2. postmesh route where <topic> 能否找到映射？
   否 → 本地文档或先补配置
   是 → 继续
3. 输出 repo 的 host 是否匹配本机 hostname？
   是 → 进入该本地 path 分析
   否 → postmesh route
4. 多 repo topic 先 where，再显式 --repo N
5. relay-login 主机预期出现 PTY/QR；不要改成 stdin pipe
```

## 命令速查

```bash
# 路由
postmesh route list
postmesh route where <topic>
printf '%s\n' '<task>' | postmesh route <topic> --repo 0 --backend codex --agent explore
postmesh route <topic> '<task>' --dry-run

# 直接执行
postmesh run '<task>' <workdir> --host <host> --backend codex --agent develop

# Session 管理
postmesh sessions list [--host H] [--topic T]
postmesh sessions show '<key>'
postmesh sessions reset '<key>'
postmesh sessions bind --key '<key>' --id '<backend-session-id>'

# SSH 连接
postmesh ssh warm <host...>
postmesh ssh status
postmesh ssh stop <host...>

# Mailbox（跨主机通信）
postmesh mailbox send --session s1 --from manager --to w1 --kind TASK ... --host dev-server
postmesh mailbox peek --session s1 --agent w1 [--host dev-server]
postmesh mailbox read --session s1 --agent w1 --owner w1 [--host dev-server]

# Swarm IPC（高级 IPC）
postmesh swarm create-session s1 --manager mgr --members w1,w2
postmesh swarm register s1 --agent w1 --host dev-server
postmesh swarm direct s1 --from mgr --to w1 --kind TASK --subject hi --body "..."
postmesh swarm poll s1 --agent w1
postmesh swarm watch s1 --agent w1 --interval 2
```

## Mailbox & Swarm

`postmesh mailbox` 和 `postmesh swarm` 是跨主机通信的核心工具。详见 `skill://agent-swarm/` 编排协议。

- `postmesh mailbox ... --host <alias>`: 跨主机 mailbox 操作（底层 SSH wire protocol）
- `postmesh swarm ...`: 高级 IPC（session/roster/ACL/routing、delivery engine）
- 本地 mailbox: 直接使用 `mailbox` CLI（PATH command）

**部署模式默认值**: Remote Transport (Mode B)。如需使用 Shared FS (Mode A)，必须显式设置 `MAILBOX_ROOT=.mailbox`。详见 `skill://agent-swarm/SKILL.md#deployment-modes`。

## Session 规则

默认 auto-resume：同一 host+workdir+backend+agent 继续同一上下文。

用 `--new-session` 的场景：
- 问题目标已完全变化
- 旧上下文被错误假设污染
- 涉及敏感隔离
- 需要独立对照实验

显式 `--session-key` 推荐格式：`<project>:<role>:<domain-or-topic>`。不要只写 `oracle`。

## 从 code_route.py 迁移

| 旧命令 | 新命令 |
|--------|--------|
| `python3 code_route.py list` | `postmesh route list` |
| `python3 code_route.py where T` | `postmesh route where T` |
| `echo TASK \| python3 code_route.py route T --backend B` | `printf '%s\n' TASK \| postmesh route T --backend B` |
