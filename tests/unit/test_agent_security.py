"""Agent identity bindings, inherited source pins and safe public projections."""

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.domain.syn import SynSnapshot, type_identity
from potbot.security.public import public_identity_descriptor
from potbot.semantics import agent_security_cli
from potbot.semantics.agent_security import (
    AgentSecurityView,
    build_agent_security_view,
    locking_cog_configuration,
    property_ref,
    security_type_snapshot,
    security_types,
)
from potbot.semantics.agent_security_cli import main
from potbot.semantics.catalog import merge_types, type_ancestors
from potbot.semantics.embers import (
    EmberBinding,
    EmberContractBundle,
    EmberContractResolver,
    EmberInstance,
    EmberRevision,
    TypeEmberContract,
)

WORKSPACE = UUID("00000000-0000-4000-8000-000000000001")
SNAPSHOT = UUID("00000000-0000-4000-8000-000000000002")


def policy() -> dict[str, object]:
    return {
        "workspace_id": str(WORKSPACE),
        "allowed_audiences": ["potbot-local"],
        "allowed_payload_types": ["syn://type/ember"],
        "allowed_domains": ["potbot.event.v2"],
    }


@pytest.fixture
def view() -> AgentSecurityView:
    identity = public_identity_descriptor(bytes(range(32)))
    return build_agent_security_view(
        identity,
        expected_signer_hash=str(identity["signer_hash"]),
        workspace_id=WORKSPACE,
        namespace="agent-security",
        snapshot_id=SNAPSHOT,
        policy=policy(),
    )


def test_registry_extends_existing_hierarchy_without_redefining_native_types() -> None:
    types = merge_types(security_type_snapshot())
    assert len(types) == 715
    assert len(security_types()) == 25
    ancestors = type_ancestors(types)
    assert "syn://type/agent" in ancestors["syn://type/wallet-agent-group"]
    assert "syn://type/software-system" in ancestors["syn://type/ed25519-locking-cog"]
    assert "syn://type/credential" in ancestors["syn://type/agent-credential"]
    native_cog = next(t for t in types if t.ref.slug == "cog")
    assert tuple(p.slug for p in native_cog.parents) == ("computational-process",)
    assert all(str(t.ref).startswith("synny://type/") for t in security_types())


def test_public_snapshot_has_exactly_typed_agent_credential_and_cog(
    view: AgentSecurityView,
) -> None:
    by_ref = {str(obj.ref): obj for obj in view.snapshot.objects}
    assert type_identity(by_ref[view.agent_ref].type_ref) == "wallet-agent"
    assert type_identity(by_ref[view.credential_ref].type_ref) == "agent-credential"
    assert type_identity(by_ref[view.locking_cog_ref].type_ref) == "ed25519-locking-cog"
    for ember in view.bundle.embers:
        if ember.value_kind == "object_ref":
            assert str(ember.value) in by_ref
    assert SynSnapshot.from_json(view.snapshot.to_json()) == view.snapshot


def test_public_projection_omits_handle_and_owner_revision(view: AgentSecurityView) -> None:
    types = merge_types(view.snapshot)
    public = view.bundle.public_projection(types)
    handle = next(e for e in view.bundle.embers if e.value_kind == "private_handle")
    for text in (public.to_json(), view.snapshot.to_json()):
        assert handle.value not in text
        assert handle.ref not in text
        assert handle.revision not in text
        assert view.bundle.fingerprint not in text
    resolver = EmberContractResolver(types, public, expected_scope=public.scope)
    assert (
        resolver.resolve(view.locking_cog_ref, projection="public").to_dict()["conformance"]
        == "public_only"
    )
    assert public.fingerprint != view.bundle.fingerprint
    with pytest.raises(InvalidInputError, match="owner"):
        locking_cog_configuration(public, types, view.locking_cog_ref)


def test_implementation_sources_are_inherited_and_exactly_hashed(view: AgentSecurityView) -> None:
    types = merge_types(view.snapshot)
    resolved = (
        EmberContractResolver(types, view.bundle, expected_scope=view.bundle.scope)
        .resolve(view.locking_cog_ref)
        .to_dict()
    )
    implementation = next(
        s for s in resolved["slots"] if s["slot_ref"] == property_ref("implementation")
    )
    assert implementation["origin"] == "inherited"
    assert len(implementation["values"]) >= 5
    for item in implementation["values"]:
        value = item["ember"]["value"]
        assert hashlib.sha256(value["source_utf8"].encode()).hexdigest() == value["sha256"]
        assert value["execution"] == "descriptive-only"
    config = locking_cog_configuration(view.bundle, types, view.locking_cog_ref)
    assert config == {"signer_hash": view.signer_hash, "policy": policy()}


