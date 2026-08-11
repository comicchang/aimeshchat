"""Delivery engine — durable outbox → transport → remote inbox.

Provides at-least-once cross-host message delivery with:
    - Durable outbox write (fsync + atomic replace) before any transport
    - Idempotency via msg_id dedup in both outbox and remote inbox
    - Retry support via ``flush()`` for pending outbox entries
    - Status tracking: accepted → delivered → consumed

The DeliveryEngine is the ``DeliverySink`` interface consumed by
SwarmKernel (C1): ``deliver()`` + ``ack()``.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from codeagent.constants import ISO_TIMESTAMP_FORMAT
from codeagent.mailbox.protocol import Message
from codeagent.mailbox.store import MailboxStore

if False:  # TYPE_CHECKING
    from codeagent.transport.router import TransportRouter

log = logging.getLogger(__name__)


# ── Receipt ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SendReceipt:
    """Return value from ``deliver()``.

    ``status`` is one of:
        - "accepted":  durable outbox written; transport not attempted or failed
        - "delivered": remote inbox write confirmed
        - "failed":    validation error; message not accepted
    ``queued`` is True when the envelope is in the outbox but remote delivery
    has not yet succeeded (caller should retry via ``flush()``).
    """

    status: str  # "accepted" | "delivered" | "failed"
    msg_id: str = ""
    error: str = ""
    queued: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "msg_id": self.msg_id,
            "error": self.error,
            "queued": self.queued,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SendReceipt:
        return cls(
            status=d.get("status", "accepted"),
            msg_id=d.get("msg_id", ""),
            error=d.get("error", ""),
            queued=d.get("queued", False),
        )


# ── DeliveryEngine ─────────────────────────────────────────────────────


class DeliveryEngine:
    """Durable outbox → transport → remote inbox.

    Lifecycle::

        engine = DeliveryEngine(mailbox_store, transport_router, outbox_root)
        receipt = engine.deliver(session_id, target, envelope)
        # receipt.status == "accepted" or "delivered"
        # receipt.queued == True means retry via flush()
        engine.ack(session_id, agent, msg_id, "consumed")
    """

    def __init__(
        self,
        mailbox_store: MailboxStore,
        transport_router: Optional[Any] = None,
        outbox_root: Optional[Path] = None,
    ) -> None:
        self._store = mailbox_store
        self._router = transport_router
        self._outbox = outbox_root or (mailbox_store.root / "_outbox")
        # Top3 dead-letter: max attempts before a retryable entry is dead-lettered
        self._max_attempts = 5
        self._backoff_base_s = 5  # exponential: 5, 10, 20, 40, 80…
        # A6: TTL retention for terminal outbox entries (delivered markers
        # + status dirs + delivered envelopes) — swept lazily from flush().
        self._retention_days = 7
        self._sweep_interval_s = 3600.0
        self._last_sweep_ts = 0.0
        # idempotency cache: msg_id → SendReceipt (process-lifetime)
        self._cache: dict[str, SendReceipt] = {}
        # host cache for _resolve_target (alias → HostSpec)
        self._host_cache: dict[str, Any] = {}
        # session-ensure cache: (session_id, host_alias) → True
        self._ensured_sessions: set[tuple[str, str]] = set()
        # full roster cache: session_id → sorted list of agent_ids
        self._session_rosters: dict[str, list[str]] = {}
        # capability cache: host_alias → set of capability strings
        self._host_capabilities: dict[str, set[str]] = {}

    @staticmethod
    def _history_entry(envelope: dict[str, Any], msg_id: str) -> dict[str, Any]:
        """Build a canonical history record preserving ALL message fields.

        C15: Uses Message.from_dict().to_dict() round-trip to preserve
        reply_to, run_id, request_id, trace_id, causation_id, attachments.

        v2: a new send (envelope without protocol_version — kernel/dict
        envelopes don't carry it) must be recorded as protocol_version=2;
        only dicts read from disk keep their original (possibly v1) version.

        Shared by deliver() and flush() so both successful paths append
        identical records — a drift here would silently fail
        append_history's validate_message and lose history.
        """
        from codeagent.mailbox.protocol import PROTOCOL_VERSION
        return Message.from_dict({
            **envelope,
            "msg_id": msg_id,
            "protocol_version": envelope.get("protocol_version", PROTOCOL_VERSION),
        }).to_dict()

    # ── public API ─────────────────────────────────────────────────────

    def deliver(
        self,
        session_id: str,
        target: Any,
        envelope: dict[str, Any],
    ) -> SendReceipt:
        """Deliver *envelope* to *target* host.

        1. Write durable outbox (fsync + atomic replace).
        2. Route to remote transport (one-shot wire or stream push).
        3. Return receipt: delivered on success, accepted+queued on failure.

        Idempotency: if *envelope.msg_id* already exists in the outbox,
        returns the cached receipt without re-sending.
        """
        # Validate envelope before writing anything
        if not isinstance(envelope, dict):
            return SendReceipt(status="failed", error="envelope must be a dict")

        msg_id = envelope.get("msg_id", "")
        sid = envelope.get("session_id", session_id)
        if not msg_id:
            return SendReceipt(status="failed", error="envelope missing msg_id")

        # ── Idempotency check ──────────────────────────────────────────
        cached = self._check_idempotency(sid, msg_id)
        if cached is not None:
            return cached

        # ── 1. Durable outbox write (fsync before transport) ───────────
        try:
            outbox_path = self._write_outbox(sid, msg_id, envelope)
        except Exception as exc:
            self._cache[msg_id] = SendReceipt(status="failed", error=str(exc))
            return self._cache[msg_id]

        accepted = SendReceipt(status="accepted", msg_id=msg_id, queued=True)
        self._cache[msg_id] = accepted

        # ── 2. Route to remote transport ───────────────────────────────
        host_alias = getattr(target, "host_alias", None) or getattr(target, "ssh_alias", "")
        from codeagent.domain import HostSpec, resolve_is_local
        is_local_host = isinstance(target, HostSpec) and resolve_is_local(target)
        if not host_alias or is_local_host:
            # Local delivery: 统一走 LocalTransport.mailbox()（内部复用
            # mailbox.cli.main，与远程 SSH transport 共用同一 args 构造），
            # 不再 inline store.send。durable outbox 保证不丢；msg_id 幂等。
            # is_local_host: 本机 repo-map host（mac=OA-MIANYIN-MAC）必须
            # 留在本地——经 transport 会 SSH 到别名而失败。
            try:
                from codeagent.transport.local import LocalTransport
                local_host = HostSpec(name="__local__", ssh_alias="__local__", hostnames=())
                local_args = self._build_mailbox_args({**envelope, "msg_id": msg_id})
                code, out, err = LocalTransport().mailbox(
                    local_host, local_args, mailbox_root=str(self._store.root)
                )
                if code != 0:
                    raise RuntimeError(
                        f"local mailbox send failed (exit {code}): {err or out}"
                    )
            except Exception as exc:
                log.warning("DeliveryEngine: local inbox write failed: %s", exc)
                self._write_status(sid, msg_id, "local_delivery_failed", str(exc))
                return accepted
            self._mark_delivered(sid, msg_id)
            delivered = SendReceipt(status="delivered", msg_id=msg_id)
            self._cache[msg_id] = delivered
            return delivered

        try:
            self._remote_send(target, envelope)
        except Exception as exc:
            # Transport failure: outbox stays pending for flush()
            log.warning("DeliveryEngine: transport failed for %s: %s", msg_id, exc)
            self._write_status(sid, msg_id, "transport_failed", str(exc))
            return accepted

        # ── 3. Transport success — mark delivered + history ────────────
        self._mark_delivered(sid, msg_id)
        # Canonical session history: local sends get it via store.send();
        # remote sends must append here or swarm cross-host fan-out leaves
        # no trace in history/.
        try:
            self._store.append_history(sid, self._history_entry(envelope, msg_id))
        except Exception as exc:
            log.warning("DeliveryEngine: history append failed: %s", exc)
        delivered = SendReceipt(status="delivered", msg_id=msg_id)
        self._cache[msg_id] = delivered
        return delivered

    # ── DeliverySink bridge (SwarmKernel interface) ───────────────────

    def deliver_sink(
        self,
        session_id: str,
        target_agent: str,
        envelope: Any,
        msg_id: str,
        created_at: str,
        from_id: str,
    ) -> SendReceipt:
        """Bridge to ``DeliverySink`` protocol used by SwarmKernel.

        Converts the 6-param kernel call into a dict envelope and delegates
        to ``deliver()``.  The *target_agent* is resolved via the host cache
        (populated at wiring time or by ``cache_host``).
        """
        # Build dict envelope from Envelope object or pass through
        if hasattr(envelope, 'subject'):
            atts = getattr(envelope, 'attachments', None)
            env_dict: dict[str, Any] = {
                "session_id": session_id,
                "from": from_id,
                "to": target_agent,
                "subject": envelope.subject,
                "body": envelope.body,
                "kind": getattr(envelope, 'kind', 'TASK'),
                "reply_to": getattr(envelope, 'reply_to', ''),
                "run_id": getattr(envelope, 'run_id', ''),
                "request_id": getattr(envelope, 'request_id', ''),
                "trace_id": getattr(envelope, 'trace_id', ''),
                "causation_id": getattr(envelope, 'causation_id', ''),
                "require_ack": getattr(envelope, 'require_ack', False),
                "receipt_type": getattr(envelope, 'receipt_type', ''),
                "msg_id": msg_id,
                "created_at": created_at,
                "_target_agent": target_agent,
            }
            if atts:
                env_dict["attachments"] = [
                    a.to_dict() if hasattr(a, "to_dict") else a for a in atts
                ]
        else:
            env_dict = dict(envelope) if not isinstance(envelope, dict) else envelope
            env_dict.setdefault("msg_id", msg_id)
            env_dict.setdefault("created_at", created_at)
            env_dict.setdefault("session_id", session_id)
            env_dict.setdefault("from", from_id)
            env_dict.setdefault("to", target_agent)
            env_dict.setdefault("trace_id", "")
            env_dict.setdefault("causation_id", "")
            env_dict["_target_agent"] = target_agent

        # Resolve target_agent → HostSpec via cache or store local target
        host = self._host_cache.get(target_agent)
        if host is not None:
            # Record the resolved host so the durable outbox entry keeps it:
            # flush() reads `_target_host` to re-send on retry — without it
            # every retry is silently skipped.
            env_dict["_target_host"] = getattr(host, "host_alias", "") or getattr(host, "ssh_alias", "")
            return self.deliver(session_id, host, env_dict)
        else:
            # No cached host — deliver locally (outbox write only)
            return self.deliver(session_id, target_agent, env_dict)

    def cache_host(self, agent_id: str, host: Any) -> None:
        """Register an agent_id → HostSpec mapping for sink resolution."""
        self._host_cache[agent_id] = host

    def cache_roster(self, session_id: str, roster: list[str]) -> None:
        """Store full roster for a session (called by kernel wiring or CLI)."""
        self._session_rosters[session_id] = sorted(set(roster))

    def _check_capability(self, host: Any) -> None:
        """Fail-closed if host transport lacks 'mailbox' capability.

        Caches capabilities per host_alias. Raises RuntimeError if
        'mailbox' is missing — the message stays queued for flush().
        """
        if self._router is None:
            return  # no router → cannot check; allow (local/dev mode)
        host_alias = getattr(host, "ssh_alias", None) or getattr(host, "name", None) or ""
        if not host_alias:
            return

        caps = self._host_capabilities.get(host_alias)
        if caps is None:
            try:
                caps = self._router.capabilities(host)
            except Exception:
                caps = {"mailbox"}  # assume capable on check failure
            self._host_capabilities[host_alias] = caps

        if "mailbox" not in caps:
            raise RuntimeError(
                f"host '{host_alias}' transport lacks 'mailbox' capability"
            )

    def _ensure_remote_session(
        self,
        session_id: str,
        host: Any,
        manager: str,
        roster: list[str],
        transport: Any = None,
    ) -> None:
        """Ensure remote host has the session. Called once per (session, host).

        Builds full roster list from ``roster`` (or envelope from/to as
        degraded fallback).  Idempotent: 'already exists' is success.
        Caches in ``_ensured_sessions`` so second message to same host
        skips the remote call entirely.
        """
        host_alias = getattr(host, "ssh_alias", None) or getattr(host, "name", None) or ""
        cache_key = (session_id, host_alias)
        if cache_key in self._ensured_sessions:
            return

        if transport is None:
            transport = self._get_transport(host)
        if transport is None:
            raise ValueError(f"no transport for host '{host_alias}'")

        agents_csv = ",".join(sorted(set(roster)))
        init_args = [
            "session-init",
            "--session", session_id,
            "--manager", manager,
            "--agents", agents_csv,
        ]

        # B4-Manifest: 同步 ACL（权威副本在本地 session.json）——否则远端
        # kernel 缺 swarm ACL，restricted policy 在远端恢复 open（控制面分裂）。
        acl = self._local_session_acl(session_id)
        if acl is not None:
            init_args.extend(["--acl", json.dumps(acl, ensure_ascii=False)])

        init_code, init_out, init_err = transport.mailbox(host, init_args)
        if init_code != 0 and "already exists" not in (init_err or init_out or ""):
            raise RuntimeError(
                f"remote session-init failed (exit {init_code}): {init_err or init_out}"
            )

        self._ensured_sessions.add(cache_key)

    def _local_session_acl(self, session_id: str) -> Optional[dict]:
        """Read the local session.json ACL (authority/policy/allowed_senders).

        Returns None if the session has no persisted ACL (legacy sessions
        created before B4-Manifest) — the caller then skips ACL sync.
        """
        try:
            meta = self._store.read_session(session_id)
        except Exception:
            return None
        if meta is None:
            return None
        return meta.get("acl")

    def flush(self, session_id: Optional[str] = None) -> int:
        """Retry all pending outbox entries. Returns count of newly delivered."""
        # A6: lazy TTL sweep — terminal delivered entries/markers older than
        # retention_days are removed before retrying (throttled hourly).
        self._sweep_ttl_lazy()
        sessions = [session_id] if session_id else self._list_sessions()
        delivered_count = 0

        for sid in sessions:
            sd = self._outbox / sid
            if not sd.is_dir():
                continue
            for envelope_file in sorted(sd.glob("*.json")):
                mid = envelope_file.stem
                # Skip already-delivered entries
                marker = sd / f".delivered-{mid}"
                if marker.exists():
                    continue
                # Skip ack-completed entries
                status_dir = sd / f".status-{mid}"
                if status_dir.exists():
                    phase = status_dir / "phase"
                    if phase.exists() and phase.read_text().strip() == "consumed":
                        continue
                # Top3 backoff: skip entries whose retry is not due yet
                if not self._attempt_due(sid, mid):
                    continue

                try:
                    envelope = json.loads(envelope_file.read_bytes())
                except (json.JSONDecodeError, OSError):
                    continue

                host_alias = envelope.get("_target_host", "")
                if not host_alias:
                    log.debug("DeliveryEngine: flush skip %s — no target host", mid)
                    continue

                try:
                    target = self._resolve_target(host_alias)
                except Exception:
                    continue

                from codeagent.domain import HostSpec, resolve_is_local
                if resolve_is_local(target):
                    # 本机 target（repo-map host 的 hostnames 匹配本机，如
                    # mac=OA-MIANYIN-MAC）：统一走 LocalTransport.mailbox()
                    # 直写本地 inbox（与 deliver() 同路径；msg_id 幂等）。
                    try:
                        from codeagent.transport.local import LocalTransport
                        local_host = HostSpec(name="__local__", ssh_alias="__local__", hostnames=())
                        local_args = self._build_mailbox_args({**envelope, "msg_id": mid})
                        code, out, err = LocalTransport().mailbox(
                            local_host, local_args, mailbox_root=str(self._store.root)
                        )
                        if code != 0:
                            raise RuntimeError(
                                f"local mailbox send failed (exit {code}): {err or out}"
                            )
                    except Exception as exc:
                        log.debug("DeliveryEngine: flush local retry failed for %s: %s", mid, exc)
                        self._handle_flush_failure(sid, mid, exc)
                        continue
                    self._mark_delivered(sid, mid)
                    try:
                        self._store.append_history(sid, self._history_entry(envelope, mid))
                    except Exception as exc:
                        log.warning("DeliveryEngine: flush history append failed: %s", exc)
                    self._cache[mid] = SendReceipt(status="delivered", msg_id=mid)
                    delivered_count += 1
                    continue

                try:
                    self._remote_send(target, envelope)
                except Exception as exc:
                    log.debug("DeliveryEngine: flush retry failed for %s: %s", mid, exc)
                    self._handle_flush_failure(sid, mid, exc)
                    continue

                self._mark_delivered(sid, mid)
                # Parity with deliver(): a successful flush retry must also
                # leave a canonical history record (it was missing on the
                # failed first attempt).
                try:
                    self._store.append_history(sid, self._history_entry(envelope, mid))
                except Exception as exc:
                    log.warning("DeliveryEngine: flush history append failed: %s", exc)
                self._cache[mid] = SendReceipt(status="delivered", msg_id=mid)
                delivered_count += 1

        return delivered_count

    def pending(self, session_id: Optional[str] = None) -> list[dict[str, Any]]:
        """Return list of undelivered envelopes from the outbox."""
        sessions = [session_id] if session_id else self._list_sessions()
        results = []

        for sid in sessions:
            sd = self._outbox / sid
            if not sd.is_dir():
                continue
            for envelope_file in sorted(sd.glob("*.json")):
                mid = envelope_file.stem
                marker = sd / f".delivered-{mid}"
                if marker.exists():
                    continue
                try:
                    results.append(json.loads(envelope_file.read_bytes()))
                except (json.JSONDecodeError, OSError):
                    continue

        return results

    def outbox_stats(self, session_id: Optional[str] = None) -> dict[str, int]:
        """Return summary counts for the outbox.

        Returns ``{'pending': N, 'delivered': M}`` where *N* is the number
        of undelivered entries and *M* is the number of delivered markers.
        """
        pending_count = len(self.pending(session_id))
        sessions = [session_id] if session_id else self._list_sessions()
        delivered = 0
        for sid in sessions:
            sd = self._outbox / sid
            if sd.is_dir():
                delivered += len(list(sd.glob('.delivered-*')))
        return {'pending': pending_count, 'delivered': delivered}

    # ── A6: retention sweep ────────────────────────────────────────────

    def _sweep_ttl_lazy(self) -> None:
        """A6: throttled lazy entry point for the outbox TTL sweep.

        Called from ``flush()`` (every kernel/CLI startup); a long-running
        process sweeps at most once per ``_sweep_interval_s``. Failures are
        logged, never fatal to the flush itself.
        """
        now = time.time()
        if now - self._last_sweep_ts < self._sweep_interval_s:
            return
        self._last_sweep_ts = now
        try:
            removed = self.sweep(retention_days=self._retention_days)
            if removed:
                log.info("DeliveryEngine: TTL sweep removed %d outbox entr%s",
                         removed, "y" if removed == 1 else "ies")
        except Exception as exc:
            log.warning("DeliveryEngine: TTL sweep failed: %s", exc)

    def sweep(self, retention_days: int = 7) -> int:
        """A6: TTL cleanup for terminal outbox entries.

        Per session dir under ``_outbox``, removes:
          - ``.delivered-<msg_id>`` markers (and their envelope
            ``<msg_id>.json``) older than *retention_days* — a delivered
            entry's durable record is the session ``history/``, so the
            outbox copy is disposable.
          - ``.status-<msg_id>/`` dirs older than *retention_days*
            (attempt metadata; pending entries keep fresh mtimes — the
            dir's own mtime is taken as max with ``meta.json`` — and are
            never touched).
          - emptied session dirs afterwards.

        Returns the number of files/dirs removed. Never touches pending
        (undelivered) entries — those must survive until flush() delivers.
        """
        import shutil

        cutoff = time.time() - retention_days * 86400
        removed = 0
        if not self._outbox.is_dir():
            return 0
        for sd in sorted(self._outbox.iterdir()):
            if not sd.is_dir() or sd.name.startswith("."):
                continue
            try:
                # Delivered markers + their envelopes (terminal).
                for marker in sd.glob(".delivered-*"):
                    try:
                        if marker.stat().st_mtime >= cutoff:
                            continue
                        mid = marker.name[len(".delivered-"):]
                        marker.unlink(missing_ok=True)
                        removed += 1
                        env = sd / f"{mid}.json"
                        if env.exists():
                            env.unlink(missing_ok=True)
                            removed += 1
                    except OSError:
                        continue
                # Status dirs (attempt/ack metadata).
                for status_dir in sd.glob(".status-*"):
                    try:
                        age_mtime = status_dir.stat().st_mtime
                        meta = status_dir / "meta.json"
                        if meta.exists():
                            age_mtime = max(age_mtime, meta.stat().st_mtime)
                        if age_mtime < cutoff:
                            shutil.rmtree(str(status_dir), ignore_errors=True)
                            removed += 1
                    except OSError:
                        continue
                # Collapse emptied session dirs.
                if not any(sd.iterdir()):
                    sd.rmdir()
            except OSError:
                continue
        return removed

    def ack(
        self,
        session_id: str,
        agent: str,
        msg_id: str,
        phase: str,
    ) -> None:
        """Update delivery status for a message.

        Called by the sender's SwarmKernel when it learns the message has
        progressed through its lifecycle:
            - "accepted":   written to outbox (set automatically by deliver)
            - "delivered":  remote inbox confirmed (set automatically on transport success)
            - "consumed":   recipient has processed the message

        Writes a status marker to the outbox entry for audit.
        """
        self._validate_msg_id(msg_id)
        sd = self._outbox / session_id
        envelope_file = sd / f"{msg_id}.json"
        if not envelope_file.exists():
            raise ValueError(f"outbox entry not found: {msg_id}")

        self._write_status(session_id, msg_id, "ack", phase)
        # Update cache
        if phase == "consumed":
            self._cache[msg_id] = SendReceipt(status="delivered", msg_id=msg_id)

    # ── Durable outbox write ───────────────────────────────────────────

    def _write_outbox(
        self, session_id: str, msg_id: str, envelope: dict[str, Any],
    ) -> Path:
        """Write envelope to durable outbox with fsync + atomic replace.

        Uses O_EXCL tmp file + os.replace (same pattern as store.send).

        Returns the final outbox path.
        """
        sd = self._outbox / session_id
        sd.mkdir(parents=True, exist_ok=True)
        dest = sd / f"{msg_id}.json"
        tmp = sd / f".tmp-{msg_id}.json"

        # Idempotency: if outbox already has this msg_id, skip
        if dest.exists():
            return dest

        payload = json.dumps(envelope, indent=2, ensure_ascii=False)

        with open(tmp, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(dest))
        return dest

    # ── Transport routing ──────────────────────────────────────────────

    def _remote_send(self, target: Any, envelope: dict[str, Any]) -> None:
        """Send envelope to remote host via transport.

        Strategy:
            - If target host is stream-capable AND an SSHStream is available,
              push via stream (fast, persistent connection).
            - Otherwise, one-shot wire invoke via transport.mailbox().

        Raises on transport failure.
        """
        host = self._extract_host(target)
        if host is None:
            raise ValueError("cannot extract HostSpec from target")

        transport = self._get_transport(host)
        if transport is None:
            raise ValueError(f"no transport for host '{host.name}'")

        # ── Capability check (fail-closed) ────────────────────────────
        self._check_capability(host)

        # ── Ensure remote session (idempotent, cached) ───────────────
        sid = envelope.get("session_id", "")
        manager = envelope.get("from", "")
        roster = self._session_rosters.get(sid)
        if roster is None:
            # 进程内缓存为空（CLI 每次新进程）：从本地 store 读 create-session
            # 持久化的权威定义（完整 roster + manager）——degraded from/to
            # fallback 会漏掉 roster 成员，导致远程 session 元数据残缺
            # （后续 "sender not in roster" 误拒绝）。
            meta = None
            try:
                meta = self._store.read_session(sid)
            except Exception:
                pass
            if meta and meta.get("agents"):
                roster = sorted(set(meta["agents"]))
                manager = meta.get("manager") or manager
        if roster is None:
            # Degraded: no cached roster — build from envelope from/to
            roster = sorted({envelope.get("from", ""), envelope.get("to", "")})
        self._ensure_remote_session(sid, host, manager, roster, transport)

        # ── Send envelope ─────────────────────────────────────────────
        args = self._build_mailbox_args(envelope)
        exit_code, stdout, stderr = transport.mailbox(host, args)
        if exit_code != 0:
            raise RuntimeError(
                f"remote mailbox send failed (exit {exit_code}): {stderr or stdout}"
            )

    def _get_transport(self, host: Any) -> Any:
        """Get transport for *host* via router or direct SSHTransport."""
        if self._router is not None:
            return self._router.get(host)
        # Fallback: direct SSHTransport
        from codeagent.transport.ssh import SSHTransport
        return SSHTransport()

    def _extract_host(self, target: Any) -> Any:
        """Extract HostSpec from target (HostSpec, Target, or similar)."""
        # target IS a HostSpec
        if hasattr(target, "ssh_alias") and hasattr(target, "name"):
            return target
        # target is a routing.Target with .host
        if hasattr(target, "host"):
            return target.host
        return None

    def _resolve_target(self, host_alias: str) -> Any:
        """Resolve a host alias back to a HostSpec for retry.

        Checks the host cache first (populated by ``cache_host`` or wiring),
        then the repo-map (real ssh_alias + shell_prefix; a repo-map host
        that is this machine is routed local by ``deliver()``), and finally
        falls back to an ad-hoc HostSpec.
        """
        # Check host cache (populated at wiring time or by cache_host)
        cached = self._host_cache.get(host_alias)
        if cached is not None:
            return cached
        try:
            from codeagent.config.repo_map import load_repo_map
            spec = load_repo_map().hosts.get(host_alias)
            if spec is not None:
                from codeagent.domain import HostSpec
                return HostSpec(
                    name=spec.name,
                    ssh_alias=spec.ssh_alias,
                    hostnames=spec.hostnames,
                    shell_prefix=spec.shell_prefix,
                    fallback_ssh_alias=spec.fallback_ssh_alias,
                )
        except Exception:
            pass
        from codeagent.domain import HostSpec
        return HostSpec(name=host_alias, ssh_alias=host_alias, hostnames=())

    def _build_mailbox_args(self, envelope: dict[str, Any]) -> list[str]:
        """Build CLI args for remote mailbox send."""
        session_id = envelope.get("session_id", "")
        from_id = envelope.get("from", "")
        to_id = envelope.get("to", "")
        subject = envelope.get("subject", "")
        kind = envelope.get("kind", "TASK")
        reply_to = envelope.get("reply_to", "")
        run_id = envelope.get("run_id", "")
        request_id = envelope.get("request_id", "")
        body = envelope.get("body", "")
        attachments = envelope.get("attachments") or []
        msg_id = envelope.get("msg_id", "")
        trace_id = envelope.get("trace_id", "")
        require_ack = bool(envelope.get("require_ack", False))
        receipt_type = envelope.get("receipt_type", "")

        args = [
            "send",
            "--session", session_id,
            "--from", from_id,
            "--to", to_id,
            "--subject", subject,
            "--body", body,
            "--kind", kind,
            "--msg-id", msg_id,
        ]
        if require_ack:
            args.append("--require-ack")
        if receipt_type:
            args.extend(["--receipt-type", receipt_type])
        for att in attachments:
            args.extend(["--attachment", json.dumps(att, ensure_ascii=False)])
        if reply_to:
            args.extend(["--reply-to", reply_to])
        if run_id:
            args.extend(["--run-id", run_id])
        if request_id:
            args.extend(["--request-id", request_id])
        if trace_id:
            args.extend(["--trace-id", trace_id])
        causation_id = envelope.get("causation_id", "")
        if causation_id:
            args.extend(["--causation-id", causation_id])

        return args

    # ── Status markers ─────────────────────────────────────────────────

    def _mark_delivered(self, session_id: str, msg_id: str) -> None:
        """Write delivered marker file."""
        sd = self._outbox / session_id
        sd.mkdir(parents=True, exist_ok=True)
        marker = sd / f".delivered-{msg_id}"
        marker.write_text(
            json.dumps({
                "delivered_at": datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT),
            }),
        )

    def _write_status(
        self, session_id: str, msg_id: str, kind: str, detail: str,
    ) -> None:
        """Write a status marker directory for *msg_id*."""
        sd = self._outbox / session_id
        sd.mkdir(parents=True, exist_ok=True)
        status_dir = sd / f".status-{msg_id}"
        status_dir.mkdir(exist_ok=True)
        (status_dir / "phase").write_text(detail)
        (status_dir / "kind").write_text(kind)
        (status_dir / "timestamp").write_text(
            datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT),
        )

    # ── Top3: retry state machine / dead-letter ────────────────────────

    @staticmethod
    def _is_terminal_error(exc: Exception) -> bool:
        """Classify delivery errors: terminal ones never succeed on retry
        (invalid roster/ACL/capability/idempotency conflict/validation).

        Structured signal first: mailbox CLI exits 2 on ValueError (terminal),
        1 on unknown (retryable). Keyword fallback covers pre-0.2.4 remotes.
        """
        msg = str(exc)
        if "mailbox send failed (exit 2)" in msg:
            return True
        markers = (
            "not in roster",
            "not in channel",
            "lacks 'mailbox' capability",
            "different payload",
            "invalid kind",
            "body exceeds",
            "invalid attachment",
            "sender not in allowed_senders",
        )
        return any(m in msg for m in markers)

    def _record_attempt(
        self, session_id: str, msg_id: str, error: str, terminal: bool = False,
    ) -> int:
        """Record one delivery attempt in ``.status-<msg_id>/meta.json``.

        Returns the (new) attempt count. On retryable failures, computes
        ``next_attempt_at`` with exponential backoff (base 5s: 5,10,20,40,80)
        so flush() skips entries that are not due yet.  Terminal failures are
        marked so flush() dead-letters immediately.
        """
        sd = self._outbox / session_id
        sd.mkdir(parents=True, exist_ok=True)
        status_dir = sd / f".status-{msg_id}"
        status_dir.mkdir(exist_ok=True)
        meta_path = status_dir / "meta.json"
        meta: dict[str, Any] = {}
        try:
            if meta_path.exists():
                meta = json.loads(meta_path.read_bytes())
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            meta = {}
        now = datetime.now(timezone.utc)
        now_iso = now.strftime(ISO_TIMESTAMP_FORMAT)
        attempt = int(meta.get("attempt_count", 0)) + 1
        meta["attempt_count"] = attempt
        meta.setdefault("first_accepted_at", now_iso)
        meta["last_attempt_at"] = now_iso
        meta["last_error"] = error[:500]
        meta["terminal"] = terminal
        if not terminal:
            backoff = self._backoff_base_s * (2 ** (attempt - 1))
            meta["next_attempt_at"] = (
                now.timestamp() + backoff
            )
        tmp = status_dir / ".tmp-meta.json"
        with open(tmp, "w") as f:
            f.write(json.dumps(meta, indent=2, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(meta_path))
        return attempt

    def _dead_letter(
        self, session_id: str, msg_id: str, reason: str,
    ) -> None:
        """Atomically move a pending outbox entry (+ status) to dead_letter.

        Keeps the envelope, error history and trace intact for
        list/requeue/purge.  No sender notification is generated here —
        dead-letter notices must never recurse (Top3).
        """
        sd = self._outbox / session_id
        dl = (self._outbox.parent / "_dead_letter") / session_id
        dl.mkdir(parents=True, exist_ok=True)
        src = sd / f"{msg_id}.json"
        if not src.exists():
            return
        os.replace(str(src), str(dl / f"{msg_id}.json"))
        # Move status dir too (attempt history)
        status_dir = sd / f".status-{msg_id}"
        if status_dir.exists():
            import shutil
            shutil.move(str(status_dir), str(dl / f".status-{msg_id}"))
        (dl / f".dead-letter-reason-{msg_id}").write_text(reason)

    def dead_letter_list(self, session_id: Optional[str] = None) -> list[dict[str, Any]]:
        """List dead-lettered envelopes (message + reason + attempts)."""
        root = self._outbox.parent / "_dead_letter"
        sessions = [session_id] if session_id else (
            sorted(d.name for d in root.iterdir() if d.is_dir()) if root.is_dir() else []
        )
        results: list[dict[str, Any]] = []
        for sid in sessions:
            dl = root / sid
            if not dl.is_dir():
                continue
            for f in sorted(dl.glob("*.json")):
                mid = f.stem
                try:
                    env = json.loads(f.read_bytes())
                except (json.JSONDecodeError, OSError):
                    env = {}
                reason = ""
                rp = dl / f".dead-letter-reason-{mid}"
                if rp.exists():
                    reason = rp.read_text(errors="replace")
                results.append({
                    "session_id": sid,
                    "msg_id": mid,
                    "to": env.get("to", ""),
                    "subject": env.get("subject", ""),
                    "reason": reason,
                })
        return results

    def dead_letter_requeue(self, session_id: str, msg_id: str) -> bool:
        """Move a dead-lettered entry back to pending (flush will retry)."""
        import shutil
        dl = (self._outbox.parent / "_dead_letter") / session_id
        src = dl / f"{msg_id}.json"
        if not src.exists():
            return False
        sd = self._outbox / session_id
        sd.mkdir(parents=True, exist_ok=True)
        os.replace(str(src), str(sd / f"{msg_id}.json"))
        # P1 (oracle-lite): 清理死信目录残留的旧 status 目录（_dead_letter 曾
        # move 它过去）——否则每次 requeue 泄漏一个目录。新 retry 从空 meta 开始。
        shutil.rmtree(str(dl / f".status-{msg_id}"), ignore_errors=True)
        (dl / f".dead-letter-reason-{msg_id}").unlink(missing_ok=True)
        # Clear attempt history so retry starts fresh (keep status dir? reset meta)
        status_dir = sd / f".status-{msg_id}"
        status_dir.mkdir(exist_ok=True)
        meta_path = status_dir / "meta.json"
        try:
            meta = json.loads(meta_path.read_bytes()) if meta_path.exists() else {}
        except (json.JSONDecodeError, OSError):
            meta = {}
        meta["attempt_count"] = 0
        meta["terminal"] = False
        meta.pop("next_attempt_at", None)
        meta_path.write_text(json.dumps(meta, indent=2))
        return True

    def dead_letter_purge(self, session_id: Optional[str] = None) -> int:
        """Delete dead-lettered entries (session-scoped or all)."""
        import shutil
        root = self._outbox.parent / "_dead_letter"
        sessions = [session_id] if session_id else (
            sorted(d.name for d in root.iterdir() if d.is_dir()) if root.is_dir() else []
        )
        removed = 0
        for sid in sessions:
            dl = root / sid
            if not dl.is_dir():
                continue
            for f in list(dl.glob("*")):
                if f.is_file() or f.is_dir():
                    if f.is_dir():
                        shutil.rmtree(f, ignore_errors=True)
                    else:
                        f.unlink(missing_ok=True)
                    removed += 1
        return removed

    def _attempt_due(self, session_id: str, msg_id: str) -> bool:
        """Top3: True if the entry is due for a retry (backoff elapsed).
        Entries without meta are due immediately."""
        sd = self._outbox / session_id
        meta_path = sd / f".status-{msg_id}" / "meta.json"
        try:
            if not meta_path.exists():
                return True
            meta = json.loads(meta_path.read_bytes())
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return True
        if meta.get("terminal"):
            return False  # terminal entries never retry (flush dead-letters them)
        nxt = meta.get("next_attempt_at")
        if nxt is None:
            return True
        return time.time() >= float(nxt)

    def _handle_flush_failure(
        self, session_id: str, msg_id: str, exc: Exception,
    ) -> None:
        """Top3: record attempt, classify; dead-letter terminal or exhausted."""
        terminal = self._is_terminal_error(exc)
        attempt = self._record_attempt(
            session_id, msg_id, str(exc), terminal=terminal,
        )
        if terminal:
            self._dead_letter(session_id, msg_id, f"terminal: {exc}"[:300])
            return
        if attempt >= self._max_attempts:
            self._dead_letter(session_id, msg_id, f"max attempts ({self._max_attempts}) exceeded: {exc}"[:300])

    # ── Idempotency ────────────────────────────────────────────────────

    def _check_idempotency(
        self, session_id: str, msg_id: str,
    ) -> Optional[SendReceipt]:
        """Return cached receipt if msg_id already delivered or in outbox."""
        # In-memory cache
        if msg_id in self._cache:
            return self._cache[msg_id]

        sd = self._outbox / session_id
        dest = sd / f"{msg_id}.json"
        if not dest.exists():
            return None

        # Outbox entry exists — reconstruct receipt from markers
        delivered_marker = sd / f".delivered-{msg_id}"
        if delivered_marker.exists():
            receipt = SendReceipt(status="delivered", msg_id=msg_id)
        else:
            receipt = SendReceipt(status="accepted", msg_id=msg_id, queued=True)
        self._cache[msg_id] = receipt
        return receipt

    # ── Session listing ────────────────────────────────────────────────

    def _list_sessions(self) -> list[str]:
        """List session directories in outbox."""
        if not self._outbox.exists():
            return []
        return sorted(
            d.name for d in self._outbox.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )

    # ── Validation ─────────────────────────────────────────────────────

    @staticmethod
    def _validate_msg_id(msg_id: str) -> None:
        if not msg_id or "/" in msg_id or "\\" in msg_id or ".." in msg_id:
            raise ValueError(f"invalid msg_id: {msg_id!r}")


class EngineDeliverySink:
    """DeliverySink adapter — makes DeliveryEngine callable via the kernel's
    ``.deliver()`` protocol.

    Resolves each target agent's registered host from the kernel routing
    table (populating the engine's host cache) before delegating to
    ``engine.deliver_sink()``, so cross-host messages go through transport
    while ``__local__``/unregistered agents fall back to local delivery.
    """

    def __init__(self, engine: "DeliveryEngine", kernel: Any = None) -> None:
        self._engine = engine
        self._kernel = kernel

    def set_kernel(self, kernel: Any) -> None:
        """Late-bind the kernel once it exists (avoids forward-reference)."""
        self._kernel = kernel

    def deliver(self, session_id: str, target_agent: str, envelope: Any,
                msg_id: str, created_at: str, from_id: str) -> SendReceipt:
        if self._kernel is not None:
            loc = self._kernel.get_location(session_id, target_agent)
            if loc and loc.host_alias and loc.host_alias != "__local__":
                # Resolve the repo-map host: its ssh_alias/shell_prefix are the
                # real transport targets, and a repo-map host that is this
                # machine (e.g. mac=OA-MIANYIN-MAC) must stay LOCAL — caching
                # it as remote makes delivery SSH into "mac" and fail.
                from codeagent.config.repo_map import load_repo_map
                from codeagent.domain import HostSpec, resolve_is_local
                spec = None
                try:
                    spec = load_repo_map().hosts.get(loc.host_alias)
                except Exception:
                    spec = None
                if spec is not None and resolve_is_local(spec):
                    # 本机 host：不 cache 远程 HostSpec，deliver_sink 走 local 分支。
                    pass
                elif spec is not None:
                    host = HostSpec(
                        name=spec.name,
                        ssh_alias=spec.ssh_alias,
                        hostnames=spec.hostnames,
                        shell_prefix=spec.shell_prefix,
                        fallback_ssh_alias=spec.fallback_ssh_alias,
                    )
                    self._engine.cache_host(target_agent, host)
                else:
                    # ad-hoc host_alias（不在 repo-map）——保持原有行为。
                    host = HostSpec(
                        name=loc.host_alias,
                        ssh_alias=loc.host_alias,
                        hostnames=(loc.host_alias,),
                    )
                    self._engine.cache_host(target_agent, host)
        return self._engine.deliver_sink(session_id, target_agent, envelope, msg_id, created_at, from_id)
