"""T02.4 bounded OpenAPI 3.1 importer, not an executable API registry.

Only explicitly selected operations are imported. Server URLs, security schemes,
callbacks, webhooks and remote references are never followed. The operator binds
an origin and credentials separately under the controller's egress policy.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any

from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.tools.api_contracts import ApiOperation, _valid_schema

_ROUTE_PARAM = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_SUPPORTED = ("get", "post", "put", "patch", "delete")
_MAX_DOCUMENT = 512 * 1024


def _local_schema(value: object, definitions: Mapping[str, Any], *, seen: tuple[str, ...] = ()) -> dict[str, Any]:
    """Resolve a bounded subset of OpenAPI schema refs at compile time."""
    if not isinstance(value, dict):
        raise InvalidInputError("OpenAPI schema must be a JSON object")
    if "$ref" in value:
        ref = value["$ref"]
        if not isinstance(ref, str) or not re.fullmatch(r"#/components/schemas/[A-Za-z0-9_.-]+", ref):
            raise InvalidInputError("Only pinned local OpenAPI component schema references are accepted")
        slug = ref.rsplit("/", 1)[-1]
        if slug in seen or len(seen) >= 10 or slug not in definitions:
            raise InvalidInputError("OpenAPI schema has a missing or recursive reference")
        if set(value) != {"$ref"}:
            raise InvalidInputError("OpenAPI schema references with sibling fields are unsupported")
        return _local_schema(definitions[slug], definitions, seen=(*seen, slug))
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"$dynamicRef", "$recursiveRef"}:
            raise InvalidInputError("Dynamic OpenAPI references are not permitted")
        if key in {"properties", "patternProperties", "$defs"}:
            if not isinstance(item, dict):
                raise InvalidInputError("OpenAPI schema property set must be object")
            result[key] = {str(k): _local_schema(v, definitions, seen=seen) for k, v in item.items()}
        elif key in {"items", "additionalProperties", "not", "contains", "if", "then", "else"} and isinstance(item, dict):
            result[key] = _local_schema(item, definitions, seen=seen)
        elif key in {"anyOf", "allOf", "oneOf", "prefixItems"}:
            if not isinstance(item, list) or len(item) > 64:
                raise InvalidInputError("Unsupported OpenAPI schema alternatives")
            result[key] = [_local_schema(v, definitions, seen=seen) for v in item]
        else:
            result[key] = copy.deepcopy(item)
    _valid_schema(canonical_json(result))
    return result


def _parameter_schema(params: list[Any], definitions: Mapping[str, Any], *, location: str) -> dict[str, Any]:
    props: dict[str, Any] = {}
    required: list[str] = []
    for parameter in params:
        if not isinstance(parameter, dict) or "$ref" in parameter:
            raise InvalidInputError("OpenAPI parameter references require a separate pinned importer")
        where, name = parameter.get("in"), parameter.get("name")
        if where not in {"path", "query"}:
            raise InvalidInputError("OpenAPI header/cookie parameters are not model-controllable")
        if where != location:
            continue
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,39}", name) or name in props:
            raise InvalidInputError("Duplicate or unsupported API parameter")
        if parameter.get("style", "form" if where == "query" else "simple") != ("form" if where == "query" else "simple"):
            raise InvalidInputError("Nonstandard parameter serialization is not supported")
        if parameter.get("explode", where == "query") != (where == "query"):
            raise InvalidInputError("Exploded parameters require separate serializers")
        schema = _local_schema(parameter.get("schema"), definitions)
        if schema.get("type") not in {"string", "integer", "number", "boolean"}:
            raise InvalidInputError("Only scalar URL parameters are supported")
        props[name] = schema
        if parameter.get("required") is True or where == "path":
            required.append(name)
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def compile_openapi(
    document: Mapping[str, Any], *, selected_effects: Mapping[str, str]
) -> tuple[ApiOperation, ...]:
    """Compile the explicitly allowed operations; no endpoints are contacted."""
    if not isinstance(document, Mapping) or not isinstance(selected_effects, Mapping):
        raise InvalidInputError("Operator-provided OpenAPI document and allowlist required")
    try:
        digest = content_hash(document)
        document_size = len(canonical_json(document).encode("utf-8"))
    except (TypeError, ValueError, RecursionError) as error:
        raise InvalidInputError("Malformed OpenAPI document") from error
    if document_size > _MAX_DOCUMENT or not str(document.get("openapi", "")).startswith("3.1."):
        raise InvalidInputError("Only bounded OpenAPI 3.1 JSON documents are supported")
    if not selected_effects or len(selected_effects) > 128:
        raise InvalidInputError("Select 1..128 explicit operation IDs")
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise InvalidInputError("OpenAPI paths must be object")
    components = document.get("components", {})
    if not isinstance(components, dict):
        raise InvalidInputError("Malformed OpenAPI components")
    definitions = components.get("schemas", {})
    if not isinstance(definitions, dict):
        raise InvalidInputError("OpenAPI schema definitions must be object")
    found: dict[str, ApiOperation] = {}
    for route, item in paths.items():
        if not isinstance(route, str) or not isinstance(item, dict) or "$ref" in item:
            raise InvalidInputError("OpenAPI path must be inline and bounded")
        for method in _SUPPORTED:
            operation = item.get(method)
            if operation is None:
                continue
            if not isinstance(operation, dict):
                raise InvalidInputError("OpenAPI operation must be object")
            identifier = operation.get("operationId")
            if identifier not in selected_effects:
                continue
            if identifier in found or not isinstance(identifier, str):
                raise InvalidInputError("OpenAPI operation IDs must be unique")
            path_params = item.get("parameters", [])
            operation_params = operation.get("parameters", [])
            if not isinstance(path_params, list) or not isinstance(operation_params, list):
                raise InvalidInputError("OpenAPI parameters must be arrays")
            params = path_params + operation_params
            if len(params) > 64:
                raise InvalidInputError("OpenAPI parameters exceed supported bounds")
            path_schema = _parameter_schema(params, definitions, location="path")
            query_schema = _parameter_schema(params, definitions, location="query")
            markers = set(_ROUTE_PARAM.findall(route))
            if markers != set(path_schema["properties"]):
                raise InvalidInputError("OpenAPI route placeholders must match the path parameters")
            in_props: dict[str, Any] = {"path": path_schema, "query": query_schema}
            if "requestBody" in operation:
                body = operation["requestBody"]
                if not isinstance(body, dict) or "$ref" in body:
                    raise InvalidInputError("OpenAPI request body must be inline")
                content = body.get("content", {})
                if not isinstance(content, dict) or not isinstance(content.get("application/json"), dict):
                    raise InvalidInputError("Only JSON request bodies are supported")
                in_props["body"] = _local_schema(content["application/json"].get("schema"), definitions)
            if method == "get" and "body" in in_props:
                raise InvalidInputError("GET request bodies are unsupported")
            responses = operation.get("responses", {})
            if not isinstance(responses, dict):
                raise InvalidInputError("OpenAPI responses must be object")
            preferred = "200" if "200" in responses else "201"
            response = responses.get(preferred)
            if not isinstance(response, dict) or "$ref" in response:
                raise InvalidInputError("Selected OpenAPI operation needs explicit 200/201 response")
            media = response.get("content", {})
            if not isinstance(media, dict) or not isinstance(media.get("application/json"), dict):
                raise InvalidInputError("Only JSON response bodies are supported")
            schema = _local_schema(media["application/json"].get("schema"), definitions)
            input_schema = {"type": "object", "properties": in_props,
                            "required": ["body"] if isinstance(operation.get("requestBody"), dict) and operation["requestBody"].get("required") else [],
                            "additionalProperties": False}
            effect = selected_effects[identifier]
            found[identifier] = ApiOperation(
                name=identifier, protocol="rest", effect_class=effect,
                target=route, method=method.upper(),
                input_schema_json=canonical_json(input_schema),
                output_schema_json=canonical_json(schema),
                source_hash=digest,
                success_statuses=(int(preferred),),
            )
    if set(found) != set(selected_effects):
        raise InvalidInputError("Requested OpenAPI operation missing or not supported")
    return tuple(found[name] for name in sorted(found))
