"""Tests for tcp.protocol — binary frame encode/decode."""
from __future__ import annotations

import json
import struct

import pytest

from codeagent.constants import (
    TCP_FRAME_HEADER_SIZE,
    TCP_MAX_FRAME_SIZE,
    TCP_SESSION_ID_SIZE,
)
from codeagent.tcp.protocol import (
    FRAME_HEADER_SIZE,
    MAX_FRAME_SIZE,
    SESSION_ID_SIZE,
    Frame,
    FrameType,
    decode_frame,
    encode_frame,
)


# ─────────────────────────────────────────────────────────────────────────────
# Constants re-export alignment
# ─────────────────────────────────────────────────────────────────────────────


class TestConstantsReExport:
    """tcp.protocol re-exports match codeagent.constants values."""

    def test_frame_header_size(self):
        assert FRAME_HEADER_SIZE == TCP_FRAME_HEADER_SIZE == 37

    def test_max_frame_size(self):
        assert MAX_FRAME_SIZE == TCP_MAX_FRAME_SIZE == 1_048_576

    def test_session_id_size(self):
        assert SESSION_ID_SIZE == TCP_SESSION_ID_SIZE == 32


# ─────────────────────────────────────────────────────────────────────────────
# FrameType enum completeness
# ─────────────────────────────────────────────────────────────────────────────


class TestFrameTypeEnum:
    """Every declared frame type has the correct value."""

    @pytest.mark.parametrize(
        "name, value",
        [
            ("HELLO", 0x01),
            ("READY", 0x02),
            ("MESSAGE", 0x10),
            ("ACK", 0x11),
            ("NACK", 0x12),
            ("PING", 0x20),
            ("PONG", 0x21),
            ("GOODBYE", 0x30),
        ],
    )
    def test_member_value(self, name: str, value: int):
        assert FrameType[name] == value

    def test_count(self):
        assert len(FrameType) == 8


# ─────────────────────────────────────────────────────────────────────────────
# encode_frame
# ─────────────────────────────────────────────────────────────────────────────


class TestEncodeFrame:
    def test_basic_structure(self):
        frame = Frame(type=FrameType.PING, session_id="abc", payload={})
        data = encode_frame(frame)
        # 4 (length) + 1 (type) + 32 (session) + 2 (empty json "{}")
        assert len(data) == 4 + 1 + 32 + 2
        # length prefix excludes itself
        length = struct.unpack(">I", data[:4])[0]
        assert length == 1 + 32 + 2

    def test_frame_type_byte(self):
        frame = Frame(type=FrameType.HELLO, session_id="s", payload={})
        data = encode_frame(frame)
        assert data[4] == 0x01

    def test_session_id_encoding_short(self):
        frame = Frame(type=FrameType.PING, session_id="hi", payload={})
        data = encode_frame(frame)
        raw_sid = data[5 : 5 + 32]
        assert raw_sid[:2] == b"hi"
        assert raw_sid[2:] == b"\x00" * 30

    def test_session_id_encoding_exact_32(self):
        sid = "A" * 32
        frame = Frame(type=FrameType.PING, session_id=sid, payload={})
        data = encode_frame(frame)
        raw_sid = data[5 : 5 + 32]
        assert raw_sid == sid.encode("utf-8")

    def test_session_id_truncation(self):
        sid = "B" * 64  # longer than 32
        frame = Frame(type=FrameType.PING, session_id=sid, payload={})
        data = encode_frame(frame)
        raw_sid = data[5 : 5 + 32]
        assert raw_sid == b"B" * 32

    def test_session_id_empty(self):
        frame = Frame(type=FrameType.PING, session_id="", payload={})
        data = encode_frame(frame)
        raw_sid = data[5 : 5 + 32]
        assert raw_sid == b"\x00" * 32

    def test_payload_json(self):
        frame = Frame(
            type=FrameType.MESSAGE,
            session_id="s",
            payload={"key": "value", "n": 42},
        )
        data = encode_frame(frame)
        payload_bytes = data[5 + 32 :]
        parsed = json.loads(payload_bytes)
        assert parsed == {"key": "value", "n": 42}

    def test_payload_unicode(self):
        frame = Frame(
            type=FrameType.MESSAGE,
            session_id="s",
            payload={"msg": "你好世界"},
        )
        data = encode_frame(frame)
        payload_bytes = data[5 + 32 :]
        assert json.loads(payload_bytes)["msg"] == "你好世界"

    def test_frame_too_large_raises(self):
        big_payload = {"x": "a" * (MAX_FRAME_SIZE + 1)}
        frame = Frame(type=FrameType.MESSAGE, session_id="s", payload=big_payload)
        with pytest.raises(ValueError, match="frame too large"):
            encode_frame(frame)


