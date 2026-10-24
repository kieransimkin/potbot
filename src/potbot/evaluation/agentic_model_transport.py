"""Explicit native-provider and loopback-only model adapters for role benchmarks.

Hosted calls require the normal Potbot disclosure/rate policy. Loopback models
can be served by Ollama, llama.cpp, LM Studio or a separately configured gateway.
No model is downloaded and no personal ChatGPT/Claude account is implicitly used.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from ipaddress import ip_address
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import uuid4

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.models.hosted_policy import HostedPolicy
from potbot.models.providers import (
    HostedProviderClient,
    ProviderCompleted,
    ProviderRequest,
    ProviderSelection,
    ProviderTextDelta,
)
from potbot.service_credentials import ServiceCredentials

if TYPE_CHECKING:
    from potbot.evaluation.agentic_model_bench import Generation, ModelTarget
    from potbot.models.prompt_compiler import CompiledPrompt


def validate_loopback_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 255:
        raise InvalidInputError("loopback endpoint is invalid")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        local = host == "localhost" or (host is not None and ip_address(host).is_loopback)
        valid = (
            parsed.scheme == "http" and local and parsed.port is not None
            and 1 <= parsed.port <= 65535 and parsed.path == "/v1"
            and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise InvalidInputError("benchmark endpoint must be explicit HTTP loopback /v1")
    return value


class BenchmarkTransport:
    """One model call without automatic retries, credential disclosure or URL overrides."""

    def __init__(
        self, *, policy: HostedPolicy | None = None,
        credentials: ServiceCredentials | None = None,
        local_key: str = "local-only",
        client_factory: Callable[..., Any] = HostedProviderClient,
        http_client: Any = None,
    ) -> None:
        self.policy = policy
        self.credentials = credentials
        self.local_key = local_key
        self.client_factory = client_factory
        self.http_client = http_client

    def request(self, target: ModelTarget, prompt: CompiledPrompt) -> Generation:
        from potbot.evaluation.agentic_model_bench import Generation

        if target.backend == "hosted":
            if self.policy is None or self.credentials is None:
                raise ScopeDeniedError("hosted benchmark requires an explicit policy and vault")
            assert target.provider is not None
            rule = self.policy.rule(target.provider, target.model)
            request = ProviderRequest(
                selection=ProviderSelection(target.provider, target.model, rule.protocol),
                prompt=prompt.user, system=prompt.system,
                max_output_tokens=target.max_output_tokens,
                timeout_seconds=90, response_format="text",
            )
            self.policy.authorize(request)
            client = self.client_factory(self.credentials.resolve, authorize=self.policy.authorize)
            completed: ProviderCompleted | None = None
            pieces = []
            started = time.monotonic()
            try:
                for event in client.stream(request, uuid4()):
                    if isinstance(event, ProviderTextDelta):
                        pieces.append(event.text)
                    if isinstance(event, ProviderCompleted):
                        completed = event
            finally:
                client.close()
            if completed is None or completed.finish_reason != "stop":
                raise InvalidInputError("provider output is incomplete, usage unresolved")
            usage = completed.usage
            charged = (rule.charge(usage.input_tokens, usage.output_tokens, total_tokens=usage.total_tokens)
                       if usage is not None else None)
            return Generation(
                text="".join(pieces), elapsed_ms=round((time.monotonic() - started) * 1000),
                input_tokens=usage.input_tokens if usage else None,
                output_tokens=usage.output_tokens if usage else None,
                billed_microunits=charged, returned_model=completed.model,
            )
        assert target.base_url is not None
        base = validate_loopback_url(target.base_url)
        import httpx

        data = {
            "model": target.model,
            "messages": [{"role": "system", "content": prompt.system},
                         {"role": "user", "content": prompt.user}],
            "max_tokens": target.max_output_tokens,
            "stream": False,
        }
        started = time.monotonic()
        own_client = self.http_client is None
        client = self.http_client or httpx.Client(
            timeout=httpx.Timeout(90.0, connect=5.0), follow_redirects=False,
            trust_env=False, limits=httpx.Limits(max_connections=1),
        )
        try:
            with client.stream("POST", base + "/chat/completions",
                               headers={"Authorization": "Bearer " + self.local_key},
                               json=data, follow_redirects=False) as response:
                if response.status_code != 200:
                    raise InvalidInputError("loopback model returned non-success status")
                chunks = []
                total = 0
                for part in response.iter_bytes():
                    total += len(part)
                    if total > 131072:
                        raise InvalidInputError("loopback model output exceeds the benchmark bound")
                    chunks.append(part)
            body = json.loads(b"".join(chunks))
        finally:
            if own_client:
                client.close()
        if (not isinstance(body, dict) or not isinstance(body.get("choices"), list)
                or len(body["choices"]) != 1):
            raise InvalidInputError("invalid loopback completion document")
        result = body["choices"][0].get("message", {}).get("content")
        if not isinstance(result, str):
            raise InvalidInputError("non-text local response is unavailable")
        usage = body.get("usage") or {}
        return Generation(
            result, round((time.monotonic() - started) * 1000),
            usage.get("prompt_tokens") if type(usage.get("prompt_tokens")) is int else None,
            usage.get("completion_tokens") if type(usage.get("completion_tokens")) is int else None,
            billed_microunits=None, returned_model=body.get("model"),
        )
