# Swarm IRC Architecture: SSH-Based Cross-Device Agent Communication

## 1. Bottom-Line Recommendation

**Layered architecture with protocol-agnostic core.** The mailbox + wire protocols form the
"kernel" — implementation-agnostic, testable in isolation. Above it sit **backends** (CLI,
Oh My Pi plugin, Tmux plugin) that all speak the same protocol. They can coexist in hybrid
deployments (e.g., CLI for cross-host, OMP plugin for local agent integration, Tmux for UI).

**MVP complexity**: ~200 lines for the protocol kernel, ~100 lines per backend.
**Hybrid deployment**: zero extra code — backends are interchangeable by design.

---

## 2. Layered Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                     BACKENDS (implementation forms)               │
│                                                                   │
│  ┌──────────┐  ┌───────────────┐  ┌────────────┐  ┌───────────┐ │
│  │ CLI Tool │  │ Oh My Pi      │  │ Tmux       │  │ Future    │ │
│  │          │  │ Plugin        │  │ Plugin     │  │ Backends  │ │
│  │ codeagent│  │ omp-swarm     │  │ tmux-swarm │  │ (SDK,     │ │
│  │ swarm    │  │ plugin        │  │ plugin     │  │  REST,    │ │
│  │ send/poll│  │               │  │            │  │  ...)     │ │
│  └────┬─────┘  └───────┬───────┘  └─────┬──────┘  └─────┬─────┘ │
│       │                │                │               │       │
│       └────────────────┼────────────────┼───────────────┘       │
│                        │                │                        │
├────────────────────────┼────────────────┼────────────────────────┤
│              SWARM PROTOCOL KERNEL (implementation-agnostic)      │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │                    SwarmController (abstract)                 │ │
│  │  register() / send() / broadcast() / poll() / spawn()        │ │
│  └──────────────────────────┬──────────────────────────────────┘ │
│                             │                                     │
│  ┌──────────────────────────┼──────────────────────────────────┐ │
│  │                    Routing Table                              │ │
│  │              agent_id → (host_alias, backend)                 │ │
│  └──────────────────────────┼──────────────────────────────────┘ │
│                             │                                     │
│  ┌───────────────┐  ┌───────┴────────┐  ┌──────────────────────┐ │
│  │ Mailbox       │  │ Wire Protocol  │  │ Artifact Transport   │ │
│  │ Protocol      │  │ (JSONL)        │  │ (SCP over CM)        │ │
│  │ Message/Store │  │ encode/decode  │  │ pull/verify          │ │
│  └───────────────┘  └────────────────┘  └──────────────────────┘ │
│                                                                   │
├───────────────────────────────────────────────────────────────────┤
│                     TRANSPORT LAYER                                │
│                                                                   │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────────┐  │
│  │ SSH Transport│  │ Local FS     │  │ Relay Transport        │  │
│  │ (ControlMst) │  │ (co-located) │  │ (bastion/PTY+expect)   │  │
│  └──────────────┘  └──────────────┘  └────────────────────────┘  │
└───────────────────────────────────────────────────────────────────┘
```

**Key principle**: The protocol kernel has NO knowledge of which backend is calling it.
A message sent via the CLI backend is indistinguishable from one sent via the OMP plugin.
All backends read and write the same `.mailbox/` directory, use the same `Message` schema,
and share the same routing table.

---

## 3. Current Infrastructure Audit

### What already exists (all in codeagent-py)

| Layer | Module | Status |
|-------|--------|--------|
| SSH multiplexing | `transport/control_master.py` | Per-host ControlMaster, `warm`/`check`/`stop` |
| SSH transport | `transport/ssh.py` | Wire protocol over stdin/stdout, fallback aliases |
| Relay transport | `transport/relay.py` | PTY+expect for bastion hosts |
| Wire protocol | `wire/protocol.py` | JSONL, version negotiation, `mailbox` command |
| Remote exec | `remote_exec.py` | Deployed on every host, handles `mailbox` locally |
| Mailbox protocol | `mailbox/protocol.py` | 7 kinds, `BROADCAST_TO="*"`, `AttachmentRef` |
| Mailbox store | `mailbox/store.py` | Filesystem CRUD, two-phase read, lease claims, history |
| Mailbox CLI | `mailbox/cli.py` | Standalone CLI (`send`/`peek`/`read`/`finalize`) |
| Artifact transport | `artifact.py` | SCP over ControlMaster, SHA256 verification |
| Session registry | `session/registry.py` | SQLite-backed, per-key locking |
| Cross-host mailbox | `transport/ssh.py:_run_ssh_mailbox()` | Remote `codeagent mailbox` via SSH wire |

### What's missing for swarm

1. **Protocol kernel**: `SwarmController` abstract interface + routing table
2. **Backend implementations**: CLI / OMP plugin / Tmux plugin adapters
3. **Cross-host delivery**: Send to remote agent via SSH → remote mailbox
4. **Remote polling**: Check remote outboxes for messages addressed to us
5. **Swarm lifecycle**: Spawn remote worker + join session + teardown

---

## 4. Protocol Kernel Design (Implementation-Agnostic)

### 4.1 Core Abstraction

```python
# src/codeagent/swarm/kernel.py

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Protocol

