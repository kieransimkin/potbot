# Browser rendered capture recovery — 1 November 2026

Baseline: `main` at `4c52d8b4402fec12ee1a8eb4e6f6795805f62ccd`.
Status: source patch prepared; **not merged or live-host qualified**.

## Confirmed failure and bounded repair

In the GitHub Actions rendered-worker matrix run `41301571455`, Patchright,
Playwright+Stealth and Patchright+Stealth passed all five real cases. Plain
Playwright failed 1/5 cases during the first JPEG screenshot with Chromium's
`Page.captureScreenshot: Unable to capture screenshot`, after fonts had loaded.

This change keeps the same private Playwright/Patchright page, existing image
shape, SHA-256 evidence, size limit (600 KiB), restricted CDP RPC allowlist,
and no host-browser attach or network privilege. For **only** that precise
surface-capture error, it retries the read once and then tries a bounded
internal `Page.captureScreenshot` with `fromSurface=false`. The fallback is not
added to the model-callable CDP allowlist. Any missing, malformed, oversized or
non-JPEG bytes fail closed. Neither mutating actions nor other errors are
retried. The worker's independent 12-second RPC deadline remains authoritative.

## Requalification

Run the added mock-based regression tests, then the existing four-profile
rendered acceptance workflow against a clean checkout on its real browser
matrix. Required evidence is **five passing cases per profile**, no skips,
matching package pins and browser-binary identities. A patched source or local
unit test alone does **not** prove that the original Chromium failure is fixed.

```sh
uv run --locked python -m pytest -q \
  tests/unit/test_browser_capture_recovery.py \
  tests/unit/test_browser_worker_hardening.py \
  tests/unit/test_browser_rendered_gate.py \
  tests/unit/test_browser_rendered_runtime.py
```

The S48 browser-controller PostgreSQL workflow passed on `0b4b51dfa`, but the
repository-wide verification workflow on `4c52d8b` remains red, with a Ruff
report of 380 findings on 1 November 2026. Do not represent that as a passing
release. The older `tests/integration/test_execution_provenance.py` still
installs host callbacks, which current sandbox worker admission rejects. Its
replacement needs real admitted child execution and a PostgreSQL transcript;
removing the test, silently allowing host callbacks, or marking it skipped is
not an acceptable completion path.

The actual Linux/WSL2 operator must separately qualify two registered agents'
leased Xpra sessions, cookie/socket isolation, stream epochs and stale frames,
TLS/DNS egress denial, operator death/restart fencing, process-tree shutdown,
audit and PostgreSQL artifact readback. CI-rendered Chromium is not a surrogate
for these host gates. All missing/failed required evidence remains incomplete.
