"""Property inheritance, privacy, revision integrity and scoped catalog conformance."""

import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.domain.syn import SynRef, SynType
from potbot.semantics.catalog import catalog_fingerprint
from potbot.semantics.embers import (
    MAX_BUNDLE_BYTES,
    EffectiveEmberInstance,
    EmberBinding,
    EmberContractBundle,
    EmberContractResolver,
    EmberContractScope,
    EmberInstance,
    EmberPointer,
    EmberRevision,
    EmberSlot,
    TypeEmberContract,
    build_bundle,
)

WORKSPACE = UUID("00000000-0000-4000-8000-000000000001")
SNAPSHOT = UUID("00000000-0000-4000-8000-000000000002")
OBJECT = "syn://cog/test-signer"
IMPLEMENTATION = "syn://thing/property-implementation"
IDENTITY = "syn://thing/property-identity"
PRIVATE = "syn://thing/property-private-operation"


def type_ref(slug: str) -> str:
    return f"syn://type/{slug}"


def definition(slug: str, *parents: str) -> SynType:
    return SynType(
        SynRef(type_ref(slug)), slug, tuple(SynRef(type_ref(p)) for p in parents), None, ()
    )


@pytest.fixture
def types() -> tuple[SynType, ...]:
    return (
        definition("thing"),
        definition("ember", "thing"),
        definition("property-ember", "ember"),
        definition("code-ember", "property-ember"),
        definition("value", "thing"),
        definition("text-value", "value"),
        definition("narrow-text", "text-value"),
        definition("integer", "value"),
        definition("private-key", "value"),
        definition("custom-private-key", "private-key"),
        definition("cog", "thing"),
        definition("base", "cog"),
        definition("left", "base"),
        definition("right", "base"),
        definition("child", "left", "right"),
    )


def ember(slug: str, value: object | None = None, **kwargs: str) -> EmberRevision:
    return EmberRevision.create(
        f"syn://ember/{slug}",
        type_ref(kwargs.pop("type", "code-ember")),
        type_ref(kwargs.pop("value_type", "text-value")),
        value if value is not None else slug,
        **kwargs,
    )


def slot(**kwargs: object) -> EmberSlot:
    values = {
        "slot_ref": IMPLEMENTATION,
        "name": "Implementation",
        "ember_type_ref": type_ref("property-ember"),
        "value_type_ref": type_ref("text-value"),
        "minimum": 1,
        "maximum": 1,
        "source": "effective",
        "visibility": "public",
        "override": "replace",
    }
    values.update(kwargs)
    return EmberSlot(**values)


def binding(*values: EmberRevision, property_ref: str = IMPLEMENTATION) -> EmberBinding:
    return EmberBinding(property_ref, tuple(value.pointer for value in values))


def contract(
    name: str,
    *declarations: EmberSlot,
    defaults: tuple[EmberBinding, ...] = (),
) -> TypeEmberContract:
    return TypeEmberContract.create(type_ref(name), declarations, defaults)


def bundle(
    types: tuple[SynType, ...],
    *contracts: TypeEmberContract,
    embers: tuple[EmberRevision, ...] = (),
    instances: tuple[EmberInstance, ...] | None = None,
) -> EmberContractBundle:
    return EmberContractBundle(
        EmberContractScope(WORKSPACE, "unit", SNAPSHOT, catalog_fingerprint(types)),
        contracts,
        embers,
        instances if instances is not None else (EmberInstance.create(OBJECT, type_ref("child")),),
    )


def resolver(types: tuple[SynType, ...], value: EmberContractBundle) -> EmberContractResolver:
    return EmberContractResolver(types, value, expected_scope=value.scope)


def resolved_values(result: EffectiveEmberInstance) -> list[object]:
    return [v["ember"]["value"] for row in result.to_dict()["slots"] for v in row["values"]]


