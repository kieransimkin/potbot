#!/usr/bin/env python3
"""S14 real-host Xpra admission test. Only targets a trusted loopback bridge.

Run twice against a disposable, operator-managed Linux/WSL2 workspace:
  healthy: while the pinned operator process is running;
  fenced:  after the operator is stopped or loses its private attachment.
This tool NEVER terminates an operator itself and never displays bearer tokens.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def loopback_root(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.port is None or parsed.username or parsed.password
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError
    except ValueError:
        raise ValueError("The Xpra acceptance target must be a literal loopback HTTP endpoint") from None
    return f"http://{parsed.netloc}"


def probe(base: str, token: str, workspace_id: UUID, *, phase: str) -> dict[str, object]:
    if phase not in {"healthy", "fenced"} or not isinstance(workspace_id, UUID):
        raise ValueError("Invalid acceptance phase or workspace")
    if not isinstance(token, str) or not 32 <= len(token) <= 4096 or "\n" in token:
        raise ValueError("The Xpra bridge token is unavailable")
    base = loopback_root(base)
    opener = build_opener(ProxyHandler({}), NoRedirects())
    codes: dict[str, int | None] = {}
    healthy_reply = None
    for path, method in (("/v1/xpra/status", "GET"), ("/v1/xpra/tickets", "POST")):
        request = Request(base + path, method=method,
                          headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
        try:
            with opener.open(request, timeout=2) as response:
                codes[path] = response.status
                payload = response.read(4097)
                if len(payload) > 4096:
                    raise ValueError("Xpra response size exceeded bounded read")
                if phase == "healthy":
                    record = json.loads(payload)
                    if (not isinstance(record, dict) or record.get("workspaceId") != str(workspace_id)):
                        raise ValueError("Xpra response belongs to another workspace")
                    if path.endswith("/status"):
                        healthy_reply = record.get("available") is True
        except HTTPError as error:
            codes[path] = error.code
        except (TimeoutError, URLError, ConnectionError, OSError):
            codes[path] = None
    if phase == "healthy":
        accepted = all(code == 200 for code in codes.values()) and healthy_reply is True
    else:
        # HTTP 503 or confirmed service disappearance both deny new grants.
        # A 401/403 could merely mean a bad token; that does NOT qualify fencing.
        accepted = all(code in {503, None} for code in codes.values())
    return {"schema": "potbot-s14-xpra-host-acceptance/v1", "phase": phase,
            "workspace_id": str(workspace_id), "passed": accepted,
            "status_http": codes["/v1/xpra/status"],
            "ticket_http": codes["/v1/xpra/tickets"],
            "warning": "Observation only; operator shutdown and live browser input require separate operator actions"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--workspace", type=UUID, required=True)
    parser.add_argument("--phase", required=True, choices=("healthy", "fenced"))
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.report.exists() or args.report.is_symlink():
            raise ValueError("Acceptance report already exists")
        result = probe(args.url, os.environ.get("POTBOT_XPRA_BRIDGE_TOKEN", ""),
                       args.workspace, phase=args.phase)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as writer:
            json.dump(result, writer, sort_keys=True)
            writer.write("\n")
            writer.flush(); os.fsync(writer.fileno())
        print(json.dumps({"phase": args.phase, "passed": result["passed"]}))
        return 0 if result["passed"] else 1
    except (ValueError, OSError):
        print('{"passed":false,"error":"xpra_acceptance_unavailable"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