@dataclass(frozen=True)
class AgentLocation:
    """Where an agent lives."""
    agent_id: str
    host_alias: str       # SSH alias, or "__local__" for co-located
    backend: str           # "cli" | "omp" | "tmux" | "custom"

class MessageSink(Protocol):
    """Anything that can receive a swarm message."""
    def on_message(self, msg: dict, location: AgentLocation) -> None: ...

class MessageSource(Protocol):
    """Anything that can produce a swarm message."""
    def next_message(self) -> Optional[dict]: ...

class SwarmKernel:
    """Protocol kernel — no backend knowledge, no I/O opinions.

    Backends register as sources/sinks. The kernel routes messages
    between them using the routing table and transport layer.
    """

    def __init__(self, transport: "SSHTransport") -> None: ...
    def register_agent(self, location: AgentLocation) -> None: ...
    def unregister_agent(self, agent_id: str) -> None: ...
    def send(self, session_id: str, to: str, kind: str,
             subject: str, body: str, **kwargs) -> str: ...
    def broadcast(self, session_id: str, kind: str,
                  subject: str, body: str, **kwargs) -> list[str]: ...
    def poll_remote(self, session_id: str, agent_id: str,
                    host_alias: str) -> Optional[dict]: ...
    def poll_all_remote(self, session_id: str) -> list[dict]: ...
```

### 4.2 Backend Interface

Every backend implements this minimal contract:

```python
class SwarmBackend(ABC):
    """A pluggable backend for the swarm kernel.

    Each backend is responsible for:
    - Presenting messages to the user/agent in its native UX
    - Accepting user/agent input in its native interaction model
    - Delegating to SwarmKernel for routing and delivery
    """

    @abstractmethod
    def start(self, kernel: SwarmKernel) -> None:
        """Initialize backend, subscribe to kernel events."""

    @abstractmethod
    def stop(self) -> None:
        """Tear down backend."""

    @abstractmethod
    def display_message(self, msg: dict, source: AgentLocation) -> None:
        """Show an incoming message in the backend's native UI."""

    @abstractmethod
    def collect_outgoing(self) -> list[dict]:
        """Return any messages the user/agent wants to send."""
```

### 4.3 Backend Implementations

#### A. CLI Backend (`codeagent swarm`)

```bash
# Send a message
codeagent swarm send --session s1 --to worker-A --kind TASK --body "analyze X"

# Poll inbox
codeagent swarm poll --session s1

# Continuous poll loop (daemon mode)
codeagent swarm watch --session s1 --interval 5

# Broadcast
codeagent swarm broadcast --session s1 --kind NOTICE --body "sync point"
```

**Strengths**: Scriptable, composable with Unix pipes, minimal dependencies.
**Best for**: Cross-host communication, CI/CD integration, cron-driven polling.

#### B. Oh My Pi Plugin

```yaml
# ~/.omp/plugins/swarm.yaml
name: swarm
version: 1
hooks:
  on_agent_start: swarm.register
  on_agent_message: swarm.send
  on_agent_stop: swarm.unregister
