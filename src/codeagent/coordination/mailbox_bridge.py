"""Mailbox subcommand — cross-host mailbox dispatch via SSH.

Local: subprocess.run(["mailbox", ...])
Remote: SSH to host, execute "mailbox ..." via ControlMaster
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Optional

from codeagent.domain import HostSpec, resolve_is_local


def _run_local(args: list[str], mailbox_root: Optional[str] = None) -> tuple[int, str, str]:
    """Run mailbox CLI locally."""
    import os
    env = {**os.environ}
    if mailbox_root:
        env["MAILBOX_ROOT"] = mailbox_root
    r = subprocess.run(["mailbox"] + args, capture_output=True, text=True, env=env, timeout=30)
    return r.returncode, r.stdout, r.stderr


def _run_remote(host: HostSpec, args: list[str], mailbox_root: Optional[str] = None) -> tuple[int, str, str]:
    """Run mailbox CLI on remote host via SSH."""
    from codeagent.transport.control_master import ControlMaster
    cm = ControlMaster(host.ssh_alias)
    if not cm.is_alive():
        cm.start()

    cmd_parts = ["mailbox"] + args
    remote_cmd = " ".join(cmd_parts)
    if mailbox_root:
        remote_cmd = f"MAILBOX_ROOT={mailbox_root} {remote_cmd}"

    ssh_cmd = cm.ssh_cmd("sh", "-c", remote_cmd)
    r = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout, r.stderr


def run_mailbox(
    args: list[str],
    host: Optional[str] = None,
    mailbox_root: Optional[str] = None,
) -> tuple[int, str, str]:
    """Dispatch mailbox command to local or remote host."""
    if not host:
        return _run_local(args, mailbox_root)

    # Resolve host from config
    from codeagent.config.repo_map import load_repo_map
    try:
        repo_map = load_repo_map()
        host_spec = repo_map.hosts.get(host)
    except FileNotFoundError:
        host_spec = None

    if host_spec is None:
        host_spec = HostSpec(
            name=host, ssh_alias=host, hostnames=(host,),
            description="ad-hoc host",
        )

    if resolve_is_local(host_spec):
        return _run_local(args, mailbox_root)
    return _run_remote(host_spec, args, mailbox_root)


def add_mailbox_subparser(sub: argparse._SubParsersAction) -> None:
    """Add 'mailbox' subcommand to CLI parser."""
    p = sub.add_parser("mailbox", help="Cross-host mailbox operations")
    p.add_argument("mailbox_args", nargs=argparse.REMAINDER,
                   help="Arguments passed to mailbox CLI (e.g., send --session s1 --from m --to w --kind TASK ...)")
    p.add_argument("--host", help="Target host (omit for local)")
    p.add_argument("--mailbox-root", help="Override MAILBOX_ROOT")


def cmd_mailbox(args: argparse.Namespace) -> int:
    """Handle 'codeagent mailbox' subcommand."""
    mailbox_args = args.mailbox_args
    if not mailbox_args:
        # Show mailbox help
        r = _run_local(["--help"])
        print(r[1])
        return r[0]

    rc, stdout, stderr = run_mailbox(
        mailbox_args,
        host=getattr(args, "host", None),
        mailbox_root=getattr(args, "mailbox_root", None),
    )
    if stdout:
        print(stdout, end="")
    if stderr:
        print(stderr, end="", file=sys.stderr)
    return rc
