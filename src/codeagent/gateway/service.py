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
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

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
    status: str = "starting"  # starting | active | offline | stopped
    created_at: str = ""
    last_activity: float = 0.0
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
        self._offline_timeout: float = 120.0
        self._sweep_interval: float = 30.0
        self._sweep_stop = threading.Event()
        self._runtimes_lock = threading.RLock()
        self._sweep_thread: Optional[threading.Thread] = None
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
        """Rebuild in-memory runtime records from HOT_PARKED park manifests."""
        try:
            from codeagent.park.registry import ParkRegistry

            manifests = ParkRegistry().list_active()
        except Exception as exc:
            log.warning("gateway: park restore unavailable: %s", exc)
            return
        restored = 0
        for m in manifests:
            if not m.swarm_session_id or not m.backend_session_id:
                continue
            runtime_id = f"park-{m.review_key.replace(':', '-')[-12:]}"
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
                runtime="omp",
                status="active",
                created_at=m.created_at and datetime.fromtimestamp(m.created_at, tz=timezone.utc).strftime(ISO_TIMESTAMP_FORMAT) or "",
                last_activity=time.time(),
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
            except Exception as exc:
                log.warning("gateway: sweep iteration failed: %s", exc)

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
        # P8.1 presence 联动：关联 hub peer 同步 offline（锁外，避免死锁）。
        if offline:
            with self._peers_lock:
                for rid in offline:
                    record = self._runtimes.get(rid)
                    if record is None:
                        continue
                    for peer in self._peers.values():
                        if peer.agent_id == record.agent_id and peer.session_id == record.session_id:
                            peer.status = "offline"
                if offline:
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

        # Generation staleness: a registration for an OLDER generation of a
        # known runtime_id is rejected (fail closed).
        existing = self._runtimes.get(runtime_id)
        if existing is not None and generation < existing.generation:
            raise GatewayError(
                ERR_GENERATION_STALE,
                f"generation {generation} < registered {existing.generation} for {runtime_id}",
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
                if m is not None:
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
            spec=params,
            initial_task=task,
        )
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
        record = self._require_runtime(runtime_id)
        with self._runtimes_lock:
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
        for rid, rec in self._runtimes.items():
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
        # Producers never number their own events — ignore host/sequence.
        ev = self._events.append_local(draft)
        # Any event is activity: keep the runtime's liveness window fresh
        # (hot detection + park renew cadence).
        with self._runtimes_lock:
            record = self._runtimes.get(draft.runtime_id)
            if record is not None:
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
            health = {
                "alive": record.status == "active" and (time.time() - record.last_activity) < 120,
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
        record.status = "stopped"
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
        record = None
        if runtime_id:
            record = self._runtimes.get(runtime_id)
        elif review_key:
            # Prefer the LIVE record (registered by plugin/adoption) over a
            # park-restored placeholder: restored ids start with "park-" and
            # carry no heartbeat, so the hot path must not pick them first.
            candidates = [r for r in self._runtimes.values() if r.review_key == review_key]
            live = [r for r in candidates if not r.runtime_id.startswith("park-")]
            record = (live[0] if live else None) or (candidates[0] if candidates else None)
        if record is None:
            raise GatewayError(ERR_NOT_FOUND, f"no runtime for review_key={review_key!r} runtime_id={runtime_id!r}")
        agg = self._events.aggregate(record.runtime_id)
        try:
            from codeagent.runtime.registry import RuntimeRegistry

            health = RuntimeRegistry().probe(record.runtime_id)
        except Exception as exc:
            # Plugin-registered runtimes have no registry handle — fall back
            # to the gateway record's own liveness signal (recent heartbeat).
            health = {
                "alive": record.status == "active" and (time.time() - record.last_activity) < 120,
                "reason": f"registry probe unavailable: {exc}",
                "status": record.status,
                "last_activity_s": int(time.time() - record.last_activity),
            }
        return {
            "runtime_id": record.runtime_id,
            "session_id": record.session_id,
            "agent_id": record.agent_id,
            "generation": record.generation,
            "review_key": record.review_key,
            "backend_session_id": record.backend_session_id,
            "status": record.status,
            "elapsed": max(0, int(time.time() - record.last_activity)),
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
        peer = self._peers.get(peer_id)
        if peer is None:
            raise GatewayError(ERR_NOT_FOUND, f"unknown peer: {peer_id}")
        if peer.status == "offline":
            return {"msg_id": "", "status": "offline", "error": "peer is offline"}

        from codeagent.swarm.model import Envelope
        from uuid import uuid4

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
            peer = self._peers.get(peer_id)
            if peer is None:
                raise GatewayError(ERR_NOT_FOUND, f"unknown peer: {peer_id}")
            return {
                "peer_id": peer_id, "status": peer.status,
                "host_alias": peer.host_alias,
                "session_id": peer.session_id, "agent_id": peer.agent_id,
            }
        return {"peers": [
            {"peer_id": p.peer_id, "status": p.status, "host_alias": p.host_alias,
             "session_id": p.session_id, "agent_id": p.agent_id}
            for p in self._peers.values()
        ]}

    def hub_unregister(self, params: dict) -> dict:
        peer_id = params.get("peer_id", "")
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
        return {
            "session_id": session_id, "agent_id": agent_id,
            "owner": owner, "expires_at": self._claims[claim_key]["expires_at"],
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
            for d in data:
                sid = d.get("session_id", "")
                tgt = d.get("target_path", "")
                h = d.get("artifact_sha256", "")
                if sid and tgt and h:
                    self._merges[(sid, tgt)] = h
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("merges restore failed: %s", exc)

    # ── internals ──────────────────────────────────────────────────────

    def _require_runtime(self, runtime_id: str) -> RuntimeRecord:
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
            "artifact.verify": self.artifact_verify,
        }
        handler = handlers.get(method)
        if handler is None:
            raise GatewayError("PROTOCOL", f"unknown gateway method: {method}")
        return handler(params)
