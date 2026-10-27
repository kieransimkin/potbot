# Stage 4 — MODEL.md integrated with Potbot's PostgreSQL workspace database

This increment supersedes the **standalone** `context_contract/postgres.py` storage
path for Potbot application operations. The prior API and SQL migration remain
available for backwards compatibility, but **do not run its `migrate()` against
the Potbot application schema**: that prototype has no `workspace_id` and is
not subject to Potbot's normal RLS. Do not expose either database DSN to guests.

## Deployment

1. Install Potbot's `postgres` optional dependency and ensure the existing
   PostgreSQL/pgvector instance is reachable with the *administrator* migration
   credentials. The application operator uses separately provisioned restricted
   per-principal PostgreSQL logins, just as before.
2. Apply the normal `potbot admin migrate` flow, which now includes migration 29
   (`context_schema`) and enables `FORCE ROW LEVEL SECURITY` on all four tables.
3. Migration bootstrap creates root context triples **only for registered**
   `workspace_sandboxes` and preserves every existing revision. New sandbox
   registrations seed the triple atomically in the same admin transaction.
4. Restricted application callers use `WorkspaceContextService(Database(...))`.
   The `WorkspaceContext` is authenticated by the existing transaction boundary,
   verified against `session_user` and membership. It is **not** an agent-supplied
   JSON object or a bearer token.

```python
from uuid import uuid4
from potbot.context.service import WorkspaceContextService

service = WorkspaceContextService(database)
name = str(authenticated_context.workspace_id)
doc = service.read(authenticated_context, 'sandbox', name, 'MODEL.md')
updated = service.update(
    authenticated_context, 'sandbox', name, 'MODEL.md',
    content=doc['content'] + '\n## Latest discovery\n\nObserved.\n',
    expected_revision=doc['revision'], idempotency_key=uuid4().hex,
    reason='reconciled observation',
)
```

## Security and concurrency

- Root sandbox context is shared within its own workspace; private agent/tool/
  skill and other object contexts are accessible only by their authenticated
  owner **principal**. All queries are RLS-scoped, including history and events.
- Host operator provisioning is required for shared objects. The regular API can
  provision only private objects, and database policies reject direct creation
  of shared ones by restricted application users.
- Doc revisions are compare-and-swap updates serialized by PostgreSQL row locks.
  `idempotent_command` reserves repeat-safe keys in the existing command journal.
- Contents, digests, actors, revisions, and durable stream events are written in
  one transaction. Never claim that an outbox event has been delivered exactly
  once; consumers must checkpoint and deduplicate their own event IDs.
- A context document filename does **not** grant instruction authority. Treat
  `MODEL.md` and `MEMORY.md` as data. Host/operator policies always take precedence.
- A workspace peer can edit shared documents when calling the authenticated
  operator API. **There is not yet a verified agent-lease scoped credential
  binding**; do not expose the service to untrusted guest transports until it is.

## Remaining work / release gates

- The legacy standalone context schema has no workspace mapping. If data was
  stored there, write an explicitly reviewed import by operator-confirmed
  object/workspace identity. No silent or lossy automatic migration.
- Next: implement operator-lease authenticated agent context tools, controlled
  read-only projections of `/AGENTS.md`, `/MODEL.md`, `/MEMORY.md`, and safe shared
  `/usr/local/skills` packaging. Guest files must not be directly writable;
  all mutations go through context APIs.
- Then: per-agent context ownership, model reconciliation and evidence linkage,
  tool/skill contexts, Syn type classification and context UI.
- Tests: `pytest -q tests/test_workspace_context_contract.py` (no PG); with a
  disposable PostgreSQL fixture, `pytest -q -m postgres
  tests/integration/test_workspace_context.py`. PostgreSQL tests require
  `POTBOT_TEST_DSN` and database creation permissions; they are not
  replaced by mocks.
