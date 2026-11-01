# S46 — registered worker admission and explicit live smoke

## Status and scope

Based on main `0b38a99a7815b5fb74e04556602333f13fb54f37`.
The existing context-RPC image client from that commit is preserved.

At commit `160fc10e`, S45's artifact read and upload acceptance stages both
completed successfully against PostgreSQL in the main verification workflow. The independent S45 job
stopped earlier: two older worker unit fixtures supplied an empty ledger and no
runtime qualification/rootfs, so they failed at the now-mandatory image-pin
check. This change repairs those fixtures without bypassing that check.

The same main run subsequently stopped at the repository-wide Ruff gate
(`scripts/check.py`): 356 errors were reported across the existing tree. This
patch does not suppress that gate or claim to resolve the lint backlog. The
older execution-provenance integration test was not reached by that run.

This stage supplies an actual `SandboxedCogExecutor` diagnostic runner. It is a
prerequisite for migrating the older executable-work provenance fixture; it is
**not** the completed migration or a production security certification. No
production admission, executor, lease, kernel-audit or sandbox policy is relaxed.

## Changes

The worker unit fixtures now use the existing `WorkspaceLeaseLedger`, a real
image pin, a real on-disk executable hash and the real lease lifetime. Only
process execution and the registered-home lookup are unit doubles. Successful
execution releases its lease; malformed results and unconfirmed termination
retain a conflict. Wrong image, qualification, rootfs, home, binary bytes and
agent identity must prevent dispatch. These tests are not a kernel sandbox.

The image gains `/usr/local/libexec/potbot-worker-probe`, copied from
`sandbox/qualification/worker_probe.py`, mode 0555. It accepts one bounded,
versioned JSON envelope with a fresh nonce and invocation UUID. It reports its
UID/GID, current directory, read-only root mount and whether IPv4/IPv6 socket
creation is denied by permissions. It never connects a socket, accepts arbitrary
commands, changes a permission or writes an agent file. The `--wire-only` mode
is for local protocol tests and is explicitly rejected by live qualification.
An arbitrary socket failure (for example descriptor exhaustion) is not accepted
as evidence of a network policy denial.

`scripts/qualify_sandbox_cog_s46.py` invokes the diagnostic through the existing
executor, its real `SharedWorkspaceBubblewrapBackend`, the real audited
`ProcessSupervisor`, registered agent identities, current image pin, exclusive
lease and operator SYN mesh. A recording supervisor observes `super().run`; it
does not replace execution. One to four selected agents run **sequentially**,
consistent with the executor's exclusive-workspace lease. This does not test
concurrent cross-agent filesystem isolation.

Successful process output is not sufficient. The runner also calls the existing
full `verify_gui_run` audit verifier with `identity_kind="agent"` and the
independently selected agent UUID. Despite its historical GUI name, this verifier
checks the worker monitor's identity, syscall transcripts, process-tree and file
index digests, kernel task-exit coverage and measured cgroup accounting. Only its
bounded summary goes into the smoke report. Private syscall transcripts remain
in the operator audit directory.

## Prerequisites: use an existing disposable qualification workspace

Use native Linux or the existing qualified WSL2 distribution, with its required
interop, mount and kernel restrictions. Do not run this against a production
workspace or weaken the host controls to make the probe pass.

Before invoking the runner, the operator must already have:

* Rebuilt, admitted and pinned a new immutable rootfs containing the exact
  diagnostic bytes from this commit. Do not copy files into an already-qualified
  image or fabricate an image/qualification hash. The existing image build
  workflow is responsible for publication and qualification.
* An existing controller-private lease ledger and one to four registered agent
  homes in that workspace. The runner neither pins an image nor provisions an
  agent nor reconciles an uncertain lease. It reuses the repository's existing
  local ledger; it does not add a persistence backend or replace PostgreSQL.
* The existing `WorkerMonitorConfig.from_environment()` configuration, including
  the approved private audit root, delegated cgroup, pinned tracer/join helper
  and required task-exit accounting. Missing evidence is a failure.
* The active operator attachment/history-monitor configuration required by the
  selected image. In particular, modern `/etc/ai` and `/etc/ai-past` mount layouts
  must retain their live operator watcher. Optional context/skills projections
  are not qualified by this diagnostic.

