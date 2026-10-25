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
