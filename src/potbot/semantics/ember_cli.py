"""Check and resolve immutable Ember contracts against a selected SYN snapshot."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.domain.errors import DomainError, InvalidInputError
from potbot.domain.syn import MAX_SYN_SNAPSHOT_BYTES, SynSnapshot

from .catalog import merge_types
from .embers import MAX_BUNDLE_BYTES, EmberContractBundle, EmberContractResolver


def _read(path: Path, limit: int) -> str:
    with path.open("rb") as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise InvalidInputError("Ember contract input exceeds its byte limit")
    return raw.decode("utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="potbot syn embers", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser(
        "validate", help="validate locally using an explicit SYN snapshot file; no database needed"
    )
    validate.add_argument("--file", type=Path, required=True, help="Ember bundle JSON")
    validate.add_argument("--catalog-file", type=Path, required=True, help="SYN snapshot JSON")
    validate.add_argument("--projection", choices=("public", "owner"), default="public")
    for name in ("import", "resolve", "export", "list"):
        command = commands.add_parser(name)
        command.add_argument("--workspace-id", type=UUID, required=True)
        command.add_argument("--principal-id", type=UUID, required=True)
        command.add_argument("--schema", default="potbot")
        if name == "import":
            command.add_argument("--snapshot-id", type=UUID, required=True)
            command.add_argument("--file", type=Path, required=True)
        else:
            command.add_argument("--bundle-id", type=UUID, required=True)
            command.add_argument(
                "--projection",
                choices=("public", "owner"),
                default="public",
                help="owner explicitly requests private bindings and requires the owning login",
            )
        if name == "resolve":
            command.add_argument("--ref", required=True, help="exact subject object URL")
        if name == "list":
            command.add_argument("--type-ref", required=True, help="exact Ember Type URL")
            command.add_argument("--include-subtypes", action="store_true")
            command.add_argument("--limit", type=int, default=100)
            command.add_argument("--offset", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        output: dict[str, Any]
        if args.command == "validate":
            bundle = EmberContractBundle.from_json(_read(args.file, MAX_BUNDLE_BYTES))
            snapshot = SynSnapshot.from_json(_read(args.catalog_file, MAX_SYN_SNAPSHOT_BYTES))
            types = merge_types(snapshot)
            resolver = EmberContractResolver(types, bundle, expected_scope=bundle.scope)
            resolver.validate_subject_types(
                {str(obj.ref): str(obj.type_ref) for obj in snapshot.objects}
            )
            # Owner input must be valid in full, even when the validation receipt
            # uses a public digest. Offline validation checks the supplied scope
            # and catalog assertion; it cannot authenticate database membership.
            resolver.validate_all(projection=bundle.projection)
            if args.projection == "owner":
                resolver.validate_all(projection="owner")
                selected = bundle
            else:
                selected = resolver.public_projection()
            output = {
                "schema_version": 1,
                "valid": True,
                "projection": args.projection,
                "conformance": "complete" if args.projection == "owner" else "public_only",
                "scope": bundle.scope.to_dict(),
                "scope_source": "offline_bundle_claim",
                "bundle_hash": selected.fingerprint,
                "instance_count": len(selected.instances),
            }
        else:
            from potbot.credentials import resolve_postgres_dsn
            from potbot.domain.auth import WorkspaceContext
            from potbot.persistence.database import Database

            from .ember_service import EmberContractService

            database = Database(resolve_postgres_dsn(), schema=args.schema)
            context = WorkspaceContext(args.workspace_id, args.principal_id)
            service = EmberContractService(database)
            if args.command == "import":
                bundle = EmberContractBundle.from_json(_read(args.file, MAX_BUNDLE_BYTES))
                output = service.import_bundle(context, args.snapshot_id, bundle)
            elif args.command == "resolve":
                output = service.resolve(
                    context, args.bundle_id, args.ref, projection=args.projection
                )
            elif args.command == "list":
                output = service.list_embers(
                    context,
                    args.bundle_id,
                    type_ref=args.type_ref,
                    include_subtypes=args.include_subtypes,
                    projection=args.projection,
                    limit=args.limit,
                    offset=args.offset,
                )
            else:
                bundle, _types = service.read_bundle(
                    context, args.bundle_id, projection=args.projection
                )
                output = bundle.to_dict()
        print(json.dumps(output, sort_keys=True, default=str))
        return 0
    except DomainError as error:
        print(json.dumps({"error": error.code.value, "message": error.message}), file=sys.stderr)
        return 2
    except (OSError, UnicodeError):
        print(
            json.dumps({"error": "source_unavailable", "message": "Ember input is unavailable"}),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
