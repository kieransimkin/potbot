# Phase 34 — per-task kernel exit ledger and mandatory worker sandbox

This increment builds on Phase 33 (`33-agent-attribution-and-forensic-file-index.md`),
NOT directly on Phase 32. It combines the two outstanding backend safety changes:
exact Linux kernel-reported per-task exit accounting and removal of the unsafe
arbitrary Python callback route for *executable work Cogs*.

## Process lifetime accounting

- The operator registers a Linux **TASKSTATS** generic-netlink exit listener
  before the first worker syscall. This is a kernel accounting interface (not
  a CPU/syscall-duration estimate, strace log, or `/proc` sampling loop).
- The listener subscribes to **all online CPUs**, watches
  `TASKSTATS_TYPE_AGGR_PID` and never double counts `AGGR_TGID` aggregates.
  It collects task exit events in an operator-private, bounded in-memory map;
  unrelated process names/arguments are neither saved nor published.
- After `strace -ff` has stopped, join each traced process **PID** to exactly
  one kernel exit event, checking 64-bit task birth seconds. Missing entries,
  duplicate/reused PIDs, taskstats version lacking required fields, netlink
  errors, kernel overflow and listener loss cause an incomplete audit and
  prevent a successful work receipt.
- `process-tree.jsonl` retains per-PID CPU user/system microseconds, peak RSS,
  kernel physical read/write byte counters, char I/O and fault counts. Entries
  continue to include the source task's syscall history and lexical file
  access index from Phase 33. All indexes remain outside sandbox mounts.
- **Exact scope:** these are the kernel's final accounting counters for each
  traced task/thread, not an approximation from 200 ms samples. They are not
  device-wide energy use, network traffic, GPU/VRAM accounting, nor exact
  instantaneous memory samples. The operator's cgroup total includes the
  tracer, while per-PID taskstats counts are only for traced tasks.
- RSS is the kernel-reported high-water RSS (KiB converted to bytes). Disk
  I/O counters follow Linux `taskstats` semantics: filesystem/block-accounted
  bytes, not every `read()` return value. `read_char`/`write_char` are provided
  separately; one should not add them to physical I/O bytes.

### Requirements and qualification

Supported: Linux x86-64/aarch64 (the existing qualified shared Bubblewrap
backend), a kernel with `CONFIG_TASKSTATS`, `CONFIG_TASK_XACCT` and
`CONFIG_TASK_IO_ACCOUNTING`, generic netlink taskstats registration rights,
and the previously required delegated cgroup-v2 tree, pinned native cgroup
launcher and pinned `strace` binary. TASKSTATS registration often requires
`CAP_NET_ADMIN` in the appropriate namespace; the operator, **not agents**,
receives any necessary capability. No taskstats sockets or audit directories
are mounted into the worker.

Run `python scripts/qualify_worker_exit_accounting.py` as the privileged
operator. The script must print `"qualified": true` and exit zero. Separately
run a real Bubblewrap cgroup integration test with fork/clone/exec, graceful
termination, timeout and forced cancellation. Expect a nonzero result (never
an unmonitored fallback) on a kernel without taskstats support, registration
privilege, task exit data or a healthy telemetry directory. CI without those
capabilities runs offline protocol tests but **does not qualify** production.

The `TaskstatsExitStream` is a bounded per-worker listener. It can receive
unrelated system task exits during a very busy, long-lasting worker. Exceeding
its capacity intentionally fences successful completion; operators may need a
central multiplexed listener in high-concurrency deployments. Netlink exit
sampling is not a cryptographic trust mechanism and same-second PID reuse is
not impossible; two events for the same traced PID also fence completion.

## Cog execution policy

- `SandboxedCogOperation` is constructed by the operator from an installed,
  versioned `WorkBinding`, a fixed *absolute guest executable path*, and the
  **exact binary SHA-256**. No model-supplied shell commands or host paths.
- `ExecutionCogRuntime` refuses legacy `HostOperation` instances in the
  **work operations** list. It requires an operator-created
  `SandboxedCogExecutor` bound to a qualified image, the same workspace lease
  ledger, an agent-home registry and a `SharedWorkspaceBubblewrapBackend`.
- Each work invocation resolves the agent through its registered workspace
  home, verifies the image and executable, claims the workspace lease, and
  sends one small typed JSON stdin envelope. The worker writes exactly one
  bounded typed JSON response; any unsolicited/faulty output fails.
- The worker runs with its registered UID/GID, private namespace, fixed guest
  workdir, resource budget, syscall tracing and taskstats exit accounting.
  Cancellation asks the operator to terminate the entire audited process tree,
  and an unconfirmed process/audit leaves its lease indeterminate.
- **Controller-only services remain host-side**: receipt signing, artifact
  range/upload mediation, database state, credential brokers and debugger
  control. Those operations must not execute arbitrary model-supplied Python.
  Existing direct artifact operations are installed by trusted services, not
  supplied as worker installations.

### Migration / rollout

The former `HostOperation` generic fixture handlers used in
`tests/integration/test_execution_provenance.py` and the artifact range/upload
fixture tests must be migrated to `SandboxedCogOperation` backed by a qualified
fixture executable (or rewritten to exercise explicit trusted controller
services). The default runtime now rejects those arbitrary in-process
installations. This is an **intentional breaking change**, not a fully green
integration-suite claim. No test-only production bypass is provided.

Work Cogs invoked without a sandbox executor fail closed; operations with
`operations=()` and the sealed controller-only artifact/receipt services remain
admissible. The pure typed transformation and canonical-contract helpers
continue to execute as controller code: they are not agent subprocess workers,
so their privileged/nonprivileged division must be maintained during further
isolation hardening.

## Validation ledger

- Offline packet tests: Linux `taskstats` UAPI field offsets, netlink attribute
  bounds, duplicate/missing PID event fencing and exact per-task counter units.
- Offline worker tests: whole tree reconstruction, private file indexing,
  kernel resource correlation, agent identity binding, worker-index overflow,
  sandbox executor admission, typed result parsing and registered guest UID.
- Still required on the actual operator machine: TASKSTATS registration smoke
  qualification, a real fork-heavy Bubblewrap run and the updated Postgres Cog
  integration tests. No kernel privilege is available in the artifact runner.
