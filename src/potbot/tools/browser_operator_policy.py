"""Read only the trusted Xpra input-mode flag before the server is started.

The full application policy is parsed again by its own loader after startup.
A missing/malformed policy NEVER starts an input-capable display accidentally.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path


def server_readonly_required(path: Path) -> bool:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError("operator Xpra policy must be a regular file")
    info = candidate.stat(follow_symlinks=False)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 8192:
        raise ValueError("operator Xpra policy is not private")

    def unique(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate policy key")
            result[key] = value
        return result

    try:
        raw = json.loads(candidate.read_text(encoding="utf-8"), object_pairs_hook=unique)
    except (UnicodeError, ValueError) as error:
        raise ValueError("invalid operator Xpra policy") from error
    if not isinstance(raw, dict) or (
            "strictXpraInput" in raw and type(raw["strictXpraInput"]) is not bool
    ) or ("browserLeases" in raw and type(raw["browserLeases"]) is not bool):
        raise ValueError("invalid operator Xpra input mode")
    strict = raw.get("strictXpraInput") is True
    if raw.get("browserLeases") is True and not strict:
        raise ValueError("browser leases require server-enforced readonly Xpra")
    if strict and raw.get("browserLeases") is not True:
        raise ValueError("readonly browser mode requires browser leases")
    if "agentDisplays" in raw and (not strict or not isinstance(raw["agentDisplays"], dict)):
        raise ValueError("agent displays require strict readonly browser reservations")
    if "browserEgress" in raw and (not strict or not isinstance(raw["browserEgress"], dict)):
        raise ValueError("browser egress requires strict Xpra input fencing")
    return strict
