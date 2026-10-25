# Potbot UI-5: Xpra-only seamless per-window transport

**No independent Xvfb, Xlib frame grabber, XTEST input injector, PNG polling,
or remote desktop framebuffer is part of this implementation.** Xpra owns the
workspace X11 display. A native Xpra WebSocket stream carries window creation,
metadata, compressed damage/codec frames, keyboard, pointer, focus and resize.
Dockview is the **only visible window chrome and docking system**.

## Architecture and isolation

```
Workspace / sandbox A (one shared DISPLAY)
  ├── repository 1 / tools ──┐
  ├── repository 2 / tools ──┴──> Xpra seamless Xorg/Xdummy :NNN
  └── other GUI processes            │ private X11 UNIX socket, MIT cookie
                                     │
Private Xpra UNIX protocol socket (host only, never sandbox-mounted)
      │
Potbot Xpra relay (localhost, short-lived one-use ticket)
      │ binary WebSocket packets (no frame decode on server)
      │
Xpra HTML5 protocol + decoder (one hidden sandboxed same-origin iframe)
      │ window events + ImageBitmap transfer  |  ^ pointer/key/focus/resize
      └──────────────────────────────┴──► Potbot Next.js Dockview
                                           ├── XBill window
                                           ├── xterm window
                                           └── any other mapped application
```

Xpra internally manages its Xorg/Xdummy backend; that is expected. Potbot
itself does not launch a separate display server. We expressly select
`--use-display=no` and fail if the session cannot create the allocated X11
socket. Multiple repositories in the same Potbot workspace **share** its
single session. The Xpra protocol socket is host-private; only the workspace's
X11 socket and a read-only Xauthority copy enter an admitted sandbox. The
mount plan is a proposal requiring the existing qualified sandbox gate; it is
not automatically mounted into arbitrary processes.

The display allocator fences workspace ownership with exclusive lock files and
refuses ambiguous existing X sockets. Xpra starts without a TCP listener or
remote launching, with transfer, printing, clipboard and audio disabled.
Startup fails closed instead of substituting another backend. A clean shutdown
waits for its owned process group, removes only its own orphaned private Unix
socket entries, archives the server log and releases locks; foreign files and
ambiguous stale session records still require operator reconciliation.

## HTML5 client adaptation and licensing

