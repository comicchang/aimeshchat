"""Gateway client — UDS RPC to the local gateway.

One request per connection; returns the parsed GatewayResponse. Also
provides ``rpc_stdio()`` for the SSH-bounded ``gateway rpc --stdio`` path
(reads one GatewayRequest from stdin, forwards, writes one response).
"""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
from typing import Any, Optional

from codeagent.gateway.events import control_socket_path
from codeagent.gateway.model import (
    ERR_FRAME_TOO_LARGE,
    GATEWAY_PROTOCOL_VERSION,
    GatewayError,
    GatewayRequest,
    GatewayResponse,
    MAX_FRAME_LENGTH,
)


class GatewayClient:
    """Unix-socket client for the local AgentGateway."""

    def __init__(self, socket_path: Optional[Path] = None, timeout: float = 15.0) -> None:
        self._socket_path = socket_path or control_socket_path()
        self._timeout = timeout

    def call(self, method: str, params: Optional[dict] = None, request_id: str = "") -> dict:
        """Send one request, return the result dict (raises GatewayError)."""
        import uuid

        req = GatewayRequest(
            v=GATEWAY_PROTOCOL_VERSION,
            id=request_id or uuid.uuid4().hex[:12],
            method=method,
            params=params or {},
        )
        resp = self._roundtrip(req)
        if not resp.ok:
            err = resp.error or {}
            raise GatewayError(err.get("code", "INTERNAL"), err.get("message", "gateway error"), err.get("context"))
        return resp.result

    def _roundtrip(self, req: GatewayRequest) -> GatewayResponse:
        if not self._socket_path.exists():
            raise GatewayError(
                "GATEWAY_DOWN",
                f"gateway socket not found: {self._socket_path} (run 'postmesh gateway start')",
            )
        payload = (req.to_json() + "\n").encode("utf-8")
        if len(payload) > MAX_FRAME_LENGTH + 1:
            return GatewayResponse(
                v=req.v, id=req.id, ok=False,
                error={"code": ERR_FRAME_TOO_LARGE,
                       "message": f"request exceeds {MAX_FRAME_LENGTH} bytes", "context": {}},
            )
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(self._timeout)
            s.connect(str(self._socket_path))
            s.sendall(payload)
            buf = bytearray()
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf.extend(chunk)
                if b"\n" in buf:
                    break
            line = bytes(buf).split(b"\n", 1)[0].decode("utf-8", errors="replace")
            if not line.strip():
                raise GatewayError("EMPTY_RESPONSE", "gateway returned an empty response")
            return GatewayResponse.parse(line)
        except OSError as exc:
            raise GatewayError("GATEWAY_CONNECT_FAILED", f"gateway connect failed: {exc}") from exc
        finally:
            s.close()


def rpc_stdio(socket_path: Optional[Path] = None) -> int:
    """SSH-bounded RPC: read one GatewayRequest from stdin, emit one response.

    Used by ``postmesh gateway rpc --stdio`` over an SSH ControlMaster —
    a bounded control call, never a long-lived stream.
    """
    line = sys.stdin.readline()
    if not line:
        return 0
    try:
        if len(line) > MAX_FRAME_LENGTH:
            resp = GatewayResponse(
                v=GATEWAY_PROTOCOL_VERSION, id="", ok=False,
                error={"code": ERR_FRAME_TOO_LARGE,
                       "message": f"frame exceeds {MAX_FRAME_LENGTH} bytes", "context": {}},
            )
        else:
            req = GatewayRequest.from_dict(json.loads(line))
            client = GatewayClient(socket_path=socket_path)
            try:
                result = client.call(req.method, req.params, request_id=req.id)
                resp = GatewayResponse.ok_response(req, result)
            except GatewayError as exc:
                resp = GatewayResponse.error_response(req, exc)
    except (json.JSONDecodeError, ValueError, GatewayError) as exc:
        err = exc if isinstance(exc, GatewayError) else GatewayError("PROTOCOL", str(exc))
        resp = GatewayResponse(
            v=GATEWAY_PROTOCOL_VERSION, id="", ok=False, error=err.to_dict(),
        )
    sys.stdout.write(resp.to_json() + "\n")
    sys.stdout.flush()
    return 0
