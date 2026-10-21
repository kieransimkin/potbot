"""Exercise the public SYN CLI across a restricted PostgreSQL application login."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from potbot.cli import main
from potbot.domain.syn import SynObject, SynRef, SynSnapshot, SynType
from tests.integration.test_named_embeddings import setup_service

pytestmark = pytest.mark.postgres


def test_cli_import_resolve_inventory_named_index_and_search(
    app_env, tmp_path, monkeypatch, capsys
) -> None:
    from potbot.memory import local_encoder

    embeddings, provider, episode = setup_service(app_env)
    named = embeddings.register_space(app_env.context, embedding_name="syn_public_text")
    type_ref = SynRef("syn://type/test-person")
    ref = SynRef("syn://thing/alice")
    payload = '{"id":"syn://thing/alice","typeRef":"syn://type/test-person","name":"Alice","amount":1.2300}'
    snapshot = SynSnapshot(
        "CLI public fixture",
        "complete",
        (SynType(type_ref, "Person", (), None, ()),),
        (SynObject(ref, type_ref, payload),),
    )
    path = tmp_path / "syn.json"
    path.write_text(snapshot.to_json(), encoding="utf-8")
    monkeypatch.setenv("POTBOT_DSN", app_env.db.dsn)

    def unexpected_model(*args, **kwargs):
        raise AssertionError("Import and exact lookups must not instantiate a model")

    monkeypatch.setattr(local_encoder, "QwenLocalEncoder", unexpected_model)
    common = [
        "--workspace-id",
        str(app_env.context.workspace_id),
        "--principal-id",
        str(app_env.context.principal_id),
        "--schema",
        app_env.schema,
        "--artifact-root",
        str(app_env.artifact_root),
    ]
    assert (
        main(
            [
                "syn",
                "import",
                *common,
                "--namespace",
                "social-graph",
                "--repository-id",
                str(app_env.repository_id),
                "--file",
                str(path),
            ]
        )
        == 0
    )
    imported = json.loads(capsys.readouterr().out)
    selected = [*common, "--snapshot-id", imported["scope"]["snapshot_id"]]
    assert imported["object_count"] == 1
    assert main(["syn", "catalog", *selected]) == 0
    catalog = json.loads(capsys.readouterr().out)
    assert catalog["selected_count"] == 48
    assert str(type_ref) in {row["ref"] for row in catalog["types"]}
    assert "syn://type/person" not in {row["ref"] for row in catalog["types"]}
    assert main(["syn", "catalog", *selected, "--type-ref", str(type_ref)]) == 0
    assert json.loads(capsys.readouterr().out)["types"] == [snapshot.types[0].to_dict()]
    assert main(["syn", "resolve", *selected, "--ref", str(ref)]) == 0
    resolved = json.loads(capsys.readouterr().out)
    assert resolved["payload_json"] == payload
    assert resolved["named_embeddings"] == {}
    schema = json.loads(
        (Path(__file__).parents[2] / "vocab/syn-resolved-record.schema.json").read_text()
    )
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    validator.validate(resolved)
    assert main(["syn", "list", *selected, "--type-ref", str(type_ref), "--page-size", "1"]) == 0
    inventory = json.loads(capsys.readouterr().out)
    assert inventory["complete"] and inventory["total_count"] == 1
    assert inventory["items"][0]["primary_data_type"] == "dt:syn.object_record"
    assert main(["syn", "resolve", *selected, "--ref", str(type_ref)]) == 0
    type_definition = json.loads(capsys.readouterr().out)
    validator.validate(type_definition)
    assert type_definition["semantic_type_ref"] is None
    alternate = str(type_ref).replace("syn://", "synny://", 1)
    assert main(["syn", "resolve", *selected, "--ref", alternate]) == 0
    alias_definition = json.loads(capsys.readouterr().out)
    validator.validate(alias_definition)
    assert alias_definition["requested_ref"] == alternate
    assert alias_definition["ref"] == str(type_ref)
    assert alias_definition["source_id"] == type_definition["source_id"]
    assert alias_definition["artifact_id"] == type_definition["artifact_id"]

    monkeypatch.setattr(local_encoder, "QwenLocalEncoder", lambda *args: provider)
    vector_args = [
        "--episode-id",
        str(episode),
        "--space-id",
        str(named["id"]),
        "--embedding-name",
        "syn_public_text",
    ]
    assert main(["syn", "index", *selected, *vector_args, "--ref", str(ref)]) == 0
    vector = json.loads(capsys.readouterr().out)["embedding"]
    assert vector["embedding_name"] == "syn_public_text"
    assert (
        main(
            [
                "syn",
                "search",
                *selected,
                *vector_args,
                "--type-ref",
                str(type_ref),
                "--query",
                "find Alice",
            ]
        )
        == 0
    )
    search = json.loads(capsys.readouterr().out)
    assert search["candidate_count"] == 1 and not search["exhaustive"]
    assert search["items"][0]["ref"] == str(ref)
    assert search["items"][0]["embedding"]["vector_id"] == vector["id"]
    assert main(["syn", "resolve", *selected, "--ref", str(ref)]) == 0
    resolved = json.loads(capsys.readouterr().out)
    validator.validate(resolved)
    assert resolved["named_embeddings"]["syn_public_text"][0]["vector_id"] == vector["id"]

    assert main(["syn", "resolve", *selected, "--ref", "syn://thing/missing"]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "scope_denied"
