"""T02.4 operator-pinned API contracts. These declarations confer no authority.

An API operation's endpoint, HTTP method, GraphQL document, or gRPC method is
chosen by a trusted installer, not supplied by a model invocation. Intent and
observations use Potbot's existing capability-contract types.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.domain.syn import parse_type_ref
from potbot.tools.capability_contracts import (
    TOOL_DEFINITION_TYPE,
    ToolCapabilityDescriptor,
    ToolInstallationDescriptor,
    ToolInvocationIntent,
    ToolObservation,
)

_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,79}\Z", re.ASCII)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_GRPC_METHOD = re.compile(r"/[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*/[A-Za-z_]\w*\Z", re.ASCII)
_PATH = re.compile(r"/(?:[A-Za-z0-9._~-]|\{[a-z][a-z0-9_]*\}|/)*\Z", re.ASCII)
_EFFECTS = frozenset({"read_only", "idempotent_write", "non_idempotent_write", "destructive"})
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_MAX_SCHEMA = 131072
_MAX_JSON_BYTES = 2 * 1024 * 1024


def _valid_schema(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_SCHEMA:
        raise InvalidInputError("API schema exceeds its byte limit")
    try:
        schema = json.loads(raw)
        if not isinstance(schema, dict) or canonical_json(schema) != raw:
            raise InvalidInputError("API schema must be canonical JSON object")
        _no_remote_refs(schema)
        from jsonschema import Draft202012Validator
        Draft202012Validator.check_schema(schema)
    except ImportError as error:
        from potbot.domain.errors import CapabilityUnavailableError
        raise CapabilityUnavailableError("API schema validation requires the semantics extra") from error
    except (ValueError, TypeError, RecursionError) as error:
        raise InvalidInputError("API schema is malformed") from error
    except Exception as error:
        # jsonschema.SchemaError must not publish a server-supplied schema fragment.
        raise InvalidInputError("API schema is invalid or unsupported") from error
    return schema


def _no_remote_refs(document: Any) -> None:
    """Never resolve arbitrary remote/local files or complex recursive schemas."""
    def visit(value: Any, depth: int) -> None:
        if depth > 40:
            raise InvalidInputError("API schema is too deeply nested")
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
                    # This stage uses fully compiled self-contained JSON schemas.
                    raise InvalidInputError("API schemas cannot contain unresolved references")
                visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, depth + 1)
    visit(document, 0)


def validate_json(schema_json: str, value: Any, *, label: str) -> None:
    """Validate strict finite JSON; output schemas are always checked too."""
    try:
        if len(canonical_json(value).encode("utf-8")) > _MAX_JSON_BYTES:
            raise InvalidInputError(f"{label} exceeds JSON byte limit")
        from jsonschema import Draft202012Validator
        schema = _valid_schema(schema_json)
        if not Draft202012Validator(schema).is_valid(value):
            raise InvalidInputError(f"{label} does not match its pinned JSON schema")
    except (ValueError, TypeError, RecursionError) as error:
        raise InvalidInputError(f"{label} must be finite JSON") from error


@dataclass(frozen=True, slots=True)
class ApiOperation:
    """Operator-curated, stable API operation. Not a dynamic API explorer."""

    name: str
    protocol: str  # rest, graphql, grpc
    effect_class: str
    target: str  # fixed relative HTTP path template OR full gRPC /service/method
    method: str  # GET/POST/...; grpc has the sentinel UNARY
    input_schema_json: str
    output_schema_json: str
    source_hash: str
    input_type_ref: str = "synny://type/potbot-data-record"
    output_type_ref: str = "synny://type/potbot-data-record"
    graphql_document: str | None = None
    timeout_ms: int = 15000
    max_output_bytes: int = 262144
    page_cursor_parameter: str | None = None
    next_cursor_field: str | None = None
    max_pages: int = 1
    success_statuses: tuple[int, ...] = (200,)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME.fullmatch(self.name):
            raise InvalidInputError("Invalid API operation name")
        if self.protocol not in {"rest", "graphql", "grpc"} or self.effect_class not in _EFFECTS:
            raise InvalidInputError("Invalid API protocol or effect class")
        if not isinstance(self.source_hash, str) or not _DIGEST.fullmatch(self.source_hash):
            raise InvalidInputError("API schema provenance must be SHA-256")
        parse_type_ref(self.input_type_ref)
        parse_type_ref(self.output_type_ref)
        _valid_schema(self.input_schema_json)
        _valid_schema(self.output_schema_json)
        if type(self.timeout_ms) is not int or not 1 <= self.timeout_ms <= 60000:
            raise InvalidInputError("API deadline must be 1..60000 ms")
        if type(self.max_output_bytes) is not int or not 1 <= self.max_output_bytes <= 1048576:
            raise InvalidInputError("API result size must be 1..1048576 bytes")
        if type(self.max_pages) is not int or not 1 <= self.max_pages <= 10:
            raise InvalidInputError("Pagination must have a strict page limit")
        if (not isinstance(self.success_statuses, tuple) or not 1 <= len(self.success_statuses) <= 8
                or any(type(x) is not int or not 200 <= x <= 299 for x in self.success_statuses)
                or len(set(self.success_statuses)) != len(self.success_statuses)):
            raise InvalidInputError("API successful HTTP status codes must be pinned")
        if self.protocol in {"rest", "graphql"}:
            if self.method not in _METHODS or not isinstance(self.target, str) or not _PATH.fullmatch(self.target):
                raise InvalidInputError("HTTP operation requires one safe relative path template")
            if self.target.startswith("//") or "/../" in self.target or "/./" in self.target:
                raise InvalidInputError("API path cannot traverse directories")
        else:
            if self.method != "UNARY" or not isinstance(self.target, str) or not _GRPC_METHOD.fullmatch(self.target):
                raise InvalidInputError("gRPC operation requires a pinned unary method")
        if self.protocol == "graphql":
            doc = self.graphql_document
            if self.method != "POST" or not isinstance(doc, str) or len(doc) > 32768:
                raise InvalidInputError("GraphQL requires fixed bounded POST document")
            header = re.match(r"\s*(query|mutation)\s+([A-Za-z_]\w*)\b", doc)
            if not header or header.group(2) != self.name.replace("-", "_").replace(".", "_"):
                raise InvalidInputError("GraphQL operation must have its own pinned operation name")
            if (header.group(1) == "query") != (self.effect_class == "read_only"):
                raise InvalidInputError("GraphQL query/mutation effect class mismatch")
        elif self.graphql_document is not None:
            raise InvalidInputError("Only GraphQL operations have documents")
        if self.protocol == "rest" and self.method not in ("GET",) and self.effect_class == "read_only":
            raise InvalidInputError("Non-GET REST calls must not silently be read-only")
        if self.protocol == "rest" and self.method == "GET" and self.effect_class != "read_only":
            raise InvalidInputError("GET cannot be registered as a mutation")
        if self.page_cursor_parameter is not None or self.next_cursor_field is not None or self.max_pages != 1:
            if not (
                self.protocol == "rest" and self.method == "GET"
                and self.effect_class == "read_only"
                and self.page_cursor_parameter and self.next_cursor_field
                and re.fullmatch(r"[a-z][a-z0-9_]{0,40}", self.page_cursor_parameter)
                and re.fullmatch(r"[a-z][a-z0-9_]{0,40}", self.next_cursor_field)
            ):
                raise InvalidInputError("Pagination is only available for bounded REST reads")

    @property
    def fingerprint(self) -> str:
        return content_hash({
            "name": self.name, "protocol": self.protocol, "effect_class": self.effect_class,
            "target": self.target, "method": self.method,
            "input_schema_json": self.input_schema_json, "output_schema_json": self.output_schema_json,
            "source_hash": self.source_hash, "input_type_ref": self.input_type_ref,
            "output_type_ref": self.output_type_ref, "graphql_document": self.graphql_document,
            "timeout_ms": self.timeout_ms, "max_output_bytes": self.max_output_bytes,
            "page_cursor_parameter": self.page_cursor_parameter,
            "next_cursor_field": self.next_cursor_field, "max_pages": self.max_pages,
            "success_statuses": self.success_statuses,
        })

    def to_capability(self) -> ToolCapabilityDescriptor:
        return ToolCapabilityDescriptor(
            name=self.name, definition_type_ref=TOOL_DEFINITION_TYPE,
            input_type_ref=self.input_type_ref, output_type_ref=self.output_type_ref,
            protocol=self.protocol, effect_class=self.effect_class,
            input_schema_json=self.input_schema_json,
            output_schema_json=self.output_schema_json,
            description=f"Pinned {self.protocol} {self.method} {self.target}",
        )


@dataclass(frozen=True, slots=True)
class ApiCallScope:
    """Controller-supplied intent, installation and immutable call authority."""

    installation: ToolInstallationDescriptor
    intent: ToolInvocationIntent
    operation_hash: str
    authorize: Any  # trusted synchronous callback(op, arguments, intent) -> None
    egress: Any | None = None  # trusted network-enforcing callback(url) -> None

    def check(self, operation: ApiOperation, arguments: Mapping[str, Any]) -> None:
        if not isinstance(operation, ApiOperation) or not isinstance(arguments, Mapping):
            raise InvalidInputError("API call needs a pinned operation and arguments")
        if not isinstance(self.installation, ToolInstallationDescriptor) or not isinstance(self.intent, ToolInvocationIntent):
            raise ScopeDeniedError("Controller must supply immutable installation and intent")
        if not isinstance(self.operation_hash, str) or not _DIGEST.fullmatch(self.operation_hash) or self.operation_hash != operation.fingerprint:
            raise ScopeDeniedError("API operation is not the pinned registered operation")
        if not callable(self.authorize):
            raise ScopeDeniedError("Missing controller call admission")
        if self.intent.installation_hash != self.installation.fingerprint:
            raise ScopeDeniedError("API operation installation identity mismatch")
        if self.intent.workspace_id != self.installation.workspace_id:
            raise ScopeDeniedError("API call crosses workspace")
        if self.intent.policy_revision_hash != self.installation.policy_revision_hash:
            raise StateMismatchError("API call uses a stale policy")
        if self.intent.effect_class != operation.effect_class:
            raise ScopeDeniedError("API call effect class is not permitted")
        if self.intent.input_type_ref != operation.input_type_ref:
            raise ScopeDeniedError("API call input semantic type is not pinned")
        if self.intent.input_hash != content_hash(arguments):
            raise ScopeDeniedError("API call arguments do not match the immutable intent")
        if self.intent.max_wall_time_ms < operation.timeout_ms or self.intent.max_output_bytes < operation.max_output_bytes:
            raise ScopeDeniedError("API invocation budgets are below operation bounds")
        validate_json(operation.input_schema_json, dict(arguments), label="API arguments")
        # Never lend caller-owned nested objects to a privileged admission hook.
        approved_snapshot = json.loads(canonical_json(dict(arguments)))
        if self.authorize(operation, approved_snapshot, self.intent) is not None:
            raise ScopeDeniedError("Controller admission callback must approve by returning None")


@dataclass(frozen=True, slots=True)
class ApiCallResult:
    """Untrusted observed API outcome. Never means independently verified."""

    operation_hash: str
    status: str
    value: Any | None
    error_code: str | None
    http_status: int | None = None
    attempts: int = 0
    pages: int = 0
    value_hash: str | None = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.operation_hash, str) or not _DIGEST.fullmatch(self.operation_hash):
            raise InvalidInputError("API result requires pinned operation identity")
        if self.status not in {"succeeded", "failed", "indeterminate"}:
            raise InvalidInputError("Unknown API observation status")
        if self.status == "succeeded" and self.error_code is not None:
            raise InvalidInputError("Successful API call cannot declare an error")
        if self.status != "succeeded" and self.value is not None:
            raise InvalidInputError("Failed API call cannot publish unverified data")
        if self.status != "succeeded" and not self.error_code:
            raise InvalidInputError("Failed API calls need error codes")
        if self.status == "succeeded" and len(canonical_json(self.value).encode("utf-8")) > _MAX_JSON_BYTES:
            raise InvalidInputError("API observation value exceeds its bound")
        object.__setattr__(self, "value_hash", content_hash(self.value) if self.status == "succeeded" else None)
        if type(self.attempts) is not int or not 0 <= self.attempts <= 20:
            raise InvalidInputError("Invalid attempt count")
        if type(self.pages) is not int or not 0 <= self.pages <= 20:
            raise InvalidInputError("Invalid page count")

    @property
    def fingerprint(self) -> str:
        return content_hash({"operation": self.operation_hash, "status": self.status,
                             "output_hash": self.value_hash, "error": self.error_code,
                             "http_status": self.http_status, "attempts": self.attempts, "pages": self.pages})

    def to_observation(self, intent: ToolInvocationIntent, operation: ApiOperation) -> ToolObservation:
        if self.operation_hash != operation.fingerprint:
            raise StateMismatchError("API observation differs from operation")
        if intent.input_type_ref != operation.input_type_ref or intent.effect_class != operation.effect_class:
            raise ScopeDeniedError("API observation does not match the invocation")
        if self.status == "succeeded" and self.value_hash != content_hash(self.value):
            raise StateMismatchError("API result was mutated after transport observation")
        return ToolObservation(
            invocation_hash=intent.fingerprint, output_type_ref=operation.output_type_ref,
            status=self.status,
            output_hash=self.value_hash if self.status == "succeeded" else None,
            artifact_sha256=None, error_code=self.error_code,
        )


def validated_service_origin(value: str, *, test_loopback: bool = False) -> str:
    """Syntactic preflight. Production still requires an *enforcing* egress proxy."""
    import ipaddress
    if not isinstance(value, str) or len(value) > 1024 or any(ord(x) < 33 or ord(x) > 126 for x in value):
        raise InvalidInputError("Invalid service origin")
    try:
        p = urlsplit(value)
        host, port = p.hostname, p.port
    except ValueError as error:
        raise InvalidInputError("Invalid service origin") from error
    if not host or p.query or p.fragment or p.username or p.password or p.path not in {"", "/"}:
        raise InvalidInputError("Service origin must not embed paths, queries or credentials")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if test_loopback and p.scheme == "http" and ip is not None and ip.is_loopback:
        return value.rstrip("/")
    if p.scheme != "https" or port not in (None, 443) or ip is not None or not host.isascii() or "." not in host or host.endswith(".localhost"):
        raise ScopeDeniedError("Remote APIs require public-name HTTPS and approved egress")
    return value.rstrip("/")
