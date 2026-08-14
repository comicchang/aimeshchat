---
name: aimeshchat-cli
description: 使用 aimeshchat CLI 在本机或 repo-map 注册的远程仓执行代码 Agent。触发条件：目标源码不在当前机器、跨机器/多仓调研、需要持久 session、SSH/relay-login 路由。
requires:
  online: true
---

# aimeshchat — 多主机代码任务唯一入口

> **编排协议**: `skill://agent-swarm/` — mailbox + gateway 编排协议（Manager/Worker 角色、INIT 握手、v2 session-based）
> **部署模式**: `skill://agent-swarm/SKILL.md#deployment-modes` — Mode A (Shared FS) vs 跨主机 transport 决策树
> **默认拓扑**: 跨主机（无共享 FS）— SSH wire protocol / relay-login 传输

## 何时使用

满足任一条件时使用 `aimeshchat route`：
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
2. aimeshchat route where <topic> 能否找到映射？
   否 → 本地文档或先补配置
   是 → 继续
3. 输出 repo 的 host 是否匹配本机 hostname？
   是 → 进入该本地 path 分析
   否 → aimeshchat route
4. 多 repo topic 先 where，再显式 --repo N
5. relay-login 主机预期出现 PTY/QR；不要改成 stdin pipe
```

## 命令速查

```bash
# 路由（同步，阻塞到完成）
aimeshchat route list
aimeshchat route where <topic>
printf '%s\n' '<task>' | aimeshchat route <topic> --repo 0 --model <provider/model>
aimeshchat route <topic> '<task>' --dry-run

# 路由（异步，后台执行，立即返回 job ID）
aimeshchat route <topic> '<task>' --background
aimeshchat job status <job_id>    # 查进度
aimeshchat job wait <job_id>      # 等完成

# 直接执行（同步/异步）
aimeshchat run '<task>' <workdir> --host <host> --model <provider/model>
aimeshchat run '<task>' <workdir> --background  # 异步

# Session 管理
aimeshchat sessions list [--host H] [--topic T]
aimeshchat sessions show '<key>'
aimeshchat sessions reset '<key>'
aimeshchat sessions bind --key '<key>' --id '<backend-session-id>'

# SSH 连接
aimeshchat ssh warm <host...>
aimeshchat ssh status
aimeshchat ssh stop <host...>

# Mailbox（跨主机通信）
aimeshchat mailbox send --session s1 --from manager --to w1 --kind TASK ... --host dev-server
aimeshchat mailbox peek --session s1 --agent w1 [--host dev-server]
aimeshchat mailbox read --session s1 --agent w1 --owner w1 [--host dev-server]

# Swarm IPC（高级 IPC）
aimeshchat swarm create-session s1 --manager mgr --members w1,w2
aimeshchat swarm register s1 --agent w1 --host dev-server
aimeshchat swarm direct s1 --from mgr --to w1 --kind TASK --subject hi --body "..."
aimeshchat swarm poll s1 --agent w1
aimeshchat swarm watch s1 --agent w1 --interval 2

# Gateway（跨设备运行时控制面，v2）
aimeshchat gateway ensure --host <H>      # 远端预检（wire v2）+ 启动 gateway
aimeshchat gateway status                 # 本机 gateway 状态
aimeshchat gateway rpc --stdio            # SSH 有界控制（session.ensure/runtime.spawn/send/stop）
aimeshchat events watch --session <id> --cursor <c> --jsonl   # 观察事件流（断线补流）
```

## Mailbox & Swarm

`aimeshchat mailbox` 和 `aimeshchat swarm` 是跨主机通信的核心工具。详见 `skill://agent-swarm/` 编排协议。

- `aimeshchat mailbox ... --host <alias>`: 跨主机 mailbox 操作（底层 SSH wire protocol）
- `aimeshchat swarm ...`: 高级 IPC（session/roster/ACL/routing、delivery engine）
- `aimeshchat gateway ...`: 跨设备运行时控制面（v2，见上）
- 本地 mailbox: 直接使用 `mailbox` CLI（PATH command）

**默认拓扑**: 跨主机（无共享 FS）。如需 Shared FS (Mode A)，必须显式设置 `MAILBOX_ROOT=.mailbox`。详见 `skill://agent-swarm/SKILL.md#deployment-modes`。

## Session 规则

默认 auto-resume：同一 host+workdir+backend+agent 继续同一上下文。

用 `--new-session` 的场景：
- 问题目标已完全变化
- 旧上下文被错误假设污染
- 涉及敏感隔离
- 需要独立对照实验

仅关闭自动续接而不换上下文 → `--no-auto-resume`。

显式 `--session-key` 推荐格式：`<project>:<role>:<domain-or-topic>`。不要只写 `oracle`。

## 异步执行模式（重要）

远程任务可能需要数分钟甚至更长时间。**不要用同步模式等待**，会超时。

正确做法：
```bash
# 1. 提交异步任务（立即返回 job ID 到 stderr）
aimeshchat route 12-OHOS '分析 SpatialGlass 效果调用链' --background
# stderr: [background] route job submitted: <12位hex> (pid=NNN)

# 2. 查进度（不阻塞）
aimeshchat job status <job_id>

# 3. 等完成（阻塞，结果在 stdout JSON 的 stdout 字段）
aimeshchat job wait <job_id>

# 4. 取结果（job wait 返回的 JSON 包含 stdout）
# 或直接读: $XDG_STATE_HOME/aimeshchat/jobs/<job_id>/result.json
```

**关键**：
- `--background` 立即返回 job ID（在 stderr），任务在后台运行
- `job wait` 返回 JSON，结果在 `stdout` 字段
- `job list` 列出所有后台 job
- `job wait --timeout <秒>` 可设超时（默认 0=永久）

## 输出过滤禁令

**调用 aimeshchat 命令时，禁止使用提前退出的管道（`| head`/`| tail`/`| grep`）过滤输出。**

原因：
- `route --background` 的 job ID 打印到 **stderr**，stdout 为空，`| tail` 抓不到 job ID
- 提前退出消费者（如 `| head`）会触发 SIGPIPE 杀死命令

允许的管道：
```bash
# ✓ 结构化转换（--json 输出后处理）
aimeshchat oracle result "$KEY" | python3 -c "import sys,json; ..."
aimeshchat oracle gc --json | jq '.cleaned'
```

禁止的管道：
```bash
# ✗ 提前退出过滤
aimeshchat route 12-OHOS '任务' --background | tail -5
aimeshchat oracle status "$KEY" | grep "runtime_id"
```

## 从 code_route.py 迁移

| 旧命令 | 新命令 |
|--------|--------|
| `python3 code_route.py list` | `aimeshchat route list` |
| `python3 code_route.py where T` | `aimeshchat route where T` |
| `echo TASK \| python3 code_route.py route T --backend B` | `printf '%s\n' TASK \| aimeshchat route T --backend B` |
