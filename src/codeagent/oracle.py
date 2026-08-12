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
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

log = logging.getLogger(__name__)

from codeagent.constants import ISO_TIMESTAMP_FORMAT
from codeagent.gateway.client import GatewayClient
from codeagent.gateway.events import control_socket_path
from codeagent.gateway.model import GatewayError
from codeagent.mailbox.store import MailboxStore, RequestLedger, resolve_root
from codeagent.park.registry import ParkRegistry
from codeagent.park.inject import build_cold_context
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


def _parse_retry_fallback_chains(path: Path) -> dict[str, list[str]]:
    """Parse ``retry.fallbackChains`` from an omp config file.

    The existing ``_parse_flat_yaml`` only handles 1-level nesting
    (section → key: value).  Fallback chains are 2-level nested:

        retry:
          fallbackChains:
            default:
              - model-a
              - model-b

    This targeted parser reads only the ``retry`` section, extracts
    ``fallbackChains.*`` sub-keys as list[str].  Returns e.g.
    ``{"default": ["model-a", "model-b"], "slow": ["model-c"]}``.

    P2: format-sensitive — relies on the exact YAML indentation layout
    (``retry:`` at col 0, ``fallbackChains:`` indented, list items 2-space
    further).  Non-standard formatting or comments inside the ``retry``
    block may cause early exit; safe because OMP's config writer uses
    canonical indentation.
    """
    chains: dict[str, list[str]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return chains

    # Phase 1: locate the retry: section boundaries.
    in_retry = False
    in_chains = False
    current_chain = ""
    retry_indent = -1
    chains_indent = -1

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # Detect section header: "retry:" at indent 0
        if not in_retry and stripped == "retry:" and not line[0].isspace():
            in_retry = True
            retry_indent = 0
            continue

        if not in_retry:
            continue

        # Compute indentation
        indent = len(line) - len(line.lstrip())

        # If we're back to top-level, retry section ended
        if indent <= retry_indent and stripped and not stripped.startswith("#"):
            # A new top-level key → exit retry section
            if not line[0].isspace():
                break
            continue

        # Inside retry: look for "fallbackChains:"
        if not in_chains and stripped == "fallbackChains:":
            in_chains = True
            chains_indent = indent
            continue

        if not in_chains:
            continue

        # Inside fallbackChains: chain names are 2-space indented under it
        if indent == chains_indent + 2 and stripped.endswith(":") and not stripped.startswith("-"):
            current_chain = stripped[:-1].strip()
            chains[current_chain] = []
            continue

        # List items: "- model-name" under current chain
        if current_chain and stripped.startswith("- "):
            model = stripped[2:].strip()
            if model:
                chains[current_chain].append(model)
            continue

        # Anything else at retry level or above → exit chains
        if indent <= chains_indent:
            break

    return chains


def _resolve_oracle_model_chain(agent_type: str, explicit_model: str) -> list[str]:
    """Resolve the fallback model chain for an oracle agent.

    - ``explicit_model`` non-empty → single-element ``[explicit_model]``
      (user override, never replaced).
    - Otherwise read ``retry.fallbackChains`` from the OMP config and
      select by *agent_type*:
        - oracle / oracle-opus → ``slow`` (or ``default``)
        - oracle-lite → ``default``
    - No config / no matching chain → return ``[]`` (keep existing behaviour).
    """
    if explicit_model:
        return [explicit_model]

    for cfg_path in _omp_config_paths():
        chains = _parse_retry_fallback_chains(cfg_path)
        if not chains:
            continue

        # Map agent_type → preferred chain name
        if agent_type in ("oracle", "oracle-opus"):
            chain = chains.get("slow") or chains.get("default")
        else:  # oracle-lite or anything else
            chain = chains.get("default")

        if chain:
            return list(chain)

    return []


def _looks_like_quota(text: str) -> bool:
    """True when *text* carries a provider quota/rate-limit marker.

    Mirrors the supervisor's ``_scan_quota_error`` markers so CLI-side
    failures (spawn errors, warm/cold degrade messages) can be reported
    explicitly as insufficient_quota instead of generic timeouts.
    """
    import re

    return bool(re.search(
        r"insufficient[_ -]?quota|quota[ _-]?exceeded|quota exceeded|"
        r"rate[ _-]?limit|payment required|\b402\b|billing|quota",
        text,
        re.IGNORECASE,
    ))


def _merge_flat_yaml(path: Path, ensure: dict[str, dict[str, str]]) -> bool:
    """Insert missing keys under their correct YAML sections (with backup).

    Only appends keys that are entirely absent — never overwrites an
    existing value. Returns True when a merge happened.
    """
    backup = path.with_suffix(path.suffix + f".bak-{int(time.time())}")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    body = "\n".join(lines)
    merged = False

    # P3-9: index section boundaries so missing keys insert at the end of
    # their owning section rather than being appended at file end.
    section_end: dict[str, int] = {}
    current_section = ""
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key_part, _, _ = stripped.partition(":")
        key_part = key_part.strip()
        if not (line.startswith("  ") or line.startswith("\t")):
            if current_section:
                section_end[current_section] = i
            current_section = key_part
    if current_section:
        section_end[current_section] = len(lines)

    # Build insertion plan: per-existing-section lines + trailing new sections.
    section_inserts: dict[str, list[str]] = {}
    new_section_lines: list[str] = []
    for section, kv in ensure.items():
        missing = {k: v for k, v in kv.items() if f"{k}:" not in body}
        if not missing:
            continue
        merged = True
        if section in section_end:
            for k, v in missing.items():
                section_inserts.setdefault(section, []).append(f"  {k}: {v}")
        else:
            if not new_section_lines:
                new_section_lines.append(f"{section}:")
            for k, v in missing.items():
                new_section_lines.append(f"  {k}: {v}")

    if not merged:
        return False

    # P3-9: apply inserts bottom-up so line indices remain valid.
    new_lines = list(lines)
    for section, insert_lines in sorted(
        section_inserts.items(), key=lambda x: section_end[x[0]], reverse=True,
    ):
        pos = section_end[section]
        for j, text in enumerate(insert_lines):
            new_lines.insert(pos + j, text)
    new_lines.extend(new_section_lines)

    if path.exists():
        path.rename(backup)
    with open(path, "w") as f:
        f.write("\n".join(new_lines) + "\n")
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


def _check_mailbox_plugin() -> Optional[str]:
    """A4: verify the installed omp-mailbox-plugin can report runtime events.

    Returns an error message when the plugin is missing or predates
    RuntimeEventReporter — otherwise None. Without the reporter, gateway
    heartbeat presence silently degrades (no runtime events), so oracle start
    must fail loudly instead of proceeding blind.
    """
    plugin_src = (
        Path.home() / ".omp" / "plugins" / "node_modules"
        / "omp-mailbox-plugin" / "src" / "index.ts"
    )
    try:
        text = plugin_src.read_text(encoding="utf-8")
    except OSError as exc:
        return (
            f"omp-mailbox-plugin not found at {plugin_src} ({exc}); oracle "
            "heartbeat presence requires it. Update: run `omp plugin update "
            "omp-mailbox-plugin` (or reinstall), then retry oracle start."
        )
    if "RuntimeEventReporter" not in text:
        return (
            f"installed omp-mailbox-plugin ({plugin_src}) predates "
            "RuntimeEventReporter — heartbeat presence is unavailable. Update "
            "the plugin (`omp plugin update omp-mailbox-plugin`), then retry "
            "oracle start."
        )
    return None


def _oracle_init_protocol(sid: str) -> str:
    """B3: self-describing protocol block embedded in the oracle-init TASK body.

    The plugin handshake picks the TASK body up as the initial user message
    (pi.sendUserMessage); embedding reply_format / required_fields / example
    makes the handshake self-describing without a separate doc fetch.
    """
    return (
        "\n\n--- ORACLE MAILBOX PROTOCOL (B3) ---\n"
        "reply_format: REPORT envelope via `mailbox send --kind REPORT` "
        "(JSON payload in --body)\n"
        "required_fields: reply_to=<original msg_id>, run_id, request_id\n"
        f"example: mailbox send --session {sid} --from oracle --to manager "
        "--kind REPORT --reply-to <msg_id> --run-id <run_id> "
        "--request-id <request_id> --subject result --body '<json>'\n"
    )


# ── A1: session_id 同步绑定 + meta.json ───────────────────────────────


def _oracle_meta_path(review_key: str) -> Path:
    """A1: meta.json path for a review key (~/.omp/oracle/<safe-key>/meta.json).

    review_key may carry ':' / '/' — sanitize into a safe directory name
    (same substitution ``_review_sid`` applies to ':').
    """
    safe = review_key.replace(":", "-").replace("/", "-").replace("\\", "-")
    return Path.home() / ".omp" / "oracle" / safe / "meta.json"


def _read_oracle_meta(review_key: str) -> dict:
    """A1: read the bound-session meta for a review key ({} when absent)."""
    try:
        path = _oracle_meta_path(review_key)
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_oracle_meta(review_key: str, backend_session_id: str, status: str,
                       swarm_session_id: str = "") -> dict:
    """A1: persist bound-session meta (backend_session_id/bound_at/status).

    ``status`` is "bound" after a successful bind; the file is the warm
    resume point for revive/ask when the park manifest is missing or stale.
    """
    meta = {
        "review_key": review_key,
        "swarm_session_id": swarm_session_id,
        "backend_session_id": backend_session_id,
        "bound_at": datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT),
        "status": status,
    }
    try:
        path = _oracle_meta_path(review_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".tmp-{uuid4().hex[:8]}.json"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))
    except OSError as exc:
        print(f"warning: oracle meta write failed: {exc}", file=sys.stderr)
    return meta


