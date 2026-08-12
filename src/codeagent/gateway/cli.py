"""Gateway + events CLI handlers — start/ensure/status/stop/serve/rpc and events watch.

- ``gateway start``  — spawn the gateway inside a private tmux pane (idempotent:
  an already-handshaking UDS returns success; a stale socket is only removed
  when same-UID, connect fails AND the tmux session is not alive).
- ``gateway ensure`` — (P3) verify a remote host's aimeshchat/wire/tmux, then
  start its gateway over SSH.
- ``gateway serve``  — foreground gateway process (the tmux pane's command).
- ``gateway rpc --stdio`` — SSH-bounded control: one request in, one response out.
- ``events watch``   — poll events.list; only the OBSERVATION connection has a
  timeout, the task itself has no hard timeout.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

from codeagent.constants import STREAM_HEARTBEAT_INTERVAL
from codeagent.gateway.client import GatewayClient, rpc_stdio
from codeagent.gateway.events import control_socket_path
from codeagent.gateway.model import GatewayError
from codeagent.launchers.tmux import (
    TMUX_SESSION_NAME,
    ensure_tmux_server,
    tmux_cmd,
    tmux_socket_path,
)


def _gateway_running() -> bool:
    """True when the UDS handshake succeeds (a gateway is live)."""
    try:
        GatewayClient(timeout=2).call("capabilities.get")
        return True
    except Exception:
        return False


def cmd_gateway_start(args) -> int:
    """Start the local gateway inside the private tmux session (idempotent)."""
    if _gateway_running():
        print("gateway: already running")
        return 0

    sock = control_socket_path()
    # Stale socket policy: only remove when same-UID, connect fails, and the
    # tmux session is not alive.
    if sock.exists():
        same_uid = _socket_owner_is_self(sock)
        tmux_alive = _tmux_session_alive()
        if same_uid and not tmux_alive:
            print(f"gateway: removing stale socket {sock}")
            try:
                sock.unlink()
            except OSError:
                pass
        elif not same_uid:
            print(
                f"gateway: refusing to remove socket {sock} (same_uid={same_uid})",
                file=sys.stderr,
            )
            return 1

    if not ensure_tmux_server():
        print("gateway: cannot start private tmux server", file=sys.stderr)
        return 1

    # A "gateway"-named window that is NOT responding is stale — remove it
    # before spawning a fresh serve pane.
    stale = _find_gateway_pane()
    if stale:
        _tmux("kill-pane", "-t", stale)

    rc, out, err = _tmux_new_gateway_pane()
    if rc != 0:
        print(f"gateway: tmux pane creation failed: {err}", file=sys.stderr)
        return 1

    # Wait for the UDS to come up (bounded).
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if _gateway_running():
            print(f"gateway: started (socket={sock})")
            return 0
        time.sleep(0.3)
    print("gateway: started but not yet responding (check pane diagnostics)", file=sys.stderr)
    return 1


def _socket_owner_is_self(path: Path) -> bool:
    try:
        import pwd

        st = path.stat()
        return st.st_uid == os.getuid()
    except (OSError, AttributeError):
        return False


def _tmux_session_alive() -> bool:
    rc, _, _ = _tmux("has-session", "-t", TMUX_SESSION_NAME)
    return rc == 0


def _tmux(*args: str) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            tmux_cmd(*args), capture_output=True, text=True, timeout=10,
        )
        return r.returncode, r.stdout, r.stderr
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 1, "", str(exc)


def _find_gateway_pane() -> Optional[str]:
    """Find a pane running the gateway serve command (by window name)."""
    rc, out, _ = _tmux("list-panes", "-t", TMUX_SESSION_NAME, "-F", "#{window_name}|#{pane_id}")
    if rc != 0:
        return None
    for line in out.splitlines():
        wname, _, pane = line.partition("|")
        if wname == "gateway":
            return pane or None
    return None


def _tmux_new_gateway_pane() -> tuple[int, str, str]:
    """Create a dedicated 'gateway' window running ``codeagent gateway serve``."""
    import shlex

    serve_cmd = " ".join(shlex.quote(a) for a in [sys.executable, "-m", "codeagent.gateway.cli", "serve"])
    rc, out, err = _tmux(
        "new-window", "-t", f"{TMUX_SESSION_NAME}:", "-n", "gateway", "-P", "-F", "#{pane_id}",
    )
    if rc != 0:
        return rc, "", err
    pane = out.strip().splitlines()[0] if out.strip() else ""
    if not pane:
        return 1, "", "tmux returned no pane id"
    return _tmux("send-keys", "-t", pane, serve_cmd, "Enter")


def cmd_gateway_ensure(args) -> int:
    """Verify a remote host (aimeshchat/wire/tmux), then start its gateway over SSH.

    Remote wire < 2 → REMOTE_UPGRADE_REQUIRED (no legacy fallback).
    Missing OMP/OpenCode only disables that runtime capability.
    """
    host = args.host
    if not host:
        print("error: gateway ensure requires --host", file=sys.stderr)
        return 1

    from codeagent.config.repo_map import load_repo_map
    from codeagent.domain import HostSpec, resolve_is_local

    try:
        spec = load_repo_map().hosts.get(host)
    except FileNotFoundError:
        spec = None
    if spec is None:
        spec = HostSpec(name=host, ssh_alias=host, hostnames=(host,), description="ad-hoc host")

    if resolve_is_local(spec):
        return cmd_gateway_start(args)

    # wire version probe via ping
    try:
        from codeagent.transport.control_master import ControlMaster
        from codeagent.wire.protocol import WIRE_VERSION, decode_line, encode_line, make_ping

        cm = ControlMaster(host, ssh_bin="ssh")
        if not cm.is_alive():
            cm.create()
        ssh_cmd = cm.ssh_cmd("export PATH=$HOME/.local/bin:$PATH; aimeshchat-remote-exec")
        proc = subprocess.Popen(ssh_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out_b, err_b = proc.communicate(input=encode_line(make_ping()), timeout=30)
        remote_ver = 0
        for raw in out_b.decode("utf-8", errors="replace").splitlines():
            try:
                msg = decode_line(raw)
                if msg.type == "pong":
                    remote_ver = msg.payload.get("wire_version", 0)
            except ValueError:
                continue
        if remote_ver < 2:
            print(
                json.dumps({
                    "host": host,
                    "error": "REMOTE_UPGRADE_REQUIRED",
                    "remote_wire_version": remote_ver,
                    "required_wire_version": WIRE_VERSION,
                }, indent=2),
            )
            return 1
    except Exception as exc:
        print(f"error: remote wire probe failed: {exc}", file=sys.stderr)
        return 1

    # Start the remote gateway (idempotent — remote `gateway start` handles
    # its own tmux serve), then verify via bounded RPC.
    try:
        from codeagent.transport.control_master import ControlMaster

        cm = ControlMaster(host, ssh_bin="ssh")
        if not cm.is_alive():
            cm.create()
        start_cmd = "export PATH=$HOME/.local/bin:$PATH; aimeshchat gateway start"
        proc = subprocess.Popen(cm.ssh_cmd(start_cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out_b, err_b = proc.communicate(timeout=60)
        out_text = out_b.decode("utf-8", errors="replace") if isinstance(out_b, bytes) else (out_b or "")
        err_text = err_b.decode("utf-8", errors="replace") if isinstance(err_b, bytes) else (err_b or "")
        if "already running" not in out_text and "started" not in out_text:
            print(f"warning: remote gateway start output: {(out_text + err_text).strip()[:200]}", file=sys.stderr)
    except Exception as exc:
        print(f"error: remote gateway start failed: {exc}", file=sys.stderr)
        return 1

    try:
        from codeagent.gateway.remote import remote_gateway_call

        result = remote_gateway_call(host, "capabilities.get", {})
        print(f"gateway ensured on {host}: {json.dumps(result)}")
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def cmd_gateway_status(args) -> int:
    if not _gateway_running():
        print("gateway: not running")
        return 1
    try:
        caps = GatewayClient(timeout=3).call("capabilities.get")
    except GatewayError as exc:
        print(f"gateway: error: {exc.message}", file=sys.stderr)
        return 1
    print(json.dumps({
        "status": "running",
        "socket": str(control_socket_path()),
        "version": caps.get("version"),
        "runtimes": caps.get("runtimes", []),
    }, indent=2))
    return 0


def cmd_gateway_stop(args) -> int:
    """Stop the local gateway — kill the tmux window, remove the socket."""
    if not _gateway_running():
        print("gateway: not running")
        return 0
    try:
        GatewayClient(timeout=3).call("runtime.stop", {"runtime_id": "__shutdown__"})
    except GatewayError:
        pass
    rc, _, err = _tmux("kill-window", "-t", f"{TMUX_SESSION_NAME}:gateway")
    if rc != 0:
        rc2, _, err2 = _tmux("kill-session", "-t", TMUX_SESSION_NAME)
        if rc2 != 0:
            print(f"gateway: stop failed: {err or err2}", file=sys.stderr)
            return 1
    sock = control_socket_path()
    if sock.exists():
        try:
            sock.unlink()
        except OSError:
            pass
    print("gateway: stopped")
    return 0


def cmd_gateway_serve(args) -> int:
    """Foreground gateway process (the tmux pane command)."""
    from codeagent.gateway.server import GatewayServer

    server = GatewayServer()
    server.serve_forever()
    return 0


def cmd_gateway_rpc(args) -> int:
    """Bounded RPC: --stdio reads one request from stdin, writes one response."""
    if args.stdio:
        return rpc_stdio()
    method = args.method
    params = json.loads(args.params) if args.params else {}
    try:
        result = GatewayClient(timeout=args.timeout).call(method, params)
    except GatewayError as exc:
        print(json.dumps({"ok": False, "error": exc.to_dict()}, indent=2))
        return 1
    print(json.dumps({"ok": True, "result": result}, indent=2))
    return 0


# ── events watch ───────────────────────────────────────────────────────


def _watch_cursor_file(session_id: str, runtime_id: str, filters: Optional[list[str]] = None) -> Optional[Path]:
    """A4: persisted watch-cursor path.

    ``$XDG_STATE_HOME/aimeshchat/watch-cursor-<key>.json`` (default
    ``~/.local/state/aimeshchat/...``) where the key is the session_id when
    filtering by session, else runtime_id, else None — an unfiltered
    global stream is not persisted.

    P2-4: include a short hash of the filter set in the filename so that
    watchers on the same session with different filters do NOT clobber
    each other's cursor.
    """
    key = session_id or runtime_id or ""
    if not key:
        return None
    # P2-4: partition cursor file by (session|runtime, filters).
    if filters:
        import hashlib
        fhash = hashlib.sha256(",".join(sorted(filters)).encode("utf-8")).hexdigest()[:12]
        key = f"{key}-f{fhash}"
    base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    return base / "aimeshchat" / f"watch-cursor-{key}.json"


def _load_watch_cursor(session_id: str, runtime_id: str, filters: Optional[list[str]] = None) -> int:
    """A4: load the persisted cursor (0 when absent or corrupt)."""
    path = _watch_cursor_file(session_id, runtime_id, filters)
    if path is None or not path.exists():
        return 0
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("cursor", 0))
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        return 0


def _save_watch_cursor(session_id: str, runtime_id: str, cursor: int, filters: Optional[list[str]] = None) -> None:
    """A4: persist the cursor so a reconnect resumes where we left off.

    改进项1: include filter hash and watcher identity in the cursor JSON so
    a reader can validate that the persisted cursor belongs to the same
    filter set and watcher (hostname+pid). The filename itself already
    partitions by filter hash (P2-4); this adds content-level provenance.
    """
    path = _watch_cursor_file(session_id, runtime_id, filters)
    if path is None:
        return
    try:
        import hashlib as _hashlib
        import socket as _socket
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        # 改进项1: build provenance record
        fhash = ""
        if filters:
            fhash = _hashlib.sha256(",".join(sorted(filters)).encode("utf-8")).hexdigest()[:12]
        data = {
            "cursor": int(cursor),
            "filters": filters or [],
            "filter_hash": fhash,
            "watcher_id": f"{_socket.gethostname()}:{os.getpid()}",
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log.debug("events watch: cursor persist failed: %s", exc)


def _parse_exit_on(raw: Optional[str]) -> list[tuple[str, str]]:
    """A4: parse --exit-on specs — comma-separated ``KIND.STATE`` pairs.

    A terminal event matches when ``ev.kind == KIND`` and
    ``ev.payload.state == STATE`` (e.g. ``TASK_STATE.agent_end``,
    ``RUNTIME_STATE.stopped``). Malformed specs are skipped with a
    warning rather than failing the whole watch.
    """
    specs: list[tuple[str, str]] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        kind, sep, state = item.partition(".")
        if not sep or not kind or not state:
            print(f"events: warning: ignoring malformed --exit-on spec {item!r}", file=sys.stderr)
            continue
        specs.append((kind.strip(), state.strip()))
    return specs


def cmd_events_watch(args) -> int:
    """Poll events.list; only the observation connection has a timeout.

    The task itself has NO hard timeout unless bounded by --exit-on /
    --max-events / --duration — cancel/release is otherwise the only way
    to terminate the runtime; client exit / shell timeout / SSH drop
    never kills the gateway or runtime.

    A4: ``--exit-on KIND.STATE,...`` terminates with exit 0 on the first
    terminal event. The cursor is persisted under
    ``~/.local/state/aimeshchat/`` so a reconnect resumes automatically;
    an explicit ``--cursor`` overrides the persisted value.
    """
    client = GatewayClient(timeout=args.timeout)

    # A4: cursor — an explicit --cursor wins; otherwise resume from file.
    session_id = getattr(args, "session", None) or ""
    runtime_id = getattr(args, "runtime_id", None) or ""
    # P2-4: compute filters BEFORE cursor load so the filter-partitioned
    # cursor file can be located correctly.
    filters = getattr(args, "filters", None)
    filters = filters.split(",") if filters else None
    explicit_cursor = getattr(args, "cursor", None) not in (None, "")
    if explicit_cursor:
        cursor = int(getattr(args, "cursor") or 0)
    else:
        cursor = _load_watch_cursor(session_id, runtime_id, filters)
    # 改进项1: human-readable output by default; --jsonl is opt-in.
    jsonl = bool(getattr(args, "jsonl", False)) and not bool(getattr(args, "plain", False))
    exit_on = _parse_exit_on(getattr(args, "exit_on", None))
    max_events = int(getattr(args, "max_events", 0) or 0)
    duration = float(getattr(args, "duration", 0) or 0)

    started = time.monotonic()
    seen = 0
    while True:
        try:
            result = client.call("events.list", {
                "cursor": int(cursor or 0),
                "filters": filters,
                "limit": args.limit,
                "session_id": session_id,
                "runtime_id": runtime_id,
            })
        except GatewayError as exc:
            if jsonl:
                print(json.dumps({"type": "error", "message": exc.message}))
            else:
                print(f"events: error: {exc.message}", file=sys.stderr)
            return 1
        events = result.get("events", [])
        for ev in events:
            seen += 1
            if jsonl:
                print(json.dumps(ev, ensure_ascii=False))
            else:
                print(f"[{ev.get('event_id')}] {ev.get('kind')} {ev.get('runtime_id')} {ev.get('created_at')}")
            # A4: terminal event → exit 0.
            if exit_on:
                kind = ev.get("kind", "")
                state = (ev.get("payload") or {}).get("state", "")
                if (kind, state) in exit_on:
                    try:
                        _save_watch_cursor(session_id, runtime_id, int(result.get("cursor") or cursor or 0), filters)
                    except (TypeError, ValueError):
                        pass
                    return 0
        cursor = result.get("cursor", cursor)
        # A4: persist after every poll so a reconnect resumes exactly here.
        try:
            _save_watch_cursor(session_id, runtime_id, int(cursor), filters)
        except (TypeError, ValueError):
            pass
        sys.stdout.flush()
        # A4: --max-events / --duration hard bounds (safety net).
        if max_events and seen >= max_events:
            return 0
        if duration and time.monotonic() - started >= duration:
            return 0
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0


def main(argv: Optional[list[str]] = None) -> int:
    """Standalone entry for ``python -m codeagent.gateway.cli``.

    The tmux gateway pane runs ``... serve`` through this entry.
    """
    import argparse as _ap

    p = _ap.ArgumentParser(prog="aimeshchat gateway", description="Local gateway control")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("start", help="Start the local gateway (idempotent)")
    sub.add_parser("status", help="Show local gateway status")
    sub.add_parser("stop", help="Stop the local gateway")
    sub.add_parser("serve", help="Foreground gateway process (tmux pane command)")
    rpc_p = sub.add_parser("rpc", help="Bounded RPC")
    rpc_p.add_argument("--stdio", action="store_true")
    rpc_p.add_argument("method", nargs="?", default="")
    rpc_p.add_argument("--params", default="")
    rpc_p.add_argument("--timeout", type=float, default=15.0)

    args = p.parse_args(argv)
    if args.cmd is None:
        p.print_help()
        return 1
    ns = argparse.Namespace(
        gw_cmd=args.cmd, host=None, stdio=getattr(args, "stdio", False),
        method=getattr(args, "method", ""), params=getattr(args, "params", ""),
        timeout=getattr(args, "timeout", 15.0),
    )
    handlers = {
        "start": cmd_gateway_start,
        "status": cmd_gateway_status,
        "stop": cmd_gateway_stop,
        "serve": cmd_gateway_serve,
        "rpc": cmd_gateway_rpc,
    }
    return handlers[args.cmd](ns)


if __name__ == "__main__":
    sys.exit(main())
