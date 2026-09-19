# Legacy migration reference

## Command migration

| Legacy | Replacement |
|---|---|
| `~/.claude/bin/codeagent-wrapper` | `aimeshchat run` |
| `--parallel` | multiple `run ... --background` plus `job status/wait` |
| `python3 code_route.py list` | `aimeshchat route list` |
| `python3 code_route.py where T` | `aimeshchat route where T` |
| `echo TASK \| python3 code_route.py route T --backend B` | `printf '%s\n' TASK \| aimeshchat route T --backend B` |

## Restricted paths

Use `aimeshchat run --skip-permissions` with the authorized `private-code-explore`, `private-code-develop`, or `private-code-oracle` profile only when the profile has been synced. If the profile is unavailable, report that prerequisite and use an explicitly authorized model; do not read a restricted path from an unauthorized role.

## Session and role rules

Auto-resume keys are host+workdir+backend+agent. Use `--new-session` for a changed objective, contaminated assumptions, sensitive isolation, or an independent experiment. Use `--session-key '<project>:<role>:<topic>'` when crossing that default boundary. Agent role precedence is user choice, explicit profile, then runtime default; unresolved mappings are errors, not guesses.

For a hand-started peer, source identity before launching OMP:

```bash
python3 scripts/swarm_attach.py <sid> --agent <id> --out /tmp/attach-<id>.sh
source /tmp/attach-<id>.sh && omp
```

Across OMP processes use `aimeshchat mailbox/swarm`; `hub` is same-process subagent IPC only.
