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

## UI-3 implementation increment — frontend-only live bridge

Completed locally, pending CI/browser acceptance:

- Server-only signed HttpOnly operator-session cookie, same-origin login/logout,
  bounded requests, explicit loopback backend URL, and configured workspace
  scope. This is **not** yet a production-grade multi-user login service.
- Browser-to-Next-to-Potbot streaming chat bridge using the backend's existing
  OpenAI completion SSE contract and `X-Potbot-Run-Id`. Chat history is
  explicitly ephemeral; no auto-retry after uncertain inference.
- Reasoning checkpoint read plus paginated event polling, whitelisted node/edge
  projection and monotonic event cursor detection. Polling throttles for
  minimized or terminal graphs, preserving WebGL resources on no-op updates.
- Accessible inspectable node list and simplified non-WebGL fallback.
- Node-only mocked route and stream/parser tests, independent of npm packages.

Next before UI-4:

1. Connect Python backend's multi-workspace discovery and independently
   authorized chat-session persistence. Replace configured-workspace fallback
   only when the backend authenticates principal/workspace membership.
2. Add typed graph projection and push/replay API (SSE or WebSocket) instead of
   repeated state+event polling. Include sequence/epoch resync and evidence
   provenance without exporting hidden reasoning.
3. Run real Next.js dependency build and Playwright tests on Windows/Linux,
   including login/logout, live chat streaming, connection loss, graph events,
   browser navigation, focus, and high-DPI WebGL performance. Current execution
   environment cannot install npm packages, so build status is unverified.
4. Implement and independently security-audit workspace PTY, browser sessions,
   persistent settings, downloads and model-install event APIs on backend track;
   connect their frontend windows only as each resource becomes available.
5. Add identity-provider integration, CSRF/origin audits, shared distributed
   login rate limits, reverse-proxy TLS, hardening and account/session revocation
   before deploying UI outside a trusted operator environment.

## UI-4 delivered — graph search, event timeline and viewport diagnostics

- Shared graph workbench for floating and fullscreen views, including
  search, type/state filters, selected-node inspector and optional accessible
  paged list. Text searches retain ancestors for visual context but do not
  treat those ancestors as independent search matches.
- Workspace-scoped view preferences remain active after closing panels.
  Accessible list mode unmounts WebGL; perf diagnostics report CPU-side
  measurements and explicit estimates rather than inferred FPS/VRAM.
- `GET /api/ui/reasoning/{runId}` now returns the already-validated, bounded
  `events` collection with **sequence/type only**. The UI stores up to 240
  recent events in memory per workspace/run. Cursor gaps do not create a
  silent success condition; the view reports incomplete source history.
- CI includes a CPU-only query benchmark; previous unsupported Dockview
  `tabAnimation` prop removed. Backend sources and credentials remain untouched.

### Follow-on UI work (do not conflate missing backend progress with success)

1. Add real PTY state/resize/reattach controls to the composer-launched terminal
   only after the backend supplies authenticated workspace PTY sessions.
2. Add streamed browser viewport/control when the backend publishes its
   browser-session and origin/permission contracts; no arbitrary iframe.
3. Wire activity panels to scoped backend downloads and model install job
   events with bytes, verified bytes, speed, checksum state, failures,
   pause/resume/cancel rights and reconnect cursors. Keep placeholders until
   those APIs actually exist.
4. Adopt durable multi-workspace discovery, user preferences, chat/session
   storage, and typed reasoning graph SSE/WebSocket events after backend
   endpoints are accepted. The current connection remains one configured
   authorized workspace plus explicit synthetic demo mode.
5. Run cross-browser Playwright tests for graph selection, redraw diagnostics,
   dock ↔ float, fullscreen focus trap, preference persistence, reduced
   motion, touch navigation and a large graph under actual browser GPUs.

## UI-5 — native Xpra-only seamless application windows

- Potbot's workspace Xpra server owns the private virtual X11 display; no
  standalone Xvfb lifecycle or XTEST/PNG compatibility bridge remains.
- Xpra's pinned, MPL-2.0 HTML5 protocol/codec client runs in one hidden
  same-origin iframe and forwards individual decoded window `ImageBitmap`
  updates to Dockview-owned surfaces. Its own window borders, toolbar and
  menus are not displayed; real X11 input/focus/resize returns through Xpra.
- The relay accepts only short-lived single-use tickets minted by the
  authenticated Next server and checks the browser Origin. The workspace X
  socket and Xauthority are sandbox-mountable; the private Xpra protocol
  socket is never exposed to sandbox tools.
- Real Xpra startup, XBill in bubblewrap, and the resulting Docker/browser
  dock view require a qualified acceptance host. CI runs pinned upstream
  source-contract and bridge tests automatically; actual XBill/Playwright
  runs are manual on a preprovisioned self-hosted runner.
- Further work: damage-region GPU updates, diverse keyboard/IME support,
  multiple display reconnect epochs, bandwidth memory benchmarking, and
  audited TLS deployment.

See [`xpra-seamless-integration.md`](xpra-seamless-integration.md) for the
complete setup, pin, contract and end-to-end test plan.

### UI-6: Native Xpra damage-region and window-controls pass

- Replaced whole-window decoded ImageBitmap export on every paint with per-window
  coalesced, bounded damage rectangles and a full-window fallback for resize,
  scroll and unusually large damage. No PNG polling, XTEST or Xvfb path returns.
