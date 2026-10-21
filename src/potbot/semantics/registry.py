"""Load the actual published vocabulary and retain immutable release identities."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from potbot.domain.errors import InvalidInputError, SchemaViolationError
from potbot.domain.serialization import canonical_json, content_hash

CONSUMER_CONTRACT = "semantic-structure-v1"
REGISTRY_DOCUMENTS = (
    "vocabulary.json",
    "data-types.json",
    "retrieval-profiles.json",
    "semantic-validation-rules.json",
    "feature-encoding.json",
    "semantic-bundle.schema.json",
    "typed-data-item.schema.json",
    "type-query.schema.json",
    "retrieval-profiles.schema.json",
    "retrieval-request.schema.json",
    "artifact-embedding-manifest.schema.json",
)


def strict_json(raw: str | bytes) -> Any:
    """Reject duplicate object keys, non-JSON constants and trailing content."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise SchemaViolationError("duplicate JSON object key")
            result[key] = value
        return result

    def constant(_: str) -> Any:
        raise SchemaViolationError("non-JSON numeric constant")

    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="strict")
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise SchemaViolationError("invalid strict UTF-8 JSON document") from error


def _index(entries: Any, kind: str, key: str = "id") -> dict[str, dict[str, Any]]:
    if not isinstance(entries, list) or not entries:
        raise SchemaViolationError(f"{kind} must be a nonempty registry list")
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get(key), str) or not entry[key]:
            raise SchemaViolationError(f"{kind} entry requires a stable identifier")
        if entry[key] in result:
            raise SchemaViolationError(f"duplicate {kind} identifier")
        result[entry[key]] = entry
    return result


def _walk(value: Any) -> list[dict[str, Any]]:
    pending = [value]
    objects: list[dict[str, Any]] = []
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            objects.append(current)
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
        if len(objects) > 100_000:
            raise SchemaViolationError("registry exceeds structural limits")
    return objects


def _check_schema_references(documents: dict[str, dict[str, Any]]) -> None:
    identifiers: dict[str, dict[str, Any]] = {}
    for name, doc in documents.items():
        if name.endswith(".schema.json"):
            identity = doc.get("$id")
            if not isinstance(identity, str) or identity in identifiers:
                raise SchemaViolationError("missing or duplicate schema identity")
            identifiers[identity] = doc
    for name, doc in documents.items():
        if not name.endswith(".schema.json"):
            continue
        for item in _walk(doc):
            if "$ref" not in item:
                continue
            ref = item["$ref"]
            if not isinstance(ref, str):
                raise SchemaViolationError("invalid schema reference")
            base, _, fragment = ref.partition("#")
            target = doc if not base else identifiers.get(base)
            if target is None or (fragment and not fragment.startswith("/")):
                raise SchemaViolationError("unregistered schema reference")
            current: Any = target
            try:
                for part in fragment.split("/")[1:]:
                    current = current[part.replace("~1", "/").replace("~0", "~")]
            except (KeyError, TypeError) as error:
                raise SchemaViolationError("unresolved schema pointer") from error


