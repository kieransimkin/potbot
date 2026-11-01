# Browser catalogue repair and rendered-worker acceptance

## Baseline and observed failures

Prepared against `375055a1041b20ac3f718a61510c9bd6fd9a1243`, containing S48.
The S48 GitHub Actions run `41282958515` passed 77 unit tests, but all six
PostgreSQL browser-controller tests failed before dispatch with
`Unknown or unregistered canonical Type`. `ToolSemanticCatalog` did not include
browser schemas in its compiled whitelist. The earlier local subset harness
could not expose that missing real-catalogue integration.

Real rendered-browser checks also reproduced two operational failures:

* `Page.aria_snapshot()` does not exist in the tested Playwright API. Both
  accessibility reads and screenshot-plus-ARIA sampling failed. The worker now
  uses `page.locator("body").aria_snapshot(timeout=5000)`.
* Polling a picker selection cleared only Python state, leaving Chromium's
  inspection overlay active. Subsequent pointer clicks did not reach the page.
  The worker now ends inspect mode and hides the overlay before returning the
  inspected node. The stop request includes an empty `highlightConfig`, which
  the tested Chromium protocol requires even for mode `none`.

## Runtime changes

The semantic catalogue recognizes the existing, compiled browser schemas only
when their Types are present in the sealed snapshot. Exact schema equality and
registered data-record ancestry remain mandatory. No arbitrary schema metadata
or network-resolving `$ref` becomes executable. All four browser request ports
also pass the existing command/effect validator before controller dispatch.
The published schema definitions and catalogue descriptions are not changed.

The worker now validates direct `execute` calls as well as its wire entrypoint,
rejects duplicate JSON keys and non-finite constants, bounds line allocation to
4,097 bytes, and rejects non-finite wheel deltas and non-integer click counts.
No command or CDP method is added to the allowlist.

Partial startup always enters cleanup. A browser-close exception still triggers
driver shutdown; state is cleared after cleanup attempts. This does not certify
that an operating-system process tree stopped after a failed close. Operator
fencing and audited process reconciliation remain authoritative.

CSS and script source metadata are bounded to 400 entries and two bounded URL
fields per entry. Removed stylesheets are forgotten, stale picker selections are
cleared on DOM document updates, and resolved remote objects are released even
when collecting listeners fails. Source locations are correlations reported by
Chromium, not independently verified repository provenance. Source-map URLs are
metadata; this change does not fetch maps or reconstruct original TypeScript.

File navigation URL-decodes the path before the existing real-path `/home`
containment check. Encoded traversal outside the workspace is still refused.
That behavior has unit coverage; the delivery environment's browser policy
blocked file navigation, so it was not qualified by a live file-URL test.

## Acceptance layers

### Real catalogue and PostgreSQL controller

`tests/unit/test_browser_semantic_catalog.py` constructs the real registry and
`ToolSemanticCatalog`, without replacing either. It checks all browser ports,
round trips, alias identities, metadata tampering, profile separation, and
command/effect rejection. The existing S48 workflow now runs it before starting
PostgreSQL, and includes semantic-contract changes in its trigger and evidence.
All six original PostgreSQL cases remain required. No failure or skip is masked.

### Rendered browser and private worker pipes

`tests/browser/test_rendered_worker.py` starts the production `BrowserWorker`
against real Chromium. It uses trusted synthetic fixture HTML/CSS/JavaScript
injected by the test harness, not a new worker operation or permission. Fixture
requests are aborted; the tests do not need external site access. CSS/JavaScript
`sourceURL` labels make source correlations deterministic without fetching them.

The five required scenarios cover:

1. ARIA, role-based fill/click, JPEG digest/readback, and visible output changes.
2. DevTools picker selection, CSS and listener source locations, accessibility,
   click interception, and normal pointer behavior after selection.
3. Keyboard, direct pointer/CDP input, scrolling, exact-locator ambiguity, and
   rejection of arbitrary JavaScript/CDP methods.
4. Two independently created browser contexts without shared form/picker state.
   This is **not** cross-agent, cross-UID or kernel isolation evidence.
5. A real worker subprocess using private stdin/stdout, malformed request
   rejection, image sampling, bounded unterminated input, and awaited exit.

`browser-rendered-acceptance.yml` runs four independent profiles: Playwright,
Patchright, Playwright + playwright-stealth, and Patchright + playwright-stealth.
It reads package pins from `sandbox/image/Dockerfile`, rather than maintaining a
second version list. Chromium is the selected engine's downloaded build in a
disposable runner, not a qualification of the production distro Chromium build.
The temporary `/usr/bin/chromium` symlink is CI-only; an existing binary/path is
refused rather than silently replaced. Production launch arguments are unchanged.

The gate rejects missing/duplicate cases, skips, failures, collection errors,
wrong profile metadata, or missing versions. CI additionally requires the exact
image package pins. Reports include versions, source hashes, JUnit digest, Git
revision, and dirty-source status. They explicitly leave PostgreSQL-controller,
Xpra, sandbox-image, kernel and multi-agent qualification false. A JUnit checker
validates a trusted test runner's report; it is not an attestation service.

## Local execution

Use a disposable test environment with `/usr/bin/chromium`, a display, and the
selected browser packages. Tests are opt-in so normal unit runs do not launch
browsers. Missing selected dependencies fail; they do not cause an acceptance skip.

```sh
POTBOT_RUN_BROWSER_RENDERED=1 POTBOT_BROWSER_LIVE_PROFILE=playwright \
  xvfb-run -a python -m pytest -q tests/browser/test_rendered_worker.py \
  -o junit_family=xunit1 --junitxml=artifacts/browser-rendered/rendered.xml
python scripts/check_browser_rendered.py artifacts/browser-rendered/rendered.xml \
  --profile playwright --output artifacts/browser-rendered/result.json
```

Add `--require-image-pins` for a required image-package match. A local pass with
different package versions records `dependency_pins_match_image: false`; it must
not be described as acceptance of the pinned deployment combination.

The delivery checks used Python 3.13.5, Playwright 1.57.0 and Chromium
144.0.7559.96 under Xvfb in a hash-verified source subset. Five rendered cases and
51 unit/gate checks passed. Restoring the original worker made all five rendered
cases fail; 21 of the 28 new worker hardening cases also failed. The real semantic
catalogue tests, PostgreSQL suite, pinned matrix and full repository lint/types
were not executable in that subset and must run in complete-checkout CI.

## Deploying the fix and remaining work

The worker is copied into the immutable sandbox image. Updating host Python
source alone does not update an already-running guest worker. The existing
sandbox-image workflow already watches both worker and HTTPS route sources.
Rebuild the image, admit its new manifest, and restart/requalify sessions through
the normal operator lifecycle. Do not automatically reopen a fenced session.

Still outstanding: successful real-catalogue/PostgreSQL and full-repository CI;
the older nested executable-worker provenance migration; the pinned browser
matrix on its actual runners; and end-to-end leased, audited, multi-agent
Xpra/Bubblewrap acceptance on the configured Linux/WSL2 host. None of those are
silently replaced by this rendered-worker suite. Existing egress, log ownership,
agent leases, sandbox execution and audit requirements are unchanged.
