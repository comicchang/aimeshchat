"""TCP wire frame protocol for cross-host mailbox IPC.

Frame format (binary):
  [4 bytes: frame_length (big-endian uint32)]
  [1 byte: frame_type]
  [32 bytes: session_id (null-padded)]
  [remaining: JSON payload]

Frame types:
  HELLO     = 0x01  # handshake
  READY     = 0x02  # handshake ack
  MESSAGE   = 0x10  # mailbox message
  ACK       = 0x11  # delivery ack
  NACK      = 0x12  # delivery nack
  PING      = 0x20  # heartbeat
  PONG      = 0x21  # heartbeat response
  GOODBYE   = 0x30  # clean disconnect
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from enum import IntEnum

from codeagent.constants import (
    TCP_FRAME_HEADER_SIZE,
    TCP_MAX_FRAME_SIZE,
    TCP_SESSION_ID_SIZE,
)


class FrameType(IntEnum):
    """Wire frame type tags."""

    HELLO = 0x01
    READY = 0x02
    MESSAGE = 0x10
    ACK = 0x11
    NACK = 0x12
    PING = 0x20
    PONG = 0x21
    GOODBYE = 0x30


FRAME_HEADER_SIZE = TCP_FRAME_HEADER_SIZE  # re-export for local use
MAX_FRAME_SIZE = TCP_MAX_FRAME_SIZE
SESSION_ID_SIZE = TCP_SESSION_ID_SIZE


@dataclass
class Frame:
    """A single decoded TCP wire frame."""

    type: FrameType
    session_id: str
    payload: dict


def encode_frame(frame: Frame) -> bytes:
    """Encode a *Frame* to the on-wire binary representation.

    Returns the complete frame bytes including the 4-byte length prefix.
    Raises :class:`ValueError` if the serialised frame exceeds *MAX_FRAME_SIZE*.
    """
    session_bytes = (
        frame.session_id.encode("utf-8")[:SESSION_ID_SIZE]
        .ljust(SESSION_ID_SIZE, b"\x00")
    )
    payload_bytes = json.dumps(frame.payload, ensure_ascii=False).encode("utf-8")
    frame_length = 1 + SESSION_ID_SIZE + len(payload_bytes)
    if frame_length > MAX_FRAME_SIZE:
        raise ValueError(f"frame too large: {frame_length} > {MAX_FRAME_SIZE}")
    return (
        struct.pack(">I", frame_length)
        + bytes([frame.type])
        + session_bytes
        + payload_bytes
    )


def decode_frame(data: bytes) -> tuple[Frame, int]:
    """Decode one *Frame* from a byte buffer.

    Returns ``(frame, bytes_consumed)``.  Raises :class:`ValueError` on
    incomplete data, oversized frames, invalid frame types, or corrupt payloads.
    """
    if len(data) < 4:
        raise ValueError("incomplete frame header")
    frame_length = struct.unpack(">I", data[:4])[0]
    if frame_length > MAX_FRAME_SIZE:
        raise ValueError(f"frame too large: {frame_length}")
    total = 4 + frame_length
    if len(data) < total:
        raise ValueError("incomplete frame payload")
    frame_type_raw = data[4]
    try:
        frame_type = FrameType(frame_type_raw)
    except ValueError:
        raise ValueError(f"invalid frame type: 0x{frame_type_raw:02x}")
    session_id = data[5 : 5 + SESSION_ID_SIZE].rstrip(b"\x00").decode("utf-8")
    payload_bytes = data[5 + SESSION_ID_SIZE : total]
    try:
        payload = json.loads(payload_bytes) if payload_bytes else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"corrupt payload: {exc}") from exc
    return Frame(type=frame_type, session_id=session_id, payload=payload), total
