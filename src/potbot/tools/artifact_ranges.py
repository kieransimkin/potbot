"""Compiled semantic contracts for bounded artifact observation, never authority."""

from __future__ import annotations

import json
from typing import Any

from potbot.domain.serialization import canonical_json

MAX_RANGE_BYTES = 8 * 1024
READ_BLOCK_BYTES = 64 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_LEASE_SECONDS = 300
PREFIX = "synny://type/potbot-artifact-"
OPEN_REQUEST, OPEN_RESULT = PREFIX + "open-read-request-v1", PREFIX + "open-read-result-v1"
READ_REQUEST, READ_RESULT = PREFIX + "read-range-request-v1", PREFIX + "read-range-result-v1"
CLOSE_REQUEST, CLOSE_RESULT = PREFIX + "close-read-request-v1", PREFIX + "close-read-result-v1"


def schemas() -> dict[str, dict[str, Any]]:
    uuid = {"type": "string", "pattern": "^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$"}
    sha = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    count = {"type": "integer", "minimum": 0, "maximum": MAX_ARTIFACT_BYTES}
    length = {"type": "integer", "minimum": 1, "maximum": MAX_RANGE_BYTES}
    common = {"schema_version": {"type": "integer", "const": 1}, "lease_id": uuid}
    pinned = {**common, "artifact_sha256": sha}
    identity = {**pinned, "artifact_id": uuid, "byte_length": count}
    values = {
        OPEN_REQUEST: {
            **identity,
            "ttl_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_LEASE_SECONDS},
        },
        OPEN_RESULT: {
            **identity,
            "expires_at_epoch": {"type": "integer", "minimum": 0},
            "media_type": {"type": "string", "minLength": 1, "maxLength": 255},
            "max_range_bytes": {"const": MAX_RANGE_BYTES},
            "read_block_bytes": {"const": READ_BLOCK_BYTES},
            "integrity": {"const": "checked_before_each_range_returns"},
        },
        READ_REQUEST: {**pinned, "offset": count, "length": length},
        READ_RESULT: {
            **identity,
            "offset": count,
            "requested_length": length,
            "returned_length": {"type": "integer", "minimum": 0, "maximum": MAX_RANGE_BYTES},
            "data_base64": {
                "type": "string",
                "maxLength": 4 * ((MAX_RANGE_BYTES + 2) // 3),
                "pattern": "^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$",
            },
            "range_sha256": sha,
            "verified_byte_length": count,
            "maximum_read_request_bytes": {"const": READ_BLOCK_BYTES},
            "read_calls": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_ARTIFACT_BYTES // READ_BLOCK_BYTES + 2,
            },
            "range_ends_at_eof": {"type": "boolean"},
            "integrity": {"const": "full_sha256_verified"},
        },
        CLOSE_REQUEST: pinned,
        CLOSE_RESULT: {
            **pinned,
            "state": {"const": "closed"},
            "artifact_deleted": {"const": False},
        },
    }
    result = {
        ref: {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": ref,
            "type": "object",
            "properties": props,
            "required": list(props),
            "additionalProperties": False,
        }
        for ref, props in values.items()
    }
    copied: dict[str, dict[str, Any]] = json.loads(canonical_json(result))
    return copied
