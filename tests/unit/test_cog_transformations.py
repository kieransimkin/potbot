"""Concrete Cog type-tree, input/output transformations and authority separation."""

import json
from dataclasses import replace
from uuid import uuid4

import pytest

from potbot.cogs import (
    COG_COMPLETION_JSON,
    COG_JSON_ACTION,
    COG_JSON_CODING,
    COG_SCOPE_CODING,
    BoundCogRegistry,
    CodingScope,
    CogRunContext,
    TypedCogValue,
    builtin_cog_snapshot,
)
from potbot.cogs.builtin import (
    COMPLETION_TEXT,
    JSON_DOCUMENT,
    MODEL_ACTION_FRAME,
    MODEL_CODING_RESPONSE,
    SCOPE_CHECKED_CODING,
)
from potbot.cogs.registry import TRANSFORM_TYPE
from potbot.domain.errors import (
    InvalidInputError,
    SchemaViolationError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.model_proposals import ControllerIdentity, ModelActionFrame, ModelCodingResponse
from potbot.domain.serialization import canonical_json
from potbot.domain.syn import SynObject, SynSnapshot, parse_type_ref
from potbot.semantics.catalog import merge_types, type_ancestors


def context(registry: BoundCogRegistry, *, contract=None):
    return CogRunContext(
        cog_snapshot_hash=registry.snapshot.fingerprint,
        task_revision_id=contract or uuid4(),
        repository_state_id=uuid4(),
        invocation_id=uuid4(),
    )


def coding_response(*, contract=None, patch_path="a.py"):
    return ModelCodingResponse(
        implementation_id=uuid4(),
        base_state_id=uuid4(),
        contract_revision_id=contract or uuid4(),
        patch=b"--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n",
        changed_files=(patch_path,),
    )


def test_registered_instance_types_and_required_ember_signatures():
    snapshot = builtin_cog_snapshot()
    assert snapshot.catalog_mode == "extend_builtin"
    assert len(snapshot.types) == 9
    assert len(snapshot.objects) == 4
    assert all(item.ref.kind == "cog" for item in snapshot.objects)
    root = next(item for item in snapshot.types if str(item.ref) == TRANSFORM_TYPE)
    assert str(root.parents[0]) == "syn://type/cog"
    assert [(prop.name, prop.minimum) for prop in root.properties] == [
        ("inputTypeRef", 1), ("outputTypeRef", 1), ("implementationBindingHash", 1)
    ]
    merged = merge_types(snapshot)
    closure = type_ancestors(merged)
    assert all("syn://type/cog" in closure[str(item.type_ref)] for item in snapshot.objects)
    assert all("inputTypeRef" in json.loads(item.payload_json) for item in snapshot.objects)
    assert BoundCogRegistry.builtins().snapshot.fingerprint == snapshot.fingerprint
    assert SynSnapshot.from_json(snapshot.to_json()).fingerprint == snapshot.fingerprint


def test_cog_chain_converts_completed_provider_text_into_scope_checked_coding_data():
    registry = BoundCogRegistry.builtins()
    revision = uuid4()
    proposed = coding_response(contract=revision)
    source = TypedCogValue(COMPLETION_TEXT, canonical_json(proposed.to_dict()), uuid4())
    ctx = context(registry, contract=revision)
    authority = CodingScope(
        proposed.implementation_id, proposed.base_state_id, revision,
        permitted_paths=("a.py",), known_checks=("unit",),
    )
    outcomes = registry.chain(
        (COG_COMPLETION_JSON, COG_JSON_CODING, COG_SCOPE_CODING), source, ctx,
        scope=authority,
    )
    assert [value.value.type_ref for value in outcomes] == [
        JSON_DOCUMENT, MODEL_CODING_RESPONSE, SCOPE_CHECKED_CODING
    ]
    assert outcomes[-1].value.value == proposed.to_dict()
    assert outcomes[0].receipt.input_artifact_id == source.source_artifact_id
    assert outcomes[0].receipt.upstream_receipt_hash is None
    assert outcomes[1].receipt.upstream_receipt_hash == outcomes[0].receipt.fingerprint
    assert outcomes[2].receipt.upstream_receipt_hash == outcomes[1].receipt.fingerprint
    assert outcomes[2].receipt.scope_hash == authority.fingerprint
    assert outcomes[0].receipt.scope_hash is None
    assert outcomes[2].receipt.binding_hash != outcomes[1].receipt.binding_hash
    assert outcomes[-1].receipt.to_dict()["task_revision_id"] == str(revision)


def test_action_path_is_an_explicit_other_cog_not_an_implicit_schema_guess():
    registry = BoundCogRegistry.builtins()
    identity = ControllerIdentity(*(uuid4() for _ in range(6)))
    from potbot.domain.execution import ActionProposal
    from potbot.domain.model_proposals import ModelActionProposal
    action = ModelActionFrame(identity, (
        ModelActionProposal("inspect", ActionProposal("inspect"), "Inspect code", ("report",)),
    ))
    ctx = context(registry)
    outcomes = registry.chain(
        (COG_COMPLETION_JSON, COG_JSON_ACTION),
        TypedCogValue(COMPLETION_TEXT, json.dumps(action.to_dict())), ctx,
    )
    assert outcomes[-1].value.type_ref == MODEL_ACTION_FRAME
    assert outcomes[-1].value.value == action.to_dict()
    with pytest.raises(SchemaViolationError):
        registry.chain(
            (COG_COMPLETION_JSON, COG_JSON_CODING),
            TypedCogValue(COMPLETION_TEXT, json.dumps(action.to_dict())), ctx,
        )


def test_data_type_mismatch_never_auto_converts_or_traverses_ancestors():
    registry = BoundCogRegistry.builtins()
    ctx = context(registry)
    with pytest.raises(StateMismatchError):
        registry.transform(COG_JSON_CODING, TypedCogValue(COMPLETION_TEXT, "{}"), ctx)
    with pytest.raises(StateMismatchError):
        registry.transform(COG_COMPLETION_JSON, TypedCogValue(JSON_DOCUMENT, {}), ctx)
    with pytest.raises(ScopeDeniedError):
        registry.transform("syn://cog/arbitrary-model-function", TypedCogValue(COMPLETION_TEXT, "{}"), ctx)
    with pytest.raises(StateMismatchError):
        registry.transform(COG_COMPLETION_JSON, TypedCogValue(COMPLETION_TEXT, "{}"), replace(ctx, cog_snapshot_hash="a" * 64))


@pytest.mark.parametrize("raw", [
    '{"a":1,"a":2}',
    '```json\n{}\n```',
    '{} trailing',
    '{"x":NaN}',
    '{"x":1e999}',
    "null",
    "[]",
])
def test_model_text_cog_enforces_exact_json_contract(raw):
    registry = BoundCogRegistry.builtins()
    with pytest.raises(SchemaViolationError):
        registry.transform(COG_COMPLETION_JSON, TypedCogValue(COMPLETION_TEXT, raw), context(registry))


def test_valid_json_is_not_a_valid_coding_result_without_checked_schema():
    registry = BoundCogRegistry.builtins()
    ctx = context(registry)
    with pytest.raises(SchemaViolationError):
        registry.chain(
            (COG_COMPLETION_JSON, COG_JSON_CODING),
            TypedCogValue(COMPLETION_TEXT, '{"schema_version":1}'), ctx,
        )


def test_scope_cog_fails_without_controller_facts_and_rejects_stale_or_wrong_paths():
    registry = BoundCogRegistry.builtins()
    revision = uuid4()
    model = coding_response(contract=revision)
    source = TypedCogValue(MODEL_CODING_RESPONSE, model.to_dict())
    ctx = context(registry, contract=revision)
    with pytest.raises(ScopeDeniedError):
        registry.transform(COG_SCOPE_CODING, source, ctx)
    with pytest.raises(StateMismatchError):
        registry.transform(
            COG_SCOPE_CODING, source, ctx,
            scope=CodingScope(model.implementation_id, model.base_state_id, uuid4(), ("a.py",), ()),
        )
    with pytest.raises(ScopeDeniedError):
        registry.transform(
            COG_SCOPE_CODING, source, ctx,
            scope=CodingScope(model.implementation_id, model.base_state_id, revision, ("safe/",), ()),
        )
    with pytest.raises(ScopeDeniedError):
        registry.transform(
            COG_COMPLETION_JSON, TypedCogValue(COMPLETION_TEXT, "{}"), ctx,
            scope=CodingScope(model.implementation_id, model.base_state_id, revision, ("a.py",), ()),
        )


def test_snapshot_tampering_cannot_swap_cog_implementation_or_transform_types():
    original = builtin_cog_snapshot()
    bad_obj = original.objects[0]
    tampered = json.loads(bad_obj.payload_json)
    tampered["outputTypeRef"] = MODEL_ACTION_FRAME
    altered = SynObject(bad_obj.ref, bad_obj.type_ref, canonical_json(tampered))
    other = replace(original, objects=(altered, *original.objects[1:]))
    with pytest.raises(ScopeDeniedError):
        BoundCogRegistry(other)
    tampered = json.loads(bad_obj.payload_json)
    tampered["implementationBindingHash"] = "0" * 64
    with pytest.raises(ScopeDeniedError):
        BoundCogRegistry(replace(original, objects=(SynObject(bad_obj.ref, bad_obj.type_ref, canonical_json(tampered)), *original.objects[1:])))
    # Introducing an unqualified/type-spoofed Cog instance cannot override the pinned registry.
    unknown = SynObject(
        ref=bad_obj.ref, type_ref=parse_type_ref("syn://type/cog"),
        payload_json=canonical_json({**json.loads(bad_obj.payload_json), "typeRef": "syn://type/cog"}),
    )
    with pytest.raises(ScopeDeniedError):
        BoundCogRegistry(replace(original, objects=(unknown, *original.objects[1:])))


def test_bad_typed_payloads_and_signatures_are_rejected():
    with pytest.raises(InvalidInputError):
        TypedCogValue("dt:legacy", "text")
    assert TypedCogValue("synny://type/potbot-data-value-array", ["list"]).value == ["list"]
    with pytest.raises(InvalidInputError):
        TypedCogValue("synny://type/potbot-data-value-decimal", float("nan"))
    with pytest.raises(InvalidInputError):
        TypedCogValue(COMPLETION_TEXT, "x" * (8 * 1024 * 1024 + 1))
    with pytest.raises(InvalidInputError):
        CodingScope(uuid4(), uuid4(), uuid4(), ("a.py", "a.py"), ())


def test_type_to_type_planner_connects_only_explicit_cogs():
    registry = BoundCogRegistry.builtins()
    assert registry.plan(COMPLETION_TEXT, MODEL_ACTION_FRAME) == (
        COG_COMPLETION_JSON, COG_JSON_ACTION
    )
    assert registry.plan(COMPLETION_TEXT, MODEL_CODING_RESPONSE) == (
        COG_COMPLETION_JSON, COG_JSON_CODING
    )
    with pytest.raises(StateMismatchError):
        registry.plan(COMPLETION_TEXT, SCOPE_CHECKED_CODING)
    assert registry.plan(
        COMPLETION_TEXT, SCOPE_CHECKED_CODING, controller_scope=True
    ) == (COG_COMPLETION_JSON, COG_JSON_CODING, COG_SCOPE_CODING)
    with pytest.raises(InvalidInputError):
        registry.plan(COMPLETION_TEXT, COMPLETION_TEXT)
    with pytest.raises(ScopeDeniedError):
        registry.plan("syn://type/invented", MODEL_CODING_RESPONSE)
    # Scheme-equivalent type identities remain the same registered Type.
    assert registry.plan("synny://type/potbot-provider-completion-text", MODEL_CODING_RESPONSE) == (
        COG_COMPLETION_JSON, COG_JSON_CODING
    )


def test_convert_follows_derived_route_and_records_each_step():
    registry = BoundCogRegistry.builtins()
    proposed = coding_response()
    scope = CodingScope(
        proposed.implementation_id, proposed.base_state_id, proposed.contract_revision_id,
        ("a.py",), (),
    )
    outputs = registry.convert(
        TypedCogValue(COMPLETION_TEXT, canonical_json(proposed.to_dict())),
        SCOPE_CHECKED_CODING, context(registry, contract=proposed.contract_revision_id),
        scope=scope,
    )
    assert len(outputs) == 3
    assert outputs[-1].value.type_ref == SCOPE_CHECKED_CODING
    assert outputs[-1].receipt.upstream_receipt_hash == outputs[1].receipt.fingerprint


def test_embedded_cog_snapshot_fully_agrees_with_runtime_contract():
    from pathlib import Path
    package_root = Path(__file__).resolve().parents[2]
    path = package_root / "vocab/potbot-cog-transforms.snapshot.json"
    if not path.is_file():
        pytest.skip("checkout-local snapshot is packaged separately")
    assert SynSnapshot.from_json(path.read_text("utf-8")).fingerprint == builtin_cog_snapshot().fingerprint
