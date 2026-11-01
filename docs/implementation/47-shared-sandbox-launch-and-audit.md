# S47 — shared sandbox launch repair and monitored smoke acceptance

## Baseline and scope

Prepared against `94de8d8f2fa1ad8304b0eeb745397a76239a5f6b`, following S46.
The S46 source-contract job passed on that commit; its manual live-worker job
was skipped. Image validation failed before building either architecture.
The main application verification also failed and is not made green by this
patch. No production host or new rootfs has been qualified by authoring S47.

This milestone fixes the observed image-validation blockers and repairs the
older shared-workspace smoke runner's mandatory monitoring path. It does not
implement the remaining nested worker/provenance protocol, migrate workspace
leases, change sandbox permissions, or permit host execution as a fallback.

## Implemented

| Item | Implementation |
| --- | --- |
| Optional context mounts | Root and agent mount arguments are constructed independently, only when their respective projection is configured. |
| Complete configured projections | Empty, short, and oversized document sets fail before seccomp FD allocation; strict role/path pairing remains. |
| Projection security | Existing real snapshot hash, read-only-file, hardlink, symlink, placeholder, agent-directory and workspace checks are retained. |
| Descriptor tests | The two old fake numeric seccomp descriptors are replaced by owned descriptor stand-ins, so a genuine error is not masked by an unrelated bad-FD cleanup error. |
| Smoke request attribution | Every probe carries an explicit host-selected workspace UUID, principal UUID, principal attribution, and a new run UUID. |
| Mandatory telemetry | The runner passes `WorkerMonitorConfig` to the real supervisor and has no unmonitored fallback. |
| Independent audit readback | A passing process response must also pass the existing full audit verifier for the same workspace, run and principal. Missing, corrupt or mismatched evidence cannot qualify. |
| Unconfirmed termination | The suite stops before another probe or marker cleanup when process shutdown is unconfirmed. |
| Namespace probe | A failed, empty or malformed guest `readlink` result cannot satisfy the namespace comparison. |
| Network probe | An isolated Python probe requires EPERM/EACCES for both IPv4 and IPv6 socket creation. It performs no DNS lookup or outbound connection. Unsupported families and unrelated failures do not qualify. |
| Report custody | The output must be fresh, canonical, operator-private and outside every guest mount before probes begin. The existing S46 exclusive writer checks again, writes mode 0600 and refuses overwrite. |
| Marker cleanup | Cleanup uses descriptor-relative unlink and refuses a substituted final directory symlink. It never follows a marker symlink. |
| CI dependency coverage | Both image-workflow path filters now include context, supervisor, telemetry, audit and report-writer dependencies. The existing validation job already runs both expanded test files. |

No database, credential format, guest identity allocation, image pin, seccomp
rule or read/write mount policy is changed. Docker remains build-time only.
The previously merged context-RPC client and S46 diagnostic worker are untouched.

## Host-only smoke CLI change

The existing runner now requires `--workspace-id` and `--principal-id`. These
must come from the trusted operator's selected disposable smoke workspace and
operator identity. They are not model-controlled parameters and do not grant
permission to execute or read another workspace.

Run only on an operator-provisioned, otherwise idle disposable smoke workspace.
This bootstrap smoke does not acquire normal worker leases or reconcile them.
It is not an admission route for an agent, and must not be run concurrently with
agents on a working workspace. S46's registered-worker runner remains the
separate leased, qualified-image acceptance path.

The selected image must already have been safely unpacked and verified by the
operator. The existing `POTBOT_WORKER_*` telemetry settings, pinned monitoring
executables, delegated cgroup and kernel exit accounting are mandatory. Do not
relax host permissions or suppress monitoring to obtain a passing result.

For a modern image with `/etc/ai` and `/etc/ai-past`, provide both backing stores
and use the dedicated fixture's history monitor. Do not start a second watcher
on an already managed production workspace. Provision the report parent with
owner-only permissions before invoking the command. For example, inside the
qualified Linux/WSL2 operator environment:

