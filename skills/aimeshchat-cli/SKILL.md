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

# 异步执行（route 无 --background；先 where 查映射，再 run --background）
aimeshchat route where <topic>                        # 查 host/path
aimeshchat run '<task>' <workdir> --host <host> --background   # 立即返回 job ID（stderr）
aimeshchat job list                                   # 列出后台 job
aimeshchat job status <job_id>                        # 查进度（不阻塞）
aimeshchat job wait <job_id> [--timeout <秒>]         # 阻塞到完成，结果在 JSON 的 stdout 字段

# 直接执行（同步）
aimeshchat run '<task>' <workdir> --host <host> --model <provider/model>

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
aimeshchat park acquire <review_key> --agent-type oracle --peer-id <id>
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

## Gateway 失败降级

**问题背景**：`aimeshchat gateway ensure --host <H>` 失败时，agent 曾回退到手动 `ssh + tmux new-session/send-keys` 编排远程 worker。这是**唯一被禁止的出口**——见下方「禁止清单」。

**降级合法性**：本章节的 `mailbox ... --host <H>` 派发路径是「Manager 唯一入口是 swarm」规则的 **sanctioned exception**——仅在 `gateway ensure` 失败且远端 worker 存活时使用；gateway 可用时仍必须走 swarm 路由。

**架构事实（决定降级边界）**：

| 层 | 依赖 | gateway 失败后 |
|---|---|---|
| mailbox transport（`mailbox ... --host <H>`） | 仅 SSH ControlMaster + 远端 `aimeshchat-remote-exec` | ✅ 可用 |
| swarm launch bootstrap（session-init + INIT send） | 同上 | ✅ 可用 |
| 远端 worker 进程**启动/监督**（runtime.spawn） | gateway（tmux supervisor，唯一合法 spawn 路径） | ❌ 不可用 |
| 事件流（`events watch`）/ park revive / message receipts | gateway | ❌ 不可用 |

即：**gateway 是远端 worker 的启动器，不是 mailbox 的依赖**。gateway 失败时任务派发与回程仍可走 mailbox 直连，但前提是远端 worker 进程**已经存活**。

### 处理流程（gateway ensure 失败时）

```
gateway ensure --host <H> 非零退出
  │
  ├─ 1. 记录完整 stderr（REMOTE_UPGRADE_REQUIRED / remote wire probe failed /
  │        remote gateway start failed / REMOTE_RPC_TIMEOUT …）
  │
  ├─ 2. 远端可达性 + wire 版本探测（gateway 之外的独立检查）：
  │        aimeshchat ssh warm <H>
  │        aimeshchat ssh status
  │      探测失败（命令不存在/SSH 拒绝）→ 这是前置条件缺失，直接报 blocker：
  │        修复项、已尝试命令、远端错误原文。禁止尝试任何变通启动。
  │
  ├─ 3. 错误分类：
  │        REMOTE_UPGRADE_REQUIRED（wire < 2）→ 终态 blocker，无 legacy 降级，禁止重试
  │        tmux/socket 类（如 "server exited unexpectedly"）→ 重试一次
  │          （可能是一次性 tmux server 冷启动失败）；仍失败进第 4 步
  │        其它（SSH/超时/远端命令缺失）→ 重试一次后进第 4 步
  │
  ├─ 4. 降级到 mailbox 直连（见下）——仅当远端 worker 已存活
  │
  └─ 5. 无 worker 存活 → 报 blocker 终止任务：错误、已尝试命令、
         需要的修复（如远端重启 gateway/tmux server）。NEVER 手动 tmux。
```

**重试纪律**：同一失败原因最多重试 1 次；再失败必须降级或上报，禁止循环重试同一命令。

### 不依赖 gateway 的降级方案：mailbox 直连

任务派发与回程全部走 `mailbox ... --host <H>`（manager-pull，单向上依赖 Manager→Worker SSH）：

