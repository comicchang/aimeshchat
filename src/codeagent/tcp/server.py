"""High-level MailboxDaemon facade.

Composes :class:`ConnectionRegistry`, :class:`SessionRoutingTable`,
:class:`codeagent.tcp.daemon.TCPConnectionDaemon`, and the mailbox /
spool stores into a single start/stop/status API.

Typical usage::

    daemon = MailboxDaemon("0.0.0.0", 5555, mailbox_store, spool_store)
    await daemon.start()
    await daemon.send_message(session_id, from_id, to_id, payload)
    info = daemon.status()
    await daemon.stop()
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import TYPE_CHECKING

from codeagent.mailbox.protocol import validate_message
from codeagent.tcp.daemon import TCPConnectionDaemon
from codeagent.tcp.protocol import Frame, FrameType, encode_frame
from codeagent.tcp.registry import ConnectionRegistry, SessionRoutingTable

if TYPE_CHECKING:
    from codeagent.mailbox.store import MailboxStore
    from codeagent.tcp.spool import SpoolStore

logger = logging.getLogger(__name__)


class MailboxDaemon:
    """Facade for the TCP mailbox forwarding daemon.

    Parameters
    ----------
    host:
        Bind address for the TCP listener (e.g. ``"0.0.0.0"``).
    port:
        Bind port for the TCP listener.
    mailbox_store:
        Local mailbox store used for inbox writes and roster validation.
    spool_store:
        Durable spool for outbound message forwarding.
    heartbeat_interval:
        Seconds between heartbeat sweeps (default 30).
    """

    def __init__(
        self,
        host: str,
        port: int,
        mailbox_store: MailboxStore,
        spool_store: SpoolStore,
        heartbeat_interval: float = 30.0,
    ) -> None:
        self._host = host
        self._port = port
        self._mailbox_store = mailbox_store
        self._spool_store = spool_store

        self._registry = ConnectionRegistry()
        self._routing = SessionRoutingTable()
        self._daemon = TCPConnectionDaemon(
            registry=self._registry,
            routing=self._routing,
            mailbox_store=mailbox_store,
            spool_store=spool_store,
            heartbeat_interval=heartbeat_interval,
        )
        # Back-reference so the daemon can call our validation layer
        self._daemon._server_ref = self  # type: ignore[attr-defined]
        # Track message IDs that were already written to inbox by
        # send_message so the daemon won't double-write on TCP receipt.
        self._local_msg_ids: set[str] = set()
        self._running = False

    # ── lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> tuple[str, int]:
        """Start the TCP daemon and replay any pending spool entries.

        Returns ``(host, port)`` of the bound socket.
        """
        addr = await self._daemon.start(self._host, self._port)
        self._host, self._port = addr[0], addr[1]
        self._running = True

        # Replay pending spool entries from a previous run
        try:
            result = await self._daemon.flush_spool(self._spool_store, self._routing)
            if result["resent"] or result["skipped"]:
                logger.info(
                    "spool replay: %d resent, %d skipped",
                    result["resent"],
                    result["skipped"],
                )
        except Exception:
            logger.exception("spool replay failed")

        logger.info("MailboxDaemon started on %s:%d", addr[0], addr[1])
        return addr

    async def stop(self) -> None:
        """Stop the daemon and clear all routing state."""
        await self._daemon.stop()
        self._routing.clear()
        self._registry.clear()
        self._local_msg_ids.clear()
        self._running = False
        logger.info("MailboxDaemon stopped")

    def status(self) -> dict:
        """Return a snapshot of the daemon's operational state."""
        return {
            "running": self._running,
            "host": self._host,
            "port": self._port,
            "connected_hosts": self._registry.list_hosts(),
            "sessions": {
                sid: sorted(hosts)
                for sid, hosts in self._routing.get_all_sessions().items()
            },
            "num_sessions": len(self._routing.get_all_sessions()),
        }

    # ── message sending ─────────────────────────────────────────────────

    async def send_message(
        self,
        session_id: str,
        from_id: str,
        to_id: str,
        msg: dict,
    ) -> dict:
        """Validate, persist locally, spool, and forward a mailbox message.

        The message is written to the local mailbox inbox first, then
        spooled for durable tracking, and finally forwarded over TCP to
        every host in the session's routing table (pub/sub).

        Forwarding writes directly to the remote StreamWriter (bypassing
        the daemon's connection handler reader) so that the remote test
        client or application can read the frame from its own StreamReader
        without competing with the daemon.

        Parameters
        ----------
        session_id:
            Target session (must exist in the local mailbox store).
        from_id:
            Sender agent id (must be in the session roster).
        to_id:
            Recipient agent id (must be in the session roster).
        msg:
            Message payload.  Must contain at minimum:
            ``subject``, ``body``, ``kind``.

        Returns
        -------
        dict
            ``{"status": "sent"|"spooled"|"local", "msg_id": str}``
        """
        roster = self._get_roster(session_id)
        if not roster:
            raise ValueError(f"session not found: {session_id}")
        if from_id not in roster:
            raise ValueError(f"sender not in roster: {from_id}")
        if to_id not in roster:
            raise ValueError(f"recipient not in roster: {to_id}")
        if "kind" not in msg:
            raise ValueError("message must specify 'kind'")
        if msg["kind"] not in {"TASK", "REPORT", "PROGRESS", "EVIDENCE",
                                "QUESTION", "RESPONSE", "NOTICE"}:
            raise ValueError(f"invalid kind: {msg['kind']}")

        # Write to local mailbox inbox
        result = self._mailbox_store.send(
            session_id=session_id,
            from_id=from_id,
            to_id=to_id,
            subject=msg.get("subject", ""),
            body=msg.get("body", ""),
            kind=msg["kind"],
            reply_to=msg.get("reply_to", ""),
            run_id=msg.get("run_id", ""),
            request_id=msg.get("request_id", ""),
        )
        # Extract msg_id: "sent → <to>/inbox/<msg_id>.json"
        msg_id = result.split("/")[-1].replace(".json", "") if "/" in result else ""
        if not msg_id:
            from codeagent.mailbox.store import gen_msg_id
            msg_id = gen_msg_id(from_id)

        # Record so the daemon won't double-write on TCP receipt
        self._local_msg_ids.add(msg_id)

        payload = self._build_payload(session_id, from_id, to_id, msg, msg_id)

        # Spool + forward to every routing target (pub/sub).
        from codeagent.tcp.spool import SpoolEntry as _SE

        target_hosts = self._routing.get_hosts(session_id)
        forwarded = False
        spooled_count = 0
        for host_alias in target_hosts:
            if host_alias == from_id:
                continue  # don't echo back to sender
            # Always spool for durable delivery tracking
            spool_id = f"{msg_id}@{host_alias}"
            entry = _SE(
                uuid=spool_id,
                session_id=session_id,
                from_id=from_id,
                to_id=to_id,
                msg_id=msg_id,
                payload=payload,
                created_at=time.time(),
                host_alias=host_alias,
            )
            self._spool_store.write(entry)
            spooled_count += 1

            # Write directly to the StreamWriter — bypasses the daemon's
            # connection handler reader so the remote client can read the
            # frame from its own StreamReader without interference.
            pair = self._registry.get(host_alias)
            if pair is not None:
                _, writer = pair
                try:
                    frame = Frame(
                        type=FrameType.MESSAGE,
                        session_id=session_id,
                        payload=payload,
                    )
                    writer.write(encode_frame(frame))
                    await writer.drain()
                    forwarded = True
                except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                    logger.warning("forward to %s failed: %s", host_alias, exc)

        if forwarded:
            status = "sent"
        elif spooled_count > 0:
            status = "spooled"
        else:
            status = "local"
        return {"status": status, "msg_id": msg_id}

    # ── internal helpers ────────────────────────────────────────────────

    def is_local_msg(self, msg_id: str) -> bool:
        """Check whether *msg_id* was already persisted locally."""
        return msg_id in self._local_msg_ids

    def _build_payload(
        self,
        session_id: str,
        from_id: str,
        to_id: str,
        msg: dict,
        msg_id: str,
    ) -> dict:
        """Build the canonical forwarding payload."""
        return {
            "session_id": session_id,
            "from": from_id,
            "to": to_id,
            "subject": msg.get("subject", ""),
            "body": msg.get("body", ""),
            "kind": msg["kind"],
            "msg_id": msg_id,
            "created_at": msg.get("created_at", "") or time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            ),
            **{
                k: msg[k]
                for k in ("reply_to", "run_id", "request_id")
                if k in msg
            },
        }

    async def _write_inbound(
        self, session_id: str, to_id: str, payload: dict,
    ) -> None:
        """Validate an inbound frame's payload and write it to the inbox.

        Called by the daemon's ``_handle_message`` for truly remote
        messages (arriving over TCP from another host).  Skips the write
        if the message was already persisted by ``send_message`` (local
        origin) to avoid duplicate inbox entries.
        """
        msg_id = payload.get("msg_id", "")

        # If this message was already written by our own send_message,
        # skip the duplicate inbox write.
        if msg_id and self.is_local_msg(msg_id):
            return

        roster = self._get_roster(session_id)
        if not roster:
            raise ValueError(f"session not found: {session_id}")

        from_id = payload.get("from", "")
        if from_id and from_id not in roster:
            raise ValueError(f"sender not in roster: {from_id}")
        if to_id and to_id not in roster:
            raise ValueError(f"recipient not in roster: {to_id}")

        kind = payload.get("kind", "REPORT")
        if kind not in {"TASK", "REPORT", "PROGRESS", "EVIDENCE",
                        "QUESTION", "RESPONSE", "NOTICE"}:
            raise ValueError(f"invalid kind: {kind}")

        # Build full envelope
        from codeagent.mailbox.store import gen_msg_id
        if not msg_id:
            msg_id = gen_msg_id(from_id or "tcp")
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        full_msg = {
            "session_id": session_id,
            "from": from_id,
            "to": to_id,
            "subject": payload.get("subject", "(forwarded)"),
            "body": payload.get("body", ""),
            "kind": kind,
            "msg_id": msg_id,
            "created_at": payload.get("created_at", "") or now,
        }

        ok, reason = validate_message(full_msg, session_id)
        if not ok:
            raise ValueError(f"inbound validation failed: {reason}")

        # Atomically write to recipient's inbox
        inbox = self._mailbox_store.agent_subdir(session_id, to_id, "inbox")
        inbox.mkdir(parents=True, exist_ok=True)
        dest = inbox / f"{msg_id}.json"
        tmp = inbox / f".tmp-{msg_id}.json"
        with open(tmp, "w") as f:
            f.write(json.dumps(full_msg, indent=2, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(dest))

    def _get_roster(self, session_id: str) -> set[str]:
        """Return the set of agent ids in *session_id*'s roster."""
        meta = self._mailbox_store.read_session(session_id)
        if meta is None:
            return set()
        roster = {meta.get("manager", "")}
        roster.update(meta.get("agents", []))
        roster.discard("")
        return roster
