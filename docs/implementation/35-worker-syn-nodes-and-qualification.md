# Phase 35 — monitored workers, local SYN node heartbeat mesh and live qualification

## Context and prerequisite

Apply the **combined Phase 34–35 patch on top of Phase 33**. The operator's
already qualified pinned Bubblewrap image, cgroup v2 directory, `strace` hash,
`potbot-cgroup-join` hash and Linux TASKSTATS listener remain mandatory.
The patch does not relax seccomp or introduce guest-to-guest TCP/Unix sockets.

## One local SYN node instance per worker invocation

The `SandboxedCogExecutor` now requires an injected, **operator-owned**
`SynWorkerNodeMesh`. Every admitted Cog gets an individual `SynWorkerNode` actor
identified by a stable workspace/run-bound SYN thing reference plus a fresh
incarnation UUID. A coroutine emits a heartbeat immediately before Cog dispatch
and every five seconds (configurable, 50 ms–60 s). All other live nodes in the
same workspace receive the typed heartbeat through the trusted relay, not from
user/model-controlled payloads. Per-node peer snapshots report last sequence,
last-seen age and a **suspect** state after three intervals without a beat.
Node faults become a worker cancellation condition and must prevent successful
lease completion while work is still executing; cancelling does not prove the
worker's cgroup has emptied.

The local envelope is `synny://type/potbot-worker-heartbeat-v1`; its schema is
`vocab/potbot-worker-heartbeat.schema.json`. The node Type is
`synny://type/potbot-worker-syn-node-v1`. Heartbeats include workspace, agent,
invocation/run, node, incarnation, monotonic time, sequence and status. The
operator supplies these identifiers from its admitted request; no worker can
choose a peer's identity by sending arbitrary input data. Increasing sequence
and stable incarnation are required. Peers from other workspaces are invisible.
The relay caps active nodes to 128 per workspace and exposes a bounded,
path-free read-only status projection for a future UI/API integration.

**Important implementation boundary:** this Phase 35 node is a distinct
**in-operator async runtime instance per worker**, *not yet a separate external
SYN daemon executable inside every sandbox*. No independently installable SYN
node runtime or peer transport was found in the available Potbot/Synsphere
repositories, so this is the protocol-compatible local implementation. The
existing SYN/Ember signing architecture remains the source of future
multi-host authentication; this local process does not claim Ed25519 signing or
cross-host consensus. Before replacing the local relay with a real SYN-node
binary, pin its source/build SHA, bind it to each agent's wallet hierarchy,
launch the instance inside a qualified sandbox with operator-only pipe/FD relay,
and qualify cancellation/restart/replay protection. **Do not treat heartbeat
messages alone as proof of a process's liveness or sandbox isolation.**

## Additional host audit hardening

- Parent/child trace relationships now detect disconnected cycles as well as
  multiple roots, missing descendants and fork conflicts. A graph with one root
  and an extra two-node cycle is **not** a complete process tree.
- Shutdown attempts `cgroup.kill` even if the outer tracer's parent is not yet
  confirmed stopped, so orphaned descendants cannot survive merely because the
  supervisor entered an indeterminate state. The operator never upgrades an
  unknown parent exit to a successful receipt just because `cgroup.kill` was
  written. A nonempty cgroup stays fenced.
- SYN node heartbeat failure, lease renewal failure, kernel taskstats failures,
  incomplete strace coverage and unconfirmed termination independently prohibit
  normal worker completion. Paths and syscall buffers stay in the operator's
  private journal, not peer messages or the UI.

## Tests and live gates

Focused offline tests cover peer delivery/isolation, duplicate/replay and
incarnation conflicts, bounded registration, heartbeat failure, worker
integration, cgroup cleanup, disconnected process cycles and qualification
predicate failures. No mock demonstrates real TASKSTATS privileges or kernel
namespace isolation. To qualify an actual installation, first run:

```bash
python scripts/qualify_worker_exit_accounting.py --children 12
```

Then, with the operator's `POTBOT_WORKER_*` monitoring variables configured,
and **using a host-provisioned immutable system image and qualified mounts**:

```bash
python scripts/qualify_phase35_worker.py \
  --rootfs /srv/potbot/images/pinned \
  --home /srv/potbot/workspaces/fixture/home \
  --model-staging /srv/potbot/model-staging \
  --shared-models /srv/potbot/models \
  --shared-tools /srv/potbot/tools \
  --image-digest "$IMAGE_SHA256" \
  --qualification-sha256 "$QUALIFICATION_SHA256" \
  --report /srv/potbot/operator/phase35-qualification.json
```

The live script starts actual Bubblewrap + shell + two forked jobs that open and
write sandbox files. It requires a stopped process tree and at least three
kernel task-exit records, a complete audited trace, and a pinned isolation
identity. It returns nonzero on missing capabilities. Repeat with cancellation,
SIGKILL, rapid fork/exit, processor hotplug and concurrent agent loads before
production; a successful smoke test is necessary but **not** a comprehensive
hostile-code qualification.

## Remaining integration work

- Existing integration fixtures based on arbitrary in-process `HostOperation`
  callbacks need migration to sandbox-executable fixtures; this development
  patch cannot claim the full PostgreSQL suite passes. Controller-only trusted
  mediators remain host-side and are distinct from executable worker Cogs.
- The canonical SYN type registry/import route and durable SYN heartbeat event
  replay must be bound to the actual SYN node project before cross-host use.
- On-disk audit journals must be retention-managed and privilege reviewed;
  per-PID TASKSTATS is unavailable on some kernels/WSL deployments.
- A native, per-worker sandbox-resident SYN node *process* requires the real
  runtime distribution; do not silently substitute a public unrelated `syn`
  npm package.
