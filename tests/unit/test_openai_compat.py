"""Offline OpenAI SDK-shaped transport and virtual-model regression tests."""

from __future__ import annotations

import io
import json
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from potbot.api.observability import EventLogger, TransferProgress
from potbot.api.openai_compat import OpenAICompatibility
from potbot.api.server import ServerSettings, create_app
from potbot.api.tasks import DevelopmentBearerAuth
from potbot.domain.auth import WorkspaceContext
from potbot.memory.embedding_profile import EmbeddingBatch
from potbot.models.hosted_policy import HostedPolicy, RateRule
from potbot.models.providers import ProviderCompleted, ProviderTextDelta, ProviderUsage

TOKEN = "openai-test-token-" + "x" * 40


class FakeProvider:
    calls: list[Any] = []

    def __init__(self, resolver: Any, *, authorize: Any) -> None:
        self.resolve = resolver
        self.authorize = authorize

    def stream(self, request: Any, operation_id: Any, cancelled: Any = None) -> Any:
        self.authorize(request)
        self.resolve(request.selection.credential_ref)
        self.calls.append(request)
        yield ProviderTextDelta("Answer ")
        yield ProviderTextDelta("42")
        yield ProviderCompleted("stop", ProviderUsage(8, 2, 10))

    def close(self) -> None:
        pass


class Credentials:
    def resolve(self, key: str) -> str:
        assert key == "secret://providers/openai"
        return "provider-key-private-canary"


def make_client(*, policy: bool = True, log_content: bool = False) -> tuple[TestClient, io.StringIO]:
    context = WorkspaceContext(uuid4(), uuid4())
    auth = DevelopmentBearerAuth({TOKEN: context})
    output = io.StringIO()
    logger = EventLogger(stream=output, log_content=log_content,
                         protected_values=("provider-key-private-canary", "other-secret-canary"))
    provider_policy = HostedPolicy(allow_prompt_disclosure=policy,
                                   rules=(RateRule("openai", "test-model", 0, 0),))
    def catalog(actor: WorkspaceContext) -> dict[str, Any]:
        assert actor == context
        return {"models": [{"provider": "openai", "model": "test-model",
                            "roles": ["reasoning", "coding"], "protocol": "responses",
                            "generation_enabled": policy, "text_generation": True}]}

    compat = OpenAICompatibility(catalog, provider_policy, Credentials(),
                                 log=logger, client_factory=FakeProvider)
    app = create_app(task_service=None, job_service=None, authenticate=auth,
                     openai_compat=compat, settings=ServerSettings(allowed_origins=("http://testserver",)))
    client = TestClient(app, raise_server_exceptions=False)
    client.headers["Authorization"] = "Bearer " + TOKEN
    return client, output


def test_models_are_openai_shaped_and_legacy_catalog_remains_separate() -> None:
    client, _ = make_client()
    with client:
        result = client.get("/v1/models")
        assert result.status_code == 200
        assert result.json() == {"object": "list", "data": [
            {"id": "openai/test-model", "object": "model", "created": 0, "owned_by": "openai"},
            {"id": "potbot", "object": "model", "created": 0, "owned_by": "potbot"},
        ]}
        assert client.get("/v1/models/potbot").json()["id"] == "potbot"
        assert client.get("/v1/models/openai/test-model").json()["owned_by"] == "openai"
        assert client.get("/v1/models/unknown").status_code == 404


def test_direct_chat_completion_routes_real_provider_without_leaking_credential() -> None:
    client, log = make_client(log_content=True)
    FakeProvider.calls.clear()
    with client:
        response = client.post("/v1/chat/completions", json={
            "model": "openai/test-model", "messages": [{"role": "user", "content": "question"}],
            "max_tokens": 40,
        })
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["choices"][0]["message"]["content"] == "Answer 42"
    assert data["usage"]["total_tokens"] == 10
    assert len(FakeProvider.calls) == 1
    assert "question" in log.getvalue()
    assert "provider-key-private-canary" not in log.getvalue() + response.text


