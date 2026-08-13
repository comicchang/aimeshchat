"""MailboxService — protocol orchestration over the atomic MailboxStore.

MailboxStore stays a pure filesystem state machine. This service adds the
protocol behaviors that span messages:

- ``send()``: writes v2 messages (require_ack / receipt_type) and returns a
  structured ``SendReceipt(status=accepted|delivered|failed, queued=bool)``.
- ``read()``: claims a message (store.read) and, when the claimed message
  carried ``require_ack=True``, emits a deterministic ``RECEIPT(READ)`` back
  to the original sender through the injected delivery sink — receipts are
  routed cross-host via ``SwarmKernel.get_location`` (EngineDeliverySink),
  never through a parallel transport.

Required-ack discipline: a message that demands an ack is NOT consumed
when the sender is not resolvable in the authoritative roster
(``ACK_ROUTE_UNRESOLVED``) — consuming without being able to ack would
silently drop the sender's expectation. Such messages are *parked* on
first encounter (marker in ``<agent>/_ack_unresolved/``): the message
stays in the inbox — never silently dropped — but later ``read()`` calls
skip it so the queue head never blocks (P1-1), and it is un-parked
automatically once its sender joins the roster.

``peek``, delivery confirmation, plugin notifications and tmux text never
generate receipts — only a successful claim does.
"""
from __future__ import annotations

import fcntl
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from codeagent.constants import ISO_TIMESTAMP_FORMAT
from codeagent.mailbox.protocol import (
    PROTOCOL_VERSION,
    MessageKind,
    ReceiptType,
    validate_agent_id,
)
from codeagent.mailbox.store import MailboxStore, gen_msg_id

# Fixed UUIDv5 namespace for deterministic READ receipt msg_ids. The name
# input is f"{session_id}:{reply_to}:{current_agent}:READ" — deterministic
# across crash replays so DeliveryEngine's msg_id idempotency prevents
# duplicate receipts.
_RECEIPT_NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")

# Sentinel returned when a require_ack message cannot be routed back.
ACK_ROUTE_UNRESOLVED = "ack_route_unresolved"


@dataclass(frozen=True)
class SendReceipt:
    """Structured send outcome (mirrors swarm.delivery.SendReceipt shape)."""

    status: str  # "accepted" | "delivered" | "failed"
    msg_id: str = ""
    error: str = ""
    queued: bool = False
    detail: str = ""  # store-level human-readable outcome (broadcast count etc.)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "msg_id": self.msg_id,
            "error": self.error,
            "queued": self.queued,
            "detail": self.detail,
        }


@dataclass
class ReadOutcome:
    """Outcome of a service.read() claim.

    ``status`` is one of:
      - "ok":                 message claimed; receipt emitted when required
      - "empty":              no message to claim
      - "ack_route_unresolved": require_ack message whose sender is not in
                              the authoritative roster — NOT consumed; the
                              message is parked (P1-1) so later reads skip
                              it instead of blocking the queue head
    ``receipt`` carries the READ-receipt send receipt when one was emitted.
    """

    message: Optional[dict] = None
    status: str = "ok"
    receipt: Optional[SendReceipt] = None
    error: str = ""