# ─────────────────────────────────────────────────────────────────────────────
# decode_frame
# ─────────────────────────────────────────────────────────────────────────────


class TestDecodeFrame:
    def test_basic_decode(self):
        frame = Frame(type=FrameType.PONG, session_id="test", payload={"ok": True})
        data = encode_frame(frame)
        result, consumed = decode_frame(data)
        assert consumed == len(data)
        assert result.type == FrameType.PONG
        assert result.session_id == "test"
        assert result.payload == {"ok": True}

    def test_incomplete_header_raises(self):
        with pytest.raises(ValueError, match="incomplete frame header"):
            decode_frame(b"\x00\x00")

    def test_incomplete_payload_raises(self):
        # Valid header claiming large payload, but no payload bytes
        header = struct.pack(">I", 1000) + b"\x00" * 33
        with pytest.raises(ValueError, match="incomplete frame payload"):
            decode_frame(header)

    def test_frame_too_large_header(self):
        # Length prefix exceeds MAX_FRAME_SIZE
        data = struct.pack(">I", MAX_FRAME_SIZE + 1) + b"\x00"
        with pytest.raises(ValueError, match="frame too large"):
            decode_frame(data)

    def test_invalid_frame_type_raises(self):
        payload = json.dumps({}).encode()
        length = 1 + 32 + len(payload)
        raw = (
            struct.pack(">I", length)
            + bytes([0xFF])  # invalid type
            + b"\x00" * 32
            + payload
        )
        with pytest.raises(ValueError, match="invalid frame type"):
            decode_frame(raw)

    def test_corrupt_json_payload_raises(self):
        length = 1 + 32 + 5
        raw = (
            struct.pack(">I", length)
            + bytes([FrameType.MESSAGE])
            + b"\x00" * 32
            + b"NOTJS"  # not valid JSON
        )
        with pytest.raises(ValueError, match="corrupt payload"):
            decode_frame(raw)

    def test_empty_payload_decodes_to_empty_dict(self):
        payload = b""
        length = 1 + 32 + len(payload)
        raw = (
            struct.pack(">I", length)
            + bytes([FrameType.READY])
            + b"\x00" * 32
            + payload
        )
        frame, consumed = decode_frame(raw)
        assert frame.payload == {}
        assert consumed == len(raw)


# ─────────────────────────────────────────────────────────────────────────────
# session_id extraction
# ─────────────────────────────────────────────────────────────────────────────


class TestSessionIdExtraction:
    def test_short_id(self):
        f = Frame(type=FrameType.PING, session_id="ab", payload={})
        decoded, _ = decode_frame(encode_frame(f))
        assert decoded.session_id == "ab"

    def test_exact_32_id(self):
        sid = "c" * 32
        f = Frame(type=FrameType.PING, session_id=sid, payload={})
        decoded, _ = decode_frame(encode_frame(f))
        assert decoded.session_id == sid

    def test_empty_id(self):
        f = Frame(type=FrameType.PING, session_id="", payload={})
        decoded, _ = decode_frame(encode_frame(f))
        assert decoded.session_id == ""

    def test_id_with_trailing_nulls_stripped(self):
        # Manually build a frame where session_id is short (null-padded)
        payload = json.dumps({}).encode()
        length = 1 + 32 + len(payload)
        sid_bytes = b"xy" + b"\x00" * 30
        raw = struct.pack(">I", length) + bytes([FrameType.PING]) + sid_bytes + payload
        frame, _ = decode_frame(raw)
        assert frame.session_id == "xy"

    def test_unicode_session_id(self):
        f = Frame(type=FrameType.PING, session_id="你好", payload={})
        decoded, _ = decode_frame(encode_frame(f))
        assert decoded.session_id == "你好"


