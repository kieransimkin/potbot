"""No-network transport verification for native and loopback model cohorts."""

from __future__ import annotations

import json

import httpx
import pytest

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.evaluation.agentic_model_bench import ModelTarget, RoleCase, prompt_for_case
from potbot.evaluation.agentic_model_transport import BenchmarkTransport
from potbot.models.hosted_policy import HostedPolicy, RateRule
from potbot.models.providers import (
    ProviderCompleted,
    ProviderTextDelta,
    ProviderUsage,
)


def _prompt():
    return prompt_for_case("test-model", RoleCase(
        "one", "triage", "Return route=local JSON", "json_subset", {"route": "local"}
    ), "generic")


def test_loopback_transport_retains_usage_and_never_accepts_redirect() -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        assert body["model"] == "tiny-model"
        assert body["stream"] is False
        assert len(body["messages"]) == 2
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"route":"local"}'}}],
                                         "usage": {"prompt_tokens": 12, "completion_tokens": 4},
                                         "model": "tiny-model"})

    client = httpx.Client(transport=httpx.MockTransport(respond), trust_env=False)
    model = ModelTarget("tiny", "tiny-model", "loopback_openai", ("triage",),
                        "rev-unknown", base_url="http://127.0.0.1:11434/v1")
    result = BenchmarkTransport(http_client=client).request(model, _prompt())
    assert result.text == '{"route":"local"}'
    assert result.output_tokens == 4
    assert result.billed_microunits is None
    assert len(requests) == 1
    assert str(requests[0].url) == "http://127.0.0.1:11434/v1/chat/completions"
    client.close()


def test_loopback_disallows_redirect_and_overlong_response() -> None:
    model = ModelTarget("tiny", "tiny-model", "loopback_openai", ("triage",),
                        "rev-unknown", base_url="http://localhost:11434/v1")
    with httpx.Client(transport=httpx.MockTransport(
        lambda _r: httpx.Response(302, headers={"location": "https://evil.test"})
    ), follow_redirects=False) as client:
        with pytest.raises(InvalidInputError):
            BenchmarkTransport(http_client=client).request(model, _prompt())


def test_hosted_requires_disclosure_policy_and_reports_provider_usage() -> None:
    rule = RateRule("openai", "test-model", 1000000, 2000000)
    policy = HostedPolicy(allow_prompt_disclosure=True, rules=(rule,))
    model = ModelTarget("openai", "test-model", "hosted", ("triage",), "rev-1",
                        provider="openai")

    class Credentials:
        def resolve(self, _ref):
            return "test-key"

    class FakeClient:
        def __init__(self, resolver, *, authorize):
            self.authorize = authorize

        def stream(self, request, _id):
            self.authorize(request)
            yield ProviderTextDelta('{"route":"local"}')
            yield ProviderCompleted("stop", ProviderUsage(10, 5, 15), model="test-model")

        def close(self):
            pass

    result = BenchmarkTransport(
        policy=policy, credentials=Credentials(), client_factory=FakeClient
    ).request(model, _prompt())
    assert result.input_tokens == 10
    assert result.output_tokens == 5
    assert result.billed_microunits == rule.charge(10, 5, total_tokens=15)
    with pytest.raises(ScopeDeniedError):
        BenchmarkTransport(
            policy=HostedPolicy(), credentials=Credentials(), client_factory=FakeClient
        ).request(model, _prompt())
