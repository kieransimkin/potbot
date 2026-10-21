"""Lossless, bounded records for explicitly selected local SYN snapshots.

SYN references have no instance or workspace component. Parsing one proves only
its syntax; resolution and authorization require the caller's separate scope.
Public payloads remain exact JSON strings, including numeric spellings and
whitespace. No SYN package, database, network, or credential service is imported.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Self

from .errors import InvalidInputError
from .records import JsonRecord, _nonempty, _version
from .serialization import canonical_json

SYN_KINDS = frozenset(
    {"type", "thing", "event", "ember", "cog", "credential", "enigma", "pondle", "snug", "om"}
)
MAX_SYN_TYPES = 10_000
MAX_SYN_OBJECTS = 100_000
MAX_SYN_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_SYN_PAYLOAD_BYTES = 2_000_000
MAX_SYN_METADATA_BYTES = 2_000_000
MAX_SYN_SOURCE_LABEL_LENGTH = 2_000
MAX_SYN_JSON_DEPTH = 128
_MAX_BIGINT = 2**63 - 1
_PRIVATE_FIELDS = frozenset({"privateData", "private_data"})
_LOCAL_REF = re.compile(
    r"syn://(?P<kind>type|thing|event|ember|cog|credential|enigma|pondle|snug|om)/"
    r"(?P<slug>[a-z0-9]+(?:[-_][a-z0-9]+)*)",
    re.ASCII,
)
_SYNNY_REF = re.compile(r"synny://type/(?P<slug>[a-z0-9]+(?:[-_][a-z0-9]+)*)", re.ASCII)


def _utf8_size(value: object, field: str, limit: int | None = None) -> int:
    if not isinstance(value, str):
        raise InvalidInputError(f"{field} must be a UTF-8 string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise InvalidInputError(f"{field} must be valid UTF-8") from error
    if limit is not None and size > limit:
        raise InvalidInputError(f"{field} exceeds its {limit}-byte limit")
    return size


def _text(
    value: object, field: str, maximum: int | None = None, *, allow_nul: bool = False
) -> None:
    _nonempty(value, field)
    _utf8_size(value, field)
    if not allow_nul and isinstance(value, str) and "\x00" in value:
        raise InvalidInputError(f"{field} cannot contain U+0000 in a PostgreSQL scalar")
    if maximum is not None and isinstance(value, str) and len(value) > maximum:
        raise InvalidInputError(f"{field} exceeds its {maximum}-character limit")


def _fields(data: object, expected: set[str]) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != expected:
        raise InvalidInputError("SYN record fields do not match the declared schema")
    return data


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise InvalidInputError("SYN records cannot contain duplicate JSON object keys")
        result[name] = value
    return result


def _public_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = _unique_object(pairs)
    if _PRIVATE_FIELDS.intersection(result):
        raise InvalidInputError("SYN public JSON cannot contain private credential fields")
    return result


def _reject_constant(value: str) -> Any:
    raise InvalidInputError("SYN JSON cannot contain non-finite numeric constants")


def _finite_decimal(value: str) -> Decimal:
    result = Decimal(value)
    if not result.is_finite():
        raise InvalidInputError("SYN JSON numbers must be finite")
    return result


def _check_json_depth(value: str) -> None:
    """Bound nesting before decoding, ignoring structural characters in strings."""
    depth = 0
    in_string = False
    escaped = False
    for character in value:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_SYN_JSON_DEPTH:
                raise InvalidInputError(
                    f"SYN JSON is excessively nested; maximum depth is {MAX_SYN_JSON_DEPTH}"
                )
        elif character in "]}":
            depth -= 1


def _load_json(value: str, *, public: bool) -> Any:
    _check_json_depth(value)
    try:
        return json.loads(
            value,
            object_pairs_hook=_public_object if public else _unique_object,
            parse_int=_finite_decimal if public else int,
            parse_float=_finite_decimal,
            parse_constant=_reject_constant,
        )
    except InvalidInputError:
        raise
    except (ValueError, InvalidOperation, RecursionError) as error:
        raise InvalidInputError("invalid or excessively nested SYN JSON") from error


def _public_json(value: object, field: str, limit: int) -> dict[str, Any]:
    _utf8_size(value, field, limit)
    if not isinstance(value, str):  # Keep the type boundary explicit for static checking.
        raise InvalidInputError(f"{field} must be a JSON string")
    parsed = _load_json(value, public=True)
    if not isinstance(parsed, dict):
        raise InvalidInputError(f"{field} must contain a JSON object")
    return parsed


class SynRef(str):
    """An exact local ``syn://kind/slug`` reference, with a 1..255 character slug.

    This boundary matches SYN's SQL slug and HTTP type-path representation. Its
    upstream Python ObjectRef constructor alone accepts longer slugs; Potbot does
    not. Parsing never performs the lossy normalization of ObjectRef.local().
    """

    __slots__ = ()

    def __new__(cls, value: object) -> Self:
        if not isinstance(value, str):
            raise InvalidInputError("SYN reference must be a string")
        if len(value) > len("syn://credential/") + 255:
            raise InvalidInputError(
                "invalid local SYN reference or slug longer than 255 characters"
            )
        match = _LOCAL_REF.fullmatch(value)
        if match is None or len(match.group("slug")) > 255:
            raise InvalidInputError(
                "invalid local SYN reference or slug longer than 255 characters"
            )
        return str.__new__(cls, value)

    @property
    def kind(self) -> str:
        return self.split("/", 3)[2]

    @property
    def slug(self) -> str:
        return self.split("/", 3)[3]


class SynnyRef(str):
    """An exact ``synny://type/<slug>`` spelling of a Type reference.

    The same slug under ``syn://type/`` identifies the same type within a scoped
    catalog. Parsing preserves spelling; use ``type_identity`` for comparison.
    Neither scheme supplies workspace scope, resolution, or authority.
    """

    __slots__ = ()

    def __new__(cls, value: object) -> Self:
        if not isinstance(value, str):
            raise InvalidInputError("Synny reference must be a string")
        if len(value) > len("synny://type/") + 255:
            raise InvalidInputError(
                "invalid Synny type reference or slug longer than 255 characters"
            )
        match = _SYNNY_REF.fullmatch(value)
        if match is None or len(match.group("slug")) > 255:
            raise InvalidInputError(
                "invalid Synny type reference or slug longer than 255 characters"
            )
        return str.__new__(cls, value)

    @property
    def kind(self) -> str:
        return "type"

    @property
    def slug(self) -> str:
        return self.split("/", 3)[3]


def parse_ref(value: object) -> SynRef | SynnyRef:
    """Parse one exact SYN or Synny reference without changing its spelling."""
    if isinstance(value, str) and value.startswith("synny://"):
        return SynnyRef(value)
    return SynRef(value)


def parse_type_ref(value: object) -> SynRef | SynnyRef:
    """Require a Type reference in either supported scheme."""
    ref = parse_ref(value)
    if ref.kind != "type":
        raise InvalidInputError("reference must identify a SYN or Synny type")
    return ref


def type_identity(value: object) -> str:
    """Return the scheme-independent Type slug for one selected catalog.

    This key does not add namespace or snapshot scope. Source records retain their
    exact reference strings; the catalog chooses each identity's preferred spelling.
    """
    return parse_type_ref(value).slug


def _typed_ref(value: object, field: str, *, type_only: bool = False) -> None:
    if not isinstance(value, (SynRef, SynnyRef)):
        raise InvalidInputError(f"{field} requires a typed SynRef or SynnyRef")
    if type_only and value.kind != "type":
        raise InvalidInputError(f"{field} must refer to a SYN or Synny type")


class _SynRecord(JsonRecord):
    __slots__ = ()

    def to_json(self) -> str:
        value = canonical_json(self.to_dict())
        _utf8_size(value, "SYN record", MAX_SYN_SNAPSHOT_BYTES)
        return value

    @classmethod
    def from_json(cls, value: str) -> Self:
        _utf8_size(value, "SYN JSON envelope", MAX_SYN_SNAPSHOT_BYTES)
        return cls.from_dict(_load_json(value, public=False))


@dataclass(frozen=True, slots=True)
class SynProperty(_SynRecord):
    name: str
    type_ref: SynRef | SynnyRef
    minimum: int = 1
    maximum: int = 1

    def __post_init__(self) -> None:
        _text(self.name, "property name", 1024)
        _typed_ref(self.type_ref, "property type_ref", type_only=True)
        if (
            type(self.minimum) is not int
            or type(self.maximum) is not int
            or not 0 <= self.minimum <= self.maximum <= _MAX_BIGINT
        ):
            raise InvalidInputError(
                "SYN property cardinality requires finite nonnegative bigint bounds"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type_ref": str(self.type_ref),
            "minimum": self.minimum,
            "maximum": self.maximum,
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        value = _fields(data, {"name", "type_ref", "minimum", "maximum"})
        return cls(
            name=value["name"],
            type_ref=parse_type_ref(value["type_ref"]),
            minimum=value["minimum"],
            maximum=value["maximum"],
        )


@dataclass(frozen=True, slots=True)
class SynType(_SynRecord):
    ref: SynRef | SynnyRef
    name: str
    parents: tuple[SynRef | SynnyRef, ...]
    definition: str | None
    properties: tuple[SynProperty, ...]
    metadata_json: str = "{}"

    def __post_init__(self) -> None:
        _typed_ref(self.ref, "type ref", type_only=True)
        _text(self.name, "type name")
        if not isinstance(self.parents, tuple):
            raise InvalidInputError("SYN type parents must be a tuple of typed references")
        for parent in self.parents:
            _typed_ref(parent, "parent ref", type_only=True)
        if len({type_identity(parent) for parent in self.parents}) != len(self.parents):
            raise InvalidInputError("SYN type contains duplicate parent refs")
        if self.definition is not None:
            _utf8_size(self.definition, "type definition")
            if "\x00" in self.definition:
                raise InvalidInputError(
                    "type definition cannot contain U+0000 in a PostgreSQL scalar"
                )
        if not isinstance(self.properties, tuple) or any(
            not isinstance(prop, SynProperty) for prop in self.properties
        ):
            raise InvalidInputError("SYN type properties must be a tuple of SynProperty values")
        if len({prop.name for prop in self.properties}) != len(self.properties):
            raise InvalidInputError("SYN type contains duplicate property names")
        _public_json(self.metadata_json, "metadata_json", MAX_SYN_METADATA_BYTES)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": str(self.ref),
            "name": self.name,
            "parents": [str(parent) for parent in self.parents],
            "definition": self.definition,
            "properties": [prop.to_dict() for prop in self.properties],
            "metadata_json": self.metadata_json,
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        value = _fields(
            data, {"ref", "name", "parents", "definition", "properties", "metadata_json"}
        )
        if not isinstance(value["parents"], list) or not isinstance(value["properties"], list):
            raise InvalidInputError("SYN parents and properties must be JSON arrays")
        return cls(
            ref=parse_type_ref(value["ref"]),
            name=value["name"],
            parents=tuple(parse_type_ref(parent) for parent in value["parents"]),
            definition=value["definition"],
            properties=tuple(SynProperty.from_dict(prop) for prop in value["properties"]),
            metadata_json=value["metadata_json"],
        )


@dataclass(frozen=True, slots=True)
class SynObject(_SynRecord):
    ref: SynRef
    type_ref: SynRef | SynnyRef
    payload_json: str

    def __post_init__(self) -> None:
        if not isinstance(self.ref, SynRef):
            raise InvalidInputError("object ref requires a native typed SynRef")
        if self.ref.kind == "type":
            raise InvalidInputError("SYN types are definitions, not instance objects")
        _typed_ref(self.type_ref, "object type_ref", type_only=True)
        payload = _public_json(self.payload_json, "payload_json", MAX_SYN_PAYLOAD_BYTES)
        if payload.get("id") != self.ref or type_identity(payload.get("typeRef")) != type_identity(
            self.type_ref
        ):
            raise InvalidInputError("SYN public payload id/typeRef must match its typed references")
        # The decoded name is inspected only; its exact JSON text is persisted.
        # An escaped U+0000 inside that public JSON string is therefore safe.
        _text(payload.get("name"), "public payload name", allow_nul=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": str(self.ref),
            "type_ref": str(self.type_ref),
            "payload_json": self.payload_json,
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        value = _fields(data, {"ref", "type_ref", "payload_json"})
        return cls(
            ref=SynRef(value["ref"]),
            type_ref=parse_type_ref(value["type_ref"]),
            payload_json=value["payload_json"],
        )


@dataclass(frozen=True, slots=True)
class SynSnapshot(_SynRecord):
    source_label: str
    catalog_mode: str
    types: tuple[SynType, ...]
    objects: tuple[SynObject, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        _version(self.schema_version)
        _text(self.source_label, "source_label", MAX_SYN_SOURCE_LABEL_LENGTH)
        if not isinstance(self.catalog_mode, str) or self.catalog_mode not in {
            "complete",
            "extend_builtin",
        }:
            raise InvalidInputError("unsupported SYN catalog_mode")
        if not isinstance(self.types, tuple) or len(self.types) > MAX_SYN_TYPES:
            raise InvalidInputError(
                f"SYN snapshot requires a tuple of at most {MAX_SYN_TYPES} types"
            )
        if not isinstance(self.objects, tuple) or len(self.objects) > MAX_SYN_OBJECTS:
            raise InvalidInputError(
                f"SYN snapshot requires a tuple of at most {MAX_SYN_OBJECTS} objects"
            )
        if any(not isinstance(item, SynType) for item in self.types) or any(
            not isinstance(item, SynObject) for item in self.objects
        ):
            raise InvalidInputError("SYN snapshots require typed definitions and objects")
        type_ids = [type_identity(item.ref) for item in self.types]
        object_refs = [item.ref for item in self.objects]
        if len(set(type_ids)) != len(type_ids) or len(set(object_refs)) != len(object_refs):
            raise InvalidInputError("SYN snapshot contains duplicate refs")
        # Escaping strings can expand the envelope beyond the sum of payload sizes.
        # Validate its actual canonical representation before hashing/publication.
        self.to_json()

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_label": self.source_label,
            "catalog_mode": self.catalog_mode,
            "types": [item.to_dict() for item in self.types],
            "objects": [item.to_dict() for item in self.objects],
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        value = _fields(
            data, {"source_label", "catalog_mode", "types", "objects", "schema_version"}
        )
        if not isinstance(value["types"], list) or len(value["types"]) > MAX_SYN_TYPES:
            raise InvalidInputError(
                f"SYN types must be a JSON array of at most {MAX_SYN_TYPES} items"
            )
        if not isinstance(value["objects"], list) or len(value["objects"]) > MAX_SYN_OBJECTS:
            raise InvalidInputError(
                f"SYN objects must be a JSON array of at most {MAX_SYN_OBJECTS} items"
            )
        return cls(
            source_label=value["source_label"],
            catalog_mode=value["catalog_mode"],
            types=tuple(SynType.from_dict(item) for item in value["types"]),
            objects=tuple(SynObject.from_dict(item) for item in value["objects"]),
            schema_version=value["schema_version"],
        )

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()
