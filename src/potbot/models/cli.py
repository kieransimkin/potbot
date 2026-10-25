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
    commands.add_parser("catalog", help="show default advisory profiles")
    specialists = commands.add_parser("specialists", help="advisory small specialist models and quantization choices")
    specialists.add_argument("--role", default=None)
    specialists.add_argument("--kind", choices=("generator", "reranker", "encoder", "embedding"))
    specialists.add_argument("--model", default=None)
    specialists.add_argument("--quantization", default="Q4_K_M")
    specialists.add_argument("--vram-mib", type=int)
    specialists.add_argument("--disk-free-mib", type=int)
    installation = commands.add_parser("install-artifact", help="install one exact operator-pinned quantized file")
    installation.add_argument("--manifest", required=True, type=Path)
    installation.add_argument("--models-root", required=True, type=Path)
    installation.add_argument("--confirm-download", action="store_true")
    installation.add_argument("--accept-model-terms", action="store_true")
    installation.add_argument("--allow-third-party-quantization", action="store_true")
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
        elif args.command == "specialists":
            from potbot.models.specialist_catalog import (
                quantization_choices, quantization_preview, specialist_models,
                shortlist_specialists,
            )

            rows = []
            for item in specialist_models(kind=args.kind, role=args.role):
                if args.model and item.model_id != args.model:
                    continue
                choices = quantization_choices(item.model_id)
                quantization = args.quantization if args.quantization in choices else choices[0]
                rows.append({
                    "model_id": item.model_id, "architecture": item.architecture,
                    "kind": item.kind, "total_parameters_b": item.total_parameters_b,
                    "active_parameters_b": item.active_parameters_b,
                    "roles": list(item.roles), "source_url": item.source_url,
                    "gated": item.gated, "notes": item.notes,
                    "quantization_choices": list(choices),
                    "selected_preview": quantization_preview(item.model_id, quantization),
                })
            if args.model and not rows:
                raise InvalidInputError("unknown or filtered specialist model")
            report = {"schema_version": 1, "advisory": True,
                      "all_quantizations_require_artifact_discovery": True,
                      "models": rows}
            if args.vram_mib is not None or args.disk_free_mib is not None:
                if args.vram_mib is None or args.disk_free_mib is None or args.role is None:
                    raise InvalidInputError("hardware shortlist requires --role, --vram-mib and --disk-free-mib")
                report["advisory_hardware_shortlist"] = list(shortlist_specialists(
                    role=args.role, quantization=args.quantization,
                    vram_mib=args.vram_mib, disk_free_mib=args.disk_free_mib,
                ))
            print(json.dumps(report, sort_keys=True))
        elif args.command == "install-artifact":
            from dataclasses import fields

            from potbot.models.specialist_catalog import (
                QuantizedArtifact, install_exact_artifact, specialist_model,
            )
            from potbot.tools.temporary import create_directory

            if not args.confirm_download:
                raise InvalidInputError("install-artifact requires explicit --confirm-download")
            source = json.loads(_read(args.manifest, 4096).decode("utf-8"))
            expected = {item.name for item in fields(QuantizedArtifact)} - {"artifact_repository"}
            if not isinstance(source, dict) or not expected <= set(source) or set(source) - expected - {"artifact_repository"}:
                raise InvalidInputError("install manifest must contain exactly the artifact fields")
            artifact = QuantizedArtifact(**source)
            if (artifact.artifact_repository not in {None, artifact.model_id}
                    and not args.allow_third_party_quantization):
                raise InvalidInputError("third-party artifacts require explicit source opt-in")
            if specialist_model(artifact.model_id).gated and not args.accept_model_terms:
                raise InvalidInputError("gated model requires explicit acceptance of upstream terms")
            try:
                from huggingface_hub import hf_hub_download
            except ImportError:
                raise InvalidInputError("install huggingface_hub to download model artifacts") from None
            root = create_directory(args.models_root.resolve())
            def fetch(selected: QuantizedArtifact, directory: Path) -> Path:
                return Path(hf_hub_download(
                    repo_id=selected.artifact_repository or selected.model_id,
                    filename=selected.filename,
                    revision=selected.revision, local_dir=str(directory),
                ))
            output = install_exact_artifact(artifact, models_root=root, fetch=fetch)
            print(json.dumps({"path": str(output), "sha256": artifact.sha256,
                              "bytes": artifact.size_bytes,
                              "artifact_repository": artifact.artifact_repository or artifact.model_id,
                              "verified_artifact": True,
                              "installed_model_qualified": False}, sort_keys=True))
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
