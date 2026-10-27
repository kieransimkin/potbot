#!/usr/bin/env python3
"""Check pinned local SYN discovery; never install SYN or create identity keys.

Missing SYN installation returns an actionable external installer requirement,
not a fake initialized identity or a successful pledge.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from potbot.domain.errors import DomainError
from potbot.tools.syn_local_daemon import SynInstallationRegistry, SynStartupPlanner


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--expected-master-hash", type=str)
    parser.add_argument("--new-agent", action="store_true")
    arguments = parser.parse_args()
    master = None
    if arguments.expected_master_hash is not None:
        value = arguments.expected_master_hash
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            parser.error("--expected-master-hash must be 64 lowercase hex characters")
        master = bytes.fromhex(value)
    try:
        planner = SynStartupPlanner(SynInstallationRegistry(arguments.registry))
        decision = (
            planner.new_agent(target_master_hash=master) if arguments.new_agent
            else planner.potbot_first_run(expected_master_hash=master)
        )
        print(json.dumps({
            "schema": "potbot-syn-bootstrap-preflight/v1",
            "state": decision.state,
            "required_mode": decision.required_mode,
            "operator_master_hash": decision.operator_master_hash,
            "verified_local_identity": decision.verified_local_identity,
            "private_keys_created": False,
            "pledge_completed": False,
        }, sort_keys=True))
        # Preflight has verified an existing operator but cannot claim pledge
        # complete. An explicit installer/pledge service is still required.
        return 0 if decision.verified_local_identity else 2
    except DomainError as exc:
        print(json.dumps({"schema":"potbot-syn-bootstrap-preflight/v1",
                          "state":"blocked", "reason":exc.code.value}), file=sys.stderr)
        return 2
    except OSError:
        print(json.dumps({"schema":"potbot-syn-bootstrap-preflight/v1",
                          "state":"blocked", "reason":"host_io_failure"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
