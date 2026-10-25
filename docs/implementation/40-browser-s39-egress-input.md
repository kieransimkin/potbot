# S39 — Xpra input fencing and opt-in sandbox HTTPS egress

## Scope and status

S39 targets Potbot `main` at `ec976f39d6f91e9740ab427dc08d2228c9e32628`, which contains the previously merged S37 and S38 browser Cogs, evidence gallery and coordinator. S36 agent logging is separately optional, and the installer accepts the S36-modified launcher policy. This source patch is **not** a claim of a fully qualified Linux/WSL2 deployment. Live Xpra, the real sandbox kernel namespace and network broker are mandatory remaining gates.

### Control planes

```
Operator policy (host-only, mode 0600)
    │  strictXpraInput + browserLeases
    ├─> WorkspaceXServer -> Xpra --readonly=yes -> native window frames
    │                                       ╰── no remote Xpra keyboard/pointer input
    └─> allowed exact HTTPS hosts -> AF_UNIX CONNECT gateway (host outside sandbox)
                                               ▲
  admitted controlled-browser worker only      │
      ├─ Chromium (AF_INET/6 denied)            │ TLS ciphertext
      ├─ Playwright context.route('**/*') -------┘ via Python ssl + socket(AF_UNIX)
      └─ reserved typed browser Cogs / operator UI browser inspector
```

The authenticated Xpra WebSocket relay still forwards the **native Xpra protocol**, not frame JPEG polling. **Read-only is enforced at the Xpra server**, not just JavaScript. It is deliberately a *display-wide* restriction in this stage; xterms or other GUI windows on that display are view-only. Manual inspection remains possible by clicking the **most recent screenshot** in Browser Inspector (mapped to CDP `DOM.getNodeForLocation` via the existing reserved RPC). A deliberate "click" mode turns the same screenshot into a reserved Playwright pointer action, not raw Xpra input. A screenshot older than five seconds cannot be used for either action.

This is an *initial secure mode*, not yet the final per-window Xpra input authority design. Direct X11 socket clients **inside the same workspace sandbox** may still inject X11 events; real per-agent X display ownership or host-enforced X11 request mediation must be qualified before hostile agents can share one display. The current workspace-wide GUI lease serializes ordinary unbrokered writes, but is not a kernel-enforced identity separation. Do not promise per-agent X11 isolation before that host gate.

## Host policy

The Xpra operator's existing `--apps-policy` JSON may opt into both strict input mode and external HTTPS at startup. These fields are optional **only** for the legacy no-leases/no-egress configuration. A policy with `browserLeases:true` but no `strictXpraInput:true` now fails closed. This prevents giving agents the impression that native Xpra input is lease-controlled when it is not.

Example host-owned policy extension (merge into existing approved workspace policy):

```json
{
  "browserLeases": true,
  "strictXpraInput": true,
  "browserEgress": {
    "socket": "/var/lib/potbot/operators/run/ws-one-connect.sock",
    "hosts": ["docs.python.org", "github.com"]
  }
}
```

This is a **fragment**, not a complete runnable policy; retain the existing `workspaceId`, `principalId`, `ledger`, `runtime` and `recipes` fields. A `controlled-browser` recipe must already be approved. Create the broker's parent directory as a real, host-owned directory with mode **0700**; the socket must not exist on startup. The operator creates an ephemeral 32-byte-equivalent bearer token in memory and starts its Unix socket with mode **0600**. Bubblewrap copies the token from a passed anonymous descriptor (`--ro-bind-data`), **not** an environment variable or host command-line argument. No model, HTTP caller or agent controls the destination host list, broker path, token, executable or sandbox mounts. `browserEgress` is entirely optional and disabled by default.

The S39 operator loader starts the browser CONNECT broker and gives its **single socket** only to the admitted `controlled-browser` process via a Bubblewrap `--ro-bind` inside that worker's mount namespace. No entire host socket directory or general TCP network namespace is shared. Only the controlled worker receives an ephemeral CONNECT bearer via its initial environment, and the worker removes it before starting Chromium / Playwright's children. Another agent process in the shared workspace sees at most the unmounted placeholder file, not the socket bind. This isolation remains contingent on the existing qualified Bubblewrap+seccomp admission and Linux mount namespace preflight.

