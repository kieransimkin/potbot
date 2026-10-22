"""Publish Agent security views and sign through their owner-scoped locking Cogs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from potbot.domain.errors import DomainError, InvalidInputError
from potbot.domain.syn import type_identity
from potbot.security.public import MAX_PUBLIC_DOCUMENT_BYTES

from .agent_security import (
    SECURITY_PROFILE,
    build_agent_security_view,
    locking_cog_configuration,
    security_type_snapshot,
    security_types,
)
from .catalog import catalog_fingerprint, merge_types
from .registry import strict_json


def _public_file(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise InvalidInputError("Agent security document must be a regular file")
        raw = stream.read(MAX_PUBLIC_DOCUMENT_BYTES + 1)
    if len(raw) > MAX_PUBLIC_DOCUMENT_BYTES:
        raise InvalidInputError("Agent security document exceeds the byte limit")
    document = strict_json(raw)
    if not isinstance(document, dict):
        raise InvalidInputError("Agent security document must be an object")
    return document


def _payload_digest(path: Path) -> str:
    """Hash exactly one stable regular-file read without keeping the payload in memory."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise InvalidInputError("payload must be a regular file")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise InvalidInputError("payload changed while computing its signature digest")
    return digest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="potbot syn agent-security", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    catalog = commands.add_parser("catalog", help="show the opt-in Agent security Type registry")
    catalog.add_argument("--summary", action="store_true")
    for name in ("export", "register", "sign"):
        command = commands.add_parser(name)
        command.add_argument("--workspace-id", type=UUID, required=True)
        if name != "sign":
            command.add_argument("--identity-file", type=Path, required=True)
            command.add_argument(
                "--expect-signer",
                required=True,
                help="independently checked base key SHA-256 fingerprint",
            )
            command.add_argument("--policy-file", type=Path, required=True)
            command.add_argument("--namespace", required=True)
            command.add_argument("--group", action="store_true")
        if name == "export":
            command.add_argument(
                "--snapshot-id",
                type=UUID,
                required=True,
                help="explicit scope for the offline contract view",
            )
            command.add_argument("--projection", choices=("public", "owner"), default="public")
        else:
            command.add_argument("--principal-id", type=UUID, required=True)
            command.add_argument("--schema", default="potbot")
        if name == "register":
            command.add_argument("--repository-id", type=UUID, required=True)
            command.add_argument("--artifact-root", type=Path, default=Path("artifacts/objects"))
        if name == "sign":
            command.add_argument("--bundle-id", type=UUID, required=True)
            command.add_argument("--cog-ref", required=True)
            command.add_argument("--vault", type=Path, required=True)
            command.add_argument("--broker", type=Path)
            command.add_argument("--payload", type=Path, required=True)
            command.add_argument("--payload-type-ref", required=True)
            command.add_argument("--audience", required=True)
            command.add_argument("--domain", required=True)
            command.add_argument("--request-id", type=UUID)
            command.add_argument("--validity-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    try:
        if args.command == "catalog":
            snapshot = security_type_snapshot()
            types = merge_types(snapshot)
            output: dict[str, Any] = {
                "schema_version": 1,
                "profile": SECURITY_PROFILE,
                "security_type_count": len(security_types()),
                "effective_type_count": len(types),
                "catalog_hash": catalog_fingerprint(types),
            }
            if not args.summary:
                output["snapshot"] = snapshot.to_dict()
        elif args.command == "export":
            view = build_agent_security_view(
                _public_file(args.identity_file),
                expected_signer_hash=args.expect_signer,
                workspace_id=args.workspace_id,
                namespace=args.namespace,
                snapshot_id=args.snapshot_id,
                policy=_public_file(args.policy_file),
                group=args.group,
            )
            bundle = view.bundle
            if args.projection == "public":
                bundle = bundle.public_projection(merge_types(view.snapshot))
            output = {
                "schema_version": 1,
                "profile": SECURITY_PROFILE,
                "snapshot": view.snapshot.to_dict(),
                "ember_bundle": bundle.to_dict(),
            }
        else:
            from potbot.credentials import resolve_postgres_dsn
            from potbot.domain.auth import WorkspaceContext
            from potbot.persistence.database import Database

            from .ember_service import EmberContractService

            database = Database(resolve_postgres_dsn(), schema=args.schema)
            context = WorkspaceContext(args.workspace_id, args.principal_id)
            contracts = EmberContractService(database)
            if args.command == "register":
                from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend

                from .service import SynService

                view = build_agent_security_view(
                    _public_file(args.identity_file),
                    expected_signer_hash=args.expect_signer,
                    workspace_id=args.workspace_id,
                    namespace=args.namespace,
                    snapshot_id=uuid4(),
                    policy=_public_file(args.policy_file),
                    group=args.group,
                )
                artifacts = ArtifactStore(database, LocalArtifactBackend(args.artifact_root))
                imported = SynService(database, artifacts).import_snapshot(
                    context, args.repository_id, args.namespace, view.snapshot
                )
                snapshot_id = UUID(imported["scope"]["snapshot_id"])
                stored = contracts.import_bundle(
                    context, snapshot_id, view.with_snapshot_id(snapshot_id)
                )
                output = {
                    **stored,
                    "profile": SECURITY_PROFILE,
                    "agent_ref": view.agent_ref,
                    "credential_ref": view.credential_ref,
                    "locking_cog_ref": view.locking_cog_ref,
                    "signer_hash": view.signer_hash,
                    "native_policy_changed": False,
                }
            else:
                from potbot.security.broker import NativeIdentityBroker

                bundle, types = contracts.read_bundle(context, args.bundle_id, projection="owner")
                config = locking_cog_configuration(
                    bundle, types, args.cog_ref, require_current_implementation=True
                )
                policy = config["policy"]
                allowed_types = {type_identity(ref) for ref in policy["allowed_payload_types"]}
                if (
                    args.audience not in policy["allowed_audiences"]
                    or args.domain not in policy["allowed_domains"]
                    or type_identity(args.payload_type_ref) not in allowed_types
                    or not 1 <= args.validity_seconds <= 300
                ):
                    raise InvalidInputError("request is outside the locking Cog signing policy")
                digest = _payload_digest(args.payload)
                request = {
                    "workspace_id": str(context.workspace_id),
                    "audience": args.audience,
                    "domain": args.domain,
                    "payload_type_ref": args.payload_type_ref,
                    "payload_sha256": digest,
                    "request_id": str(args.request_id or uuid4()),
                    "validity_seconds": args.validity_seconds,
                }
                output = NativeIdentityBroker(args.broker).sign(
                    args.vault,
                    request,
                    expected_signer_hash=config["signer_hash"],
                    known_type_refs=frozenset(str(t.ref) for t in types),
                )
        print(json.dumps(output, sort_keys=True))
        return 0
    except (DomainError, OSError, ValueError) as error:
        # Paths, source data, operation handles and native output are not copied into errors.
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "error": type(error).__name__,
                    "message": "Agent security operation failed validation or access checks",
                }
            ),
            file=sys.stderr,
        )
        return 2
