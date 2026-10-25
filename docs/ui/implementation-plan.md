# Next.js frontend implementation plan

## Completed in UI-1/UI-2 foundation

- Orbital workspace ring with active centre, upward offscreen exit, reveal zone,
  keyboard traversal, per-workspace visuals and reduced-motion support.
- Default full-frame chat panel with responsive composer. The preview does not
  impersonate Potbot inference.
- Dockview floating menu, graph, browser, terminal, preferences, downloads and
  installation panes; drag docking, edge controls and per-workspace local layout
  persistence. Menu cannot maximize; graph has an application fullscreen overlay.
- Default-enabled 3D graph preview only in explicit demo mode. Three.js renders
  instanced nodes and batched links on demand; closing graph minimizes it to a
  distinct utility orb for restoration.
- Terminal launched from composer into an initially near-composer floating pane.
  No process starts because the authenticated PTY contract is not yet present.
- Backend contract and rendering/performance research pack; no Python files
  changed by this increment.

## Work still missing (must be tracked in the backend/frontend split)

### Backend track

1. Authenticated multi-workspace discovery, access checks and active session list.
2. Chat message/session persistence and generation stream with cancellation and
   typed **urgent/deferred/background** steering acceptance.
3. Typed reasoning graph projection and SSE/WebSocket replay with gaps, stable
   IDs, versioned causality, public summaries, scope and token/cost metadata.
4. Workspace-scoped, reattachable PTY terminal with resize, flow control,
   transcript permissions, close versus terminate and shell isolation.
5. Controlled browser session creation, viewport streaming, input, auth, status,
   reconnect and allowed-origin restrictions. No arbitrary third-party iframe.
6. User preferences, workspace layouts, model catalogue/quantization selection,
   installation jobs, downloads, progress bytes, verification, cancellation.
7. Service startup wiring (UV+Node bootstrap, frontend process lifecycle,
   reverse proxy, TLS/auth/session management) and formal API schema tests.

### Frontend track

- UI-3: connect graph projections and cursor replay, add search, selection,
  time filtering, 2D accessible fallback and renderer benchmarks.
- UI-4: actual xterm.js terminal with streaming, resize/reconnect and controls.
- UI-5: browser frame/controls with responsive canvas or video streaming.
- UI-6: account preferences, real download/install events and user notification
  centre; cross-device persisted layouts once authorized.
- Authentication flow, streaming chat/history, error boundaries, retries, robust
  cancel/steering UX, keyboard focus management and accessibility audits.
- Cross-browser Playwright E2E scenarios: orb switching, menu never maximize,
  Dockview dragging to each edge, graph close→orb→restore, graph maximize,
  workspace-specific layout isolation, terminal composer reveal, and no fake
  progress in disconnected backend mode.

## Acceptance and instrumentation

- Run Node pure TypeScript tests for orbital positioning, saved layouts, graph
  projection caps and deterministic coordinates.
- Install dependencies using `npm install`, commit resulting lockfile, run
  `npm run typecheck` and `npm run build`. These **weren't possible in the
  patch's offline build container**, so do not mark them green yet.
- Measure orbit animation under real browser conditions, first graph render,
  idle draw calls, 1500-node interaction, data refresh and 100 workspace swaps.
- Repeat with reduced motion, WebGL unavailable, keyboard-only controls, small
  viewports, malformed localStorage and lost WebSocket connections.

## Branch ownership

- Frontend chat owns `frontend/**`, `docs/ui/**`, and UI E2E tests.
- Backend chat owns `src/potbot/**`, migrations, session/terminal/browser APIs.
- The UI/backend contract is versioned collaboratively and reviewed before
  either track changes the event shapes.
