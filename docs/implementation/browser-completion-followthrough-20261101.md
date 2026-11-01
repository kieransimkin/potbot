# Browser-control S48 follow-through: child Cogs, capture and host evidence

**Baseline:** Potbot `main` at `0af9b450a3dcaf2b21780c7cf5e72b34e3236f32`, which already contains the earlier screenshot-recovery change. The new changes deliberately preserve the operator's read-only defaults, sandbox requirements, private Xpra streams, existing browser Cog catalog, restricted CDP/HTTPS policy, pinned executable/image admission and independent PostgreSQL RLS. No guest-supplied code or plan runs inside the controller.

## 1. Rendered capture failure

GitHub Actions rendered matrix run `41305705273` showed the previous 2-second Playwright screenshot timeout in three profiles. The worker now collects JPEG pixels on the same private, already-approved CDP session using `Page.captureScreenshot(format=jpeg, quality=48, fromSurface=false, captureBeyondViewport=false)` with a six-second bound. The operation is **not** added to `CDP_READ` or public `validate_command`. It does not retry a click or navigation, and it rejects invalid, malformed, empty or oversized JPEG payloads. The existing 600 KiB evidence ceiling, MIME, digest, optional embedded bytes, immutable ArtifactStore and worker RPC timeout remain unchanged. This is the displayed page in the currently admitted worker, **not** a host/browser attach channel.

The original `Page.screenshot` implementation used a fragile headful compositor surface. The new read-only path was tested in the delivery environment against real Chromium under Xvfb with before/after DOM changes and distinct valid JPEG hashes; its 11 isolated unit tests pass. **This local test is not the pinned Playwright/Patchright CI matrix**. CI must still rerun all four profiles and require all five real cases per profile, no skips, matching package/binary hashes.

## 2. Operator-compiled nested Cog plans

`src/potbot/controller/child_plans.py` adds `ChildWorkStep`, `InstalledChildPlan`, `compile_child_plans`, and `execute_child_plan` for **operator-installed work only**. A plan is an immutable tuple keyed by the exact installed `(kind, operation_ref)` and made available only at `ExecutionCogRuntime` construction. No agent/model/page/guest input can create a new executable, operation, parent, child, input schema, effect grant or credential. `SandboxedCogExecutor` still owns child execution, verified binary pins, agent identity, leases and lifecycle monitoring. The parent worker first completes, then the controller schedules listed child Cogs via `ExecutionCall.child`; each child has its own run ID, independent admission and durable SYN causal chain. Supporting links are resolved from actual, authenticated PostgreSQL terminal events. Unfinished children prevent a parent success receipt.

Compiler rejects unknown operations, duplicate steps, forward/unknown links, cycles, depth >15, expanded families >256, different policy pins, incompatible inherited input ports and stronger child effect classes. Plans may not be passed as plain dictionaries to the runtime. The parent's wall-clock budget covers its family; individual workers retain their own supervised limits. Indeterminate sandbox/audit cancellations poison the runtime and require operator reconciliation, including read-only workers with unconfirmed process exit. The browser's existing direct-service fencing is unchanged.

The legacy PostgreSQL execution-provenance fixture installed 17 host callback workers that the newer runtime correctly forbids. The provided guarded migration replaces these with installed `SandboxedCogOperation` descriptors and a *scripted test-only process/RPC boundary*. Its operator-compiled plan generates the original 18-run family, including subagent->tool and a supporting script->MCP event link; it preserves signing/replay, no-repeat, RLS, API and WebSocket checks. The independent test double may run a host Python CLI and MCP fixture to verify *that harness*, but it **does not qualify those subprocesses as sandbox workers**. It must not be described as the full S46 live execution or Linux/WSL2 kernel gate. The source checker and exact-source evidence now include the child-plan and sandbox-worker implementation.

## 3. Cross-agent live-host closeout

`scripts/check_browser_host_closeout.py` enforces a schema and verifies independently produced, private, immutable operator artifacts. A valid report must bind **two distinct registered agents** to the same workspace, pinned source commit/image and operator epoch; require evidence for all nine real drills: simultaneous registered sessions; separate X11 cookies/sockets and hidden sibling homes; view-only streams and controlled input; consumed ticket/replayed epoch rejection; operator-loss action fencing; TLS/DNS/network policy denial; workspace/foreign-principal PostgreSQL artifact reads; creator-owned logs and archival; and complete process trees, file/syscall/exit accounting with reconciled leases. All evidence files must exist, be private regular files in one operator directory, and match SHA-256 digests, including exact checked-in source hashes. Missing, duplicate, dirty, tampered, skipped or contradictory evidence fails closed.

**Important:** this checker *cannot* authenticate a human operator's truthfulness or remotely attest a host. It never creates synthetic success evidence and deliberately emits `production_qualified:false` with `operator_signoff_required:true`, even when it validates a complete host report. Machine-checked evidence plus real independent host observations and operator approval is the release condition. CI unit tests with synthetic manifest contents are negative/contract tests only.

### Required target host procedure

1. Build, qualify and pin a new immutable image for the commit; never run the developer test-only browser-binary redirection in production.
2. On a disposable Linux/WSL2 machine with configured **real** Bubblewrap+seccomp, Xpra Xdummy, Chromium and operator service, launch two independently registered agents with the production browser-control Cog path and separate leased sessions.
3. Exercise the nine drills described above and capture exact source/instance/epoch/lease references and private operator artifacts. Use S46's `scripts/qualify_sandbox_cog_s46.py --run-live` for worker process/exit and S14/S17 operator/Xpra/log qualification, alongside dedicated browser/replay/network/DB probes. A host must not mark a drill successful from an assertion embedded in guest data alone.
4. Validate the operator-owned manifest and cryptographic file hashes:

```sh
uv run --locked python scripts/check_browser_host_closeout.py \
  --manifest /var/logs/ai/operator/browser-host-closeout.json \
  --output /var/logs/ai/operator/browser-host-result.json
```

The manifest format is defined by `SCHEMA` and `REQUIRED` in the checker; raw forensic evidence and secrets remain in private operator storage, not public CI artifacts. Use a fresh output file; a failed validation never overwrites prior reports.

## 4. Verification and release status

```sh
uv run --locked python -m pytest -q \
  tests/unit/test_browser_capture_recovery.py \
  tests/unit/test_child_plans.py \
  tests/unit/test_browser_host_closeout.py \
  tests/unit/test_browser_provenance_s48.py \
  tests/unit/test_sandbox_cog_worker.py
uv run --locked python -m pytest -q \
  tests/integration/test_execution_provenance.py \
  tests/integration/test_browser_controller_provenance.py
```

Run `browser-rendered-acceptance.yml`, `browser-controller-s48.yml`, `browser-control-completion-s42.yml`, full frontend tests and `scripts/check.py --with-postgres --with-sandbox` against the merged commit. The provenance checker cannot pass until the migrated fixture produces its fresh committed-source evidence. The main verification run at `0af9b45` was already red; the prior branch reported many unrelated Ruff formatting/import errors. These are not silently waived by this patch.

**Delivery evidence:** 11 capture unit tests, 12 child-plan contract tests and 16 host-manifest unit tests passed in isolated source-compatible harnesses; an independent headful Chromium CDP pixel check passed. Full-repository PostgreSQL, CI browser engine matrix, target Xpra/Bubblewrap, the actual nested sandbox worker and operator signoff have **not** been run here. Those requirements remain open; a source patch is not a production qualification.
