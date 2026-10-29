# Browser stream lifetime hardening against Potbot main

**Target commit:** `216d847b12adf25d6e012871ba49f9fb2fc76628` (xpra s17, 29 October 2026).

S37–S42 browser controls, private Xpra displays, and agent Dockview streaming
are already merged in this baseline. This delta does **not** reinstall them.
It closes three frontend lifecycle races and improves rejection cleanup:

1. **Inventory retirement before the first window:** A running decoder may
   have no observed window yet. Retiring its session now revokes the publisher
   even without a corresponding Dockview pane.
2. **No reused epochs:** Stream epochs monotonically increase across session
   disappearance/reappearance and hub resets. A late frame cannot alias the
   new connection's first epoch.
3. **Latest inventory wins:** Overlapping HTTP polls use a latest-request-only
   fence. A stale success or error cannot resurrect an agent browser after a
   newer inventory response or unmount.
4. **Frame memory + readiness:** Bitmap ownership is released even when a
   frame originates from an obsolete iframe or has the wrong origin/channel;
   duplicate `ready` signals cannot replace the active stream epoch.

### Source checks

From the repository root:

```bash
# Check the patch before applying; no custom installer.
git apply --check potbot-browser-main-216d847-hardening.patch
git apply potbot-browser-main-216d847-hardening.patch

cd frontend
npm install --ignore-scripts --no-audit --no-fund
npm run typecheck
node --experimental-strip-types --test tests/agent-xpra-s41.test.mjs tests/agent-xpra-s42.test.mjs
npm run build
```

Also run the existing Python browser suite and source gate:

```bash
uv run --locked python scripts/check_browser_control_s42.py --root .
uv run --locked python -m pytest -q tests/unit/test_agent_xpra_streams_s41.py \
  tests/unit/test_browser_control_completion_s42.py \
  tests/unit/test_agent_xpra_displays_s40.py
```

These tests do not establish qualified Linux/WSL2 X11 isolation, TLS/DNS
broker behavior, successful PostgreSQL evidence readback, or real Xpra video
reconnection. Those live host gates remain mandatory. This patch changes no
sandbox permissions, server credentials, or egress policy.
