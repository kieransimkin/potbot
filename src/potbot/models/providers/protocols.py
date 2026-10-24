"""Small native wire adapters; only public answer text becomes a text delta."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

from potbot.domain.errors import InvalidInputError
from potbot.models.providers.contracts import (
    ProviderAPIError,
    ProviderCompleted,
    ProviderEvent,
    ProviderRequest,
    ProviderTextDelta,
    ProviderUsage,
)


def request_body(request: ProviderRequest) -> tuple[str, dict[str, Any]]:
    """Return a fixed-origin relative route and exactly one protocol's fields."""
    selection = request.selection
    protocol = selection.protocol
    body: dict[str, Any] = {"model": selection.model, "stream": True}
    if protocol == "responses":
        body.update(input=request.prompt, max_output_tokens=request.max_output_tokens, store=False)
        if request.system:
            body["instructions"] = request.system
        if request.response_format == "json_object":
            body["text"] = {"format": {"type": "json_object"}}
        path = "/v1/responses"
    elif protocol in {"chat_completions", "messages"}:
        messages = [{"role": "user", "content": request.prompt}]
        if request.system:
            if protocol == "messages":
                body["system"] = request.system
            else:
                messages.insert(0, {"role": "system", "content": request.system})
        body["messages"] = messages
        if protocol == "messages":
            body["max_tokens"] = request.max_output_tokens
            path = "/v1/messages"
        else:
            # EvoLink's Gemini/DeepSeek/GLM routes document max_tokens; some
            # silently ignore max_completion_tokens. Its GPT compatibility
            # layer translates max_tokens to the newer field when required.
            output_field = (
                "max_tokens" if selection.provider == "evolink" else "max_completion_tokens"
            )
            body[output_field] = request.max_output_tokens
            body["stream_options"] = {"include_usage": True}
            if request.response_format == "json_object":
                body["response_format"] = {"type": "json_object"}
            path = "/v1/chat/completions"
    else:
        config: dict[str, Any] = {"maxOutputTokens": request.max_output_tokens}
        body = {
            "contents": [{"role": "user", "parts": [{"text": request.prompt}]}],
            "generationConfig": config,
        }
        if request.system:
            body["systemInstruction"] = {"parts": [{"text": request.system}]}
        if request.response_format == "json_object":
            config["responseMimeType"] = "application/json"
        for source, target in (("temperature", "temperature"), ("top_p", "topP"), ("seed", "seed")):
            value = getattr(request, source)
            if value is not None:
                config[target] = value
        model = selection.model.removeprefix("models/")
        return f"/v1beta/models/{quote(model, safe='')}:streamGenerateContent?alt=sse", body
    for name in ("temperature", "top_p", "seed"):
        value = getattr(request, name)
        if value is not None:
            body[name] = value
    return path, body


