"""Bounded semantic shape checks; authority and reference resolution stay external."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from importlib import import_module
from typing import Any

from potbot.domain.errors import CapabilityUnavailableError, SchemaViolationError
from potbot.domain.serialization import canonical_json
from potbot.semantics.registry import SemanticRegistry, strict_json

_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
_ABSENT = object()
ABSENT = _ABSENT


@dataclass(frozen=True, slots=True)
class ParsedLiteral:
    term_json: str
    source_lexeme: str

    @property
    def term(self) -> dict[str, Any]:
        return dict(json.loads(self.term_json))


def parse_scalar_literal(source: str) -> ParsedLiteral:
    """Map one JSON scalar without float conversion; retain its original lexeme.

    A decimal/exponent token remains a decimal even when its value is integral.
    Absent input is deliberately outside this API and outside the wire null type.
    """
    if not isinstance(source, str) or len(source.encode("utf-8")) > 4096:
        raise SchemaViolationError("literal requires bounded UTF-8 source text")
    token = source.strip()
    if _NUMBER.fullmatch(token):
        decimal = "." in token or "e" in token.lower()
        try:
            value = Decimal(token)
            exponent = value.as_tuple().exponent
            if (
                not isinstance(exponent, int)
                or abs(value.adjusted()) > 4096
                or abs(exponent) > 4096
            ):
                raise SchemaViolationError("numeric expansion exceeds literal budget")
            normalized = format(value, "f")
        except (InvalidOperation, ValueError) as error:
            raise SchemaViolationError("invalid numeric literal") from error
        if "." in normalized:
            normalized = normalized.rstrip("0").rstrip(".")
        if value.is_zero():
            normalized = "0"
        term: dict[str, Any] = {"term": "decimal" if decimal else "integer", "value": normalized}
    else:
        value_json = strict_json(token)
        if value_json is None:
            term = {"term": "null"}
        elif type(value_json) is bool:
            term = {"term": "boolean", "value": value_json}
        elif isinstance(value_json, str):
            term = {"term": "string", "value": value_json}
        else:
            raise SchemaViolationError("expected a single scalar literal")
    return ParsedLiteral(canonical_json(term), token)


@dataclass(frozen=True, slots=True)
class ValidatedProposal:
    """Retained structural proposal; it has no authentication or truth status."""

    payload_json: str
    registry_fingerprint: str
    source_sha256: str
    pending_checks: tuple[str, ...] = (
        "authenticated_ownership",
        "entity_and_evidence_resolution",
        "authority_and_binding",
        "target_applicability",
        "ecosystem_version_adapters",
    )

    @property
    def admission_status(self) -> str:
        return "untrusted_proposal"

    @property
    def payload(self) -> dict[str, Any]:
        return dict(json.loads(self.payload_json))


class SemanticValidator:
    def __init__(self, registry: SemanticRegistry | None = None) -> None:
        self.registry = registry or SemanticRegistry.load()
        try:
            library = import_module("jsonschema")
        except ImportError as error:
            raise CapabilityUnavailableError(
                "semantic validation requires the optional semantics extra"
            ) from error
        self._schema = self.registry.document("semantic-bundle.schema.json")
        library.Draft202012Validator.check_schema(self._schema)
        self._validator_type = library.Draft202012Validator
        self._format_checker = library.FormatChecker()
        self._vocabulary = self.registry.document("vocabulary.json")

    def _bounded_copy(self, payload: Any) -> Any:
        pending = [(payload, 0)]
        count = 0
        while pending:
            value, depth = pending.pop()
            count += 1
            if count > 100_000 or depth > 40:
                raise SchemaViolationError("semantic envelope exceeds structural limits")
            if isinstance(value, dict):
                if any(not isinstance(key, str) for key in value):
                    raise SchemaViolationError("semantic keys must be strings")
                pending.extend((item, depth + 1) for item in value.values())
            elif isinstance(value, list):
                pending.extend((item, depth + 1) for item in value)
            elif isinstance(value, str):
                try:
                    size = len(value.encode("utf-8", errors="strict"))
                except UnicodeError as error:
                    raise SchemaViolationError("semantic text is not valid UTF-8") from error
                if size > self._vocabulary["frame_limits"]["max_literal_utf8_bytes"]:
                    raise SchemaViolationError("semantic text exceeds literal byte budget")
            elif value is not None and type(value) not in (bool, int):
                raise SchemaViolationError("semantic numeric values use lossless typed strings")
        return strict_json(canonical_json(payload))

    def _shape(self, payload: Any, definition: str | None = None) -> None:
        schema = self._schema
        if definition is not None:
            schema = {"$ref": f"#/$defs/{definition}", "$defs": self._schema["$defs"]}
        validator = self._validator_type(schema, format_checker=self._format_checker)
        error = next(validator.iter_errors(payload), None)
        if error is not None:
            # No user payload or credential-like source text is echoed in errors.
            location = "/".join(str(part) for part in error.absolute_path) or "root"
            raise SchemaViolationError(f"semantic schema violation at {location}")

    def validate_term(self, term: Any) -> dict[str, Any]:
        value = self._bounded_copy(term)
        self._shape(value, "term")
        if value["term"] == "timestamp":
            self._timestamp(value["value"])
        return dict(value)

    @staticmethod
    def _timestamp(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise SchemaViolationError("invalid semantic timestamp") from error
        if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
            raise SchemaViolationError("semantic timestamps require explicit UTC")
        return parsed

    def _guard(self, value: dict[str, Any]) -> None:
        limits = self._vocabulary["guard_limits"]
        pending = [(value, 1)]
        nodes = 0
        bounded_nodes = []
        fields = {entry["id"]: entry for entry in self._vocabulary["context_fields"]}
        while pending:
            guard, depth = pending.pop()
            nodes += 1
            if depth > limits["max_depth"] or nodes > limits["max_nodes"]:
                raise SchemaViolationError("guard exceeds depth or node budget")
            if not isinstance(guard, dict) or not isinstance(guard.get("op"), str):
                raise SchemaViolationError("guard nodes require a registered operator")
            bounded_nodes.append(guard)
            operator = guard["op"]
            if operator in {"all", "any"}:
                children = guard.get("children", [])
                if (
                    not isinstance(children, list)
                    or len(children) > limits["max_children_per_boolean_node"]
                ):
                    raise SchemaViolationError("guard exceeds boolean child budget")
                pending.extend((child, depth + 1) for child in children)
            elif operator == "not":
                pending.append((guard.get("child", {}), depth + 1))
            elif operator == "in":
                members = guard.get("values", [])
                if not isinstance(members, list) or len(members) > limits["max_members_per_set"]:
                    raise SchemaViolationError("guard exceeds membership set budget")
            if operator not in self._vocabulary["guard_operators"]:
                raise SchemaViolationError("unregistered guard operator")
        # Validate node fields only after recursion and cardinality have been bounded.
        self._shape(value, "guard")
        for guard in bounded_nodes:
            operator = guard["op"]
            left = guard.get("left")
            if (
                isinstance(left, dict)
                and left.get("field") in fields
                and operator in {"eq", "ne", "in", "lt", "le", "gt", "ge"}
            ):
                field = fields[left["field"]]
                expected = field["value_type"]
                operands = guard.get("values", [guard.get("right")])
                operand_types = []
                for operand in operands:
                    if not isinstance(operand, dict):
                        raise SchemaViolationError("guard operands require typed terms")
                    kind = operand.get("term")
                    domain = operand.get("domain")
                    operand_types.append((kind, domain, operand.get("unit")))
                    if expected in self._vocabulary["enums"]:
                        if kind != "enum" or domain != expected:
                            raise SchemaViolationError("guard enum domain does not match field")
                    elif expected != "declared_key_type" and kind != expected:
                        raise SchemaViolationError("guard term type does not match field")
                    if expected == "entity" and operand.get("entity_type") != field.get(
                        "value_entity_type"
                    ):
                        raise SchemaViolationError("guard entity type does not match field")
                    if operator in {"lt", "le", "gt", "ge"} and kind not in {
                        "integer",
                        "decimal",
                        "timestamp",
                        "quantity",
                    }:
                        raise SchemaViolationError("ordered comparison requires an ordered scalar")
                if len(set(operand_types)) != 1:
                    raise SchemaViolationError(
                        "guard membership values must share one declared type"
                    )

    def validate_guard(self, guard: Any) -> dict[str, Any]:
        value = self._bounded_copy(guard)
        if not isinstance(value, dict):
            raise SchemaViolationError("guard must be an object")
        self._guard(value)
        return dict(value)

    def validate_bundle(self, payload: dict[str, Any] | str | bytes) -> ValidatedProposal:
        if isinstance(payload, str | bytes):
            try:
                raw = payload.encode("utf-8") if isinstance(payload, str) else payload
            except UnicodeError as error:
                raise SchemaViolationError("semantic bundle must be valid UTF-8") from error
            if len(raw) > 2_000_000:
                raise SchemaViolationError("semantic bundle exceeds byte budget")
            value = self._bounded_copy(strict_json(raw))
        else:
            value = self._bounded_copy(payload)
            raw = canonical_json(value).encode("utf-8")
        if not isinstance(value, dict):
            raise SchemaViolationError("semantic bundle must be an object")
        frames = value.get("frames", [])
        if (
            not isinstance(frames, list)
            or len(frames) > self._vocabulary["frame_limits"]["max_frames_per_bundle"]
        ):
            raise SchemaViolationError("semantic bundle exceeds frame budget")
        # Bound recursive guards before invoking the general schema validator.
        for frame in frames:
            if not isinstance(frame, dict):
                raise SchemaViolationError("semantic frame must be an object")
            applicability = frame.get("applicability")
            if not isinstance(applicability, dict):
                raise SchemaViolationError("semantic applicability must be an object")
            for guard in (frame.get("when"), applicability.get("guard")):
                if not isinstance(guard, dict):
                    raise SchemaViolationError("frame conditions require explicit guards")
                self._guard(guard)
        self._shape(value)
        identifiers: dict[str, dict[str, Any]] = {}
        for frame in frames:
            if frame["local_id"] in identifiers:
                raise SchemaViolationError("frame identifiers must be unique")
            identifiers[frame["local_id"]] = frame
            unknown = False
            pending = [frame]
            while pending:
                part = pending.pop()
                if isinstance(part, dict):
                    if part.get("term") == "unknown" or (
                        part.get("op") == "const" and part.get("value") == "unknown"
                    ):
                        unknown = True
                    if part.get("term") == "timestamp":
                        self._timestamp(part["value"])
                    pending.extend(part.values())
                elif isinstance(part, list):
                    pending.extend(part)
            if unknown and frame["coverage"]["status"] != "partial":
                raise SchemaViolationError("unresolved meaning requires explicit partial coverage")
            atom = frame["atom"]
            if atom["predicate"] == "sem:measurement":
                arguments = atom["arguments"]
                metric, amount = arguments["metric"], arguments["value"]
                if metric["term"] == "enum" and amount["term"] == "quantity":
                    expected_unit = {
                        "elapsed_time": "s",
                        "peak_memory": "B",
                        "input_tokens": "count",
                        "output_tokens": "count",
                    }[metric["value"]]
                    if amount["unit"] != expected_unit:
                        raise SchemaViolationError("measurement unit does not match metric")
            interval = frame["scope"]["valid_time"]
            if interval["kind"] == "interval":
                start, end = interval["start"], interval["end"]
                if start is None and end is None:
                    raise SchemaViolationError("use scope_only for an unbounded interval")
                begin = self._timestamp(start) if start is not None else None
                finish = self._timestamp(end) if end is not None else None
                if begin is not None and finish is not None and begin >= finish:
                    raise SchemaViolationError("valid-time interval must be nonempty and ordered")
        group_ids: set[str] = set()
        for group in value["conjunction_groups"]:
            identity = group["local_id"]
            if identity in identifiers or identity in group_ids:
                raise SchemaViolationError("frame and group identifiers share one namespace")
            group_ids.add(identity)
            if any(member not in identifiers for member in group["member_claim_ids"]):
                raise SchemaViolationError("conjunction member does not resolve to one frame")
            members = [identifiers[member] for member in group["member_claim_ids"]]
            qualifiers = ("mode", "force", "polarity", "when", "scope", "applicability")
            if any(any(member[key] != members[0][key] for key in qualifiers) for member in members):
                raise SchemaViolationError("conjunction members require shared qualifiers")
            if any(
                member["polarity"] == "negative" or member["force"] == "may" for member in members
            ):
                raise SchemaViolationError(
                    "coupled prohibition or permission remains unmapped source"
                )
        return ValidatedProposal(
            canonical_json(value), self.registry.fingerprint, hashlib.sha256(raw).hexdigest()
        )
