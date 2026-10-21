# Phase 2: durable records and task coordination

This delivery implements P006–P021 from the implementation plan and adds the requested
initial standard local vector integration. PostgreSQL is authoritative for metadata
and coordination; filesystem objects hold payloads. Autonomous model-driven coding
is a later phase.

## Acceptance coverage

| Step | Implemented deliverable | Principal executable checks |
| --- | --- | --- |
| P006 | Scoped workspaces, principals, repositories and episodes | `test_database.py`: migrations and cross-workspace foreign keys |
| P007 | Content-addressed local backend, atomic publication, immutable metadata | `test_artifacts.py`, `test_evidence_outbox.py`: corruption, containment, interruption |
| P008 | Append-only events with timestamps, producers, causal links and payload references | `test_evidence_outbox.py`: persistence, provenance, immutable SQL writes |
| P009 | Canonical command hashing, scoped idempotency and durable results | `test_database.py`, `test_tasks.py`: concurrent requests and rollback |
| P010 | Atomic event/outbox creation, stable keys and versions | `test_evidence_outbox.py`: replay and precommit failure |
| P011 | Leases, generations, fencing and restartable fixture handler | `test_evidence_outbox.py`: killed process, reclaim, expired completion, bounded errors |
| P012 | Immutable contracts and compare-and-swap revision history | `test_tasks.py`: predecessor scope, stale revision and retained history |
| P013 | Versioned task service, CLI and local HTTP adapter | `test_tasks.py`: fresh subprocess and authenticated HTTP round trips |
| P014 | Durable lifecycle, reasons/dependencies, pause/resume and admission guards | `test_tasks.py`: restart while paused, blocked state, stale tokens and terminal guards |
| P015 | Multidimensional reservations and conservative usage reconciliation | `test_budgets.py`: final-allowance race, custom dimensions, uncertainty and overruns |
| P016 | Configuration precedence and immutable task snapshots | `test_configuration.py`, `test_model_persistence.py`, `test_tasks.py` |
| P017 | Concrete environment manifests and configuration-linked fingerprints | `test_configuration.py`, `test_model_persistence.py` |
| P018 | Immutable deployment identities, separate health/qualification, five defaults | `test_models.py` (unit), `test_model_persistence.py` |
| P019 | Generation requests, ordered context manifests and partial/final invocations | `test_model_persistence.py`: exact inputs, partial usage and incompatible roles |
| P020 | Authenticated context and normal-role PostgreSQL row security | `test_database.py`, `test_tasks.py`, `test_embedding_persistence.py` |
| P021 | Versioned envelopes and operation-aware public errors | `test_wire.py`, `test_tasks.py`: all public codes and unknown versions |

PostgreSQL acceptance tests run against the real server. Each application test creates
a unique disposable database and two genuine restricted login roles, then removes
only those generated resources. The original Phase 1 schema harness remains separate.

## Provision an application workspace

Use PostgreSQL 17 with pgvector 0.8.7 installed. The repository Compose service can be
used for experiments, but its database storage is disposable. Set `POTBOT_ADMIN_DSN`
to an explicit installation/migration login, then:

```bash
uv run --locked potbot admin migrate
uv run --locked potbot admin workspace-create --name danceflow
```

Migrations 1–4 are transactional and checksum checked; repeat execution reports the
same versions. Changed histories are rejected. Application tables live in `potbot`,
and pgvector lives in `public`. Select another application schema using
`potbot admin --schema NAME ...`.

Supply `POTBOT_NEW_ROLE_PASSWORD` through your shell's secret mechanism, then create
and bind a normal login using the returned workspace UUID:

```bash
uv run --locked potbot admin login-create --role potbot_local
uv run --locked potbot admin principal-bind --role potbot_local --workspace-id WORKSPACE_UUID
```

Record the returned principal UUID. Set `POTBOT_DSN` to the new login's connection
string for application commands. Task/worker/embedding services never use admin
credentials. The installation login needs ownership and RLS bypass for provisioning.
Each authenticated application principal is bound to a dedicated PostgreSQL login;
it may be explicitly authorized for multiple workspaces. A production shared-login
identity broker would require a separate adapter.

Application logins must be `NOSUPERUSER`, `NOBYPASSRLS`, `NOCREATEDB`, `NOCREATEROLE`,
`NOREPLICATION`, have no role memberships, and have no schema creation or application
object ownership privileges. Row policies check both transaction scope and the
`session_user` binding. Changing scope settings does not grant another workspace.
Public errors return the same unavailable response for missing/inaccessible records.

Register a repository with the application identity:

```bash
uv run --locked potbot admin repository-create \
  --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  --name potbot --source-uri https://github.com/kieransimkin/potbot
```

Registration stores a source reference; it does not yet clone or capture code.
New workspaces receive all five canonical reasoning/coding model names, unqualified,
without fabricated deployment settings.

## Tasks and the HTTP API

`examples/task-contract.json` is a complete fixture investigation contract. Replace
its objective, criteria and baseline reference for real work.

```bash
uv run --locked potbot task --dev-auth --workspace WORKSPACE_UUID --principal PRINCIPAL_UUID \
  submit --repository REPOSITORY_UUID --contract examples/task-contract.json --key example-1
uv run --locked potbot task --dev-auth --workspace WORKSPACE_UUID --principal PRINCIPAL_UUID \
  inspect TASK_UUID
uv run --locked potbot task --dev-auth --workspace WORKSPACE_UUID --principal PRINCIPAL_UUID \
  control TASK_UUID pause --expected-token 0 --reason "Review requirements" --key pause-1
```

