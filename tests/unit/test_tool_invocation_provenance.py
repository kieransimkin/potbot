"""T01.3 — installation, immutable intent, and outcome hash links."""

from __future__ import annotations

from uuid import UUID

import pytest

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import canonical_json
from potbot.tools.capability_contracts import (
    ToolCapabilityDescriptor,
    ToolImplementationDescriptor,
    ToolInstallationDescriptor,
    ToolInvocationIntent,
    ToolObservation,
    verify_observation_link,
)

DIGEST = "b" * 64
POLICY = "c" * 64
WORKSPACE = UUID("00000000-0000-0000-0000-0000000000a1")
TASK = UUID("00000000-0000-0000-0000-0000000000a2")
INVOKE = UUID("00000000-0000-0000-0000-0000000000a3")


def installation() -> ToolInstallationDescriptor:
    return ToolInstallationDescriptor(
        installation_ref="syn://thing/installed-tool-fixture",
        workspace_id=WORKSPACE,
        implementation_hash=DIGEST,
        configuration_hash=DIGEST,
        policy_revision_hash=POLICY,
        permitted_scopes=("repo.read",),
    )


def intent(**changes: object) -> ToolInvocationIntent:
    fields = {
        "invocation_id": INVOKE,
        "workspace_id": WORKSPACE,
        "task_revision_id": TASK,
        "installation_hash": installation().fingerprint,
        "input_type_ref": "synny://type/potbot-data-record",
        "input_hash": DIGEST,
        "effect_class": "read_only",
        "policy_revision_hash": POLICY,
        "max_wall_time_ms": 30000,
        "max_output_bytes": 4096,
        "idempotency_key": "fixture-read-1",
    }
    fields.update(changes)
    return ToolInvocationIntent(**fields)


def test_installation_has_no_credentials_or_execution_grant() -> None:
    record = installation()
    assert ToolInstallationDescriptor.from_dict(record.to_dict()) == record
    assert "secret" not in record.to_dict()
    assert "approved" not in record.to_dict()
    with pytest.raises(InvalidInputError):
        ToolInstallationDescriptor.from_dict({**record.to_dict(), "secret": "bad"})


def test_invocation_roundtrip_and_stable_hash() -> None:
    first = intent()
    second = ToolInvocationIntent.from_dict(first.to_dict())
    assert second == first
    assert first.fingerprint == second.fingerprint
    first.assert_installation(installation())
    assert first.fingerprint != intent(input_hash="d" * 64).fingerprint
    assert first.fingerprint != intent(policy_revision_hash="e" * 64).fingerprint
    assert first.fingerprint != intent(max_wall_time_ms=1000).fingerprint


def test_wrong_workspace_or_policy_does_not_match_installation() -> None:
    with pytest.raises(ScopeDeniedError):
        intent(workspace_id=UUID("00000000-0000-0000-0000-0000000000a4")).assert_installation(
            installation()
        )
    with pytest.raises(ScopeDeniedError):
        intent(policy_revision_hash=DIGEST).assert_installation(installation())
    with pytest.raises(InvalidInputError):
        intent(max_output_bytes=0)
    with pytest.raises(InvalidInputError):
        intent(max_wall_time_ms=True)
    with pytest.raises(InvalidInputError):
        intent(effect_class="read_or_write")


def test_observation_links_exact_immutable_invocation() -> None:
    action = intent()
    observed = ToolObservation(
        invocation_hash=action.fingerprint,
        output_type_ref="synny://type/potbot-data-record",
        status="succeeded",
        output_hash=DIGEST,
        artifact_sha256=None,
        error_code=None,
    )
    assert ToolObservation.from_dict(observed.to_dict()) == observed
    verify_observation_link(action, observed)
    assert observed.fingerprint != action.fingerprint
    with pytest.raises(ScopeDeniedError):
        verify_observation_link(intent(policy_revision_hash="e" * 64), observed)
    with pytest.raises(InvalidInputError):
        ToolObservation.from_dict({**observed.to_dict(), "verified": True})


def test_unknown_or_failed_outcomes_cannot_claim_completed_artifacts() -> None:
    with pytest.raises(InvalidInputError):
        ToolObservation(
            invocation_hash=DIGEST,
            output_type_ref="synny://type/potbot-data-record",
            status="failed",
            output_hash=DIGEST,
            artifact_sha256=None,
            error_code="timeout",
        )
    with pytest.raises(InvalidInputError):
        ToolObservation(
            invocation_hash=DIGEST,
            output_type_ref="synny://type/potbot-data-record",
            status="indeterminate",
            output_hash=None,
            artifact_sha256=None,
            error_code=None,
        )
    indeterminate = ToolObservation(
        invocation_hash=DIGEST,
        output_type_ref="synny://type/potbot-data-record",
        status="indeterminate",
        output_hash=None,
        artifact_sha256=None,
        error_code="worker_lost_after_dispatch",
    )
    assert indeterminate.status == "indeterminate"


def test_complete_descriptor_implementation_installation_intent_result_chain() -> None:
    schema = canonical_json({"type": "object"})
    descriptor = ToolCapabilityDescriptor(
        name="fixture.read",
        definition_type_ref="synny://type/tool-definition",
        input_type_ref="synny://type/potbot-data-record",
        output_type_ref="synny://type/potbot-data-record",
        protocol="rest",
        effect_class="read_only",
        input_schema_json=schema,
        output_schema_json=schema,
        description="Read-only fixture",
    )
    implementation = ToolImplementationDescriptor(
        implementation_ref="syn://thing/fixture-read-v1",
        capability_hash=descriptor.fingerprint,
        package_digest=DIGEST,
        publisher="Fixture",
        version="1",
    )
    installed = ToolInstallationDescriptor(
        installation_ref="syn://thing/fixture-installation",
        workspace_id=WORKSPACE,
        implementation_hash=implementation.fingerprint,
        configuration_hash=DIGEST,
        policy_revision_hash=POLICY,
        permitted_scopes=("repo.read",),
    )
    action = intent(installation_hash=installed.fingerprint)
    output = ToolObservation(
        invocation_hash=action.fingerprint,
        output_type_ref=descriptor.output_type_ref,
        status="succeeded",
        output_hash=DIGEST,
        artifact_sha256=None,
        error_code=None,
    )
    implementation.assert_capability(descriptor)
    installed.assert_implementation(implementation)
    action.assert_installation(installed)
    action.assert_capability(descriptor)
    verify_observation_link(action, output, descriptor)
    with pytest.raises(ScopeDeniedError):
        action.assert_capability(
            ToolCapabilityDescriptor.from_dict(
                {**descriptor.to_dict(), "effect_class": "destructive"}
            )
        )
    with pytest.raises(ScopeDeniedError):
        installed.assert_implementation(
            ToolImplementationDescriptor.from_dict(
                {**implementation.to_dict(), "package_digest": "d" * 64}
            )
        )
    with pytest.raises(ScopeDeniedError):
        verify_observation_link(
            action,
            ToolObservation.from_dict({**output.to_dict(), "output_type_ref": "syn://type/cog"}),
            descriptor,
        )
