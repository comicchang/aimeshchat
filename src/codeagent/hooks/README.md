# Swarm Hooks — OMP Plugin Integration

This module provides lifecycle hooks that wire the OMP (oh-my-pi) plugin
system into the SwarmKernel session/roster/messaging infrastructure.

## OMP Plugin YAML

To register these hooks in an OMP plugin, add the following to your
plugin configuration YAML:

```yaml
name: codeagent-swarm
description: Swarm kernel hooks for agent lifecycle management
version: "1.0"

hooks:
  on_agent_start:
    - module: codeagent.hooks.swarm_hooks
      function: on_agent_start
      params:
        # session_id and agent_id are injected from the plugin context
        host_alias: "__local__"
        backend: "omp"

  on_agent_message:
    - module: codeagent.hooks.swarm_hooks
      function: on_agent_message
      description: >
        Routes inbound messages through the swarm kernel.
        Message 'to' field: agent_id (direct), '#channel_id' (channel),
        '*' (broadcast).

  on_agent_stop:
    - module: codeagent.hooks.swarm_hooks
      function: on_agent_stop
```

## Hook Functions

### `on_agent_start(session_id, agent_id, host_alias, backend)`

Called when an OMP agent process starts. Registers the agent's location
in the SwarmKernel routing table so other agents can discover and
send messages to it.

**Parameters:**
- `session_id` — Swarm session ID (from plugin context)
- `agent_id` — Agent's unique identifier (from plugin context)
- `host_alias` — SSH alias or `"__local__"` for co-located agents
- `backend` — Runner backend: `"cli"`, `"omp"`, or `"tmux"`

### `on_agent_message(session_id, agent_id, msg_dict)`

Called for inbound messages that need routing through the swarm kernel.
Routes based on the `to` field:
- Plain agent ID → direct message
- `#channel_id` → channel message
- `*` → broadcast

### `on_agent_stop(session_id, agent_id)`

Called when an OMP agent process exits. Removes the agent from the
SwarmKernel routing table.

## Standalone Usage (Outside OMP)

```python
from codeagent.hooks.swarm_hooks import on_agent_start, on_agent_stop

# Register
on_agent_start(session_id="s1", agent_id="worker-1", backend="omp")

# ... agent does work ...

# Unregister
on_agent_stop(session_id="s1", agent_id="worker-1")
```

## Testing

Each hook function accepts an optional `store_root` parameter to
override the mailbox store location. Use `reset()` to clear the
module-level singleton between tests:

```python
from codeagent.hooks.swarm_hooks import reset, on_agent_start

reset()
on_agent_start("s1", "agent-1", store_root=tmp_path)
```
