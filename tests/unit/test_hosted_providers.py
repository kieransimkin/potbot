"""Native provider wire contracts run against in-process HTTP mocks only."""

from __future__ import annotations

import json
from collections.abc import Iterator
from uuid import uuid4

import httpx
import pytest

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.models.providers import (
    HostedProviderClient,
    ProviderAPIError,
    ProviderCompleted,
    ProviderRequest,
    ProviderSelection,
    ProviderTextDelta,
    ProviderUsage,
    is_known_non_text_model,
)
from potbot.models.providers.client import _sse
from potbot.models.providers.protocols import request_body, usage_from

KEY = "provider-secret-canary-not-a-real-key"


def event(value: dict, name: str = "", newline: str = "\n") -> bytes:
    prefix = f"event: {name}{newline}" if name else ""
    return (prefix + "data: " + json.dumps(value, ensure_ascii=False) + newline * 2).encode()


def wire(protocol: str) -> bytes:
    if protocol == "responses":
        return event({"type": "response.output_text.delta", "delta": "héllo"}) + event(
            {
                "type": "response.completed",
                "response": {
                    "id": "resp-1",
                    "model": "served-model",
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 7,
                        "total_tokens": 17,
                        "input_tokens_details": {"cached_tokens": 3},
                        "output_tokens_details": {"reasoning_tokens": 2},
                    },
                },
            }
        )
    if protocol == "messages":
        return b"".join(
            [
                event(
                    {
                        "type": "message_start",
                        "message": {
                            "id": "msg-1",
                            "model": "served-model",
                            "usage": {
                                "input_tokens": 2,
                                "cache_read_input_tokens": 5,
                                "cache_creation_input_tokens": 3,
                                "output_tokens": 0,
                            },
                        },
                    }
                ),
                event(
                    {
                        "type": "content_block_delta",
                        "delta": {"type": "thinking_delta", "thinking": "hidden reasoning"},
                    }
                ),
                event(
                    {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "héllo"},
                    }
                ),
                event(
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn"},
                        "usage": {"output_tokens": 7},
                    }
                ),
                event({"type": "message_stop"}),
            ]
        )
    if protocol == "gemini":
        return event(
            {
                "modelVersion": "served-model",
                "responseId": "gem-1",
                "candidates": [
                    {
                        "index": 0,
                        "content": {
                            "parts": [
                                {"thought": True, "text": "hidden reasoning"},
                                {"text": "héllo"},
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 5,
                    "thoughtsTokenCount": 2,
                    "cachedContentTokenCount": 3,
                    "totalTokenCount": 17,
                },
            }
        )
    return b"".join(
        [
            event(
                {
                    "id": "chat-1",
                    "model": "served-model",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "héllo", "reasoning_content": "hidden reasoning"},
                        }
                    ],
                }
            ),
            event({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
            event(
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 7,
                        "total_tokens": 17,
                        "prompt_tokens_details": {"cached_tokens": 3},
                        "completion_tokens_details": {"reasoning_tokens": 2},
                    },
                }
            ),
            b"data: [DONE]\n\n",
        ]
    )


class Fragmented(httpx.SyncByteStream):
    def __init__(self, content: bytes, size: int = 3):
        self.content, self.size = content, size
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        for offset in range(0, len(self.content), self.size):
            yield self.content[offset : offset + self.size]

    def close(self) -> None:
        self.closed = True


def client_for(handler, *, authorize=lambda request: None, resolver=lambda reference: KEY):
    return HostedProviderClient(
        resolver,
        http_client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
        authorize=authorize,
    )