def test_diamond_inherits_shared_default_once_with_provenance(types: tuple[SynType, ...]) -> None:
    code = ember("implementation-a")
    data = bundle(types, contract("base", slot(), defaults=(binding(code),)), embers=(code,))
    result = resolver(types, data).resolve(OBJECT).to_dict()
    assert result["conformance"] == "complete"
    assert result["slots"][0]["origin"] == "inherited"
    assert len(result["slots"][0]["values"]) == 1
    assert result["slots"][0]["values"][0]["source_types"] == [type_ref("base")]
    assert result["slots"][0]["declarations"][0]["revision"] == data.type_contracts[0].revision
    assert result["bundle_revision"] == data.fingerprint
    assert EmberContractBundle.from_json(data.to_json()) == data


def test_type_aliases_and_record_order_do_not_change_content_identity(
    types: tuple[SynType, ...],
) -> None:
    code = ember("implementation-a")
    first = bundle(types, contract("base", slot(), defaults=(binding(code),)), embers=(code,))

    def alias(value: str) -> str:
        return value.replace("syn://type/", "synny://type/")

    alias_slot = replace(
        slot(),
        ember_type_ref=alias(type_ref("property-ember")),
        value_type_ref=alias(type_ref("text-value")),
    )
    alias_code = EmberRevision.create(
        code.ref, alias(code.type_ref), alias(code.value_type_ref), code.value
    )
    second = bundle(
        types,
        TypeEmberContract.create(alias(type_ref("base")), (alias_slot,), (binding(code),)),
        embers=(alias_code,),
        instances=(EmberInstance.create(OBJECT, alias(type_ref("child"))),),
    )
    assert first.type_contracts[0].revision == second.type_contracts[0].revision
    assert code.revision == alias_code.revision
    assert first.fingerprint == second.fingerprint
    assert resolved_values(resolver(types, second).resolve(OBJECT)) == [code.value]
    with pytest.raises(InvalidInputError, match="duplicate Type contract identity"):
        replace(first, type_contracts=(*first.type_contracts, *second.type_contracts))


def test_conflicting_parent_defaults_require_explicit_resolution(
    types: tuple[SynType, ...],
) -> None:
    a, b = ember("implementation-a"), ember("implementation-b")
    base = contract("base", slot())
    left = contract("left", defaults=(binding(a),))
    right = contract("right", defaults=(binding(b),))
    with pytest.raises(InvalidInputError, match="conflict"):
        resolver(types, bundle(types, base, left, right, embers=(a, b)))
    selected = bundle(
        types, base, left, right, contract("child", defaults=(binding(b),)), embers=(a, b)
    )
    assert resolved_values(resolver(types, selected).resolve(OBJECT)) == ["implementation-b"]


def test_fixed_default_cannot_be_changed_by_subtype_or_instance(types: tuple[SynType, ...]) -> None:
    a, b = ember("implementation-a"), ember("implementation-b")
    base = contract("base", slot(override="fixed"), defaults=(binding(a),))
    with pytest.raises(InvalidInputError, match="fixed default"):
        resolver(
            types, bundle(types, base, contract("left", defaults=(binding(b),)), embers=(a, b))
        )
    data = bundle(
        types,
        base,
        embers=(a, b),
        instances=(EmberInstance.create(OBJECT, type_ref("child"), (binding(b),)),),
    )
    with pytest.raises(InvalidInputError, match="fixed inherited"):
        resolver(types, data).resolve(OBJECT)


def test_additive_defaults_deduplicate_and_respect_cardinality(types: tuple[SynType, ...]) -> None:
    a, b, c = ember("a"), ember("b"), ember("c")
    base = contract("base", slot(override="append", maximum=3), defaults=(binding(a),))
    data = bundle(
        types,
        base,
        contract("left", defaults=(binding(b),)),
        contract("right", defaults=(binding(a),)),
        embers=(a, b, c),
        instances=(EmberInstance.create(OBJECT, type_ref("child"), (binding(c),)),),
    )
    result = resolver(types, data).resolve(OBJECT)
    assert resolved_values(result) == ["a", "b", "c"]
    assert result.to_dict()["slots"][0]["origin"] == "combined"
    too_many = contract("child", slot(override="append", maximum=2))
    with pytest.raises(InvalidInputError, match="cardinality"):
        resolver(types, replace(data, type_contracts=(*data.type_contracts, too_many))).resolve(
            OBJECT
        )


