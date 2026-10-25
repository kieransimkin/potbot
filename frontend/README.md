# Potbot Orb Workspace — UI-1/UI-2

Next.js frontend for Potbot, designed to live under `potbot/frontend/`. This first
increment implements an orbital workspace selector and a floating/dockable window
shell, with an on-demand Three.js graph **demo preview**. It deliberately does
**not** claim that the backend's workspace discovery, chat sessions, PTY, browser,
preferences or download/installation telemetry contracts exist yet.

## Run

Node 22+ is recommended. From the repository's `frontend/` directory:

```bash
npm install  # generates package-lock.json; commit it after validating the build
npm run dev
```

Open `http://127.0.0.1:3100/?demo=1` for an **explicitly synthetic** four-workspace
preview. The root route `/` never invents backend resources: until UI-backend
integration is configured it displays a setup notice.

The first build environment used for this patch has no external npm registry
access, so neither `npm install`, `npm run build`, nor an interactive browser E2E
run could be completed here. The pure layout/orbit/graph tests run without npm
packages on Node 22:

```bash
npm test
node --experimental-strip-types tests/bench-graph.mjs
```

These checks are not substitutes for a browser build. Once dependencies are
installed, run `npm run typecheck` and `npm run build`, then test the interface
with Playwright on Chromium, Firefox and WebKit.

## Controls

- Hover the top orbital ring, or click **Workspaces** to reveal all workspace
  orbs. **Alt + Left/Right** switches and recentres the active orb. The other
  orbs move above the top edge after selection.
- The chat is the **permanent central surface**. Other panes float by default.
  Drag a Dockview pane header or tab to reposition/dock; each pane also has
  explicit dock-left/right/above/below and float controls.
- The **menu cannot maximize**. The graph can maximize using its `⛶` control
  even while it is floating. Escape restores the graph to its prior pane.
- Close the graph to animate into an independent **utility orb** near the
  upper-right; click that orb to restore the floating pane. It does not join the
  workspace ring.
- The **terminal** button inside the composer creates a floating pane, initially
  anchored near the composer. It remains a placeholder until a workspace-scoped
  authenticated PTY service is provided by the backend.
- The menu opens **Preferences**, **Downloads** and **Model installations** in
  independent dockable panes. All progress indicators accurately report that
  backend telemetry is not connected; no fake downloads/installations appear.
- Window layouts are persisted in **browser localStorage per workspace** with a
  versioned schema. Switching workspaces unmounts its layout and restores the
  newly selected workspace's layout.

## Performance choices

The orbital selector uses CSS transforms and opacity. Motion handles spring
interpolation and shared-layout animation without creating a WebGL context for
navigation. Dockview does all the heavy docking, saving/restoring, floating and
resizing. The graph preview uses **one InstancedMesh** for all nodes plus a
single line-segment geometry for edges and does not run a perpetual animation
loop. It renders only on interaction, resize, visibility and data changes.

The graph caps device pixel ratio to 1.5, nodes to 1500, and edges to 4500.
When hidden or replaced with its utility orb, its WebGL resources are disposed.
A keyboard-focusable list/fallback should be expanded in UI-3 for production
accessibility. The included CPU diagnostic measures layout projection only,
not actual GPU frame times or VRAM usage.

See `../docs/ui/performance-research.md` and
`../docs/ui/frontend-backend-contract.md` for design trade-offs and missing
backend services.

## Safety boundaries

The preview never accepts provider keys in browser code. No Next.js route is
currently permitted to proxy Potbot's privileged bearer credentials. When
connecting to a real backend, implement user authentication, workspace-scoped
sessions, CSRF protection for mutations, and a server-side credential boundary
before enabling remote requests. Do not expose Potbot's service API token using
`NEXT_PUBLIC_` environment variables.

## UI-3 — live operator bridge, streaming chat and reasoning checkpoint graph

