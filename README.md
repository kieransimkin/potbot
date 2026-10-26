# potbot

An evidence-driven, multi-model coding agent, built in stages from the architecture
and implementation plan in `docs/`.

**Implemented: Phases 1–4, the Phase 5 model foundation, and hosted jobs/API (`0.5.0.dev9`).** Tasks, contracts,
evidence, artifacts, worker jobs, budgets, configuration, environment identities, and
model invocation records are durable. Potbot can capture a local Git repository,
materialize isolated candidates, execute registered tools under a qualified Linux
sandbox, and reconcile interrupted runs. The standard local Qwen encoder indexes
explicit sources and searches PostgreSQL with pgvector under hard process supervision.
Scoped SYN snapshots, pinned semantic catalogs and bounded typed proposal validation are also implemented.
The scripted patch pipeline now records baseline failures, freezes a coding handoff,
applies a bounded patch, checks its exact resulting state, and exports a retained
patch plus an immutable verification manifest. It resumes from durable checkpoints.
The Phase 5 foundation adds bounded model responses, recipient-specific context
packets, durable inference reservations and settlement, a deterministic streaming
gateway, coding-result admission, and local host preflight for the five default models.
Hosted adapters now support Claude, OpenAI, Grok, Gemini and EvoLink, with dynamic
model discovery, explicit disclosure/rate policy and durable usage accounting.
A PostgreSQL job queue, REST API, WebSocket streams and browser dashboard expose
generation progress and retained output.

The opt-in [artifact-range Cog profile](docs/implementation/15-artifact-range-cogs.md)
opens actor/policy-bound read leases, returns verified ranges up to 8 KiB and
closes access through the existing monitored SYN boundary. Its local POSIX
backend verifies the full source using bounded reads; reconnecting clients can
reconstruct exact bytes from out-of-order or duplicate ranges. The optional
[31-Cog upload profile](docs/implementation/16-artifact-upload-cogs.md) adds open,
chunk, inspect, commit and abort tools with durable slots, bounded missing-offset
pages and verified streaming assembly. Physical retention, actual network/crash
recovery and other backend qualification remain separate gates.

The Agent security extension adds published Ed25519 verification keys, base-certified
hourly signatures, a one-shot native password custody tier, inherited Ember contracts
and owner-separated PostgreSQL storage. Semantic signing checks inherited implementation
revisions against installed source inputs before invoking the broker. This source
policy does not attest the executable or qualify a deployment host. See the
[implementation addendum](docs/agent-identity-and-ember-runtime.md) for commands,
exact Type inventory, custody limits and recorded verification results.

The current execution flow uses operator-selected tools and scripted proposals.
Multi-model reasoning-tree selection, local-model live qualification, learned memory
admission, live web discovery, IBM MCP, DanceFlow MCP, and the custom semantic embedding
space remain later stages. The five default reasoning and coding models are registered
as **unqualified catalog entries**; a name is never treated as a tested deployment.

The [plugin discovery strategy](docs/implementation/plugin-discovery-strategy.md)
plans task-led capability search, inspectable installation choices and qualification
gates, including continuing research across agent ecosystems. It preserves the
tool platform's existing stage statuses; this roadmap does not establish live discovery.

The [tool-plan gap audit](docs/implementation/09-tool-plan-gap-audit.md) defines
18 additional requirement groups, concrete acceptance scenarios and independent
core/Python/specialist qualification gates. The [mandatory SYN tool-Cog contract](docs/implementation/10-syn-tool-cogs.md) specifies one typed input/output, handler binding, controller-mediated effects and the Cog-driven Python debugger test. These remain planned work; validate
the synchronized 48-stage roadmap with `python scripts/check_tool_plan.py`.

The [Python-first debugger Cog interface](docs/implementation/08-debugger-python-first.md) adds registered language-neutral operations, controller admission, stop-scoped stack/variable handles and start/progress/terminal telemetry on the existing execution ledger. Python is the first backend; the guide separates local fixture, durable PostgreSQL and qualified Linux sandbox evidence.

