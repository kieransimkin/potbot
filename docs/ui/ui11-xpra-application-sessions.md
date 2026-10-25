# UI-11 — Qualified workspace Xpra application sessions

## What is implemented

The Xpra relay now offers an **operator-controlled application session API**.
A browser authenticated to Next.js may request a *kind* from a trusted policy,
but cannot supply a command line, environment, image, working directory, URL,
mount, device or host process ID. The Xpra relay starts processes only from a
qualified `SharedWorkspaceBubblewrapBackend` with an active image pin. No Xvfb,
XTEST, VNC desktop, native host shell, or unconfined launch fallback exists.

The same **workspace** (also called *repository* in the Potbot UI) owns the
X display, its Unix socket and Xauthority, every application session and every
Dockview window. Multiple Git repositories inside that workspace share those.

- An operator-approved **terminal** recipe can launch xterm running Bash.
- An operator-approved **browser** recipe can launch a GUI browser from the
  qualified image. Because the sandbox isolates the network, internet browsing
  is *not* enabled until a separately qualified network/proxy policy exists.
- Further GUI app recipes are permitted only if the operator pins their exact
  executable and argument vector and the immutable image contains that binary.
- Xpra owns the window/protocol/codec transport. The UI manages individual
  Dockview windows, forwards Xpra keyboard/pointer events and controls the
  application process separately.
- Closing/minimizing a Dockview pane only hides/detaches its window. **Stop**
  is explicit and requires confirmation; **Reattach** works after refreshing
  or reopening the UI, while the Xpra relay process remains alive.

## Host-only setup

Prerequisites: Linux/qualified WSL2, Potbot's signed and unpacked rootfs,
`bwrap`, `prlimit`, `libseccomp`, `xauth`, Xpra Xdummy and installed GUI binaries.
The host operator must have already activated the immutable rootfs digest and
qualification SHA-256 through `WorkspaceLeaseLedger.pin_image`. **This is not
performed by the UI or by an agent.**

Create a JSON policy at an operator-private path **outside** the sandbox home
and rootfs (e.g. `/var/lib/potbot/operators/workspace-apps.json`). It must be
owned by the Xpra relay process UID and have mode `0600`:

```json
{
  "workspaceId": "00000000-0000-4000-8000-000000000001",
  "principalId": "00000000-0000-4000-8000-000000000002",
  "ledger": "/var/lib/potbot/ledgers/workspace.sqlite3",
  "runtime": {
    "imageDigest": "<64-lowercase-hex-sha256>",
    "qualificationSha256": "<64-lowercase-hex-sha256>",
    "rootfs": "/var/lib/potbot/images/qualified-rootfs",
    "home": "/var/lib/potbot/workspaces/workspace-one/home",
    "modelStaging": "/var/lib/potbot/workspaces/workspace-one/var-ai",
    "sharedModels": "/var/lib/potbot/shared-models",
    "sharedTools": "/var/lib/potbot/shared-tools",
    "aiConfig": "/var/lib/potbot/workspaces/workspace-one/etc-ai",
    "aiPast": "/var/lib/potbot/operator-ai-history/workspace-one"
  },
  "recipes": [
    { "kind": "terminal", "title": "Bash (xterm)",
      "argv": ["/usr/bin/xterm", "-fa", "Monospace", "-e", "/bin/bash"] },
    { "kind": "browser", "title": "Workspace Firefox",
      "argv": ["/usr/bin/firefox", "--no-remote", "about:blank"] }
  ]
}
```

The example contains placeholders, **not working credentials**. Install each
executable in the pinned rootfs and verify the path before adding its recipe.
For legacy rootfs images that truly lack `/etc/ai` and `/etc/ai-past`, omit
`aiConfig` and `aiPast` together. On modern images, both are mandatory; the
operator starts the AI configuration watcher before permitting launches. If
another process owns that monitor, the Xpra application service fails closed.

Launch the Xpra relay as the operator controlling that workspace, using the
**same** `--workspace-id` and `--sandbox-root` home specified in the policy:

```bash
export POTBOT_XPRA_BRIDGE_TOKEN='<long-host-only-credential>'
PYTHONPATH=src python -m potbot.tools.x11_workspace_server \
  --workspace-id 00000000-0000-4000-8000-000000000001 \
  --sandbox-root /var/lib/potbot/workspaces/workspace-one/home \
  --browser-origin http://127.0.0.1:3100 \
  --port 8765 \
  --apps-policy /var/lib/potbot/operators/workspace-apps.json
```

