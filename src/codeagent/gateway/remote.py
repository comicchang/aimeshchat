"""Remote gateway RPC over SSH ControlMaster — Manager → remote host.

The Manager drives remote hosts by executing ONE bounded
``codeagent gateway rpc --stdio`` per control call (session.ensure /
runtime.spawn / message send / stop). The remote NEVER initiates a
reverse SSH connection and never opens HTTP/TCP ports.

``SSHStream`` carries the long-lived mailbox/runtime event flow in the
opposite direction (remote → Manager); this module is the bounded
control plane (Manager → remote).
"""
from __future__ import annotations

import json
import logging
import subprocess
import uuid
from pathlib import Path
from typing import Any, Optional

from codeagent.gateway.model import (
    GATEWAY_PROTOCOL_VERSION,
    GatewayError,
    GatewayRequest,
    GatewayResponse,
)
from codeagent.transport.control_master import ControlMaster

log = logging.getLogger(__name__)

_RPC_TIMEOUT_S = 30


def remote_gateway_call(
    host_alias: str,
    method: str,
    params: Optional[dict] = None,
    *,
    timeout: int = _RPC_TIMEOUT_S,
    ssh_bin: str = "ssh",
    shell_prefix: str = "",
) -> dict:
    """Execute one gateway RPC on *host_alias* and return the result dict.

    Raises GatewayError on RPC failure or remote-side error codes.
    """
    cm = ControlMaster(host_alias, ssh_bin=ssh_bin)
    if not cm.is_alive():
        cm.create()

    req = GatewayRequest(
        v=GATEWAY_PROTOCOL_VERSION,
        id=uuid.uuid4().hex[:12],
        method=method,
        params=params or {},
    )
    prefix = shell_prefix or "export PATH=$HOME/.local/bin:$PATH"
    remote_cmd = f"{prefix}; postmesh gateway rpc --stdio"
    ssh_cmd = cm.ssh_cmd(remote_cmd)

    try:
        proc = subprocess.Popen(
            ssh_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out_b, err_b = proc.communicate(
            input=(req.to_json() + "\n").encode("utf-8"), timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise GatewayError("REMOTE_RPC_TIMEOUT", f"remote gateway rpc timed out: {exc}") from exc
    except OSError as exc:
        raise GatewayError("REMOTE_RPC_FAILED", f"remote gateway rpc failed: {exc}") from exc

    out = out_b.decode("utf-8", errors="replace").strip()
    err = err_b.decode("utf-8", errors="replace").strip()
    if not out:
        raise GatewayError(
            "REMOTE_EMPTY_RESPONSE",
            f"remote gateway returned no response (ssh rc={proc.returncode}): {err[:300]}",
        )
    try:
        resp = GatewayResponse.parse(out)
    except (json.JSONDecodeError, ValueError) as exc:
        raise GatewayError(
            "REMOTE_BAD_RESPONSE", f"remote gateway response not JSON: {out[:200]}",
        ) from exc
    if not resp.ok:
        err_info = resp.error or {}
        raise GatewayError(
            err_info.get("code", "REMOTE_ERROR"),
            err_info.get("message", "remote gateway error"),
            err_info.get("context"),
        )
    return resp.result
