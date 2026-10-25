# R7 plan: temporal steering, recursive atomic tasks and specialist model routing

This increment adds a **non-executing advisory foundation**. It does not silently
modify running reasoning graphs, cancel provider calls, qualify local weights,
authorize external effects or auto-promote benchmark winners. R7 must integrate
these contracts with PostgreSQL/SYN ledgers, the R2.5 steering inbox, admitted
Cog operations and the model benchmarking/health monitor before production use.

## Why use a hybrid routing architecture?

Independent implementations provide useful patterns but are not drop-in Potbot
controllers:

- [LangGraph](https://github.com/langchain-ai/langgraph) has conditional edges,
  checkpointing and durable human interrupts. Reuse the state/interrupt *pattern*,
  not an additional overlapping persistence authority.
- [AutoGen SelectorGroupChat](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/selector-group-chat.html)
  has model-based speaker selection and optionally custom deterministic selection.
- [Hugging Face smolagents](https://huggingface.co/docs/smolagents/reference/agents)
  distinguishes atomic tool calls from expressive code agents; registered Cogs
  remain Potbot's only execution authority.
- [RouteLLM](https://github.com/lm-sys/RouteLLM) trains quality/cost routers from
  measured preference data. Potbot should favor held-out, paired role outcomes
  and health monitoring rather than trust model self-confidence.
- [DSPy MIPROv2](https://dspy.ai/3.4.0/api/optimizers/MIPROv2/) jointly optimizes
  instructions and examples. Keep task-role and model-family prompt identity
  pinned and compare on held-out cases.
- [LiteLLM Router](https://github.com/BerriAI/litellm) provides deployment-level
  latency/rate-limit routing and fallbacks. Potbot additionally needs task-level
  quality, evidence and permission gates.

**Fastest decision:** exact schema / receipts / permissions / dependencies use
ordinary code. **Fast model:** ambiguous intent, atomicity, tool-selection and
source-relevance classification. **Larger model:** novel decomposition, coding,
complex contradiction resolution and final answer synthesis.

## Temporal steering: three principal categories, four implementation results

1. `interrupt`: explicit **stop/pause/replace now**. Apply the deterministic
   urgent detector *before* any language-model round-trip. The controller fences
   additional affected work and assesses cancellation. Any already-dispatched
   provider call retains its uncertain accounting until reconciliation.
2. `deferred`: a new instruction for *after* the present activity. This is the
   **default for action requests** without a clear urgent qualifier. Queue with
   explicit dependency on current completion, not as a new hypothesis.
3. `background`: supplementary task knowledge, no new executable instruction.
   Record a source-linked context revision. Material changes (e.g. RAM/platform/
   license constraints) request re-evaluation before next affected dispatch;
   unchanged facts do not prompt tree expansion.
4. `clarify`: genuinely ambiguous message/query that needs a user decision.

Multi-intent messages must be split into component steering events. A single
classification is only an advisory fast path; it is **not** the entire NLP
parser. Replies should confirm what stopped, what's queued, what background
was saved, and what remains uncertain. The existing spawn/reopen/no_change R2.5
semantics determine **where** to alter reasoning, not **when** to do it;
therefore temporal steering is a separate dimension.

`potbot.controller.steering_triage.classify_steering` provides the rule-first
classifier with optional model callback. It does no I/O and no mutation.
For candidate small models benchmark `Qwen3-0.6B`, `Qwen3.5-0.8B`,
`LFM2.5-1.2B-Instruct`, and `SmolLM2-360M-Instruct`. Clear emergency stop is
handled deterministically first. Measure urgency false-negatives, deferred
misroutes, context-impact recall, and first-alert latency against gold labels.

## Recursive decomposition and a stopping criterion

Every subtask gets its **own** input/output contract, dependencies, admitted
capability references, limits and checkable acceptance criteria. Recursively
split only when no registered bounded operation can satisfy those criteria.

An atomic leaf is eligible when **all** are true:

1. Exactly one host-registered operation implements the requested effect.
2. Preconditions, permissions, resource bounds and required inputs are known.
3. Output is independently verifiable; retries/idempotence and side effects
   have explicit semantics.
4. The operation cannot be usefully subdivided without losing its contract.

Otherwise, decompose again, ask for a missing input, or declare blocked. Limit
node count, depth, fan-out and budgets. Reject dependency cycles and repeated
plans rather than recurse indefinitely. Successful child planning never grants
execution permission. For a model-selected action, Potbot must match the exact
Cog identity and registered input schema before dispatch.

`potbot.controller.atomic_tasks.ATOMIC_KINDS` enumerates 41 testable candidate
leaf operations across input parsing, routing, scheduling, evidence, coding,
verification, monitoring and human approval. `TaskGraph` enforces a finite DAG
and host-authorized leaf admission in memory. It is *not* yet the production
multi-worker PostgreSQL execution scheduler.

### Example: KeywordMoves end-to-end objective

- Goal: produce a reviewed YouTube-relevant keyword shortlist from lyrics and
  explicitly supplied evidence.
- Split into literal extraction, source import, candidate matching, human review
  and shortlist verification.
- `text-library/extract-literal` and `observed-evidence/import-observations` are
  separate permitted atomic Cog/tool executions; they can run independently.
- A native-source result has an exact source hash. An unavailable import is not
  zero search volume and an LLM-proposed phrase is not actual demand.
- `candidate-shortlist/select` remains blocked until a source-hash-bound
  editorial review exists. No automatic publication follows.

## Models and quantization

`potbot.models.specialist_catalog` lists candidate *upstream model identities*.
No candidate is an installed, qualified deployment. Exact family/filename,
immutable commit, quantization, tokenizer/chat-template compatibility,
licence and host runtime must be verified before installation.

| Model | Total/active B | Likely first experiment |
| --- | ---: | --- |
| Qwen3-0.6B / Qwen3.5-0.8B | 0.6 / 0.8 | temporal steering, intent, atomicity |
| SmolLM2-135M / 360M | 0.135 / 0.36 | classifier baselines |
| FunctionGemma-270M | 0.27 | finetuned function/argument selection |
| LFM2.5-230M / 350M | 0.23 / 0.35 | fast tool extraction and triage |
| LFM2.5-1.2B Instruct / Thinking | 1.17 | fast tool routing / deeper planning |
| Qwen2.5-Coder-0.5B / 1.5B | 0.5 / 1.5 | code triage / small repairs |
| Qwen3-1.7B / Qwen3.5-2B | 1.7 / 2 | hypothesis, planning, critique |
| SmolLM3-3B | 3 | reasoning/critique alternative |
| Granite 4.0 H Tiny (MoE) | **7 / 1** | tool-use and hybrid reasoning |
| LFM2.5-8B-A1B (MoE) | **8.3 / 1.5** | complex tool routing / planning |
| Qwen3 Reranker 0.6B | 0.6 | evidence candidate ranking, *not* chat |
| Qwen3 Embedding 0.6B | 0.6 | vector retrieval, *not* chat |
| ModernBERT-base | 0.15 | **fine-tuned** classifier head, *not* chat |

Sources: official [Qwen3](https://github.com/QwenLM/Qwen3),
[Qwen3.5](https://huggingface.co/Qwen/Qwen3.5-0.8B),
[Qwen2.5 Coder](https://huggingface.co/Qwen/Qwen2.5-Coder-1.5B-Instruct),
[FunctionGemma](https://ai.google.dev/gemma/docs/functiongemma),
[Liquid AI](https://huggingface.co/collections/LiquidAI/lfm25),
[Granite](https://huggingface.co/ibm-granite/granite-4.0-h-tiny),
[SmolLM3](https://huggingface.co/HuggingFaceTB/SmolLM3-3B),
[Qwen3 Reranker](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B) and
[ModernBERT](https://huggingface.co/answerdotai/ModernBERT-base).

### Required storage/VRAM metadata

A **specific installed model** requires an exact record:

- `model_id`, `immutable_revision`, `quantization`, `backend`, `artifact_uri`,
  `sha256`, measured `disk_bytes` and legal/access status.
- `total_parameters`, `active_parameters` where known, and architecture.
- measured `peak_vram_mib`, `peak_ram_mib`, context length, KV-cache type,
  offload split, batch size and tested host identity.
- deployment qualification and per-role benchmark receipts.

The catalogue exposes **Q2_K, Q3_K_M, Q4_K_M, Q5_K_M, Q6_K,
Q8_0, F16 and BF16 as _candidate_ strategies** for generators; the exact
quantized file must actually exist. Non-generative models default to BF16
previews and require their own encoders/rerankers. Sizes shown by `models
specialists` are parametric projections with explicit runtime headroom, *not*
measured GPU fitting claims. For MoE, use **total** parameters in all storage
estimates, never the active expert count. A quantized variant is individually
selected by an explicit manifest including SHA256, exact size and full commit
revision; only an explicit operator command downloads it. A verified file is
not proof the complete deployment can load or generate.

```bash
potbot models specialists --role steering --quantization Q4_K_M
potbot models specialists --model ibm-granite/granite-4.0-h-tiny --quantization Q5_K_M
potbot models install-artifact --manifest operator-pinned-gguf.json \
    --models-root ./models --confirm-download
```

For gated FunctionGemma, explicitly acknowledge upstream terms using
`--accept-model-terms`; this does not log you into the vendor or bypass rights.
The installer relies on the **optional** `huggingface_hub` package and validates
exact bytes. The operator must supply the quantized file's *real* repository,
manifest and checksum; not every official base repo provides GGUF weights. The
current example command only supports artifacts in the same canonical repository
and does not automatically discover or authorize third-party quantizers.
Supply `artifact_repository` in the manifest and
`--allow-third-party-quantization` when the exact checksum-verified GGUF is
published by a distinct account; the base model licence still applies.

## Monitoring and self-optimization roadmap

- **R7.1:** Connect temporal steering to the durable PostgreSQL inbox and run
  checkpoint; urgent messages fence affected new dispatch, deferred work gains
  dependency edges, and background context gets a source-linked revision.
- **R7.2:** Expose the atomic operation registry through Cog contracts; models
  propose typed decompositions, deterministic admission enforces graph limits,
  then the scheduler starts independent eligible leaves.
- **R7.3:** Benchmark each atomic kind against small models, strong models,
  deterministic tools and prompt variants. Evaluate accuracy, tail latency,
  tokens, memory, licence, cost and *quality of reviewing peers* using
  independent gold checks with A/B order inversion.
- **R7.4:** Add conservative role-specific runtime selection. Require paired
  holdout data, minimum correctness floors and acceptance evidence. New routes
  operate in shadow/canary mode before promotion; regressions automatically
  **revert to the last qualified route**, but never auto-grant extra permissions.
- **R7.5:** Continuous drift and assumption monitoring. Collect source-preserving
  health events per task/input distribution, model revision, quantization,
  backend, host and prompt. Investigate changes in error rate, cost, latency,
  refusal, schema compliance and judged correctness. Schedule repeat benchmarks
  with explicit paid-call limits.
- **R7.6:** Promote reusable verified atomic workflows to object memory through
  existing approval/admission gates; no self-authored model claim is ground truth.

### Acceptance gates

1. An urgent user `stop` is acknowledged before next affected tool dispatch;
   an uncertain in-flight paid call is never replayed automatically.
2. Deferred tasks execute only after their declared predecessor completes.
3. A background RAM constraint revises relevant assumptions without creating an
   unauthorized task or cancelling unrelated work.
4. Recursion terminates on admitted atomic capabilities, missing inputs, max
   depth/node count, or a demonstrably unresolvable decomposition.
5. Judge benchmark scores are calibrated against reference pairs, randomized
   A/B ordering and false-positive verdicts; no judge may certify itself.
6. A chosen quantized variant loads successfully on the exact host at its tested
   context/batch or is marked unqualified—not "fits in VRAM" on arithmetic.
7. Routing changes require independently checked outcomes and preserve full
   rollback, task provenance and held reservations.

The current increment supplies catalogue estimates, exact-asset selection,
non-executing task/steering contracts and benchmark fixtures. The integrations
above remain explicit future stages; green unit tests are **not** their
production qualification.
