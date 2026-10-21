"""Complete, immutable identities for the standard 1024-dimensional vector space."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol

from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import content_hash

QWEN_MODEL = "Qwen/Qwen3-Embedding-0.6B"
QWEN_REVISION = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
TRANSFORMERS_VERSION = "4.57.6"
TORCH_VERSION = "2.9.1"
QUERY_INSTRUCTION = (
    "Retrieve relevant code, documentation, and proven workflows for the programming task"
)


@dataclass(frozen=True, slots=True)
class EmbeddingProfile:
    model: str = QWEN_MODEL
    model_revision: str = QWEN_REVISION
    tokenizer_revision: str = QWEN_REVISION
    query_instruction: str = QUERY_INSTRUCTION
    document_format: str = "raw"
    pooling: str = "last_token"
    normalization: str = "l2"
    dimensions: int = 1024
    precision: str = "float32"
    metric: str = "cosine"
    preprocessing: str = "identity-v1"
    generation: str = "standard-v1"
    backend: str = "transformers"
    backend_version: str = TRANSFORMERS_VERSION
    runtime_version: str = TORCH_VERSION
    max_input_tokens: int = 2048
    max_batch_size: int = 4
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or type(self.schema_version) is not int:
            raise InvalidInputError("unknown embedding schema")
        for name in (
            "model",
            "query_instruction",
            "generation",
            "backend",
            "backend_version",
            "runtime_version",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise InvalidInputError("embedding profile requires complete versioned inputs")
        for revision in (self.model_revision, self.tokenizer_revision):
            if not isinstance(revision, str) or not re.fullmatch(
                r"[0-9a-f]{40}|[0-9a-f]{64}", revision
            ):
                raise InvalidInputError("embedding model and tokenizer require immutable revisions")
        if (
            (
                self.document_format,
                self.pooling,
                self.normalization,
                self.precision,
                self.metric,
                self.preprocessing,
            )
            != ("raw", "last_token", "l2", "float32", "cosine", "identity-v1")
            or self.dimensions != 1024
            or type(self.dimensions) is not int
        ):
            raise InvalidInputError(
                "this integration supports only the standard 1024-dimensional profile"
            )
        if (
            type(self.max_input_tokens) is not int
            or not 1 <= self.max_input_tokens <= 32768
            or type(self.max_batch_size) is not int
            or not 1 <= self.max_batch_size <= 32
        ):
            raise InvalidInputError("embedding input and batch limits must be bounded")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def fingerprint(self) -> str:
        return content_hash(self.to_dict())

    def prepare(self, text: str, kind: Literal["query", "document"]) -> str:
        if not isinstance(text, str) or not text.strip() or kind not in {"query", "document"}:
            raise InvalidInputError("embedding input must be nonempty query or document text")
        if len(text) > 2_000_000:
            raise InvalidInputError("embedding input exceeds the byte admission limit")
        return f"Instruct: {self.query_instruction}\nQuery:{text}" if kind == "query" else text


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    vectors: tuple[tuple[float, ...], ...]
    input_tokens: int
    elapsed_ms: int
    usage_origin: str = "measured"

    def __post_init__(self) -> None:
        if (
            type(self.input_tokens) is not int
            or self.input_tokens < 0
            or type(self.elapsed_ms) is not int
            or self.elapsed_ms < 0
            or self.usage_origin not in {"synthetic", "measured", "provider_reported"}
        ):
            raise InvalidInputError("invalid embedding usage")
        for vector in self.vectors:
            validate_vector(vector)


def validate_vector(vector: tuple[float, ...] | list[float]) -> tuple[float, ...]:
    if (
        not isinstance(vector, (tuple, list))
        or len(vector) != 1024
        or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector)
    ):
        raise InvalidInputError("embedding output must contain exactly 1024 finite numbers")
    norm = math.sqrt(sum(v * v for v in vector))
    if not math.isfinite(norm) or abs(norm - 1) > 1e-4:
        raise InvalidInputError("embedding output must be L2-normalized and nonzero")
    return tuple(float(v) for v in vector)


class EmbeddingProvider(Protocol):
    profile: EmbeddingProfile

    def count_tokens(self, texts: tuple[str, ...], kind: Literal["query", "document"]) -> int: ...

    def encode(
        self, texts: tuple[str, ...], kind: Literal["query", "document"]
    ) -> EmbeddingBatch: ...
