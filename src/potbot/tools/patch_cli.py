"""Run, resume and export the explicitly scripted Phase 4 acceptance workflow."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from potbot.controller.patch_pipeline import PatchPipeline
    from potbot.domain.auth import WorkspaceContext
    from potbot.persistence.database import Database


def _key(value: str) -> str:
    if not value.strip() or len(value) > 128:
        raise argparse.ArgumentTypeError("key must contain 1 to 128 nonblank characters")
    return value


def _schema(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value):
        raise argparse.ArgumentTypeError("schema must be a bounded lowercase SQL identifier")
    return value


def _demo_pipeline(
    database: Database,
    context: WorkspaceContext,
    pipeline: PatchPipeline,
    fixtures: Path,
    preexisting: bool,
    key: str,
) -> dict[str, Any]:
    """Bind demo options before replay, including recovery of an inner commit."""
    from urllib.parse import urlsplit
    from urllib.request import url2pathname

    from potbot.domain.errors import RevisionConflictError, StateMismatchError
    from potbot.domain.serialization import content_hash
    from potbot.evaluation.patch_fixture import fixture_contract, fixture_patch, fixture_profile
    from potbot.evaluation.repository_fixture import FixtureVariant, create_repository_fixture
    from potbot.persistence.database import RepositoryStore, idempotent_command

    parent = Path(os.path.abspath(fixtures))
    profile = fixture_profile(preexisting=preexisting)
    variant = FixtureVariant.PRE_EXISTING_FAILURE if preexisting else FixtureVariant.BASELINE
    request = {
        "schema_version": 1,
        "fixtures": str(parent),
        "variant": variant.value,
        "profile": profile.to_dict(),
    }
    pipeline_key = "pipeline-create:" + content_hash(key)
    demo_key = "patch-demo:" + content_hash(key)
    with database.transaction(context) as connection:

        def create() -> dict[str, Any]:
            prior = connection.execute(
                "SELECT result FROM command_results WHERE workspace_id=%s "
                "AND operation='create_patch_pipeline' AND idempotency_key=%s",
                (context.workspace_id, pipeline_key),
            ).fetchone()
            if prior is not None:
                # The inner pipeline can commit before this wrapper's result is
                # acknowledged. Its immutable profile and registered source must
                # still describe this exact demo request before it can be reused.
                bound = connection.execute(
                    "SELECT r.source_uri,p.content,p.content_digest FROM patch_pipelines d "
                    "JOIN verification_profiles p ON p.workspace_id=d.workspace_id AND p.id=d.profile_id "
                    "JOIN repository_states s ON s.workspace_id=d.workspace_id AND s.id=d.base_state_id "
                    "JOIN repositories r ON r.workspace_id=s.workspace_id AND r.id=s.repository_id "
                    "WHERE d.workspace_id=%s AND d.id=%s",
                    (context.workspace_id, UUID(prior["result"]["id"])),
                ).fetchone()
                if bound is None or content_hash(bound["content"]) != bound["content_digest"]:
                    raise StateMismatchError("The prior demo pipeline cannot be verified.")
                expected = profile.to_dict()
                actual = {
                    name: bound["content"].get(name)
                    for name in ("schema_version", "name", "version")
                }
                fields = set(expected["checks"][0])
                actual["checks"] = [
                    {name: check.get(name) for name in fields}
                    for check in bound["content"]["checks"]
                ]
                source = urlsplit(bound["source_uri"])
                source_path = Path(
                    url2pathname(("//" + source.netloc if source.netloc else "") + source.path)
                )
                if actual != expected or source.scheme != "file" or source_path.parent != parent:
                    raise RevisionConflictError(
                        "This demo key was used for different fixture options."
                    )
                return dict(prior["result"])
            fixture = create_repository_fixture(parent, variant)
            repository = RepositoryStore(database).create(
                context,
                name="Phase 4 scripted fixture",
                source_uri=fixture.path.as_uri(),
            )
            assert fixture.baseline_commit is not None
            return pipeline.create(
                context,
                repository["id"],
                fixture.path,
                fixture_contract(fixture.baseline_commit),
                profile,
                fixture_patch(fixture.path),
                key,
            )

        return idempotent_command(
            connection,
            context,
            "create_patch_demo",
            demo_key,
            request,
            create,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="potbot patch", description=__doc__)
    parser.add_argument("--dev-auth", action="store_true", required=True)
    parser.add_argument("--workspace", type=UUID, required=True)
    parser.add_argument("--principal", type=UUID, required=True)
    parser.add_argument("--schema", type=_schema, default="potbot")
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--receipts", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser(
        "demo", help="submit a disposable fixture and produce a verified patch"
    )
    demo.add_argument("--fixtures", type=Path, required=True)
    demo.add_argument("--key", type=_key, required=True)
    demo.add_argument("--preexisting-failure", action="store_true")
    demo.add_argument(
        "--stop-after",
        choices=("baseline", "application", "verification", "export"),
        default="export",
    )
    demo.add_argument("--output", type=Path)
    resume = commands.add_parser(
        "resume", help="resume a durable pipeline without repeating completed work"
    )
    resume.add_argument("pipeline_id", type=UUID)
    resume.add_argument(
        "--stop-after",
        choices=("baseline", "application", "verification", "export"),
        default="export",
    )
    resume.add_argument("--output", type=Path)
    inspect = commands.add_parser("inspect", help="inspect a pipeline and its task")
    inspect.add_argument("pipeline_id", type=UUID)
    export = commands.add_parser(
        "export", help="copy retained immutable patch and manifest artifacts"
    )
    export.add_argument("export_id", type=UUID)
    export.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    from potbot.api.wire import error_response
    from potbot.controller.patch_pipeline import PatchPipeline
    from potbot.credentials import resolve_postgres_dsn
    from potbot.domain.auth import WorkspaceContext
    from potbot.domain.execution import CapabilityContext
    from potbot.evaluation.patch_fixture import fixture_tools
    from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
    from potbot.persistence.database import Database
    from potbot.tools.gateway import ToolGateway
    from potbot.tools.patch_application import patch_runtime_roots
    from potbot.tools.receipts import RunReceiptStore
    from potbot.tools.repository import CandidateWorkspaceManager

    try:
        database = Database(resolve_postgres_dsn(), args.schema)
        context = WorkspaceContext(args.workspace, args.principal)
        artifacts = ArtifactStore(database, LocalArtifactBackend(args.artifacts))
        candidates = CandidateWorkspaceManager(database, artifacts, args.candidates)
        registry = fixture_tools()
        gateway = ToolGateway(
            database,
            artifacts,
            candidates,
            registry,
            CapabilityContext(registry.inventory()),
            RunReceiptStore(args.receipts),
            read_only_roots=patch_runtime_roots(),
        )
        pipeline = PatchPipeline(database, artifacts, candidates, gateway)
        if args.command == "demo":
            created = _demo_pipeline(
                database, context, pipeline, args.fixtures, args.preexisting_failure, args.key
            )
            identity = UUID(created["id"])
            print(f"Pipeline {identity}", file=sys.stderr, flush=True)
            result = pipeline.resume(
                context,
                identity,
                stop_after=args.stop_after,
                progress=lambda stage: print("Phase 4: " + stage, file=sys.stderr, flush=True),
            )
        elif args.command == "resume":
            result = pipeline.resume(
                context,
                args.pipeline_id,
                stop_after=args.stop_after,
                progress=lambda stage: print("Phase 4: " + stage, file=sys.stderr, flush=True),
            )
        elif args.command == "inspect":
            result = pipeline.inspect(context, args.pipeline_id)
        else:
            result = pipeline.exports.write_bundle(context, args.export_id, args.output)
        if args.command in {"demo", "resume"} and args.output is not None and result.get("export"):
            result["bundle"] = pipeline.exports.write_bundle(
                context, UUID(result["export"]["id"]), args.output
            )
        print(json.dumps({"schema_version": 1, "data": result}, indent=2))
        incomplete = args.command in {"demo", "resume"} and result.get("outcome") in {
            "partial",
            "blocked",
        }
        return 1 if incomplete else 0
    except Exception as error:
        print(json.dumps(error_response(error).body, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
