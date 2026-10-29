# S17 — Host-private Xpra log custody and GUI acceptance

**Baseline:** Potbot main `844eba3` (S16 merged). **Status:** offline tested; live Linux/WSL2 qualification remains a host-specific gate.

## Production change

Xpra's host daemon previously wrote its own log under the shared workspace home (`/home/.potbot/x11/xpra.log` or its per-agent display root). In a **managed** GUI session (one with an S15 kernel audit factory), this log now goes under a separately provisioned operator-only directory. This is distinct from agent-created sandbox logs in `/var/logs/ai` and is **not mounted into the sandbox**.

Provision the host directory, as the service owner on the Linux/WSL2 operator host:

```sh
install -d -m 0700 /var/logs/ai/operator
export POTBOT_XPRA_LOG_ROOT=/var/logs/ai/operator
```

The service needs the same S15 `POTBOT_OPERATOR_AUDIT_ROOT`, `POTBOT_OPERATOR_CGROUP_ROOT`, pinned strace/join helper and exact `POTBOT_XPRA_SHA256` settings. A managed process fails to start if the log root is absent, shared, symlinked, or intersects its display root. The selected log root is an operator environment setting, **never** a client-supplied path.

Each display uses a SHA-256-derived namespace (no guest names, raw paths or IDs in the pathname). One active `xpra.log` is exclusively created per namespace with mode 0600. On shutdown, the owned log is preserved under a unique archive name with a separate fsync'd receipt recording only its SHA-256 and byte count. Archive receipts are revalidated before any new process starts; unpaired archives, stale active logs, symlinks, oversized logs and tampering require operator reconciliation. There is deliberately no automatic cleanup of forensic data.

The managed display checks its active log during health polling; log tampering or reaching the 64 MiB ceiling fences new admission. Legacy non-managed standalone Xpra logging is unchanged to preserve existing deployments until they opt into kernel accounting.

## Acceptance and recovery

Run the qualified S13/S14 operator, launch and stop an actual monitored Xpra display and controlled browser, and verify an S16 audit for an exact completed GUI run. The following tools are read-only with respect to existing evidence:

```sh
PYTHONPATH=src python scripts/qualify_xpra_logs_s17.py \
  --log-root /var/logs/ai/operator \
  --report /var/logs/ai/operator-log-report.json

PYTHONPATH=src python scripts/qualify_gui_host_s17.py \
  --audit-root /path/to/private/audit-root \
  --log-root /var/logs/ai/operator \
  --workspace-id WORKSPACE_UUID --run-id COMPLETED_GUI_RUN_UUID \
  --identity-kind principal --subject-id OPERATOR_PRINCIPAL_UUID \
  --report /var/logs/ai/verified-gui-report.json
```

The combined gate rejects any unreconciled S16 history and any active/orphaned Xpra logfile. It requires at least one archived host log and a fully valid S16 process/syscall audit for the specified run. Its report never includes raw file paths or captured output. For an S40 agent browser, explicitly choose `--identity-kind agent --subject-id REGISTERED_AGENT_UUID` using the host's admitted identity.

**Failure drills required on the actual host:** kill the operator, stop the Xpra process mid-write, trigger a trace-size overrun, corrupt a *disposable* archive receipt, and make an agent browser spawn a short-lived child. Each failure must leave a fenced or indeterminate lease, preserving private audit data; no automatic repin, lease release, log deletion or process relaunch occurs.

## Further environment-specific work

S17 cannot attest that a specific Windows/WSL2 system supports nested seccomp, delegated cgroup v2, kernel taskstats, Xpra Xdummy, or that all kernel descendants exit as expected. Those properties require a live operator host with privileges and real processes. The S16 recovery inventory remains read-only, so a separate reviewed, host-authorized reconciliation workflow must still prove empty cgroups and lease ownership before releasing retained indeterminate records. Also verify that the host-local `/var/logs/ai/operator` directory is backed up/archived under the deployment's retention policy; this patch does not silently delete logs.