```

The OMP plugin integrates directly into the agent lifecycle:
- When an agent spawns → auto-register with the swarm kernel
- When an agent sends a message → route through swarm instead of local-only
- Received swarm messages appear as regular agent messages in OMP's UI

**Strengths**: Zero-config for OMP-native agents, seamless UX integration.
**Best for**: Local agent orchestration, session management.

#### C. Tmux Plugin

```bash
# ~/.tmux/plugins/tmux-swarm/
tmux split-window -v "codeagent swarm watch --session s1"
```

A tmux pane runs the swarm watch loop, displaying:
```
┌─ Swarm: s1 ─────────────────────────────────────────┐
│ [TASK]    manager → worker-A   "analyze rendering"   │
│ [REPORT]  worker-A → manager   "done: 3 findings"    │
│ [PROGRESS] worker-B → manager  "45/100 files"        │
│ [NOTICE]  manager → *          "checkpoint v2"       │
│──────────────────────────────────────────────────────│
│ > _                                                   │
└──────────────────────────────────────────────────────┘
```

**Strengths**: Always visible during tmux sessions, no window switching needed.
**Best for**: Real-time monitoring, manual intervention during long-running swarms.

### 4.4 Hybrid Deployment Example

```
┌─────────────────────────────────────────────────────────────┐
│ Manager Host (macOS)                                         │
│                                                              │
│  ┌──────────────────────┐   ┌──────────────────────────┐   │
│  │ SwarmKernel           │   │ .mailbox/<session>/        │   │
│  │  routing:             │   │  ├── routing.json          │   │
│  │    mgr    → __local__ │   │  ├── history/              │   │
│  │    wkr-A  → dev3-cf   │   │  ├── manager/inbox/        │   │
│  │    wkr-B  → yellow    │   │  └── ...                   │   │
│  └───┬──────┬──────┬─────┘   └──────────────────────────┘   │
│      │      │      │                                         │
│  ┌───┴──┐ ┌─┴───┐ ┌┴──────────┐                             │
│  │ CLI  │ │ OMP │ │ Tmux       │  ← All three active        │
│  │backend│ │plugin│ │ plugin    │     simultaneously         │
│  └──────┘ └─────┘ └────────────┘                             │
│      │      │      │                                         │
│      │      │      └─ "swarm watch" pane shows live feed     │
│      │      └─ OMP agents auto-join swarm on spawn            │
│      └─ CLI for ad-hoc commands: swarm send/broadcast         │
└──────────────────────────────────────────────────────────────┘
```

In this deployment:
- The **Tmux plugin** runs `codeagent swarm watch`, displaying all messages in a tmux pane
- The **OMP plugin** auto-registers agents as they spawn; agent messages flow through swarm
- The **CLI** is used for manual ad-hoc commands (send, broadcast, status)
- All three backends share the same `SwarmKernel` instance and `.mailbox/` directory

---

## 5. Topology: Hub-and-Spoke

```
┌──────────────────────────────────────────────────────────────┐
│                      Manager Host (Hub)                       │
│                                                               │
│  ┌────────────────────┐  ┌──────────────────────────────┐    │
│  │   SwarmKernel      │  │  .mailbox/<session>/          │    │
│  │                    │  │  ├── session.json (roster)     │    │
│  │  routing_table:    │  │  ├── routing.json             │    │
│  │    mgr   → local   │  │  ├── history/                 │    │
│  │    wkr-A → host-B  │  │  ├── manager/inbox/           │    │
│  │    wkr-B → host-C  │  │  ├── worker-A/ (local ref)    │    │
│  └────────┬───────────┘  │  └── worker-B/ (local ref)    │    │
│           │              └──────────────────────────────┘    │
│  ┌────────┴──────────────────────────────────────────────┐   │
│  │              SSH ControlMasters                         │   │
│  │  host-B: ~/.codeagent/sockets/a1b2c3.sock              │   │
│  │  host-C: ~/.codeagent/sockets/d4e5f6.sock              │   │
│  └───────────────────────────────────────────────────────┘   │
└──────────────┬──────────────────────┬────────────────────────┘
               │ SSH (push + poll)     │ SSH (push + poll)
      ┌────────┴───────┐       ┌──────┴──────────┐
      │    Host B       │       │    Host C        │
      │  ┌────────────┐ │       │  ┌─────────────┐ │
      │  │ worker-A   │ │       │  │ worker-B    │ │
      │  │ reads own  │ │       │  │ reads own   │ │
      │  │ local      │ │       │  │ local       │ │
      │  │ inbox      │ │       │  │ inbox       │ │
      │  └────────────┘ │       │  └─────────────┘ │
      │                 │       │                  │
      │  .mailbox/S/    │       │  .mailbox/S/     │
      │  ├── wkr-A/     │       │  ├── wkr-B/      │
      │  │   ├── inbox/ │       │  │   ├── inbox/  │
      │  │   └── ...    │       │  │   └── ...     │
      │  └── manager/   │       │  └── manager/    │
      │      └── inbox/ │       │      └── inbox/  │
      └─────────────────┘       └─────────────────┘
