# persist-oracle CLI contracts

## CLI commands

```bash
KEY='<project>:advisor:<domain>:<topic>[:<role_suffix>]'
aimeshchat oracle start "$KEY" --variant reasoning --system "..." --prompt '初始问题'
aimeshchat oracle ask "$KEY" '追加信息' [--wait-binding]
aimeshchat oracle status "$KEY"
aimeshchat oracle list
aimeshchat oracle wait "$KEY"
aimeshchat oracle result "$KEY" [--raw]
aimeshchat oracle revive "$KEY" [--mode bg|resume]
aimeshchat oracle attach "$KEY" '问题'
aimeshchat oracle release "$KEY" [--purge]
aimeshchat oracle doctor [--fix]
aimeshchat oracle gc [--dry-run] [--json]
```

`watch` is an event stream, not completion. `wait` runs without a finite deadline and completes on new output or `agent_end`.

## Receipt status

| Status | Meaning | Next action |
|---|---|---|
| `mailbox_persisted` | queue only; no backend turn proof | wait/result; do not call answered |
| `binding_pending` / `claimed` / `session_live` | delivery/session progressing | wait or use `--wait-binding` |
| `turn_triggered` | backend turn established | wait, then result |
| `failed_safe` | not triggered | resend unchanged |
| `ambiguous` | may have triggered | status/manual confirmation; never auto-resend |

## Freshness anchor

After each ask record `request_id`, `generation`, `last_ask_at`, and command state. `wait/result` accepts only output after the latest ask anchor; old transcript, mailbox REPORT, and session files are not valid current results. Notifications only wake the manager; they do not prove completion or freshness.

## Binding, fallback, and GC

`start` may return `binding=pending` while the runtime remains alive; use `--wait-binding` before asking. Hot reuses a live backend session, warm resumes a persisted session, and cold rebuilds with a snapshot; report each downgrade. `gc` removes expired released sessions and is throttled by the CLI. Keep sessions isolated by review key.

## Output safety

Do not pipe oracle commands into early-exit consumers such as `head`, `tail`, or `grep`; they can lose runtime/session identifiers or trigger SIGPIPE. Structured transforms such as JSON parsing or `jq` are safe.
