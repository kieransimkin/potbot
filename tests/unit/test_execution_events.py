"""T01.3 causal execution event boundary, independent of PostgreSQL."""

from dataclasses import replace
from uuid import uuid4

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.domain.execution_events import ExecutionEventInput
from potbot.persistence.schema import application_migrations


def fixture():
    return ExecutionEventInput(
        event_type_ref="synny://type/workflow-execution",
        event_type_version="1.0.0",
        subject_type_ref="synny://type/workflow-execution",
        subject_version="1.0.0",
        run_id=uuid4(),
        trigger_kind="api_request",
        trigger_ref="request:synthetic-001",
        trigger_input_sha256="a" * 64,
        emitter_run_id=uuid4(),
        stage="started",
    )


def test_root_event_requires_verifiable_input_and_type():
    item = fixture()
    assert item.stage == "started"
    for change in (
        {"event_type_ref": "workflow-execution"},
        {"subject_type_ref": "syn://event/not-a-type"},
        {"trigger_ref": ""},
        {"trigger_input_sha256": "bad"},
        {"trigger_kind": "unknown"},
        {"stage": "made-up"},
        {"elapsed_ms": -1},
    ):
        with pytest.raises(InvalidInputError):
            replace(item, **change)


def test_causal_child_requires_parent_event_and_run():
    root = fixture()
    with pytest.raises(InvalidInputError):
        replace(root, trigger_kind="parent_event")
    with pytest.raises(InvalidInputError):
        replace(root, parent_event_id=uuid4())
    child = replace(
        root, trigger_kind="parent_event", parent_event_id=uuid4(),
        parent_run_id=root.run_id, run_id=uuid4(),
    )
    assert child.parent_run_id == root.run_id


def test_migration_19_keeps_previous_ledger():
    migrations = application_migrations()
    assert migrations[-1].version == 19
    assert migrations[-1].name == "execution_events_schema"
    assert migrations[-1].workspace_tables == ("syn_execution_events",)
    assert migrations[-1].append_only_tables == ("syn_execution_events",)
