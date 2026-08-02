# codeagent

Multi-host code agent orchestration with SSH, session persistence, and routing.

Unified CLI for executing AI code agents across local and remote machines, with automatic session resumption and topic-based routing.

## Features

- **Multi-backend**: codex, claude, gemini, opencode, omp
- **SSH transport**: Independent ControlMaster per host, no global ControlPersist changes
- **Session persistence**: SQLite-backed registry, auto-resume by namespace key
- **Topic routing**: repo-map.json maps topics to host/path, with local detection
- **Remote helper**: `codeagent-remote-exec` console entrypoint, installed per host via `uv tool install`
- **Mailbox IPC**: session-based direct-inbox for agent-to-agent communication (local + cross-host via SSH)
- **Wire protocol**: JSONL over SSH stdin/stdout, no shell quoting issues
- **Swarm IPC**: IRC-style kernel with session/roster/ACL/routing, delivery engine with durable outbox, real-time receiver (watch + stream modes), artifact transport

## Installation

Requirements: Python 3.10+ and [uv](https://docs.astral.sh/uv/).
Zero runtime dependencies (stdlib only).

### One command

```bash
uv tool install "git+https://github.com/comicchang/codeagent-py"
```

Installs 5 entrypoints to `~/.local/bin` (ensure it's on your `PATH`).
Only **two** matter: `codeagent` (the CLI) and `codeagent-remote-exec`
(the remote helper, auto-discovered over SSH on each host).
`mailbox` / `mailbox-hook` / `mailbox-health` are authoritative mailbox
management entrypoints (not shims).

Remote hosts need the same single command (that's the whole deployment —
no daemon, no shared filesystem, no service).

### Alternatives

```bash
pip install "git+https://github.com/comicchang/codeagent-py"  # in a venv (PEP 668)
# or from source: git clone → uv tool install . --force
```

## Quick Start (agent chat, zero config)

No config files needed — a swarm session is pure CLI:

```bash
# 1. Start a session with 2 agents (manager + worker)
codeagent swarm create-session s1 --manager mgr --members w1
codeagent swarm register s1 --agent w1 --host __local__

# 2. Talk
codeagent swarm direct s1 --from mgr --to w1 --kind TASK --subject hi --body "hello w1"
codeagent swarm poll s1 --agent w1            # w1 reads its inbox

# 3. Broadcast / channel / notice / poll
codeagent swarm broadcast s1 --from mgr --kind NOTICE --subject sync --body "everyone"
codeagent swarm watch s1 --agent w1 --interval 2   # polling loop
```

Cross-host: register the worker with its SSH host instead of `__local__`
(`--host dev-server`) — delivery goes over SSH automatically, same commands.

Optional: topic routing (`repo-map.json`) and model config (`models.json`)
are only needed for `codeagent run`/`route` — see `examples/`.

## Usage

```bash
# Local execution
codeagent run "analyze the rendering pipeline"

# SSH to remote host
codeagent run "list all source files" ~/src/project --host dev-server

# Route via repo-map (topic → host → path)
codeagent route MyTopic "analyze module X" --repo 0
codeagent route list
codeagent route where "MyTopic"

# Session management
codeagent sessions list
codeagent sessions show <key>
codeagent sessions reset <key>
codeagent sessions bind --key <k> --id <session-id>

# SSH connection management
codeagent ssh warm dev-server build-box
codeagent ssh status
codeagent ssh stop dev-server
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

### models.json

Agent presets at `~/.codeagent/models.json` (shared with Go codeagent-wrapper):

```json
{
  "default_backend": "opencode",
  "default_model": "provider/model-name",
  "agents": {
    "explore": {
      "backend": "opencode",
      "model": "provider/fast-model",
      "description": "Code exploration (1M context, low cost)"
    },
    "develop": {
      "backend": "codex",
      "model": "provider/strong-model",
      "description": "Code implementation",
      "yolo": true
    },
    "reviewer": {
      "backend": "claude",
      "model": "provider/reasoning-model",
      "description": "Code review"
    },
    "oracle": {
      "backend": "codex",
      "model": "provider/strong-model",
      "description": "Persistent context technical advisor",
      "yolo": true
    },
    "oracle-arch": {
      "backend": "codex",
      "model": "provider/strong-model",
      "description": "Architecture decisions"
    }
  }
}
```

Use `--agent <name>` to select a preset. The `oracle` preset with session persistence enables cross-consultation context.

## Session Management

Sessions are auto-resumed by default. Key = `host:workdir:backend:agent`.

```bash
# View all sessions
codeagent sessions list

# Filter by host or topic
codeagent sessions list --host dev-server
codeagent sessions list --topic MyTopic

# Force new session (don't resume)
codeagent run "start fresh analysis" --new-session

# Manual session binding
codeagent sessions bind --key "dev-server:/src:opencode:explore" --id abc123
```

## SSH Connection Management

ControlMaster sockets are managed independently per host:

```bash
# Pre-establish connections (e.g., at session start)
codeagent ssh warm dev-server build-box

# Check status
codeagent ssh status
#   dev-server: alive (/run/user/1000/codeagent/ssh/abc123.sock)
#   build-box: dead

# Close connections
codeagent ssh stop dev-server
```

Socket path: `$XDG_RUNTIME_DIR/codeagent/ssh/<host-hash>.sock`

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  CLI / Runners                                                      │
│  ├─ config/repo_map.py       # repo-map.json loader                │
│  ├─ routing/resolver.py      # topic → host → path resolution      │
│  ├─ runners/go_wrapper.py    # Go codeagent-wrapper                 │
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

Remote hosts only need `codeagent-remote-exec` on PATH.  Install it on each
machine the same way as locally (no agent-side daemon, no shared filesystem):

```bash
# On each remote machine:
git clone https://github.com/comicchang/codeagent-py
cd codeagent-py
uv tool install . --force
codeagent --version        # verify the CLI
codeagent-remote-exec --help   # verify the remote helper
```

That is the whole deployment: five console entrypoints via `uv tool install`,
no manual pip steps.  Optional per-host configuration (topic routing, relay
login, shell prefix) lives in `~/.config/codeagent/repo-map.json` — see
`examples/repo-map.json`.

### Upgrading

To upgrade to a tagged release on all hosts:

```bash
# On each remote machine:
uv tool install --force git+https://github.com/comicchang/codeagent-py@v0.2.0
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
codeagent swarm create-session s1 --manager mgr --members w1,w2

# 2. Register agents (location = __local__ for co-located)
codeagent swarm register s1 --agent mgr --host __local__
codeagent swarm register s1 --agent w1  --host __local__

# 3. Send a direct message
codeagent swarm direct s1 --from mgr --to w1 --kind TASK --subject "analyze" --body "check src/"

# 4. Poll + ack lifecycle
out=$(codeagent swarm poll s1 --agent w1)
msg_id=$(echo "$out" | jq -r '.messages[0].msg_id')
codeagent swarm ack s1 --agent w1 --msg-id "$msg_id" --phase consumed
# --phase released returns the message to inbox for re-processing

# 5. Watch for new messages (continuous, polling loop)
codeagent swarm watch s1 --agent mgr --interval 2

# 6. Durable outbox (cross-host delivery with retry)
codeagent swarm outbox pending              # list undelivered envelopes
codeagent swarm outbox flush                 # retry all pending envelopes
codeagent swarm outbox status                # show outbox summary counts
```

### Mailbox CLI

`codeagent mailbox` provides the lower-level store operations used by the kernel:

```bash
# Session management
codeagent mailbox session-init --session s1 --manager mgr --agents w1,w2

# Send message
codeagent mailbox send --session s1 --from mgr --to w1 --kind TASK --subject "analyze" --body "..."

# Send with attachments (repeat --attachment for multiple)
codeagent mailbox send --session s1 --from mgr --to w1 --kind TASK \
  --subject "results ready" --body "see attached" \
  --attachment '{"artifact_id":"art-1","source_host":"worker-1","remote_root":"/tmp/artifacts","relative_path":"out/result.json","size":1024,"sha256":"'$(printf 'a%.0s' {1..64})'"}'

# Broadcast to every roster member except the sender
codeagent mailbox send --session s1 --from mgr --to '*' --kind NOTICE --subject "standby" --body "..."

# Peek inbox
codeagent mailbox peek --session s1 --agent w1

# Read (inbox→processing)
codeagent mailbox read --session s1 --agent w1 --owner w1

# Finalize (processing→archive)
codeagent mailbox finalize --session s1 --agent w1 --msg-id <id> --owner w1

# Status update
codeagent mailbox status --session s1 --agent w1 --state BUSY --current-task "working"

# Canonical history (newest first; filters: --since/--before/--limit/--from/--kind)
codeagent mailbox history --session s1 --json --kind TASK --limit 10
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
codeagent mailbox send --session s1 --from mgr --to w1 --kind EVIDENCE \
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
codeagent mailbox send --session s1 --from mgr --to w1 --kind TASK ... --host dev-server
codeagent mailbox peek --session s1 --agent w1 --host dev-server
```

### Standalone CLI

The `mailbox`, `mailbox-hook`, and `mailbox-health` commands remain available:

```bash
mailbox send --session s1 --from mgr --to w1 ...
mailbox-hook s1 w1
mailbox-health --session s1 --agent w1
```

### OMP Plugin Environment Variables

When launching agents via `codeagent run` or the OMP runner, these env vars
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

`codeagent` replaces `code_route.py` as the routing/execution layer. The Go `codeagent-wrapper` binary is preserved as-is for codex/claude/gemini/opencode backends.

| Old command | New command |
|-------------|-------------|
| `python3 code_route.py list` | `codeagent route list` |
| `python3 code_route.py where <topic>` | `codeagent route where <topic>` |
| `echo task \| python3 code_route.py route <topic>` | `codeagent route <topic> <task>` |

## Development

```bash
uv run pytest tests/ -v    # Run all tests
uv run codeagent --version # Verify CLI
```

## ACKNOWLEDGEMENTS

### codeagent-wrapper / myclaude

This project builds on **[stellarlinkco/myclaude](https://github.com/stellarlinkco/myclaude)**,
the Go-based `codeagent-wrapper` binary distributed as an npm package.

- **What we use**: `GoWrapperRunner` (`src/codeagent/runners/go_wrapper.py`) wraps the Go
  `codeagent-wrapper` binary to execute AI code agents through a unified interface.
- **Installation**: Wrapper is installed via `npx github:stellarlinkco/myclaude` (GitHub npm package, not public registry).
- **License**: Upstream wrapper is AGPL-3.0. codeagent-py calls it as an independent subprocess.

### tmux-agent-skills

The v3 session-based direct-inbox mailbox protocol, standalone CLI, and manager/worker skills
were previously maintained at **[comicchang/tmux-agent-skills](https://github.com/comicchang/tmux-agent-skills)**
(now archived). The protocol lives on in `src/codeagent/mailbox/` and
`skills/tmux-agent-manager/`, `skills/tmux-agent-worker/`.

A unified `tmux-agent` skill (B5 era) merges manager and worker into a single
skill with mode-based dispatch. Use `skill://tmux-agent-manager` for the
manager profile and `skill://tmux-agent-worker` for the worker profile.

## License

MIT — see [LICENSE](LICENSE).

Formerly EnPL-1.0. The Enlightened Public License is preserved at [LICENSE-EnPL-1.0.md](LICENSE-EnPL-1.0.md) for historical/theme reasons.
