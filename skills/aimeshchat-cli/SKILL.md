---
name: aimeshchat-cli
description: 多主机代码 Agent CLI。触发：目标源码不在当前机器、跨机器调研、需持久 session。不触发：本地编码、单机任务。
disable-model-invocation: true
---

# aimeshchat — 多主机代码任务唯一入口
## §1 触发、边界与 DO/DON'T

触发：目标仓不在当前机器、跨机器调研、持久 session、受限路径需授权委派。不触发：当前 CWD 可直接读取的本地编码、单机任务、无 repo-map 的纯文档 topic。

| DO | DON'T |
|---|---|
| 先用判断树和 `route where` 确认 host/path，再选择 `run`、`route` 或本地工具 | 猜测 host/path，或对本地可读任务强行远程路由 |
| 跨主机走 `swarm`/`mailbox --host`，以匹配 request_id 的 REPORT 和 artifact 校验作为完成证据 | 用 tmux send-keys、capture-pane 或 status 残留代替 mailbox 生命周期 |
| 让 CLI 自动管理远程 timeout；失败按错误分类重试一次后报告 blocker 或合法降级 | 手工传远程 timeout、无限重试，或 gateway 失败时手工启动 tmux |
| 记录命令、stderr、host、session/job ID 和验收结果 | 用 `head`/`tail`/`grep` 提前截断 CLI 输出，丢失状态或触发 SIGPIPE |

## §2 执行验收

1. 定位：确认 host、workdir、权限与 session key；完成条件：输入可复现。
2. 执行：按 §1 选择入口；完成条件：job/session 已建立并记录 ID。
3. 回收：等待匹配终态并校验附件；完成条件：REPORT、size、sha256 均通过。
4. 输出：报告产物、证据、降级能力和 blocker；完成条件：无未标注推测。

交叉引用：编排协议见 `skill://agent-swarm/`；Oracle 持久 review 见 `skill://persist-oracle/`。

> **编排协议**: `skill://agent-swarm/` — mailbox + gateway 编排协议（Manager/Worker 角色、INIT 握手、v2 session-based）
> **部署模式**: `skill://agent-swarm/SKILL.md#deployment-modes` — Mode A (Shared FS) vs 跨主机 transport 决策树
> **默认拓扑**: 跨主机（无共享 FS）— SSH wire protocol / relay-login 传输

## 何时使用

满足任一条件时使用 aimeshchat（按下方判断树选择 `run` 或 `route`）：
- 目标仓不在当前 CWD
- topic 有 `.repo-map.json`
- 目标 host 与本机 hostname 不匹配
- 需要在 SSH/relay-login 主机运行工具
- 目标路径受项目访问限制不可直读（如 vendor/），需委派授权模型——即使 CWD 已在目标仓内

不要路由：
- CWD 已位于目标仓且本机可直接 Read/Grep/LSP。该排除仅在当前会话模型有权直读目标路径时成立
- 纯文档 topic 无 repo-map
- 只需处理当前本地文件

## 必做判断树