@pytest.mark.parametrize(
    "change",
    [
        {"minimum": 0},
        {"maximum": 2},
        {"source": "effective"},
        {"visibility": "public"},
        {"override": "replace"},
        {"ember_type_ref": type_ref("ember")},
        {"value_type_ref": type_ref("value")},
    ],
)
def test_subtypes_cannot_weaken_parent_constraints(
    types: tuple[SynType, ...], change: dict[str, object]
) -> None:
    parent = slot(source="instance", visibility="private", override="fixed")
    with pytest.raises(InvalidInputError, match="weakens"):
        resolver(
            types,
            bundle(types, contract("base", parent), contract("child", replace(parent, **change))),
        )


def test_compatible_subtype_narrows_value_type(types: tuple[SynType, ...]) -> None:
    value = ember("narrow", value_type="narrow-text")
    parent = contract("base", slot(minimum=0, maximum=3))
    child = contract(
        "child",
        slot(value_type_ref=type_ref("narrow-text"), override="fixed"),
        defaults=(binding(value),),
    )
    assert resolved_values(
        resolver(types, bundle(types, parent, child, embers=(value,))).resolve(OBJECT)
    ) == ["narrow"]


def test_required_instance_binding_is_distinct_from_required_effective_default(
    types: tuple[SynType, ...],
) -> None:
    value = ember("identity")
    declaration = slot(source="instance")
    with pytest.raises(InvalidInputError, match="instance-only"):
        resolver(
            types,
            bundle(
                types, contract("base", declaration, defaults=(binding(value),)), embers=(value,)
            ),
        )
    missing = bundle(types, contract("base", declaration))
    with pytest.raises(InvalidInputError, match="cardinality"):
        resolver(types, missing).resolve(OBJECT)
    supplied = replace(
        missing,
        embers=(value,),
        instances=(EmberInstance.create(OBJECT, type_ref("child"), (binding(value),)),),
    )
    assert resolved_values(resolver(types, supplied).resolve(OBJECT)) == ["identity"]


def test_optional_slot_and_explicit_null_are_different_from_absence(
    types: tuple[SynType, ...],
) -> None:
    null = EmberRevision.create(
        "syn://ember/null-value", type_ref("property-ember"), type_ref("value"), None
    )
    base = contract("base", slot(minimum=0, value_type_ref=type_ref("value")))
    absent = bundle(types, base)
    assert resolver(types, absent).resolve(OBJECT).to_dict()["slots"][0]["values"] == []
    present = replace(
        absent,
        embers=(null,),
        instances=(EmberInstance.create(OBJECT, type_ref("child"), (binding(null),)),),
    )
    assert resolved_values(resolver(types, present).resolve(OBJECT)) == [None]


def private_bundle(types: tuple[SynType, ...], secret_name: str) -> EmberContractBundle:
    code = ember("shared-code")
    handle = ember(
        secret_name,
        f"secret://unit/{secret_name}",
        value_kind="private_handle",
        visibility="private",
        value_type="private-key",
    )
    secret_slot = slot(
        slot_ref=PRIVATE,
        name="Signing operation",
        value_type_ref=type_ref("private-key"),
        source="instance",
        visibility="private",
        override="fixed",
    )
    return bundle(
        types,
        contract("base", slot(), secret_slot, defaults=(binding(code),)),
        embers=(code, handle),
        instances=(
            EmberInstance.create(
                OBJECT, type_ref("child"), (binding(handle, property_ref=PRIVATE),)
            ),
        ),
    )


def test_public_projection_excludes_private_values_ids_digests_and_hidden_presence(
    types: tuple[SynType, ...],
) -> None:
    first = private_bundle(types, "private-one")
    second = private_bundle(types, "private-two")
    public = first.public_projection(types)
    assert public.projection == "public"
    assert public.fingerprint == second.public_projection(types).fingerprint
    output = public.to_json()
    assert first.fingerprint not in output
    assert "private-one" not in output
    assert "secret://" not in output
    assert len(public.embers) == 1
    assert public.type_contracts[0].slots[1].visibility == "private"
    assert EmberContractBundle.from_json(output) == public
    result = resolver(types, public).resolve(OBJECT, projection="public").to_dict()
    assert result["conformance"] == "public_only"
    assert "instance_revision" not in result and "bundle_revision" not in result
    assert len(result["slots"]) == 1
    with pytest.raises(InvalidInputError, match="complete owner conformance"):
        resolver(types, public).resolve(OBJECT)
    missing = replace(
        first,
        embers=(first.embers[0],),
        instances=(EmberInstance.create(OBJECT, type_ref("child")),),
    )
    assert missing.public_projection(types).fingerprint == public.fingerprint
    with pytest.raises(InvalidInputError, match="cardinality"):
        resolver(types, missing).resolve(OBJECT)


