# Workspace filesystem sandbox

Potbot workspaces require an explicit host directory before user-facing local development
operations may run. The directory is a durable, immutable part of the workspace security
boundary; callers cannot replace it by supplying alternative artifact, candidate, receipt,
model, cache or temporary directories.

## Provisioning

After applying migration 14, create a workspace with an absolute sandbox root:

```bash
potbot admin workspace-create --name my-project --sandbox-root /srv/potbot/my-project
```

On Windows the path may be a normal absolute Windows directory, for example:

```powershell
.\start.ps1 admin workspace-create --name my-project --sandbox-root 'D:\Potbot\my-project'
```

The root itself may not be a symlink, junction/reparse point, or filesystem root. Potbot
creates a fixed layout below it:

- `repository/` — the repository copy used by Potbot
- `candidates/` — fenced mutable candidate trees
- `models/` — workspace-local model assets
- `tools/` and `dependencies/` — downloaded development tools and dependency state
- `environments/`, `builds/`, and `tests/` — runtime environments and development work
- `artifacts/` and `receipts/` — controller-owned durable output
- `cache/`, `tmp/`, and `logs/` — workspace-local caches, temporary files and logs

The root is registered once in `workspace_sandboxes`. The application database role has
read-only access to that registration and migration security makes the row append-only.
The same canonical root cannot be registered to two workspaces.

Workspaces created before migration 14 intentionally have no implicit filesystem authority.
An administrator must bind one root exactly once:

```bash
potbot admin workspace-sandbox-set --workspace-id <UUID> --sandbox-root /srv/potbot/existing
```

Until that succeeds, workspace-bound local execution and local-model operations fail closed.

## Repository placement

`potbot execution ... prepare --source` accepts repository sources only from the registered
`repository/` directory. Import or clone an existing repository into that directory as an
explicit operator/controller action before starting model- or tool-controlled development.
The sandboxed child process is never given the original external source path.

## Runtime confinement

A workspace-bound tool receives the candidate tree as its primary writable filesystem plus
only selected writable workspace support directories. Potbot moves ordinary developer cache
and temporary locations into the workspace by setting `HOME`, `TMPDIR`, `TMP`, `TEMP`,
`XDG_CACHE_HOME`, Hugging Face/Transformers/Torch caches, `UV_CACHE_DIR`, `PIP_CACHE_DIR`,
`CARGO_HOME` and npm's cache to locations below the registered root.

The current qualified execution backend is Linux Bubblewrap + seccomp. It creates private
user, mount, PID and network namespaces, drops capabilities, denies network/mount/ptrace and
namespace-escape syscalls, and mounts only:

1. the candidate and explicitly selected workspace support directories as writable;
2. workspace model assets and explicitly installed trusted runtime inputs as read-only; and
3. the small system runtime allowlist needed to execute the selected binary (`/usr`, `/bin`,
   runtime libraries and the active Python runtime) as read-only.

The last item is a deliberate runtime exception to literal zero host reads. Arbitrary host
home directories, sibling repositories, controller credential storage and other host paths
are not mounted. No host path is writable outside the registered workspace.

If the qualified isolation backend cannot be established, the operation returns a capability
error and no ordinary subprocess fallback is permitted.

### Windows and macOS

Migration 14, canonical path confinement and workspace-local storage work on these platforms,
but the repository does not yet contain a qualified Windows AppContainer/Hyper-V/WSL or macOS
sandbox backend. Therefore local model/tool execution remains fail-closed on native Windows
and macOS. A later platform-isolation phase must qualify those backends before Potbot may run
workspace-controlled processes there.

## Local models

Workspace-attached embedding commands default to `models/qwen3-embedding-0.6b` within the
registered root. Explicit model or artifact paths must remain in their corresponding sandbox
area. The supervised local encoder places worker scratch state and caches under `tmp/`/`cache/`
and runs the worker through the same qualified isolation backend. Standalone setup/smoke
commands remain separate host-qualification operations and do not acquire workspace authority.

## Security properties

The sandbox registration is an authorization boundary, not just a convenience directory.
Tests cover path traversal/link escapes, writable mounts outside the root, migration identity,
and workspace-local model storage. Production callers must load the durable sandbox record;
caller-supplied paths are checked against it rather than treated as authority.
