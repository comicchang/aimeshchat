"""TCP daemon CLI subcommands.

Provides ``start``, ``stop``, and ``status`` sub-commands for the
:class:`~codeagent.tcp.server.MailboxDaemon`.  The daemon is spawned as
a background process (unless ``--foreground`` is passed) and its PID is
recorded in a PID file so that ``stop`` and ``status`` can locate it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path

from codeagent.constants import TCP_DAEMON_PORT

logger = logging.getLogger(__name__)

# ── PID file helper ─────────────────────────────────────────────────────

DEFAULT_PID_FILE = Path.home() / ".config" / "codeagent" / "mailbox-daemon.pid"


def _pid_path(pid_file: str | None) -> Path:
    """Resolve PID file path."""
    return Path(pid_file) if pid_file else DEFAULT_PID_FILE


def _read_pid(path: Path) -> int | None:
    """Read PID from *path*. Returns None if the file is missing or invalid."""
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _write_pid(path: Path, pid: int) -> None:
    """Atomically write *pid* to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(str(pid))
    os.replace(str(tmp), str(path))


def _remove_pid(path: Path) -> None:
    """Remove PID file if it exists."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _process_alive(pid: int) -> bool:
    """Return True if a process with *pid* is currently running."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# ── daemon process (runs inside child) ──────────────────────────────────


def _run_daemon(host: str, port: int, mailbox_root: str | None) -> None:
    """Entry-point for the daemon child process.

    Sets up the asyncio event loop, starts :class:`MailboxDaemon`, and
    blocks until a shutdown signal is received.
    """
    from codeagent.mailbox.store import MailboxStore
    from codeagent.tcp.server import MailboxDaemon
    from codeagent.tcp.spool import SpoolStore

    root = Path(mailbox_root) if mailbox_root else None
    mailbox_store = MailboxStore(root=root)
    spool_store = SpoolStore(root or Path.home() / ".config" / "codeagent" / "mailbox")
    daemon = MailboxDaemon(host, port, mailbox_store, spool_store)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("shutdown signal received")
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    async def _main() -> None:
        addr = await daemon.start()
        logger.info("daemon ready on %s:%d", addr[0], addr[1])
        await shutdown_event.wait()
        await daemon.stop()

    try:
        loop.run_until_complete(_main())
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


# ── subcommand handlers ─────────────────────────────────────────────────


def _cmd_start(args: argparse.Namespace) -> int:
    """Start the mailbox daemon as a background process."""
    pid_file = _pid_path(args.pid_file)
    pid = _read_pid(pid_file)

    # Already running?
    if pid is not None and _process_alive(pid):
        print(f"daemon already running (pid={pid})")
        return 0

    # Stale PID file — clean up
    if pid is not None:
        _remove_pid(pid_file)

    host = args.host
    port = args.port

    if args.foreground:
        # Foreground mode — run directly (useful for debugging / systemd)
        _run_daemon(host, port, args.mailbox_root)
        return 0

    # Background mode — fork
    child_pid = os.fork()
    if child_pid == 0:
        # Detach from terminal
        os.setsid()
        try:
            _run_daemon(host, port, args.mailbox_root)
        except Exception as exc:
            # Write error to stderr (will appear on parent's terminal
            # briefly before the process exits)
            print(f"daemon error: {exc}", file=sys.stderr)
            os._exit(1)
        os._exit(0)

    # Parent: wait a moment for the child to start, then verify
    time.sleep(0.5)
    if _process_alive(child_pid):
        _write_pid(pid_file, child_pid)
        print(f"daemon started (pid={child_pid}, {host}:{port})")
        return 0
    else:
        print("daemon failed to start", file=sys.stderr)
        return 1


def _cmd_stop(args: argparse.Namespace) -> int:
    """Stop the running mailbox daemon."""
    pid_file = _pid_path(args.pid_file)
    pid = _read_pid(pid_file)

    if pid is None:
        print("daemon not running (no PID file)")
        return 0

    if not _process_alive(pid):
        print("daemon not running (stale PID file)")
        _remove_pid(pid_file)
        return 0

    try:
        os.kill(pid, signal.SIGTERM)
    except PermissionError:
        print(f"permission denied: cannot kill pid {pid}", file=sys.stderr)
        return 1

    # Wait for graceful shutdown (up to 5 seconds)
    for _ in range(50):
        if not _process_alive(pid):
            break
        time.sleep(0.1)

    if _process_alive(pid):
        print(f"warning: daemon (pid={pid}) still alive after SIGTERM", file=sys.stderr)
        return 1

    _remove_pid(pid_file)
    print(f"daemon stopped (pid={pid})")
    return 0


