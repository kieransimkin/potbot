# Phase 7 benchmark protocol

## Three distinct gates

1. **Controller fixture:** test search, Cog conversion, rollback and verification using authored data; never advertise this as live-model performance.
2. **Deployment qualification:** make actual calls to an explicit deployed provider/model revision with exact tokenizer or API protocol, usage accounting and a well-scoped coding/verification receipt. An unknown provider revision, unverified source or unresolved paid request is unqualified.
3. **Held-out comparative evaluation:** compare *paired* tasks and identical repository/test revisions across cohorts, with fixed budgets, model order or randomized counterbalancing, a neutral evaluator, and complete candidate/usage receipts.

## Required records

Capture a stable task and suite manifest, repo commit and working-tree snapshot, environment lock, role, Cog snapshot and activation hash, model and provider revision, prompt/context hashes and disclosure policy, invocations, actual tests, wall-clock and normalized monetary usage, cancelled/failed/indeterminate outcomes and a link to an immutable patch/candidate verification artifact. A model's own statement that tests passed is not evidence.

Primary metric: **actually verified** success on previously unseen tasks at a fixed candidate budget. Secondary metrics: paired improvement versus a single candidate, latency distribution, known and unknown cost, coverage, invalid structured-output rate, model unavailability, redacted code leakage, and effects of code/example retrieval. Do not omit indeterminate or timed-out calls from cost totals or silently label them as incorrect patches. For small cohorts, show uncertainty intervals rather than a definitive model ranking.

EvoLink model entries, API route support and role suitability must be qualified per exact deployment, not assumed from the discovered catalogue. Any third-party example remains untrusted data; reproducing it in an isolated candidate and passing the current task's tests is necessary before claiming the adaptation worked.

## Reproducible held-out manifest (Phase 7 increment 1)

A `BenchmarkSuite` is a versioned, content-addressed manifest with exact
`task_revision_digest`, `repository_state_digest` and `test_suite_digest` for
each case. It stores only the **digest** of the hidden evaluator and keeps that
artifact out of model prompts. It separately pins the model-visible input digest,
model selectors, cohort attempt ceilings, randomization digest, classification
(`synthetic_fixture` or `live_deployment`) and total known monetary ceiling.

The `--suite path/to/suite.json` flag checks that all exported attempts belong to
the declared cases, use the correct identities, have consecutive ordinals and
remain within declared per-cohort and known-cost limits. A suite **cannot**
attest that model providers were called, that evaluator data remained private,
or that a verification receipt is genuine. The read-only CLI still withholds
success rates until an authenticated controller-owned checker is supplied.

**Cost and latency accounting applies to every attempt**, including an
indeterminate or blocked request. Known values are reported as a lower bound
when any usage is missing, and missing values are counted. A suspect request
that may have reached a paid provider must be reconciled; it cannot be silently
retried and omitted from totals.

Example audit command (no remote calls, no model evaluation):

```bash
python scripts/benchmark_phase7.py --suite suite.json \
  --input attempts.json --output audit.json
```

The intended next benchmark runner must (a) obtain explicit provider/model
qualification and task budgets, (b) load the model-visible artifact but not the
hidden evaluator, (c) execute candidates and tests in the existing verified
sandbox, (d) settle or reconcile every invocation and (e) persist signed or
otherwise independently verifiable receipts before allowing published scores.
