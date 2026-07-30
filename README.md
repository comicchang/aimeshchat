# codeagent

Multi-host code agent orchestration with SSH, session persistence, and routing.

Unified CLI for executing AI code agents (codex/claude/gemini/opencode/omp) across local and remote machines, with automatic session resumption and topic-based routing.

## Features

- **Multi-backend**: codex, claude, gemini, opencode, omp
- **SSH transport**: Independent ControlMaster per host, no global ControlPersist changes
- **Session persistence**: SQLite-backed registry, auto-resume by namespace key
- **Topic routing**: repo-map.json maps topics to host/path, with local detection
- **Remote helper**: `python -m codeagent.remote_exec` deployed via dotai setup
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

## Usage

```bash
# Local execution
codeagent "analyze the rendering pipeline"

# SSH to remote host
codeagent --host yellow "list all Binder classes" ~/src/P1-4.0

# Route via repo-map (topic → host → path)
codeagent route OHOS-玻璃 "analyze frosted glass implementation" --repo 0
codeagent route list
codeagent route where "13-AOSP"

# Session management
codeagent sessions list
codeagent sessions show <key>
codeagent sessions reset <key>
codeagent sessions bind --key <k> --id <session-id>

# SSH connection management
codeagent ssh warm yellow dev3 gen8-cf
codeagent ssh status
codeagent ssh stop yellow
```

## Configuration

### repo-map.json

Location (searched in order):
1. `$CODEAGENT_REPO_MAP`
2. `~/.config/codeagent/repo-map.json`
3. `~/.codeagent/repo-map.json`
4. `~/src/dotai/profiles/policy/repo-map.json`

```json
{
  "midocs_root": "~/Dropbox/logseq/pages/mi-docs",
  "hosts": {
    "yellow": {
      "ssh_alias": "yellow",
      "hostnames": ["yellow", "mcshyucs192069"],
      "transport": "ssh",
      "shell_prefix": "export PATH=$HOME/.linuxbrew/bin:$PATH"
    }
  }
}
```

### models.json

Agent presets at `~/.codeagent/models.json` (shared with Go codeagent-wrapper).

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

The remote helper is deployed via the existing dotai setup flow:

```bash
# On each remote machine (already part of dotai setup):
python -m codeagent.remote_exec
```

No separate installation needed — `codeagent-py` is added to dotai's setup scripts.

## Development

```bash
uv run pytest tests/ -v    # 155 tests
uv run codeagent --version
```

## Relationship to code-route

`codeagent` replaces `code_route.py` as the routing/execution layer. The Go `codeagent-wrapper` binary is preserved as-is for codex/claude/gemini/opencode backends.

Migration: `code_route.py` commands map directly:
- `code_route.py list` → `codeagent route list`
- `code_route.py where <topic>` → `codeagent route where <topic>`
- `code_route.py route <topic>` → `codeagent route <topic> <task>`
