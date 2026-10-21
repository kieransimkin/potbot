# Phase 1 — implementation and operator guide

## Scope and traceability

This implementation follows P001–P005 of
`Multi_Model_Coding_Agent_Implementation_Plan.docx`, the supplied plan dated
21 October 2026. SHA-256 of the source document:
`7827cfcc96f44ce0f074b09b53ade8f1e70dd9a4a31336cd742da441c44795a5`.
Python 3.12 is the proposed reference language, with a standard-library domain
layer and an optional Psycopg 3 persistence adapter. The project is named **potbot**, as selected by the user.

| Step | Implementation deliverables | Executable acceptance evidence |
| --- | --- | --- |
| P001 | Typed `src` package, console entry point, `uv.lock`, `scripts/dev.py`, explicit component boundaries | CLI tests, import-isolation checks, wheel build and installation into a clean environment |
| P002 | Real Git factory, divergent sibling commits, faulty program and behavioral tests, repair function, four variants, caller sentinel | Failing baseline becomes passing after repair; untracked input changes execution; pre-existing failure remains; histories and cleanup are isolated |
| P003 | PostgreSQL 17/pgvector 0.8.7 Compose service, strict explicit test DSN, per-run schemas, ownership manifests, migration/check/cleanup CLI | Real driver write/read and vector distance; rollback and checksum tests; unsafe target and missing marker rejection; crash recovery |
| P004 | Pinned CI actions, locked install, shared check runner, sanitized diagnostics, `always()` service cleanup | The same check runner is executable locally; required database tests fail when configuration is missing or unreachable |
| P005 | Immutable versioned UUID/digest/provenance/environment/outcome records, typed errors, strict JSON round trips | Invalid versions, digests, fields, timestamps, and inconsistent outcomes rejected before persistence; domain import has no service side effects |
| Reuse requirement added during implementation | Scoped library/tool/workflow search intents, attempted-search evidence, reuse/adapt/build decisions, and a structural review gate | Missing channels, mismatched task/scope/query, unsupported candidate references, and concealed search limitations are rejected |

The additional requirement introduced during implementation makes reuse
discovery a standard step before coding. Phase 1 includes versioned contracts
and a pure validation gate for external library searches, external tool searches,
and scoped internal workflow searches. The gate requires recorded attempts and
an evidence-linked reuse/adapt/build decision, and preserves degraded-search
status. See `reasoning-policy.md` for the detailed integration policy.

The tool and API packages define import boundaries only. The controller contains
the pure reuse policy, but Phase 1 does not execute model inference, ensemble
reasoning, graph search, memory retrieval, web searches, feedback learning, or a
general-purpose execution sandbox. Search receipts will be supplied by later
adapters; validating a supplied record does not prove a search actually ran.
The database migration is a small vector probe rather than the later memory
schema. Future work can introduce those features through these boundaries.

## Reproduce the development setup

Prerequisites: Python 3.12, Git, and uv. Docker with Compose is needed only for
the supplied PostgreSQL service; an equivalent dedicated local PostgreSQL 17
installation with pgvector 0.8.7 also works.

```bash
python scripts/dev.py --version
uv run --locked potbot info
uv run --locked python scripts/check.py
```

The first command installs the locked project and development dependencies and
runs the console application. No model weights, provider API keys, or web search
credentials are required. The check runner performs lint, formatting,
strict type checking, domain/CLI/Git tests, and a fresh wheel installation with
version/help/domain-import probes outside the source directory.

For a core-only installation, build a wheel and install it without the
`postgres` extra. `potbot --help`, `--version`, `info`, and the domain
package work without Psycopg or model provider SDKs. PostgreSQL commands report
a missing-driver error when actually invoked without the extra.

## Real Git fixtures

The factory creates one UUID-named child repository under a caller-selected
parent. It preserves an existing caller sentinel or creates a sentinel for
cleanup tests. Each repository has its own root commit and two sibling
candidate commits. Git signing, hooks, templates, and ambient Git environment
overrides are disabled for fixture creation.

