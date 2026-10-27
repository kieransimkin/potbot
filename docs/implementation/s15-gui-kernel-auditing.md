# S15 — Kernel-accounted Xpra and controlled-browser process trees

**Base commit:** `ba8e413d2740282f9dd1712a8eb583b09cf89cb2` (27 October 2026).
**Mode:** host-only opt-in; mandatory if the running Xpra backend attaches to S13's
managed operator. Not a new browser executable, sandbox, telemetry format, or
an exception to S14 owner fencing.

## Scope

Every managed Xpra display, graphical application, and controlled-browser
subprocess is launched through Potbot's **existing** `WorkerMonitorSession`:

- An operator-pinned `cgroup_join` helper places the wrapper into a delegated
  cgroup-v2 leaf before it can spawn `strace` or any Xpra/Chromium child.
- `TaskstatsExitStream` subscribes to kernel exit events *before* launch.
- `strace -f -ff -ttt -T -yy` accounts for forked child processes and per-PID
  file-related syscalls, including opening modes and observed failures.
- The private host audit has cgroup CPU/IO/memory totals, process-tree index,
  per-PID syscall lifetime and file-access index with SHA-256 checksums.
- On every supervised exit, the app lease remains held until the audit has
  finalized. Unconfirmed descendants, missing exits, trace overflow, bad
  hashes or an unhealthy collector make the GUI operation **indeterminate**.
  The controller must reconcile it; no retry or clean lease completion.
- Browser lease tokens and S39's strict Xpra input/egress policy remain unchanged.
  Raw audit paths, shell arguments, CDP content and filenames stay outside UI.

Operator GUI and the shared Xpra display are recorded as `identity_kind=principal`.
For S40 private agent browsers and their separate displays, attribution is
`identity_kind=agent` **only after** `AgentHomeRegistry.lookup` verifies the
agent's enrolled workspace identity. Unverified agent IDs cannot contribute to
agent utilisation. Each agent display is independently monitored; it does not
share an audit cgroup with the browser launched within it.

## Operator prerequisites

On the qualified Linux/WSL2 host, configure these existing host-only settings:

```sh
POTBOT_OPERATOR_AUDIT_ROOT=/host/private/gui-audit
POTBOT_OPERATOR_CGROUP_ROOT=/sys/fs/cgroup/potbot
POTBOT_OPERATOR_STRACE=/usr/bin/strace
POTBOT_OPERATOR_CGROUP_JOIN=/host/operator/bin/potbot-cgroup-join
POTBOT_OPERATOR_STRACE_SHA256=EXACT_HEX_SHA256
POTBOT_OPERATOR_CGROUP_JOIN_SHA256=EXACT_HEX_SHA256
POTBOT_XPRA_SHA256=EXACT_HOST_XPRA_EXECUTABLE_SHA256
```

The audit root must be 0700, the cgroup subtree delegated exclusively to the
controller, and all listed executable hashes approved by the operator. The
sandbox must not mount those paths. A managed Xpra service **fails closed** when
these prerequisites, the S13 operator socket, or kernel TASKSTATS are missing.

The existing private Xpra application policy retains its pinned workspace,
principal, rootfs, AI config/history, strict input and allowed browser CONNECT
hosts. Only the registered S40 agent-display path supplies per-agent audit
identity; no extra agent credentials or secret route is introduced.

## Acceptance on the real host

First start the managed operator and Xpra with real qualified cgroup/strace
settings. Launch an approved graphical application and a controlled browser,
confirm an interactive browser action, and terminate both through their
existing API endpoints. Observe that the browser and its descendant tasks
leave a private `lifetime.json` record with `audit_complete=true`.

For each completed app, supply its Xpra application session ID as run ID:

```sh
PYTHONPATH=src python scripts/qualify_gui_telemetry_s15.py \
  --audit-root /host/private/gui-audit \
  --workspace-id WORKSPACE_UUID \
  --run-id APP_SESSION_UUID \
  --report /host/private/s15-app-qualification.json
```

A separate Xpra host/display worker has its own UUID in the private identity
record. Do not mistake the display process for one of the app session IDs.
The qualification script verifies the real private index SHA-256 hashes,
TASKSTATS coverage, a complete process tree and cgroup totals; it reports
only bounded counts and totals. No raw file paths are exported.

Also deliberately break the operator socket on a *disposable* workspace,
trigger trace overflow and interrupt the monitored process. All such tests must
fence active browser commands, terminate associated child process trees,
produce an incomplete/indeterminate receipt and prevent a clean GUI lease.

## Known limitations and future gates

This increment is a **prepared backend implementation**. It has not yet been
qualified on this session's actual host, and no real Xpra/WSL2 task accounting
was executed here. It does not prove process isolation from Windows, cover
other WSL hosts, or provide cluster-wide operator fencing. Host Xpra's current
log path under the workspace must be redirected to the approved log mount in a
follow-up; kernel trace and file indexes in this increment remain host-private.

The GUI support code does not elevate the existing syscall trace to a filesystem
mediation policy. It observes access; authorization and system-write isolation
continue to come from existing Bubblewrap and the S13 operator controls.

## Implementation/checks

```sh
PYTHONPATH=src uv run --locked python -m pytest -q \
  tests/unit/test_gui_resource_audit_s15.py \
  tests/unit/test_gui_resource_integration_s15.py \
  tests/unit/test_gui_agent_attribution_s15.py \
  tests/unit/test_gui_audit_qualification_s15.py \
  tests/unit/test_xpra_application_sessions.py \
  tests/unit/test_xpra_operator_fence_s14.py
```

Run host CI and standalone Xpra socket/network/WSL2 qualifications separately.
