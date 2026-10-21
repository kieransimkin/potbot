# Initial standard local embeddings

This implements the reference encoder from `Local_Embedding_Addendum.docx`:
**Qwen/Qwen3-Embedding-0.6B at 1,024 dimensions**. It provides typed encoding,
immutable space/source identities, budgeted indexing, and exact PostgreSQL retrieval.
The custom internal semantic representation and learned embedding space are deferred.

Embedding materializations now have explicit names. The initial view is
`general_text`; each operation selects the name **and** an immutable space UUID.
[Named embeddings and artifact manifests](named-embeddings.md) documents application
migration 5, multiple views, the wire schema, and the coordinated upgrade procedure.

## Encoding identity

| Setting | Reference configuration |
| --- | --- |
| Model and tokenizer revision | `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3` |
| Transformers / PyTorch | `4.57.6` / `2.9.1` (CPU build on Linux/Windows with uv) |
| Precision / pooling | CPU float32 / last real token with left padding |
| Output / metric | 1,024 dimensions, L2 normalized, cosine distance |
| Document format | Raw document text |
| Query format | `Instruct: {instruction}\nQuery: {query}` |
| Query instruction | Retrieve relevant code, documentation, and proven workflows for the programming task |
| Default bounds | 2,048 input tokens; at most four texts per encoder batch |
| Preprocessing / generation | `identity-v1` / `standard-v1` |

The complete profile is content hashed. Different model revisions, formatting,
instructions, precision, dimensions, limits, or generations produce different identities.
Equal vector length does not make two spaces compatible. Sources are never silently
truncated; oversized input is rejected. The service currently dispatches one text at
a time, while the local adapter supports bounded batches.

## Install and verify the local provider

Phase 3 runs the standard local provider in a supervised Linux worker. Install
Bubblewrap and libseccomp, and ensure private user/PID/mount/network namespaces are
available. Unsupported or restricted hosts return `capability_unavailable`; they do
not run the model outside the required boundary. Model files may be downloaded on
another platform, but inference currently requires this qualified Linux backend.
See [Phase 3](phase-3.md) for the launch profile and platform limits.

```bash
uv sync --locked --extra postgres --extra semantics --extra embeddings --dev
uv run --locked potbot embedding setup --model-dir models/qwen3-embedding-0.6b
uv run --locked potbot embedding smoke --model-dir models/qwen3-embedding-0.6b
```

Phase 4's shared dependency downloader downloads six exact-revision files, about 1.21 GB total, and atomically writes
`potbot-model.json` with every SHA256 and byte count. Inference verifies this manifest
before loading with `local_files_only=True` and `trust_remote_code=False`. A missing,
changed or incompatible model fails explicitly. Setup verifies and reuses complete
installations and resumes owned partial downloads with visible progress. CLI encoding
commands prepare the model before starting the worker; the worker itself remains
local-only and never switches providers. The weights and generated model manifest
are excluded from Git. `potbot dependencies status|download` exposes the same manager;
see [startup and dependencies](startup.md).

The standalone smoke uses a 256-token profile and a small fixed corpus. It runs
preparation and inference inside the same supervised worker as the service and checks
finite normalized vectors and two expected rankings. It is an installation check;
broad retrieval-quality qualification remains separate.

## PostgreSQL sources and retrieval

First provision a workspace/principal/repository as described in `phase-2.md` and set
`POTBOT_DSN` to the restricted application login. Use the same profile options for
space registration, indexing and querying.

```bash
uv run --locked potbot embedding register-space \
  --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  --embedding-name general_text

uv run --locked potbot embedding put \
  --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  --repository-id REPOSITORY_UUID --source-key connection-cleanup \
  --file examples/reusable-workflow.txt
```

Keep the returned space and source UUIDs. The `put` operation records explicit source
text and metadata as an immutable revision backed by an artifact. The same key/text/
metadata returns the same revision. Changed text creates a new revision and advances
the current head. Explicit source registration is the current boundary; automated
chunking, extraction and learned-memory admission are later work.

Encoding requires a running task for the same repository, with a sufficient budget.
Task submission stays `submitted` until the trusted controller initializes it. For a
local fixture, the following illustrates that service boundary after submitting
`examples/task-contract.json`; this does not run or verify repository code:

