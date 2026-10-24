"""Compiled bounded upload contracts; declarations never grant write authority."""

from __future__ import annotations

import json
from typing import Any

from potbot.domain.serialization import canonical_json
from potbot.tools.artifact_ranges import MAX_ARTIFACT_BYTES, MAX_LEASE_SECONDS, MAX_RANGE_BYTES

PAGE_SLOTS = 128
MAX_CHUNKS = MAX_ARTIFACT_BYTES // MAX_RANGE_BYTES
PREFIX = "synny://type/potbot-artifact-"
NAMES = ("open-upload", "upload-chunk", "inspect-upload", "commit-upload", "abort-upload")
PORTS = tuple((PREFIX + name + "-request-v1", PREFIX + name + "-result-v1") for name in NAMES)


def schemas() -> dict[str, dict[str, Any]]:
    uuid = {"type": "string", "pattern": "^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$"}
    sha = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    count = {"type": "integer", "minimum": 0, "maximum": MAX_ARTIFACT_BYTES}
    slots = {"type": "integer", "minimum": 0, "maximum": MAX_CHUNKS}
    pinned = {
        "schema_version": {"type": "integer", "const": 1},
        "upload_id": uuid,
        "artifact_sha256": sha,
    }
    identity = {**pinned, "byte_length": count}
    state = {"enum": ["open", "expired", "committed", "aborted"]}
    values = (
        (
            {
                **identity,
                "media_type": {"type": "string", "minLength": 1, "maxLength": 255},
                "ttl_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_LEASE_SECONDS},
            },
            {
                **identity,
                "expires_at_epoch": {"type": "integer", "minimum": 0},
                "chunk_bytes": {"const": MAX_RANGE_BYTES},
                "expected_chunks": slots,
                "state": {"const": "open"},
            },
        ),
        (
            {
                **pinned,
                "offset": count,
                "chunk_sha256": sha,
                "data_base64": {
                    "type": "string",
                    "minLength": 4,
                    "maxLength": 4 * ((MAX_RANGE_BYTES + 2) // 3),
                    "pattern": "^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$",
                },
            },
            {
                **pinned,
                "offset": count,
                "chunk_sha256": sha,
                "stored_bytes": {"type": "integer", "minimum": 1, "maximum": MAX_RANGE_BYTES},
                "duplicate": {"type": "boolean"},
            },
        ),
        (
            {**pinned, "cursor": slots},
            {
                **identity,
                "state": state,
                "expected_chunks": slots,
                "received_chunks": slots,
                "received_bytes": count,
                "missing_offsets": {"type": "array", "maxItems": PAGE_SLOTS, "items": count},
                "next_cursor": {"anyOf": [slots, {"type": "null"}]},
            },
        ),
        (
            pinned,
            {
                **identity,
                "state": {"const": "committed"},
                "artifact_id": uuid,
                "media_type": {"type": "string", "minLength": 1, "maxLength": 255},
            },
        ),
        (
            pinned,
            {**pinned, "state": {"const": "aborted"}, "staged_bytes_deleted": {"const": False}},
        ),
    )
    result = {}
    for ports, pair in zip(PORTS, values, strict=True):
        for ref, props in zip(ports, pair, strict=True):
            result[ref] = {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": ref,
                "type": "object",
                "properties": props,
                "required": list(props),
                "additionalProperties": False,
            }
    copied: dict[str, dict[str, Any]] = json.loads(canonical_json(result))
    return copied
