# potbot — Phase 1

This is the executable foundation for the architecture and implementation plan.
Phase 1 implements the package, real repository fixtures, a PostgreSQL/pgvector
integration harness, CI, typed domain contracts, and a structured reuse-discovery
policy. Model inference, live reasoning search, memory retrieval, and learning
are scheduled for later phases.

Python 3.12 is the proposed reference implementation language. The application
core has no third-party runtime dependencies. PostgreSQL support is an optional
extra. Importing the domain neither imports model providers nor contacts a
database or inference service.

## Install and run

Install Python 3.12, Git, and [uv](https://docs.astral.sh/uv/). From this directory:

```bash
python scripts/dev.py --version
uv run --locked potbot info
```

No model downloads or model API credentials are needed. `uv.lock` records the
exact dependency resolution. The package also supports an ordinary wheel
installation with `pip install .` or `pip install '.[postgres]'`.

## Development checks

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest -m 'not postgres'
```

The PostgreSQL suite is deliberately opt-in locally and mandatory in CI. Running
it without an explicit dedicated test DSN fails; it does not silently skip or
substitute an in-memory database.

## Package boundaries

| Package | Responsibility |
| --- | --- |
| `potbot.domain` | Versioned immutable identities, provenance, environment references, outcomes, typed errors |
| `potbot.persistence` | Explicit real PostgreSQL integration harness and bootstrap migration |
| `potbot.evaluation` | Disposable real Git repositories with measurable faulty and repaired behavior |
| `potbot.tools` | Reserved boundary for execution and information tools |
| `potbot.controller` | Pure reuse-discovery policy; boundary for later reasoning and orchestration |
| `potbot.api` | Reserved boundary for external interfaces |

Detailed fixture, database, CI, and verification instructions are included in
`docs/phase-1.md`.

## Reuse before implementation

Every programming task must plan discovery of existing libraries, existing
tools, and reusable internal workflows. A structured review records the search
evidence and chooses reuse, adaptation, or a new implementation. Missing search
coverage is rejected; unavailable sources remain explicitly marked.

Phase 1 provides the contracts and deterministic validation policy. Later tool,
memory, and model adapters will execute the searches and supply trusted receipts.
See `docs/reasoning-policy.md` for the standard reasoning workflow.