@pytest.mark.parametrize(
    "provider,protocol,path,header",
    [
        ("openai", "responses", "/v1/responses", "authorization"),
        ("openai", "chat_completions", "/v1/chat/completions", "authorization"),
        ("xai", "responses", "/v1/responses", "authorization"),
        ("xai", "chat_completions", "/v1/chat/completions", "authorization"),
        ("anthropic", "messages", "/v1/messages", "x-api-key"),
        ("gemini", "gemini", "/v1beta/models/test-model:streamGenerateContent", "x-goog-api-key"),
        ("evolink", "chat_completions", "/v1/chat/completions", "authorization"),
        ("evolink", "responses", "/v1/responses", "authorization"),
        ("evolink", "messages", "/v1/messages", "authorization"),
        ("evolink", "gemini", "/v1beta/models/test-model:streamGenerateContent", "authorization"),
    ],
)
def test_all_requested_provider_protocols_use_native_wire_and_reported_usage(
    provider, protocol, path, header
):
    calls = []
    fragment = Fragmented(wire(protocol))

    def handle(request):
        calls.append(request)
        assert request.method == "POST" and request.url.path == path
        assert KEY in request.headers[header]
        assert KEY not in str(request.url) and KEY.encode() not in request.content
        body = json.loads(request.content)
        if protocol == "gemini":
            assert body["contents"][0]["parts"] == [{"text": "question"}]
            assert body["systemInstruction"] == {"parts": [{"text": "policy"}]}
            assert "model" not in body and "messages" not in body
        elif protocol == "responses":
            assert body["input"] == "question" and body["instructions"] == "policy"
            assert body["store"] is False and body["max_output_tokens"] == 20
        elif protocol == "messages":
            assert body["system"] == "policy" and body["max_tokens"] == 20
            assert request.headers["anthropic-version"] == "2023-06-01"
        else:
            assert body["messages"][0] == {"role": "system", "content": "policy"}
            field = "max_tokens" if provider == "evolink" else "max_completion_tokens"
            assert body[field] == 20
            assert body["stream_options"] == {"include_usage": True}
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=fragment)

    client = client_for(handle)
    request = ProviderRequest(
        ProviderSelection(provider, "test-model", protocol),
        "question",
        system="policy",
        max_output_tokens=20,
    )
    events = list(client.stream(request, uuid4()))
    assert events[0] == ProviderTextDelta("héllo")
    terminal = events[-1]
    assert isinstance(terminal, ProviderCompleted)
    assert terminal.finish_reason == "stop" and terminal.model == "served-model"
    assert terminal.usage.input_tokens == 10
    assert terminal.usage.output_tokens == 7
    assert terminal.usage.total_tokens == 17
    assert terminal.usage.origin == "provider_reported"
    assert "money_microunits" not in terminal.usage.to_dict()
    assert len(calls) == 1 and fragment.closed
    assert KEY not in json.dumps(terminal.to_dict())


def test_authorization_is_required_and_precedes_secret_resolution_and_dispatch():
    keys, calls = [], []
    client = client_for(
        lambda request: calls.append(request), authorize=None, resolver=lambda ref: keys.append(ref)
    )
    with pytest.raises(ScopeDeniedError):
        list(
            client.stream(
                ProviderRequest(ProviderSelection("openai", "example"), "prompt"), uuid4()
            )
        )
    assert keys == [] and calls == []


def test_policy_rejection_precedes_credentials_and_send():
    def deny(request):
        raise ScopeDeniedError("Provider disclosure was not approved.")

    client = client_for(
        lambda request: pytest.fail("must not send"),
        authorize=deny,
        resolver=lambda ref: pytest.fail("must not read key"),
    )
    with pytest.raises(ScopeDeniedError):
        list(
            client.stream(
                ProviderRequest(ProviderSelection("openai", "example"), "prompt"), uuid4()
            )
        )


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 429, 500, 503])
def test_redirects_and_http_errors_never_retry_or_expose_error_body(status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            status,
            headers={"location": "https://elsewhere.invalid/collect"},
            text=KEY + " private prompt",
        )

    with pytest.raises(ProviderAPIError) as caught:
        list(
            client_for(handle).stream(
                ProviderRequest(ProviderSelection("openai", "example"), "prompt"), uuid4()
            )
        )
    assert len(calls) == 1
    assert caught.value.dispatched and caught.value.status_code == status
    assert KEY not in str(caught.value) and "private prompt" not in str(caught.value)


def test_network_exception_is_sanitized_and_not_retried():
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout(KEY + " prompt canary")

    with pytest.raises(ProviderAPIError) as caught:
        list(
            client_for(handle).stream(
                ProviderRequest(ProviderSelection("openai", "example"), "prompt"), uuid4()
            )
        )
    assert caught.value.dispatched and caught.value.retryable and len(calls) == 1
    assert KEY not in str(caught.value)


def test_missing_usage_does_not_fabricate_zero_tokens_or_cost():
    content = event({"type": "response.output_text.delta", "delta": "answer"}) + event(
        {
            "type": "response.completed",
            "response": {"id": "resp", "status": "completed", "usage": None},
        }
    )
    client = client_for(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=content
        )
    )
    terminal = list(
        client.stream(ProviderRequest(ProviderSelection("openai", "example"), "prompt"), uuid4())
    )[-1]
    assert isinstance(terminal, ProviderCompleted) and terminal.usage is None