The [source inventory Cog](docs/implementation/11-tool-source-inventory.md) implements T01.1 over existing committed repository snapshots. It maps every captured source module to its integration owner and roadmap stages, retains source/lock hashes and records monitored progress. Existing adapter code is distinguished from installation and host qualification; no inspected code is imported or executed. Its required CI gate uses real PostgreSQL, sealed SYN definitions and API replay.

The [canonical semantic tool contracts](docs/implementation/12-canonical-tool-contracts.md) implement T01.2 with versioned tool/capability/implementation/installation/invocation/evidence records. Three installed SYN Cogs search a host-owned corpus, round-trip checked contracts and convert explicitly typed units. Inherited Ember checks and start/progress/completion statistics use the existing semantic and execution services; mandatory real PostgreSQL/API acceptance remains separate from the documentation fixture.

The [execution provenance core](docs/implementation/13-execution-provenance.md) adds 17 registered work-kind Cogs and seal/replay Cogs with policy-bound causal runs, signed trace artifacts and read-only historical inspection. T01.3 remains in progress for universal production adoption, general artifact range transport and remote hardware qualification. [Shared temporary storage](docs/implementation/14-windows-temporary-storage.md) preserves inherited Windows scratch ACLs across tests, builds and workers, with actual file-lifecycle checks and explicit cleanup failures.

## Install and check

The [programme acceptance guide](docs/implementation/17-tool-programme-acceptance.md)
connects admitted API/MCP/CLI transports to monitored SYN work Cogs and reports all
48 tool-plan stages. Passing implemented-profile fixtures never claims full-programme
completion; missing mandatory evidence fails. Physical-device profiles remain
disabled and unqualified until a target is available.

Run the entry point for your platform from the extracted checkout:

```powershell
# Windows Command Prompt: start.bat
# Windows PowerShell:
.\start.ps1
```

```bash
# Linux or macOS:
bash ./start.sh
```

Each launcher installs pinned `uv` when needed, obtains managed Python 3.12, and
uses `uv sync --locked --all-extras --no-dev` to install all Python runtime extras.
It reports progress and then enters the same startup module. On first startup,
an unknown PostgreSQL URL is requested without echo, checked, and saved in the
native operating-system credential store. Subsequent starts reuse that value.
See [startup and dependencies](docs/startup.md) for credentials, downloads and host requirements.

Help and dependency management work without a configured database:

```bash
bash ./start.sh --help
bash ./start.sh dependencies status
bash ./start.sh dependencies download
```

