"""Explicit installation commands, separate from authenticated task operations."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
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
    workspace = commands.add_parser("workspace-create", help="create one workspace")
    workspace.add_argument("--name", required=True)
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
    args = parser.parse_args(argv)
    try:
        _identifier(args.schema)
        result: dict[str, Any]
        if args.command == "repository-create":
            result = RepositoryStore(Database(_environment("POTBOT_DSN"), args.schema)).create(
                WorkspaceContext(args.workspace_id, args.principal_id),
                name=args.name,
                source_uri=args.source_uri,
            )
        else:
            dsn = _environment("POTBOT_ADMIN_DSN")
            if args.command == "migrate":
                result = {"versions": list(migrate(dsn, args.schema))}
            elif args.command == "workspace-create":
                result = {"workspace_id": str(provision_workspace(dsn, args.schema, args.name))}
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
