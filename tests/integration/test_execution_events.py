"""Migration 19 SYN event source isolation and cursor replay with real Postgres."""

from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from potbot.controller.execution_events import ExecutionEventService
from potbot.controller.syn_service import preview_execution_events
from potbot.domain.errors import ScopeDeniedError
from potbot.domain.execution_events import ExecutionEventInput
from potbot.domain.serialization import canonical_json
from potbot.domain.syn_credentials import SynCredentialScope
from potbot.domain.syn_exchange import SynExportPolicy

pytestmark = pytest.mark.postgres


def event(*, run_id=None, trigger_kind="api_request", parent_event_id=None, parent_run_id=None):
    return ExecutionEventInput(
        event_type_ref="synny://type/workflow-execution",
        event_type_version="1.0",
        subject_type_ref="synny://type/workflow-execution",
        subject_version="1.0",
        run_id=run_id or uuid4(),
        trigger_kind=trigger_kind,
        trigger_ref="synthetic:test-request",
        trigger_input_sha256="a" * 64,
        emitter_run_id=uuid4(),
        stage="started" if parent_event_id is None else "updated",
        parent_event_id=parent_event_id,
        parent_run_id=parent_run_id,
    )


def test_typed_event_root_child_replay_and_cross_workspace_denial(app_env):
    first = ExecutionEventService(app_env.db)
    root = event()
    root_record = first.append(app_env.context, root)
    child = replace(
        event(
            run_id=root.run_id,
            trigger_kind="parent_event",
            parent_event_id=UUID(root_record["id"]),
            parent_run_id=root.run_id,
        ),
        trigger_ref=root_record["event_ref"],
    )
    child_record = first.append(app_env.context, child)
    page = first.events(app_env.context, after=0, run_id=root.run_id, limit=1)
    assert page["has_more"] is True
    assert page["items"][0]["id"] == root_record["id"]
    next_page = first.events(app_env.context, after=page["next_cursor"], run_id=root.run_id)
    assert [row["id"] for row in next_page["items"]] == [child_record["id"]]
    assert root_record["event_ref"] == "syn://event/" + root_record["id"]
    assert child_record["sequence"] > root_record["sequence"]
    assert ExecutionEventService(app_env.other_db).events(app_env.other_context)["items"] == []
    with pytest.raises(ScopeDeniedError):
        ExecutionEventService(app_env.other_db).append(
            app_env.other_context,
            replace(child, emitter_run_id=uuid4()),
        )
    with pytest.raises(ScopeDeniedError):
        first.append(app_env.context, replace(child, parent_run_id=uuid4()))


def test_metadata_export_from_committed_events_retains_cursors_and_private_source(app_env):
    source = ExecutionEventService(app_env.db)
    root = event()
    root_record = source.append(
        app_env.context,
        replace(root, trigger_ref="https://private.example/?token=synthetic-private-input"),
    )
    child = source.append(
        app_env.context,
        replace(
            event(
                run_id=root.run_id,
                trigger_kind="parent_event",
                parent_event_id=UUID(root_record["id"]),
                parent_run_id=root.run_id,
            ),
            trigger_ref=root_record["event_ref"],
            input_artifact_ref="private-input-reference",
        ),
    )
    scope = SynCredentialScope.for_service(
        uuid4(), app_env.context.workspace_id, "https://syn.example"
    )
    policy = SynExportPolicy(uuid4(), scope, uuid4())
    first = preview_execution_events(source, app_env.context, policy=policy, scope=scope, limit=1)
    assert first["delivery"] == "not_sent" and first["has_more"]
    assert first["next_cursor"] == root_record["sequence"]
    assert first["items"][0]["payload"]["source"]["event_id"] == root_record["id"]
    second = preview_execution_events(
        source, app_env.context, policy=policy, scope=scope, after=first["next_cursor"]
    )
    assert second["next_cursor"] == child["sequence"]
    assert second["items"][0]["payload"]["execution"]["parent_event_id"] == root_record["id"]
    assert "private.example" not in canonical_json(first)
    assert "private-input-reference" not in canonical_json(second)
    assert source.events(app_env.context)["items"] == [root_record, child]
    other_scope = SynCredentialScope.for_service(
        scope.installation_id, app_env.other_context.workspace_id, "https://syn.example"
    )
    assert (
        preview_execution_events(
            ExecutionEventService(app_env.other_db),
            app_env.other_context,
            policy=SynExportPolicy(uuid4(), other_scope, uuid4()),
            scope=other_scope,
        )["items"]
        == []
    )
