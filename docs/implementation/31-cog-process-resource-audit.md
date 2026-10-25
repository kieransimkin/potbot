# Phase 31 — operator-owned Cog process and system telemetry

This patch extends the qualified shared-workspace Bubblewrap backend without
mounting any host observability controls inside the guest. Operator launches
`strace -f -ff -ttt -T -yy` before the sandbox executable runs. A child
pre-exec hook joins a dedicated cgroup-v2 leaf *before* exec. Forked descendants
inheriting the cgroup cannot evade lifetime CPU, memory peak and I/O counters
through early exit. The raw syscall audit follows the process tree.

## Operator provisioning

Only the trusted host/operator may set these environment variables:

- `POTBOT_OPERATOR_AUDIT_ROOT` — existing private (0700), non-symlink
  directory outside every guest mount, preferably `/var/lib/potbot/worker-audit`.
- `POTBOT_OPERATOR_CGROUP_ROOT` — existing delegated cgroup-v2 subtree
  with CPU, memory, IO and PID accounting enabled. The operator needs permission
  to create/remove child cgroups and move its own children into them.
- `POTBOT_OPERATOR_STRACE` — absolute host path to the qualified strace
  executable, e.g. `/usr/bin/strace`. Never use a guest-provided executable.
- `POTBOT_OPERATOR_STRACE_SHA256` — exact SHA-256 of that strace binary.
- `POTBOT_OPERATOR_CGROUP_JOIN` — absolute path to the operator-built, trusted
  native launcher described in `native/potbot-cgroup-join/README.md`.
- `POTBOT_OPERATOR_CGROUP_JOIN_SHA256` — exact SHA-256 of the launcher.

The helper runs outside Bubblewrap, joins its private cgroup and then `execv()`s
strace. Python's unsafe `preexec_fn` is **not** used. A missing/mismatched
binary pin fails the launch closed. Requalify both SHA-256 values whenever the
operator image or strace package is updated.

Missing qualification stops shared sandbox execution, rather than running an
unmonitored worker. The legacy Bubblewrap runtime still exists for non-shared
legacy tools; migrate all Cog dispatches to the shared backend before claiming
**all Cogs** are covered. In-process host Cog handlers and non-supervised
Xpra/model child processes need their own operator enrollment.

`POTBOT_OPERATOR_AUDIT_ROOT/<workspace UUID>/<worker UUID>/` contains:
`identity.json`, raw `trace.<pid>` files, per-PID structured `file-events.jsonl`,
`live.json`, and immutable-on-completion `lifetime.json` with process identities,
counts, peak bytes and cgroup lifetime totals. Only the operator can write them.
The 256 MiB default trace ceiling fails the execution closed instead of dropping
file accesses. Raw traces include potential syscall arguments and file paths;
never expose or log them through web APIs. Apply local retention/backup policy.

The read-only `GET /v1/system/utilisation` endpoint requires existing bearer
authentication. The frontend `/api/ui/utilisation` additionally requires its
operator cookie. Both expose aggregate host load, PSI bottlenecks, and
workspace-specific agent CPU share, **not** paths, process command lines or
other workspaces' agent identifiers. An unconfigured monitor reports unavailable.

### Fidelity and next qualification gate

Cgroup counters are authoritative **for the worker cgroup** and include strace
overhead. Per-PID CPU/RSS/IO values obtained from `/proc` are *sampled lower
bounds*, not final per-PID lifetime totals for very short processes. Trace file
coverage includes syscalls on strace-supported Linux ABIs; translating complex
relative/renamed paths into canonical inode identity is still incomplete. Raw
per-PID strace files remain the authoritative forensic record. Structured file
journals now reconstruct strace's unfinished/resumed calls and record the two
pathname roles for rename/link operations. Missing or unpaired continuations
mark the audit incomplete. Per-PID completed syscall count and summed syscall
**wall duration** are persisted, but syscall wall duration is *not* CPU time.
Cgroup lifetime CPU, peak memory and IO totals remain authoritative at cgroup
scope; exact final CPU/RSS/IO for short-lived individual processes still needs
an independently qualified task-exit collector. The 180-second and 2-second UI windows are time-weighted from a continuously
running host sampler, independently of UI polling. Each reading is a normalized
aggregate saturation indicator: maximum of busy CPU and Linux CPU/memory/I/O
PSI. It is not a sum of resource percentages or a GPU capacity indicator.
The first window warms up after API service start. Each agent heat bar currently
represents its share of host *busy CPU time*; it is not yet an IO/GPU attribution.

Production qualification remains necessary for full per-PID exit statistics,
no syscall drops, small performance overhead and strong replay guarantees.
Replace strace with a CO-RE eBPF collector plus task-exit accounting and LSM
file hooks where supported; treat any ring-buffer overflow as indeterminate,
and keep the private cgroup-v2 resource ledger as the accounting boundary.

**Do not treat this development patch alone as a fully qualified production
forensic or hostile multi-agent isolation boundary.**

### Kernel accounting follow-up

The next phase should attach a single operator-owned, cgroup-filtered eBPF task
exit collector (or qualified Linux taskstats listener) **before** the worker
executes. Match exit records against tracee task IDs and kernel task start times,
record per-task user/system CPU, read/write bytes, peak RSS and exit status,
reconcile totals with cgroup lifetime statistics, and fail closed if any event
is missing, truncated or dropped. Include kernel/WSL2 feature checks and
explicit overhead benchmarks. Linux taskstats/netlink may lose kernel-to-user
messages under pressure, so it cannot be treated as inherently lossless.

Agent IDs for `ToolGateway` currently follow the authenticated principal rather
than a separately resolved agent-home identity; they must be transitioned to a
host-owned Cog/agent registry before claiming independently accurate heat bars
for multiple agents operating under the same principal.
