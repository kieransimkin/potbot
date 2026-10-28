# Browser-control completion S41–S42: source and host acceptance

## Status and scope

This package completes the missing source integration between the S40 private
agent Xpra servers and the operator's Dockview UI, and strengthens the already
implemented browser controls. It is cumulative: S41 does **not** need to be
installed separately. Existing S37–S40 interfaces remain unchanged.

| Capability | Source | Qualification still required |
|---|---|---|
| Playwright, Patchright, stealth, bounded CDP | existing browser worker | installed Chromium + real host smoke |
| Direct pointer/keyboard, role/name actions, DevTools picker | existing browser RPC/UI | rendered click and accessibility tests |
| Image/ARIA/CSS evidence, immutable artifacts and gallery | existing Cogs/store | real PostgreSQL readback + isolation |
| One browser per agent, separate X11 auth + private mount namespace | S40 | real Bubblewrap/Xpra and cross-agent tests |
| Agent-private native Xpra streams, Dockview windows | S41 (included) | two live agent browsers concurrently |
| Replay-proof ticket, monotonic epochs, bitmap reclamation | S41–S42 | operator restart and reconnection fault tests |
| HTTPS egress allowlist via UNIX CONNECT | S39 | DNS/TLS and failed-direct-network tests |

The source tests are not evidence that live kernel isolation or end-to-end
browser networking has passed. **Do not mark the feature production qualified**
until the host gates below have been exercised on the deployed image.

## Security boundaries

A workspace operator owns the server and all private display roots. Agent
processes receive only their own agent-home mount and X11 Unix socket. Their
Xauthority credentials are sealed, read-only memory-backed objects. The viewer
gets neither the Xauthority cookie nor the agent's browser reservation token.
Browser input runs through the independently reserved Playwright/Patchright
channel. The operator UI's private-agent Xpra streams are view-only, with Xpra
configured read-only at the server. Public application endpoints must not grant
browser ownership or termination to a different agent.

The UI uses globally scoped window identifiers. Every connection receives a
monotonic stream epoch; disconnect invalidates old frames even if X11 numeric
window IDs are recycled. Every transferred ImageBitmap is closed after copying
or rejection. A stale canvas cleanup cannot hide its replacement. Suspicious
WebSocket URL query parameters are rejected before consuming a one-use ticket.

## Source verification

```bash
python scripts/check_browser_control_s42.py --root .
uv run --locked python -m pytest -q \
  tests/unit/test_browser_cogs_s37.py tests/unit/test_browser_s38.py \
  tests/unit/test_browser_s39.py tests/unit/test_xpra_browser_control.py \
  tests/unit/test_xpra_browser_worker.py tests/unit/test_agent_xpra_displays_s40.py \
  tests/unit/test_agent_xpra_streams_s41.py \
  tests/unit/test_browser_control_completion_s42.py \
  tests/unit/test_xpra_application_sessions.py
cd frontend
npm install --ignore-scripts --no-audit --no-fund
npm run typecheck && npm test && npm run build
```

## Mandatory live host acceptance

Use Linux/WSL2 with a qualified pinned immutable rootfs, operator epoch,
Bubblewrap+seccomp, Xpra >= 6.3/Xdummy and the real Chromium worker. The tests
must verify all of the following with independent trace evidence:

1. Two registered agents start private browsers simultaneously, each with
   different X displays/Xauthority and hidden sibling `/home` entries. Agent A
   cannot open B's socket or read B's cookie, model cache or browser profile.
2. A human observer receives one-use tickets for both browsers and opens
   two Dockview windows. Confirm the same numeric X window ID remains unique,
   read-only stream input does not reach Xpra, and the browser controller's
   reserved role/keyboard/pointer commands still work.
3. Close/restart one agent's browser while streaming. Verify the other stream
   continues, stale JPEG/ImageBitmap frames cannot paint, every old ticket is
   denied and a new ticket is issued only after independent agent-session
   validation.
4. Kill/restart the controlling operator during an in-flight click or submit.
   Verify process/lease fencing, no automatic retry, no old-epoch reconnect,
   and confirmed process-tree shutdown before display retirement.
5. Test HTTP/HTTPS allowlist behavior with actual TLS validation and DNS
   rebinding adversaries. Block direct TCP, WebSockets, POST, local IPs, private
   DNS answers, and redirects outside the approved allowlist.
6. Store screenshot/DOM/ARIA evidence in real PostgreSQL+ArtifactStore. Read
   through the authenticated viewer, then attempt another workspace/principal
   and require complete denial even with a leaked artifact identifier.
7. With the S36 logging feature enabled, prove agent-owned log partitions,
   operator scanning and archive persistence, and cancellation on watcher loss.

The host qualification output must pin the workspace ID, executable/image
SHA-256, operator epoch, and observed isolation state. Passing only the source
gate **must not** set `runtime_qualified=true`.

## Applying or upgrading

Run the single-file `potbot_browser_complete.py` from the repository root.
Its `--dry-run` preflights all modifications. It supports missing S41 files or
exact, published S41 files; arbitrary changed files are refused. Backups are
saved under `.git/potbot-browser-completion-backup/` and nothing contacts GitHub.
