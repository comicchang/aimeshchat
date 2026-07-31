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
from codeagent.transport.ssh import SSHTransport

log = logging.getLogger(__name__)


def _get_transport(host: HostSpec, repo_map=None):
    """Select transport based on host.transport field."""
    if host.transport == "relay-login":
        relay_zsh = getattr(repo_map, 'relay_zsh', '') if repo_map else ''
        if not relay_zsh:
            raise ValueError(f"host '{host.name}' uses relay-login but relay_zsh not configured")
        from codeagent.transport.relay import RelayTransport
        return RelayTransport(relay_zsh)
    return SSHTransport()


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

    return p


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

    # Remote host specified — SSH wire transport.
    # file mode (or auto fallback): SSH transport
    from codeagent.config.repo_map import load_repo_map
    from codeagent.domain import HostSpec, resolve_is_local
    from codeagent.transport.control_master import ControlMaster

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

    # Remote via SSH — use wire protocol over stdin JSONL
    from codeagent.constants import DEFAULT_MAILBOX_TIMEOUT
    from codeagent.transport.ssh import _run_ssh_mailbox
    from codeagent.wire.protocol import make_mailbox_request

    cm = ControlMaster(host_spec.ssh_alias)
    if not cm.is_alive():
        cm.create()

    req = make_mailbox_request(
        args=mailbox_args,
        mailbox_root=mailbox_root or "",
    )
    ssh_cmd = cm.ssh_cmd("codeagent-remote-exec")

    exit_code, stdout, stderr = _run_ssh_mailbox(ssh_cmd, req, timeout=DEFAULT_MAILBOX_TIMEOUT)
    if stdout:
        print(stdout, end="")
    if stderr:
        print(stderr, end="", file=sys.stderr)
    return exit_code


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
