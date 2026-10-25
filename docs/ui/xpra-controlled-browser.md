# Xpra controlled browser, DevTools picker and accessibility-first automation

## Contract

This patch adds an **operator-admitted `controlled-browser` GUI recipe**, using
one Chromium process tree within the already-qualified Xpra/Bubblewrap workspace.
Xpra continues to handle the native per-window display and human keyboard/mouse
input. The additional command channel controls the **same Chromium** through
Playwright or Patchright, plus **allowlisted CDP inspection and input** calls. Optional
`playwright-stealth` is selected by the operator at launch, not implicitly enabled
for every agent. No Xvfb, VNC desktop or global CDP TCP port is introduced.

### Operator recipe

Add the following to the private application policy's `recipes` array after the
qualified image is installed, signed and its digest activated:

```json
{"kind":"controlled-browser", "title":"Controlled Chromium",
 "argv":["/opt/potbot/browser/bin/python", "/opt/potbot/browser_worker.py", "--engine", "playwright"]}
```

Alternatively select `"patchright"` and append `"--stealth"` to the **operator**
argv. The binary, worker and Python dependencies are built into the immutable
sandbox rootfs by `sandbox/image/Dockerfile`, not run on the host. Updating the
image requires normal qualification and pin rotation. Both Playwright and
Patchright use bundled drivers against the distro Chromium; perform an actual
version-compatibility smoke test on the target image before production.

**Network access remains denied** by the existing sandbox's unshared network
namespace and seccomp rules. This patch works against `about:blank` and local
`file:///home/...` test pages. Arbitrary `https://` navigation is accepted as
a navigation request but cannot succeed until a separately scoped, qualified
network proxy is added; the bridge never falls back to host networking. This is
not a stealth or CAPTCHA-bypass guarantee.

### API

Authenticated operator traffic, same workspace lease:

`POST /v1/xpra/apps/{sessionId}/browser` through the local Xpra relay (bearer),
proxied by Next at `/api/ui/xpra/apps/{sessionId}/browser` (operator cookie,
same-origin mutation, `x-potbot-workspace`). Body size <=4096 bytes; results
<=1.5 MB. Commands:

| `op` | Inputs | Behavior |
|---|---|---|
| `state` | - | Driver, page title/URL, picker state, failed-request paths |
| `navigate` | `url` | URL navigation, file only under `/home` |
| `accessibility` | - | Playwright `page.aria_snapshot()` screen-reader representation |
| `interactive` | - | Bounded keyboard-focusable/semantic controls and rects |
| `role.click` | `role`,`name` | Require unique accessible role/name then click |
| `role.fill` | `role`,`name`,`text` | Require unique accessible input then fill |
| `input.pointer` | `action`,`x`,`y` | Browser-native mouse move/click/wheel |
| `input.keyboard` | `action`,`key` or `text` | Browser-native press/type |
| `inspect.start` | - | DevTools `Overlay.setInspectMode` in native Xpra Chromium |
| `inspect.poll` | - | Selected DOM node + CSS sources/lines, events, box, AX |
| `inspect.stop` | - | Disable picker and remove native highlight |
| `inspect.at` | `x`,`y` | DOM hit-test at page viewport coordinates |
| `cdp` | `method`, optional `params` | Restricted read-only CDP queries plus validated `Input.dispatch*` events |
| `sample` | optional `image` | JPEG screenshot bytes/hash, ARIA snapshot, failed requests |

Picker procedure: launch controlled Chromium; open its **Xpra native window**;
select **Pick element in Xpra window** in the Browser panel; hover until the
real DevTools overlay highlights the element; click it; the panel polls for
`Overlay.inspectNodeRequested` and displays DOM identity, computed CSS,
matched CSS rules with stylesheet URLs and line numbers, event handler script
IDs and accessibility roles/names. CSS source maps appear only when provided
by the page. Production/minified React components without source maps do not
magically map back to TSX implementation files.

Use accessibility-first actions in this order: `accessibility` -> `interactive`
-> `role.click`/`role.fill` -> keyboard Tab/Enter when appropriate ->
coordinate pointer input only where semantic metadata is inadequate.

### Security and qualification

- Each browser command is scoped to a **running, operator-launched**
  `controlled-browser` Xpra application session. Arbitrary `Runtime.evaluate`,
  `Target.attachToTarget`, unbounded CDP commands, shell, process PID and
  mount operations are not exposed.
- The browser runs inside the workspace (repository) sandbox, not the host.
  Chromium is launched with `--no-sandbox` **only because** the already
  qualified outer Bubblewrap/seccomp namespace supplies the isolation boundary.
- Other applications on the **same X display** share the workspace X11 trust
  boundary. Per-window delivery in Xpra is not per-window kernel isolation.
- No local browser profiles or arbitrary shell/JS evaluation cross the bridge.
  **DOM, CDP and screenshots may contain sensitive page data** and must be
  treated as workspace-private evidence; never forward them to untrusted agents
  or external telemetry without explicit redaction. Frame sampling is
  capped, hashed, and only enabled on request. Page output is untrusted text.
- A broken command pipe/time-out marks the browser outcome **indeterminate**;
  the supervisor fences and terminates rather than replaying a click/submit.
- A second feature phase is required for agent-facing SYN tool/Cog registration,
  evidence retention, controlled network proxy, multi-tab selection and
  automatic window-to-session mapping. This patch is the operator UI/control
  foundation, not a claim of those later capabilities.

### Tests

```bash
PYTHONPATH=src pytest -q tests/unit/test_xpra_browser_control.py \
  tests/unit/test_xpra_application_sessions.py \
  tests/integration/test_xpra_native_bridge.py
cd frontend && npm test && npm run typecheck && npm run build
```

Real acceptance must be run on the separately qualified Xpra/Xdummy+
Bubblewrap+Chromium host with the new image. Validate non-headless browser
creation, picker event delivery through a native Xpra window, screenshot vs
frame stream geometry, accessible role clicks, DOM CSS mapping and teardown.
A source-only unit test is **not** proof of that environment working.

### Local fixture walkthrough

Open the copied `tests/fixtures/browser-inspector.html` from a checkout under
`/home` using a **file URL that stays inside the same sandbox home**. Use
`accessibility` to find `textbox "Message"` and `button "Save message"`;
`role.fill` the textbox, then `role.click` the button; verify the live status
text through a fresh ARIA snapshot and sample the viewport. Activate the
DevTools picker, click the blue Save button in its Xpra window and inspect the
corresponding CSS rule, event listener and accessible name. This fixture is
strictly local and requires no outbound network access.
