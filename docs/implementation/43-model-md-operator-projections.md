# Stage 5 — Operator-owned MODEL.md filesystem projections

This stage adds a **read-only, point-in-time** projection of the PostgreSQL
context triple into a Linux/WSL2 Bubblewrap workspace. It does **not** expose
PostgreSQL credentials, unlock guest writes, or claim real-time document updates.

## Runtime wiring

The operator constructs `WorkspaceContextService` with Potbot's existing
restricted `Database` login and a `WorkspaceContext` obtained from the trusted
host's authenticated principal. The projection manager captures all three
sandbox documents with one database SELECT and verifies each SHA-256 digest.

```python
manager = ContextProjectionManager(
    service=WorkspaceContextService(database),
    principal=trusted_workspace_context,
    parent=Path('/run/potbot/context-snapshots'),  # host-private, mode 0700
)
operator = AiWorkspaceOperator(workspace_id=trusted_workspace_context.workspace_id,
                               ledger=ledger, runtime=runtime,
                               context_manager=manager)
```

The optional host operator CLI can enable projections with
`--context-principal-id UUID --context-parent /run/potbot/context-snapshots`
and a host-only `POTBOT_DSN` environment variable. The existing database
transaction verifies that principal UUID against the restricted login and
workspace membership; it cannot be self-asserted by the guest.

The immutable sandbox image must have **regular-file placeholders** at
`/AGENTS.md`, `/MODEL.md` and `/MEMORY.md`. Rebuild/publish the new image before
activating projection on a workspace. Older images continue with their existing
mount contract until explicitly upgraded. New builds also contain an
`/usr/local/skills` directory, which can be bound read-only to a trusted shared
skill repository via `SharedWorkspaceRuntime.shared_skills`.

The operator-owned snapshot directory is **not** beneath the guest's writable
`/home`, `/var/ai`, `/etc/ai` or shared tools. Every launch checks the source
files' type, length, mode, link count and digest and refuses overlapping mounts.
Guest paths are individually `--ro-bind` mounted and cannot be written directly.

## Snapshot freshness and follow-up

The projection is a **pinned snapshot for the lifetime of the operator**.
Context edits made via the PostgreSQL service after operator startup will be
reflected after the next safe operator restart. In-place automatic refresh
requires a coordinated no-live-execution boundary and is a separate stage.
Do not claim write-through or instantaneous updates.

Remaining work: lease-scoped agent context mutation tools (not general DB
access), per-agent triples and private mounts, concurrency-managed shared skill
publishing, operator-driven refresh/watching, tool and primitive context
provisioning, and Syn classification.

## Validation

`uv run pytest -q tests/test_context_projection.py` runs the host-only unit
suite. With a disposable PostgreSQL fixture, the Stage 4 integration suite
remains mandatory. Actual Bubblewrap image qualification and read-only mount
verification must run on a supported Linux/WSL2 host before production use.