def test_virtual_potbot_executes_bounded_reasoning_passes() -> None:
    client, log = make_client()
    FakeProvider.calls.clear()
    with client:
        response = client.post("/v1/chat/completions", json={
            "model": "potbot", "messages": [{"role": "user", "content": "Solve this"}],
        })
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "Answer 42"
    assert response.json()["usage"]["total_tokens"] == 30
    assert len(FakeProvider.calls) == 3
    assert '"event": "reasoning.stage"' in log.getvalue()
    assert "Solve this" not in log.getvalue()  # metadata-only default


def test_streaming_chat_and_responses() -> None:
    client, _ = make_client()
    with client:
        with client.stream("POST", "/v1/chat/completions", json={
            "model": "openai/test-model", "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "hi"}],
        }) as response:
            assert response.status_code == 200
            events = [line[6:] for line in response.iter_lines() if line.startswith("data: ")]
        assert events[-1] == "[DONE]"
        content = [json.loads(item) for item in events[:-1]]
        assert any(chunk["choices"] and chunk["choices"][0]["delta"].get("content") == "Answer " for chunk in content)
        assert content[-1]["usage"]["total_tokens"] == 10
        with client.stream("POST", "/v1/responses", json={
            "model": "openai/test-model", "input": "hello", "stream": True
        }) as response:
            assert response.status_code == 200
            content = response.read().decode()
            assert "response.created" in content
            assert "response.output_text.delta" in content
            assert "response.completed" in content
        response = client.post("/v1/responses", json={"model": "openai/test-model", "input": "hello"})
        assert response.status_code == 200
        assert response.json()["output"][0]["content"][0]["text"] == "Answer 42"


def test_invalid_inputs_and_disabled_policy_fail_without_dispatch() -> None:
    client, _ = make_client(policy=False)
    FakeProvider.calls.clear()
    with client:
        assert client.get("/v1/models").json()["data"] == [
            {"id": "potbot", "object": "model", "created": 0, "owned_by": "potbot"}
        ]
        assert client.post("/v1/chat/completions", json={
            "model": "potbot", "messages": [{"role": "user", "content": "hi"}]
        }).status_code == 503
        response = client.post("/v1/chat/completions", json={
            "model": "potbot", "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "unsafe"}}],
        })
        assert response.status_code == 400
        assert client.post("/v1/chat/completions", data='{"model":1,"model":2}',
                           headers={"Content-Type": "application/json"}).status_code == 400
    assert FakeProvider.calls == []


def test_event_logger_redacts_and_download_progress() -> None:
    output = io.StringIO()
    logger = EventLogger(stream=output, log_content=True, protected_values=("secret-value",))
    logger.emit("api.query", content="hello secret-value", api_key="secret-value")
    assert "secret-value" not in output.getvalue()
    assert "[REDACTED]" in output.getvalue()
    event = type("Progress", (), dict(stage="downloading", item="sample.safetensors",
                                     completed_bytes=50, total_bytes=100))()
    TransferProgress(logger, interval=0)(event)
    assert '"percent": 50.0' in output.getvalue()


def test_pinned_embedding_adapter_when_explicitly_available() -> None:
    context = WorkspaceContext(uuid4(), uuid4())
    logger = EventLogger(stream=io.StringIO())
    from potbot.memory.embedding_profile import QWEN_MODEL

    def encode(texts: tuple[str, ...]) -> EmbeddingBatch:
        assert texts == ("hello",)
        return EmbeddingBatch(((1.0,) + (0.0,) * 1023,), 2, 3)

    adapter = OpenAICompatibility(
        lambda _: {"models": []}, HostedPolicy(), Credentials(),
        log=logger, client_factory=FakeProvider, embedding_encoder=encode)
    assert any(model["id"] == "local/" + QWEN_MODEL for model in adapter.models(context)["data"])
    result = adapter.embeddings(context, {"model": "local/" + QWEN_MODEL, "input": "hello"}, "run1")
    assert len(result["data"][0]["embedding"]) == 1024
    assert result["usage"]["total_tokens"] == 2