The small program deliberately subtracts one from a total. Its actual unittest
command fails. The documented repair removes only that subtraction, preserving
unrelated tracked modifications and execution-relevant untracked input.

| Variant | Purpose |
| --- | --- |
| `baseline` | Demonstrate real failing-to-passing behavior after the one-line repair |
| `dirty_tracked` | Preserve an existing tracked-file modification while repairing |
| `untracked_input` | Include `runtime_offset.txt`; changing its value changes observed execution |
| `pre_existing_failure` | Keep an unrelated failing test after the target defect is repaired |

Example lifecycle:

```python
from pathlib import Path
from tempfile import TemporaryDirectory

from potbot.evaluation.repository_fixture import (
    check_repository_fixture,
    cleanup_repository_fixture,
    create_repository_fixture,
    repair_repository_fixture,
)

with TemporaryDirectory(prefix="potbot-demo-") as temporary:
    parent = Path(temporary)
    fixture = create_repository_fixture(parent)
    try:
        assert check_repository_fixture(fixture).returncode == 1
        repair_repository_fixture(fixture)
        assert check_repository_fixture(fixture).returncode == 0
    finally:
        cleanup_repository_fixture(fixture)
    assert parent.exists()
    assert fixture.sentinel_path.exists()
```

The same operations are exposed through `potbot fixture`:

```bash
uv run --locked potbot fixture create --parent /tmp/potbot-fixtures
uv run --locked potbot fixture check PATH --token TOKEN
uv run --locked potbot fixture repair PATH --token TOKEN
uv run --locked potbot fixture cleanup PATH --token TOKEN
```

Replace `PATH` and `TOKEN` with the values from `create`. A baseline `check`
intentionally exits 1; a repaired baseline exits 0. Validation failures exit 2.
Cleanup requires the original token, exact path, and ownership marker, and
deletes only the fixture child. If construction is interrupted, the ownership
marker supports recovery with `load_repository_fixture(path, token)`.

## Real PostgreSQL and vector harness

The Compose service binds to loopback port 55432, uses a dedicated disposable
test user/database, and stores data in a temporary filesystem. The published
image tag is `pgvector/pgvector:0.8.7-pg17-bookworm`.

```bash
docker compose up --detach --wait --wait-timeout 90
export POTBOT_TEST_DSN='postgresql://potbot_test:potbot_test_only@127.0.0.1:55432/potbot_test'
uv run --locked python scripts/check.py --with-postgres
docker compose down --volumes --remove-orphans
```

Use a shell `trap` or your normal process supervisor to run the final cleanup
command even if a local check fails. The CI workflow provides an unconditional
cleanup step. These credentials are solely for this disposable local fixture.

The harness accepts only an explicit PostgreSQL URI with a loopback host,
port, password, the exact `potbot_test` user, and a `potbot_test`
database (or its generated fixture suffix). It never falls back to
`DATABASE_URL`. Bootstrap refuses an unmarked database containing user objects;
it creates the vector extension and test identity in one transaction. Do not
activate the extension manually before first bootstrap: bootstrap deliberately
requires a fresh database.

Individual commands are available for diagnosing a run:

```bash
mkdir -p .test-runs
uv run --locked potbot db bootstrap
uv run --locked potbot db create --manifest .test-runs/example.json
uv run --locked potbot db migrate --manifest .test-runs/example.json
uv run --locked potbot db check --manifest .test-runs/example.json
uv run --locked potbot db cleanup --manifest .test-runs/example.json
```

Choose a new manifest path for each allocation. Manifests are created with
owner-only permissions and contain no database password. The schema name,
database UUID, run UUID, creation time, and ownership token must match both the
database registry and the schema marker before a destructive operation can
proceed. Only owned fixture tables and their schema are removed; external
dependencies cause cleanup to refuse and roll back. A tombstone records
successful cleanup, making repeated cleanup idempotent.

Each migration's content checksum is recorded with the DDL in the same
transaction. A changed applied migration is rejected. The application driver
writes and reads a real `vector(3)` value and calculates its L2 distance.
Tests deliberately introduce a broken migration to confirm that its table
creation and migration ledger entry roll back together.

