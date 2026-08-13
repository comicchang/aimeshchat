"""CLI facade — unified entry point for all aimeshchat commands."""
from __future__ import annotations

from codeagent.artifact import ArtifactDescriptor, pull_artifact, verify_artifact, validate_descriptor
import argparse
import dataclasses

from codeagent import __version__
from codeagent.domain import RepoMap
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional
from uuid import uuid4

from codeagent.config.repo_map import load_repo_map
from codeagent.domain import (
    HostSpec,
    RunContext,
    RunRequest,
    RunResult,
    Target,
    resolve_is_local,
)
from codeagent.routing.resolver import resolve_target
from codeagent.session.registry import SessionRegistry
from codeagent.transport.base import TransportError
from codeagent.transport.local import LocalTransport
from codeagent.transport.router import TransportRouter
from codeagent.transport.ssh import SSHTransport
from codeagent.swarm.kernel import SwarmKernel, LocalDeliverySink
from codeagent.swarm.delivery import DeliveryEngine
from codeagent.swarm.model import Address, AddressKind, AgentLocation, Envelope, ExecutionMode, ReturnMode
from codeagent.mailbox.store import MailboxStore, resolve_root

log = logging.getLogger(__name__)

_router = TransportRouter()


def _get_transport(host: HostSpec, repo_map=None):
    """Select transport based on host.transport field."""
    return _router.get(host, repo_map)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser("aimeshchat", description="Multi-host code agent orchestration")
    p.add_argument("--version", "-v", action="version", version=f"%(prog)s {__version__}")

    sub = p.add_subparsers(dest="command")

    # run
    run_p = sub.add_parser("run", help="Execute a task")
    run_p.add_argument("task", nargs="?", help="Task text (or stdin)")
    run_p.add_argument("workdir", nargs="?", default="", help="Working directory")
    run_p.add_argument("--host", help="Execute on remote host")
    run_p.add_argument("--backend", default="omp")
    run_p.add_argument("--agent")
    run_p.add_argument("--model")
    run_p.add_argument("--skills")
    run_p.add_argument("--session-key", help="Explicit session namespace")
    run_p.add_argument("--new-session", action="store_true")
    run_p.add_argument("--no-auto-resume", action="store_true")
    run_p.add_argument("--skip-permissions", action="store_true", default=False)
    run_p.add_argument("--output", help="Write structured JSON to file")
    # Execution mode flags (mutually exclusive, default = synchronous foreground)
    run_mode = run_p.add_mutually_exclusive_group()
    run_mode.add_argument(
        "--tmux", action="store_true", default=False,
        help="Spawn in the current tmux session (new window/pane, visible & interactive)",
    )
    run_mode.add_argument(
        "--split", action="store_true", default=False,
        help="Like --tmux but splits the current pane instead of a new window",
    )
    run_mode.add_argument(
        "--background", action="store_true", default=False,
        help="Run in a background thread (non-blocking; poll with 'job status <id>')",
    )
    # Hidden flag: child process writes result to this job dir.
    run_p.add_argument("--_bg-job-id", dest="_bg_job_id", default=None, help=argparse.SUPPRESS)
    # job subcommand
    job_p = sub.add_parser("job", help="Manage background jobs")
    job_sub = job_p.add_subparsers(dest="job_cmd")
    job_sub.add_parser("list", help="List recent jobs")
    status_p = job_sub.add_parser("status", help="Show job status")
    status_p.add_argument("job_id", help="Job ID to query")
    wait_p = job_sub.add_parser("wait", help="Wait for job completion")
    wait_p.add_argument("job_id", help="Job ID to wait on")
    wait_p.add_argument("--timeout", type=float, default=0, help="Seconds to wait (0 = forever)")

    # route
    route_p = sub.add_parser("route", help="Route task via repo-map")
    route_p.add_argument("args", nargs="*", help="list | where <topic> | <topic> [task...]")
    route_p.add_argument("--repo", type=int, default=0)
    route_p.add_argument("--backend", default="omp")
    route_p.add_argument("--agent")
    route_p.add_argument("--model")
    route_p.add_argument("--raw", action="store_true")
    route_p.add_argument("--json", action="store_true", dest="json_output")
    route_p.add_argument("--dry-run", action="store_true")
    route_p.add_argument("--new-session", action="store_true")
    route_p.add_argument("--no-auto-resume", action="store_true")
    route_p.add_argument("--skip-permissions", action="store_true", default=False)
    route_p.add_argument("--skills")
    route_p.add_argument("--session-key")
    route_p.add_argument("--output")

    # sessions
    sess_p = sub.add_parser("sessions", help="Manage session registry")
    sess_sub = sess_p.add_subparsers(dest="sess_cmd")
    ls_p = sess_sub.add_parser("list")
    ls_p.add_argument("--host")
    ls_p.add_argument("--topic")
    show_p = sess_sub.add_parser("show")
    show_p.add_argument("key")
    reset_p = sess_sub.add_parser("reset")
    reset_p.add_argument("key")
    bind_p = sess_sub.add_parser("bind")
    bind_p.add_argument("--key", required=True)
    bind_p.add_argument("--id", required=True, dest="session_id")

    # session (storage lifecycle — A6)
    ssl_p = sub.add_parser("session", help="Session storage lifecycle")
    ssl_sub = ssl_p.add_subparsers(dest="session_cmd")
    ssl_clean = ssl_sub.add_parser("clean", help="Delete whole sessions older than N days "
                                                 "(history/archive/events/outbox)")
    ssl_clean.add_argument("--older-than", type=int, required=True,
                           help="Delete sessions older than this many days")
    ssl_clean.add_argument("--json", action="store_true", dest="json_output",
                           help="JSON output")

    # ssh
    ssh_p = sub.add_parser("ssh", help="Manage SSH connections")
    ssh_sub = ssh_p.add_subparsers(dest="ssh_cmd")
    warm_p = ssh_sub.add_parser("warm")
    warm_p.add_argument("hosts", nargs="*")
    ssh_sub.add_parser("status")
    stop_p = ssh_sub.add_parser("stop")
    stop_p.add_argument("hosts", nargs="*")

    # mailbox
    mbox_p = sub.add_parser("mailbox", help="Cross-host mailbox operations")
    mbox_p.add_argument("mailbox_args", nargs=argparse.REMAINDER, help="Arguments passed to mailbox CLI")
    mbox_p.add_argument("--host", help="Target host (omit for local)")
    mbox_p.add_argument("--mailbox-root", help="Override MAILBOX_ROOT")

    art_p = sub.add_parser("artifact", help="Pull and verify remote artifacts")
    art_sub = art_p.add_subparsers(dest="art_cmd")

    pull_p = art_sub.add_parser("pull", help="Pull an artifact from a remote host")
    pull_p.add_argument("--host", required=True, help="SSH alias for remote host")
    pull_p.add_argument("--artifact-id", required=True, help="Artifact identifier")
    pull_p.add_argument("--remote-root", default="/tmp/aimeshchat-artifacts", help="Remote artifact root directory")
    pull_p.add_argument("--relative-path", required=True, help="Relative path within remote root")
    pull_p.add_argument("--size", type=int, required=True, help="Expected file size in bytes")
    pull_p.add_argument("--sha256", required=True, help="Expected SHA-256 hex digest")
    pull_p.add_argument("--media-type", default="application/octet-stream", help="MIME media type")
    pull_p.add_argument("--dest", required=True, help="Local destination file path")

    verify_p = art_sub.add_parser("verify", help="Verify a local artifact")
    verify_p.add_argument("--file", required=True, help="Path to local file")
    verify_p.add_argument("--sha256", required=True, help="Expected SHA-256 hex digest")
    verify_p.add_argument("--size", type=int, required=True, help="Expected file size in bytes")

    # ── swarm ───────────────────────────────────────────────────────────
    _build_swarm_parser(sub)

    # ── park ────────────────────────────────────────────────────────────
    park_p = sub.add_parser("park", help="Manage park instances (Hot→Warm→Cold revive)")
    park_sub = park_p.add_subparsers(dest="park_cmd")

    park_list_p = park_sub.add_parser("list", help="List park instances")
    park_list_p.add_argument("--lifecycle", help="Filter by lifecycle (hot_parked/cold_resumable/released)")
    park_list_p.add_argument("--all", action="store_true", help="Show all non-released instances (not just hot_parked)")

    park_info_p = park_sub.add_parser("info", help="Show park instance details")
    park_info_p.add_argument("review_key", help="Review key")

    park_revive_p = park_sub.add_parser("revive", help="Revive or spawn a park instance")
    park_revive_p.add_argument("review_key", help="Review key")
    park_revive_p.add_argument("--prompt", help="Incremental prompt for the revived instance")

    park_release_p = park_sub.add_parser("release", help="Release a park instance")
    park_release_p.add_argument("review_key", help="Review key")
    park_release_p.add_argument("--agent-type", help="Agent type (oracle/oracle-lite/etc)")
    park_release_p.add_argument("--peer-id", help="OMP peer agent ID")
    park_release_p.add_argument("--mailbox-id", help="Mailbox agent ID")
    park_release_p.add_argument("--backend-id", help="Backend session ID")

    park_acquire_p = park_sub.add_parser("acquire", help="Acquire a park instance")
    park_acquire_p.add_argument("review_key", help="Review key")
    park_acquire_p.add_argument("--agent-type", default="oracle", help="Agent type (oracle/oracle-lite/etc)")
    park_acquire_p.add_argument("--peer-id", default="", help="OMP peer agent ID")
    park_acquire_p.add_argument("--mailbox-id", default="", help="Mailbox agent ID")
    park_acquire_p.add_argument("--backend-id", default="", help="Backend session ID")

    park_renew_p = park_sub.add_parser("renew", help="Renew a park instance (update TTL)")
    park_renew_p.add_argument("review_key", help="Review key")

    park_sweep_p = park_sub.add_parser("sweep", help="Evict expired park instances")
    park_sweep_p.add_argument("--dry-run", action="store_true", help="Preview without evicting")

    # ── gateway ─────────────────────────────────────────────────────────
    gw_p = sub.add_parser("gateway", help="Per-device local control plane")
    gw_sub = gw_p.add_subparsers(dest="gw_cmd")
    gw_start = gw_sub.add_parser("start", help="Start the local gateway (idempotent)")
    gw_ensure = gw_sub.add_parser("ensure", help="Verify + start a remote host's gateway")
    gw_ensure.add_argument("--host", required=True, help="SSH alias for the remote host")
    gw_status = gw_sub.add_parser("status", help="Show local gateway status")
    gw_stop = gw_sub.add_parser("stop", help="Stop the local gateway")
    gw_serve = gw_sub.add_parser("serve", help="Foreground gateway process (tmux pane command)")
    gw_rpc = gw_sub.add_parser("rpc", help="Bounded RPC to the local gateway")
    gw_rpc.add_argument("--stdio", action="store_true", help="Read one request from stdin, write one response")
    gw_rpc.add_argument("method", nargs="?", default="", help="Gateway method (when not --stdio)")
    gw_rpc.add_argument("--params", default="", help="JSON params dict (when not --stdio)")
    gw_rpc.add_argument("--timeout", type=float, default=15.0)
    gw_health = gw_sub.add_parser("health", help="P5: gateway health check / watch")
    gw_health.add_argument("--watch", action="store_true", help="Continuous monitoring mode")
    gw_health.add_argument("--interval", type=float, default=5.0, help="Poll interval in seconds (--watch)")

    # ── events ──────────────────────────────────────────────────────────
    ev_p = sub.add_parser("events", help="Runtime event observability")
    ev_sub = ev_p.add_subparsers(dest="ev_cmd")
    ev_watch = ev_sub.add_parser("watch", help="Watch runtime events (cursor-resumable)")
    ev_watch.add_argument("--session", default="", help="Filter by session_id")
    ev_watch.add_argument("--request-id", default="", help="Filter by request_id")
    ev_watch.add_argument("--runtime-id", default="", help="Filter by runtime_id")
    ev_watch.add_argument("--cursor", default=None,
                          help="Resume cursor (local event_id); defaults to the persisted "
                               "watch cursor so reconnects resume automatically")
    ev_watch.add_argument("--filters", default="", help="Comma-separated event kinds")
    ev_watch.add_argument("--limit", type=int, default=200)
    ev_watch.add_argument("--interval", type=float, default=1.0)
    ev_watch.add_argument("--timeout", type=float, default=10.0,
                          help="Observation connection timeout (never the task timeout)")
    ev_watch.add_argument("--exit-on", default="",
                          help="A4: comma-separated terminal specs KIND.STATE (e.g. "
                               "TASK_STATE.agent_end,RUNTIME_STATE.stopped); exit 0 on the first match")
    ev_watch.add_argument("--max-events", type=int, default=0,
                          help="A4: stop after this many events (0 = unlimited)")
    ev_watch.add_argument("--duration", type=float, default=0.0,
                          help="A4: stop after this many seconds (0 = unlimited)")
    ev_watch.add_argument("--jsonl", action="store_true", dest="jsonl", help="Emit one JSON event per line")
    ev_watch.add_argument("--plain", action="store_true", dest="plain",
                          help="Human-readable lines (default; mutually exclusive with --jsonl)")

    # ── runtime ─────────────────────────────────────────────────────────
    rt_p = sub.add_parser("runtime", help="Runtime supervision")
    rt_sub = rt_p.add_subparsers(dest="rt_cmd")
    rt_status = rt_sub.add_parser("status", help="Probe a runtime")
    rt_status.add_argument("runtime_id", help="Runtime ID")
    rt_stop = rt_sub.add_parser("stop", help="Stop a runtime")
    rt_stop.add_argument("runtime_id", help="Runtime ID")
    rt_stop.add_argument("--reason", default="stopped")

    # ── oracle ──────────────────────────────────────────────────────────
    ora_p = sub.add_parser("oracle", help="Persistent-context advisory sessions")
    ora_sub = ora_p.add_subparsers(dest="ora_cmd")

    ora_start = ora_sub.add_parser("start", help="Create review/session/runtime（含 A1 绑定窗口）")
    ora_start.add_argument("review_key", help="Review key (e.g. project:oracle:domain:topic)")
    ora_start.add_argument("--agent", default="", metavar="PROFILE",
                           help="DEPRECATED（弃用）: 兼容占位参数，无模型语义——模型/提示词策略已归 skill，"
                                "请用 --model/--variant 显式指定；此参数不再参与模型解析"
                                "（传了会打弃用告警）")
    ora_start.add_argument("--backend", default="omp", help="Runtime backend (omp|opencode)")
    ora_start.add_argument("--workdir", default="", help="Working directory")
    ora_start.add_argument("--model", default="", help="B1: 显式指定模型（推荐；无 --model 时走 runtime.context → execution-context 继承）")
    ora_start.add_argument("--model-strict", action="store_true", default=False, dest="model_strict",
                           help="P0: 无 --model 且 runtime.context 查询失败时直接报 MODEL_CONTEXT_UNAVAILABLE "
                                "(默认回退 execution-context，不 fatal)")
    ora_start.add_argument("--variant", default="", help="Q5: model variant (e.g. reasoning/thinking; default empty)")
    ora_start.add_argument("--system", default="", help="Q5: system prompt (prepended to prompt; default empty)")
    ora_start.add_argument("--prompt", default="", help="Initial prompt (default empty — first TASK comes via ask)（初始 prompt；oracle 慢启动时 backend session 绑定需 ≤60s，绑定超时返回 binding=pending 不杀 runtime）")
    ora_start.add_argument("--apply-memory-config", action="store_true", dest="apply_memory_config",
                           help="D4: auto-merge missing OMP memory config keys (default: detect + warn only)")

    ora_ask = ora_sub.add_parser("ask", help="Hot/warm/cold deliver a prompt to the review")
    ora_ask.add_argument("review_key")
    # U1: prompt 必填（去掉 nargs='?'）——不再 fallback 读 stdin，避免
    # 忘记 prompt 时终端挂起（非交互 stdin 会一直阻塞）。
    ora_ask.add_argument("prompt", help="Prompt text (required — no stdin fallback)")
    ora_ask.add_argument("--agent", default="", metavar="PROFILE",
                         help="DEPRECATED（弃用）: 兼容占位参数，无模型语义——请用 --model/--variant 显式指定（传了会打弃用告警）")
    ora_ask.add_argument("--backend", default="omp")
    ora_ask.add_argument("--model", default="",
                         help="显式指定模型（推荐；覆盖 manifest 已落盘模型）")
    ora_ask.add_argument("--wait-binding", action="store_true", default=False,
                        help="Block until backend session binding completes (≤60s) before "
                             "steering; avoids the binding_pending silent-drop window")
    # A15: 投递成功后阻塞等新产出内联返回（复用 wait 的 baseline 过滤逻辑）
    ora_ask.add_argument("--wait", action="store_true", default=False,
                        help="投递成功后阻塞等新产出内联返回")

    ora_status = ora_sub.add_parser("status", help="Aggregate receipt/progress/park")
    ora_status.add_argument("review_key")

    # E2: list all parked oracle reviews (ParkRegistry.list_active + lifecycle)
    ora_list = ora_sub.add_parser("list", help="List all parked oracle reviews (active + lifecycle)")

    ora_watch = ora_sub.add_parser("watch", help="Watch runtime events (cursor-resumable)")
    ora_watch.add_argument("review_key")
    ora_watch.add_argument("--cursor", default=None,
                          help="Resume cursor; defaults to the persisted watch cursor")
    ora_watch.add_argument("--interval", type=float, default=1.0)
    ora_watch.add_argument("--timeout", type=float, default=10.0)
    ora_watch.add_argument("--exit-on", default="",
                          help="A4: comma-separated terminal specs KIND.STATE or kind-only "
                               "KIND (e.g. TASK_STATE.agent_end, ASSISTANT_PROGRESS exits on "
                               "any new output); exit 0 on the first match")
    ora_watch.add_argument("--max-events", type=int, default=0,
                          help="A4: stop after this many events (0 = unlimited)")
    ora_watch.add_argument("--duration", type=float, default=0.0,
                          help="A4: stop after this many seconds (0 = unlimited)")
    ora_watch.add_argument("--jsonl", action="store_true", dest="jsonl",
                           help="Emit one JSON event per line (default: human-readable)")
    ora_watch.add_argument("--plain", action="store_true", dest="plain",
                           help="Human-readable lines (default)")

    ora_wait = ora_sub.add_parser("wait", help="A3/B1: block until NEW assistant output (or agent_end), print final text")
    ora_wait.add_argument("review_key")
    ora_wait.add_argument("--timeout", type=float, default=300.0,
                          help="B1: max seconds to wait (default 300); on timeout emits "
                               "{status: timeout, suggestion: use oracle result}")
    ora_wait.add_argument("--interval", type=float, default=5.0,
                          help="B1: poll interval in seconds (default 5)")
    ora_wait.add_argument("--all", action="store_true", default=False,
                          help="P0-2: skip result truncation; print full answer without MAX_OUTPUT_BYTES cap")
    ora_wait.add_argument("--auto-recover", action="store_true", default=False,
                          help="P1-1: on stuck signal, auto release+revive then retry")

    ora_release = ora_sub.add_parser("release", help="Terminal state + release park + stop runtime")
    ora_release.add_argument("review_key")
    ora_release.add_argument("--purge", action="store_true",
                             help="P1-1: hard destroy — delete OMP session files + park row "
                                  "(default: soft release, revivable)")
    ora_release.add_argument("--force", action="store_true",
                             help="B2: skip the unread-REPORT confirmation before release")
    ora_release.add_argument("--keep-advisor", action="store_true", dest="keep_advisor",
                             help="P2-2: preserve advisor session files for debugging "
                                  "(a digest is always saved before deletion)")

    ora_revive = ora_sub.add_parser("revive", help="Revive a released/cold park instance")
    ora_revive.add_argument("review_key")
    ora_revive.add_argument("--mode", default="bg", choices=["bg", "pane", "resume"],
                            help="P1-2: revive mode — bg (default, supervised) | pane (tmux) "
                                 "| resume (omp --resume attach)")

    ora_result = ora_sub.add_parser("result", help="Print the oracle's latest response (from session transcript)")
    ora_result.add_argument("review_key")
    ora_result.add_argument("--all", action="store_true", help="Print all assistant messages")
    ora_result.add_argument("--strict", action="store_true",
                            help="改进项5: fail instead of degrading to the best-effort "
                                 "session-file scan when backend_session_id is missing/mismatched")
    ora_result.add_argument("--raw", action="store_true",
                            help="P0: print only the last assistant message as plain text "
                                 "(default: JSON with source/confidence/messages/meta)")
    ora_result.add_argument("--include-digest", action="store_true", dest="include_digest",
                            help="P2-2: also return the advisor digest (if available) "
                                 "in the result JSON")

    # P2: unified attach entry — hot send (HOT_PARKED) or revive (released/cold)
    ora_attach = ora_sub.add_parser("attach", help="Attach to an existing oracle session (hot send or revive)")
    ora_attach.add_argument("review_key")
    ora_attach.add_argument("prompt", nargs="?", default="", help="Prompt text (required for HOT_PARKED hot send; or stdin)")
    ora_attach.add_argument("--mode", default="bg", choices=["bg", "pane", "resume"],
                            help="P2: attach mode — bg (default, supervised) | pane (tmux) "
                                 "| resume (omp --resume attach); forwarded to revive when applicable")

    # P2-4: three-source consistency check (gateway ↔ park ↔ opencode.db)
    ora_doctor = ora_sub.add_parser("doctor", help="P2-4: three-source consistency check")
    ora_doctor.add_argument("--fix", action="store_true", default=False,
                            help="Attempt conservative fixes")

    return p


