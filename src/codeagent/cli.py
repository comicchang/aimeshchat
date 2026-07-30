"""CLI facade — unified entry point for all codeagent commands."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from codeagent.config.repo_map import load_repo_map
from codeagent.domain import RunRequest, RunResult, current_hostname
from codeagent.routing.resolver import resolve_target
from codeagent.runners.go_wrapper import GoWrapperRunner
from codeagent.runners.omp import OMPRunner
from codeagent.session.registry import SessionRegistry
from codeagent.transport.local import LocalTransport
from codeagent.transport.ssh import SSHTransport


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser("codeagent", description="Multi-host code agent orchestration")
    p.add_argument("--version", "-v", action="version", version="%(prog)s 0.1.0")

    sub = p.add_subparsers(dest="command")

    # run (default)
    run_p = sub.add_parser("run", help="Execute a task")
    run_p.add_argument("task", nargs="?", help="Task text (or stdin)")
    run_p.add_argument("workdir", nargs="?", default="", help="Working directory")
    run_p.add_argument("--host", help="Execute on remote host")
    run_p.add_argument("--backend", help="Backend (codex/claude/gemini/opencode/omp)")
    run_p.add_argument("--agent", help="Agent preset name")
    run_p.add_argument("--model", help="Model override")
    run_p.add_argument("--skills", help="Comma-separated skill names")
    run_p.add_argument("--session-key", help="Explicit session namespace")
    run_p.add_argument("--new-session", action="store_true", help="Force new session")
    run_p.add_argument("--no-auto-resume", action="store_true", help="Disable auto-resume")
    run_p.add_argument("--skip-permissions", action="store_true", default=True)
    run_p.add_argument("--output", help="Write structured JSON to file")

    # route
    route_p = sub.add_parser("route", help="Route task via repo-map")
    route_p.add_argument("subcmd", nargs="?", choices=["list", "where"], help="Sub-command")
    route_p.add_argument("topic_or_task", nargs="?", help="Topic name or task text")
    route_p.add_argument("--repo", type=int, default=0, help="Repo index")
    route_p.add_argument("--backend", help="Backend override")
    route_p.add_argument("--agent", help="Agent preset")
    route_p.add_argument("--model", help="Model override")
    route_p.add_argument("--raw", action="store_true", help="No route prompt wrapping")
    route_p.add_argument("--json", action="store_true", dest="json_output", help="JSON output")
    route_p.add_argument("--dry-run", action="store_true", help="Show routing decision only")
    route_p.add_argument("--new-session", action="store_true")
    route_p.add_argument("--no-auto-resume", action="store_true")

    # sessions
    sess_p = sub.add_parser("sessions", help="Manage session registry")
    sess_sub = sess_p.add_subparsers(dest="sess_cmd")
    ls_p = sess_sub.add_parser("list")
    ls_p.add_argument("--host", help="Filter by host")
    ls_p.add_argument("--topic", help="Filter by topic")
    show_p = sess_sub.add_parser("show")
    show_p.add_argument("key", help="Session key")
    reset_p = sess_sub.add_parser("reset")
    reset_p.add_argument("key", help="Session key to reset")
    bind_p = sess_sub.add_parser("bind")
    bind_p.add_argument("--key", required=True)
    bind_p.add_argument("--id", required=True, dest="session_id")

    # ssh
    ssh_p = sub.add_parser("ssh", help="Manage SSH connections")
    ssh_sub = ssh_p.add_subparsers(dest="ssh_cmd")
    warm_p = ssh_sub.add_parser("warm", help="Pre-establish connections")
    warm_p.add_argument("hosts", nargs="*", help="Host aliases")
    ssh_sub.add_parser("status", help="Show connection status")
    stop_p = ssh_sub.add_parser("stop", help="Close connections")
    stop_p.add_argument("hosts", nargs="*", help="Host aliases")

    return p


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
        skills=args.skills,
        skip_permissions=args.skip_permissions,
        session_key=args.session_key,
        new_session=args.new_session,
        no_auto_resume=args.no_auto_resume,
        host=args.host,
    )

    repo_map = load_repo_map()
    registry = SessionRegistry()
    ssh_transport = SSHTransport()
    local_transport = LocalTransport()

    target = resolve_target(request, repo_map)

    # Auto-resume check
    session_id = None
    if not request.new_session and not request.no_auto_resume:
        key = request.session_key or registry.compute_key(request, target)
        record = registry.lookup(key)
        if record and record.status == "active":
            session_id = record.session_id
            print(f"[codeagent] resuming session {session_id[:12]}... (key={key})", file=sys.stderr)

    # Select runner
    backend = request.backend or "opencode"
    if backend == "omp":
        runner = OMPRunner()
    else:
        runner = GoWrapperRunner()

    # Execute
    if target.is_local:
        result = local_transport.execute(runner, request, target, session_id=session_id)
    else:
        ssh_transport.warm(target.ssh_alias)
        result = ssh_transport.execute(runner, request, target, session_id=session_id)

    # Record session
    if result.session_id:
        key = request.session_key or registry.compute_key(request, target)
        registry.upsert(key, result, request, target)

    # Output
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


def _cmd_route(args: argparse.Namespace) -> int:
    repo_map = load_repo_map()

    if args.subcmd == "list":
        for name, spec in sorted(repo_map.topics.items()):
            hosts = [r.host for r in spec.repos]
            print(f"  {name:40s} [{', '.join(hosts)}] {spec.description}")
        return 0

    if args.subcmd == "where":
        topic = repo_map.topic(args.topic_or_task)
        print(f"Topic: {topic.name}")
        print(f"Description: {topic.description}")
        for i, r in enumerate(topic.repos):
            host = repo_map.hosts.get(r.host)
            local = " [LOCAL]" if host and resolve_is_local(host) else ""
            print(f"  [{i}] {r.host}:{r.path}{local}  {r.note}")
        return 0

    # Route execution
    task = args.topic_or_task or sys.stdin.read().strip()
    if not task:
        print("error: no task", file=sys.stderr)
        return 1

    request = RunRequest(
        task=task,
        topic=args.topic_or_task,
        repo_index=args.repo,
        backend=args.backend,
        agent=args.agent,
        model=args.model,
        new_session=args.new_session,
        no_auto_resume=args.no_auto_resume,
        raw=args.raw,
    )

    if args.dry_run:
        target = resolve_target(request, repo_map)
        print(f"Dry run: {target.host.name}:{target.workdir} (local={target.is_local})")
        return 0

    return _cmd_run_from_request(request, repo_map)


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
        else:
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
        hosts = args.hosts or []
        for h in hosts:
            ok = transport.warm(h)
            print(f"  {h}: {'ok' if ok else 'failed'}")
        return 0

    if args.ssh_cmd == "status":
        for name, sock in transport.list_sockets():
            alive = transport.check(name)
            print(f"  {name}: {'alive' if alive else 'dead'} ({sock})")
        return 0

    if args.ssh_cmd == "stop":
        hosts = args.hosts or []
        for h in hosts:
            transport.stop(h)
            print(f"  {h}: stopped")
        return 0

    return 0


def _cmd_run_from_request(request: RunRequest, repo_map) -> int:
    """Shared run logic for route and direct run."""
    registry = SessionRegistry()
    ssh_transport = SSHTransport()
    local_transport = LocalTransport()

    target = resolve_target(request, repo_map)

    session_id = None
    if not request.new_session and not request.no_auto_resume:
        key = request.session_key or registry.compute_key(request, target)
        record = registry.lookup(key)
        if record and record.status == "active":
            session_id = record.session_id

    backend = request.backend or "opencode"
    runner = OMPRunner() if backend == "omp" else GoWrapperRunner()

    if target.is_local:
        result = local_transport.execute(runner, request, target, session_id=session_id)
    else:
        ssh_transport.warm(target.ssh_alias)
        result = ssh_transport.execute(runner, request, target, session_id=session_id)

    if result.session_id:
        key = request.session_key or registry.compute_key(request, target)
        registry.upsert(key, result, request, target)

    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.returncode


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        # Default: treat as run
        args.command = "run"
        args.task = None
        args.workdir = ""
        args.host = None
        args.backend = None
        args.agent = None
        args.model = None
        args.skills = None
        args.session_key = None
        args.new_session = False
        args.no_auto_resume = False
        args.skip_permissions = True
        args.output = None

    handlers = {
        "run": _cmd_run,
        "route": _cmd_route,
        "sessions": _cmd_sessions,
        "ssh": _cmd_ssh,
    }
    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        return 1
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
