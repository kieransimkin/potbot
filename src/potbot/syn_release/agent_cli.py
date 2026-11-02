"""Privileged Linux/WSL controller for release-verified Syn Agent processes.

The private local terminal belongs to Syn. Stdin/stdout carry only bounded
public/encrypted enrollment frames, never passwords, seeds or private keys.
"""

from __future__ import annotations

import argparse
import os
import selectors
import sys
from pathlib import Path
from uuid import UUID

from potbot.domain.errors import DomainError, ScopeDeniedError, StateMismatchError

from .agent import SynAgentDeployment
from .contracts import Policy, canonical, document, fields
from .store import SynReleaseStore, private_read


def _output(value: object) -> None:
    sys.stdout.buffer.write(canonical(value) + b"\n")
    sys.stdout.buffer.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("session", "attach"))
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--agent-root", type=Path, required=True)
    parser.add_argument("--agent-id", type=UUID, required=True)
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--expected-operator", required=True)
    parser.add_argument("--network-id", required=True)
    parser.add_argument("--group-id", type=UUID, required=True)
    parser.add_argument("--group-epoch", type=int, required=True)
    parser.add_argument("--approved-device-profile", required=True)
    parser.add_argument("--new-identity", action="store_true")
    args = parser.parse_args(argv)
    try:
        if sys.platform != "linux" or os.getuid() != 0:
            raise ScopeDeniedError(
                "Run the privileged Syn Agent controller in native Linux or the managed WSL2 host"
            )
        policy = Policy.parse(private_read(args.policy))
        deployment = SynAgentDeployment(
            SynReleaseStore(args.store),
            policy,
            args.agent_root,
            agent_id=args.agent_id,
            uid=args.uid,
            expected_operator=args.expected_operator,
            network_id=args.network_id,
            group_id=args.group_id,
            group_epoch=args.group_epoch,
            device_profile=args.approved_device_profile,
            policy_path=args.policy,
        )
        if args.operation == "attach":
            if args.new_identity:
                raise ScopeDeniedError("Read-only attachment cannot create an identity")
            _output(deployment.attach())
            return 0
        with deployment.session(new_identity=args.new_identity) as session:
            _output(session.initial)
            certificate = None
            with selectors.DefaultSelector() as selector:
                selector.register(sys.stdin.buffer, selectors.EVENT_READ)
                while True:
                    if session.process.poll() is not None:
                        raise StateMismatchError(
                            "Owned Syn Agent session exited; deployment fenced"
                        )
                    if not selector.select(0.25):
                        continue
                    raw = sys.stdin.buffer.readline(65538)
                    if not raw:
                        return 0
                    if len(raw) > 65537 or not raw.endswith(b"\n"):
                        raise ScopeDeniedError("Private Syn enrollment frame exceeds its bound")
                    frame = fields(
                        document(raw[:-1], canonical_only=True), {"operation", "payload"}
                    )
                    response = session.exchange(frame["operation"], frame["payload"])
                    if frame["operation"] == "activate":
                        certificate = frame["payload"]
                    elif frame["operation"] in {"serve", "restart"}:
                        if certificate is not None:
                            response = deployment.retain_assignment(
                                response, adoption_receipt=certificate, session=session
                            )
                        else:
                            response = deployment.attach()
                            session.observe(response, owner_uid=args.uid)
                    _output(response)
    except (DomainError, OSError, ValueError, KeyError):
        print(
            "Syn Agent deployment refused; reconcile the pinned release, private native custody and host registration",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
