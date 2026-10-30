"""Bounded untrusted HTTP inputs: no PostgreSQL required."""
import asyncio
from uuid import uuid4

import pytest
from starlette.requests import Request

from potbot.api.ui_context import _integer, _json_small, _params, _target
from potbot.domain.errors import InvalidInputError


def get_request(query: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/test",
                    "query_string": query.encode(), "headers": []})


def test_target_validation_and_query_duplicates():
    assert _target({"kind": "sandbox", "name": str(uuid4()), "role": "MODEL.md"})
    with pytest.raises(InvalidInputError):
        _target({"kind": "sandbox", "name": "../etc", "role": "MODEL.md"})
    with pytest.raises(InvalidInputError):
        _target({"kind": "agent", "name": "x", "role": "model.md"})
    with pytest.raises(InvalidInputError):
        _params(get_request("kind=sandbox&kind=agent"), frozenset({"kind"}))
    with pytest.raises(InvalidInputError):
        _params(get_request("password=x"), frozenset({"kind"}))


def test_revision_is_bounded_and_unsigned():
    assert _integer("1", maximum=100) == 1
    assert _integer(None, maximum=100, default=25) == 25
    for value in ("-1", "1.5", "abc", "9999999999999999999"):
        with pytest.raises(InvalidInputError):
            _integer(value, maximum=100)


def request_body(data: bytes, content_type: str = "application/json") -> Request:
    consumed = False
    async def receive():
        nonlocal consumed
        if consumed:
            return {"type": "http.request", "body": b"", "more_body": False}
        consumed = True
        return {"type": "http.request", "body": data, "more_body": False}
    return Request({"type": "http", "method": "POST", "path": "/context",
                    "headers": [(b"content-type", content_type.encode())]}, receive)


def test_reject_duplicate_json_keys():
    request = request_body(b'{"kind":"agent","kind":"sandbox"}')
    with pytest.raises(InvalidInputError):
        asyncio.run(_json_small(request))


def test_reject_oversized_json():
    request = request_body(b'{"text":"' + b'a'*9000 + b'"}')
    with pytest.raises(InvalidInputError):
        asyncio.run(_json_small(request))