def test_private_default_has_no_public_pointer_or_revision(types: tuple[SynType, ...]) -> None:
    private = ember("private-policy", "a private local policy", visibility="private")
    data = bundle(
        types,
        contract("base", slot(visibility="private"), defaults=(binding(private),)),
        embers=(private,),
    )
    projected = data.public_projection(types)
    assert projected.embers == ()
    assert projected.type_contracts[0].defaults == ()
    assert private.ref not in projected.to_json()
    assert private.revision not in projected.to_json()
    assert data.type_contracts[0].revision != projected.type_contracts[0].revision
    assert resolver(types, projected).resolve(OBJECT, projection="public").to_dict()["slots"] == []


def test_projection_drops_unbound_public_embers(types: tuple[SynType, ...]) -> None:
    code, unused = ember("used"), ember("unbound-data")
    data = bundle(types, contract("base", slot(), defaults=(binding(code),)), embers=(code, unused))
    assert data.public_projection(types).embers == (code,)


def test_private_handles_cannot_be_public_or_inherited(types: tuple[SynType, ...]) -> None:
    with pytest.raises(InvalidInputError, match="private operation handles require"):
        ember("bad-handle", "secret://unit/signer", value_kind="private_handle")
    private = ember(
        "private", "secret://unit/signer", value_kind="private_handle", visibility="private"
    )
    with pytest.raises(InvalidInputError, match="cannot be Type defaults"):
        resolver(
            types,
            bundle(
                types,
                contract("base", slot(visibility="private"), defaults=(binding(private),)),
                embers=(private,),
            ),
        )
    data = bundle(
        types,
        contract("base", slot()),
        embers=(private,),
        instances=(EmberInstance.create(OBJECT, type_ref("child"), (binding(private),)),),
    )
    with pytest.raises(InvalidInputError, match="public property"):
        resolver(types, data).resolve(OBJECT)


@pytest.mark.parametrize(
    "payload",
    [
        {"privateKey": "forbidden"},
        {"nested": [{"recovery_words": ["forbidden"]}]},
        {"metadata": {"original_entropy": "forbidden"}},
        {"privateData": "forbidden"},
    ],
)
def test_recognized_secret_fields_are_rejected_recursively(payload: object) -> None:
    with pytest.raises(InvalidInputError, match="raw secret material"):
        ember("forbidden", payload)


def test_secret_material_type_and_subtype_cannot_bypass_private_boundary(
    types: tuple[SynType, ...],
) -> None:
    with pytest.raises(InvalidInputError, match="secret material"):
        ember("bad", value_type="private-key")
    secret = ember("subtype", value_type="custom-private-key")
    with pytest.raises(InvalidInputError, match="secret material Type ancestry"):
        resolver(types, bundle(types, embers=(secret,)))
    secret = EmberRevision.create(
        "syn://ember/secret-as-ember",
        type_ref("custom-private-key"),
        type_ref("text-value"),
        "not-exportable",
    )
    with pytest.raises(InvalidInputError, match="secret material Type ancestry"):
        resolver(types, bundle(types, embers=(secret,)))


def test_public_bundle_rejects_private_binding_even_if_value_marked_public(
    types: tuple[SynType, ...],
) -> None:
    value = ember("hidden-public-metadata")
    data = bundle(
        types,
        contract("base", slot(visibility="private")),
        embers=(value,),
        instances=(EmberInstance.create(OBJECT, type_ref("child"), (binding(value),)),),
    )
    with pytest.raises(InvalidInputError, match="private property bindings"):
        resolver(types, replace(data, projection="public"))
    data = bundle(
        types,
        contract("base", slot(visibility="private"), defaults=(binding(value),)),
        embers=(value,),
    )
    with pytest.raises(InvalidInputError, match="private default bindings"):
        resolver(types, replace(data, projection="public"))