```bash
# ① 预建 SSH ControlMaster（mailbox transport 复用）
aimeshchat ssh warm <H>

# ② session 初始化（远端 inbox 目录 + 身份）
aimeshchat mailbox session-init --session <sid> --manager manager --agents w1,w2 --host <H>
#   等价一体化入口：aimeshchat swarm launch --bootstrap（同走 SSH，不经 gateway）

# ③ 派发 TASK（inbox append-only，天然不打断）
aimeshchat mailbox send --session <sid> --from manager --to w1 --kind TASK \
  --subject '...' --body '{"request_id":"req1","run_id":"r1"}' --host <H>

# ④ manager-pull 回程（与正常路径相同）
aimeshchat mailbox read --session <sid> --agent manager --owner manager --host <H> --json
aimeshchat mailbox finalize --session <sid> --agent manager --msg-id <id> --owner manager --host <H>
```

**降级前提（必须全部满足）**：
- 远端 worker 进程已存活且正常轮询本地 inbox（OMP 插件 polling）
- 满足 Dispatch Gate：`status IDLE/DONE/BLOCKED` 且无未处理 REPORT 才发 TASK
- 回程 manager-pull；Worker 侧 status/REPORT 经 `--host` 读取，与正常路径同规约

**降级代价（如实上报，不假装 gateway 能力）**：
- 无 `events watch` 事件流 → 进度只能靠 `mailbox peek/stats` + `ssh <H> cat status.json` 轮询
- 无 message receipts（READ/RUNNING 回流）→ 只有 `require_ack` 关闭时的两阶段消费可确认读取
- park revive / runtime stop 不可用 → 这些操作在 gateway 恢复前一律**报 blocker**，不绕过

**worker 存活判定**（不依赖 gateway，全部走 `--host` / ssh cat）：

```bash
aimeshchat mailbox stats --session <sid> --agent <w> --host <H>      # 计数可达 → transport 通
ssh <H> "cat <mailbox-root>/<sid>/<w>/status.json"                    # 5 字段快照
```

stats 命令成功 + status.json 存在 ≠ 进程存活（文件可能是历史残留）；**只有 worker 进程实际消费（inbox 计数下降、processing/archive 推进）才算存活**。无法确认存活 → 走第 5 步报 blocker。

### 禁止清单（gateway 失败时同样适用）

| 禁止 | 原因 |
|---|---|
| `ssh <H> tmux new-session/new-window` 手工创建 worker 会话 | gateway 是远端 worker 唯一合法启动器；绕过即破坏 supervisor/plugin 握手与事件账本 |
| `ssh <H> tmux send-keys` 向 worker stdin 注入任务 | 打断正在生成的 assistant 回合；不证明送达也不证明读取 |
| `tmux capture-pane` 读终端文本推断状态 | 状态只从 status.json + inbox/REPORT 读取 |
| `ssh <H> "cat > status.json"` 等手写状态 | 所有 mailbox/status 变更必须走 CLI（`mailbox status` 为 worker 自报） |
| gateway 失败后不探测 worker 直接放弃 | 先做第 2/4 步，worker 存活就该走 mailbox 直连完成任务 |

**正确做法**：gateway 失败 → 探测可达性与 wire → 分类重试 → worker 存活则 mailbox 直连完成全部可交付工作 → 能力缺口（events/park/receipts）如实上报为降级代价，而不是用 tmux 手搓补上。

### 管道禁令（reinforce）

调用 aimeshchat 命令时**永远禁止**提前退出管道（`| head`/`| tail`/`| grep`）：`run --background` 的 job ID 在 **stderr**，stdout 为空，`| tail` 抓不到；`| head` 触发 SIGPIPE 杀死命令并让 timeout 机制失效。只允许结构化转换管道（`--json` 输出后处理，见「输出过滤禁令」）。

## 远程 Worker 管理

编排远程 Worker（跨主机 mailbox-worker）时，**禁止 `ssh + tmux send-keys` 向远程 OMP 进程注入任务**：

- send-keys 写入 worker 进程 stdin，会**打断正在生成的 assistant 回合**（任务被腰斩）
- send-keys 成功既不证明送达也不证明读取；只有 mailbox 文件和 status/REPORT 能证明进度
- 远程主机没有共享 tmux socket，send-keys 路径**根本不存在**
- `capture-pane` 读终端文本推断状态同样禁止——状态只从 status.json + inbox 读取

### 状态源（判断在线 / 空闲 / 忙碌）

