# Phase 38 — monitored Cog operator release gate

The Phase 34 sandbox-only work dispatch and kernel TASKSTATS exit collector
already exist. This gate validates their prerequisites **on the target host**;
it does not substitute for integration tests, and it does not itself install
sandboxing or syscall hooks.

Run from the repository root using the same environment, kernel, effective
privileges, pinned executables and cgroup delegation as the operator:

```bash
python scripts/qualify_operator_workers.py \
  --rootfs /PATH/rootfs --home /PATH/workspace-home \
  --model-staging /PATH/staging --shared-models /PATH/models \
  --shared-tools /PATH/tools --image-digest IMAGE_DIGEST \
  --qualification-sha256 QUALIFICATION_SHA256 \
  --report /PATH/operator-private/worker-qualification.json
```

This invokes the existing Phase 35 real Bubblewrap/cgroup/fork and audited
process-tree smoke test, rather than trusting an arbitrary success command.
It must additionally be paired with the registered-agent Cog-dispatch and
forced cancellation/timeout integration suite for release sign-off.

The runner fails nonzero when Linux/cgroup v2/configuration is missing, if
TASKSTATS cannot register or account for every probe, if the live integration
command fails/times out, or if any required stage is omitted. A successful
exit is a prerequisite, **not** proof of comprehensive forensic completeness:
a separate release assessment must still verify syscall loss detection,
namespace escape tests, full Cog-route enumeration, and operation under stress.

Do not publish raw trace output or private filesystem paths with the JSON
qualification summary. Pin the approved operator image/build and retain
qualified test evidence for each deployment. Re-run after any kernel, cgroup,
strace, taskstats, sandbox image or dispatch change.