Pinned upstream: [`Xpra-org/xpra-html5` at `3752e59b7bb3120a5f20794ce754b3b76ac96c4b`](https://github.com/Xpra-org/xpra-html5/commit/3752e59b7bb3120a5f20794ce754b3b76ac96c4b)
(Mozilla Public License 2.0). The exact upstream source files retain their
copyright and licence; Potbot adds `frontend/vendor/xpra-html5/potbot-window-bridge.js`
under the project's normal source licence policy. Preserve all upstream notices
when distributing a build.

The **build-time patch** is `frontend/scripts/prepare-xpra-html5.py`. It checks:

- immutable git HEAD and no uncommitted tracked modifications;
- method interfaces from the actual upstream `Client.js` and `Window.js`;
- exact, unique anchors in upstream `index.html`, failing rather than guessing
  after upstream refactors.

It then copies upstream decoder/protocol modules to ignored
`frontend/public/xpra-html5/` and patches only its entrypoint: it attaches our
window-only bridge before connecting, hides its menu, titlebars, window
controls and visible desktop, disables transfer/clipboard/audio features and
redirects disconnected sessions to the parent UI rather than Xpra's login
page. The upstream decoder still maintains **offscreen** per-window canvases
because those are required to assemble incremental damaged regions before
exporting frames. No upstream window borders appear in Potbot. We deliberately
preserve Xpra's tested codec, keyboard mapping and X11 protocol implementation
rather than rewritting them. `UPSTREAM-REVISION` and `LICENSE` are bundled.

The client processes native Xpra compressed damage and decodes through upstream
WebWorkers/codec modules. The Potbot adapter observes successful native
`XpraWindow.do_paint()` callbacks and coalesces overlapping damage bounds. It
transfers only the affected crop using `createImageBitmap(canvas, x, y, w, h)`
with a per-window in-flight cap and approximately 30 FPS scheduling. Large,
unknown, scroll or resize damage falls back to a **complete window** transfer,
never a virtual-desktop framebuffer. The Dockview pane maintains a persistent
2D canvas: full frames establish its baseline, subsequent damage rectangles
update only their local region. Generation and sequence checks discard stale
frames after ID reuse or reconnection. Minimized panes suspend extra bitmap
copies and demand a new full baseline when restored.

The window toolbar exposes per-window displayed FPS, incremental-frame counts
and estimated **decoded browser pixel copy volume**. These are not GPU usage,
encoded network bandwidth or end-to-end frame latency measurements. A separate
synthetic, CPU-only diagnostic is available with `npm run benchmark:xpra`.
Actual frame rates, browser GPU memory, decoder throughput, resize behavior,
input latency and multiple game windows remain subject to Playwright acceptance
on a real Xpra/Xdummy host. Future work may use native GPU texture handles or
shared worker buffers to reduce the browser-side copy further.

### Prepare the pinned source

```bash
git clone https://github.com/Xpra-org/xpra-html5.git external/xpra-html5
git -C external/xpra-html5 checkout 3752e59b7bb3120a5f20794ce754b3b76ac96c4b
python frontend/scripts/prepare-xpra-html5.py \
  --upstream external/xpra-html5 --output frontend/public/xpra-html5
```

**Do not** commit the generated upstream tree to Potbot. It is pinned and
rebuilt on CI or operator setup. It must be built before opening the Xpra
window interface.

## Native workspace session

Install Xpra 6.3+ with Xdummy/Xorg support, `xauth`, and Potbot's `web` extras
on the Linux execution host. No Xvfb dependency is required.

```bash
export POTBOT_XPRA_BRIDGE_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(44))')"
PYTHONPATH=src python -m potbot.tools.x11_workspace_server \
  --workspace-id YOUR-POTBOT-WORKSPACE-UUID \
  --sandbox-root /absolute/provisioned/workspace \
  --browser-origin http://127.0.0.1:3100 \
  --port 8765
```

The process emits display metadata. `session.json` provides the exact X11
socket and Xauthority paths for the sandbox runner. Expose the display only to
processes authorized within the same workspace. The sandbox's `DISPLAY` and
`XAUTHORITY` are set according to `sandbox_mount_plan()`. Never mount the Xpra
protocol Unix socket into sandbox processes.

Next.js `frontend/.env.local` requires, in addition to normal UI-3 settings:

```dotenv
POTBOT_UI_XPRA_BRIDGE_URL=http://127.0.0.1:8765/
POTBOT_UI_XPRA_BRIDGE_TOKEN=THE_SAME_PRIVATE_SERVICE_BEARER
POTBOT_UI_XPRA_PUBLIC_WS_URL=ws://127.0.0.1:8765/v1/xpra/ws
```

Only the UI server sees the private bearer. The authenticated same-origin Next
route `POST /api/ui/xpra/ticket` mints a 30-second, one-use capability from the
private relay. Browser WebSocket origin, ticket expiry/replay and relay size
limits are checked. For an HTTPS public origin expose the relay through an
authenticated TLS WebSocket reverse proxy, specify `wss://.../v1/xpra/ws`, and
preserve the original browser Origin for verification. A WebSocket ticket is a
capability and must not be logged or reused. Do not deploy this single-operator
bridge as a general public multi-user proxy before additional authentication
and access-control review.

Closing a Dockview pane hides the view but leaves its application/process
running in Xpra. Reconnecting after an interrupted browser session requests a
new ticket. The backend continues independently; uncertain GUI input delivery
is never replayed automatically.

## Verification and acceptance

- Python: `PYTHONPATH=src python -m pytest -q tests/integration/test_xpra_native_bridge.py tests/integration/test_xpra_vendor_contract.py`.
- Frontend: `cd frontend && npm test && npm run benchmark:xpra && npm install && npm run typecheck && npm run build`.
- Pinned upstream compatibility: GitHub Actions workflow
  `xpra-native-window-acceptance.yml` checks out the precise upstream SHA,
  patches it, validates native hooks and runs frontend typecheck/build.
- **Real Xpra / XBill / Bubblewrap browser acceptance (opt-in):** on a qualified
  Linux host, prepare the above HTML5 source, install Xpra 6.3+, `xbill`,
  `bwrap`, `xauth`, Node dependencies, Python Playwright and its Chromium
  browser. Then run:

```bash
POTBOT_RUN_REAL_XPRA_XBILL=1 PYTHONPATH=src \
  python tests/acceptance/xpra_xbill_dock_smoke.py
```

The acceptance harness creates an isolated workspace, launches Xpra and the
XBill executable through **Bubblewrap** with private X socket and Xauthority,
starts the frontend in live mode, then checks for an XBill Dockview pane, an
actual decoded canvas, and keyboard/pointer input. It keeps a screenshot in
`artifacts/xpra-xbill-dock.png` when successful. It refuses missing isolation,
real binaries, HTML5 assets or browser dependencies; no mocked images count as
an end-to-end pass. CI's real-browser acceptance job is only manually runnable
on an explicitly provisioned `self-hosted, linux, xpra` worker. It is **not
claimed to have passed locally** without that host.

## Remaining optimizations and qualification

1. Verify Xpra 6.3+ launcher options, Xorg/Xdummy resources and Unix WebSocket
   upgrade against a real packaged installation; cannot be replaced by a mock.
2. Optimize decoded damage-region transfers and benchmark FPS/CPU/GPU/memory on
   1, 5, 20 simultaneously updating X applications at several resolutions.
3. Qualify keyboard layouts, non-US IME, drag-and-drop, primary clipboard opt-in,
   multi-monitor geometry and resizing under high DPI.
4. Add a full workspace-session broker/reconnect epoch to prevent stale window
   IDs after server restart; currently one configured authorized workspace.
5. Review TLS/reverse-proxy routing, origin/CSP and browser iframe boundaries
   before untrusted remote access.

## UI-7: multiple docked application windows and frame pacing

The Xpra connection is **one per workspace**, not one per Git repository. A
new dockable **Xpra applications** panel lists all known windows, including
ones minimized in Dockview. Closing a pane preserves its X11 process;
restoring the pane requests a complete frame before accepting damage patches.
The utility rail displays a window count and an entry into the switcher.
When the WebSocket disconnects, the client preserves the inventory, displays
connection status, and offers **Reconnect Xpra** after bounded retries.

Missing incremental bitmap sequences trigger a resynchronization request.
Per-window FPS, P95 frame intervals and input-to-next-frame timings are
**client-side approximations**. These samples cannot prove server input
receipt and should not be used as authoritative remote-control latency.

The real multiwindow acceptance is `tests/acceptance/xpra_xbill_dock_smoke.py`.
It requires both XBill and xterm in a bubblewrap sandbox and asserts separate
Dockview canvases, pointer/key forwarding, and recovery of a minimized xterm
without stopping the process. Enable the manual `run_xbill_browser` workflow
only on an authorized preprovisioned runner labeled `xpra`. This test has not
been executed in a local no-Xpra environment; it must be run on the actual host.

The Xpra restart shim removes only a socket inode previously observed and
owned by the same session after clean termination. Unknown files in private
socket folders remain a **fail-closed** error requiring operator inspection.
