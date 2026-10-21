# Named embeddings and artifact manifests

Each stored vector belongs to a named representation and an exact immutable encoding
space. Application migration **5** introduces this contract without changing existing
profile fingerprints, source hashes, vector values, artifact bytes or budget records.
The initial named representation is **`general_text`**.

## Identities and storage

| Field or record | Meaning |
| --- | --- |
| `embedding_name` | A canonical representation label, such as `general_text` or `workflow_text`. |
| `embedding_spaces.id` | The exact pinned encoder profile, including its model/tokenizer revision, query formatting, normalization and generation. |
| `embedding_views.id` | An immutable workspace-scoped binding of a name to a space and supported input format. |
| `embedding_sources.id` | One immutable source occurrence/revision, including its repository, source key, text and metadata. |
| `embedding_vectors.id` | One materialization of a source under the selected name and space. |
| `artifacts.id` and `digest` | Immutable, content-addressed raw bytes. Several source occurrences can share these bytes. |

Names contain 1–64 ASCII characters, start with a lowercase letter, and use lowercase
letters/digits with optional single dot, underscore or hyphen separators. Spaces,
uppercase letters, repeated separators and trailing separators are rejected. Names
are case-sensitive; validation never silently rewrites them.

`embedding_views` is immutable and protected by the existing workspace row-security
model. Its unique key is `(workspace_id, embedding_name, space_id)`. Its only supported
`input_format` is currently `source_text_v1`: the exact stored source text encoded as
UTF-8, passed through the space's existing raw-document preparation. The corresponding
query preparation remains pinned by the profile.

The vector uniqueness key is:

```text
(workspace_id, source_id, embedding_name, space_id)
```

A foreign key requires the named space binding to exist before a vector is inserted.
The same name can retain multiple space generations, and the same space can have
multiple names. Callers must always choose the exact space UUID; there is no implicit
"latest" alias or fallback to a different representation.

The name is deliberately outside `EmbeddingProfile.to_dict()`. Registering a second
name for the same profile therefore reuses the same space identity and creates a
different immutable view binding. Existing profile hashes retain their meaning.

## API and CLI

The Python API adds an optional keyword to the existing operations:

```python
space = service.register_space(context, embedding_name="general_text")
service.index_source(context, episode_id, source_id, space["id"], embedding_name="general_text")
results = service.search(
    context,
    episode_id,
    repository_id,
    space["id"],
    "database connection cleanup",
    embedding_name="general_text",
    limit=5,
)
```

Omitting the keyword selects `general_text`. Registration returns `embedding_name`,
`view_id` and `input_format` alongside the existing space fields. Indexing and search
return the selected name, view ID, exact profile hash and encoding-input hash as
provenance. A missing binding or incompatible provider fails before model dispatch.

To add another explicitly labelled raw-text view using the configured profile:

Set `POTBOT_DSN` to the restricted application login first. Indexing and search also
require the pinned local model and a running task with sufficient budget; see the
[local-provider setup](embeddings.md). Registration and manifest export do not load
the model.

```bash
uv run --locked potbot embedding register-space \
  --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  --embedding-name workflow_text

uv run --locked potbot embedding index \
  --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  --episode-id TASK_UUID --source-id SOURCE_UUID --space-id SPACE_UUID \
  --embedding-name workflow_text

uv run --locked potbot embedding search \
  --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  --episode-id TASK_UUID --repository-id REPOSITORY_UUID --space-id SPACE_UUID \
  --embedding-name workflow_text --query "How do I close a database connection?"
```

Use the returned space UUID and identical profile options throughout. Re-registering
the same name/profile is idempotent. Indexing an already completed source/name/space
returns its original vector without another model call. A second name is a separate
materialization; concurrent duplicate indexing still accounts for all compute attempts.

Retirement or supersession of a source excludes all of its named vectors from current
retrieval. The existing source-head generation fence still rejects a vector produced
while its source was changing. Name and space selection add no authority to a source.

## Artifact schema

Raw artifact metadata and content publication retain their existing contracts. A new
derived manifest exposes current named attachments:

```python
manifest = artifacts.embedding_manifest(context, artifact_id)
```

```bash
uv run --locked potbot embedding manifest \
  --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  --artifact-id ARTIFACT_UUID
```

