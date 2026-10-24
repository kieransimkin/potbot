# R3/R4: model-aware prompts, multi-model hypotheses, admitted evidence investigation

This increment builds on R1/R2's single-writer reasoning tree and the OpenAI-compatible
`potbot` endpoint. It is a functioning, **bounded local/hosted orchestration increment**,
not a claim of a finished distributed autonomous controller. The API's hosted
provider clients remain subject to the operator's credentials, disclosure policy,
rate rules and configured provider capabilities. No model download or local
quantization is triggered by selecting a catalogue entry.

## Live execution

`potbot serve` now defaults to `--reasoning-mode tree` (use `fixed` to retain the
previous proposal/critique/synthesis workflow). A workspace-scoped request to
`model: "potbot"` does the following:

1. Resolve up to eight eligible reasoning models from the existing configured
   provider directory. Prefer models from **different providers** for two
   independent, schema-constrained hypothesis generators; use the same model
   twice if only one is available.
2. Compile each invocation with a versioned, model-family-aware prompt policy.
   Exact model-native control tokens, tokenizer templates and tool-call dialects
   are **not** fabricated in the prompt. The existing provider adapter handles
   native wire protocol; unqualified thinking switches are not enabled.
3. Validate the returned JSON object with 1–3 hypotheses per generator. Each
   hypothesis has a bounded text, assumptions, and a list of evidence queries
   identifying exactly one of `external_libraries`, `external_tools` or
   `internal_workflows`. Duplicate text is collapsed deterministically.
4. Persist an R1/R2 node for each proposal and hypothesis, including its model,
   role, revision, prompt hash, claim, evidence receipt refs and token charges.
   Emit structured lifecycle events through the existing API event logger.
5. Consult only **explicitly host-registered** evidence tools. Each lookup is
   bounded, logged and returns source IDs, SHA-256 digests, excerpts and
   controller-admitted tool receipt hashes. Duplicate queries are reused
   within the run. Unconfigured channels are recorded as `unavailable`, not
   fabricated `completed` searches. No arbitrary commands or MCP endpoints
   can be executed from a model response.
6. Compile a synthesis request using the hypothesis/evidence records as data.
   Only the final user-facing synthesis is streamed through the OpenAI API.
   The tree snapshot persists under
   `<reasoning-root>/<workspace-uuid>/<run-id>.json`.

The interpreter executes sequentially with **one writer per run**. Interrupted
provider/tool calls remain indeterminate on recovery; restarting HTTP does not
silently replay them. These file snapshots do not supply PostgreSQL worker
fencing, cross-process durable reservations, full SYN event-ledger publishing or
normal task-budget settlement. The OpenAI-compatible transport remains an
explicitly policy-gated interactive endpoint.

### Start

```bash
./start.sh serve --workspace-id <UUID> --principal-id <UUID> \
    --provider-policy providers.json \
    --reasoning-mode tree --reasoning-root .potbot/reasoning
```

The default is `tree`. Use `--reasoning-mode fixed` for compatibility with older
free-form providers that cannot yet generate the strict proposal JSON. A failed
schema is a failed run, not a silently accepted hallucinated hypothesis. Every
API request receives a new run ID and is scoped to the authenticated principal.
Do not configure a reasoning root outside the workspace's authorized persistent
storage. Persisted hypotheses may contain sensitive task material; the run
directory is made private on POSIX and files use mode `0600`.

### Explicit evidence-tool registration

`EvidenceGateway` accepts host-owned `RegisteredEvidenceTool` values. Each
tool identifies one search channel, an installed SYN type, and an admitted
invocation handler. Model JSON cannot add registrations or change permissions.
`canonical_cog_search_tool(runtime=..., channel=...)` is a concrete R4 bridge
to the existing `BoundCogRegistry.tool_contracts()` and `ToolContractRuntime`:

- The **host** must first provision a workspace, pin a repository state, import
  the installed SYN/Ember contract snapshot, provide an authorized corpus, and
  bind an admission callback. See `tests/integration/test_semantic_tool_contracts.py`.
- Each lookup creates a distinct `CogRunContext` and executes the existing
  registered `SEARCH` Cog through its controller; it returns the sealed source
  SHA-256 and Cog receipt fingerprint. No raw shell/network/MCP call is opened.
- If no gateway is configured, R4 safely records unavailable search channels.
  The standard `serve` entry point does **not** automatically provision a Cog
  corpus or live web credentials, and therefore starts with no external tools.

For live web and MCP sources, the next integration must explicitly install and
qualify channel adapters via the ToolGateway/Cog admission path, with the same
source digest and invocation provenance requirements. Simply adding an MCP
server URL to model output will not authorize execution.

## Model-specific prompt compilation

`potbot.models.prompt_compiler` provides canonical role instructions for
`hypothesis_generator`, `investigator`, `critic`, `steering_evaluator` and
`synthesizer`. The **meaning** of the task and the evidence payload is kept
separate from the authoritative worker instructions. Each invocation records
an immutable SHA-256 hash of prompt version, family, variant, role and exact
rendered inputs. The transport retains ownership of tokenizer protocol.

Family protocol descriptors cover Gemma 3 vs Gemma 4, Qwen 2/Qwen 3/Qwen
3.5–3.6, LFM2.5, Phi-4, GPT-OSS and DeepSeek. These are **advisory** until
qualified for the exact model revision, quantization, backend and tokenizer.
Generic fallback works without inferring unsupported model controls. Existing
`ModelProfile.to_dict()` exports the advisory `prompt_profile` metadata.

### Qualification

`potbot.models.prompt_qualification.compare_prompt_variants` runs the same
held-out cases against three configurations (`generic`, `family`,
`model_role`) and returns versioned, content-hashed results. The caller must
supply the actual inference function and an **independent** outcome evaluator;
no model scores its own output as verified. All reports are explicitly
`qualified: false` until an operator-managed qualification/admission workflow
is added. Do not automatically promote a prompt because it produced a longer
answer or its model claimed higher confidence.

## Tests

```bash
uv run --locked pytest -q tests/unit/test_prompt_compiler_r3.py \
  tests/unit/test_reasoning_investigation_r4.py \
  tests/unit/test_reasoning_tree.py tests/unit/test_openai_compat.py
```

Integration tests cover the OpenAI `potbot` endpoint with a deterministic
provider fixture, correct model routing, stream-safe synthesis, run persistence,
invalid model proposals, bounded tool calls, unavailable channels and proof
that tool registrations are host-controlled. Live account requests, full
PostgreSQL/SYN admission, Linux sandbox isolation and multi-worker reconciliation
must pass their separate CI/deployment gates before being called qualified.

## Remaining after R3/R4

- **R2 production**: PostgreSQL-backed leases, usage reservations/settlement,
  workspace-scoped event records and robust crash reconciliation.
- **R2.5**: Queue incoming user steering prompts, evaluate current/rejected
  branches at a safe checkpoint and issue one explicit response per prompt.
- **R5**: Independent critique, evidence-weighted scoring, pruning and branch
  expansion. R3/R4 does not yet implement a search over scored branches.
- **R6–R8**: Adaptive beam/MCTS policies, calibration, trusted memory promotion,
  benchmark suites, model-level prompt qualification and deployment operations.
