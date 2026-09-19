# Agent-swarm remote operations

The canonical remote deployment details remain in `skill://agent-swarm/operations/remote.md`. Use this reference for v2 gateway runtime, cross-device TASK lifecycle, remote writing, and manager-pull behavior.

## Gateway and runtime

The per-device Gateway (UDS) plus Manager-initiated SSH stdio is authoritative. Remote hosts do not connect back or expose HTTP/TCP. Gateway owns `session.ensure`, `runtime.spawn`, `runtime.send`, and event streaming; mailbox remains the durable task channel.

## TASK completion

`DELIVERED → READ → RUNNING → PROGRESS → DONE` is the observable lifecycle. Terminal completion requires a matching REPORT and verified artifact; runtime state alone is not terminal proof. During disconnect, durable outbox and cursor replay provide recovery.

## Writing boundary

Partition documents into non-overlapping requests. Include `base_revision`, `target_path`, and `artifact_id` in TASK/REPORT bodies. Only Manager applies one serialized merge request. Conflicting hashes for the same path produce `PROTOCOL_CONFLICT`; do not overwrite an existing artifact. Use `reply_to` for QUESTION/RESPONSE and `require_ack` for important messages.

## Legacy note

v1 commands are for unmigrated Workers only. v2 `omp-execd` MCP is a separate architecture and does not inherit mailbox lifecycle. Current cross-host manager-pull uses `aimeshchat mailbox ... --host <H>`.
