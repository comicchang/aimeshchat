# Remote Deployment Mode (Cross-Host SSH Transport)

> Protocol reference: `skill://agent-swarm/protocol/mailbox.md` | Role files: `skill://agent-swarm/roles/manager.md`, `skill://agent-swarm/roles/worker.md`

## 1. Overview

Remote mode applies when Workers are on hosts **without shared filesystem access** to the Manager's mailbox root. All communication crosses a host boundary via SSH transport.

The primitive for all cross-host mailbox operations is:

```bash
codeagent mailbox <subcommand> ... --host <H>
```

This IS the real cross-host transport. Every cross-host read, send, peek, stats, and status call routes through this entry point. Never construct bare `mailbox` commands with guessed remote paths — `--host <H>` handles SSH routing, path resolution, and CLI invocation on the target host.

### What IS the transport

| Command | Scope | Notes |
|---|---|---|
| `codeagent mailbox read --session <id> --agent <a> --owner <a> --host <H>` | cross-host | Manager reads Worker's host-local inbox |
| `codeagent mailbox peek --session <id> --agent <a> --host <H>` | cross-host | non-destructive inbox count |
| `codeagent mailbox stats --session <id> --agent <a> --host <H>` | cross-host | 4-dir stats (inbox/processing/archive/_corrupt) |
| `codeagent mailbox send --host <H> ...` | cross-host | push a message to remote host's local mailbox CLI |
| `codeagent mailbox stats --session <id> --agent <a> --host <H>` | cross-host | read inbox/processing/archive/_corrupt counts (read-only) |
| `codeagent mailbox status --session <id> --agent <a> --host <H> ...` | cross-host | **write-only** status update (IDLE/BUSY/DONE/BLOCKED); not a read command |
| `codeagent swarm direct <s> --from X --to <a> --kind TASK ...` | local | SessionManifest-aware routing, local kernel delivery |

### What does NOT exist (never use)

| Phantom command | Real replacement | Why it fails |
|---|---|---|
| `codeagent swarm send ...` | `codeagent swarm direct ...` | `send` is not a subcommand; `direct` is the real dispatch command |
| `codeagent swarm poll --session ... --host <H>` | `codeagent mailbox read --host <H>` | `poll` is local-only; no `--host` flag |
| `codeagent swarm status --host <H>` | `codeagent mailbox stats --host <H>` | `status` is local-only; use `mailbox stats` for cross-host counts |
| `codeagent manager-poll` | `codeagent mailbox read --host <H>` | not a real subcommand |
| `codeagent swarm status --all-hosts` | iterate hosts with `mailbox stats --host <H>` | not a real subcommand |
| `tmux send-keys` on remote Workers | n/a | no shared tmux socket across hosts |

## 2. Execution Mode

Each Worker declares `execution_mode` in the SessionManifest. The two modes are **mutually exclusive** within a session.

| `execution_mode` | Description | Worker host requirements | Mailbox lifecycle |
|---|---|---|---|
| `mailbox-worker` | Full OMP process runs on Worker host; communicates via mailbox protocol | `omp` binary + mailbox plugin | Full INIT → TASK → REPORT via mailbox |
| `local-omp-mcp` | All OMP agents/models run on Manager host; Worker host runs only `omp-execd --stdio` MCP server | `omp-execd` binary only | **No mailbox INIT lifecycle**; tasks dispatched via MCP tool calls |

### Decision table

```
Does the Worker host have the full `omp` binary and can run OMP agents locally?
  │
  ├─ Yes → mailbox-worker
  │   - Worker runs its own OMP process with plugin
  │   - Full mailbox INIT/TASK/REPORT lifecycle
  │   - Requires bidirectional SSH OR manager-pull return mode
  │
  └─ No (only needs MCP executor) → local-omp-mcp
      - Manager host owns all agent/model execution
      - Worker host runs `omp-execd --stdio` as MCP server
      - No mailbox INIT; tasks dispatched via named MCP workspace tools
      - Only requires Manager→Worker SSH (unidirectional)
```

**`local-omp-mcp` does not participate in the mailbox INIT/TASK/REPORT lifecycle.** Its tasks are distributed through MCP tool calls, not mailbox messages. The mailbox path exists only for compatibility or legacy diagnostics.

`mailbox-worker` and `local-omp-mcp` must not be used for the same `agent_id` within one session. `session-init` validates manifest consistency and rejects conflicting declarations.

## 3. Topology Profiles

