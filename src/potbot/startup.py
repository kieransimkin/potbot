"""Shared startup boundary for the BAT, PowerShell and Bash launchers."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence

from potbot.credentials import credential_profile, resolve_postgres_dsn, validate_connection
from potbot.domain.errors import DomainError


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    from potbot.cli import main as cli_main

    # Commands perform credential resolution only when they actually require a
    # database. Help, version, offline catalogs and dependency management remain
    # usable without a running database or an available vault.
    if arguments:
        return cli_main(arguments)
    try:
        verified = False

        def verify_new(dsn: str) -> None:
            nonlocal verified
            validate_connection(dsn)
            verified = True

        dsn = resolve_postgres_dsn(validator=verify_new)
        if not verified:
            validate_connection(dsn)
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "startup": {
                        "status": "ready",
                        "postgres_connection": "verified",
                        "credential_profile": credential_profile(),
                        "database_migrated": False,
                        "model_downloaded": False,
                    },
                },
                sort_keys=True,
            )
        )
        print("Potbot is ready. Choose a command below; startup does not modify the database.")
        try:
            return cli_main(["--help"])
        except SystemExit as completed_help:
            return 0 if completed_help.code in {None, 0} else 1
    except DomainError as error:
        print(
            json.dumps({"schema_version": 1, "error": error.to_dict()}, sort_keys=True),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
