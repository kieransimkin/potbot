"""Inventory contracts; synthetic snapshots/journals do not qualify deployment."""

import asyncio
import json
from dataclasses import replace
from uuid import uuid4

import pytest

from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.cogs.inventory import SIGNATURE
from potbot.cogs.registry import BoundCogRegistry
from potbot.controller.cog_work import CogWorkMonitor
from potbot.controller.tool_inventory import ToolInventoryRuntime
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    CapabilityUnavailableError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.serialization import content_hash
from potbot.tools.inventory import CORE_ANCHORS, OWNERS, PYTHON_ANCHORS, build_inventory
from potbot.tools.repository_snapshot import SnapshotProfile, _entry, _snapshot
from tests.unit.test_debugger_cogs_live import FixtureArtifacts, FixtureEvents


def test_acceptance_recognizes_documented_namespaced_stages_and_rejects_unknown_refs():
    from potbot.tools.inventory import STAGES
    from scripts.check_tool_inventory import check_stage_references

    check_stage_references(STAGES)
    with pytest.raises(AssertionError, match="Undocumented inventory stage references"):
        check_stage_references({**STAGES, "syn_release": ("syn-release:SYN-R99",)})


def fixture_snapshot(*, omit=(), extra=None, kind="committed", version="0.5.0.dev4"):
    files = {path: b"# synthetic source fixture\n" for path in (*CORE_ANCHORS, *PYTHON_ANCHORS)}
    files.update(
        {
            "pyproject.toml": f'[project]\nversion="{version}"\n'.encode(),
            "src/potbot/__init__.py": f'__version__ = "{version}"\n'.encode(),
        }
    )
    files.update(extra or {})
    return _snapshot(
        [_entry(path, 0o644, data, "file") for path, data in files.items() if path not in omit],
        "1" * 40,
        "2" * 40,
        kind,
        SnapshotProfile(),
        {},
    )


def runtime(snapshot, *, admit=lambda *_: None, events=None, loader=None):
    registry = BoundCogRegistry.tool_inventory()
    events = events or FixtureEvents()
    actor = WorkspaceContext(uuid4(), uuid4())
    artifacts = FixtureArtifacts()
    host = ToolInventoryRuntime(
        CogWorkMonitor(
            events,
            artifacts,
            actor,
            emitter_run_id=uuid4(),
            event_type_ref="synny://type/workflow-execution",
        ),
        loader or (lambda: snapshot),
        task_revision_id=uuid4(),
        repository_state_id=uuid4(),
        expected_digest=snapshot.content_digest,
        catalog_hash=registry.snapshot.fingerprint,
        admit=admit,
    )
    context = CogRunContext(host.catalog_hash, host.task_revision, host.repository_state, uuid4())
    source = TypedCogValue(
        SIGNATURE.input_type_ref,
        {
            "schema_version": 1,
            "profile": "core",
            "expected_content_digest": snapshot.content_digest,
        },
    )
    return registry, host, context, source, events, artifacts


def test_inventory_is_deterministic_complete_and_source_only():
    installer_sources = {
        "src/potbot/wsl_kernel.py": b"# WSL installer verification\n",
        "src/potbot/wsl_kernel_activation.py": b"# WSL installer activation\n",
    }
    snapshot = fixture_snapshot(extra=installer_sources)
    first = build_inventory(snapshot)
    assert first == build_inventory(snapshot)
    assert first["inventory_sha256"] == content_hash(
        {k: v for k, v in first.items() if k != "inventory_sha256"}
    )
    assert all(
        x["qualification"] == "not_established_by_source_inventory" for x in first["components"]
    )
    assert all(x["state"] == "unqualified" and not x["enabled"] for x in first["specialist_routes"])
    assert "synthetic source fixture" not in json.dumps(first)
    entrypoints = next(item for item in first["components"] if item["id"] == "entrypoints")
    assert set(installer_sources) <= {item["path"] for item in entrypoints["source_files"]}
    assert (
        sum(len(x["source_files"]) for x in first["components"])
        == len(snapshot.manifest["files"]) - 2
    )


@pytest.mark.parametrize("variant", ["missing", "owner", "working_tree", "version"])
def test_inventory_rejects_missing_drift_or_unowned_source(variant):
    kwargs = {
        "missing": {"omit": (CORE_ANCHORS[3],)},
        "owner": {"extra": {"src/potbot/unknown/new.py": b"# no owner"}},
        "working_tree": {"kind": "working_tree"},
        "version": {"extra": {"src/potbot/__init__.py": b'__version__="changed"'}},
    }[variant]
    with pytest.raises(StateMismatchError):
        build_inventory(fixture_snapshot(**kwargs))


def test_optional_hosts_do_not_block_core_but_cannot_silently_enable():
    assert build_inventory(fixture_snapshot())["profile"] == "core"
    with pytest.raises(CapabilityUnavailableError):
        build_inventory(fixture_snapshot(), profile="android")
    with pytest.raises(StateMismatchError):
        build_inventory(fixture_snapshot(omit=(PYTHON_ANCHORS[0],)), profile="python_debugger")


