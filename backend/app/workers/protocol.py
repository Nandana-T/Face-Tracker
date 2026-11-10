"""Wire protocol between the video worker and the API (and API -> browser frame relay).

Transport: one WebSocket initiated by the WORKER (outbound only, so a worker next to an RTSP
camera behind NAT can serve a hosted API). Text frames carry JSON control messages; binary frames
carry one JPEG plus its detection metadata:

    [uint32 big-endian N][N bytes UTF-8 JSON header][JPEG bytes]

Everything is size-bounded (see MAX_*). Full-resolution pixel arrays are never serialised; frames
are down-scaled JPEGs.
"""

from __future__ import annotations

import json
import struct
from typing import Any

# worker -> API
MSG_HELLO = "hello"
MSG_HEARTBEAT = "heartbeat"
MSG_ACCEPTED = "session.accepted"
MSG_STATE = "session.state"
MSG_STATS = "session.stats"
MSG_ENDED = "session.ended"
# API -> worker
MSG_START = "session.start"
MSG_STOP = "session.stop"

MAX_CONTROL_BYTES = 16 * 1024
MAX_HEADER_BYTES = 32 * 1024
MAX_FRAME_BYTES = 2 * 1024 * 1024  # whole binary message

_LEN = struct.Struct(">I")


class ProtocolError(ValueError):
    pass


def encode_control(message: dict[str, Any]) -> str:
    text = json.dumps(message, separators=(",", ":"))
    if len(text.encode("utf-8")) > MAX_CONTROL_BYTES:
        raise ProtocolError("control message too large")
    return text


def decode_control(text: str) -> dict[str, Any]:
    if len(text) > MAX_CONTROL_BYTES:
        raise ProtocolError("control message too large")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError("invalid JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("type"), str):
        raise ProtocolError("message must be an object with a string 'type'")
    return data


def pack_frame(header: dict[str, Any], jpeg: bytes) -> bytes:
    raw = json.dumps(header, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_HEADER_BYTES:
        raise ProtocolError("frame header too large")
    message = _LEN.pack(len(raw)) + raw + jpeg
    if len(message) > MAX_FRAME_BYTES:
        raise ProtocolError("frame too large")
    return message


def unpack_frame(message: bytes) -> tuple[dict[str, Any], bytes]:
    if len(message) > MAX_FRAME_BYTES or len(message) < _LEN.size:
        raise ProtocolError("invalid frame size")
    (n,) = _LEN.unpack_from(message)
    if n > MAX_HEADER_BYTES or _LEN.size + n > len(message):
        raise ProtocolError("invalid frame header length")
    try:
        header = json.loads(message[_LEN.size : _LEN.size + n])
    except json.JSONDecodeError as exc:
        raise ProtocolError("invalid frame header") from exc
    if not isinstance(header, dict):
        raise ProtocolError("frame header must be an object")
    jpeg = message[_LEN.size + n :]
    if not jpeg.startswith(b"\xff\xd8"):
        raise ProtocolError("frame payload is not a JPEG")
    return header, jpeg
