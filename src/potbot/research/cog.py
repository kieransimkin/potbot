"""Installed Phase 7 Cog: a typed research action -> observed source batch.

This is an additive preview registry. Its public type definitions must be
imported and pinned into a SYN release before production admission. Binding
hashes attest an installed handler identity, not audited code execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from potbot.cogs.contracts import (
    CogOutcome,
    CogReceipt,
    CogRunContext,
    CogSignature,
    TypedCogValue,
    check_cog_input,
    check_cog_output,
)
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.serialization import content_hash
from potbot.domain.syn import SynRef
from potbot.research.contracts import ResearchAction
from potbot.research.repository import SnapshotResearchAdapter

RESEARCH_ACTION = "synny://type/potbot-research-action"
RESEARCH_BATCH = "synny://type/potbot-research-observation-batch"
REPOSITORY_COG = "syn://cog/potbot-research-repository-inspection"
SIGNATURE = CogSignature(
    cog_ref=SynRef(REPOSITORY_COG),
    cog_type_ref="synny://type/potbot-research-repository-inspection-cog",
    input_type_ref=RESEARCH_ACTION,
    output_type_ref=RESEARCH_BATCH,
    handler_ref="potbot.cogs.phase7_repository_inspection",
    implementation_revision=1,
    requires_controller_scope=True,
)
# Digest of installed signatures/type identities. This is not a deployed SYN
# release. The authoritative controller must verify the activated release too.
RESEARCH_COG_SNAPSHOT_DIGEST = content_hash(
    {"phase7_cog_registry": 1, "installed_signatures": [SIGNATURE.to_dict()]}
)


@dataclass(frozen=True, slots=True)
class ResearchControllerScope:
    workspace_id: UUID
    task_id: UUID
    need_revision_id: UUID
    repository_state_id: UUID
    source_snapshot_artifact_id: UUID

    def __post_init__(self) -> None:
        for field in (
            "workspace_id",
            "task_id",
            "need_revision_id",
            "repository_state_id",
            "source_snapshot_artifact_id",
        ):
            if not isinstance(getattr(self, field), UUID):
                raise InvalidInputError("research Cog controller scope requires UUID identities")

    @property
    def fingerprint(self) -> str:
        return content_hash({name: str(getattr(self, name)) for name in self.__dataclass_fields__})


def transform_repository_research(
    source: TypedCogValue,
    context: CogRunContext,
    scope: ResearchControllerScope,
    reader: SnapshotResearchAdapter,
) -> CogOutcome:
    """Inspect an immutable repository snapshot with an explicit Cog receipt.

    Controller must supply the scoped reader and trusted artifact reference.
    Never invoke using untrusted model-supplied scope or an unactivated Cog.
    """
    if not isinstance(source, TypedCogValue) or not isinstance(context, CogRunContext):
        raise InvalidInputError("research Cog requires typed data and context")
    if not isinstance(scope, ResearchControllerScope) or not isinstance(
        reader, SnapshotResearchAdapter
    ):
        raise ScopeDeniedError("research Cog requires a trusted source scope")
    if context.cog_snapshot_hash != RESEARCH_COG_SNAPSHOT_DIGEST:
        raise ScopeDeniedError("research Cog must use the exact installed signature snapshot")
    check_cog_input(SIGNATURE, source)
    action = ResearchAction.from_dict(source.value)
    if (
        action.scope.workspace_id != scope.workspace_id
        or action.scope.task_id != scope.task_id
        or action.scope.need_revision_id != scope.need_revision_id
        or action.scope.repository_state_id != scope.repository_state_id
        or context.task_revision_id != action.scope.contract_revision_id
        or context.repository_state_id != scope.repository_state_id
        or reader.revision != reader.snapshot.content_digest
    ):
        raise StateMismatchError("research Cog input does not match controller-pinned state")
    results = reader.inspect(action)
    output = TypedCogValue(
        RESEARCH_BATCH,
        {
            "schema_version": 1,
            "action_id": str(action.action_id),
            "source_revision": reader.revision,
            "observations": [result.to_dict() for result in results],
        },
    )
    check_cog_output(SIGNATURE, output)
    receipt = CogReceipt(
        cog_ref=REPOSITORY_COG,
        cog_type_ref=SIGNATURE.cog_type_ref,
        binding_hash=SIGNATURE.binding_hash,
        cog_snapshot_hash=context.cog_snapshot_hash,
        task_revision_id=context.task_revision_id,
        repository_state_id=context.repository_state_id,
        invocation_id=context.invocation_id,
        input_type_ref=source.type_ref,
        input_hash=source.fingerprint,
        output_type_ref=output.type_ref,
        output_hash=output.fingerprint,
        input_artifact_id=source.source_artifact_id,
        upstream_receipt_hash=context.upstream_receipt_hash,
        scope_hash=scope.fingerprint,
    )
    return CogOutcome(output, receipt)
