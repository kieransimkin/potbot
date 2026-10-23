"""Canonical contracts, inherited Embers and monitored installed Cog execution."""

import asyncio
import json
from dataclasses import replace
from uuid import uuid4

import pytest

from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.cogs.registry import BoundCogRegistry
from potbot.cogs.tool_contracts import CONVERT, SEARCH, TOOL_COG, VALIDATE, contract_ember_bundle
from potbot.controller.cog_work import CogWorkMonitor
from potbot.controller.tool_contracts import ToolContractRuntime
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.domain.syn import SynRef
from potbot.semantics.catalog import merge_types
from potbot.semantics.embers import EmberContractResolver, EmberInstance, TypeEmberContract
from potbot.tools.capability_contracts import (
    ToolCapabilityDescriptor,
    ToolImplementationDescriptor,
    ToolInstallationDescriptor,
    ToolInvocationIntent,
    ToolObservation,
    verify_observation_link,
)
from potbot.tools.semantic_contracts import (
    CAPABILITY,
    EVIDENCE,
    IMPLEMENTATION,
    INSTALLATION,
    INVOCATION,
    QUERY,
    RESULTS,
    TOOL,
    ToolSemanticCatalog,
    check_executable_binding,
    check_target_receipt,
    require_compatible_schema,
    schemas,
)
from tests.unit.test_debugger_cogs_live import FixtureArtifacts, FixtureEvents, assert_lifecycles


def records(actor):
    capability = ToolCapabilityDescriptor(
        "contract.search",
        "synny://type/tool-definition",
        QUERY,
        RESULTS,
        "cog",
        "read_only",
        canonical_json(schemas()[QUERY]),
        canonical_json(schemas()[RESULTS]),
        "Search a host-owned pinned corpus",
    )
    implementation = ToolImplementationDescriptor(
        "syn://thing/search-implementation-v1", capability.fingerprint, "1" * 64, "potbot", "1"
    )
    installation = ToolInstallationDescriptor(
        "syn://thing/search-installation-v1",
        actor.workspace_id,
        implementation.fingerprint,
        "2" * 64,
        "3" * 64,
        ("contract.search",),
    )
    intent = ToolInvocationIntent(
        uuid4(),
        actor.workspace_id,
        uuid4(),
        installation.fingerprint,
        QUERY,
        "4" * 64,
        "read_only",
        installation.policy_revision_hash,
        1000,
        4096,
        "fixture",
    )
    observation = ToolObservation(
        intent.fingerprint, RESULTS, "succeeded", "5" * 64, "6" * 64, None
    )
    return capability, implementation, installation, intent, observation


def runtime(admit=lambda *_: None, events=None):
    registry = BoundCogRegistry.tool_contracts()
    events, artifacts = events or FixtureEvents(), FixtureArtifacts()
    host = ToolContractRuntime(
        CogWorkMonitor(
            events,
            artifacts,
            WorkspaceContext(uuid4(), uuid4()),
            emitter_run_id=uuid4(),
            event_type_ref="synny://type/workflow-execution",
        ),
        catalog=ToolSemanticCatalog(registry.snapshot),
        task_revision_id=uuid4(),
        repository_state_id=uuid4(),
        source_digest="7" * 64,
        corpus=({"id": "fixture", "text": "Python debugger stack", "source_sha256": "8" * 64},),
        admit=admit,
    )
    return registry, host, events, artifacts


def invoke(registry, host, signature, value, upstream=None):
    context = CogRunContext(
        registry.snapshot.fingerprint, host.task_revision, host.repository_state, uuid4(), upstream
    )
    return asyncio.run(
        registry.invoke(
            str(signature.cog_ref),
            TypedCogValue(signature.input_type_ref, value),
            context,
            controller=host,
        )
    )


def table():
    return {
        "schema_version": 1,
        "source_sha256": "9" * 64,
        "locale": "und",
        "timezone": "UTC",
        "mime": "application/json",
        "encoding": "utf-8",
        "unit": "ms",
        "target_unit": "s",
        "precision": 3,
        "tolerance": "0",
        "rows": [{"value": "1250", "source_start": 5, "source_end": 9}],
    }


def session():
    return {
        "schema_version": 1,
        "kind": "debugger",
        "session_ref": str(uuid4()),
        "target_ref": "fixture-python",
        "process_identity": "owned-process-start-123",
        "source_sha256": "a" * 64,
        "adapter": "python-debugpy",
        "adapter_version": "1.8.22",
        "capabilities": [
            {"operation": "stack", "state": "supported", "reason": "Python qualification"},
            {"operation": "icsp", "state": "unqualified", "reason": "No probe qualification"},
        ],
    }


