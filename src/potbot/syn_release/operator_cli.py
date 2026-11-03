"""Operator deployment CLI: host setup, explicit bootstrap, attach and supervision."""

from __future__ import annotations

import argparse
import json
import platform
import signal
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from potbot.domain.errors import DomainError


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if (
        platform.system() == "Windows"
        and arguments
        and arguments[0] not in {"host-setup", "--help", "-h"}
    ):
        try:
            from .host import WslOperatorHost
            from .wsl_controller import forward

            return forward(WslOperatorHost(), "syn-operator", arguments)
        except (DomainError, OSError, ValueError):
            print(
                json.dumps(
                    {
                        "code": "operator_unavailable",
                        "message": "Prepare the managed WSL host and its reviewed Potbot controller first",
                    }
                )
            )
            return 2
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    setup = commands.add_parser(
        "host-setup",
        help="Provision native keystore prerequisites; Windows installs its managed WSL2 distro",
    )
    setup.add_argument("--controller-wheel", type=Path)
    setup.add_argument("--controller-sha256")
    for name in ("unlock", "bootstrap", "attach", "serve", "install-service", "switch-release"):
        sub = commands.add_parser(name)
        sub.add_argument("--store", type=Path, required=True)
        sub.add_argument("--policy", type=Path, required=True)
        sub.add_argument("--operator-root", type=Path, required=True)
        if name == "switch-release":
            sub.add_argument("--target-policy", type=Path, required=True)
        if name == "bootstrap":
            sub.add_argument("--new-identity", action="store_true", required=True)
            sub.add_argument("--keyring-service", required=True)
            sub.add_argument("--keyring-account", required=True)
            sub.add_argument("--network-id", required=True)
    args = parser.parse_args(arguments)
    if args.operation == "host-setup" and bool(args.controller_wheel) != bool(
        args.controller_sha256
    ):
        parser.error(
            "Controller installation requires both --controller-wheel and --controller-sha256"
        )
    try:
        if args.operation == "host-setup":
            from .host import native_host_setup

            value = native_host_setup()
            if args.controller_wheel:
                from .host import WslOperatorHost
                from .wsl_controller import install_controller

                value = {
                    **value,
                    "controller": install_controller(
                        WslOperatorHost(), args.controller_wheel, args.controller_sha256
                    ),
                }
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
            elif args.operation == "switch-release":
                from .operator_transition import switch_release

                value = switch_release(deployment, Policy.parse(private_read(args.target_policy)))
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