The Next.js server uses its existing `POTBOT_UI_XPRA_BRIDGE_*` settings to
access this loopback-only relay. For **multiple** authorized workspaces,
configure a separate private Xpra bridge for each workspace in the server-only
`POTBOT_UI_XPRA_BRIDGES_JSON` environment variable, alongside
`POTBOT_UI_WORKSPACES_JSON` (the latter keeps its original `id`, `name`, `url`,
`token`, optional `model` shape):

```json
[
  {"workspaceId":"00000000-0000-4000-8000-000000000001",
   "bridgeUrl":"http://127.0.0.1:8765/",
   "bridgeToken":"<private-host-bearer>",
   "publicWsUrl":"ws://127.0.0.1:8765/v1/xpra/ws"},
  {"workspaceId":"00000000-0000-4000-8000-000000000003",
   "bridgeUrl":"http://127.0.0.1:8766/",
   "bridgeToken":"<separate-private-host-bearer>",
   "publicWsUrl":"ws://127.0.0.1:8766/v1/xpra/ws"}
]
```

The Xpra bridges are validated against the independently allowed Potbot backend
workspaces. Unknown IDs, duplicate entries, non-loopback bridge HTTP URLs or
invalid WebSocket URLs fail closed. The old single-workspace settings remain
supported. Do not place the bridge bearer, launch policy or Xauthority in
browser-visible configuration. An unconfigured workspace can still show other
UI panels but cannot launch an application or open an Xpra stream.

## Authenticated API contract

| Request | Meaning |
|---|---|
| `GET /v1/xpra/apps` | Explicit operator recipes and session inventory |
| `POST /v1/xpra/apps` | `{ "kind": "terminal" }` only; exact operator recipe |
| `POST /v1/xpra/apps/{id}/attach` | Reattach process entry after UI reconnect |
| `POST /v1/xpra/apps/{id}/detach` | Detach entry; process survives |
| `POST /v1/xpra/apps/{id}/terminate` | Explicitly terminate the process group |

These endpoints require the **private Xpra relay service bearer**. The public
Next.js equivalents are under `/api/ui/xpra/apps` and require the existing
operator cookie, same-origin checks for mutations and the exact workspace header.
Only a whitelisted session projection is returned (ID, title, kind, state,
exit code, start time, detach state); host PIDs, argv, mounts and sockets are
never forwarded.

## Isolation and reliability notes

- The normal shared-rootfs sandbox still denies all sockets. Only a trusted
  Xpra application invocation swaps in a seccomp profile allowing AF_UNIX
  sockets, bound by a separate network namespace and a single physical Xpra
  socket mounted into its private guest filesystem. No host X socket directory
  or Xpra control socket is mounted.
- The Xauthority cookie is stored inside the workspace's operator-provisioned
  `.potbot/x11` directory, not in the immutable rootfs. The host validates its
  owner, mode and location before every launch.
- One full-workspace write lease covers *all* launched GUI processes; a second
  tool requiring the same exclusive home lease may be delayed until the GUI
  session group is closed. This conservative choice prevents uncoordinated
  shared-home writes, at the cost of concurrency. It does **not** establish
  independent kernel-enforced per-agent filesystem isolation.
- Host-side process starts, stops, lease fencing, abnormal exit and Xpra/AI
  watcher failure are tracked. Closing the browser or detaching a pane does
  not kill applications. Restarting the **Xpra operator process** terminates
  its owned app group; an unknown orphaned lease requires reconciliation rather
  than automatic replay.
- Application delivery is Xpra's native individual-window channel. There is
  no HTML framebuffer polling path and no synthetic terminal window pretending
  to be a running process.
- This implementation does not grant network access to a browser or implement
  generalized PTY streaming outside the Xpra window. Both require separate
  host-reviewed policies.

## Qualification

```bash
PYTHONPATH=src pytest -q tests/unit/test_xpra_application_sessions.py \
  tests/integration/test_xpra_native_bridge.py
cd frontend && npm test && npm run typecheck && npm run build
```

On a **real**, separately provisioned operator host, launch the relay with
`--apps-policy`, use the UI to launch xterm, observe a new native Xpra window,
press keys in that window, minimize then restore the Dockview pane, reconnect
the Next.js browser, reattach the process, and finally Stop it. Verify the
active group lease is released *only* after all applications exit. An opt-in
real Xpra/browser acceptance step is still required; tests using a fake Xpra
relay do not constitute production qualification.