def _runtime_log_path(handle) -> Optional[Path]:
    """A1: runtime log file for a spawned handle.

    OMP interactive handles carry the supervisor spec path — the log lives
    beside it (<runtime_dir>/<runtime_id>.log). Falls back to the
    supervisor's canonical runtime dir (``_runtime_dir``).
    """
    spec_path = getattr(handle, "extra", {}).get("spec_path") if hasattr(handle, "extra") else None
    if spec_path:
        p = Path(spec_path)
        if p.parent.is_dir():
            return p.parent / f"{handle.runtime_id}.log"
    try:
        from codeagent.runtime.supervisor import _runtime_dir

        return _runtime_dir(handle.runtime_id) / f"{handle.runtime_id}.log"
    except Exception:
        return None


def _scan_runtime_log_for_session_id(log_path: Optional[Path]) -> str:
    """A1: extract the native backend session id from a runtime log.

    Scans for ``session_id=`` / ``backend_session=`` markers (the OMP plugin
    / opencode runner print the native backend session id as it binds).
    ``backend_session*`` matches win over generic ``session_id`` (which can
    also carry the ora-* swarm sid); the LAST match wins (the bound id is
    printed once the session actually starts).
    """
    if log_path is None or not log_path.exists():
        return ""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    backend_matches = re.findall(r"backend_session(?:_id)?\s*=\s*([^\s,;\"']+)", text)
    if backend_matches:
        return backend_matches[-1].strip()
    for g in reversed(re.findall(r"session_id\s*=\s*([^\s,;\"']+)", text)):
        g = g.strip().strip('"').strip("'")
        if g and not g.startswith("ora-"):
            return g
    return ""


def _poll_backend_session_id(handle, timeout: float = 60.0, interval: float = 0.5) -> str:
    """A1: synchronously bind the backend session id after spawn (≤60s).

    Fast path: the handle already carries a ``backend_session_id`` (adapters
    that resolve it synchronously). Otherwise poll the runtime log for up to
    *timeout* seconds, scanning for ``session_id=`` / ``backend_session=``.
    Returns "" when binding times out (caller must NOT report success).
    """
    direct = getattr(handle, "backend_session_id", "") or ""
    if direct:
        return direct
    log_path = _runtime_log_path(handle)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sid = _scan_runtime_log_for_session_id(log_path)
        if sid:
            return sid
        time.sleep(interval)
    return ""


def _resolve_bound_session_id(review_key: str, manifest) -> str:
    """A1: backend session id for a review key — manifest → meta.json.

    The park manifest is authoritative (updated on start/ask/revive);
    meta.json is the A1 persistence that survives manifest loss and warms
    revive/ask before spawning.
    """
    if manifest is not None and getattr(manifest, "backend_session_id", ""):
        return manifest.backend_session_id
    meta = _read_oracle_meta(review_key)
    return (meta or {}).get("backend_session_id", "") or ""


