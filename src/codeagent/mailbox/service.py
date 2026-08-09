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
silently drop the sender's expectation.

``peek``, delivery confirmation, plugin notifications and tmux text never
generate receipts — only a successful claim does.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
                              the authoritative roster — NOT consumed
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

    def read(self, session_id: str, agent_id: str, owner: str) -> ReadOutcome:
        """Claim the oldest message; emit RECEIPT(READ) when required.

        Order of operations:
          1. Inspect the oldest inbox message WITHOUT consuming.
          2. If it demands an ack and the sender is not in the authoritative
             roster, return ``ack_route_unresolved`` and do NOT claim.
          3. Claim via store.read() (inbox → processing, atomic).
          4. If the claimed message required an ack, emit a deterministic
             READ receipt to the original sender.
        """
        validate_agent_id(agent_id)
        validate_agent_id(owner)

        # ── Pre-claim inspection (no consumption) ──────────────────────
        inbox = self._store.agent_subdir(session_id, agent_id, "inbox")
        files = self._store.list_messages(inbox)
        if not files:
            return ReadOutcome(message=None, status="empty")
        try:
            first = json.loads(files[0].read_bytes())
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            first = {}

        if first.get("require_ack") and first.get("kind") != MessageKind.RECEIPT.value:
            if not self._sender_resolvable(session_id, first.get("from", "")):
                return ReadOutcome(
                    message=first,
                    status=ACK_ROUTE_UNRESOLVED,
                    error=(
                        f"require_ack message {first.get('msg_id', '?')} from "
                        f"{first.get('from', '?')!r} cannot be acked: sender not "
                        f"in authoritative roster"
                    ),
                )

        # ── Claim ──────────────────────────────────────────────────────
        claimed = self._store.read(session_id, agent_id, owner)
        if claimed is None:
            return ReadOutcome(message=None, status="empty")

        outcome = ReadOutcome(message=claimed, status="ok")
        if claimed.get("require_ack") and claimed.get("kind") != MessageKind.RECEIPT.value:
            receipt = self._emit_read_receipt(session_id, agent_id, claimed)
            outcome.receipt = receipt
        return outcome

    # ── Internals ───────────────────────────────────────────────────────

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