def test_actual_ember_type_and_referent_type_are_checked_separately(
    types: tuple[SynType, ...],
) -> None:
    for invalid in (ember("wrong-ember", type="value"), ember("wrong-value", value_type="integer")):
        data = bundle(
            types,
            contract("base", slot()),
            embers=(invalid,),
            instances=(EmberInstance.create(OBJECT, type_ref("child"), (binding(invalid),)),),
        )
        with pytest.raises(InvalidInputError, match="Ember Type or represented value Type"):
            resolver(types, data).resolve(OBJECT)


def test_revision_pins_reject_changes_and_missing_revision(types: tuple[SynType, ...]) -> None:
    value = ember("a")
    changed = value.to_dict()
    changed["value"] = "tampered"
    with pytest.raises(InvalidInputError, match="immutable content"):
        EmberRevision.from_dict(changed)
    data = bundle(types, contract("base", slot(), defaults=(binding(value),)), embers=(value,))
    altered = data.to_dict()
    altered["type_contracts"][0]["slots"][0]["minimum"] = 0
    with pytest.raises(InvalidInputError, match="immutable content"):
        EmberContractBundle.from_dict(altered)
    altered = data.to_dict()
    altered["instances"][0]["type_ref"] = type_ref("left")
    with pytest.raises(InvalidInputError, match="immutable content"):
        EmberContractBundle.from_dict(altered)
    bad = TypeEmberContract.create(
        type_ref("base"),
        (slot(),),
        (EmberBinding(IMPLEMENTATION, (EmberPointer(value.ref, "0" * 64),)),),
    )
    with pytest.raises(InvalidInputError, match="unresolved or mismatched revision"):
        resolver(types, bundle(types, bad, embers=(value,)))


def test_scoped_catalog_binding_prevents_cross_workspace_or_revision_reuse(
    types: tuple[SynType, ...],
) -> None:
    data = bundle(types)
    for scope in (
        replace(data.scope, workspace_id=SNAPSHOT),
        replace(data.scope, snapshot_id=WORKSPACE),
        replace(data.scope, namespace="other"),
        replace(data.scope, catalog_hash="0" * 64),
    ):
        with pytest.raises(InvalidInputError, match="scope does not match"):
            EmberContractResolver(types, data, expected_scope=scope)
    changed_types = (*types, definition("new-type", "thing"))
    with pytest.raises(InvalidInputError, match="catalog hash"):
        resolver(changed_types, data)


@pytest.mark.parametrize(
    "minimum,maximum", [(True, 1), (0, False), (-1, 1), (2, 1), (0, 1025), (0, None)]
)
def test_cardinality_is_strict_and_bounded(minimum: object, maximum: object) -> None:
    with pytest.raises(InvalidInputError, match="cardinality"):
        slot(minimum=minimum, maximum=maximum)


def test_unknown_types_undeclared_properties_and_duplicates_fail(
    types: tuple[SynType, ...],
) -> None:
    with pytest.raises(InvalidInputError, match="absent from the selected catalog"):
        resolver(types, bundle(types, contract("unknown", slot())))
    value = ember("a")
    with pytest.raises(InvalidInputError, match="undeclared"):
        resolver(
            types, bundle(types, contract("base", defaults=(binding(value),)), embers=(value,))
        )
    with pytest.raises(InvalidInputError, match="undeclared"):
        resolver(
            types,
            bundle(
                types,
                embers=(value,),
                instances=(EmberInstance.create(OBJECT, type_ref("child"), (binding(value),)),),
            ),
        )
    with pytest.raises(InvalidInputError, match="repeat"):
        binding(value, value)
    with pytest.raises(InvalidInputError, match="duplicate Type slot"):
        contract("base", slot(), slot())
    with pytest.raises(InvalidInputError, match="duplicate property"):
        EmberInstance.create(OBJECT, type_ref("child"), (binding(value), binding(value)))