Cross-host topology is determined by which hosts can SSH to which.

### Profile B: Bidirectional

Both Manager→Worker and Worker→Manager SSH are available.

```
┌──────────┐   SSH (both directions)   ┌──────────┐
│ Manager  │◄──────────────────────────►│ Worker H │
│   host   │                            │   host   │
└──────────┘                            └──────────┘
```

- Workers can write directly to Manager's host-local mailbox.
- Manager can read Worker's host-local mailbox.
- Both sides use `codeagent mailbox ... --host <H>`.
- Suitable for `mailbox-worker` execution mode.

### Profile C: Manager-Pull (default for cross-host)

Only Manager→Worker SSH is available. Worker cannot SSH back to Manager.

```
┌──────────┐    SSH (one direction)     ┌──────────┐
│ Manager  │───────────────────────────►│ Worker H │
│   host   │    ◄─── Worker writes      │   host   │
│          │        to host-local mgr   └──────────┘
│          │        inbox; Manager
│          │        pulls via --host
└──────────┘
```

**This is the default for cross-host with single-direction SSH.** Manager-pull is the correct return mode whenever Worker→Manager SSH is unavailable or unreliable.

## 4. Manager-Pull Workflow

Manager-pull uses a "push task, pull results" pattern:

### Step 1: Manager pushes TASK to Worker host

```bash
codeagent mailbox send \
  --session <session-id> --from manager --to <worker-id> \
  --kind TASK --subject "<subject>" --body "<body>" \
  --host <worker-host>
```

This invokes the mailbox CLI on the remote host via SSH, writing the message into the Worker's host-local inbox.

### Step 2: Worker processes on its host

