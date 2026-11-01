# S48: browser controller provenance and post-dispatch fencing

## Scope and baseline

This change repairs the registered browser controller path; it does not add an
in-process worker exemption or claim live Xpra/Chromium qualification. It was
prepared against `6a8ed533c6041e2adb42115ed7eb54bc9eddfbc5` and checked against
`b9f0199210ee4a68c38529320091fa5a4423d4d0`. The intervening context/skill changes
do not change the three patched controller modules.

## Defects addressed

`ExecutionCogRuntime` already selected `BoundCogRegistry.browser()`, but
`PinnedExecutionJournal` recognized only execution, artifact-range and
artifact-upload snapshots. Consequently the real journal rejected browser
invocations before any browser dispatch. The journal now recognizes the exact
installed browser snapshot and its four direct Cogs. Browser and artifact
profiles remain separate; a browser snapshot cannot grant an artifact operator
or vice versa. Unknown snapshots, altered signatures and mismatched input ports
are still rejected. No caller-supplied catalogue is trusted.

A separate failure window existed after a successful browser RPC/evidence write:
post-effect admission, output validation or final journal persistence could fail.
The execution runtime became poisoned, but its browser service could still be
reused by another runtime. Side-effecting browser invocations now trigger a
service fence on these errors. The service uses its own bounded invocation-to-
session map, not a model-provided session or output field, to select the target.
An admission failure that never reaches service dispatch has no recorded session
and cannot stop an unrelated browser.

The service records local revocation before attempting operator termination. If
the stop request fails, further calls through that service are still rejected.
The original action/journal failure remains the reported exception, and the
existing journal emits `indeterminate` where it can persist a terminal event.
If that persistence also fails, the existing reconciliation-required error is
retained. No success or process-exit claim is synthesized.

## Limits and invariants

- The local fenced set is bounded by the existing maximum of 12 approved sessions;
  the admitted-invocation map retains the existing 4,096-entry limit.
- Revocation survives constructing another execution runtime with the **same
  service instance**. It is not a durable cross-restart revocation database and
  does not prove that a failed operator stop terminated an already running RPC.
  The operator remains responsible for session/lease and process reconciliation.
- Read-only observe/capture journal failures remain failures, not automatic
  mutating-action fences. Existing command effect classification is unchanged.
- Work admission, worker executable pins, sandboxing, reservation ownership,
  restricted CDP commands, HTTPS policy and evidence visibility are unchanged.
- No blind retry or automatic unfence is introduced. A fenced service has no
  reset operation. Do not create a fresh service merely to evade an uncertain
  session's operator reconciliation.

## Tests

`tests/unit/test_browser_provenance_s48.py` checks exact catalogue separation,
invocation-bound fencing, failed operator termination, pre/post-dispatch denial,
result validation, completion failure, cancellation and read-only behavior. Its
runtime tests use the actual invocation method, browser handler, service and
evidence recorder, with a journal transport double. These are not database or
live-browser acceptance.

`tests/integration/test_browser_controller_provenance.py` contains six
PostgreSQL acceptance tests. It constructs the real sealed registry and execution
runtime, persists real events/private artifacts, and substitutes only the browser
RPC/stop boundary using the existing S37 fixture. It covers all four Cogs,
causal event chains, source/manifest/image hashes, terminal receipts, duplicate
invocations, cross-workspace event visibility, post-action persistence failure,
post-picker revocation, read-only persistence failure, admission denial, and
incorrect catalogue/repository/task scope. The fixture JPEG is synthetic; this
suite does not validate an image decoder or a rendered browser.

Run in a complete checkout with locked dependencies:

```sh
uv run --locked python -m pytest -q \
  tests/unit/test_browser_cogs_s37.py \
  tests/unit/test_browser_provenance_s48.py \
  tests/unit/test_sandbox_cog_worker.py
```

Use only an explicitly disposable PostgreSQL fixture for the integration suite:

```sh
uv run --locked python -m pytest -q \
  tests/integration/test_browser_controller_provenance.py
```

The dedicated `browser-controller-s48.yml` workflow supplies its own disposable
Compose project, checks that all six integration cases actually ran without
skips, and records checked-out commit and source hashes. Its successful report
explicitly sets `live_browser_qualified` to `false`. It does not replace or
suppress the existing repository-wide checks.

## Delivery validation and remaining gates

Local delivery validation uses a hash-verified source subset, not the complete
application checkout. The external offline harness doubles unavailable domain,
catalogue, database and process imports. The real changed source and browser
handlers are exercised, but local passing tests must not be represented as a
real sealed-catalogue or PostgreSQL acceptance result. That harness is not part
of this repository patch. Full-repository CI must run the ordinary tests without
those substitutes.

Still required: the new PostgreSQL controller workflow; repository-wide lint,
type and integration verification; migration of the older nested provenance
fixture from host callbacks to admitted sandbox executables; and real leased
multi-agent Xpra/browser qualification on a configured Linux/WSL2 host. S48 does
not mark any of those gates complete.
