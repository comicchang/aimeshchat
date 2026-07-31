# codeagent-py 最终综合检视上下文

## 一、项目起源

### 背景
用户有 3 天 SSHFS/FUSE 调试失败经历（macOS 27 beta 完全不支持）。
约束：不用 rsync（SSD 不够）、不用 Samba（WSL 冲突）、不用 ControlPersist 1h。
最终方案：远程执行（SSH + wire protocol），不复制源码。

### 原始诉求
1. 跨主机代码 Agent 编排（SSH/session/routing）
2. 每台机器只运行 dotai setup，不手动 pip install
3. 跨主机 Agent 双向通信（mailbox IPC）
4. SSH artifact 传输（选择性文件拉取）

## 二、架构演进

### Phase 1: Route Parity（从 code_route.py 迁移）
- topic routing（repo-map.json → host → path）
- relay PTY 支持
- SSH fallback

### Phase 2: tmux-agent-skills 合并
原因：mailbox 协议与 SSH transport 深度耦合，单一事实来源。

### Phase 3: 跨主机 mailbox
- wire protocol 扩展（CMD_MAILBOX, MSG_MAILBOX_RESULT）
- SSH stdin JSONL（非 sh -c + base64）
- mailbox_root 通过 wire body 传输

### Phase 4: SSH artifact 传输
- Worker 发 artifact descriptor
- Manager 通过 SSH ControlMaster pull（SFTP/SCP）
- sha256 + size 校验

## 三、Oracle 评审历史（16 轮）

| Round | 评分 | 关键发现 |
|-------|------|----------|
| 1 | 3/10 | P0: deploy order, remote_exec, mailbox authority |
| 2 | 8/10 | P2: 7 项（expand_path, cmd_stats, adapters 等） |
| 3 | 7.5/10 | P1 回归: relay catch ValueError |
| 4 | 4.5/10 | wire 未注册, ControlMaster API, 路径安全 |
| 5 | 6.7/10 | --mailbox-root 失效, SSH 假成功 |
| 6 | 5.9/10 | relay import 缺失（旧快照） |
| 7 | 6.8/10 | RepoMap midocs_root, relay 版本覆盖 |
| 8 | 7.4/10 | read_status 部分校验, clear 删 _corrupt |
| 9 | 8.5/10 | send roster, 并发 claim, wire 版本统一 |
| 10 | 87/100 | constants, PTY 健壮性, 集成测试 |
| 11 | 89/100 | 8 SSH 集成错误路径测试 |
| 12 | 91/100 | cli 99%, control_master 100%, store 100% |
| 13 | 92/100 | relay 100%, dead code removed |
| 14 | 96/100 | relay 100%, 603 tests, 95.84% coverage |
| 15 | 95/100 | wire factories tested, empty dirs removed |
| 16 | **98/100** | 634 tests, 97.84% coverage |

## 四、当前状态

### codeagent-py (efe038d)
- 634 tests passed, 97.84% coverage (gate 85%)
- 所有关键模块 100%：ssh/relay/store/cli/control_master/wire/adapters
- 4 个 skill：codeagent, codeagent-oracle-consult, tmux-agent-manager, tmux-agent-worker

### tmux-agent-skills (bbe53bf)
- 21 tests passed
- tools/ 现在是 shim（委托到 codeagent.mailbox）
- 原始代码保留在 mailbox.original

### dotai (1c3948e)
- components.json: 4 个 skill 的 remote_source 指向 codeagent-py
- orchestrator.py: uv tool install 在 sync_remote_skill_repos 之后

## 五、tmux-agent-skills 是否已被包含？

**是的。** codeagent-py 已完全包含 tmux-agent-skills 的功能：

| tmux-agent-skills 内容 | codeagent-py 对应 |
|------------------------|-------------------|
| tools/mailbox (634行) | src/codeagent/mailbox/store.py + cli.py |
| tools/mailbox-hook | src/codeagent/mailbox/hook.py |
| tools/mailbox-health | src/codeagent/mailbox/health.py |
| manager/SKILL.md | skills/tmux-agent-manager/SKILL.md |
| manager/OPERATIONS.md | skills/tmux-agent-manager/OPERATIONS.md |
| manager/CHEATSHEET.md | skills/tmux-agent-manager/CHEATSHEET.md |
| worker/SKILL.md | skills/tmux-agent-worker/SKILL.md |
| tests/test_mailbox.py (21 tests) | tests/test_mailbox_module.py (25 tests) |

tmux-agent-skills 的 tools/ 现在是 shim，委托到 codeagent.mailbox。

## 六、请检视

1. tmux-agent-skills 是否应该 archive？
2. 当前架构是否完整？
3. 文档是否准确？
4. 部署是否闭环？
5. 还有什么遗漏？

## 七、设计决策记录

1. **合并 vs 依赖**：合并仓库，因为 mailbox 与 SSH transport 深度耦合
2. **3 模块 vs 8 模块**：protocol.py + store.py + cli.py，不过度拆分
3. **wire protocol 扩展**：CMD_MAILBOX，通过 stdin JSONL 传输
4. **shell 注入防护**：远端走 wire protocol，不走 shell 字符串拼接
5. **向后兼容**：standalone `mailbox` CLI 100% 兼容原版
6. **许可证**：MIT（EnPL-1.0 作为内部彩蛋保留）
7. **致谢**：ACKNOWLEDGEMENTS 致谢 stellarlinkco/myclaude + tmux-agent-skills

## 八、行业最佳实践参考

- **单一职责**：每个 skill 只处理一个特定职责
- **渐进式披露**：核心指令 <500 行
- **编码部落知识**：捕获项目特定约定
- **A2A 协议**：agent-to-agent 通信标准（Linux Foundation）
- **MCP**：agent-to-tool 协议（与此处 agent-to-agent 正交）
- **Mailbox IPC**：解耦、同步机制、灵活性
