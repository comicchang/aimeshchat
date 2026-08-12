#!/usr/bin/env bash
# swarm-watch.sh — open a tmux pane polling a swarm session inbox.
#
# Usage:
#   ./scripts/tmux/swarm-watch.sh <session_id> <agent_id> [interval] [pane_name]
#
# Opens a new tmux window (or pane) running `aimeshchat swarm watch` in a loop.
# Requires: aimeshchat on PATH, tmux.

set -euo pipefail

SESSION_ID="${1:?Usage: swarm-watch.sh <session_id> <agent_id> [interval] [pane_name]}"
AGENT_ID="${2:?Usage: swarm-watch.sh <session_id> <agent_id> [interval] [pane_name]}"
INTERVAL="${3:-5}"
PANE_NAME="${4:-swarm-watch-${AGENT_ID}}"

# Check prerequisites
if ! command -v aimeshchat >/dev/null 2>&1; then
    echo "error: aimeshchat not found on PATH" >&2
    exit 1
fi

if ! command -v tmux >/dev/null 2>&1; then
    echo "error: tmux not found" >&2
    exit 1
fi

# If already inside tmux, split the current window; otherwise create a new session
CMD="aimeshchat swarm watch ${SESSION_ID} --agent ${AGENT_ID} --interval ${INTERVAL}"

if [ -n "${TMUX:-}" ]; then
    # Already inside tmux — split the current pane
    tmux split-window -v -l 12 -n "${PANE_NAME}" "${CMD}"
else
    # Outside tmux — create a new session
    tmux new-session -d -s "${PANE_NAME}" -n watch "${CMD}"
    echo "tmux session '${PANE_NAME}' started. Attach with: tmux attach -t ${PANE_NAME}"
fi