```bash
uv run --locked python scripts/qualify_shared_workspace.py \
  --rootfs "$SMOKE_ROOTFS" \
  --home "$SMOKE_HOME" \
  --model-staging "$SMOKE_STAGING" \
  --shared-models "$SMOKE_MODELS" \
  --shared-tools "$SMOKE_TOOLS" \
  --ai-config "$SMOKE_AI_CONFIG" \
  --ai-past "$SMOKE_AI_HISTORY" \
  --image-digest "$IMAGE_SHA256" \
  --workspace-id "$SMOKE_WORKSPACE_ID" \
  --principal-id "$OPERATOR_PRINCIPAL_ID" \
  --report "$OPERATOR_REPORTS/s47-$(date +%s%N).json"
```

Only an older image without the AI mount destinations can omit the two AI
backing stores. All paths and environment values above must already be selected
and provisioned by the operator; this command does not provision them.

The report schema is now `potbot-sandbox-qualification-smoke/v2`. It includes
the operator scope, a smoke-run identifier, the qualifier script hash, and each
probe's run ID and audit-verification flag. Its checksum covers those fields.
Do not reinterpret an old v1 report as v2 evidence. The runner's existing zero
qualification placeholder remains a bootstrap input, not an activated image
pin. Neither this report nor a successful image build automatically approves or
activates the image. Approval, image pinning and full adversarial qualification
remain operator responsibilities.

Exit 0 means all smoke predicates passed; exit 1 means a completed smoke report
contains a failure; exit 2 means setup, execution, shutdown or report custody
prevented completion. An unconfirmed process aborts the suite without publishing
a new success report. Retain its private audit and reconcile it through the
operator; do not automatically clear leases or delete possible live-worker files.

## Regression coverage and verification

The two existing image-validation test files now contain 107 tests. They cover
all four root/agent projection combinations, incomplete configurations, changed
snapshot bytes, missing/symlinked/writable/hardlinked files, workspace binding,
owned FD cleanup, typed process evidence, missing/mismatched audits, per-probe
attribution, monitor absence, unconfirmed shutdown, namespace-read failures,
explicit socket denial, private output and safe marker unlink.

In the preparation environment, those tests passed using the actual changed
source, the real `ContextProjection`/`AgentContextProjection` code, the project's
complete `ProcessRequest` class, and the real S46 private report writer. This was
an isolated partial-source harness: unavailable host services and some unrelated
dependencies were explicit test doubles. It was not a full repository checkout,
installed-wheel test, PostgreSQL run, live monitor or actual Bubblewrap launch.
The verification bundle records that distinction and the exact source hashes.

The same projection matrix against the original launcher produced three failures
and one pass: neither projection, root-only and agent-only failed; both-present
passed. This reproduces the strict-zip regression without a live sandbox.

Run normally from a complete repository with the locked environment:

```bash
uv sync --locked --dev
uv run --locked python -m pytest -q \
  tests/unit/test_shared_workspace_isolation.py \
  tests/unit/test_workspace_qualification_smoke.py
```

Repository CI uses the complete checkout and does not load the preparation
harness. Do not count source-unit results as real host or kernel acceptance.

## Remaining work, in order

1. Re-run the full image validation job and rebuild/publish both images. S47
   addresses the observed validation failures, not unexecuted build/publish steps.
2. Finish repository-wide lint cleanup and rerun type/integration gates without disabling them.
   The main verification run remained red at the inspected baseline.
3. Migrate nested execution-provenance fixtures to admitted sandbox worker
   execution with a bounded child-work protocol. Do not re-enable arbitrary
   host callbacks or weaken exclusive workspace leases.
4. Run S46 and this v2 smoke on the actual qualified operator host, then exercise
   cancellation, forced termination, rapid child exit and audit recovery.
5. Complete live multi-agent Xpra/browser acceptance and the remaining independent
   per-worker SYN process integration. Unit doubles are not evidence for either.
