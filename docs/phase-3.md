# Phase 3: repository isolation and controlled tools

This delivery implements P022–P034 and the Phase 3 extensions P154–P157. It connects
durable tasks to repository snapshots, isolated candidate directories, registered tool
operations, process supervision, and restart reconciliation. It also adds the pinned
semantic registry, typed proposal validation, semantic release pins, and hard
supervision for the existing standard Qwen embedding integration.

Execution is currently driven by an operator or a scripted controller. Persisted search
nodes are execution envelopes; reasoning-tree selection, reasoning/coding model
transports, multi-model ensembles, learned memory admission, and custom embeddings
remain later phases. IBM MCP, the DanceFlow MCP server, live web acquisition, and
automated library/workflow searches are not implemented in this phase. Reserved
external invocation references do not enable an external transport.

## Prerequisites and upgrade

Use Python 3.12, Git, PostgreSQL with pgvector, and the application login provisioned
in the [Phase 2 guide](phase-2.md#provision-an-application-workspace). The execution
backend requires Linux, Bubblewrap, libseccomp, `prlimit`, and a host that permits
unprivileged user, mount, PID, and network namespaces. The verified environment is
Linux x86_64. Native Windows and macOS process execution are not qualified.

```bash
uv sync --locked --extra postgres --extra semantics --dev
sudo apt-get install bubblewrap libseccomp2 util-linux
```

The package installation command is portable; the system-package example is for
Debian/Ubuntu. Installing the binaries alone is insufficient if a container, security
policy, or kernel prevents their namespace operations. Required isolation failures
return `capability_unavailable` and never trigger a host-process fallback.

For an existing installation, preserve the database and artifact storage, stop old
application workers, and run the updated migration command with the installation
identity in `POTBOT_ADMIN_DSN`:

```bash
uv run --locked potbot admin --schema potbot migrate
```

Application migrations are transactional and checksum checked. The existing history
through migration 5 is retained; the new migrations add:

| Migration | Tables and behavior |
| --- | --- |
| 6, `repository_schema` | Immutable repository states and parent edges; owned candidate directories; workspace leases with generations; stopped-process workspace reconciliation records. |
| 7, `semantic_schema` | Immutable component releases, semantic releases and catalog entries; an explicit active release selection; separate task and context-packet semantic pins. |
| 8, `execution_schema` | Knowledge views, search nodes, typed actions and dependencies, action runs/completions, recovery claims and reconciliation decisions, information needs and immutable revisions; checked run/node references on evidence. |

All new application tables retain workspace scoping and relational ownership checks.
The authoritative migration order is in
[`application_migrations`](../src/potbot/persistence/schema.py). Old configuration
hashes, model invocation records, embedding spaces, and completed evidence are not
rewritten. Earlier tasks without a semantic pin remain inspectable; new semantic
execution requires a supported, declared pin. The upgrade does not silently assign
the current release to an old task.

Use `POTBOT_DSN` for the restricted application login. Database credentials, artifact
storage, and private receipts must remain outside candidate directories and outside
any read-only asset mount supplied to a tool. Retain candidate and receipt storage
across controller restarts when work may require reconciliation.

## Operator CLI

`potbot execution` provides five commands. Every invocation requires `--dev-auth`,
`--workspace`, `--principal`, `--artifacts`, `--candidates`, and `--receipts`; `--schema`
defaults to `potbot`. Put these shared options before the subcommand. Development
authentication preserves the existing workspace/login checks; it is not a production
identity-provider integration.

The following Bash helper avoids repeating the shared options. Set the three identity
variables to the UUIDs returned by provisioning and task submission, and choose
application-owned persistent directories for the paths:

```bash
export POTBOT_WORKSPACE='WORKSPACE_UUID'
export POTBOT_PRINCIPAL='PRINCIPAL_UUID'
export POTBOT_TASK='TASK_UUID'
export POTBOT_ARTIFACTS='/absolute/path/potbot-data/artifacts'
export POTBOT_CANDIDATES='/absolute/path/potbot-data/candidates'
export POTBOT_RECEIPTS='/absolute/path/potbot-data/receipts'

pbexec() {
  uv run --locked potbot execution \
    --dev-auth --workspace "$POTBOT_WORKSPACE" --principal "$POTBOT_PRINCIPAL" \
    --schema potbot --artifacts "$POTBOT_ARTIFACTS" \
    --candidates "$POTBOT_CANDIDATES" --receipts "$POTBOT_RECEIPTS" "$@"
}
```

Start with a newly submitted task whose contract permits the intended capabilities.
The default tools require `inspect`, `compile`, and `test`, respectively. Include those
names in the contract's `capabilities` array and provide sufficient `calls` and
`tool_ms` budget. The Phase 2 example contract only grants `local_embeddings`; it must
be adapted before submitting a repository-execution task.

### Prepare a candidate

```bash
pbexec prepare "$POTBOT_TASK" --source /absolute/path/to/local/repository
```

This captures the working tree at its current commit, records an environment manifest,
and allocates a new isolated candidate. A submitted task advances through
`initializing` to `running`. An already running task can prepare another candidate.
The response includes `data.state`, `data.candidate`, and `data.environment_id`; use
`data.candidate.id` for subsequent commands.

Tracked modifications and deletions are captured automatically. Use repeatable
`--include relative/path` arguments for execution-relevant untracked, ignored, or
generated inputs, and `--exclude relative/path` for sensitive paths. Paths are literal
repository-relative paths; selected directories are recursive. For example:

```bash
pbexec prepare "$POTBOT_TASK" --source /absolute/path/to/local/repository \
  --include generated/runtime.json --exclude local-secrets
```

Capture leaves the original repository unchanged. The manifest records commit/tree
identities, file content digests and modes, dependency-lock identities, declared
exclusions, and submodule references. Branch labels do not define identity. Submodule
contents are not yet qualified for materialization: their references are retained,
and allocation fails clearly instead of claiming a reproducible candidate. Snapshot
limits are 20,000 files, 32 MiB total payload bytes, and 16 MiB per file. Unsupported
special files, unsafe symlinks, unavailable declared inputs, and changing source bytes
are rejected. See [the snapshot implementation](../src/potbot/tools/repository_snapshot.py).

### Run, inspect, and recover

```bash
pbexec run "$POTBOT_TASK" --candidate CANDIDATE_UUID --tool inspect.files --key inspect-1
pbexec run "$POTBOT_TASK" --candidate CANDIDATE_UUID --tool compile.python --key compile-1
pbexec run "$POTBOT_TASK" --candidate CANDIDATE_UUID --tool test.python --key test-1
pbexec inspect ACTION_UUID
pbexec reconcile RUN_UUID
pbexec resume "$POTBOT_TASK"
```

The default operations are fixed installed definitions:

| Tool | Required capability | Operation |
| --- | --- | --- |
| `inspect.files` | `inspect` | List candidate files with the current Python runtime. |
| `compile.python` | `compile` | Run Python `compileall` inside the candidate. |
| `test.python` | `test` | Run `unittest discover -s tests -t . -v` inside the candidate. |

The CLI does not accept arbitrary command strings. Each default tool has a 30-second
deadline and a 1 MiB limit for each captured stream. The controller API can install
other bounded definitions. Runtime dependencies must already exist in explicitly
permitted runtime/assets; the sandbox does not download packages. A source directory
that needs a different test runner requires an appropriate registered definition.

`run` creates immutable inputs and a proposal, obtains a candidate lease, reserves
budget, checks admission, and dispatches once. The key identifies the command: replay
of a completed action returns its durable completion; a changed task/tool/candidate
or an unresolved attempt cannot silently reuse the key. `inspect` returns the action,
run, and completion, including separate stdout/stderr artifact identifiers. Read the
payloads through the scoped `ArtifactStore` API. Inspections work after restart.

CLI success is exit 0. An ordinary command failure preserves its nonzero exit code
where representable; timeouts use 124, cancellation 130, and infrastructure or
inconclusive outcomes 1. Inspection itself exits successfully even when the inspected
command failed. JSON responses retain `schema_version: 1` and structured `data` or
error fields. See [the execution CLI](../src/potbot/tools/execution_cli.py).

## Service APIs and admission

The Phase 3 API is a Python service interface plus the local execution CLI. The
existing task HTTP routes remain available; this phase adds no execution HTTP routes.

| Interface | Responsibility |
| --- | --- |
| `RepositoryStateStore.capture`, `get`, `snapshot` | Publish and reload immutable repository inputs, parent lineage, environment linkage, and snapshot artifacts. |
| `CandidateWorkspaceManager.allocate`, `claim`, `renew`, `release`, `publish_state`, `cleanup` | Materialize owned candidates, enforce lease generations, publish checked state, and remove only proven disposable directories. |
| `ExecutionStore.create_inputs`, `propose`, `inspect` | Freeze the contract/state/evidence/need-revision manifest and semantic release, persist registered proposals and dependencies, and inspect durable attempts. |
| `ToolRegistry`, `ToolDefinition`, `CapabilityContext` | Install executable definitions and controller-owned grants. Untrusted proposal text cannot add privileges. |
| `ToolGateway.execute`, `reconcile`, `resume` | Admit operations, use the qualified supervisor, publish results, and recover without repeating uncertain work. |
| `InformationNeedStore.create`, `revise`, `inspect`, `history` | Persist questions, decisions, alternatives, evidence requirements, resolution criteria, scope, priority, budget, and retrieval-profile references with immutable revision history. |

Admission checks task lifecycle and active contract, tool identity and arguments,
relative input/output paths, candidate ownership, current lease, the actual input
fingerprint, dependency completions, scoped evidence, and budget reservation. A
proposal cannot supply authenticated workspace identity or substitute its own
permissions. Unknown tools, fabricated references, stale state, and stale fencing
tokens fail before launch. Search nodes and information needs can be assembled by a
scripted controller now; autonomous search policy remains separate work.

## Isolation, capture, and deadlines

[`BubblewrapBackend`](../src/potbot/tools/isolation.py) constructs a private filesystem
and private user/PID/network namespaces, drops capabilities, and installs seccomp.
The candidate is writable; system runtimes and explicitly selected assets are mounted
read-only. `/tmp` is private scratch, `/dev` is minimal, and `/proc` is intentionally
empty. No host home directory, credentials directory, artifact root, or receipt root
is implicitly mounted. Children inherit the restrictions. Direct networking, including
local sockets, is denied; this profile is unsuitable for tests requiring a live
database or network service inside the candidate.

The supervisor passes an argument array and a small explicit environment. It does not
inherit the controller's database credentials. Default process limits are a 4 GiB
virtual address space, 60 CPU seconds, 128 MiB per output file, and 256 file descriptors.
These are per-process limits, alongside the tool's wall-clock deadline and bounded
capture. They do not provide an aggregate process-count, aggregate memory, or total
workspace disk quota. The backend identity records executable hashes, mounts, network
policy, seccomp policy, and resource limits.

On cancellation or deadline expiry, supervision sends SIGTERM and escalates to SIGKILL
after 250 ms. Private PID-namespace teardown and parent-death handling cover detached
descendants. Final waits are bounded. A result reports `processes_stopped` explicitly;
an unconfirmed stop yields `inconclusive`/`indeterminate` and cannot create a trusted
completion receipt. Outcomes distinguish ordinary nonzero exit, timeout, cancellation,
infrastructure error, and inconclusive execution. A missing executable or missing
sandbox is operational evidence, not proof that an algorithm failed.

Secret references are resolved only against the installed operation and controller
grants. Known literal, URL-encoded, and base64 forms are redacted before output
publication; metadata supports explicit secret fields, and bounded capture handles
partial secret suffixes. Configured redaction-policy failure suppresses the raw
capture. The gateway also checks result files for supplied secret material before
snapshot publication. Arbitrary hostile transformations are outside this coverage;
secret-bearing operations must remain trusted adapters. These are explicit coverage
limits, not a claim to recognize every possible credential encoding.

## Restart, cancellation, and quarantine

The controller persists dispatch intent before launching a command. After confirmed
shutdown, it writes a private, atomic completion receipt outside the candidate, then
publishes the database completion and captured artifacts. The receipt binds the
original action/run, candidate, lease generation, input/result fingerprints, outcome,
usage duration, redacted streams, and stop proof.

Recovery distinguishes these cases:

| Observed state | Recovery behavior |
| --- | --- |
| Durable completion exists | Return it, reconcile remaining accounting/release work, and do not execute again. |
| Completion acknowledgment was lost, but a matching stopped-process receipt and unchanged result bytes exist | Claim recovery with a new fence, publish the observed result, and record a recognized completion. |
| Receipt is missing, inconsistent, or result bytes have changed | Retain an indeterminate run, quarantine the candidate, hold uncertain budget, and refuse blind replay. |
| Recovery owner itself fails | Its claim remains durable; further recovery must respect the active lease and can replace an expired or quarantined generation. |

Lease expiry alone does not prove that a filesystem writer stopped. Unresolved
candidates cannot be reassigned for mutation or cleaned up as ordinary disposable
work. Recovery never uses a historical PID as permission to kill an unrelated current
process. This design does not claim exactly-once subprocess execution.

Task cancellation uses the existing `potbot task ... control TASK_UUID cancel`
operation with the latest update token and an idempotency key. It blocks new work and
signals active supervised work through task/lease checks. Already consumed work stays
charged; completed work is not erased. Pause, cancellation, ordinary failure, and
indeterminate completion retain separate meanings.

`execution resume` reconstructs runs, task lifecycle, semantic compatibility, unresolved
operations, and budget. It can reconcile recognized receipts, but it neither starts a
tool nor changes a paused task to running. Use the existing task `control ... resume`
operation for that lifecycle transition. `may_schedule` is true only for a running,
compatible task without unresolved attempts. These behaviors are exercised through
the real [gateway restart tests](../tests/integration/test_execution_gateway.py).

## Semantic contracts and release pins

The authoritative [vocabulary package](../vocab/) version 0.1.0 contains 45 data types
(39 assignable and six abstract), 25 entity types, 16 predicates, 16 concepts, and seven
retrieval profiles. `SemanticRegistry.load()` validates every shipped identifier, the
explicit subtype graph, bounded local schema references, and content hashes.
`inventory(kind)` lists the complete selected catalog; `require_type(...,
assignable=True)` rejects abstract families as primary assignments. Unknown references,
duplicates, omitted entries, and subtype cycles fail validation.

`SemanticValidator` validates typed terms, named predicate roles, mode/force, polarity,
scope, intervals, and bounded guard ASTs. `parse_scalar_literal` retains numeric
lexemes and exact integer/decimal meaning. Boolean, integer, decimal, string, null,
absent, and unresolved values remain distinct. Modality and conditions retain their
own fields: `must(not P)` differs from `may(not P)`, and `when` differs from
applicability. Unsupported atomic disjunction and coupled prohibitions are rejected.

A structurally valid envelope remains a proposal. Validation does not resolve an
entity reference, evaluate a guard, establish truth, or authenticate a producer.
Catalog assertions and schema validity do not qualify a classifier or model.

`SemanticReleaseStore` registers, inspects, inventories, and explicitly selects
releases. New tasks and packets freeze vocabulary, type registry, classifier-policy,
query-profile, and consumer-contract identities as separate versioned extensions.
Selecting release B preserves tasks and packets pinned to A. A changed definition
cannot overwrite an existing release, and missing compatibility declarations fail
clearly. Information-need retrieval profiles are checked against the task's frozen
release. See the [release service](../src/potbot/semantics/releases.py) and
[release acceptance tests](../tests/integration/test_semantic_releases.py).

## Hard supervision of standard local embeddings

Install the optional encoder and download its pinned files explicitly as described in
the [embedding guide](embeddings.md). Existing weights can be reused. Run a real local
smoke with a deadline that includes model preparation:

```bash
uv sync --locked --extra postgres --extra semantics --extra embeddings --dev
uv run --locked potbot embedding smoke \
  --model-dir models/qwen3-embedding-0.6b --timeout-ms 120000
```

`QwenLocalEncoder.encode_supervised` delegates to an isolated worker with a private
scratch directory. The pinned model and trusted adapter are read-only, the worker has
no database access, and network access is disabled. Input text, document/query
preparation, model/tokenizer revisions, pooling, normalization, dimensions, and the
named embedding identity remain unchanged. The existing Qwen profile still yields
1024-dimensional, finite, L2-normalized vectors in the standard space.

The service reserves capacity before tokenizer/model loading. The wall deadline
covers preparation and inference, cancellation stops the worker, and the parent
checks profile/input hashes and process stop proof before considering a result.
Final task and source fences prevent late publication. Confirmed unstarted work
releases unused capacity; post-dispatch uncertainty or missing shutdown proof keeps
the reservation held. Confirmed compute remains charged even when later publication
is fenced. `embedding_worker_outcome` events record outcomes separately from vectors.

The worker uses two CPU threads and a 32 GiB per-process virtual-address-space ceiling;
this ceiling is not a measurement of resident memory. The CLI smoke reports isolation
and timeout scope. Phase 2's earlier runtime allowance was accounting-only; it is not
retroactively described as a hard-timeout test. Live model smoke is an explicit local
qualification and is separate from deterministic timeout/SQL fixtures and normal CI.

## Acceptance map and verification

Run the ordinary checks without services, or request both real acceptance suites:

```bash
uv run --locked python scripts/check.py
uv run --locked python scripts/check.py --with-postgres --with-sandbox
```

The second command requires the explicit `POTBOT_TEST_DSN` and a working Linux sandbox.
`--with-postgres` alone runs SQL tests without sandbox-marked cases;
`--with-sandbox` alone runs process tests that do not need PostgreSQL. Required missing
services fail rather than silently becoming mocked tests. The combined gate includes
gateway isolation, cancellation, restart, recovery, and embedding-accounting fixtures.
CI installs Bubblewrap/libseccomp and requests that combined gate. It does not download
Qwen weights.

The [verification report](phase-3-verification.json) records the delivery's combined
gate, installed-wheel checks, database version, and exact migration checksums.
The [implementation status](phase-3-implementation-status.json) maps the 17 approved
steps to their delivered modules and acceptance files. The populated
[v5-to-v8 upgrade and real CLI tests](../tests/integration/test_phase3_upgrade.py)
check historical hash preservation and command replay through the public entry point.
The [real embedding smoke report](phase-3-embedding-verification.json) remains a
separate model check. These reports extend the earlier phase records.

The map below covers this phase only. Test names are relative to `tests/`.

| Step | Delivered behavior | Principal acceptance checks |
| --- | --- | --- |
| P022 | Immutable commit/content/environment identity and checked parent lineage | `integration/test_repository_states.py`: moved branches, reload, cross-repository parents and immutable lineage. |
| P023 | Dirty tracked inputs, explicit untracked/generated inputs and exclusions; verified materialization | `unit/test_repository_snapshots.py`, `integration/test_repository_states.py`: source preservation, changed runtime input identity, exclusions and digest checks. |
| P024 | Independent owned candidates and bounded cleanup | `integration/test_repository_states.py`: conflicting candidate edits and preservation of source/sibling/artifacts. |
| P025 | Single writer, lease renewal, increasing fences and quarantine | `integration/test_repository_states.py`: concurrent claims, expired publication, stopped-process reconciliation and root ownership. |
| P026 | Immutable knowledge views, search nodes, typed actions, dependencies and action attempts | `integration/test_execution_gateway.py`: exact durable inputs, registered kinds, evidence scope and restart inspection. |
| P027 | Controller capabilities and admission before launch | `integration/test_execution_gateway.py`: stale state/lease, missing reservation, path escapes and untrusted permission text. |
| P028 | Information needs and immutable, scoped revisions | `integration/test_information_needs.py`: revision history, predecessor/scope checks, idempotency and stale tokens. |
| P029 | Authorized secret references and redaction before publication | `unit/test_secret_capture.py`, `integration/test_process_isolation.py`, `integration/test_execution_gateway.py`: denied references, canary injection, bounded capture and stored-output checks. |
| P030 | Enforced Bubblewrap/seccomp boundary with no host fallback | `integration/test_process_isolation.py`, `integration/test_execution_gateway.py`, `unit/test_process_contracts.py`: outside read/write, symlink, network, child inheritance and missing backend. |
| P031 | Supervised registered tools and durable separate captures/input/result state | `integration/test_execution_gateway.py`: real compile/test, literal metacharacters and output inspection after restart. |
| P032 | Deadlines, descendant shutdown, operational outcome classes and stop proof | `integration/test_process_isolation.py`, `unit/test_process_contracts.py`: detached child, cancellation, missing executable and forced final-wait failure. |
| P033 | Cancel active work and reconstruct reconciled tasks without duplicate execution | `integration/test_execution_gateway.py`: real cancellation, charged usage, pause/restart/resume and completed-action replay. |
| P034 | Receipt-based reconciliation and quarantine after lost acknowledgment | `integration/test_execution_gateway.py`: mutate once, crash before commit, recovery-owner expiry and unmatched result refusal. |
| P154 | Pinned immutable catalog loader and explicit subtype graph | `unit/test_semantics.py`, `integration/test_semantic_releases.py`: exact inventory, missing/duplicate IDs, unknown references, abstract assignments and cycles. |
| P155 | Precise typed literals and bounded semantic proposal envelopes | `unit/test_semantics.py`: numeric lexemes, null/absent/unknown, role signatures, modality, intervals, guards and unsupported atomic claims. |
| P156 | Versioned task/context semantic pins and compatibility checks | `integration/test_semantic_releases.py`: A/B selection, retained historical hashes, component immutability, unsupported releases and legacy behavior. |
| P157 | Hard local embedding preparation/inference supervision and conservative accounting | `integration/test_supervised_embeddings.py`: blocked stages, stopped workers, held uncertain usage, confirmed unused release and late-publication fences; explicit real Qwen smoke for the unchanged profile. |

The executable tests define the acceptance scope. Passing them qualifies the shipped
fixture operations and the stated Linux profile; it does not imply arbitrary tool,
model, MCP server, or production hosting qualification.
