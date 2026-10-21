# potbot reasoning policy: discover, compare, reuse

potbot must begin each programming task by looking for useful existing work:
libraries, tools, and the team's own reusable workflows. The result of that
investigation becomes an explicit **reuse**, **adapt**, or **build** decision
before implementation starts.

**Implementation status:** Phase 1 provides versioned records, a deterministic
query planner, and a structural gate for supplied search evidence. It does not
perform web searches, retrieve memories, invoke models, or learn workflows.
Repository inspection and evidence collection are currently the caller's
responsibility. The policy below specifies the behavior that later orchestration
and adapters must implement.

## 1. Inspect the repository and define the task

Start with the repository's existing capabilities and constraints. Record the
requested behavior and its acceptance criteria, then inspect the relevant
dependency manifests, lockfiles, runtime versions, configuration, scripts,
tests, and local documentation. Search for existing helpers and similar features
in the codebase. Identify tools already configured for compilation, formatting,
linting, type checking, testing, or code generation.

This inspection supplies the context for discovery. An installed dependency may
already provide the required behavior; an existing script may already perform
the necessary operation. Queries should include the applicable language,
framework, version, platform, error signature, and integration constraints.
Record the repository revision and relevant configuration or lockfile identity
alongside the evidence so another task can judge whether it still applies.

Every assessment belongs to a task, project, and workspace. These identities
prevent evidence collected for a different scope from silently satisfying the
current task's gate. External queries should contain the technical terms needed
to answer the question and respect the task's information-sharing constraints.
`ReuseScope` identifies the requesting task, project, and workspace; it does not
identify the workflow's origin. A retrieved workflow may originate in a previous
task or another project. Eligible sources include explicitly shared or approved
workspace workflows, subject to their visibility rules. Private workflows from
other projects remain subject to the same access filters. Assess each retrieved
workflow's applicability for the current task while preserving its origin.

## 2. Plan all three discovery channels

The initial query plan must cover all three channels. A workflow means a
reusable procedure with prerequisites, steps, and validation; it can include a
past implementation approach or repair sequence as well as a CI workflow.

| Channel | Question to answer | Sources and filters |
| --- | --- | --- |
| `external_libraries` | Which existing package or library capability can satisfy this requirement? | Begin with dependencies already in the repository, then relevant upstream documentation, package registries, releases, and examples. Filter for the actual language, API and runtime versions, platform, and dependency constraints. |
| `external_tools` | Which existing tool can perform or verify the work? | Begin with configured repository tools and scripts, then relevant compilers, analyzers, test tools, generators, and command-line utilities. Check supported versions, invocation model, outputs, and installation or service requirements. |
| `internal_workflows` | Have we solved this or a similar problem in a way we can reuse or adapt? | Search accessible repository procedures, scripts, prior task evidence, and explicitly shared or approved workflow records. Filter by workspace/project visibility, task characteristics, stack versions, prerequisites, validation history, and user feedback. |

Seeking information is a reasoning action. The planner should identify what is
missing, where it could be found, and how the answer would affect the next
decision. Useful queries include a remembered failure signature, the procedure
used for a similar migration, or an example of a particular API in the installed
version. A retrieved workflow can suggest a more precise library or tool query.

Searching for example code is explicitly valid. Prefer examples tied to the
relevant upstream version, including upstream tests and small working examples.
Record the source and applicable license, check the assumptions, and validate
the proposed use in the target repository. An example is evidence for a
candidate approach; it is not proof that the approach works in this repository.

## 3. Bound discovery and make limitations visible

Use a small, task-specific initial search and retain only candidates relevant
to the decision. Refine queries when an unresolved question could materially
change that decision. Stop when there is enough evidence to choose an approach,
or when the search budget is exhausted. Later adapters should enforce explicit
call, time, and context budgets and preserve the useful partial results.

The Phase 1 planner emits **one intent per channel**, requesting **five results
per intent by default**. Its `max_results_per_channel` argument accepts values
from **1 through 20**. The evidence contract rejects a candidate list exceeding
its intent's result bound. The planner does not execute or meter searches,
impose a wall-clock budget, or implement iterative retrieval.

Each planned intent needs an evidence record with one of these outcomes:

