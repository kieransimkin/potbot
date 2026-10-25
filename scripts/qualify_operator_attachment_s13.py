#!/usr/bin/env python3
"""Real, read-only local operator socket qualification. Not an OS escape test."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from potbot.tools.operator_attachment_s13 import OperatorAttachmentClient, mount_fingerprint
from potbot.tools.operator_cli import parse_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--socket-path", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        paths = parse_manifest(args.manifest)
        if args.report.resolve() == args.manifest.resolve() or (
            args.report.resolve().is_relative_to(paths.home.resolve())
        ):
            raise ValueError("Qualification receipts must be controller-private")
        client = OperatorAttachmentClient(
            socket_path=args.socket_path, workspace_id=paths.workspace_id,
            image_digest=paths.image_digest,
            qualification_sha256=paths.qualification_sha256,
            mounts_sha256=mount_fingerprint(paths.ai_config_dir, paths.ai_history_dir),
        )
        status = client.require_healthy()
        report = {"schema": "potbot-s13-live-local-attachment/v1",
                  "workspaceId": str(paths.workspace_id),
                  "imageDigest": paths.image_digest,
                  "epoch": status["epoch"],
                  "watcherHealthy": status["history_healthy"],
                  "releaseGatesPending": status["pending_release_gates"],
                  "readOnlyProbe": True,
                  "warning": "A live Unix socket check is not adversarial OS sandbox qualification"}
        report["sha256"] = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()
        if args.report.is_symlink():
            raise ValueError("Qualification report destination may not be redirected")
        args.report.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(args.report, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(report, sort_keys=True, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        print(json.dumps({"qualified_local_socket": True, "receipt_sha256": report["sha256"]}))
        return 0
    except Exception:
        print('{"qualified_local_socket":false,"error":"operator_attach_failed"}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