def _replace_ember(
    bundle: EmberContractBundle, old: EmberRevision, new: EmberRevision
) -> EmberContractBundle:
    return replace(
        bundle,
        embers=tuple(new if e.pointer == old.pointer else e for e in bundle.embers),
        type_contracts=tuple(
            TypeEmberContract.create(
                contract.type_ref,
                contract.slots,
                tuple(
                    EmberBinding(
                        binding.slot_ref,
                        tuple(
                            new.pointer if pointer == old.pointer else pointer
                            for pointer in binding.embers
                        ),
                    )
                    for binding in contract.defaults
                ),
            )
            for contract in bundle.type_contracts
        ),
        instances=tuple(
            EmberInstance.create(
                i.object_ref,
                i.type_ref,
                tuple(
                    EmberBinding(
                        b.slot_ref, tuple(new.pointer if p == old.pointer else p for p in b.embers)
                    )
                    for b in i.bindings
                ),
            )
            for i in bundle.instances
        ),
    )


@pytest.mark.parametrize(
    "slot,value",
    [
        ("verification-key-ember", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="),
        ("private-signing-operation-ember", "secret://potbot/identity/another/hourly-sign"),
    ],
)
def test_locking_cog_rejects_inconsistent_identity_or_handle(
    view: AgentSecurityView, slot: str, value: str
) -> None:
    old = next(e for e in view.bundle.embers if type_identity(e.type_ref) == slot)
    new = EmberRevision.create(
        old.ref,
        old.type_ref,
        old.value_type_ref,
        value,
        value_kind=old.value_kind,
        visibility=old.visibility,
    )
    with pytest.raises(InvalidInputError):
        locking_cog_configuration(
            _replace_ember(view.bundle, old, new), merge_types(view.snapshot), view.locking_cog_ref
        )


def test_group_is_one_wallet_identity_and_inherits_agent_contract() -> None:
    identity = public_identity_descriptor(bytes(range(32)))
    view = build_agent_security_view(
        identity,
        expected_signer_hash=str(identity["signer_hash"]),
        workspace_id=WORKSPACE,
        namespace="group",
        snapshot_id=SNAPSHOT,
        policy=policy(),
        group=True,
    )
    types = merge_types(view.snapshot)
    resolved = (
        EmberContractResolver(types, view.bundle, expected_scope=view.bundle.scope)
        .resolve(view.agent_ref)
        .to_dict()
    )
    assert type_identity(resolved["type_ref"]) == "wallet-agent-group"
    assert resolved["conformance"] == "complete"
    assert (
        locking_cog_configuration(view.bundle, types, view.locking_cog_ref)["signer_hash"]
        == view.signer_hash
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"workspace_id": str(SNAPSHOT)},
        {"allowed_payload_types": ["syn://type/unregistered"]},
        {"allowed_payload_types": ["syn://type/ember", "synny://type/ember"]},
        {"allowed_domains": ["arbitrary"]},
        {"allowed_audiences": []},
    ],
)
def test_factory_rejects_invalid_policy(changes: dict[str, object]) -> None:
    identity = public_identity_descriptor(bytes(range(32)))
    with pytest.raises(InvalidInputError):
        build_agent_security_view(
            identity,
            expected_signer_hash=str(identity["signer_hash"]),
            workspace_id=WORKSPACE,
            namespace="bad",
            snapshot_id=SNAPSHOT,
            policy={**policy(), **changes},
        )


