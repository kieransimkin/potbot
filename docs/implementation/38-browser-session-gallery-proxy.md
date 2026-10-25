# S38 — Exclusive browser sessions, evidence gallery, and gated CONNECT broker

**Status: source patch, offline tests only.** Requires the S37 browser Cog patch; compatible with S36 log mounts. Base repository revision `68d65e25f647652b4b4119495779ea8fb57e4cae`. S38 adds no Xvfb, host CDP listener, broad host filesystem access, unauthenticated screenshot retrieval or generic HTTP fetch.

## Session reservations

`BrowserSessionCoordinator` is a per-operator-process, workspace/principal-bound state machine. A browser is owned by at most one reservation (`owner_kind=operator|agent`, UUID owner, opaque unguessable 32-byte random token, monotonic deadline and generation). Its host API is `reserve`, `renew`, `operation`, `release`, `fence`, `retired`. A second reservation cannot take over even after a token expires; a stale/expired/inflight session must be terminated and explicitly reconciled. After a voluntary release the **same holder** may reacquire, but changing agent/operator identity requires browser process termination and retirement so cookies cannot be inherited by a new holder. The coordinator can run commands concurrently on *different* session IDs, but not twice on the same session.

To enable it, set **`"browserLeases": true`** in the **host-only** Xpra application policy. The app supervisor then checks reservations on every browser RPC. The authenticated Xpra bridge provides `GET/POST /v1/xpra/apps/{session_id}/browser/lease`. Next.js mirrors the API at `/api/ui/xpra/apps/{sessionId}/lease` after enforcing its existing operator login, workspace selection and same-origin checks. The React inspector acquires an operator lease on selection, renews it while open and sends its token via `X-Potbot-Browser-Lease` on command requests. Tokens are not saved to disk or returned by the status/inventory routes.

For an **agent-controlled SYN browser Cog**, the operator must reserve its chosen session with `owner_kind="agent"`, `owner_id` from the registered agent home, and provide that exact host-created `BrowserLease` as part of `BrowserControlService(..., reservations=(lease,))`. Browser control rejects a lease from another workspace or a session not on the service's approved allowlist. The model cannot choose a lease token or switch browser targets. Existing unreserved S37 services remain usable only if browser leases are not enabled; this is a legacy/operator-only profile, **not** multi-agent qualification.

A human operator with the underlying Xpra raw keyboard/mouse stream can still modify the GUI. The reservation fences **machine RPC commands**, not the Xpra binary input stream. Window-ID-to-agent attribution and operator manual-input arbitration require a separate, verified Xpra protocol broker. Until that is qualified, run agent reservations only when trusted operators agree not to interact manually with that Chromium window.

## Durable evidence viewer

S37 already writes immutable observation, image and manifest artifacts and emits normal SYN Cog work receipts. S38 adds `BrowserEvidenceGallery` as a **derived, operator-private SQLite index**. It stores only artifact references, hashes, session/workspace/principal IDs, and capture timestamps, never image bytes or raw HTML. Do not create this index in the sandbox, `/var/logs/ai`, `/var/ai`, `/home` or any shared tool path. The index parent must be an existing operator-owned mode-0700 directory outside guest paths, and the SQLite file is mode 0600. A separate long-lived operator process can read the same index after restart.

Configure the existing runtime (the same `ArtifactStore`, authenticated identity and Cog scope):

```python
from pathlib import Path
from potbot.tools.browser_gallery import BrowserEvidenceGallery
from potbot.controller.browser_cogs import BrowserControlService

gallery = BrowserEvidenceGallery(
    Path("/var/lib/potbot/operators/workspace-A/browser-gallery.sqlite3"),
    existing_artifact_store,
    forbidden_roots=(qualified_rootfs, workspace_home, model_staging, shared_tools),
)
# The operator must create /var/lib/potbot/operators/workspace-A with mode 0700.
service = BrowserControlService(
    actor=authenticated_workspace_context, apps=owned_xpra_supervisor,
    artifacts=existing_artifact_store, policy=admitted_execution_policy,
    approved_sessions=(session_id,), gallery=gallery,
    reservations=(agent_browser_lease,),  # optional for legacy operator-only mode
)
# Supply browser_gallery=gallery to potbot.api.server.create_app(...).
```

For the ordinary `potbot serve` entrypoint, supply these six *operator-trusted* paths instead of constructing the reader manually (combine them with your existing required `serve` flags):

```bash
potbot serve \
  --browser-gallery-index /var/lib/potbot/operators/workspace-A/browser-gallery.sqlite3 \
  --browser-artifact-root /var/lib/potbot/artifacts \
  --browser-rootfs /var/lib/potbot/images/qualified-rootfs \
  --browser-home /var/lib/potbot/workspaces/workspace-A/home \
  --browser-model-staging /var/lib/potbot/workspaces/workspace-A/var-ai \
  --browser-shared-tools /var/lib/potbot/shared-tools
# Also supply the normal --workspace-id, --principal-id and --provider-policy flags.
```

