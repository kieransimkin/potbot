# Hosted reasoning and coding providers

The hosted adapter package adds direct API transports for Claude, OpenAI, Grok,
Gemini and EvoLink. It accepts exact provider model identifiers without a fixed
model shortlist. The existing five local model profiles remain separate.

Hosted identities are `provider + model + protocol`. A provider's model alias may
change its serving implementation. The adapter records the actual response model
when supplied, alongside the requested identity. It does not invent local weight
hashes or tokenizer revisions for a remote service, and discovery never qualifies
a model as a working coding agent.

## API surfaces

| Provider key | Default text API | Catalogue API | Authentication |
| --- | --- | --- | --- |
| `anthropic` | `https://api.anthropic.com/v1/messages` | `/v1/models` | `x-api-key` and pinned `anthropic-version: 2023-06-01` |
| `openai` | `https://api.openai.com/v1/responses` | `/v1/models` | Bearer API key |
| `xai` | `https://api.x.ai/v1/responses` | `/v1/models` | Bearer API key |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse` | `/v1beta/models` | `x-goog-api-key` header |
| `evolink` | Model-family default on `https://direct.evolink.ai` (see below) | `https://api.evolink.ai/v1/models` | Bearer API key |

OpenAI and xAI also support the `chat_completions` adapter. EvoLink supports
`chat_completions`, `responses`, `messages`, and `gemini` protocol selections for
models whose provider documentation permits that protocol. When no protocol is
configured, EvoLink Claude models use `/v1/messages`, Gemini models use native
`/v1beta/models/{model}:streamGenerateContent?alt=sse`, and GPT models use
`/v1/responses`. Other EvoLink models default to `/v1/chat/completions`. An
operator rule can select a different documented protocol for that model. Unknown
and future model families require verification of the route and parameters before
use. A failed request does not trigger an attempt on another API. The Responses
adapter sends `store: false`.

EvoLink Chat requests use `max_tokens`: its Gemini, GLM and DeepSeek references
document this field, while Gemini can ignore `max_completion_tokens`. EvoLink's
GPT compatibility layer translates `max_tokens` to the newer field. Direct OpenAI
and xAI Chat requests use `max_completion_tokens`. Native Messages, Gemini and
Responses requests use their own documented output bounds. These are provider
request limits, not proof of a billing ceiling; the controller reconciles actual
reported usage and preserves uncertainty after disconnects.

Sampling parameters are omitted unless explicitly supplied. This matters because
some reasoning models reject temperature or seed parameters. A model's presence
in an account catalogue does not establish support for every optional parameter.

## Model discovery and selection

`HostedProviderClient.list_models(provider)` follows bounded native catalogue
pagination and returns all models in that response, including media models.
Anthropic cursors and Gemini page tokens are handled explicitly. Compatible
catalogues exposing `has_more` and `last_id` use cursor pagination too. Repeated
cursors, incomplete pagination metadata and size limits produce an error rather
than a partial list presented as complete.

Each catalogue entry includes `text_generation`, which is `true`, `false`, or
`null`. Explicit capability metadata is used when available. Minimal
OpenAI-compatible entries often expose only a name, so their capability remains
unknown. Recognizable image, embedding, video and audio product names are marked
outside text generation, and the same dedicated-media family guard rejects their
selection for reasoning/coding before dispatch. This conservative name guard is
not a complete capability classifier; opaque aliases and future names still need
operator verification. A multimodal input capability alone does not disqualify a
text reasoning model.
All entries retain `qualified: false`; unknown capabilities require verification.

EvoLink's changing account catalogue supplies the model identifiers, including
models released after this Potbot version. The implementation has no fixed list
of supported text model names. Image, audio, video, moderation and embedding
entries remain visible in the inventory, but their specialized generation
endpoints are not exposed as reasoning or coding inference. This increment
implements text reasoning/coding transports; it does not implement media jobs.

Model selection also requires the operator's outbound disclosure policy and
configured model/rate rules. Catalogue discovery alone grants no right to send a
repository's content to a provider. The hosted adapter refuses generation when
its controller authorizer is absent, before reading the provider credential.

`HostedModelDirectory(database, policy, credentials)` supplies the API/CLI
directory callbacks. `catalog(context)` returns the five local advisory profiles,
provider configuration flags, explicit operator model identities and cached
discovered entries without making HTTP calls. `discover(context, provider)` uses
only the provider's model-list endpoint. It rechecks current database membership
before the request and after the response, before publishing or caching metadata.
Every cached read also requires a fresh database membership check.

The process-local cache is isolated by workspace and provider. Defaults are a
five-minute TTL, 128 entries and 32 MiB of serialized metadata, with LRU eviction.
A catalogue larger than the configured cache allowance is returned completely by
discovery but is not cached; no partial list is represented as complete. The
HTTP discovery path has a 60-second elapsed deadline checked at page and chunk
boundaries, with individual I/O timeouts capped at 15 seconds. Membership checks
do not hold a database transaction open during HTTP requests.

Each hosted directory entry distinguishes credential configuration, matching
operator rules, text capabilities and `generation_enabled`. An opaque or future
model ID with unknown capabilities requires the operator to verify its text
generation API before authorizing it. A wildcard rate rule is broad authorization
for unknown names; it is not a capability verification. Metadata-declared media
entries are disabled in the directory, while the generation transport additionally
blocks recognizable dedicated-media names. The cache is advisory metadata, not a
durable qualification record or a universal classifier for future model names.