def cmd_oracle_start(args: argparse.Namespace) -> int:
    """Create review/session/runtime and return the runtime id."""
    # A4: heartbeat defense — refuse to start when the mailbox plugin cannot
    # report runtime events (presence/heartbeat would silently degrade).
    plugin_err = _check_mailbox_plugin()
    if plugin_err is not None:
        print(f"error: {plugin_err}", file=sys.stderr)
        return 1
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

    # ── Model fallback chain (reuse retry.fallbackChains from OMP config) ──
    model_chain = _resolve_oracle_model_chain(args.agent, args.model or "")
    primary_model = model_chain[0] if model_chain else (args.model or "")
    chain_env: dict[str, str] = {}
    if model_chain:
        chain_env["OMP_MODEL_FALLBACK_CHAIN"] = ",".join(model_chain)
        log.debug("oracle start: model chain resolved: %s (primary=%s)", model_chain, primary_model)

    # ── Swarm session + agent registration ────────────────────────────
    kernel, store = _kernel_and_store()
    sid = _review_sid(review_key)
    try:
        kernel.create_session(sid, "manager", [_ORACLE_AGENT])
    except ValueError as exc:
        # 改进项4: only swallow the idempotent "already exists" case; a real
        # conflict (manager mismatch / corrupt session dir) must surface.
        if "already exists" not in str(exc):
            raise
        log.debug("oracle start: swarm session %s already exists (idempotent)", sid)
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
    except ValueError as exc:
        # P3-7: only swallow duplicate-registration; real errors must surface.
        if "already" not in str(exc).lower() and "exist" not in str(exc).lower():
            raise
        log.debug("oracle start: agent %s already registered (idempotent)", _ORACLE_AGENT)

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
                body=prompt + _oracle_init_protocol(sid),  # B3: self-describing handshake
                kind="TASK",
                run_id=f"run-{uuid4().hex[:10]}",
                request_id=f"req-{uuid4().hex[:10]}",
                require_ack=True,
            )
        except Exception as exc:
            print(f"warning: initial task dispatch failed: {exc}", file=sys.stderr)

    # ── Spawn runtime (native path) ───────────────────────────────────
    reg = RuntimeRegistry()
    spawn_env = {**memory_env, **chain_env}
    handle = reg.spawn(backend, {
        "session_id": sid,
        "agent_id": _ORACLE_AGENT,
        "review_key": review_key,
        "workdir": workdir,
        "task": prompt,
        "model": primary_model,
        "gateway_socket": str(control_socket_path()),
        "owner_pid": os.getpid(),
        "nonce": uuid4().hex[:12],
        "short_task": False,
        "env": spawn_env,
    })

    # ── A1: session_id 同步绑定 ──────────────────────────────────────
    # Poll the runtime log (≤60s) for the native backend session id so the
    # review's warm resume point is known BEFORE start returns, then persist
    # it to ~/.omp/oracle/<review_key>/meta.json for revive/ask reuse.
    bound_session_id = _poll_backend_session_id(handle)
    if not bound_session_id:
        # A1: binding failure must NOT report success — stop the runtime to
        # avoid a leak and return non-zero.
        try:
            reg.stop(handle.runtime_id, "session-id-binding-timeout")
        except Exception as stop_exc:
            log.debug("oracle start: runtime stop after binding timeout failed (%s)", stop_exc)
        print("error: session_id binding timeout "
              f"(no 'session_id='/backend_session=' marker in the runtime log for "
              f"{handle.runtime_id} within 60s)", file=sys.stderr)
        return 1

    # Persist backend session into the park manifest (authoritative).
    # NOTE: manifest.model stores the EXPLICIT override only (args.model).
    # The resolved primary (chain[0]) is used for spawn, but persisting it
    # here would make revive treat it as an explicit override and collapse
    # the fallback chain to one element.
    manifest = ParkManifest(
        review_key=review_key,
        swarm_session_id=sid,
        agent_type=args.agent,
        model=args.model or "",
        host="__local__",
        workdir=workdir,
        lifecycle=Lifecycle.HOT_PARKED,
        backend_session_id=bound_session_id,
        created_at=(existing.created_at if existing else time.time()),
        last_activity_at=time.time(),
    )
    if existing is not None:
        registry.update(review_key, manifest)
    else:
        registry.acquire(review_key, manifest)

    # A1: persist the bound-session meta (backend_session_id/bound_at/status).
    _write_oracle_meta(review_key, bound_session_id, "bound", swarm_session_id=sid)

    # Adopt the runtime into the LOCAL gateway so presence/status work even
    # for runtimes without a plugin handshake (opencode/generic).
    _adopt_runtime(review_key, sid, handle, backend)

    print(json.dumps({
        "review_key": review_key,
        "session_id": sid,
        "runtime_id": handle.runtime_id,
        "backend": backend,
        "backend_session_id": bound_session_id,
        "bound": True,
        "meta_path": str(_oracle_meta_path(review_key)),
        "generation": handle.generation,
        "mode": handle.mode,
        "capabilities": sorted(handle.capabilities),
        "model_chain": model_chain,
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
        # Runtime not alive: if it died of quota exhaustion, say so
        # EXPLICITLY before silently degrading to warm/cold.
        quota = health.get("quota_error", "")
        if quota:
            print(json.dumps({
                "warning": "insufficient_quota",
                "review_key": review_key,
                "detail": quota[:300],
                "degrade_hint": "primary model quota exhausted — retry with oracle-lite or a cheaper model",
            }, indent=2), file=sys.stderr)
    except GatewayError as exc:
        # 改进项3: the hot path always degrades to warm/cold — log WHY the
        # in-loop send was skipped (gateway down, runtime gone, etc.).
        log.debug("oracle ask: hot path skipped (%s: %s) — degrading to warm/cold",
                  exc.code, exc.message)
    except Exception as exc:
        log.debug("oracle ask: hot path failed unexpectedly (%s) — degrading to warm/cold", exc)

    # ── Warm: native backend session resume ───────────────────────────
    registry = ParkRegistry()
    manifest = registry.lookup(review_key)
    # A1: warm 前置 — meta.json 绑定的 backend session 复用（manifest 优先）。
    bound_sid = _resolve_bound_session_id(review_key, manifest)
    if manifest and bound_sid:
        # P2-12: registry is created up front so a failed warm spawn can be
        # stopped from the except handler before degrading to cold.
        reg = RuntimeRegistry()
        warm_handle = None
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
            # Model fallback chain (same resolution as start/revive).
            ask_model_chain = _resolve_oracle_model_chain(args.agent, manifest.model or "")
            ask_primary = ask_model_chain[0] if ask_model_chain else (manifest.model or "")
            ask_chain_env: dict[str, str] = {}
            if ask_model_chain:
                ask_chain_env["OMP_MODEL_FALLBACK_CHAIN"] = ",".join(ask_model_chain)
            warm_handle = reg.spawn(_resolve_backend(args.agent, args.backend), {
                "session_id": sid,
                "agent_id": _ORACLE_AGENT,
                "review_key": review_key,
                "workdir": manifest.workdir or os.getcwd(),
                "task": prompt,
                "model": ask_primary,
                "backend_session_id": bound_sid,
                "gateway_socket": str(control_socket_path()),
                "owner_pid": os.getpid(),
                "nonce": uuid4().hex[:12],
                "short_task": False,
                "env": ask_chain_env,
            })
            # P2-11: never overwrite the manifest's known backend session id
            # with an empty one — the opencode extraction window may close
            # before the session id is known, and clobbering it would destroy
            # the resume point (next warm attempt degrades to cold).
            new_backend_session_id = warm_handle.backend_session_id or bound_sid
            if not warm_handle.backend_session_id:
                print(
                    f"warning: warm: backend session id extraction window failed — "
                    f"preserving previous id {bound_sid!r}",
                    file=sys.stderr,
                )
            # Update manifest backend id + lifecycle (in-place, no UNIQUE clash).
            registry.update(review_key, ParkManifest(
                review_key=review_key,
                swarm_session_id=manifest.swarm_session_id,
                agent_type=manifest.agent_type,
                model=manifest.model,
                host=manifest.host,
                workdir=manifest.workdir,
                lifecycle=Lifecycle.HOT_PARKED,
                backend_session_id=new_backend_session_id,
                round=manifest.round + 1,
                created_at=manifest.created_at,
                last_activity_at=time.time(),
            ))
            _adopt_runtime(review_key, sid, warm_handle, _resolve_backend(args.agent, args.backend))
            print(json.dumps({
                "method": "warm",
                "review_key": review_key,
                "runtime_id": warm_handle.runtime_id,
                "old_backend_session_id": bound_sid,
                "new_backend_session_id": new_backend_session_id,
                "model_chain": ask_model_chain,
                "note": (
                    "native backend session resumed (OMP --resume / opencode --session)"
                    if new_backend_session_id else
                    "warm runtime spawned; session id pending — previous id preserved"
                ),
            }, indent=2))
            return 0
        except Exception as exc:
            # P2-12: the warm runtime was already spawned — if a later step
            # (registry.update / _adopt_runtime) raised, stop it before the
            # cold path, or the resumed process leaks.
            if warm_handle is not None:
                try:
                    reg.stop(warm_handle.runtime_id, "warm-failed-degrade-cold")
                except Exception as stop_exc:
                    log.debug("oracle ask: warm runtime stop failed (%s): %s",
                              warm_handle.runtime_id, stop_exc)
            error_text = str(exc)
            warm_fail: dict = {
                "method": "warm_failed",
                "review_key": review_key,
                "error": error_text,
                "note": "backend resume failed — degrading to cold",
            }
            if _looks_like_quota(error_text):
                warm_fail["error_type"] = "insufficient_quota"
                warm_fail["degrade_hint"] = (
                    "model quota exhausted — retry with oracle-lite or a cheaper model"
                )
            print(json.dumps(warm_fail, indent=2), file=sys.stderr)

    # ── Cold: snapshot reconstruction ─────────────────────────────────
    # P1-7: cold 分支必须注入 snapshot 上下文（build_cold_context），不能
    # 信 park_revive 返回的 rv.context——stale HOT_PARKED manifest 会让
    # revive_or_spawn 返回 method="hot" 的路由提示（"use hub send to
    # peer_agent_id=..."），round3 起该提示被当成首轮 prompt 注入重建
    # 实例（回归）。这里直接生成 snapshot 上下文，忽略决策层的 context。
    cold_context = build_cold_context(review_key)
    # Model fallback chain (same resolution as start/revive; explicit
    # --model / manifest override wins).
    cold_explicit = (manifest.model if manifest else "") or (getattr(args, "model", "") or "")
    ask_model_chain = _resolve_oracle_model_chain(args.agent, cold_explicit)
    cold_primary = ask_model_chain[0] if ask_model_chain else cold_explicit
    cold_chain_env: dict[str, str] = {}
    if ask_model_chain:
        cold_chain_env["OMP_MODEL_FALLBACK_CHAIN"] = ",".join(ask_model_chain)
    try:
        reg = RuntimeRegistry()
        handle = reg.spawn(_resolve_backend(args.agent, args.backend), {
            "session_id": _review_sid(review_key),
            "agent_id": _ORACLE_AGENT,
            "review_key": review_key,
            "workdir": manifest.workdir if manifest else os.getcwd(),
            "task": cold_context + "\n\n" + prompt,
            "model": cold_primary,
            "gateway_socket": str(control_socket_path()),
            "owner_pid": os.getpid(),
            "nonce": uuid4().hex[:12],
            "short_task": False,
            "env": cold_chain_env,
        })
    except Exception as exc:
        error_text = str(exc)
        cold_fail: dict = {
            "method": "cold_failed",
            "review_key": review_key,
            "error": error_text,
        }
        if _looks_like_quota(error_text):
            cold_fail["error_type"] = "insufficient_quota"
            cold_fail["degrade_hint"] = (
                "model quota exhausted — retry with oracle-lite or a cheaper model"
            )
        print(json.dumps(cold_fail, indent=2), file=sys.stderr)
        return 1
    _adopt_runtime(review_key, _review_sid(review_key), handle, _resolve_backend(args.agent, args.backend))
    print(json.dumps({
        "method": "cold",
        "review_key": review_key,
        "runtime_id": handle.runtime_id,
        "backend_session_id": handle.backend_session_id,
        "model_chain": ask_model_chain,
        "note": "snapshot reconstruction (no live/hot session)",
    }, indent=2))
    return 0


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
        # Quota exhaustion must be surfaced EXPLICITLY (not as a generic
        # timeout/dead-runtime) — with a concrete degradation hint.
        quota = (info.get("runtime_health") or {}).get("quota_error", "")
        if quota:
            out["runtime"]["quota_error"] = quota
            out["runtime"]["degrade_hint"] = (
                "model quota exhausted — retry with oracle-lite or a cheaper model, "
                "or wait for quota reset"
            )
    except GatewayError as exc:
        # 改进项2: gateway 不在时给结构化降级 + 修复提示，而不是裸 error 字符串。
        if exc.code in ("GATEWAY_DOWN", "GATEWAY_CONNECT_FAILED"):
            out["runtime"] = {
                "status": "gateway_down",
                "hint": "run 'aimeshchat gateway start'",
            }
        else:
            out["runtime"] = {"status": "unavailable", "error": exc.message}
    except Exception as exc:
        out["runtime"] = {"status": "unavailable", "error": str(exc)}

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
                            "_mtime": req_dir.stat().st_mtime,
                        })
        except Exception:
            reqs = []
    # P3-8: sort by filesystem mtime (creation/modification order), not
    # request_id lexicographic order — hex IDs don't sort chronologically.
    reqs.sort(key=lambda r: r.get("_mtime", 0))
    for r in reqs:
        r.pop("_mtime", None)
    out["requests"] = reqs[-5:]  # most recent by time

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

    # B2: mailbox unread aggregation — unread count + latest REPORT preview +
    # recommendation.  Inbox files == delivered-but-not-read (two-phase
    # consumption: inbox → processing → archive).
    if manifest and manifest.swarm_session_id:
        try:
            inbox_dir = store.agent_subdir(manifest.swarm_session_id, _ORACLE_AGENT, "inbox")
            unread = 0
            latest_report: Optional[dict] = None
            for f in store.list_messages(inbox_dir):
                try:
                    msg = json.loads(f.read_bytes())
                except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                    continue
                unread += 1
                if msg.get("kind") == "REPORT":
                    created = msg.get("created_at", "")
                    if latest_report is None or created >= latest_report.get("created_at", ""):
                        latest_report = {
                            "msg_id": msg.get("msg_id"),
                            "from": msg.get("from"),
                            "created_at": created,
                            "subject": (msg.get("subject") or "")[:120],
                            "body_preview": (msg.get("body") or "")[:300],
                        }
            if unread == 0:
                recommendation = "none"
            elif latest_report is not None:
                recommendation = "read or ack REPORT before release"
            else:
                recommendation = "read inbox before release"
            out["mailbox"] = {
                "unread": unread,
                "latest_report": latest_report,
                "recommendation": recommendation,
            }
        except Exception:
            out["mailbox"] = None
    else:
        out["mailbox"] = None

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


def _fallback_find_session_for_key(review_key: str, since: Optional[float] = None) -> Optional[Path]:
    """A2-③ filesystem source: recursive scan, EXACT review_key match, start-time window.

    Recursively walks ~/.omp/agent/sessions/ (``os.walk``) for .jsonl files
    and scores candidates instead of first-hit matching — a short tail
    segment (e.g. ``blur`` from ``proj:oracle:gfx:blur``) previously matched
    unrelated sessions mentioning that word.  Scoring:
      +100  full *review_key* appears EXACTLY (word-boundary, not substring)
      +10   file lives under an oracle session dir (``ora-*`` / ``__advisor``)
      +1    specific tail segment (>= 5 chars) appears
    ``since`` (epoch mtime) filters out sessions written before the review
    started. A key-derived signal (full-key exact OR specific tail) is
    REQUIRED — the ``ora-*`` dir bonus alone never matches an unrelated key.
    Returns the best-scoring file (or ``None`` when nothing scores).
    """
    sessions_root = Path.home() / ".omp" / "agent" / "sessions"
    if not sessions_root.is_dir():
        return None

    # A2: recursive walk — every session file anywhere under the root.
    all_files: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(sessions_root):
        for name in filenames:
            if name.endswith(".jsonl"):
                all_files.append(Path(dirpath) / name)
    if not all_files:
        return None

    # Sort by mtime descending; drop files older than *since* (the review
    # start), keep the 5 most recent within the window.
    recent: list[Path] = []
    for f in all_files:
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if since is not None and mtime < since:
            continue
        recent.append((mtime, f))
    recent.sort(key=lambda t: t[0], reverse=True)
    recent = [f for _m, f in recent[:5]]

    # Derive tokens: full review_key first (most specific), then the tail
    # segment ONLY when it is specific enough (>= 5 chars) — short generic
    # segments ("blur", "v1") cause false positives against old sessions.
    tail = review_key.rsplit(":", 1)[-1].strip()
    full_key = review_key if len(review_key) >= 6 else ""
    tail_ok = tail and tail != review_key and len(tail) >= 5

    def _exact(lower: str, needle: str) -> bool:
        """A2: word-boundary containment — 'proj:oracle:gfx:blur' must NOT
        match 'proj:oracle:gfx:blur2' (substring false positive). Empty
        before/after (string start/end) counts as a boundary."""
        _alpha = "abcdefghijklmnopqrstuvwxyz_0123456789"
        idx = 0
        while True:
            idx = lower.find(needle, idx)
            if idx < 0:
                return False
            before = lower[idx - 1] if idx > 0 else ""
            after = lower[idx + len(needle)] if idx + len(needle) < len(lower) else ""
            if (not before or before not in _alpha) and (not after or after not in _alpha):
                return True
            idx += len(needle)
        return False

    best: Optional[Path] = None
    best_score = 0
    best_key_score = 0  # A2: key-derived signal (full-key exact / tail) —
                        # the ora-* dir bonus alone must NOT match an
                        # unrelated key (精确 review_key 匹配要求).
    for f in recent:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lower = text.lower()
        key_score = 0
        if full_key and _exact(lower, full_key.lower()):
            key_score += 100
        if tail_ok and _exact(lower, tail.lower()):
            key_score += 1
        if key_score == 0:
            continue  # no key signal — skip regardless of dir bonus
        score = key_score
        if "ora-" in f.name.lower() or "ora-" in str(f.parent).lower() or "__advisor" in f.name.lower():
            score += 10
        if score > best_score:
            best, best_score, best_key_score = f, score, key_score
    return best if best_key_score > 0 else None


def _review_start_ts(review_key: str, manifest) -> Optional[float]:
    """A2: review start timestamp (epoch) — meta bound_at → manifest created_at.

    Used as the lower bound of the filesystem source's time window (only
    session files modified AFTER the review started are considered).
    """
    meta = _read_oracle_meta(review_key)
    bound_at = meta.get("bound_at", "")
    if bound_at:
        try:
            return datetime.fromisoformat(bound_at.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            pass
    if manifest is not None and getattr(manifest, "created_at", 0):
        try:
            return float(manifest.created_at)
        except (TypeError, ValueError):
            return None
    return None


def _review_reply_to_candidates(review_key: str) -> set[str]:
    """A2: plausible ``reply_to`` encodings for a review key.

    Mailbox ``reply_to`` is validated as a safe path component — a raw
    review key with ':' (proj:oracle:gfx:blur) can never appear verbatim.
    Senders sanitize: full substitution (proj-oracle-gfx-blur) or the short
    slug used by ``_review_sid``/tmux (``replace(':', '-')[-12:]``).
    """
    safe = review_key.replace(":", "-").replace("/", "-").replace("\\", "-")
    slug = review_key.replace(":", "-")[-12:]
    return {review_key, safe, slug}


def _scan_mailbox_report(review_key: str, manifest) -> Optional[str]:
    """A2-② mailbox source: latest REPORT envelope answering the review key.

    Matches REPORT messages whose ``reply_to`` encodes the review key
    (raw / sanitized / slug — see _review_reply_to_candidates). Scans the
    swarm session history (manifest → meta), then falls back to a recursive
    scan of the whole mailbox root (a plugin may report under a different
    session). Returns the REPORT body when found, else None.
    """
    store = MailboxStore()
    swarm_ids: list[str] = []
    if manifest is not None and getattr(manifest, "swarm_session_id", ""):
        swarm_ids.append(manifest.swarm_session_id)
    meta = _read_oracle_meta(review_key)
    if meta.get("swarm_session_id"):
        swarm_ids.append(meta["swarm_session_id"])
    reply_keys = _review_reply_to_candidates(review_key)

    def _match(msg: dict) -> bool:
        return (msg.get("kind") == "REPORT"
                and (msg.get("reply_to") or "") in reply_keys)

    # Known sessions first — canonical history, newest first.
    for swarm_id in dict.fromkeys(swarm_ids):
        try:
            for msg in store.read_history(swarm_id, kind="REPORT"):
                if _match(msg):
                    return msg.get("body", "") or ""
        except Exception:
            continue
    # Fallback: bounded recursive scan of every session history directory.
    # P2: cap at 200 files to prevent runaway scans on large mailboxes.
    try:
        candidates: list[tuple[float, dict]] = []
        file_count = 0
        scan_done = False
        for dirpath, _dirs, files in os.walk(resolve_root()):
            if scan_done:
                break
            if Path(dirpath).name != "history":
                continue
            for name in files:
                file_count += 1
                if file_count > 200:
                    scan_done = True
                    break
                if not name.endswith(".json"):
                    continue
                p = Path(dirpath) / name
                try:
                    msg = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if _match(msg):
                    try:
                        candidates.append((p.stat().st_mtime, msg))
                    except OSError:
                        continue
        if candidates:
            candidates.sort(key=lambda t: t[0], reverse=True)
            return candidates[0][1].get("body", "") or ""
    except Exception:
        pass
    return None


def cmd_oracle_result(args: argparse.Namespace) -> int:
    """A2: unified result extraction — session transcript → mailbox REPORT → FS scan.

    Source priority (first hit wins):
      ① session_transcript — meta.json/manifest backend_session_id → the
         standard session-file API (_find_session_file + extract)
      ② mailbox_report    — swarm history REPORT envelope with
         reply_to == review_key (the task's terminal outcome)
      ③ filesystem        — recursive scan matching review_key EXACTLY with
         an mtime window after the review started (fallback scan)

    Output is JSON carrying source/confidence so callers can weigh the
    answer. ``--all`` returns every assistant message, default the latest.
    """
    review_key = args.review_key
    strict = bool(getattr(args, "strict", False))
    want_all = bool(getattr(args, "all", False))
    raw_mode = bool(getattr(args, "raw", False))
    manifest = ParkRegistry().lookup(review_key)
    start_since = _review_start_ts(review_key, manifest)
    max_msgs = 10**6 if want_all else 1

    out: dict = {
        "review_key": review_key,
        "source": "",
        "confidence": 0.0,
        "messages": [],
        "meta": {"strict": strict, "all": want_all},
    }

    # ── ① session transcript（meta.json session_id → 标准 API）────────
    bound_sid = _resolve_bound_session_id(review_key, manifest)
    if bound_sid:
        out["meta"]["session_id"] = bound_sid
        path = _find_session_file(bound_sid)
        if path is not None:
            msgs = _extract_assistant_messages(path, max_messages=max_msgs)
            if msgs:
                out.update({
                    "source": "session_transcript",
                    "confidence": 0.95,
                    "messages": msgs,
                })
                out["meta"]["path"] = str(path)
                if raw_mode:
                    print(msgs[-1])
                else:
                    print(json.dumps(out, indent=2))
                return 0

    # ── ② mailbox REPORT（reply_to == review_key 的终端信封）─────────
    report = _scan_mailbox_report(review_key, manifest)
    if report:
        out.update({
            "source": "mailbox_report",
            "confidence": 0.9,
            "messages": [report],
        })
        if raw_mode:
            print(report)
        else:
            print(json.dumps(out, indent=2))
        return 0

    # ── ③ filesystem（递归 + 精确 key + start 后时间窗）──────────────
    if not strict:
        try:
            path = _fallback_find_session_for_key(review_key, since=start_since)
        except Exception:  # pragma: no cover — defensive
            path = None
        if path is not None:
            msgs = _extract_assistant_messages(path, max_messages=max_msgs)
            if msgs:
                out.update({
                    "source": "filesystem",
                    "confidence": 0.7,
                    "messages": msgs,
                })
                out["meta"]["path"] = str(path)
                out["meta"]["since"] = start_since
                if raw_mode:
                    print(msgs[-1])
                else:
                    print(json.dumps(out, indent=2))
                return 0

    # ── Nothing found ─────────────────────────────────────────────────
    if strict:
        print(f"no result found for review key {review_key!r} (strict mode — "
              f"filesystem fallback refused; backend_session_id={bound_sid!r})",
              file=sys.stderr)
    elif not bound_sid:
        print(f"no result for review key {review_key!r} (oracle start 后才有)", file=sys.stderr)
    else:
        print(f"no result for review key {review_key!r} (session {bound_sid!r} not "
              f"found; no mailbox REPORT; no matching session file)", file=sys.stderr)
    return 1


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
        # 改进项1: human-readable by default; --jsonl is opt-in.
        jsonl=bool(getattr(args, "jsonl", False)),
        plain=bool(getattr(args, "plain", False)),
        exit_on=getattr(args, "exit_on", ""),
        max_events=getattr(args, "max_events", 0),
        duration=getattr(args, "duration", 0.0),
    )
    return cmd_events_watch(ns)


# ── wait（B1：阻塞到 agent_end 事件，然后内联最终文本）────────────────


def _wait_final_text(review_key: str) -> Optional[str]:
    """B1: extract the latest assistant text for inline printing on agent_end.

    Same sources as ``oracle result`` (primary backend session transcript,
    then best-effort scan) but returns the text instead of printing.
    """
    manifest = ParkRegistry().lookup(review_key)
    backend_id = (manifest.backend_session_id if manifest else "") or ""
    path = _find_session_file(backend_id) if backend_id else None
    if path is None:
        try:
            path = _fallback_find_session_for_key(review_key)
        except Exception:  # pragma: no cover — defensive
            path = None
    if path is None:
        return None
    msgs = _extract_assistant_messages(path, max_messages=1)
    return msgs[-1] if msgs else None


def cmd_oracle_wait(args: argparse.Namespace) -> int:
    """B1: block until the review's runtime emits a TASK_STATE.agent_end event.

    Polls the gateway event stream every ``--interval`` (default 5s) up to
    ``--timeout`` (default 300s).  On agent_end, prints the final assistant
    text inline (same transcript sources as ``oracle result``) and exits 0.
    On timeout, emits ``{status: timeout, suggestion: "use oracle result"}``
    and returns 1 so callers can fall back to ``oracle result``.
    """
    review_key = args.review_key
    interval = max(0.1, float(getattr(args, "interval", 5.0) or 5.0))
    timeout = max(0.0, float(getattr(args, "timeout", 300.0) or 300.0))

    try:
        info = _gateway().call("runtime.info", {"review_key": review_key})
        runtime_id = info.get("runtime_id", "")
        session_id = info.get("session_id", "")
    except GatewayError as exc:
        print(json.dumps({"status": "error", "error": exc.code,
                          "message": exc.message}, indent=2))
        return 1
    if not runtime_id:
        print(json.dumps({"status": "error", "error": "NO_RUNTIME",
                          "message": f"no active runtime for review key {review_key!r} "
                                     "(use 'oracle start' first)"}, indent=2))
        return 1

    # P1-1: start from the current high-water cursor rather than 0 — avoids
    # replaying the full event history on every poll iteration.  Do one
    # initial full drain (cursor 0, large limit) to catch an agent_end
    # that already landed; subsequent iterations only see NEW events.
    cursor: int = 0
    deadline = time.monotonic() + timeout
    try:
        boot = _gateway().call("events.list", {
            "cursor": 0,
            "filters": ["TASK_STATE"],
            "limit": 10_000,
            "session_id": session_id,
            "runtime_id": runtime_id,
        })
        for ev in boot.get("events", []):
            if ev.get("kind") == "TASK_STATE" and (ev.get("payload") or {}).get("state") == "agent_end":
                final = _wait_final_text(review_key)
                if final is not None:
                    print(final)
                return 0
        # P1-1: advance cursor to the high-water mark so the main loop
        # only polls for genuinely new events.
        cursor = int(boot.get("cursor") or 0)
    except GatewayError:
        pass  # fall through — will retry in the main loop
    while True:
        try:
            result = _gateway().call("events.list", {
                "cursor": cursor,
                "filters": ["TASK_STATE"],
                "limit": 200,
                "session_id": session_id,
                "runtime_id": runtime_id,
            })
        except GatewayError as exc:
            print(json.dumps({"status": "error", "error": exc.code,
                              "message": exc.message}, indent=2))
            return 1
        for ev in result.get("events", []):
            if ev.get("kind") == "TASK_STATE" and (ev.get("payload") or {}).get("state") == "agent_end":
                final = _wait_final_text(review_key)
                if final is not None:
                    print(final)  # B1: inline final text, like `oracle result`
                return 0
        cursor = int(result.get("cursor") or cursor or 0)
        if time.monotonic() >= deadline:
            print(json.dumps({"status": "timeout",
                              "suggestion": "use oracle result"}, indent=2))
            return 1
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            return 130


# ── release ────────────────────────────────────────────────────────────


def cmd_oracle_release(args: argparse.Namespace) -> int:
    """Write terminal state, stop the runtime, release the park lease.

    P1-1: 默认 soft release —— lifecycle=RELEASED_SOFT，保留 OMP session
    文件，`oracle revive` 可 warm 复活；--purge 硬销毁 —— 删 OMP session
    文件 + 删 park 行（registry.release(mode="hard") → registry.delete）。
    """
    review_key = args.review_key
    registry = ParkRegistry()
    manifest = registry.lookup(review_key)

    # B2: release guard — refuse to silently drop unread REPORTs in the
    # oracle inbox.  Advisory + interactive confirmation; --force skips.
    if manifest:
        try:
            store = MailboxStore()
            inbox_dir = store.agent_subdir(manifest.swarm_session_id, _ORACLE_AGENT, "inbox")
            unread_reports = 0
            for f in store.list_messages(inbox_dir):
                try:
                    msg = json.loads(f.read_bytes())
                except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                    continue
                if msg.get("kind") == "REPORT":
                    unread_reports += 1
            if unread_reports > 0 and not getattr(args, "force", False):
                answer = input(f"有 {unread_reports} 条未读 REPORT，确认释放？(y/N，或 use --force to skip) ").strip().lower()
                if answer not in ("y", "yes"):
                    print(json.dumps({
                        "review_key": review_key,
                        "release_aborted": True,
                        "unread_reports": unread_reports,
                    }, indent=2))
                    return 1
        except Exception:
            # B2: mailbox unreachable/unreadable — the guard is advisory,
            # never a hard gate on release.
            pass

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
    purge = bool(getattr(args, "purge", False))
    release_mode = "soft"
    if manifest:
        if purge:
            # P1-1: 硬销毁——删 OMP session 文件 + 删 park 行。
            _purge_omp_session(manifest)
            registry.release(review_key, mode="hard")
            release_mode = "hard"
        else:
            # P1-1: 软释放（默认）——RELEASED_SOFT，session 文件保留可 revive。
            registry.release(review_key, mode="soft")
            release_mode = "soft"

    print(json.dumps({
        "review_key": review_key,
        "runtime_stopped": stopped,
        "park_released": manifest is not None,
        "release_mode": release_mode,
        "session_purged": purge and manifest is not None,
    }, indent=2))
    return 0


# ── revive（P1-2：RELEASED_SOFT / COLD_RESUMABLE → HOT_PARKED）──────────


def _purge_omp_session(manifest: ParkManifest) -> list[str]:
    """P1-1: 硬销毁——删除 OMP session 文件（--purge）。

    删除对象（存在才删，返回实际删除路径列表）：
    - manifest.omp_session_path（若已记录）
    - _find_session_file(backend_session_id) 命中的 ``*_{sid}.jsonl``
    - 同目录下 ``*_{sid}`` 命名的会话子目录（``<ts>_<sid>/``，含 __advisor
      等附属文件）
    """
    removed: list[str] = []
    targets: set[Path] = set()

    raw_path = getattr(manifest, "omp_session_path", "") or ""
    if raw_path:
        p = Path(raw_path)
        if p.exists():
            targets.add(p)

    sid = manifest.backend_session_id or ""
    if sid:
        found = _find_session_file(sid)
        if found is not None:
            targets.add(found)
            parent = found.parent
            if parent.is_dir():
                for child in parent.glob(f"*_{sid}"):
                    if child.is_dir() and child not in targets:
                        targets.add(child)

    for p in sorted(targets, key=str):
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            removed.append(str(p))
        except OSError as exc:
            print(f"warning: purge session file failed: {p}: {exc}", file=sys.stderr)
    return removed


def cmd_oracle_revive(args: argparse.Namespace) -> int:
    """P1-2: 从 RELEASED_SOFT / COLD_RESUMABLE 复活。

    路由：
    - absent        → error not_found（需 start）
    - HOT_PARKED    → error already_active（用 ask）
    - RELEASED_HARD → error purged（需 start）
    - BROKEN        → error broken（purge 后 start）
    - 有 backend_session_id → warm（复用原生会话）；无 → cold（快照重建）

    mode: bg（默认）/pane 走 RuntimeRegistry.spawn（监督式 runtime）；
    resume 走 ``omp --resume`` 前台附着（绕过 postmesh）。
    """
    review_key = args.review_key
    mode = getattr(args, "mode", "bg") or "bg"
    registry = ParkRegistry()
    manifest = registry.lookup(review_key)

    if manifest is None:
        print(json.dumps({"error": "not_found", "hint": "use 'oracle start' first"}, indent=2),
              file=sys.stderr)
        return 1
    if manifest.lifecycle == Lifecycle.HOT_PARKED:
        print(json.dumps({"error": "already_active", "hint": "use 'oracle ask' to deliver a prompt"}, indent=2),
              file=sys.stderr)
        return 1
    if manifest.lifecycle == Lifecycle.RELEASED_HARD:
        print(json.dumps({"error": "purged", "hint": "use 'oracle start' to create a new instance"}, indent=2),
              file=sys.stderr)
        return 1
    if manifest.lifecycle == Lifecycle.BROKEN:
        print(json.dumps({"error": "broken", "hint": "use 'oracle release --purge' then 'oracle start'"}, indent=2),
              file=sys.stderr)
        return 1

    # A1: warm 前置 — meta.json 绑定的 backend session 复用（manifest 优先）。
    # 缺少 manifest 字段但 meta 有绑定时仍走 warm，避免冷重建丢失上下文。
    bound_sid = _resolve_bound_session_id(review_key, manifest)
    method = "warm" if bound_sid else "cold"
    try:
        if method == "warm":
            runtime_id, backend_session_id, model_chain = _revive_warm(review_key, manifest, mode)
        else:
            runtime_id, backend_session_id, model_chain = _revive_cold(review_key, manifest, mode)
    except Exception as exc:
        print(json.dumps({
            "error": f"{method}_failed",
            "review_key": review_key,
            "message": str(exc),
        }, indent=2), file=sys.stderr)
        return 1

    # A1: refresh the bound-session meta after a successful revive so the
    # next warm attempt reuses the (possibly new) backend session id.
    if backend_session_id:
        _write_oracle_meta(review_key, backend_session_id, "bound",
                           swarm_session_id=manifest.swarm_session_id or "")

    print(json.dumps({
        "review_key": review_key,
        "method": method,
        "mode": mode,
        "lifecycle": Lifecycle.HOT_PARKED.value,
        "runtime_id": runtime_id,
        "backend_session_id": backend_session_id,
        "model_chain": model_chain,
    }, indent=2))
    return 0


# ── attach（P2：统一入口——hot send 或 revive）──────────────────────────


def cmd_oracle_attach(args: argparse.Namespace) -> int:
    """P2: Unified attach entry — connect (HOT_PARKED) or revive (released/cold).

    Routes:
    - absent         → error not_found (hint: use start)
    - HOT_PARKED     → delegate to cmd_oracle_ask (hot send to live runtime)
    - RELEASED_SOFT  → delegate to cmd_oracle_revive (warm revive)
    - COLD_RESUMABLE → delegate to cmd_oracle_revive (cold revive)
    - RELEASED_HARD  → cmd_oracle_revive reports purged
    - BROKEN         → cmd_oracle_revive reports broken

    mode: bg（默认）/pane/resume — forwarded to revive when applicable.
    """
    review_key = args.review_key
    registry = ParkRegistry()
    manifest = registry.lookup(review_key)

    # P2: manifest absent → cannot attach
    if manifest is None:
        print(json.dumps({
            "error": "not_found",
            "hint": "use 'oracle start' first",
        }, indent=2), file=sys.stderr)
        return 1

    # P2: already active → hot send via ask
    if manifest.lifecycle == Lifecycle.HOT_PARKED:
        return cmd_oracle_ask(args)

    # P2: released/cold/broken → revive (lifecycle routing inside revive)
    return cmd_oracle_revive(args)


def _revive_warm(review_key: str, manifest: ParkManifest, mode: str) -> tuple[str, str, list[str]]:
    """P1-2: warm 复活——复用 backend_session_id 的原生会话。

    resume → _attach_omp_session 前台附着（无 runtime_id）。
    bg/pane → RuntimeRegistry.spawn（``omp --resume <sid>`` 监督式 tmux
    pane），与 cmd_oracle_ask 的 warm 路径同构。
    """
    sid = manifest.swarm_session_id or _review_sid(review_key)
    # A1: 复用 meta.json 绑定的 backend session（manifest 缺失/被清时仍 warm）。
    bound_sid = _resolve_bound_session_id(review_key, manifest)

    if mode == "resume":
        _attach_omp_session(bound_sid, review_key, manifest.workdir)
        return "", bound_sid, []

    # ── Model fallback chain (reuse retry.fallbackChains from OMP config) ──
    agent_type = manifest.agent_type or _ORACLE_AGENT
    model_chain = _resolve_oracle_model_chain(agent_type, manifest.model or "")
    primary_model = model_chain[0] if model_chain else (manifest.model or "")
    chain_env: dict[str, str] = {}
    if model_chain:
        chain_env["OMP_MODEL_FALLBACK_CHAIN"] = ",".join(model_chain)

    backend = _resolve_backend(agent_type, "omp")
    reg = RuntimeRegistry()
    handle = reg.spawn(backend, {
        "session_id": sid,
        "agent_id": _ORACLE_AGENT,
        "review_key": review_key,
        "workdir": manifest.workdir or os.getcwd(),
        "task": "",
        "model": primary_model,
        "backend_session_id": bound_sid,
        "gateway_socket": str(control_socket_path()),
        "owner_pid": os.getpid(),
        "nonce": uuid4().hex[:12],
        "short_task": False,
        "env": chain_env,
    })
    try:
        # P2-11 同款保护：session id 提取窗口失败时保留原值，避免破坏续接点。
        new_backend_session_id = handle.backend_session_id or bound_sid
        if not handle.backend_session_id:
            print(f"warning: revive warm: backend session id extraction window failed — "
                  f"preserving previous id {bound_sid!r}", file=sys.stderr)
        _flip_to_hot(review_key, manifest, sid=sid, backend_session_id=new_backend_session_id)
        _adopt_runtime(review_key, sid, handle, backend)
    except Exception:
        # P2-12 同款：后续步骤失败时停掉已 spawn 的 runtime，避免泄漏。
        try:
            reg.stop(handle.runtime_id, "revive-warm-failed")
        except Exception:
            pass
        raise
    return handle.runtime_id, new_backend_session_id, model_chain


def _revive_cold(review_key: str, manifest: ParkManifest, mode: str) -> tuple[str, str, list[str]]:
    """P1-2: cold 复活——快照重建新会话（无 backend session 可复用时）。

    resume 模式没有可附着的会话，退化为 bg（监督式 spawn）。
    首轮 task 注入 build_cold_context（与 cmd_oracle_ask cold 分支一致）。
    """
    if mode == "resume":
        mode = "bg"
    sid = _review_sid(review_key)
    # ── Model fallback chain (reuse retry.fallbackChains from OMP config) ──
    agent_type = manifest.agent_type or _ORACLE_AGENT
    model_chain = _resolve_oracle_model_chain(agent_type, manifest.model or "")
    primary_model = model_chain[0] if model_chain else (manifest.model or "")
    chain_env: dict[str, str] = {}
    if model_chain:
        chain_env["OMP_MODEL_FALLBACK_CHAIN"] = ",".join(model_chain)
    backend = _resolve_backend(agent_type, "omp")
    reg = RuntimeRegistry()
    handle = reg.spawn(backend, {
        "session_id": sid,
        "agent_id": _ORACLE_AGENT,
        "review_key": review_key,
        "workdir": manifest.workdir or os.getcwd(),
        "task": build_cold_context(review_key),
        "model": primary_model,
        "gateway_socket": str(control_socket_path()),
        "owner_pid": os.getpid(),
        "nonce": uuid4().hex[:12],
        "short_task": False,
        "env": chain_env,
    })
    try:
        _flip_to_hot(review_key, manifest, sid=sid,
                     backend_session_id=handle.backend_session_id or "")
        _adopt_runtime(review_key, sid, handle, backend)
    except Exception:
        try:
            reg.stop(handle.runtime_id, "revive-cold-failed")
        except Exception:
            pass
        raise
    return handle.runtime_id, handle.backend_session_id or "", model_chain


def _flip_to_hot(review_key: str, manifest: ParkManifest, sid: str,
                 backend_session_id: str) -> None:
    """P1-2: revive 成功后把 park manifest 原位翻回 HOT_PARKED。

    用 update()（而非 acquire()）——COLD_RESUMABLE 不是终态，acquire
    会拒绝回迁（仅 RELEASED_SOFT 允许重新 acquire）；update 与
    cmd_oracle_ask 的 warm 路径一致。
    """
    ParkRegistry().update(review_key, ParkManifest(
        review_key=review_key,
        swarm_session_id=sid,
        agent_type=manifest.agent_type,
        model=manifest.model,
        host=manifest.host,
        workdir=manifest.workdir,
        lifecycle=Lifecycle.HOT_PARKED,
        backend_session_id=backend_session_id,
        round=manifest.round + 1,
        created_at=manifest.created_at,
        last_activity_at=time.time(),
        release_mode="",
        omp_session_path=getattr(manifest, "omp_session_path", "") or "",
    ))


def _attach_omp_session(session_id: str, review_key: str, workdir: str = "") -> None:
    """P1-3: resume 模式——前台 ``omp --resume`` 附着（绕过 postmesh）。

    设计稿的 ``omp -s <sid>`` 在真实 omp CLI（v17）中不存在：交互式续接
    旗标是 ``-r/--resume=<sid>``，这里用实际旗标。附带向 gateway 声明
    presence（runtime.declare 是 Phase 3 可选 API，失败静默降级）。
    """
    try:
        subprocess.Popen(
            ["omp", "--resume", session_id],
            cwd=workdir or None,
        )
    except OSError as exc:
        print(f"warning: omp --resume attach failed: {exc}", file=sys.stderr)
    try:
        _gateway().call("runtime.declare", {  # P3: weak presence declaration
            "review_key": review_key,
            "backend_session_id": session_id,
            "mode": "native_resume",
            "agent_id": _ORACLE_AGENT,
        })
    except Exception as exc:
        log.debug("oracle revive: gateway presence declare skipped (%s)", exc)
