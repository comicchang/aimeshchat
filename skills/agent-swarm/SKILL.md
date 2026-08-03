---
name: agent-swarm
description: Unified agent orchestration over the codeagent swarm/mailbox protocol — manager dispatch or worker execution, determined by role. Progressive disclosure: loads only the relevant role file.
---

# agent-swarm — Unified Orchestration Protocol

> Role-specific rules: `skill://agent-swarm/roles/manager.md` or `skill://agent-swarm/roles/worker.md` | Protocol reference: `skill://agent-swarm/protocol/mailbox.md`

## Role Determination

Read **one** role file based on your identity:

| `$OMP_WORKER_ID` | Role | Load |
|---|---|---|
| `manager` | Manager | `skill://agent-swarm/roles/manager.md` |
| anything else | Worker | `skill://agent-swarm/roles/worker.md` |

If `$OMP_WORKER_ID` is unset, you are a **Worker**. Do not load the other role file — progressive disclosure keeps protocol noise minimal.

## Shared Protocol Reference

The canonical mailbox protocol (message schema, status.json contract, two-phase consumption, CLI commands, error handling) lives in `skill://agent-swarm/protocol/mailbox.md`. Both roles reference it; neither duplicates its content.

## Deployment Modes

本协议支持两种**互斥**的部署模式。所有 mailbox 命令、路径引用和通信假设必须与当前部署模式一致；混合模式会导致路径不一致、消息丢失或同步冲突。

**Default mode: B (Remote Transport)** — 本项目的标准部署方式。

### Decision Tree

```
.mailbox/ 目录是否通过 Syncthing（或其他共享 FS）在所有主机间同步？
  │
  ├─ 是 → Mode A: Shared FS
  │       - mailbox ops 是本地文件系统操作
  │       - transport 层不参与 mailbox 通信
  │       - 操作指南: skill://agent-swarm/operations/local.md
  │
  └─ 否 → Mode B: Remote Transport (DEFAULT)
          - 无共享文件系统
          - mailbox 通过 SSH wire protocol 跨主机
          - codeagent mailbox ... --host <alias>
          - codeagent swarm ... 提供高级 IPC
          - 操作指南: skill://agent-swarm/operations/remote.md
```

| Condition | Mode | MAILBOX_ROOT | Reference |
|---|---|---|---|
| 所有 agent 共享文件系统（Syncthing） | Mode A: Shared FS | 显式 `MAILBOX_ROOT=.mailbox` | `skill://agent-swarm/operations/local.md` |
| 任何 agent 在无共享 FS 的远程主机 | Mode B: Remote Transport | 默认 `resolve_root()` | `skill://agent-swarm/operations/remote.md` |

Determine mode from the session roster and workers.toml. If all agents share the mailbox root via Syncthing, use Mode A. If any agent is on a host without filesystem access to the mailbox root, use Mode B. Mixed sessions use Mode B for the remote agents and Mode A for the rest.

### MAILBOX_ROOT Consistency

**这是最常见的模式混合错误。** 以下规则适用于两种模式：

1. **Mode A**: 所有参与者必须显式设置 `MAILBOX_ROOT=.mailbox`（env 或 `--mailbox-root`）
2. **Mode B**: 本地操作使用默认 `resolve_root()`（`~/.local/share/codeagent/mailbox`）；远程操作通过 `--mailbox-root` 参数传递到远端
3. **永远不要**在 Mode A 中省略 `MAILBOX_ROOT`——CLI 默认值会指向不同路径
4. **永远不要**在 Mode B 中假设所有主机共享同一 `MAILBOX_ROOT` 路径

```bash
# Mode A: 必须显式设置
MAILBOX_ROOT=.mailbox mailbox send --session s1 --from manager --to w1 --kind TASK ...

# Mode B: 本地操作（默认 MAILBOX_ROOT）
mailbox send --session s1 --from manager --to w1 --kind TASK ...

# Mode B: 跨主机操作
codeagent mailbox send --session s1 --from manager --to w1 --kind TASK ... --host dev-server
```

### Mode-Mixing Audit Checklist

在编辑任何 mailbox 相关命令前，检查：
- [ ] 命令是否设置了 `MAILBOX_ROOT` 或 `--mailbox-root`？（Mode A 必须）
- [ ] 路径引用 `.mailbox/` 是否与当前模式一致？
- [ ] `Syncthing` 相关假设是否仅出现在 Mode A 上下文中？
- [ ] `send-keys` 是否仅用于本地 Worker？（Mode B 远程 Worker 不可用）
- [ ] CLI 命令是否使用正确的 resolution order？

## Shared Invariants

These rules apply to **every** agent regardless of role:

1. **Evidence honesty** — insufficient evidence → `[EVIDENCE PENDING]` or `[INFERENCE: reason]`. Never fabricate.
2. **msg_id correlation** — every reply references the original `msg_id` via `--reply-to`. Never reuse or overwrite a sent message.
3. **Never fabricate** — do not invent protocol fields, status values, inbox paths, or worker IDs. Verify from INIT, session.json, or workers.toml.
4. **CLI-only writes** — all mailbox and status.json mutations go through the standalone `mailbox` CLI. Never hand-write JSON.
5. **No capture-pane** — do not use terminal text to infer agent state. Use status.json and inbox polling.
6. **Two-phase consumption** — `mailbox read` (inbox→processing) → process → `mailbox finalize` (processing→archive). No shortcuts.
7. **status.json is a snapshot** — five fields only. Full conclusions belong in REPORT messages and artifacts, not in status.

## Initialization Flow

1. **Determine role** from `$OMP_WORKER_ID` (see table above).
2. **Load your role file** — `roles/manager.md` or `roles/worker.md`.
3. **Read the protocol** — `protocol/mailbox.md` for the canonical CLI schema and state machine.
4. **Load deployment mode** — `operations/local.md` or `operations/remote.md` based on session topology.
5. **Follow role-specific initialization** — Manager self-init or Worker INIT handshake.

## CLI Resolution Order

Standalone `mailbox` commands are the authoritative interface:

1. PATH command `mailbox` (from `codeagent` package via `uv tool install`)
2. `codeagent mailbox` as unified cross-host entry point
3. For swarm sessions: `codeagent swarm ...` subcommands

Never route through `scripts/tmux_worker.py` or other legacy wrappers.

## Legacy (v1)

v1 concepts deprecated by this protocol:

- **control envelope** (A-plane) → replaced by `mailbox read`
- **B-plane** (event-emit) → replaced by `status.json`
- **mailbox/outbox → relay → mailbox/inbox** → replaced by direct inbox
- **cursor / unread / mark-read** → replaced by `mailbox read` / `mailbox finalize` two-phase

Legacy commands (`request`, `request-role`, `batch-request`, `event-emit`, `event-wait`, `mailbox-send`, `mailbox-check`, `mailbox-relay`, `manager-poll`) are for unmigrated Workers only. New v2 work must not use them.

---

- Manager rules: `skill://agent-swarm/roles/manager.md`
- Worker rules: `skill://agent-swarm/roles/worker.md`
- Protocol reference: `skill://agent-swarm/protocol/mailbox.md`
- Deployment modes: `skill://agent-swarm/operations/local.md` / `skill://agent-swarm/operations/remote.md`
