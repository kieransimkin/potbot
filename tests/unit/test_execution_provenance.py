"""Envelope/adversarial contracts; durable acceptance lives in the PostgreSQL gate."""

from dataclasses import replace
from uuid import uuid4

import pytest

from potbot.cogs.execution import EXECUTION_COGS, execution_ember_bundle
from potbot.cogs.registry import BoundCogRegistry
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.semantics.catalog import merge_types
from potbot.semantics.embers import EmberContractResolver
from potbot.tools.execution_contracts import (
    ExecutionPolicy,
    WorkBinding,
    fields,
    match_remote_target,
    remote_target,
)
from potbot.tools.semantic_contracts import QUERY, RESULTS


def policy(**overrides):
    return ExecutionPolicy(
        **{
            "policy_revision_hash": "a" * 64,
            "configuration_hash": "b" * 64,
            "source_digest": "c" * 64,
            "executor_version": "fixture-v1",
            **overrides,
        }
    )


def target():
    return {
        "schema_version": 1,
        "protocol": "swd",
        "target_ref": "fixture-device",
        "probe_ref": "fixture-probe",
        "core_ref": "core-0",
        "firmware_sha256": "a" * 64,
        "symbols_sha256": "b" * 64,
        "halt_revision": 1,
        "endpoint_protected": True,
        "effects": dict.fromkeys(("reset", "flash", "erase", "unlock"), False),
        "capabilities": dict.fromkeys(
            ("breakpoints", "step", "stack", "registers", "memory"), "unqualified"
        ),
        "qualification": "unqualified",
    }


def binding(**overrides):
    return WorkBinding(
        **{
            "kind": "script",
            "operation_ref": "syn://thing/fixture-script",
            "input_type_ref": QUERY,
            "output_type_ref": RESULTS,
            "implementation_hash": "d" * 64,
            "effect_class": "read_only",
            "policy": policy(),
            **overrides,
        }
    )


def test_profiles_are_separate_and_existing_identities_survive():
    registry = BoundCogRegistry.provenance()
    assert len(registry.installed()) == 23
    assert len(EXECUTION_COGS) == 19
    assert len({str(s.cog_ref) for s in EXECUTION_COGS}) == 19
    for cog in EXECUTION_COGS:
        assert cog.requires_controller_scope
        assert cog.handler_ref == "potbot.cogs.execution.execute"
    assert (
        BoundCogRegistry.tool_contracts().snapshot.fingerprint
        == "9fbdb8e42201caf9153f1d702602833a43efd221a48a41322d5e9d37ae8ee0e6"
    )
    assert (
        BoundCogRegistry.tool_inventory().snapshot.fingerprint
        == "0acd227fddd48b1ed72bcce5abaf380a92a2cff8cd40656ab515cbc4b13b750f"
    )
    assert (
        BoundCogRegistry.debugger().snapshot.fingerprint
        == "f6817f193e79d6ccfa51a78a7444815f20ac0fff0f2a9c5f970f888001d3e7f1"
    )
    with pytest.raises(ScopeDeniedError):
        replace(registry, contracts_profile=True)
    bundle = execution_ember_bundle(workspace_id=uuid4(), snapshot_id=uuid4(), namespace="fixture")
    assert (
        len(
            EmberContractResolver(
                merge_types(registry.snapshot), bundle, expected_scope=bundle.scope
            ).validate_all()
        )
        == 19
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"policy_revision_hash": "A" * 64},
        {"source_digest": "bad"},
        {"configuration_hash": ""},
        {"approval_receipt_hash": "bad"},
        {"executor_version": ""},
        {"max_wall_ms": 0},
        {"max_wall_ms": True},
        {"max_output_bytes": 16385},
        {"budget_json": '{"tokens":5}'},
        {"budget_json": '{"tokens":{"limit":-1,"unit":"tokens"}}'},
        {"budget_json": '{"tokens": {"limit":1,"unit":"tokens"}}'},
        {"host_clock_uncertainty_ms": -1},
        {"host_clock_offset_ms": 86400001},
    ],
)
def test_policy_rejects_ambiguous_or_unbounded_values(changes):
    with pytest.raises(InvalidInputError):
        policy(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "unknown"},
        {"operation_ref": "syn://cog/unsafe"},
        {"input_type_ref": "python"},
        {"effect_class": "automatic"},
        {"links": [uuid4()]},
        {"links": tuple(uuid4() for _ in range(17))},
        {"trace_id": "0" * 32},
        {"span_id": "A" * 16},
        {"parent_event_id": "not-uuid"},
    ],
)
def test_binding_rejects_unregistered_identity(changes):
    with pytest.raises(InvalidInputError):
        binding(**changes)


def test_configuration_and_policy_changes_invalidate_the_scope_digest():
    first = binding()
    assert first.policy.fingerprint == content_hash(first.policy.to_dict())
    assert (
        binding(policy=policy(configuration_hash="e" * 64)).policy.fingerprint
        != first.policy.fingerprint
    )
    assert (
        binding(policy=policy(policy_revision_hash="f" * 64)).policy.fingerprint
        != first.policy.fingerprint
    )


def test_budget_document_and_trace_context_are_bounded():
    large = canonical_json({f"budget-{i}": {"limit": 10**1500, "unit": "n"} for i in range(3)})
    with pytest.raises(InvalidInputError):
        policy(budget_json=large)
    for changes in ({"trace_id": "a" * 32}, {"span_id": "b" * 16}):
        with pytest.raises(InvalidInputError):
            binding(**changes)
    assert binding(trace_id="a" * 32, span_id="b" * 16).to_dict()["trace_id"] == "a" * 32


@pytest.mark.parametrize(
    "field,value",
    [
        ("protocol", []),
        ("protocol", "automatic"),
        ("endpoint_protected", False),
        ("halt_revision", True),
        ("firmware_sha256", "bad"),
        ("symbols_sha256", "bad"),
        ("probe_ref", ""),
        ("core_ref", ""),
        ("qualification", []),
        (
            "capabilities",
            dict.fromkeys(("breakpoints", "step", "stack", "registers", "memory"), []),
        ),
    ],
)
def test_remote_declarations_fail_closed(field, value):
    record = target()
    record[field] = value
    with pytest.raises(InvalidInputError):
        remote_target(record)


@pytest.mark.parametrize("effect", ["reset", "flash", "erase", "unlock"])
def test_remote_attach_never_implies_programming_effects(effect):
    record = target()
    record["effects"][effect] = True
    with pytest.raises(InvalidInputError):
        remote_target(record)


@pytest.mark.parametrize(
    "key,value",
    [
        ("probe_ref", "other"),
        ("core_ref", "core-1"),
        ("firmware_sha256", "c" * 64),
        ("symbols_sha256", "d" * 64),
        ("halt_revision", 2),
    ],
)
def test_remote_identity_and_revision_must_match(key, value):
    record = target()
    assert remote_target(record) == record
    match_remote_target(record, target())
    record[key] = value
    with pytest.raises(InvalidInputError):
        match_remote_target(record, target())


def test_inline_values_need_exact_version_and_bounded_size():
    for value in (
        {"schema_version": True},
        {"schema_version": 2},
        {"schema_version": 1, "extra": 2},
        {"schema_version": 1, "value": "x" * 16384},
    ):
        with pytest.raises(InvalidInputError):
            fields(value, {"schema_version"}, {"value"})