UI-3 adds server-only, single-operator authentication and a limited bridge to
**existing** Potbot API endpoints. Live mode deliberately exposes **one**
configured workspace. The current backend does not yet have authenticated
multi-workspace discovery or durable chat sessions. Demo workspaces remain
available exclusively at `/?demo=1` on a development build.

1. Copy `.env.example` to `.env.local` and replace **every** placeholder with
   actual, private operator values. `POTBOT_UI_PASSWORD` is the local UI login;
   `POTBOT_UI_COOKIE_SECRET` should be independently random (32+ characters).
2. `POTBOT_UI_PUBLIC_ORIGIN` must match exactly how you open Next.js (for
   example `http://127.0.0.1:3100`). A non-loopback public origin requires
   HTTPS. Cookie sessions last eight hours, are HttpOnly and SameSite=Strict.
3. `POTBOT_UI_BACKEND_URL` is restricted to an explicit HTTP **loopback** `/`
   origin (for example `http://127.0.0.1:8000`); forward a remote backend via a
   trusted tunnel or reverse proxy instead of weakening the server's URL policy.
4. `POTBOT_UI_BACKEND_TOKEN` must be a **workspace-scoped** backend credential;
   `POTBOT_UI_WORKSPACE_ID` must be its actual workspace. The existing backend
   does not provide an identity-introspection endpoint, so configuration must
   be checked by the operator. Never put this token into `NEXT_PUBLIC_*`.
5. Start Potbot's service and then `npm install && npm run dev` in `frontend/`.
   Open `/`, authenticate with the operator password, and submit a message.

The browser calls authenticated same-origin Next.js `/api/ui/*` routes, never
Potbot directly. Next forwards the normal OpenAI-compatible chat stream and
safe run ID, then polls the existing `/v1/reasoning/{run_id}` snapshot and
paginated events. The graph is a **sanitized projection** containing only node
labels, kinds, state, edges and revision; raw results, prompt hashes and claim
tokens are not forwarded. Sequence gaps reset the cursor and replay from the
current checkpoint. Polling is throttled while the graph is minimized or
when the run is terminal. The default graph is still 3D WebGL with an accessible
node-list fallback.

Chat context is maintained in this **browser tab only**; refreshing the page
or signing out clears it. This is not a persisted chat-session service. A lost
connection after dispatch has an **uncertain** outcome and is **never**
automatically resubmitted. The existing steering endpoint is not automatically
used for new chat messages, since the UI currently lacks a durable session and
explicit steering-intent workflow.

### Security and deployment boundaries

This is a **single-operator** bridge, not multi-user authentication. It rejects
cross-origin mutations, validates the cookie HMAC and lifetime, enforces a
small in-process login-attempt limit, constrains request sizes and workspace
IDs, disables upstream redirects, and never returns server secrets to the
browser. Deploy behind TLS and an **external rate limiter**; the in-process
limiter is not sufficient for multi-process or Internet-facing authentication.
Do not expose this developer-facing bridge publicly until an account-aware,
workspace-authorized identity provider and end-to-end security review are added.

### Tests

```bash
npm test                          # offline, built-in Node 22 tests
npm run typecheck                 # requires npm dependencies installed
npm run build                     # requires npm dependencies installed
```

A mocked integration suite covers login, origin enforcement, credential
isolation, workspace bounds, chat streaming, safe graph projection and cursor
handling. A real Potbot service is needed for end-to-end browser acceptance.

## UI-4 — graph workbench, event timeline, rendering telemetry

The floating and fullscreen graph now share a searchable inspector. Filter
nodes by recorded state or kind, search by public node label or identifier,
see ancestor context retained for search hits, and select any node using a
keyboard-accessible, paginated list. A **List** toggle unmounts the WebGL canvas
without losing selection or filters. Graph search, view mode and the diagnostics
preference are stored locally per workspace; account-level sync still requires
an authorized backend preferences service.

The live event timeline displays the actual sequence and type fields returned
by `/v1/reasoning/{run_id}/events`. The Next.js BFF **never forwards raw event
payloads**, task secrets or claim tokens. Its 240-entry bounded history detects
cursor gaps, reports incomplete source history and resumes from the most recent
available cursor rather than looping through missing events. Potbot currently
does not supply event timestamps here, so the UI does not fabricate them.

