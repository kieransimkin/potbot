"""Versioned, scoped Type contracts and deterministic property-Ember resolution.

Contracts are checked views over an explicitly selected SYN catalog. They do not
rewrite that catalog, execute inherited code, admit implementations, or authorize
signing. Public views are derived by a positive projection and have their own
content identities; private binding identities are never exported in a public view.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, NoReturn, Self
from uuid import UUID

from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.domain.syn import SynRef, SynType, parse_type_ref, type_identity
from potbot.semantics.catalog import canonical_type_ref, catalog_fingerprint, type_ancestors

MAX_BUNDLE_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 10_000
MAX_SLOTS = 256
MAX_VALUES = 1024
MAX_EFFECTIVE_SLOTS = 100_000
MAX_VALUE_BYTES = 128 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_HANDLE = re.compile(r"secret://[a-z0-9][a-z0-9._/-]{0,1022}\Z", re.ASCII)
_SECRET_FIELDS = frozenset(
    {
        "privatedata",
        "privatekey",
        "extendedprivatekey",
        "secretkey",
        "seed",
        "entropy",
        "originalentropy",
        "mnemonic",
        "recoveryphrase",
        "recoverywords",
        "password",
        "passphrase",
        "chaincode",
        "vaultkey",
        "sessionkey",
    }
)
_SECRET_TYPES = frozenset(
    {
        "private-key",
        "extended-private-key",
        "secret-key",
        "entropy",
        "original-entropy",
        "mnemonic",
        "recovery-phrase",
        "recovery-words",
        "wallet-seed",
        "vault-key",
    }
)


def _fail(message: str) -> NoReturn:
    raise InvalidInputError(message)


def _fields(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        _fail(f"{label} fields do not match the Ember contract schema")
    assert isinstance(value, dict)
    return value


def _text(value: object, label: str, maximum: int = 1024) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _fail(f"{label} must be a bounded nonempty string")
    assert isinstance(value, str)
    if any(ord(c) < 32 for c in value):
        _fail(f"{label} cannot contain control characters")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        _fail("Ember contract revisions must be lowercase SHA-256 digests")
    assert isinstance(value, str)
    return value


def _array(value: object, label: str, maximum: int = MAX_RECORDS) -> list[Any]:
    if not isinstance(value, list) or len(value) > maximum:
        _fail(f"{label} must be a bounded JSON array")
    assert isinstance(value, list)
    return value


def _ref(value: object, kind: str | None = None) -> str:
    result = SynRef(value)
    if kind is not None and result.kind != kind:
        _fail(f"expected a SYN {kind} reference")
    return str(result)


def _type(value: object) -> str:
    return str(parse_type_ref(value))


def _key(value: object) -> str:
    return str(canonical_type_ref(value))


def _uuid(value: object, label: str) -> UUID:
    if not isinstance(value, str):
        _fail(f"{label} must be a canonical UUID string")
    try:
        parsed = UUID(str(value))
    except ValueError:
        _fail(f"{label} must be a canonical UUID string")
    if str(parsed) != value:
        _fail(f"{label} must be a canonical UUID string")
    return parsed


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate JSON member in Ember contract")
        result[key] = value
    return result


def _constant(_value: str) -> Any:
    _fail("non-finite JSON numbers are not permitted")


def _decode(value: str) -> Any:
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_BUNDLE_BYTES:
        _fail("Ember contract JSON exceeds its byte limit")
    # Bound nesting before JSON decoding, including pathological attacker input.
    depth, quoted, escaped = 0, False, False
    for character in value:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > 64:
                _fail("Ember contract JSON is excessively nested")
        elif character in "]}":
            depth -= 1
    try:
        return json.loads(value, object_pairs_hook=_unique, parse_constant=_constant)
    except InvalidInputError:
        raise
    except (ValueError, RecursionError):
        _fail("invalid Ember contract JSON")


def _no_secret_material(value: Any) -> None:
    """This view accepts operation handles, never raw key/recovery material.

    Positive projection is still essential: arbitrary mislabeled prose cannot be
    proven nonsecret by inspecting names. The producer owns classification; these
    checks additionally reject recognized secret fields at any nesting level.
    """
    if isinstance(value, dict):
        for name, child in value.items():
            normalized = re.sub(r"[^a-z]", "", name.lower())
            if normalized in _SECRET_FIELDS:
                _fail("raw secret material is not allowed in an Ember contract view")
            _no_secret_material(child)
    elif isinstance(value, list):
        for child in value:
            _no_secret_material(child)


@dataclass(frozen=True, slots=True)
class EmberContractScope:
    workspace_id: UUID
    namespace: str
    snapshot_id: UUID
    catalog_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, UUID) or not isinstance(self.snapshot_id, UUID):
            _fail("Ember contract scope requires checked workspace and snapshot UUIDs")
        _text(self.namespace, "namespace", 128)
        _digest(self.catalog_hash)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": str(self.workspace_id),
            "namespace": self.namespace,
            "snapshot_id": str(self.snapshot_id),
            "catalog_hash": self.catalog_hash,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = _fields(value, {"workspace_id", "namespace", "snapshot_id", "catalog_hash"}, "scope")
        return cls(
            _uuid(data["workspace_id"], "workspace_id"),
            data["namespace"],
            _uuid(data["snapshot_id"], "snapshot_id"),
            data["catalog_hash"],
        )


@dataclass(frozen=True, slots=True, order=True)
class EmberPointer:
    ref: str
    revision: str

    def __post_init__(self) -> None:
        _ref(self.ref, "ember")
        _digest(self.revision)

    def to_dict(self) -> dict[str, Any]:
        return {"ref": self.ref, "revision": self.revision}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = _fields(value, {"ref", "revision"}, "Ember pointer")
        return cls(data["ref"], data["revision"])


@dataclass(frozen=True, slots=True)
class EmberRevision:
    ref: str
    type_ref: str
    value_type_ref: str
    value_kind: str
    visibility: str
    value_json: str = field(repr=False)
    revision: str

    def __post_init__(self) -> None:
        _ref(self.ref, "ember")
        _type(self.type_ref)
        _type(self.value_type_ref)
        if self.value_kind not in {"json", "object_ref", "private_handle"}:
            _fail("unknown Ember value kind")
        if self.visibility not in {"public", "private"}:
            _fail("unknown Ember visibility")
        if not isinstance(self.value_json, str) or len(self.value_json.encode()) > MAX_VALUE_BYTES:
            _fail("Ember value exceeds its byte limit")
        value = _decode(self.value_json)
        if canonical_json(value) != self.value_json:
            _fail("Ember value_json must be canonical JSON")
        _no_secret_material(value)
        if self.value_kind == "object_ref":
            _ref(value)
        if self.value_kind == "private_handle":
            if (
                self.visibility != "private"
                or not isinstance(value, str)
                or not _HANDLE.fullmatch(value)
            ):
                _fail("private operation handles require private visibility and secret:// syntax")
        elif type_identity(self.value_type_ref) in _SECRET_TYPES:
            _fail("secret material Types require an owner-private operation handle")
        if self.revision != content_hash(self._body(normalize=True)):
            _fail("Ember revision does not match its immutable content")

    @property
    def pointer(self) -> EmberPointer:
        return EmberPointer(self.ref, self.revision)

    @property
    def value(self) -> Any:
        return _decode(self.value_json)

    def _body(self, *, normalize: bool = False) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "type_ref": _key(self.type_ref) if normalize else self.type_ref,
            "value_type_ref": _key(self.value_type_ref) if normalize else self.value_type_ref,
            "value_kind": self.value_kind,
            "visibility": self.visibility,
            "value": self.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._body(), "revision": self.revision}

    @classmethod
    def create(
        cls,
        ref: str,
        type_ref: str,
        value_type_ref: str,
        value: Any,
        *,
        value_kind: str = "json",
        visibility: str = "public",
    ) -> Self:
        encoded = canonical_json(value)
        body = {
            "ref": ref,
            "type_ref": _key(type_ref),
            "value_type_ref": _key(value_type_ref),
            "value_kind": value_kind,
            "visibility": visibility,
            "value": value,
        }
        return cls(
            ref, type_ref, value_type_ref, value_kind, visibility, encoded, content_hash(body)
        )

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = _fields(
            value,
            {
                "ref",
                "type_ref",
                "value_type_ref",
                "value_kind",
                "visibility",
                "value",
                "revision",
            },
            "Ember revision",
        )
        return cls(
            data["ref"],
            data["type_ref"],
            data["value_type_ref"],
            data["value_kind"],
            data["visibility"],
            canonical_json(data["value"]),
            data["revision"],
        )


@dataclass(frozen=True, slots=True)
class EmberSlot:
    slot_ref: str
    name: str
    ember_type_ref: str
    value_type_ref: str
    minimum: int = 0
    maximum: int = 1
    source: str = "effective"
    visibility: str = "public"
    override: str = "replace"

    def __post_init__(self) -> None:
        _ref(self.slot_ref, "thing")
        _text(self.name, "slot name", 255)
        _type(self.ember_type_ref)
        _type(self.value_type_ref)
        if (
            type(self.minimum) is not int
            or type(self.maximum) is not int
            or not 0 <= self.minimum <= self.maximum <= MAX_VALUES
        ):
            _fail("slot cardinality must satisfy 0 <= minimum <= maximum <= 1024")
        if self.source not in {"effective", "instance"}:
            _fail("slot source must be effective or instance")
        if self.visibility not in {"public", "private"}:
            _fail("unknown slot visibility")
        if self.override not in {"fixed", "replace", "append"}:
            _fail("unknown slot override rule")

    def to_dict(self, *, normalize: bool = False) -> dict[str, Any]:
        return {
            "slot_ref": self.slot_ref,
            "name": self.name,
            "ember_type_ref": _key(self.ember_type_ref) if normalize else self.ember_type_ref,
            "value_type_ref": _key(self.value_type_ref) if normalize else self.value_type_ref,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "source": self.source,
            "visibility": self.visibility,
            "override": self.override,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = _fields(
            value,
            {
                "slot_ref",
                "name",
                "ember_type_ref",
                "value_type_ref",
                "minimum",
                "maximum",
                "source",
                "visibility",
                "override",
            },
            "Ember slot",
        )
        return cls(**data)


@dataclass(frozen=True, slots=True)
class EmberBinding:
    slot_ref: str
    embers: tuple[EmberPointer, ...]

    def __post_init__(self) -> None:
        _ref(self.slot_ref, "thing")
        if not isinstance(self.embers, tuple) or len(self.embers) > MAX_VALUES:
            _fail("Ember bindings require a bounded immutable pointer tuple")
        if any(not isinstance(pointer, EmberPointer) for pointer in self.embers):
            _fail("Ember bindings require checked pointers")
        if len(set(self.embers)) != len(self.embers):
            _fail("an Ember binding cannot repeat the same revision")

    def to_dict(self) -> dict[str, Any]:
        return {"slot_ref": self.slot_ref, "embers": [p.to_dict() for p in sorted(self.embers)]}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = _fields(value, {"slot_ref", "embers"}, "Ember binding")
        return cls(
            data["slot_ref"],
            tuple(
                EmberPointer.from_dict(p)
                for p in _array(data["embers"], "binding Embers", MAX_VALUES)
            ),
        )


def _bindings(value: tuple[EmberBinding, ...]) -> list[dict[str, Any]]:
    return [binding.to_dict() for binding in sorted(value, key=lambda item: item.slot_ref)]


def _checked_bindings(value: object) -> None:
    if not isinstance(value, tuple) or len(value) > MAX_SLOTS:
        _fail("bindings require a bounded immutable tuple")
    if any(not isinstance(binding, EmberBinding) for binding in value):
        _fail("bindings require checked EmberBinding records")
    if len({binding.slot_ref for binding in value}) != len(value):
        _fail("duplicate property slot binding")


@dataclass(frozen=True, slots=True)
class TypeEmberContract:
    type_ref: str
    slots: tuple[EmberSlot, ...]
    defaults: tuple[EmberBinding, ...] = field(repr=False)
    revision: str

    def __post_init__(self) -> None:
        _type(self.type_ref)
        if not isinstance(self.slots, tuple) or len(self.slots) > MAX_SLOTS:
            _fail("Type slots require a bounded immutable tuple")
        if any(not isinstance(slot, EmberSlot) for slot in self.slots):
            _fail("Type slots require checked EmberSlot records")
        if len({slot.slot_ref for slot in self.slots}) != len(self.slots):
            _fail("duplicate Type slot declaration")
        _checked_bindings(self.defaults)
        if self.revision != content_hash(self._body(normalize=True)):
            _fail("Type contract revision does not match its immutable content")

    def _body(self, *, normalize: bool = False) -> dict[str, Any]:
        return {
            "type_ref": _key(self.type_ref) if normalize else self.type_ref,
            "slots": [
                s.to_dict(normalize=normalize) for s in sorted(self.slots, key=lambda s: s.slot_ref)
            ],
            "defaults": _bindings(self.defaults),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._body(), "revision": self.revision}

    @classmethod
    def create(
        cls,
        type_ref: str,
        slots: Iterable[EmberSlot] = (),
        defaults: Iterable[EmberBinding] = (),
    ) -> Self:
        declarations, bindings = tuple(slots), tuple(defaults)
        body = {
            "type_ref": _key(type_ref),
            "slots": [
                s.to_dict(normalize=True) for s in sorted(declarations, key=lambda s: s.slot_ref)
            ],
            "defaults": _bindings(bindings),
        }
        return cls(type_ref, declarations, bindings, content_hash(body))

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = _fields(value, {"type_ref", "slots", "defaults", "revision"}, "Type contract")
        return cls(
            data["type_ref"],
            tuple(EmberSlot.from_dict(v) for v in _array(data["slots"], "slots", MAX_SLOTS)),
            tuple(
                EmberBinding.from_dict(v) for v in _array(data["defaults"], "defaults", MAX_SLOTS)
            ),
            data["revision"],
        )


@dataclass(frozen=True, slots=True)
class EmberInstance:
    object_ref: str
    type_ref: str
    bindings: tuple[EmberBinding, ...] = field(repr=False)
    revision: str

    def __post_init__(self) -> None:
        if SynRef(self.object_ref).kind == "type":
            _fail("a native Type is a class declaration, not an ordinary Ember-contract instance")
        _type(self.type_ref)
        _checked_bindings(self.bindings)
        if self.revision != content_hash(self._body(normalize=True)):
            _fail("instance revision does not match its immutable content")

    def _body(self, *, normalize: bool = False) -> dict[str, Any]:
        return {
            "object_ref": self.object_ref,
            "type_ref": _key(self.type_ref) if normalize else self.type_ref,
            "bindings": _bindings(self.bindings),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._body(), "revision": self.revision}

    @classmethod
    def create(cls, object_ref: str, type_ref: str, bindings: Iterable[EmberBinding] = ()) -> Self:
        selected = tuple(bindings)
        return cls(
            object_ref,
            type_ref,
            selected,
            content_hash(
                {
                    "object_ref": object_ref,
                    "type_ref": _key(type_ref),
                    "bindings": _bindings(selected),
                }
            ),
        )

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = _fields(value, {"object_ref", "type_ref", "bindings", "revision"}, "instance")
        return cls(
            data["object_ref"],
            data["type_ref"],
            tuple(
                EmberBinding.from_dict(v) for v in _array(data["bindings"], "bindings", MAX_SLOTS)
            ),
            data["revision"],
        )


@dataclass(frozen=True, slots=True)
class EmberContractBundle:
    scope: EmberContractScope
    type_contracts: tuple[TypeEmberContract, ...] = field(repr=False)
    embers: tuple[EmberRevision, ...] = field(repr=False)
    instances: tuple[EmberInstance, ...] = field(repr=False)
    projection: str = "owner"
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            _fail("unsupported Ember contract schema_version")
        if not isinstance(self.scope, EmberContractScope):
            _fail("Ember contract bundle requires checked scope")
        if self.projection not in {"owner", "public"}:
            _fail("unknown Ember bundle projection")
        for records, expected in (
            (self.type_contracts, TypeEmberContract),
            (self.embers, EmberRevision),
            (self.instances, EmberInstance),
        ):
            if not isinstance(records, tuple) or len(records) > MAX_RECORDS:
                _fail("Ember bundle collections must be bounded immutable tuples")
            if any(not isinstance(record, expected) for record in records):
                _fail("Ember bundle contains an unchecked record")
        if len({_key(c.type_ref) for c in self.type_contracts}) != len(self.type_contracts):
            _fail("duplicate Type contract identity, including scheme aliases")
        if len({e.pointer for e in self.embers}) != len(self.embers):
            _fail("duplicate Ember revision")
        if len({i.object_ref for i in self.instances}) != len(self.instances):
            _fail("duplicate Ember-contract instance identity")
        if self.projection == "public" and any(e.visibility != "public" for e in self.embers):
            _fail("public Ember bundles cannot contain private values")
        if len(canonical_json(self.to_dict()).encode()) > MAX_BUNDLE_BYTES:
            _fail("Ember contract bundle exceeds its byte limit")

    def to_dict(self, *, normalize: bool = False) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "projection": self.projection,
            "scope": self.scope.to_dict(),
            "type_contracts": [
                {**c._body(normalize=normalize), "revision": c.revision}
                for c in sorted(self.type_contracts, key=lambda c: _key(c.type_ref))
            ],
            "embers": [
                {**e._body(normalize=normalize), "revision": e.revision}
                for e in sorted(self.embers, key=lambda e: e.pointer)
            ],
            "instances": [
                {**i._body(normalize=normalize), "revision": i.revision}
                for i in sorted(self.instances, key=lambda i: i.object_ref)
            ],
        }

    @property
    def fingerprint(self) -> str:
        return content_hash(self.to_dict(normalize=True))

    def to_json(self) -> str:
        """Serialize this bundle as explicitly classified; owner bundles are private."""
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = _fields(
            value,
            {
                "schema_version",
                "projection",
                "scope",
                "type_contracts",
                "embers",
                "instances",
            },
            "Ember bundle",
        )
        return cls(
            EmberContractScope.from_dict(data["scope"]),
            tuple(
                TypeEmberContract.from_dict(v) for v in _array(data["type_contracts"], "contracts")
            ),
            tuple(EmberRevision.from_dict(v) for v in _array(data["embers"], "Embers")),
            tuple(EmberInstance.from_dict(v) for v in _array(data["instances"], "instances")),
            data["projection"],
            data["schema_version"],
        )

    @classmethod
    def from_json(cls, value: str) -> Self:
        return cls.from_dict(_decode(value))

    def public_projection(self, types: Iterable[SynType]) -> EmberContractBundle:
        return EmberContractResolver(types, self, expected_scope=self.scope).public_projection()


@dataclass(frozen=True, slots=True)
class _ResolvedSlot:
    declarations: tuple[tuple[str, str, EmberSlot], ...]
    defaults: tuple[EmberPointer, ...]
    default_sources: tuple[tuple[EmberPointer, tuple[str, ...]], ...]
    defaults_declared: bool
    minimum: int
    maximum: int
    source: str
    visibility: str
    override: str


@dataclass(frozen=True, slots=True)
class EffectiveEmberInstance:
    """A detached immutable result. Public results carry no private identities."""

    payload_json: str = field(repr=False)

    def to_dict(self) -> dict[str, Any]:
        result = _decode(self.payload_json)
        assert isinstance(result, dict)
        return result

    def to_json(self) -> str:
        return self.payload_json

    @property
    def fingerprint(self) -> str:
        return content_hash(self.to_dict())


class EmberContractResolver:
    """Resolve against a checked scope supplied by the authenticated enclosing service."""

    def __init__(
        self,
        types: Iterable[SynType],
        bundle: EmberContractBundle,
        *,
        expected_scope: EmberContractScope,
    ) -> None:
        if not isinstance(bundle, EmberContractBundle) or not isinstance(
            expected_scope, EmberContractScope
        ):
            _fail("Ember resolver requires checked bundle and expected scope")
        if bundle.scope != expected_scope:
            _fail("Ember contract scope does not match the selected workspace/namespace/snapshot")
        self.types = tuple(types)
        if catalog_fingerprint(self.types) != expected_scope.catalog_hash:
            _fail("Ember contract catalog hash does not match the selected catalog")
        self.bundle = bundle
        self.ancestors = type_ancestors(self.types)
        self._types = {_key(t.ref): t for t in self.types}
        self._contracts = {_key(c.type_ref): c for c in bundle.type_contracts}
        self._embers = {e.pointer: e for e in bundle.embers}
        self._instances = {i.object_ref: i for i in bundle.instances}
        self._effective: dict[str, dict[str, _ResolvedSlot]] = {}
        self._range_compatibility: dict[tuple[str, ...], bool] = {}
        self._validate_references()
        self._build_contracts()

    def _known_type(self, reference: str) -> None:
        if _key(reference) not in self._types:
            _fail("Ember contract refers to a Type absent from the selected catalog")

    def _subtype(self, actual: str, required: str) -> bool:
        return _key(required) in self.ancestors[_key(actual)]

    def _validate_references(self) -> None:
        for contract in self.bundle.type_contracts:
            self._known_type(contract.type_ref)
            for slot in contract.slots:
                self._known_type(slot.ember_type_ref)
                self._known_type(slot.value_type_ref)
        for ember in self.bundle.embers:
            self._known_type(ember.type_ref)
            self._known_type(ember.value_type_ref)
            secret_ancestry = {
                type_identity(ref)
                for assigned in (ember.type_ref, ember.value_type_ref)
                for ref in self.ancestors[_key(assigned)]
            } & _SECRET_TYPES
            if secret_ancestry and ember.value_kind != "private_handle":
                _fail("secret material Type ancestry requires a private operation handle")
        for instance in self.bundle.instances:
            self._known_type(instance.type_ref)
        records: tuple[TypeEmberContract | EmberInstance, ...] = (
            *self.bundle.type_contracts,
            *self.bundle.instances,
        )
        for record in records:
            bindings = record.defaults if isinstance(record, TypeEmberContract) else record.bindings
            for binding in bindings:
                for pointer in binding.embers:
                    if pointer not in self._embers:
                        _fail("Ember binding has an unresolved or mismatched revision")

    def _check_narrowing(self, child: EmberSlot, parent: EmberSlot) -> None:
        if (
            child.minimum < parent.minimum
            or child.maximum > parent.maximum
            or not self._subtype(child.ember_type_ref, parent.ember_type_ref)
            or not self._subtype(child.value_type_ref, parent.value_type_ref)
            or (parent.source == "instance" and child.source != "instance")
            or (parent.visibility == "private" and child.visibility != "private")
            or (parent.override == "fixed" and child.override != "fixed")
            or (child.override not in {"fixed", parent.override})
        ):
            _fail("subtype Ember declaration weakens an inherited property contract")

    def _check_value(self, pointer: EmberPointer, slot: _ResolvedSlot, *, default: bool) -> None:
        ember = self._embers[pointer]
        if default and (slot.source == "instance" or ember.value_kind == "private_handle"):
            _fail("instance-only properties and private operation handles cannot be Type defaults")
        if slot.visibility == "public" and ember.visibility != "public":
            _fail("a public property cannot bind a private Ember")
        for _source_ref, _revision, declaration in slot.declarations:
            if not self._subtype(ember.type_ref, declaration.ember_type_ref) or not self._subtype(
                ember.value_type_ref, declaration.value_type_ref
            ):
                _fail("Ember Type or represented value Type violates the property contract")

    def _compatible_ranges(self, required: tuple[str, ...]) -> bool:
        if len(required) < 2:
            return True
        if required not in self._range_compatibility:
            self._range_compatibility[required] = any(
                all(ref in ancestors for ref in required) for ancestors in self.ancestors.values()
            )
        return self._range_compatibility[required]

    def _combine_slot(
        self,
        type_ref: str,
        slot_ref: str,
        parents: list[_ResolvedSlot],
        own: EmberSlot | None,
        default: EmberBinding | None,
    ) -> _ResolvedSlot:
        declarations: dict[tuple[str, str], EmberSlot] = {}
        for parent in parents:
            for source, revision, declaration in parent.declarations:
                declarations[(source, revision)] = declaration
        if own is not None:
            for declaration in declarations.values():
                self._check_narrowing(own, declaration)
            declarations[(type_ref, self._contracts[type_ref].revision)] = own
        if not declarations:
            _fail("a default or binding refers to an undeclared Ember property")
        ordered = tuple((ref, revision, s) for (ref, revision), s in sorted(declarations.items()))
        values = tuple(declarations.values())
        for required in (
            tuple(sorted({_key(s.ember_type_ref) for s in values})),
            tuple(sorted({_key(s.value_type_ref) for s in values})),
        ):
            if not self._compatible_ranges(required):
                _fail("inherited Ember Type constraints have no compatible Type in the catalog")
        minimum = max(s.minimum for s in values)
        maximum = min(s.maximum for s in values)
        if minimum > maximum:
            _fail("inherited Ember property cardinalities conflict")
        source = "instance" if any(s.source == "instance" for s in values) else "effective"
        visibility = "private" if any(s.visibility == "private" for s in values) else "public"
        overrides = {s.override for s in values}
        if "fixed" in overrides:
            override = "fixed"
        elif len(overrides) != 1:
            _fail("inherited Ember override rules conflict; use an explicit fixed restriction")
        else:
            override = next(iter(overrides))
        defaults_declared = default is not None or any(p.defaults_declared for p in parents)
        if source == "instance" and defaults_declared:
            _fail("instance-only properties cannot declare or inherit Type defaults")
        variants = {p.defaults for p in parents if p.defaults_declared}
        inherited = set().union(*(set(v) for v in variants)) if variants else set()
        sources: dict[EmberPointer, set[str]] = {}
        for parent in parents:
            for pointer, supplying_types in parent.default_sources:
                sources.setdefault(pointer, set()).update(supplying_types)
        parent_fixed = [p for p in parents if p.override == "fixed" and p.defaults_declared]
        if default is not None:
            provided = set(default.embers)
            if any(p.override == "append" for p in parents):
                selected = inherited | provided
            else:
                selected = provided
            sources = {p: sources.get(p, set()) for p in selected}
            for pointer in provided:
                sources[pointer].add(type_ref)
        else:
            if len(variants) > 1 and override != "append":
                _fail(
                    "inherited default Embers conflict; an explicit compatible resolution is required"
                )
            selected = inherited
        for parent in parent_fixed:
            if set(parent.defaults) != selected:
                _fail("a subtype cannot change an inherited fixed default")
        if override == "fixed" and defaults_declared and len(selected) < minimum:
            _fail("fixed inherited defaults cannot satisfy the required cardinality")
        result = _ResolvedSlot(
            ordered,
            tuple(sorted(selected)),
            tuple((p, tuple(sorted(sources[p]))) for p in sorted(selected)),
            defaults_declared,
            minimum,
            maximum,
            source,
            visibility,
            override,
        )
        if len(selected) > maximum:
            _fail("inherited default Embers exceed the property's maximum cardinality")
        for pointer in result.defaults:
            self._check_value(pointer, result, default=True)
        return result

    def _build_contracts(self) -> None:
        total = 0
        for ref in sorted(self._types, key=lambda ref: (len(self.ancestors[ref]), ref)):
            parent_states = [self._effective[_key(p)] for p in self._types[ref].parents]
            contract = self._contracts.get(ref)
            own = {} if contract is None else {s.slot_ref: s for s in contract.slots}
            defaults = {} if contract is None else {b.slot_ref: b for b in contract.defaults}
            slot_ids = set(own) | set(defaults)
            for parent in parent_states:
                slot_ids.update(parent)
            if len(slot_ids) > MAX_SLOTS:
                _fail("effective Type contract exceeds its property count limit")
            total += len(slot_ids)
            if total > MAX_EFFECTIVE_SLOTS:
                _fail("effective Ember contract closure exceeds its size limit")
            self._effective[ref] = {
                slot_ref: self._combine_slot(
                    ref,
                    slot_ref,
                    [p[slot_ref] for p in parent_states if slot_ref in p],
                    own.get(slot_ref),
                    defaults.get(slot_ref),
                )
                for slot_ref in sorted(slot_ids)
            }
        for instance in self.bundle.instances:
            slots = self._effective[_key(instance.type_ref)]
            if any(binding.slot_ref not in slots for binding in instance.bindings):
                _fail("instance binds an undeclared Ember property")
            if self.bundle.projection == "public" and any(
                slots[binding.slot_ref].visibility == "private" for binding in instance.bindings
            ):
                _fail("public projection cannot include private property bindings")
        if self.bundle.projection == "public":
            for contract in self.bundle.type_contracts:
                slots = self._effective[_key(contract.type_ref)]
                if any(slots[b.slot_ref].visibility == "private" for b in contract.defaults):
                    _fail("public projection cannot include private default bindings")

    def resolve(self, object_ref: str, *, projection: str = "owner") -> EffectiveEmberInstance:
        if projection not in {"owner", "public"}:
            _fail("unknown effective Ember projection")
        if projection == "owner" and self.bundle.projection == "public":
            _fail("a public projection cannot establish complete owner conformance")
        _ref(object_ref)
        if object_ref not in self._instances:
            _fail("Ember-contract instance is absent from the selected bundle")
        instance = self._instances[object_ref]
        supplied = {binding.slot_ref: binding for binding in instance.bindings}
        output: list[dict[str, Any]] = []
        for slot_ref, slot in self._effective[_key(instance.type_ref)].items():
            if projection == "public" and slot.visibility == "private":
                continue
            binding = supplied.get(slot_ref)
            inherited = set(slot.defaults)
            if binding is None:
                selected = inherited
                origin = "inherited"
            else:
                own = set(binding.embers)
                if slot.override == "fixed" and slot.defaults_declared and own != inherited:
                    _fail("instance attempts to replace a fixed inherited Ember")
                selected = inherited | own if slot.override == "append" else own
                origin = "instance" if not inherited or slot.override != "append" else "combined"
            if slot.source == "instance" and binding is None:
                selected = set()
            if not slot.minimum <= len(selected) <= slot.maximum:
                _fail("effective Ember binding cardinality violates its property contract")
            for pointer in selected:
                self._check_value(pointer, slot, default=False)
            provenance: list[dict[str, Any]] = []
            for pointer in sorted(selected):
                sources = (
                    dict(slot.default_sources).get(pointer, ()) if pointer in inherited else ()
                )
                entry: dict[str, Any] = {
                    "ember": self._embers[pointer].to_dict(),
                    "source_types": list(sources),
                    "instance_bound": binding is not None and pointer in binding.embers,
                }
                provenance.append(entry)
            row: dict[str, Any] = {
                "slot_ref": slot_ref,
                "minimum": slot.minimum,
                "maximum": slot.maximum,
                "source": slot.source,
                "override": slot.override,
                "origin": origin,
                "values": provenance,
                "declarations": [
                    {"type_ref": ref, **({"revision": revision} if projection == "owner" else {})}
                    for ref, revision, _s in slot.declarations
                ],
            }
            output.append(row)
        result: dict[str, Any] = {
            "schema_version": 1,
            "projection": projection,
            "scope": self.bundle.scope.to_dict(),
            "object_ref": instance.object_ref,
            "type_ref": _key(instance.type_ref),
            "conformance": "complete" if projection == "owner" else "public_only",
            "slots": output,
        }
        if projection == "owner":
            result["instance_revision"] = instance.revision
            result["bundle_revision"] = self.bundle.fingerprint
        return EffectiveEmberInstance(canonical_json(result))

    def validate_all(self, *, projection: str = "owner") -> tuple[EffectiveEmberInstance, ...]:
        if projection not in {"owner", "public"}:
            _fail("unknown effective Ember projection")
        if projection == "owner" and self.bundle.projection == "public":
            _fail("a public projection cannot establish complete owner conformance")
        return tuple(self.resolve(ref, projection=projection) for ref in sorted(self._instances))

    def validate_subject_types(self, object_types: Mapping[str, str]) -> None:
        """Check subjects and represented referents against one selected snapshot.

        A subject's direct Type must be the snapshot's asserted direct Type. An
        object-valued Ember may require an ancestor Type of its actual referent.
        Ember revision records live in this bundle; private records need not be
        copied into public SYN artifacts to participate in property resolution.
        """
        for instance in self.bundle.instances:
            actual = object_types.get(instance.object_ref)
            if actual is None or _key(actual) != _key(instance.type_ref):
                _fail("an Ember subject differs from the selected snapshot")
        for ember in self.bundle.embers:
            if ember.value_kind != "object_ref":
                continue
            actual = object_types.get(ember.value)
            if actual is None:
                _fail("an Ember referent is absent from the selected snapshot")
            self._known_type(actual)
            if not self._subtype(actual, ember.value_type_ref):
                _fail("an Ember referent Type differs from the selected snapshot")

    def public_projection(self) -> EmberContractBundle:
        """Produce a parseable public bundle with no private IDs or owner digests."""
        contracts: list[TypeEmberContract] = []
        instances: list[EmberInstance] = []
        visible: set[EmberPointer] = set()
        for contract in self.bundle.type_contracts:
            slots = self._effective[_key(contract.type_ref)]
            defaults = tuple(
                b for b in contract.defaults if slots[b.slot_ref].visibility == "public"
            )
            for binding in defaults:
                visible.update(binding.embers)
            contracts.append(TypeEmberContract.create(contract.type_ref, contract.slots, defaults))
        for instance in self.bundle.instances:
            slots = self._effective[_key(instance.type_ref)]
            bindings = tuple(
                b for b in instance.bindings if slots[b.slot_ref].visibility == "public"
            )
            for binding in bindings:
                visible.update(binding.embers)
            instances.append(EmberInstance.create(instance.object_ref, instance.type_ref, bindings))
        result = EmberContractBundle(
            self.bundle.scope,
            tuple(contracts),
            tuple(self._embers[p] for p in sorted(visible)),
            tuple(instances),
            "public",
        )
        # Validate only public conformance. Missing private bindings are not exposed.
        EmberContractResolver(self.types, result, expected_scope=result.scope).validate_all(
            projection="public"
        )
        return result


def build_bundle(
    types: Iterable[SynType],
    *,
    workspace_id: UUID,
    namespace: str,
    snapshot_id: UUID,
    type_contracts: Iterable[TypeEmberContract] = (),
    embers: Iterable[EmberRevision] = (),
    instances: Iterable[EmberInstance] = (),
) -> EmberContractBundle:
    """Build and fully validate an owner bundle with a computed catalog pin."""
    selected = tuple(types)
    scope = EmberContractScope(workspace_id, namespace, snapshot_id, catalog_fingerprint(selected))
    bundle = EmberContractBundle(scope, tuple(type_contracts), tuple(embers), tuple(instances))
    EmberContractResolver(selected, bundle, expected_scope=scope).validate_all()
    return bundle
