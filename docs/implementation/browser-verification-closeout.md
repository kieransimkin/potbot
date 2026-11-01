# Browser verification: current blockers and explicit acceptance boundaries

Baseline: `ef9a48a9c46a3ccfabc1d969665073f57224117e` (1 November 2026).
This patch changes test infrastructure and stale assertions. It does not change
production browser, sandbox, context ACL, database or worker admission code.
It is not a declaration that all outstanding browser work is complete.

## Confirmed CI failures addressed

1. The main verification run `41297786186` stopped at two context assertions:
   62 cases passed. These still forbade the current workspace's shared
   `AGENTS.md` at the host broker, although Stage 10C explicitly allows the
   request to reach PostgreSQL's per-role grant and CAS checks. The assertions
   now deny a foreign workspace and another agent. All three current-workspace
   roles have positive broker-path checks and negative service-denial checks.
   Registration, image pin, lease acquisition and failed-operation lease
   settlement remain covered. The service double is not SQL authorization
   qualification; `tests/integration/test_context_acl.py` remains necessary.
2. Browser-controller run `41297712383` passed 123 tests then failed three
   worker fixtures before PostgreSQL startup. Their `__new__` construction
   bypassed the production constructor's new `_context_binding` field. The
   fixture explicitly initializes `None`. No runtime fallback, missing-field
   tolerance or disabled context admission has been added.
3. All four profiles in rendered run `41297712411` stopped at setup because the
   disposable runner already contained `/usr/bin/chromium`. Browser downloads
   succeeded, but the workflow's refusal to create that path prevented tests.

## CI-only browser selection

Production still launches `/usr/bin/chromium` from the qualified rootfs. No
production environment override or host-browser attach facility is added.
`scripts/browser_rendered_runtime.py` is a **test helper**, not copied into the
sandbox image. It permits only `system` (default) and `engine` test modes.

In explicit rendered acceptance, `engine` redirects the engine library's real
`BrowserType.launch` call to that same library instance's `executable_path`.
The original call must still name Chromium and `/usr/bin/chromium`; a changed
production launch contract fails the test rather than being silently accepted.
Other options and arguments are preserved, and the class method is restored
on leaving the context, including exceptions. The stdin/stdout child test uses
the same small test entrypoint to run the unchanged production `serve()`.
No arbitrary executable path is accepted from environment, model or page data.

The workflow installs the package versions already pinned in the Dockerfile,
installs each selected engine's Chromium, and no longer writes, replaces or
removes the runner's system Chromium. The four profiles and fail-fast=false
remain. Existing package-pin and no-skips evidence requirements remain.

For clarity: tests using the helper execute the real worker and browser with a
**test-selected binary**, not the deployment rootfs. They do not qualify Xpra,
Bubblewrap, an operator's image pin, or the host's syscall-monitoring policy.

## Evidence

Each rendered report must include the selected launcher source (`system` or
`engine`), SHA-256 and byte length, as well as package versions and reported
browser version. The identity describes the selected launch file; a system
launcher can itself be a wrapper rather than the ultimate ELF payload. The
engine-managed Chrome executable is a binary in the hosted matrix.

The evidence checker rejects missing, duplicate or malformed metadata, failed
or skipped cases and incorrect dependency pins. Hosted CI additionally requires
`--require-engine-binary`. The helper is included in recorded source hashes.
An older report without executable identity is intentionally rejected. These
hashes are evidence of what a trusted test runner selected, not remote
attestation or a defense against a malicious CI runner.

## Verification commands in a complete checkout

```sh
uv sync --locked --extra postgres --extra semantics --extra credentials \
  --extra providers --extra web --extra security --extra debugger --dev
uv run --locked python -m pytest -q \
  tests/test_context_agent_skills.py \
  tests/unit/test_context_release_hardening.py \
  tests/unit/test_sandbox_cog_worker.py \
  tests/unit/test_browser_rendered_runtime.py \
  tests/unit/test_browser_rendered_gate.py \
  tests/unit/test_browser_semantic_catalog.py \
  tests/unit/test_browser_worker_hardening.py \
  tests/unit/test_browser_cogs_s37.py \
  tests/unit/test_browser_provenance_s48.py
```

`browser-controller-s48.yml` must subsequently run its six real PostgreSQL
cases without skips. `browser-rendered-acceptance.yml` must run all four
profiles. Both workflows remain independent of the main verification chain.

The local delivery environment has only a hash-verified source subset. The
worker/gate/selection unit tests and five real Chromium tests can run there,
but the complete application context and sandbox-fixture suites cannot. The
ZIP's verification report records the exact local result, dependency versions
and missing gates. Local package versions must not be relabeled as image pins.

## Remaining gates, not waived

- Run full repository lint, formatting, type and integration checks. This patch
  does not repair or suppress unrelated repository-wide findings.
- Migrate `tests/integration/test_execution_provenance.py`: it still installs
  `HostOperation` callbacks forbidden by current worker admission. This patch
  does not silently substitute host execution or omit the test. Migration
  requires an admitted, bounded child-work protocol and sandbox fixture workers
  with real parent/child receipts, cancellation and process/audit settlement.
- Complete real leased multi-agent Xpra/Bubblewrap acceptance on the configured
  Linux/WSL2 host, including authenticated stream replay denial, cross-agent
  filesystem/X11 denial, egress allowlisting, operator-loss fencing and database
  evidence readback. Hosted rendered tests cannot certify these boundaries.
- Rebuild/qualify/pin an image containing the earlier merged production worker
  changes. This test-only patch itself changes no files copied into the image.

A green source test is not a green host qualification. Missing, failed or
skipped required evidence must remain incomplete rather than becoming a pass.
