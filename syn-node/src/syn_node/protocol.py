"""Bounded SYN local wire encoding; byte-compatible with Potbot Phase 42."""

from __future__ import annotations

import json
import re
import socket
import struct
from typing import Any
from uuid import UUID

MAX_FRAME_BYTES = 65_536


class SynProtocolError(Exception):
    """Invalid, unauthenticated or otherwise inadmissible local SYN operation."""


def exact(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise SynProtocolError(f"{label} does not match its versioned fields")
    return value


def hex_bytes(value: object, size: int, label: str) -> bytes:
    if not isinstance(value, str) or re.fullmatch(rf"[0-9a-f]{{{2 * size}}}", value) is None:
        raise SynProtocolError(f"{label} has an invalid canonical encoding")
    return bytes.fromhex(value)


def uuid(value: object) -> str:
    if not isinstance(value, str):
        raise SynProtocolError("request identity must be a UUID")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise SynProtocolError("request identity must be a canonical UUID") from exc
    return value


def canonical(payload: dict[str, Any]) -> bytes:
    try:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                          allow_nan=False).encode("ascii")
    except (ValueError, TypeError, UnicodeEncodeError) as exc:
        raise SynProtocolError("invalid SYN wire data") from exc


def parse(raw: bytes) -> dict[str, Any]:
    if not 0 < len(raw) <= MAX_FRAME_BYTES:
        raise SynProtocolError("SYN wire frame exceeded its limit")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise SynProtocolError("duplicate SYN wire field")
            result[key] = item
        return result

    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except SynProtocolError:
        raise
    except (ValueError, UnicodeError) as exc:
        raise SynProtocolError("invalid SYN JSON") from exc
    if not isinstance(payload, dict) or canonical(payload) != raw:
        raise SynProtocolError("SYN JSON must be a canonical object")
    return payload


def read_exact(conn: socket.socket, count: int) -> bytes:
    out = bytearray()
    while len(out) < count:
        data = conn.recv(count - len(out))
        if not data:
            raise SynProtocolError("truncated SYN local wire frame")
        out.extend(data)
    return bytes(out)


def receive(conn: socket.socket) -> dict[str, Any]:
    count = struct.unpack(">I", read_exact(conn, 4))[0]
    if not 0 < count <= MAX_FRAME_BYTES:
        raise SynProtocolError("SYN local message length rejected")
    return parse(read_exact(conn, count))


def send(conn: socket.socket, payload: dict[str, Any]) -> None:
    data = canonical(payload)
    if not 0 < len(data) <= MAX_FRAME_BYTES:
        raise SynProtocolError("SYN local response exceeds message limit")
    conn.sendall(struct.pack(">I", len(data)) + data)
