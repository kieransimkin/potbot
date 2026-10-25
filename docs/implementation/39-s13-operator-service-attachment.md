# S13 — operator attachment, workspace admission, and UI health

**Baseline:** GitHub `68d65e25f647652b4b4119495779ea8fb57e4cae` plus S12 post-activation release gates. The S13 source changes are additive to S12; S12 must be applied separately if it has not yet been committed.

## Purpose

S7's in-process `/etc/ai` watcher cannot be shared with API/worker processes. S13 makes the operator the **only** AI-history owner, publishes a read-only status socket on the host, and allows other processes to attach without instantiating duplicate watchers. The operator starts under the native user service **before** workers/API in managed mode. Ordinary hosted-provider inference can still run without a shared workspace in legacy mode, but do not call that arrangement "S13-qualified".

## Trust and mount boundaries

- The endpoint is AF_UNIX at an explicit host-private path, mode `0600`, within an existing user-owned `0700` directory **outside** every guest mount. Linux `SO_PEERCRED` must match the controller's effective UID at both ends. This is protection from *other Unix users*, not from malicious processes running as the same UID. Stronger protection still needs a dedicated operator UID and delegated privileged IPC.
- Every request binds exact workspace UUID, OCI image digest, qualification digest, and a SHA-256 of both canonical `/etc/ai` source paths. The response carries a fresh per-process epoch, live watcher status, running-process count, and an S12 release-gate count (or `null` when absent). No shell, file, PID, secret or run-output RPC exists.
- A worker's first successful response latches the epoch; it refuses a new operator epoch without worker process restart. Short-lived positive cache is at most 150ms. A missing/unhealthy endpoint fails closed for **managed** sandbox dispatch. `ProcessSupervisor` also polls it while running; loss makes outcomes inconclusive and stops the process group.
- In managed mode a `SharedWorkspaceBubblewrapBackend` accepts a live attached operator instead of a duplicate `AiConfigHistoryMonitor`, but still validates disjoint mounts, a trusted rootfs, pinned binaries, Bubblewrap/seccomp and the original `/etc/ai` backing directories. The socket is never mounted in the guest.
- S12's per-tool release gate remains authoritative: a reported healthy operator is **not** permission to run an unreleased tool, and a pending release doesn't disable unrelated tools. UI projections intentionally omit filesystem paths and bearer values.

## Operator startup

For native Linux or a dedicated WSL2 guest, install a managed service with a previously reviewed and pinned operator manifest:

```bash
./start.sh service install \
  --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  --provider-policy /operator/private/hosted-policy.json \
  --operator-manifest /operator/private/workspace.json \
  --replace
```

The service config retains the manifest's canonical path. On service start it verifies workspace identity, starts `potbot operator run --manifest ... --socket-path <private-server-dir>/operator.sock`, waits for successful local attestation (bounded to 8 seconds), and only then launches the API/worker with pinned `POTBOT_OPERATOR_*` environment variables. Operator exit or a stale epoch fences new guest launches. Operator shutdown is last, after confirmed child-process shutdown. **Do not place the manifest or socket under `/home`, `/etc/ai`, `/usr/local/tools` or any other writable guest mount.**

The corresponding explicitly managed standalone command is:

```bash
./start.sh operator run --manifest /operator/private/workspace.json \
  --socket-path /operator/private/run/operator.sock
```

The socket directory must already exist with mode `0700` and must not be a symlink. The operator reuses its S7 single-writer history lock; after a crash it can reclaim only a stale socket inode while holding that lock. It does not discard uncertain workspace leases or replay uncertain tool upgrades.

## Status contract

`GET /v1/ui/operator` requires the ordinary workspace bearer, exact configured Origin and fresh RLS membership. Next.js proxies through `/api/ui/operator` with an authenticated operator session and strict response projection. The page `/setup/operator` polls every 3 seconds while visible. It shows current attachment health, a short epoch identifier, active supervised runs and **unreleased** S12 gate count (`unknown` if migration/module not present); no control endpoints are exposed.

## Acceptance and known limits

```bash
PYTHONPATH=src pytest -q tests/unit/test_operator_attachment_s13.py
python scripts/qualify_operator_attachment_s13.py \
  --manifest /operator/private/workspace.json \
  --socket-path /operator/private/run/operator.sock \
  --report /operator/private/qualification-s13.json
```

The second is a genuine local socket/manifest check, **not** proof that hostile code is isolated. On your qualified Linux/WSL2 runner also execute `scripts/qualify_shared_workspace.py` with telemetry enabled, validate `/etc/ai` write and `/etc/ai-past` denial, test a supervised worker after deliberately stopping its owner, and verify an S12 upgrade remains blocked before postflight/repin. Exercise service stop while a lease is indeterminate; the service must not report a clean reconciliation. Check native manager semantics (`systemd KillMode`, WSL lifecycle) before production deployment.

S13 does **not** replace the host-private S9/S12 SQLite journal with a cross-host ledger. Cross-host fencing requires PostgreSQL or an equivalent distributed lease service. The status socket never bypasses existing tool leases or PostgreSQL RLS. The shared Bash PTY watchdog additionally checks the live remote attachment and requests forceful termination on failure. Xpra/browser subprocesses and other auxiliary processes still need their own explicit cancellation/stop qualification; merely exposing this health socket does not automatically stop every process.