| 信息 | 命令 | 含义 |
|---|---|---|
| Worker 计数 | `aimeshchat mailbox stats --session <sid> --agent <w> --host <H>` | inbox/processing/archive/_corrupt 计数 |
| 状态快照 | `ssh <H> "cat <mailbox-root>/<sid>/<w>/status.json"` | 5 字段：state/current_task/last_conclusion/updated_at |
| 待收 REPORT | `aimeshchat mailbox peek --session <sid> --agent manager --host <H>` | manager 在远程 host 的 inbox 是否有未消费消息 |

**IDLE 判定 = 三者同时成立**：`status.json.state == IDLE` + `stats` 中 inbox=0 + manager inbox 无未消费 REPORT。

注意：`mailbox status` 是**只写**命令（worker 自报状态）；Manager 侧读状态用 `stats` + ssh cat status.json。status.json 只是 availability snapshot，任务终态以 mailbox 事件账本（REPORT + request_id）为准。

### 等待完成（manager-pull 轮询）

每 5 秒循环，直到收到匹配 `request_id` 的 REPORT：

```bash
# ① 拉 REPORT（两阶段消费第一步）
aimeshchat mailbox read --session <sid> --agent manager --owner manager --host <H> --json
# ② 验证：REPORT 的 request_id 匹配 TASK；附件用 artifact pull 拉取，sha256/size 与实物一致
aimeshchat artifact pull --host <H> --artifact-id <id> --relative-path <p> --size <n> --sha256 <hex> --dest <local>
# ③ finalize 归档（未验证前不要 finalize）
aimeshchat mailbox finalize --session <sid> --agent manager --msg-id <id> --owner manager --host <H>
```

- 终态以收到匹配 `request_id` 的 REPORT 为准，**不是** status DONE
- REPORT 校验失败（sha256/size 不符）→ 拒绝并要求 worker 重发，不进入下一任务
- v2 替代：`aimeshchat gateway ensure --host <H>` + `aimeshchat events watch --session <sid> --cursor <c> --jsonl` 事件流观察
- gateway ensure 失败 → 见「Gateway 失败降级」：worker 存活则 mailbox 直连完成派发与回程，无事件流
- `aimeshchat job wait <job_id>` 只适用于 `run --background` 场景，与 mailbox worker 无关

### Dispatch Gate（避免打断）

任务体**永远**经 `aimeshchat mailbox send --host <H>` 进 inbox；inbox 是 append-only 队列，worker 两阶段消费（read→finalize），新任务只是排队，**天然不打断**正在执行的任务。

- 仅当 `status IDLE/DONE/BLOCKED` 且无未处理 REPORT 时派发新 TASK；`BUSY` 一律不发
- send-keys 仅允许对**本地** worker 发 `MAILBOX_PENDING` 唤醒（可选加速，不证明送达）；远程 worker 禁止

### 任务队列

**不需要自建队列**。mailbox inbox 就是持久化队列：消息不可变、append-only、两阶段消费保证至少一次处理。

- 单 worker 串行，多 TASK 自然排队；Manager 用 `request_id`/`run_id` 跟踪生命周期，终态 CAS 防重
- 需要并发 → 拆多个 worker（不同 agent_id），不要给单 worker 堆队列
- 崩溃恢复：worker 端 `mailbox recover-stale` 回收 >300s 的 processing 租约后继续

## Session 规则

默认 auto-resume：同一 host+workdir+backend+agent 继续同一上下文。

用 `--new-session` 的场景：
- 问题目标已完全变化
- 旧上下文被错误假设污染
- 涉及敏感隔离
- 需要独立对照实验

仅关闭自动续接而不换上下文 → `--no-auto-resume`。

显式 `--session-key` 推荐格式：`<project>:<role>:<domain-or-topic>`。不要只写 `oracle`。

## 输出过滤禁令

**调用 aimeshchat 命令时，禁止使用提前退出的管道（`| head`/`| tail`/`| grep`）过滤输出。**

原因：
- `run --background` 的 job ID 打印到 **stderr**，stdout 为空，`| tail` 抓不到 job ID
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
aimeshchat run '任务' <workdir> --background | tail -5
aimeshchat oracle status "$KEY" | grep "runtime_id"
```

## 从 code_route.py 迁移

| 旧命令 | 新命令 |
|--------|--------|
| `python3 code_route.py list` | `aimeshchat route list` |
| `python3 code_route.py where T` | `aimeshchat route where T` |
| `echo TASK \| python3 code_route.py route T --backend B` | `printf '%s\n' TASK \| aimeshchat route T --backend B` |