def _probe_daemon(host: str, port: int, timeout: float = 2.0) -> dict | None:
    """Connect to the daemon and send a ``daemon-status`` request.

    Returns the parsed JSON response dict on success, or ``None`` if the
    daemon is unreachable.
    """
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except (ConnectionRefusedError, TimeoutError, OSError):
        return None

    try:
        req = json.dumps({"command": "daemon-status"}).encode() + b"\n"
        sock.sendall(req)
        buf = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            if b"\n" in buf:
                break
        if not buf:
            return None
        return json.loads(buf.decode().strip())
    except (json.JSONDecodeError, OSError):
        return None
    finally:
        sock.close()


def _cmd_status(args: argparse.Namespace) -> int:
    """Show the current daemon status."""
    pid_file = _pid_path(args.pid_file)
    pid = _read_pid(pid_file)

    if pid is None:
        print(json.dumps({"running": False, "reason": "no PID file"}, indent=2))
        return 0

    if not _process_alive(pid):
        _remove_pid(pid_file)
        print(json.dumps({"running": False, "reason": "process not found"}, indent=2))
        return 0

    # Process is alive — try to get detailed status from the daemon
    result = _probe_daemon(args.host, args.port)
    if result is not None:
        result["pid"] = pid
        print(json.dumps(result, indent=2))
    else:
        print(json.dumps({
            "running": True,
            "pid": pid,
            "host": args.host,
            "port": args.port,
            "warning": "could not probe daemon (port may be wrong)",
        }, indent=2))
    return 0


# ── client helper ────────────────────────────────────────────────────────


def send_to_daemon(
    mailbox_args: list[str],
    mailbox_root: str | None = None,
    host: str = "127.0.0.1",
    port: int = TCP_DAEMON_PORT,
    timeout: float = 30.0,
) -> tuple[int, str, str]:
    """Send a mailbox request to the daemon over a TCP socket.

    Returns ``(exit_code, stdout, stderr)``.
    """
    import socket as _socket

    try:
        sock = _socket.create_connection((host, port), timeout=timeout)
    except (ConnectionRefusedError, TimeoutError, OSError) as exc:
        return 1, "", f"error: cannot connect to daemon: {exc}\n"

    try:
        req = {
            "command": "mailbox",
            "args": mailbox_args,
        }
        if mailbox_root:
            req["mailbox_root"] = mailbox_root
        data = json.dumps(req).encode() + b"\n"
        sock.sendall(data)

        # Read response (one JSON line)
        buf = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sock.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                chunk = sock.recv(65536)
            except _socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            if b"\n" in buf:
                break

        if not buf:
            return 1, "", "error: no response from daemon\n"
        resp = json.loads(buf.decode().strip())
        return resp.get("exit_code", 1), resp.get("stdout", ""), resp.get("stderr", "")
    except (json.JSONDecodeError, OSError) as exc:
        return 1, "", f"error: daemon communication failed: {exc}\n"
    finally:
        sock.close()


# ── entry-point ─────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """Entry-point for ``mailbox-daemon`` sub-commands."""
    p = argparse.ArgumentParser(
        "mailbox-daemon", description="TCP mailbox daemon management",
    )
    sub = p.add_subparsers(dest="subcmd")

    # start
    start_p = sub.add_parser("start", help="Start the daemon")
    start_p.add_argument("--host", default="127.0.0.1", help="Bind address")
    start_p.add_argument("--port", type=int, default=TCP_DAEMON_PORT, help="Bind port")
    start_p.add_argument("--foreground", action="store_true", help="Run in foreground")
    start_p.add_argument("--pid-file", help="Override PID file path")
    start_p.add_argument("--mailbox-root", help="Override MAILBOX_ROOT")

    # stop
    stop_p = sub.add_parser("stop", help="Stop the daemon")
    stop_p.add_argument("--pid-file", help="Override PID file path")

    # status
    status_p = sub.add_parser("status", help="Show daemon status")
    status_p.add_argument("--host", default="127.0.0.1", help="Daemon host")
    status_p.add_argument("--port", type=int, default=TCP_DAEMON_PORT, help="Daemon port")
    status_p.add_argument("--pid-file", help="Override PID file path")

    args = p.parse_args(argv)

    if args.subcmd is None:
        p.print_help()
        return 1

    handlers = {
        "start": _cmd_start,
        "stop": _cmd_stop,
        "status": _cmd_status,
    }
    return handlers[args.subcmd](args)