def test_all_six_contracts_and_linked_invocation_round_trip():
    _, host, _, _ = runtime()
    values = records(host.monitor.actor)
    for ref, record in zip(
        (CAPABILITY, IMPLEMENTATION, INSTALLATION, INVOCATION, EVIDENCE), values, strict=True
    ):
        typed = TypedCogValue(ref, record.to_dict())
        encoded = host.catalog.encode(typed)
        assert host.catalog.encode(host.catalog.decode(encoded)) == encoded
    capability, implementation, installation, intent, observation = values
    typed = TypedCogValue(
        TOOL,
        {
            "schema_version": 1,
            "tool_ref": "syn://thing/search-tool",
            "capability_hash": capability.fingerprint,
            "classification": "discovery_descriptor",
        },
    )
    assert host.catalog.decode(host.catalog.encode(typed)) == typed
    check_executable_binding(host.catalog, SEARCH, capability, implementation, installation)
    intent.assert_installation(installation)
    verify_observation_link(intent, observation, capability)
    with pytest.raises(ScopeDeniedError):
        check_executable_binding(
            host.catalog,
            replace(SEARCH, implementation_revision=2),
            capability,
            implementation,
            installation,
        )
    with pytest.raises(ScopeDeniedError):
        verify_observation_link(intent, replace(observation, invocation_hash="0" * 64), capability)


def test_schema_inspection_cannot_mutate_installed_definitions_or_data_ancestry():
    registry, host, _, _ = runtime()
    fingerprint = registry.snapshot.fingerprint
    inspected = schemas()
    inspected[QUERY]["properties"]["schema_version"]["const"] = 99
    assert schemas()[QUERY]["properties"]["schema_version"]["const"] == 1
    assert BoundCogRegistry.tool_contracts().snapshot.fingerprint == fingerprint
    descriptor = records(host.monitor.actor)[0]
    with pytest.raises(InvalidInputError):
        host.catalog.validate(
            TypedCogValue(
                CAPABILITY, replace(descriptor, output_type_ref="syn://type/cog").to_dict()
            )
        )


def test_search_validation_and_conversion_cogs_have_own_lifecycles():
    registry, host, events, artifacts = runtime()
    source = {"schema_version": 1, "text": "Python", "limit": 8}
    first = invoke(registry, host, SEARCH, source)
    assert first.value.value["items"][0]["id"] == "fixture"
    assert host.catalog.decode(host.catalog.encode(first.value)) == first.value
    checked = invoke(
        registry,
        host,
        VALIDATE,
        {
            "schema_version": 1,
            "type_ref": RESULTS,
            "schema_hash": host.catalog.schema_hash(RESULTS),
            "value": first.value.value,
        },
        first.receipt.fingerprint,
    )
    assert host.catalog.decode(checked.value.value["canonical_json"]) == first.value
    converted = invoke(registry, host, CONVERT, table())
    assert converted.value.value["rows"] == [{"value": "1.250", "source_start": 5, "source_end": 9}]
    assert converted.value.value["loss"] == []
    assert_lifecycles(events.values, (first, checked, converted))
    assert len(artifacts.values) == 6


@pytest.mark.parametrize("ref", ["SearchQuery", "syn://type/unknown-contract", "syn://thing/query"])
def test_shorthand_unknown_and_non_type_are_rejected(ref):
    _, host, _, _ = runtime()
    with pytest.raises(InvalidInputError):
        host.catalog.decode(canonical_json({"type_ref": ref, "schema_hash": "0" * 64, "value": {}}))


def test_aliases_registered_ranges_and_duplicate_json():
    _, host, _, _ = runtime()
    value = TypedCogValue(
        QUERY.replace("syn://", "synny://"), {"schema_version": 1, "text": "Python", "limit": 2}
    )
    assert host.catalog.decode(host.catalog.encode(value)) == value
    descriptor = records(host.monitor.actor)[0]
    with pytest.raises(InvalidInputError):
        host.catalog.validate(
            TypedCogValue(
                CAPABILITY, replace(descriptor, output_type_ref="syn://type/absent").to_dict()
            )
        )
    from potbot.domain.errors import SchemaViolationError

    with pytest.raises(SchemaViolationError):
        host.catalog.decode('{"type_ref":"a","type_ref":"b"}')


def test_invalid_inheritance_and_ember_required_default_optional_contracts():
    registry, host, _, _ = runtime()
    bundle = contract_ember_bundle(
        workspace_id=host.monitor.actor.workspace_id, snapshot_id=uuid4(), namespace="fixture"
    )
    types = merge_types(registry.snapshot)
    resolved = EmberContractResolver(types, bundle, expected_scope=bundle.scope).validate_all()
    assert all(len(r.to_dict()["slots"]) == 5 for r in resolved)
    assert all(any(s["origin"] == "inherited" for s in r.to_dict()["slots"]) for r in resolved)
    stripped = replace(
        bundle, instances=(EmberInstance.create(str(SEARCH.cog_ref), SEARCH.cog_type_ref),)
    )
    with pytest.raises(InvalidInputError):
        EmberContractResolver(types, stripped, expected_scope=bundle.scope).validate_all()
    parent = next(c for c in bundle.type_contracts if c.type_ref == TOOL_COG)
    widened = replace(parent.slots[0], minimum=0)
    child = TypeEmberContract.create(SEARCH.cog_type_ref, (widened,))
    with pytest.raises(InvalidInputError):
        EmberContractResolver(
            types,
            replace(bundle, type_contracts=(*bundle.type_contracts, child)),
            expected_scope=bundle.scope,
        )
    altered = tuple(
        replace(t, parents=(SynRef("syn://type/missing-parent"),)) if str(t.ref) == QUERY else t
        for t in registry.snapshot.types
    )
    with pytest.raises(InvalidInputError):
        ToolSemanticCatalog(replace(registry.snapshot, types=altered))