- A Dockview pane must receive a full frame after attach/reconnect before
  accepting cropped updates; frames carry per-window generations and sequence
  numbers to prevent stale updates after X window ID reuse.
- Minimized panes disable bitmap copies; restored panes request full resync.
  Pointer focus, window close and keyboard release remain owned by Xpra.
- ResizeObserver is debounced and wheel input uses a non-passive listener.
- Visible Xpra windows display FPS and approximate decoded-pixel copy volume
  without claiming actual GPU utilization or network bandwidth.
- Fixed an existing frontend TypeScript discriminated-union import/narrowing
  failure, and cleaned up *owned* private Xpra Unix sockets after shutdown so
  clean workspace restarts work under CI.
- **Pending:** full pinned HTML5 production build, actual browser WebGL/Canvas
  frame-rate profiling, real Xpra/XBill dock-and-input acceptance and
  multi-workspace backend discovery/session storage.

### UI-7 — Xpra application manager, window lifecycle and measured diagnostics

- Add a dedicated dockable **Xpra applications** window, accessible from the
  menu and from a bounded utility-rail entry. Search and filter all windows by
  title, X window ID and minimized/visible state; the panel is a projection
  only and never kills applications. Minimize state remains workspace-scoped.
- Keep Xpra binary socket admission and private authentication unchanged.
  Restore a minimized pane by reattaching its canvas and requesting a full
  frame. Missing incremental sequence numbers request a full resynchronization
  rather than drawing an incompletely updated image.
- Add an explicit **Reconnect Xpra** action when automatic reconnect attempts
  are exhausted. The client continues to show known windows while offline,
  but does not claim that disconnected applications are currently paintable.
- Add per-window rolling P95 frame interval and median input-to-next-frame
  timings as **browser-side proxies**. An unrelated X11 animation can satisfy
  the proxy, so it must not be reported as acknowledged input latency. Actual
  input-to-painted-frame round-trip requires server-side correlation IDs.
- The opt-in self-hosted acceptance script now opens **both XBill and xterm**
  in the same private workspace Xpra display and tests two separate Dockview
  windows, input routing, minimize/restore, surviving processes and screenshot
  capture. It does not run on ordinary GitHub-hosted runners. It still requires
  a qualified Linux Xpra 6.3+ host, bubblewrap, Chromium and test X11 packages.
- The frontend CI additionally runs a synthetic 500-window search benchmark;
  this is **CPU time only**, not browser GPU FPS, Xpra transport bandwidth or
  measured window switching latency.
- Fix UI-6 TypeScript narrowing errors involving asynchronous React state
  updates and the Xpra window ID; the next CI run must confirm `tsc --noEmit`.

**Still pending:** Xpra input-to-frame correlation acknowledgements, per-window
icon extraction from upstream Xpra icon events, qualified compositor/WebGL FPS,
IME/multi-key layouts, audited remote TLS deployments, and the persistent
multi-user workspace identity contract. UI preferences, model installations,
progress bars, downloads, PTYs and browsing sessions remain backend-dependent:
these views should never fabricate active jobs, bytes or install success.

## UI-8 — Workspace activity and Xpra closeout

**Implemented in the frontend increment:**

- Read-only, operator-authenticated `/api/ui/activity` with normalized real
  `/v1/jobs` and `/v1/jobs/events` status, progress stages and ordered cursors.
  Sensitive event payloads, job inputs, lease tokens and result bodies stay on
  the backend. No automatic replay follows an indeterminate job.
- A dockable activity window with search, active/finished filters, status,
  recent job event types and explicit cursor-gap warnings.
- Read-only `/api/ui/models` with the actual model catalogue and advisory
  qualification flags. A listed/qualified *profile* isn't proof that weights
  are installed or fit available VRAM.
- The downloads and installation panes no longer just point to future plans:
  they link to real activity/catalogue information while explicitly marking
  unsupported transfer/install telemetry and side-effecting controls absent.
- Terminal/browser panels can reopen existing Xpra application windows.
- Xpra self-hosted XBill+xterm acceptance strengthened to require actual
  nonuniform decoded game pixels rather than merely a created canvas.

**Still required from the backend/qualified host:**

1. `/v1/workspaces` and durable scoped chat session discovery. Current live
   UI serves one explicitly configured workspace identity.
2. `GET /v1/transfers`, cursor-based events and authorized transfer actions
   exposing source identity, bytes written, verified bytes, rate, resumability,
   content hash, failure and cancellation. Existing CLI downloader callback is
   NOT already exposed as an HTTP progress stream.
3. Workspace model installation lifecycle: pinned artifact, quantization,
   exact file size/checksum, measured RAM/VRAM, readiness, install job ID and
   its scoped event cursor. Advisory `/v1/models` is not that contract.
4. Authorized PTY and browser **launch/reattach** APIs. Xpra handles desktop
   window rendering/input for already-running apps but cannot grant launch
   permissions to untrusted chat clients.
5. Run the opt-in real-Xpra, Bubblewrap, XBill, xterm and Chromium acceptance
   workflow on a preprovisioned self-hosted runner. The normal CI contracts
   do not prove that real Xpra rendering passed.
6. Preserve user appearance settings in a scoped backend user-preferences
   endpoint with revision/etag conflict handling for cross-device sync; today's
   controls save local workspace preferences only.

The latest frontend CI issue following UI-6 was the Xpra-control union type and
import narrowing; the UI-7 changes included here address that, but a fresh
CI run is required for acceptance.
