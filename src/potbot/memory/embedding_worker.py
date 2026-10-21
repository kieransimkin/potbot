"""Private worker protocol: no credentials, persistence APIs or publication rights."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Literal, cast

from potbot.domain.errors import DomainError, InvalidInputError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.memory.embedding_profile import EmbeddingProfile
from potbot.memory.local_encoder import QwenLocalEncoder


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(16_777_217)
        if len(raw) > 16_777_216:
            raise InvalidInputError("worker request exceeds the supported bound")
        request = json.loads(raw)
        if (
            not isinstance(request, dict)
            or set(request)
            != {"schema_version", "operation", "texts", "kind", "model_dir", "profile"}
            or request["schema_version"] != 1
            or request["operation"] not in {"count", "encode"}
            or request["kind"] not in {"query", "document"}
            or not isinstance(request["texts"], list)
        ):
            raise InvalidInputError("worker request is not supported")
        profile = EmbeddingProfile(**request["profile"])
        encoder = QwenLocalEncoder(Path(request["model_dir"]), profile)
        texts = tuple(request["texts"])
        kind = cast(Literal["query", "document"], request["kind"])
        if request["operation"] == "count":
            payload: dict[str, object] = {"input_tokens": encoder.count_tokens(texts, kind)}
        else:
            batch = encoder.encode(texts, kind)
            payload = {
                "vectors": batch.vectors,
                "input_tokens": batch.input_tokens,
                "elapsed_ms": batch.elapsed_ms,
            }
        payload.update(
            {
                "operation": request["operation"],
                "profile_hash": profile.fingerprint,
                "input_hash": content_hash({"texts": list(texts), "kind": kind}),
            }
        )
        sys.stdout.write(canonical_json(payload) + "\n")
        return 0
    except DomainError as error:
        sys.stderr.write(canonical_json({"error": error.code.value}) + "\n")
        return 2
    except (OSError, ValueError, TypeError, KeyError):
        sys.stderr.write('{"error":"invalid_worker_request"}\n')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
