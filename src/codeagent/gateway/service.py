"""AgentGateway — per-device orchestration service.

The gateway composes the existing authorities (MailboxStore/SwarmKernel/
ParkRegistry/MailboxService/runtime launcher) and exposes them over a
local UDS. It does NOT re-implement protocol logic — mailbox semantics
live in MailboxService, delivery in DeliveryEngine, park leases in
ParkRegistry. This module wires them behind fixed RPC methods.

Fail-closed rules:
  - identity (session_id/agent_id/runtime_id) must match a registered
    runtime for owner-scoped operations
  - generation must not be stale
  - unknown runtimes → UNSUPPORTED_RUNTIME
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from codeagent.constants import ISO_TIMESTAMP_FORMAT
from codeagent.gateway.events import EventStore, control_socket_path
from codeagent.gateway.model import (
    ERR_GENERATION_STALE,
    ERR_NOT_AUTHORIZED,
    ERR_NOT_FOUND,
    ERR_OWNER_MISMATCH,
    ERR_PROTOCOL,
    ERR_PROTOCOL_CONFLICT,
    ERR_UNSUPPORTED_RUNTIME,
    GATEWAY_PROTOCOL_VERSION,
    GatewayError,
    RuntimeEventDraft,
)
from codeagent.mailbox.service import MailboxService
from codeagent.mailbox.store import MailboxStore
from codeagent.swarm.kernel import SwarmKernel

log = logging.getLogger(__name__)


@dataclass
class RuntimeRecord:
    """A registered/launched runtime known to this gateway."""

    runtime_id: str
    session_id: str
    agent_id: str
    generation: int
    review_key: str = ""
    backend_session_id: str = ""
    host_alias: str = "__local__"
    runtime: str = ""  # "omp" | "opencode" | "generic"
    owner_pid: int = 0
    nonce: str = ""
    status: str = "starting"  # starting | active | offline | stopped | unknown
    created_at: str = ""
    last_activity: float = 0.0
    started_at: float = 0.0  # A12: epoch of process start (real elapsed baseline)
    spec: dict = field(default_factory=dict)
    initial_task: str = ""


@dataclass
class _HubPeer:
    """Hub-layer peer ↔ swarm agent mapping (P8.1 cross-device routing)."""

    peer_id: str
    session_id: str
    agent_id: str
    host_alias: str
    mailbox_root: str = ""
    status: str = "online"  # online | offline | unknown
    registered_at: float = 0.0


class AgentGateway:
    """Local gateway service — fixed RPC surface over existing authorities."""

    def __init__(
        self,
        store: Optional[MailboxStore] = None,
        events: Optional[EventStore] = None,
        kernel: Optional[SwarmKernel] = None,
        restore_from_park: bool = True,
        peers_file: Optional[Path] = None,
    ) -> None:
        self._store = store or MailboxStore()
        self._events = events or EventStore()
        # Kernel wired with EngineDeliverySink — cross-device routing via
        # DeliveryEngine (durable outbox → transport → remote inbox).
        self._engine = None
        if kernel is None:
            from codeagent.swarm.delivery import DeliveryEngine, EngineDeliverySink

            engine = DeliveryEngine(mailbox_store=self._store, transport_router=None)
            sink = EngineDeliverySink(engine)
            self._kernel = SwarmKernel(store=self._store, sink=sink)
            sink.set_kernel(self._kernel)
            self._engine = engine
        else:
            self._kernel = kernel
        self._svc = MailboxService(store=self._store, kernel=self._kernel)
        self._runtimes: dict[str, RuntimeRecord] = {}
        # P8.3 presence: heartbeat timeout → offline sweep.
        # A4: 120s → 300s to cover slow networks; cold-start grace below
        # (timeout * 2) is unchanged.
        # P2: 300s trades liveness detection speed for resilience on
        # unreliable networks / slow-starting runtimes (e.g. remote SSH
        # tunnel flaps, OMP cold-start).  Shorter timeouts catch genuine
        # failures faster but cause false positives on transient pauses.
        self._offline_timeout: float = 300.0
        self._sweep_interval: float = 30.0
        self._sweep_stop = threading.Event()
        self._runtimes_lock = threading.RLock()
        self._sweep_thread: Optional[threading.Thread] = None
        # A6: hourly retention sweep (EventStore.sweep + outbox TTL).
        self._retention_sweep_interval: float = 3600.0
        self._last_retention_sweep: float = 0.0
        self._start_sweep_loop()
        # P8.1 hub: peer ↔ swarm agent mapping for cross-device routing.
        self._peers: dict[str, _HubPeer] = {}
        self._peers_lock = threading.RLock()
        # F2: hub peers are PERSISTED (they map remote devices, not local
        # processes) — survive gateway restarts.
        self._peers_file = peers_file or (control_socket_path().parent / "peers.json")
        self._restore_peers()
        # actas: runtime role exclusivity claims (session_id:agent_id → claim).
        self._claims: dict[str, dict] = {}
        # Cross-device write merge bookkeeping (session_id, target_path) →
        # artifact_sha256 — persisted so conflict detection survives restarts.
        self._merges: dict[tuple[str, str], str] = {}
        # P2-2: RLock for merges dict concurrent access.
        self._merges_lock = threading.RLock()
        self._restore_merges()
        # A3: gateway restart recovery — rebuild runtime records from the
        # park registry so hot detection / presence survive a restart even
        # for runtimes without a plugin re-register (opencode/generic).
        # Tests disable it to keep isolation from the real park DB.
        if restore_from_park:
            self._restore_runtimes_from_park()

    # ── F2: hub peer persistence ──────────────────────────────────────

    def _save_peers(self) -> None:
        """Persist hub peers atomically (survive gateway restarts)."""
        try:
            self._peers_file.parent.mkdir(parents=True, exist_ok=True)
            # P2-2: snapshot peers under lock; write file outside lock.
            with self._peers_lock:
                data = [{
                    "peer_id": p.peer_id, "session_id": p.session_id,
                    "agent_id": p.agent_id, "host_alias": p.host_alias,
                    "mailbox_root": p.mailbox_root, "status": p.status,
                    "registered_at": p.registered_at,
                } for p in self._peers.values()]
            tmp = self._peers_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            tmp.replace(self._peers_file)
        except OSError as exc:
            log.warning("hub peers persist failed: %s", exc)

    def _restore_peers(self) -> None:
        """Rebuild hub peers from the persisted file on gateway startup."""
        try:
            if not self._peers_file.exists():
                return
            data = json.loads(self._peers_file.read_text(encoding="utf-8"))
            # P2-2: lock peer writes during restore.
            with self._peers_lock:
                for d in data:
                    peer = _HubPeer(
                        peer_id=d.get("peer_id", ""),
                        session_id=d.get("session_id", ""),
                        agent_id=d.get("agent_id", ""),
                        host_alias=d.get("host_alias", ""),
                        mailbox_root=d.get("mailbox_root", ""),
                        status=d.get("status", "unknown"),
                        registered_at=d.get("registered_at", 0.0),
                    )
                    if peer.peer_id:
                        self._peers[peer.peer_id] = peer
                if self._peers:
                    log.info("gateway: restored %d hub peer(s)", len(self._peers))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("hub peers restore failed: %s", exc)

    def _restore_runtimes_from_park(self) -> None:
        """Rebuild in-memory runtime records from HOT_PARKED park manifests.

        A2: restored records are PLACEHOLDERS, not live runtimes — they are
        created with status='unknown' and last_activity=0 (no liveness
        signal). Only a fresh plugin re-register (heartbeat NOT_FOUND →
        plugin re-registers) flips them to active; until then probes report
        not-alive instead of faking a hot runtime.
        """
        try:
            from codeagent.park.registry import ParkRegistry

            manifests = ParkRegistry().list_active()
        except Exception as exc:
            log.warning("gateway: park restore unavailable: %s", exc)
            return
        restored = 0
        # P2-2: lock runtime writes during park restore.
        with self._runtimes_lock:
            for m in manifests:
                if not m.swarm_session_id or not m.backend_session_id:
                    continue
                # P3-g: use full review_key suffix (up to 32 chars) to avoid
                # collision from 12-char truncation; prefix with random hex if
                # key is still very short.
                _rk_slug = m.review_key.replace(':', '-')
                if len(_rk_slug) >= 24:
                    runtime_id = f"park-{_rk_slug[-24:]}"
                else:
                    runtime_id = f"park-{_rk_slug}-{secrets.token_hex(4)}"
                if runtime_id in self._runtimes:
                    continue
                self._runtimes[runtime_id] = RuntimeRecord(
                    runtime_id=runtime_id,
                    session_id=m.swarm_session_id,
                    agent_id=m.mailbox_agent_id or "oracle",
                    generation=int(m.round or 0) + 1,
                    review_key=m.review_key,
                    backend_session_id=m.backend_session_id,
                    host_alias=m.host or "__local__",
                    runtime=m.agent_type or "omp",  # A2: read agent backend from manifest
                    status="unknown",  # A2: placeholder — not a live runtime
                    created_at=m.created_at and datetime.fromtimestamp(m.created_at, tz=timezone.utc).strftime(ISO_TIMESTAMP_FORMAT) or "",
                    last_activity=0.0,  # A2: no heartbeat signal until re-register
                    started_at=m.created_at,  # A12: manifest created_at as start marker
                )
                restored += 1
        if restored:
            log.info("gateway: restored %d runtime(s) from park registry", restored)

    def stop(self) -> None:
        """Stop the presence sweep loop (idempotent)."""
        self._sweep_stop.set()
        if self._sweep_thread is not None and self._sweep_thread.is_alive():
            self._sweep_thread.join(timeout=5)
            self._sweep_thread = None

    # ── P8.3 presence sweep ────────────────────────────────────────────

    def _start_sweep_loop(self) -> None:
        """Background daemon thread — marks stale runtimes offline."""
        self._sweep_thread = threading.Thread(target=self._sweep_loop, daemon=True)
        self._sweep_thread.start()

    def _sweep_loop(self) -> None:
        while not self._sweep_stop.wait(self._sweep_interval):
            try:
                self._sweep_once()
                self._retention_sweep()
            except Exception as exc:
                log.warning("gateway: sweep iteration failed: %s", exc)

    def _retention_sweep(self) -> None:
        """A6: hourly retention — EventStore.sweep() + outbox TTL sweep.

        EventStore.sweep() prunes TOOL_* detail rows older than 7 days
        (previously dead code); the DeliveryEngine outbox sweep removes
        terminal delivered entries/markers past their TTL. Both are
        time-throttled to once per hour per gateway process.
        """
        now = time.time()
        if now - self._last_retention_sweep < self._retention_sweep_interval:
            return
        self._last_retention_sweep = now
        try:
            removed = self._events.sweep()
            if removed:
                log.info("gateway: events retention sweep removed %d row(s)", removed)
        except Exception as exc:
            log.warning("gateway: events retention sweep failed: %s", exc)
        if self._engine is not None:
            try:
                cleaned = self._engine.sweep()
                if cleaned:
                    log.info("gateway: outbox retention sweep removed %d entry(ies)", cleaned)
            except Exception as exc:
                log.warning("gateway: outbox retention sweep failed: %s", exc)

    def _sweep_once(self) -> list[str]:
        """Mark active-but-stale runtimes offline; sync hub peers.

        Returns the list of runtime ids transitioned to offline.
        Lock-protected: a concurrent heartbeat/event that refreshed
        last_activity between the scan and the transition re-verifies
        under the lock — no false offline flip (P8 review Q5 race fix).
        """
        offline: list[str] = []
        with self._runtimes_lock:
            now = time.time()
            for rid, record in list(self._runtimes.items()):
                if record.status != "active":
                    continue
                # Re-verify under the lock: a heartbeat/event may have
                # refreshed last_activity since the initial scan.
                # Cold-start grace: double the timeout for runtimes < 3 min old
                # so slow-starting processes aren't prematurely swept offline.
                timeout = self._offline_timeout
                if record.created_at:
                    try:
                        created_ts = datetime.fromisoformat(
                            record.created_at
                        ).timestamp()
                        if (now - created_ts) < 180:
                            timeout = self._offline_timeout * 2
                    except (ValueError, OSError):
                        pass
                if (now - record.last_activity) <= timeout:
                    continue
                record.status = "offline"
                offline.append(rid)
                try:
                    self._events.append_local(RuntimeEventDraft(
                        runtime_id=rid, generation=record.generation,
                        session_id=record.session_id, agent_id=record.agent_id,
                        request_id="", run_id="", kind="AGENT_STATUS",
                        created_at=datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT),
                        payload={
                            "old_status": "active", "new_status": "offline",
                            "reason": "heartbeat_timeout",
                            "last_activity": record.last_activity,
                        },
                    ))
                except Exception as exc:
                    log.warning("gateway: offline event append failed: %s", exc)
            # A3: dropped stopped records — runtime_stop keeps the record
            # briefly (so the stopping caller can read back the state), the
            # sweep removes it so stopped runtimes never linger/aggregate.
            for rid in [rid for rid, rec in list(self._runtimes.items()) if rec.status == "stopped"]:
                self._runtimes.pop(rid, None)
            # A9: prune expired session claims — _claims grows unboundedly
            # otherwise (TTL 3600s default, takeover already handles expiry).
            if self._claims:
                expired = [k for k, c in self._claims.items() if c["expires_at"] <= now]
                for k in expired:
                    self._claims.pop(k, None)
        # P8.1 presence 联动：关联 hub peer 同步 offline（锁外，避免死锁）。
        # P2-2: snapshot rid→(session_id, agent_id) under runtimes lock FIRST,
        # then sync peers under peers lock without touching _runtimes.
        if offline:
            offline_meta: dict[str, tuple[str, str]] = {}
            with self._runtimes_lock:
                for rid in offline:
                    record = self._runtimes.get(rid)
                    if record is not None:
                        offline_meta[rid] = (record.session_id, record.agent_id)
            with self._peers_lock:
                for rid, (sid, aid) in offline_meta.items():
                    for peer in self._peers.values():
                        if peer.agent_id == aid and peer.session_id == sid:
                            peer.status = "offline"
                if offline_meta:
                    self._save_peers()
        return offline

    # ── capabilities ───────────────────────────────────────────────────

    def capabilities(self, params: Optional[dict] = None) -> dict:
        from codeagent.runtime.registry import RuntimeRegistry  # lazy (P5)

        try:
            runtimes = sorted(RuntimeRegistry().names())
        except Exception as exc:
            # Degraded: capability listing failure must not take the gateway
            # down, but must NOT be silent (it once masked an unbound-method bug).
            log.warning("gateway: runtime capability listing failed: %s", exc)
            runtimes = ["omp"]
        return {
            "version": GATEWAY_PROTOCOL_VERSION,
            "runtimes": runtimes,
            "features": [
                "runtime_events", "message_receipts", "park", "events_cursor",
            ],
        }

    # ── session.ensure ─────────────────────────────────────────────────

    def session_ensure(self, params: dict) -> dict:
        """Create (or merge) a session from the Manager's authoritative manifest.

        The remote gateway caches a READ-ONLY copy of the Manager's
        manifest — it never invents its own manager/roster for the same
        session id.
        """
        session_id = params.get("session_id", "")
        manager_id = params.get("manager_id", "")
        roster = params.get("roster", []) or []
        acl = params.get("acl")
        if not session_id or not manager_id:
            raise GatewayError("PROTOCOL", "session.ensure requires session_id + manager_id")
        # Validate against an existing local session if present (authority check).
        existing = self._store.read_session(session_id)
        if existing is not None:
            old_manager = existing.get("manager", "")
            if old_manager and old_manager != manager_id:
                raise GatewayError(
                    "MANIFEST_CONFLICT",
                    f"session {session_id} already has manager={old_manager!r}; "
                    f"refusing manager={manager_id!r}",
                )
        self._store.session_init(
            session_id, manager_id, [a for a in roster if a != manager_id],
            acl=acl,
        )
        meta = self._store.read_session(session_id) or {}
        return {
            "session_id": session_id,
            "manager_id": meta.get("manager", manager_id),
            "roster": sorted(set(meta.get("agents", [])) | ({manager_id} if manager_id else set())),
            "manifest_revision": meta.get("manifest_revision", 1),
        }

    # ── runtime lifecycle ──────────────────────────────────────────────

    def runtime_register(self, params: dict) -> dict:
        """Plugin handshake — validate identity, return initial task.

        Identity: session_id/agent_id/runtime_id/review_key/generation/
        gateway_socket/owner_pid/nonce. Registration is owner-scoped:
        only the launcher's owner_pid+nonce+generation may register.
        """
        session_id = params.get("session_id", "")
        agent_id = params.get("agent_id", "")
        runtime_id = params.get("runtime_id", "")
        generation = int(params.get("generation", 0) or 0)
        review_key = params.get("review_key", "")
        backend_session_id = params.get("backend_session_id", "")
        runtime = params.get("runtime", "omp")
        owner_pid = int(params.get("owner_pid", 0) or 0)
        nonce = params.get("nonce", "")

        if not session_id or not agent_id or not runtime_id:
            raise GatewayError(ERR_NOT_AUTHORIZED, "runtime.register requires session_id/agent_id/runtime_id")

        # P2-2: lock the entire check-then-set for runtimes dict.
        with self._runtimes_lock:
            # Generation staleness: a registration for an OLDER generation of a
            # known runtime_id is rejected (fail closed).
            existing = self._runtimes.get(runtime_id)
            if existing is not None and generation < existing.generation:
                raise GatewayError(
                    ERR_GENERATION_STALE,
                    f"generation {generation} < registered {existing.generation} for {runtime_id}",
                )
            # A7: owner identity consistency — a SAME-generation re-registration
            # must present the same owner_pid+nonce the gateway stored (the
            # supervisor writes identity.json 0600 and the plugin echoes it back).
            # A2/A2.3: a NEW generation (launcher restart after stop/removal) is
            # allowed to take over with a fresh identity — recovery path.
            if existing is not None and generation == existing.generation:
                if (existing.owner_pid or existing.nonce) and (
                    existing.owner_pid != owner_pid or existing.nonce != nonce
                ):
                    raise GatewayError(
                        ERR_OWNER_MISMATCH,
                        f"owner identity mismatch for {runtime_id}: "
                        f"pid {existing.owner_pid} != {owner_pid} or nonce mismatch",
                    )

            # Roster membership (authoritative session.json).
            meta = self._store.read_session(session_id)
            if meta is None:
                raise GatewayError(ERR_NOT_AUTHORIZED, f"session not found: {session_id}")
            roster = {meta.get("manager", "")} | set(meta.get("agents", []))
            if agent_id not in roster:
                raise GatewayError(ERR_NOT_AUTHORIZED, f"agent {agent_id!r} not in session roster")

            record = RuntimeRecord(
                runtime_id=runtime_id,
                session_id=session_id,
                agent_id=agent_id,
                generation=generation,
                review_key=review_key,
                backend_session_id=backend_session_id,
                runtime=runtime,
                owner_pid=owner_pid,
                nonce=nonce,
                status="active",
                created_at=datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT),
                last_activity=time.time(),
                started_at=time.time(),  # A12: real elapsed baseline
            )
            # Preserve spec/initial_task from a prior spawn of the same runtime.
            if existing is not None:
                record.spec = existing.spec
                record.initial_task = existing.initial_task
            self._runtimes[runtime_id] = record

        # Sync the real backend session id into the park manifest so warm
        # resume survives a gateway restart (in-memory record lost).
        if review_key and backend_session_id:
            try:
                from codeagent.domain.park import Lifecycle
                from codeagent.park.registry import ParkRegistry

                registry = ParkRegistry()
                m = registry.lookup(review_key)
                # A1: only warm up HOT_PARKED leases — a RELEASED manifest
                # must NOT be resurrected by a plugin re-register.
                if m is not None and m.lifecycle == Lifecycle.HOT_PARKED:
                    from dataclasses import replace

                    registry.update(review_key, replace(
                        m,
                        backend_session_id=backend_session_id,
                        lifecycle=Lifecycle.HOT_PARKED,
                    ))
            except Exception as exc:
                log.warning("gateway: park backend sync failed: %s", exc)

        # Initial task: the oldest TASK addressed to this agent, if any.
        initial_task = ""
        initial_task_msg_id = ""
        try:
            inbox = self._store.agent_subdir(session_id, agent_id, "inbox")
            files = self._store.list_messages(inbox)
            for f in files:
                try:
                    msg = json.loads(f.read_bytes())
                except (json.JSONDecodeError, OSError):
                    continue
                if msg.get("kind") == "TASK":
                    initial_task = msg.get("body", "")
                    initial_task_msg_id = msg.get("msg_id", "")
                    break
        except Exception as exc:
            log.warning("gateway: initial task scan failed: %s", exc)
        record.initial_task = initial_task

        self._events.append_local(RuntimeEventDraft(
            runtime_id=runtime_id, generation=generation,
            session_id=session_id, agent_id=agent_id,
            request_id="", run_id="", kind="RUNTIME_STATE",
            created_at=record.created_at,
            payload={"state": "active", "runtime": runtime, "backend_session_id": backend_session_id},
        ))
        return {
            "runtime_id": runtime_id,
            "session_id": session_id,
            "agent_id": agent_id,
            "generation": generation,
            "initial_task": initial_task,
            "initial_task_msg_id": initial_task_msg_id,
        }

    def runtime_declare(self, params: dict) -> dict:
        """P3: Weak presence declaration for native_resume sessions.

        Unlike runtime.register, this does NOT require owner_pid/nonce —
        there is no plugin handshake.  It registers a placeholder record
        for status/presence awareness so ``runtime.info`` / heartbeat sweep
        can see the session.  ``review_key`` must exist in the park registry
        to prevent arbitrary runtime forgery (A7 weak gate).

        Params: review_key, backend_session_id, mode (default 'native_resume'),
                runtime_id (optional; derived if absent), agent_id (default 'oracle').
        """
        review_key = params.get("review_key", "")
        backend_session_id = params.get("backend_session_id", "")
        mode = params.get("mode", "native_resume")
        runtime_id = params.get("runtime_id", "")
        agent_id = params.get("agent_id", "oracle")

        if not review_key:
            raise GatewayError(ERR_NOT_AUTHORIZED, "runtime.declare requires review_key")
        if not backend_session_id:
            raise GatewayError(ERR_NOT_AUTHORIZED, "runtime.declare requires backend_session_id")

        # A7: review_key must exist in the park registry — this is the weak
        # identity gate for declare (prevents arbitrary runtime forgery).
        from codeagent.park.registry import ParkRegistry

        manifest = ParkRegistry().lookup(review_key)
        if manifest is None:
            raise GatewayError(ERR_NOT_FOUND, f"review_key not found: {review_key}")

        session_id = manifest.swarm_session_id or ""

        with self._runtimes_lock:
            existing = None
            if runtime_id:
                existing = self._runtimes.get(runtime_id)
            if existing is None:
                # Idempotent lookup by backend_session_id (oracle omits runtime_id).
                for rec in self._runtimes.values():
                    if rec.backend_session_id == backend_session_id:
                        existing = rec
                        runtime_id = rec.runtime_id
                        break

            if existing is not None:
                # Idempotent re-declare: refresh activity.
                existing.status = "active"
                existing.last_activity = time.time()
                record = existing
            else:
                if not runtime_id:
                    import hashlib

                    slug = hashlib.sha256(
                        backend_session_id.encode()
                    ).hexdigest()[:16]
                    runtime_id = f"native-{slug}"
                record = RuntimeRecord(
                    runtime_id=runtime_id,
                    session_id=session_id,
                    agent_id=agent_id,
                    generation=0,
                    review_key=review_key,
                    backend_session_id=backend_session_id,
                    runtime="native",
                    status="active",
                    created_at=datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT),
                    last_activity=time.time(),
                )
                self._runtimes[runtime_id] = record

        return {
            "runtime_id": runtime_id,
            "status": record.status,
            "mode": mode,
        }

    def runtime_spawn(self, params: dict) -> dict:
        """Spawn a runtime via the launcher + supervisor.

        Delegates to the RuntimeRegistry adapter (P5); the record keeps
        spec for probe/stop and the plugin handshake.
        """
        runtime = params.get("runtime", "omp")
        session_id = params.get("session_id", "")
        agent_id = params.get("agent_id", "")
        review_key = params.get("review_key", "")
        workdir = params.get("workdir", "")
        task = params.get("task", "")
        model = params.get("model", "")
        short_task = bool(params.get("short_task", False))

        from codeagent.runtime.registry import RuntimeRegistry

        registry = RuntimeRegistry()
        handle = registry.spawn(
            runtime,
            request={
                "session_id": session_id,
                "agent_id": agent_id,
                "review_key": review_key,
                "workdir": workdir,
                "task": task,
                "model": model,
                "short_task": short_task,
            },
        )
        record = RuntimeRecord(
            runtime_id=handle.runtime_id,
            session_id=session_id,
            agent_id=agent_id,
            generation=handle.generation,
            review_key=review_key,
            backend_session_id=handle.backend_session_id,
            host_alias=handle.host_alias,
            runtime=runtime,
            status="active",
            created_at=datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT),
            last_activity=time.time(),
            started_at=time.time(),  # A12: real elapsed baseline
            spec=params,
            initial_task=task,
        )
        # P2-2: lock runtimes dict write.
        with self._runtimes_lock:
            self._runtimes[handle.runtime_id] = record
        return {
            "runtime_id": handle.runtime_id,
            "generation": handle.generation,
            "backend_session_id": handle.backend_session_id,
            "mode": getattr(handle, "mode", ""),
            "capabilities": sorted(getattr(handle, "capabilities", [])),
        }

    def runtime_heartbeat(self, params: dict) -> dict:
        runtime_id = params.get("runtime_id", "")
        # A2.3: NOT_FOUND here is the RECOVERY trigger — the plugin treats it
        # as "gateway lost me (restart/stop)" and re-registers with a NEW
        # generation; runtime_register accepts the re-register (see A7/A2.3
        # owner check — a newer generation takes over the runtime_id).
        record = self._require_runtime(runtime_id)
        with self._runtimes_lock:
            # P3-d: re-read record under lock to avoid stale-dict read after
            # a concurrent runtime_register replaced the record object.
            record = self._runtimes.get(runtime_id)
            if record is None:
                raise GatewayError(ERR_NOT_FOUND, f"runtime disappeared during heartbeat: {runtime_id}")
            was_offline = record.status == "offline"
            record.last_activity = time.time()
            # P8.3: heartbeat restores an offline runtime.
            if was_offline:
                record.status = "active"
                try:
                    self._events.append_local(RuntimeEventDraft(
                        runtime_id=runtime_id, generation=record.generation,
                        session_id=record.session_id, agent_id=record.agent_id,
                        request_id="", run_id="", kind="AGENT_STATUS",
                        created_at=datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT),
                        payload={"old_status": "offline", "new_status": "active", "reason": "heartbeat"},
                    ))
                except Exception as exc:
                    log.warning("gateway: restore event append failed: %s", exc)
        if was_offline:
            with self._peers_lock:
                for peer in self._peers.values():
                    if peer.agent_id == record.agent_id and peer.session_id == record.session_id:
                        peer.status = "online"
            self._save_peers()
        # Park lease renewal happens in-process (no subprocess).
        if record.review_key:
            try:
                from codeagent.park.registry import ParkRegistry

                ParkRegistry().renew(record.review_key)
            except Exception as exc:
                log.warning("gateway: park renew failed: %s", exc)
        return {"runtime_id": runtime_id, "status": record.status}

    def runtime_status(self, params: dict) -> dict:
        """Query one runtime's presence status (in-memory, no probe)."""
        runtime_id = params.get("runtime_id", "")
        record = self._require_runtime(runtime_id)
        return {
            "runtime_id": runtime_id,
            "status": record.status,
            "last_activity": record.last_activity,
            "host_alias": record.host_alias,
            "session_id": record.session_id,
            "agent_id": record.agent_id,
        }

    def runtimes_list(self, params: dict) -> dict:
        """List registered runtimes and their presence status."""
        session_id = params.get("session_id", "")
        results = []
        # P2-2: snapshot runtimes under lock to avoid dict resize during iteration.
        with self._runtimes_lock:
            snapshot = list(self._runtimes.items())
        for rid, rec in snapshot:
            if session_id and rec.session_id != session_id:
                continue
            results.append({
                "runtime_id": rid,
                "session_id": rec.session_id,
                "agent_id": rec.agent_id,
                "status": rec.status,
                "last_activity": rec.last_activity,
                "host_alias": rec.host_alias,
                "review_key": rec.review_key,
                "backend_session_id": rec.backend_session_id,
            })
        return {"runtimes": results}

    def runtime_event(self, params: dict) -> dict:
        """Append a producer event (plugin/supervisor) to the EventStore."""
        draft = RuntimeEventDraft.from_dict(params.get("event", {}) or {})
        # P3-h: hold the lock across generation check + append + liveness
        # update so a concurrent runtime_register cannot swap the record
        # between the check and the append (TOCTOU on generation).
        with self._runtimes_lock:
            record = self._runtimes.get(draft.runtime_id)
            if record is None:
                raise GatewayError(ERR_NOT_FOUND, f"unknown runtime: {draft.runtime_id}")
            if draft.generation != record.generation:
                raise GatewayError(
                    ERR_GENERATION_STALE,
                    f"generation {draft.generation} != registered {record.generation} "
                    f"for {draft.runtime_id}",
                )
            # Producers never number their own events — ignore host/sequence.
            ev = self._events.append_local(draft)
            # Any event is activity: keep the runtime's liveness window fresh
            # (hot detection + park renew cadence).
            record.last_activity = time.time()
        return {"event_id": ev.event_id, "source_sequence": ev.source_sequence}

    def runtime_send(self, params: dict) -> dict:
        """Deliver a message to a runtime's agent inbox (in-loop steering).

        Goes through MailboxService so require_ack / receipts behave like
        any other mailbox message.
        """
        runtime_id = params.get("runtime_id", "")
        record = self._require_runtime(runtime_id)
        receipt = self._svc.send(
            session_id=record.session_id,
            from_id=params.get("from", "manager"),
            to_id=record.agent_id,
            subject=params.get("subject", "steer"),
            body=params.get("body", ""),
            kind=params.get("kind", "TASK"),
            reply_to=params.get("reply_to", ""),
            run_id=params.get("run_id", ""),
            request_id=params.get("request_id", ""),
            require_ack=bool(params.get("require_ack", False)),
        )
        if receipt.status == "failed":
            raise GatewayError(ERR_NOT_FOUND, receipt.error or "runtime_send failed")
        return {"msg_id": receipt.msg_id, "status": receipt.status}

    def runtime_probe(self, params: dict) -> dict:
        runtime_id = params.get("runtime_id", "")
        record = self._require_runtime(runtime_id)
        try:
            from codeagent.runtime.registry import RuntimeRegistry

            health = RuntimeRegistry().probe(runtime_id)
        except Exception as exc:
            # P3-a: log probe fallback so sink assembly failures aren't silent.
            log.warning("gateway: runtime_probe registry probe failed for %s: %s", runtime_id, exc)
            # A2: status='unknown' (park-restored placeholder) must NEVER
            # report alive without a REAL probe — only genuinely registered
            # runtimes (active + heartbeat) get the liveness fallback.
            alive = (
                record.status == "active"
                and record.last_activity > 0
                and (time.time() - record.last_activity) < 120
            )
            health = {
                "alive": alive,
                "reason": f"registry probe unavailable: {exc}",
                "status": record.status,
            }
        return {
            "runtime_id": runtime_id,
            "generation": record.generation,
            "health": health,
        }

    def runtime_stop(self, params: dict) -> dict:
        runtime_id = params.get("runtime_id", "")
        record = self._require_runtime(runtime_id)
        reason = params.get("reason", "stopped")
        try:
            from codeagent.runtime.registry import RuntimeRegistry

            RuntimeRegistry().stop(runtime_id, reason)
        except Exception as exc:
            log.warning("gateway: runtime stop failed (marking stopped anyway): %s", exc)
        # P2-2: lock status mutation.
        with self._runtimes_lock:
            record.status = "stopped"
        # A3: the record stays in-memory (stopped) so the stopping caller can
        # read back the state; the sweep removes it shortly after so stopped
        # runtimes never linger or get aggregated by runtime_info.
        self._events.append_local(RuntimeEventDraft(
            runtime_id=runtime_id, generation=record.generation,
            session_id=record.session_id, agent_id=record.agent_id,
            request_id="", run_id="", kind="RUNTIME_STATE",
            created_at=datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT),
            payload={"state": "stopped", "reason": reason},
        ))
        return {"runtime_id": runtime_id, "status": "stopped"}

    def runtime_info(self, params: dict) -> dict:
        """Aggregate runtime observability for park info (by review_key or id)."""
        runtime_id = params.get("runtime_id", "")
        review_key = params.get("review_key", "")
        # P2-2: snapshot record under lock to avoid dict resize during iteration.
        with self._runtimes_lock:
            record = None
            if runtime_id:
                record = self._runtimes.get(runtime_id)
                # A3: never report a stopped runtime as the answer — the sweep
                # cleans stopped records; before it does, treat them as absent.
                if record is not None and record.status == "stopped":
                    record = None
            elif review_key:
                # A3: prefer the LIVE record (registered by plugin/adoption) over
                # stopped or park-restored placeholders: filter stopped records,
                # then take the newest by last_activity desc (restored records
                # carry last_activity=0 so a live registration wins naturally).
                candidates = [r for r in self._runtimes.values() if r.review_key == review_key]
                live = [r for r in candidates if r.status != "stopped"]
                record = max(live, key=lambda r: r.last_activity) if live else None
        if record is None:
            raise GatewayError(ERR_NOT_FOUND, f"no runtime for review_key={review_key!r} runtime_id={runtime_id!r}")
        # A8: aggregate ONLY this generation's events — a re-registered
        # runtime must not inherit the previous generation's stats.
        agg = self._events.aggregate(record.runtime_id, record.generation)
        try:
            from codeagent.runtime.registry import RuntimeRegistry

            health = RuntimeRegistry().probe(record.runtime_id)
        except Exception as exc:
            # P3-a: log probe fallback so receipt fallback isn't silent.
            log.warning("gateway: runtime_info probe failed for %s: %s", record.runtime_id, exc)
            # Plugin-registered runtimes have no registry handle — fall back
            # to the gateway record's own liveness signal (recent heartbeat).
            # A2: status='unknown' (park-restored placeholder) must never
            # report alive without a REAL probe.
            alive = (
                record.status == "active"
                and record.last_activity > 0
                and (time.time() - record.last_activity) < 120
            )
            health = {
                "alive": alive,
                "reason": f"registry probe unavailable: {exc}",
                "status": record.status,
                "last_activity_s": int(time.time() - record.last_activity) if record.last_activity > 0 else None,
            }
        return {
            "runtime_id": record.runtime_id,
            "session_id": record.session_id,
            "agent_id": record.agent_id,
            "generation": record.generation,
            "review_key": record.review_key,
            "backend_session_id": record.backend_session_id,
            "status": record.status,
            # A12: elapsed is now the real runtime age (since started_at);
            # idle seconds move to their own field. elapsed stays for
            # compatibility with callers of the old (mislabeled) field.
            "started_at": record.started_at,
            "elapsed": max(0, int(time.time() - record.started_at)) if record.started_at else 0,
            # A12: idle_s is only meaningful after a first activity signal —
            # placeholders (last_activity=0) report None, like health.last_activity_s.
            "idle_s": max(0, int(time.time() - record.last_activity)) if record.last_activity > 0 else None,
            "last_event": agg,
            "tool_stats": {"tool_count": agg.get("tool_count", 0), "error_count": agg.get("error_count", 0)},
            "runtime_health": health,
        }

    # ── events ─────────────────────────────────────────────────────────

    def events_list(self, params: dict) -> dict:
        events, cursor = self._events.list_after(
            cursor=int(params.get("cursor", 0)),
            filters=params.get("filters"),
            limit=int(params.get("limit", 200)),
            session_id=params.get("session_id", ""),
            runtime_id=params.get("runtime_id", ""),
        )
        return {
            "events": [e.to_dict() for e in events],
            "cursor": cursor,
        }

    # ── message (MailboxService bridge) ────────────────────────────────

    def message_send(self, params: dict) -> dict:
        receipt = self._svc.send(
            session_id=params.get("session_id", ""),
            from_id=params.get("from", ""),
            to_id=params.get("to", ""),
            subject=params.get("subject", ""),
            body=params.get("body", ""),
            kind=params.get("kind", "REPORT"),
            reply_to=params.get("reply_to", ""),
            run_id=params.get("run_id", ""),
            request_id=params.get("request_id", ""),
            require_ack=bool(params.get("require_ack", False)),
        )
        if receipt.status == "failed":
            raise GatewayError(ERR_NOT_FOUND, receipt.error or "message_send failed")
        return {"msg_id": receipt.msg_id, "status": receipt.status, "detail": receipt.detail}

    def message_peek(self, params: dict) -> dict:
        return self._svc.peek(
            params.get("session_id", ""), params.get("agent", ""),
            int(params.get("max_messages", 5)), int(params.get("max_subject", 80)),
        )

    def message_read(self, params: dict) -> dict:
        from codeagent.mailbox.service import ACK_ROUTE_UNRESOLVED

        outcome = self._svc.read(
            params.get("session_id", ""), params.get("agent", ""),
            params.get("owner", ""),
            msg_id=params.get("msg_id", ""),  # P1-8: targeted claim
        )
        if outcome.status == ACK_ROUTE_UNRESOLVED:
            raise GatewayError(ERR_NOT_AUTHORIZED, outcome.error or "ack route unresolved")
        result = {"status": outcome.status, "message": outcome.message}
        if outcome.receipt is not None:
            result["receipt"] = outcome.receipt.to_dict()
        return result

    def message_finalize(self, params: dict) -> dict:
        out = self._store.finalize(
            params.get("session_id", ""), params.get("agent", ""),
            params.get("msg_id", ""), params.get("owner", ""),
        )
        return {"status": out}

    def message_release(self, params: dict) -> dict:
        out = self._store.release(
            params.get("session_id", ""), params.get("agent", ""),
            params.get("msg_id", ""), params.get("owner", ""),
        )
        return {"status": out}

    # ── artifact verification ──────────────────────────────────────────

    def artifact_verify(self, params: dict) -> dict:
        """Verify a local artifact and record the verdict in the RequestLedger.

        Params::

            {session_id, request_id, run_id, path, sha256, size,
             agent_id?}

        If *agent_id* is omitted the service scans the session directory
        to locate the agent whose events tree contains *request_id*.
        """
        from codeagent.artifact import verify_artifact
        from codeagent.mailbox.store import RequestLedger

        session_id: str = params.get("session_id", "")
        request_id: str = params.get("request_id", "")
        run_id: str = params.get("run_id", "")
        artifact_path: str = params.get("path", "")
        sha256: str = params.get("sha256", "")
        size: int = int(params.get("size", 0))
        agent_id: str = params.get("agent_id", "")

        if not all([session_id, request_id, run_id, artifact_path, sha256]):
            raise GatewayError(ERR_PROTOCOL, "artifact.verify: missing required params")

        # Resolve agent_id — caller may omit it; scan session directory.
        sd = self._store.session_dir(session_id)
        if not agent_id:
            # Walk session_dir/<agent_id>/events/<request_id> to find the agent.
            if sd.is_dir():
                for child in sorted(sd.iterdir()):
                    if not child.is_dir():
                        continue
                    if (child / "events" / request_id).is_dir():
                        agent_id = child.name
                        break
            if not agent_id:
                raise GatewayError(
                    ERR_NOT_FOUND,
                    f"no agent found with events for request {request_id!r} "
                    f"in session {session_id!r}",
                )

        ledger = RequestLedger(sd, agent_id)

        # Verify the artifact file.
        # verify_artifact raises ValueError for: not-a-file, size mismatch,
        # sha256 mismatch.  We let file-not-found propagate as-is; only
        # hash/size mismatches are recorded as BLOCKED.
        try:
            verified = verify_artifact(Path(artifact_path), sha256, size)
        except ValueError as exc:
            if "not a file" in str(exc):
                raise
            verified = False

        result = ledger.record_artifact_verdict(request_id, run_id, verified)

        terminal = result["terminal"]
        # Map terminal CAS result to return status.
        if not result["cas"]:
            # Already in a terminal state — report "EXISTS" to signal idempotency.
            return {"verified": verified, "terminal": terminal, "status": "EXISTS"}
        return {"verified": verified, "terminal": terminal}

    # ── park bridge ────────────────────────────────────────────────────

    def park_revive(self, params: dict) -> dict:
        from codeagent.park.router import park_revive

        rv = park_revive(params.get("review_key", ""), params.get("prompt", "") or "")
        return {"method": rv.method, "success": rv.success, "context": rv.context}

    def park_release(self, params: dict) -> dict:
        from codeagent.park.registry import ParkRegistry

        ParkRegistry().release(params.get("review_key", ""))
        return {"released": params.get("review_key", "")}

    # ── P8.1 hub cross-device routing ──────────────────────────────────

    def hub_register(self, params: dict) -> dict:
        """Register a hub peer ↔ swarm agent mapping.

        Also registers the agent in the kernel routing table so
        EngineDeliverySink resolves the remote host for cross-device
        delivery.
        """
        peer_id = params.get("peer_id", "")
        session_id = params.get("session_id", "")
        agent_id = params.get("agent_id", "")
        host_alias = params.get("host_alias", "")
        if not peer_id or not session_id or not agent_id or not host_alias:
            raise GatewayError("PROTOCOL", "hub.register requires peer_id/session_id/agent_id/host_alias")
        peer = _HubPeer(
            peer_id=peer_id,
            session_id=session_id,
            agent_id=agent_id,
            host_alias=host_alias,
            mailbox_root=params.get("mailbox_root", ""),
            status="online",
            registered_at=time.time(),
        )
        # P2-2: lock peers dict write.
        with self._peers_lock:
            self._peers[peer_id] = peer
        # Register in the kernel routing so delivery resolves the remote host.
        try:
            from codeagent.swarm.model import AgentLocation, ExecutionMode

            persisted = self._store.read_session(session_id)
            authority = (persisted or {}).get("manager", "hub") if persisted else "hub"
            # Materialize the session in the live kernel WITHOUT hijacking an
            # existing persisted authority.
            if self._kernel.get_session(session_id) is None:
                try:
                    self._kernel.create_session(session_id, authority, [agent_id, "manager"])
                except ValueError:
                    pass  # concurrent creation — proceed to merge
            # Persist the roster/ACL merge (manager + agent) under the
            # existing authority; never replace it.
            if persisted is not None:
                try:
                    self._store.session_init(
                        session_id, authority, [agent_id, "manager"],
                        acl={
                            "authority": authority,
                            "allowed_senders": ["hub", "manager", agent_id],
                            "room_members": ["hub", "manager", agent_id],
                            "policy": "open",
                        },
                    )
                except Exception as exc:
                    log.warning("hub_register: session manifest merge failed for %s: %s", session_id, exc)
            # Existing sessions (live kernel) need the manager merged in-memory
            # too; the persisted session.json merge happens via store on next
            # kernel load.
            session = self._kernel.get_session(session_id)
            if session is not None:
                if "manager" not in session.roster:
                    session.roster.members.append("manager")
                session.acl.allowed_senders = list(set(session.acl.allowed_senders) | {"manager"})
                session.acl.room_members = list(set(session.acl.room_members) | {"manager"})
            self._kernel.register(
                AgentLocation(
                    agent_id=agent_id,
                    host_alias=host_alias,
                    backend="cli",
                    execution_mode=ExecutionMode.MAILBOX_WORKER,
                ),
                session_id,
            )
        except Exception as exc:
            log.warning("hub_register: routing registration failed for %s: %s", peer_id, exc)
        self._save_peers()
        return {"peer_id": peer_id, "status": peer.status, "agent_id": agent_id}

    def hub_send(self, params: dict) -> dict:
        """Cross-device hub message send (peer → remote agent inbox).

        Checks presence (P8.3) first — offline peers fail fast, never blind
        send. Routes through the kernel's EngineDeliverySink so the durable
        outbox → transport → remote inbox path is used; require_ack flows
        through protocol v2.
        """
        peer_id = params.get("peer_id", "")
        # P2-2: lock peers dict read.
        with self._peers_lock:
            peer = self._peers.get(peer_id)
        if peer is None:
            raise GatewayError(ERR_NOT_FOUND, f"unknown peer: {peer_id}")
        if peer.status == "offline":
            # P3-f: raise instead of returning ok=True for unreachable peer.
            raise GatewayError(ERR_NOT_FOUND, f"peer {peer_id} is offline")

        from codeagent.swarm.model import Envelope

        env = Envelope(
            subject=params.get("subject", "hub-message"),
            body=params.get("content", params.get("body", "")),
            kind=params.get("kind", "TASK"),
            run_id=params.get("run_id", "") or f"run-{uuid4().hex[:10]}",
            request_id=params.get("request_id", "") or f"req-{uuid4().hex[:10]}",
            require_ack=bool(params.get("require_ack", True)),
        )
        try:
            receipt = self._kernel.direct(
                peer.session_id, params.get("from", "hub"), peer.agent_id, env,
            )
        except (ValueError, PermissionError) as exc:
            raise GatewayError(ERR_NOT_FOUND, f"hub_send failed: {exc}") from exc
        return {"msg_id": receipt.msg_id, "status": receipt.status, "peer_id": peer_id}

    def hub_status(self, params: dict) -> dict:
        """Query peer presence (one peer or all)."""
        peer_id = params.get("peer_id", "")
        if peer_id:
            # P2-2: lock peers dict read.
            with self._peers_lock:
                peer = self._peers.get(peer_id)
            if peer is None:
                raise GatewayError(ERR_NOT_FOUND, f"unknown peer: {peer_id}")
            return {
                "peer_id": peer_id, "status": peer.status,
                "host_alias": peer.host_alias,
                "session_id": peer.session_id, "agent_id": peer.agent_id,
            }
        # P2-2: snapshot peers under lock to avoid dict resize during iteration.
        with self._peers_lock:
            snapshot = list(self._peers.values())
        return {"peers": [
            {"peer_id": p.peer_id, "status": p.status, "host_alias": p.host_alias,
             "session_id": p.session_id, "agent_id": p.agent_id}
            for p in snapshot
        ]}

    def hub_unregister(self, params: dict) -> dict:
        peer_id = params.get("peer_id", "")
        # P2-2: lock peers dict check-then-pop.
        with self._peers_lock:
            if peer_id not in self._peers:
                raise GatewayError(ERR_NOT_FOUND, f"unknown peer: {peer_id}")
            peer = self._peers.pop(peer_id)
        self._save_peers()
        # Clean the kernel routing entry too (unregister from the session).
        try:
            self._kernel.unregister(peer.session_id, peer.agent_id)
        except Exception as exc:
            log.warning("hub_unregister: kernel unregister failed for %s: %s", peer_id, exc)
        return {"peer_id": peer_id, "unregistered": True}

    # ── actas: runtime role exclusivity (session.claim) ───────────────

    def session_claim(self, params: dict) -> dict:
        """Claim exclusive runtime authority over an agent role in a session.

        actas 运行时独占（agmsg actas 借鉴）：同一 session 的同一 agent_id
        在同一时刻只能被一个 runtime 认领。claim 带 owner（runtime_id）与
        TTL（默认 3600s）；同 owner 重复 claim 幂等续期；不同 owner 抢占
        已被认领的角色 → ERR_PROTOCOL_CONFLICT。release 显式释放。
        """
        session_id = params.get("session_id", "")
        agent_id = params.get("agent_id", "")
        owner = params.get("owner", "")  # runtime_id or peer_id
        ttl = float(params.get("ttl", 3600))
        if not session_id or not agent_id or not owner:
            raise GatewayError("PROTOCOL", "session.claim requires session_id/agent_id/owner")
        # P3-e: TTL bounds — must be positive and within sane upper limit.
        _CLAIM_TTL_MAX = 86400  # 24h upper bound
        if ttl <= 0 or ttl > _CLAIM_TTL_MAX:
            raise GatewayError(
                ERR_PROTOCOL,
                f"session.claim ttl must be in (0, {_CLAIM_TTL_MAX}]; got {ttl}",
            )
        now = time.time()
        claim_key = f"{session_id}:{agent_id}"
        with self._runtimes_lock:
            existing = self._claims.get(claim_key)
            if existing is not None and existing["owner"] != owner:
                if existing["expires_at"] > now:
                    raise GatewayError(
                        ERR_PROTOCOL_CONFLICT,
                        f"role {agent_id} in {session_id} already claimed by {existing['owner']} "
                        f"until {existing['expires_at']:.0f}",
                    )
                # expired claim → takeover allowed
            self._claims[claim_key] = {"owner": owner, "claimed_at": now, "expires_at": now + ttl}
            # P3-e: lock read — return the persisted claim, not a stale dict ref.
            persisted = self._claims[claim_key]
        return {
            "session_id": session_id, "agent_id": agent_id,
            "owner": owner, "expires_at": persisted["expires_at"],
        }

    def session_release(self, params: dict) -> dict:
        session_id = params.get("session_id", "")
        agent_id = params.get("agent_id", "")
        owner = params.get("owner", "")
        claim_key = f"{session_id}:{agent_id}"
        with self._runtimes_lock:
            existing = self._claims.get(claim_key)
            if existing is None:
                return {"released": False, "reason": "no claim"}
            if owner and existing["owner"] != owner:
                raise GatewayError(ERR_NOT_AUTHORIZED, f"claim owned by {existing['owner']}, not {owner}")
            self._claims.pop(claim_key, None)
        return {"released": True, "session_id": session_id, "agent_id": agent_id}

    # ── write merge (cross-device) ──────────────────────────────────────

    @staticmethod
    def write_parse_body(body: str) -> dict:
        """Parse a TASK/REPORT body JSON for merge fields.

        Returns a dict with ``base_revision``, ``target_path``, and
        ``artifact_id`` if present and valid; returns ``{}`` on any
        parse or schema error (never raises).
        """
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(data, dict):
            return {}
        result: dict = {}
        for key in ("base_revision", "target_path", "artifact_id"):
            val = data.get(key)
            if isinstance(val, str) and val:
                result[key] = val
        return result

    def write_merge(self, params: dict) -> dict:
        """Manager-side merge guard for cross-device writes.

        Params: {session_id, request_id, run_id, target_path,
                 base_revision, artifact_sha256, body}

        If the same ``target_path`` already has a recorded
        ``artifact_sha256`` that differs, raises
        ``ERR_PROTOCOL_CONFLICT``.  Otherwise records the mapping
        and returns ``{merged: True}``.
        """
        session_id = params.get("session_id", "")
        target_path = params.get("target_path", "")
        artifact_sha256 = params.get("artifact_sha256", "")
        if not session_id or not target_path or not artifact_sha256:
            raise GatewayError(
                ERR_NOT_FOUND,
                "write.merge requires session_id, target_path, artifact_sha256",
            )
        key = (session_id, target_path)
        # P2-3: lock check-then-set to prevent concurrent conflict writes
        # both seeing no conflict and both writing (last-write-wins).
        with self._merges_lock:
            existing = self._merges.get(key)
            if existing is not None and existing != artifact_sha256:
                raise GatewayError(
                    ERR_PROTOCOL_CONFLICT,
                    f"conflict on {target_path!r}: existing sha256={existing!r} "
                    f"!= incoming sha256={artifact_sha256!r}",
                )
            self._merges[key] = artifact_sha256
        self._save_merges()
        return {"merged": True}

    def _save_merges(self) -> None:
        """Persist merge records (conflict detection survives restarts)."""
        try:
            self._peers_file.parent.mkdir(parents=True, exist_ok=True)
            # P2-2: snapshot merges under lock; write file outside lock.
            with self._merges_lock:
                data = [{"session_id": s, "target_path": t, "artifact_sha256": h}
                        for (s, t), h in self._merges.items()]
            tmp = self._peers_file.with_name("merges.json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            tmp.replace(self._peers_file.with_name("merges.json"))
        except OSError as exc:
            log.warning("merges persist failed: %s", exc)

    def _restore_merges(self) -> None:
        try:
            p = self._peers_file.with_name("merges.json")
            if not p.exists():
                return
            data = json.loads(p.read_text(encoding="utf-8"))
            # P2-2: lock merges dict writes during restore.
            with self._merges_lock:
                for d in data:
                    sid = d.get("session_id", "")
                    tgt = d.get("target_path", "")
                    h = d.get("artifact_sha256", "")
                    if sid and tgt and h:
                        self._merges[(sid, tgt)] = h
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("merges restore failed: %s", exc)

    # P2-13: merge_reset — clear stale merge latches so (session, path)
    # pairs don't accumulate indefinitely.  The manager calls this after
    # confirming the merge was written to the repo, or when abandoning
    # a merge attempt.
    def merge_reset(self, params: dict) -> dict:
        """Clear merge conflict records.

        Params: {session_id, target_path?}
        If ``target_path`` is given, clears only that (session, path) pair.
        Otherwise clears all records for the session.
        Returns ``{reset: <count>}``.
        """
        session_id = params.get("session_id", "")
        target_path = params.get("target_path", "")
        if not session_id:
            raise GatewayError(ERR_NOT_FOUND, "merge.reset requires session_id")
        removed = 0
        with self._merges_lock:
            if target_path:
                if self._merges.pop((session_id, target_path), None) is not None:
                    removed = 1
            else:
                keys = [k for k in self._merges if k[0] == session_id]
                for k in keys:
                    del self._merges[k]
                    removed += 1
        if removed:
            self._save_merges()
        return {"reset": removed}

    # ── internals ──────────────────────────────────────────────────────

    def _require_runtime(self, runtime_id: str) -> RuntimeRecord:
        # P2-2: lock runtime dict read.
        with self._runtimes_lock:
            record = self._runtimes.get(runtime_id)
        if record is None:
            raise GatewayError(ERR_NOT_FOUND, f"unknown runtime: {runtime_id}")
        return record

    # ── dispatch ───────────────────────────────────────────────────────

    def dispatch(self, method: str, params: dict) -> dict:
        """Route a gateway method name to its handler (fail-closed)."""
        handlers = {
            "capabilities.get": self.capabilities,
            "session.ensure": self.session_ensure,
            "runtime.register": self.runtime_register,
            "runtime.declare": self.runtime_declare,  # P3: weak presence declaration
            "runtime.spawn": self.runtime_spawn,
            "runtime.heartbeat": self.runtime_heartbeat,
            "runtime.event": self.runtime_event,
            "runtime.send": self.runtime_send,
            "runtime.probe": self.runtime_probe,
            "runtime.stop": self.runtime_stop,
            "runtime.info": self.runtime_info,
            "runtime.status": self.runtime_status,
            "runtime.list": self.runtimes_list,
            "events.list": self.events_list,
            "message.send": self.message_send,
            "message.peek": self.message_peek,
            "message.read": self.message_read,
            "message.finalize": self.message_finalize,
            "message.release": self.message_release,
            "park.revive": self.park_revive,
            "park.release": self.park_release,
            "hub.register": self.hub_register,
            "hub.send": self.hub_send,
            "hub.status": self.hub_status,
            "hub.unregister": self.hub_unregister,
            "session.claim": self.session_claim,
            "session.release": self.session_release,
            "write.merge": self.write_merge,
            "write.merge_reset": self.merge_reset,  # P2-13
            "artifact.verify": self.artifact_verify,
        }
        handler = handlers.get(method)
        if handler is None:
            raise GatewayError("PROTOCOL", f"unknown gateway method: {method}")
        return handler(params)
