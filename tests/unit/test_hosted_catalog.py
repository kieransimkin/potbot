"""Scoped model-directory reads and discovery use local fake DB and HTTP boundaries."""

from __future__ import annotations

import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import uuid4

import httpx
import pytest

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.models.hosted_catalog import HostedModelDirectory
from potbot.models.hosted_policy import HostedPolicy, RateRule
from potbot.models.providers import HostedProviderClient, ProviderAPIError
from potbot.models.registry import DEFAULT_MODELS
from potbot.service_credentials import ServiceCredentials

KEY = "catalogue-private-key-canary-for-tests"


class ScopedDatabase:
    def __init__(self, *contexts):
        self.allowed = set(contexts)
        self.checked = []
        self.active = 0
        self.lock = threading.Lock()

    @contextmanager
    def transaction(self, context):
        with self.lock:
            self.checked.append(context)
            if context not in self.allowed:
                raise ScopeDeniedError("The requested resource is not accessible.")
            self.active += 1
        try:
            yield object()
        finally:
            with self.lock:
                self.active -= 1


class EmptyStore:
    def get(self, name):
        return None


def credentials():
    return ServiceCredentials(environment={"EVOLINK_API_KEY": KEY}, store=EmptyStore())


def policy(*rules, allow=True):
    return HostedPolicy(allow_prompt_disclosure=allow, rules=tuple(rules))


def rule(model="*", *, roles=("reasoning", "coding"), protocol=None):
    return RateRule("evolink", model, 1_000_000, 2_000_000, roles, protocol)


@pytest.fixture
def http_factory(monkeypatch):
    clients, calls, closed = [], [], []

    def install(handler):
        def make(resolver, *, authorize):
            def handle(request):
                calls.append(request)
                assert request.method == "GET"
                assert request.content == b""
                return handler(request)

            transport = httpx.Client(transport=httpx.MockTransport(handle))
            clients.append(transport)

            class RecordingClient(HostedProviderClient):
                def close(self):
                    closed.append(True)
                    super().close()

            return RecordingClient(resolver, http_client=transport, authorize=authorize)

        monkeypatch.setattr("potbot.models.hosted_catalog.HostedProviderClient", make)
        return calls, closed

    yield install
    for client in clients:
        client.close()


def test_catalog_returns_five_local_profiles_and_operator_models_without_network(http_factory):
    context = WorkspaceContext(uuid4(), uuid4())
    database = ScopedDatabase(context)
    http_factory(lambda request: pytest.fail("catalog must not make HTTP requests"))
    directory = HostedModelDirectory(
        database,
        policy(rule("gemini-future-model", roles=("reasoning",))),
        credentials(),
    )
    result = directory.catalog(context)
    assert database.checked == [context]
    assert [(m["canonical_model"], m["role"]) for m in result["local_models"]] == list(
        DEFAULT_MODELS
    )
    assert all(not m["qualified"] for m in result["local_models"])
    assert len(result["providers"]) == 5
    providers = {p["id"]: p for p in result["providers"]}
    assert providers["evolink"]["configured"] and providers["evolink"]["generation_enabled"]
    assert not providers["openai"]["configured"]
    model = result["models"][0]
    assert model["model"] == "gemini-future-model" and model["protocol"] == "gemini"
    assert model["roles"] == ["reasoning"] and model["generation_enabled"]
    assert model["text_generation"] is None and model["capability_verification_required"]
    assert model["source"] == "operator_policy" and not model["qualified"]
    assert KEY not in json.dumps(result)


def test_discovery_keeps_all_models_and_applies_roles_protocols_and_media_filters(http_factory):
    context = WorkspaceContext(uuid4(), uuid4())
    database = ScopedDatabase(context)

    def handle(request):
        assert database.active == 0
        assert str(request.url) == "https://api.evolink.ai/v1/models"
        assert request.headers["authorization"] == "Bearer " + KEY
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "claude-future", "type": "text"},
                    {"id": "opaque-code", "type": "text"},
                    {"id": "opaque-video", "type": "video"},
                    {"id": "gpt-image-future"},
                    {"id": "unknown-future"},
                ]
            },
        )

    calls, closed = http_factory(handle)
    directory = HostedModelDirectory(
        database,
        policy(rule(), rule("claude-future", roles=("coding",), protocol="chat_completions")),
        credentials(),
    )
    discovery = directory.discover(context, "evolink")
    assert discovery["provider"] == "evolink" and discovery["cached"]
    assert not discovery["qualified"] and len(discovery["models"]) == 5
    models = {m["model"]: m for m in discovery["models"]}
    assert models["claude-future"]["roles"] == ["coding"]
    assert models["claude-future"]["protocol"] == "chat_completions"
    assert models["opaque-code"]["generation_enabled"]
    assert models["unknown-future"]["generation_enabled"]
    assert models["unknown-future"]["capability_verification_required"]
    assert not models["opaque-video"]["generation_enabled"]
    assert not models["gpt-image-future"]["generation_enabled"]
    assert models["gpt-image-future"]["protocol"] is None
    assert len(database.checked) == 2 and len(calls) == 1 and closed == [True]
    result = directory.catalog(context)
    assert len(result["models"]) == 5 and len(calls) == 1
    assert database.checked == [context] * 3
    assert KEY not in json.dumps(result)


def test_discovery_does_not_grant_disclosure_or_missing_operator_rules(http_factory):
    context = WorkspaceContext(uuid4(), uuid4())
    database = ScopedDatabase(context)
    http_factory(lambda request: httpx.Response(200, json={"data": [{"id": "unknown-future"}]}))
    for configured_policy in (policy(), policy(rule(), allow=False)):
        directory = HostedModelDirectory(database, configured_policy, credentials())
        result = directory.discover(context, "evolink")
        assert not result["models"][0]["generation_enabled"]
        assert not result["models"][0]["qualified"]


