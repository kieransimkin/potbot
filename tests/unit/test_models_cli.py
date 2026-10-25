"""Offline CLI remains usable before database, weights or secrets are configured."""

import json
from uuid import uuid4

import pytest

from potbot.cli import main
from potbot.domain.model_proposals import ModelCodingResponse
from potbot.domain.serialization import canonical_json
from potbot.models.registry import DEFAULT_MODELS


def test_catalog_preserves_unqualified_roles_without_database(monkeypatch, capsys):
    monkeypatch.delenv("POTBOT_DSN", raising=False)
    monkeypatch.delenv("POTBOT_ADMIN_DSN", raising=False)
    assert main(["models", "catalog"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result["profiles"]) == len(DEFAULT_MODELS)
    assert result["live_inference_available"] is False
    assert result["qualified_deployments"] == 0
    assert all(profile["qualified"] is False for profile in result["profiles"])


@pytest.mark.parametrize("schema", ("action-proposal-v1", "coding-result-v1"))
def test_response_schemas_are_usable_json_schemas(schema, capsys):
    import jsonschema

    assert main(["models", "schema", "--schema", schema]) == 0
    result = json.loads(capsys.readouterr().out)
    jsonschema.Draft202012Validator.check_schema(result)
    assert result["additionalProperties"] is False


def test_validated_saved_response_is_explicitly_not_executable(tmp_path, capsys):
    response = ModelCodingResponse(
        uuid4(),
        uuid4(),
        uuid4(),
        inability_code="insufficient_context",
        inability_reason="The required source file was not supplied.",
    )
    path = tmp_path / "saved-response.json"
    path.write_text(canonical_json(response.to_dict()))
    assert (
        main(["models", "validate-response", "--schema", "coding-result-v1", "--input", str(path)])
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["structurally_valid"] is True
    assert result["executable"] is False
    assert result["semantic_admission"] == "required"


@pytest.mark.parametrize(
    "raw", (b"{} trailing", b"```json\n{}\n```", b'{"duplicate":1,"duplicate":2}', b"\xff")
)
def test_invalid_saved_response_is_classified_without_echoing_content(raw, tmp_path, capsys):
    path = tmp_path / "bad-response.json"
    path.write_bytes(raw)
    assert (
        main(["models", "validate-response", "--schema", "coding-result-v1", "--input", str(path)])
        == 1
    )
    result = capsys.readouterr()
    assert result.out == "" and json.loads(result.err)["error"]["code"] == "schema_violation"


def test_preflight_rejects_raw_duplicate_fields_without_echoing_secret(tmp_path, capsys):
    path = tmp_path / "local-host.json"
    path.write_text('{"schema_version":1,"schema_version":1,"secret":"DO_NOT_ECHO_CANARY"}')
    assert (
        main(["models", "preflight", "--profile", str(path), "--project-root", str(tmp_path)]) == 2
    )
    output = capsys.readouterr()
    assert "DO_NOT_ECHO_CANARY" not in output.out + output.err
    result = json.loads(output.out)
    assert result["status"] == "blocked" and result["qualified"] is False


def test_missing_profile_is_a_classified_input_failure(tmp_path, capsys):
    assert main(["models", "preflight", "--profile", str(tmp_path / "missing.json")]) == 1
    result = capsys.readouterr()
    assert json.loads(result.err)["error"]["code"] == "invalid_input"