### Recovery after interruption

```bash
uv run --locked potbot db list
uv run --locked potbot db cleanup --manifest .test-runs/example.json
```

The registry can recover active handles after the creating process exits.
`db list` returns ownership capabilities; keep that output local. The check
runner stores a recovery manifest in `.test-runs/` if normal cleanup fails,
and excludes such manifests from uploaded diagnostics.

To test database prerequisites explicitly:

```bash
uv run --locked pytest -m postgres
```

This command fails if the test DSN, database service, driver, or required vector
extension is unavailable. The suite does not substitute SQLite or silently
skip PostgreSQL tests. Several rejection tests create uniquely named disposable
databases, so the dedicated test account requires that capability; the supplied
container account has it.

## Shared domain contracts

The domain uses frozen, slotted dataclasses. Every wire record declares
`schema_version=1`, exact allowed fields, and explicit `to_dict`/`from_dict` and
`to_json`/`from_json` conversions. Unknown versions and malformed inputs fail
with a stable typed error.

| Record | Meaning and invariants |
| --- | --- |
| `RecordId` | UUID identity, with canonical UUID text on the wire |
| `ContentDigest` | Lowercase SHA-256 digest; alternate algorithms are rejected in version 1 |
| `ProducerIdentity` | Model, controller, tool, or human; a model producer requires its deployment UUID |
| `EnvironmentRef` | Immutable environment UUID plus content digest of its manifest |
| `ErrorInfo` | Stable error code and a nonempty message |
| `OperationOutcome` | Operation UUID, typed provenance and environment, outcome status, aware timestamps, result digest or error |

A successful outcome needs a result digest and finish time, with no error.
Failure, cancellation, timeout, and indeterminate states cannot publish a
success result. Cancellation/timeout/indeterminate states require the matching
error code. An indeterminate result cannot claim a known finish time. Start,
finish, and observation timestamps must be chronologically consistent.

Typed exceptions distinguish invalid input, scope denial, revision conflict,
timeout, cancellation, and indeterminate execution. They provide stable codes
without binding the domain to a model SDK or database driver. Recording an
outcome describes an observation; it does not itself certify code correctness.

## CI behavior and diagnostics

`.github/workflows/ci.yml` installs the locked project, starts the real
PostgreSQL/pgvector service, and invokes the shared check runner with
`--with-postgres`. Every command's nonzero exit status fails the job.
Database prerequisites are mandatory in that mode. The runner builds and
installs the package into a new environment to verify the distribution, rather
than relying exclusively on the editable development installation.

Failure artifacts contain selected command logs, sanitized pytest XML, and a
structured check summary. The sanitizer removes known environment secrets,
PostgreSQL connection URIs, and ownership capabilities. The workflow does not
upload repository trees, process environments, model files, database data,
fixture manifests, or recovery tokens. Redaction is a bounded safeguard for
these diagnostic formats, not a general secret-scanning service.

Service cleanup uses `if: always()`. Test functions clean their own fixtures
in finalizers, and the migration probe cleans its schema in a `finally` block.
A hard-killed local process may still require the documented registry/manifest
recovery. GitHub-hosted runner disposal provides the final service boundary
if a job cannot execute its cleanup step.

See `verification.json` for results actually executed for this delivery.
Hosted GitHub Actions and the Docker image itself are separate execution
environments; local validation does not imply that a hosted job was submitted.

## Technical references

- [uv project and CI documentation](https://docs.astral.sh/uv/guides/integration/github/)
- [Official pgvector source and supported Docker tags](https://github.com/pgvector/pgvector)
- [Official PostgreSQL 17.11 release source](https://www.postgresql.org/ftp/source/v17.11/)
- [PostgreSQL DROP SCHEMA behavior](https://www.postgresql.org/docs/17/sql-dropschema.html)
- [PostgreSQL connection parameters](https://www.postgresql.org/docs/17/libpq-connect.html)
