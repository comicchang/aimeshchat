"""Gateway UDS server — one NDJSON request per connection, one response.

Framing: a client connects, writes exactly one JSON line (GatewayRequest),
reads exactly one JSON line (GatewayResponse), then closes. Frames larger
than the 1 MiB limit get FRAME_TOO_LARGE immediately. The server runs in
the foreground (``codeagent gateway serve``, inside a tmux pane) and is
managed by ``codeagent gateway start|stop``.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
from pathlib import Path
from typing import Optional

from codeagent.gateway.model import (
    ERR_FRAME_TOO_LARGE,
    ERR_PROTOCOL,
    ERR_VERSION_INCOMPATIBLE,
    GATEWAY_PROTOCOL_VERSION,
    GatewayError,
    GatewayRequest,
    GatewayResponse,
    MAX_FRAME_LENGTH,
)
from codeagent.gateway.service import AgentGateway

log = logging.getLogger(__name__)

READ_TIMEOUT_S = 30


class GatewayServer:
    """Unix-socket gateway server (local control plane)."""

    def __init__(
        self,
        socket_path: Optional[Path] = None,
        gateway: Optional[AgentGateway] = None,
    ) -> None:
        from codeagent.gateway.events import control_socket_path

        self._socket_path = socket_path or control_socket_path()
        self._gateway = gateway or AgentGateway()
        self._sock: Optional[socket.socket] = None
        self._threads: list[threading.Thread] = []
        self._shutdown = threading.Event()

    # ── lifecycle ──────────────────────────────────────────────────────

    def serve_forever(self) -> None:
        """Bind the UDS, then accept connections until stop() is called."""
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._socket_path.parent, 0o700)
        except OSError:
            pass

        # Stale socket handling: only remove when it is actually stale.
        if self._socket_path.exists():
            if _socket_is_alive(self._socket_path):
                raise GatewayError(
                    "ALREADY_RUNNING",
                    f"gateway already running at {self._socket_path}",
                )
            log.warning("removing stale gateway socket %s", self._socket_path)
            self._socket_path.unlink()

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(self._socket_path))
        sock.listen(16)
        try:
            os.chmod(self._socket_path, 0o600)
        except OSError:
            pass
        self._sock = sock
        log.info("gateway listening on %s", self._socket_path)

        while not self._shutdown.is_set():
            try:
                conn, _addr = sock.accept()
            except OSError:
                break
            t = threading.Thread(target=self._handle_connection, args=(conn,), daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        """Signal shutdown and close the listening socket."""
        self._shutdown.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._socket_path.exists():
            try:
                self._socket_path.unlink()
            except OSError:
                pass

    # ── connection handling ────────────────────────────────────────────

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(READ_TIMEOUT_S)
            line = _read_frame(conn)
            if line is None:
                return
            if len(line) > MAX_FRAME_LENGTH:
                resp = GatewayResponse(
                    v=GATEWAY_PROTOCOL_VERSION, id="", ok=False,
                    error={"code": ERR_FRAME_TOO_LARGE,
                           "message": f"frame exceeds {MAX_FRAME_LENGTH} bytes",
                           "context": {}},
                )
                _write_frame(conn, resp.to_json())
                return
            try:
                req = GatewayRequest.from_dict(json.loads(line))
            except (json.JSONDecodeError, ValueError, GatewayError) as exc:
                err = exc if isinstance(exc, GatewayError) else GatewayError(ERR_PROTOCOL, str(exc))
                resp = GatewayResponse(
                    v=GATEWAY_PROTOCOL_VERSION, id="", ok=False, error=err.to_dict(),
                )
                _write_frame(conn, resp.to_json())
                return
            if req.v != GATEWAY_PROTOCOL_VERSION:
                resp = GatewayResponse(
                    v=GATEWAY_PROTOCOL_VERSION, id=req.id, ok=False,
                    error={"code": ERR_VERSION_INCOMPATIBLE,
                           "message": f"gateway protocol v{req.v} != v{GATEWAY_PROTOCOL_VERSION}",
                           "context": {}},
                )
                _write_frame(conn, resp.to_json())
                return
            try:
                result = self._gateway.dispatch(req.method, req.params)
                resp = GatewayResponse.ok_response(req, result)
            except GatewayError as exc:
                resp = GatewayResponse.error_response(req, exc)
            except Exception as exc:  # noqa: BLE001 — structured fail-closed
                log.exception("gateway method %s failed", req.method)
                resp = GatewayResponse(
                    v=req.v, id=req.id, ok=False,
                    error={"code": "INTERNAL", "message": str(exc), "context": {}},
                )
            _write_frame(conn, resp.to_json())
        finally:
            try:
                conn.close()
            except OSError:
                pass


# ── framing helpers ────────────────────────────────────────────────────


def _read_frame(conn: socket.socket) -> Optional[bytes]:
    """Read one line (newline-terminated) from the socket."""
    buf = bytearray()
    while True:
        try:
            chunk = conn.recv(65536)
        except (socket.timeout, OSError):
            return None
        if not chunk:
            return None
        buf.extend(chunk)
        nl = buf.find(b"\n")
        if nl >= 0:
            return bytes(buf[:nl])
        if len(buf) > MAX_FRAME_LENGTH:
            # Too large without a terminator — bail with a marker so the
            # caller can respond FRAME_TOO_LARGE.
            return bytes(buf[:MAX_FRAME_LENGTH + 1])


def _write_frame(conn: socket.socket, line: str) -> None:
    try:
        conn.sendall(line.encode("utf-8") + b"\n")
    except OSError:
        pass


def _socket_is_alive(path: Path) -> bool:
    """True when another process is listening on *path*."""
    if not path.exists():
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(str(path))
        s.close()
        return True
    except OSError:
        return False
