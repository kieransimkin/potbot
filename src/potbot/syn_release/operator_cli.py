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
from uuid import UUID

from potbot.domain.errors import DomainError


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if (
        platform.system() == "Windows"
        and arguments
        and arguments[0] not in {"host-setup", "host-status", "--help", "-h"}
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
    setup.add_argument(
        "--replace-pending-controller",
        action="store_true",
        help="replace an interrupted controller selection with the supplied reviewed wheel and digest",
    )
    commands.add_parser("host-status", help="Report managed WSL host and worker prerequisites")
    for name in (
        "unlock",
        "bootstrap",
        "attach",
        "serve",
        "install-service",
        "switch-release",
        "configure-context-route",
    ):
        sub = commands.add_parser(name)
        sub.add_argument("--store", type=Path, required=True)
        sub.add_argument("--policy", type=Path, required=True)
        sub.add_argument("--operator-root", type=Path, required=True)
        if name in {"serve", "install-service"}:
            sub.add_argument("--context-rpc", action="store_true")
        if name == "configure-context-route":
            sub.add_argument("--workspace-id", type=UUID, required=True)
            sub.add_argument("--include-mcp", action="store_true")
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
    if (
        args.operation == "host-setup"
        and args.replace_pending_controller
        and not args.controller_wheel
    ):
        parser.error(
            "--replace-pending-controller requires an explicit controller wheel and digest"
        )
    try:
        if args.operation == "host-status":
            from .host import WslOperatorHost

            value = WslOperatorHost().status()
        elif args.operation == "host-setup":
            if args.controller_wheel:
                from .host import WslOperatorHost
                from .wsl_controller import install_controller

                manager = WslOperatorHost()
                install_controller(
                    manager,
                    args.controller_wheel,
                    args.controller_sha256,
                    replace_pending=args.replace_pending_controller,
                )
                value = manager._read_receipt()
            else:
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

                value = install_service(deployment, args.policy, context_rpc=args.context_rpc)
            elif args.operation == "configure-context-route":
                from potbot.context.installed_syn_route import provision_context_route

                path = provision_context_route(
                    deployment, args.workspace_id, include_mcp=args.include_mcp
                )
                value = {"workspace_id": str(args.workspace_id), "route": str(path)}
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
                deployment.supervise(stop=stopped.is_set, context_rpc=args.context_rpc)
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
