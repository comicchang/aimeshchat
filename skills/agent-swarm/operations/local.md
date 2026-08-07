# Local Deployment Mode (Shared-fs / Syncthing)

> Protocol reference: `skill://agent-swarm/protocol/mailbox.md`

## Overview

Local mode applies when all agents share the mailbox root filesystem via Syncthing. Every participant reads and writes `.mailbox/` directly — no remote transport layer.

## Syncthing Shared Root

Mailbox directory `.mailbox/` is under the shared repository root. Syncthing syncs the entire root across all participating machines. Each agent writes directly to the recipient's `inbox/` — no relay or indirection.

**Sync conflict handling**: `.sync-conflict-*` files are never valid messages. Compare with original, identify sender, request resend via CLI. Never rename conflict files.

## Local send-keys Wake

For Workers sharing a tmux socket with the Manager, `tmux send-keys` can deliver an optional wake signal:

```bash
tmux send-keys -t <target> -l -- "MAILBOX_PENDING; check v2 inbox"
tmux send-keys -t <target> C-m
```

This is a **convenience wake only** — it proves neither delivery nor reading. The mailbox file and subsequent status/REPORT are the only proof of progress.

## Worker Startup

Workers in local mode are launched by the Manager. Startup uses marker stages: `PANE_ALIVE → SHELL_READY → CWD_VERIFIED → AGENT_STARTED`. Worker writes `.mailbox/<session>/<agent>/status.json` to `IDLE` after initialization — this is the v2 dispatchable snapshot.

## Identity File

Each Worker process gets a unique identity path injected at launch:

```bash
TOKEN=$(date +%s)_$RANDOM
mkdir -p ~/.omp/mailbox-identity
OMP_MAILBOX_IDENTITY_FILE=~/.omp/mailbox-identity/${TOKEN}.json omp -c
```

## Diagnostics

- `~/.drafts/tmux-workers/<id>/ipc` — reserved for startup diagnostics.
- `mailbox stats` — shows all 4 dirs (inbox/processing/archive/_corrupt) for sync health.
- Growing `inbox` count without status change → Syncthing delay or Worker stall.

---

## Relationship to Active Design

本文件描述 **Mode A (Shared FS)** 部署模式的运维细节。
协议权威定义在 `skill://agent-swarm/protocol/mailbox.md`，
Skill 入口在 `skill://agent-swarm/SKILL.md`（active design version: **v2 session-based**）。
拓扑选择和 MAILBOX_ROOT 规则见 `SKILL.md` §Deployment Modes。
