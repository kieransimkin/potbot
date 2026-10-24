"""Publish verification keys, sign through native custody, and verify public evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Never
from uuid import UUID, uuid4

from potbot.domain.errors import DomainError, InvalidInputError
from potbot.domain.syn import MAX_SYN_SNAPSHOT_BYTES, SynSnapshot
from potbot.semantics.catalog import merge_types
from potbot.tools.temporary import allocate_file

from .broker import NativeIdentityBroker, build_native_broker
from .public import (
    MAX_PUBLIC_DOCUMENT_BYTES,
    SIGNATURE_DOMAINS,
    SQLiteReplayStore,
    VerificationPolicy,
    accept_signature_bundle_digest,
    verify_public_identity,
    verify_signature_bundle_digest,
)


def _bounded_read(path: Path, maximum: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise InvalidInputError("public input must be a regular file")
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise InvalidInputError("public input exceeds its document size limit")
    return data


def _payload_digest(path: Path) -> str:
    # Hash the exact bytes in a single read pass without retaining large artifacts.
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


def _known_types(catalog: Path | None) -> frozenset[str] | None:
    if catalog is None:
        return None
    snapshot = SynSnapshot.from_json(_bounded_read(catalog, MAX_SYN_SNAPSHOT_BYTES).decode("utf-8"))
    return frozenset(str(record.ref) for record in merge_types(snapshot))


def _same_path(first: Path, second: Path) -> bool:
    if first.resolve() == second.resolve():
        return True
    try:
        return first.samefile(second)
    except FileNotFoundError:
        return False


def _emit(
    value: dict[str, Any], output: Path | None, *, protected_paths: Sequence[Path] = ()
) -> None:
    encoded = (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode(
        "ascii"
    )
    if output is None:
        print(encoded.decode("ascii"), end="")
        return
    if any(_same_path(output, path) for path in protected_paths):
        raise InvalidInputError("public output must not replace a vault, input, or replay ledger")
    # No recursive mkdir: native custody owns its private vault-directory ACL.
    descriptor, temporary = allocate_file(output.parent, prefix=".potbot-public-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print(
        json.dumps(
            {"written": str(output), "sha256": hashlib.sha256(encoded).hexdigest()}, sort_keys=True
        )
    )


def _scope_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--expected-signer-hash",
        required=True,
        help="independently trusted SHA-256 of the base public key",
    )
    parser.add_argument("--workspace-id", type=UUID, required=True)
    parser.add_argument("--audience", required=True)
    parser.add_argument("--domain", choices=sorted(SIGNATURE_DOMAINS), required=True)
    parser.add_argument(
        "--payload-type-ref", required=True, help="exact allowed syn://type or synny://type URL"
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        help="independently trusted effective SYN snapshot; defaults to shipped catalog",
    )


def _native_argument(parser: argparse.ArgumentParser) -> None:
    # This permits both `identity --broker X sign ...` and `identity sign --broker X ...`.
    parser.add_argument(
        "--broker",
        type=Path,
        default=argparse.SUPPRESS,
        help="explicit trusted native broker executable",
    )


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        if message.startswith("unrecognized arguments:"):
            message = (
                "unrecognized identity arguments; private inputs belong only on the native terminal"
            )
        super().error(message)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _ArgumentParser(prog="potbot identity", description=__doc__)
    parser.add_argument("--broker", type=Path, help="explicit trusted native broker executable")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser(
        "build", help="build the pinned native broker with cargo --locked --release"
    )
    build.add_argument("--source", type=Path)
    build.add_argument("--target-dir", type=Path)
    build.add_argument("--output", type=Path, help="write the public build receipt")
    preflight = sub.add_parser(
        "preflight", help="report native memory protection readiness without reading secrets"
    )
    _native_argument(preflight)
    preflight.add_argument("--output", type=Path)
    for name in ("init", "recover"):
        command = sub.add_parser(
            name, help="create a protected vault using native-only terminal input"
        )
        _native_argument(command)
        command.add_argument("--vault", type=Path, required=True)
        command.add_argument(
            "--policy", type=Path, required=True, help="public fixed signing policy JSON"
        )
        command.add_argument("--catalog", type=Path)
        command.add_argument(
            "--output", type=Path, help="publish only the public identity descriptor"
        )
        if name == "recover":
            command.add_argument("--expected-signer-hash", required=True)
    public = sub.add_parser(
        "public", help="publish a vault's raw base verification key and fingerprint"
    )
    _native_argument(public)
    public.add_argument("--vault", type=Path, required=True)
    public.add_argument(
        "--expected-signer-hash", help="verify against an independently trusted fingerprint"
    )
    public.add_argument("--output", type=Path)
    check_key = sub.add_parser(
        "verify-public", help="verify a published base key against an independent fingerprint pin"
    )
    check_key.add_argument("--identity", type=Path, required=True)
    check_key.add_argument("--expected-signer-hash", required=True)
    check_key.add_argument("--output", type=Path)
    sign = sub.add_parser(
        "sign", help="sign exact file bytes with a native hourly key and export its public bundle"
    )
    _native_argument(sign)
    sign.add_argument("--vault", type=Path, required=True)
    sign.add_argument("--payload", type=Path, required=True)
    _scope_arguments(sign)
    sign.add_argument("--request-id", type=UUID, help="unique request UUID; generated when omitted")
    sign.add_argument("--validity-seconds", type=int, default=120)
    sign.add_argument("--output", type=Path)
    for name in ("verify", "accept"):
        command = sub.add_parser(
            name,
            help="verify public evidence"
            if name == "verify"
            else "verify and durably consume a request before authorizing an action",
        )
        command.add_argument("--bundle", type=Path, required=True)
        command.add_argument("--payload", type=Path, required=True)
        _scope_arguments(command)
        command.add_argument("--clock-skew-seconds", type=int, default=30)
        command.add_argument("--output", type=Path)
        if name == "accept":
            command.add_argument(
                "--replay-db",
                type=Path,
                required=True,
                help="durable verifier-owned request ledger",
            )
    for name in ("recovery", "change-password"):
        command = sub.add_parser(
            name,
            help="native-only terminal operation; private input/output never passes through Python",
        )
        _native_argument(command)
        command.add_argument("--vault", type=Path, required=True)
        command.add_argument("--expected-signer-hash", required=True)
    backup = sub.add_parser(
        "backup",
        help="ask the native broker to copy an encrypted vault with protected file permissions",
    )
    _native_argument(backup)
    backup.add_argument("--vault", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument("--expected-signer-hash")
    args = parser.parse_args(argv)
    try:
        result: dict[str, Any]
        protected: list[Path] = []
        output = getattr(args, "output", None)
        for name in (
            "vault",
            "policy",
            "payload",
            "bundle",
            "identity",
            "replay_db",
            "catalog",
            "broker",
        ):
            supplied = getattr(args, name, None)
            if isinstance(supplied, Path):
                protected.append(supplied)
        if output is not None and any(_same_path(output, path) for path in protected):
            raise InvalidInputError(
                "output must not replace a vault, input, broker, or replay ledger"
            )
        if args.command == "build":
            result = build_native_broker(source=args.source, target_directory=args.target_dir)
            protected.append(Path(str(result["executable"])))
        elif args.command in ("verify", "accept"):
            policy = VerificationPolicy(
                expected_signer_hash=args.expected_signer_hash,
                audience=args.audience,
                workspace_id=str(args.workspace_id),
                domain=args.domain,
                allowed_payload_type_refs=frozenset((args.payload_type_ref,)),
                known_type_refs=_known_types(args.catalog),
                clock_skew_seconds=args.clock_skew_seconds,
            )
            bundle = _bounded_read(args.bundle, MAX_PUBLIC_DOCUMENT_BYTES)
            digest = _payload_digest(args.payload)
            protected.extend((args.bundle, args.payload))
            if args.command == "accept":
                protected.append(args.replay_db)
                if output is not None and any(_same_path(output, path) for path in protected):
                    raise InvalidInputError(
                        "public output must not replace an input or replay ledger"
                    )
                result = accept_signature_bundle_digest(
                    bundle, digest, policy=policy, replay_store=SQLiteReplayStore(args.replay_db)
                ).to_dict()
            else:
                result = verify_signature_bundle_digest(bundle, digest, policy=policy).to_dict()
        elif args.command == "verify-public":
            result = verify_public_identity(
                _bounded_read(args.identity, MAX_PUBLIC_DOCUMENT_BYTES),
                expected_signer_hash=args.expected_signer_hash,
            ).to_dict()
            protected.append(args.identity)
        else:
            broker = NativeIdentityBroker(args.broker)
            if args.command == "preflight":
                result = broker.preflight()
                _emit(result, output)
                return 0 if result["ready"] else 78
            protected.append(args.vault)
            if output is not None and args.command != "backup" and _same_path(output, args.vault):
                raise InvalidInputError("public output must not replace the private vault")
            if args.command == "public":
                result = broker.public(args.vault, expected_signer_hash=args.expected_signer_hash)
            elif args.command == "init":
                protected.append(args.policy)
                result = broker.initialize(
                    args.vault, args.policy, known_type_refs=_known_types(args.catalog)
                )
            elif args.command == "recover":
                protected.append(args.policy)
                result = broker.recover(
                    args.vault,
                    args.policy,
                    expected_signer_hash=args.expected_signer_hash,
                    known_type_refs=_known_types(args.catalog),
                )
            elif args.command == "sign":
                protected.append(args.payload)
                result = broker.sign(
                    args.vault,
                    {
                        "workspace_id": str(args.workspace_id),
                        "audience": args.audience,
                        "domain": args.domain,
                        "payload_type_ref": args.payload_type_ref,
                        "payload_sha256": _payload_digest(args.payload),
                        "request_id": str(args.request_id or uuid4()),
                        "validity_seconds": args.validity_seconds,
                    },
                    expected_signer_hash=args.expected_signer_hash,
                    known_type_refs=_known_types(args.catalog),
                )
            elif args.command == "recovery":
                result = broker.recovery(args.vault, expected_signer_hash=args.expected_signer_hash)
            elif args.command == "change-password":
                result = broker.change_password(
                    args.vault, expected_signer_hash=args.expected_signer_hash
                )
            elif args.command == "backup":
                result = broker.backup(
                    args.vault, args.output, expected_signer_hash=args.expected_signer_hash
                )
                output = None  # The native output path contains the encrypted backup.
            else:
                raise InvalidInputError("unsupported identity operation")
        _emit(result, output, protected_paths=protected)
        return 0
    except DomainError as error:
        print(json.dumps({"error": error.code.value, "message": error.message}), file=sys.stderr)
        return 2
    except (OSError, UnicodeError):
        print(
            json.dumps(
                {"error": "invalid_input", "message": "identity input/output operation failed"}
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
