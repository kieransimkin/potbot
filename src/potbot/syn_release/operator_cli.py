"""Operator deployment CLI: host setup, explicit bootstrap, attach and supervision."""

from __future__ import annotations

import argparse
import json
import signal
import threading
from collections.abc import Sequence
from pathlib import Path

from potbot.domain.errors import DomainError


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser(
        "host-setup",
        help="Provision native keystore prerequisites; Windows installs its managed WSL2 distro",
    )
    for name in ("unlock", "bootstrap", "attach", "serve", "install-service"):
        sub = commands.add_parser(name)
        sub.add_argument("--store", type=Path, required=True)
        sub.add_argument("--policy", type=Path, required=True)
        sub.add_argument("--operator-root", type=Path, required=True)
        if name == "bootstrap":
            sub.add_argument("--new-identity", action="store_true", required=True)
            sub.add_argument("--keyring-service", required=True)
            sub.add_argument("--keyring-account", required=True)
            sub.add_argument("--network-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.operation == "host-setup":
            from .host import native_host_setup

            value = native_host_setup()
        else:
            from .contracts import Policy
            from .operator import SynOperatorDeployment
            from .store import SynReleaseStore, private_read

            deployment = SynOperatorDeployment(
                SynReleaseStore(args.store),
                Policy.parse(private_read(args.policy)),
                args.operator_root,
                policy_path=args.policy,
            )
            if args.operation == "unlock":
                deployment.unlock()
                return 0
            if args.operation == "install-service":
                from .system_service import install_service

                value = install_service(deployment, args.policy)
            elif args.operation == "bootstrap":
                value = deployment.bootstrap(
                    new_identity=args.new_identity,
                    keyring_service=args.keyring_service,
                    keyring_account=args.keyring_account,
                    network_id=args.network_id,
                )
            elif args.operation == "attach":
                value = deployment.attach()
            else:
                stopped = threading.Event()
                for sig in (signal.SIGINT, signal.SIGTERM):
                    signal.signal(sig, lambda *_: stopped.set())
                deployment.supervise(stop=stopped.is_set)
                return 0
        print(json.dumps(value, sort_keys=True))
        return 0
    except DomainError as exc:
        print(json.dumps(exc.to_dict(), sort_keys=True))
        return 2
    except (OSError, ValueError, KeyError):
        print(
            json.dumps(
                {
                    "code": "operator_unavailable",
                    "message": "Operator host, registration or native keystore requires reconciliation",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