**Perf** opens a diagnostic overlay showing instanced-node and line counts,
scene-build wall time, synchronous render CPU time, capped pixel ratio, draw
count and rough graph-buffer estimate. These figures are **not GPU timings,
frame rate or measured peak VRAM**. The Three.js canvas draws on demand and
selection changes reuse the existing scene, changing instance attributes and
camera position rather than rebuilding the geometry. WebGL context loss falls
back to an accessible node list. `npm run benchmark:graph` provides a separate
CPU-only 1,500-node query and timeline diagnostic.

The Preferences window now keeps the local compact setting active even after
the window is closed, offers accessible graph list mode and graph diagnostics,
and links directly to the dockable Downloads and Model Installations windows.
Those two windows still explicitly report **backend telemetry unavailable**.
No made-up transfer progress or installation status is displayed.

### Validation

```bash
npm test
npm run benchmark:graph
npm install --no-audit --no-fund
npm run typecheck
npm run build
```

The UI-4 patch also removes the `tabAnimation` Dockview prop, which was rejected
by the pinned `dockview-react` TypeScript contract in the preceding CI run.
The full dependency typecheck, Next production build, and a real-browser WebGL
benchmark still require npm installation in the target/CI environment.

## Native Xpra-only application windows (UI-5)

Potbot uses **one Xpra-owned X11 display per workspace** and the pinned
upstream Xpra HTML5 protocol/codec engine with *no visible Xpra desktop
chrome*. Each application window is an independently draggable/dockable
Dockview canvas. The operator's keyboard, pointer, focus and resize commands
are sent back via the same Xpra binary WebSocket protocol, not via XTEST.
No independent Xvfb server or PNG screenshot polling path exists.

Prepare the exact pinned HTML5 client before running Next.js:

```bash
python frontend/scripts/prepare-xpra-html5.py \
  --upstream external/xpra-html5 --output frontend/public/xpra-html5
```

The private backend relay and the Next.js server require the Xpra token and
public WebSocket URL described in
[`docs/ui/xpra-seamless-integration.md`](../docs/ui/xpra-seamless-integration.md).
Real sandboxed XBill/Dockview acceptance remains opt-in until qualified.

## UI-6 — efficient Xpra damage surfaces and per-window diagnostics

The Xpra HTML5 decoder still owns the native protocol, codecs and X11 input.
Potbot now copies only coalesced changed rectangles from each decoded *window*
canvas into Dockview. Scrolling, resizing, unknown packets and damage exceeding
70% of the window use a full-window baseline instead. Each frame includes the
Xpra window generation and a monotonic sequence, preventing late updates from
repainting a recycled X11 window identifier.

Minimizing an application pane suspends `ImageBitmap` copies without killing its
X process; reopening requests a complete frame before incremental updates.
Window input uses Xpra's native key/pointer/focus methods. Resize is debounced,
and wheel events use a non-passive listener to avoid scrolling the page.

Each pane displays an approximate rendered FPS, count of incremental patches and
cumulative decoded pixel-copy volume. An unsupported bitmap decoder reports an
error with a manual retry control; it does **not** spin indefinitely. These
measurements are not compressed stream bandwidth, measured GPU memory or
end-to-end remote interaction latency.

```bash
npm test
npm run benchmark:xpra
npm run typecheck
npm run build
```

The CPU-only synthetic damage benchmark shows theoretical pixel-copy savings,
not actual browser/GPU FPS. Live acceptance still needs the Xpra/XBill and
Playwright-equipped host described in the Xpra integration guide. This patch
also fixes two Xpra CI failures: a TypeScript control union and cleanup of
orphaned sockets after an owned Xpra process exits.

### UI-7 — Workspace Xpra application manager

