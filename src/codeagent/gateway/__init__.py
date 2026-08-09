"""Gateway — per-device local control plane for runtime/mailbox orchestration."""
from codeagent.gateway.client import GatewayClient, rpc_stdio
from codeagent.gateway.events import EventStore, control_socket_path, events_db_path
from codeagent.gateway.model import (
    GATEWAY_PROTOCOL_VERSION,
    GatewayError,
    GatewayRequest,
    GatewayResponse,
    RuntimeEvent,
    RuntimeEventDraft,
)
from codeagent.gateway.service import AgentGateway

__all__ = [
    "AgentGateway",
    "EventStore",
    "GATEWAY_PROTOCOL_VERSION",
    "GatewayClient",
    "GatewayError",
    "GatewayRequest",
    "GatewayResponse",
    "RuntimeEvent",
    "RuntimeEventDraft",
    "control_socket_path",
    "events_db_path",
    "rpc_stdio",
]