```

**Key design decisions:**

- **Each host has its own local mailbox.** No shared filesystem.
- **Manager's mailbox holds session roster + canonical history.**
- **Worker hosts have a `manager/` inbox** — the Manager polls it for outbound messages.
- **Remote workers only read their own local inbox** (no SSH needed for receive).
- **The Manager does all cross-host work**: push to remote inboxes, poll remote outboxes.

---

## 6. Message Flow

### 6.1 Manager → Worker (push)

```
Backend (CLI/OMP/Tmux) calls: kernel.send(to="worker-A", kind="TASK", body="analyze X")
  → SwarmKernel looks up: worker-A → AgentLocation(host="dev3-cf", backend="cli")
  → SSH to dev3-cf: codeagent mailbox send --session S --from manager --to worker-A ...
  → Message lands on dev3-cf: .mailbox/S/worker-A/inbox/<msg_id>.json
  → Worker-A polls: codeagent mailbox peek --session S --agent worker-A
  → Worker-A reads: codeagent mailbox read --session S --agent worker-A --owner worker-A
```

### 6.2 Worker → Manager (poll)

```
Worker-A writes locally: codeagent mailbox send --from worker-A --to manager ...
  → Message lands on dev3-cf: .mailbox/S/manager/inbox/<msg_id>.json
  → Manager periodically SSH-polls dev3-cf:
      ssh dev3-cf codeagent mailbox read --session S --agent manager --owner manager
  → Kernel delivers to all registered backends: display_message(msg, source=dev3-cf)
  → OMP plugin shows it in agent panel; Tmux plugin shows it in watch pane
```

### 6.3 Broadcast (Manager → all workers, routed to all backends)

```
Backend calls: kernel.broadcast(kind="NOTICE", body="checkpoint reached")
  → Kernel iterates routing table
  → For each remote worker:
      ssh <host> codeagent mailbox send --from manager --to <worker> ...
  → For local workers:
      codeagent mailbox send --from manager --to <worker> ...
  → Appends ONE canonical history record on Manager
  → All backends receive display_message() callback
```

### 6.4 Worker → Worker (relayed through Manager)

```
Worker-A → local mailbox send --to manager (body includes "forward_to": "worker-B")
  → Manager polls dev3-cf, reads message
  → Kernel detects forward_to, relays:
      ssh yellow codeagent mailbox send --from worker-A --to worker-B ...
  → Worker-B receives via local inbox poll
```

---

## 7. SSH Tunnel / AuthSock Forwarding Analysis

### 7.1 SSH_AUTH_SOCK forwarding → NOT APPLICABLE

`SSH_AUTH_SOCK` forwards the authentication agent's Unix socket for key signing only.
It uses a hardcoded `SSH2_AGENTC_*` protocol — cannot carry arbitrary messages.

**Verdict**: The mailbox protocol over SSH wire is the correct IPC layer.

### 7.2 SSH -R/-L port forwarding → POSSIBLE but OVERKILL

Adds port management, TCP server lifecycle, and auth complexity. The existing
`_run_ssh_mailbox()` via ControlMaster is simpler and already tested.

**Verdict**: Not recommended.

### 7.3 ControlMaster nesting → TRANSPARENT

```
Local → router (ProxyJump) → dev3 (final)
```

OpenSSH handles multi-hop internally. The application sees only `ssh dev3`.
`ControlPersist` and `ServerAliveInterval` apply end-to-end.

### 7.4 Cloudflare tunnel → TRANSPARENT

```
Host dev3-cf
    ProxyCommand cloudflared access ssh --hostname ...
```

`ProxyCommand` is a drop-in replacement for a TCP connection. ControlMaster works identically.

### 7.5 The user's actual topology

| Host | SSH path | ControlMaster |
|------|----------|---------------|
| `dev3-cf` | CF tunnel → router ProxyJump | One socket for `dev3-cf` |
| `dev4-cf` | CF tunnel → router ProxyJump | One socket for `dev4-cf` |
| `yellow` | Direct TCP | One socket for `yellow` |
| `vphone-vm` | `localhost:2222` | One socket for `vphone-vm` |
| `home` | Direct TCP | One socket for `home` |

All work with the existing `SSHTransport` — no swarm-layer awareness needed.

---

## 8. Reliability

### 8.1 Message Durability

| Scenario | Guarantee | Mechanism |
|----------|-----------|-----------|
| SSH disconnect mid-send | At-most-once | `os.replace()` on remote is atomic |
| Worker crash mid-read | Recoverable | Two-phase read with lease; `recover_stale()` after 300s |
| Manager crash | No message loss | Messages on worker filesystems; history on manager FS |
| Network partition | Autonomous operation | Workers continue locally; outbox queues messages |
| Disk full | Fail-fast | `fsync()` + error propagation |

### 8.2 Connection Recovery

```python
def _ensure_connected(self, host_alias: str) -> None:
    for attempt in range(3):
        try:
            if not self._transport.check(host_spec):
                self._transport.warm(host_spec)
            return
        except TransportError:
            if attempt == 2: raise
            time.sleep(2 ** attempt)
