# potbot

An evidence-driven, multi-model coding agent, built in stages from the architecture
and implementation plan in `docs/`.

**Implemented: Phase 1, Phase 2 (P006–P021), and the initial standard local embedding
integration.** Tasks, contracts, evidence, artifacts, worker jobs, budgets, configuration,
environment identities, and model invocation records are durable. The local Qwen encoder
can index explicit sources and search them through PostgreSQL with pgvector.

Reasoning/coding inference, repository execution orchestration, learned memory admission,
and the custom semantic embedding space remain later stages. The five default reasoning
and coding models are registered as **unqualified catalog entries**; a name is never
treated as a tested deployment.

## Install and check

Use Python 3.12, Git, and [uv](https://docs.astral.sh/uv/):

```bash
uv sync --locked --extra postgres --dev
uv run --locked potbot info
uv run --locked python scripts/check.py
```

The core package has no mandatory third-party runtime dependencies. The PostgreSQL
driver and local encoder are separate extras. Domain imports and CLI help need neither
model weights nor running services. `uv.lock` pins the complete dependency resolution.

For the real PostgreSQL test suite:

```bash
docker compose up --detach --wait
export POTBOT_TEST_DSN='postgresql://potbot_test:potbot_test_only@127.0.0.1:55432/potbot_test'
uv run --locked python scripts/check.py --with-postgres
docker compose down --volumes
```

`compose.yaml` is a disposable test database with temporary storage. Use persistent
PostgreSQL and an artifact directory on persistent storage for retained tasks.
Selecting PostgreSQL tests without a usable explicit test DSN fails; tests never
silently substitute an in-memory database. CI runs this same required SQL suite.

## Work with tasks

Provision the application schema, a workspace, and a restricted database principal
with `potbot admin`. Then submit and inspect tasks through `potbot task`. The CLI and
versioned HTTP adapter share the same service operations and workspace checks.

```bash
uv run --locked potbot admin --help
uv run --locked potbot task --help
uv run --locked potbot worker --help
```

The [Phase 2 guide](docs/phase-2.md) contains provisioning, task submission,
worker recovery, API, configuration, and acceptance-test instructions. A valid starter
contract is in [examples/task-contract.json](examples/task-contract.json).

## Standard local vector embeddings

The reference integration uses **Qwen/Qwen3-Embedding-0.6B**, pinned to revision
`97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`, with **1,024 dimensions**, last-token pooling,
L2 normalization, and cosine distance. It runs locally on CPU in float32. Setup is an
explicit download; encoding never falls back to a remote service.

```bash
uv sync --locked --extra postgres --extra embeddings --dev
uv run --locked potbot embedding setup --model-dir models/qwen3-embedding-0.6b
uv run --locked potbot embedding smoke --model-dir models/qwen3-embedding-0.6b
```

The pinned weight/tokenizer files occupy about 1.21 GB on disk; runtime memory is
additional. Linux and Windows uv installs use the CPU PyTorch index. Model files stay
outside Git. For an existing local installation, run `smoke` directly; setup downloads
only into a new directory.

The [embedding guide](docs/embeddings.md) explains source registration, budgeted indexing,
exact filtered search, retirement, and the opt-in live SQL test. This first integration
uses explicit sources and exact search; automatic repository chunking, hybrid ranking,
ANN indexes, and custom learned embeddings are later work.

Vectors have **named representations**, starting with `general_text`. Indexing and
search select both a name and an exact immutable space; artifacts expose a typed
`named_embeddings` manifest that retains source and profile provenance. The
[named-embedding guide](docs/named-embeddings.md) covers application migration 5,
additional named views, the JSON schema and the coordinated upgrade procedure.

## Package boundaries

| Package | Responsibility |
| --- | --- |
| `potbot.domain` | Immutable contracts, provenance, configuration, identity, error taxonomy |
| `potbot.persistence` | Migrations, scoped SQL transactions, artifacts, evidence, outbox, fenced workers |
| `potbot.controller` | Task lifecycle, contract revisions, atomic budgets, reuse-discovery policy |
| `potbot.api` | Versioned task operations, development authentication, HTTP and CLI adapters |
| `potbot.models` | Deployment registry, resolved configuration, environments, packets and invocation records |
| `potbot.memory` | Standard local encoding profiles, source versions, vector persistence and exact retrieval |
| `potbot.evaluation` | Real Git fixtures and sanitized verification diagnostics |
| `potbot.tools` | Reserved boundary for repository, execution, and information adapters |

## Reuse before implementation

The standard reasoning policy requires discovery of existing libraries, tools, and
internal workflows before proposing new code. A structured review records evidence
and chooses reuse, adaptation, or a new implementation. The contracts and deterministic
gate are implemented; later tool adapters will perform live discovery and supply
trusted receipts. See [docs/reasoning-policy.md](docs/reasoning-policy.md).

The Phase 1 fixture instructions remain in [docs/phase-1.md](docs/phase-1.md).
