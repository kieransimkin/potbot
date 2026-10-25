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
- `POTBOT_OPERATOR_STRACE` — absolute host path to a pinned executable, e.g.
  `/usr/bin/strace`. Never use an executable from `/home` or shared tool store.

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
per-PID strace files remain the authoritative forensic record. The 180-second and 2-second UI windows are time-weighted from a continuously
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
