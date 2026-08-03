# A2A Adapter 接入可行性评估（2026-08）

评估对象：把 A2A（Agent2Agent，Google → Linux Foundation）接入 codeagent-py v0.2.1 swarm 系统的可行性。基线事实见 [external-comparison.md](./external-comparison.md)（A2A 行）；A2A 规范细节以 2026-03 稳定版 v1.0 为准（[linuxfoundation.org](https://www.linuxfoundation.org)、[a2a-protocol.org](https://a2a-protocol.org)，经 web 核实）。

**结论摘要**：单向 client 形态（我们调外部 A2A agent）**可行且值得做**，工作量小、不破坏零 daemon 卖点；双向 server 形态**现阶段不建议**——需常驻 HTTP 端点 + 可达网络，破坏零 daemon/零依赖卖点，且真实拓扑无入站通路（见 §5）。

## 1. A2A 简介（v1.0）

- **协议形态**：JSON-RPC 2.0 over HTTP(S)，状态推送走 SSE（Server-Sent Events）或 Webhook。
- **AgentCard 发现**：每个 agent 暴露机器可读 AgentCard（name/description/url/skills/securitySchemes/preferredTransport）；client 先 GET AgentCard 了解能力与端点再调用。v1.0 起支持加密签名 AgentCard。
- **Task 生命周期**：远端执行可跟踪工作 → 返回 stateful Task。状态机：`submitted → working ⇄ input-required → completed / failed / canceled / rejected`。client 可轮询 `tasks/get` 或订阅 SSE 状态流。
- **Message / Artifact**：Message = role（user/agent）+ parts（TextPart / FilePart / DataPart）；Artifact = 任务产出 part 集合（带 artifact 索引与描述）。流式增量用 SSE `message` 事件。
- **与我们的关系**：A2A 无会话/roster/ACL/幂等键/跨主机 mailbox 概念；状态由远端 agent 持有，HTTP 层无状态。

## 2. 概念映射

| 我们的概念 | A2A 对应 | 映射难度 | 说明 |
|-----------|---------|---------|------|
| session | 无（client 侧维护 `session_id ↔ task_id` 映射，或塞进 DataPart） | 中 | A2A 无会话组；多消息关联靠任务链/自定字段 |
| roster | AgentCard skills / 无 | 高 | 外部 agent 不感知我们的 roster；ACL 需 adapter 层补 |
| kind（TASK/REPORT/PROGRESS/…） | Task.state + Message role + DataPart 自定 `kind` 字段 | 中 | TASK→新 task；PROGRESS→working+SSE；REPORT/RESPONSE→completed+artifacts；NOTICE→无对应（需 DataPart） |
| delivery receipt（accepted/delivered/consumed） | JSON-RPC 响应 / task 状态：accepted≈submitted；delivered≈message/send 返回 message；consumed≈completed+artifacts | 中 | "delivered" 无 A2A 原生语义，需远端回显确认 |
| msg_id 幂等 | **无内置幂等键** | 高 | 需把 msg_id 作 messageId/DataPart 携带，远端去重是对方责任；我们 outbox+msg_id 重试仍安全（at-least-once，对方不去重则可能重复） |
| opaque cursor | 无 | — | SSE 续传用 task_id + `tasks/resubscribe`；adapter 本地保存 task_id↔cursor 映射 |
| attachments（sha256 校验链） | FilePart（URI） | 高 | 外部 agent 读不到我们的本地路径，需 artifact HTTP 端点或 base64（撞 100KiB 上限） |

## 3. 接入形态 ① 单向：我们作为 A2A client

外部 A2A agent 视为 swarm 里的一个远程 agent：路由表中 `agent_id → a2a://host/...`，kernel 的 `deliver()` 经新 DeliverySink 走 HTTP 出站。**全程无入站端口，零 daemon 卖点不破坏。**

| 项 | 内容 | 工作量 |
|----|------|--------|
| 新模块 | `src/codeagent/a2a/client.py`：AgentCard 拉取+校验、JSON-RPC 封装、Task 轮询/SSE 订阅、envelope→Message(Parts) 映射、msg_id 携带与回执映射 | ~300–450 行 |
| 新 adapter | `A2ASink` 实现现有 `DeliverySink` 协议（[kernel.py:49-52](../src/codeagent/swarm/kernel.py)），复用 durable outbox + `flush()` 重试管线 | ~100–150 行 |
| 改动点 | 路由/CLI：位置类型支持 `a2a://`（`AgentLocation` 扩展）；配置：AgentCard URL 注册表（repo-map 或 config 文件）；`SendReceipt` 桥接（accepted=已入 outbox / delivered=远端 task submitted 确认 / queued=重试） | 3–4 处小改 |
| 测试 | mock A2A server（stdlib http.server 假实现或 httpx MockTransport）：映射单测、回执映射、flush 重试幂等、SSE 解析、超时/错误码 | ~10–15 用例 |
| 估算 | 2–4 人日（不含对方 agent 联调） | — |

## 4. 接入形态 ② 双向：外部 agent 经 A2A server 发现我们

需要常驻（或按需拉起）HTTP server 暴露 AgentCard + JSON-RPC 方法（`agent/getCard`、`message/send`、`tasks/get|send|cancel|pushNotificationConfig`）+ SSE/Webhook。

| 项 | 内容 | 工作量 |
|----|------|--------|
| 新模块 | `src/codeagent/a2a/server.py`：AgentCard 生成（从 session.json/capabilities）、JSON-RPC 方法实现、SSE/Webhook 推送、鉴权 | ~500–800 行 |
| 新 CLI | `codeagent a2a serve`（on-demand 拉起/拆除）、`codeagent a2a discover` | ~150 行 |
| 改动点 | 入站消息 → `store.send` 幂等写入（msg_id 从 DataPart 提取，复用现有去重）；外部身份（securitySchemes/auth header）→ session 成员映射（**ACL 语义难等价**：channel/broadcast/restricted policy 无 A2A 对应）；artifacts 需 FilePart URI 端点（否则附件不可达）；sender 侧 consumed 回执需 Webhook 回推 | 4–5 处 |
| 测试 | 本机起 server + 假外部 client 调 message/send：幂等、鉴权、ACL、并发、SSE 断线重订阅 | ~15–20 用例 |
| 估算 | 5–10 人日 + 网络/部署成本 | — |

## 5. 阻塞点与建议

**阻塞点（按严重度）：**

1. **网络拓扑（硬阻塞）**：真机环境 yellow/win-wsl → mac 无反向 SSH 通路（[real-machine-tests-2026-08.md](./real-machine-tests-2026-08.md) 拓扑节）。A2A server 放 mac 对外不可达；需部署在可达节点或走 relay/tunnel 入站——当前无此设施。
2. **零 daemon 卖点**：双向 = 常驻 HTTP 端点。缓解：`a2a serve` 按需拉起 + **出站 Webhook 推送**（Webhook 是出站连接，入站零常驻）；仍需要 public URL 或隧道。
3. **语义缺口集中在 adapter 层**：A2A 无 session/roster/ACL/幂等键/cursor，双向时全部要自补；ACL（channel/restricted policy）无法等价表达，只能按身份白名单近似。
4. **attachments**：sha256 校验链依赖本地文件路径（[store.py attachment_error](../src/codeagent/mailbox/store.py)），A2A FilePart 是 URI；附件互通需额外 artifact 端点，或暂只通文本/DataPart。
5. **规范与生态**：v1.0 于 2026-03 才稳定（此前 0.2.x 变动大），第三方实现成熟度参差；跨厂商去重/回执行为无统一保证。

**建议（何时值得做）：**

- **现在就值得**：形态①（单向 client）。复用现有 outbox/flush/msg_id 管线，改动封闭在 sink 层，是接入外部 A2A agent 的最低成本路径；顺带把 external-comparison.md 的 P0/P1（trace_id envelope、AgentCard 结构化 capabilities）吸收进自有 envelope/session.json。
- **暂缓**：形态②（双向 server）。等到出现明确入站需求（外部 agent 主动调我们）+ 可达网络设施（relay 入站或公网端点）再启动；启动时优先 on-demand serve + Webhook 出站推送，避免常驻进程。
- **不建议**：为"看起来更标准"引入 A2A 替代自有 SSH wire + file mailbox——两者面向不同场景：A2A 解决异构 agent 互操作，我们解决自有 fleet 的零依赖跨主机投递；自研 fleet 内部继续走 mailbox，A2A 仅作边界适配层。

## 附：引用索引

- A2A 基线：docs/external-comparison.md（A2A 行 + 结论）
- A2A v1.0 事实：linuxfoundation.org / a2a-protocol.org（2026-03 稳定版，web 核实）
- 我方接口：src/codeagent/swarm/kernel.py（DeliverySink 协议）、src/codeagent/swarm/delivery.py（outbox/flush/msg_id）、src/codeagent/mailbox/store.py（send 幂等/附件校验/大小上限）
- 网络拓扑：docs/real-machine-tests-2026-08.md（yellow/win-wsl → mac 无反向通路）
