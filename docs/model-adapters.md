# Model adapter profiles and local preflight

This Phase 5 foundation preserves the five exact model variants already declared
in Potbot's catalogue. A catalogue profile describes how an adapter should treat
that model. It is not an installed model, a deployment configuration, or evidence
of live qualification.

| Role | Exact model | Controller instruction handling |
| --- | --- | --- |
| Reasoning | `microsoft/Phi-4-mini-instruct` | Native system message through the exact tokenizer template. |
| Reasoning | `google/gemma-3-4b-it` | Explicitly prepend instructions to the first user turn; text mode. |
| Coding | `Qwen/Qwen2.5-Coder-7B-Instruct` | Native system message through the exact tokenizer template. |
| Coding | `google/codegemma-7b-it` | Explicitly prepend instructions to the first user turn. |
| Coding | `deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct` | Native system message through the exact tokenizer template. |

The adapter delegates chat formatting and token counting to an explicitly supplied
local tokenizer. Template bytes, tokenizer revision, prompt adapter and limits
belong to the immutable deployment identity. Potbot does not copy a different
family's chat tokens, silently switch to a base/FIM model, or infer a qualified
context limit from a model's name. The initial response protocol is a bounded JSON
envelope; native tool syntax alone does not authorize execution.

## Inspect the catalogue

```bash
bash ./start.sh models catalog
```

```powershell
.\start.ps1 models catalog
```

Every profile reports `qualified: false`, `deployment_configured: false` and
`live_qualification_required: true`. The output includes the relevant primary
source URLs and adapter notes. It does not download weights or require a database.

## Read-only local preflight

```bash
bash ./start.sh models preflight --profile local-host.json --project-root .
```

The host document has these exact top-level fields:

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer `1`. |
| `deployment` | Complete existing `DeploymentSpec.to_dict()` representation. |
| `model_revision` | Immutable 40- or 64-character lowercase hexadecimal revision/digest. Mutable branches such as `main` are rejected. |
| `model_directory` | Relative POSIX path below this project's `models/` directory. |
| `files` | Explicit array of pinned files, each with `path`, `sha256`, `bytes` and `kind`. |
| `backend_distribution` | Exact installed Python distribution providing the configured backend. Its normalized name must match `deployment.backend`. |
| `context_memory_bytes` | Positive, explicit estimate of context memory for the configured request limits. |
| `runtime_overhead_bytes` | Positive, explicit estimate of runtime overhead. |

Each `files` entry identifies an ordinary local file by relative path, expected
SHA256 and positive byte size. Supported `kind` values are `weights`, `tokenizer`,
`configuration` and `template`. Every kind is required; there must be exactly one
template entry. A template entry may be the raw UTF-8 Jinja file or a JSON file
with a single string-valued `chat_template` property. Multiple template choices
must be resolved explicitly before producing the host profile.

The deployment object uses the existing immutable registry fields: canonical
model, weights digest, backend/version, quantization, tokenizer revision, chat
template digest, output adapter, tool protocol, usable context and output limits,
prompt versions, roles, response schemas, resource profile and cancellation
behaviour. No default revision, backend or memory estimate is filled in for a
missing field. The `resource_profile.memory_bytes` value is the resident weight
allocation; preflight adds the explicit context and runtime estimates. Its
measurement must be `estimated` or `measured`, and its device must match the host
observation.

For this host-document format, `deployment.weights_digest` is the SHA256 of the
canonical JSON list of the sorted **weight-file inventory**, including each
weight file's path, SHA256, byte size and kind. The Python helper
`weights_inventory_digest(files)` computes that identity. Expected file hashes
must come from the operator's trusted, pinned acquisition record. Preflight
rehashes the actual bytes; it does not prove that an arbitrary operator-supplied
inventory is an authentic upstream release. `deployment.chat_template_digest`
is SHA256 of the exact UTF-8 template string.

Preflight verifies every declared file and the selected template, rejects linked
paths, compares installed package metadata against the exact backend version, and
compares resource requirements with observed available CPU memory. This is a
point-in-time feasibility observation, not a memory reservation. GPU capacity
requires a separately supplied device observation from a later device adapter;
CPU free memory is never used as GPU memory. Missing observations remain explicit
blockers.

No backend/model module is imported by preflight. It performs no network request,
model download, credential lookup, model-code execution or inference. Secret
values are not valid host-profile fields. An incomplete profile produces a
sanitized `invalid_host_profile` blocker without echoing its input.

## What a successful check establishes

The most successful result is `ready_for_load_check`. Every report still says:

```json
{
  "qualified": false,
  "loading_tested": false,
  "generation_tested": false,
  "network_used": false
}
```

The report retains deployment and host-profile fingerprints, checked byte/file
counts, resource observations, blockers and the outstanding conformance inventory.
That inventory is a list of requirements, not test results. Deterministic test
fixtures use `origin: fixture` for their host observations; they cannot pass as
measurements from a user's machine.

The next live checks must load an eligible deployment under the normal resource
and budget controls, exercise its actual tokenizer and bounded output, preserve
cross-model evidence identifiers, and test cancellation, failure and repair. A
coding deployment must additionally produce an applicable exact-base patch and
pass the fixture's targeted repository checks. P051–P055 remain live qualification
work; none is completed by the catalogue or this preflight command.

## Model-specific evidence and constraints

The following primary sources were checked on 21 October 2026. Documentation
links using `main` are references, not revisions accepted by a host profile.

- [Microsoft's Phi-4-mini-instruct model card](https://huggingface.co/microsoft/Phi-4-mini-instruct)
  documents its own chat/function-call format and a 128K family context claim.
  Provider examples may enable remote model code. This profile does not enable it.
- [Google's Gemma 3 4B IT card](https://huggingface.co/google/gemma-3-4b-it)
  describes the multimodal model and requires acceptance of access conditions.
  Potbot's initial profile admits text only. Google's
  [prompt-formatting guidance](https://ai.google.dev/gemma/docs/core/prompt-structure)
  describes placing controller instructions in the initial user turn. Exact
  tokenizer formatting still controls the resulting bytes.
- [Qwen's Qwen2.5-Coder-7B-Instruct card](https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct)
  distinguishes its default 32,768-token configuration from longer YaRN
  configurations. Longer configurations require independent deployment identity
  and qualification.
- [Google's CodeGemma 7B IT card](https://huggingface.co/google/codegemma-7b-it)
  distinguishes instruction chat/generation from the pretrained completion/FIM
  variants. Access conditions apply to its files.
- [DeepSeek's Coder V2 Lite Instruct card](https://huggingface.co/deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct)
  reports 16B total and 2.4B active parameters; resident weight planning must use
  the complete deployment. Its
  [tokenizer configuration](https://huggingface.co/deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct/blob/main/tokenizer_config.json)
  exposes a limit that differs from the card's family context claim. Potbot
  requires an explicit usable deployment limit. Provider examples using custom
  code do not constitute approval to run unreviewed code.