## Credentials and outbound requests

The injected resolver receives only one of these references:

```text
secret://providers/anthropic
secret://providers/openai
secret://providers/xai
secret://providers/gemini
secret://providers/evolink
```

Resolved keys are used in authentication headers, never query strings, serialized
requests, job records or exception messages. Provider error bodies can contain
prompt text or credentials, so errors expose only a stable local classification,
HTTP status, retryability and whether dispatch may have occurred.

Every destination is fixed in the provider adapter. Requests cannot supply an
arbitrary base URL. Production HTTP clients verify TLS, ignore environment proxy
configuration, disable redirects and disable implicit transport retries. Explicit
catalogue reads use the fixed provider metadata endpoint and require the matching
credential; they do not send prompts or repository content.

## Streaming and accounting

The adapter normalizes native SSE into `ProviderTextDelta` and one
`ProviderCompleted`. It handles fragmented UTF-8, comments, multiline `data`
fields, and LF/CRLF/CR line endings. Raw wire size, individual SSE events, event
count, answer byte size and timeouts are bounded. A complete ordinary JSON
response is also accepted from compatible endpoints that do not stream.

Native thinking channels, encrypted reasoning and tool-call argument deltas do
not become public answer text. The completion record distinguishes `stop`,
`length`, `refusal`, `tool_calls`, and `failed`. Truncation, malformed JSON,
inconsistent terminal status or interrupted streams cannot masquerade as a
normal stop. Model-proposed tool calls never execute in this transport.

Usage is provider-reported and preserves input, output, total, cached-input,
cache-creation and reasoning counters. Anthropic cache-read and cache-creation
tokens are included in total input. Gemini thought tokens are included in total
output. OpenAI-compatible completion/output counts already include their
reasoning component, so it is not added twice. Intermediate stream usage cannot
stand in for terminal accounting.

Missing terminal usage remains unknown. The durable controller must retain its
reservation until reconciliation instead of inventing zero usage. Token usage
does not prove a billed dollar amount; monetary accounting uses the explicitly
configured conservative rate policy and identifies it as an estimate. Current
prices, cache discounts and special billing tiers are not hardcoded here.

The controller owns the durable dispatch marker, task budgets, queue lease and
result acceptance. The client makes one HTTP attempt. Closing a local stream
cannot prove that the remote model stopped or that it will not be billed, so
`stop(operation_id)` closes the connection but always returns `false` for remote
stop confirmation. Cancellation is checked before sending and at stream I/O
boundaries; individual reads are capped at 30 seconds and the requested deadline
is checked between reads. Provider completion beyond a disconnected client is
not a confirmed cancellation.

## Validation scope

The automated tests use `httpx.MockTransport` and native protocol fixtures. They
exercise all provider/protocol combinations, authentication placement, fragmented
streams, pagination, media eligibility, usage, failure, output bounds and local
cancellation. No provider key, account catalogue or billable generation request
was used to run those tests. Account permissions and the selected model's actual
parameter support require an explicitly budgeted live qualification.

## Primary API references

Checked on 22 October 2026:

- [Claude API overview](https://docs.anthropic.com/claude/reference/getting-started-with-the-api),
  [model listing](https://docs.anthropic.com/en/api/models-list), and
  [Messages streaming](https://platform.claude.com/docs/en/build-with-claude/streaming).
- [OpenAI model listing](https://developers.openai.com/api/reference/resources/models/methods/list)
  and [Responses streaming events](https://developers.openai.com/api/reference/resources/responses/streaming-events/).
- [xAI model listing](https://docs.x.ai/developers/rest-api-reference/inference/models)
  and [Responses reference](https://docs.x.ai/developers/rest-api-reference/inference/responses).
- [Gemini model metadata and listing](https://ai.google.dev/api/models)
  and [native content generation](https://ai.google.dev/api/generate-content).
- [EvoLink CLI catalogue mapping](https://evolink.ai/docs/en/cli/reference),
  [model inventory](https://evolink.ai/models),
  [Chat Completions reference](https://evolink.ai/docs/en/api-manual/language-series/gpt/chat-completions/chat-completions-reference),
  [Responses quickstart](https://evolink.ai/docs/en/api-manual/language-series/gpt/responses/responses-quickstart),
  [Claude Messages reference](https://evolink.ai/docs/en/api-manual/language-series/claude/claude-messages-api),
  [native Gemini quickstart](https://evolink.ai/docs/en/api-manual/language-series/gemini/native-api/native-api-quickstart),
  [Gemini Chat output-limit reference](https://evolink.ai/docs/en/api-manual/language-series/gemini/openai-sdk/openai-sdk-reference),
  [DeepSeek Chat reference](https://evolink.ai/docs/en/api-manual/language-series/deepseek-v4/deepseek-v4-chat),
  [GLM Chat reference](https://evolink.ai/docs/en/api-manual/language-series/glm/chat-completions/chat-completions-reference),
  and [Messages integration guidance](https://evolink.ai/docs/en/integration-guide/pi).
