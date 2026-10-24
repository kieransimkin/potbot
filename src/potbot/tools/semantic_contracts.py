"""Canonical, opt-in semantic schemas; descriptions never confer execution authority.

Schema evolution is deliberately conservative: only new optional fields are
compatible under the same major contract. Active invocations retain their exact
schema and catalog hashes. This is not a general JSON Schema implication solver.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from potbot.cogs.contracts import CogSignature, TypedCogValue
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.domain.syn import SynSnapshot, type_identity
from potbot.semantics.catalog import merge_types, type_ancestors
from potbot.semantics.registry import strict_json
from potbot.tools.capability_contracts import (
    ToolCapabilityDescriptor,
    ToolImplementationDescriptor,
    ToolInstallationDescriptor,
    ToolInvocationIntent,
    ToolObservation,
)

PREFIX = "syn://type/potbot-tool-"
Descriptor = (
    ToolCapabilityDescriptor
    | ToolImplementationDescriptor
    | ToolInstallationDescriptor
    | ToolInvocationIntent
    | ToolObservation
)
TOOL = PREFIX + "descriptor-v1"
CAPABILITY = PREFIX + "capability-v1"
IMPLEMENTATION = PREFIX + "implementation-v1"
INSTALLATION = PREFIX + "installation-v1"
INVOCATION = PREFIX + "invocation-v1"
EVIDENCE = PREFIX + "evidence-v1"
QUERY = PREFIX + "search-query-v1"
RESULTS = PREFIX + "search-results-v1"
TABLE = PREFIX + "unit-table-v1"
CHECK = PREFIX + "contract-check-v1"
CHECKED = PREFIX + "contract-checked-v1"
SESSION = PREFIX + "target-session-v1"
TARGET_RECEIPT = PREFIX + "target-receipt-v1"


def obj(properties: dict[str, Any], optional: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(set(properties) - set(optional)),
        "additionalProperties": False,
    }


TEXT = {"type": "string", "minLength": 1, "maxLength": 4096}
HASH = {"type": "string", "minLength": 64, "maxLength": 64, "pattern": "^[0-9a-f]{64}$"}
VERSION = {"type": "integer", "const": 1}
TYPE = {"type": "string", "pattern": "^syn(?:ny)?://type/[a-z0-9]+(?:[-_][a-z0-9]+)*$"}
UUID_TEXT = {
    "type": "string",
    "minLength": 36,
    "maxLength": 36,
    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
}


def array(items: dict[str, Any], maximum: int = 128) -> dict[str, Any]:
    return {"type": "array", "items": items, "maxItems": maximum}


def schemas() -> dict[str, dict[str, Any]]:
    """Return fresh schema values; callers cannot mutate the installed catalog."""
    record_classes: dict[str, type[Descriptor]] = {
        CAPABILITY: ToolCapabilityDescriptor,
        IMPLEMENTATION: ToolImplementationDescriptor,
        INSTALLATION: ToolInstallationDescriptor,
        INVOCATION: ToolInvocationIntent,
        EVIDENCE: ToolObservation,
    }
    # Existing exact record constructors remain authoritative for representation
    # and link validation; publish their fields as registered semantic schemas.
    result = {
        ref: obj({name: {} for name in sorted(cls._FIELDS)}) for ref, cls in record_classes.items()
    }
    result.update(
        {
            TOOL: obj(
                {
                    "schema_version": VERSION,
                    "tool_ref": {**TEXT, "pattern": "^syn://thing/[a-z0-9_-]+$"},
                    "capability_hash": HASH,
                    "classification": {"const": "discovery_descriptor"},
                }
            ),
            QUERY: obj(
                {
                    "schema_version": VERSION,
                    "text": {**TEXT, "maxLength": 256},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 32},
                }
            ),
            RESULTS: obj(
                {
                    "schema_version": VERSION,
                    "query_hash": HASH,
                    "corpus_hash": HASH,
                    "items": array(obj({"id": TEXT, "text": TEXT, "source_sha256": HASH}), 32),
                    "complete": {"type": "boolean"},
                }
            ),
            TABLE: obj(
                {
                    "schema_version": VERSION,
                    "source_sha256": HASH,
                    "locale": {"const": "und"},
                    "timezone": {"const": "UTC"},
                    "mime": {"const": "application/json"},
                    "encoding": {"const": "utf-8"},
                    "unit": {"enum": ["ms", "s"]},
                    "target_unit": {"enum": ["ms", "s"]},
                    "precision": {"type": "integer", "minimum": 0, "maximum": 12},
                    "tolerance": {"type": "string", "pattern": "^[0-9]{1,12}(?:\\.[0-9]{1,12})?$"},
                    "rows": array(
                        obj(
                            {
                                "value": {
                                    "type": "string",
                                    "pattern": "^-?[0-9]{1,12}(?:\\.[0-9]{1,12})?$",
                                },
                                "source_start": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": 16777216,
                                },
                                "source_end": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 16777216,
                                },
                            }
                        )
                    ),
                    "loss": array(
                        obj({"row": {"type": "integer", "minimum": 0}, "absolute": TEXT}), 128
                    ),
                },
                ("loss",),
            ),
            CHECK: obj(
                {"schema_version": VERSION, "type_ref": TYPE, "schema_hash": HASH, "value": {}}
            ),
            CHECKED: obj(
                {
                    "schema_version": VERSION,
                    "type_ref": TYPE,
                    "schema_hash": HASH,
                    "value_hash": HASH,
                    "canonical_json": TEXT,
                }
            ),
            SESSION: obj(
                {
                    "schema_version": VERSION,
                    "kind": {"enum": ["debugger", "device", "profiler"]},
                    "session_ref": UUID_TEXT,
                    "target_ref": TEXT,
                    "process_identity": TEXT,
                    "source_sha256": HASH,
                    "adapter": TEXT,
                    "adapter_version": TEXT,
                    "capabilities": array(
                        obj(
                            {
                                "operation": TEXT,
                                "state": {"enum": ["supported", "unsupported", "unqualified"]},
                                "reason": TEXT,
                            }
                        )
                    ),
                }
            ),
            TARGET_RECEIPT: obj(
                {
                    "schema_version": VERSION,
                    "session_hash": HASH,
                    "session_ref": UUID_TEXT,
                    "target_ref": TEXT,
                    "process_identity": TEXT,
                    "source_sha256": HASH,
                    "kind": {"enum": ["capture", "stop", "stack", "value"]},
                    "revision": {"type": "integer", "minimum": 1},
                    "artifact_sha256": HASH,
                    "verification": {"enum": ["reported", "verified"]},
                }
            ),
        }
    )
    # Publish actual field schemas as well as constructor-level semantic checks.
    for ref in record_classes:
        props = result[ref]["properties"]
        for name in props:
            props[name] = (
                VERSION
                if name == "schema_version"
                else TYPE
                if name.endswith("type_ref")
                else HASH
                if name.endswith(("_hash", "_digest"))
                else UUID_TEXT
                if name.endswith("_id")
                else TEXT
            )
        if ref == CAPABILITY:
            props["effect_class"] = {
                "enum": ["read_only", "idempotent_write", "non_idempotent_write", "destructive"]
            }
            props["protocol"] = {
                "enum": ["cog", "mcp", "cli", "rest", "graphql", "grpc", "sql", "browser", "solver"]
            }
            for name in ("input_schema_json", "output_schema_json"):
                props[name] = {"type": "string", "maxLength": 131072}
        if ref == INSTALLATION:
            props["permitted_scopes"] = {**array(TEXT, 64), "uniqueItems": True}
        if ref == INVOCATION:
            props["max_wall_time_ms"] = {"type": "integer", "minimum": 1, "maximum": 3600000}
            props["max_output_bytes"] = {"type": "integer", "minimum": 1, "maximum": 16777216}
        if ref == EVIDENCE:
            props["status"] = {"enum": ["succeeded", "failed", "indeterminate"]}
            for name in ("output_hash", "artifact_sha256"):
                props[name] = {"anyOf": [HASH, {"type": "null"}]}
            props["error_code"] = {"anyOf": [TEXT, {"type": "null"}]}
    # No mutable schema nodes (including shared scalar definitions) escape.
    return json.loads(canonical_json(result))  # type: ignore[no-any-return]


def require_compatible_schema(before: dict[str, Any], after: dict[str, Any]) -> None:
    """Allow only append-only optional properties; all other changes need a new major Type."""
    previous, following = json.loads(canonical_json(before)), json.loads(canonical_json(after))
    old_props, new_props = previous.pop("properties", {}), following.pop("properties", {})
    if previous != following or any(new_props.get(k) != v for k, v in old_props.items()):
        raise InvalidInputError("Incompatible schema evolution requires a new versioned Type")
    if not set(old_props) <= set(new_props) or (set(new_props) - set(old_props)) & set(
        after.get("required", [])
    ):
        raise InvalidInputError("New required fields are incompatible")


@dataclass(frozen=True, slots=True)
class ToolSemanticCatalog:
    snapshot: SynSnapshot
    _schemas_json: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        merged = merge_types(self.snapshot)
        known = {type_identity(t.ref): t for t in merged}
        ancestry = type_ancestors(merged)
        from potbot.tools.artifact_ranges import schemas as artifact_range_schemas
        from potbot.tools.execution_schemas import schemas as execution_schemas

        definitions = schemas()
        definitions.update(
            {
                ref: schema
                for ref, schema in {**execution_schemas(), **artifact_range_schemas()}.items()
                if type_identity(ref) in known
            }
        )
        for ref, schema in definitions.items():
            row = known.get(type_identity(ref))
            if row is None or "synny://type/potbot-data-record" not in ancestry[str(row.ref)]:
                raise InvalidInputError("Canonical contract must be a registered data record")
            if json.loads(row.metadata_json).get("schema") != schema:
                raise InvalidInputError("Canonical schema differs from its sealed Type")
        object.__setattr__(self, "_schemas_json", canonical_json(definitions))

    def _definitions(self) -> dict[str, dict[str, Any]]:
        # Only compiled, whitelisted schemas are executable. Arbitrary metadata
        # cannot install a validator or introduce a network-resolving $ref.
        result: dict[str, dict[str, Any]] = json.loads(self._schemas_json)
        return result

    def schema_hash(self, type_ref: str) -> str:
        identity = type_identity(type_ref)
        try:
            return content_hash(
                next(v for k, v in self._definitions().items() if type_identity(k) == identity)
            )
        except StopIteration as error:
            raise InvalidInputError("Unknown or unregistered canonical Type") from error

    def validate(self, value: TypedCogValue, *, _depth: int = 0) -> None:
        from jsonschema import Draft202012Validator

        self.schema_hash(value.type_ref)
        if _depth > 8:
            raise InvalidInputError("Typed execution envelope nesting exceeds its bound")
        # Strict canonical JSON bounds and duplicate checks apply to every value.
        if len(canonical_json(value.value).encode("utf-8")) > 131072:
            raise InvalidInputError("Canonical contract exceeds 128 KiB")
        definitions = self._definitions()
        ref = next(k for k in definitions if type_identity(k) == type_identity(value.type_ref))
        schema = definitions[ref]
        if next(Draft202012Validator(schema).iter_errors(value.value), None) is not None:
            raise InvalidInputError("Value violates its registered semantic schema")
        parsers: dict[str, type[Descriptor]] = {
            CAPABILITY: ToolCapabilityDescriptor,
            IMPLEMENTATION: ToolImplementationDescriptor,
            INSTALLATION: ToolInstallationDescriptor,
            INVOCATION: ToolInvocationIntent,
            EVIDENCE: ToolObservation,
        }
        if ref in parsers:
            parsers[ref].from_dict(value.value)
        from potbot.tools.execution_contracts import REQUEST, RESULT

        if ref in {REQUEST, RESULT}:
            payload = value.value["input" if ref == REQUEST else "result"]
            self.validate(TypedCogValue(payload["type_ref"], payload["value"]), _depth=_depth + 1)
        available = {type_identity(t.ref) for t in merge_types(self.snapshot)}
        for key, item in value.value.items():
            if key.endswith("type_ref") and type_identity(item) not in available:
                raise InvalidInputError("Unregistered referenced data Type")
            if key in {"input_type_ref", "output_type_ref"}:
                ancestors = type_ancestors(merge_types(self.snapshot))
                actual = next(
                    t
                    for t in merge_types(self.snapshot)
                    if type_identity(t.ref) == type_identity(item)
                )
                if "synny://type/potbot-data-data" not in ancestors[str(actual.ref)]:
                    raise InvalidInputError("Tool ports require registered data representations")
        if ref == CAPABILITY:
            ancestors = type_ancestors(merge_types(self.snapshot))
            ref = value.value["definition_type_ref"]
            canonical = next(
                t for t in merge_types(self.snapshot) if type_identity(t.ref) == type_identity(ref)
            )
            if not any(
                type_identity(r) == "tool-interface-record" for r in ancestors[str(canonical.ref)]
            ):
                raise InvalidInputError("Capability definition must inherit tool-interface-record")

    def encode(self, value: TypedCogValue) -> str:
        self.validate(value)
        return canonical_json(
            {
                "type_ref": value.type_ref,
                "schema_hash": self.schema_hash(value.type_ref),
                "value": value.value,
            }
        )

    def decode(self, source: str) -> TypedCogValue:
        if not isinstance(source, str) or len(source.encode("utf-8")) > 262144:
            raise InvalidInputError("Typed envelope exceeds its bound")
        raw = strict_json(source)
        if not isinstance(raw, dict) or set(raw) != {"type_ref", "schema_hash", "value"}:
            raise InvalidInputError("Typed envelope fields differ from schema")
        if raw["schema_hash"] != self.schema_hash(raw["type_ref"]):
            raise InvalidInputError("Typed envelope schema pin changed")
        result = TypedCogValue(raw["type_ref"], raw["value"])
        self.validate(result)
        return result


def check_executable_binding(
    catalog: ToolSemanticCatalog,
    signature: CogSignature,
    capability: ToolCapabilityDescriptor,
    implementation: ToolImplementationDescriptor,
    installation: ToolInstallationDescriptor,
) -> None:
    """Validate an installed binding, separately from controller admission."""
    for ref, descriptor in (
        (CAPABILITY, capability),
        (IMPLEMENTATION, implementation),
        (INSTALLATION, installation),
    ):
        catalog.validate(TypedCogValue(ref, descriptor.to_dict()))
    implementation.assert_capability(capability)
    installation.assert_implementation(implementation)
    payload = next(
        (
            json.loads(o.payload_json)
            for o in catalog.snapshot.objects
            if o.ref == signature.cog_ref
        ),
        None,
    )
    if payload is None or (
        payload["implementationBindingHash"] != signature.binding_hash
        or payload["handlerRef"] != signature.handler_ref
        or payload["implementationRevision"] != signature.implementation_revision
        or capability.input_type_ref != signature.input_type_ref
        or capability.output_type_ref != signature.output_type_ref
        or capability.input_schema_json != canonical_json(schemas()[signature.input_type_ref])
        or capability.output_schema_json != canonical_json(schemas()[signature.output_type_ref])
    ):
        raise ScopeDeniedError("Executable binding differs from installed Cog and pinned schemas")


def check_target_receipt(
    catalog: ToolSemanticCatalog, session: dict[str, Any], receipt: dict[str, Any]
) -> None:
    catalog.validate(TypedCogValue(SESSION, session))
    catalog.validate(TypedCogValue(TARGET_RECEIPT, receipt))
    if receipt["session_hash"] != content_hash(session) or any(
        receipt[k] != session[k]
        for k in ("session_ref", "target_ref", "process_identity", "source_sha256")
    ):
        raise ScopeDeniedError("Target receipt is not bound to this exact session/process/source")
