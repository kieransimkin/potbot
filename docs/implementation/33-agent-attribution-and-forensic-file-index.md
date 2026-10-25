# Phase 33 — verified agent attribution and process/file index

This is an incremental hardening step **on top of Phase 32**. It does not claim
production-grade per-task CPU/I/O totals or replace the qualified cgroup-v2
boundary. Its changes are intentionally private to the operator except for the
existing aggregate utilisation view.

## Correct agent identity

The authenticated workspace `WorkspaceContext.principal_id` identifies a user
or service principal, *not* an AI agent. Before this phase, general ToolGateway
launches reused that UUID as their `telemetry_agent_id`, producing misleading
agent heat bars when two agents acted under one principal.

There are now two explicit attribution kinds:

- `agent`: an `AgentHomeRegistry` on the same pinned workspace lease ledger
  identifies the exact configured agent UUID at dispatch time. Native shared-tool
  installation already validates the registered agent home and uses this kind.
- `principal`: a gateway without an explicitly host-bound agent retains its
  authenticated principal attribution in private audit, but never contributes
  to the **agent** heat bars. The live UI worker list suppresses its subject ID.

A trusted host controller may construct a per-agent `ToolGateway` using
`agent_homes=<AgentHomeRegistry>` and `host_agent_id=<UUID>`. Both must be
present, and the registry must use the same `WorkspaceLeaseLedger`. The gateway
re-checks the registration at every execution dispatch, after normal admission.
Neither identifier can be chosen by model-generated tool arguments. Existing
single-principal gateways remain valid and use the explicitly labelled
principal attribution until a verified agent is selected by the operator.

## Private per-worker process tree

`process-tree.jsonl` records one observation per traced PID/TID with:

- fork/clone/vfork parent PID, process versus thread relationship, observed
  execution/exit markers, first/last syscall timestamps;
- sampled Linux `/proc` CPU ticks, RSS pages, read/write byte observations,
  linked using `pid:start_ticks` where available;
- that PID's syscall counts, syscall wall duration and file-event count.

A successfully returned child PID without a matching `strace -ff` transcript
causes `tree_complete=false`. Conflicting parent edges or multiple unlinked roots
also cause an incomplete result, preventing successful completion of the Cog.
`start_ticks` is **not** invented for short-lived processes missed by sampling;
there remains a potential PID-reuse limitation. The tree reflects trace evidence,
not guaranteed kernel parentage or exact process runtime accounting.

## Private per-file index

`file-index.jsonl` groups the existing private `file-events.jsonl` records by
PID and strace-reported pathname. It retains each accessed or attempted access
mode, earliest/latest timestamps, and both successful and failed-attempt counts.
Relative paths are labelled lexical, not asserted to represent canonical inode
identity. Records such as a failed `openat` never masquerade as a successful
file read. Both indexes are created with `O_EXCL|O_NOFOLLOW`, mode 0600, fsynced
before lifetime receipt, and SHA-256 digests are included in `lifetime.json`.
Audit path/entry budgets fail closed rather than silently truncate records.

The compact `lifetime.json` stores index digests, counts, and cgroup lifetime
counters. Per-PID detailed measurements have moved into the separately bounded
process-tree journal so thousands of processes do not overflow the receipt's
small JSON size limit. No raw paths, command lines, syscall arguments or
principal IDs are exposed by the public read-only utilisation route.

## Bounded live-worker index

Each worker now maintains a separate operator-only
`<audit-root>/<workspace-id>/.active/<worker-id>.json` snapshot. The sampler
updates it together with the private worker record, and a confirmed stopped
cgroup removes the active entry. Faulted/unconfirmed processes are left for
operator reconciliation; stale records are rejected by the reader. The UI
enumerates this active index rather than the first 256 *historical* worker
folders, so completed job history cannot starve new jobs from the display.
A rolling Phase 32 deployment without `.active` uses the old bounded scan and
marks an overflow rather than silently claiming complete heat attribution.

The reader prunes inactive worker CPU baselines and scopes them by workspace,
preventing unbounded growth and cross-workspace baseline mixing. More than
1,024 indexed active workers makes the result explicitly partial; verified
agent heat totals are withheld rather than showing percentages calculated
from a subset of the running workers.

## Verification / remaining gates

Unit tests cover short-lived and threaded fork relationships, interrupted
clone entries, missing tracee detection, symlink rejection, indexed modes,
failed attempts, register/ledger identity checks, and UI attribution exclusion.
Live workload qualification still requires a Linux/WSL2 operator configured
with delegated cgroup-v2 subtree, pinned `strace` and native cgroup join helper.

**Scope boundary**: this increment instruments the shared-sandbox
`ToolGateway` and shared-tool installer process launches. Trusted in-process
`ExecutionCogRuntime` handlers are not automatically moved into the sandbox by
this patch. Enforcing sandbox execution for *every* Cog worker requires a
separate dispatch-path audit and migration; this patch must not be used to
claim that guarantee.

**Still outstanding**: exact end-of-life CPU/user/system nanoseconds, I/O byte
counters, and peak RSS *for each PID* must come from a qualified task-exit
collector (CO-RE eBPF or verified taskstats), with loss detection and matching
by PID plus kernel start time. A `strace` transcript does not provide that data.
The monitor's cgroup CPU includes tracer overhead; the UI agent heat bars show
only verified-agent share of **host busy CPU**, not all resource utilisation.