class MailboxService:
    """Protocol layer over MailboxStore (local authority for the mailbox).

    Parameters
    ----------
    store:
        The atomic filesystem store. Defaults to the standard mailbox root.
    kernel:
        Optional SwarmKernel used to resolve cross-host receipt targets.
        When provided, an EngineDeliverySink is wired so READ receipts to
        remote senders go through the durable outbox + transport. When
        absent, receipts are written locally via the store.
    """

    def __init__(
        self,
        store: Optional[MailboxStore] = None,
        kernel: object = None,  # SwarmKernel (avoid import cycle at class level)
    ) -> None:
        self._store = store or MailboxStore()
        self._kernel = kernel
        self._sink = None
        if kernel is not None:
            try:
                from codeagent.swarm.delivery import DeliveryEngine, EngineDeliverySink

                engine = DeliveryEngine(mailbox_store=self._store, transport_router=None)
                sink = EngineDeliverySink(engine)
                sink.set_kernel(kernel)
                self._sink = sink
            except Exception:
                # No kernel wiring (e.g. partial install) — fall back to
                # local-only delivery. Receipts to local senders still work.
                self._sink = None

    # ── Send ────────────────────────────────────────────────────────────

    def send(
        self,
        session_id: str,
        from_id: str,
        to_id: str,
        subject: str,
        body: str,
        kind: str = "REPORT",
        reply_to: str = "",
        run_id: str = "",
        request_id: str = "",
        command_id: str = "",  # P1-1: 透传给 store.send（gateway 写 command 消息时 = request_id）
        trace_id: str = "",
        causation_id: str = "",
        attachments: Optional[list] = None,
        msg_id: Optional[str] = None,
        require_ack: bool = False,
        receipt_type: str = "",
    ) -> SendReceipt:
        """Send a message and return a structured SendReceipt.

        Writes v2 messages via MailboxStore (the local authority). Cross-host
        routing stays with the DeliveryEngine — this service does not
        re-implement transport. Raises ValueError on validation failure
        (callers map to terminal exit codes).
        """
        mid = msg_id or gen_msg_id(from_id)
        try:
            detail = self._store.send(
                session_id=session_id,
                from_id=from_id,
                to_id=to_id,
                subject=subject,
                body=body,
                kind=kind,
                reply_to=reply_to,
                run_id=run_id,
                request_id=request_id,
                command_id=command_id,
                trace_id=trace_id,
                causation_id=causation_id,
                attachments=attachments,
                msg_id=mid,
                require_ack=require_ack,
                receipt_type=receipt_type,
            )
        except ValueError as exc:
            return SendReceipt(status="failed", msg_id=mid, error=str(exc))
        return SendReceipt(status="delivered", msg_id=mid, detail=detail)

    # ── Read (claim + receipt) ─────────────────────────────────────────

    def peek(self, session_id: str, agent_id: str, max_messages: int = 5, max_subject: int = 80) -> dict:
        """Non-consuming peek — never generates a receipt."""
        return self._store.peek(session_id, agent_id, max_messages, max_subject)

    def read(self, session_id: str, agent_id: str, owner: str,
             msg_id: str = "") -> ReadOutcome:  # P1-8: optional targeted claim
        """Claim a message; emit RECEIPT(READ) when required.

        When ``msg_id`` is provided (P1-8), claim that specific message
        instead of the oldest — prevents claim-drift when the caller
        already knows which message to consume.

        Order of operations:
          1. Inspect the target (or oldest *unparked*) inbox message WITHOUT
             consuming (P1-1: ack-route-unresolved messages are parked on
             first encounter — marked in ``_ack_unresolved/`` and skipped —
             so a single bad message never blocks the queue head).
          2. If it demands an ack and the sender is not in the authoritative
             roster, park it, return ``ack_route_unresolved`` and do NOT
             claim. The parked message is un-parked automatically once its
             sender joins the roster.
          3. Claim via store.read() (inbox → processing, atomic), skipping
             parked messages.
          4. If the claimed message required an ack, emit a deterministic
             READ receipt to the original sender.

        The pre-claim inspection and the claim run under ONE per-agent lock
        (P2-12) and always target the exact pre-checked message, so the
        require_ack discipline cannot be bypassed by a TOCTOU: without the
        lock a concurrent read could consume the inspected message and let
        this claim drift to a different, unchecked message.
        """
        validate_agent_id(agent_id)
        validate_agent_id(owner)

        # P2-12: serialize service reads per agent (cross-process flock).
        # The pre-check and the claim must be atomic with respect to other
        # service reads, otherwise the ack discipline can be raced.
        agent_dir = self._store.agent_dir(session_id, agent_id)
        agent_dir.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(str(agent_dir / ".read.lock"), os.O_CREAT | os.O_RDWR)
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass  # non-POSIX — degrade to unlocked; store claim stays atomic

            # ── Pre-claim inspection (no consumption) ──────────────────
            inbox = self._store.agent_subdir(session_id, agent_id, "inbox")
            files = self._store.list_messages(inbox)
            if not files:
                return ReadOutcome(message=None, status="empty")

            # P1-1: parked ack-route-unresolved messages must not block the
            # queue head. Markers live in <agent>/_ack_unresolved/; parked
            # messages STAY in the inbox (ack expectation kept visible — never
            # silently dropped) but are skipped so later messages flow.
            parked = self._ack_unresolved_ids(session_id, agent_id)
            # Recover ack semantics: once the sender joins the roster, un-park
            # so the message is claimed normally and its READ receipt emitted.
            for mid in list(parked):
                marker = self._ack_unresolved_marker(session_id, agent_id, mid)
                if marker and self._sender_resolvable(session_id, marker.get("from", "")):
                    self._unpark_ack_unresolved(session_id, agent_id, mid)
                    parked.discard(mid)

            # P1-8: when msg_id is set, inspect that specific message;
            # otherwise fall back to the oldest non-parked.
            first_file = None
            first: dict = {}
            if msg_id:
                first_file = next((f for f in files if f.stem == msg_id), None)
                if first_file is None:
                    return ReadOutcome(message=None, status="empty")
                try:
                    first = json.loads(first_file.read_bytes())
                except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                    first = {}
            else:
                for f in files:
                    if f.stem in parked:
                        continue
                    first_file = f
                    try:
                        first = json.loads(f.read_bytes())
                    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                        first = {}
                    break
                if first_file is None:
                    # Everything claimable is parked — nothing to consume.
                    return ReadOutcome(message=None, status="empty")

            if first.get("require_ack") and first.get("kind") != MessageKind.RECEIPT.value:
                if not self._sender_resolvable(session_id, first.get("from", "")):
                    # P1-1: park instead of blocking the head forever. The first
                    # read still reports ACK_ROUTE_UNRESOLVED (callers surface
                    # it); subsequent reads skip the parked message and claim
                    # later ones.
                    self._park_ack_unresolved(
                        session_id, agent_id, first_file.stem, first,
                        f"require_ack message {first.get('msg_id', '?')} from "
                        f"{first.get('from', '?')!r} cannot be acked: sender not "
                        f"in authoritative roster",
                    )
                    return ReadOutcome(
                        message=first,
                        status=ACK_ROUTE_UNRESOLVED,
                        error=(
                            f"require_ack message {first.get('msg_id', '?')} from "
                            f"{first.get('from', '?')!r} cannot be acked: sender not "
                            f"in authoritative roster"
                        ),
                    )

            # ── Claim ──────────────────────────────────────────────────
            claimed = self._store.read(
                session_id, agent_id, owner,
                skip_msg_ids=parked,
                # P2-12: always claim the EXACT pre-checked message — never a
                # drift candidate that skipped the require_ack inspection.
                target_msg_id=first_file.stem,
            )
            if claimed is None:
                return ReadOutcome(message=None, status="empty")
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            os.close(lock_fd)

        outcome = ReadOutcome(message=claimed, status="ok")
        if claimed.get("require_ack") and claimed.get("kind") != MessageKind.RECEIPT.value:
            receipt = self._emit_read_receipt(session_id, agent_id, claimed)
            outcome.receipt = receipt
        return outcome

    # ── Internals ───────────────────────────────────────────────────────

    # P1-1: ack-route-unresolved parking — markers under
    # <agent>/_ack_unresolved/<msg_id>.json. The message itself stays in the
    # inbox (visible, never silently dropped); read() skips parked ones.

    def _ack_unresolved_dir(self, session_id: str, agent_id: str) -> Path:
        """Marker dir for parked ack-route-unresolved messages."""
        return self._store.agent_subdir(session_id, agent_id, "_ack_unresolved")

    def _ack_unresolved_ids(self, session_id: str, agent_id: str) -> set[str]:
        """Message ids (file stems) currently parked (markers present)."""
        d = self._ack_unresolved_dir(session_id, agent_id)
        if not d.exists():
            return set()
        return {
            p.stem for p in d.glob("*.json")
            if not p.is_symlink()  # P3-6: symlink markers must not be trusted
        }

    def _ack_unresolved_marker(
        self, session_id: str, agent_id: str, msg_id: str,
    ) -> Optional[dict]:
        """Read a park marker; None when absent/unreadable."""
        p = self._ack_unresolved_dir(session_id, agent_id) / f"{msg_id}.json"
        try:
            return json.loads(p.read_bytes())
        except Exception:
            return None

    def _park_ack_unresolved(
        self, session_id: str, agent_id: str, msg_id: str, msg: dict, error: str,
    ) -> None:
        """Persist a park marker (fsync + atomic replace)."""
        d = self._ack_unresolved_dir(session_id, agent_id)
        d.mkdir(parents=True, exist_ok=True)
        marker = {
            "msg_id": msg_id,
            "from": msg.get("from", ""),
            "marked_at": datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT),
            "error": error,
        }
        dest = d / f"{msg_id}.json"
        tmp = d / f".tmp-{msg_id}.json"
        with open(tmp, "w") as f:
            json.dump(marker, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(dest))

    def _unpark_ack_unresolved(self, session_id: str, agent_id: str, msg_id: str) -> None:
        """Remove a park marker (sender is resolvable again)."""
        (self._ack_unresolved_dir(session_id, agent_id) / f"{msg_id}.json").unlink(
            missing_ok=True,
        )

    def _sender_resolvable(self, session_id: str, sender: str) -> bool:
        """True when *sender* is in the authoritative session roster.

        The roster comes from session.json (manager ∪ agents) — the same
        authority used by the kernel. Without a resolvable sender there is
        no route for the READ receipt, so the message must not be consumed.
        """
        if not sender:
            return False
        meta = self._store.read_session(session_id)
        if meta is None:
            return False
        roster = {meta.get("manager", "")} | set(meta.get("agents", []))
        return sender in roster

    def _emit_read_receipt(self, session_id: str, agent_id: str, original: dict) -> Optional[SendReceipt]:
        """Generate and deliver a deterministic RECEIPT(READ).

        Receipt fields (per protocol v2):
          to=original.from / from=current_agent / reply_to=original.msg_id
          kind=RECEIPT / receipt_type=READ / require_ack=False
          run_id/request_id inherited from the original message
        msg_id = UUIDv5(session_id, reply_to, current_agent, READ).
        """
        reply_to = original.get("msg_id", "")
        if not reply_to:
            return None
        receipt_msg_id = str(uuid.uuid5(_RECEIPT_NS, f"{session_id}:{reply_to}:{agent_id}:READ"))
        created_at = datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT)

        body = json.dumps({
            "receipt_type": ReceiptType.READ.value,
            "msg_id": reply_to,
            "reader": agent_id,
        }, ensure_ascii=False)

        # Deliver via the injected sink (cross-host when a kernel is wired).
        if self._sink is not None:
            try:
                from codeagent.swarm.model import Envelope

                env = Envelope(
                    subject=f"READ {reply_to}",
                    body=body,
                    kind=MessageKind.RECEIPT.value,
                    reply_to=reply_to,
                    run_id=original.get("run_id", ""),
                    request_id=original.get("request_id", ""),
                    trace_id=original.get("trace_id", ""),
                    require_ack=False,
                    receipt_type=ReceiptType.READ.value,
                )
                sender = original.get("from", "")
                sink_receipt = self._sink.deliver(
                    session_id, sender, env, receipt_msg_id, created_at, agent_id,
                )
                return SendReceipt(
                    status=getattr(sink_receipt, "status", "delivered"),
                    msg_id=receipt_msg_id,
                    queued=getattr(sink_receipt, "queued", False),
                )
            except Exception:
                # Receipt emission failure must not lose the claim; fall
                # back to a direct local write of the receipt envelope.
                pass

        try:
            self._store.send(
                session_id=session_id,
                from_id=agent_id,
                to_id=original.get("from", ""),
                subject=f"READ {reply_to}",
                body=body,
                kind=MessageKind.RECEIPT.value,
                reply_to=reply_to,
                run_id=original.get("run_id", ""),
                request_id=original.get("request_id", ""),
                trace_id=original.get("trace_id", ""),
                msg_id=receipt_msg_id,
                require_ack=False,
                receipt_type=ReceiptType.READ.value,
            )
            return SendReceipt(status="delivered", msg_id=receipt_msg_id)
        except ValueError as exc:
            return SendReceipt(status="failed", msg_id=receipt_msg_id, error=str(exc))
