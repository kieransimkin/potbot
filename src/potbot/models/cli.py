"""Offline model catalogue, response contracts and pinned host preflight."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from potbot.domain.coding import CODING_RESPONSE_SCHEMA
from potbot.domain.errors import DomainError, InvalidInputError
from potbot.domain.model_proposals import ACTION_RESPONSE_SCHEMA
from potbot.models.envelopes import MAX_MODEL_OUTPUT_BYTES, parse_model_response, response_contract
from potbot.models.preflight import preflight_document
from potbot.models.profiles import default_model_profiles


def _read(path: Path, maximum: int) -> bytes:
    try:
        with path.open("rb") as stream:
            content = stream.read(maximum + 1)
    except OSError:
        raise InvalidInputError("The requested input file could not be read.") from None
    if len(content) > maximum:
        raise InvalidInputError("The requested input file exceeds its byte limit.")
    return content


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="potbot models", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("catalog", help="show the five default advisory profiles")
    preflight = commands.add_parser("preflight", help="verify a local pinned host without loading")
    preflight.add_argument("--profile", type=Path, required=True)
    preflight.add_argument("--project-root", type=Path, default=Path.cwd())
    schema = commands.add_parser("schema", help="print a versioned model response JSON Schema")
    schema.add_argument(
        "--schema", choices=(ACTION_RESPONSE_SCHEMA, CODING_RESPONSE_SCHEMA), required=True
    )
    validate = commands.add_parser("validate-response", help="validate complete saved model output")
    validate.add_argument(
        "--schema", choices=(ACTION_RESPONSE_SCHEMA, CODING_RESPONSE_SCHEMA), required=True
    )
    validate.add_argument("--input", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "catalog":
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "profiles": [profile.to_dict() for profile in default_model_profiles()],
                        "live_inference_available": False,
                        "qualified_deployments": 0,
                    },
                    sort_keys=True,
                )
            )
        elif args.command == "preflight":
            report = preflight_document(
                _read(args.profile, 1024 * 1024), project_root=args.project_root
            ).to_dict()
            print(json.dumps(report, sort_keys=True))
            return 0 if report["status"] == "ready_for_load_check" else 2
        elif args.command == "schema":
            print(json.dumps(response_contract(args.schema), sort_keys=True, indent=2))
        else:
            parse_model_response(
                _read(args.input, MAX_MODEL_OUTPUT_BYTES), response_schema=args.schema
            )
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "response_schema": args.schema,
                        "structurally_valid": True,
                        "semantic_admission": "required",
                        "executable": False,
                    },
                    sort_keys=True,
                )
            )
    except DomainError as error:
        print(json.dumps({"error": error.to_dict()}, sort_keys=True), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