## HTTP behavior and restrictions

Chromium does **not** directly support the mounted Unix domain socket as an HTTP proxy. The worker instead registers one Playwright `context.route('**/*')` handler before the first page. For HTTPS GET/HEAD requests:

1. Reject malformed URLs, credentials, arbitrary ports, IP literals, IDN/punycode labels, untrusted schemes and requests containing control characters.
2. Connect to the host-only CONNECT broker over its one mounted AF_UNIX socket and authenticate with a short-lived token.
3. The host-only broker checks the exact allowlist, resolves DNS once, rejects *all* non-global records and connects to the verified literal IP on port 443.
4. The worker upgrades that tunnel to **hostname-verified TLS** using the guest image CA store. The host broker sees ciphertext, not the resulting HTTP document.
5. The worker accepts bounded results (4 MiB maximum), discards cookies and unsupported hop-by-hop headers, and supplies approved response headers and body to `route.fulfill`. Explicit redirects are handled through new independently inspected requests, never by directly following an unapproved URL.
6. Every unsupported request and every exception invokes `route.abort('blockedbyclient')`, with no `route.continue_` and no direct fallback.

The scope intentionally excludes HTTP plaintext, POST/PUT/PATCH/DELETE, WebSockets, WebRTC, uploads, downloads and streaming media. Some websites will be incomplete; this must be surfaced as an unsupported capability, not silently bypassed. The browser remains unable to create AF_INET/6 sockets. Browser output, including DOM and accessibility tree, remains untrusted.

**Caveat:** `route.fulfill` is a browser automation mechanism, not a complete HTTP proxy or proof of native network mediation for all Chromium subsystems. Native browser-side control and WebSocket/QUIC/non-HTTP traffic must be denied by the existing seccomp/network namespace, independently of request interception. The Xpra worker's Python routing adds bounded visibility, but does not replace the kernel boundary.

## Rollout and qualification

```bash
# Source-only, offline checks
PYTHONPATH=src python -m pytest -q tests/unit/test_browser_s39.py \
  tests/unit/test_browser_s38.py tests/unit/test_browser_cogs_s37.py \
  tests/unit/test_xpra_browser_control.py tests/unit/test_xpra_browser_worker.py
cd frontend && npm install --no-audit --no-fund --ignore-scripts && npm run typecheck && npm test && npm run build
```

Additional **mandatory live host acceptance** before enabling `browserEgress` in production:

- Run the qualified immutable rootfs, native Xpra 6.3+ with Xdummy, a real read-only Xpra connection, and a browser using the pinned Playwright/Patchright version. Try keyboard, wheel, buttons, Xpra reconnect and forged browser UI; no input may be delivered through the read-only Xpra client. Verify screenshot inspection/click uses only the reserved browser command path.
- Inside the sandbox, verify **AF_INET/AF_INET6 cannot be created**, even by child processes and third-party executables; only the one mounted AF_UNIX broker socket is reachable. Test a foreign agent's mount namespace and verify it **cannot** connect to the broker. A mere failed outbound request is insufficient; use kernel seccomp/mount namespace evidence.
- Prove valid allowlisted GET resources resolve through the broker and verify actual TLS certificate and hostname mismatch rejection. Test mixed DNS records, 127/8, RFC1918, 169.254/16, `::1`, IPv4-mapped IPv6, rebinding, IDNs, explicit ports, redirects to private hosts, giant responses, HEAD, gzip/streamed bodies, POST and WebSocket fallbacks.
- Stop/restart the Xpra operator under in-flight browser requests, release a lease and try to transfer a live profile to another agent, and ensure there is **no replay** of uncertain submit/click.
- Check `browserEgress` absent -> no external networking, then test a malicious local X11 client and require a *separate Xpra server or proven mediation* before claiming hostile-agent per-window isolation.
- Capture real PostgreSQL screenshot/ARIA evidence and confirm separate workspace/principal access fails; S36 log operator, if enabled, must remain healthy.

The existing Xpra operator remains loopback-bound. No Docker container is run to manage sandboxes. The image workflow rebuilds a new signed, versioned immutable rootfs whenever the embedded worker or HTTPS routing code changes; the host must verify and pin it before any new browser is admitted.
