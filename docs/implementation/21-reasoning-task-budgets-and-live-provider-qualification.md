# R6.1 — Task-bound reasoning accounting and live provider smoke qualification

This increment follows R2.5/R5/R6, keeping the existing OpenAI-compatible API
and *outbound* OpenAI/Gemini transports intact. **No paid requests are performed
by installation, schema migration, or CI.**

## Scope

- Migration 25 adds a workspace-scoped reasoning invocation receipt linked to
  the running task, immutable contract revision, reasoning run, node, and the
  authoritative `budget_reservations` row. Prompts, keys and full completions
  are not stored in accounting rows.
- A trusted task context can opt into accounting with `potbot_task_id` on a
  `model: "potbot"` Chat Completions or Responses request. It must be a canonical
  UUID for a provisioned **running** task. Requests not specifying it remain
  interactive and do not make task-budget claims. A task ID does not select
  another workspace: every operation runs under the bearer principal and SQL RLS.
- Before a provider call, a single SQL transaction checks the active run lease,
  obtains a task-budget reservation and records the bound node receipt. A second
  transaction atomically checks the lease and current task contract while
  committing a `dispatched` marker. Only then may the transport be invoked.
- Provider terminal usage is saved as `observed` **before** calling the existing
  `BudgetLedger.settle`; a crash between those operations can be reconciled via
  `ReasoningBudgetJournal.settle_observed` without repeating inference.
- A confirmed failure **before** dispatch releases the reservation. A failure
  after possible dispatch or a missing usage report marks it `indeterminate`
  and retains the full allowance. Reconciliation requires actual provider usage
  from a trustworthy source; never guess zero tokens or silently replay calls.
- Budget dimensions required for task-bound requests: `calls`, `tokens`,
  `inference_ms`, `money_microunits`, and `concurrency` (plus the standard
  `tool_ms` task-contract dimension). Insufficient budget fails closed.
  Admission estimates tokens conservatively from input bytes + output limit,
  and cost from the operator's configured model rates, not provider invoices.

## API example

The existing bearer token authenticates the configured workspace service:

```json
{
  "model": "potbot",
  "potbot_task_id": "YOUR-EXISTING-RUNNING-TASK-UUID",
  "messages": [{"role": "user", "content": "Compare these approaches"}],
  "stream": true
}
```

`GET /v1/reasoning/{run_id}/invocations` returns workspace-scoped receipt
metadata, current accounting states, usage estimates and associated reservation
IDs. Use `/v1/tasks/{episode_id}/budget` to inspect the task-wide authoritative
balance. No endpoint permits a caller to insert arbitrary usage or override a
provider observation. This `potbot_task_id` field is a Potbot extension, not an
OpenAI-standard parameter.

## One-call live OpenAI/Gemini qualification

Install the locked provider extras and configure `providers.openai` and/or
`providers.gemini` through Potbot's OS credential vault. Use an operator-owned
policy file permitting the exact model and authorizing disclosure.

```bash
uv run --locked python scripts/qualify_hosted_reasoning.py \
  --provider openai --model OPERATOR_ENABLED_MODEL \
  --provider-policy providers.json
```

The command above performs **only local preflight**. To authorize exactly one
paid request and disclose the fixed synthetic prompt, add:

```bash
  --allow-paid --confirm-prompt-disclosure \
  --report artifacts/openai-reasoning-smoke.json
```

For Gemini use `--provider gemini` and an operator-approved model. The smoke
checks structured JSON, streaming terminal state and provider usage. It writes
only the prompt/answer SHA256 hashes, latency, token metadata and returned model
identity. It does not publish secrets, raw response text, hidden reasoning or
provider error bodies. This is **smoke evidence**, not a qualified production
model deployment; account-specific rate limits and capability matrices remain
subject to further acceptance work.

## Qualification checklist

1. Run the unit tests for provider contracts and the new accounting adapter.
2. Run real PostgreSQL acceptance under a provisioned test workspace and
   principal: `tests/integration/test_reasoning_budget_postgres.py`.
3. Exercise both providers with operator keys, consenting explicitly to paid
   requests. Compare returned model IDs, actual usage and policy ceiling.
4. Crash/restart at reserved, dispatched, observed and settled checkpoints.
   No unobserved provider call may be auto-replayed, and no abandoned
   reservation may be silently released.
5. Run `python scripts/check.py --with-postgres --with-sandbox` in the pinned
   CI environment. Windows service and native identity gates must also remain
   green. CI never requires real paid provider credentials.

## Boundaries

- The task budget admission/settlement path applies to reasoning-tree model
  calls when `potbot_task_id` is explicitly supplied; it does **not** convert
  arbitrary interactive direct-model calls into background task jobs.
- Parallel branch workers, hardware-qualified local inference and model/tool
  performance benchmarking remain future work.
- The provider smoke tool requires explicit operator approval and does not
  claim complete OpenAI/Gemini multimodal feature coverage.
