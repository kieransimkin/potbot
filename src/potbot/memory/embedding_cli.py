"""Explicit pinned-model setup and budgeted local vector operations."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from potbot.dependencies import (
    MODEL_DIRECTORY,
    ConsoleProgress,
    ensure_standard_model,
    project_root,
)
from potbot.domain.embeddings import DEFAULT_EMBEDDING_NAME, SOURCE_TEXT_INPUT_FORMAT
from potbot.domain.errors import DomainError, InvalidInputError
from potbot.memory.embedding_profile import EmbeddingProfile


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="potbot embedding", description=__doc__)
    default_model_dir = project_root() / "models" / MODEL_DIRECTORY
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("profile", help="show the exact standard local embedding profile")
    setup = sub.add_parser("setup", help="download pinned model artifacts explicitly")
    setup.add_argument("--model-dir", type=Path, default=default_model_dir)
    smoke = sub.add_parser("smoke", help="qualify local encoding without a database")
    smoke.add_argument("--model-dir", type=Path, default=default_model_dir)
    smoke.add_argument("--max-input-tokens", type=int, default=256)
    smoke.add_argument("--timeout-ms", type=int, default=120_000)
    for name in ("register-space", "put", "index", "search", "retire", "manifest"):
        command = sub.add_parser(name)
        command.add_argument("--workspace-id", type=UUID, required=True)
        command.add_argument("--principal-id", type=UUID, required=True)
        command.add_argument("--schema", default="potbot")
        command.add_argument("--artifact-root", type=Path)
        command.add_argument("--model-dir", type=Path)
        command.add_argument("--max-input-tokens", type=int, default=2048)
        if name in {"register-space", "index", "search"}:
            command.add_argument(
                "--embedding-name",
                default=DEFAULT_EMBEDDING_NAME,
                help="registered representation name; always paired with an exact space",
            )
        if name in {"put", "search", "retire"}:
            command.add_argument("--repository-id", type=UUID, required=True)
        if name in {"put", "retire"}:
            command.add_argument("--source-key", required=True)
        if name == "put":
            command.add_argument("--file", type=Path, required=True)
        if name in {"index", "search"}:
            command.add_argument("--episode-id", type=UUID, required=True)
            command.add_argument("--space-id", type=UUID, required=True)
            command.add_argument("--runtime-budget-ms", type=int, default=60_000)
        if name == "index":
            command.add_argument("--source-id", type=UUID, required=True)
        if name == "search":
            command.add_argument("--query", required=True)
            command.add_argument("--limit", type=int, default=5)
        if name == "manifest":
            command.add_argument("--artifact-id", type=UUID, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "profile":
            profile = EmbeddingProfile()
            output: object = {
                "profile": profile.to_dict(),
                "fingerprint": profile.fingerprint,
                "default_embedding_name": DEFAULT_EMBEDDING_NAME,
                "input_format": SOURCE_TEXT_INPUT_FORMAT,
            }
        elif args.command == "setup":
            from potbot.memory.local_encoder import setup_local_model

            output = setup_local_model(args.model_dir)
        elif args.command == "smoke":
            from potbot.memory.local_encoder import QwenLocalEncoder

            profile = replace(EmbeddingProfile(), max_input_tokens=args.max_input_tokens)
            ensure_standard_model(
                project_root(), model_dir=args.model_dir, progress=ConsoleProgress()
            )
            encoder = QwenLocalEncoder(args.model_dir, profile)
            documents = encoder.encode_supervised(
                (
                    "A Python context manager closes its database connection when the block exits.",
                    "A database transaction commits changes atomically and rolls back on failure.",
                    "A CSS stylesheet controls colors, layout, and fonts.",
                ),
                "document",
                timeout_ms=args.timeout_ms,
            )
            queries = encoder.encode_supervised(
                (
                    "How can I ensure a database connection is closed?",
                    "How can I make database writes atomic?",
                ),
                "query",
                timeout_ms=args.timeout_ms,
            )
            scores = [
                [sum(a * b for a, b in zip(q, d, strict=True)) for d in documents.vectors]
                for q in queries.vectors
            ]
            ranks = [max(range(len(row)), key=row.__getitem__) for row in scores]
            if ranks != [0, 1]:
                raise InvalidInputError("local embedding smoke retrieval did not match the fixture")
            output = {
                "schema_version": 1,
                "profile": profile.to_dict(),
                "profile_hash": profile.fingerprint,
                "dimensions": 1024,
                "finite_normalized": True,
                "top_document_indices": ranks,
                "cosine_scores": scores,
                "input_tokens": documents.input_tokens + queries.input_tokens,
                "elapsed_ms": documents.elapsed_ms + queries.elapsed_ms,
                "usage_origin": "measured",
                "qualification": "local_smoke_only",
                "isolation_backend": "bubblewrap-seccomp-v1",
                "hard_timeout_ms": args.timeout_ms,
                "preparation_included": True,
            }
        else:
            from potbot.credentials import resolve_postgres_dsn
            from potbot.domain.auth import WorkspaceContext
            from potbot.memory.embeddings import EmbeddingService
            from potbot.memory.local_encoder import QwenLocalEncoder
            from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
            from potbot.persistence.database import Database

            dsn = resolve_postgres_dsn()
            database = Database(dsn, schema=args.schema)
            context = WorkspaceContext(args.workspace_id, args.principal_id)
            from potbot.workspace_sandbox import load_workspace_sandbox

            sandbox = load_workspace_sandbox(database, context)
            model_dir = (
                sandbox.confine(args.model_dir, area="models")
                if args.model_dir is not None
                else sandbox.path("models") / MODEL_DIRECTORY
            )
            artifact_root = (
                sandbox.confine(args.artifact_root, area="artifacts")
                if args.artifact_root is not None
                else sandbox.path("artifacts") / "objects"
            )
            profile = replace(EmbeddingProfile(), max_input_tokens=args.max_input_tokens)
            if args.command in {"index", "search"}:
                ensure_standard_model(
                    sandbox.root, model_dir=model_dir, progress=ConsoleProgress()
                )
            service = EmbeddingService(
                database,
                ArtifactStore(database, LocalArtifactBackend(artifact_root)),
                QwenLocalEncoder(model_dir, profile, sandbox_root=sandbox.root),
            )
            if args.command == "register-space":
                output = service.register_space(context, embedding_name=args.embedding_name)
            elif args.command == "put":
                output = service.register_source(
                    context,
                    args.repository_id,
                    args.source_key,
                    sandbox.confine(args.file, area="repository", must_exist=True).read_text(
                        encoding="utf-8"
                    ),
                    metadata={
                        "path": str(sandbox.confine(args.file, area="repository", must_exist=True))
                    },
                )
            elif args.command == "index":
                output = service.index_source(
                    context,
                    args.episode_id,
                    args.source_id,
                    args.space_id,
                    embedding_name=args.embedding_name,
                    runtime_budget_ms=args.runtime_budget_ms,
                )
            elif args.command == "search":
                output = service.search(
                    context,
                    args.episode_id,
                    args.repository_id,
                    args.space_id,
                    args.query,
                    embedding_name=args.embedding_name,
                    limit=args.limit,
                    runtime_budget_ms=args.runtime_budget_ms,
                )
            elif args.command == "manifest":
                output = service.artifacts.embedding_manifest(context, args.artifact_id)
            else:
                service.retire_source(context, args.repository_id, args.source_key)
                output = {"retired": True}
        print(json.dumps(output, sort_keys=True, default=str))
        return 0
    except DomainError as error:
        print(json.dumps({"error": error.code.value, "message": error.message}), file=sys.stderr)
        return 2
    except OSError:
        print(
            json.dumps(
                {"error": "source_unavailable", "message": "local embedding input is unavailable"}
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
