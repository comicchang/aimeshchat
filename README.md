# aimeshchat

Multi-host code agent orchestration with SSH, session persistence, and routing.

Unified CLI for executing AI code agents across local and remote machines, with automatic session resumption and topic-based routing.

## Features

- **Backends**: omp (default), opencode (warm resume), generic (explicit-only)
- **SSH transport**: Independent ControlMaster per host, no global ControlPersist changes
- **Session persistence**: SQLite-backed registry, auto-resume by namespace key
- **Topic routing**: repo-map.json maps topics to host/path, with local detection
- **Remote helper**: `aimeshchat-remote-exec` console entrypoint, installed per host via `uv tool install`
- **Mailbox IPC**: session-based direct-inbox for agent-to-agent communication (local + cross-host via SSH)
- **Wire protocol**: JSONL over SSH stdin/stdout, no shell quoting issues
- **Swarm IPC**: IRC-style kernel with session/roster/ACL/routing, delivery engine with durable outbox, real-time receiver (watch + stream modes), artifact transport

## Installation

Requirements: Python 3.10+ and [uv](https://docs.astral.sh/uv/).
Zero runtime dependencies (stdlib only).

### One command

```bash
uv tool install "git+https://github.com/comicchang/postmesh-py"
```

Installs 5 entrypoints to `~/.local/bin` (ensure it's on your `PATH`).
Only **two** matter: `aimeshchat` (the CLI) and `aimeshchat-remote-exec`
(the remote helper, auto-discovered over SSH on each host).
`mailbox` / `mailbox-hook` / `mailbox-health` are authoritative mailbox
management entrypoints (not shims).

Remote hosts need the same single command (that's the whole deployment —
no daemon, no shared filesystem, no service).

### Alternatives

```bash
pip install "git+https://github.com/comicchang/postmesh-py"  # in a venv (PEP 668)
# or from source: git clone → uv tool install . --force
```

## Quick Start (agent chat, zero config)

No config files needed — a swarm session is pure CLI:

```bash
# 1. Start a session with 2 agents (manager + worker)
aimeshchat swarm create-session s1 --manager mgr --members w1
aimeshchat swarm register s1 --agent w1 --host __local__

# 2. Talk
aimeshchat swarm direct s1 --from mgr --to w1 --kind TASK --subject hi --body "hello w1"
aimeshchat swarm poll s1 --agent w1            # w1 reads its inbox

# 3. Broadcast / channel / notice / poll
aimeshchat swarm broadcast s1 --from mgr --kind NOTICE --subject sync --body "everyone"
aimeshchat swarm watch s1 --agent w1 --interval 2   # polling loop
```

Cross-host: register the worker with its SSH host instead of `__local__`
(`--host dev-server`) — delivery goes over SSH automatically, same commands.

Optional: topic routing (`repo-map.json`) is only needed for `aimeshchat run`/`route` — see `examples/`.
Models are passed per invocation (`--model`/`--variant`/`--system`) — there is no `models.json`.

## Usage

```bash
# Local execution
aimeshchat run "analyze the rendering pipeline"

# SSH to remote host
aimeshchat run "list all source files" ~/src/project --host dev-server

# Route via repo-map (topic → host → path)
aimeshchat route MyTopic "analyze module X" --repo 0
aimeshchat route list
aimeshchat route where "MyTopic"

# Session management
aimeshchat sessions list
aimeshchat sessions show <key>
aimeshchat sessions reset <key>
aimeshchat sessions bind --key <k> --id <session-id>

# SSH connection management
aimeshchat ssh warm dev-server build-box
aimeshchat ssh status
aimeshchat ssh stop dev-server
```

## Configuration

### repo-map.json

Location (searched in order):
1. `$CODEAGENT_REPO_MAP`
2. `~/.config/codeagent/repo-map.json`
3. `~/.codeagent/repo-map.json`

```json
{
  "midocs_root": "~/docs",
  "relay_zsh": "",
  "hosts": {
    "dev-server": {
      "ssh_alias": "dev-server",
      "hostnames": ["dev-server", "build-host-001"],
      "description": "Main development server",
      "transport": "ssh",
      "shell_prefix": "export PATH=$HOME/.local/bin:$PATH",
      "fallback_ssh_alias": ""
    },
    "build-box": {
      "ssh_alias": "build-box.example.com",
      "hostnames": ["build-box"],
      "description": "CI/build machine",
      "transport": "ssh",
      "shell_prefix": "",
      "fallback_ssh_alias": ""
    },
    "cloud-dev": {
      "ssh_alias": "cloud-dev.example.com",
      "hostnames": ["cloud-dev-user"],
      "description": "Cloud development environment",
      "transport": "relay-login",
      "shell_prefix": "",
      "fallback_ssh_alias": ""
    }
  }
}
```

> **`shell_prefix`** is passed as shell source to the remote host before
> each command.  Treat it as trusted-config-only — any value is executed
> verbatim on the target machine.

### Topic .repo-map.json

Place in `{midocs_root}/<TopicName>/.repo-map.json`:

```json
{
  "description": "My project analysis",
  "repos": [
    {"host": "dev-server", "path": "~/src/main-project", "note": "Main codebase"},
    {"host": "build-box", "path": "/opt/build/workspace", "note": "Build artifacts"}
  ]
}
```

### Model selection

There is **no `models.json`** — the CLI never reads one. Models are decided per
invocation via the `ExecutionSpec`, passed explicitly as CLI flags:

```bash
# Explicit model (authoritative — overrides everything else)
aimeshchat run "analyze the rendering pipeline" --model provider/model-name

# Oracle advisors expand an ExecutionSpec into explicit flags:
aimeshchat oracle start "$KEY" --model gpt-5.6-sol --variant reasoning \
  --system "..." --prompt "..."
```

Without `--model`, the resolution order is: the caller's `runtime.context`
(gateway mechanism) → `~/.omp/agent/execution-context.json` /
`$AIMESHCHAT_EXECUTION_CONTEXT` → as a backward-compat fallback only, the OMP
agent profile's `model:` field in `agents/<agent_type>.md` (`_read_agent_model()`).
If all are absent the command fails and asks for `--model`.

- `config.yml` `fallbackChains` / `modelRoles` are **never parsed**.
- `--agent` is a deprecated compatibility placeholder with **no model
  semantics** (passing it emits a deprecation warning). Pass
  `--model`/`--variant`/`--system` explicitly.
- Oracle advisors (`oracle` / `oracle-lite` / `oracle-opus`) work the same
  way — the skill owns the model/prompt strategy and passes it explicitly;
  see `skills/persist-oracle/SKILL.md`.

## Session Management

Sessions are auto-resumed by default. Key = `host:workdir:backend:agent`.

```bash
# View all sessions
aimeshchat sessions list

# Filter by host or topic
aimeshchat sessions list --host dev-server
aimeshchat sessions list --topic MyTopic

# Force new session (don't resume)
aimeshchat run "start fresh analysis" --new-session

# Manual session binding
aimeshchat sessions bind --key "dev-server:/src:opencode:explore" --id abc123
```

## SSH Connection Management

ControlMaster sockets are managed independently per host:

```bash
# Pre-establish connections (e.g., at session start)
aimeshchat ssh warm dev-server build-box

# Check status
aimeshchat ssh status
#   dev-server: alive (/run/user/1000/aimeshchat/ssh/abc123.sock)
#   build-box: dead

# Close connections
aimeshchat ssh stop dev-server
```

Socket path: `$XDG_RUNTIME_DIR/aimeshchat/ssh/<host-hash>.sock`

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  CLI / Runners                                                      │
│  ├─ config/repo_map.py       # repo-map.json loader                │
│  ├─ routing/resolver.py      # topic → host → path resolution      │
│  ├─ runners/base.py                # runner interface (omp/opencode)│
│  ├─ runners/omp.py           # omp CLI                             │
│  ├─ session/registry.py      # SQLite session store                 │
│  └─ remote_exec.py           # remote helper (deployed to hosts)    │
├─────────────────────────────────────────────────────────────────────┤
│  Swarm Layer                                                        │
│  ├─ swarm/kernel.py          # session/roster/ACL/routing kernel    │
│  ├─ swarm/delivery.py        # durable outbox → transport delivery  │
│  ├─ swarm/receiver.py        # real-time push (watch + stream)      │
│  ├─ swarm/model.py           # AgentLocation, Envelope, Address…    │
│  └─ hooks/swarm_hooks.py     # OMP plugin lifecycle hooks           │
├─────────────────────────────────────────────────────────────────────┤
│  Mailbox (Store / Protocol / CLI)                                   │
│  ├─ mailbox/store.py         # filesystem CRUD, two-phase read      │
│  ├─ mailbox/protocol.py      # Message schema, 7 kinds, attachments │
│  └─ mailbox/cli.py           # standalone mailbox CLI               │
├─────────────────────────────────────────────────────────────────────┤
│  Transport Router → Wire Protocol                                   │
│  ├─ transport/router.py      # centralized host → transport select  │
│  ├─ transport/ssh.py         # SSH + ControlMaster                  │
│  ├─ transport/relay.py       # bastion/PTY+expect                   │
│  ├─ transport/local.py       # local subprocess                     │
│  ├─ wire/protocol.py         # JSONL wire protocol                  │
│  └─ artifact.py              # SCP over CM, SHA256 verify           │
└─────────────────────────────────────────────────────────────────────┘
```

## Remote Deployment

Remote hosts only need `aimeshchat-remote-exec` on PATH.  Install it on each
machine the same way as locally (no agent-side daemon, no shared filesystem):

```bash
# On each remote machine:
git clone https://github.com/comicchang/postmesh-py
cd postmesh-py
uv tool install . --force
aimeshchat --version        # verify the CLI
aimeshchat-remote-exec --help   # verify the remote helper
```

That is the whole deployment: five console entrypoints via `uv tool install`,
no manual pip steps.  Optional per-host configuration (topic routing, relay
login, shell prefix) lives in `~/.config/codeagent/repo-map.json` — see
`examples/repo-map.json`.

### Upgrading

To upgrade to a tagged release on all hosts:

```bash
# On each remote machine:
uv tool install --force git+https://github.com/comicchang/postmesh-py@v0.2.0
```

The `--force` flag replaces the existing installation in-place. No daemon
restart needed — the next invocation uses the new version.

## Swarm IPC

IRC-style agent-to-agent communication via `SwarmKernel` — session/roster/ACL/routing with
pluggable delivery (local mailbox or cross-host via `TransportRouter`).
`watch` mode uses polling (configurable interval); `stream` mode uses real-time
push over SSH (long-lived connection, no polling).

### Quick Start (localhost)

```bash
# 1. Create a swarm session with manager + two workers
aimeshchat swarm create-session s1 --manager mgr --members w1,w2

# 2. Register agents (location = __local__ for co-located)
aimeshchat swarm register s1 --agent mgr --host __local__
aimeshchat swarm register s1 --agent w1  --host __local__

# 3. Send a direct message
aimeshchat swarm direct s1 --from mgr --to w1 --kind TASK --subject "analyze" --body "check src/"

# 4. Poll + ack lifecycle
out=$(aimeshchat swarm poll s1 --agent w1)
msg_id=$(echo "$out" | jq -r '.messages[0].msg_id')
aimeshchat swarm ack s1 --agent w1 --msg-id "$msg_id" --phase consumed
# --phase released returns the message to inbox for re-processing

# 5. Watch for new messages (continuous, polling loop)
aimeshchat swarm watch s1 --agent mgr --interval 2

# 6. Durable outbox (cross-host delivery with retry)
aimeshchat swarm outbox pending              # list undelivered envelopes
aimeshchat swarm outbox flush                 # retry all pending envelopes
aimeshchat swarm outbox status                # show outbox summary counts
```

### Mailbox CLI

`aimeshchat mailbox` provides the lower-level store operations used by the kernel:

```bash
# Session management
aimeshchat mailbox session-init --session s1 --manager mgr --agents w1,w2

# Send message
aimeshchat mailbox send --session s1 --from mgr --to w1 --kind TASK --subject "analyze" --body "..."

# Send with attachments (repeat --attachment for multiple)
aimeshchat mailbox send --session s1 --from mgr --to w1 --kind TASK \
  --subject "results ready" --body "see attached" \
  --attachment '{"artifact_id":"art-1","source_host":"worker-1","remote_root":"/tmp/artifacts","relative_path":"out/result.json","size":1024,"sha256":"'$(printf 'a%.0s' {1..64})'"}'

# Broadcast to every roster member except the sender
aimeshchat mailbox send --session s1 --from mgr --to '*' --kind NOTICE --subject "standby" --body "..."

# Peek inbox
aimeshchat mailbox peek --session s1 --agent w1

# Read (inbox→processing)
aimeshchat mailbox read --session s1 --agent w1 --owner w1

# Finalize (processing→archive)
aimeshchat mailbox finalize --session s1 --agent w1 --msg-id <id> --owner w1

# Status update
aimeshchat mailbox status --session s1 --agent w1 --state BUSY --current-task "working"

# Canonical history (newest first; filters: --since/--before/--limit/--from/--kind)
aimeshchat mailbox history --session s1 --json --kind TASK --limit 10
```

Sends land in the recipient's per-agent archive on finalize; the canonical
history (`<mailbox>/<session>/history/<msg_id>.json`) is an append-only,
session-wide log independent of per-recipient archives — a broadcast appends
exactly one record for the whole swarm. Messages may carry `attachments`
(list of artifact references: artifact_id, source_host, remote_root,
relative_path, size, sha256, media_type), validated on send.

Attachments are specified via repeatable `--attachment` flags, each taking
a JSON object:

```bash
aimeshchat mailbox send --session s1 --from mgr --to w1 --kind EVIDENCE \
  --subject "output" --body "attached" \
  --attachment '{"artifact_id":"art-1","source_host":"worker-1","remote_root":"/tmp/art","relative_path":"out/res.json","size":1024,"sha256":"'$(printf 'a%.0s' {1..64})'"}' \
  --attachment '{"artifact_id":"art-2","source_host":"worker-1","remote_root":"/tmp/art","relative_path":"out/log.txt","size":512,"sha256":"'$(printf 'b%.0s' {1..64})'","media_type":"text/plain"}'
```

The full set of attachment fields (all required except `media_type`, which
defaults to `application/octet-stream`):

| Field           | Description                                      |
|-----------------|--------------------------------------------------|
| artifact_id     | Unique artifact identifier                       |
| source_host     | Host alias where the artifact lives              |
| remote_root     | Absolute directory root on the remote host       |
| relative_path   | Path relative to remote_root (no traversal)      |
| size            | File size in bytes (non-negative integer)        |
| sha256          | 64-char lowercase hex SHA-256 digest             |
| media_type      | MIME type (default: application/octet-stream)    |

Consumers pull artifacts via `codeagent.artifact.pull_artifact` over the
existing SSH ControlMaster.

### Cross-Host

Add `--host <host>` to execute on a remote host via SSH:

```bash
aimeshchat mailbox send --session s1 --from mgr --to w1 --kind TASK ... --host dev-server
aimeshchat mailbox peek --session s1 --agent w1 --host dev-server
```

### Standalone CLI

The `mailbox`, `mailbox-hook`, and `mailbox-health` commands remain available:

```bash
mailbox send --session s1 --from mgr --to w1 ...
mailbox-hook s1 w1
mailbox-health --session s1 --agent w1
```

### OMP Plugin Environment Variables

When launching agents via `aimeshchat run` or the OMP runner, these env vars
are injected so the mailbox plugin activates automatically:

| Variable                    | Purpose                                                  |
|-----------------------------|----------------------------------------------------------|
| `OMP_MAILBOX_SESSION_ID`    | Swarm session ID (inherited from launcher)               |
| `OMP_MAILBOX_AGENT_ID`      | Worker agent ID within the session                       |
| `OMP_MAILBOX_IDENTITY_FILE` | Path to identity JSON (plugin polls this file to activate)|
| `MAILBOX_ROOT`              | Mailbox filesystem root (optional override)              |
| `SWARM_SESSION_ID`          | Alias for `OMP_MAILBOX_SESSION_ID`                       |

The plugin reads `OMP_MAILBOX_IDENTITY_FILE` at startup; when valid JSON
appears, it activates and begins polling its inbox. Identity belongs to the
launcher, not to the agent's reasoning.

## Relationship to code-route

`aimeshchat` replaces `code_route.py` as the routing/execution layer. Execution runs on the native `omp` / `opencode` runners — there is no Go `codeagent-wrapper`.

| Old command | New command |
|-------------|-------------|
| `python3 code_route.py list` | `aimeshchat route list` |
| `python3 code_route.py where <topic>` | `aimeshchat route where <topic>` |
| `echo task \| python3 code_route.py route <topic>` | `aimeshchat route <topic> <task>` |

## Development

```bash
uv run pytest tests/ -v    # Run all tests
uv run aimeshchat --version # Verify CLI
```

## ACKNOWLEDGEMENTS

### tmux-agent-skills

The v3 session-based direct-inbox mailbox protocol, standalone CLI, and manager/worker skills
were previously maintained at **[comicchang/tmux-agent-skills](https://github.com/comicchang/tmux-agent-skills)**
(now archived). The protocol lives on in `src/codeagent/mailbox/` and
已合并进 `skills/agent-swarm/`.

A unified `agent-swarm` skill (formerly `tmux-agent`, renamed in v0.2.x) merges
manager and worker into a single skill with role-based dispatch. Use
`skill://agent-swarm/roles/manager.md` for the manager profile and
`skill://agent-swarm/roles/worker.md` for the worker profile.

## License

MIT — see [LICENSE](LICENSE).

Formerly EnPL-1.0. The Enlightened Public License is preserved at [LICENSE-EnPL-1.0.md](LICENSE-EnPL-1.0.md) for historical/theme reasons.
