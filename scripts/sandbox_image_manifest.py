#!/usr/bin/env python3
"""Render an immutable OCI reference manifest from trusted CI-provided identities.

This does not verify a signature or attest that the runtime sandbox is safe.
The host provisioner must verify image signature and extracted-rootfs identity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_IMAGE = re.compile(r"[a-z0-9][a-z0-9._/-]{1,199}\Z")


def manifest(image: str, digest: str, source_commit: str, platform: str) -> dict[str, object]:
    if not _IMAGE.fullmatch(image):
        raise ValueError("image repository must be an explicit lowercase registry path")
    if not _SHA.fullmatch(digest) or not _COMMIT.fullmatch(source_commit):
        raise ValueError("manifest requires immutable digest and Git commit")
    if platform not in {"linux/amd64", "linux/arm64"}:
        raise ValueError("image platform must be linux/amd64 or linux/arm64")
    document: dict[str, object] = {
        "schema": "potbot-sandbox-system-image/v1",
        "source_commit": source_commit,
        "platform": platform,
        "image": image,
        "digest": digest,
        "image_ref": f"{image}@sha256:{digest}",
        "guest_home": "/home",
        "guest_writable_persistent_mounts": ["/home", "/usr/local/tools"],
        "guest_shared_tool_writable_mount": "/usr/local/tools",
        "guest_writable_ephemeral_mounts": ["/var/ai"],
        "guest_shared_model_readonly_mount": "/opt/potbot/models",
        "model_promotions_operator_owned": True,
        "guest_default_shell": "/bin/bash",
        "rootfs_read_only_required": True,
        "runtime_qualified": False,
        "signature_verified_by_host": False,
    }
    document["manifest_sha256"] = hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    data = manifest(args.image, args.digest, args.source_commit, args.platform)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
