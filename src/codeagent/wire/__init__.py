"""Wire protocol for JSONL communication with remote exec helpers."""
from codeagent.wire.protocol import (
    WIRE_VERSION,
    WireMessage,
    decode_line,
    encode_line,
    make_accepted,
    make_capabilities,
    make_error,
    make_ping,
    make_pong,
    make_ready,
    make_request,
    make_result,
    make_session,
)

__all__ = [
    "WIRE_VERSION",
    "WireMessage",
    "decode_line",
    "encode_line",
    "make_accepted",
    "make_capabilities",
    "make_error",
    "make_ping",
    "make_pong",
    "make_ready",
    "make_request",
    "make_result",
    "make_session",
]
