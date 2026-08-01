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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

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
        # idempotency cache: msg_id → SendReceipt (process-lifetime)
        self._cache: dict[str, SendReceipt] = {}
        # host cache for _resolve_target (alias → HostSpec)
        self._host_cache: dict[str, Any] = {}

    @staticmethod
    def _history_entry(envelope: dict[str, Any], msg_id: str) -> dict[str, str]:
        """Build a canonical history record (full 8-field message schema).

        Shared by deliver() and flush() so both successful paths append
        identical records — a drift here would silently fail
        append_history's validate_message and lose history.
        """
        return {
            "session_id": envelope.get("session_id", ""),
            "from": envelope.get("from", ""),
            "to": envelope.get("to", ""),
            "subject": envelope.get("subject", ""),
            "body": envelope.get("body", ""),
            "kind": envelope.get("kind", "TASK"),
            "msg_id": msg_id,
            "created_at": envelope.get("created_at", ""),
        }

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
        if not host_alias:
            # Local delivery: write straight to the recipient inbox
            # (durable + idempotent via msg_id).  The outbox entry above
            # guarantees no loss if this write fails mid-way.
            try:
                self._store.send(
                    session_id=sid,
                    from_id=envelope.get("from", ""),
                    to_id=envelope.get("to", target if isinstance(target, str) else ""),
                    subject=envelope.get("subject", ""),
                    body=envelope.get("body", ""),
                    kind=envelope.get("kind", "TASK"),
                    reply_to=envelope.get("reply_to", ""),
                    run_id=envelope.get("run_id", ""),
                    request_id=envelope.get("request_id", ""),
                    msg_id=msg_id,
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
    ) -> None:
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
            env_dict["_target_agent"] = target_agent

        # Resolve target_agent → HostSpec via cache or store local target
        host = self._host_cache.get(target_agent)
        if host is not None:
            # Record the resolved host so the durable outbox entry keeps it:
            # flush() reads `_target_host` to re-send on retry — without it
            # every retry is silently skipped.
            env_dict["_target_host"] = getattr(host, "host_alias", "") or getattr(host, "ssh_alias", "")
            self.deliver(session_id, host, env_dict)
        else:
            # No cached host — deliver locally (outbox write only)
            self.deliver(session_id, target_agent, env_dict)

    def cache_host(self, agent_id: str, host: Any) -> None:
        """Register an agent_id → HostSpec mapping for sink resolution."""
        self._host_cache[agent_id] = host

    def flush(self, session_id: Optional[str] = None) -> int:
        """Retry all pending outbox entries. Returns count of newly delivered."""
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

                try:
                    self._remote_send(target, envelope)
                except Exception as exc:
                    log.debug("DeliveryEngine: flush retry failed for %s: %s", mid, exc)
                    self._write_status(sid, mid, "flush_failed", str(exc))
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

        args = self._build_mailbox_args(envelope)
        # Ensure the remote host has the session (sessions are per-host;
        # two machines never share a mailbox filesystem). Idempotent:
        # remote session-init errors on already-exists are ignored.
        try:
            init_args = [
                "session-init",
                "--session", envelope.get("session_id", ""),
                "--manager", envelope.get("from", ""),
                "--agents", envelope.get("to", ""),
            ]
            init_code, init_out, init_err = transport.mailbox(host, init_args)
            if init_code != 0 and "already exists" not in (init_err or init_out or ""):
                raise RuntimeError(
                    f"remote session-init failed (exit {init_code}): {init_err or init_out}"
                )
        except Exception as exc:
            if "already exists" in str(exc):
                pass
            else:
                raise

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
        then falls back to a minimal SSH HostSpec.
        """
        # Check host cache (populated at wiring time or by cache_host)
        cached = self._host_cache.get(host_alias)
        if cached is not None:
            return cached
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
        for att in attachments:
            args.extend(["--attachment", json.dumps(att, ensure_ascii=False)])
        if reply_to:
            args.extend(["--reply-to", reply_to])
        if run_id:
            args.extend(["--run-id", run_id])
        if request_id:
            args.extend(["--request-id", request_id])

        return args

    # ── Status markers ─────────────────────────────────────────────────

    def _mark_delivered(self, session_id: str, msg_id: str) -> None:
        """Write delivered marker file."""
        sd = self._outbox / session_id
        sd.mkdir(parents=True, exist_ok=True)
        marker = sd / f".delivered-{msg_id}"
        marker.write_text(
            json.dumps({
                "delivered_at": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
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
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

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
                msg_id: str, created_at: str, from_id: str) -> None:
        if self._kernel is not None:
            loc = self._kernel.get_location(session_id, target_agent)
            if loc and loc.host_alias and loc.host_alias != "__local__":
                from codeagent.domain import HostSpec
                host = HostSpec(
                    name=loc.host_alias,
                    ssh_alias=loc.host_alias,
                    hostnames=(loc.host_alias,),
                )
                self._engine.cache_host(target_agent, host)
        self._engine.deliver_sink(session_id, target_agent, envelope, msg_id, created_at, from_id)
