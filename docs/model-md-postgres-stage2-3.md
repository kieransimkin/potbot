# MODEL.md Stage 2–3: PostgreSQL and operator boundary

This patch **replaces**, rather than extends, the unmerged SQLite Stage 2 patch.
Apply after Stage 1 only. The PostgreSQL schema is authoritative; the
sandbox-facing Markdown files will be projections in a later stage.

## Database setup

Install `psycopg[binary]` and `psycopg-pool` in the operator environment.
Provision a dedicated PostgreSQL database and least-privilege operator
database role through the existing deployment secret manager. Do not put
database credentials inside sandbox mounts.

```python
from psycopg_pool import ConnectionPool
from context_contract.postgres import PostgresContextStore
from context_contract.operator import OperatorContextService, Principal

with ConnectionPool(conninfo=operator_database_url) as pool:
    store = PostgresContextStore(pool)
    store.migrate()  # operator startup/migration job only
    service = OperatorContextService(store)
    operator = Principal("operator:primary",
                         frozenset({"context:read","context:write","context:provision"}))
    service.provision_sandbox(operator, "sandbox-123")
```

## Guarantees and boundaries

- Current document, immutable revision and durable outbox event commit atomically.
- Object-level row locks serialize first creation and conflicting updates.
- `expected_revision=0` means document must not exist; stale writes raise `Conflict`.
- Provisioning preserves existing content, including deliberately empty documents.
- `Principal` must be constructed by the authenticated operator transport, never
  accepted directly from untrusted agent JSON. The service is not a network API.
- Permission strings are a first boundary, not yet object-scoped authorization.
- Run migrations through controlled deployment; runtime migration is illustrative.
- Future work: object-scoped ACLs, outbox dispatcher, root mount projections,
  sandbox lifecycle hooks, skill registry, Syn typing and PostgreSQL integration tests.
