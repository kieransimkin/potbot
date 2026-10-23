"""Local generation admission checks without loading model weights."""

import hashlib
import json
from pathlib import Path

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.domain.execution import ToolDefinition
from potbot.domain.serialization import content_hash
from potbot.models.local_tool import checked_model, load_request


def test_old_tool_definition_identity_is_preserved():
    tool = ToolDefinition("inspect.local", "inspect", ("/usr/bin/python3",))
    historical = {
        k: getattr(tool, k)
        for k in tool.__dataclass_fields__
        if k not in {"accepts_stdin", "memory_bytes", "cpu_seconds"}
    }
    assert tool.fingerprint == content_hash(historical)


@pytest.mark.parametrize(
    "changes",
    [
        {"memory_bytes": 1},
        {"memory_bytes": True},
        {"cpu_seconds": 0},
        {"cpu_seconds": 3601},
        {"cpu_seconds": True},
    ],
)
def test_invalid_resource_profile_is_rejected(changes):
    with pytest.raises(InvalidInputError):
        ToolDefinition("model.local", "local_generation", ("/usr/bin/python3",), **changes)


def test_explicit_profile_changes_definition_identity():
    base = ToolDefinition("model.local", "local_generation", ("/usr/bin/python3",))
    bounded = ToolDefinition(
        "model.local",
        "local_generation",
        ("/usr/bin/python3",),
        memory_bytes=24 * 1024**3,
        cpu_seconds=120,
    )
    assert base.fingerprint != bounded.fingerprint


@pytest.mark.parametrize(
    "payload",
    [
        {"prompt": "x", "max_new_tokens": 0},
        {"prompt": "x", "max_new_tokens": 513},
        {"prompt": "x", "max_new_tokens": True},
        {"prompt": "", "max_new_tokens": 10},
        {"prompt": "x", "max_new_tokens": 10, "expected": "oracle must not enter"},
    ],
)
def test_request_schema_rejects_unsafe_or_out_of_bound_inputs(tmp_path: Path, payload):
    path = tmp_path / "request.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(InvalidInputError):
        load_request(path)


def test_model_manifest_and_weight_identity_are_both_checked(tmp_path: Path):
    weights = tmp_path / "model.safetensors"
    weights.write_bytes(b"test weights, not a real model")
    revision = "a" * 40
    manifest = {
        "model_id": "test/model",
        "revision": revision,
        "files": [
            {
                "path": weights.name,
                "bytes": weights.stat().st_size,
                "sha256": hashlib.sha256(weights.read_bytes()).hexdigest(),
            }
        ],
    }
    path = tmp_path / "model-manifest.json"
    path.write_text(json.dumps(manifest))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert checked_model(tmp_path, "test/model", revision, digest) == manifest
    weights.write_bytes(b"tampered weight bytes")
    with pytest.raises(InvalidInputError, match="model file changed"):
        checked_model(tmp_path, "test/model", revision, digest)
    path.write_text("{}")
    with pytest.raises(InvalidInputError, match="manifest changed"):
        checked_model(tmp_path, "test/model", revision, digest)