```
1. CWD 是否已在目标仓内？
   是 → 当前会话模型可直读？可 → 直接本地工具；否 → 本机 aimeshchat run 委派授权模型
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
printf '%s\n' '<task>' | aimeshchat route <topic> --repo 0
aimeshchat route <topic> '<task>' --dry-run

# 异步执行（route/run 均支持 --background；先 where 查映射）
aimeshchat route where <topic>                        # 查 host/path
aimeshchat run '<task>' <workdir> --host <host> --background   # 立即返回 job ID（stderr）
aimeshchat job list                                   # 列出后台 job
aimeshchat job status <job_id>                        # 查进度（不阻塞）
aimeshchat job wait <job_id> [--timeout <秒>]         # 阻塞到完成，结果在 JSON 的 stdout 字段

# 直接执行（同步）
aimeshchat run '<task>' <workdir> --host <host>

# Session 管理
aimeshchat sessions list [--host H] [--topic T]
aimeshchat sessions show '<key>'
aimeshchat sessions reset '<key>'
aimeshchat sessions bind --key '<key>' --id '<backend-session-id>'

# SSH 连接
aimeshchat ssh warm <host...>
aimeshchat ssh status
aimeshchat ssh stop <host...>

# Mailbox（跨主机通信；--host 省略=本地）
aimeshchat mailbox session-init --session <sid> --manager manager --agents w1,w2 --host <H>
aimeshchat mailbox send --session <sid> --from manager --to w1 --kind TASK \
  --subject '...' --body '{"request_id":"req1","run_id":"r1"}' --host <H>
aimeshchat mailbox peek --session <sid> --agent w1 --host <H>      # 非破坏：数量+预览
aimeshchat mailbox read --session <sid> --agent manager --owner manager --host <H> --json  # manager 拉 REPORT
aimeshchat mailbox read --session <sid> --agent w1 --owner w1 --host <H>                   # worker 消费
aimeshchat mailbox finalize --session <sid> --agent w1 --msg-id <id> --owner w1 --host <H>
aimeshchat mailbox release --session <sid> --agent w1 --msg-id <id> --owner w1 --host <H>  # 处理失败退回 inbox
aimeshchat mailbox stats --session <sid> --agent w1 --host <H>     # inbox/processing/archive/_corrupt 计数
aimeshchat mailbox recover-stale --session <sid> --agent w1 --host <H>  # >300s 租约回收
aimeshchat mailbox clear --session <sid> --agent w1 --host <H>     # 仅任务+回执完全处理完后清 archive

# Swarm IPC（高级 IPC，SessionManifest-aware）
aimeshchat swarm create-session <sid> --manager manager --members w1,w2
aimeshchat swarm register <sid> --agent w1 --host <H> --backend omp
aimeshchat swarm direct <sid> --from manager --to w1 --kind TASK --subject '...' --body '...'
aimeshchat swarm poll <sid> --agent w1                 # local-only 轮询
aimeshchat swarm watch <sid> --agent w1 --interval 2   # 轮询循环
aimeshchat swarm launch <sid> --bootstrap --pull --poll-interval 5   # 一键 bootstrap 远端 worker + manager-pull

# Artifact（REPORT 附件拉取与校验）
aimeshchat artifact pull --host <H> --artifact-id <id> --relative-path <p> \
  --size <n> --sha256 <hex> --dest <local-path>
aimeshchat artifact verify --file <local-path> --size <n> --sha256 <hex>

# Gateway（跨设备运行时控制面，v2）
aimeshchat gateway ensure --host <H>      # 远端预检（wire v2）+ 启动 gateway
aimeshchat gateway status                 # 本机 gateway 状态
aimeshchat gateway rpc --stdio            # SSH 有界控制（session.ensure/runtime.spawn/send/stop）
aimeshchat events watch --session <sid> --cursor <c> --jsonl   # 观察事件流（断线补流）

# Park（auto-exit:false 实例生命周期，如 oracle 系列）
# --agent-type 传部署配置中的 agent role
aimeshchat park acquire <review_key> --agent-type <agent-role> --peer-id <id>
aimeshchat park renew <review_key>
aimeshchat park release <review_key>
aimeshchat park sweep
```

## Mailbox & Swarm 关系

`mailbox`（leaf transport）与 `swarm`（SessionManifest-aware routing）是两层：
- 跨主机必须走 `swarm` 或 `mailbox --host <H>`；本地共用 FS 时 PATH 命令 `mailbox` 即可
- Manager 的唯一入口是 `swarm` 子命令；bare `mailbox send` 仅用于 bootstrap 和故障诊断
- Worker 的唯一入口是 `mailbox read` + 两阶段消费
- 详见 `skill://agent-swarm/`

**默认拓扑**: 跨主机（无共享 FS）。如需 Shared FS (Mode A)，必须显式设置 `MAILBOX_ROOT=.mailbox`。详见 `skill://agent-swarm/SKILL.md#deployment-modes`。

## §3 远程失败与迁移参考

Gateway 失败、远程 Worker 状态、SSH alias/重试、manager-pull 降级和 artifact 验证：详见 `references/gateway-operations.md`。

旧 wrapper、`code_route.py`、`--parallel`、受限路径委派、session resume 与跨实例身份：详见 `references/legacy-migration.md`。

主文件保留的硬边界：跨主机任务以匹配 `request_id` 的 REPORT 加 artifact size/sha256 校验完成；远程 worker 不使用手工 tmux 注入；远程 timeout 由 CLI 自动管理。
