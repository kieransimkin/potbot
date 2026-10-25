# S5 — Shared tools: readonly execution and per-tool maintenance writer

**Scope:** the existing `/usr/local/tools` store and the `SharedToolStore` approval/cleanup workflow are authoritative. This increment closes the ordinary-execution write mount that previously let an unbrokered Bash process change any shared tool. Tools remain physically shared and group-writable when a trusted installer is admitted.

- Normal interactive shells and tool executions see `/usr/local/tools` through an **OS read-only bind mount**. The `/home/<agent>/Tools` symlink continues working and does not grant write permission.
- Only the controller-provisioned `ToolWriteGrant` may add a **nested writable bind** for a single `/usr/local/tools/<slug>` installation. Its process must be issued from the verified system rootfs, with a registered `AgentHome`, pinned command, matching agent UID/GID, and active exclusive `shared_tool/<slug>` lease.
- The `SharedToolInstallExecutor` keeps the guard active while the supervised child runs. Failed, timed-out, fenced or uncertain operations leave a blocking indeterminate lease; successful exit alone never registers a tool or proves cleanup.
- The operator-supplied `postflight` must independently validate actual writes, expected artifacts, exact bytes and clean routine. Its SHA-256 evidence is recorded separately from the process output receipt. A subsequent `SharedToolStore.register` step remains mandatory.
- `SharedWorkspaceBubblewrapBackend` accepts these grants **only from host-side construction**, never guest environment variables or serialized tool requests. Existing broad-home workspace leases are still required for arbitrary write-capable shells; per-tool locks are cooperative unless the OS mount restrictions qualify on the actual host.

## Acceptance still needed

Run real Linux/WSL2 Bubblewrap checks to prove a normal agent **cannot write** `Tools/<other-tool>`, while an admitted installer can write **only** its own tool, and cannot swap a verified binary after pin/launch. Repeat under concurrency, symlink/rename attacks and lease fencing. This increment supplies tests of mount planning, but those are not substitutes for host qualification.
