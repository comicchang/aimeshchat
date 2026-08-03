# 外部多 Agent 协同方案对比（2026-08 调研）

调研目标：跨主机/跨目录多 agent 联合、通讯渠道、协同工作的既有方案，评估 codeagent-py swarm 的定位与可吸收点。

## 对照表

| 类别 | 代表 | 传输/部署 | 与 codeagent-py 的关系 |
|------|------|-----------|----------------------|
| **A2A 协议**（Google → Linux Foundation） | agent↔agent 标准：JSON-RPC 2.0 over HTTP + Agent Card 能力发现 + SSE/Webhook | 每 agent 常驻 HTTP server | 与 swarm 最接近的行业标准；我们走 SSH wire（零常驻） |
| **MCP**（Anthropic） | agent → 外部工具/资源标准 | 本地/远程工具桥 | 互补，不冲突；我们已有自有工具链 |
| **ACP**（IBM） | agent 通信协议 | 已并入 A2A | — |
| **编排框架** | LangGraph / CrewAI / AutoGen / MetaGPT / OpenAI Agents SDK | 进程内（同机同进程） | 不解决跨主机；我们是跨主机 |
| **消息总线** | Redis Streams / Kafka / NATS | 常驻 broker 服务 | 我们零依赖；durable outbox ≈ 总线留存；trace header 可吸收 |
| **AMQ**（Agent Message Queue） | file-based 消息 + crash-safe 原子投递 + 跨会话路由 | 单机文件 | 与 mailbox 几乎同构；AMQ 无跨主机 transport |
| **execution 层** | SSH / tmux / mosh | 远程环境持久化 | 已用 SSH ControlMaster + tmux |
| **tmux-agent-sidebar**（hiroppy） | tmux TUI 监控 agent 状态 | 本机 tmux | dotai 已集成（disabled）；只读监控无通信 |

## 结论

**"跨主机 + 零常驻服务 + 纯 SSH wire + file mailbox + 持久 session" 组合没有直接同类。**
文件持久化 + SSH 传输 + 幂等投递是正确的差异化路径。不建议为"看起来更标准"引入 HTTP 常驻服务或消息中间件——那会牺牲零 daemon/零依赖的核心卖点。

## 可吸收点（按优先级）

1. **trace_id envelope（P0，观测性）**：A2A Task.context_id / 总线 trace header / AMQ correlation_id 均为同类实践。我们的 envelope 有 session_id/msg_id/run_id，缺 trace_id —— 跨主机问题排查只能靠 msg_id grep。
2. **Agent Card 标准化（P1，互操作）**：A2A 的 AgentCard（name/description/skills/preferredTransport）映射到 session.json 的 agent metadata；我们已有 capabilities 雏形无结构。
3. **channel ack 可观测性（P2）**：A2A Task 状态机比 inbox/processing/archive 三态更细粒度；channel 消息加 expected_ack_count/ack_status 让 sender 可观测分发进度。
4. **AMQ 原子投递审计（P3，确认）**：AMQ 的 tmp→rename+fsync 与我们一致；".delivered" 标记与远程 inbox 写入之间的 crash 窗口由 msg_id 幂等兜底，设计已覆盖，审计为文档确认。

## 详细来源

- A2A：Google 2025-04 提出，Linux Foundation 托管；JSON-RPC 2.0 + Agent Cards + SSE/Webhook
- ACP：IBM 提出，2025 年中并入 A2A
- AMQ：file-based messaging、crash-safe atomic delivery、cross-session routing
- 消息总线：Redis Streams / Kafka / NATS；envelope 应带 task_id/trace_id/source_agent_id
