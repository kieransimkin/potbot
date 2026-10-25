"""Host-only operator process for the existing S7 /etc/ai history owner.

This entrypoint does not authorize guest execution and does not expose vault
secrets or control sockets. It runs the real AiWorkspaceOperator on a separately
provisioned Linux/WSL2 host. Other processes do NOT gain a shared Python
supervisor by virtue of this watcher process; worker attach/health IPC needs a
separately qualified cross-process controller integration.
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Sequence
from uuid import UUID

from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError

if TYPE_CHECKING:
    from potbot.tools.shared_workspace_isolation import SharedWorkspaceRuntime

_HEX = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = frozenset({"workspace_id", "image_digest", "qualification_sha256", "rootfs",
                     "home", "model_staging", "shared_models", "shared_tools", "ai_config_dir",
                     "ai_history_dir", "controller_ledger"})


@dataclass(frozen=True, slots=True)
class OperatorPaths:
    workspace_id: UUID
    image_digest: str
    qualification_sha256: str
    rootfs: Path
    home: Path
    model_staging: Path
    shared_models: Path
    shared_tools: Path
    ai_config_dir: Path
    ai_history_dir: Path
    controller_ledger: Path

    def runtime(self) -> SharedWorkspaceRuntime:
        from potbot.tools.shared_workspace_isolation import SharedWorkspaceRuntime

        return SharedWorkspaceRuntime(
            self.image_digest, self.rootfs, self.home, self.qualification_sha256,
            model_staging=self.model_staging, shared_models=self.shared_models,
            shared_tools=self.shared_tools, ai_config_dir=self.ai_config_dir,
            ai_history_dir=self.ai_history_dir,
        )


def parse_manifest(manifest: Path) -> OperatorPaths:
    """Operator-selected exact host paths; no values originate with a guest."""
    path = Path(manifest)
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or path.resolve(strict=True) != path):
        raise InvalidInputError("Operator manifest must be an existing real absolute file")
    if path.stat().st_mode & 0o022:
        raise ScopeDeniedError("Operator manifest must not be group/world writable")
    if path.stat().st_size > 8192:
        raise InvalidInputError("Operator manifest exceeds size allowance")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != _FIELDS:
            raise ValueError
        identity = UUID(payload["workspace_id"])
        if str(identity) != payload["workspace_id"]:
            raise ValueError
        if not all(isinstance(payload[name], str) and _HEX.fullmatch(payload[name])
                   for name in ("image_digest", "qualification_sha256")):
            raise ValueError
        def absolute(name: str) -> Path:
            raw = payload[name]
            p = Path(raw)
            if not isinstance(raw, str) or not p.is_absolute() or p.is_symlink():
                raise ValueError
            resolved = p.resolve(strict=True)
            if resolved != p:
                raise ValueError
            return resolved
        fields = {name: absolute(name) for name in _FIELDS - {
            "workspace_id", "image_digest", "qualification_sha256"
        }}
        if fields["controller_ledger"].is_relative_to(fields["home"]):
            raise ValueError
        if fields["ai_history_dir"].is_relative_to(fields["home"]):
            raise ValueError
        return OperatorPaths(identity, payload["image_digest"], payload["qualification_sha256"],
                             **fields)
    except (KeyError, ValueError, TypeError, UnicodeError, OSError):
        raise InvalidInputError("Operator manifest contains unsafe paths or identity") from None


def run_operator(paths: OperatorPaths, *, stop: threading.Event,
                 pulse: Callable[[dict[str, object]], None] | None = None) -> None:
    from potbot.tools.ai_workspace_operator import AiWorkspaceOperator
    from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger

    ledger = WorkspaceLeaseLedger(paths.controller_ledger, workspace_home=paths.home)
    with AiWorkspaceOperator(workspace_id=paths.workspace_id, ledger=ledger,
                             runtime=paths.runtime()) as operator:
        while True:
            status = operator.status()
            if not status["history_healthy"]:
                raise CapabilityUnavailableError("AI history monitor faulted; guest execution fenced")
            if pulse is not None:
                pulse(status)
            # A termination signal requests quiescence but is NOT authorization
            # to abandon an active or uncertain workspace lease. Remain alive
            # until the host operator has reconciled every outstanding child.
            if stop.is_set() and not ledger.active(paths.workspace_id):
                break
            time.sleep(0.5)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="potbot operator", description=__doc__)
    parser.add_argument("run", nargs="?", default="run")
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        paths = parse_manifest(args.manifest)
        stopped = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            if sig is not None:
                signal.signal(sig, lambda _sig, _frame: stopped.set())
        run_operator(paths, stop=stopped)
        print(json.dumps({"operator": "stopped", "workspace_id": str(paths.workspace_id)}))
        return 0
    except (CapabilityUnavailableError, InvalidInputError, ScopeDeniedError) as error:
        print(json.dumps({"operator": "failed", "error_code": error.code.value}), file=sys.stderr)
        return 1
    except Exception:
        # No raw pathname, lease metadata, process arguments, or inotify stack
        # diagnostics on stdout/stderr in the normal managed service log.
        print('{"operator":"failed","error_code":"internal_error"}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