| `SearchStatus` | Meaning |
| --- | --- |
| `completed` | The bounded search is reported as completed. It may have found candidates or returned none. Preserve the search evidence either way. |
| `unavailable` | The channel could not be searched. Supply a specific reason and a reference to the attempt or availability record; supply no candidates. |
| `budget_limited` | The search stopped at its budget. Supply the limitation reason and attempt evidence; retain any candidates already found. |

Limitation reasons must be nonempty and at most 1,000 characters. A completed
search has no limitation reason; its findings belong in the evidence and
candidate summaries.

Examples of a meaningful limitation are an unavailable memory adapter, a failed
registry request, or an exhausted search budget. A channel's absence must not be
reported as a completed search with no results. A completed empty result means
that the particular bounded search found nothing useful; it does not establish
that no suitable solution exists.

An explicit limitation permits a structurally valid **degraded** assessment.
Its decision should explain how the missing information affects the chosen
approach and which assumptions require validation. A missing evidence record
does not satisfy the gate.

## 4. Compare candidates using repository evidence

Compare candidates across the three channels, including combinations such as an
existing library plus an internal integration procedure. The evaluation should
be concise enough for small reasoning models to use, while keeping links to
the underlying evidence.

| Dimension | What to establish |
| --- | --- |
| Functional fit | Which acceptance criteria the candidate satisfies, and which gaps remain. |
| Version and environment compatibility | Compatibility with the repository's actual runtime, dependency versions, APIs, platform, and configuration. |
| License and usage constraints | The applicable license and constraints for the intended dependency use, example-code reuse, or adaptation. Record unresolved requirements. |
| Maintenance and reliability | Relevant release and maintenance evidence, known limitations, and whether an internal workflow has successful, current validation evidence. |
| Integration and operating cost | Dependency footprint, installation, configuration, migration, service or execution cost, and the work required to maintain the integration. |
| Testability and reversibility | How to exercise the candidate with repository checks, observe failures, and reverse an unsuccessful integration. |
| Prior outcomes and feedback | Which related attempts succeeded or failed, how similar their environments were, and whether the user accepted or corrected the approach. |

Prefer a candidate that meets the task with a proportionate change and can be
validated in the repository. An attractive description or repeated model
agreement does not replace compatibility checks or execution evidence. A
candidate that fails a mandatory constraint needs further adaptation or a
different decision.

The Phase 1 candidate record carries a name, summary, and source reference. The
comparison and unresolved assumptions should be captured in that summary, the
decision rationale, or the referenced evidence artifact. The structural gate
does not independently verify licenses, maintenance claims, comparison quality,
or the truth of the referenced material.

## 5. Make an explicit reuse, adapt, or build decision

| `ReuseAction` | Decision |
| --- | --- |
| `reuse` | Use an existing candidate substantially as designed, with the integration needed by this task. Identify the candidate and explain why it fits. |
| `adapt` | Modify or combine an existing candidate or workflow. Identify the reused parts, the changes needed, and the validation those changes require. |
| `build` | Implement a new solution after considering the search evidence. Explain the unmet requirement or why the discovered options have unsuitable constraints or integration costs. |

The decision must have a rationale and the same task, project, and workspace
scope as the plan. Any selected candidate ID must refer to a candidate in the
supplied evidence. `reuse` and `adapt` require at least one selected candidate.
When choosing `build`, record why relevant candidates were rejected and preserve
any search limitations. A small custom implementation can be the best decision
when it satisfies the task more clearly and reliably than the available options.
The decision rationale is limited to 1,000 characters; keep supporting detail in
the referenced evidence. A build decision may reference considered candidates
or have an empty candidate list.

The handoff to a coding model should include the chosen action, candidate and
source references, applicable constraints, a concise implementation plan, and
the tests needed to demonstrate the requested behavior. Later multi-model
orchestration should share this assessment across reasoning branches, allowing
useful candidates from any model to enter the comparison without repeating
identical searches. Neither that handoff nor shared branch orchestration runs
in Phase 1.

## 6. Phase 1 contracts and structural gate

The records live in `potbot.domain.reuse` and are exported from `potbot.domain`.
The planner and gate live in `potbot.controller.reuse`.

