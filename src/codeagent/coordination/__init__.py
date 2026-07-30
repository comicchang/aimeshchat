"""Coordination subsystem — mailbox protocol for multi-agent orchestration.

This module provides the mailbox-based coordination protocol used by
tmux-agent-manager and tmux-agent-worker. It is intentionally kept
separate from the execution subsystem (transport/runner/session).

Key components:
- mailbox_cli: standalone CLI for inbox/processing/archive lifecycle
- models: message types, status, roster
- store: filesystem-based atomic inbox store

The mailbox root defaults to $MAILBOX_ROOT env var, falling back to
$XDG_STATE_HOME/codeagent/mailbox or ~/.local/state/codeagent/mailbox.
"""
from __future__ import annotations

__version__ = "0.1.0"
