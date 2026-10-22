"""Offline compilation of a selected, self-contained OpenAPI subset."""
from __future__ import annotations

from copy import deepcopy

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.tools.api_openapi import compile_openapi


def document() -> dict:
    return {"openapi":"3.1.0","info":{"title":"Fixture","version":"v1"},
      "servers":[{"url":"http://evil.example/"}],
      "components":{"schemas":{"Result":{"type":"object","required":["value"],
                                         "properties":{"value":{"type":"integer"}}}}},
      "paths":{"/items/{id}":{"get":{"operationId":"get_items","parameters":[
          {"in":"path","name":"id","required":True,"schema":{"type":"string"}},
          {"in":"query","name":"limit","schema":{"type":"integer","minimum":1,"maximum":10}}],
          "responses":{"200":{"description":"ok","content":{"application/json":{"schema":{"$ref":"#/components/schemas/Result"}}}}}}
        }}}


def test_compile_selected_pinned_operation_without_following_servers() -> None:
    ops = compile_openapi(document(), selected_effects={"get_items":"read_only"})
    assert len(ops) == 1
    assert ops[0].target == "/items/{id}"
    assert ops[0].method == "GET"
    assert ops[0].output_schema_json.find("$ref") == -1
    assert ops[0].fingerprint == compile_openapi(document(),selected_effects={"get_items":"read_only"})[0].fingerprint


def test_reject_missing_unknown_and_unsafe_parameters() -> None:
    for mutate in (lambda d:d["paths"]["/items/{id}"]["get"]["parameters"].pop(0),
                   lambda d:d["paths"]["/items/{id}"]["get"]["parameters"][0].update({"in":"header"}),
                   lambda d:d["paths"]["/items/{id}"]["get"]["parameters"][0].update({"schema":{"type":"array"}}),
                   lambda d:d["paths"]["/items/{id}"]["get"]["parameters"].append(
                       {"in":"query","name":"limit","schema":{"type":"integer"}})):
        d = document(); mutate(d)
        with pytest.raises(InvalidInputError):
            compile_openapi(d,selected_effects={"get_items":"read_only"})


def test_reject_remote_cyclic_and_missing_schema_refs() -> None:
    for new in ({"$ref":"https://example.net/schema.json"},
                {"$ref":"#/components/schemas/Unknown"},
                {"$ref":"#/components/schemas/Result"}):
        d = document()
        d["components"]["schemas"]["Result"] = new
        with pytest.raises(InvalidInputError):
            compile_openapi(d,selected_effects={"get_items":"read_only"})


def test_reject_schema_drift_and_unselected_mutation() -> None:
    d = document()
    base = compile_openapi(d,selected_effects={"get_items":"read_only"})[0]
    changed = deepcopy(d)
    changed["info"]["version"] = "v2"
    assert compile_openapi(changed,selected_effects={"get_items":"read_only"})[0].fingerprint != base.fingerprint
    with pytest.raises(InvalidInputError):
        compile_openapi(d,selected_effects={"unknown_tool":"read_only"})
    with pytest.raises(InvalidInputError):
        compile_openapi(d,selected_effects={"get_items":"destructive"})
    with pytest.raises(InvalidInputError):
        compile_openapi({**d,"openapi":"3.0.1"},selected_effects={"get_items":"read_only"})


def test_reject_non_array_openapi_parameters_without_uncaught_type_error() -> None:
    d=document()
    d["paths"]["/items/{id}"]["get"]["parameters"]={"x":1}
    with pytest.raises(InvalidInputError):
        compile_openapi(d,selected_effects={"get_items":"read_only"})


def test_bad_openapi_media_shape_is_a_classified_input_error() -> None:
    for is_request in (False, True):
        d=document()
        method=d["paths"]["/items/{id}"]["get"]
        if is_request:
            method["requestBody"]={"content":{"application/json":[]}}
        else:
            method["responses"]["200"]["content"]["application/json"]=[]
        with pytest.raises(InvalidInputError):
            compile_openapi(d, selected_effects={"get_items":"read_only"})


def test_openapi_records_declared_success_status() -> None:
    d=document()
    response=d["paths"]["/items/{id}"]["get"]["responses"].pop("200")
    d["paths"]["/items/{id}"]["get"]["responses"]["201"]=response
    op=compile_openapi(d,selected_effects={"get_items":"read_only"})[0]
    assert op.success_statuses==(201,)
