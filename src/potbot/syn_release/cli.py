"""Explicit operator CLI; never called from an Agent Cog or startup fallback."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from collections.abc import Sequence
from pathlib import Path

from potbot.domain.errors import DomainError
from potbot.syn_release.contracts import Policy
from potbot.syn_release.source import GitHubSource
from potbot.syn_release.store import SynReleaseStore, private_read


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if (
        platform.system() == "Windows"
        and arguments
        and not any(arg in {"--help", "-h"} for arg in arguments)
    ):
        try:
            from .host import WslOperatorHost
            from .wsl_controller import forward

            return forward(WslOperatorHost(), "syn-release", arguments)
        except (DomainError, OSError, ValueError):
            print(
                json.dumps(
                    {
                        "code": "source_unavailable",
                        "message": "Prepare the managed WSL host and its reviewed Potbot controller first",
                    }
                )
            )
            return 2
    parser = argparse.ArgumentParser(
        description="Verify and install an independently pinned official Syn release on Linux/macOS"
    )
    parser.add_argument("operation", choices=("fetch", "install", "verify"))
    parser.add_argument(
        "--store",
        required=True,
        type=Path,
        help="Provisioned private operator directory outside all Agent homes",
    )
    parser.add_argument(
        "--policy",
        required=True,
        type=Path,
        help="Independent current trust/compatibility policy; operator-private file",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Require retained authenticated payloads and dependency cache; no network fallback",
    )
    parser.add_argument(
        "--python", type=Path, help="Exact trusted host Python executable for a new installation"
    )
    parser.add_argument(
        "--uv", type=Path, help="Exact trusted uv 0.12.23 executable for a new installation"
    )
    options = parser.parse_args(arguments)
    if options.operation == "install" and (options.python is None or options.uv is None):
        parser.error("install requires explicit --python and --uv executable paths")
    try:
        store = SynReleaseStore(options.store)
        policy = Policy.parse(private_read(options.policy))
        source = (
            None if options.offline else GitHubSource(os.environ.get("POTBOT_SYN_GITHUB_TOKEN"))
        )
        if options.operation == "fetch":
            release = store.fetch(policy, source=source, offline=options.offline)
            result = {
                "manifest_sha256": policy.manifest_sha256,
                "verified_payloads": len(release.assets),
                "installed": False,
                "node_qualified": False,
                "offline": options.offline,
            }
        elif options.operation == "install":
            result = store.install(
                policy, python=options.python, uv=options.uv, source=source, offline=options.offline
            )
        else:
            result = store.verify(policy)
        print(json.dumps(result, sort_keys=True))
        return 0
    except DomainError as error:
        print(json.dumps(error.to_dict(), sort_keys=True))
        return 2
    except OSError:
        # Private paths, signed URLs, proxy credentials and tokens never enter output.
        print(
            json.dumps(
                {
                    "code": "source_unavailable",
                    "message": "Syn operator storage, tool or network access failed; reconcile before retrying",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
