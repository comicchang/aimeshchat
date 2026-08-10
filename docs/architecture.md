# codeagent 平台架构声明

> 本文件是平台状态持久化策略与错误处理的**权威声明**（oracle 第五轮评审 F1/F2 输出）。
> 与 `.drafts/cross-agent-platform-arch-review.md`（mi-docs 归档）互补。

## 一、状态持久化策略

| 状态 | 持久化层 | 恢复策略 |
|------|----------|----------|
| session manifest（roster/ACL/manager） | `session.json` | gateway 重启直接读取（kernel `_load_persisted_sessions`） |
| park 实例 | ParkRegistry SQLite（`park.sqlite3` WAL） | gateway 重启扫描 `list_active()` 重建 `_runtimes`（A3） |
| hub peer 映射 | `~/.local/share/postmesh/gateway/peers.json`（原子写） | gateway 重启从 peers.json 恢复（F2） |
| runtime liveness | **内存态**（进程级心跳） | 插件重注册（OMP session_start / opencode heartbeat）+ ParkRegistry 恢复 |
| EventStore 事件 | SQLite WAL（`events.sqlite3`） | 永久保留（cursor 补流）；tool update 明细 7 天 sweep |
| write.merge 记录 | **内存态** `_merges` | 重启丢失（可接受：merge 是一次性操作，冲突由 sha256 重新检测） |

**原则**：持久化"跨重启必须保留的权威状态"（manifest/park/peer/事件），内存态只放"进程级瞬态"（liveness）。hub peer 因映射远端设备（非本地进程）而落入持久化侧——这是 F2 修复的灰色地带。

## 二、错误处理策略

| 分类 | 行为 | 位置 |
|------|------|------|
| `fatal` | 显式抛错退出（terminal CAS / 校验失败） | `GatewayError(ERR_*)`，CLI exit 2 |
| `degraded` | 降级但可继续（capability 缺失 → 选替代 adapter） | `RuntimeRegistry.get` 抛 `UNSUPPORTED_RUNTIME/CAPABILITY`，调用方 catch 降级 |
| `silent` | 非关键路径可忽略（须 `log.warning`，禁止裸 `except: pass`） | 心跳失败、诊断上报失败 |

**红线**：capability 选择、身份校验、authority 合并等 fail-closed 路径禁止 `except Exception` 静默吞错——必须捕获具体异常并记录。已修复实例：`RuntimeRegistry.names()` 未绑定调用曾静默降级（TypeError 被吞）；`hub_register` 过宽 except（已改 log.warning）。

## 三、运行时职责（identity/握手）

- **owner**：supervisor 进程（长驻）——identity 0600 文件的 owner_pid 必须是 supervisor，非瞬时 launcher（插件 stale 检查 `process.kill(owner,0)` 拒死进程）
- **握手**：插件 `runtime.register`（OMP 在 activate + session_start 重注册；opencode 在加载 + 首次 session.status 补发 backend_session_id）
- **adoption**：无插件 runtime（opencode/generic）由 `oracle start/ask` 三路径 `_adopt_runtime` 注册
- **消息通道语义**：inbox 是持久化消息存储，**不是实时通道**。实时投递走 gateway `runtime.send`（steer）；initial_task 走 `runtime.register` 返回值 + `initial_task_msg_id` 消费（claim+finalize），避免陈旧 TASK 歧义

## 四、inbox 消息通道声明（#8 修复的架构含义）

- inbox = 持久化队列：先进先出，`mailbox read` 消费（claim → processing → finalize）
- 实时投递 ≠ inbox 扫描：插件唤醒走 `pi.sendMessage(steer/nextTurn)`；initial_task 由握手返回值显式传递
- warm resume 必须先 enqueue TASK（`oracle ask` warm 路径）再 spawn，插件拿到的是新 prompt 而非陈旧 TASK
