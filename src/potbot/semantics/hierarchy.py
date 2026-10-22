"""Inspect the opt-in hierarchy and advisory SYN primitive placements offline.

Type ancestry, primitive storage kind, and the subject described by a record are
independent. This module never reclassifies imported objects or creates them.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from importlib.resources import files
from typing import Any

from potbot.domain.errors import InvalidInputError
from potbot.domain.syn import SYN_KINDS, SynSnapshot, SynType, parse_type_ref, type_identity

from .catalog import (
    builtin_types,
    catalog_fingerprint,
    extension_types,
    merge_types,
    type_ancestors,
)

PROFILE_ID = "urn:potbot:semantic-placement:0.1.0"


def _rows(value: object, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise InvalidInputError(f"hierarchy {name} must contain object records")
    return value


def _known_kind(value: object, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if not isinstance(value, str) or value not in SYN_KINDS:
        raise InvalidInputError("unknown SYN primitive kind")


def validate_profile(document: dict[str, Any]) -> tuple[SynType, ...]:
    """Check registry references and explicit recommendations, not their truth.

    Recommendations do not become hard import constraints through this check.
    Native and protected definitions cannot be replaced by this extension.
    """
    if (
        type(document.get("schema_version")) is not int
        or document.get("schema_version") != 1
        or document.get("profile_id") != PROFILE_ID
    ):
        raise InvalidInputError("unsupported Potbot hierarchy profile")
    records = tuple(SynType.from_dict(row) for row in _rows(document.get("types"), "types"))
    baseline = (*builtin_types(), *extension_types())
    baseline_ids = {row.ref.slug for row in baseline}
    if any(row.ref.slug in baseline_ids for row in records):
        raise InvalidInputError("hierarchy extension cannot replace a native or protected type")
    snapshot = SynSnapshot("Potbot semantic placement 0.1.0", "extend_builtin", records, ())
    merged = merge_types(snapshot)
    type_ancestors(merged)
    known = {row.ref.slug for row in merged}

    def reference(value: object) -> str:
        slug = type_identity(value)
        if slug not in known:
            raise InvalidInputError(f"unregistered hierarchy type reference: {value}")
        return slug

    kinds = _rows(document.get("primitive_kinds"), "primitive kinds")
    for row in kinds:
        _known_kind(row.get("kind"))
    if len(kinds) != len(SYN_KINDS) or {row.get("kind") for row in kinds} != set(SYN_KINDS):
        raise InvalidInputError("hierarchy must describe exactly the ten native SYN kinds")
    for row in kinds:
        reference(row.get("native_root_type_ref"))

    relations = _rows(document.get("relations"), "relations")
    relation_names: set[str] = set()
    for row in relations:
        name = row.get("name")
        if not isinstance(name, str) or not name or name in relation_names:
            raise InvalidInputError("hierarchy relation names must be unique nonempty strings")
        relation_names.add(name)
        if row.get("requires_distinct_identity") is not True:
            raise InvalidInputError("hierarchy referent relations require distinct identities")
        for side in ("domains", "ranges"):
            endpoints = _rows(row.get(side), f"relation {side}")
            if not endpoints:
                raise InvalidInputError("hierarchy relations require explicit endpoint scopes")
            for endpoint in endpoints:
                reference(endpoint.get("type_ref"))
                allowed = endpoint.get("allowed_kinds")
                if not isinstance(allowed, list) or not allowed:
                    raise InvalidInputError("relation endpoint kinds must be explicit")
                for kind in allowed:
                    _known_kind(kind)
                if len(set(allowed)) != len(allowed):
                    raise InvalidInputError("duplicate relation endpoint kind")

    placements = _rows(document.get("placements"), "placements")
    seen: set[str] = set()
    for row in placements:
        slug = reference(row.get("type_ref"))
        if slug in seen:
            raise InvalidInputError("duplicate placement identity across type URL schemes")
        seen.add(slug)
        if not isinstance(row.get("abstract"), bool):
            raise InvalidInputError("placement abstractness must be explicit")
        _known_kind(row.get("preferred_kind"), optional=row["abstract"])
        if row["abstract"] and row.get("preferred_kind") is not None:
            raise InvalidInputError("abstract classes cannot recommend a concrete assignment")
        if row.get("preferred_kind") == "type" and slug != "type":
            raise InvalidInputError("type-definition records are not native Type rows")
        if not isinstance(row.get("rationale"), str) or not row["rationale"]:
            raise InvalidInputError("primitive recommendations require a rationale")
        for view in _rows(row.get("related_views"), "related views"):
            reference(view.get("target_type_ref"))
            _known_kind(view.get("target_kind"))
            if not isinstance(view.get("relation"), str) or view["relation"] not in relation_names:
                raise InvalidInputError("related view refers to an unknown relation")
            if view.get("requires_distinct_identity") is not True:
                raise InvalidInputError("record and referent views need distinct identities")
    if not {row.ref.slug for row in records} <= seen:
        raise InvalidInputError("every authored class needs an explicit placement decision")
    placement_by_id = {type_identity(row["type_ref"]): row for row in placements}
    reused: set[str] = set()
    for row in _rows(document.get("reuse_decisions"), "reuse decisions"):
        slug = reference(row.get("type_ref"))
        placement = placement_by_id.get(slug)
        if slug in reused or placement is None:
            raise InvalidInputError("native reuse decisions need unique explicit placements")
        reused.add(slug)
        _known_kind(row.get("preferred_kind"), optional=placement["abstract"])
        if row.get("preferred_kind") != placement["preferred_kind"]:
            raise InvalidInputError("native reuse decisions must agree with their placements")
    return records


@lru_cache(maxsize=1)
def _document() -> dict[str, Any]:
    raw = files(__package__).joinpath("hierarchy_profile.json").read_text(encoding="utf-8")
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise InvalidInputError("hierarchy profile must be an object")
    validate_profile(document)
    return document


def profile_document() -> dict[str, Any]:
    """Return a detached copy of the complete, explicitly versioned design."""
    document: dict[str, Any] = json.loads(json.dumps(_document()))
    return document


def profile_hash() -> str:
    canonical = json.dumps(_document(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def profile_snapshot() -> SynSnapshot:
    """Return an opt-in types-only snapshot; the default catalog stays unchanged."""
    return SynSnapshot(
        "Potbot semantic placement 0.1.0",
        "extend_builtin",
        tuple(SynType.from_dict(row) for row in _document()["types"]),
        (),
    )


@lru_cache(maxsize=1)
def _graph() -> tuple[tuple[SynType, ...], dict[str, tuple[str, ...]]]:
    merged = merge_types(profile_snapshot())
    return merged, type_ancestors(merged)


def _preferred_refs() -> dict[str, str]:
    merged, _ = _graph()
    preferred = {row.ref.slug: str(row.ref) for row in merged}
    preferred.update((type_identity(row["ref"]), row["ref"]) for row in _document()["types"])
    return preferred


def _type_card(record: SynType) -> dict[str, Any]:
    merged, ancestry = _graph()
    preferred = _preferred_refs()
    placements = {type_identity(row["type_ref"]): row for row in _document()["placements"]}
    native_ids = {row.ref.slug for row in builtin_types()}
    legacy_ids = {row.ref.slug for row in extension_types()}
    placement = placements.get(record.ref.slug)
    return {
        "ref": preferred[record.ref.slug],
        "effective_ref": str(record.ref),
        "name": record.name,
        "definition": record.definition,
        "parents": [preferred[parent.slug] for parent in record.parents],
        "ancestors": [preferred[type_identity(ref)] for ref in ancestry[str(record.ref)]],
        "children": sorted(
            preferred[child.ref.slug] for child in merged if record.ref in child.parents
        ),
        "origin": (
            "native"
            if record.ref.slug in native_ids
            else "protected_legacy"
            if record.ref.slug in legacy_ids
            else "authored_profile"
        ),
        "abstract": None if placement is None else placement["abstract"],
        "placement": None if placement is None else json.loads(json.dumps(placement)),
    }


def inspect_hierarchy(
    *,
    type_ref: str | None = None,
    include_subtypes: bool = False,
    primitive_kind: str | None = None,
    summary: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Browse the full graph; primitive filtering selects recommendations only."""
    if not isinstance(include_subtypes, bool) or not isinstance(summary, bool):
        raise InvalidInputError("hierarchy include-subtypes and summary must be booleans")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise InvalidInputError("hierarchy limit must be an integer from 1 to 1000")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise InvalidInputError("hierarchy offset must be a nonnegative integer")
    if include_subtypes and type_ref is None:
        raise InvalidInputError("include-subtypes requires a type-ref")
    if primitive_kind is not None:
        _known_kind(primitive_kind)
    merged, ancestry = _graph()
    selected = list(merged)
    if type_ref is not None:
        slug = parse_type_ref(type_ref).slug
        target = next((row for row in merged if row.ref.slug == slug), None)
        if target is None:
            raise InvalidInputError("type is absent from the selected hierarchy profile")
        selected = [
            row
            for row in selected
            if row.ref == target.ref
            or (include_subtypes and str(target.ref) in ancestry[str(row.ref)])
        ]
    if primitive_kind is not None:
        recommended = {
            type_identity(row["type_ref"])
            for row in _document()["placements"]
            if row["preferred_kind"] == primitive_kind
        }
        selected = [row for row in selected if row.ref.slug in recommended]
    # The slug is the identity: alternate scheme spelling never changes order.
    selected.sort(key=lambda row: row.ref.slug.encode("ascii"))
    page = selected[offset : offset + limit]
    has_more = offset + len(page) < len(selected)
    output: dict[str, Any] = {
        "schema_version": 1,
        "profile_id": PROFILE_ID,
        "profile_status": _document()["status"],
        "profile_hash": profile_hash(),
        "catalog_hash": catalog_fingerprint(merged),
        "native_type_count": len(builtin_types()),
        "legacy_representation_type_count": len(extension_types()),
        "authored_type_count": len(_document()["types"]),
        "effective_type_count": len(merged),
        "selected_count": len(selected),
        "returned_count": 0 if summary else len(page),
        "offset": offset,
        "limit": limit,
        "has_more": False if summary else has_more,
        "next_offset": offset + len(page) if has_more and not summary else None,
        "primitive_filter_semantics": "explicit_advisory_recommendation",
        "coverage": "bundled_profile_only",
        "enforces_import_placement": False,
    }
    if not summary:
        output["types"] = [_type_card(row) for row in page]
    return output


def inspect_placement(type_ref: str, *, check_kind: str | None = None) -> dict[str, Any]:
    """Explain a type's recommendation without inventing one from ancestors."""
    if check_kind is not None:
        _known_kind(check_kind)
    output = inspect_hierarchy(type_ref=type_ref, limit=1)
    card = output.pop("types")[0]
    placement = card["placement"]
    decision = (
        "no_recommendation"
        if placement is None
        else "abstract"
        if placement["abstract"]
        else "recommendation"
        if check_kind is None
        else "matches_recommendation"
        if check_kind == placement["preferred_kind"]
        else "different_from_recommendation"
    )
    output.update({"type": card, "checked_kind": check_kind, "decision": decision})
    return output