Worker reads from its local inbox (no `--host` needed — it's local):

```bash
mailbox read --session <session-id> --agent <worker-id> --owner <worker-id> --json
# ... process task ...
mailbox status --session <session-id> --agent <worker-id> \
  --state BUSY --current-task "<task>" --last-conclusion "<progress>"
```

### Step 3: Worker writes REPORT to host-local Manager inbox

Worker writes the REPORT to Manager's inbox **on the Worker's own host**. This is a local write — the Manager's mailbox root on the Worker host is pre-configured or declared in the manifest:

```bash
mailbox send \
  --session <session-id> --from <worker-id> --to manager \
  --kind REPORT --subject "<subject>" --body "<body>"
```

### Step 4: Manager pulls REPORT from Worker host

```bash
codeagent mailbox read \
  --session <session-id> --agent manager --owner manager \
  --host <worker-host>
```

Manager uses `--host <H>` to SSH into the Worker host and read from the Manager's inbox there. The two-phase consumption (read → finalize) still applies:

```bash
# Claim from remote host
codeagent mailbox read --session <session-id> --agent manager --owner manager --host <H> --json
# ... verify report ...
# Finalize on remote host
codeagent mailbox finalize --session <session-id> --agent manager --msg-id <id> --owner manager --host <H>
```

### Worker status write (host-local)

Worker writes status to its own host-local mailbox — no `--host`:

```bash
mailbox status --session <session-id> --agent <worker-id> \
  --state DONE --current-task "<task>" --last-conclusion "<result>"
```

### Manager status check (remote)

Manager reads Worker status from the Worker's host:

```bash
# Read inbox/processing/archive counts (cross-host stats)
codeagent mailbox stats --session <session-id> --agent <worker-id> --host <worker-host>
```

### Key constraint

Worker MUST NOT attempt reverse SSH to Manager. The host-local Manager inbox is the **only** return path in manager-pull mode. `send-keys` is impossible (no shared tmux socket). No other transport exists.

**Mailbox path resolution**: Worker uses bare `mailbox` CLI (local FS, no `--host`) for all reads, writes, and status updates on its own host. Manager uses `codeagent mailbox ... --host <H>` for all cross-host operations to the Worker's host. Never use `--host` on the Worker side or omit it on the Manager side when crossing host boundaries.

## 5. Worker Startup by Execution Mode

### `mailbox-worker` startup

Worker host runs a full OMP process with mailbox plugin. Standard INIT lifecycle applies.

```bash
# 1. Manager pushes INIT TASK to Worker host
codeagent mailbox send \
  --session <session-id> --from manager --to <worker-id> \
  --kind TASK --subject "INIT" --body "<init-body>" \
  --host <worker-host>

# 2. Worker host: OMP process starts, plugin activates
#    Worker reads INIT from local inbox
mailbox read --session <session-id> --agent <worker-id> --owner <worker-id> --json
#    Validates identity, registers, writes IDLE
mailbox status --session <session-id> --agent <worker-id> \
  --state IDLE --current-task "waiting for TASK" --last-conclusion "INIT accepted"
mailbox finalize --session <session-id> --agent <worker-id> --msg-id <id> --owner <worker-id>

# 3. Manager verifies Worker status (read stats from remote host)
codeagent mailbox stats --session <session-id> --agent <worker-id> --host <worker-host>
#    Expect: Worker processed INIT; state=IDLE visible in status.json on Worker host
```

The Worker then runs `mailbox-health` gate check and enters normal TASK polling. All subsequent communication uses the manager-pull pattern (§4).

### `local-omp-mcp` startup

Worker host runs only `omp-execd --stdio` as an MCP server. **No mailbox INIT lifecycle.**

```bash
# 1. Manager launches omp-execd on Worker host via SSH
ssh <worker-host> "omp-execd --stdio --workspace <path>"

# 2. Manager connects MCP client to the remote executor
#    Tasks are dispatched via MCP tool calls, not mailbox messages

# 3. Results flow back through MCP response channel
#    No REPORT messages, no status.json writes from Worker
```

The `local-omp-mcp` Worker does not read INIT, does not write status.json, and does not participate in the mailbox protocol. Its communication is entirely through the MCP tool/response channel managed by the Manager host.

## 6. Stream vs Poll

### Polling model (default for remote Workers)

Remote Workers without a local notification adapter must actively poll:

```bash
# Worker polls at boundaries
mailbox peek --session <session-id> --agent <worker-id>     # count check
mailbox read --session <session-id> --agent <worker-id> --owner <worker-id> --json  # claim
```

Polling points:
- Task start (before entering work loop)
- After each major phase boundary
- Before sending final REPORT
- After terminal status write (to catch any trailing messages)

### Cursor and reconnect

The mailbox read command auto-advances: each `read` claims one message from inbox to processing. There is no external cursor to manage. If a Worker crashes mid-processing, `mailbox recover-stale` returns expired messages (>300s lease) to inbox on next startup.

Reconnect after disconnect:
1. Run `mailbox recover-stale` to reclaim any orphaned processing messages.
2. Check `mailbox stats` for non-zero processing count.
3. Resume normal poll → read → process → finalize cycle.

### Notification-only constraint

An OMP plugin or adapter on the Worker host MAY provide `peek`-based notification (`📬 MAILBOX: N pending...`). This is **advisory only** — it never consumes messages. The Worker MUST still call `mailbox read` to claim. Plugin notification accelerates response but does not replace polling.

## 7. Diagnostics

### Cross-host mailbox stats

```bash
# Check Worker inbox health from Manager host
codeagent mailbox stats --session <session-id> --agent <worker-id> --host <worker-host>
# Shows: inbox/processing/archive/_corrupt counts
```

Interpreting stats:
- `inbox > 0` + Worker `status.json` shows `IDLE` → Worker hasn't picked up the task yet
- `processing > 0` for extended time → Worker may have crashed; run `recover-stale` on Worker host
- `corrupt > 0` → check message validation; notify sender to resend
- `archive` growing normally → healthy processing

### Host preflight checklist

Before launching remote Workers, verify:

- [ ] **SSH connectivity**: `ssh <worker-host> echo ok` succeeds without interactive prompt
- [ ] **CLI installed**: `ssh <worker-host> "which codeagent && codeagent --version"` returns expected version
- [ ] **Mailbox root exists**: `ssh <worker-host> "ls $MAILBOX_ROOT/<session-id>/"` shows expected directory structure (use the host's actual `MAILBOX_ROOT` or `resolve_root()`; never assume a fixed `.mailbox/` path)
- [ ] **Roster registered**: `session.json` on Worker host includes the Worker's `agent_id` in agents list
- [ ] **Execution mode consistent**: manifest declares correct `execution_mode` for this Worker
- [ ] **omp binary** (for `mailbox-worker`): `ssh <worker-host> "which omp"` succeeds
- [ ] **omp-execd binary** (for `local-omp-mcp`): `ssh <worker-host> "which omp-execd"` succeeds
- [ ] **Return mode declared**: `manager-pull` is set if Worker→Manager SSH is unavailable

### Status diagnostics

```bash
# Manager checks Worker stats across hosts
codeagent mailbox stats --session <session-id> --agent <worker-id> --host <worker-host>

# STALE diagnosis: updated_at exceeds SLA
# → check SSH connectivity, Worker process liveness, inbox count
# → STALE is diagnostic only, not equivalent to IDLE or BLOCKED
```

## 8. Runbook: 5-Worker Mixed Session

> **Pre-conditions**: All commands below are verified against the installed CLI (`codeagent 0.2.5+`).
> `create-session` requires `--manager` and `--members` flags (added by CLI fix subagent).
> `swarm direct` requires `--run-id` / `--request-id` for envelope routing (added by CLI fix subagent).
> Remote hosts MUST have `codeagent` in `PATH` and mailbox root directories writable by the SSH user.
> Manager MUST have SSH access (`ControlMaster` preferred) to all worker hosts.

Example: Manager on host `M` orchestrating 5 Workers across 3 remote hosts, using manager-pull return mode.

### Manifest

| Worker ID | Host | execution_mode | backend |
|---|---|---|---|
| `w-frontend` | `H1` | `mailbox-worker` | `cli` |
| `w-backend` | `H1` | `mailbox-worker` | `cli` |
| `w-reverse` | `H2` | `mailbox-worker` | `cli` |
| `w-docs` | `H2` | `mailbox-worker` | `cli` |
| `w-remote-analysis` | `H3` | `local-omp-mcp` | `omp` |

> Backend values are `cli | omp | tmux` (the `register --backend` enum). `cli` = mailbox-worker via CLI transport; `omp` = OMP process with mailbox plugin; `tmux` = tmux-pane agent. `mcp` does NOT exist as a backend value.

---

### Phase 0: Create session + register routing (on M)

```bash
SID="run-$(date +%s)"

# 1. Create session with roster
#    --manager: the agent ID running on M
#    --members: all agent IDs in this session (comma-separated)
codeagent swarm create-session "$SID" \
  --manager manager \
  --members w-frontend,w-backend,w-reverse,w-docs,w-remote-analysis

# 2. Register each worker's host + backend
#    --backend: cli (mailbox-worker via CLI) or omp (OMP process)
codeagent swarm register "$SID" --agent w-frontend        --host H1 --backend cli
codeagent swarm register "$SID" --agent w-backend         --host H1 --backend cli
codeagent swarm register "$SID" --agent w-reverse         --host H2 --backend cli
codeagent swarm register "$SID" --agent w-docs            --host H2 --backend cli
codeagent swarm register "$SID" --agent w-remote-analysis --host H3 --backend omp
```

---

### Phase 1: Remote session-init + preflight (on M → each host)

```bash
# 1. Initialize mailbox session on each worker host
#    Creates per-agent mailbox dirs + session.json on the remote host
codeagent mailbox session-init --session "$SID" --manager manager \
  --agents w-frontend,w-backend --host H1

codeagent mailbox session-init --session "$SID" --manager manager \
  --agents w-reverse,w-docs --host H2

# 2. Preflight: SSH + CLI on all hosts
for H in H1 H2 H3; do
  ssh "$H" "codeagent --version" || echo "FAIL: $H missing codeagent"
done

# 3. Verify mailbox dirs exist on each host
for H in H1 H2; do
  ssh "$H" "ls $HOME/.mailbox/$SID/" || echo "FAIL: $H no mailbox root for $SID"
done

# 4. Verify H3 has omp binary (for local-omp-mcp mode)
ssh H3 "which omp-execd" || echo "FAIL: H3 missing omp-execd"
```

---

### Phase 2: Send INIT via mailbox send (on M → remote hosts)

INIT uses `mailbox send --host` for cross-host delivery, NOT `swarm direct` (which is local-kernel only).

```bash
# Push INIT TASK to each mailbox-worker on H1
codeagent mailbox send --session "$SID" --from manager --to w-frontend \
  --kind TASK --subject "INIT" --body '{"agent_id":"w-frontend","role":"frontend-engineer"}' \
  --host H1

codeagent mailbox send --session "$SID" --from manager --to w-backend \
  --kind TASK --subject "INIT" --body '{"agent_id":"w-backend","role":"backend-engineer"}' \
  --host H1

# Push INIT TASK to each mailbox-worker on H2
codeagent mailbox send --session "$SID" --from manager --to w-reverse \
  --kind TASK --subject "INIT" --body '{"agent_id":"w-reverse","role":"reverse-engineer"}' \
  --host H2

codeagent mailbox send --session "$SID" --from manager --to w-docs \
  --kind TASK --subject "INIT" --body '{"agent_id":"w-docs","role":"docs-engineer"}' \
  --host H2

# H3 (w-remote-analysis): no mailbox INIT
# Launch omp-execd on H3; manager connects via MCP tool calls
ssh H3 "omp-execd --stdio --workspace /data/analysis" &
```

---

### Phase 3: Wait for IDLE via mailbox stats (on M)

```bash
# Poll until all mailbox-workers report IDLE (inbox=0, archive>0)
for H in H1 H2; do
  for W in w-frontend w-backend w-reverse w-docs; do
    # Skip workers not on this host
    case "$W" in
      w-frontend|w-backend) [ "$H" = "H1" ] || continue ;;
      w-reverse|w-docs)     [ "$H" = "H2" ] || continue ;;
    esac
    echo "=== $W on $H ==="
    codeagent mailbox stats --session "$SID" --agent "$W" --host "$H"
  done
done

# Also check worker status.json on the remote host
for H in H1 H2; do
  ssh "$H" "cat ~/.local/share/codeagent/mailbox/$SID/w-*/status.json 2>/dev/null" || true
done
```

---

### Phase 4: Dispatch TASK + manager-pull REPORT (on M)

```bash
# 1. Push TASK to each mailbox-worker
codeagent mailbox send --session "$SID" --from manager --to w-frontend \
  --kind TASK --subject "Implement login UI" \
  --body '{"task":"Build login form with OAuth2 buttons","run_id":"r1","request_id":"req1"}' \
  --host H1

codeagent mailbox send --session "$SID" --from manager --to w-backend \
  --kind TASK --subject "Auth API endpoints" \
  --body '{"task":"Implement /auth/login and /auth/callback","run_id":"r1","request_id":"req2"}' \
  --host H1

codeagent mailbox send --session "$SID" --from manager --to w-reverse \
  --kind TASK --subject "Analyze binary X" \
  --body '{"task":"Reverse engineer /tmp/target.bin","run_id":"r1","request_id":"req3"}' \
  --host H2

codeagent mailbox send --session "$SID" --from manager --to w-docs \
  --kind TASK --subject "Write API docs" \
  --body '{"task":"Document /auth endpoints in OpenAPI 3.1","run_id":"r1","request_id":"req4"}' \
  --host H2

# H3 (w-remote-analysis): dispatch via MCP tool call, not mailbox

# 2. Pull REPORT from each remote host
#    Manager reads from its own inbox on the remote host (--agent manager --owner manager)
codeagent mailbox read --session "$SID" --agent manager --owner manager --host H1 --json
codeagent mailbox read --session "$SID" --agent manager --owner manager --host H2 --json

# 3. Verify artifacts (if REPORT includes attachment refs)
# codeagent artifact pull --host H1 --artifact-id <id> \
#   --relative-path output/result.tar.gz --size <bytes> --sha256 <hex> --dest ./artifacts/

# 4. Finalize consumed messages on each host
codeagent mailbox finalize --session "$SID" --agent manager --msg-id <report-msg-id> --owner manager --host H1
codeagent mailbox finalize --session "$SID" --agent manager --msg-id <report-msg-id> --owner manager --host H2
```

---

### Key observations

- **`swarm create-session`** requires `--manager` and `--members` — bare `create-session <sid>` fails without them.
- **`swarm register --backend`** accepts `cli | omp | tmux` only. `mcp` does NOT exist; use `omp` for OMP-process workers.
- **`swarm direct`** is local-kernel dispatch only (no `--host`). For cross-host, use `mailbox send --host <H>`.
- **`mailbox send --host`** works because the CLI extracts `--host` from raw REMAINDER args — it is valid after the subcommand.
- **`session-init`** runs on each remote host via `--host` to create per-agent mailbox dirs before any messages arrive.
- **Manager-pull**: Manager reads its own inbox on the worker host (`--agent manager --owner manager --host H`). Workers write REPORT locally (no `--host`). Manager never SSHs back from worker side.
- **`local-omp-mcp` Worker (w-remote-analysis on H3)** bypasses the entire mailbox lifecycle — no `session-init`, no INIT, no REPORT. Tasks go via MCP tool calls.
- **Host H1 and H2** each host two workers. Manager pulls once per host (mailbox returns one message at a time from the Manager inbox).
- **No `send-keys`** at any point — all workers are remote; no shared tmux socket.
