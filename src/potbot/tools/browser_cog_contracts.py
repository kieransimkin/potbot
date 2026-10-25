"""Sealed SYN wire contracts and effect admission for workspace browser tools.

These are tool *descriptors*, never permission to attach to Chromium or run JS.
The operator pins a live Xpra application/session in BrowserControlService.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.tools.xpra_browser_worker import CDP_INPUT, CDP_READ, validate_command

OBSERVE_REQUEST = "syn://type/potbot-browser-observe-request-v1"
ACT_REQUEST = "syn://type/potbot-browser-act-request-v1"
PICK_REQUEST = "syn://type/potbot-browser-pick-request-v1"
CAPTURE_REQUEST = "syn://type/potbot-browser-capture-request-v1"
EVIDENCE_RESULT = "syn://type/potbot-browser-evidence-result-v1"

SESSION_ID = re.compile(r"[a-f0-9]{32}\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")
OP_GROUPS = {
    "observe": frozenset({"state", "accessibility", "interactive", "inspect.poll", "inspect.at"}),
    "act": frozenset({"navigate", "input.pointer", "input.keyboard", "role.click", "role.fill"}),
    "pick": frozenset({"inspect.start", "inspect.stop"}),
    "capture": frozenset({"sample"}),
}


def validate_request(group: str, request: object) -> tuple[str, dict[str, Any]]:
    if group not in OP_GROUPS or not isinstance(request, dict) or set(request) != {
        "schema_version", "session_id", "command"
    } or type(request["schema_version"]) is not int or request["schema_version"] != 1:
        raise InvalidInputError("browser Cog input differs from its sealed versioned contract")
    session = request["session_id"]
    if not isinstance(session, str) or SESSION_ID.fullmatch(session) is None:
        raise InvalidInputError("browser Cog needs a registered application session ID")
    try:
        cmd = validate_command(request["command"])
    except (ValueError, TypeError, OverflowError) as error:
        raise InvalidInputError("browser Cog command is not on the allowlist") from error
    operation = cmd["op"]
    if operation == "cdp":
        method = cmd["method"]
        allowed = (group == "observe" and method in CDP_READ) or (
            group == "act" and method in CDP_INPUT
        )
    else:
        allowed = operation in OP_GROUPS[group]
    if not allowed:
        raise ScopeDeniedError("browser effect differs from its registered Cog class")
    if group == "capture" and cmd.get("image", True) is not True:
        raise ScopeDeniedError("capture Cog must retain verified pixels")
    # Snapshot mutable model-provided mappings before crossing the RPC boundary.
    frozen = json.loads(json.dumps(cmd, allow_nan=False, separators=(",", ":")))
    return session, frozen


def schemas() -> dict[str, dict[str, Any]]:
    uri = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
    sha = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    artifact_id = {"type": "string", "pattern": "^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$"}
    maybe_id = {"anyOf": [artifact_id, {"type": "null"}]}
    maybe_sha = {"anyOf": [sha, {"type": "null"}]}
    request = {
        "type": "object", "additionalProperties": False,
        "required": ["schema_version", "session_id", "command"],
        "properties": {
            "schema_version": {"const": 1}, "session_id": uri,
            "command": {"type": "object", "minProperties": 1, "maxProperties": 10,
                        "required": ["op"], "properties": {"op": {"type": "string", "maxLength": 32}}},
        },
    }
    result = {
        "type": "object", "additionalProperties": False,
        "required": [
            "schema_version", "session_id", "status", "kind", "command_sha256",
            "observation_artifact_id", "observation_sha256", "image_artifact_id",
            "image_sha256", "manifest_artifact_id", "manifest_sha256", "preview",
        ],
        "properties": {
            "schema_version": {"const": 1}, "session_id": uri,
            "status": {"const": "recorded"},
            "kind": {"enum": ["observation", "action", "capture"]},
            "command_sha256": sha, "observation_artifact_id": artifact_id,
            "observation_sha256": sha, "image_artifact_id": maybe_id,
            "image_sha256": maybe_sha, "manifest_artifact_id": artifact_id,
            "manifest_sha256": sha,
            "preview": {"anyOf": [{"type": "null"}, {"type": "object", "maxProperties": 8,
                                   "additionalProperties": {"type": ["string", "boolean", "number", "null", "array", "object"]}}]},
        },
    }
    mapping = {ref: request for ref in (
        OBSERVE_REQUEST, ACT_REQUEST, PICK_REQUEST, CAPTURE_REQUEST
    )}
    mapping[EVIDENCE_RESULT] = result
    # Provide an independent schema instance for each semantic Type.
    for ref in mapping:
        mapping[ref] = copy.deepcopy(mapping[ref])
        mapping[ref]["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        mapping[ref]["$id"] = ref
    return mapping
