"""Oracle CLI — persistent-context advisory sessions (hot/warm/cold).

``codeagent oracle start|ask|status|watch|release``

Native-first design (per operator direction):
  - OMP:     omp-config memory (memsearch/autoRecall/handoffSaveToDisk) +
             parked-revive; the runtime is a tmux-supervised interactive
             OMP process that can accept in-loop steer.
  - OpenCode: native session DB + ``--session`` continuation (the subagent
             ``task_id`` mechanism); oh-my-openagent adds NO extra session
             fields — it only passes the native backend session id through.

Methods: hot (in-loop send to a live runtime) → warm (resume native backend
session) → cold (snapshot reconstruction). ``status`` must report the ACTUAL
method used — never claim a hot revive that did not happen.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional
from uuid import uuid4

from codeagent.gateway.client import GatewayClient
from codeagent.gateway.events import control_socket_path
from codeagent.gateway.model import GatewayError
from codeagent.mailbox.store import MailboxStore, RequestLedger, resolve_root
from codeagent.park.registry import ParkRegistry
from codeagent.park.router import park_revive
from codeagent.domain.park import Lifecycle, ParkManifest
from codeagent.runtime.base import CAP_WARM_RESUME
from codeagent.runtime.registry import RuntimeRegistry

_ORACLE_AGENT = "oracle"


def _kernel_and_store():
    from codeagent.cli import _get_swarm_kernel

    return _get_swarm_kernel()


def _gateway() -> GatewayClient:
    return GatewayClient(timeout=10)


def _review_sid(review_key: str) -> str:
    """Swarm session id for a review key (existing ora-* scheme)."""
    safe = review_key.replace(":", "-")[-12:]
    return f"ora-{safe}-{uuid4().hex[:10]}"


def _adopt_runtime(review_key: str, sid: str, handle, backend: str) -> None:
    """Adopt a spawned runtime into the local gateway (presence/status).

    Needed for runtimes WITHOUT a plugin handshake (opencode/generic); OMP
    runtimes are adopted by the plugin's runtime.register instead.
    """
    try:
        from codeagent.gateway.client import GatewayClient

        GatewayClient(timeout=10).call("runtime.register", {
            "session_id": sid,
            "agent_id": _ORACLE_AGENT,
            "runtime_id": handle.runtime_id,
            "review_key": review_key,
            "generation": handle.generation,
            "backend_session_id": handle.backend_session_id,
            "runtime": backend,
            "owner_pid": os.getpid(),
            "nonce": uuid4().hex[:12],
        })
    except Exception as exc:
        print(f"warning: gateway runtime adoption failed: {exc}", file=sys.stderr)


def _resolve_backend(agent: str, requested: str) -> str:
    """Oracle prefers OMP → OpenCode (warm_resume required)."""
    from codeagent.cli import _resolve_agent_backend

    return _resolve_agent_backend(agent, requested)


# ── OMP native memory config integration (B1) ─────────────────────────


def _omp_config_paths() -> list[Path]:
    """Candidate omp config locations (config.yml / common.yml)."""
    candidates = [
        Path(os.environ["OMP_CONFIG"]) if os.environ.get("OMP_CONFIG") else None,
        Path.home() / ".omp" / "agent" / "config.yml",
        Path.home() / ".omp" / "config" / "common.yml",
        Path.home() / ".config" / "omp" / "common.yml",
    ]
    return [p for p in candidates if p is not None and p.exists()]


def _parse_flat_yaml(path: Path) -> dict:
    """Minimal YAML-subset parser for the omp config format.

    Supports ``key: value`` and 2-space-indented children (this file's
    exact shape). Returns a dict of section → {key: value}. Never raises
    on malformed lines — verification is best-effort.
    """
    out: dict[str, dict[str, str]] = {}
    current = ""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if ":" not in stripped:
                continue
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()
            if line.startswith("  ") or line.startswith("\t"):
                if current:
                    out.setdefault(current, {})[key] = val
            else:
                current = key
                out.setdefault(key, {})
    except OSError:
        pass
    return out


def _merge_flat_yaml(path: Path, ensure: dict[str, dict[str, str]]) -> bool:
    """Append missing sections to a flat YAML file (with backup).

    Only appends keys that are entirely absent — never overwrites an
    existing value. Returns True when a merge happened.
    """
    backup = path.with_suffix(path.suffix + f".bak-{int(time.time())}")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    body = "\n".join(lines)
    merged = False
    additions: list[str] = []
    for section, kv in ensure.items():
        for key, val in kv.items():
            if f"{key}:" in body:
                continue  # already present — do not overwrite
            if section not in body:
                if additions and additions[-1].startswith(section + ":"):
                    pass
                else:
                    additions.append(f"{section}: ")
            additions.append(f"  {key}: {val}")
            merged = True
    if not merged:
        return False
    if path.exists():
        path.rename(backup)
    with open(path, "w") as f:
        f.write(body + ("\n" if body else "") + "\n".join(additions) + "\n")
    return True


def ensure_omp_memory_config() -> dict:
    """Verify/merge OMP native memory config (memsearch/autoRecall/handoff).

    Per the native-first directive: codeagent actively ensures the OMP
    native persistence knobs are on. Missing keys are merged (with a
    timestamped backup); existing values are never overwritten.
    """
    paths = _omp_config_paths()
    report: dict = {
        "config_path": str(paths[0]) if paths else "",
        "backend": "", "auto_recall": False, "handoff_save_to_disk": False,
        "merged": False, "missing": [],
    }
    if not paths:
        report["missing"] = ["omp config file not found (autoRecall/handoff unverified)"]
        return report
    path = paths[0]
    parsed = _parse_flat_yaml(path)
    report["backend"] = parsed.get("memory", {}).get("backend", "")
    auto_recall = parsed.get("memsearch", {}).get("autoRecall", "") or parsed.get("memory", {}).get("autoRecall", "")
    report["auto_recall"] = auto_recall.lower() == "true"
    handoff = parsed.get("compaction", {}).get("handoffSaveToDisk", "")
    report["handoff_save_to_disk"] = handoff.lower() == "true"
    if not report["auto_recall"]:
        report["missing"].append("memsearch.autoRecall=true")
    if not report["handoff_save_to_disk"]:
        report["missing"].append("compaction.handoffSaveToDisk=true")
    if not report["backend"]:
        report["missing"].append("memory.backend=memsearch")

    if report["missing"]:
        try:
            ensure = {}
            if not report["auto_recall"]:
                ensure.setdefault("memsearch", {})["autoRecall"] = "true"
            if not report["handoff_save_to_disk"]:
                ensure.setdefault("compaction", {})["handoffSaveToDisk"] = "true"
            if not report["backend"]:
                ensure.setdefault("memory", {})["backend"] = "memsearch"
            report["merged"] = _merge_flat_yaml(path, ensure)
        except OSError as exc:
            report["merge_error"] = str(exc)
    return report


# ── start ──────────────────────────────────────────────────────────────


def cmd_oracle_start(args: argparse.Namespace) -> int:
    """Create review/session/runtime and return the runtime id."""
    review_key = args.review_key
    backend = _resolve_backend(args.agent, args.backend)
    workdir = args.workdir or os.getcwd()

    # ── OMP native memory config (B1): verify/merge autoRecall + handoff ──
    memory_report = ensure_omp_memory_config()
    if memory_report["merged"]:
        print(f"omp memory config merged: {memory_report['config_path']}", file=sys.stderr)
    elif memory_report["missing"] and not memory_report["config_path"]:
        print(f"warning: {memory_report['missing'][0]}", file=sys.stderr)
    memory_env = {"OMP_MEMORY_CONFIG_PATH": memory_report["config_path"]} if memory_report["config_path"] else {}

    # ── Swarm session + agent registration ────────────────────────────
    kernel, store = _kernel_and_store()
    sid = _review_sid(review_key)
    try:
        kernel.create_session(sid, "manager", [_ORACLE_AGENT])
    except ValueError:
        pass  # already exists (idempotent start)
    from codeagent.swarm.model import AgentLocation, ExecutionMode, ReturnMode

    try:
        kernel.register(
            AgentLocation(
                agent_id=_ORACLE_AGENT,
                host_alias="__local__",
                backend=backend,
                execution_mode=ExecutionMode.MAILBOX_WORKER,
                return_mode=ReturnMode.BIDIRECTIONAL,
                mailbox_root=str(resolve_root()),
            ),
            session_id=sid,
        )
    except ValueError:
        pass

    # ── Park (idempotent; backend session filled after spawn) ─────────
    registry = ParkRegistry()
    existing = registry.lookup(review_key)

    # ── Initial TASK into the oracle inbox (plugin handshake picks it up
    #    as the initial task via pi.sendUserMessage). ─────────────────
    prompt = args.prompt or ""
    if prompt:
        try:
            from codeagent.mailbox.service import MailboxService

            MailboxService(store=store).send(
                session_id=sid,
                from_id="manager",
                to_id=_ORACLE_AGENT,
                subject="oracle-init",
                body=prompt,
                kind="TASK",
                run_id=f"run-{uuid4().hex[:10]}",
                request_id=f"req-{uuid4().hex[:10]}",
                require_ack=True,
            )
        except Exception as exc:
            print(f"warning: initial task dispatch failed: {exc}", file=sys.stderr)

    # ── Spawn runtime (native path) ───────────────────────────────────
    reg = RuntimeRegistry()
    handle = reg.spawn(backend, {
        "session_id": sid,
        "agent_id": _ORACLE_AGENT,
        "review_key": review_key,
        "workdir": workdir,
        "task": prompt,
        "model": args.model or "",
        "gateway_socket": str(control_socket_path()),
        "owner_pid": os.getpid(),
        "nonce": uuid4().hex[:12],
        "short_task": False,
        "env": memory_env,
    })

    # Persist backend session into the park manifest (authoritative).
    manifest = ParkManifest(
        review_key=review_key,
        swarm_session_id=sid,
        agent_type=args.agent,
        model=args.model or "",
        host="__local__",
        workdir=workdir,
        lifecycle=Lifecycle.HOT_PARKED,
        backend_session_id=handle.backend_session_id,
        created_at=(existing.created_at if existing else time.time()),
        last_activity_at=time.time(),
    )
    if existing is not None:
        registry.update(review_key, manifest)
    else:
        registry.acquire(review_key, manifest)

    # Adopt the runtime into the LOCAL gateway so presence/status work even
    # for runtimes without a plugin handshake (opencode/generic).
    _adopt_runtime(review_key, sid, handle, backend)

    print(json.dumps({
        "review_key": review_key,
        "session_id": sid,
        "runtime_id": handle.runtime_id,
        "backend": backend,
        "backend_session_id": handle.backend_session_id,
        "generation": handle.generation,
        "mode": handle.mode,
        "capabilities": sorted(handle.capabilities),
    }, indent=2))
    return 0


# ── ask (hot → warm → cold) ────────────────────────────────────────────


def cmd_oracle_ask(args: argparse.Namespace) -> int:
    """Deliver a prompt to the review's runtime: hot in-loop, warm resume,
    or cold reconstruction. Reports the ACTUAL method used."""
    review_key = args.review_key
    prompt = args.prompt or sys.stdin.read().strip()
    if not prompt:
        print("error: no prompt provided", file=sys.stderr)
        return 1

    # ── Hot: live runtime, in-loop send ───────────────────────────────
    try:
        info = _gateway().call("runtime.info", {"review_key": review_key})
        health = info.get("runtime_health", {})
        if info.get("status") == "active" and health.get("alive"):
            result = _gateway().call("runtime.send", {
                "runtime_id": info["runtime_id"],
                "from": "manager",
                "body": prompt,
                "kind": "TASK",
                "require_ack": True,
                "request_id": f"ask-{uuid4().hex[:10]}",
                "run_id": f"run-{uuid4().hex[:10]}",
            })
            print(json.dumps({
                "method": "hot",
                "review_key": review_key,
                "runtime_id": info["runtime_id"],
                "backend_session_id": info.get("backend_session_id", ""),
                "msg_id": result.get("msg_id", ""),
                "note": "in-loop send to live runtime (plugin steer)",
            }, indent=2))
            return 0
    except GatewayError as exc:
        if exc.code not in ("NOT_FOUND", "GATEWAY_DOWN", "GATEWAY_CONNECT_FAILED", "REMOTE_*"):
            pass  # fall through to warm/cold
    except Exception:
        pass

    # ── Warm: native backend session resume ───────────────────────────
    registry = ParkRegistry()
    manifest = registry.lookup(review_key)
    if manifest and manifest.backend_session_id:
        try:
            # Enqueue the prompt as a TASK FIRST so the resumed runtime's
            # plugin picks it up as the initial task (not a stale one).
            sid = manifest.swarm_session_id or _review_sid(review_key)
            try:
                from codeagent.mailbox.service import MailboxService

                MailboxService().send(
                    session_id=sid,
                    from_id="manager",
                    to_id=_ORACLE_AGENT,
                    subject="oracle-ask",
                    body=prompt,
                    kind="TASK",
                    run_id=f"run-{uuid4().hex[:10]}",
                    request_id=f"req-{uuid4().hex[:10]}",
                    require_ack=True,
                )
            except Exception as exc:
                print(f"warning: warm task enqueue failed: {exc}", file=sys.stderr)
            reg = RuntimeRegistry()
            handle = reg.spawn(_resolve_backend(args.agent, args.backend), {
                "session_id": sid,
                "agent_id": _ORACLE_AGENT,
                "review_key": review_key,
                "workdir": manifest.workdir or os.getcwd(),
                "task": prompt,
                "model": manifest.model or "",
                "backend_session_id": manifest.backend_session_id,
                "gateway_socket": str(control_socket_path()),
                "owner_pid": os.getpid(),
                "nonce": uuid4().hex[:12],
                "short_task": False,
            })
            # Update manifest backend id + lifecycle (in-place, no UNIQUE clash).
            registry.update(review_key, ParkManifest(
                review_key=review_key,
                swarm_session_id=manifest.swarm_session_id,
                agent_type=manifest.agent_type,
                model=manifest.model,
                host=manifest.host,
                workdir=manifest.workdir,
                lifecycle=Lifecycle.HOT_PARKED,
                backend_session_id=handle.backend_session_id,
                round=manifest.round + 1,
                created_at=manifest.created_at,
                last_activity_at=time.time(),
            ))
            _adopt_runtime(review_key, sid, handle, _resolve_backend(args.agent, args.backend))
            print(json.dumps({
                "method": "warm",
                "review_key": review_key,
                "runtime_id": handle.runtime_id,
                "old_backend_session_id": manifest.backend_session_id,
                "new_backend_session_id": handle.backend_session_id,
                "note": "native backend session resumed (OMP --resume / opencode --session)",
            }, indent=2))
            return 0
        except Exception as exc:
            print(json.dumps({
                "method": "warm_failed",
                "review_key": review_key,
                "error": str(exc),
                "note": "backend resume failed — degrading to cold",
            }, indent=2), file=sys.stderr)

    # ── Cold: snapshot reconstruction ─────────────────────────────────
    rv = park_revive(review_key, prompt)
    try:
        reg = RuntimeRegistry()
        handle = reg.spawn(_resolve_backend(args.agent, args.backend), {
            "session_id": _review_sid(review_key),
            "agent_id": _ORACLE_AGENT,
            "review_key": review_key,
            "workdir": manifest.workdir if manifest else os.getcwd(),
            "task": rv.context + "\n\n" + prompt,
            "model": (manifest.model if manifest else "") or (getattr(args, "model", "") or ""),
            "gateway_socket": str(control_socket_path()),
            "owner_pid": os.getpid(),
            "nonce": uuid4().hex[:12],
            "short_task": False,
        })
    except Exception as exc:
        print(json.dumps({
            "method": "cold_failed",
            "review_key": review_key,
            "error": str(exc),
        }, indent=2), file=sys.stderr)
        return 1
    _adopt_runtime(review_key, _review_sid(review_key), handle, _resolve_backend(args.agent, args.backend))
    print(json.dumps({
        "method": "cold",
        "review_key": review_key,
        "runtime_id": handle.runtime_id,
        "backend_session_id": handle.backend_session_id,
        "note": "snapshot reconstruction (no live/hot session)",
    }, indent=2))
    return 0


# ── status ─────────────────────────────────────────────────────────────


def cmd_oracle_status(args: argparse.Namespace) -> int:
    """Aggregate receipt/progress/park for a review key."""
    review_key = args.review_key
    out: dict = {"review_key": review_key}

    registry = ParkRegistry()
    manifest = registry.lookup(review_key)
    out["park"] = (
        {
            "lifecycle": manifest.lifecycle.value,
            "round": manifest.round,
            "backend_session_id": manifest.backend_session_id,
            "agent_type": manifest.agent_type,
            "last_activity_at": manifest.last_activity_at,
        }
        if manifest else None
    )

    # Runtime observability (EventStore aggregation).
    try:
        info = _gateway().call("runtime.info", {"review_key": review_key})
        out["runtime"] = {
            "runtime_id": info.get("runtime_id"),
            "status": info.get("status"),
            "elapsed_s": info.get("elapsed"),
            "backend_session_id": info.get("backend_session_id"),
            "generation": info.get("generation"),
            "last_event": info.get("last_event", {}),
            "tool_stats": info.get("tool_stats", {}),
            "runtime_health": info.get("runtime_health", {}),
        }
    except Exception as exc:
        out["runtime"] = {"error": str(exc)}

    # Request ledger (terminal truth).
    store = MailboxStore()
    reqs: list[dict] = []
    if manifest and manifest.swarm_session_id:
        try:
            events_root = store.session_dir(manifest.swarm_session_id) / _ORACLE_AGENT / "events"
            if events_root.is_dir():
                for req_dir in sorted(events_root.iterdir()):
                    if not req_dir.is_dir():
                        continue
                    lg = RequestLedger(store.session_dir(manifest.swarm_session_id), _ORACLE_AGENT)
                    for run_id, evs in lg._read_entries_all_runs(req_dir.name).items():
                        reqs.append({
                            "request_id": req_dir.name,
                            "run_id": run_id,
                            "states": [e["event"] for e in evs],
                            "terminal": next((e["event"] for e in evs if e["event"] in {"DONE", "BLOCKED", "CANCELLED", "EXPIRED", "UNKNOWN_STALE"}), ""),
                        })
        except Exception:
            reqs = []
    out["requests"] = reqs[-5:]  # most recent

    # Latest READ receipt / progress from history.
    if manifest and manifest.swarm_session_id:
        try:
            store = MailboxStore()
            history = store.read_history(manifest.swarm_session_id, limit=50)
            receipts = [m for m in history if m.get("kind") == "RECEIPT"]
            out["receipts"] = [
                {"msg_id": m.get("msg_id"), "reply_to": m.get("reply_to"), "from": m.get("from")}
                for m in receipts[-3:]
            ]
        except Exception:
            out["receipts"] = []

    print(json.dumps(out, indent=2))
    return 0


# ── result：从 OMP 会话转录提取最新回答 ───────────────────────────────


def _find_session_file(backend_session_id: str) -> Optional[Path]:
    """Locate the OMP session transcript file for a backend session id.

    Session files live under ~/.omp/agent/sessions/<dir>/*_<session_id>.jsonl
    where <dir> is the cwd-derived name (e.g. -src-codeagent-py). Falls back
    to scanning all session dirs when the derived dir misses.
    """
    sessions_root = Path.home() / ".omp" / "agent" / "sessions"
    if not sessions_root.is_dir():
        return None

    def _candidate_dirs() -> list[Path]:
        dirs: list[Path] = []
        for d in sessions_root.iterdir():
            if not d.is_dir():
                continue
            if any(f.name.endswith(f"_{backend_session_id}.jsonl") for f in d.iterdir()):
                dirs.append(d)
        return dirs

    candidates = _candidate_dirs()
    if not candidates:
        return None
    best: Optional[Path] = None
    for d in candidates:
        for f in sorted(d.glob(f"*_{backend_session_id}.jsonl")):
            if best is None or f.stat().st_mtime > best.stat().st_mtime:
                best = f
    return best


def _extract_assistant_messages(path: Path, max_messages: int = 1) -> list[str]:
    """Extract the last *max_messages* assistant text messages from a session JSONL."""
    msgs: list[str] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if obj.get("type") != "message":
                    continue
                m = obj.get("message", {})
                if m.get("role") != "assistant":
                    continue
                content = m.get("content", [])
                text = "".join(
                    c.get("text", "") for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                )
                if text.strip():
                    msgs.append(text)
    except OSError:
        return []
    return msgs[-max_messages:]


def cmd_oracle_result(args: argparse.Namespace) -> int:
    """Print the persist-oracle's latest response for a review key.

    Reads the OMP session transcript directly — no manual file extraction.
    ``--all`` prints every assistant message since the last checkpoint;
    default prints the latest one.
    """
    review_key = args.review_key
    manifest = ParkRegistry().lookup(review_key)
    backend_id = (manifest.backend_session_id if manifest else "") or ""
    if not backend_id:
        print(f"no backend session for review key {review_key!r} "
              "(oracle start 后才有)", file=sys.stderr)
        return 1

    path = _find_session_file(backend_id)
    if path is None:
        print(f"session transcript not found for {backend_id} "
              f"(under ~/.omp/agent/sessions/)", file=sys.stderr)
        return 1

    max_msgs = 0 if getattr(args, "all", False) else 1
    msgs = _extract_assistant_messages(path, max_messages=max_msgs or 10**6)
    if not msgs:
        print(f"(no assistant messages in {path.name})", file=sys.stderr)
        return 1
    for m in msgs:
        print(m)
        print("\n" + "─" * 60 + "\n")
    return 0


def cmd_oracle_watch(args: argparse.Namespace) -> int:
    """Watch a review's runtime events (cursor-resumable)."""
    review_key = args.review_key
    try:
        info = _gateway().call("runtime.info", {"review_key": review_key})
        runtime_id = info.get("runtime_id", "")
        session_id = info.get("session_id", "")
    except GatewayError as exc:
        print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return 1

    from codeagent.gateway.cli import cmd_events_watch

    ns = argparse.Namespace(
        ev_cmd="watch",
        session=session_id,
        request_id="",
        runtime_id=runtime_id,
        cursor=args.cursor,
        filters="",
        limit=200,
        interval=args.interval,
        timeout=args.timeout,
        jsonl=True,
    )
    return cmd_events_watch(ns)


# ── release ────────────────────────────────────────────────────────────


def cmd_oracle_release(args: argparse.Namespace) -> int:
    """Write terminal state, stop the runtime, release the park lease."""
    review_key = args.review_key
    registry = ParkRegistry()
    manifest = registry.lookup(review_key)

    # Stop the runtime (gateway) — the only way to terminate it.
    stopped = False
    if manifest:
        try:
            info = _gateway().call("runtime.info", {"review_key": review_key})
            rid = info.get("runtime_id")
            if rid:
                _gateway().call("runtime.stop", {"runtime_id": rid, "reason": "oracle release"})
                stopped = True
        except GatewayError as exc:
            print(f"warning: runtime stop failed: {exc.code}: {exc.message}", file=sys.stderr)

    # Release the park lease.
    if manifest:
        registry.release(review_key)

    print(json.dumps({
        "review_key": review_key,
        "runtime_stopped": stopped,
        "park_released": manifest is not None,
        "note": "terminal written; runtime identity cleanup complete",
    }, indent=2))
    return 0
