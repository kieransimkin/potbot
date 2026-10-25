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