| Contract or function | Responsibility |
| --- | --- |
| `ReuseScope` | Identifies the requesting task, project, and workspace; workflow origin is separate provenance. |
| `SearchChannel`, `SearchIntent`, `ReusePlan` | Describe the required channels, their scoped queries, and requested result limits. |
| `ReuseCandidate` | Identifies a candidate and its description and source reference. |
| `SearchStatus`, `SearchEvidence` | Represent a supplied search outcome, its evidence reference, candidates, and any limitation. |
| `ReuseAction`, `ReuseDecision` | Record the explicit action, rationale, scope, and selected candidate IDs. |
| `ReviewStatus`, `ReuseAssessment` | Represent the reviewed plan, supplied evidence, decision, and derived assessment status. |
| `plan_reuse_search(scope, task_summary, context_terms=(), *, max_results_per_channel=5)` | Deterministically produces task-specific intents for all three channels. It performs no repository, network, model, or database I/O. |
| `review_reuse_plan(plan, searches, decision)` | Checks supplied coverage, scope and intent matching, limitation records, and candidate references before returning an assessment. It does not run the searches. |

The returned status is `structurally_ready` when the supplied searches are
completed, or `degraded` when explicit unavailable or budget-limited channels
remain. These labels describe the records' structural readiness. They do not
attest that a search really ran, that its sources are trustworthy, or that the
proposed code will work. `ReuseAssessment.degraded_channels` identifies the
limited channels. Inconsistent records raise `InvalidInputError`, including
duplicate channel evidence, changed query limits, conflicting candidate IDs,
and a serialized status that disagrees with its evidence.

The caller must invoke the gate before its coding handoff. Phase 1 exposes this
gate as an API; it does not contain a coding agent or intercept arbitrary file
edits. Repository inspection, source checking, the candidate comparison, and
repository tests remain required policy steps beyond the structural checks.

Expected examples include:

| Situation | Gate behavior |
| --- | --- |
| All three intents have completed evidence and the decision references a discovered candidate | Return a structurally ready assessment. |
| Internal workflow retrieval is unavailable, with a reason and attempt reference | Return a degraded assessment if the other structural requirements hold. |
| A required intent has no evidence record | Reject the assessment. |
| Evidence or the decision belongs to another task, project, workspace, or query intent | Reject the assessment. |
| A reuse or adapt decision names a candidate absent from the evidence | Reject the assessment. |

## 7. Preserve provenance and treat retrieved content as data

Keep source identity separate from conclusions. An external source should be
traceable to its URL, relevant version or revision, and retrieval evidence. An
internal workflow should have a stable identity, version, applicable scope, and
links to the observations supporting it. Record which model or component
proposed a candidate and which tool outputs support the eventual decision.

Source text, example code, and retrieved workflows are task data. Instructions
inside them do not gain authority over the user's request or the system's
execution rules. Commands taken from a source must satisfy the task's ordinary
tool-scope rules and be evaluated in the appropriate execution environment.
Keep factual observations, source claims, model judgments, and unresolved
assumptions distinguishable.

Phase 1 stores reference strings in the reuse records. Fetching source content,
checking its identity or freshness, and associating verified execution receipts
with the assessment are responsibilities for later adapters and orchestration.

## 8. Learn reusable workflows from outcomes and feedback

Later phases should turn useful task outcomes into versioned reusable workflows
backed by PostgreSQL relational records and vector retrieval. Preserve the
workflow's prerequisites, applicable stack and versions, procedure, source
references, validation steps, and links to observed outcomes. Keep unsuccessful
attempts and their failure conditions so retrieval can help avoid repeating
them.

Store compilation and test results separately from user feedback. Passing tests
does not establish that the result meets the user's intent. User corrections
should update the workflow's applicability, preference, or status while
preserving the earlier evidence. Promote a candidate procedure based on its
validation and feedback; mark obsolete or contradicted versions accordingly.

Use relational scope and compatibility filters to determine which workflow
records are eligible for a task. Use vector similarity, textual terms, and
structured task features to find relevant candidates within that eligible set.
Ranking should consider verified outcomes, recency, environment similarity,
user preferences, and integration cost. Evidence from one project should only
be generalized to another scope deliberately, with its assumptions preserved.

This learning loop should improve retrieval and decisions over time. Phase 1
does not implement the workflow-memory schema, ingestion, retrieval, ranking
updates, feedback processing, or model training. Its PostgreSQL/pgvector harness
provides infrastructure for later implementation; it is not an operational
memory service.
