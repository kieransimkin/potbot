# PostgreSQL reasoning coordination, R6 expansion and upstream model providers

This increment builds on R2.5, R3/R4 and R5 without changing the task contract,
Cog admission or hosted-provider disclosure rules.

## What works

- **Reasoning state**: migration 24 adds workspace-scoped PostgreSQL runs, steering
  prompts and append-only events. The API service obtains a unique run lease,
  checkpoint updates are fenced by its token and expiry, and terminal checkpoints
  close the run. Checkpoints include task revision, branch relationships, scores,
  evidence receipt references and completed model results. The local JSON copy
  is only a supplemental diagnostic snapshot when this backend is configured.
- **Cross-process steering**: the authenticated `/v1/reasoning/{run_id}/steer`
  endpoint writes to PostgreSQL, where the running reasoning owner reads it at
  a checkpoint and publishes a separate decision. Subsequent lookups and
  `/steering` history are scoped by database membership. A new prompt cannot
  be submitted after the synthesis intake has been sealed.
- **Recovery**: `PostgresReasoningStore.expire()` marks expired executions
  `indeterminate` and never automatically restarts charged/uncertain inference.
  Operators must inspect the provider invocation and accounting before
  deciding on manual continuation.
- **R6 adaptive search**: the server defaults to two bounded rounds. After the
  independent R5 critique, the top branches receive new model-generated,
  schema-checked alternative hypotheses. Children link to their source branches.
  The existing registered evidence gateway runs only host-authorized Cogs;
  a hypothesis cannot grant new capabilities.
- **External model providers**: Potbot already has outbound native transports for
  `openai` (`/v1/responses` or Chat Completions) and `gemini`
  (`/v1beta/models/...:streamGenerateContent?alt=sse`), API-key vault integration,
  explicit model discovery, streamed text and provider-reported usage.
  Structured reasoning stages request native JSON-object generation on these
  two providers; synthesis remains plain text. Other hosted providers keep
  their existing prompt-schema-only fallback.

## Provision before startup

Apply the application's schema migration through the existing administrative
migration workflow. Use a restricted PostgreSQL application login, a valid
workspace/principal, and separate API and provider credentials:

```powershell
.\start.ps1 credentials service-set --name api.access_token
.\start.ps1 credentials service-set --name providers.openai
.\start.ps1 credentials service-set --name providers.gemini
.\start.ps1 serve --workspace-id <UUID> --principal-id <UUID> `
  --provider-policy providers.json --reasoning-mode tree --reasoning-rounds 2
```

The policy is **fail closed**. Explicitly set `allow_prompt_disclosure` and add
model-specific rules for `openai` (`protocol: responses`) and `gemini`
(`protocol: gemini`). Use provider-published prices for the relevant account and
model, not zero-value placeholder prices. Model discovery requires an explicit
`POST /v1/providers/{provider}/models/discover` call with the documented
v1 mutation envelope; the returned list is metadata and **does not** authorize
unqualified models or paid calls. The `potbot` virtual model can choose only
models already allowed by the operator policy. No provider keys are exposed by
`/v1/models` or task events.

## Observe a running reasoning tree

```text
GET /v1/reasoning/{run_id}
GET /v1/reasoning/{run_id}/events?after=0&limit=100
POST /v1/reasoning/{run_id}/steer
GET /v1/reasoning/{run_id}/steering
GET /v1/reasoning/{run_id}/steering/{request_id}
```

The run's snapshot includes model-output summaries and potentially sensitive
prompts. Authenticated workspace membership is required. Avoid enabling full
prompt/response console logging unless specifically needed; it is opt-in.

## Boundaries and production qualification

A run has **one fenced writer**, not multiple concurrently scheduled branch
workers. The PostgreSQL lease must remain valid throughout each provider call;
lease expiry prevents late publication. Expired runs are indeterminate rather
than automatically resumed. This update does not implement distributed work
stealing, true parallel branch execution, transactional task-budget settlement
for the interactive API, or automatic tool/MCP installation. The shared task
budget admission and provider invocation ledgers remain separate integration
work. Model-specific structured JSON may still be unsupported by particular
model releases and must be qualified before unattended production use.

Outbound OpenAI/Gemini connectivity is not an implementation of the entire
**inbound** OpenAI API or Google's native Gemini API. Tools, multimodal inputs,
context caching, grounding, speech, batch processing and native provider-side
function-call execution are not enabled by this increment.

Real PostgreSQL migration/RLS/lease tests, simultaneous API process tests,
credentialed provider smoke tests and a successful full CI run are required
before calling this production qualified. Tests with mocked providers establish
wire shape and controller behaviour only; they do not establish API entitlement
or model support on a particular account.
