# MODEL.md Stage 9 — admitted refresh, agent reconciliation, context UI

## Behaviour

- The operator checks PostgreSQL root context freshness every 10 seconds by
  default. It only creates a new set
  of immutable snapshots when the revision/digest differs.
- Refresh requires **both** an in-process supervisor gate and a workspace-wide
  host lease. Existing workers must finish before it can proceed. Each mounted
  file remains immutable: **only subsequent launches** receive the new content.
  Any failed validation or uncommitted refresh prevents publication.
- Previously registered agent homes are reconciled at operator startup when
  `--project-agent-context` is enabled. Files are created using the existing
  no-follow, create-only helper and have their authoritative contents in
  PostgreSQL. No arbitrary filesystem home is adopted.
- The API exposes authenticated, RLS-scoped context inspection, history,
  diffs, events, and an explicit restoration endpoint. Restoration invokes the
  existing `ContextHistoryInspector.restore` compare-and-swap path and requires
  exactly one expected revision and an idempotency key. The Next.js UI at
  `/context` displays history and performs a confirmed, audited restore.
- Neither the HTTP API nor the sandbox obtains database administrator rights,
  a PostgreSQL DSN, or direct writes to mounted Markdown files.

## Operational configuration

Rebuild/qualify the sandbox image and start the operator with the existing
`--context-principal-id`, `--context-parent`, and, if needed,
`--project-agent-context` flags. By default the operator polls every 10 seconds
for root context changes. The host-only `run_operator` function accepts a refresh
interval override; the CLI currently uses its fixed 10-second default.

The browser UI uses the existing operator-authenticated Next.js sessions,
scoped workspace backend connections, and the authenticated Python API.
The Next proxy supports at most one target per request and sanitizes upstream
errors instead of exposing PostgreSQL credentials or raw SQL diagnostics.

## Opt-in Syn type release

`src/potbot/context/syn_release.py` exports `context_syn_snapshot()` and
`context_syn_catalog()` with three exact-filename types described in
`vocab/context-document-types-1.0.0.json`. The older `AGENTS.md` alias stays
unchanged. The original 0.1.0 catalog and its hashes are not rewritten.
Callers must **explicitly** select this release; it is not an automatic
migration of already-pinned Syn snapshots. Source provenance and release
approval remain operator-controlled.

## Important boundaries

The refresh is **not an in-place update** of a running agent's bind mounts;
that would violate the immutable revision contract. Existing running agents
retain their snapshot. The host lease ledger must be the shared source of
execution-admission truth for all concurrent workers on the same host.

Currently the workspace host lease ledger uses SQLite for temporary process
coordination; it is not authoritative for context documents. PostgreSQL remains
canonical for all document state/history. Multi-host lease coordination must
be qualified separately before claiming distributed refresh safety.

The UI route is an authenticated API surface; it does not grant operator/admin
powers. PostgreSQL RLS and explicit shared-object write grants remain enforced.
If the browser can authenticate but has no permission to restore a shared
MODEL.md, the action must fail. Do not weaken RLS to make the UI convenient.

## Test and release gates

```shell
uv run pytest -q tests/test_context_refresh.py tests/test_context_ui_api.py tests/test_context_syn_release.py
uv run --extra postgres pytest -q tests/integration/test_workspace_context.py tests/integration/test_context_acl.py tests/integration/test_context_inspection.py tests/integration/test_context_refresh.py
```

Run the full host integration suite on real Linux/WSL2 + Bubblewrap with an
explicit disposable PostgreSQL fixture before production. Verify overlap
rejection, process-lease fencing, rollback under concurrent writers, old/new
snapshot isolation, and persistence across operator restart.
