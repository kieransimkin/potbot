"""The research source path is a declared Cog, not an inferred conversion."""

from dataclasses import replace
from uuid import uuid4

import pytest

from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.research.cog import (
    RESEARCH_ACTION,
    RESEARCH_BATCH,
    RESEARCH_COG_SNAPSHOT_DIGEST,
    ResearchControllerScope,
    transform_repository_research,
)
from tests.unit.test_phase7_research import action, inspector


def test_repository_inspection_has_typed_input_output_and_provenance(tmp_path):
    selected = action()
    _root, reader, _snapshot = inspector(tmp_path, selected.scope)
    scope = ResearchControllerScope(
        selected.scope.workspace_id,
        selected.scope.task_id,
        selected.scope.need_revision_id,
        selected.scope.repository_state_id,
        uuid4(),
    )
    context = CogRunContext(
        RESEARCH_COG_SNAPSHOT_DIGEST,
        selected.scope.contract_revision_id,
        selected.scope.repository_state_id,
        uuid4(),
    )
    input_value = TypedCogValue(RESEARCH_ACTION, selected.to_dict())
    result = transform_repository_research(input_value, context, scope, reader)
    assert result.value.type_ref == RESEARCH_BATCH
    assert result.value.value["observations"][0]["status"] == "inspected"
    assert result.receipt.input_hash == input_value.fingerprint
    assert result.receipt.output_hash == result.value.fingerprint
    assert result.receipt.scope_hash == scope.fingerprint
    assert result.receipt.binding_hash


def test_repository_cog_cannot_use_wrong_authority_or_type(tmp_path):
    selected = action()
    _root, reader, _snapshot = inspector(tmp_path, selected.scope)
    scope = ResearchControllerScope(
        selected.scope.workspace_id,
        selected.scope.task_id,
        selected.scope.need_revision_id,
        selected.scope.repository_state_id,
        uuid4(),
    )
    context = CogRunContext(
        RESEARCH_COG_SNAPSHOT_DIGEST,
        selected.scope.contract_revision_id,
        selected.scope.repository_state_id,
        uuid4(),
    )
    inp = TypedCogValue(RESEARCH_ACTION, selected.to_dict())
    with pytest.raises(ScopeDeniedError):
        transform_repository_research(
            inp, replace(context, cog_snapshot_hash="f" * 64), scope, reader
        )
    with pytest.raises(StateMismatchError):
        transform_repository_research(inp, context, replace(scope, task_id=uuid4()), reader)
    with pytest.raises(StateMismatchError):
        transform_repository_research(
            inp, context, replace(scope, repository_state_id=uuid4()), reader
        )
    with pytest.raises(StateMismatchError):
        transform_repository_research(
            inp, context, replace(scope, need_revision_id=uuid4()), reader
        )
