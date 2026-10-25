#!/usr/bin/env python3
"""Gate each model-downloading tool's installation with declared model paths.

The trusted operator supplies offline instrumented probe JSON; an untested tool
is never admitted just because it declares HF_HOME. Avoid live downloads in CI.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.tools.model_cache_routes import (
    MODEL_ENV,
    MODEL_HOME_ALIASES,
    TOOL_DOWNLOAD_POLICIES,
    validate_tool_installation,
)


def check_image_contract(dockerfile: Path) -> None:
    content = dockerfile.read_text(encoding="utf-8")
    for name, target in MODEL_ENV.items():
        if f"{name}={target}" not in content:
            raise StateError(f"image fails to export {name}={target}")
    for name, target in MODEL_HOME_ALIASES.items():
        if f"ln -s {target} /etc/skel/{name}" not in content:
            raise StateError(f"image lacks /etc/skel/{name}")
    if "mkdir -p /home /var/ai /opt/potbot/models" not in content:
        raise StateError("sandbox rootfs has no model mount destinations")


class StateError(Exception):
    pass


def run(tool: str, probe: Path) -> dict[str, object]:
    declared = {item.name.casefold(): item for item in TOOL_DOWNLOAD_POLICIES}
    if tool.casefold() not in declared:
        raise StateError("tool missing from source-audited model-download inventory")
    payload = json.loads(probe.read_text(encoding="utf-8"))
    if (not isinstance(payload, dict) or payload.get("schema") != "potbot-tool-model-probe/v1"
            or payload.get("tool") != tool or payload.get("network_used") is not False
            or not isinstance(payload.get("paths"), list)
            or not all(isinstance(v, str) for v in payload["paths"])):
        raise StateError("model-download probe must be explicit, offline and schema-matched")
    env = payload.get("environment")
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                            for k, v in env.items()):
        raise StateError("model-download probe missing exact environment")
    validate_tool_installation(declared[tool.casefold()], env=env,
                               destinations=tuple(payload["paths"]), probe_performed=True)
    return {"tool": tool, "qualified_for_model_routes": True,
            "observed_destinations": payload["paths"],
            "no_network_used": True, "tool_install_approved": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-contract", type=Path)
    parser.add_argument("--tool")
    parser.add_argument("--probe", type=Path)
    args = parser.parse_args()
    try:
        if args.image_contract:
            check_image_contract(args.image_contract)
        if args.tool or args.probe:
            if not args.tool or not args.probe:
                raise StateError("tool gate needs both --tool and --probe")
            print(json.dumps(run(args.tool, args.probe), sort_keys=True))
        return 0
    except (StateError, InvalidInputError, ScopeDeniedError, OSError, ValueError) as exc:
        print(f"model cache gate failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