The strict JSON contract is
[`artifact-embedding-manifest.schema.json`](../vocab/artifact-embedding-manifest.schema.json),
with a complete fixture in
[`example-artifact-embedding-manifest.json`](../vocab/example-artifact-embedding-manifest.json).
The runtime also supplies immutable `NamedEmbeddingReference` and
`ArtifactEmbeddingManifest` wire records with strict parsing and validation.

`named_embeddings` maps each name to a **list of references**. A list preserves separate
source occurrences and space generations even when their underlying artifact bytes
are identical. Each reference pins the vector, source, view and space, plus the source
hash, profile hash, exact encoding-input hash, input format, dimensions, metric,
profile generation and usage origin. Vector arrays are kept in PostgreSQL rather than
duplicated into ordinary artifact metadata.

Three hashes have different definitions:

| Hash | Input |
| --- | --- |
| `artifact_digest` | The immutable artifact's raw bytes. |
| `source_content_hash` | The existing canonical object containing source text and metadata. |
| `encoding_input_hash` | The exact prepared document text encoded as UTF-8. |

For the current `source_text_v1` format, the encoding-input hash equals the source
artifact digest. This equality follows from the explicit raw-text format; it must not
be assumed for future transformed input formats.

The manifest is a current association view. It retains the artifact identity while
including only active source heads visible in the authenticated workspace. Retired
and superseded source references disappear from this view; historical rows remain
in the database for audit. An artifact with no eligible embeddings has an empty map.
The manifest is not a promise of exhaustive semantic coverage or a permanently frozen
retrieval snapshot.

This runtime schema is an additional versioned contract. The published semantic
vocabulary and exact data-type schemas remain at release `0.1.0`; embedding names are
not data types, semantic predicates or retrieval-profile names. A retrieval profile
can select one or more registered names together with exact space IDs.

## Upgrade an existing installation

This is a **coordinated application and schema upgrade**. Stop old application writers
and workers, install this code and its locked dependencies, then use the existing
administrative migration command with the intended installation's explicit admin DSN:

```bash
uv sync --locked --extra postgres --extra embeddings --dev
uv run --locked potbot admin migrate
```

Including `--extra embeddings` retains the optional local encoder dependencies during
the exact dependency sync. A database/manifest-only installation can omit that extra.
Existing pinned model files can be reused.

`POTBOT_ADMIN_DSN` must already identify the database you intend to migrate. For a
non-default schema, use `potbot admin --schema SCHEMA_NAME migrate`. Restart application
workers with the updated code after the migration succeeds. No command here discovers
or connects to a production database implicitly.

Migration 5 creates the named-view table, backfills a `general_text` view for every
existing space, assigns existing vectors that default name, replaces vector uniqueness
and adds the binding foreign key and matching index. It preserves original vector IDs,
values, space/source hashes, artifact references and budget reservations. Existing
immutable triggers remain enabled. The migration framework applies the revision and
its row-security policies atomically; a repeated application is a no-op after checksum
verification.

Old binaries use an `ON CONFLICT` target that the new named uniqueness key replaces,
so simultaneous old/new writers are not supported. Historical migrations 1–4 are
unchanged. There is no automatic downgrade that discards named materializations.

## Verification and current support

The normal CI command exercises the migration and named persistence against real
PostgreSQL/pgvector, using explicitly synthetic vectors to test selection and identity:

```bash
uv run --locked python scripts/check.py --with-postgres
```

The new cases cover populated revision-4 upgrades, repeat migration, named-view grants
and immutability, name/space isolation, idempotent indexing, invalid names, source changes,
provider/profile changes during dispatch, CLI name selection, workspace isolation and
artifact-manifest provenance. Confirmed encoding usage is settled even when a changed
provider/profile causes the result to be rejected before publication or search.
The existing local model smoke
and opt-in live embedding integration test remain separate.

The supported encoder remains the pinned Qwen 1,024-dimensional L2/cosine profile.
Naming a view does not create a semantic transformation, train an encoder, change
dimensions or invoke another model. Two names over the same raw text and exact profile
can legitimately contain identical vector values. Procedure renderers, code-specific
models and custom semantic encodings need explicit future input formats and evaluation.
