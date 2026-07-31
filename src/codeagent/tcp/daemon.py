"""Asyncio TCP daemon for cross-host mailbox forwarding.

Implements a binary frame server that accepts remote host connections,
routes mailbox messages between sessions, and provides heartbeat
monitoring.  Each incoming connection is expected to send a HELLO frame
with its ``host_alias`` before exchanging MESSAGE / ACK / PING / PONG
traffic.

Frame wire format is defined in :mod:`codeagent.tcp.protocol`.

Lifecycle::

    daemon = TCPConnectionDaemon(registry, routing, mailbox_store, spool_store)
    await daemon.start(host="0.0.0.0", port=5555)
    # … daemon runs in background …
    await daemon.stop()
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

from codeagent.tcp.protocol import (
    Frame,
    FrameType,
    decode_frame,
    encode_frame,
)
from codeagent.tcp.spool import SpoolStore

if TYPE_CHECKING:
    from codeagent.mailbox.store import MailboxStore
    from codeagent.tcp.registry import ConnectionRegistry, SessionRoutingTable

logger = logging.getLogger(__name__)

# ── defaults ────────────────────────────────────────────────────────────
FRAME_READ_TIMEOUT = 30.0  # seconds to wait for a complete frame
STALE_HOST_TIMEOUT = 90.0  # seconds of silence before a host is considered dead


class TCPConnectionDaemon:
    """Asyncio TCP server that speaks the binary frame protocol.

    Parameters
    ----------
    registry:
        Shared connection registry (host_alias → stream pair).
    routing:
        Shared session routing table (session_id → host set).
    mailbox_store:
        Local mailbox store used to persist inbound messages and
        validate session rosters.
    spool_store:
        Durable spool for outbound message forwarding.
    heartbeat_interval:
        Seconds between heartbeat sweeps (default 30).
    """

    def __init__(
        self,
        registry: ConnectionRegistry,
        routing: SessionRoutingTable,
        mailbox_store: MailboxStore,
        spool_store: SpoolStore,
        heartbeat_interval: float = 30.0,
    ) -> None:
        self._registry = registry
        self._routing = routing
        self._mailbox_store = mailbox_store
        self._spool_store = spool_store
        self._heartbeat_interval = heartbeat_interval

        self._server: asyncio.AbstractServer | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._write_locks: dict[str, asyncio.Lock] = {}
        self._last_pong: dict[str, float] = {}

    # ── lifecycle ───────────────────────────────────────────────────────

    async def start(self, host: str, port: int) -> tuple[str, int]:
        """Bind the TCP listener and start the heartbeat loop.

        Returns ``(host, port)`` of the bound socket (useful when
        *port* is 0 for an ephemeral port).
        """
        self._server = await asyncio.start_server(
            self._handle_connection, host, port,
        )
        addr = self._server.sockets[0].getsockname()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("TCP daemon listening on %s:%d", addr[0], addr[1])
        return addr[0], addr[1]

    async def stop(self) -> None:
        """Gracefully shut down: close all connections, cancel heartbeat."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        # Close every open stream
        for host_alias in list(self._registry.list_hosts()):
            pair = self._registry.get(host_alias)
            if pair is not None:
                _, writer = pair
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
            self._registry.remove(host_alias)

        self._write_locks.clear()
        self._last_pong.clear()
        logger.info("TCP daemon stopped")

    # ── connection handler ──────────────────────────────────────────────

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Process one inbound TCP connection through its lifetime.

        Supports two protocols on the same port:
        - Binary frame protocol (inter-host mailbox forwarding)
        - JSON request/response (CLI daemon management)

        The first byte determines the protocol: ``0x7B`` (``{``)
        indicates a JSON request; anything else is a binary frame.
        """
        peer = writer.get_extra_info("peername", ("unknown", 0))
        host_alias: str | None = None

        try:
            # Peek at the first byte to determine the protocol.
            first = await asyncio.wait_for(
                reader.readexactly(1), timeout=FRAME_READ_TIMEOUT,
            )
            if not first:
                writer.close()
                return

            # JSON request/response protocol (CLI daemon management)
            if first[0] == 0x7B:  # '{'
                await self._handle_json_request(first, reader, writer)
                return

            # Binary frame protocol — put the byte back and proceed.
            # Wrap reader so _read_frame sees the peeked byte first.
            original_readexactly = reader.readexactly
            buf = first

            async def _patched_readexactly(n: int) -> bytes:
                nonlocal buf
                if buf:
                    if len(buf) >= n:
                        result, buf = buf[:n], buf[n:]
                        return result
                    remaining = buf
                    buf = b""
                    return remaining + await original_readexactly(n - len(remaining))
                return await original_readexactly(n)

            reader.readexactly = _patched_readexactly  # type: ignore[method-assign]

            # First frame MUST be HELLO carrying the remote host alias
            frame = await self._read_frame(reader)
            reader.readexactly = original_readexactly  # type: ignore[method-assign]
            if frame is None or frame.type != FrameType.HELLO:
                logger.warning("bad handshake from %s — closing", peer)
                writer.close()
                return

            host_alias = frame.payload.get("host_alias", "unknown")
            self._registry.register(host_alias, reader, writer)
            self._write_locks[host_alias] = asyncio.Lock()
            self._last_pong[host_alias] = time.monotonic()

            # Respond with READY
            await self._send_frame(writer, Frame(
                type=FrameType.READY,
                session_id="",
                payload={"status": "ok", "host_alias": host_alias},
            ))
            logger.info("host %s connected from %s", host_alias, peer)

            # Main frame processing loop
            while True:
                frame = await self._read_frame(reader)
                if frame is None:
                    break  # connection closed or malformed
                await self._process_frame(host_alias, frame)

        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            logger.debug("connection lost: %s (%s)", host_alias, peer)
        except Exception:
            logger.exception("handler error for %s (%s)", host_alias, peer)
        finally:
            if host_alias is not None:
                self._registry.remove(host_alias)
                self._write_locks.pop(host_alias, None)
                self._last_pong.pop(host_alias, None)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # ── JSON request handler (CLI daemon management) ───────────────────

    async def _handle_json_request(
        self,
        first_byte: bytes,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a JSON request/response exchange.

        Reads the remainder of the line, parses JSON, dispatches to
        the appropriate handler, and writes back a JSON response.
        """
        try:
            line = first_byte + await asyncio.wait_for(
                reader.readline(), timeout=FRAME_READ_TIMEOUT,
            )
            req = json.loads(line.decode().strip())
            command = req.get("command", "")

            if command == "daemon-status":
                server = getattr(self, "_server_ref", None)
                if server is not None:
                    resp = server.status()
                else:
                    resp = {"running": True}
            elif command == "mailbox":
                resp = await self._handle_mailbox_request(req)
            else:
                resp = {"exit_code": 1, "stdout": "", "stderr": f"unknown command: {command}\n"}

            writer.write(json.dumps(resp, ensure_ascii=False).encode() + b"\n")
            await writer.drain()
        except (json.JSONDecodeError, asyncio.TimeoutError) as exc:
            resp = {"exit_code": 1, "stdout": "", "stderr": f"invalid request: {exc}\n"}
            writer.write(json.dumps(resp).encode() + b"\n")
            try:
                await writer.drain()
            except Exception:
                pass
        except Exception as exc:
            logger.exception("JSON request handler error")
            resp = {"exit_code": 1, "stdout": "", "stderr": f"handler error: {exc}\n"}
            writer.write(json.dumps(resp).encode() + b"\n")
            try:
                await writer.drain()
            except Exception:
                pass

    async def _handle_mailbox_request(self, req: dict) -> dict:
        """Execute a mailbox CLI request locally and return the result.

        Mirrors the wire-protocol mailbox handling in
        :func:`codeagent.remote_exec._handle_mailbox` but runs
        synchronously in the daemon's event loop.
        """
        import io
        args = req.get("args", [])
        if not isinstance(args, list):
            return {"exit_code": 1, "stdout": "", "stderr": "mailbox 'args' must be a list\n"}

        mailbox_root = req.get("mailbox_root", "")
        if mailbox_root and isinstance(mailbox_root, str):
            import re
            if not re.match(r"^/[a-zA-Z0-9/_.-]+$", mailbox_root):
                return {"exit_code": 1, "stdout": "", "stderr": f"invalid mailbox_root: {mailbox_root}\n"}
            args = ["--mailbox-root", mailbox_root] + list(args)

        import sys as _sys
        old_stdout, old_stderr = _sys.stdout, _sys.stderr
        buf_out, buf_err = io.StringIO(), io.StringIO()
        try:
            _sys.stdout = buf_out
            _sys.stderr = buf_err
            from codeagent.mailbox.cli import main as mailbox_main
            mailbox_main(args)
            exit_code = 0
        except SystemExit as e:
            code = e.code
            if code is None:
                exit_code = 0
            elif isinstance(code, int):
                exit_code = code
            else:
                buf_err.write(f"{code}\n")
                exit_code = 1
        except Exception as e:
            buf_err.write(f"error: {e}\n")
            exit_code = 1
        finally:
            _sys.stdout, _sys.stderr = old_stdout, old_stderr

        return {
            "exit_code": exit_code,
            "stdout": buf_out.getvalue(),
            "stderr": buf_err.getvalue(),
        }

    # ── frame reading ───────────────────────────────────────────────────

    async def _read_frame(
        self, reader: asyncio.StreamReader,
    ) -> Frame | None:
        """Read one complete frame from *reader*.

        Returns ``None`` when the connection is cleanly closed or the
        received data is malformed (both trigger connection teardown).
        """
        try:
            header = await asyncio.wait_for(
                reader.readexactly(4), timeout=FRAME_READ_TIMEOUT,
            )
        except (asyncio.IncompleteReadError, asyncio.TimeoutError):
            return None

        import struct
        frame_length = struct.unpack(">I", header)[0]
        if frame_length > 1_048_576:  # MAX_FRAME_SIZE
            return None

        try:
            body = await asyncio.wait_for(
                reader.readexactly(frame_length), timeout=FRAME_READ_TIMEOUT,
            )
        except (asyncio.IncompleteReadError, asyncio.TimeoutError):
            return None

        try:
            frame, _ = decode_frame(header + body)
            return frame
        except (ValueError, json.JSONDecodeError):
            return None

    # ── frame dispatch ──────────────────────────────────────────────────

    async def _process_frame(self, sender_alias: str, frame: Frame) -> None:
        """Dispatch a decoded frame to the appropriate handler."""
        if frame.type == FrameType.MESSAGE:
            await self._handle_message(sender_alias, frame)
        elif frame.type == FrameType.ACK:
            self._handle_ack(sender_alias, frame)
        elif frame.type == FrameType.PING:
            await self._handle_ping(sender_alias, frame)
        elif frame.type == FrameType.PONG:
            self._handle_pong(sender_alias)
        elif frame.type == FrameType.GOODBYE:
            raise ConnectionResetError(f"{sender_alias} said goodbye")

    async def _handle_message(self, sender_alias: str, frame: Frame) -> None:
        """Process an inbound MESSAGE frame: persist → ACK."""
        to_id = frame.payload.get("to", "")
        payload = dict(frame.payload)

        # Ensure session_id is present in the payload for mailbox store
        if "session_id" not in payload:
            payload["session_id"] = frame.session_id

        # Let the server layer validate the session roster and write to
        # the mailbox inbox.  If the server reference was injected we use
        # it; otherwise we fall back to a direct store write.
        server = getattr(self, "_server_ref", None)
        if server is not None:
            try:
                await server._write_inbound(frame.session_id, to_id, payload)
            except (ValueError, KeyError) as exc:
                logger.warning("inbound message rejected: %s", exc)
                await self._send_nack(sender_alias, frame.session_id, str(exc))
                return
        else:
            # Direct write (used when server layer is not attached)
            try:
                self._mailbox_store.send(
                    session_id=frame.session_id,
                    from_id=payload.get("from", "unknown"),
                    to_id=to_id,
                    subject=payload.get("subject", "(forwarded)"),
                    body=payload.get("body", json.dumps(payload)),
                    kind=payload.get("kind", "REPORT"),
                )
            except Exception as exc:
                logger.warning("inbound write failed: %s", exc)
                await self._send_nack(sender_alias, frame.session_id, str(exc))
                return

        # ACK back to sender
        await self._send_frame_to(sender_alias, Frame(
            type=FrameType.ACK,
            session_id=frame.session_id,
            payload={"msg_id": payload.get("msg_id", ""), "status": "delivered"},
        ))

    def _handle_ack(self, sender_alias: str, frame: Frame) -> None:
        """Process an ACK: mark the corresponding spool entry as acked."""
        msg_id = frame.payload.get("msg_id", "")
        if not msg_id:
            return
        # Spool key is ``msg_id@host_alias``
        spool_id = f"{msg_id}@{sender_alias}"
        try:
            self._spool_store.ack(
                spool_uuid=spool_id,
                session_id=frame.session_id,
                host_alias=sender_alias,
            )
        except FileNotFoundError:
            logger.debug("ACK for unknown spool entry: %s", spool_id)

    async def _handle_ping(self, sender_alias: str, frame: Frame) -> None:
        """Respond to a PING with a PONG."""
        await self._send_frame_to(sender_alias, Frame(
            type=FrameType.PONG,
            session_id=frame.session_id,
            payload={},
        ))

    def _handle_pong(self, sender_alias: str) -> None:
        """Record the last pong timestamp for a host."""
        self._last_pong[sender_alias] = time.monotonic()

    # ── outbound helpers ────────────────────────────────────────────────

    async def _send_nack(
        self, host_alias: str, session_id: str, reason: str,
    ) -> None:
        """Send a NACK frame to *host_alias*."""
        await self._send_frame_to(host_alias, Frame(
            type=FrameType.NACK,
            session_id=session_id,
            payload={"reason": reason},
        ))

    async def send_to_host(
        self, host_alias: str, frame: Frame,
    ) -> bool:
        """Send *frame* to a specific connected host.

        Returns ``True`` on success, ``False`` if the host is not
        connected or the write fails.
        """
        return await self._send_frame_to(host_alias, frame)

    async def _send_frame_to(
        self, host_alias: str, frame: Frame,
    ) -> bool:
        """Send a frame to *host_alias* through the registry."""
        pair = self._registry.get(host_alias)
        if pair is None:
            return False
        _, writer = pair
        return await self._send_frame(writer, frame)

    async def _send_frame(
        self, writer: asyncio.StreamWriter, frame: Frame,
    ) -> bool:
        """Encode and write *frame* to *writer* with drain."""
        try:
            data = encode_frame(frame)
            writer.write(data)
            await writer.drain()
            return True
        except (ConnectionResetError, BrokenPipeError, OSError):
            return False

    async def deliver_message(
        self,
        session_id: str,
        payload: dict,
        to_host: str,
        spool_store: SpoolStore | None = None,
    ) -> bool:
        """Spool a message (if a store is provided) and send it to *to_host*.

        Returns ``True`` when the frame was successfully written to the
        socket.  The caller is responsible for waiting on the subsequent
        ACK and marking the spool entry as acked.
        """
        msg_id = payload.get("msg_id", "")
        if spool_store is not None:
            from codeagent.tcp.spool import SpoolEntry, uuid as _uuid
            entry = SpoolEntry(
                uuid=msg_id or str(_uuid.uuid4()),
                session_id=session_id,
                from_id=payload.get("from", ""),
                to_id=payload.get("to", ""),
                msg_id=msg_id,
                payload=payload,
                created_at=time.time(),
                host_alias=to_host,
            )
            spool_store.write(entry)

        return await self.send_to_host(to_host, Frame(
            type=FrameType.MESSAGE,
            session_id=session_id,
            payload=payload,
        ))

    async def flush_spool(
        self, spool_store: SpoolStore,
        routing: SessionRoutingTable | None = None,
    ) -> dict[str, int]:
        """Replay all pending spool entries to their target hosts.

        Returns ``{"resent": N, "skipped": M}``.
        """
        resent = 0
        skipped = 0
        for entry in spool_store.replay():
            if not self._registry.is_connected(entry.host_alias):
                skipped += 1
                continue
            ok = await self.send_to_host(entry.host_alias, Frame(
                type=FrameType.MESSAGE,
                session_id=entry.session_id,
                payload=entry.payload,
            ))
            if ok:
                resent += 1
            else:
                skipped += 1
        return {"resent": resent, "skipped": skipped}

    # ── heartbeat ───────────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        """Periodically send PINGs and evict stale hosts."""
        while True:
            try:
                await asyncio.sleep(self._heartbeat_interval)
                now = time.monotonic()
                for host_alias in list(self._registry.list_hosts()):
                    last = self._last_pong.get(host_alias, 0.0)
                    if now - last > STALE_HOST_TIMEOUT:
                        logger.warning("evicting stale host %s", host_alias)
                        pair = self._registry.get(host_alias)
                        if pair is not None:
                            _, writer = pair
                            try:
                                writer.close()
                            except Exception:
                                pass
                        self._registry.remove(host_alias)
                        self._write_locks.pop(host_alias, None)
                        self._last_pong.pop(host_alias, None)
                        continue

                    await self._send_frame_to(host_alias, Frame(
                        type=FrameType.PING,
                        session_id="",
                        payload={},
                    ))
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("heartbeat sweep error")