def _build_swarm_parser(sub: argparse._SubParsersAction) -> None:
    """Register the ``swarm`` subcommand tree."""
    swarm_p = sub.add_parser("swarm", help="Swarm kernel operations")
    swarm_sub = swarm_p.add_subparsers(dest="swarm_cmd")

    # create-session
    cs_p = swarm_sub.add_parser("create-session", help="Create a new swarm session")
    cs_p.add_argument("session_id", help="Session identifier")
    cs_p.add_argument("--manager", required=True, help="Manager agent ID")
    cs_p.add_argument("--members", required=True, help="Comma-separated member agent IDs")
    cs_p.add_argument("--policy", default="open", choices=["open", "restricted"],
                      help="B4: ACL policy (default open; restricted = authority-only broadcast)")
    cs_p.add_argument("--allowed-senders", default="",
                      help="B4: comma-separated allowed senders for restricted sessions "
                           "(must be roster subset; manager always included)")

    # register
    reg_p = swarm_sub.add_parser("register", help="Register an agent in the routing table")
    reg_p.add_argument("session_id")
    reg_p.add_argument("--agent", required=True)
    reg_p.add_argument("--host", required=True, help="Host alias (or __local__)")
    reg_p.add_argument("--backend", default="cli", choices=["cli", "omp", "tmux"])
    reg_p.add_argument("--card", default="",
                       help="P2: agent_card JSON {display_name,description,agent_version,capabilities[]}")
    reg_p.add_argument("--execution-mode", default=None,
                       choices=["mailbox-worker", "local-omp-mcp"],
                       help="How this agent is executed")
    reg_p.add_argument("--return-mode", default=None,
                       choices=["manager-pull", "bidirectional"],
                       help="How results flow back from worker to manager")
    reg_p.add_argument("--mailbox-root", default="",
                       help="Root path for agent mailbox storage")

    # whoami
    who_p = swarm_sub.add_parser("whoami", help="Show this agent's identity + agent card")
    who_p.add_argument("session_id")
    who_p.add_argument("--agent", required=True)

    # direct
    dir_p = swarm_sub.add_parser("direct", help="Send a direct message")
    dir_p.add_argument("session_id")
    dir_p.add_argument("--to", required=True, help="Recipient agent ID")
    dir_p.add_argument("--from", dest="sender", required=True, help="Sender agent ID")
    dir_p.add_argument("--kind", default="TASK")
    dir_p.add_argument("--subject", required=True)
    dir_p.add_argument("--body", required=True)
    dir_p.add_argument("--attachment", action="append", default=[], help="Attachment JSON (repeatable)")
    dir_p.add_argument("--run-id", default="", help="Run ID for request tracking")
    dir_p.add_argument("--request-id", default="", help="Request ID for causation chain")
    dir_p.add_argument("--reply-to", default="", help="Message ID being replied to")
    dir_p.add_argument(
        "--require-ack", action="store_true", default=False,
        help="v2: demand a RECEIPT(READ) from the recipient when consumed",
    )

    # channel
    ch_p = swarm_sub.add_parser("channel", help="Send to a channel")
    ch_p.add_argument("session_id")
    ch_p.add_argument("channel_id", help="Channel identifier")
    ch_p.add_argument("--from", dest="sender", required=True, help="Sender agent ID")
    ch_p.add_argument("--kind", default="TASK")
    ch_p.add_argument("--subject", default="")
    ch_p.add_argument("--body", required=True)
    ch_p.add_argument("--attachment", action="append", default=[])
    ch_p.add_argument("--run-id", default="", help="Run ID for request tracking")
    ch_p.add_argument("--request-id", default="", help="Request ID for causation chain")
    ch_p.add_argument("--reply-to", default="", help="Message ID being replied to")
    ch_p.add_argument(
        "--require-ack", action="store_true", default=False,
        help="v2: demand a RECEIPT(READ) from each recipient when consumed",
    )

    # create-channel
    cc_p = swarm_sub.add_parser("create-channel", help="Create a named channel within a session")
    cc_p.add_argument("session_id")
    cc_p.add_argument("channel_id", help="Channel identifier")
    cc_p.add_argument("--members", required=True, help="Comma-separated channel member agent IDs")

    # broadcast
    bc_p = swarm_sub.add_parser("broadcast", help="Broadcast to all session members")
    bc_p.add_argument("session_id")
    bc_p.add_argument("--from", dest="sender", required=True, help="Sender (must be authority)")
    bc_p.add_argument("--kind", default="NOTICE")
    bc_p.add_argument("--subject", default="")
    bc_p.add_argument("--body", required=True)
    bc_p.add_argument("--run-id", default="", help="Run ID for request tracking")
    bc_p.add_argument("--request-id", default="", help="Request ID for causation chain")
    bc_p.add_argument("--reply-to", default="", help="Message ID being replied to")
    bc_p.add_argument(
        "--require-ack", action="store_true", default=False,
        help="v2: demand a RECEIPT(READ) from each recipient when consumed",
    )

    # notice
    nt_p = swarm_sub.add_parser("notice", help="Send a notice to the session")
    nt_p.add_argument("session_id")
    nt_p.add_argument("--from", dest="sender", required=True)
    nt_p.add_argument("--topic", required=True)
    nt_p.add_argument("--audience", default="", help="Audience (reserved for future use)")
    nt_p.add_argument("--body", required=True)
    nt_p.add_argument("--ttl", type=int, default=0)
    nt_p.add_argument("--kind", default="NOTICE")
    nt_p.add_argument("--subject", required=True)
    nt_p.add_argument("--run-id", default="", help="Run ID for request tracking")
    nt_p.add_argument("--request-id", default="", help="Request ID for causation chain")
    nt_p.add_argument("--reply-to", default="", help="Message ID being replied to")
    nt_p.add_argument(
        "--require-ack", action="store_true", default=False,
        help="v2: demand a RECEIPT(READ) from each recipient when consumed",
    )

    # poll
    pl_p = swarm_sub.add_parser("poll", help="Poll agent inbox")
    pl_p.add_argument("session_id")
    pl_p.add_argument("--agent", required=True)
    pl_p.add_argument("--cursor", default="")
    pl_p.add_argument("--limit", type=int, default=50)

    # ack
    ack_p = swarm_sub.add_parser("ack", help="Acknowledge a message")
    ack_p.add_argument("session_id")
    ack_p.add_argument("--agent", required=True)
    ack_p.add_argument("--msg-id", required=True)
    ack_p.add_argument("--phase", default="consumed", choices=["consumed", "released"])

    # status
    st_p = swarm_sub.add_parser("status", help="Show session status")
    st_p.add_argument("session_id")
    st_p.add_argument("--trace", default="", help="Top4: 按 trace_id 聚合跨主机消息链")

    # watch
    wt_p = swarm_sub.add_parser("watch", help="Watch agent inbox (poll loop)")
    wt_p.add_argument("session_id")
    wt_p.add_argument("--agent", required=True)
    wt_p.add_argument("--interval", type=int, default=5, help="Poll interval in seconds")
    wt_p.add_argument("--iterations", type=int, default=0, help="Max iterations (0 = infinite)")

    # outbox
    ob_p = swarm_sub.add_parser("outbox", help="Outbox management")
    ob_sub = ob_p.add_subparsers(dest="outbox_cmd")

    ob_pending = ob_sub.add_parser("pending", help="List undelivered envelopes")
    ob_pending.add_argument("--session", dest="session_id", help="Filter by session ID")
    ob_pending.add_argument("--json", action="store_true", dest="json_output", help="JSON output")

    ob_flush = ob_sub.add_parser("flush", help="Retry all pending envelopes")
    ob_flush.add_argument("--session", dest="session_id", help="Filter by session ID")

    ob_status = ob_sub.add_parser("status", help="Show outbox summary counts")
    ob_status.add_argument("--session", dest="session_id", help="Filter by session ID")

    # Top3 dead-letter management
    ob_dead = ob_sub.add_parser("dead", help="List dead-lettered envelopes")
    ob_dead.add_argument("--session", dest="session_id", help="Filter by session ID")

    ob_requeue = ob_sub.add_parser("requeue", help="Move a dead-lettered entry back to pending")
    ob_requeue.add_argument("msg_id", help="Message ID to requeue")
    ob_requeue.add_argument("--session", required=True, dest="session_id", help="Session ID")

    ob_purge = ob_sub.add_parser("purge", help="Delete dead-lettered entries")
    ob_purge.add_argument("--session", dest="session_id", help="Filter by session ID")

    # launch
    la_p = swarm_sub.add_parser("launch", help="Bootstrap remote workers and start manager-pull loop")
    la_p.add_argument("session_id", help="Session identifier")
    la_p.add_argument("--bootstrap", action="store_true",
                      help="Send session-init + INIT task to each remote worker")
    la_p.add_argument("--pull", action="store_true",
                      help="Start manager-pull loop after bootstrap")
    la_p.add_argument("--poll-interval", type=int, default=5,
                      help="Pull loop interval in seconds (default 5)")
    la_p.add_argument("--max-iterations", type=int, default=0,
                      help="Max pull iterations (0 = until all workers terminal)")


