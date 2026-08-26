# 启动路径 × 身份注入矩阵

> 审计基准：安装副本 v0.2.7（python3.13/site-packages）与仓内 HEAD v0.2.8。
> 身份文件必需字段：`session_id` + `agent_id|worker_id`（二选一命名）；
> `nonce` 条件式校验；`owner_pid` 用于存活/PID 复用检查与 sweep 清理判定。

| 启动路径 | 写身份文件 | 注入 OMP_MAILBOX_* env | 唤醒能力 | 说明 |
|---|---|---|---|---|
| `_run_sync`（默认前台） | ✅ OMPRunner._extra_env fallback | ✅ os.environ | 有 | 环境预设 `SWARM_SESSION_ID` 即激活 fallback |
| `_run_in_tmux`（--tmux/--split） | ✅（--mailbox-agent 时） | ✅ argv env 前缀经 tmux shell | 有 | 本次新增 |
| `_run_in_background`（--background） | ✅（--mailbox-agent 时） | ✅ Popen `env=` 显式传递 | 有 | 本次新增；禁止 K=V 进 argv（shell=False 会当可执行名） |
| gateway runtime.spawn | ✅ RunContext 分支原有 | ✅ | 有 | |
| oracle bootstrap | ✅ RunContext 分支原有（3600s timeout） | ✅ | 有 | |
| **手工 `omp` 启动（无任何注入）** | ❌ | ❌ | **无（聋哑节点）** | Interactive-Peer 场景用 `scripts/swarm_attach.py` 补身份 |

## --mailbox-agent 参数

- `--mailbox-agent <id>`：启用身份写入与注入（CODEAGENT_ROLE=worker）
- `--mailbox-session <sid>`：缺省取 `SWARM_SESSION_ID` env，再缺省自动生成
- `--mailbox-root <root>`：非默认 mailbox root

进程内幂等缓存：同一 args 多次调用复用同一身份文件（G7 修复）。

## 已知限制

- attach 身份无 nonce → 插件 nonce 校验条件式放行（设计使然）
- 无 owner_pid 的身份会被插件 sweep（10min 周期）清理 → 长会话在启动 omp 前重新 attach
- 目标库 schema 缺 identity_key 列时 restore 裸 OperationalError（无干净报错）
