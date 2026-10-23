"""Disclosure boundaries, strict wire contracts, preflight and stable retries."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator, ValidationError

from potbot.api.syn_service_cli import main
from potbot.controller.execution_events import ExecutionEventService
from potbot.controller.syn_service import (
    ScopedSynClient,
    preview_execution_events,
    sync_execution_events,
)
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.execution_events import EXECUTION_STAGES, EXECUTION_TRIGGER_KINDS
from potbot.domain.serialization import canonical_json
from potbot.domain.syn_credentials import SynCredentialScope
from potbot.domain.syn_exchange import (
    EXECUTION_SUMMARY_CHANNEL,
    EXECUTION_SUMMARY_CONTRACT,
    EXPORT_APPROVAL,
    SynExportPolicy,
    execution_summary_message_id,
    project_execution_summary,
    validate_execution_summary,
)


def policy_document():
    return {
        "schema_version": 1,
        "contract": EXECUTION_SUMMARY_CONTRACT,
        "approval": EXPORT_APPROVAL,
        "policy_id": str(uuid4()),
        "installation_id": str(uuid4()),
        "workspace_id": str(uuid4()),
        "recipient_origin": "https://syn.example",
        "recipient_namespace_id": str(uuid4()),
    }


def local_event():
    identifier = str(uuid4())
    return {
        "schema_version": 1,
        "sequence": 8,
        "id": identifier,
        "event_ref": "syn://event/" + identifier,
        "run_id": str(uuid4()),
        "emitter_run_id": str(uuid4()),
        "parent_run_id": None,
        "parent_event_id": None,
        "trigger_kind": "api_request",
        "stage": "started",
        "elapsed_ms": None,
        "trigger_ref": "https://private.example/?access_token=DO-NOT-SHARE",
        "input_artifact_ref": "private/path",
        "output_artifact_ref": "secret://private",
        "trigger_input_sha256": "a" * 64,
        "output_sha256": "b" * 64,
        "event_type_ref": "syn://type/private-project",
        "event_type_version": "PRIVATE",
        "subject_type_ref": "syn://type/private-project",
        "subject_version": "PRIVATE",
        "future_private_payload": {"privateData": "DO-NOT-SHARE"},
    }


def rig():
    policy = SynExportPolicy.from_dict(policy_document())
    context = WorkspaceContext(policy.scope.workspace_id, uuid4())
    source = Mock(spec=ExecutionEventService)
    source.events.return_value = {"items": [local_event()], "next_cursor": 8, "has_more": False}
    remote = Mock()
    remote.send.return_value = {"sequence": 2, "hash": "c" * 64}
    return policy, context, source, remote, ScopedSynClient(policy.scope, remote)


def test_projection_excludes_every_unclassified_source_field_and_does_not_mutate():
    policy = SynExportPolicy.from_dict(policy_document())
    event = local_event()
    before = deepcopy(event)
    result = project_execution_summary(event, policy)
    assert event == before
    assert result["assertion"] == "source_report"
    assert result["source"]["event_id"] == event["id"]
    wire = canonical_json(result)
    for name in (
        "trigger_ref",
        "input_artifact_ref",
        "output_artifact_ref",
        "trigger_input_sha256",
        "output_sha256",
        "event_type_ref",
        "event_type_version",
        "subject_version",
        "future_private_payload",
    ):
        assert name not in wire
        assert str(event[name]) not in wire
    assert "DO-NOT-SHARE" not in wire


def test_published_schema_accepts_projection_and_denies_nested_private_extensions():
    schema = json.loads(
        (Path(__file__).parents[2] / "vocab/syn-execution-summary.schema.json").read_text()
    )
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    payload = project_execution_summary(local_event(), SynExportPolicy.from_dict(policy_document()))
    validator.validate(payload)
    execution = schema["properties"]["execution"]["properties"]
    assert set(execution["stage"]["enum"]) == EXECUTION_STAGES
    assert set(execution["trigger_kind"]["enum"]) == EXECUTION_TRIGGER_KINDS
    for section in (payload, payload["execution"], payload["source"], payload["recipient"]):
        section["privateData"] = "must not cross"
        with pytest.raises(ValidationError):
            validator.validate(payload)
        del section["privateData"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("approval", ""),
        ("contract", "future"),
        ("extra", "not-approved"),
        ("policy_id", "not-uuid"),
        ("recipient_origin", "https://user:secret@syn.example"),
        ("recipient_origin", "http://remote.example"),
        ("recipient_namespace_id", None),
    ],
)
def test_policy_rejects_implicit_approval_unknown_versions_and_unsafe_scope(field, value):
    document = policy_document()
    document[field] = value
    with pytest.raises(InvalidInputError):
        SynExportPolicy.from_dict(document)


@pytest.mark.parametrize(
    "path,value",
    [
        (("schema_version",), True),
        (("schema_version",), 2),
        (("contract",), "future"),
        (("assertion",), "verified_fact"),
        (("source", "event_id"), str(uuid4()).upper()),
        (("source", "sequence"), True),
        (("source", "sequence"), -1),
        (("source", "sequence"), 2**53),
        (("source", "extra"), "private"),
        (("execution", "stage"), "DO-NOT-SHARE"),
        (("execution", "stage"), []),
        (("execution", "trigger_kind"), "invented"),
        (("execution", "elapsed_ms"), float("nan")),
        (("execution", "elapsed_ms"), True),
        (("execution", "elapsed_ms"), 2**53),
        (("execution", "parent_run_id"), str(uuid4())),
        (("execution", "trigger_kind"), "parent_event"),
        (("execution", "artifact_ref"), "secret://private"),
        (("recipient", "origin_sha256"), "https://private.example"),
    ],
)
def test_wire_rejects_extensions_malformed_metadata_and_authority_claims(path, value):
    result = project_execution_summary(local_event(), SynExportPolicy.from_dict(policy_document()))
    target = result
    for name in path[:-1]:
        target = target[name]
    target[path[-1]] = value
    with pytest.raises(InvalidInputError):
        validate_execution_summary(result)


def test_delivery_ids_are_stable_versioned_scoped_and_preserve_source_provenance():
    policy = SynExportPolicy.from_dict(policy_document())
    event = local_event()
    payload = project_execution_summary(event, policy)
    identity = execution_summary_message_id(payload)
    assert identity != UUID(event["id"])
    assert identity == execution_summary_message_id(deepcopy(payload))
    # Reapproval and private local edits do not alter already approved wire bytes.
    again = project_execution_summary(
        {**event, "trigger_ref": "changed-private"}, replace(policy, policy_id=uuid4())
    )
    assert again == payload
    assert execution_summary_message_id(again) == identity
    changed_stage = deepcopy(payload)
    changed_stage["execution"]["stage"] = "failed"
    # Changed content keeps its identity, so Syn detects conflict instead of hiding it.
    assert execution_summary_message_id(changed_stage) == identity
    for other in (
        replace(policy, recipient_namespace_id=uuid4()),
        replace(
            policy,
            scope=SynCredentialScope.for_service(
                uuid4(), policy.scope.workspace_id, "https://syn.example"
            ),
        ),
        replace(
            policy,
            scope=SynCredentialScope.for_service(
                policy.scope.installation_id, uuid4(), "https://syn.example"
            ),
        ),
        replace(
            policy,
            scope=SynCredentialScope.for_service(
                policy.scope.installation_id, policy.scope.workspace_id, "https://other.example"
            ),
        ),
    ):
        assert execution_summary_message_id(project_execution_summary(event, other)) != identity


def test_v1_published_retry_identity_vector():
    document = {
        "schema_version": 1,
        "contract": EXECUTION_SUMMARY_CONTRACT,
        "approval": EXPORT_APPROVAL,
        "policy_id": "11111111-1111-4111-8111-111111111111",
        "installation_id": "22222222-2222-4222-8222-222222222222",
        "workspace_id": "33333333-3333-4333-8333-333333333333",
        "recipient_origin": "https://syn.example",
        "recipient_namespace_id": "44444444-4444-4444-8444-444444444444",
    }
    event = local_event()
    event.update(
        id="55555555-5555-4555-8555-555555555555",
        event_ref="syn://event/55555555-5555-4555-8555-555555555555",
        run_id="66666666-6666-4666-8666-666666666666",
        emitter_run_id="77777777-7777-4777-8777-777777777777",
    )
    payload = project_execution_summary(event, SynExportPolicy.from_dict(document))
    assert str(execution_summary_message_id(payload)) == "609cdd2c-19ae-5961-b249-b5611ed5dc9d"


def test_page_preflight_sends_nothing_when_later_record_is_invalid():
    policy, context, source, remote, client = rig()
    source.events.return_value["items"].append({**local_event(), "stage": "private-value"})
    with pytest.raises(InvalidInputError):
        sync_execution_events(client, source, context, policy=policy)
    remote.send.assert_not_called()


@pytest.mark.parametrize("mismatch", ["workspace", "origin", "installation"])
def test_scope_denied_before_reading_events_or_contacting_syn(mismatch):
    policy, context, source, remote, client = rig()
    scope = policy.scope
    other = SynCredentialScope.for_service(
        uuid4() if mismatch == "installation" else scope.installation_id,
        uuid4() if mismatch == "workspace" else scope.workspace_id,
        "https://other.example" if mismatch == "origin" else "https://syn.example",
    )
    with pytest.raises(ScopeDeniedError):
        sync_execution_events(client, source, context, policy=replace(policy, scope=other))
    source.events.assert_not_called()
    remote.send.assert_not_called()


def test_preview_and_send_share_identical_projection_and_source_cursor():
    policy, context, source, remote, client = rig()
    preview = preview_execution_events(source, context, policy=policy, scope=policy.scope, after=3)
    assert preview["delivery"] == "not_sent"
    remote.send.assert_not_called()
    result = sync_execution_events(client, source, context, policy=policy, after=3)
    assert remote.send.call_args.kwargs == {
        key: value for key, value in preview["items"][0].items() if key != "source_sequence"
    }
    assert result["next_cursor"] == 8
    assert result["items"][0]["receipt"]["sequence"] == 2
    first_call = remote.send.call_args
    remote.send.side_effect = RuntimeError("lost acknowledgement")
    with pytest.raises(RuntimeError):
        sync_execution_events(client, source, context, policy=policy, after=3)
    assert remote.send.call_args == first_call


def test_scoped_transport_rejects_legacy_and_unsupported_execution_channels():
    policy, context, source, remote, client = rig()
    payload = project_execution_summary(source.events.return_value["items"][0], policy)
    for channel, message_id in (
        ("potbot.execution-events", str(uuid4())),
        ("potbot.execution-events.v2", str(uuid4())),
        (EXECUTION_SUMMARY_CHANNEL, payload["source"]["event_id"]),
    ):
        with pytest.raises(InvalidInputError):
            client.send(
                str(context.workspace_id),
                channel=channel,
                message_id=message_id,
                sender="potbot",
                payload=payload,
            )
    remote.send.assert_not_called()


def test_cli_preview_reads_no_syn_token_and_policy_failure_reads_no_credentials(
    tmp_path, monkeypatch, capsys
):
    policy, context, source, _remote, _client = rig()
    document = policy_document()
    document.update(
        installation_id=str(policy.scope.installation_id),
        workspace_id=str(context.workspace_id),
        recipient_namespace_id=str(policy.recipient_namespace_id),
    )
    path = tmp_path / "export-policy.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setenv("POTBOT_INSTALLATION_ID", document["installation_id"])
    monkeypatch.setenv("POTBOT_SYN_WORKSPACE_ID", document["workspace_id"])
    monkeypatch.setenv("POTBOT_SYN_URL", document["recipient_origin"])
    credential_read = Mock(side_effect=AssertionError("preview must not read Syn credentials"))
    monkeypatch.setattr("potbot.api.syn_service_cli.configured_client", credential_read)
    database_read = Mock(return_value=(object(), context))
    monkeypatch.setattr("potbot.api.jobs_cli.database_context", database_read)
    monkeypatch.setattr(
        "potbot.controller.execution_events.ExecutionEventService", lambda _: source
    )
    args = [
        "--workspace-id",
        document["workspace_id"],
        "sync-events",
        "--principal-id",
        str(context.principal_id),
        "--export-policy",
        str(path),
        "--preview",
    ]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["delivery"] == "not_sent"
    credential_read.assert_not_called()
    database_read.reset_mock()
    path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    assert main(args) == 1
    assert "Syn operation failed" in capsys.readouterr().err
    database_read.assert_not_called()
    credential_read.assert_not_called()