def test_strict_json_and_detached_results(types: tuple[SynType, ...]) -> None:
    value = bundle(types)
    with pytest.raises(InvalidInputError, match="duplicate JSON"):
        EmberContractBundle.from_json('{"schema_version":1,"schema_version":1}')
    with pytest.raises(InvalidInputError, match="fields do not match"):
        EmberContractBundle.from_dict({**value.to_dict(), "unexpected": True})
    with pytest.raises(InvalidInputError, match="byte limit"):
        EmberContractBundle.from_json(" " * (MAX_BUNDLE_BYTES + 1))
    with pytest.raises(InvalidInputError, match="excessively nested"):
        EmberContractBundle.from_json("[" * 65 + "0" + "]" * 65)
    with pytest.raises(InvalidInputError, match="non-finite"):
        EmberContractBundle.from_json('{"bad":NaN}')
    before = value.fingerprint
    detached = value.to_dict()
    detached["instances"].clear()
    assert value.fingerprint == before


def test_build_bundle_validates_instances_and_public_result_is_standalone(
    types: tuple[SynType, ...],
) -> None:
    code = ember("implementation")
    declaration = contract("base", slot(), defaults=(binding(code),))
    result = build_bundle(
        types,
        workspace_id=WORKSPACE,
        namespace="unit",
        snapshot_id=SNAPSHOT,
        type_contracts=(declaration,),
        embers=(code,),
        instances=(EmberInstance.create(OBJECT, type_ref("child")),),
    )
    assert resolver(types, result).validate_all()[0].to_dict()["conformance"] == "complete"
    projected = result.public_projection(types)
    assert json.loads(projected.to_json())["projection"] == "public"
    with pytest.raises(InvalidInputError, match="cardinality"):
        build_bundle(
            types,
            workspace_id=WORKSPACE,
            namespace="unit",
            snapshot_id=SNAPSHOT,
            type_contracts=(contract("base", slot()),),
            instances=(EmberInstance.create(OBJECT, type_ref("child")),),
        )


