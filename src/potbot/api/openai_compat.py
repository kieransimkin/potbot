"""OpenAI wire-shape adapter over Potbot's existing hosted model transports.

This endpoint is an explicitly policy-gated interactive transport, not the
budgeted task/lease API. The synthetic ``potbot`` model performs a bounded
proposal/critique/synthesis pass. It does not claim to activate the unfinished
Phase-7 durable autonomous solve controller or publish private reasoning traces.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from potbot.api.observability import EventLogger
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.models.hosted_policy import HostedPolicy
from potbot.memory.embedding_profile import QWEN_MODEL
from potbot.models.providers import (
    HostedProviderClient,
    ProviderCompleted,
    ProviderRequest,
    ProviderSelection,
    ProviderTextDelta,
)
from potbot.models.providers.contracts import is_known_non_text_model
from potbot.service_credentials import ServiceCredentials


class CompatibilityError(Exception):
    def __init__(self, message: str, *, status: int = 400, code: str = "invalid_request_error"):
        self.message, self.status, self.code = message, status, code
        super().__init__(message)


def error_document(error: CompatibilityError) -> dict[str, Any]:
    return {"error": {"message": error.message, "type": error.code, "param": None, "code": error.code}}


def _positive(value: object, name: str, default: int, maximum: int) -> int:
    if value is None:
        return default
    if type(value) is not int or not 1 <= value <= maximum:
        raise CompatibilityError(f"{name} must be an integer between 1 and {maximum}.")
    return value


def _text_parts(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(
        isinstance(part, dict)
        and part.get("type") in {"text", "input_text", "output_text"}
        and isinstance(part.get("text"), str)
        for part in value
    ):
        return "\n".join(part["text"] for part in value)
    raise CompatibilityError("Only text messages are supported by this compatibility endpoint.")


def _messages(value: object) -> tuple[str, str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 256:
        raise CompatibilityError("messages must be a nonempty bounded list.")
    system: list[str] = []
    dialogue: list[str] = []
    for message in value:
        if not isinstance(message, dict) or set(message) - {"role", "content", "name"}:
            raise CompatibilityError("Unsupported message fields.")
        role = message.get("role")
        if role not in {"system", "developer", "user", "assistant"}:
            raise CompatibilityError("Unsupported message role.")
        content = _text_parts(message.get("content"))
        if role in {"system", "developer"}:
            system.append(content)
        else:
            dialogue.append(f"{role.upper()}: {content}")
    if not dialogue:
        raise CompatibilityError("At least one user or assistant message is required.")
    instruction = "\n\n".join(system)
    prompt = "\n\n".join(dialogue)
    if len(prompt.encode()) > 262144 or len(instruction.encode()) > 65536:
        raise CompatibilityError("Conversation exceeds the supported text length.")
    return instruction, prompt


def parse_chat(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CompatibilityError("Request must be a JSON object.")
    allowed = {
        "model", "messages", "stream", "max_tokens", "max_completion_tokens",
        "temperature", "top_p", "user", "stream_options",
    }
    if set(value) - allowed:
        raise CompatibilityError("Unsupported chat-completion parameters.")
    model = value.get("model")
    if not isinstance(model, str) or not model or len(model) > 256:
        raise CompatibilityError("model must be a bounded string.")
    system, prompt = _messages(value.get("messages"))
    stream = value.get("stream", False)
    if type(stream) is not bool:
        raise CompatibilityError("stream must be a boolean.")
    option = value.get("stream_options", {})
    if not isinstance(option, dict) or set(option) - {"include_usage"} or type(option.get("include_usage", False)) is not bool:
        raise CompatibilityError("Only stream_options.include_usage is supported.")
    if "max_tokens" in value and "max_completion_tokens" in value:
        raise CompatibilityError("Choose max_tokens or max_completion_tokens, not both.")
    for param, upper in (("temperature", 2.0), ("top_p", 1.0)):
        number = value.get(param)
        if number is not None and (type(number) not in {float, int} or not 0 <= number <= upper):
            raise CompatibilityError(f"{param} is outside the supported range.")
    if value.get("user") is not None and (not isinstance(value["user"], str) or len(value["user"]) > 128):
        raise CompatibilityError("user must be a short string.")
    return {
        "model": model,
        "system": system,
        "prompt": prompt,
        "stream": stream,
        "max_output_tokens": _positive(
            value.get("max_completion_tokens", value.get("max_tokens")), "max_tokens", 2048, 8192
        ),
        "temperature": value.get("temperature"),
        "top_p": value.get("top_p"),
        "include_usage": option.get("include_usage", False),
    }


def parse_responses(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {
        "model", "input", "instructions", "stream", "max_output_tokens", "temperature", "top_p", "user"
    }:
        raise CompatibilityError("Unsupported Responses API parameters.")
    instructions = value.get("instructions", "")
    if not isinstance(instructions, str):
        raise CompatibilityError("instructions must be text.")
    inp = value.get("input")
    messages = [{"role": "user", "content": inp}] if isinstance(inp, str) else inp
    if not isinstance(messages, list):
        raise CompatibilityError("input must be text or a list of text messages.")
    clean = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant", "system", "developer"}:
            raise CompatibilityError("Unsupported Responses API input message.")
        clean.append({"role": message["role"], "content": message.get("content")})
    if instructions:
        clean.insert(0, {"role": "system", "content": instructions})
    return parse_chat({
        "model": value.get("model"), "messages": clean,
        "stream": value.get("stream", False),
        "max_tokens": value.get("max_output_tokens"),
        "temperature": value.get("temperature"), "top_p": value.get("top_p"),
        "user": value.get("user"),
    })


def _usage(raw: dict[str, Any] | None) -> dict[str, int]:
    raw = raw or {}
    return {
        "prompt_tokens": int(raw.get("input_tokens", 0)),
        "completion_tokens": int(raw.get("output_tokens", 0)),
        "total_tokens": int(raw.get("total_tokens", 0)),
    }


@dataclass(frozen=True)
class FinalAnswer:
    text: str
    usage: dict[str, int]
    finish_reason: str
    provider: str
    invocations: int


class OpenAICompatibility:
    """Policy-authenticated backend; provider client factory is injectable for tests."""

    def __init__(
        self,
        catalog: Callable[[WorkspaceContext], dict[str, Any]],
        policy: HostedPolicy,
        credentials: ServiceCredentials,
        *,
        log: EventLogger | None = None,
        client_factory: Callable[..., Any] = HostedProviderClient,
        max_potbot_calls: int = 3,
        embedding_encoder: Callable[[tuple[str, ...]], Any] | None = None,
    ) -> None:
        if not 1 <= max_potbot_calls <= 3:
            raise ValueError("potbot reasoning calls must be bounded")
        self.catalog, self.policy, self.credentials = catalog, policy, credentials
        self.log = log or EventLogger()
        self.client_factory = client_factory
        self.max_potbot_calls = max_potbot_calls
        self.embedding_encoder = embedding_encoder

    @staticmethod
    def _embedding_installed() -> bool:
        from potbot.dependencies import MODEL_DIRECTORY, project_root
        from potbot.memory.local_encoder import MODEL_FILES, MANIFEST

        directory = project_root() / "models" / MODEL_DIRECTORY
        return ((directory / MANIFEST).is_file()
                and all((directory / filename).is_file() for filename in MODEL_FILES))

    def embeddings(self, context: WorkspaceContext, body: object, run_id: str) -> dict[str, Any]:
        # Recheck workspace credentials/membership through the standard catalogue.
        self.catalog(context)
        if not isinstance(body, dict) or set(body) - {"model", "input", "encoding_format", "user"}:
            raise CompatibilityError("Unsupported embedding parameters.")
        if body.get("model") != "local/" + QWEN_MODEL:
            raise CompatibilityError("Embedding model is unavailable.", status=404,
                                     code="model_not_found")
        if body.get("encoding_format", "float") != "float":
            raise CompatibilityError("Only float embedding format is supported.")
        inputs = body.get("input")
        if isinstance(inputs, str):
            inputs = [inputs]
        if (not isinstance(inputs, list) or not 1 <= len(inputs) <= 4
                or any(not isinstance(item, str) or not item.strip()
                       or len(item.encode()) > 16384 for item in inputs)):
            raise CompatibilityError("Embedding inputs must contain 1 to 4 bounded text strings.")
        if self.embedding_encoder is None and not self._embedding_installed():
            raise CompatibilityError("The pinned embedding model is not installed.", status=503,
                                     code="model_unavailable")
        self.log.emit("embedding.start", run_id=run_id, model=body["model"], count=len(inputs))
        if self.embedding_encoder is not None:
            batch = self.embedding_encoder(tuple(inputs))
        else:
            from potbot.dependencies import MODEL_DIRECTORY, project_root
            from potbot.memory.local_encoder import QwenLocalEncoder

            model_dir = project_root() / "models" / MODEL_DIRECTORY
            batch = QwenLocalEncoder(model_dir).encode_supervised(
                tuple(inputs), "document", timeout_ms=60_000)
        result = {"object": "list", "model": body["model"],
                  "data": [{"object": "embedding", "index": i, "embedding": list(vector)}
                           for i, vector in enumerate(batch.vectors)],
                  "usage": {"prompt_tokens": batch.input_tokens, "total_tokens": batch.input_tokens}}
        self.log.emit("embedding.completed", run_id=run_id, model=body["model"],
                      count=len(inputs), tokens=batch.input_tokens,
                      elapsed_ms=batch.elapsed_ms)
        return result

    def _eligible(self, context: WorkspaceContext) -> list[dict[str, Any]]:
        listing = self.catalog(context)
        return [model for model in listing.get("models", []) if model.get("generation_enabled") and model.get("text_generation") is not False and not is_known_non_text_model(model["model"])]

    def models(self, context: WorkspaceContext) -> dict[str, Any]:
        entries = [
            {"id": "potbot", "object": "model", "created": 0, "owned_by": "potbot"}
        ]
        entries.extend(
            {"id": f"{record['provider']}/{record['model']}", "object": "model", "created": 0,
             "owned_by": record["provider"]}
            for record in self._eligible(context)
        )
        if self.embedding_encoder is not None or self._embedding_installed():
            entries.append({"id": "local/" + QWEN_MODEL, "object": "model", "created": 0,
                            "owned_by": "potbot"})
        return {"object": "list", "data": sorted(entries, key=lambda entry: entry["id"])}

    def model(self, context: WorkspaceContext, model_id: str) -> dict[str, Any]:
        for model in self.models(context)["data"]:
            if model["id"] == model_id:
                return model
        raise CompatibilityError("The requested model is unavailable or disallowed.", status=404, code="model_not_found")

    def _route(self, context: WorkspaceContext, model: str) -> list[dict[str, Any]]:
        records = self._eligible(context)
        if model == "potbot":
            choices = [r for r in records if "reasoning" in r.get("roles", [])]
            if not choices:
                raise CompatibilityError("No configured reasoning model is available for potbot.", status=503, code="model_unavailable")
            return choices[:2]
        for record in records:
            if f"{record['provider']}/{record['model']}" == model:
                return [record]
        raise CompatibilityError("The requested model is unavailable or disallowed.", status=404, code="model_not_found")

    def _invoke(
        self,
        model: dict[str, Any], system: str, prompt: str, options: dict[str, Any],
        on_text: Callable[[str], None], operation_id: str,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[str, dict[str, int], str]:
        provider = model["provider"]
        selection = ProviderSelection(provider, model["model"], model.get("protocol"))
        request = ProviderRequest(
            selection, prompt=prompt, system=system,
            max_output_tokens=options["max_output_tokens"],
            timeout_seconds=min(120, self.policy.max_timeout_seconds),
            temperature=options["temperature"], top_p=options["top_p"],
        )
        self.policy.authorize(request)
        from potbot.tools.secrets import CaptureRedactor

        key = self.credentials.resolve(request.selection.credential_ref)
        protected = CaptureRedactor((key,))
        prompt_bytes = (prompt + system).encode("utf-8")
        if any(pattern in prompt_bytes for pattern in protected.patterns):
            raise CompatibilityError("A service credential cannot appear in a model prompt.",
                                     status=403, code="invalid_request_error")
        self.log.emit("model.query", run_id=operation_id, provider=provider,
                      model=model["model"], bytes=len(prompt.encode()), content=prompt)
        rate = self.policy.rule(provider, model["model"])
        estimate_tokens = len((prompt + system).encode()) + request.max_output_tokens
        if rate.charge(estimate_tokens, request.max_output_tokens) > self.policy.max_cost_microunits:
            raise CompatibilityError("Estimated inference cost exceeds operator policy.",
                                     status=403, code="budget_exceeded")
        # Secrets stay in the resolver and the client; never appear in events.
        client = self.client_factory(self.credentials.resolve, authorize=self.policy.authorize)
        output: list[str] = []
        usage: dict[str, int] = _usage(None)
        finish: str | None = None
        try:
            for event in client.stream(request, uuid4(), cancelled=cancelled):
                if isinstance(event, ProviderTextDelta):
                    output.append(event.text)
                    on_text(event.text)
                elif isinstance(event, ProviderCompleted):
                    usage = _usage(event.usage.to_dict() if event.usage else None)
                    finish = event.finish_reason
        finally:
            client.close()
        if finish is None:
            raise CompatibilityError("The model did not complete its response.", status=502, code="provider_error")
        self.log.emit("model.response", run_id=operation_id, provider=provider,
                      model=model["model"], content="".join(output), bytes=sum(len(p.encode()) for p in output))
        self.log.emit("model.complete", run_id=operation_id, provider=provider,
                      model=model["model"], usage=usage, finish_reason=finish)
        return "".join(output), usage, finish

    def stream(
        self, context: WorkspaceContext, options: dict[str, Any], on_text: Callable[[str], None],
        operation_id: str, cancelled: Callable[[], bool] | None = None,
    ) -> FinalAnswer:
        routes = self._route(context, options["model"])
        self.log.emit("api.query", run_id=operation_id, model=options["model"],
                      bytes=len(options["prompt"].encode()), content=options["prompt"])
        started = time.monotonic()
        total = _usage(None)
        outputs: list[str] = []
        final_text = ""
        finish = "stop"
        invocations = 0
        try:
            if options["model"] != "potbot":
                final_text, usage, finish = self._invoke(
                    routes[0], options["system"], options["prompt"], options,
                    on_text, operation_id, cancelled,
                )
                invocations = 1
                total = usage
            else:
                # Three bounded, auditable *outputs*: proposal, review, synthesis.
                # No hidden chain-of-thought is returned or recorded.
                count = self.max_potbot_calls
                for index in range(count):
                    provider = routes[index % len(routes)]
                    self.log.emit("reasoning.stage", run_id=operation_id,
                                  stage=("proposal", "critique", "synthesis")[index],
                                  provider=provider["provider"], model=provider["model"])
                    if index == 0:
                        system = options["system"] + "\nProvide a concise draft answer with supporting evidence and uncertainties."
                        prompt = options["prompt"]
                    elif index == count - 1:
                        system = options["system"] + "\nProduce the best final answer. Resolve weaknesses in the drafts. Do not reveal private intermediate deliberation."
                        prompt = options["prompt"] + "\n\nPrior draft(s):\n" + "\n\n".join(outputs)
                    else:
                        system = options["system"] + "\nReview the draft for mistakes, missing evidence and alternatives. Return a concise review."
                        prompt = options["prompt"] + "\n\nDraft:\n" + outputs[-1]
                    # Constrain intermediate output, context and cumulative calls.
                    prompt = prompt[:min(self.policy.max_prompt_bytes // 2, 60000)]
                    system = system[:12000]
                    emit = on_text if index == count - 1 else (lambda _delta: None)
                    result, usage, finish = self._invoke(
                        provider, system, prompt, options, emit, operation_id, cancelled,
                    )
                    invocations += 1
                    outputs.append(result[:18000])
                    final_text = result
                    for name in total:
                        total[name] += usage[name]
                    if finish != "stop":
                        break
                if finish != "stop":
                    raise CompatibilityError("The reasoning workflow did not complete.", status=502, code="provider_error")
            self.log.emit("api.response", run_id=operation_id, model=options["model"],
                          duration_ms=int((time.monotonic() - started) * 1000),
                          bytes=len(final_text.encode()), usage=total,
                          invocations=invocations, content=final_text)
            return FinalAnswer(final_text, total, finish, routes[-1]["provider"], invocations)
        except Exception:
            self.log.emit("api.failure", run_id=operation_id, model=options["model"],
                          duration_ms=int((time.monotonic() - started) * 1000))
            raise


def chat_document(identifier: str, created: int, model: str, result: FinalAnswer) -> dict[str, Any]:
    return {
        "id": identifier, "object": "chat.completion", "created": created,
        "model": model, "choices": [{"index": 0, "message": {"role": "assistant", "content": result.text},
                                "logprobs": None, "finish_reason": result.finish_reason}],
        "usage": result.usage,
    }


def response_document(identifier: str, created: int, model: str, result: FinalAnswer) -> dict[str, Any]:
    return {
        "id": identifier, "object": "response", "created_at": created, "model": model,
        "status": "completed", "error": None, "incomplete_details": None,
        "output": [{"id": "msg_" + uuid4().hex, "type": "message", "status": "completed",
                    "role": "assistant", "content": [{"type": "output_text", "text": result.text,
                                                        "annotations": []}]}],
        "usage": {"input_tokens": result.usage["prompt_tokens"],
                  "output_tokens": result.usage["completion_tokens"],
                  "total_tokens": result.usage["total_tokens"]},
    }


def sse(event: dict[str, Any] | str, name: str | None = None) -> bytes:
    header = f"event: {name}\n" if name else ""
    data = event if isinstance(event, str) else json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return (header + "data: " + data + "\n\n").encode("utf-8")
