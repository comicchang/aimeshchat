---
name: tmux-agent-manager
description: DEPRECATED — redirects to skill://tmux-agent (roles/manager.md). This stub exists for backward compatibility.
---

# ⚠️ DEPRECATED — Redirect

**This skill has been merged into `skill://tmux-agent`.**

The tmux-agent-manager and tmux-agent-worker skills were consolidated into a single unified skill with progressive disclosure:

- **New unified skill**: `skill://tmux-agent/`
- **Manager role**: `skill://tmux-agent/roles/manager.md`
- **Worker role**: `skill://tmux-agent/roles/worker.md`
- **Shared protocol**: `skill://tmux-agent/protocol/mailbox.md`

## Migration

Replace references to `skill://tmux-agent-manager` with `skill://tmux-agent/roles/manager.md`. The protocol content is identical; only the file structure changed.

## Why Deprecated

Two separate skills (manager + worker) duplicated the mailbox protocol and created cross-reference loops. A single skill with progressive disclosure loads only the relevant role file, eliminating duplication and simplifying maintenance.

---

*This stub is retained for backward compatibility. New work should reference `skill://tmux-agent/` directly.*
