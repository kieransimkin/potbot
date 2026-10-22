# Phase 7 — Active investigation and evidence-based benchmarking (increment 1)

**Baseline:** GitHub `kieransimkin/potbot` `74dac45d5535595975e10f545fd6d9cb1652e6a8`, 22 October 2026. Apply this increment *after* that commit, or confirm that each patch still applies to your newer checkout. No remote merge is performed by this package.

## Included work

| Roadmap | State | Changes |
|---|---|---|
| P072: research-action query lineage | Implemented in contracts and migration 16; **DB verification pending** | `research/contracts.py`, `controller/research_store.py`, `persistence/phase7_schema.py` |
| P073: bounded research transport | Partial; no unqualified production egress | `research/http_transport.py`; request deadline, response limit, no redirects; permitted URLs and reservation/disclosure IDs |
| P074: source snapshot observations | Partial; immutable repository observations and DB schema, **database/artefact verification pending** | `research/repository.py`, `ResearchObservation`, repository-artifact reference |
| P075: exact repository inspection | Implemented pure adapter and local tests, production workspace binding pending | `SnapshotResearchAdapter` reads only validated `RepositorySnapshot` payload bytes; no mutable filesystem access |
| Cog transformation rule | Implemented as additive installed Cog signature and preview type catalog; **not activated** | `research/cog.py`, `vocab/phase7-research-types.json` |
| Benchmark preparation | Offline scoring/audit and immutable held-out suite contracts implemented; live tests not qualified | `evaluation/benchmark_ledger.py`, `evaluation/benchmark_manifest.py`, `scripts/benchmark_phase7.py` |
| Baseline CI cleanup | Separate lint patch included in delivery | Latest `verify` job fails in Ruff before reaching tests; apply the companion style patch |

## Admission model

A research action states its workspace, task, contract revision, information-need ID and revision, repository state and semantic-release hash, plus a structured query. It includes source category, operation, literal symbols to preserve through reformulation, target version, requested observation, decision consequence, evidence criteria, stopping condition and fallback. The identity hash includes these scope and version qualifiers. Equal semantic-search strings are not treated as proof of equivalent sources, and changed dependency versions cannot silently reuse a prior result.

The controller authenticates scope against PostgreSQL and rejects stale needs, changed contracts or unknown repository states before writing an action. A transactionally idempotent command makes repeat admission safe. Admission **does not dispatch a tool** or spend money. External effects require their own permission and budget ledger before invocation.

The `SnapshotResearchAdapter` reads the captured immutable repository payload directly; its selected file bytes do not change if a user edits the checkout afterward. Searches are literal, bounded and deterministic. Symlinks are never followed. It distinguishes missing source, binary text, over-limit search, zero matches, and inspected content. Observations retain file SHA-256, content snapshot identity, line ranges, a retrieval timestamp and a known/unknown completeness distinction. License/attribution stays **unknown** until genuinely inspected.

## Cog data path

`ResearchAction` (`synny://type/potbot-research-action`) → **Repository inspection Cog** (`syn://cog/potbot-research-repository-inspection`) → `ResearchObservationBatch` (`synny://type/potbot-research-observation-batch`). The installed implementation is `potbot.research.cog.transform_repository_research`; its `CogSignature` declares exact input/output types and requires a separate trusted controller-scope input. The receipt includes context, handler binding, source/output hashes, invocation and scope digest. **A Cog result is not authorization to execute a patch, resolve a need, or promote memory.**

The type/Cog declarations in `vocab/phase7-research-types.json` are marked **pending semantic-registry import**. Before production use, create and pin a new SYN semantic release containing them, verify its ancestry and Cog instances against the installed signature and acquire a valid repository snapshot artifact. Existing tasks retain their original immutable semantic release and cannot silently acquire these new types.

## Transport and network boundary

`fetch_approved` accepts only an operator-constructed exact HTTPS URL, an authorization/reservation identity, a bounded maximum response and a total deadline; it never follows redirects. It makes no assumption that a network fetch was authorized merely because the URL is syntactically valid. `httpx.MockTransport` tests cover oversized bodies (including no content-length), 302 redirects to localhost, timeouts, rate limits and transport failures.

**Production use still requires an egress proxy/network namespace that enforces a pinned DNS/IP/host allowlist.** Hostname validation alone does not prevent DNS rebinding. There is no general-purpose arbitrary-URL web fetch, provider search catalogue, MCP source acquisition, credential injection, or untrusted example-code execution in this increment. Provider-specific request schemas and budgets belong to later P073/P076–P084 work.

## Benchmarking

The existing `scripts/benchmark_phase6.py` measures deterministic *authored* fixture behavior (6 tasks, deliberately faulty first candidates). Its pass@k figures **are not estimates of LLM success**. They are useful as correctness regression tests only.

The new `BenchmarkSample` requires case ID, model ID/revision, exact task state/test-suite digests, cohort, ordinal, result and verification receipt, wall time and money usage (explicitly optional). `evaluate_benchmark` refuses to combine synthetic and live observations, and requires identical task/test versions across cohorts. Pairwise comparison reports direction only if `baseline` and `treatment` are explicit (except canonical `single` versus `portfolio`). A trusted controller-owned receipt checker is required to publish **verified** success rates. Missing usage is counted, not assumed free. Indeterminate work is excluded from complete-case performance claims rather than turned into a failure.

CLI for inspecting exported records without a trusted database receipt checker:

```powershell
.\start.ps1 -m scripts/benchmark_phase7.py --input observations.json --output audit.json
```

Or from a configured environment:

```bash
python scripts/benchmark_phase7.py --input observations.json --output audit.json
```

With `--suite suite.json`, the report also verifies exact case/cohort identities, ordinal ceilings and known cost limits; it records the suite manifest digest. All recorded attempts, including indeterminate and blocked ones, contribute known usage and missing-usage counts. The report intentionally says `qualification: unverified_claims` and withholds success rates. A future authenticated benchmark runner must bind its checker to the retained PostgreSQL verification and usage ledgers and maintain a **held-out** task set. The CLI does not run models or attempt to rate providers.

## Verification and remaining gates

Verified locally in Python 3.13 with available test packages: 338 relevant unit tests and 33 focused Phase 7 tests (included in the 338) pass. The full repository suite could not be qualified in this environment because its locked optional dependencies and Python 3.12 were not installed. New focused Phase 7 tests cover contracts, snapshot inspection, Cog transformation, HTTP restrictions, benchmark claims and migration structure. Local PostgreSQL was unavailable; the two new `postgres` integration tests are supplied but not executed. Full Ruff, mypy, PostgreSQL, sandbox, clean-wheel and remote GitHub Actions checks remain pending.

Before declaring Phase 7 complete: (1) register/pin the new SYN types and Cog; (2) pass migration/RLS tests on real PostgreSQL; (3) wire production research actions into the controller and worker budgets; (4) add authorized provider search, examples, documentation and MCP fetches, with complete source snapshots and license metadata; (5) link example adaptations, experiments and need resolution to evaluated repository candidates; (6) run independent held-out live-model benchmarks with actual measured usage, hardware profiles and verification receipts; (7) obtain green latest-head CI.
