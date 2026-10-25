"""S39 offline request-arbitration contracts; never require external network."""
from __future__ import annotations

import asyncio
import json
import os
import ssl
from pathlib import Path

import pytest

from potbot.tools.browser_https_routes import (
    EgressDenied, RoutedResponse, checked_target, fetch_https, install_https_routes,
    require_egress_config,
)
from potbot.tools.browser_operator_policy import server_readonly_required

TOKEN = "t" * 44
SOCKET = "/home/.tmp/.potbot-connect/egress.sock"


@pytest.mark.parametrize("url", [
    "http://example.com/", "https://127.0.0.1/", "https://localhost/",
    "https://example.com:8443/", "https://user:secret@example.com/",
    "https://xn--bcher-kva.example/", "https://example.com./",
    "https://example.com:443@somewhere.example/", "https://EXAMPLE.com/",
    "javascript:alert(1)", "file:///home/private", "https://a..com/",
    "https://example.com/\nHost: evil.example", "https://example.com/\x00",
])
def test_no_unapproved_scheme_authority_or_header_injection(url):
    with pytest.raises(EgressDenied):
        checked_target(url, "GET")


def test_only_https_get_head_and_fragment_stripped():
    assert checked_target("https://example.com/path?x=1#secret", "GET") == (
        "example.com", "/path?x=1")
    assert checked_target("https://example.com:443", "HEAD") == ("example.com", "/")
    for method in ("POST", "PUT", "DELETE", "OPTIONS", "CONNECT"):
        with pytest.raises(EgressDenied):
            checked_target("https://example.com", method)


def test_complete_private_broker_capability_required():
    assert require_egress_config({}) is None
    assert require_egress_config({"POTBOT_BROWSER_CONNECT_SOCKET": SOCKET,
                                  "POTBOT_BROWSER_CONNECT_TOKEN_FILE": "/home/.tmp/.potbot-connect/token"},
                                  read_token=lambda _: TOKEN) == (SOCKET, TOKEN)
    with pytest.raises(EgressDenied):
        require_egress_config({"POTBOT_BROWSER_CONNECT_TOKEN_FILE": "/home/.tmp/.potbot-connect/token"})
    with pytest.raises(EgressDenied):
        require_egress_config({"POTBOT_BROWSER_CONNECT_SOCKET": "/etc/passwd",
                               "POTBOT_BROWSER_CONNECT_TOKEN_FILE": "/home/.tmp/.potbot-connect/token"})


def test_tls_must_verify_hostname_before_creating_any_connection():
    unverified = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    unverified.check_hostname = False
    unverified.verify_mode = ssl.CERT_NONE
    with pytest.raises(EgressDenied):
        fetch_https("https://example.com", "GET", socket_path=SOCKET,
                    token=TOKEN, ssl_context=unverified,
                    create_socket=lambda: pytest.fail("unverified connection opened"))


class FakeRequest:
    def __init__(self, url: str, method: str = "GET") -> None:
        self.url, self.method = url, method


class FakeRoute:
    def __init__(self, url: str, method: str = "GET") -> None:
        self.request = FakeRequest(url, method)
        self.fulfilled: dict | None = None
        self.aborted: str | None = None
        self.continued = False

    async def fulfill(self, **kwargs):
        self.fulfilled = kwargs

    async def abort(self, reason):
        self.aborted = reason

    async def continue_(self):
        self.continued = True


class FakeContext:
    def __init__(self):
        self.pattern = None
        self.callback = None

    async def route(self, pattern, callback):
        self.pattern, self.callback = pattern, callback


def test_interceptor_only_fulfills_eligible_https_gets_without_direct_fallback():
    calls = []
    def fake_fetch(url, method, *, socket_path, token):
        calls.append((url, method, socket_path, token))
        return RoutedResponse(200, {"content-type": "text/plain"}, b"safe response")

    async def exercise():
        ctx = FakeContext()
        await install_https_routes(ctx, SOCKET, TOKEN, fetch=fake_fetch)
        assert ctx.pattern == "**/*"
        good = FakeRoute("https://example.com/doc")
        await ctx.callback(good)
        assert good.fulfilled == {"status": 200, "headers": {"content-type": "text/plain"},
                                  "body": b"safe response"}
        assert calls == [("https://example.com/doc", "GET", SOCKET, TOKEN)]
        for url, method in (("http://example.com", "GET"),
                            ("https://example.com", "POST"),
                            ("https://127.0.0.1", "GET")):
            route = FakeRoute(url, method)
            await ctx.callback(route)
            assert route.aborted == "blockedbyclient"
            assert not route.continued and route.fulfilled is None
        assert len(calls) == 1
    asyncio.run(exercise())


def test_upstream_exception_cannot_become_direct_network_call():
    def broken(*args, **kwargs):
        raise OSError("connection refused: private data")
    async def exercise():
        ctx = FakeContext()
        await install_https_routes(ctx, SOCKET, TOKEN, fetch=broken)
        route = FakeRoute("https://example.com")
        await ctx.callback(route)
        assert route.aborted == "blockedbyclient"
        assert route.fulfilled is None
        assert not route.continued
    asyncio.run(exercise())


def test_oversized_reply_is_denied():
    def oversized(*args, **kwargs):
        return RoutedResponse(200, {}, b"x" * (4 * 1024 * 1024 + 1))
    async def exercise():
        ctx = FakeContext()
        await install_https_routes(ctx, SOCKET, TOKEN, fetch=oversized)
        route = FakeRoute("https://example.com")
        await ctx.callback(route)
        assert route.aborted == "blockedbyclient"
    asyncio.run(exercise())


def test_strict_xpra_requires_private_policy_and_lease(tmp_path):
    path = tmp_path / "private.json"
    def write(value):
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)
    write({"browserLeases": True, "strictXpraInput": True})
    assert server_readonly_required(path) is True
    write({"browserLeases": True})
    with pytest.raises(ValueError): server_readonly_required(path)
    write({"browserLeases": True, "strictXpraInput": True, "browserEgress": {}})
    assert server_readonly_required(path) is True  # full loader validates broker policy
    write({"browserLeases": False, "strictXpraInput": True})
    with pytest.raises(ValueError): server_readonly_required(path)
    write({"browserLeases": True, "strictXpraInput": True})
    path.chmod(0o644)
    with pytest.raises(ValueError): server_readonly_required(path)


def test_duplicate_json_policy_keys_rejected(tmp_path):
    path = tmp_path / "operator.json"
    path.write_text('{"browserLeases":true,"browserLeases":true,"strictXpraInput":true}')
    path.chmod(0o600)
    with pytest.raises(ValueError): server_readonly_required(path)