Click **Manage Xpra application windows** in the utility rail or open
**Xpra applications** from the menu. Search by title or X window ID, filter
Visible / Minimized, focus an existing window or restore one that was hidden.
Each pane remains dockable and resizable; the **Minimize** action never kills
the application. If the Xpra session is disconnected, the panel offers a manual
**Reconnect Xpra** control after automatic retries are exhausted.

Window footers report approximate decoded-copy volume, observed frame rate,
P95 client-side frame gap, and input-to-next-frame **proxy** latency. Do not
interpret these as measured GPU FPS, bandwidth or verified input acknowledgements.

```bash
cd frontend
npm test
npm run benchmark:windows    # CPU only: 500 synthetic application windows
npm run benchmark:xpra       # CPU only: synthetic damage rectangles
npm run typecheck
npm run build
```

The opt-in Linux acceptance workflow adds a second application (`xterm`) next
to XBill, verifies both become separate Dockview windows, sends input to each,
then minimizes/restores xterm while its sandbox process continues to run.
This requires a provisioned Linux Xpra host and is not simulated as passing.

## UI-8 — Real workspace activity and model catalogue

The **Workspace activity** window and preferences pages now read actual
workspace-scoped Potbot API data through authenticated, server-side Next.js
routes. Neither a fake progress bar nor raw job inputs/provider metadata are
forwarded to the browser. A job status is not a download status:

- `GET /api/ui/activity?after=N` reads the workspace's `/v1/jobs` and
  `/v1/jobs/events`, returning up to 50 job summaries and 100 sequence-ordered
  event types; payloads, prompts, lease tokens and service keys are discarded.
- `GET /api/ui/models` reads `/v1/models` and projects only advisory model
  identities, roles and qualification/configuration flags. **This is not an
  installed-model inventory.**
- Activity has a dockable window, search, state filter, manual refresh and
  8-second live polling while the tab is visible. Panels remain workspace-scoped.
- The Downloads pane continues to identify transfer telemetry as unavailable:
  the backend has a CLI downloader with `DownloadProgress`, but no authorized
  transfer-status HTTP API. The Model Installations pane displays real catalogue
  entries, but never invents install completion or current disk consumption.
- Terminal and Browser panes can open Xpra application windows that the
  workspace has **already launched**; backend PTY/browser launch and session
  reattachment are separate pending capabilities.

This intentionally leaves the missing API work visible in the UI. Backend
contracts proposed in `docs/ui/frontend-backend-contract.md` identify the next
server-side milestone without conflating planned endpoints with implemented ones.

## UI-10: multiple workspace connections and saved conversations

The operator shell can now connect to up to 12 individually authenticated
Potbot workspace services using the server-only `POTBOT_UI_WORKSPACES_JSON`.
The selected workspace orb switches the backend credential and independent
PostgreSQL-backed chat history. The chat header offers a conversation picker,
New Conversation and explicit pending-turn reconciliation. Backend migration 27
must be applied before enabling these routes. This is **not** a way to access
another workspace using a bearer token from a different workspace.

See [the UI-10 implementation and security contract](../docs/ui/ui10-durable-workspace-chat.md)
for configuration, endpoint details, persistence/recovery limits and the next
backend milestones. Single-workspace environment variables remain supported.

### UI-11 — Qualified Xpra application sessions

The Xpra application manager now exposes an operator-pinned Launch menu and
running-process sessions. Bash/xterm and browser panels may start approved
recipes. Closing a Dockview window never terminates the application; an
explicit Stop action does. Application sessions are scoped to the workspace and
persist across Next.js reconnections while the Xpra operator remains running.
Launches stay **disabled** without a pinned rootfs, live Xpra socket, qualified
Linux Bubblewrap/AF_UNIX IPC and operator-private `--apps-policy`.
See `docs/ui/ui11-xpra-application-sessions.md`.

For multiple concurrent Xpra workspaces, set the server-only `POTBOT_UI_XPRA_BRIDGES_JSON`
array with separate scoped bridge tokens and endpoints. Legacy single-workspace
`POTBOT_UI_XPRA_BRIDGE_*` settings are still accepted.
