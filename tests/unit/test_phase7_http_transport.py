"""Configured HTTPS egress contract; no arbitrary redirects or filesystem access."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from potbot.domain.errors import InvalidInputError
from potbot.research.http_transport import FetchPermit, FetchStatus, fetch_approved


def permit(**kwargs):
    return FetchPermit(
        exact_url=kwargs.get("exact_url", "https://api.official.example/v1/search"),
        reservation_id=uuid4(),
        action_id=uuid4(),
        disclosure_decision_id=uuid4(),
        max_bytes=kwargs.get("max_bytes", 100),
        deadline_seconds=kwargs.get("deadline_seconds", 1.0),
    )


def fetch_case(response, p=None):
    async def runner():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: response)) as client:
            return await fetch_approved(p or permit(), client=client)

    return asyncio.run(runner())


def test_successful_bounded_result_and_exact_digest():
    result = fetch_case(httpx.Response(200, content=b"fixture document"))
    assert result.status == FetchStatus.FETCHED
    assert result.received_bytes == len(b"fixture document")
    assert result.sha256 is not None
    assert result.data == b"fixture document"


def test_redirect_does_not_follow_to_internal_network():
    requests = []

    def handler(request):
        requests.append(request.url)
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/latest/token"})

    async def runner():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_approved(permit(), client=client)

    result = asyncio.run(runner())
    assert result.status == FetchStatus.REDIRECT_DENIED
    assert len(requests) == 1
    assert result.data is None


def test_large_body_by_header_and_unknown_length():
    assert (
        fetch_case(
            httpx.Response(200, headers={"Content-Length": "900"}, content=b"a" * 900)
        ).status
        == FetchStatus.RESPONSE_TOO_LARGE
    )
    assert (
        fetch_case(httpx.Response(200, content=b"a" * 120)).status == FetchStatus.RESPONSE_TOO_LARGE
    )


def test_rate_limit_and_http_failures_classified():
    assert fetch_case(httpx.Response(429)).status == FetchStatus.RATE_LIMITED
    assert fetch_case(httpx.Response(503)).status == FetchStatus.UNAVAILABLE


def test_timeout_is_bounded_even_when_mock_provider_hangs():
    async def slow(request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, content=b"late")

    async def runner():
        async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
            return await fetch_approved(permit(deadline_seconds=0.01), client=client)

    assert asyncio.run(runner()).status == FetchStatus.TIMED_OUT


def test_unsafe_urls_and_unsupported_limits_rejected():
    urls = (
        "http://api.test/search",
        "https://127.0.0.1/v1",
        "https://localhost/v1",
        "https://api.test:8080/secret",
        "https://name:pass@api.test/v1",
        "https://api.test/v1?token=abc",
        "https://api.test/v1#fragment",
        "https://metadata/v1",
    )
    for url in urls:
        with pytest.raises(InvalidInputError):
            permit(exact_url=url)
    with pytest.raises(InvalidInputError):
        replace(permit(), max_bytes=2**30)
    with pytest.raises(InvalidInputError):
        replace(permit(), deadline_seconds=True)