def test_source_is_parsed_without_importing_and_invalid_metadata_fails_closed():
    snapshot = fixture_snapshot(
        extra={
            "src/potbot/__init__.py": b'__version__="0.5.0.dev4"\nraise RuntimeError("must never execute")'
        }
    )
    assert build_inventory(snapshot)["source_version"] == "0.5.0.dev4"
    with pytest.raises(StateMismatchError):
        build_inventory(fixture_snapshot(extra={"pyproject.toml": b"not TOML!"}))


def test_cog_invocation_and_all_lifecycle_stats():
    registry, host, context, source, events, artifacts = runtime(fixture_snapshot())
    out = asyncio.run(registry.invoke(str(SIGNATURE.cog_ref), source, context, controller=host))
    assert (
        out.receipt.input_hash == source.fingerprint
        and out.value.type_ref == SIGNATURE.output_type_ref
    )
    assert events.values[0]["stage"] == "started" and events.values[-1]["stage"] == "completed"
    assert len([e for e in events.values if e["stage"] == "updated"]) == len(OWNERS) + 1
    terminal = events.values[-1]
    assert terminal["telemetry"]["metrics"]["elapsed_wall_ms"]["provenance"] == "measured"
    assert terminal["telemetry"]["metrics"]["target_cpu_ms"]["value"] is None
    output = artifacts.values[terminal["output_artifact_ref"].removeprefix("artifact:")]
    assert output["cog_receipt"] == out.receipt.to_dict()
    with pytest.raises(ScopeDeniedError):
        asyncio.run(registry.invoke(str(SIGNATURE.cog_ref), source, context, controller=host))


@pytest.mark.parametrize(
    "variant", ["denied", "revoked", "changed", "profile", "wrong_type", "direct", "no_monitor"]
)
def test_failed_admission_or_monitoring_has_zero_source_dispatch(variant):
    reads = []
    calls = []

    def admit(_signature, source, _context):
        calls.append(1)
        if variant == "denied" or variant == "revoked" and len(calls) == 2:
            raise ScopeDeniedError("Fixture denied")
        if variant == "changed":
            source.value["profile"] = "python_debugger"

    class Broken(FixtureEvents):
        def append(self, actor, event):
            raise OSError("fixture storage unavailable")

    registry, host, context, source, events, _ = runtime(
        fixture_snapshot(),
        admit=admit,
        loader=lambda: reads.append(1),
        events=Broken() if variant == "no_monitor" else None,
    )
    if variant == "profile":
        source.value["profile"] = "android"
    if variant == "wrong_type":
        source = TypedCogValue(SIGNATURE.output_type_ref, source.value)
    with pytest.raises((ScopeDeniedError, InvalidInputError, StateMismatchError, OSError)):
        if variant == "direct":
            asyncio.run(host.dispatch(source))
        else:
            asyncio.run(registry.invoke(str(SIGNATURE.cog_ref), source, context, controller=host))
    assert reads == []
    if variant == "denied":
        assert [e["stage"] for e in events.values] == ["failed"]


def test_frozen_typed_call_and_changed_catalog_are_rejected():
    registry, host, context, source, _, _ = runtime(fixture_snapshot())
    with pytest.raises(StateMismatchError):
        asyncio.run(
            registry.invoke(
                str(SIGNATURE.cog_ref),
                source,
                replace(context, cog_snapshot_hash="0" * 64),
                controller=host,
            )
        )
    loaded = fixture_snapshot(extra={"src/potbot/tools/new.py": b"# changed"})
    host.load = lambda: loaded
    with pytest.raises(StateMismatchError):
        asyncio.run(registry.invoke(str(SIGNATURE.cog_ref), source, context, controller=host))


def test_existing_context_and_persistent_service_have_explicit_owners():
    progress = []
    snapshot = fixture_snapshot(
        extra={
            "src/potbot/context/service.py": b"raise RuntimeError('never import')",
            "src/potbot/service.py": b"raise RuntimeError('never import')",
        }
    )
    report = build_inventory(snapshot, progress=progress.append)
    owners = {item["id"]: item for item in report["components"]}
    assert [row["path"] for row in owners["context"]["source_files"]] == [
        "src/potbot/context/service.py"
    ]
    assert "src/potbot/service.py" in {row["path"] for row in owners["entrypoints"]["source_files"]}
    assert len(progress) == len(owners) == len(OWNERS)
    assert all(
        item["qualification"] == "not_established_by_source_inventory" for item in owners.values()
    )


def test_syn_release_adapter_has_its_own_source_owner_without_import_or_node_admission():
    snapshot = fixture_snapshot(
        extra={
            "src/potbot/syn_release/store.py": b"raise RuntimeError('inventory must not execute an installer')"
        }
    )
    inventory = build_inventory(snapshot)
    component = next(x for x in inventory["components"] if x["id"] == "syn_release")
    assert component["source_state"] == "present"
    assert component["stage_refs"] == ["syn-release:SYN-R02", "syn-release:SYN-R03"]
    assert "operator-owned" in component["integration_owner"]
    assert component["qualification"] == "not_established_by_source_inventory"
    assert [entry["path"] for entry in component["source_files"]] == [
        "src/potbot/syn_release/store.py"
    ]
