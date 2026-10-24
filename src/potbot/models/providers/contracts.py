"""Hosted identities and bounded requests without invented local model assets."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from potbot.domain.errors import InvalidInputError, ModelUnavailableError
from potbot.domain.serialization import content_hash

PROVIDERS = ("anthropic", "openai", "xai", "gemini", "evolink")
DEFAULT_PROTOCOLS = {
    "anthropic": "messages",
    "openai": "responses",
    "xai": "responses",
    "gemini": "gemini",
    "evolink": "chat_completions",
}
ALLOWED_PROTOCOLS = {
    "anthropic": {"messages"},
    "openai": {"responses", "chat_completions"},
    "xai": {"responses", "chat_completions"},
    "gemini": {"gemini"},
    "evolink": {"responses", "chat_completions", "messages", "gemini"},
}
ORIGINS = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
    "xai": "https://api.x.ai",
    "gemini": "https://generativelanguage.googleapis.com",
    "evolink": "https://direct.evolink.ai",
}
CATALOG_ORIGINS = {**ORIGINS, "evolink": "https://api.evolink.ai"}
MAX_TEXT_BYTES = 4 * 1024 * 1024
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,255}\Z")
_NON_TEXT_FAMILY = re.compile(
    r"(?:text-embedding|embedding|embeddings|gemini-embedding|dall-e|gpt-image|whisper|tts|"
    r"omni-moderation|text-moderation|evolink-moderation|imagen|veo|sora|grok-imagine|suno|"
    r"seedance|seedream|seed-audio|kling|nano-banana|nanobanana|flux|hailuo|midjourney|"
    r"qwen-image|wan-image|qwen-tts|gemini-omni)(?:$|[-_.0-9])"
)


def is_known_non_text_model(model: str) -> bool:
    """Reject recognizable dedicated media families, without claiming text qualification.

    Catalogues still retain these entries. A false result means only that this
    small deny list did not recognize the name; operator capability verification
    remains necessary, especially for opaque aliases and future model names.
    """
    name = model.rsplit("/", 1)[-1].lower()
    return bool(_NON_TEXT_FAMILY.match(name)) or (
        name.startswith("gemini-") and bool(re.search(r"-(?:image|tts|native-audio)(?:-|$)", name))
    )


def default_protocol(provider: str, model: str) -> str:
    """Use documented native EvoLink routes for the three major model families."""
    provider_name(provider)
    model_name(model)
    if provider == "evolink":
        name = model.rsplit("/", 1)[-1].lower()
        if name.startswith("claude-"):
            return "messages"
        if name.startswith("gemini-"):
            return "gemini"
        if name.startswith("gpt-"):
            return "responses"
    return DEFAULT_PROTOCOLS[provider]


def provider_name(value: object) -> str:
    if not isinstance(value, str) or value not in PROVIDERS:
        raise InvalidInputError("Unknown hosted model provider.")
    return value


def model_name(value: object) -> str:
    if not isinstance(value, str) or not _MODEL.fullmatch(value) or ".." in value or "//" in value:
        raise InvalidInputError("A bounded provider model identifier is required.")
    return value


def _text(value: object, *, allow_empty: bool = False, maximum: int = MAX_TEXT_BYTES) -> str:
    if not isinstance(value, str) or (not value and not allow_empty) or "\x00" in value:
        raise InvalidInputError("Hosted input must be bounded UTF-8 text.")
    try:
        if len(value.encode("utf-8")) > maximum:
            raise InvalidInputError("Hosted input exceeds its byte bound.")
    except UnicodeError:
        raise InvalidInputError("Hosted input must be valid UTF-8 text.") from None
    return value


@dataclass(frozen=True, slots=True)
class ProviderSelection:
    provider: str
    model: str
    protocol: str | None = None

    def __post_init__(self) -> None:
        provider_name(self.provider)
        model_name(self.model)
        if is_known_non_text_model(self.model):
            raise InvalidInputError("A dedicated non-text model cannot be selected for generation.")
        protocol = (
            default_protocol(self.provider, self.model) if self.protocol is None else self.protocol
        )
        if not isinstance(protocol, str) or protocol not in ALLOWED_PROTOCOLS[self.provider]:
            raise InvalidInputError("The provider does not support the selected API protocol.")
        object.__setattr__(self, "protocol", protocol)

    @property
    def origin(self) -> str:
        return ORIGINS[self.provider]

    @property
    def credential_ref(self) -> str:
        return "secret://providers/" + self.provider

    def to_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model, "protocol": self.protocol}


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    selection: ProviderSelection
    prompt: str
    system: str = ""
    max_output_tokens: int = 2048
    timeout_seconds: float = 60
    temperature: float | None = None
    top_p: float | None = None
    seed: int | None = None
    max_response_bytes: int = MAX_TEXT_BYTES
    response_format: str = "text"

    def __post_init__(self) -> None:
        if not isinstance(self.selection, ProviderSelection):
            raise InvalidInputError("A typed hosted model selection is required.")
        if self.response_format not in {"text", "json_object"}:
            raise InvalidInputError("Unsupported hosted response format.")
        if self.response_format == "json_object" and self.selection.protocol not in {
            "responses", "chat_completions", "gemini"
        }:
            raise InvalidInputError("The selected protocol cannot enforce native JSON output.")
        _text(self.prompt)
        _text(self.system, allow_empty=True, maximum=65536)
        if (
            type(self.max_output_tokens) is not int
            or not 1 <= self.max_output_tokens <= 1_048_576
            or type(self.max_response_bytes) is not int
            or not 1 <= self.max_response_bytes <= MAX_TEXT_BYTES
            or type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or not 0.1 <= self.timeout_seconds <= 3600
        ):
            raise InvalidInputError("Hosted generation bounds are invalid.")
        for value, maximum in ((self.temperature, 2), (self.top_p, 1)):
            if value is not None and (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 <= value <= maximum
            ):
                raise InvalidInputError("Hosted sampling parameters are invalid.")
        if self.seed is not None and (type(self.seed) is not int or not 0 <= self.seed < 2**63):
            raise InvalidInputError("Hosted seed must be a bounded nonnegative integer.")
        if self.seed is not None and self.selection.protocol in {"messages", "responses"}:
            raise InvalidInputError("The selected protocol has no supported seed parameter.")

    @property
    def fingerprint(self) -> str:
        return content_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "selection": self.selection.to_dict(),
            "prompt": self.prompt,
            "system": self.system,
            "max_output_tokens": self.max_output_tokens,
            "timeout_seconds": self.timeout_seconds,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "seed": self.seed,
            "max_response_bytes": self.max_response_bytes,
            "response_format": self.response_format,
        }


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    reasoning_tokens: int = 0
    origin: str = "provider_reported"

    def __post_init__(self) -> None:
        values = (
            self.input_tokens,
            self.output_tokens,
            self.total_tokens,
            self.cached_input_tokens,
            self.cache_creation_input_tokens,
            self.reasoning_tokens,
        )
        if (
            any(type(v) is not int or not 0 <= v <= 2**53 - 1 for v in values)
            or self.total_tokens < self.input_tokens + self.output_tokens
            or self.origin != "provider_reported"
            or self.cached_input_tokens + self.cache_creation_input_tokens > self.input_tokens
            or self.reasoning_tokens > self.output_tokens
        ):
            raise InvalidInputError("Provider usage is inconsistent or outside accounting bounds.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "origin": self.origin,
        }


@dataclass(frozen=True, slots=True)
class ProviderTextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ProviderCompleted:
    finish_reason: str
    usage: ProviderUsage | None
    model: str | None = None
    provider_request_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "finish_reason": self.finish_reason,
            "usage": self.usage.to_dict() if self.usage else None,
            "model": self.model,
            "provider_request_id": self.provider_request_id,
        }


ProviderEvent = ProviderTextDelta | ProviderCompleted


class ProviderAPIError(ModelUnavailableError):
    """Sanitized transport classification; response bodies are never exposed."""

    def __init__(
        self,
        provider_error_code: str,
        *,
        dispatched: bool,
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        self.provider_error_code = provider_error_code
        self.dispatched = dispatched
        self.retryable = retryable
        self.status_code = status_code
        super().__init__("Hosted provider request failed: " + provider_error_code + ".")


@dataclass(frozen=True, slots=True)
class CatalogModel:
    provider: str
    model: str
    display_name: str
    text_generation: bool | None
    supported_protocols: tuple[str, ...] = ()
    modalities: tuple[str, ...] = ()
    input_token_limit: int | None = None
    output_token_limit: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "display_name": self.display_name,
            "text_generation": self.text_generation,
            "supported_protocols": list(self.supported_protocols),
            "modalities": list(self.modalities),
            "input_token_limit": self.input_token_limit,
            "output_token_limit": self.output_token_limit,
            "qualified": False,
            "capability_verification_required": self.text_generation is None,
        }
