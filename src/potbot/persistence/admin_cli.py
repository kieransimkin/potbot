"""Explicit installation commands, separate from authenticated task operations."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.credentials import resolve_postgres_dsn
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import DomainError, InvalidInputError
from potbot.persistence.database import (
    Database,
    RepositoryStore,
    _identifier,
    admin_connection,
    migrate,
    provision_principal,
    provision_workspace,
)


def _environment(name: str) -> str:
    if name == "POTBOT_DSN":
        return resolve_postgres_dsn()
    if name == "POTBOT_ADMIN_DSN":
        return resolve_postgres_dsn("admin")
    value = os.environ.get(name)
    if not value:
        raise InvalidInputError(f"Set the explicit {name} environment variable.")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="potbot admin", description=__doc__)
    parser.add_argument("--schema", default="potbot")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="apply and verify application migrations")
    workspace = commands.add_parser("workspace-create", help="create one sandboxed workspace")
    workspace.add_argument("--name", required=True)
    workspace.add_argument(
        "--sandbox-root",
        required=True,
        help="absolute host folder dedicated to this workspace; created when absent",
    )
    sandbox_set = commands.add_parser(
        "workspace-sandbox-set",
        help="bind one legacy workspace to an immutable sandbox root",
    )
    sandbox_set.add_argument("--workspace-id", required=True, type=UUID)
    sandbox_set.add_argument("--sandbox-root", required=True)
    login = commands.add_parser("login-create", help="create a restricted PostgreSQL login")
    login.add_argument("--role", required=True)
    login.add_argument("--password-env", default="POTBOT_NEW_ROLE_PASSWORD")
    principal = commands.add_parser("principal-bind", help="bind a restricted login to a workspace")
    principal.add_argument("--role", required=True)
    principal.add_argument("--workspace-id", required=True, type=UUID)
    repository = commands.add_parser(
        "repository-create", help="register a repository as an app principal"
    )
    repository.add_argument("--workspace-id", required=True, type=UUID)
    repository.add_argument("--principal-id", required=True, type=UUID)
    repository.add_argument("--name", required=True)
    repository.add_argument("--source-uri", required=True)
    inventory = commands.add_parser("cog-installations", help="show grant pins for hosted Cogs")
    inventory.add_argument("--profile", required=True, type=Path)
    inventory.add_argument("--operator-manifest", required=True, type=Path)
    for name, help_text in (
        ("cog-grants-show", "read the current policy for an installed Syn Agent"),
        ("cog-grants-publish", "replace the grants for an installed Syn Agent"),
    ):
        grants = commands.add_parser(name, help=help_text)
        grants.add_argument("--workspace-id", required=True, type=UUID)
        grants.add_argument("--principal-id", required=True, type=UUID)
        grants.add_argument("--agent-id", required=True, type=UUID)
        if name == "cog-grants-publish":
            grants.add_argument("--expected-generation", required=True, type=int)
            grants.add_argument("--node-sha256", required=True)
            grants.add_argument("--enrollment-sha256")
            grants.add_argument("--revision-sha256", required=True)
            grants.add_argument("--grants-file", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        _identifier(args.schema)
        result: dict[str, Any]
        if args.command == "cog-installations":
            from potbot.controller.cog_capability_policy import installation_hash
            from potbot.controller.native_cog_dispatch import installed_cog_operations, read_profile
            from potbot.tools.operator_cli import parse_manifest

            profile, revision = read_profile(args.profile)
            paths = parse_manifest(args.operator_manifest)
            operations = installed_cog_operations(profile, revision, paths)
            result = {
                "revision_sha256": revision,
                "workspace_id": str(paths.workspace_id),
                "operations": [
                    {
                        "operation_ref": op.binding.operation_ref,
                        "installation_sha256": installation_hash(op),
                    }
                    for op in operations
                ],
            }
        elif args.command == "repository-create":
            result = RepositoryStore(Database(_environment("POTBOT_DSN"), args.schema)).create(
                WorkspaceContext(args.workspace_id, args.principal_id),
                name=args.name,
                source_uri=args.source_uri,
            )
        else:
            dsn = _environment("POTBOT_ADMIN_DSN")
            if args.command == "migrate":
                result = {"versions": list(migrate(dsn, args.schema))}
            elif args.command in {"cog-grants-show", "cog-grants-publish"}:
                from potbot.controller.cog_capability_policy import CogCapabilityPolicy
                from potbot.controller.cog_grant_store import (
                    current_cog_policy,
                    parse_grants,
                    publish_cog_policy,
                )

                actor = WorkspaceContext(args.workspace_id, args.principal_id)
                if args.command == "cog-grants-show":
                    with admin_connection(dsn, args.schema) as connection:
                        published = current_cog_policy(connection, actor, args.agent_id)
                else:
                    try:
                        with args.grants_file.open("rb") as grant_file:
                            raw = grant_file.read(65537)
                        if len(raw) > 65536:
                            raise ValueError
                        grants_value = json.loads(raw)
                    except (OSError, ValueError, RecursionError):
                        raise InvalidInputError(
                            "Supply a readable JSON grant file up to 64 KiB"
                        ) from None
                    policy = CogCapabilityPolicy(
                        actor,
                        args.agent_id,
                        args.node_sha256,
                        args.revision_sha256,
                        parse_grants(grants_value),
                        enrollment_sha256=args.enrollment_sha256,
                    )
                    published = publish_cog_policy(
                        dsn, args.schema, policy, args.expected_generation
                    )
                result = (
                    published.to_dict()
                    if published is not None
                    else {"generation": 0, "grants": []}
                )
            elif args.command == "workspace-create":
                identity = provision_workspace(
                    dsn, args.schema, args.name, sandbox_root=args.sandbox_root
                )
                result = {"workspace_id": str(identity), "sandbox_root": args.sandbox_root}
            elif args.command == "workspace-sandbox-set":
                from potbot.persistence.database import configure_workspace_sandbox

                result = {
                    "workspace_id": str(args.workspace_id),
                    "sandbox": configure_workspace_sandbox(
                        dsn, args.schema, args.workspace_id, args.sandbox_root
                    ),
                }
            elif args.command == "login-create":
                from psycopg import sql

                _identifier(args.role)
                password = _environment(args.password_env)
                if len(password) < 16:
                    raise InvalidInputError(
                        "The application login password needs at least 16 characters."
                    )
                with admin_connection(dsn, args.schema) as connection:
                    connection.execute(
                        sql.SQL(
                            "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
                            "NOREPLICATION NOBYPASSRLS NOINHERIT"
                        ).format(sql.Identifier(args.role), sql.Literal(password))
                    )
                result = {"database_role": args.role}
            else:
                context = provision_principal(dsn, args.schema, args.role, args.workspace_id)
                result = {
                    "workspace_id": str(context.workspace_id),
                    "principal_id": str(context.principal_id),
                }
        print(
            json.dumps(
                {"schema_version": 1, "status": "succeeded", "result": result},
                default=str,
                sort_keys=True,
            )
        )
        return 0
    except Exception as error:
        # Administrative database diagnostics may contain credentials, object
        # names or unrelated records. Public output never includes raw SQL errors.
        code = error.code.value if isinstance(error, DomainError) else "internal_error"
        message = (
            error.message
            if isinstance(error, DomainError)
            else "The administrative operation failed."
        )
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "failed",
                    "error": {
                        "code": code,
                        "message": message,
                        "retryable": False,
                    },
                },
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
