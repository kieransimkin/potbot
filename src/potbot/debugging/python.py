"""Explicit debugpy configuration only; this module never launches or attaches."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from potbot.debugging.contracts import DebugIdentity
from potbot.domain.errors import InvalidInputError

DEBUGPY_VERSION = "1.8.22"


def launch_configuration(
    identity: DebugIdentity,
    *,
    program: str | Path,
    python: Path,
    arguments: tuple[str, ...] = (),
) -> dict[str, Any]:
    """A pinned source target; the controller still approves this exact configuration."""
    path = identity.source(program)
    if not python.is_absolute() or not python.is_file():
        raise InvalidInputError("Python debugger requires an explicit installed interpreter")
    if len(arguments) > 128 or any(not isinstance(x, str) or len(x) > 4096 for x in arguments):
        raise InvalidInputError("Python debugger arguments exceed their bound")
    return {
        "name": "potbot-python",
        "type": "python",
        "request": "launch",
        "program": str(path),
        "python": [str(python)],
        "args": list(arguments),
        "cwd": str(identity.source_root),
        "console": "internalConsole",
        "justMyCode": True,
        "stopOnEntry": False,
        "subProcess": False,
        "redirectOutput": True,
    }