def test_workspace_scope_is_checked_even_on_a_cache_hit_and_never_shared(http_factory):
    a, b = WorkspaceContext(uuid4(), uuid4()), WorkspaceContext(uuid4(), uuid4())
    database = ScopedDatabase(a, b)
    calls, _closed = http_factory(
        lambda request: httpx.Response(200, json={"data": [{"id": "workspace-a-private-model"}]})
    )
    directory = HostedModelDirectory(database, policy(rule()), credentials())
    directory.discover(a, "evolink")
    assert len(directory.catalog(a)["models"]) == 1
    assert directory.catalog(b)["models"] == []
    database.allowed.remove(a)
    with pytest.raises(ScopeDeniedError):
        directory.catalog(a)
    with pytest.raises(ScopeDeniedError):
        directory.discover(a, "evolink")
    assert len(calls) == 1


def test_discovery_revalidates_membership_after_http_before_publishing_or_caching(http_factory):
    context = WorkspaceContext(uuid4(), uuid4())
    database = ScopedDatabase(context)

    def handle(request):
        database.allowed.remove(context)
        return httpx.Response(200, json={"data": [{"id": "private-model"}]})

    _calls, closed = http_factory(handle)
    directory = HostedModelDirectory(database, policy(rule()), credentials())
    with pytest.raises(ScopeDeniedError):
        directory.discover(context, "evolink")
    assert closed == [True]
    database.allowed.add(context)
    assert directory.catalog(context)["models"] == []


def test_cache_ttl_and_lru_limits_do_not_return_partial_catalogs(http_factory):
    a, b = WorkspaceContext(uuid4(), uuid4()), WorkspaceContext(uuid4(), uuid4())
    database = ScopedDatabase(a, b)
    now = [1.0]
    http_factory(lambda request: httpx.Response(200, json={"data": [{"id": "full-model"}]}))
    directory = HostedModelDirectory(
        database,
        policy(rule()),
        credentials(),
        cache_ttl_seconds=10,
        max_cache_entries=1,
        clock=lambda: now[0],
    )
    directory.discover(a, "evolink")
    directory.discover(b, "evolink")
    assert directory.catalog(a)["models"] == []
    assert len(directory.catalog(b)["models"]) == 1
    now[0] += 10
    assert directory.catalog(b)["models"] == []


def test_oversized_cache_entry_returns_complete_discovery_without_caching(http_factory):
    context = WorkspaceContext(uuid4(), uuid4())
    database = ScopedDatabase(context)
    records = [{"id": "model-one"}, {"id": "model-two"}]
    http_factory(lambda request: httpx.Response(200, json={"data": records}))
    directory = HostedModelDirectory(database, policy(rule()), credentials(), max_cache_bytes=1)
    result = directory.discover(context, "evolink")
    assert len(result["models"]) == 2 and not result["cached"]
    assert directory.catalog(context)["models"] == []


@pytest.mark.parametrize("encoded", [False, True])
def test_provider_secret_echo_is_rejected_before_cache_or_public_metadata(http_factory, encoded):
    context = WorkspaceContext(uuid4(), uuid4())
    database = ScopedDatabase(context)
    text = base64.b64encode(KEY.encode()).decode() if encoded else KEY
    http_factory(
        lambda request: httpx.Response(
            200, json={"data": [{"id": "test-model", "display_name": text}]}
        )
    )
    directory = HostedModelDirectory(database, policy(rule()), credentials())
    with pytest.raises(ProviderAPIError) as caught:
        directory.discover(context, "evolink")
    assert KEY not in str(caught.value)
    assert directory.catalog(context)["models"] == []


@pytest.mark.parametrize("provider", ["unknown", "https://other.invalid", None, [], {}])
def test_invalid_provider_never_contacts_a_destination(http_factory, provider):
    context = WorkspaceContext(uuid4(), uuid4())
    database = ScopedDatabase(context)
    http_factory(lambda request: pytest.fail("must reject before HTTP"))
    directory = HostedModelDirectory(database, policy(), credentials())
    with pytest.raises(InvalidInputError):
        directory.discover(context, provider)
    assert database.checked == [context]


def test_cache_mutations_and_public_dict_mutations_cannot_corrupt_other_reads(http_factory):
    contexts = [WorkspaceContext(uuid4(), uuid4()) for _ in range(8)]
    database = ScopedDatabase(*contexts)
    http_factory(
        lambda request: httpx.Response(
            200, json={"data": [{"id": "concurrent-model", "type": "text"}]}
        )
    )
    directory = HostedModelDirectory(database, policy(rule()), credentials(), max_cache_entries=4)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(lambda context: directory.discover(context, "evolink"), contexts)
        )
    assert all(len(result["models"]) == 1 for result in results)
    result = directory.discover(contexts[0], "evolink")
    result["models"][0]["model"] = "modified"
    result["models"][0]["roles"].clear()
    copied = directory.catalog(contexts[0])["models"][0]
    assert copied["model"] == "concurrent-model" and copied["roles"] == ["reasoning", "coding"]


@pytest.mark.parametrize(
    "options",
    [
        {"cache_ttl_seconds": 0},
        {"cache_ttl_seconds": float("nan")},
        {"max_cache_entries": False},
        {"max_cache_entries": 513},
        {"max_cache_bytes": 0},
    ],
)
def test_cache_configuration_is_bounded(options):
    with pytest.raises(InvalidInputError):
        HostedModelDirectory(ScopedDatabase(), policy(), credentials(), **options)