def _get_swarm_kernel(store_root: Optional[Path] = None) -> tuple[SwarmKernel, MailboxStore]:
    """Create a SwarmKernel with DeliveryEngine sink for cross-host delivery.

    When a transport router is available, uses DeliveryEngine as the sink
    so that messages to remote targets are delivered via transport (SSH/relay)
    with durable outbox write + retry.  Falls back to LocalDeliverySink for
    pure-local usage.
    """
    from codeagent.swarm.delivery import DeliveryEngine, EngineDeliverySink

    store = MailboxStore(root=store_root)
    router = _router

    engine = DeliveryEngine(mailbox_store=store, transport_router=router)
    sink = EngineDeliverySink(engine)
    kernel = SwarmKernel(store=store, sink=sink)
    sink.set_kernel(kernel)

    # Opportunistic flush: retry pending outbox entries on startup.
    # Failures are logged but don't prevent kernel creation.
    try:
        flushed = engine.flush()
        if flushed:
            log.info("_get_swarm_kernel: flushed %d pending outbox entries on startup", flushed)
    except Exception as exc:
        log.debug("_get_swarm_kernel: opportunistic flush failed: %s", exc)

    return kernel, store


def _parse_attachments(raw: list[str]) -> list:
    """Parse --attachment JSON strings into AttachmentRef objects."""
    from codeagent.mailbox.protocol import AttachmentRef
    refs = []
    for item in raw:
        d = json.loads(item)
        refs.append(AttachmentRef.from_dict(d))
    return refs


