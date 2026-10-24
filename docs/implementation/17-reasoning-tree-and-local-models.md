# R1/R2 reasoning-tree foundation and low-memory model catalogue

This increment extends the existing OpenAI compatibility foundation. It is not a
completed autonomous reasoning controller and does not switch the `potbot` virtual
model to tree execution. The prior fixed proposal/critique/synthesis path remains
unchanged.

## Implemented

- `potbot.controller.reasoning_tree.ReasoningTree`: bounded, single-writer
  durable JSON snapshot of hypotheses, parent edges, task revisions, node claims,
  rejection reasons, evidence references, token allowances and event sequence.
  Atomic replace and fsync prevent torn snapshots. Recovered in-flight claims
  become **indeterminate**, not automatically re-dispatched.
- Steering decisions are recorded with prompt, explanation and idempotency key.
  `spawn_hypothesis` and `reopen_branch` change the task revision and cause
  in-flight old-revision results to be marked indeterminate. `no_change` and
  `needs_clarification` preserve the active revision. A steering decision is
  supplied by a trusted caller: automatic LLM-based steering evaluation is not
  implemented in this increment.
- Eight modern advisory local-model candidates supplement the five existing
  models. Their model IDs and upstream availability require operator verification;
  no download, quantization or live qualification is implied by catalogue entry.
- A conservative shortlist estimates weight memory from **total** parameters,
  not active MoE parameters. This is not a runtime VRAM measurement.

## Boundaries and next stages

The JSON snapshot controller is suitable for deterministic unit testing and
single-writer prototypes **only**. Do not use it as a shared multi-worker
scheduler. R2 production work must implement PostgreSQL transactions, fencing,
workspace-scoped permissions, durable budget reservations and settlement,
indeterminate provider reconciliation, and SYN event-ledger integration.
R2.5 must provide authenticated steering submission, queued evaluation,
versioned task understanding, automatic choice of spawn/reopen/no-change,
and a dedicated steering response to each prompt. R3 must connect qualified
models to node execution and the OpenAI virtual model; R4–R6 add evidence
Cogs, branch scoring, search and convergence. No private model reasoning
traces should be exposed through public endpoints.

The following names are provisional operator-verifiable catalogue candidates:
`google/gemma-4-26b-a4b-it`, `Qwen/Qwen3.5-35B-A3B`,
`LiquidAI/LFM2.5-8B-A1B`, `google/gemma-4-e4b-it`.
Do not infer a valid Hugging Face repository or compatible tokenizer from these
strings alone. The user-requested "Qwen3.6–35B" is not silently treated as
an established repository identifier; the advisory candidate uses a distinct
Qwen3.5 family label pending exact upstream verification.

## Test

```bash
uv run --locked pytest -q tests/unit/test_reasoning_tree.py \
  tests/unit/test_model_profiles.py tests/unit/test_models.py \
  tests/unit/test_local_selection.py
```
