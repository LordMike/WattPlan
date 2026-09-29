"""Versioned, lossless optimizer snapshots that fit in Recorder when possible.

Keep the codec free of Home Assistant imports so archives can be replayed locally.
"""

from __future__ import annotations

import base64
import lzma
import math
from typing import Any

import msgpack

MAGIC = b"WP1"
MAX_DECODED_BYTES = 8_000_000
MAX_ENCODED_CHARS = 16_000


def _check_finite(value: Any) -> None:
    """Reject non-standard numeric values before publishing a replay archive."""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Planner snapshot contains a non-finite number")
    if isinstance(value, dict):
        for item in value.values():
            _check_finite(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_finite(item)


def encode_reproduction(value: dict[str, Any]) -> str:
    """Encode primitive optimizer values without numeric rounding."""
    _check_finite(value)
    raw = MAGIC + msgpack.packb(value, use_bin_type=True, strict_types=True)
    if len(raw) > MAX_DECODED_BYTES:
        raise ValueError("Planner snapshot is too large to decode safely")
    payload = base64.b85encode(lzma.compress(raw, preset=6)).decode("ascii")
    if len(payload) > MAX_ENCODED_CHARS:
        raise ValueError("Encoded planner snapshot is too large for Recorder")
    return payload


def decode_reproduction(payload: str) -> dict[str, Any]:
    """Decode an archived snapshot without executing code from its contents."""
    if len(payload) > MAX_ENCODED_CHARS:
        raise ValueError("Encoded planner snapshot is too large")
    decoder = lzma.LZMADecompressor(memlimit=32 * 1024 * 1024)
    data = decoder.decompress(
        base64.b85decode(payload.encode("ascii")), max_length=MAX_DECODED_BYTES + 1
    )
    if len(data) > MAX_DECODED_BYTES or not decoder.eof or decoder.unused_data:
        raise ValueError("Invalid or oversized planner snapshot")
    if not data.startswith(MAGIC):
        raise ValueError("Unsupported planner snapshot format")
    result = msgpack.unpackb(
        data[len(MAGIC) :],
        raw=False,
        strict_map_key=True,
    )
    if not isinstance(result, dict) or result.get("schema_version") != 1:
        raise ValueError("Unsupported planner snapshot schema")
    _check_finite(result)
    return result