```

### 8.3 Polling Model

- Manager polls each remote host every N seconds (configurable, default 5s)
- Polling is cheap: one `ssh ... codeagent mailbox read` per host
- No persistent connections needed between polls
- Workers can be completely passive (read own local inbox)

---

## 9. MVP Implementation Path

### Phase 1: SwarmKernel (protocol kernel)

**New file**: `src/codeagent/swarm/kernel.py`

```python
class SwarmKernel:
    def register_agent(self, location: AgentLocation) -> None
    def send(self, session_id, to, kind, subject, body, **kw) -> str
    def broadcast(self, session_id, kind, subject, body, **kw) -> list[str]
    def poll_remote(self, session_id, agent_id, host_alias) -> Optional[dict]
    def poll_all_remote(self, session_id) -> list[dict]
```

**Lines**: ~150

### Phase 2: CLI Backend

**New subcommand**: `codeagent swarm`

```bash
codeagent swarm init   --session s1 --workers worker-A:dev3-cf,worker-B:yellow
codeagent swarm send   --session s1 --to worker-A --kind TASK --body "..."
codeagent swarm poll   --session s1
codeagent swarm watch  --session s1 --interval 5
codeagent swarm broadcast --session s1 --kind NOTICE --body "..."
```

**Lines**: ~120 (thin wrapper around SwarmKernel)

### Phase 3: Worker Integration

Workers run a minimal loop (on remote host or locally):

```bash
while true; do
  msg=$(codeagent mailbox read --session s1 --agent worker-A --owner worker-A)
  [ -n "$msg" ] && process_message "$msg"
  sleep 5
done
```

### Phase 4: Oh My Pi Plugin (optional)

```yaml
# ~/.omp/plugins/swarm.yaml
name: swarm
hooks:
  on_agent_start: swarm.register
  on_agent_message: swarm.send
```

**Lines**: ~80 (YAML config + Python adapter)

### Phase 5: Tmux Plugin (optional)

```bash
# ~/.tmux/plugins/tmux-swarm/swarm.tmux
tmux split-window -v "codeagent swarm watch --session s1"
```

**Lines**: ~30 (shell script)

---

## 10. What NOT to Build

1. **Real-time push** — Polling is fine. Tasks take minutes to hours; 5s latency is negligible.
2. **TCP servers** — SSH ControlMaster already provides authenticated, encrypted channels.
3. **Message broker** (RabbitMQ, Redis, NATS) — Overkill. Filesystem mailbox is persistent and concurrent.
4. **HTTP/WebSocket API** — SSH is the transport. Adding HTTP adds TLS, auth, new attack surface.
5. **Distributed consensus** — Manager is single authority for routing. Acceptable for MVP.
6. **Backend-specific protocol** — The kernel has ONE protocol. Backends adapt their UX, not the wire format.

---

## 11. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| SSH CM exhaustion | Low | Medium | ControlPersist=10; per-host multiplex limit |
| Poll overhead | Low | Low | Batch polls; increase interval for idle workers |
| CF tunnel auth expiry | Medium | High | ServerAliveInterval keeps alive; ControlPersist reuses session |
| FS lock contention | Low | Medium | O_EXCL + atomic rename; two-phase read |
| Message loss (manager crash) | Medium | Medium | Worker-host queues survive; history is append-only |
| Clock skew | Low | Low | Timestamps informational only; msg_id causal chain |

---

## 12. Comparison with Alternatives

| Approach | Pro | Con |
|----------|-----|-----|
| **Layered SSH Mailbox** (recommended) | 100% reuse; backend-agnostic; filesystem persistence | Polling latency; Manager SPOF |
| Central MQ (Redis/RabbitMQ) | Real-time push | New dependency; auth; deployment |
| SSH tunnel forwarding | Direct TCP | Port management; TCP server lifecycle |
| Syncthing-shared mailbox | Zero polling; P2P | Conflict resolution; Syncthing dependency |
| gRPC over SSH | Structured RPC | Overkill for text; proto compilation |
