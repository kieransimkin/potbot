# S8 — Host-only AI configuration operator command

The S7 `AiWorkspaceOperator` now has a bounded host-owned process entrypoint:

```
potbot operator run --manifest /srv/potbot/private/workspace-operator.json
```

It accepts **only** an operator-owned real absolute JSON manifest whose mode
must not be group/world writable. The manifest has exactly these fields:

```json
{
  "workspace_id": "00000000-0000-0000-0000-000000000001",
  "image_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "qualification_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "rootfs": "/srv/potbot/images/verified-rootfs",
  "home": "/srv/potbot/workspaces/example/home",
  "model_staging": "/srv/potbot/workspaces/example/ai-staging",
  "shared_models": "/srv/potbot/global-model-library",
  "shared_tools": "/srv/potbot/workspaces/example/shared-tools",
  "ai_config_dir": "/srv/potbot/workspaces/example/ai-config",
  "ai_history_dir": "/srv/potbot/private/example-ai-past",
  "controller_ledger": "/srv/potbot/private/example-leases.sqlite"
}
```

The values above are *illustrative only*. Use actual qualified paths and real
registered image/hash identities. The ledger must already be registered and
its `image_status` must match the selected rootfs and qualification; this
command does **not** create a trusted image pin, a new workspace or a new
credential. The `/etc/ai` path is workspace-writable, and the `/etc/ai-past`
history must be outside guest-writable mounts. A failed journal audit stops
startup, and a failing running watcher fences the in-process supervisor.

**Important integration boundary:** a separate `potbot operator` process does
not automatically attach the REST server's or job worker's Python supervisor
to its in-process `AiConfigHistoryMonitor`. Cross-process worker admission,
heartbeat checks, watcher failover fencing, lease reconciliation and final
service-manager startup integration still require a qualified IPC/control-plane
increment. Until then, do not describe the whole service as fully supervised
merely because this operator process is running.

The process handles SIGTERM/SIGINT and invokes the S7 shutdown procedure,
which refuses a clean result if tool/PTY leases remain active or a worker
could not be confirmed stopped. A hard process kill or OS crash is still a
limitation: the next owner must audit the persisted history before admitting
any guest. Run real inotify/WSL2 acceptance and the existing qualification
script before enabling untrusted workspace agents.