@dataclass(frozen=True, slots=True)
class SemanticRegistry:
    """A registry snapshot stores JSON strings; callers cannot mutate its identity."""

    _documents: tuple[tuple[str, str], ...]
    _manifest_json: str

    @classmethod
    def load(cls, directory: Path | None = None) -> SemanticRegistry:
        if directory is None:
            bundled = files("potbot.semantics").joinpath("data")
            source = Path(__file__).resolve().parents[3] / "vocab"
            directory_source: Any = bundled if bundled.is_dir() else source
        else:
            directory_source = Path(directory)
        raw_documents: dict[str, bytes] = {}
        for name in REGISTRY_DOCUMENTS:
            try:
                raw = directory_source.joinpath(name).read_bytes()
            except (OSError, FileNotFoundError) as error:
                raise SchemaViolationError(f"semantic release is missing {name}") from error
            if len(raw) > 4_000_000:
                raise SchemaViolationError("semantic registry document exceeds size limit")
            raw_documents[name] = raw
        return cls.from_documents(raw_documents)

    @classmethod
    def from_documents(cls, documents: dict[str, bytes]) -> SemanticRegistry:
        if set(documents) != set(REGISTRY_DOCUMENTS):
            raise SchemaViolationError("semantic release document inventory is incomplete")
        parsed = {name: strict_json(raw) for name, raw in documents.items()}
        if any(not isinstance(doc, dict) for doc in parsed.values()):
            raise SchemaViolationError("registry documents must be JSON objects")
        cls._validate(parsed)
        inventory = cls._catalogs(parsed)
        components = {}
        for name, doc in parsed.items():
            identity = doc.get("$id", doc.get("id", doc.get("version")))
            if not isinstance(identity, str) or not identity:
                raise SchemaViolationError("every registry component needs a release identity")
            components[name] = {
                "identity": identity,
                "content_hash": content_hash(doc),
                "bytes_sha256": hashlib.sha256(documents[name]).hexdigest(),
            }
        manifest = {
            "schema_version": 1,
            "consumer_contract": CONSUMER_CONTRACT,
            "vocabulary_release": parsed["vocabulary.json"]["id"],
            "components": components,
            "catalogs": {
                kind: {
                    "identifiers": sorted(entries),
                    "count": len(entries),
                    "content_hash": content_hash(entries),
                }
                for kind, entries in inventory.items()
            },
        }
        return cls(
            tuple((name, canonical_json(parsed[name])) for name in sorted(parsed)),
            canonical_json(manifest),
        )

    @staticmethod
    def _catalogs(documents: dict[str, dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
        vocabulary = documents["vocabulary.json"]
        return {
            "data_type": _index(documents["data-types.json"]["types"], "data type"),
            "entity_type": _index(vocabulary["entity_types"], "entity type"),
            "predicate": _index(vocabulary["predicates"], "predicate"),
            "concept": _index(vocabulary["concepts"], "concept"),
            "retrieval_profile": _index(
                documents["retrieval-profiles.json"]["profiles"], "profile"
            ),
        }

    @classmethod
    def _validate(cls, documents: dict[str, dict[str, Any]]) -> None:
        try:
            catalogs = cls._catalogs(documents)
            vocabulary = documents["vocabulary.json"]
            data_types = catalogs["data_type"]
            _check_schema_references(documents)
            _index(vocabulary["context_fields"], "context field")
            _index(documents["data-types.json"]["classification_rules"], "classifier rule")
            _index(documents["semantic-validation-rules.json"]["rules"], "semantic rule")
            _index(documents["retrieval-profiles.json"]["profiles"], "profile name", "name")
            parents: dict[str, list[str]] = {}
            for identity, definition in data_types.items():
                if type(definition.get("abstract")) is not bool:
                    raise SchemaViolationError("data type must explicitly declare abstract status")
                ancestors = definition.get("is_a")
                if not isinstance(ancestors, list) or len(set(ancestors)) != len(ancestors):
                    raise SchemaViolationError("data subtype parents must be distinct identifiers")
                if any(parent not in data_types for parent in ancestors):
                    raise SchemaViolationError("unregistered data subtype parent")
                parents[identity] = ancestors
            visited: set[str] = set()
            visiting: set[str] = set()

            def visit(identity: str) -> None:
                if identity in visiting:
                    raise SchemaViolationError("data subtype graph contains a cycle")
                if identity in visited:
                    return
                visiting.add(identity)
                for parent in parents[identity]:
                    visit(parent)
                visiting.remove(identity)
                visited.add(identity)

            for identity in parents:
                visit(identity)
            terms = set(vocabulary["literal_types"]) | {"entity", "unknown"}
            entity_types = catalogs["entity_type"]
            for field in vocabulary["context_fields"]:
                for key in ("selector_entity_type", "value_entity_type"):
                    if field.get(key) is not None and field[key] not in entity_types:
                        raise SchemaViolationError(
                            "context field references an unknown entity type"
                        )
                if field["value_type"] not in terms | set(vocabulary["enums"]) | {
                    "declared_key_type"
                }:
                    raise SchemaViolationError("context field references an unknown value type")
            for profile in catalogs["retrieval_profile"].values():
                if not set(profile["match_intents"]).issubset(vocabulary["match_intents"]):
                    raise SchemaViolationError("profile references an unknown match intent")
                _index(profile["channels"], "profile channel")
            for predicate in catalogs["predicate"].values():
                roles = predicate["roles"]
                if not isinstance(roles, dict) or not roles:
                    raise SchemaViolationError("predicate must have named role signatures")
                for signature in roles.values():
                    allowed = signature.get("term_kinds", [signature.get("term_kind")])
                    if not set(allowed).issubset(terms):
                        raise SchemaViolationError("predicate references an unknown term kind")
                    if not set(signature.get("entity_types", [])).issubset(entity_types):
                        raise SchemaViolationError("predicate references an unknown entity type")
                    if "domain" in signature and signature["domain"] not in vocabulary["enums"]:
                        raise SchemaViolationError("predicate references an unknown enum domain")
                if predicate.get("functional_key") and not set(
                    predicate["functional_key"]
                ).issubset(roles):
                    raise SchemaViolationError(
                        "functional key references an unknown predicate role"
                    )
            schema = documents["semantic-bundle.schema.json"]
            if schema["properties"]["vocabulary_release"]["const"] != vocabulary["id"]:
                raise SchemaViolationError("schema and vocabulary release identities disagree")
            if documents["retrieval-profiles.json"]["vocabulary_release"] != vocabulary["id"]:
                raise SchemaViolationError("profile and vocabulary releases disagree")
            if set(schema["$defs"]["entity"]["properties"]["entity_type"]["enum"]) != set(
                entity_types
            ):
                raise SchemaViolationError("schema omits or invents a registered entity type")
            schema_predicates = {
                branch["properties"]["predicate"]["const"]
                for branch in schema["$defs"]["atom"]["oneOf"]
            }
            if schema_predicates != set(catalogs["predicate"]):
                raise SchemaViolationError("schema and predicate inventory disagree")
            # The type-query enum is a second exact inventory, including abstract families.
            declared_enums = [
                set(item["enum"])
                for item in _walk(documents["type-query.schema.json"])
                if isinstance(item.get("enum"), list)
                and any(
                    isinstance(value, str) and value.startswith("dt:") for value in item["enum"]
                )
            ]
            if not declared_enums or any(values != set(data_types) for values in declared_enums):
                raise SchemaViolationError("schema and data type inventory disagree")
        except (KeyError, TypeError, ValueError, RecursionError) as error:
            if isinstance(error, SchemaViolationError):
                raise
            raise SchemaViolationError("malformed semantic registry definition") from error

    @property
    def manifest(self) -> dict[str, Any]:
        return dict(json.loads(self._manifest_json))

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self._manifest_json.encode("utf-8")).hexdigest()

    @property
    def release_key(self) -> str:
        return "urn:potbot:semantic-release:sha256:" + self.fingerprint

    def documents(self) -> dict[str, dict[str, Any]]:
        return {name: json.loads(raw) for name, raw in self._documents}

    def document(self, name: str) -> dict[str, Any]:
        try:
            return dict(json.loads(dict(self._documents)[name]))
        except KeyError as error:
            raise InvalidInputError("unregistered semantic document") from error

    def inventory(self, kind: str) -> tuple[dict[str, Any], ...]:
        catalogs = self._catalogs(self.documents())
        if kind not in catalogs:
            raise InvalidInputError("unregistered semantic catalog kind")
        return tuple(catalogs[kind][identity] for identity in sorted(catalogs[kind]))

    def require_type(self, identity: str, *, assignable: bool = False) -> dict[str, Any]:
        types = self._catalogs(self.documents())["data_type"]
        if identity not in types or (assignable and types[identity]["abstract"]):
            raise SchemaViolationError("type is unknown or cannot be a primary assignment")
        return types[identity]

    def descendants(self, identity: str, *, include_self: bool = False) -> tuple[str, ...]:
        self.require_type(identity)
        types = self._catalogs(self.documents())["data_type"]
        found = {identity}
        while True:
            additions = {key for key, value in types.items() if set(value["is_a"]) & found}
            if additions.issubset(found):
                break
            found.update(additions)
        if not include_self:
            found.remove(identity)
        return tuple(sorted(found))