The artifact root must match the one already used by S37 to publish artifacts. The gallery index must be in an existing, operator-owned mode-0700 folder and outside all supplied sandbox paths. The flags are all-or-none; omitted means gallery disabled. This startup configuration **only enables readback**—the Xpra S37 controller must still receive the matching gallery object to index new evidence.

`BrowserControlService` writes the index **after** S37 artifact publication. A gallery-index failure after a browser action is an indeterminate outcome and fences the browser. An observation failure may leave immutable but undiscoverable artifacts; the existing SYN execution journal remains authoritative for recovery.

The authenticated Potbot REST API adds:

- `GET /v1/browser/evidence?limit=20&before=<numeric-cursor>` — descending per-principal listing (no raw content or token)
- `GET /v1/browser/evidence/{invocation-uuid}/observation` — verified JSON (up to 250 KiB)
- `GET /v1/browser/evidence/{invocation-uuid}/image` — verified JPEG (up to 600 KiB)

Each read revalidates the artifact digest and manifest scope via the existing `ArtifactStore`. Next.js exposes a constrained authenticated mirror under `/api/ui/browser/evidence`. The browser pane now includes an evidence gallery with pagination, private DOM/ARIA JSON and JPEG preview. Neither arbitrary artifact UUID reads nor host filesystem paths are accepted. The image is loaded via a bounded same-origin fetch with a temporary object URL, revoked on teardown.

## Gated HTTPS egress

`BrowserConnectGateway` binds an **operator-private AF_UNIX socket**, not a TCP listener. It supports only bounded HTTP/1.1 `CONNECT` requests for **exact operator-approved ASCII DNS names on port 443**, authenticated by a dedicated bearer. Public DNS resolution is validated *before* connecting; the broker connects by the checked literal IP to avoid a second DNS lookup and rejects any answer containing special-use, loopback, private, multicast or link-local addresses. Tunnels have byte, concurrency, connect and lifetime budgets. Raw browser cookies and HTTP response bodies are never parsed or logged by the broker; TLS remains end-to-end. Because a CONNECT tunnel is opaque, SNI/ECH-based domain-fronting and certificate-origin enforcement are **not** yet qualified. Do not claim this broker alone establishes end-to-end hostname isolation; test those cases before enabling the namespace-local forwarder.

**This patch deliberately does NOT enable Chromium internet access.** Chromium accepts HTTP/HTTPS/SOCKS proxies, not a mounted Unix-domain proxy endpoint. Potbot's current Xpra browser sandbox deliberately blocks AF_INET and remains a separate network namespace. A new *namespace-local*, explicitly qualified HTTP forwarder and reviewed seccomp/network policy must be implemented and proven before a browser can reach this gateway. Never mount the broker's private UNIX socket into an agent's general workspace shell or add `--proxy-server=...` without the forwarding qualification. In particular, never add a `direct://` fallback or open host network ports automatically.

## Acceptance and outstanding gates

Offline tests:

```bash
PYTHONPATH=src python3 -m pytest -q tests/unit/test_browser_s38.py \
  tests/unit/test_browser_cogs_s37.py \
  tests/unit/test_xpra_browser_control.py \
  tests/unit/test_xpra_browser_worker.py
cd frontend && npm install --no-audit --no-fund && npm run typecheck && npm test && npm run build
```

Host-required testing (not passed by this patch):

1. Two separate admitted agent Chromium sessions concurrently run `role.click`, screenshots and ARIA inspection without shared cookies/profiles or cross-session RPC access; reserve contention and timeout fencing are observed.
2. UI operator acquires/releases a browser reservation, reloads, and confirms stale leases are not silently inherited. Fencing of uncertain clicks/submit is independently demonstrated; no retry occurs.
3. Browser capture is read back from **real PostgreSQL + existing ArtifactStore** after restart through the authenticated gallery route. Attempt access from a second principal/workspace; verify refusal even with leaked artifact IDs.
4. Verify actual Xpra per-window input cannot be used to bypass agent exclusivity, or explicitly prohibit simultaneous operator input until per-window arbitration is implemented.
5. Host-side CONNECT gateway denies all unlisted hosts, Unicode/IDNA edge-cases, IP literals, private or mixed DNS results, HTTP non-CONNECT, tunneling over port !=443, and oversized/timed-out connections; test DNS rebinding with independent resolution and transport spies. Only then qualify a namespace-local forwarder and its firewall/secomp profile.
6. Deploy S36 before enabling agent log capture, and confirm the operator-owned log archive, watcher health and sandbox identity gates are active.

No live host/browser/real DNS request is performed by the offline tests. The `browser_gallery` option is opt-in; without it, S38 evidence routes are absent, and with `browserLeases` absent the old Xpra operator behavior is unchanged.
