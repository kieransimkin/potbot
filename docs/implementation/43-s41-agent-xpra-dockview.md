# S41 — Agent-private native Xpra viewing through Dockview

## Baseline

Targets Potbot `main` **ba8e413d2740282f9dd1712a8eb583b09cf89cb2** (2026-10-27). S40's agent Xpra display pool and S14's operator-epoch fencing are prerequisites; the installer verifies their structure. Existing S39 browser control and the ordinary operator display remain unchanged. S41 adds read-only observation only; it does not grant a model, browser page or operator viewer the ability to start, stop or control another agent's browser.

## Authority and route separation

- The S40 host operator starts one **Xpra/Xdummy server per admitted agent**, in a separate X11 namespace with its own Xauthority and known live controlled-browser subprocess. Those processes remain under `XpraApplicationSupervisor` and the current operator attachment. S41 does **not** create agents or X servers from HTTP.
- The Xpra bridge bearer authenticates the operator's **read-only** `GET /v1/xpra/agent-sessions`; the result contains a bounded list of live `{agentId,id,title,state,startedAt,viewOnly}` records with no socket, PID, path, X cookie, or Cog reservation token.
- The operator's session-cookie and same-origin-protected Next.js BFF forwards `POST /api/ui/xpra/agent-sessions/{agentId}/{sessionId}/ticket`. The backend requires an exact active agent + session pair, live pinned workspace, read-only private display, and operator health.
- The backend `AgentXpraTicketAuthority` mints 30-second, single-use tickets bound to the **specific private socket inode and creation timestamp**. All tickets are revoked on use, even on failure. Tickets are never interpreted as commands or reusable browser reservations.
- The **dedicated** `/v1/xpra/agent/ws` relay consumes the ticket, checks trusted `Origin`, connects only to that private Xpra's operator-owned AF_UNIX protocol socket, and monitors operator/session/socket identity every 250ms. Any owner epoch change, session retirement or socket replacement ends the stream. Remote input remains blocked by each private server's `--readonly=yes` flag.
- A private Xpra ticket cannot be used at the legacy `/v1/xpra/ws` endpoint, and a legacy ticket cannot be used at `/v1/xpra/agent/ws`.

## Frontend

- A `AgentXpraStreamHost` polls the operator's read-only session inventory and creates **one independent Xpra HTML5 decoder iframe per agent browser**, each with a new random channel and a fresh short-lived ticket after reconnect.
- Native window IDs are qualified by workspace + browser session: `agentx11:{workspace-UUID}:{session-32hex}:xwin-{XID}`. Two different X servers may both generate `xwin-1`; their Dockview panes and canvas frames never share keys.
- The agent frame hub admits only same-origin messages from the **exact corresponding decoder iframe**, requires the correct channel and Xpra frame generation/sequence, and closes every transferred `ImageBitmap` whether painted or rejected. Panels send only native `frame`/`visibility` requests. No keyboard, pointer, resize, clipboard, or file transfer control is exposed.
- Windows open as independent Dockview panes; the existing application manager includes an **Agent-private browser displays** area to reopen a hidden pane. On session retirement its panes and frame buffers are destroyed. The existing operator Xpra stream remains independent and functional.

## Deployment and smoke testing

The module is installed with the normal Python package; there are no new downloaded executable dependencies or rootfs changes. The S40 operator policy must already have agent-private displays enabled and private Xpra servers configured with `browserLeases: true` and `strictXpraInput: true`.

```bash
PYTHONPATH=src python -m pytest -q tests/unit/test_agent_xpra_streams_s41.py \
  tests/unit/test_agent_xpra_displays_s40.py tests/unit/test_xpra_application_sessions.py \
  tests/unit/test_xpra_operator_fence_s14.py tests/integration/test_xpra_native_bridge.py
cd frontend
npm test && npm run typecheck && npm run build
```

**Mandatory real-host acceptance, not established by unit tests:** start two enrolled agent browsers, verify two private Xpra sockets with distinct Xauthority and Linux mount namespaces; run both and confirm the Xpra UI shows two independent panes even when their numeric X11 window IDs collide. Verify direct Xpra input fails, agent browser Cog commands still work for the owning agent, and observation works for the operator. Test wrong agent/session, repeated/expired tickets, owner-epoch loss during streaming, Xpra crash, hard session termination, and restart with a new Unix socket. Confirm orphaned process/lease handling and per-agent directory isolation. Run full Next.js tests and build on the deployment OS.

### Limitations

The backend is **operator-only** and purposefully read-only. It is not a bidirectional per-window input arbitration mechanism, screenshot archive, raw CDP tool, or agent launch API. Existing operator display is not merged into agent X servers; the UI merely presents independent decoded streams in a shared Dockview workspace. Do not assume the lack of direct mouse events in the UI is an isolation boundary without the server-side `--readonly=yes` and qualified host namespaces.