# ─────────────────────────────────────────────────────────────────────────────
# Round-trip for every FrameType
# ─────────────────────────────────────────────────────────────────────────────


class TestRoundTripAllTypes:
    @pytest.mark.parametrize(
        "ftype",
        [
            FrameType.HELLO,
            FrameType.READY,
            FrameType.MESSAGE,
            FrameType.ACK,
            FrameType.NACK,
            FrameType.PING,
            FrameType.PONG,
            FrameType.GOODBYE,
        ],
    )
    def test_round_trip(self, ftype: FrameType):
        original = Frame(
            type=ftype,
            session_id="round-trip-test",
            payload={"type": ftype.name, "data": [1, 2, 3]},
        )
        wire = encode_frame(original)
        decoded, consumed = decode_frame(wire)
        assert consumed == len(wire)
        assert decoded.type == original.type
        assert decoded.session_id == original.session_id
        assert decoded.payload == original.payload


# ─────────────────────────────────────────────────────────────────────────────
# Boundary lengths
# ─────────────────────────────────────────────────────────────────────────────


class TestBoundaryLengths:
    def test_zero_length_payload(self):
        """Payload of 0 bytes (just `{}` is 2 bytes; truly empty only via raw)."""
        # Use raw construction: empty payload_bytes -> payload = {}
        length = 1 + 32 + 0
        raw = struct.pack(">I", length) + bytes([FrameType.ACK]) + b"\x00" * 32
        frame, consumed = decode_frame(raw)
        assert frame.payload == {}
        assert consumed == 4 + length

    def test_max_frame_size_payload(self):
        """Payload that fills exactly to MAX_FRAME_SIZE."""
        # payload_bytes len = MAX_FRAME_SIZE - 1 - 32
        target_payload_len = MAX_FRAME_SIZE - 1 - 32
        payload_str = '"' + "a" * (target_payload_len - 2) + '"'
        assert len(payload_str.encode()) == target_payload_len
        f = Frame(type=FrameType.MESSAGE, session_id="max", payload=json.loads(payload_str))
        wire = encode_frame(f)
        decoded, consumed = decode_frame(wire)
        assert consumed == len(wire)
        assert decoded.payload == json.loads(payload_str)

    def test_one_over_max_frame_size(self):
        """Payload that is 1 byte over the limit should raise during encode."""
        target_payload_len = MAX_FRAME_SIZE - 1 - 32 + 1
        payload_str = '"' + "a" * (target_payload_len - 2) + '"'
        f = Frame(type=FrameType.MESSAGE, session_id="s", payload=json.loads(payload_str))
        with pytest.raises(ValueError, match="frame too large"):
            encode_frame(f)

    def test_minimum_wire_frame(self):
        """Smallest possible frame: 0-byte payload."""
        length = 1 + 32 + 0
        raw = struct.pack(">I", length) + bytes([FrameType.HELLO]) + b"\x00" * 32
        assert len(raw) == 4 + 33  # 37 bytes = FRAME_HEADER_SIZE
        frame, consumed = decode_frame(raw)
        assert consumed == len(raw)
        assert frame.type == FrameType.HELLO


# ─────────────────────────────────────────────────────────────────────────────
# Multiple frames in a single buffer
# ─────────────────────────────────────────────────────────────────────────────


class TestMultiFrameBuffer:
    def test_two_frames_sequential(self):
        f1 = Frame(type=FrameType.PING, session_id="a", payload={})
        f2 = Frame(type=FrameType.PONG, session_id="b", payload={"r": 1})
        buf = encode_frame(f1) + encode_frame(f2)
        decoded1, c1 = decode_frame(buf)
        decoded2, c2 = decode_frame(buf[c1:])
        assert decoded1.type == FrameType.PING
        assert decoded1.session_id == "a"
        assert decoded2.type == FrameType.PONG
        assert decoded2.session_id == "b"
        assert decoded2.payload == {"r": 1}
        assert c1 + c2 == len(buf)

    def test_partial_second_frame_raises(self):
        f1 = Frame(type=FrameType.PING, session_id="a", payload={})
        buf = encode_frame(f1) + b"\x00\x00\x00\x10"  # claims 16 bytes but no data
        _, c1 = decode_frame(buf)
        with pytest.raises(ValueError, match="incomplete frame payload"):
            decode_frame(buf[c1:])
