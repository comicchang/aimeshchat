# codeagent

Multi-host code agent orchestration with SSH, session persistence, and routing.

Unified CLI for executing AI code agents across local and remote machines, with automatic session resumption and topic-based routing.

## Features

- **Multi-backend**: codex, claude, gemini, opencode, omp
- **SSH transport**: Independent ControlMaster per host, no global ControlPersist changes
- **Session persistence**: SQLite-backed registry, auto-resume by namespace key
- **Topic routing**: repo-map.json maps topics to host/path, with local detection
- **Remote helper**: `codeagent-remote-exec` console entrypoint, deployed via `dotai setup`
- **Mailbox IPC**: session-based direct-inbox for agent-to-agent communication (local + cross-host via SSH)
- **Wire protocol**: JSONL over SSH stdin/stdout, no shell quoting issues

## Installation

```bash
cd codeagent-py
uv sync
uv run codeagent --version
```

Or install as CLI tool:

```bash
uv tool install .
```

## Quick Start

```bash
# 1. Create config directory
mkdir -p ~/.config/codeagent

# 2. Copy example configs (see below)
cp examples/repo-map.json ~/.config/codeagent/
cp examples/models.json ~/.codeagent/

# 3. Verify
codeagent route list
codeagent ssh status
```

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
      "ssh_alias": "build-box.internal",
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
codeagent --new-session "start fresh analysis"

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
codeagent CLI
  ├─ config/repo_map.py    # repo-map.json loader
  ├─ routing/resolver.py   # topic → host → path resolution
  ├─ runners/
  │   ├── go_wrapper.py    # Go codeagent-wrapper (codex/claude/gemini/opencode)
  │   └── omp.py           # omp CLI
  ├─ session/
  │   ├── registry.py      # SQLite session store
  │   ├── key.py           # namespace key computation
  │   └── lock.py          # per-key flock
  ├─ transport/
  │   ├── local.py         # local subprocess
  │   ├── ssh.py           # SSH + ControlMaster
  │   └── control_master.py
  ├─ wire/protocol.py      # JSONL wire protocol
  └─ remote_exec.py        # remote helper (deployed to each host)
```

## Remote Deployment

Remote hosts need `codeagent-remote-exec` on PATH. Deploy via dotai setup:

```bash
# On each remote machine (part of dotai setup):
dotai setup
```

This installs `codeagent` and `codeagent-remote-exec` via `uv tool install` from the
cloned codeagent-py repo. No manual pip install needed.

## Mailbox (Agent IPC)

`codeagent mailbox` provides cross-host agent-to-agent communication using
the session-based direct-inbox protocol:

```bash
# Session management
codeagent mailbox session-init --session s1 --manager mgr --agents w1,w2

# Send message
codeagent mailbox send --session s1 --from mgr --to w1 --kind TASK --subject "analyze" --body "..."

# Peek inbox
codeagent mailbox peek --session s1 --agent w1

# Read (inbox→processing)
codeagent mailbox read --session s1 --agent w1 --owner w1

# Finalize (processing→archive)
codeagent mailbox finalize --session s1 --agent w1 --msg-id <id> --owner w1

# Status update
codeagent mailbox status --session s1 --agent w1 --state BUSY --current-task "working"
```

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

## License

MIT
