# codeagent-py 最终架构决策上下文

## 一、项目起源与约束

### 背景
- 用户有 3 天 SSHFS/FUSE 调试失败经历（macOS 27 beta 完全不支持）
- 不用 rsync（SSD 不够）、不用 Samba（WSL 冲突）、不用 ControlPersist 1h
- 每台机器只运行 dotai setup，不手动 pip install

### 环境
- macOS 27 beta (Apple Silicon M4 Pro)
- 远程主机：Linux dev servers、WSL、Cloudflare tunnel、bastion relay
- SSH 配置：ProxyJump、ControlMaster auto、ServerAliveInterval 15

## 二、尝试过/被否决的方案

### 方案 1: SSHFS/FUSE（3 天调试，全部失败）
- macFUSE kext I/O 破损
- FUSE-T dext 未注册
- go-nfsv4 静默失败
- **结论**：macOS 27 beta 不支持 FUSE

### 方案 2: rsync
- 代码仓远超 SSD 容量
- **结论**：空间不足

### 方案 3: Samba
- WSL 与 Windows Samba 冲突
- **结论**：不可用

### 方案 4: ControlPersist 1h
- 破坏端口转发
- **结论**：用户明确拒绝

### 方案 5: dispatch-only（subprocess 调远端 mailbox CLI）
- shell 注入风险（body 中的 $、反引号）
- **结论**：oracle-lite 否决

### 方案 6: StreamLocal Unix socket 转发
- ProxyJump 时 socket 路径在跳板机解析，不到目标机
- Cloudflare tunnel 不支持
- **结论**：不可行

### 方案 7: mailbox 网关 HTTP 服务
- 引入额外依赖，部署复杂
- **结论**：过度工程化

## 三、当前实现

### 架构
- 文件系统 mailbox（inbox/processing/archive）
- SSH wire protocol（stdin JSONL，非 shell）
- SSH ControlMaster per host
- SQLite session registry

### 状态
- 667 tests, 97.58% coverage
- 所有关键模块 100% 覆盖
- IRC 广播 + 附件引用 + 规范历史已实现
- 部署闭环已修复（fail-closed + 5 entrypoint 验证）

### 痛点
1. **实时性**：依赖 Syncthing 同步，延迟秒到分钟级
2. **无共享文件系统时无法工作**：必须有 Syncthing 或 NFS
3. **跨主机通信需要中心化**：当前 Manager 必须知道所有 Worker 的 host

## 四、用户需求

### 核心需求
1. 跨主机 Agent 双向通信（Manager↔Worker、Worker↔Worker）
2. 广播（一对多）
3. 私聊（一对一）
4. 频道/房间（session 即频道）
5. 文档/文件传输

### 优先需求（MVP）
1. Manager↔Worker 双向通信（必须）
2. 跨主机通过 SSH（必须）
3. 不依赖共享文件系统（必须）
4. 实时性（毫秒级，非秒级）

### 可选需求
1. Worker↔Worker peer 通信
2. 广播
3. 多实现形式协同（CLI + OMP 插件 + Tmux 插件）

## 五、SSH Socket 转发方案（Oracle-lite 建议）

### TCP Loopback 转发（推荐）
```bash
# Worker→Manager
ssh -L 127.0.0.1:5555:127.0.0.1:5555 worker-a

# Manager→Worker（反向）
ssh -R 127.0.0.1:15555:127.0.0.1:5555 worker-a

# 通过 ProxyJump（透明）
ssh -J jumphost worker-a -L 127.0.0.1:5555:127.0.0.1:5555 -R 127.0.0.1:15555:127.0.0.1:5555
```

### 架构：Hub-and-Spoke
```
Manager (hub)
├── mailbox-daemon :5555
├── routing table: worker-a→:15555, worker-b→:25555
│
├── SSH → Worker A (-L 5555 -R 15555)
├── SSH → Worker B (-L 5555 -R 25555)
└── SSH → Worker C (-L 5555 -R 35555)
```

### MVP：混合传输
- 文件系统：持久化 + 崩溃恢复 + 无网络时降级
- TCP daemon：实时转发（毫秒级）
- CLI --transport：可选，向后兼容
- ~350 行新代码，~2 天

## 六、请 Oracle 决断

1. **是否实施 TCP loopback 转发？**
2. **与当前文件系统 mailbox 的关系？**（替换 vs 共存 vs 混合）
3. **MVP 范围是否合理？**
4. **有没有被遗漏的风险？**
5. **最终架构建议？**