All mounted directories must be disjoint. Policy, report, ledger and audit
storage must be outside the guest mounts. Policy and report directory are
operator-private; existing ACLs and permissions are checked, not repaired.

## Configure and run

The example `sandbox/qualification/worker-policy.example.json` contains
**deliberately invalid placeholders**, not authorizations. Save an edited copy
in an existing private operator directory. Replace every placeholder with the
existing workspace/agent registration and image pin values. `workspace_name`
must match the registry's group identity. Optional `shared_skills` may be added;
unknown fields, executable overrides, commands and new mount classes are refused.

Commit the patch before qualification. The runner compares the working bytes of
its listed source files against `git show HEAD:<path>`. It refuses source drift,
missing committed files and probe/image digest mismatches. This is exact-source
binding for those listed files, not a signed attestation of every dependency.

From the committed repository on the qualified Linux/WSL2 host:

```bash
uv run --locked --extra web --extra semantics python \
  scripts/qualify_sandbox_cog_s46.py \
  --policy /srv/potbot/operator/qualification/worker-policy.json \
  --report /srv/potbot/operator/qualification/s46-worker-new-run.json \
  --run-live
```

Use a fresh output name for each run. The report parent must already be a real,
operator-owned private directory. The policy must be a private ordinary file,
not a symlink or hardlink. The runner never overwrites an earlier report.

Exit 0 means the narrow smoke passed for every selected agent. Exit 1 means no
pass is established. Standard error gives a bounded phase and exception class;
detailed evidence remains in the existing private operator logs. An unconfirmed
worker may leave an indeterminate lease by design: use the existing operator
reconciliation procedure rather than deleting the ledger or rerunning blindly.

The report binds the commit, listed source hashes, image and qualification
hashes, diagnostic hash, selected agent/run IDs, observed guest identity,
isolation-identity digest and verified audit summary. It is not signed.

## CI

`.github/workflows/sandbox-worker-s46.yml` runs source contracts on hosted CI.
That job cannot establish a live-host pass.

The live job requires all of: manual workflow dispatch on this repository's
`main`, `run_live=true`, passing source contracts and a self-hosted Linux runner
labelled `potbot-worker-qualified`. Configure the `worker-qualification`
environment with required reviewers and main-only deployment protection **before
enabling this job**. Set its `POTBOT_S46_POLICY` variable to the private policy
path (not policy contents). Provision monitor/operator environment settings on
the dedicated runner. Do not expose that runner to untrusted pull-request jobs.

The live job serializes qualification, does not cancel a running worker on a new
request, and uploads only the bounded report after success. It does not upload
syscall transcripts, capabilities, credentials or raw browser evidence. A skipped
live job is not a pass and must not be promoted to production qualification.

## Tests and remaining acceptance

Run source contracts in the complete checkout:

```bash
uv run --locked --extra web --extra semantics python -m pytest -q \
  tests/unit/test_sandbox_cog_worker.py \
  tests/unit/test_sandbox_worker_acceptance_s46.py \
  tests/unit/test_worker_probe_s46.py
```

Local protocol tests execute the actual diagnostic file only in `--wire-only`
mode. Validation tests deliberately inject synthetic records; neither constitutes
real kernel evidence. The accompanying delivery verification report distinguishes
what was executed locally from what is wired for CI or requires a live host.

Still required after this patch:

1. Resolve the repository-wide lint/format/type baseline and run the complete
   source suite. Run this live worker smoke on the qualified host as a separate
   gate; hosted unit success does not imply a live pass.
2. Migrate `tests/integration/test_execution_provenance.py` from arbitrary
   `HostOperation` workers to admitted executables, including a real bounded
   child-work protocol/orchestration path for its nested workflows. The current
   one-shot worker stdin/result transport does not itself implement that protocol.
   Do not replace it with a host callback, remove its gate, or label the S46 smoke
   as PostgreSQL execution-receipt acceptance.
3. Complete real-host browser qualification separately: private Xpra streams,
   leases, cross-agent boundaries, HTTPS rules, PostgreSQL evidence readback and
   operator/display failure recovery. The probe does not launch a browser.

The report explicitly lists these excluded claims. The existing main verification
workflow and legacy provenance test are retained, not skipped or made optional.
