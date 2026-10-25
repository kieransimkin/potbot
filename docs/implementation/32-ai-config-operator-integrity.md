# S7 — Full /etc/ai history audit and operator lifetime

**Baseline:** `kieransimkin/potbot` `bd04a3add52380b33d02eb4524595e5acb982e35`, after S5+S6. Linux-only; no schema migrations. This increment strengthens the host journal; it is not a new untrusted guest API.

## Why

S6 captured settled file-state reversals, but trusted the most recent `head.json` enough to restart even if an older diff or a previous binary payload was corrupted. It also walked the filesystem by pathname: `O_NOFOLLOW` on a leaf does **not** prevent a concurrent writer replacing an intermediate directory with a symlink between scans. A separate operator controller factory was needed to keep the watcher running alongside admitted work.

## Changes

1. `ai_config_history._scan` now recursively holds Linux `O_DIRECTORY|O_NOFOLLOW` descriptors, traverses with `dir_fd`, checks the same inode/metadata before and after reading, and rejects hardlinked files that could be changed outside the watched namespace. Races fail closed rather than causing traversal outside the workspace.
2. `ai_history_audit.audit_ai_history` examines **every** numbered `.reverse.json` / `.reverse.diff` pair, checks a continuous sequence, enforces byte/document/revision limits, rejects other files and duplicate JSON keys, checks file bytes against their hashes (not merely hashes written in `head.json`), verifies the human diff, and walks every inverse back to the original baseline. The report contains only metadata and digests, never historical configuration bytes.
3. `AiConfigHistoryMonitor.start` now calls that complete audit while it holds the singleton operator lock, before admitting a live sandbox. A missing, altered or incomplete old revision prevents restart. No automatic reset/repair of a compromised history exists.
4. `AiWorkspaceOperator` holds the single `AiConfigHistoryMonitor`, pinned `SharedWorkspaceRuntime` and `AiManagedProcessSupervisor` together. `gateway_parameters()` supplies exactly these objects to the existing `ToolGateway`. Normal executable dispatch is cancelled if the monitor fails or the operator begins shutdown. `close()` requires supervised work to be terminated and **all** workspace leases (including detached Bash sessions) to have been settled, then flushes and stops the monitor. It refuses to silently drop evidence when a session is still active.
5. `scripts/audit_ai_history.py` performs read-only, offline verification with the operator stopped. It exits nonzero on any inconsistent state. This is a recovery diagnostic, **not** automatic restoration.

## Integration example (host/controller process)

```python
from potbot.tools.ai_workspace_operator import AiWorkspaceOperator

with AiWorkspaceOperator(
    workspace_id=workspace_id,
    ledger=workspace_lease_ledger,
    runtime=pinned_shared_runtime,
) as owner:
    gateway = ToolGateway(
        ...,
        **owner.gateway_parameters(),
    )
    # Keep the owner running for the lifetime of every execution, PTY and
    # approved tool installation. Pass owner.monitor to all other executors.
```

Creating an operator object does not automatically launch a service or install a kernel sandbox. The host controller must adopt this owner at its actual workspace startup point; deployments that use other process constructors should continue to fail closed if S6 mounts are configured without the monitor.

`AiWorkspaceOperator.close()` marks its managed supervisor as closing and cancels supervised subprocess work. Detached PTYs are launched through `SharedBashSessionSupervisor`, not this `ProcessSupervisor`; their own watcher health check and workspace lease apply. The host must explicitly terminate those PTYs before stopping the operator. If any lease is active or indeterminate, `close()` refuses to stop history capture. A controller crash can still leave an indeterminate process; reconcile it independently before an operator restart.

## Audit command

```bash
uv run --locked python scripts/audit_ai_history.py \
  --past /srv/potbot/workspaces/<workspace>/ai-past \
  --config /srv/potbot/workspaces/<workspace>/etc-ai
```

Run after the last guest process and watcher are stopped. The command acquires the history's nonblocking exclusive lock, so it refuses to audit an actively changing journal. A readback pass is also performed on each watcher restart while the operator already owns that lock. `--config` requires the live root to match the persisted latest snapshot byte for byte; omitting it audits only history.

**Retention:** Full replay may be expensive for many revisions; the default offline audit caps at 50,000 revisions and 2 GiB of history reads. Hitting a limit fails closed rather than skipping a damaged period. Plan a separately verified, content-addressed checkpoint and archival policy before this store grows indefinitely.

## What is NOT proven

- Inotify events are coalesced and cannot attribute every individual write syscall to a particular process or recover intermediate values lost between snapshots.
- Stored SHA-256 digests detect accidental damage or guest-disallowed edits, but are not append-only hardware attestations against a malicious privileged host operator.
- A successful unit test/mock launch is not equivalent to a real live Bubblewrap + seccomp + WSL2 + kernel DAC qualification. Test with group-write permissions, multi-agent processes, directory symlink attacks, history read-only mounts and abnormal exits on the actual host.
- Automatic reversal/rollback of a live `/etc/ai` tree is **not** enabled. A future restore tool must obtain an exclusive workspace lease, terminate guest writers and journal the compensating transaction; simply applying a text diff is unsafe for binary files and directory changes.

## Tests

```bash
uv run --locked python -m pytest -q \
  tests/unit/test_ai_config_history_s6.py \
  tests/unit/test_ai_history_audit_s7.py \
  tests/unit/test_ai_workspace_operator_s7.py

uv run --locked python scripts/check.py --with-sandbox
```

The first suite uses actual Linux inotify and FD traversal. The operator test uses a real workspace-lease fixture but **mocks subprocess execution**; the final Linux/WSL2 isolation qualification remains a separate release gate.
