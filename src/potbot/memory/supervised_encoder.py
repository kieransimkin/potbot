"""Hard-deadline local Qwen preparation and inference in an isolated worker."""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from potbot.domain.errors import (
    CapabilityUnavailableError,
    IndeterminateExecutionError,
    InvalidInputError,
    ModelUnavailableError,
    OperationCancelledError,
    OperationTimeoutError,
)
from potbot.domain.serialization import canonical_json, content_hash
from potbot.memory.embedding_profile import EmbeddingBatch, EmbeddingProfile
from potbot.tools.supervisor import ProcessRequest, ProcessResult, ProcessSupervisor


class SupervisedEncodingFailure(IndeterminateExecutionError):
    """A failed worker retains dispatch certainty for budget reconciliation."""

    def __init__(self, result: ProcessResult) -> None:
        self.dispatched = result.dispatched
        self.result = result
        if not result.processes_stopped:
            self.code = IndeterminateExecutionError.code
        elif result.outcome == "timeout":
            self.code = OperationTimeoutError.code
        elif result.outcome == "cancelled":
            self.code = OperationCancelledError.code
        elif result.error_code == "model_unavailable":
            self.code = ModelUnavailableError.code
        elif not result.dispatched:
            self.code = CapabilityUnavailableError.code
        else:
            self.code = ModelUnavailableError.code
        super().__init__("local embedding worker did not return a confirmed result")


class SupervisedQwenEncoder:
    """The parent owns publication; a killed/fenced worker has no database access."""

    def __init__(
        self,
        model_dir: Path,
        profile: EmbeddingProfile | None = None,
        *,
        supervisor: ProcessSupervisor | None = None,
        worker_python: Path | None = None,
        timeout_ms: int = 60_000,
    ) -> None:
        self.profile = profile or EmbeddingProfile()
        self.model_dir = model_dir.resolve()
        self.supervisor = supervisor or ProcessSupervisor()
        self.worker_python = worker_python or Path(sys.executable)
        self.timeout_ms = timeout_ms

    def _run(
        self,
        texts: tuple[str, ...],
        kind: Literal["query", "document"],
        operation: Literal["count", "encode"],
        timeout_ms: int,
        cancelled: Callable[[], bool] | None,
    ) -> dict[str, object]:
        if not isinstance(texts, tuple) or not 1 <= len(texts) <= self.profile.max_batch_size:
            raise InvalidInputError("embedding batch exceeds the configured bound")
        for text in texts:
            self.profile.prepare(text, kind)
        if not self.model_dir.is_dir():
            raise SupervisedEncodingFailure(
                ProcessResult(
                    "infrastructure_error",
                    None,
                    b"",
                    b"local model directory unavailable",
                    0,
                    False,
                    False,
                    False,
                    None,
                    "model_unavailable",
                    processes_stopped=True,
                )
            )
        source_root = Path(__file__).resolve().parents[2]
        request_payload = {
            "schema_version": 1,
            "operation": operation,
            "texts": list(texts),
            "kind": kind,
            "model_dir": str(self.model_dir),
            "profile": self.profile.to_dict(),
        }
        bootstrap = (
            "import runpy,sys;sys.path.insert(0,sys.argv[1]);"
            "runpy.run_module('potbot.memory.embedding_worker',run_name='__main__')"
        )
        # Each invocation gets an empty writable scratch root. The model and
        # trusted adapter are read-only; application artifacts and DSNs are absent.
        with tempfile.TemporaryDirectory(prefix="potbot-embedding-worker-") as temporary:
            work = Path(temporary)
            request = ProcessRequest(
                (str(self.worker_python), "-I", "-c", bootstrap, str(source_root)),
                work,
                work,
                timeout_ms=timeout_ms,
                max_output_bytes=4_194_304,
                environment={
                    "HF_HUB_OFFLINE": "1",
                    "TRANSFORMERS_OFFLINE": "1",
                    "TOKENIZERS_PARALLELISM": "false",
                    "OMP_NUM_THREADS": "2",
                    "MKL_NUM_THREADS": "2",
                    "OPENBLAS_NUM_THREADS": "2",
                },
                read_only_roots=(self.model_dir, source_root, self.worker_python.parent.parent),
                stdin=canonical_json(request_payload).encode("utf-8"),
                memory_bytes=32 * 1024**3,
                cpu_seconds=max(1, (timeout_ms + 999) // 1000),
            )
            result = self.supervisor.run(request, cancelled=cancelled)
        if result.outcome != "completed" or result.stdout_truncated or not result.processes_stopped:
            raise SupervisedEncodingFailure(result)
        try:
            payload = json.loads(result.stdout)
            if (
                not isinstance(payload, dict)
                or payload.get("profile_hash") != self.profile.fingerprint
                or payload.get("input_hash") != content_hash({"texts": list(texts), "kind": kind})
                or payload.get("operation") != operation
            ):
                raise ValueError
            return payload
        except (ValueError, UnicodeDecodeError) as error:
            raise SupervisedEncodingFailure(result) from error

    def count_tokens(self, texts: tuple[str, ...], kind: Literal["query", "document"]) -> int:
        payload = self._run(texts, kind, "count", self.timeout_ms, None)
        tokens = payload.get("input_tokens")
        if type(tokens) is not int or not 0 < tokens <= self.profile.max_input_tokens * len(texts):
            raise InvalidInputError("local worker returned invalid token usage")
        return tokens

    def encode_supervised(
        self,
        texts: tuple[str, ...],
        kind: Literal["query", "document"],
        *,
        timeout_ms: int,
        cancelled: Callable[[], bool] | None = None,
    ) -> EmbeddingBatch:
        payload = self._run(texts, kind, "encode", timeout_ms, cancelled)
        try:
            vectors = payload["vectors"]
            tokens = payload["input_tokens"]
            elapsed = payload["elapsed_ms"]
            if not isinstance(vectors, list) or type(tokens) is not int or type(elapsed) is not int:
                raise ValueError
            if not 0 < tokens <= self.profile.max_input_tokens * len(texts) or len(vectors) != len(
                texts
            ):
                raise ValueError
            return EmbeddingBatch(tuple(tuple(row) for row in vectors), tokens, elapsed)
        except (TypeError, ValueError, KeyError) as error:
            raise InvalidInputError("local worker returned an invalid embedding batch") from error

    def encode(self, texts: tuple[str, ...], kind: Literal["query", "document"]) -> EmbeddingBatch:
        return self.encode_supervised(texts, kind, timeout_ms=self.timeout_ms)
