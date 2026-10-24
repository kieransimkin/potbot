# R6.6 — Agentic graph model-role benchmarking, judge calibration and monitoring

This is an **opt-in benchmark runner** for systems with a real local OpenAI-compatible
inference endpoint, plus optionally operator-enabled native OpenAI/Anthropic/Gemini
accounts. It neither downloads weights nor infers that a ChatGPT or Claude web
subscription grants paid API access. It never runs paid calls in CI.

## Goals

1. Compare **the same held-out task/input** across a model/role/prompt matrix.
2. Establish which tiny local models can replace larger ones *in particular roles*,
   while recording accuracy, latency, token usage, and reported provider billing.
3. Measure evaluator reliability separately: can a judge detect objectively wrong
   model answers, and is its preference stable after exchanging A and B positions?
4. Monitor result drift across repeated runs of identical suites; retain separate
   health evidence for **workflows and assumptions**.
5. Propose conservative **advisory** routing changes, never silently changing
   production policy without external qualification and operator approval.

## Verified upstream tiny-model candidates

The following identifiers name real open-weight families (not verified local
installations or measured VRAM requirements):

| Purpose | Upstream model | Guidance |
| --- | --- | --- |
| Minimum reasoning | [Qwen/Qwen3-0.6B](https://huggingface.co/Qwen/Qwen3-0.6B) | Thinking *and* non-thinking; use the exact tokenizer/backend switch when qualified |
| Small reasoning | [Qwen/Qwen3-1.7B](https://huggingface.co/Qwen/Qwen3-1.7B) | Better-capacity reasoning candidate, still measured before adoption |
| Minimum coding | [Qwen/Qwen2.5-Coder-0.5B-Instruct](https://huggingface.co/Qwen/Qwen2.5-Coder-0.5B-Instruct) | Ultra-light code classification and small edits |
| Small coding | [Qwen/Qwen2.5-Coder-1.5B-Instruct](https://huggingface.co/Qwen/Qwen2.5-Coder-1.5B-Instruct) | Preferred first coding candidate |
| Cheap classifier | [HuggingFaceTB/SmolLM2-360M-Instruct](https://huggingface.co/HuggingFaceTB/SmolLM2-360M-Instruct) | Triage/categorisation baseline, not trusted as a verifier |

Model aliases in `examples/agentic-benchmark/local-models.json` are examples for
an Ollama-style OpenAI-compatible endpoint on loopback, **not exact repository
revisions or qualified deployments**. The runtime/quantization/KV cache must be
measured under the intended GPU and context settings. Never infer real VRAM
residency solely from parameter count. A Qwen3 thinking-mode comparison requires
a separately qualified backend configuration; a prompt string is not proof
that the model actually used a thinking mode.

## Preflight (no model requests)

```bash
uv run --locked python scripts/benchmark_agentic_models.py preflight \
  --models examples/agentic-benchmark/local-models.json \
  --suite examples/agentic-benchmark/role-suite.json
```

The bundled suite uses simple **synthetic** KeywordMoves/DanceFlow graph-planning,
evidence-provenance, steering, coding-defect, tool selection, triage, and
blind-judge cases. These are deterministic rubric matches **not** proof that a
model can build, run, or verify a repository patch. The full KeywordMoves
acceptance suite is a separate gate: see the workflow integration guide and
`scripts/run_keywordmoves_workflow.py`.

## Live LOCAL model comparison

Install and run the desired model server separately (for instance, an
OpenAI-compatible Ollama, llama.cpp, or LM Studio endpoint). Confirm its model
IDs and revision/quantization. The example config assumes HTTP loopback
`http://127.0.0.1:11434/v1` and has three model targets; edit the file if the
server uses different model aliases. Only a loopback /v1 URL is accepted; the
benchmark does not follow redirects, use proxies, or accept arbitrary remote URLs.

```bash
uv run --locked python scripts/benchmark_agentic_models.py run \
  --models examples/agentic-benchmark/local-models.json \
  --suite examples/agentic-benchmark/role-suite.json \
  --output-dir .potbot/benchmarks \
  --confirm-prompt-disclosure \
  --repeat 3 --judge-derived --store-text
```

`--store-text` is **optional** and saves private generated responses in the
result directory; omit it to retain only digests, scores, and metadata.
`POTBOT_BENCH_LOCAL_KEY` can supply a bearer token for the local endpoint
without putting credentials in the model config. Treat all outputs as sensitive.
Each run creates a new ID and a durable `possibly_dispatched`/terminal journal.
An interrupted provider attempt remains unresolved and is **never** automatically
replayed by this tool. A `comparison.html` view shows each model's scores and,
when explicitly enabled, its actual text, grouped by task and prompt variant.

## ChatGPT/Claude providers: two supported connectivity choices

**Native API:** Enable exact `openai` and `anthropic` models via Potbot's existing
`ServiceCredentials` native vault and `HostedPolicy` rates. Edit
`examples/agentic-benchmark/hosted-models.template.json` to use actually
accessible model IDs, revisions, and roles. Selecting a hosted provider requires
**both** `--allow-paid` and `--confirm-prompt-disclosure`, a disclosure-enabled
operator policy and finite `--max-paid-calls` and `--max-cost-microunits` limits.
Every request passes Potbot's normal model-specific disclosure policy.

**Local browser-account gateway:** A separately authorized tool such as
CatGPT-Gateway may expose ChatGPT and Claude as OpenAI-compatible model IDs on
the machine's loopback endpoint. Add those aliases to the `loopback_openai`
config *only after that local server is running and verified*. This runner does
not log into websites, infer account entitlements, or bypass gateway policies.
All credentials remain outside the model configuration.

### Paid examples (only with explicit operator approval)

```bash
uv run --locked python scripts/benchmark_agentic_models.py run \
  --models private/hosted-models.json \
  --suite examples/agentic-benchmark/role-suite.json \
  --provider-policy private/providers.json \
  --output-dir .potbot/benchmarks \
  --allow-paid --confirm-prompt-disclosure \
  --max-paid-calls 100 --max-cost-microunits 1000000
```

The cost field uses **operator-supplied rate ceilings**, not an invoice. Real
provider usage, latency and model identity are recorded; missing usage stays
unknown, not zero. These benchmarks are not transactional Potbot task-budget
calls. They require a separate explicit benchmark call/cost cap; a future
integration should move admission and settlement into the task-budget ledger.

## Prompt variants and judging

`generic`, `family`, and `model_role` are compared against identical task and
input records. Prompt identity includes the selected profile, system/user text,
model family and role. Only the benchmark `json_subset`/`exact_text` rubric
has access to expected values; model input receives the task and context only.

The judge role is measured separately in two ways:

- **Fixed A/B pairs:** known correct/incorrect examples, each in both positions.
- **Derived pairs:** two actually observed model outputs for the same case,
  one matching the independent rubric and one not. Model IDs are masked;
  judges cannot score their own outputs. Both A/B orders must agree with the
  reference. If there is no such pair or no independent qualified grader, no
  judge ranking is invented. This measures rubric agreement rather than a
  universally reliable evaluator.

Sample size and paired input identity are mandatory. The report uses a Wilson
lower confidence bound and requires at least 12 complete case/trials by
default before offering **any** routing recommendation. Faster local
substitutes are suggested only inside a narrow quality margin and remain
advisory. The score for a simple syntactic fixture must never be displayed
as verified coding or agent task success. Judge/model identity may introduce
bias; this protocol measures but does not eliminate it.

## Continuing workflow and assumptions monitoring

```bash
uv run --locked python scripts/benchmark_agentic_models.py monitor \
  --history-dir .potbot/benchmarks \
  --events private/workflow-assumption-observations.json \
  --output .potbot/benchmarks/health-report.json
```

The monitor compares compatible suite identities, separate execution windows,
and workflow/assumption evidence. `workflow`, `model`, and `assumption` events
retain source digests, statuses and optional receipt hashes. It flags performance
regressions, indeterminate calls, missing evidence and hypothesis failures.
A receipt *reference* is not proof of a verified execution: source-origin and
actual receipt validation remain the responsibility of the trusted SYN ledger.
No routing, permissions, workflows or prompt profiles are automatically changed.
Schedule the runner on the actual model-equipped host through the existing
service/job scheduler. The included weekly GitHub Actions workflow performs
**offline unit/preflight checks only**—no downloads, provider secrets, or costs.

## Integrate into production Potbot

1. Persist run/attempt/metric hashes into scoped SYN execution events, keeping
   benchmark result content out of the public API and repository.
2. Add real code-patch tests through Potbot's qualified Linux sandbox, and
   genuine KeywordMoves full-workflow outputs under a held-out source revision.
3. Qualify model/role/prompt combinations against independent evidence, including
   failure, timeout, refusal, token usage, and judge-position bias.
4. Implement a controller-owned routing policy with minimum evidence, confidence
   bounds, workload stratification, latency/cost ceilings, drift detection, and
   human-approved promotion/rollback. Do not let a model certify itself.
5. Run periodic checks in the *actual hardware/provider environment*, and only
   replay deterministic offline tasks after verifying there was no uncertain
   paid execution.
