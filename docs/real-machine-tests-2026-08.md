# 真机三机测试报告（2026-08-03）

跨主机 swarm/mailbox 系统的真机验证记录。主机：mac（本机，OA-MIANYIN-MAC）/ yellow（黄区，单向可达）/ win-wsl（OA-MIANYIN-2，主通道 + tun fallback）。

## 网络拓扑

- mac → yellow ✓、mac → win-wsl ✓（主通道曾瞬时超时，tun fallback 可用）
- yellow → mac ✗、win-wsl → mac ✗：黄区/内网无反向 SSH 通路（拓扑限制，非产品缺陷）
- 跨主机投递为直接 host→host，无中继；多跳转发需中间节点主动转发（见测试 5）

## 测试结果（10 项复杂场景，全部 PASS）

| # | 场景 | 结果 | 验证点 |
|---|------|------|--------|
| 1 | channel 创建/发送 | ✅ | 成员定向投递、sender 排除、非成员 ACL 拒绝 |
| 2 | attachments 跨主机 | ✅ | 校验链（sha256 64-hex/size/source_host）；非法 → accepted 回执；合法 → delivered |
| 3 | ack/consume 生命周期 | ✅ | inbox → read(processing) → finalize(archive) |
| 4 | 并发多发送方 | ✅ | 3 并发 register + 4 并发 direct 无丢（routing 锁 + merge） |
| 5 | 多层绕路 | ✅ | mac → win-wsl → yellow 两跳链式转发，消息完整 |
| 6 | tmux 嵌套 | ✅ | tmux 会话内 swarm CLI 正常发送 |
| 7 | 大消息边界 | ✅ | 99,999B delivered（长度精确）；100,001B 拒绝 + accepted 回执 |
| 8 | ACL 权限 | ✅ | channel 非成员拒绝；broadcast open policy 全员允许（设计如此）；restricted 分支仅单测覆盖（CLI 未暴露 policy） |
| 9 | 重启恢复 | ✅ | ControlMaster kill 自动重建自愈；pending 稳定保留；flush 不误报 |
| 10 | FIFO 顺序 | ✅ | 严格发送序消费 |

前序测试（v0.2.0）：mac→yellow burst 10 同秒全送达 + body 完整 + opaque cursor；watch 断线重连续读全部消息。

## 测试发现并修复的 3 个 bug

### Bug A：本机 repo-map host 误路由远程（`df3f20f`）
`EngineDeliverySink` 只判 `host_alias != "__local__"`，register `host=mac`（本机，ssh_alias=OA-MIANYIN-MAC）被 SSH 到字面 `mac` 失败 → broadcast 混合路由 mw1 报 transport failed。
**修复**：解析 repo-map（真实 ssh_alias/shell_prefix/fallback）+ `resolve_is_local` 判定本机不 cache 远程 HostSpec；`deliver()` 对 HostSpec 同样判定。

### Bug B：flush 对本机 target 走 transport（`df3f20f`）
`_target_host=mac` 无条件 `_remote_send` → SSH 本机别名 "No route to host" + 每条 10s ConnectTimeout 超时。
**修复**：flush 对 `resolve_is_local` 的 target 直写本地 inbox（msg_id 幂等）；`_resolve_target` 补 repo-map 解析（原重建 ad-hoc HostSpec 丢 shell_prefix）。

### Bug C：_ensure_remote_session roster 依赖进程内缓存（`3c398b1`）
`cache_roster` 无任何调用方，`_session_rosters` 从未填充 → CLI 每次新进程第一个发往某 host 的消息用 envelope from/to 的 degraded fallback 建远程 session → roster 残缺（漏成员）+ manager 错置（首个消息 sender 冒充 manager）。后果：win-wsl 转发到 yellow 时 "sender not in roster: ww1" 误拒绝。
**修复**：roster 缺失时从本地 store 读 create-session 持久化的权威定义（完整 agents + 真 manager），仅 store 也没有时才用 from/to 兜底。

## 遗留（未完成项）

- yellow/win-wsl 同步 v0.2.1（网络受限超时；作为接收方 0.2.0 可用，发送方修复在 mac 侧）
- 真实 OMP 唤醒端到端（需真实会话验证 launcher env → inbox → triggerTurn）
- relay（clouddev）QR 真机（用户暂时忽略）
- ACL restricted policy 的 CLI 入口（`create-session --policy`）
- receiver callback 失败不消费的真机验证（依赖 OMP hook 环境）