An identical operation/key/request returns its original durable result. A reused key
with different input conflicts. Use `inspect` for current state after a replay: the
command result is the original response, not a live projection. `history`, `revise`
and `budget` expose retained contracts and accounting. Revisions require the expected
active contract UUID; controls require the latest update token.

Submission leaves a task `submitted`. The trusted controller can transition it through
`initializing` to `running`; this phase does not pretend to capture or execute a
repository automatically. Paused/blocked tasks admit no new reservations. Running work
still settles usage, terminal tasks preserve their records, and human acceptance is
separate from technical status.

For local HTTP, set `POTBOT_DEV_TOKEN` to at least 32 characters:

```bash
uv run --locked potbot task --dev-auth --workspace WORKSPACE_UUID --principal PRINCIPAL_UUID \
  serve --port 8080
```

The server binds to `127.0.0.1`; requests require `Authorization: Bearer ...`.
Production hosting needs its own authentication and transport adapter.

| Route | Operation |
| --- | --- |
| `POST /v1/tasks` | Submit repository UUID and contract |
| `GET /v1/tasks/{id}` | Inspect state, contract and blockers |
| `GET /v1/tasks/{id}/contracts` | Read immutable history |
| `POST /v1/tasks/{id}/contracts` | Revise with expected contract UUID |
| `POST /v1/tasks/{id}/control` | Pause, resume or cancel with expected token |
| `GET /v1/tasks/{id}/budget` | Inspect limits, usage and held reservations |

POST bodies contain `schema_version: 1`, `request_id`, `idempotency_key`, and `payload`.
Unknown fields/versions fail. Errors have a stable code, scoped message and retry flag
derived from operation semantics. State mismatch, timeout and indeterminate execution
remain distinct. Connection loss after transaction admission is conservatively
reported as uncertain, not as permission to repeat arbitrary external work.

## Evidence, artifacts and workers

`ArtifactStore` separates authoritative metadata from bytes. SHA256 content identities
have workspace-scoped locators. Publication writes/flushed temporary bytes, links the
final name atomically and verifies the object before metadata commits. Reads validate
locator, length and digest. Symlinks and traversal are rejected. The root is an
operator-controlled storage boundary and must be inaccessible to untrusted repository
processes.

A SQL rollback can leave unreferenced complete bytes, never a committed partial object.
Garbage collection and alternative object-storage backends remain future work.
Immutable records reject mutation through privileges and database triggers.

Evidence distinguishes human input, trusted controller decisions, tool observations
and model proposals. A model's `verified` flag cannot turn into a tool observation.
Events retain causal/correlation references and optional payload artifacts; later tool
adapters will add execution-run binding.

```bash
uv run --locked potbot worker --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  --artifact-root artifacts/objects enqueue-fixture --key demo-job --text "fixture evidence"
uv run --locked potbot worker --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  --artifact-root artifacts/objects run-once --worker-id worker-1
```

The event and job commit together. A worker claims a short lease, works outside the
claim transaction and completes only with its current owner/generation and an unexpired
lease. Completion rechecks expiration after artifact I/O. Reclaimed jobs reject stale
workers, retries are bounded, and terminal errors preserve the original evidence.
Processing is at least once; arbitrary external effects have no exactly-once guarantee.

## Budgets, configuration and model records

Required task budget dimensions are `tokens`, `calls`, `tool_ms` and `concurrency`;
custom integer dimensions are supported. Reservation serializes on the episode row.
Settlement accounts for every reserved usage dimension, with spent usage derived from
append-only records. Confirmed unused capacity returns; uncertain usage stays held.
Measured overruns remain recorded and can exhaust a task. Runtime reservations are
accounting bounds; hard process timeouts belong to the later execution supervisor.

Configuration precedence is installation → deployment → workspace → repository → task.
Lists replace; policy/secret-reference maps merge by key. Unknown settings and protected
overrides fail. Submission freezes a resolved snapshot. Only `secret://...` references
are supported. Environment manifests record concrete OS/container, interpreter,
toolchains, services, hashed inputs and secret references linked to that configuration.

Deployment records preserve weights, tokenizer, chat template, backend/version,
quantization, adapters, limits, prompts, roles, resources and cancellation behavior.
Health does not imply qualification. Context packets preserve ordered source references;
requests retain sampling, schema, deadline and cancellation reference. Final records
retain raw output and partial usage, including failed/interrupted scripted invocations.
Live reasoning/coding transports and conformance qualification remain later phases.

## Verification

`python scripts/check.py --with-postgres` checks formatting, lint, strict typing, all
ordinary tests, real SQL tests, wheel creation and a clean core-only install outside
the checkout. Live Qwen has an additional explicit check in `embeddings.md`; ordinary
CI never downloads weights. Delivery evidence is in `phase-2-verification.json` and
`embedding-verification.json`.

Primary references: [PostgreSQL row security](https://www.postgresql.org/docs/17/ddl-rowsecurity.html),
[row locking](https://www.postgresql.org/docs/17/sql-select.html),
[psycopg transactions](https://www.psycopg.org/psycopg3/docs/basic/transactions.html),
[pgvector](https://github.com/pgvector/pgvector/tree/v0.8.7).
