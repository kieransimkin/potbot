#!/usr/bin/env python3
"""Browser-control source admission checklist (NOT a host qualification).

This only verifies source-level invariants. Real Xpra, WSL/Bubblewrap,
PostgreSQL artifact readback, and cross-agent isolation require live acceptance.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path


REQUIRED = (
    'src/potbot/tools/xpra_browser_worker.py',
    'src/potbot/tools/xpra_browser_control.py',
    'src/potbot/tools/agent_xpra_displays.py',
    'src/potbot/tools/xpra_app_sessions.py',
    'src/potbot/tools/agent_xpra_streams.py',
    'src/potbot/tools/x11_workspace_server.py',
    'src/potbot/tools/browser_session_leases.py',
    'src/potbot/tools/browser_evidence.py',
    'src/potbot/tools/browser_https_routes.py',
    'frontend/src/components/AgentXpraStreams.tsx',
    'frontend/src/lib/agent-xpra-hub.ts',
    'frontend/src/lib/agent-xpra-contract.ts',
    'frontend/src/lib/xpra-contract.ts',
    'frontend/tests/agent-xpra-s41.test.mjs',
    'frontend/tests/agent-xpra-s42.test.mjs',
    'tests/unit/test_agent_xpra_streams_s41.py',
    'tests/unit/test_browser_control_completion_s42.py',
)


def audit(root: Path) -> dict[str, object]:
    failures = []
    checked = []
    files = {}
    for name in REQUIRED:
        path = root / name
        if not path.is_file() or path.is_symlink():
            failures.append(f'missing or redirected: {name}')
            continue
        files[name] = path.read_text(encoding='utf8')
        checked.append(name)
        if name.endswith('.py'):
            try: ast.parse(files[name], filename=name)
            except SyntaxError: failures.append(f'invalid Python source: {name}')

    clauses = {
        'private Agent Xpra display pool': ('src/potbot/tools/agent_xpra_displays.py',
             ('class AgentXpraDisplayPool:', 'readonly_mode=True', '--tmpfs', '--ro-bind-data')),
        'agent browser command lease': ('src/potbot/tools/xpra_app_sessions.py',
             ('agent_browser_reservation(', 'actor_agent_id', 'agent_stream_socket(')),
        'view-only ticket cannot be replayed': ('src/potbot/tools/agent_xpra_streams.py',
             ('_pending.pop(ticket, None)', 'created_ns', 'still_live(')),
        'agent WebSocket admission': ('src/potbot/tools/x11_workspace_server.py',
             ('/v1/xpra/agent/ws', 'agent_streams.still_live(ticket)',
              '# S42: only an exact single agent-stream ticket is admitted.')),
        'monotonic stream epochs': ('frontend/src/lib/agent-xpra-hub.ts',
             ('private epochs = new Map', 'this.epochs.set(sessionId, epoch)',
              'this.streams.get(sessionId)?.epoch !== epoch')),
        'rendered bitmap cleanup': ('frontend/src/components/AgentXpraStreams.tsx',
             ('data.bitmap.close()', 'clearTimeout(readyWatchdog)', 'hub.disconnected(session.id, epoch || undefined)')),
        'read-only Xpra control stream': ('frontend/src/lib/agent-xpra-hub.ts',
             ('"frame" | "visibility"', 'this.send(w.sessionId, w.id, "frame")')),
        'strict browser command validation': ('src/potbot/tools/xpra_browser_worker.py',
             ('validate_command(', 'CDP_READ', 'CDP_INPUT', 'ALLOWED_ROLES')),
        'bounded browser commands': ('src/potbot/tools/xpra_browser_control.py',
             ('MAX_REQUEST', 'MAX_REPLY', 'session fenced')),
    }
    for label, (path, tokens) in clauses.items():
        content = files.get(path, '')
        if not all(token in content for token in tokens): failures.append(f'source invariant missing: {label}')
    return {'schema': 'potbot-browser-source-audit/v1',
            'source_checks_passed': not failures,
            'live_host_qualified': False,
            'checked_files': len(checked),
            'failures': failures,
            'host_gates': ['private Xpra/Xdummy multi-agent isolation',
                           'Bubblewrap mount and AF_UNIX capability validation',
                           'real Chromium and operator-owned CONNECT broker',
                           'PostgreSQL evidence readback and principal RLS',
                           'operator epoch recovery and uncertain action fencing']}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    try: outcome = audit(args.root.resolve(strict=True))
    except (OSError, UnicodeError) as err:
        print('browser audit failed: unable to read project sources', file=sys.stderr)
        return 2
    data = json.dumps(outcome, indent=2, sort_keys=True) + '\n'
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(data, encoding='utf8')
    print(data)
    return 0 if outcome['source_checks_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
