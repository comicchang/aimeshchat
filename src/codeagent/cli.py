"""CLI facade — unified entry point for all codeagent commands."""
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

from codeagent.config.repo_map import load_repo_map
from codeagent.domain import (
    HostSpec,
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
from codeagent.swarm.model import Address, AddressKind, AgentLocation, Envelope
from codeagent.mailbox.store import MailboxStore

log = logging.getLogger(__name__)

_router = TransportRouter()


def _get_transport(host: HostSpec, repo_map=None):
    """Select transport based on host.transport field."""
    return _router.get(host, repo_map)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser("codeagent", description="Multi-host code agent orchestration")
    p.add_argument("--version", "-v", action="version", version=f"%(prog)s {__version__}")

    sub = p.add_subparsers(dest="command")

    # run
    run_p = sub.add_parser("run", help="Execute a task")
    run_p.add_argument("task", nargs="?", help="Task text (or stdin)")
    run_p.add_argument("workdir", nargs="?", default="", help="Working directory")
    run_p.add_argument("--host", help="Execute on remote host")
    run_p.add_argument("--backend", default="opencode")
    run_p.add_argument("--agent")
    run_p.add_argument("--model")
    run_p.add_argument("--skills")
    run_p.add_argument("--session-key", help="Explicit session namespace")
    run_p.add_argument("--new-session", action="store_true")
    run_p.add_argument("--no-auto-resume", action="store_true")
    run_p.add_argument("--skip-permissions", action="store_true", default=False)
    run_p.add_argument("--output", help="Write structured JSON to file")

    # route
    route_p = sub.add_parser("route", help="Route task via repo-map")
    route_p.add_argument("args", nargs="*", help="list | where <topic> | <topic> [task...]")
    route_p.add_argument("--repo", type=int, default=0)
    route_p.add_argument("--backend", default="opencode")
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
    pull_p.add_argument("--remote-root", default="/tmp/codeagent-artifacts", help="Remote artifact root directory")
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

    # channel
    ch_p = swarm_sub.add_parser("channel", help="Send to a channel")
    ch_p.add_argument("session_id")
    ch_p.add_argument("channel_id", help="Channel identifier")
    ch_p.add_argument("--from", dest="sender", required=True, help="Sender agent ID")
    ch_p.add_argument("--kind", default="TASK")
    ch_p.add_argument("--subject", default="")
    ch_p.add_argument("--body", required=True)
    ch_p.add_argument("--attachment", action="append", default=[])

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
    loc = AgentLocation(
        agent_id=args.agent,
        host_alias=args.host,
        backend=args.backend,
    )
    reg = kernel.register(loc, args.session_id)
    if args.card:
        try:
            card = json.loads(args.card)
        except json.JSONDecodeError as exc:
            print(f"error: invalid --card JSON: {exc}", file=sys.stderr)
            return 1
        kernel.set_agent_card(args.session_id, args.agent, card)
    print(json.dumps({
        "agent_id": reg.agent_id,
        "session_id": reg.session_id,
        "host_alias": reg.location.host_alias,
        "backend": reg.location.backend,
    }, indent=2))
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


def _swarm_direct(kernel: SwarmKernel, args: argparse.Namespace) -> int:
    attachments = _parse_attachments(args.attachment) if args.attachment else []
    env = Envelope(subject=args.subject, body=args.body, kind=args.kind, attachments=attachments)
    receipt = kernel.direct(args.session_id, args.sender, args.to, env)
    print(json.dumps({
        "msg_id": receipt.msg_id,
        "status": receipt.status,
        "session_id": receipt.session_id,
        "target": receipt.target,
    }, indent=2))
    return 0


def _swarm_channel(kernel: SwarmKernel, args: argparse.Namespace) -> int:
    attachments = _parse_attachments(args.attachment) if args.attachment else []
    env = Envelope(subject=args.subject, body=args.body, kind=args.kind, attachments=attachments)
    receipts = kernel.channel(args.session_id, args.sender, args.channel_id, env)
    out = [{"msg_id": r.msg_id, "recipient": r.recipient, "status": r.status} for r in receipts]
    print(json.dumps(out, indent=2))
    return 0


def _swarm_broadcast(kernel: SwarmKernel, args: argparse.Namespace) -> int:
    env = Envelope(subject=args.subject, body=args.body, kind=args.kind)
    receipts = kernel.broadcast(args.session_id, args.sender, env)
    out = [{"msg_id": r.msg_id, "recipient": r.recipient, "status": r.status} for r in receipts]
    print(json.dumps(out, indent=2))
    return 0


def _swarm_notice(kernel: SwarmKernel, args: argparse.Namespace) -> int:
    env = Envelope(subject=args.subject, body=args.body, kind=args.kind)
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
        store = kernel._store
        msg = store.read(args.session_id, args.agent, owner=args.agent)
        if msg is None:
            print(f"error: no message to ack: {args.msg_id}", file=sys.stderr)
            return 1
        actual_id = msg.get("msg_id", "")
        if actual_id != args.msg_id:
            # Release the message back to inbox so it is not lost.
            store.release(args.session_id, args.agent, actual_id, owner=args.agent)
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


def _execute(request: RunRequest, target: Target, registry: SessionRegistry, repo_map=None) -> RunResult:
    """Core execution: local → LocalTransport, remote → SSH/RelayTransport.

    Session lifecycle (all under per-key lock):
      1. Compute namespace key
      2. Lookup existing session → get real backend session_id
      3. Mark starting
      4. Execute (transport receives real session_id)
      5. Mark observed/active/failed based on result
    """
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
        elif result.returncode == 0:
            registry.mark_active(ns_key)
        else:
            registry.mark_failed(ns_key)

        return result

    with registry.run_with_lock(ns_key):
        result = _run()

    return result


def _cmd_run(args: argparse.Namespace) -> int:
    task = args.task or sys.stdin.read().strip()
    if not task:
        print("error: no task provided", file=sys.stderr)
        return 1

    request = RunRequest(
        task=task,
        workdir=args.workdir,
        backend=args.backend,
        agent=args.agent,
        model=args.model,
        skills=getattr(args, 'skills', None),
        skip_permissions=args.skip_permissions,
        session_key=args.session_key,
        new_session=args.new_session,
        no_auto_resume=args.no_auto_resume,
        host=args.host,
    )

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
    result = _execute(request, target, registry, repo_map)

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
    from codeagent.domain.park import Lifecycle

    registry = ParkRegistry()
    cmd = args.park_cmd

    if cmd is None:
        print("park: missing subcommand. Try: codeagent park list|info|revive|release|sweep")
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
            import json
            print(json.dumps({
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
            }, indent=2))
        else:
            print(f"(no instance for '{args.review_key}')")

    elif cmd == "revive":
        result = park_revive(args.review_key, args.prompt or "")
        print(f"method={result.method} success={result.success}")
        print(result.context[:500])

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
        "ssh": _cmd_ssh,
        "mailbox": _cmd_mailbox,
        "artifact": _cmd_artifact,
        "swarm": _cmd_swarm,
        "park": _cmd_park,
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
