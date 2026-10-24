"""Discoverable execution wire schemas; data does not grant execution authority."""

from __future__ import annotations

import json
from typing import Any

from potbot.domain.serialization import canonical_json
from potbot.tools.execution_contracts import (
    MAX_EVENTS,
    MAX_RUNS,
    MAX_TRACE_BYTES,
    RECEIPT,
    REPLAY_REQUEST,
    REPLAY_RESULT,
    REQUEST,
    RESULT,
    SEAL_REQUEST,
    SEAL_RESULT,
    TERMINAL,
    WORK_KINDS,
)


def schemas() -> dict[str, dict[str, Any]]:
    """Return independent nested schema values for each versioned data Type."""
    sha = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    uuid = {"type": "string", "pattern": "^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$"}
    artifact = {
        "type": "string",
        "pattern": "^artifact:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$",
    }
    operation = {
        "type": "string",
        "pattern": "^syn://thing/[a-z0-9]+(?:[-_][a-z0-9]+)*$",
        "maxLength": 512,
    }
    type_ref = {
        "type": "string",
        "pattern": "^(?:syn|synny)://type/[a-z0-9]+(?:[-_][a-z0-9]+)*$",
        "maxLength": 512,
    }

    def obj(properties: dict[str, Any], optional: tuple[str, ...] = ()) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": properties,
            "required": [k for k in properties if k not in optional],
            "additionalProperties": False,
        }

    def nullable(schema: dict[str, Any]) -> dict[str, Any]:
        return {"anyOf": [schema, {"type": "null"}]}

    typed = obj({"type_ref": type_ref, "value": {}})
    version = {"type": "integer", "const": 1}
    root_status = {"type": "string", "enum": sorted(TERMINAL)}
    count = {"type": "integer", "minimum": 1, "maximum": MAX_EVENTS}
    run_count = {"type": "integer", "minimum": 1, "maximum": MAX_RUNS}
    expected = {"expected_input_sha256": sha, "expected_policy_hash": sha}
    summary = {
        "schema_version": version,
        "receipt_id": uuid,
        "body_sha256": sha,
        "trace_sha256": sha,
        "trace_artifact_ref": artifact,
        "signature_artifact_ref": artifact,
        "signature_status": {"const": "valid_at_seal"},
        "outcome_verification": {"const": "not_assessed"},
        "root_status": root_status,
        "event_count": count,
        "run_count": run_count,
        "trace_complete": {"const": True},
        "reexecution_authorized": {"const": False},
    }
    metric = obj(
        {
            "value": {"type": ["number", "null"]},
            "unit": {"type": "string", "minLength": 1, "maxLength": 32},
            "provenance": {"enum": ["measured", "unavailable"]},
            "scope": {"type": "string", "maxLength": 64},
        },
        ("scope",),
    )
    metrics = {
        "type": "object",
        "additionalProperties": metric,
        "required": ["elapsed_wall_ms", "controller_cpu_ms"],
        "maxProperties": 32,
    }
    event = obj(
        {
            "sequence": {"type": "integer", "minimum": 1, "maximum": 9007199254740991},
            "event_ref": {
                "type": "string",
                "pattern": "^syn://event/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$",
            },
            "run_id": uuid,
            "parent_run_id": nullable(uuid),
            "parent_event_id": nullable(uuid),
            "stage": {"enum": ["started", "updated", *sorted(TERMINAL)]},
            "trigger_input_sha256": sha,
            "output_sha256": nullable(sha),
            "elapsed_ms": {"type": "integer", "minimum": 0},
            "work_kind": {"enum": [*WORK_KINDS, "seal", "replay"]},
            "metrics": metrics,
        }
    )
    values = {
        REQUEST: obj({"schema_version": version, "operation_ref": operation, "input": typed}),
        RESULT: obj(
            {
                "schema_version": version,
                "operation_ref": operation,
                "result": typed,
                "work_run_id": uuid,
                "status": {"const": "completed"},
            }
        ),
        SEAL_REQUEST: obj({"schema_version": version, "root_run_id": uuid, **expected}),
        SEAL_RESULT: obj(summary),
        REPLAY_REQUEST: obj(
            {
                "schema_version": version,
                "receipt_id": uuid,
                **expected,
                "limit": {"type": "integer", "minimum": 1, "maximum": 8},
                "cursor": uuid,
            },
            ("cursor",),
        ),
        REPLAY_RESULT: obj(
            {
                **summary,
                "items": {"type": "array", "items": event, "maxItems": 8},
                "after_index": {"type": "integer", "minimum": 0, "maximum": MAX_EVENTS},
                "next_cursor": nullable(uuid),
                "page_complete": {"type": "boolean"},
                "page_qualification": {"const": "bounded_projection_of_retained_trace"},
            }
        ),
        RECEIPT: obj(
            {
                "schema_version": version,
                "type_ref": {"const": RECEIPT},
                "workspace_id": uuid,
                "owner_principal_id": uuid,
                "root_run_id": uuid,
                "catalog_hash": sha,
                "policy_hash": sha,
                "input_sha256": sha,
                "root_scope_hash": sha,
                "trace_sha256": sha,
                "trace_artifact_id": uuid,
                "trace_bytes": {"type": "integer", "minimum": 1, "maximum": MAX_TRACE_BYTES},
                "trace_media_type": {"const": "application/json"},
                "trace_encoding": {"const": "utf-8"},
                "trace_complete": {"const": True},
                "run_count": run_count,
                "event_count": count,
                "root_status": root_status,
                "outcome_verification": {"const": "not_assessed"},
            }
        ),
    }
    for ref, schema in values.items():
        schema.update({"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": ref})
    result: dict[str, dict[str, Any]] = json.loads(canonical_json(values))
    return result