def test_json_schema_describes_owner_and_public_wire_formats(types: tuple[SynType, ...]) -> None:
    import jsonschema

    schema = json.loads(
        (
            Path(__file__).resolve().parents[2] / "vocab/ember-contract-bundle.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    owner = private_bundle(types, "private-operation")
    validator.validate(owner.to_dict())
    validator.validate(owner.public_projection(types).to_dict())
    falsely_public = owner.to_dict()
    falsely_public["projection"] = "public"
    assert list(validator.iter_errors(falsely_public))
    unknown = owner.to_dict()
    unknown["type_contracts"][0]["slots"][0]["unknown"] = True
    assert list(validator.iter_errors(unknown))


def test_missing_instance_is_reported_without_echoing_an_untrusted_reference(
    types: tuple[SynType, ...],
) -> None:
    with pytest.raises(InvalidInputError, match="absent from the selected bundle") as caught:
        resolver(types, bundle(types)).resolve("syn://cog/absent")
    assert "syn://cog/absent" not in str(caught.value)


def test_multi_parent_value_constraints_are_intersected(types: tuple[SynType, ...]) -> None:
    extra_types = (*types, definition("both-values", "text-value", "integer"))
    a = contract("left", slot(value_type_ref=type_ref("text-value")))
    b = contract("right", slot(value_type_ref=type_ref("integer")))
    allowed = ember("intersection", value_type="both-values")
    data = bundle(
        extra_types,
        a,
        b,
        embers=(allowed,),
        instances=(EmberInstance.create(OBJECT, type_ref("child"), (binding(allowed),)),),
    )
    assert resolved_values(resolver(extra_types, data).resolve(OBJECT)) == ["intersection"]
    bad = ember("one-side")
    data = replace(
        data,
        embers=(bad,),
        instances=(EmberInstance.create(OBJECT, type_ref("child"), (binding(bad),)),),
    )
    with pytest.raises(InvalidInputError, match="represented value Type"):
        resolver(extra_types, data).resolve(OBJECT)


def test_projection_is_idempotent_and_owner_repr_does_not_print_private_bindings(
    types: tuple[SynType, ...],
) -> None:
    owner = private_bundle(types, "secret-repr")
    projected = owner.public_projection(types)
    assert projected.public_projection(types) == projected
    assert "secret-repr" not in repr(owner)
    assert "secret-repr" not in repr(owner.instances[0])


def test_empty_public_bundle_cannot_claim_owner_validation(types: tuple[SynType, ...]) -> None:
    public = bundle(types, instances=()).public_projection(types)
    checked = resolver(types, public)
    with pytest.raises(InvalidInputError, match="complete owner"):
        checked.validate_all(projection="owner")
    with pytest.raises(InvalidInputError, match="unknown effective"):
        checked.validate_all(projection="other")


def test_subjects_are_exact_and_object_referents_allow_checked_subtypes(
    types: tuple[SynType, ...],
) -> None:
    value = EmberRevision.create(
        "syn://ember/subject-reference",
        type_ref("property-ember"),
        type_ref("base"),
        OBJECT,
        value_kind="object_ref",
    )
    data = bundle(types, embers=(value,))
    checked = resolver(types, data)
    checked.validate_subject_types({OBJECT: "synny://type/child"})
    with pytest.raises(InvalidInputError, match="subject"):
        checked.validate_subject_types({OBJECT: type_ref("base")})
    with pytest.raises(InvalidInputError, match="subject"):
        checked.validate_subject_types({})
    no_subjects = resolver(types, replace(data, instances=()))
    with pytest.raises(InvalidInputError, match="referent is absent"):
        no_subjects.validate_subject_types({})
    with pytest.raises(InvalidInputError, match="referent Type"):
        no_subjects.validate_subject_types({OBJECT: type_ref("text-value")})


def test_explicit_empty_default_is_preserved_through_a_diamond(types: tuple[SynType, ...]) -> None:
    value = ember("cleared-default")
    base = contract("base", slot(minimum=0), defaults=(binding(value),))
    left = contract("left", defaults=(binding(),))
    data = bundle(types, base, left, embers=(value,))
    with pytest.raises(InvalidInputError, match="explicit compatible resolution"):
        resolver(types, data)
    child = contract("child", defaults=(binding(),))
    checked = resolver(types, replace(data, type_contracts=(base, left, child)))
    assert resolved_values(checked.resolve(OBJECT)) == []


def test_fixed_empty_default_cannot_be_replaced_by_type_or_instance(
    types: tuple[SynType, ...],
) -> None:
    value = ember("cannot-replace-empty")
    base = contract("base", slot(minimum=0, override="fixed"), defaults=(binding(),))
    data = bundle(types, base, embers=(value,))
    child = contract("child", defaults=(binding(value),))
    with pytest.raises(InvalidInputError, match="fixed default"):
        resolver(types, replace(data, type_contracts=(base, child)))
    instance = EmberInstance.create(OBJECT, type_ref("child"), (binding(value),))
    with pytest.raises(InvalidInputError, match="fixed inherited"):
        resolver(types, replace(data, instances=(instance,))).resolve(OBJECT)


def test_additive_parent_cannot_bypass_other_parents_fixed_default(
    types: tuple[SynType, ...],
) -> None:
    first, second = ember("fixed-parent-value"), ember("additive-parent-value")
    left = contract("left", slot(override="fixed", maximum=2), defaults=(binding(first),))
    right = contract("right", slot(override="append", maximum=2), defaults=(binding(second),))
    child = contract("child", defaults=(binding(first),))
    with pytest.raises(InvalidInputError, match="fixed default"):
        resolver(types, bundle(types, left, right, child, embers=(first, second)))


def test_incompatible_inherited_ranges_fail_even_without_instances(
    types: tuple[SynType, ...],
) -> None:
    left = contract("left", slot(value_type_ref=type_ref("text-value")))
    right = contract("right", slot(value_type_ref=type_ref("integer")))
    with pytest.raises(InvalidInputError, match="no compatible Type"):
        resolver(types, bundle(types, left, right, instances=()))


def test_empty_default_cannot_bypass_instance_origin_or_fixed_cardinality(
    types: tuple[SynType, ...],
) -> None:
    with pytest.raises(InvalidInputError, match="instance-only"):
        resolver(
            types, bundle(types, contract("base", slot(source="instance"), defaults=(binding(),)))
        )
    with pytest.raises(InvalidInputError, match="required cardinality"):
        resolver(
            types,
            bundle(
                types, contract("base", slot(override="fixed"), defaults=(binding(),)), instances=()
            ),
        )
