# Stage 8 — MODEL.md inspection, reversible context, semantic role proposal

This stage adds a read-only inspection/repair *host library*. PostgreSQL remains
canonical and no guest receives credentials or filesystem write access.

## APIs

`ContextHistoryInspector(WorkspaceContextService(database))`:

- `inspect(context,kind,name,role)`: current revision, digest, size, bounded
  metadata history; authenticated workspace and PostgreSQL RLS are mandatory.
- `revision(context,kind,name,role,n)`: retrieves a historical revision and
  verifies its SHA-256 digest before returning any text.
- `diff(context,kind,name,role,a,b)`: bounded unified diff for operator review.
  Historical text is *data* and must not be sent as privileged instructions.
- `restore(...,source_revision=n,expected_revision=current,
  idempotency_key=...)`: uses the existing audited, permission-checked
  `WorkspaceContextService.update()` CAS path; **never** rewrites history or
  bypasses grants. Stale revisions reject the restore.
- `freshness(context,kind,name,snapshot)`: checks whether an operator-pinned
  read-only snapshot is stale. This does not replace mounted inodes and must
  not be treated as automatic refresh.

## Semantic typing

`vocab/context-document-roles-1.0.json` is an additive **candidate** for the
next immutable Syn type release. It defines exact-case MODEL.md, MEMORY.md,
SKILL.md roles and references the already published AGENTS.md alias.
`classify_context_basename()` is a deterministic local helper for checked
basenames, not a claim of Syn registration. It never infers document validity,
trust, agent permissions or instruction authority.

The pinned `vocab/data-types.json`, Syn snapshot, crosswalk and their checksums
are deliberately unmodified: changing them silently would violate the
existing immutable catalog contract and regression tests. Publication of the
new Syn type release requires a separate controlled catalog migration.

## Qualification

Run `uv run pytest -q tests/test_context_inspection.py` and the existing
context tests. Real PostgreSQL RLS and Bubblewrap tests remain release gates.
No live-mount refresh, new guest RPC endpoint, or auto-provisioning of every
primitive constructor is claimed by this patch.

## Host inspection command

Use the restricted application login with `POTBOT_DSN` set in the trusted
operator process environment. For example (illustrative IDs):

```shell
potbot context --workspace "$WORKSPACE_UUID" --principal "$PRINCIPAL_UUID" \
  --kind sandbox --name "$WORKSPACE_UUID" --role MODEL.md inspect
potbot context --workspace "$WORKSPACE_UUID" --principal "$PRINCIPAL_UUID" \
  --kind sandbox --name "$WORKSPACE_UUID" --role MODEL.md diff --before 1 --after 2
potbot context --workspace "$WORKSPACE_UUID" --principal "$PRINCIPAL_UUID" \
  --kind sandbox --name "$WORKSPACE_UUID" --role MODEL.md restore \
  --source 1 --expected 2 --idempotency-key reviewed-rollback-1 --confirm
```

Explicit `--confirm` is required for restore, but never substitutes for the
PostgreSQL grant. RLS and CAS remain in force. Commands are host-side only.

The additional disposable-database fixture tests audited restore semantics:
`uv run --extra postgres pytest -q tests/integration/test_context_inspection.py`.
This is **not** safe to run against a production PostgreSQL database.
