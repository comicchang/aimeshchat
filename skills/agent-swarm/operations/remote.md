# Remote Deployment Mode (SSH / Relay / Swarm)

> Protocol reference: `skill://agent-swarm/protocol/mailbox.md`

## Overview

Remote mode applies when Workers are on hosts without direct filesystem access to the Manager's mailbox root. Communication uses the `codeagent` CLI's remote transport (`mailbox --host`, `codeagent swarm` subcommands) instead of shared-fs Syncthing.

## Remote Transport

Remote Workers use the `codeagent` CLI for all mailbox operations. CLI resolution order:

1. PATH command `mailbox` (from `codeagent` package via `uv tool install`)
2. `codeagent mailbox` as unified cross-host entry point
3. For swarm sessions: `codeagent swarm ...` subcommands

All mailbox commands (`send`, `read`, `finalize`, `peek`, `stats`, `status`, etc.) work across hosts via the transport layer.

## No send-keys

Remote SSH Workers have **no local tmux socket** with the Manager. Therefore:

- `tmux send-keys` is **never** used for Manager→Worker communication.
- The INIT check prompt is sent via available runner/interactive channel, not send-keys.
- All formal communication (INIT, TASK, REPORT, NOTICE) goes through the mailbox.
- The only notification path is shared inbox + Worker active polling.

## Notification Strategy

| Path | Reliability | Usage |
|---|---|---|
| Mailbox direct inbox | authoritative | all formal messages |
| Runner/adapter peek | advisory notification only | plugin notifies, never consumes |
| Active Worker polling | fallback when no adapter | `mailbox read` at boundaries |

Remote Workers must actively poll their inbox at task start, major phase boundaries, before final REPORT, and after terminal status — even without adapter notification.

## Worker Startup

Remote Workers are started by the Manager but run on their own host. After launch:

1. Worker sets `$OMP_SESSION_ID` and `$OMP_WORKER_ID`.
2. Reads `skill://agent-swarm` (current protocol).
3. Completes INIT handshake via mailbox.
4. Runs `mailbox-health` gate check.
5. Writes IDLE status.

## Mixed Sessions

If a session has both local and remote Workers:
- Local Workers: use shared-fs Syncthing path + optional send-keys wake.
- Remote Workers: use remote transport for all communication.
- Manager handles both modes simultaneously, using the appropriate path per Worker.

## Diagnostics

- `mailbox stats` across hosts — check inbox/processing/archive counts for sync health.
- Remote Worker status `STALE` → check transport connectivity, not Syncthing.
- Remote transport failures → re-verify `codeagent` CLI installation on the remote host.

---

*Content in this file is a structural placeholder. Deployment mode details are maintained alongside the protocol.*
