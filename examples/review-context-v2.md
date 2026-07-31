# codeagent-py 综合检视上下文（含 Skill 设计最佳实践）

## 项目历史

### 起源
- 3 天 SSHFS/FUSE 调试失败（macOS 27 beta）
- 最终方案：远程执行（SSH + wire protocol）

### 演进
1. Phase 1: Route Parity（从 code_route.py 迁移）
2. Phase 2: tmux-agent-skills 合并到 codeagent-py
3. Phase 3: 跨主机 mailbox（wire protocol 扩展）

### Oracle 评审历史
- Round 1: 3/10 → 修复 P0（deploy、remote_exec、mailbox authority）
- Round 2: 8/10 → 修复 P2（7 项）
- Round 3: 7.5/10 → 修复 P1 回归 + 2 P2
- Round 4: 4.5/10 → 修复 wire protocol 集成、安全、root 对齐
- 当前：9882438，388 tests + 21 acceptance gate

## 当前架构

### 4 个 Skill
1. **codeagent**: 跨主机代码 Agent 编排（SSH/session/routing）
2. **codeagent-oracle-consult**: 通过 codeagent 持久 session 咨询 Oracle
3. **tmux-agent-manager**: 多 Worker 编排协议（dispatch/poll/wait/ack）
4. **tmux-agent-worker**: Worker agent 协议（INIT/TASK/status）

### mailbox 模块
- protocol.py: Message 类型 + 校验
- store.py: MailboxStore 文件系统 I/O
- cli.py: standalone CLI（100% 兼容原版）
- hook.py: peek-only notification
- health.py: 只读诊断

### wire protocol 扩展
- CMD_MAILBOX 注册到 _REQUEST_REQUIRED
- MSG_MAILBOX_RESULT 类型
- mailbox_root 通过 wire body 传输
- stdin JSONL（非 sh -c + base64）

## 行业最佳实践（来自搜索）

### AI Agent Skill 设计原则

1. **单一职责（Single Purpose Scoping）**：每个 skill 只处理一个特定职责
2. **清晰输入输出**：定义结构化的输入输出
3. **渐进式披露（Progressive Disclosure）**：核心指令 <500 行，详细参考材料按需加载
4. **编码部落知识（Codify Tribal Knowledge）**：捕获项目特定约定和内部流程
5. **独立测试**：每个 skill 独立测试后再集成
6. **版本管理**：版本化 skill 以管理更新
7. **明确边界**：区分 agent 判断、工具执行、任务工作单元

### 多 Agent 编排协议原则

1. **编排 vs 编舞**：
   - 编排（集中式）：中央协调器控制流程
   - 编舞（去中心化）：agent 直接交互
2. **核心编排原语**：任务分解、agent 路由、状态管理、恢复
3. **指令设计**：
   - 单一响应原则
   - 声明 subagent 角色
   - 清晰直接的语言
   - 每个 subagent 一个知识源
4. **通信协议**：A2A（Agent2Agent）是 Linux Foundation 下的开放标准

### Mailbox IPC 模式

1. **解耦**：发送者不需要知道具体接收者
2. **同步机制**：内置信号量管理并发访问
3. **灵活性**：多个进程通过多个 mailbox 通信
4. **阻塞/非阻塞**：支持等待消息或稍后检索

## 用户要求

1. Skill 是否合理？职责划分是否清晰？
2. 文档是否完整？有没有遗漏或过时的引用？
3. 与行业最佳实践的对比
4. 跨主机 mailbox 的架构设计是否正确？
5. **SSH artifact 传输**：通过已有 SSH ControlMaster 选择性拉取文件

## SSH artifact 传输方案

用户希望通过 SSH 实现文件传输（类似 rsync）。Oracle 建议：
- Worker 发 artifact descriptor（artifact_id, path, size, sha256, media_type）
- Manager 通过已有 SSH ControlMaster pull（SFTP/SCP）
- 不走 mailbox JSONL（1 MiB 限制）
- 不走 relay PTY（用独立 forward）
- 控制面和数据面分离

## 事实更正

- LICENSE: MIT（EnPL-1.0 作为内部彩蛋保留，已从 EPL-1.0 改名）
- coordination/mailbox_bridge.py: 已在 Round 7 删除

## 请检视

1. **Skill 设计**：4 个 skill 的职责是否符合"单一职责"原则？触发条件是否清晰？
2. **文档完整性**：SKILL.md 是否准确？CLI 路径引用是否更新？
3. **mailbox 架构**：是否符合 IPC mailbox 模式？解耦是否充分？
4. **wire protocol**：扩展是否安全？有没有注入风险？
5. **部署闭环**：dotai setup 是否完整？
6. **与行业对比**：与 A2A 协议、MCP 的关系