```python
import os
from uuid import UUID
from potbot.controller.tasks import TaskService
from potbot.domain.auth import WorkspaceContext
from potbot.domain.tasks import TaskStatus
from potbot.persistence.database import Database

ctx = WorkspaceContext(
    UUID(os.environ["POTBOT_WORKSPACE_ID"]), UUID(os.environ["POTBOT_PRINCIPAL_ID"])
)
task_id = UUID(os.environ["POTBOT_TASK_ID"])
service = TaskService(Database(os.environ["POTBOT_DSN"]))
task = service.inspect(ctx, task_id)
task = service.transition(
    ctx,
    task_id,
    task["update_token"],
    TaskStatus.INITIALIZING,
    "Initialize the explicit embedding fixture",
    idempotency_key="fixture-init",
)
service.transition(
    ctx,
    task_id,
    task["update_token"],
    TaskStatus.RUNNING,
    "Run the bounded embedding fixture",
    idempotency_key="fixture-run",
)
```

Then index and query:

```bash
uv run --locked potbot embedding index \
  --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  --episode-id TASK_UUID --space-id SPACE_UUID --source-id SOURCE_UUID \
  --embedding-name general_text

uv run --locked potbot embedding search \
  --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  --episode-id TASK_UUID --repository-id REPOSITORY_UUID --space-id SPACE_UUID \
  --embedding-name general_text \
  --query "How can I ensure a database connection closes?" --limit 5
```

Search is exact cosine distance over the selected name, space and repository, with row
security and a current-source-head join. Smaller distance is closer. No approximate
index, cross-space score averaging, or synthetic fallback is used. Similarity locates
potentially useful sources; it is not evidence that a source is correct or applicable.

`retire` excludes a source immediately from subsequent retrieval:

```bash
uv run --locked potbot embedding retire \
  --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  --repository-id REPOSITORY_UUID --source-key connection-cleanup
```

Retirement is a visibility tombstone; historical bytes/rows remain for audit. Source
head generation is checked after encoding, so a retired or changed source cannot be
published by stale work. Repeated completed indexing returns the existing vector
without another model call. Concurrent duplicate encodes may both incur compute, but
produce one stored vector and account for both attempts.

## Accounting and current boundaries

Before tokenizer/model initialization the service reserves one call, one concurrency
slot, the profile's maximum input-token allowance, and a bounded runtime estimate.
This is conservative admission: the default profile requires 2,048 remaining tokens
even for a short input, then releases confirmed unused tokens on settlement. Every
valid call records measured actual tokens and full preparation/encoding runtime.
Preparation failures charge confirmed preparation time; uncertain failures after
dispatch hold the reservation for reconciliation. Paused/blocked tasks cannot dispatch.

The default runtime reservation is 60,000 ms, selectable with `--runtime-budget-ms`.
Phase 3 also uses this allowance as a hard worker deadline covering preparation and
inference. Cancellation and deadline expiry trigger process termination and bounded
cleanup. A worker cannot publish vectors unless its result identity is valid, all
processes are confirmed stopped, and the task, provider profile and source generation
still match. Observed runtime, including cleanup, is retained even if it exceeds the
reservation. Missing stop proof holds the reservation, including when startup
acknowledgment was lost; it is never treated as confirmed unused capacity.

Deterministic providers used by the SQL tests explicitly retain their synthetic,
accounting-only contract. Production Qwen calls use process supervision. Indexing is
still explicit after source registration; background memory workers will integrate
with the outbox in the planned memory phases.

## Reproduce the real encoder + database check

With the dedicated test server running and pinned local weights installed:

```bash
POTBOT_TEST_DSN='postgresql://potbot_test:potbot_test_only@127.0.0.1:55432/potbot_test' \
POTBOT_LIVE_MODEL_DIR="$PWD/models/qwen3-embedding-0.6b" \
POTBOT_LIVE_REPORT="$PWD/artifacts/live-embedding-report.json" \
OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 \
.venv/bin/python -m pytest -q tests/integration/live_embedding_roundtrip.py
```

The explicit test creates a disposable database and genuine normal-role identities.
It encodes two documents, persists 1,024-dimensional pgvector values, searches a query,
checks same-input idempotency, retires a source, verifies workspace isolation, and
checks measured budget settlement. Missing weights or services fail this check.
It is not automatically collected by ordinary CI; deterministic provider tests cover
the SQL and failure contracts there. Synthetic test usage is labeled synthetic.

The original Phase 2 delivery run is in `embedding-verification.json`. The expected database
cleanup source ranked first with cosine distance about 0.260, ahead of a CSS distractor
at about 0.879. The supervised Phase 3 smoke is recorded separately in
`phase-3-embedding-verification.json`; the earlier report is preserved as historical
evidence. These tiny-corpus measurements establish integration correctness only.

Primary references: [Qwen model card](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B),
[pinned source revision](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B/tree/97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3),
[pgvector](https://github.com/pgvector/pgvector/tree/v0.8.7),
[uv CPU PyTorch configuration](https://docs.astral.sh/uv/guides/integration/pytorch/).
