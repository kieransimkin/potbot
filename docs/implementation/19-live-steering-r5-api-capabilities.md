# R2.5 and R5: live steering, independent critique, bounded beam selection

Implemented over the R3/R4 **single-owner snapshot** reasoning workflow. This
increment does **not** claim durable PostgreSQL multi-worker reasoning, native
Gemini API parity, full OpenAI API parity, or autonomous search over unlimited
tree depths. The R5 beam operates over the R3/R4 candidate hypotheses; R6
will add iterative expand/stop policies and learned/calibrated scores.

## Live user steering (R2.5)

Every `potbot` model inference creates an opaque `run_id`. The OpenAI chat or
Responses route now returns it as the `X-Potbot-Run-Id` header. For interactive
steering, start the inference with `stream: true`, read that header before
completion, then post an independent user prompt to the scoped native route:

```http
POST /v1/reasoning/<32-character-run-id>/steer
Authorization: Bearer <API-token>
Content-Type: application/json

{"request_id":"operator-msg-0001","prompt":"The target has only 2 GB of RAM."}
```

The `202` response means **queued**, never "already applied". Poll
`GET /v1/reasoning/<run-id>/steering/operator-msg-0001` until `status` is
`decided`. List the history at `GET /v1/reasoning/<run-id>/steering`. Each
answer contains a user-facing `explanation`, `decision`, applicable branch ID,
proposed hypothesis, and task-understanding revision. A response applies to
**that steering message**, not to the final original query. A duplicate
`request_id` and identical prompt return the original result; reusing the ID
for a different prompt is rejected.

The runner evaluates at checkpoints: after hypothesis generation, between
investigations, around R5 critique, and after first synthesis. Model suggestions
are strictly validated as **data** and only the owner can commit decisions:

- `spawn_hypothesis`: record the new branch and investigate it. Steering-authored
  hypotheses currently have no generated tool questions; they receive a critic
  assessment with explicit missing evidence rather than fabricated findings.
- `reopen_branch`: admit only a previously `pruned` or `rejected` investigation
  node. Its prior decision and reasoning events remain in the snapshot; reuse
  its previous receipts without re-running an uncertain external Cog.
- `no_change`: preserve the task revision and active branches.
- `needs_clarification`: decline an ambiguous or invalid branch change, with an
  independent explanatory response.

A new understanding revision makes any still-running prior model result stale
at node admission. Final synthesis is buffered until the steering checkpoint,
so a changed task can get one more bounded critique/synthesis pass without
sending a superseded answer fragment. The inbox is sealed at the last
checkpoint, and new prompts submitted after it closes are rejected rather
than silently appended. Accepted and decided messages remain persisted under
`<reasoning-root>/<workspace>/<run-id>.steering/`.

**Isolation:** the bearer token resolves the workspace, and no request may
choose another workspace, a tool, provider credentials, or filesystem root.
All operations are bounded; path components are opaque and checked. The
in-process lock only coordinates *one* ASGI process. File-based inboxes are
**not** qualified for multiple ASGI processes or cross-machine workers. R2
production must migrate these records into scoped PostgreSQL transactions,
lease-aware sequencing, and durable outbox/SYN event publication.

## Branch evaluation and pruning (R5)

A designated critic (preferably a different provider) receives an evidence
packet containing branch IDs and findings; it returns one strict JSON record
per branch with 0–4 relevance, evidence alignment, falsification risk, and
uncertainty ratings plus rationale and test suggestions. Extra keys,
unknown IDs, missing branches, duplicate IDs, nonfinite values, and invalid
scales are rejected. The critic cannot call a Cog or mark a finding verified.

The controller scores candidates deterministically using the bounded ratings,
independent verified source receipts and the result status of admitted search
channels. Missing/unavailable/budget-limited searches receive **no evidence
credit**. A stable top-two beam is selected, with other completed branches
marked `pruned` and preserved for later steering. Models' recommendations are
advisory; rejected suggestions are not automatically treated as disproven.
Each assessment and selection emits inspectable tree events.

This is branch prioritization, **not proof**. A branch's assessment is versioned
with its tree state and must be retested against actual code or runtime before
any implementation can be called verified. The scoring constants are heuristic
and must be calibrated against held-out benchmark outcomes in R6/R8.

## API compatibility matrix (current scope)

| Capability | Potbot OpenAI wire compatibility | Native Gemini wire compatibility |
| --- | --- | --- |
| List/retrieve models | Yes, policy-limited `/v1/models` | No native route |
| Text chat completions | Yes, selected text parameters | Outbound Gemini provider only |
| SSE chat text | Yes | Outbound provider, translated |
| Responses | Minimal text-only request/response and SSE output | No |
| Embeddings | One pinned local Qwen encoder (when installed) | No native route |
| Multimodal inputs | No | No |
| Structured tool/function calling | No general OpenAI tool-call wire API | No native route |
| Native files, batches, realtime, media | No | No |
| `generateContent`, `streamGenerateContent`, `countTokens` | Not native OpenAI operations | No inbound Gemini API |
| Live reasoning steering | Potbot extension `/v1/reasoning/.../steer` | No native route |

Potbot's upstream Gemini **client** is not a Gemini-compatible **server**.
Its Gemini adapter and the hosted OpenAI provider have their own tested
subsets; do not infer full upstream feature coverage or model qualification
from their presence. See official references:
[OpenAI API](https://developers.openai.com/api/reference/overview),
[Gemini native API](https://ai.google.dev/api), and
[Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai).

## Acceptance and execution

```bash
uv run --locked python -m pytest -q \
  tests/unit/test_reasoning_steering_r25.py \
  tests/unit/test_reasoning_evaluation_r5.py \
  tests/unit/test_reasoning_tree.py \
  tests/unit/test_reasoning_investigation_r4.py \
  tests/unit/test_openai_compat.py
```

These are deterministic offline tests, not qualification of a paid provider,
PostgreSQL concurrency, sandbox installation, or hardware-loaded local model.
The same patch also includes the CI #170 Ruff/import and Windows scratch-file
fixes: the original R3/R4 patch should **not** be reapplied to updated main.

### Remaining R2/R6 work

- Persist steering requests and task revisions in workspace-scoped PostgreSQL.
- Provide a real asynchronous run-creation endpoint for nonstreaming clients;
  the streaming header is the current way to learn an active run ID.
- Add user-interaction interrupts, cancellation, and manual approval policies
  for high-impact changes; track all associated runs in the SYN event ledger.
- Generate evidence questions for steering-generated hypotheses, revalidate
  stale evidence, and expand the top-scoring branches recursively.
- Qualify model/role-specific prompts and scoring against actual outcomes.
- Implement additional OpenAI endpoints and a distinct native Gemini API
  compatibility surface, each with conformance tests before advertising parity.