@pytest.mark.parametrize(
    "content,code",
    [
        (
            b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n',
            "incomplete_provider_stream",
        ),
        (b'data: {"a":1,"a":2}\n\n', "invalid_provider_json"),
        (b'data: {"a": NaN}\n\n', "invalid_provider_json"),
        (b'data: {"a": "\xff"}\n\n', "invalid_provider_utf8"),
        (b'data: {"type":"response.completed"}', "incomplete_provider_event"),
        (b'event: error\ndata: {"message":"secret"}\n\n', "provider_stream_error"),
    ],
)
def test_partial_invalid_and_error_streams_are_not_successful(content, code):
    client = client_for(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Fragmented(content)
        )
    )
    with pytest.raises(ProviderAPIError) as caught:
        list(
            client.stream(
                ProviderRequest(ProviderSelection("openai", "example"), "prompt"), uuid4()
            )
        )
    assert caught.value.provider_error_code == code and caught.value.dispatched


def test_sse_comments_multiline_data_utf8_and_all_line_endings():
    for newline in ("\n", "\r\n", "\r"):
        wire = (
            ":"
            + "ignored"
            + newline
            + "event: test"
            + newline
            + 'data: {"text":'
            + newline
            + 'data: "héllo"}'
            + newline * 2
        ).encode()
        parsed = list(_sse(iter(wire[i : i + 1] for i in range(len(wire))), 1024))
        assert parsed == [("test", '{"text":\n"héllo"}')]


def test_output_bound_counts_utf8_bytes_before_publishing_delta():
    client = client_for(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=wire("responses")
        )
    )
    with pytest.raises(ProviderAPIError) as caught:
        list(
            client.stream(
                ProviderRequest(
                    ProviderSelection("openai", "example"), "prompt", max_response_bytes=5
                ),
                uuid4(),
            )
        )
    assert caught.value.provider_error_code == "provider_output_limit"


def test_cancellation_before_send_is_definitively_undispatched():
    client = client_for(
        lambda request: pytest.fail("must not send"),
        resolver=lambda ref: pytest.fail("must not read key"),
    )
    with pytest.raises(ProviderAPIError) as caught:
        list(
            client.stream(
                ProviderRequest(ProviderSelection("openai", "example"), "prompt"),
                uuid4(),
                cancelled=lambda: True,
            )
        )
    assert not caught.value.dispatched and caught.value.provider_error_code == "cancelled"


def test_stop_closes_local_connection_without_claiming_remote_cancellation():
    fragment = Fragmented(wire("responses"))
    client = client_for(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=fragment
        )
    )
    identity = uuid4()
    stream = client.stream(
        ProviderRequest(ProviderSelection("openai", "example"), "prompt"), identity
    )
    assert next(stream) == ProviderTextDelta("héllo")
    assert client.stop(identity) is False
    with pytest.raises(ProviderAPIError) as caught:
        next(stream)
    assert caught.value.dispatched and fragment.closed


def test_catalog_paginates_anthropic_and_preserves_exact_ids():
    calls = []

    def handle(request):
        calls.append(request)
        if "after_id" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "claude-new-a", "display_name": "A"}],
                    "has_more": True,
                    "last_id": "claude-new-a",
                },
            )
        assert request.url.params["after_id"] == "claude-new-a"
        return httpx.Response(200, json={"data": [{"id": "claude-new-b"}], "has_more": False})

    models = client_for(handle, authorize=None).list_models("anthropic")
    assert [m.model for m in models] == ["claude-new-a", "claude-new-b"]
    assert all(m.text_generation is True and m.to_dict()["qualified"] is False for m in models)
    assert len(calls) == 2


def test_gemini_catalog_uses_provider_capabilities_and_cursor():
    calls = []

    def handle(request):
        calls.append(request)
        assert "x-goog-api-key" in request.headers and "key" not in request.url.params
        if "pageToken" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/new-gemini",
                            "supportedGenerationMethods": ["generateContent", "countTokens"],
                            "inputTokenLimit": 100000,
                            "outputTokenLimit": 1000,
                        }
                    ],
                    "nextPageToken": "next",
                },
            )
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "models/text-embedding-new",
                        "supportedGenerationMethods": ["embedContent"],
                    }
                ]
            },
        )

    models = client_for(handle).list_models("gemini")
    assert models[0].model == "new-gemini" and models[0].text_generation is True
    assert models[1].text_generation is False and len(calls) == 2


def test_evolink_discovers_all_returned_models_not_a_fixed_shortlist():
    def handle(request):
        assert str(request.url) == "https://api.evolink.ai/v1/models"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "future-provider/new-code-model", "type": "text"},
                    {"id": "future-movie-model", "type": "video"},
                    {"id": "unknown-new-model"},
                    {"id": "gpt-image-example"},
                ]
            },
        )

    models = {m.model: m for m in client_for(handle).list_models("evolink")}
    assert len(models) == 4
    assert models["future-provider/new-code-model"].text_generation is True
    assert models["future-movie-model"].text_generation is False
    assert models["gpt-image-example"].text_generation is False
    assert models["unknown-new-model"].text_generation is None
    assert models["unknown-new-model"].to_dict()["capability_verification_required"]


