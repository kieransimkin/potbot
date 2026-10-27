# S16 — Independent GUI audit verification and restart inventory

S16 is an additive, host-private audit consumer built on the existing S15
`WorkerMonitorSession` output. It does **not** start workers, import executable
plugins, delete traces, finalize leases, reconcile kernel cgroups or expose a web
endpoint. The S15 qualifier remains available; the stricter S16 verifier is
separately invoked by the operator.

The qualifier verifies the SHA-256 of the raw trace stream and both JSONL
indexes, and validates the contents of those indexes. The exact traced PID set
must equal the kernel-exit process set and the set of nodes in the process tree.
It checks the single root, parent relationships, cycles, file-access mode
counters and the actual file event count. Audit integrity counters may exist
with zero values; positive counters fail qualification. Private files must be
owned by the operator, non-symlinked and inaccessible to group/other users.
Only summaries leave the private audit directory; raw paths and trace contents
are never copied into reports.

The optional `--agent-id` selects an enrolled S40 browser identity. It is the
**operator's responsibility** to select it from trusted admission data, not
from the browser or model. Agent qualification refuses an unspecified agent ID.
For a principal-owned GUI session, use `--principal-id` (or omit the selector
for legacy principal-only checks).

```bash
PYTHONPATH=src python scripts/qualify_gui_audit_s16.py \
  --audit-root /host/private/gui-audit --workspace-id WORKSPACE_UUID \
  --run-id APP_SESSION_UUID --principal-id PRINCIPAL_UUID \
  --report /host/private/s16-qualification.json

PYTHONPATH=src python scripts/audit_gui_recovery_s16.py \
  --audit-root /host/private/gui-audit --workspace-id WORKSPACE_UUID \
  --report /host/private/s16-recovery.json
```

`inspect_recovery` classifies completed and incomplete runs and stale active
markers without altering any of them. Reports that require reconciliation are
not permission to clear a lease. The operator must separately prove that all
processes and descendants have exited and reconcile the relevant S15 ledger.

The verifier reads a finite, private snapshot; it is not a transactional
filesystem snapshot or a replacement for kernel-level admission controls.
Live Bubblewrap, cgroup-v2 and TASKSTATS tests still require a qualified
Linux/WSL2 host. File names are lexical syscall evidence, not unique inodes.
