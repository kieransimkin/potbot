"""Explicit local SYN snapshots, exact type inventories and budgeted vector queries."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from potbot.domain.embeddings import DEFAULT_EMBEDDING_NAME
from potbot.domain.errors import DomainError, InvalidInputError
from potbot.domain.syn import MAX_SYN_SNAPSHOT_BYTES, SYN_KINDS, SynSnapshot, parse_type_ref

from .catalog import (
    builtin_types,
    catalog_fingerprint,
    catalog_provenance,
    data_type_ref,
    extension_types,
)

if TYPE_CHECKING:
    from potbot.dependencies import DownloadProgress


def ensure_standard_model(
    root: Path,
    *,
    model_dir: Path | None = None,
    progress: Callable[[DownloadProgress], None] | None = None,
) -> dict[str, Any]:
    """Keep model preparation lazy and separately replaceable by CLI callers."""
    from potbot.dependencies import ensure_standard_model as prepare_model

    return prepare_model(root, model_dir=model_dir, progress=progress)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "embers":
        from .ember_cli import main as ember_main

        return ember_main(arguments[1:])
    if arguments and arguments[0] == "agent-security":
        from .agent_security_cli import main as agent_security_main

        return agent_security_main(arguments[1:])
    parser = argparse.ArgumentParser(prog="potbot syn", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("embers", help="validate, store and resolve scoped inherited Ember contracts")
    sub.add_parser("agent-security", help="publish typed Agent, Credential and locking Cog views")
    catalog = sub.add_parser(
        "catalog", help="inspect bundled native classes and protected data aliases offline"
    )
    catalog.add_argument("--type-ref", help="one exact syn://type URL or registered dt: alias")
    catalog.add_argument("--summary", action="store_true", help="omit individual type definitions")
    catalog.add_argument(
        "--snapshot-id", type=UUID, help="inspect an imported catalog instead of the bundled seed"
    )
    catalog.add_argument("--workspace-id", type=UUID)
    catalog.add_argument("--principal-id", type=UUID)
    catalog.add_argument("--schema", default="potbot")
    catalog.add_argument("--artifact-root", type=Path, default=Path("artifacts/objects"))
    hierarchy = sub.add_parser("hierarchy", help="inspect the opt-in Potbot hierarchy offline")
    hierarchy.add_argument("--type-ref", help="exact syn://type or synny://type URL")
    hierarchy.add_argument("--include-subtypes", action="store_true")
    hierarchy.add_argument("--primitive-kind", choices=sorted(SYN_KINDS))
    hierarchy.add_argument("--summary", action="store_true")
    hierarchy.add_argument("--limit", type=int, default=100)
    hierarchy.add_argument("--offset", type=int, default=0)
    placement = sub.add_parser("placement", help="explain an advisory native primitive placement")
    placement.add_argument("--type-ref", required=True, help="exact type URL; no compact aliases")
    placement.add_argument("--check-kind", choices=sorted(SYN_KINDS))
    for name in ("import", "resolve", "list", "index", "search"):
        command = sub.add_parser(name)
        command.add_argument("--workspace-id", type=UUID, required=True)
        command.add_argument("--principal-id", type=UUID, required=True)
        command.add_argument("--schema", default="potbot")
        command.add_argument("--artifact-root", type=Path, default=Path("artifacts/objects"))
        if name == "import":
            command.add_argument("--namespace", required=True)
            command.add_argument("--repository-id", type=UUID, required=True)
            command.add_argument("--file", type=Path, required=True)
        else:
            command.add_argument("--snapshot-id", type=UUID, required=True)
        if name in {"resolve", "index"}:
            command.add_argument("--ref", required=True)
        if name in {"list", "search"}:
            command.add_argument("--type-ref", help="exact source-asserted semantic type")
            command.add_argument(
                "--data-type", help="checked record representation, as dt: ID or SYN alias"
            )
            command.add_argument("--include-subtypes", action="store_true")
            command.add_argument(
                "--primitive-kind",
                choices=sorted(SYN_KINDS - {"type"}),
                help="require this stored native instance kind as well as any type filters",
            )
        if name == "list":
            command.add_argument("--page-size", type=int, default=100)
            command.add_argument("--cursor", type=UUID)
        if name in {"index", "search"}:
            command.add_argument("--episode-id", type=UUID, required=True)
            command.add_argument("--space-id", type=UUID, required=True)
            command.add_argument("--embedding-name", default=DEFAULT_EMBEDDING_NAME)
            command.add_argument("--runtime-budget-ms", type=int, default=60_000)
            command.add_argument("--model-dir", type=Path)
            command.add_argument("--max-input-tokens", type=int, default=2048)
        if name == "search":
            command.add_argument("--query", required=True)
            command.add_argument("--limit", type=int, default=5)
            command.add_argument("--record-kind", choices=("objects", "types"), default="objects")
    args = parser.parse_args(arguments)
    try:
        if args.command == "hierarchy":
            from .hierarchy import inspect_hierarchy

            output: dict[str, Any] = inspect_hierarchy(
                type_ref=args.type_ref,
                include_subtypes=args.include_subtypes,
                primitive_kind=args.primitive_kind,
                summary=args.summary,
                limit=args.limit,
                offset=args.offset,
            )
        elif args.command == "placement":
            from .hierarchy import inspect_placement

            output = inspect_placement(args.type_ref, check_kind=args.check_kind)
        elif args.command == "catalog" and args.snapshot_id is None:
            if args.workspace_id is not None or args.principal_id is not None:
                raise InvalidInputError("select --snapshot-id when inspecting a workspace catalog")
            types = tuple(sorted((*builtin_types(), *extension_types()), key=lambda row: row.ref))
            if args.type_ref:
                ref = (
                    data_type_ref(args.type_ref)
                    if args.type_ref.startswith("dt:")
                    else parse_type_ref(args.type_ref)
                )
                types = tuple(row for row in types if row.ref.slug == ref.slug)
                if not types:
                    raise InvalidInputError("type is absent from the bundled catalog")
            output = {
                "schema_version": 1,
                "native_type_count": len(builtin_types()),
                "data_type_alias_count": len(extension_types()),
                "catalog_hash": catalog_fingerprint(),
                "provenance": catalog_provenance(),
                "selected_count": len(types),
            }
            if not args.summary:
                output["types"] = [row.to_dict() for row in types]
        else:
            from potbot.credentials import resolve_postgres_dsn
            from potbot.domain.auth import WorkspaceContext
            from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
            from potbot.persistence.database import Database

            from .service import SynService

            if (
                args.command == "search"
                and args.primitive_kind is not None
                and args.record_kind == "types"
            ):
                raise InvalidInputError("primitive-kind applies only to object searches")
            if args.workspace_id is None or args.principal_id is None:
                raise InvalidInputError("snapshot operations require workspace-id and principal-id")
            dsn = resolve_postgres_dsn()
            database = Database(dsn, schema=args.schema)
            context = WorkspaceContext(args.workspace_id, args.principal_id)
            artifacts = ArtifactStore(database, LocalArtifactBackend(args.artifact_root))
            service = SynService(database, artifacts)
            if args.command == "catalog":
                output = service.catalog(
                    context, args.snapshot_id, type_ref=args.type_ref, summary=args.summary
                )
            elif args.command == "import":
                with args.file.open("rb") as source:
                    raw = source.read(MAX_SYN_SNAPSHOT_BYTES + 1)
                if len(raw) > MAX_SYN_SNAPSHOT_BYTES:
                    raise InvalidInputError("SYN input exceeds the 64 MiB snapshot limit")
                snapshot = SynSnapshot.from_json(raw.decode("utf-8"))
                output = service.import_snapshot(
                    context, args.repository_id, args.namespace, snapshot
                )
            elif args.command == "resolve":
                output = service.resolve(context, args.snapshot_id, args.ref)
            elif args.command == "list":
                output = service.list_objects(
                    context,
                    args.snapshot_id,
                    type_ref=args.type_ref,
                    primary_data_type=args.data_type,
                    include_subtypes=args.include_subtypes,
                    primitive_kind=args.primitive_kind,
                    page_size=args.page_size,
                    cursor=args.cursor,
                )
            else:
                from potbot.dependencies import MODEL_DIRECTORY, ConsoleProgress, project_root
                from potbot.memory.embedding_profile import EmbeddingProfile
                from potbot.memory.embeddings import EmbeddingService
                from potbot.memory.local_encoder import QwenLocalEncoder

                root = project_root()
                if args.model_dir is None:
                    args.model_dir = root / "models" / MODEL_DIRECTORY
                profile = replace(EmbeddingProfile(), max_input_tokens=args.max_input_tokens)
                ensure_standard_model(root, model_dir=args.model_dir, progress=ConsoleProgress())
                embeddings = EmbeddingService(
                    database, artifacts, QwenLocalEncoder(args.model_dir, profile)
                )
                if args.command == "index":
                    output = service.index(
                        context,
                        args.snapshot_id,
                        args.ref,
                        embeddings,
                        args.episode_id,
                        args.space_id,
                        embedding_name=args.embedding_name,
                        runtime_budget_ms=args.runtime_budget_ms,
                    )
                else:
                    output = service.search(
                        context,
                        args.snapshot_id,
                        args.query,
                        embeddings,
                        args.episode_id,
                        args.space_id,
                        embedding_name=args.embedding_name,
                        runtime_budget_ms=args.runtime_budget_ms,
                        limit=args.limit,
                        type_ref=args.type_ref,
                        primary_data_type=args.data_type,
                        include_subtypes=args.include_subtypes,
                        record_kind=args.record_kind,
                        primitive_kind=args.primitive_kind,
                    )
        print(json.dumps(output, sort_keys=True, default=str))
        return 0
    except DomainError as error:
        print(json.dumps({"error": error.code.value, "message": error.message}), file=sys.stderr)
        return 2
    except (OSError, UnicodeError):
        print(
            json.dumps(
                {"error": "source_unavailable", "message": "SYN input or artifact is unavailable"}
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
