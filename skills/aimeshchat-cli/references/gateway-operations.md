# Gateway and remote worker operations

## Gateway failure decision

1. Run `aimeshchat gateway ensure --host <H>` and record complete stderr.
2. Probe independently with `aimeshchat ssh warm <H>` and `aimeshchat ssh status`.
3. Classify the failure:

| Evidence | DO | DON'T | Completion evidence |
|---|---|---|---|
| `REMOTE_UPGRADE_REQUIRED` / wire < 2 | Report terminal blocker with repair command | Retry or use legacy startup | Error and required upgrade recorded |
| tmux/socket cold-start error | Retry once, then continue to worker liveness check | Loop retries | Second result recorded |
| SSH/timeout/remote command error | Retry once; classify auth/config/network | Hand-build a bypass | `TransportError` label or blocker |
| worker already alive | Use `mailbox ... --host <H>` manager-pull | Start another worker with SSH/tmux | Matching REPORT received |
| worker not provably alive | Report blocker and required remote repair | Pretend status.json proves liveness | Liveness evidence or blocker |

Gateway starts/supervises workers; mailbox transport can remain usable when an existing worker is alive. A gateway failure never authorizes manual remote `tmux new-session` or `send-keys`.

## Mailbox-only fallback

```bash
aimeshchat ssh warm <H>
aimeshchat mailbox session-init --session <sid> --manager manager --agents w1,w2 --host <H>
aimeshchat mailbox send --session <sid> --from manager --to w1 --kind TASK \
  --subject '...' --body '{"request_id":"req1","run_id":"r1"}' --host <H>
aimeshchat mailbox read --session <sid> --agent manager --owner manager --host <H> --json
```

Use this only when the worker is alive, status is `IDLE/DONE/BLOCKED`, no REPORT is pending, and manager-pull is reachable. Verify REPORT `request_id`, pull artifacts, verify size/sha256, then finalize. Missing event streams, receipts, park/revive, or runtime stop are explicit degradation costs.

## Remote worker state

| Evidence | Meaning |
|---|---|
| `mailbox stats --host <H>` | transport and inbox/processing/archive counts |
| remote `status.json` | availability snapshot only |
| manager inbox REPORT | task result candidate |
| inbox consumption plus archive/processing movement | evidence that worker is alive |

`status.json` existence alone is not liveness. IDLE requires `status.json.state == IDLE`, worker inbox empty, and manager inbox free of unprocessed REPORT. Use mailbox as the durable queue; split concurrent work across agent IDs rather than building a second queue.

## SSH retry policy

Before cross-host commands run `ssh -G <H>` and verify a configured alias. Network failures (DNS, refusal, timeout) get the CLI's bounded retry; auth/config failures fail immediately. Never manually pass remote `--timeout`; let the CLI choose its role-aware timeout and heartbeat.

## Remote worker safety

| DO | DON'T |
|---|---|
| Use mailbox two-phase `read → process → finalize`; use status/inbox/report evidence | Inject tasks with remote `tmux send-keys` or infer state with `capture-pane` |
| Dispatch only when IDLE/DONE/BLOCKED and no pending REPORT | Dispatch while BUSY |
| Use `request_id`/`run_id` and artifact sha256/size for terminal CAS | Treat status DONE or a file's presence as task completion |
| Retry one transient SSH failure and report blocker after exhaustion | Retry forever or hand-write status.json |