def test_catalog_cursor_loop_fails_instead_of_claiming_a_complete_catalog():
    response = {"data": [{"id": "claude-example"}], "has_more": True, "last_id": "repeat"}
    with pytest.raises(ProviderAPIError) as caught:
        client_for(lambda request: httpx.Response(200, json=response)).list_models("anthropic")
    assert caught.value.provider_error_code == "invalid_catalog_pagination"


def test_catalog_cumulative_deadline_bounds_continuous_metadata_reads(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("potbot.models.providers.client.time.monotonic", lambda: now[0])

    def handle(request):
        now[0] = 61.0
        return httpx.Response(200, json={"data": [{"id": "claude-future"}]})

    with pytest.raises(ProviderAPIError) as caught:
        client_for(handle).list_models("anthropic")
    assert caught.value.provider_error_code == "model_catalog_deadline_exceeded"


@pytest.mark.parametrize("provider", ["openai", "xai", "evolink"])
def test_minimal_catalog_capability_is_unknown_not_qualified(provider):
    models = client_for(
        lambda request: httpx.Response(200, json={"data": [{"id": "future-model"}]})
    ).list_models(provider)
    assert models[0].text_generation is None and not models[0].to_dict()["qualified"]


def test_accounting_avoids_cache_and_reasoning_double_count():
    assert usage_from(
        "responses",
        {
            "input_tokens": 10,
            "output_tokens": 20,
            "total_tokens": 30,
            "input_tokens_details": {"cached_tokens": 3},
            "output_tokens_details": {"reasoning_tokens": 9},
        },
    ) == ProviderUsage(10, 20, 30, 3, 0, 9)
    assert usage_from(
        "messages",
        {
            "input_tokens": 2,
            "cache_read_input_tokens": 5,
            "cache_creation_input_tokens": 3,
            "output_tokens": 20,
        },
    ) == ProviderUsage(10, 20, 30, 5, 3, 0)
    assert usage_from(
        "gemini",
        {
            "promptTokenCount": 10,
            "candidatesTokenCount": 4,
            "thoughtsTokenCount": 16,
            "totalTokenCount": 30,
        },
    ) == ProviderUsage(10, 20, 30, 0, 0, 16)


@pytest.mark.parametrize(
    "data",
    [
        {"input_tokens": True, "output_tokens": 1},
        {"input_tokens": -1, "output_tokens": 1},
        {"input_tokens": 10, "output_tokens": 10, "total_tokens": 5},
        {"input_tokens": 1, "output_tokens": 1, "output_tokens_details": {"reasoning_tokens": 5}},
    ],
)
def test_invalid_usage_cannot_settle_a_reservation(data):
    with pytest.raises(ProviderAPIError):
        usage_from("responses", data)


def test_selection_accepts_future_model_ids_without_arbitrary_destinations():
    request = ProviderRequest(
        ProviderSelection("evolink", "brand/future-model:version"), "question"
    )
    assert request.selection.origin == "https://direct.evolink.ai"
    assert request.selection.credential_ref == "secret://providers/evolink"
    assert len(request.fingerprint) == 64
    for value in ("https://elsewhere.invalid/model", "../model", "model?secret=key", "bad\nmodel"):
        with pytest.raises(InvalidInputError):
            ProviderSelection("evolink", value)


@pytest.mark.parametrize(
    "model",
    [
        "gpt-image-1",
        "openai/text-embedding-3-large",
        "models/gemini-embedding-001",
        "gemini-2.5-flash-image",
        "gemini-2.5-flash-preview-tts",
        "imagen-4.0-generate-001",
        "veo3.1-fast",
        "sora2",
        "suno-v5",
        "seedance-2.0",
        "Seedream-4.0",
        "grok-imagine-image",
        "kling-v2.1",
        "flux.2-pro",
        "qwen-image-plus",
        "tts-1",
        "whisper-1",
        "omni-moderation-latest",
    ],
)
def test_dedicated_media_names_are_blocked_for_generation_but_remain_in_inventory(model):
    assert is_known_non_text_model(model)
    with pytest.raises(InvalidInputError):
        ProviderSelection("evolink", model)
    catalog = client_for(
        lambda request: httpx.Response(200, json={"data": [{"id": model}]})
    ).list_models("evolink")
    assert catalog[0].model == model
    assert catalog[0].text_generation is False
    assert catalog[0].supported_protocols == ()


@pytest.mark.parametrize(
    "model,protocol,field",
    [
        ("claude-future-version", "messages", "max_tokens"),
        ("gemini-future-version", "gemini", "generationConfig"),
        ("gpt-future-version", "responses", "max_output_tokens"),
        ("deepseek-future-version", "chat_completions", "max_tokens"),
        ("glm-future-version", "chat_completions", "max_tokens"),
        ("evolink/auto", "chat_completions", "max_tokens"),
        ("brand/future-version", "chat_completions", "max_tokens"),
    ],
)
def test_evolink_family_defaults_use_documented_routes_and_output_fields(model, protocol, field):
    selection = ProviderSelection("evolink", model)
    assert selection.protocol == protocol
    _, body = request_body(ProviderRequest(selection, "question", max_output_tokens=128))
    value = body[field]
    assert (value["maxOutputTokens"] if isinstance(value, dict) else value) == 128
    assert "max_completion_tokens" not in body


def test_evolink_explicit_gemini_chat_route_uses_effective_output_cap():
    request = ProviderRequest(
        ProviderSelection("evolink", "gemini-future-version", "chat_completions"),
        "question",
        max_output_tokens=128,
    )
    path, body = request_body(request)
    assert path == "/v1/chat/completions"
    assert body["max_tokens"] == 128 and "max_completion_tokens" not in body


@pytest.mark.parametrize("protocol", ["", [], {}, False, 0])
def test_falsey_invalid_protocol_does_not_silently_choose_a_default(protocol):
    with pytest.raises(InvalidInputError):
        ProviderSelection("evolink", "future-model", protocol)


def test_sampling_is_omitted_by_default_and_only_mapped_when_explicit():
    _, body = request_body(ProviderRequest(ProviderSelection("openai", "example"), "question"))
    assert "temperature" not in body and "top_p" not in body
    _, native = request_body(
        ProviderRequest(ProviderSelection("gemini", "example"), "question", top_p=0.5, seed=3)
    )
    assert native["generationConfig"]["topP"] == 0.5 and native["generationConfig"]["seed"] == 3
    with pytest.raises(InvalidInputError):
        ProviderRequest(ProviderSelection("anthropic", "example"), "question", seed=2)


@pytest.mark.parametrize("protocol", ["messages", "gemini", "chat_completions"])
def test_initial_or_intermediate_usage_is_not_terminal_accounting(protocol):
    if protocol == "messages":
        content = b"".join(
            [
                event(
                    {
                        "type": "message_start",
                        "message": {"usage": {"input_tokens": 10, "output_tokens": 0}},
                    }
                ),
                event(
                    {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "answer"},
                    }
                ),
                event({"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
                event({"type": "message_stop"}),
            ]
        )
    elif protocol == "gemini":
        content = event(
            {"usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 0}}
        ) + event(
            {"candidates": [{"content": {"parts": [{"text": "answer"}]}, "finishReason": "STOP"}]}
        )
    else:
        content = (
            event(
                {
                    "choices": [{"delta": {"content": "answer"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 1},
                }
            )
            + event({"choices": [{"finish_reason": "stop"}]})
            + b"data: [DONE]\n\n"
        )
    client = client_for(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=content
        )
    )
    terminal = list(
        client.stream(
            ProviderRequest(ProviderSelection("evolink", "example", protocol), "question"), uuid4()
        )
    )[-1]
    assert terminal.usage is None


def test_named_terminal_must_match_response_status():
    content = event(
        {
            "type": "response.completed",
            "response": {"status": "failed", "usage": {"input_tokens": 1, "output_tokens": 1}},
        }
    )
    client = client_for(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=content
        )
    )
    with pytest.raises(ProviderAPIError) as caught:
        list(
            client.stream(
                ProviderRequest(ProviderSelection("openai", "example"), "question"), uuid4()
            )
        )
    assert caught.value.provider_error_code == "invalid_provider_terminal"


@pytest.mark.parametrize(
    "status,expected", [("completed", "stop"), ("incomplete", "length"), ("failed", "failed")]
)
def test_complete_json_response_keeps_terminal_status(status, expected):
    value = {
        "id": "resp-1",
        "model": "served",
        "status": status,
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "answer"}]}],
        "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
    }
    events = list(
        client_for(lambda request: httpx.Response(200, json=value)).stream(
            ProviderRequest(ProviderSelection("openai", "example"), "question"), uuid4()
        )
    )
    assert events[0] == ProviderTextDelta("answer")
    assert events[-1].finish_reason == expected
