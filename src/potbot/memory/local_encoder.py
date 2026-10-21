"""Offline Qwen reference encoder. Network downloads occur only in explicit setup."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from potbot.domain.errors import InvalidInputError, ModelUnavailableError
from potbot.memory.embedding_profile import (
    QWEN_MODEL,
    QWEN_REVISION,
    TORCH_VERSION,
    TRANSFORMERS_VERSION,
    EmbeddingBatch,
    EmbeddingProfile,
    validate_vector,
)

MODEL_FILES = (
    "config.json",
    "model.safetensors",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.json",
    "merges.txt",
)
MANIFEST = "potbot-model.json"


class LocalModelUnavailable(ModelUnavailableError):
    """An explicitly configured local dependency or artifact is unavailable."""


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def setup_local_model(model_dir: Path) -> dict[str, Any]:
    """Explicit, resumable setup with pinned hashes; never called by encode."""
    from potbot.dependencies import ConsoleProgress, ensure_standard_model, project_root

    return ensure_standard_model(project_root(), model_dir=model_dir, progress=ConsoleProgress())


def verify_local_model(model_dir: Path, profile: EmbeddingProfile) -> None:
    try:
        manifest = json.loads((model_dir / MANIFEST).read_text(encoding="utf-8"))
        if (
            manifest["schema_version"] != 1
            or manifest["model"] != profile.model
            or manifest["revision"] != profile.model_revision
            or manifest["tokenizer_revision"] != profile.tokenizer_revision
            or set(manifest["files"]) != set(MODEL_FILES)
        ):
            raise ValueError
        for name in MODEL_FILES:
            path = model_dir / name
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size != manifest["files"][name]["bytes"]
                or _digest(path) != manifest["files"][name]["sha256"]
            ):
                raise ValueError
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise LocalModelUnavailable(
            "pinned local model manifest is missing, incompatible, or corrupt"
        ) from error


class QwenLocalEncoder:
    """CPU float32 adapter following Qwen's last-token/L2 reference convention."""

    def __init__(self, model_dir: Path, profile: EmbeddingProfile | None = None) -> None:
        self.profile = profile or EmbeddingProfile()
        if (
            self.profile.model != QWEN_MODEL
            or self.profile.model_revision != QWEN_REVISION
            or self.profile.tokenizer_revision != QWEN_REVISION
            or self.profile.backend != "transformers"
            or self.profile.backend_version != TRANSFORMERS_VERSION
            or self.profile.runtime_version != TORCH_VERSION
        ):
            raise InvalidInputError("local adapter requires its pinned Qwen runtime profile")
        self.model_dir = model_dir.resolve()
        self._tokenizer: Any = None
        self._model: Any = None
        self._torch: Any = None

    def _load_tokenizer(self) -> None:
        if self._tokenizer is not None:
            return
        verify_local_model(self.model_dir, self.profile)
        try:
            for package, version in (
                ("transformers", TRANSFORMERS_VERSION),
                ("torch", TORCH_VERSION),
            ):
                if importlib.metadata.version(package).split("+")[0] != version:
                    raise LocalModelUnavailable(
                        "local embedding runtime does not match the registered version"
                    )
            transformers = importlib.import_module("transformers")
            self._torch = importlib.import_module("torch")
            self._tokenizer = transformers.AutoTokenizer.from_pretrained(
                str(self.model_dir),
                local_files_only=True,
                trust_remote_code=False,
                padding_side="left",
            )
        except (ImportError, importlib.metadata.PackageNotFoundError, OSError) as error:
            raise LocalModelUnavailable("pinned local embedding runtime is unavailable") from error

    def _inputs(
        self, texts: tuple[str, ...], kind: Literal["query", "document"]
    ) -> tuple[list[str], int]:
        if not isinstance(texts, tuple) or not 1 <= len(texts) <= self.profile.max_batch_size:
            raise InvalidInputError("embedding batch exceeds the configured bound")
        prepared = [self.profile.prepare(text, kind) for text in texts]
        self._load_tokenizer()
        tokenized = self._tokenizer(prepared, padding=False, truncation=False)
        lengths = [len(ids) for ids in tokenized["input_ids"]]
        if any(length > self.profile.max_input_tokens for length in lengths):
            raise InvalidInputError(
                "embedding input exceeds the configured token limit; truncation is disabled"
            )
        return prepared, sum(lengths)

    def count_tokens(self, texts: tuple[str, ...], kind: Literal["query", "document"]) -> int:
        return self._inputs(texts, kind)[1]

    def encode_supervised(
        self,
        texts: tuple[str, ...],
        kind: Literal["query", "document"],
        *,
        timeout_ms: int,
        cancelled: Callable[[], bool] | None = None,
    ) -> EmbeddingBatch:
        """Production service entry: preparation and inference share one deadline."""
        from potbot.memory.supervised_encoder import SupervisedQwenEncoder

        return SupervisedQwenEncoder(self.model_dir, self.profile).encode_supervised(
            texts, kind, timeout_ms=timeout_ms, cancelled=cancelled
        )

    def encode(self, texts: tuple[str, ...], kind: Literal["query", "document"]) -> EmbeddingBatch:
        start = time.monotonic()
        prepared, tokens = self._inputs(texts, kind)
        if self._model is None:
            transformers = importlib.import_module("transformers")
            self._model = (
                transformers.AutoModel.from_pretrained(
                    str(self.model_dir),
                    local_files_only=True,
                    trust_remote_code=False,
                    use_safetensors=True,
                    dtype=self._torch.float32,
                    attn_implementation="eager",
                )
                .to("cpu")
                .eval()
            )
        batch = self._tokenizer(prepared, padding=True, truncation=False, return_tensors="pt")
        with self._torch.inference_mode():
            output = self._model(**batch, use_cache=False)
            # Left padding guarantees the final position is the last real token.
            pooled = output.last_hidden_state[:, -1, :]
            normalized = self._torch.nn.functional.normalize(pooled, p=2, dim=1)
            vectors = tuple(validate_vector(row) for row in normalized.tolist())
        return EmbeddingBatch(vectors, tokens, int((time.monotonic() - start) * 1000))