def test_offline_export_is_public_by_default(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    identity = public_identity_descriptor(bytes(range(32)))
    identity_file, policy_file = tmp_path / "identity.json", tmp_path / "policy.json"
    identity_file.write_text(json.dumps(identity))
    policy_file.write_text(json.dumps(policy()))
    assert (
        main(
            [
                "export",
                "--workspace-id",
                str(WORKSPACE),
                "--snapshot-id",
                str(SNAPSHOT),
                "--identity-file",
                str(identity_file),
                "--expect-signer",
                str(identity["signer_hash"]),
                "--policy-file",
                str(policy_file),
                "--namespace",
                "offline",
            ]
        )
        == 0
    )
    document = json.loads(capsys.readouterr().out)
    bundle = EmberContractBundle.from_dict(document["ember_bundle"])
    assert bundle.projection == "public"
    assert all(e.value_kind != "private_handle" for e in bundle.embers)
    assert document["snapshot"]["objects"]


def test_snapshot_rebinding_keeps_public_records_and_revalidates_scope(
    view: AgentSecurityView,
) -> None:
    other = UUID("00000000-0000-4000-8000-000000000003")
    rebound = view.with_snapshot_id(other)
    assert rebound.scope.snapshot_id == other
    assert rebound.fingerprint != view.bundle.fingerprint
    EmberContractResolver(
        merge_types(view.snapshot), rebound, expected_scope=rebound.scope
    ).validate_all()


def _different_implementation(view: AgentSecurityView, component: str) -> EmberContractBundle:
    old = next(
        ember
        for ember in view.bundle.embers
        if type_identity(ember.type_ref) == "code-implementation-ember"
        and ember.value["component"] == component
    )
    value = dict(old.value)
    value["source_utf8"] += "\n# Historical implementation fixture.\n"
    value["sha256"] = hashlib.sha256(value["source_utf8"].encode()).hexdigest()
    new = EmberRevision.create(
        "syn://ember/implementation-" + value["sha256"], old.type_ref, old.value_type_ref, value
    )
    return _replace_ember(view.bundle, old, new)


def test_execution_policy_accepts_the_complete_current_inherited_implementation(
    view: AgentSecurityView,
) -> None:
    assert locking_cog_configuration(
        view.bundle,
        merge_types(view.snapshot),
        view.locking_cog_ref,
        require_current_implementation=True,
    ) == {"signer_hash": view.signer_hash, "policy": policy()}


@pytest.mark.parametrize("component", ["public.py", "native/Cargo.lock", "native/src/main.rs"])
def test_historical_source_is_descriptive_but_cannot_silently_execute_local_broker(
    view: AgentSecurityView,
    component: str,
) -> None:
    historical = _different_implementation(view, component)
    types = merge_types(view.snapshot)
    assert locking_cog_configuration(historical, types, view.locking_cog_ref) == {
        "signer_hash": view.signer_hash,
        "policy": policy(),
    }
    with pytest.raises(InvalidInputError, match="implementation revisions"):
        locking_cog_configuration(
            historical, types, view.locking_cog_ref, require_current_implementation=True
        )


def test_current_implementation_check_preserves_type_alias_equivalence(
    view: AgentSecurityView,
) -> None:
    old = next(
        e for e in view.bundle.embers if type_identity(e.type_ref) == "code-implementation-ember"
    )
    alias = EmberRevision.create(
        old.ref,
        old.type_ref.replace("synny://", "syn://"),
        old.value_type_ref.replace("synny://", "syn://"),
        old.value,
    )
    assert alias.revision == old.revision
    assert (
        locking_cog_configuration(
            _replace_ember(view.bundle, old, alias),
            merge_types(view.snapshot),
            view.locking_cog_ref,
            require_current_implementation=True,
        )["signer_hash"]
        == view.signer_hash
    )


@pytest.mark.parametrize("change", ["missing-component", "metadata", "source-digest"])
def test_execution_requires_complete_matching_source_revisions(
    view: AgentSecurityView,
    change: str,
) -> None:
    old = next(
        e for e in view.bundle.embers if type_identity(e.type_ref) == "code-implementation-ember"
    )
    if change == "missing-component":
        changed = replace(
            view.bundle,
            type_contracts=tuple(
                TypeEmberContract.create(
                    contract.type_ref,
                    contract.slots,
                    tuple(
                        EmberBinding(
                            binding.slot_ref, tuple(p for p in binding.embers if p != old.pointer)
                        )
                        for binding in contract.defaults
                    ),
                )
                for contract in view.bundle.type_contracts
            ),
        )
    else:
        value = dict(old.value)
        if change == "metadata":
            value["implementation_role"] = "different-implementation-role"
        else:
            value["sha256"] = "0" * 64
        new = EmberRevision.create(old.ref, old.type_ref, old.value_type_ref, value)
        changed = _replace_ember(view.bundle, old, new)
    types = merge_types(view.snapshot)
    locking_cog_configuration(changed, types, view.locking_cog_ref)
    with pytest.raises(InvalidInputError, match="implementation revisions"):
        locking_cog_configuration(
            changed, types, view.locking_cog_ref, require_current_implementation=True
        )


def test_current_source_must_be_inherited_from_a_type_contract(
    view: AgentSecurityView,
) -> None:
    implementation_slot = property_ref("implementation")
    original = next(
        c for c in view.bundle.type_contracts if type_identity(c.type_ref) == "ed25519-locking-cog"
    )
    binding = next(b for b in original.defaults if b.slot_ref == implementation_slot)
    changed = replace(
        view.bundle,
        type_contracts=tuple(
            TypeEmberContract.create(c.type_ref, c.slots, ()) if c == original else c
            for c in view.bundle.type_contracts
        ),
        instances=tuple(
            EmberInstance.create(i.object_ref, i.type_ref, (*i.bindings, binding))
            if i.object_ref == view.locking_cog_ref
            else i
            for i in view.bundle.instances
        ),
    )
    types = merge_types(view.snapshot)
    locking_cog_configuration(changed, types, view.locking_cog_ref)
    with pytest.raises(InvalidInputError, match="inherited code"):
        locking_cog_configuration(
            changed, types, view.locking_cog_ref, require_current_implementation=True
        )


def _mock_sign_service(
    monkeypatch: pytest.MonkeyPatch,
    view: AgentSecurityView,
    bundle: EmberContractBundle,
) -> None:
    from potbot import credentials
    from potbot.persistence import database
    from potbot.semantics import ember_service

    class Service:
        def __init__(self, database: object) -> None:
            pass

        def read_bundle(
            self, context: object, bundle_id: UUID, *, projection: str
        ) -> tuple[EmberContractBundle, tuple]:
            assert projection == "owner"
            return bundle, merge_types(view.snapshot)

    monkeypatch.setattr(credentials, "resolve_postgres_dsn", lambda: "unused-unit-test-dsn")
    monkeypatch.setattr(database, "Database", lambda *args, **kwargs: object())
    monkeypatch.setattr(ember_service, "EmberContractService", Service)


def _sign_arguments(view: AgentSecurityView, payload: Path, vault: Path) -> list[str]:
    return [
        "sign",
        "--workspace-id",
        str(WORKSPACE),
        "--principal-id",
        str(SNAPSHOT),
        "--bundle-id",
        str(SNAPSHOT),
        "--cog-ref",
        view.locking_cog_ref,
        "--vault",
        str(vault),
        "--payload",
        str(payload),
        "--payload-type-ref",
        "synny://type/ember",
        "--audience",
        "potbot-local",
        "--domain",
        "potbot.event.v2",
    ]


def test_semantic_sign_checks_current_source_before_hashing_or_custody(
    view: AgentSecurityView,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from potbot.security import broker

    _mock_sign_service(monkeypatch, view, _different_implementation(view, "public.py"))

    def forbidden(*args: object, **kwargs: object) -> Any:
        raise AssertionError(
            "historical implementation must be rejected before payload or custody access"
        )

    monkeypatch.setattr(agent_security_cli, "_payload_digest", forbidden)
    monkeypatch.setattr(broker, "NativeIdentityBroker", forbidden)
    assert main(_sign_arguments(view, tmp_path / "payload", tmp_path / "vault")) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert json.loads(captured.err)["error"] == "InvalidInputError"


def test_semantic_sign_forwards_only_checked_current_configuration_and_exact_payload(
    view: AgentSecurityView,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from potbot.security import broker

    _mock_sign_service(monkeypatch, view, view.bundle)
    payload = tmp_path / "payload"
    payload.write_bytes(b"exact binary payload\x00\xff")
    observed: list[tuple[dict, dict]] = []

    class Native:
        def __init__(self, executable: object) -> None:
            pass

        def sign(self, vault: Path, request: dict, **kwargs: Any) -> dict:
            observed.append((request, kwargs))
            return {"checked-public-result": True}

    monkeypatch.setattr(broker, "NativeIdentityBroker", Native)
    assert main(_sign_arguments(view, payload, tmp_path / "vault")) == 0
    assert json.loads(capsys.readouterr().out) == {"checked-public-result": True}
    request, options = observed.pop()
    assert request["payload_sha256"] == hashlib.sha256(payload.read_bytes()).hexdigest()
    assert options["expected_signer_hash"] == view.signer_hash
    assert "ed25519-locking-cog" in {type_identity(ref) for ref in options["known_type_refs"]}


def test_semantic_payload_hash_rejects_special_files_and_mutations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(InvalidInputError, match="regular file"):
        agent_security_cli._payload_digest(Path(os.devnull))
    with pytest.raises(InvalidInputError, match="regular file"):
        agent_security_cli._public_file(Path(os.devnull))
    payload = tmp_path / "payload"
    payload.write_bytes(b"original")
    original = hashlib.file_digest

    def mutate(stream: Any, algorithm: str) -> Any:
        result = original(stream, algorithm)
        payload.write_bytes(b"changed and longer")
        return result

    monkeypatch.setattr(hashlib, "file_digest", mutate)
    with pytest.raises(InvalidInputError, match="changed"):
        agent_security_cli._payload_digest(payload)


@pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO behavior")
def test_semantic_payload_fifo_does_not_wait_for_a_writer(tmp_path: Path) -> None:
    fifo = tmp_path / "payload-pipe"
    os.mkfifo(fifo)
    with pytest.raises(InvalidInputError, match="regular file"):
        agent_security_cli._payload_digest(fifo)