For development, use Python 3.12, Git, and [uv](https://docs.astral.sh/uv/):

```bash
uv sync --locked --extra postgres --extra semantics --extra credentials --extra providers --extra web --extra security --dev
uv run --locked potbot info
uv run --locked python scripts/check.py
```

The core package has no mandatory third-party runtime dependencies. The PostgreSQL
driver, semantic validation, credential vault, hosted providers, web server, public
signature verification and local encoder are separate extras. Domain imports and
CLI help need neither model weights nor running services. `uv.lock` pins the complete
dependency resolution.

For the real PostgreSQL suite and Linux process-isolation acceptance suite, install
Bubblewrap, libseccomp, and `prlimit` from the host's packages. On Debian/Ubuntu:

```bash
sudo apt-get install bubblewrap libseccomp2 util-linux
```

The host must permit Bubblewrap's user, mount, PID, and network namespaces.

Ubuntu 24.04 also needs its Bubblewrap-specific AppArmor profile when unprivileged
user-namespace restrictions are enabled. See [Linux sandbox setup](docs/linux-sandbox-ci.md)
for the targeted profile setup and the early isolation check used by CI.

Then run:

```bash
docker compose up --detach --wait
export POTBOT_TEST_DSN='postgresql://potbot_test:potbot_test_only@127.0.0.1:55432/potbot_test'
uv run --locked python scripts/check.py --with-postgres --with-sandbox
docker compose down --volumes
```

`compose.yaml` is a disposable test database with temporary storage. Use persistent
PostgreSQL and an artifact directory on persistent storage for retained tasks.
Selecting PostgreSQL tests without a usable explicit test DSN fails; tests never
silently substitute an in-memory database. Selecting sandbox tests requires a working
isolation backend; unavailable isolation fails closed. CI requests both suites. Omit
`--with-sandbox` for the SQL-only gate on a host without qualified process isolation.

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

## First verified patch

The [Phase 4 guide](docs/phase-4.md) documents the full scripted fixture, its
checkpoint/resume commands, exported manifest, and acceptance evidence:

```bash
bash ./start.sh patch --help
```

The fixture uses a supplied repair and a clearly identified scripted coding adapter.
It requires no reasoning/coding weights. Mandatory checks bind the current contract,
exact repository state, environment, check definition and actual execution. A changed
or removed test, missing dependency, unexecuted check, or superseded contract cannot
silently turn into a passing gate. Imported patches require fresh application-bound checks.

Migration 10 supports both the GitHub SYN-first history and the previously released
Phase 3 ZIP history without rewriting either migration ledger. The [upgrade guide](docs/phase-4.md#database-upgrade)
explains the converging revisions.

## Model foundation

The [Phase 5 guide](docs/phase-5.md) describes model proposal admission, exact
recipient contexts, separately budgeted schema repair, restart handling and the
bridge into the existing verified patch pipeline. The [model adapter guide](docs/model-adapters.md)
documents the five advisory profiles and pinned local host configuration.

```bash
bash ./start.sh models catalog
bash ./start.sh models schema --schema action-proposal-v1
bash ./start.sh models schema --schema coding-result-v1
bash ./start.sh models preflight --profile local-host.json --project-root .
```

These commands operate without database credentials or model downloads. The local
foundation gateway uses a clearly identified offline fixture transport. Preflight can
establish readiness for a later load check; it does not load a model or qualify a
deployment. Local live inference and model residency scheduling remain outstanding
Phase 5 work. The [historical dev1 status](docs/phase-5-implementation-status.json)
records that milestone; the [hosted increment](docs/hosted-jobs.md) records the new scope.

## OpenAI-compatible client API and persistent service

The [OpenAI compatibility and persistent service guide](docs/openai-compatible-service.md)
adds `/v1/chat/completions`, `/v1/responses`, `/v1/embeddings`, OpenAI-format
`/v1/models`, and the `potbot` three-stage reasoning model. The existing
model directory is preserved at `/v1/potbot/models`. Installed provider models
remain bound by operator policy and the OS credential vault; the interactive
transport does **not** replace durable task-budget accounting or claim full
Phase-7 autonomous reasoning.

```bash
./start.sh service install --workspace-id <UUID> --principal-id <UUID> --provider-policy providers.json
./start.sh service status
./start.sh credentials api-show
```

The same `service` subcommand works through `start.ps1` and `start.bat` on Windows.
User-owned systemd/launchd/Task Scheduler persistence starts both HTTP and worker
processes. Metadata-only console/JSONL event logging is on by default; opt into
redacted query/response content via `--log-content`. Explicit
`--download-embeddings` downloads and verifies the local Qwen assets with progress.
A provisioned PostgreSQL workspace/principal and provider policy remain prerequisites.

## Model-aware prompting and R3/R4 reasoning

The [R3/R4 integration guide](docs/implementation/18-model-prompts-r3-r4.md)
adds a versioned, model-aware prompt compiler, held-out A/B/C prompt comparison,
independent hypothesis generation, bounded evidence investigation and a SYN Cog
search bridge. `potbot serve` now uses the bounded tree for the OpenAI virtual
`potbot` model by default (`--reasoning-mode fixed` restores the older three-pass
workflow). Evidence Cogs are host-registered: unavailable search channels are
reported explicitly, and the standard server does not silently enable live
web/MCP access. The R1/R2 file-based tree remains single-writer, not a
PostgreSQL multi-worker execution controller; task-budget settlement and R2.5
live steering remain future work.

## Hosted models, API and job dashboard

The [hosted jobs guide](docs/hosted-jobs.md) covers credentials, operator policy,
task startup, accounting and this increment's implementation plan. The
[REST/WebSocket guide](docs/http-api.md) documents clients and replay semantics.

```powershell
.\start.ps1 credentials service-set --name providers.openai
.\start.ps1 credentials service-set --name api.access_token
.\start.ps1 serve --workspace-id <UUID> --principal-id <UUID> --provider-policy providers.json
# A second terminal runs the worker:
.\start.ps1 jobs worker --workspace-id <UUID> --principal-id <UUID> --provider-policy providers.json
```

The default dashboard is `http://127.0.0.1:8765`. Provision the database workspace,
configure provider policy and start a task before submitting a generation job.
The same arguments work with `start.bat` and `bash ./start.sh`. See
[provider adapters](docs/hosted-providers.md) for all five services. Provider
catalogues retain media entries, while reasoning/coding jobs select text models.
Hosted output remains unverified until admitted and checked by the coding workflow.

## DanceFlow KeywordMoves workflow acceptance

The [pinned KeywordMoves workflow acceptance guide](docs/implementation/22-danceflow-keywordmoves-workflow-acceptance.md)
adds an opt-in, bounded DAG over host-authorized KeywordMoves operations. It
exercises real offline extraction, Trends/observation imports, multi-source
composition, editorial review and shortlist selection in separate CI, preserving
source hashes and resuming without replay. It does not download models, scrape
platforms, or grant external tool permissions to a reasoning model.

## Repository capture and controlled execution

The [Phase 3 guide](docs/phase-3.md) describes its original migration history and the complete
`prepare`, `run`, `inspect`, `reconcile`, and `resume` workflow:

```bash
uv run --locked potbot execution --help
```

Snapshots preserve tracked changes plus explicitly included untracked/generated inputs
without modifying the source repository. Candidates have ownership markers and fenced
single-writer leases. The default CLI tools inspect files, compile Python, and run
Python `unittest` discovery. Their captured outputs, actual input/result states, and
usage remain inspectable after restart.

Run receipts are kept outside candidates. A matching stopped-process receipt can
reconcile a lost completion acknowledgment; missing or inconsistent evidence leaves
the workspace quarantined and prevents blind replay. `execution resume` reconstructs
durable state and accounting without starting a command or changing task lifecycle.

The qualified backend is **Linux Bubblewrap plus seccomp**. It denies direct networking
and access to unmounted host files; the supervisor has no unrestricted host fallback.
CPU, address-space, file-size, and descriptor limits are per process. Aggregate memory
and process-count quotas, native Windows/macOS execution, submodule materialization,
and installation of repository-specific native dependencies are outside this delivery.
The startup launchers install Potbot's own Python dependencies; arbitrary repository
build environments remain explicit execution inputs.

## Standard local vector embeddings

The reference integration uses **Qwen/Qwen3-Embedding-0.6B**, pinned to revision
`97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`, with **1,024 dimensions**, last-token pooling,
L2 normalization, and cosine distance. It runs locally on CPU in float32. The integrated
downloader prepares the pinned files before CLI encoding operations; encoding never
falls back to a remote service.

```bash
uv sync --locked --extra postgres --extra semantics --extra embeddings --dev
uv run --locked potbot embedding setup --model-dir models/qwen3-embedding-0.6b
uv run --locked potbot embedding smoke --model-dir models/qwen3-embedding-0.6b --timeout-ms 120000
```

The pinned weight/tokenizer files occupy about 1.21 GB on disk; runtime memory is
additional. Model files stay outside Git. Completed installations are verified and
reused offline; interrupted downloads resume from owned partial files. Supervised encoding and
the smoke command require the qualified Linux sandbox described above. The deadline
covers tokenizer/model preparation and inference; a timed-out, cancelled, or unconfirmed
worker cannot publish a late vector. Confirmed usage is charged, confirmed unused capacity
is released, and uncertain usage remains held for reconciliation.

The [embedding guide](docs/embeddings.md) explains source registration, budgeted indexing,
exact filtered search, retirement, and the opt-in live SQL test. This first integration
uses explicit sources and exact search; automatic repository chunking, hybrid ranking,
ANN indexes, and custom learned embeddings are later work.

Vectors have **named representations**, starting with `general_text`. Indexing and
search select both a name and an exact immutable space; artifacts expose a typed
`named_embeddings` manifest that retains source and profile provenance. The
[named-embedding guide](docs/named-embeddings.md) covers application migration 5,
additional named views, the JSON schema and the coordinated upgrade procedure.

## SYN semantic catalogs and exact inventories

Import public SYN snapshots, resolve scoped references, enumerate instances by
declared type, and rank selected snapshot records with named embeddings.
The bundled catalog contains 529 native classes reconciled with the supplied database,
plus 47 protected representation types preserving all 45 existing data types and
adding two public-record types. The complete extracted registry is ready to import
from [syn-database-types.snapshot.json](vocab/syn-database-types.snapshot.json).
Future types-only exports capture the live native registry without reading instance
tables; bounded public-record snapshots supply resolvable objects.

Type URLs `syn://type/<slug>` and `synny://type/<slug>` resolve the same type.
Native classes prefer `syn://`; Potbot representation types prefer
`synny://type/potbot-data-...`, with existing `dt:*` IDs retained for compatibility.
Instance URLs remain native `syn://` references. Alias spelling does not create
another type, artifact, or vector within the selected snapshot.

Every lookup selects an explicit immutable snapshot within the authenticated workspace.
Import and exact reads load no encoder. The [SYN integration guide](docs/syn-semantic-integration.md)
covers migration 6, read-only export, type/representation distinctions, stable pagination,
hashes, coverage limits, and the `potbot syn` commands.

### Expanded hierarchy and native primitive placement

The opt-in [Potbot hierarchy](docs/type-hierarchy-and-syn-primitives.md) adds the
80 APS 0.2 declarations and 34 process, event, question, evidence, assessment,
and definition specializations. Its complete graph contains 690 unique types,
including the unchanged native and protected legacy catalogs. New public type
references use full URLs, with concise authored slugs and preferred `synny://`
spelling. The default catalog and existing snapshots retain their current content.

```bash
uv run --locked potbot syn hierarchy --summary
uv run --locked potbot syn placement --type-ref synny://type/agent-run-record
uv run --locked potbot syn placement --type-ref synny://type/verification-assessment
```

Placement cards explain when an item fits a Thing, Cog, Event, Ember, Enigma,
Pondle, Snug, Om, Credential or Type. They are versioned recommendations; imports
preserve the source's actual primitive kind. Exact inventory and vector search
accept `--primitive-kind` together with their semantic type filters. The
[types-only extension](vocab/potbot-hierarchy.snapshot.json) can be imported
explicitly through the existing snapshot command. See the
[full hierarchy reference](docs/type-hierarchy-reference.md) for all authored
classes, exact parents and placement decisions.

## Semantic types and release identities

The committed [vocabulary package](vocab/) supplies 45 data types, including six
abstract families, plus entity types, predicates, concepts, and retrieval profiles.
The loader checks the exact catalog, explicit subtype graph, local schema references,
and release hashes. Typed literals preserve distinctions between booleans, integers,
decimals, strings, null, absent, and unresolved values.

New tasks and context packets freeze their semantic release identities. Selecting a
new release does not rewrite historical configurations or packets. Structural
validation leaves an envelope as a proposal: entity resolution, truth adjudication,
guard evaluation, and learned semantic classification remain later work. See the
[semantic API and compatibility notes](docs/phase-3.md#semantic-contracts-and-release-pins).

## Object memory implementation plan

The [per-object memory implementation plan](docs/object-memory/implementation-plan.md)
specifies a separate memory graph for every Syn object, integrated with existing
Type inheritance, Ember contracts, candidate admission and qualified context packets.
Its [32-stage register](docs/object-memory/plan.json) is entirely `not_started`;
run `python scripts/check_memory_graph_plan.py` for planning consistency, not runtime acceptance.

## Package boundaries

| Package | Responsibility |
| --- | --- |
| `potbot.domain` | Immutable contracts, provenance, configuration, identity, error taxonomy |
| `potbot.persistence` | Migrations, scoped SQL transactions, artifacts, evidence, outbox, fenced workers |
| `potbot.controller` | Task lifecycle, contracts, budgets, durable actions and information needs, reuse policy |
| `potbot.api` | Versioned task operations, development authentication, HTTP and CLI adapters |
| `potbot.models` | Deployment registry, resolved configuration, environments, packets and invocation records |
| `potbot.memory` | Supervised local encoding, source versions, named vectors and exact retrieval |
| `potbot.semantics` | SYN catalogs and exact inventories, immutable releases, typed proposals and task pins |
| `potbot.evaluation` | Real Git fixtures and sanitized verification diagnostics |
| `potbot.tools` | Repository snapshots, candidate leases, tool admission, Linux isolation, captures and recovery |

## Reuse before implementation

The standard reasoning policy requires discovery of existing libraries, tools, and
internal workflows before proposing new code. A structured review records evidence
and chooses reuse, adaptation, or a new implementation. The contracts and deterministic
gate are implemented; later tool adapters will perform live discovery and supply
trusted receipts. See [docs/reasoning-policy.md](docs/reasoning-policy.md).

The Phase 1 fixture instructions remain in [docs/phase-1.md](docs/phase-1.md).

## Live steering and evidence-ranked reasoning (R2.5/R5)

The `potbot` virtual model now evaluates queued user steering prompts at
reasoning checkpoints and ranks candidate hypotheses with independent critique
and evidence-source receipts. Streaming OpenAI responses expose
`X-Potbot-Run-Id`; submit steering prompts to
`POST /v1/reasoning/{run_id}/steer` and poll the decision through
`GET /v1/reasoning/{run_id}/steering/{request_id}`. The bounded beam preserves
pruned branches for possible reopening. See
[implementation and API limits](docs/implementation/19-live-steering-r5-api-capabilities.md).
These single-writer file snapshots do not yet provide PostgreSQL worker fencing
or complete OpenAI/Gemini API compatibility.

### PostgreSQL-backed reasoning and upstream OpenAI/Gemini models

The [reasoning runtime/provider guide](docs/implementation/20-postgres-reasoning-and-model-providers.md)
explains migration 24, fenced checkpoints, database-backed steering, bounded
adaptive expansion and using OpenAI and Gemini **as outbound providers**.
This is not a claim of complete inbound protocol emulation or fully qualified
parallel reasoning orchestration.

## Model-role benchmarks and agentic graph monitoring

Potbot includes an **opt-in**, source-reproducible benchmark runner for comparing
small local models and explicitly authorised hosted OpenAI/Claude/Gemini models
across graph roles and prompt variants. It offers per-model/per-role quality,
latency, provider usage and cost reports, calibrated blind A/B judge tests, and
advisory monitoring of workflows and assumptions. It never downloads weights,
uses personal web accounts by assumption, runs paid CI inference or automatically
promotes a model based on its own judgement.

```bash
uv run --locked python scripts/benchmark_agentic_models.py preflight \
  --models examples/agentic-benchmark/local-models.json \
  --suite examples/agentic-benchmark/role-suite.json
```

See [the operator and methodology guide](docs/implementation/23-agentic-model-benchmark-and-monitoring.md)
for the full local and hosted run commands, private side-by-side reports, paid
limits, judge position-swap checks and periodic drift monitoring.

## Atomic task planning and small specialist models (R7 foundation)

Potbot also supplies a bounded atomic-operation taxonomy, non-executing recursive
DAG validator and fast rule-first temporal steering classifier. Incoming messages
can be classified as `interrupt`, `deferred` (the default for new actions),
`background` or `clarify`; this **does not yet** alter running PostgreSQL tasks.
A separate advisory catalogue covers ultra-small reasoners, coders, tool routers,
rerankers and encoders with alternative quantization estimates and explicit
immutable-artifact validation:

```bash
uv run --locked potbot models specialists --role steering --quantization Q4_K_M
uv run --locked potbot models specialists --role coding --vram-mib 4096 --disk-free-mib 8192
uv run --locked potbot models install-artifact --manifest operator-artifact.json \
    --models-root ./models --confirm-download
```

See [the R7 design and security boundaries](docs/implementation/24-atomic-task-routing-and-specialists.md)
for recursive stopping conditions, candidate model identities, hardware estimate
limitations, licensing and the controller/benchmark integration still required.