def _cmd_swarm(args: argparse.Namespace) -> int:
    """Dispatch swarm subcommands."""
    cmd = args.swarm_cmd
    if cmd is None:
        print("error: specify a swarm subcommand", file=sys.stderr)
        return 1

    kernel, store = _get_swarm_kernel()

    try:
        if cmd == "create-session":
            return _swarm_create_session(kernel, args)
        elif cmd == "register":
            return _swarm_register(kernel, args)
        elif cmd == "whoami":
            return _swarm_whoami(kernel, args)
        elif cmd == "direct":
            return _swarm_direct(kernel, args)
        elif cmd == "channel":
            return _swarm_channel(kernel, args)
        elif cmd == "create-channel":
            return _swarm_create_channel(kernel, args)
        elif cmd == "broadcast":
            return _swarm_broadcast(kernel, args)
        elif cmd == "notice":
            return _swarm_notice(kernel, args)
        elif cmd == "poll":
            return _swarm_poll(kernel, args)
        elif cmd == "ack":
            return _swarm_ack(kernel, args)
        elif cmd == "status":
            return _swarm_status(kernel, args)
        elif cmd == "watch":
            return _swarm_watch(kernel, args)
        elif cmd == "outbox":
            return _swarm_outbox(kernel, args)
        elif cmd == "launch":
            return _swarm_launch(kernel, args)
        else:
            print(f"error: unknown swarm command: {cmd}", file=sys.stderr)
            return 1
    except (ValueError, PermissionError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _swarm_create_session(kernel: SwarmKernel, args: argparse.Namespace) -> int:
    members = [m.strip() for m in args.members.split(",") if m.strip()]
    acl = None
    if args.policy != "open":
        allowed = [a.strip() for a in (args.allowed_senders or "").split(",") if a.strip()]
        # P2 (oracle-lite): allowed_senders 必须是 roster 子集（early fail）
        valid = set(members) | {args.manager}
        invalid = [a for a in allowed if a not in valid]
        if invalid:
            print(f"error: --allowed-senders 含非 roster 成员: {invalid}", file=sys.stderr)
            return 1
        from codeagent.swarm.model import ACL
        acl = ACL(
            authority=args.manager,
            allowed_senders=list(allowed) or [args.manager],
            room_members=sorted(set(members) | {args.manager}),
            policy=args.policy,
        )
    session = kernel.create_session(args.session_id, args.manager, members, acl=acl)
    print(json.dumps({
        "session_id": session.session_id,
        "manager_id": session.manager_id,
        "roster": list(session.roster),
        "acl": {"authority": session.acl.authority, "policy": session.acl.policy},
    }, indent=2))
    return 0


def _swarm_register(kernel: SwarmKernel, args: argparse.Namespace) -> int:
    exec_mode = ExecutionMode(args.execution_mode) if args.execution_mode else None
    ret_mode = ReturnMode(args.return_mode) if args.return_mode else None
    loc = AgentLocation(
        agent_id=args.agent,
        host_alias=args.host,
        backend=args.backend,
        execution_mode=exec_mode,
        return_mode=ret_mode,
        mailbox_root=args.mailbox_root,
    )
    reg = kernel.register(loc, args.session_id)
    if args.card:
        try:
            card = json.loads(args.card)
        except json.JSONDecodeError as exc:
            print(f"error: invalid --card JSON: {exc}", file=sys.stderr)
            return 1
        kernel.set_agent_card(args.session_id, args.agent, card)
    out: dict = {
        "agent_id": reg.agent_id,
        "session_id": reg.session_id,
        "host_alias": reg.location.host_alias,
        "backend": reg.location.backend,
    }
    if exec_mode:
        out["execution_mode"] = exec_mode.value
    if ret_mode:
        out["return_mode"] = ret_mode.value
    if args.mailbox_root:
        out["mailbox_root"] = args.mailbox_root
    print(json.dumps(out, indent=2))
    return 0


def _swarm_whoami(kernel: SwarmKernel, args: argparse.Namespace) -> int:
    """P2: 本机 agent 身份 + agent card（纯 advertisement，不授予权限）。"""
    import socket as _socket
    loc = kernel.get_location(args.session_id, args.agent)
    cards = kernel.get_agent_cards(args.session_id)
    print(json.dumps({
        "agent_id": args.agent,
        "hostname": _socket.gethostname(),
        "host_alias": loc.host_alias if loc else "",
        "backend": loc.backend if loc else "",
        "agent_card": cards.get(args.agent, {}),
        # transport_capabilities: 硬编码 transport 层能力（与 agent_card 的
        # 用户自定义 capabilities 字段区分——oracle-lite P2）
        "transport_capabilities": sorted({"mailbox", "stream", "artifact"}),
    }, indent=2))
    return 0


def _swarm_create_channel(kernel: SwarmKernel, args: argparse.Namespace) -> int:
    members = [m.strip() for m in args.members.split(",") if m.strip()]
    ch = kernel.create_channel(args.session_id, args.channel_id, members)
    print(json.dumps({
        "channel_id": ch.channel_id,
        "members": list(ch.members),
    }, indent=2))
    return 0


def _require_kind_correlation(kind: str, run_id: str, request_id: str, reply_to: str) -> int:
    """Validate correlation fields for TASK/REPORT kinds.

    Returns 0 on success, prints error and returns 1 on failure.
    """
    if kind in ("TASK", "INIT") and not run_id:
        print(f"error: --run-id is required for kind={kind}", file=sys.stderr)
        return 1
    if kind in ("TASK", "INIT") and not request_id:
        print(f"error: --request-id is required for kind={kind}", file=sys.stderr)
        return 1
    if kind == "REPORT" and not reply_to:
        print("error: --reply-to is required for kind=REPORT", file=sys.stderr)
        return 1
    if kind == "REPORT" and not run_id:
        print("error: --run-id is required for kind=REPORT", file=sys.stderr)
        return 1
    if kind == "REPORT" and not request_id:
        print("error: --request-id is required for kind=REPORT", file=sys.stderr)
        return 1
    return 0


def _swarm_direct(kernel: SwarmKernel, args: argparse.Namespace) -> int:
    kind = args.kind.upper()
    if _require_kind_correlation(kind, args.run_id, args.request_id, args.reply_to):
        return 1

    attachments = _parse_attachments(args.attachment) if args.attachment else []
    env = Envelope(subject=args.subject, body=args.body, kind=args.kind, attachments=attachments,
                   reply_to=args.reply_to, run_id=args.run_id, request_id=args.request_id,
                   require_ack=getattr(args, "require_ack", False))
    receipt = kernel.direct(args.session_id, args.sender, args.to, env)
    print(json.dumps({
        "msg_id": receipt.msg_id,
        "status": receipt.status,
        "session_id": receipt.session_id,
        "target": receipt.target,
    }, indent=2))
    return 0


def _swarm_channel(kernel: SwarmKernel, args: argparse.Namespace) -> int:
    kind = args.kind.upper()
    if _require_kind_correlation(kind, args.run_id, args.request_id, args.reply_to):
        return 1
    attachments = _parse_attachments(args.attachment) if args.attachment else []
    env = Envelope(subject=args.subject, body=args.body, kind=args.kind, attachments=attachments,
                   reply_to=args.reply_to, run_id=args.run_id, request_id=args.request_id,
                   require_ack=getattr(args, "require_ack", False))
    receipts = kernel.channel(args.session_id, args.sender, args.channel_id, env)
    out = [{"msg_id": r.msg_id, "recipient": r.recipient, "status": r.status} for r in receipts]
    print(json.dumps(out, indent=2))
    return 0


def _swarm_broadcast(kernel: SwarmKernel, args: argparse.Namespace) -> int:
    kind = args.kind.upper()
    if _require_kind_correlation(kind, args.run_id, args.request_id, args.reply_to):
        return 1
    env = Envelope(subject=args.subject, body=args.body, kind=args.kind,
                   reply_to=args.reply_to, run_id=args.run_id, request_id=args.request_id,
                   require_ack=getattr(args, "require_ack", False))
    receipts = kernel.broadcast(args.session_id, args.sender, env)
    out = [{"msg_id": r.msg_id, "recipient": r.recipient, "status": r.status} for r in receipts]
    print(json.dumps(out, indent=2))
    return 0


def _swarm_notice(kernel: SwarmKernel, args: argparse.Namespace) -> int:
    kind = args.kind.upper()
    if _require_kind_correlation(kind, args.run_id, args.request_id, args.reply_to):
        return 1
    env = Envelope(subject=args.subject, body=args.body, kind=args.kind,
                   reply_to=args.reply_to, run_id=args.run_id, request_id=args.request_id,
                   require_ack=getattr(args, "require_ack", False))
    receipts = kernel.notice(args.session_id, args.sender, args.topic, env, ttl=args.ttl)
    out = [{"msg_id": r.msg_id, "recipient": r.recipient, "status": r.status} for r in receipts]
    print(json.dumps(out, indent=2))
    return 0


def _swarm_poll(kernel: SwarmKernel, args: argparse.Namespace) -> int:
    result = kernel.poll(args.session_id, args.agent, cursor=args.cursor, limit=args.limit)
    print(json.dumps({
        "messages": result.messages,
        "cursor": result.cursor,
        "has_more": result.has_more,
    }, indent=2))
    return 0


def _swarm_ack(kernel: SwarmKernel, args: argparse.Namespace) -> int:
    # For consumed phase, first read the message (inbox → processing) to create
    # a claim file, then finalize (processing → archive).
    #
    # P0-3 fix: store.read() pops the EARLIEST message (mtime-ordered), so its
    # msg_id may differ from the user-supplied --msg-id.  Use the actual msg_id
    # from the read result to avoid losing the wrong message.  If they differ,
    # release the message back to inbox and report the mismatch.
    if args.phase == "consumed":
        # P2-6: use MailboxService.read() instead of store.read() so the claim
        # goes through the service's per-agent lock (P2-12), ack-route-unresolved
        # parking (P1-1), and READ receipt emission — store.read() bypasses all
        # three, causing the sender's ledger to stay stuck at DISPATCHED.
        from codeagent.mailbox.service import MailboxService
        svc = MailboxService(kernel._store, kernel=kernel)
        outcome = svc.read(args.session_id, args.agent, owner=args.agent, msg_id=args.msg_id)
        if outcome.message is None:
            print(f"error: no message to ack: {args.msg_id} (status={outcome.status})", file=sys.stderr)
            return 1
        actual_id = outcome.message.get("msg_id", "")
        if actual_id != args.msg_id:
            # Release the message back to inbox so it is not lost.
            kernel._store.release(args.session_id, args.agent, actual_id, owner=args.agent)
            print(
                f"error: msg_id mismatch: requested={args.msg_id} "
                f"actual={actual_id}. Message released back to inbox.",
                file=sys.stderr,
            )
            return 1
    status = kernel.ack(args.session_id, args.agent, args.msg_id, args.phase)
    print(json.dumps({"status": status}))
    return 0


def _swarm_status(kernel: SwarmKernel, args: argparse.Namespace) -> int:
    session = kernel.get_session(args.session_id)
    if session is None:
        print(f"error: session not found: {args.session_id}", file=sys.stderr)
        return 1
    if args.trace:
        # Top4: trace status —— 按 trace_id 聚合 canonical history
        try:
            result = kernel.trace(args.session_id, args.trace)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return 0
    locations = {}
    for member in session.roster:
        loc = kernel.get_location(args.session_id, member)
        if loc:
            locations[member] = {"host": loc.host_alias, "backend": loc.backend}
    print(json.dumps({
        "session_id": session.session_id,
        "manager_id": session.manager_id,
        "roster": list(session.roster),
        "acl": {
            "authority": session.acl.authority,
            "policy": session.acl.policy,
        },
        "locations": locations,
    }, indent=2))
    return 0


def _swarm_watch(kernel: SwarmKernel, args: argparse.Namespace) -> int:
    """Poll loop — prints new messages at --interval seconds."""
    import time as _time

    # Opportunistic flush: retry pending outbox entries before polling.
    try:
        sink = kernel._sink
        engine = getattr(sink, "_engine", None)
        if engine is not None:
            flushed = engine.flush(session_id=args.session_id)
            if flushed:
                log.info("_swarm_watch: flushed %d pending outbox entries", flushed)
    except Exception as exc:
        log.debug("_swarm_watch: opportunistic flush failed: %s", exc)

    cursor = ""
    iteration = 0
    max_iter = args.iterations
    while True:
        result = kernel.poll(args.session_id, args.agent, cursor=cursor, limit=50)
        for msg in result.messages:
            print(json.dumps(msg, ensure_ascii=False))
        cursor = result.cursor
        iteration += 1
        if max_iter and iteration >= max_iter:
            break
        _time.sleep(args.interval)
    return 0


def _swarm_outbox(kernel: SwarmKernel, args: argparse.Namespace) -> int:
    """Dispatch ``swarm outbox`` subcommands."""
    cmd = args.outbox_cmd
    if cmd is None:
        print("error: specify an outbox subcommand (pending|flush|status)", file=sys.stderr)
        return 1

    sink = kernel._sink
    engine = getattr(sink, "_engine", None)
    if engine is None:
        print("error: kernel has no DeliveryEngine sink; outbox commands require EngineDeliverySink", file=sys.stderr)
        return 1

    session_id = getattr(args, "session_id", None)

    if cmd == "pending":
        envelopes = engine.pending(session_id=session_id)
        if getattr(args, "json_output", False):
            out = [{k: e.get(k, "") for k in ("msg_id", "to", "kind")} for e in envelopes]
            print(json.dumps(out, indent=2))
        else:
            for e in envelopes:
                print(f"{e.get('msg_id', '?'):40s}  {e.get('to', '?'):20s}  {e.get('kind', '?')}")
        return 0

    if cmd == "flush":
        flushed = engine.flush(session_id=session_id)
        print(json.dumps({"flushed": flushed}))
        return 0 if flushed > 0 else 1

    if cmd == "status":
        stats = engine.outbox_stats(session_id=session_id)
        print(json.dumps(stats))
        return 0

    if cmd == "dead":
        entries = engine.dead_letter_list(session_id=session_id)
        if not entries:
            print("(no dead-lettered messages)")
            return 0
        for e in entries:
            print(f"{e['msg_id']:40s}  {e['to']:20s}  {e['reason']}")
        return 0

    if cmd == "requeue":
        ok = engine.dead_letter_requeue(args.session_id, args.msg_id)
        if not ok:
            print(f"error: dead-letter entry not found: {args.msg_id}", file=sys.stderr)
            return 1
        print(json.dumps({"requeued": args.msg_id}))
        return 0

    if cmd == "purge":
        removed = engine.dead_letter_purge(session_id=session_id)
        print(json.dumps({"purged": removed}))
        return 0

    print(f"error: unknown outbox command: {cmd}", file=sys.stderr)
    return 1


def _swarm_launch(kernel: SwarmKernel, args: argparse.Namespace) -> int:
    """Bootstrap remote workers and start manager-pull loop.

    1. If --bootstrap: SSH to each non-local agent — mailbox session-init + send INIT.
    2. If --pull: poll loop — pull_remote/ingest/replay until all workers terminal.
    """
    session = kernel.get_session(args.session_id)
    if session is None:
        print(f"error: session not found: {args.session_id}", file=sys.stderr)
        return 1

    manager = session.manager_id
    roster = list(session.roster)
    roster_csv = ",".join(roster)

    # ── Phase 1: Bootstrap remote workers ────────────────────────────
    if args.bootstrap:
        remote_agents = []
        for a in roster:
            if a == manager:
                continue
            loc = kernel.get_location(args.session_id, a)
            if not loc:
                continue
            if loc.host_alias == "__local__":
                continue
            em = loc.execution_mode
            if em is not None and em != ExecutionMode.MAILBOX_WORKER:
                log.info("launch bootstrap: skipping %s (execution_mode=%s)", a, em.value)
                continue
            remote_agents.append(a)
        for agent in remote_agents:
            loc = kernel.get_location(args.session_id, agent)
            host = loc.host_alias

            # session-init on remote host
            cmd = [
                "aimeshchat", "mailbox", "session-init",
                "--host", host,
                "--session", args.session_id,
                "--manager", manager,
                "--agents", roster_csv,
            ]
            if loc.mailbox_root:
                cmd.extend(["--mailbox-root", loc.mailbox_root])
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if r.stdout.strip():
                    print(r.stdout.strip())
                if r.returncode != 0:
                    print(f"error: session-init failed for {agent}@{host}: {r.stderr.strip()}", file=sys.stderr)
                    return 1
            except (subprocess.TimeoutExpired, OSError) as exc:
                print(f"error: session-init exception for {agent}@{host}: {exc}", file=sys.stderr)
                return 1

            # send INIT task to this worker
            import uuid as _uuid
            init_run_id = f"run-{_uuid.uuid4().hex[:12]}"
            init_req_id = f"req-{_uuid.uuid4().hex[:12]}"
            cmd = [
                "aimeshchat", "mailbox", "send",
                "--host", host,
                "--session", args.session_id,
                "--to", agent,
                "--from", manager,
                "--kind", "TASK",
                "--subject", "INIT",
                "--body", json.dumps({"session_id": args.session_id, "agent": agent}),
                "--run-id", init_run_id,
                "--request-id", init_req_id,
            ]
            if loc.mailbox_root:
                cmd.extend(["--mailbox-root", loc.mailbox_root])
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if r.stdout.strip():
                    print(r.stdout.strip())
                if r.returncode != 0:
                    print(f"error: send INIT failed for {agent}@{host}: {r.stderr.strip()}", file=sys.stderr)
                    return 1
            except (subprocess.TimeoutExpired, OSError) as exc:
                print(f"error: send INIT exception for {agent}@{host}: {exc}", file=sys.stderr)
                return 1

        print(json.dumps({
            "phase": "bootstrap",
            "session_id": args.session_id,
            "remote_agents": remote_agents,
            "status": "done",
        }, indent=2))

    # ── Phase 2: Manager-pull loop ───────────────────────────────────
    if not args.pull:
        return 0

    import time as _time

    # Build set of non-manager agent_ids that have locations registered.
    registered_workers = []
    for a in roster:
        if a == manager:
            continue
        loc = kernel.get_location(args.session_id, a)
        if not loc:
            continue
        em = loc.execution_mode
        if em is not None and em != ExecutionMode.MAILBOX_WORKER:
            log.info("launch pull: skipping %s (execution_mode=%s)", a, em.value)
            continue
        rm = loc.return_mode
        if rm == ReturnMode.BIDIRECTIONAL:
            log.info("launch pull: skipping %s (return_mode=bidirectional)", a)
            continue
        registered_workers.append(a)
    registered_workers = sorted(registered_workers)
    if not registered_workers:
        print("warning: no registered workers found — skipping pull loop", file=sys.stderr)
        return 0

    print(json.dumps({
        "phase": "pull",
        "session_id": args.session_id,
        "tracking_workers": registered_workers,
    }, indent=2))

    iteration = 0
    max_iter = args.max_iterations
    # Collect unique remote hosts from worker locations
    remote_hosts = sorted({
        kernel.get_location(args.session_id, w).host_alias
        for w in registered_workers
        if kernel.get_location(args.session_id, w)
        and kernel.get_location(args.session_id, w).host_alias != "__local__"
    })
    while True:
        # Pull messages from each remote host (for manager-pull return mode).
        try:
            messages = []
            for host in remote_hosts:
                messages.extend(kernel.pull_remote(args.session_id, host))
            persisted_ids = set(kernel.ingest(args.session_id, messages))
            if persisted_ids:
                print(f"ingested {len(persisted_ids)} message(s)", file=sys.stderr)

            # finalize/release each pulled message based on ingest outcome
            for msg in messages:
                mid = msg.get("msg_id", "")
                pull_host = msg.get("_pull_host", "")
                pull_root = msg.get("_pull_mailbox_root", "")
                if mid in persisted_ids:
                    kernel.finalize_remote(pull_host, args.session_id,
                                           manager, msg, pull_root)
                else:
                    kernel.release_remote(args.session_id, mid,
                                          pull_host, manager, pull_root)
        except (ValueError, OSError) as exc:
            log.debug("launch pull_remote/ingest error: %s", exc)

        # Replay canonical history to check for terminal REPORTs.
        all_history = kernel._store.read_history(args.session_id)
        reported = set()
        for m in all_history:
            if m.get("kind") == "REPORT" and m.get("from") != manager:
                reported.add(m["from"])

        if reported >= set(registered_workers):
            print(json.dumps({
                "phase": "pull",
                "status": "all_workers_reported",
                "reported": sorted(reported),
            }, indent=2))
            break

        iteration += 1
        if max_iter and iteration >= max_iter:
            print(json.dumps({
                "phase": "pull",
                "status": "max_iterations",
                "reported": sorted(reported),
                "pending": sorted(set(registered_workers) - reported),
            }, indent=2))
            break

        _time.sleep(args.poll_interval)

    return 0


def _execute(request: RunRequest, target: Target, registry: SessionRegistry, repo_map=None, run_context: Optional[RunContext] = None) -> RunResult:
    """Core execution: local → LocalTransport, remote → SSH/RelayTransport.

    Session lifecycle (all under per-key lock):
      1. Compute namespace key
      2. Lookup existing session → get real backend session_id
      3. Mark starting
      4. Execute (transport receives real session_id)
      5. Mark observed/active/failed based on result
    """
    if run_context is not None:
        log.info("[oracle] run_context: review_key=%s run_id=%s swarm_session=%s",
                 run_context.review_key, run_context.run_id, run_context.swarm_session_id)
    ns_key = request.session_key or registry.compute_key(request, target)

    def _run() -> RunResult:
        # Lookup existing session for resume
        backend_session_id = None
        if not request.new_session and not request.no_auto_resume:
            record = registry.lookup(ns_key)
            if record and record.status == "active" and record.session_id:
                backend_session_id = record.session_id
                print(f"[codeagent] resuming session {backend_session_id[:12]}... (key={ns_key})", file=sys.stderr)

        # Mark starting (preserves existing session_id via COALESCE)
        registry.mark_starting(ns_key, request, target,
                               clear_session=request.new_session)

        # Execute with exception handling
        try:
            if target.is_local:
                transport = LocalTransport()
                host = HostSpec(name="__local__", ssh_alias="__local__", hostnames=())
                result = transport.execute(request, host, target.workdir, session_id=backend_session_id)
            else:
                transport = _get_transport(target.host, repo_map)
                try:
                    if hasattr(transport, 'warm'):
                        transport.warm(target.host)
                    result = transport.execute(request, target.host, target.workdir, session_id=backend_session_id)
                except TransportError:
                    if not target.host.fallback_ssh_alias:
                        raise
                    log.warning(
                        "warm/execute for %s failed, retrying with fallback %s",
                        target.host.ssh_alias,
                        target.host.fallback_ssh_alias,
                    )
                    fallback_host = dataclasses.replace(
                        target.host,
                        ssh_alias=target.host.fallback_ssh_alias,
                        fallback_ssh_alias="",
                    )
                    if hasattr(transport, 'warm'):
                        transport.warm(fallback_host)
                    result = transport.execute(request, fallback_host, target.workdir, session_id=backend_session_id)
        except Exception as exc:
            # Transport failed — mark session as failed
            registry.mark_failed(ns_key)
            return RunResult(returncode=1, stderr=f"transport error: {exc}")

        # Update session state
        if result.session_id:
            registry.mark_observed(ns_key, result.session_id)
            registry.upsert(ns_key, result, request, target)
            # Auto park acquire: register oracle session as HOT_PARKED so
            # park info/renew/release work without manual steps.  Done here
            # (inside _execute) rather than in _cmd_run so it fires even if
            # the outer shell backgrounds the process before stdout prints.
            if request.agent and request.agent.startswith("oracle") and result.returncode == 0:
                try:
                    subprocess.run(
                        [
                            "aimeshchat", "park", "acquire",
                            ns_key, "--agent-type", request.agent,
                            "--backend-id", result.session_id,
                            "--peer-id", result.session_id,
                        ],
                        capture_output=True, timeout=10,
                    )
                except Exception as exc:
                    log.warning("auto park acquire failed (non-fatal): %s", exc)
        elif result.returncode == 0:
            registry.mark_active(ns_key)
        else:
            registry.mark_failed(ns_key)

        return result

    with registry.run_with_lock(ns_key):
        result = _run()

    return result


def _bootstrap_oracle_swarm(
    request: RunRequest,
    ns_key: str,
) -> RunContext:
    """Pre-spawn bootstrap for oracle: create RunContext, swarm session, and dispatch INIT.

    Phase 1 of Oracle design: sets up the mailbox infrastructure so the
    oracle agent has a persistent session before _execute runs.  Does NOT
    call communicate/pump — that is Phase 2.

    Returns a fully-populated RunContext.  Side effects:
      - Creates swarm session dirs in mailbox root
      - Registers oracle AgentLocation in kernel routing table
      - Writes INIT envelope to oracle mailbox
    """
    run_id = uuid4().hex[:10]
    request_id = uuid4().hex[:12]
    safe_key = ns_key.replace(":", "-")[-12:]
    sid = f"ora-{safe_key}-{run_id}"
    mailbox_root = resolve_root()
    kernel, _store = _get_swarm_kernel(store_root=mailbox_root)

    # Create session with manager + oracle roster
    kernel.create_session(
        session_id=sid,
        manager_id="manager",
        roster=["oracle"],
    )

    # Register oracle agent location
    kernel.register(
        AgentLocation(
            agent_id="oracle",
            host_alias="__local__",
            backend="omp",
            execution_mode=ExecutionMode.MAILBOX_WORKER,
            return_mode=ReturnMode.BIDIRECTIONAL,
            mailbox_root=str(mailbox_root),
        ),
        session_id=sid,
    )

    # Dispatch manager → oracle TASK/INIT envelope
    kernel.direct(
        session_id=sid,
        sender="manager",
        to_agent="oracle",
        envelope=Envelope(
            subject="oracle-init",
            body=request.task,
            kind="TASK",
            run_id=run_id,
            request_id=request_id,
        ),
    )

    # Inject mailbox identity so oracle knows how to receive follow-ups
    request.task += (
        f"\n\n--- MAILBOX IDENTITY ---\n"
        f"You are oracle agent in swarm session {sid}.\n"
        f"Check inbox: mailbox read --session {sid} --agent oracle --owner oracle --json\n"
        f"Read skill://agent-swarm/roles/worker.md for protocol.\n"
        f"Manager may send follow-up TASK/QUESTION via mailbox.\n"
        f"Before each response, check your inbox for new messages.\n"
    )

    rc = RunContext(
        review_key=ns_key,
        generation=1,
        run_id=run_id,
        request_id=request_id,
        swarm_session_id=sid,
        mailbox_root=str(mailbox_root),
    )
    log.info(
        "[oracle] bootstrap complete: sid=%s run_id=%s mailbox=%s",
        sid, run_id, mailbox_root,
    )
    return rc


def _resolve_agent_backend(agent: Optional[str], requested: str) -> str:
    """Resolve the runtime backend for an agent.

    Oracle-class agents (agent.startswith("oracle")) prefer OMP (full
    hot/in-loop) and fall back to OpenCode — resolved via the RuntimeRegistry
    with the persist-oracle required capability ``warm_resume``. Generic is
    only used when explicitly requested. Non-oracle agents pass through.
    """
    if not agent or not agent.startswith("oracle"):
        return requested
    from codeagent.runtime.base import CAP_WARM_RESUME
    from codeagent.runtime.registry import RuntimeRegistry

    reg = RuntimeRegistry()
    try:
        return reg.get(requested or None, required_capabilities=frozenset({CAP_WARM_RESUME})).name
    except Exception:
        for name in ("omp", "opencode"):
            try:
                return reg.get(name, required_capabilities=frozenset({CAP_WARM_RESUME})).name
            except Exception:
                continue
        return requested


def _cmd_run(args: argparse.Namespace) -> int:
    task = args.task or sys.stdin.read().strip()
    if not task:
        print("error: no task provided", file=sys.stderr)
        return 1

    # ── background child mode (--_bg-job-id) ───────────────────────────
    # Hidden flag: this is a detached child process — run synchronously
    # and persist the result to the job directory on exit.
    bg_job_id = getattr(args, "_bg_job_id", None)
    if bg_job_id:
        return _run_bg_child(args, task, bg_job_id)

    # ── tmux foreground mode (--tmux / --split) ────────────────────────
    if getattr(args, "tmux", False) or getattr(args, "split", False):
        return _run_in_tmux(args, task)

    # ── background thread mode (--background) ──────────────────────────
    if getattr(args, "background", False):
        return _run_in_background(args, task)

    # ── synchronous foreground (default — unchanged) ───────────────────
    return _run_sync(args, task)


def _run_bg_child(args: argparse.Namespace, task: str, job_id: str) -> int:
    """Background child process entry — run sync, persist result to job dir.

    This function is reached only via ``--_bg-job-id`` (hidden flag set by
    ``_run_in_background``).  It executes the task synchronously and writes
    the ``RunResult`` to the job directory via ``JobManager.mark_done``.
    """
    result = _run_sync(args, task)
    try:
        from codeagent.job import get_manager
        mgr = get_manager()
        # Reconstruct a RunResult from the exit code (stdout/stderr already
        # printed to /dev/null by the parent Popen; the wire result is in
        # the session registry, not here).  We persist the returncode.
        mgr.mark_done(job_id, RunResult(returncode=result))
    except Exception as exc:
        log.warning("bg child: failed to persist job %s: %s", job_id, exc)
    return result


def _run_sync(args: argparse.Namespace, task: str) -> int:
    """Original synchronous foreground execution — no behavior change."""
    # Runtime resolution via the registry — oracle agents are NOT hardcoded
    # to OMP (they prefer it, but degrade to OpenCode when unavailable).
    backend = _resolve_agent_backend(args.agent, args.backend)

    request = RunRequest(
        task=task,
        workdir=args.workdir,
        backend=backend,
        agent=args.agent,
        model=args.model,
        skills=getattr(args, 'skills', None),
        skip_permissions=args.skip_permissions,
        session_key=args.session_key,
        new_session=args.new_session,
        no_auto_resume=args.no_auto_resume,
        host=args.host,
    )

    # Propagate --output path so OMPRunner._build_cmd can pass it to omp
    if args.output:
        request.output = args.output

    repo_map = None
    try:
        repo_map = load_repo_map()
    except FileNotFoundError:
        if not request.host:
            raise  # topic routing requires repo-map
        # ad-hoc host: empty repo-map is fine
        repo_map = RepoMap(midocs_root=Path("."), hosts={}, topics={})
    registry = SessionRegistry()
    target = resolve_target(request, repo_map)

    # Oracle pre-spawn bootstrap: create RunContext + swarm session + INIT envelope
    run_context: Optional[RunContext] = None
    if request.agent and request.agent.startswith("oracle"):
        ns_key = request.session_key or registry.compute_key(request, target)
        run_context = _bootstrap_oracle_swarm(request, ns_key)
        # Timeout handled by OMPRunner._ORACLE_TIMEOUT (3600s) — don't override here

        # Warm resume: check oracle inbox for pending manager messages
        if not request.new_session and run_context is not None:
            _kernel, _store = _get_swarm_kernel(
                store_root=Path(run_context.mailbox_root),
            )
            pending_result = _kernel.poll(
                run_context.swarm_session_id, "oracle",
            )
            pending = pending_result.messages
            if pending:
                request.task += "\n\n--- PENDING MAILBOX MESSAGES ---\n"
                for msg in pending:
                    request.task += (
                        f"From {msg.get('from', '?')}: "
                        f"{msg.get('body', '')[:500]}\n"
                    )
                request.task += "--- END PENDING ---\n"

    try:
        result = _execute(request, target, registry, repo_map, run_context=run_context)
    except Exception as exc:
        # Crash recovery: try to recover oracle response from mailbox
        recovered = ""
        if run_context:
            try:
                store = MailboxStore(root=Path(run_context.mailbox_root))
                msgs = store.read_history(run_context.swarm_session_id)
                for m in reversed(msgs):
                    if m.get('kind') == 'REPORT' and m.get('from') == 'oracle':
                        recovered = m.get('body', '')[:2000]
                        break
            except Exception:
                pass
        if recovered:
            print(f"[recovered from mailbox]: {recovered}", file=sys.stderr)
        print(f"[oracle error - output may be incomplete]: {exc}", file=sys.stderr)
        result = RunResult(returncode=1, stderr=str(exc))

    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    if args.output:
        Path(args.output).write_text(json.dumps({
            "session_id": result.session_id,
            "backend": result.backend,
            "host": result.host,
            "workdir": result.workdir,
            "exit_code": result.returncode,
        }, indent=2))
    return result.returncode


def _run_in_tmux(args: argparse.Namespace, task: str) -> int:
    """Spawn the agent in the user's current tmux session (visible, interactive).

    Reconstructs the ``aimeshchat run`` command *without* --tmux/--split
    flags so the child runs in synchronous foreground mode inside the pane.
    """
    from codeagent.launchers.tmux import detect_current_tmux, spawn_in_current_tmux

    if detect_current_tmux() is None:
        print(
            "error: --tmux/--split requires running inside a tmux session.\n"
            "Start tmux or byobu first, then retry.",
            file=sys.stderr,
        )
        return 1

    # Reconstruct the equivalent aimeshchat CLI command (no tmux flags).
    argv: list[str] = ["aimeshchat", "run"]
    if args.workdir:
        argv.append(args.workdir)
    argv.append(task)
    if args.host:
        argv.extend(["--host", args.host])
    if args.backend:
        argv.extend(["--backend", args.backend])
    if args.agent:
        argv.extend(["--agent", args.agent])
    if args.model:
        argv.extend(["--model", args.model])
    if getattr(args, "skills", None):
        argv.extend(["--skills", args.skills])
    if args.session_key:
        argv.extend(["--session-key", args.session_key])
    if args.new_session:
        argv.append("--new-session")
    if args.no_auto_resume:
        argv.append("--no-auto-resume")
    if args.skip_permissions:
        argv.append("--skip-permissions")
    if args.output:
        argv.extend(["--output", args.output])

    try:
        pane_id = spawn_in_current_tmux(
            argv,
            label="aimeshchat",
            split=getattr(args, "split", False),
            cwd=args.workdir or "",
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"[tmux] agent spawned in pane {pane_id}", file=sys.stderr)
    return 0


def _run_in_background(args: argparse.Namespace, task: str) -> int:
    """Submit the execution as a detached subprocess and return immediately.

    The child process writes its result to a job directory on disk so the
    caller can poll via ``aimeshchat job status/wait/list`` even after the
    parent CLI exits.

    Uses ``start_new_session=True`` so the child is not killed when the
    parent's terminal closes (SIGHUP).
    """
    from codeagent.job import get_manager

    mgr = get_manager()
    job_id = mgr.create_placeholder(
        task=task[:120],
        host=args.host or "__local__",
        workdir=args.workdir,
    )

    # Reconstruct the equivalent aimeshchat run command (no --background),
    # adding --_bg-job-id so the child knows where to write its result.
    argv: list[str] = [sys.executable, "-m", "codeagent.cli", "run"]
    if args.workdir:
        argv.append(args.workdir)
    argv.append(task)
    if args.host:
        argv.extend(["--host", args.host])
    if args.backend:
        argv.extend(["--backend", args.backend])
    if args.agent:
        argv.extend(["--agent", args.agent])
    if args.model:
        argv.extend(["--model", args.model])
    if getattr(args, "skills", None):
        argv.extend(["--skills", args.skills])
    if args.session_key:
        argv.extend(["--session-key", args.session_key])
    if args.new_session:
        argv.append("--new-session")
    if args.no_auto_resume:
        argv.append("--no-auto-resume")
    if args.skip_permissions:
        argv.append("--skip-permissions")
    if args.output:
        argv.extend(["--output", args.output])
    # Hidden flag: child writes result to job dir.
    argv.extend(["--_bg-job-id", job_id])

    # Detach: start_new_session so SIGHUP from terminal doesn't kill the child.
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # Write the child PID so the job dir can track liveness.
    mgr.mark_running(job_id, pid=proc.pid)

    print(f"[background] job submitted: {job_id}  (pid={proc.pid})", file=sys.stderr)
    print(f"  poll:  aimeshchat job status {job_id}", file=sys.stderr)
    print(f"  wait:  aimeshchat job wait {job_id}", file=sys.stderr)
    return 0


def _build_route_prompt(topic: str, task: str) -> str:
    """Wrap task with standard route prompt for structured output."""
    return (
        "你正在执行一项代码调研任务。\n\n"
        "输出要求：\n"
        "1. 直接输出调研结果\n"
        "2. 结构清晰，使用标题分段\n"
        "3. 关键发现用代码引用佐证\n"
        "4. 结尾给出结论和建议\n\n"
        f"主题：{topic}\n\n"
        f"任务：{task}"
    )


def _cmd_route(args: argparse.Namespace) -> int:
    repo_map = load_repo_map()
    positional = args.args or []

    # list
    if not positional or positional[0] == "list":
        if getattr(args, 'json_output', False):
            data = {
                name: {
                    "hosts": [r.host for r in spec.repos],
                    "description": spec.description,
                    "repos": [{"host": r.host, "path": r.path, "note": r.note} for r in spec.repos],
                }
                for name, spec in sorted(repo_map.topics.items())
            }
            print(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            for name, spec in sorted(repo_map.topics.items()):
                hosts = [r.host for r in spec.repos]
                print(f"  {name:40s} [{', '.join(hosts)}] {spec.description}")
        return 0

    # where <topic>
    if positional[0] == "where":
        if len(positional) < 2:
            print("error: codeagent route where <topic>", file=sys.stderr)
            return 1
        topic = repo_map.topic(positional[1])
        if getattr(args, 'json_output', False):
            data = {
                "name": topic.name,
                "description": topic.description,
                "repos": [
                    {"index": i, "host": r.host, "path": r.path, "note": r.note,
                     "local": bool(repo_map.hosts.get(r.host) and resolve_is_local(repo_map.hosts[r.host]))}
                    for i, r in enumerate(topic.repos)
                ],
            }
            print(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            print(f"Topic: {topic.name}")
            print(f"Description: {topic.description}")
            for i, r in enumerate(topic.repos):
                host = repo_map.hosts.get(r.host)
                local = " [LOCAL]" if host and resolve_is_local(host) else ""
                print(f"  [{i}] {r.host}:{r.path}{local}  {r.note}")
        return 0

    # <topic> [task...]
    topic_name = positional[0]
    task_text = " ".join(positional[1:]) if len(positional) > 1 else ""
    if not task_text:
        task_text = sys.stdin.read().strip()
    if not task_text:
        print("error: no task", file=sys.stderr)
        return 1

    try:
        topic = repo_map.topic(topic_name)
    except KeyError:
        print(f"error: topic not found: {topic_name}", file=sys.stderr)
        return 1

    # Wrap task with structured prompt unless --raw
    if not getattr(args, 'raw', False):
        task_text = _build_route_prompt(topic_name, task_text)

    request = RunRequest(
        task=task_text,
        topic=topic_name,
        repo_index=args.repo,
        backend=args.backend,
        agent=args.agent,
        model=args.model,
        skills=getattr(args, 'skills', None),
        session_key=args.session_key,
        new_session=args.new_session,
        no_auto_resume=args.no_auto_resume,
        raw=getattr(args, 'raw', False),
        skip_permissions=getattr(args, 'skip_permissions', False),
    )
    target = resolve_target(request, repo_map)

    if args.dry_run:
        info = f"Topic: {topic_name} → host={target.host.name} path={target.workdir} local={target.is_local}"
        if getattr(args, 'json_output', False):
            print(json.dumps({"dry_run": True, "topic": topic_name,
                              "host": target.host.name, "path": target.workdir,
                              "local": target.is_local}))
        else:
            print(info)
        return 0

    registry = SessionRegistry()
    result = _execute(request, target, registry, repo_map)

    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    if getattr(args, 'output', None):
        Path(args.output).write_text(json.dumps({
            "session_id": result.session_id,
            "backend": result.backend,
            "host": result.host,
            "workdir": result.workdir,
            "exit_code": result.returncode,
        }, indent=2))
    return result.returncode


def _cmd_sessions(args: argparse.Namespace) -> int:
    registry = SessionRegistry()

    if args.sess_cmd == "list":
        records = registry.list_all(host=getattr(args, "host", None), topic=getattr(args, "topic", None))
        for r in records:
            print(f"  {r.key[:50]:50s} {r.session_id[:12]:12s} {r.status:12s} {r.host}:{r.workdir}")
        return 0

    if args.sess_cmd == "show":
        r = registry.lookup(args.key)
        if r:
            print(json.dumps(r.__dict__, indent=2))
            return 0
        print(f"not found: {args.key}")
        return 1

    if args.sess_cmd == "reset":
        registry.delete(args.key)
        print(f"reset: {args.key}")
        return 0

    if args.sess_cmd == "bind":
        registry.bind(args.key, args.session_id)
        print(f"bound: {args.key} -> {args.session_id}")
        return 0

    return 0


def _cmd_ssh(args: argparse.Namespace) -> int:
    transport = SSHTransport()

    if args.ssh_cmd == "warm":
        for name in (args.hosts or []):
            host = HostSpec(name=name, ssh_alias=name, hostnames=(name,))
            transport.warm(host)
            print(f"  {name}: ok")
        return 0

    if args.ssh_cmd == "status":
        for name, sock in transport.list_sockets():
            host = HostSpec(name=name, ssh_alias=name, hostnames=(name,))
            alive = transport.check(host)
            print(f"  {name}: {'alive' if alive else 'dead'} ({sock})")
        return 0

    if args.ssh_cmd == "stop":
        for name in (args.hosts or []):
            host = HostSpec(name=name, ssh_alias=name, hostnames=(name,))
            transport.stop(host)
            print(f"  {name}: stopped")
        return 0

    return 0


def _cmd_mailbox(args: argparse.Namespace) -> int:
    """Dispatch mailbox command to local or remote host."""
    raw_args = args.mailbox_args
    if not raw_args:
        from codeagent.mailbox.cli import main as mailbox_main
        mailbox_main(["--help"])
        return 0

    # Extract --host from mailbox_args (argparse REMAINDER swallows it)
    mailbox_args = []
    host = getattr(args, "host", None)
    mailbox_root = getattr(args, "mailbox_root", None)
    i = 0
    while i < len(raw_args):
        if raw_args[i] == "--host" and i + 1 < len(raw_args):
            host = host or raw_args[i + 1]
            i += 2
        elif raw_args[i].startswith("--host="):
            host = host or raw_args[i].split("=", 1)[1]
            i += 1
        elif raw_args[i] == "--mailbox-root" and i + 1 < len(raw_args):
            mailbox_root = mailbox_root or raw_args[i + 1]
            i += 2
        elif raw_args[i].startswith("--mailbox-root="):
            mailbox_root = mailbox_root or raw_args[i].split("=", 1)[1]
            i += 1
        else:
            mailbox_args.append(raw_args[i])
            i += 1

    if not host:
        # No remote host specified — local mailbox operations.
        from codeagent.mailbox.cli import main as mailbox_main
        if mailbox_root:
            mailbox_args = ["--mailbox-root", mailbox_root] + mailbox_args
        old_stdout, old_stderr = sys.stdout, sys.stderr
        try:
            sys.stdout = sys.__stdout__
            sys.stderr = sys.__stderr__
            mailbox_main(mailbox_args)
        except SystemExit as e:
            return e.code or 0
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
        return 0

    # Remote host specified — dispatch through TransportRouter.
    from codeagent.config.repo_map import load_repo_map
    from codeagent.domain import HostSpec, resolve_is_local

    repo_map = None
    try:
        repo_map = load_repo_map()
        host_spec = repo_map.hosts.get(host)
    except FileNotFoundError:
        host_spec = None

    if host_spec is None:
        host_spec = HostSpec(name=host, ssh_alias=host, hostnames=(host,), description="ad-hoc host")

    if resolve_is_local(host_spec):
        from codeagent.mailbox.cli import main as mailbox_main
        if mailbox_root:
            mailbox_args = ["--mailbox-root", mailbox_root] + mailbox_args
        mailbox_main(mailbox_args)
        return 0

    # Remote via transport selected by TransportRouter.
    transport = _router.get(host_spec, repo_map)

    exit_code, stdout, stderr = transport.mailbox(
        host_spec, mailbox_args, mailbox_root=mailbox_root or "",
    )
    if stdout:
        print(stdout, end="")
    if stderr:
        print(stderr, end="", file=sys.stderr)
    return exit_code


def _cmd_park(args: argparse.Namespace) -> int:
    """Dispatch park subcommands."""
    from codeagent.park.registry import ParkRegistry
    from codeagent.park.router import park_revive
    from codeagent.domain.park import Lifecycle, ParkManifest

    registry = ParkRegistry()
    cmd = args.park_cmd

    if cmd is None:
        print("park: missing subcommand. Try: aimeshchat park list|info|revive|release|sweep")
        return 1

    if cmd == "list":
        if args.all:
            with registry._connect() as conn:
                rows = conn.execute(
                    "SELECT manifest_json FROM park_leases WHERE lifecycle != 'released'"
                ).fetchall()
            manifests = [registry._dict_to_manifest(json.loads(r[0])) for r in rows]
        else:
            manifests = registry.list_active()
        if args.lifecycle:
            manifests = [m for m in manifests if m.lifecycle == Lifecycle(args.lifecycle)]
        for m in manifests:
            print(f"  {m.review_key}  lifecycle={m.lifecycle.value}  round={m.round}  agent={m.agent_type}")
        if not manifests:
            print("(no park instances)")

    elif cmd == "info":
        m = registry.lookup(args.review_key)
        if m:
            out = {
                "review_key": m.review_key,
                "lifecycle": m.lifecycle.value,
                "agent_type": m.agent_type,
                "model": m.model,
                "backend_session_id": m.backend_session_id,
                "peer_agent_id": m.peer_agent_id,
                "round": m.round,
                "created_at": m.created_at,
                "last_activity_at": m.last_activity_at,
                "soft_expires_at": m.soft_expires_at,
            }
            # Runtime observability comes from the gateway EventStore
            # (last_event/tool_stats/elapsed/runtime_health) — NOT from
            # progress files.
            try:
                from codeagent.gateway.client import GatewayClient
                from codeagent.gateway.model import GatewayError as GWErr

                client = GatewayClient(timeout=5)
                info = client.call("runtime.info", {"review_key": args.review_key})
                out["runtime"] = info
            except GWErr as exc:
                out["runtime_error"] = f"{exc.code}: {exc.message}"
            except Exception as exc:
                out["runtime_error"] = str(exc)
            print(json.dumps(out, indent=2))
        else:
            print(f"(no instance for '{args.review_key}')")

    elif cmd == "revive":
        rv = park_revive(args.review_key, args.prompt or "")
        out = {
            "method": rv.method,
            "success": rv.success,
            "context": rv.context[:500],
            "prompt": args.prompt or "",
        }
        if args.prompt and rv.method in ("hot", "warm", "cold"):
            cmd_list = [
                sys.executable, "-m", "codeagent.cli", "run",
                "--session-key", args.review_key,
                "--agent", "oracle",
                args.prompt,
                str(Path.cwd()),
            ]
            if rv.method == "cold":
                cmd_list.insert(cmd_list.index("run") + 1, "--new-session")
            try:
                # No hard timeout: oracle single turn can run 30–60min and the
                # skill promises explicit-release termination, not a kill. A
                # fixed timeout=3600 cut long tasks (TimeoutExpired was also
                # uncaught → crash). wait --timeout is the observation bound.
                r = subprocess.run(cmd_list, capture_output=True, text=True)
            except (subprocess.TimeoutExpired, Exception) as e:  # noqa: BLE001
                out["revive_error"] = f"{type(e).__name__}: {e}"
                out["revive_output"] = (getattr(e, "stdout", b"") or b"")[-500:]
                out["revive_returncode"] = -1
            else:
                out["revive_output"] = r.stdout[-500:]
                out["revive_returncode"] = r.returncode
        print(json.dumps(out, indent=2))

    elif cmd == "acquire":
        import time
        m = ParkManifest(
            review_key=args.review_key,
            lifecycle=Lifecycle.HOT_PARKED,
            agent_type=args.agent_type,
            peer_agent_id=args.peer_id,
            mailbox_agent_id=args.mailbox_id,
            backend_session_id=args.backend_id,
            created_at=time.time(),
        )
        ok = registry.acquire(args.review_key, m)
        if ok:
            print(f"Acquired: {args.review_key} (agent={args.agent_type})")
        else:
            print(f"Already exists: {args.review_key}")
            return 1

    elif cmd == "renew":
        registry.renew(args.review_key)
        print(f"Renewed: {args.review_key}")

    elif cmd == "release":
        registry.release(args.review_key)
        print(f"Released: {args.review_key}")

    elif cmd == "sweep":
        if args.dry_run:
            from codeagent.park.constants import PARK_DEFAULTS
            print(f"Dry run: would sweep expired instances (TTL={PARK_DEFAULTS['ttl_seconds']}s)")
        else:
            evicted = registry.sweep()
            if evicted:
                for k in evicted:
                    print(f"Evicted: {k}")
            else:
                print("(no expired instances)")

    return 0


def _cmd_artifact(args: argparse.Namespace) -> int:
    """Pull artifacts from remote hosts via ControlMaster, or verify local files."""
    if args.art_cmd == "pull":
        desc = ArtifactDescriptor(
            artifact_id=args.artifact_id,
            relative_path=args.relative_path,
            size=args.size,
            sha256=args.sha256,
            media_type=args.media_type,
        )
        try:
            dest = pull_artifact(
                host_alias=args.host,
                remote_root=args.remote_root,
                desc=desc,
                dest=Path(args.dest),
            )
            print(f"pulled {desc.artifact_id} → {dest} ({desc.size} bytes)")
            return 0
        except (TransportError, ValueError, FileNotFoundError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if args.art_cmd == "verify":
        try:
            verify_artifact(
                path=Path(args.file),
                expected_sha256=args.sha256,
                expected_size=args.size,
            )
            print(f"verified {args.file}: ok ({args.size} bytes)")
            return 0
        except (ValueError, FileNotFoundError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    return 1


def _cmd_gateway(args: argparse.Namespace) -> int:
    """Dispatch gateway subcommands."""
    from codeagent.gateway import cli as gw_cli

    cmd = args.gw_cmd
    if cmd is None:
        print("gateway: missing subcommand. Try: aimeshchat gateway start|ensure|status|stop|serve|rpc|health", file=sys.stderr)
        return 1
    handlers = {
        "start": gw_cli.cmd_gateway_start,
        "ensure": gw_cli.cmd_gateway_ensure,
        "status": gw_cli.cmd_gateway_status,
        "stop": gw_cli.cmd_gateway_stop,
        "serve": gw_cli.cmd_gateway_serve,
        "rpc": gw_cli.cmd_gateway_rpc,
        "health": gw_cli.cmd_gateway_health,
    }
    return handlers[cmd](args)


def _cmd_events(args: argparse.Namespace) -> int:
    """Dispatch events subcommands."""
    from codeagent.gateway import cli as gw_cli

    if args.ev_cmd == "watch":
        return gw_cli.cmd_events_watch(args)
    print("events: missing subcommand. Try: aimeshchat events watch", file=sys.stderr)
    return 1


def _cmd_runtime(args: argparse.Namespace) -> int:
    """Dispatch runtime subcommands."""
    from codeagent.gateway.client import GatewayClient
    from codeagent.gateway.model import GatewayError

    try:
        client = GatewayClient(timeout=10)
        if args.rt_cmd == "status":
            result = client.call("runtime.probe", {"runtime_id": args.runtime_id})
            print(json.dumps(result, indent=2))
            health = result.get("health", {})
            return 0 if health.get("alive") else 1
        if args.rt_cmd == "stop":
            result = client.call("runtime.stop", {"runtime_id": args.runtime_id, "reason": args.reason})
            print(json.dumps(result, indent=2))
            return 0
    except GatewayError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    print("runtime: missing subcommand. Try: aimeshchat runtime status|stop", file=sys.stderr)
    return 1


def _warn_deprecated_agent(args: argparse.Namespace) -> None:
    """B1: --agent 弃用告警——不删除参数（向后兼容），但明确引导 --model/--variant。

    模型/提示词策略已归 skill，CLI 只保留执行/路由/会话/mailbox；--agent
    仅保留为兼容占位参数，无模型语义（完全去 role，不再参与模型解析）。
    """
    agent = getattr(args, "agent", "") or ""
    if agent:
        print(
            f"warning: --agent 已弃用——模型/提示词策略归 skill，请用 --model/--variant "
            f"显式指定；--agent {agent!r} 仅保留为兼容占位参数（无模型语义，不参与模型解析）",
            file=sys.stderr,
        )


def _cmd_oracle(args: argparse.Namespace) -> int:
    """Dispatch oracle subcommands (start/ask/status/list/watch/wait/release/revive/result/attach/doctor)."""
    from codeagent.oracle import (
        cmd_oracle_ask,
        cmd_oracle_attach,
        cmd_oracle_doctor,
        cmd_oracle_list,
        cmd_oracle_release,
        cmd_oracle_result,
        cmd_oracle_revive,
        cmd_oracle_start,
        cmd_oracle_status,
        cmd_oracle_wait,
        cmd_oracle_watch,
    )

    cmd = args.ora_cmd
    if cmd is None:
        print("oracle: missing subcommand. Try: aimeshchat oracle start|ask|status|list|watch|wait|release|revive|result|attach|doctor", file=sys.stderr)
        return 1
    handlers = {
        "start": cmd_oracle_start,
        "ask": cmd_oracle_ask,
        "status": cmd_oracle_status,
        "list": cmd_oracle_list,
        "watch": cmd_oracle_watch,
        "wait": cmd_oracle_wait,
        "release": cmd_oracle_release,
        "revive": cmd_oracle_revive,
        "result": cmd_oracle_result,
        "attach": cmd_oracle_attach,
        "doctor": cmd_oracle_doctor,
    }
    if cmd in ("start", "ask"):
        # P0-B/P1-B: 去 role——--agent 无模型语义（仅兼容占位，传了打弃用
        # 告警）；不再强制显式 --model（模型经 ExecutionSpec 解析链：显式
        # --model → runtime.context → execution-context 继承），全部缺失时
        # 由 cmd_oracle_start 报错要求 --model。
        _warn_deprecated_agent(args)
    return handlers[cmd](args)


def _cmd_job(args: argparse.Namespace) -> int:
    """Dispatch ``job`` subcommands (list / status / wait)."""
    from codeagent.job import get_manager

    cmd = args.job_cmd
    if cmd is None:
        print("error: specify a job subcommand: list | status <id> | wait <id>", file=sys.stderr)
        return 1

    mgr = get_manager()

    if cmd == "list":
        jobs = mgr.list_jobs()
        if not jobs:
            print("no jobs found")
            return 0
        for j in jobs:
            print(f"{j.job_id}  {j.status:8s}  {j.created_at}  {j.task[:60]}")
        return 0

    if cmd == "status":
        try:
            info = mgr.status(args.job_id)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(info.to_dict(), indent=2, ensure_ascii=False))
        return 0

    if cmd == "wait":
        try:
            info = mgr.wait(args.job_id, timeout=args.timeout)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(info.to_dict(), indent=2, ensure_ascii=False))
        return info.returncode if info.returncode is not None else 0

    print(f"job: unknown subcommand {cmd!r}", file=sys.stderr)
    return 1


def _cmd_session(args: argparse.Namespace) -> int:
    """A6: session storage lifecycle — ``session clean --older-than N``.

    Deletes whole sessions (history/archive/events/outbox) older than N
    days via MailboxStore.clean_older_than; sessions with an active park
    lease are skipped and reported.
    """
    cmd = getattr(args, "session_cmd", None)
    if cmd is None:
        print("error: specify a session subcommand. Try: aimeshchat session clean --older-than 30",
              file=sys.stderr)
        return 1
    if cmd == "clean":
        store = MailboxStore()
        result = store.clean_older_than(args.older_than)
        if getattr(args, "json_output", False):
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            for sid in result["removed"]:
                print(f"removed {sid}")
            for sid in result["skipped"]:
                print(f"skipped {sid} (active park lease / locked)")
            print(f"clean: removed {len(result['removed'])}, skipped {len(result['skipped'])}")
        return 0
    print(f"session: unknown subcommand {cmd!r}", file=sys.stderr)
    return 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    handlers = {
        "run": _cmd_run,
        "route": _cmd_route,
        "sessions": _cmd_sessions,
        "session": _cmd_session,
        "ssh": _cmd_ssh,
        "mailbox": _cmd_mailbox,
        "artifact": _cmd_artifact,
        "swarm": _cmd_swarm,
        "park": _cmd_park,
        "gateway": _cmd_gateway,
        "events": _cmd_events,
        "runtime": _cmd_runtime,
        "oracle": _cmd_oracle,
        "job": _cmd_job,
    }
    # args.command is guaranteed to be one of the registered subcommands:
    # argparse rejects unknown names, and ``None`` was handled above.
    handler = handlers[args.command]

    try:
        return handler(args)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
