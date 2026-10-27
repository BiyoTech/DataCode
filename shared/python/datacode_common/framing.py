"""Length-prefixed JSON framing — one implementation, several protocols.

Both long-lived channels the platform runs over a socket use the same frame:

    4-byte big-endian payload length, then that many bytes of UTF-8 JSON.

``executor_protocol`` (warm SQLMesh Context session) and ``warm_protocol``
(resident task runner) therefore share this module instead of restating the
codec. The one place that deliberately keeps its own copy is
``workspace-runtime/src/executor/server.py``: that process must be able to start
from a bare interpreter with a cleared environment, before any package path is
assumed, so it cannot import from here — a documented, tested duplication rather
than an accidental one.

Why a length prefix at all: the payloads are JSON objects of unbounded size, and
newline delimiting would break the moment a task's output contains a newline
inside a string. The length is read first, so a frame is either complete or
absent — never half-interpreted.
"""

from __future__ import annotations

import json
import struct
from typing import Any, Mapping

# 64 MiB. Model metadata for very large repositories can be big, but anything
# past this is almost certainly a serialization bug rather than user data, and
# an unbounded read is a denial-of-service primitive.
MAX_FRAME_BYTES = 64 * 1024 * 1024

_HEADER = struct.Struct(">I")
HEADER_SIZE = _HEADER.size


class ProtocolError(ValueError):
    """Malformed frame, oversized payload, or an unsupported message shape."""


def encode(message: Mapping[str, Any]) -> bytes:
    """Frame one JSON message.

    ``default=str`` keeps a stray non-JSON value (a Path, an Enum) from turning
    into an exception at the transport layer, where the caller has no context to
    act on it.
    """
    payload = json.dumps(dict(message), separators=(",", ":"), default=str).encode("utf-8")
    if len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError(f"payload exceeds {MAX_FRAME_BYTES} bytes")
    return _HEADER.pack(len(payload)) + payload


def decode(payload: bytes) -> dict[str, Any]:
    """Decode one frame's payload (without the length header)."""
    try:
        message = json.loads(payload.decode("utf-8"))
    except ValueError as exc:
        raise ProtocolError(f"frame payload is not valid JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise ProtocolError("frame payload must be a JSON object")
    return message


def frame_length(header: bytes) -> int:
    """Payload length declared by a 4-byte header.

    Raises:
        ProtocolError: The header is truncated or declares an oversized frame.
            Checked HERE rather than after reading, so an absurd length is never
            allocated for.
    """
    if len(header) != HEADER_SIZE:
        raise ProtocolError("truncated frame header")
    (length,) = _HEADER.unpack(header)
    if length > MAX_FRAME_BYTES:
        raise ProtocolError(f"declared payload {length} exceeds {MAX_FRAME_BYTES}")
    return length


async def read_frame(reader) -> dict[str, Any]:
    """Read one framed message from an asyncio StreamReader.

    Returns ``{}`` on a clean EOF (the peer closed between frames), which every
    caller treats as "the session ended" rather than an error: a resident
    process exiting after its work is done is normal.
    """
    header = await reader.read(HEADER_SIZE)
    if not header:
        return {}
    length = frame_length(header)

    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = await reader.read(remaining)
        if not chunk:
            raise ProtocolError("truncated frame payload")
        chunks.append(chunk)
        remaining -= len(chunk)
    return decode(b"".join(chunks))


def read_frame_sync(stream) -> dict[str, Any]:
    """Blocking counterpart of :func:`read_frame` for a file-like stream.

    Used by the resident runner, which is a plain synchronous process: one task
    at a time is the contract, so an event loop would add machinery without
    adding concurrency.
    """
    header = _read_exactly(stream, HEADER_SIZE)
    if not header:
        return {}
    length = frame_length(header)
    payload = _read_exactly(stream, length)
    if len(payload) < length:
        raise ProtocolError("truncated frame payload")
    return decode(payload)


def _read_exactly(stream, size: int) -> bytes:
    """Read exactly ``size`` bytes, or fewer only at EOF."""
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


__all__ = [
    "HEADER_SIZE",
    "MAX_FRAME_BYTES",
    "ProtocolError",
    "decode",
    "encode",
    "frame_length",
    "read_frame",
    "read_frame_sync",
]