def parse_json(data: str | bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    def finite(value: str) -> Any:
        raise ValueError("non-finite")

    try:
        result = json.loads(data, object_pairs_hook=unique, parse_constant=finite)
        if not isinstance(result, dict):
            raise ValueError("object required")
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProviderAPIError("invalid_provider_json", dispatched=True) from None


def _int(value: object, default: int | None = None) -> int | None:
    if value is None:
        return default
    if type(value) is not int or value < 0:
        raise ProviderAPIError("invalid_provider_usage", dispatched=True)
    return value


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def usage_from(protocol: str, data: object) -> ProviderUsage | None:
    if not isinstance(data, dict):
        return None
    cache = creation = reasoning = 0
    if protocol == "gemini":
        incoming = _int(data.get("promptTokenCount"))
        outgoing = _int(data.get("candidatesTokenCount"))
        reasoning = _int(data.get("thoughtsTokenCount"), 0) or 0
        cache = _int(data.get("cachedContentTokenCount"), 0) or 0
        if outgoing is not None:
            outgoing += reasoning
        total = _int(data.get("totalTokenCount"))
    elif protocol == "messages":
        incoming = _int(data.get("input_tokens"))
        outgoing = _int(data.get("output_tokens"))
        cache = _int(data.get("cache_read_input_tokens"), 0) or 0
        creation = _int(data.get("cache_creation_input_tokens"), 0) or 0
        if incoming is not None:
            incoming += cache + creation
        total = None
    else:
        incoming = _int(data.get("input_tokens" if protocol == "responses" else "prompt_tokens"))
        outgoing = _int(
            data.get("output_tokens" if protocol == "responses" else "completion_tokens")
        )
        input_details = _dict(
            data.get("input_tokens_details" if protocol == "responses" else "prompt_tokens_details")
        )
        output_details = _dict(
            data.get(
                "output_tokens_details" if protocol == "responses" else "completion_tokens_details"
            )
        )
        cache = _int(input_details.get("cached_tokens"), 0) or 0
        reasoning = _int(output_details.get("reasoning_tokens"), 0) or 0
        total = _int(data.get("total_tokens"))
    if incoming is None or outgoing is None:
        return None
    try:
        return ProviderUsage(
            incoming,
            outgoing,
            incoming + outgoing if total is None else total,
            cache,
            creation,
            reasoning,
        )
    except InvalidInputError:
        raise ProviderAPIError("invalid_provider_usage", dispatched=True) from None


def _identity(value: object) -> str | None:
    if isinstance(value, str) and 0 < len(value) <= 256 and value.isprintable():
        return value
    return None


def _reason(protocol: str, value: object) -> str:
    if protocol == "messages":
        mapping = {
            "end_turn": "stop",
            "stop_sequence": "stop",
            "max_tokens": "length",
            "model_context_window_exceeded": "length",
            "refusal": "refusal",
            "tool_use": "tool_calls",
            "pause_turn": "tool_calls",
        }
    elif protocol == "gemini":
        mapping = {
            "STOP": "stop",
            "MAX_TOKENS": "length",
            "CONTINUATION": "length",
            "SAFETY": "refusal",
            "RECITATION": "refusal",
            "BLOCKLIST": "refusal",
            "PROHIBITED_CONTENT": "refusal",
            "SPII": "refusal",
            "IMAGE_SAFETY": "refusal",
        }
    else:
        mapping = {
            "stop": "stop",
            "length": "length",
            "content_filter": "refusal",
            "tool_calls": "tool_calls",
            "function_call": "tool_calls",
        }
    return mapping.get(value, "failed") if isinstance(value, str) else "failed"


class StreamDecoder:
    """Accumulate final usage, preserve actual model identity, and reject truncation."""

    def __init__(self, protocol: str, request_id: str | None = None) -> None:
        self.protocol = protocol
        self.request_id = request_id
        self.model: str | None = None
        self.usage_data: dict[str, Any] = {}
        self.usage_is_terminal = False
        self.reason: str | None = None
        self.done = False
        self.saw_text = False

    def _text(self, value: object) -> Iterator[ProviderTextDelta]:
        if isinstance(value, str) and value:
            self.saw_text = True
            yield ProviderTextDelta(value)

    def _terminal(self) -> ProviderCompleted:
        self.done = True
        return ProviderCompleted(
            self.reason or "failed",
            usage_from(self.protocol, self.usage_data) if self.usage_is_terminal else None,
            self.model,
            self.request_id,
        )

    def feed(self, event_name: str, data: str) -> Iterator[ProviderEvent]:
        if self.done:
            if data == "[DONE]":
                return
            raise ProviderAPIError("event_after_terminal", dispatched=True)
        if data == "[DONE]":
            if self.protocol != "chat_completions" or self.reason is None:
                raise ProviderAPIError("incomplete_provider_stream", dispatched=True)
            yield self._terminal()
            return
        value = parse_json(data)
        if "error" in value or value.get("type") == "error" or event_name == "error":
            raise ProviderAPIError("provider_stream_error", dispatched=True)
        event = value.get("type", event_name)
        if self.protocol == "responses":
            if event == "response.output_text.delta":
                yield from self._text(value.get("delta"))
            elif event in {"response.completed", "response.incomplete", "response.failed"}:
                response = _dict(value.get("response"))
                if response.get("status") != event.removeprefix("response."):
                    raise ProviderAPIError("invalid_provider_terminal", dispatched=True)
                self.model = _identity(response.get("model"))
                self.request_id = _identity(response.get("id")) or self.request_id
                self.usage_data = _dict(response.get("usage"))
                self.usage_is_terminal = True
                self.reason = (
                    "stop"
                    if event == "response.completed"
                    else (
                        "length"
                        if event == "response.incomplete"
                        and _dict(response.get("incomplete_details")).get("reason")
                        == "max_output_tokens"
                        else "failed"
                    )
                )
                output = response.get("output", [])
                if isinstance(output, list):
                    for item in output:
                        if isinstance(item, dict) and item.get("type") in {
                            "function_call",
                            "custom_tool_call",
                        }:
                            self.reason = "tool_calls"
                        for part in _dict(item).get("content", []):
                            if isinstance(part, dict) and part.get("type") == "refusal":
                                self.reason = "refusal"
                yield self._terminal()
            return
        if self.protocol == "messages":
            if event == "message_start":
                message = _dict(value.get("message"))
                self.model = _identity(message.get("model"))
                self.request_id = _identity(message.get("id")) or self.request_id
                self.usage_data.update(_dict(message.get("usage")))
            elif event == "content_block_start":
                block = _dict(value.get("content_block"))
                if block.get("type") == "text":
                    yield from self._text(block.get("text"))
            elif event == "content_block_delta":
                delta = _dict(value.get("delta"))
                if delta.get("type") == "text_delta":
                    yield from self._text(delta.get("text"))
            elif event == "message_delta":
                self.usage_data.update(_dict(value.get("usage")))
                stop = _dict(value.get("delta")).get("stop_reason")
                if stop is not None:
                    self.reason = _reason(self.protocol, stop)
                    self.usage_is_terminal = "output_tokens" in _dict(value.get("usage"))
            elif event == "message_stop":
                if self.reason is None:
                    raise ProviderAPIError("incomplete_provider_stream", dispatched=True)
                yield self._terminal()
            return
        if self.protocol == "gemini":
            self.model = _identity(value.get("modelVersion")) or self.model
            self.request_id = _identity(value.get("responseId")) or self.request_id
            self.usage_data.update(_dict(value.get("usageMetadata")))
            feedback = _dict(value.get("promptFeedback"))
            if feedback.get("blockReason"):
                self.reason = "refusal"
            for candidate in value.get("candidates", []):
                if not isinstance(candidate, dict) or candidate.get("index", 0) != 0:
                    raise ProviderAPIError("unexpected_provider_candidate", dispatched=True)
                content = _dict(candidate.get("content"))
                for part in content.get("parts", []):
                    if isinstance(part, dict) and not part.get("thought", False):
                        if "functionCall" in part:
                            self.reason = "tool_calls"
                        yield from self._text(part.get("text"))
                if candidate.get("finishReason") and self.reason != "tool_calls":
                    self.reason = _reason(self.protocol, candidate["finishReason"])
            if self.reason is not None and isinstance(value.get("usageMetadata"), dict):
                self.usage_is_terminal = True
            return
        self.model = _identity(value.get("model")) or self.model
        self.request_id = _identity(value.get("id")) or self.request_id
        if isinstance(value.get("usage"), dict):
            self.usage_data = value["usage"]
        for choice in value.get("choices", []):
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                raise ProviderAPIError("unexpected_provider_candidate", dispatched=True)
            delta = _dict(choice.get("delta"))
            yield from self._text(delta.get("content"))
            if choice.get("finish_reason") is not None:
                self.reason = _reason(self.protocol, choice["finish_reason"])
        if self.reason is not None and isinstance(value.get("usage"), dict):
            self.usage_is_terminal = True

    def finish(self) -> Iterator[ProviderEvent]:
        if self.done:
            return
        if self.protocol == "gemini" and self.reason is not None:
            yield self._terminal()
            return
        raise ProviderAPIError("incomplete_provider_stream", dispatched=True)


def decode_document(
    protocol: str, value: dict[str, Any], request_id: str | None
) -> Iterator[ProviderEvent]:
    """Some compatible routes return a complete JSON response despite stream=true."""
    if "error" in value:
        raise ProviderAPIError("provider_response_error", dispatched=True)
    decoder = StreamDecoder(protocol, request_id)
    if protocol == "responses":
        for item in value.get("output", []):
            for part in _dict(item).get("content", []):
                if isinstance(part, dict) and part.get("type") == "output_text":
                    yield from decoder._text(part.get("text"))
        status = value.get("status")
        event = (
            "response.completed"
            if status == "completed"
            else ("response.incomplete" if status == "incomplete" else "response.failed")
        )
        yield from decoder.feed(event, json.dumps({"type": event, "response": value}))
    elif protocol == "messages":
        for part in value.get("content", []):
            if isinstance(part, dict) and part.get("type") == "text":
                yield from decoder._text(part.get("text"))
        decoder.usage_data = _dict(value.get("usage"))
        decoder.usage_is_terminal = True
        decoder.model = _identity(value.get("model"))
        decoder.request_id = _identity(value.get("id")) or request_id
        decoder.reason = _reason(protocol, value.get("stop_reason"))
        yield decoder._terminal()
    elif protocol == "gemini":
        yield from decoder.feed("", json.dumps(value))
        yield from decoder.finish()
    else:
        choices = value.get("choices", [])
        for choice in choices:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                raise ProviderAPIError("unexpected_provider_candidate", dispatched=True)
            choice["delta"] = choice.get("message", {})
        yield from decoder.feed("", json.dumps(value))
        yield from decoder.feed("", "[DONE]")
