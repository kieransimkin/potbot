"""SYN classes and explicit Potbot representation aliases, with bounded DAG checks.

Native class membership describes a referent. The protected ``potbot-data-``
classes describe checked representations; no native superclass is inferred for
their data root. ``syn://type/x`` and ``synny://type/x`` identify one class. The
preferred spelling is ``syn://`` for native classes and ``synny://`` for Potbot
classes. Source records retain their exact supplied spelling and public metadata.
The bundle is extracted statically, never by importing the uploaded application.
"""

from __future__ import annotations

import hashlib
import heapq
import json
from collections.abc import Iterable
from dataclasses import replace
from functools import lru_cache
from importlib.resources import files
from typing import Any

from potbot.domain.errors import InvalidInputError
from potbot.domain.syn import MAX_SYN_TYPES, SynnyRef, SynRef, SynSnapshot, SynType, type_identity

POTBOT_TYPE_PREFIX = "potbot-data-"
MAX_SYN_ANCESTRY_PAIRS = 1_000_000


@lru_cache(maxsize=1)
def _bundle() -> dict[str, Any]:
    value = json.loads(files(__package__).joinpath("catalog.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise InvalidInputError("unsupported bundled SYN catalog")
    return value


@lru_cache(maxsize=1)
def builtin_types() -> tuple[SynType, ...]:
    """Return all 529 native database classes, retaining source seed provenance."""
    return tuple(SynType.from_dict(row) for row in _bundle()["native_types"])


@lru_cache(maxsize=1)
def extension_types() -> tuple[SynType, ...]:
    """Return 45 existing data-type aliases and two checked public-record types."""
    return tuple(SynType.from_dict(row) for row in _bundle()["extension_types"])


def canonical_type_ref(value: object) -> SynRef | SynnyRef:
    """Resolve scheme aliases to one spelling; this does not register a class."""
    slug = type_identity(value)
    if slug.startswith(POTBOT_TYPE_PREFIX):
        return SynnyRef(f"synny://type/{slug}")
    return SynRef(f"syn://type/{slug}")


def data_type_ref(id_or_ref: str) -> SynnyRef:
    """Resolve only an explicitly registered data alias; do not invent a mapping."""
    candidate = id_or_ref
    if isinstance(id_or_ref, str) and id_or_ref.startswith(("syn://", "synny://")):
        candidate = canonical_type_ref(id_or_ref)
    for record in extension_types():
        original_id = json.loads(record.metadata_json)["data_type"]["id"]
        if candidate in (original_id, record.ref):
            if not isinstance(record.ref, SynnyRef):
                raise InvalidInputError("bundled Potbot data alias must use the synny namespace")
            return record.ref
    raise InvalidInputError("unknown Potbot data type or SYN data alias")


def catalog_provenance() -> dict[str, Any]:
    """Return a fresh copy of public source versions, exact file digests and policy."""
    value = json.loads(json.dumps(_bundle()["provenance"]))  # no mutable cache escapes
    if not isinstance(value, dict):
        raise InvalidInputError("invalid bundled SYN provenance")
    return value


def _canonical_type(record: SynType) -> SynType:
    """Normalize graph references while retaining supplied record content."""
    if not isinstance(record, SynType):
        raise InvalidInputError("SYN catalogs require checked type definitions")
    return replace(
        record,
        ref=canonical_type_ref(record.ref),
        parents=tuple(canonical_type_ref(parent) for parent in record.parents),
        properties=tuple(
            replace(prop, type_ref=canonical_type_ref(prop.type_ref)) for prop in record.properties
        ),
    )


def _ordered_graph(
    types: Iterable[SynType],
) -> tuple[dict[str, SynType], tuple[str, ...]]:
    records: dict[str, SynType] = {}
    for record in types:
        record = _canonical_type(record)
        if record.ref in records:
            raise InvalidInputError(f"duplicate SYN type identity: {record.ref}")
        records[str(record.ref)] = record
        if len(records) > MAX_SYN_TYPES:
            raise InvalidInputError(f"merged SYN catalog exceeds {MAX_SYN_TYPES} types")

    remaining: dict[str, int] = {}
    children: dict[str, list[str]] = {ref: [] for ref in records}
    for ref, record in records.items():
        if len(set(record.parents)) != len(record.parents):
            raise InvalidInputError(f"duplicate parent reference in SYN type: {ref}")
        for parent in record.parents:
            if parent not in records:
                raise InvalidInputError(f"missing SYN parent type: {parent}")
            children[str(parent)].append(ref)
        names: set[str] = set()
        for prop in record.properties:
            if prop.name in names:
                raise InvalidInputError(f"duplicate SYN property name on type: {ref}")
            names.add(prop.name)
            if prop.type_ref not in records:
                raise InvalidInputError(f"missing SYN property range: {prop.type_ref}")
        remaining[ref] = len(record.parents)

    # Kahn traversal handles deep input without Python recursion. Display labels
    # need not be globally unique; identity is the class slug across both schemes.
    ready = [ref for ref, count in remaining.items() if count == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        ref = heapq.heappop(ready)
        order.append(ref)
        for child in children[ref]:
            remaining[child] -= 1
            if not remaining[child]:
                heapq.heappush(ready, child)
    if len(order) != len(records):
        raise InvalidInputError("SYN type ancestry contains a cycle")
    return records, tuple(order)


def merge_types(snapshot: SynSnapshot) -> tuple[SynType, ...]:
    """Merge the explicit catalog mode and protected aliases, then validate the DAG.

    Complete snapshots never acquire absent native classes from the bundled seed.
    In extend_builtin mode, an explicit native definition replaces its matching
    seed completely, including parents and properties. Scheme aliases identify
    the same class. Neither mode may supply a protected potbot-data- slug in
    either scheme.
    """
    if not isinstance(snapshot, SynSnapshot):
        raise InvalidInputError("catalog merge requires a checked SYN snapshot")
    records = (
        {str(record.ref): record for record in builtin_types()}
        if snapshot.catalog_mode == "extend_builtin"
        else {}
    )
    supplied: set[str] = set()
    for supplied_record in snapshot.types:
        record = _canonical_type(supplied_record)
        if record.ref.slug.startswith(POTBOT_TYPE_PREFIX):
            raise InvalidInputError("snapshot cannot supply protected Potbot type definitions")
        if record.ref in supplied:
            raise InvalidInputError(f"duplicate SYN type identity: {record.ref}")
        supplied.add(str(record.ref))
        records[str(record.ref)] = record
    records.update((str(record.ref), record) for record in extension_types())
    records, _ = _ordered_graph(records.values())
    return tuple(records[ref] for ref in sorted(records))


def type_ancestors(types: Iterable[SynType]) -> dict[str, tuple[str, ...]]:
    """Return unique, sorted ancestors including self, or fail before an oversized closure.

    The bound limits the actual materialization; it never silently omits ancestor
    pairs. Redundant direct edges are valid and do not duplicate memberships.
    """
    records, order = _ordered_graph(types)
    ancestors: dict[str, set[str]] = {}
    total = 0
    for ref in order:
        inherited = {ref}
        for parent in records[ref].parents:
            inherited.update(ancestors[str(parent)])
            if total + len(inherited) > MAX_SYN_ANCESTRY_PAIRS:
                raise InvalidInputError(
                    f"SYN ancestry exceeds {MAX_SYN_ANCESTRY_PAIRS} unique pairs"
                )
        total += len(inherited)
        if total > MAX_SYN_ANCESTRY_PAIRS:
            raise InvalidInputError(f"SYN ancestry exceeds {MAX_SYN_ANCESTRY_PAIRS} unique pairs")
        ancestors[ref] = inherited
    return {ref: tuple(sorted(ancestors[ref])) for ref in sorted(ancestors)}


def catalog_fingerprint(types: Iterable[SynType] | None = None) -> str:
    """Pin provenance, protected extensions and the effective snapshot definitions."""
    selected = (*builtin_types(), *extension_types()) if types is None else tuple(types)
    records, _ = _ordered_graph(selected)
    material = {
        "schema_version": 1,
        "provenance": catalog_provenance(),
        "protected_extensions": [record.to_dict() for record in extension_types()],
        "types": [records[ref].to_dict() for ref in sorted(records)],
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