@pytest.mark.parametrize("change", ["required", "range", "unit", "drop"])
def test_incompatible_schema_refresh_is_rejected_and_old_pin_retained(change):
    _, host, _, _ = runtime()
    before = schemas()[QUERY]
    after = json.loads(canonical_json(before))
    if change == "required":
        after["required"].append("new")
    elif change == "range":
        after["properties"]["limit"]["maximum"] = 16
    elif change == "unit":
        after["properties"]["text"]["format"] = "date"
    else:
        del after["properties"]["text"]
    pin = host.catalog.schema_hash(QUERY)
    with pytest.raises(InvalidInputError):
        require_compatible_schema(before, after)
    assert host.catalog.schema_hash(QUERY) == pin
    refreshed = {**before, "properties": {**before["properties"], "comment": {"type": "string"}}}
    require_compatible_schema(before, refreshed)
    assert host.catalog.schema_hash(QUERY) == pin != content_hash(refreshed)


@pytest.mark.parametrize("change", ["unit", "locale", "date", "precision", "span"])
def test_ambiguous_or_lossy_table_fails_without_success(change):
    registry, host, events, _ = runtime()
    value = table()
    if change == "unit":
        value["unit"] = "seconds?"
    elif change == "locale":
        value["locale"] = "en-GB"
    elif change == "date":
        value["rows"][0]["value"] = "01/02/03"
    elif change == "precision":
        value["precision"] = 0
    else:
        value["rows"][0]["source_end"] = 4
    with pytest.raises(InvalidInputError):
        invoke(registry, host, CONVERT, value)
    assert not any(e["stage"] == "completed" for e in events.values)


def test_declared_loss_and_source_identity_are_retained():
    registry, host, _, _ = runtime()
    value = table()
    value.update(precision=1, tolerance="0.05")
    result = invoke(registry, host, CONVERT, value).value.value
    assert result["loss"] == [{"row": 0, "absolute": "0.050"}]
    assert result["source_sha256"] == value["source_sha256"]
    for kind in ("device", "profiler", "debugger"):
        target = {**session(), "kind": kind}
        for receipt_kind in ("capture", "stop", "stack", "value"):
            receipt = {
                "schema_version": 1,
                "session_hash": content_hash(target),
                **{
                    k: target[k]
                    for k in ("session_ref", "target_ref", "process_identity", "source_sha256")
                },
                "kind": receipt_kind,
                "revision": 1,
                "artifact_sha256": "b" * 64,
                "verification": "reported",
            }
            check_target_receipt(host.catalog, target, receipt)
            with pytest.raises(ScopeDeniedError):
                check_target_receipt(
                    host.catalog, target, {**receipt, "process_identity": "another-process"}
                )


@pytest.mark.parametrize(
    "case",
    ["denied", "revoked", "mutated", "wrong-type", "direct", "server-request", "monitor-failed"],
)
def test_admission_and_raw_dispatch_reject_without_search(case):
    calls = []

    def admit(_sig, value, _ctx):
        calls.append(1)
        if case == "denied" or case == "revoked" and len(calls) == 2:
            raise ScopeDeniedError("revoked")
        if case == "mutated":
            value.value["text"] = "changed"

    class Broken(FixtureEvents):
        def append(self, actor, event):
            raise OSError("unavailable monitor")

    registry, host, events, _ = runtime(admit, Broken() if case == "monitor-failed" else None)
    source = TypedCogValue(
        RESULTS if case == "wrong-type" else QUERY,
        {"schema_version": 1, "text": "Python", "limit": 2},
    )
    host._corpus = "invalid JSON would fail if dispatched"
    context = CogRunContext(
        registry.snapshot.fingerprint, host.task_revision, host.repository_state, uuid4()
    )
    with pytest.raises((ScopeDeniedError, InvalidInputError, StateMismatchError, OSError)):
        if case == "direct":
            asyncio.run(host.dispatch(SEARCH, source))
        else:
            asyncio.run(
                registry.invoke(
                    "syn://cog/server-sampling"
                    if case == "server-request"
                    else str(SEARCH.cog_ref),
                    source,
                    context,
                    controller=host,
                )
            )
    # Any dispatch would parse the deliberately invalid corpus and raise a
    # different exception, making the zero-dispatch assertions above fail.
    assert not any(e["stage"] == "completed" for e in events.values)
