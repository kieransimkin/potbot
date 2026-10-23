# SYN snapshots, exact type lookup, and named embeddings

Potbot can import a public SYN snapshot, resolve its exact references, enumerate
instances by declared type, and rank selected records through the existing local
embedding service. Every database operation selects an authenticated workspace and
an immutable snapshot. Import, resolution, and inventory work without loading an
encoder.

This implementation adds application migration **6**, a bundled native catalog, a
protected representation-type crosswalk, a standalone exporter, and the `potbot syn`
commands. The published semantic vocabulary remains at **0.1.0**. Its JSON contracts
and the architecture, implementation-plan, and semantic-design DOCX files retain
their original identities.

## How this fits the existing plans

| Existing plan | Connection to this implementation |
| --- | --- |
| [Architecture specification](Multi_Model_Coding_Agent_Architecture_Specification.docx), “Memory model and lifecycle” and “Retrieval and context assembly” | Imported assertions retain source provenance. Admission, applicability, verification, and permission decisions remain with their existing owners. Exact lookup and vector ranking have separate jobs. |
| [Implementation plan](Multi_Model_Coding_Agent_Implementation_Plan.docx), P085–P094 | Snapshot records provide immutable external source material. They do not introduce another accepted-memory lifecycle. |
| Implementation plan, P097, P099–P104 | Exact identifiers, faithful source representations, independent embedding spaces, scoped materialization, and filtered vector retrieval supply the integration points. |
| [Local embedding addendum](Local_Embedding_Addendum.docx), E01, E03, E04, E07–E08 | Encoding profiles stay explicit; original record text survives indexing; declared identities and relations support exact operations. |
| [Semantic vocabulary design](Agent_Semantic_Vocabulary_and_Semantics_v0_1.docx), sections 3, 10–13 and S01, S02, S07, S08, S10 | The adapter implements a bounded source-specific catalog and enumeration path. General parsed-value materialization, the seven complete retrieval profiles, condition evaluation, and learned semantic projections remain separate work. |

The original contracts are described in [the vocabulary README](../vocab/README.md).
The current dense encoder and its qualification boundary are documented in
[local embeddings](embeddings.md) and [named embeddings](named-embeddings.md).

## Verified SYN reference and source behavior

The inspected SYN implementation uses this exact local reference form:

```text
syn://{kind}/{slug}
kind: type, thing, event, ember, cog, credential, enigma, pondle, snug, om
slug: [a-z0-9]+(?:[-_][a-z0-9]+)*
```

The kind occupies the URL authority position. A workspace, host, database, snapshot,
or peer ID is not encoded in the URL. For example, `syn://thing/readme` can identify
different records in two SYN installations. Potbot preserves the exact string and
supplies the namespace and snapshot separately.

Parsing does not normalize titles, lowercase input, decode percent escapes, or repair
malformed references. `SYN://thing/readme`, `syn://thing/Readme`, and
`syn://thing/readme?revision=2` fail this local boundary. Slugs are limited to 255
characters, matching SYN's SQL slug domain and Type HTTP path. The upstream Python
`ObjectRef` constructor alone accepts longer slugs; its separate `ObjectRef.local()`
factory performs lossy title normalization and truncation. Potbot does not use that
factory to reconstruct imported identities. See the checked adapter in
[`domain/syn.py`](../src/potbot/domain/syn.py).

### Equivalent Type URL spellings

For Type references, Potbot accepts `syn://type/{slug}` and `synny://type/{slug}`
as two spellings of **one type identity** in the selected catalog. Both are exact,
case-sensitive syntaxes with the same 255-character slug limit. The preferred
spelling is `syn://` for native classes and `synny://` for Potbot's protected
`potbot-data-` classes:

| Preferred reference | Equivalent reference | Type identity |
| --- | --- | --- |
| `syn://type/person` | `synny://type/person` | Native person class. |
| `synny://type/potbot-data-file-agents-md` | `syn://type/potbot-data-file-agents-md` | Potbot's checked AGENTS.md file representation. |
| `synny://type/potbot-data-record` | `syn://type/potbot-data-record` | Potbot's structured-record representation. |

The prefix distinguishes Potbot's representation type `potbot-data-record` from the
native semantic class `record`; changing only the scheme cannot separate
two meanings. The combined catalog contains one definition per type identity.
Duplicate declarations or direct parents using both spellings are rejected, rather
than materialized twice. Instance identities remain `syn://{non-type-kind}/{slug}`;
`synny://thing/...` is unsupported.

`SynRef` and `SynnyRef` preserve their exact input spelling. `parse_ref()` dispatches
between them, `parse_type_ref()` restricts the result to a Type, and `type_identity()`
returns the common slug for comparison within an already selected catalog. The
catalog chooses the preferred spelling for effective definitions, parents, and
property ranges. Imported source references and public JSON bytes remain unchanged.
These aliases add neither global identity nor authority.

The inspected source has database-backed Type endpoints, including
`GET /api/type/{slug}`. Its nine other primitive controllers return placeholder
responses. Type search is capped at ten results. The P2P module implements transport,
discovery, and status, without a general object-reference resolution protocol.
Potbot's resolver therefore reads its imported snapshot; it does not discover a
global SYN network or resolve objects through libp2p.

The source audit covers SYN's `src/syn/refs.py`, `sqltypes.py`, `models.py`,
`controllers/type.py`, the primitive controllers, and `p2p.py`. Exact source paths,
versions, and recorded file digests for the catalog and core contracts are retained
in [`syn-typing-crosswalk.json`](../vocab/syn-typing-crosswalk.json), under
`provenance`. The original uploaded SYN project was an inspected input. Potbot now
also pins Syn at `vendor/syn` and provides an optional lightweight runtime client
for its communication/receipt service. [Runtime setup](syn-service.md) is separate
from snapshot resolution; ontology records still resolve through the selected
local snapshot rather than a global libp2p resolver.

## Four different kinds of identity

| Axis | Example | What it establishes |
| --- | --- | --- |
| SYN storage kind | `thing` in `syn://thing/alice` | Which primitive record family holds the public record. |
| Declared semantic class | `syn://type/person`, equivalently `synny://type/person`, in the record's `typeRef` | The source's declared class of the represented referent. |
| Potbot representation type | `synny://type/potbot-data-syn-object-record`, with compatibility ID `dt:syn.object_record` | The checked form of the imported record itself. |
| Embedding name and space | `general_text` plus an exact space UUID | Which registered vector representation and encoder profile were used. |

An imported instance has one declared `typeRef` and the derived primary representation
type `dt:syn.object_record`. A Type definition has the derived primary representation
type `dt:syn.type_definition`; it is not an instance of the class that it defines.
These representation assignments come from the adapter, rather than a caller's
assertion inside public JSON.

The `dt:*` values remain supported compatibility IDs for the existing data registry.
Resolved output exposes `primary_data_type` as that compatibility ID and
`primary_data_type_ref` as its preferred Type URL. A source may assert a protected
type in `typeRef`, under either scheme, but that assertion alone does not turn its
public-record wrapper into a checked agent definition, file, or workflow.

The crosswalk deliberately preserves these distinctions. SYN's native `artifact`
class describes a material artifact; Potbot's content-addressed artifact holds bytes.
A `cog` describes a computational process; an agent definition describes a reusable
configuration or role. A workflow definition and a workflow execution have separate
identities. Native class assignment alone does not establish a verified outcome,
an agent deployment, a permission, or an accepted memory.

### Native catalog and protected representation types

The bundled native catalog contains **529 classes**, reconciled against the supplied
PostgreSQL **16.15** database dump. Its **859 direct parent edges** are retained,
including multiple parents and redundant transitive edges. The dump contains **zero
direct TypeProperty declarations**; the adapter can import declarations from later
snapshots.

The original Python seeds define 450 classes: 369 from `syn-core` version 4 and 81
from `syn-feeds` version 1. All 450 references are present in the database catalog;
the additional **79 classes are generated feed-intersection types**. They do not add
new primitive kinds. The database's parent set is authoritative on overlaps. For
`computational-procedure`, it removes one redundant edge present in the Python seed;
the original names and definitions otherwise agree. This preserves the supplied
database graph rather than silently reconstructing it from seed code.

[`syn-database-types.snapshot.json`](../vocab/syn-database-types.snapshot.json) contains
the complete extracted 529-type registry with `objects: []`. The source dump was
parsed as data, without executing its SQL or importing instance/credential rows into
this package. Exact dump provenance is recorded in
[`syn-database-types.provenance.json`](../vocab/syn-database-types.provenance.json),
and the crosswalk retains the original seed provenance and reconciliation. These
counts describe the supplied snapshot; a running database can
subsequently add or change classes.

All **45 existing definitions** in
[`data-types.json`](../vocab/data-types.json) retain their IDs, definitions, declared
parents, classifier policies, and agent/workflow distinctions. Each has an explicit
Type mapping with preferred references under `synny://type/potbot-data-...`. Two
additional public-record definitions bring the protected extension catalog to
**47 types**, for **576 logical definitions** in the combined bundled catalog.
Supporting both schemes does not double these counts:

| Compatibility ID | Preferred Type reference |
| --- | --- |
| `dt:file.agents_md` | `synny://type/potbot-data-file-agents-md` |
| `dt:document.json` | `synny://type/potbot-data-document-json` |
| `dt:value.integer` | `synny://type/potbot-data-value-integer` |
| `dt:agent.definition.coding` | `synny://type/potbot-data-agent-definition-coding` |
| `dt:workflow.definition.verification` | `synny://type/potbot-data-workflow-definition-verification` |
| `dt:syn.object_record` | `synny://type/potbot-data-syn-object-record` |
| `dt:syn.type_definition` | `synny://type/potbot-data-syn-type-definition` |

Each preferred reference has an equivalent `syn://type/` spelling with exactly the
same slug. Use `data_type_ref()` or the complete crosswalk to map compatibility IDs;
those mappings are registered explicitly. The representation root has
no native semantic superclass. This keeps a record about a person distinct from the
person it describes.

The preserved agent branch includes generic definitions, coordination, planning,
research, coding, review, verification assistance, memory curation, and composite
definitions, plus separate deployments and runs. The workflow branch includes
generic definitions, coding, research, verification, memory curation, evaluation,
deployment, and composite definitions, plus separate runs. Composite roles and
workflow kinds remain declared facets. Registering their aliases does not instantiate
those definitions or materialize checked agent/workflow occurrences from arbitrary
SYN payloads.

[`semantics/catalog.py`](../src/potbot/semantics/catalog.py) implements two explicit
catalog modes:

- **`complete`** uses the supplied native definitions and the 47 protected types.
  Missing seed classes are not silently reintroduced. The database exporter uses
  this mode so local edits and custom classes survive.
- **`extend_builtin`** starts from the 529 bundled native classes. A supplied matching native
  definition replaces that entire definition, including its parents and properties.
  The protected types are then added.

Both modes reject source definitions with the reserved `potbot-data-` slug prefix
under either scheme. Other source definitions may use either supported Type spelling;
merge normalizes their effective catalog references. The merged graph must have
unique type identities, existing parents and property range types, and no cycles.
Display names may repeat; the scoped type identity controls lookup. Ancestry
includes the type itself and each reachable ancestor once, even when several paths
reach the same ancestor.

### TypeProperty declarations

`SynType.properties` preserves the direct `TypeProperty` declarations on that type:
name, range `type_ref`, and explicit `minimum` and `maximum` cardinalities. The default
is `1..1`; `0..n` is supported; both bounds are finite nonnegative signed-bigint
values with `maximum >= minimum`.

There is no inherited-property merge, override-resolution policy, or general
instance-value validator in this adapter. A property declaration describes the
source catalog. Its presence does not prove that an imported record has all the
required values. This policy is recorded in the crosswalk and the
[`SynProperty` contract](../src/potbot/domain/syn.py).

## Export a running SYN database without changing it

The extracted database snapshot already included in this repository can be imported
directly using the commands below. To reproduce its type-only extraction from the
separately retained SQL file, use the standard-library extractor:

```bash
python scripts/extract_syn_types_dump.py \
  --dump /path/to/syn.sql \
  --output vocab/syn-database-types.snapshot.json \
  --provenance vocab/syn-database-types.provenance.json \
  --source-label syn-database-types \
  --expected-sha256 2f4d949fb5b2996ee88d2dbcbbbe6996cdd7213d1c4aef50b26fad4857a1ab3e
```

[`extract_syn_types_dump.py`](../scripts/extract_syn_types_dump.py) verifies the
selected dump and reads only the `types`, `typeParents`, and `typeProperties` COPY
sections. Other COPY contents are skipped without decoding. It does not connect to
PostgreSQL or execute any SQL. The original dump and its instance/credential rows
remain outside the repository and delivery package.

For a fresh snapshot of a running database, run
[`scripts/export_syn_snapshot.py`](../scripts/export_syn_snapshot.py) using the
existing SYN Python environment, where SQLAlchemy and its PostgreSQL driver are
already installed. The script imports neither the SYN application nor Potbot. It
reads the explicitly selected database, without starting a server or invoking
migrations.

For registry bootstrap, use the **types-only** mode from that environment:

```bash
uv run --no-sync python /path/to/potbot/scripts/export_syn_snapshot.py \
  --config /path/to/syn.toml \
  --schema public \
  --source-label local-syn-types --types-only \
  --output /path/to/syn-types.json
```

This reads only `types`, `typeParents`, and `typeProperties`. The result declares
`catalog_mode: "complete"` and `objects: []`, so it captures the database's visible
native registry, including custom classes, without querying instance tables. After
import, Type definitions resolve and can be indexed; no instance objects are present
in that snapshot.

Omit `--types-only` to export the public instance records as well. That mode requires
the complete snapshot to fit the object and byte limits below. Large seeded
installations can exceed the 100,000-object cap. Full-graph sharding, live federation,
and incremental object collection are future work; the exporter fails explicitly
when a full snapshot exceeds a bound.

Alternatively, use `--database-url-env SYN_EXPORT_DSN` to name an existing environment
variable. The TOML option reads `database.url` only. Use an existing account with read
access to the required source tables. The source schema must already contain the
current TypeProperty and Credential structures; export fails if required tables or
columns are unavailable.

The exporter opens a **read-only, repeatable-read transaction**, selects public
columns explicitly, and writes the completed file atomically. Its source label is a
nonsecret description of 1–200 ASCII letters, digits, spaces, dots, underscores, or
dashes, beginning with a letter or digit. The broader import schema accepts nonblank
labels up to 2,000 characters. Neither should contain connection strings or secrets.

| Export coverage | Treatment |
| --- | --- |
| Type definitions, including custom database classes | Included with public metadata. |
| Direct `typeParents` edges | Included and checked as a DAG. |
| Direct `typeProperties` declarations | Included with explicit cardinalities. |
| Nine instance primitive tables | Included in full mode, as visible to the configured database role; not read in types-only mode. |
| Credential `publicKeyHash` | Included in full mode when present, using a tagged base64 representation. |
| Credential `privateData` | Never selected. Embedded `privateData` / `private_data` keys in public JSON cause failure. |
| Ordinary object relationships, event-target links, chronicles, jobs | Omitted by this exporter. |

Ordinary relationship edges are not treated as subclass edges. An exported snapshot
can be complete for the selected public-record projection while omitting those
relationship tables. Source-role visibility also bounds coverage; the exporter does
not claim access to hidden records or another database.

PostgreSQL serializes JSONB public columns to text before the driver receives them,
preserving persisted numeric values without a floating-point conversion. Whitespace
or original number spellings already discarded when JSONB was first stored cannot
be recovered. After export, each `payload_json` and `metadata_json` string is retained
exactly by Potbot, including its numeric lexemes and whitespace.

`publicKeyHash` is opaque public data. It does not prove key possession or ownership.
This integration provides no decryption, signing, credential authorization, or private
key service. SYN's `enigma` primitive represents a question.

## Install, migrate, and import

Use the provisioning steps in [Phase 2](phase-2.md) to create the application schema,
workspace, restricted principal, and repository. Upgrade the application and apply
migration 6 with the intended installation's explicitly configured administrative
connection:

```bash
uv sync --locked --extra postgres --dev
uv run --locked potbot admin --schema potbot migrate
```

Include `--extra embeddings` when retaining the optional encoder dependencies. Keep
existing application writers on the same application/schema revision during the
upgrade. `POTBOT_ADMIN_DSN` is used for administration; set **`POTBOT_DSN` to the
restricted application login** for the commands below.

The bundled catalog is available offline:

```bash
uv run --locked potbot syn catalog --summary
uv run --locked potbot syn catalog --type-ref dt:agent.definition.coding
```

Omit `--summary` to return every bundled definition. `--type-ref` selects one exact
native reference or registered data-type alias.

Import the extracted database registry directly:

```bash
uv run --locked potbot syn import \
  --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  --repository-id REPOSITORY_UUID --namespace syn-registry \
  --file vocab/syn-database-types.snapshot.json \
  --artifact-root artifacts/objects --schema potbot
```

Retain its returned snapshot UUID as `REGISTRY_SNAPSHOT_UUID` for the catalog command
below. This import makes the supplied native Type definitions available; its instance
inventory is explicitly empty.

To try object resolution and named vectors, import the separate synthetic fixture:

```bash
uv run --locked potbot syn import \
  --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  --repository-id REPOSITORY_UUID --namespace syn-demo \
  --file vocab/example-syn-snapshot.json \
  --artifact-root artifacts/objects --schema potbot
```

Retain this second UUID as `DEMO_SNAPSHOT_UUID`. The `readme` and `example-document`
commands below use that fixture. For later real data, select a new exported file and
its intended namespace and use the returned snapshot UUID explicitly.

A namespace is an immutable workspace-local name bound to one Potbot
repository. Reusing it with another repository fails. Reimporting identical effective
content in the same namespace returns the existing snapshot.

Import validates the full input and merged catalog before publication. All snapshot
membership, source references, ancestry rows, and the publication seal commit in one
transaction. Migration 6 prevents membership changes after sealing and checks
published counts, parent edges, and the exact ancestry closure. All new tables use
the existing workspace row-security boundary. See
[`syn_schema.py`](../src/potbot/persistence/syn_schema.py) and
[`SynService.import_snapshot`](../src/potbot/semantics/service.py).

Artifact bytes use the existing immutable backend. A rolled-back import can leave
unreferenced bytes, while no partially published snapshot becomes visible through
the service. Import does not create vectors or load model weights.

### Hashes and the stored envelope

| Identity | Exact material |
| --- | --- |
| `source_hash` | SHA-256 of the canonical supplied `SynSnapshot` dictionary. |
| `catalog_hash` | SHA-256 of the effective definitions, protected extensions, and recorded catalog provenance. |
| Snapshot `content_hash` | SHA-256 of the canonical effective envelope containing the supplied snapshot and the full merged catalog with provenance. This equals the snapshot artifact digest. |
| Individual record artifact digest | SHA-256 of the exact public payload UTF-8 bytes; Type records use their canonical `SynType` JSON. |

The canonical JSON serializer sorts keys, uses compact separators, preserves Unicode,
and emits UTF-8. Input-file indentation and envelope key order do not define snapshot
identity. Text inside `payload_json` and `metadata_json` remains byte-significant:
changing its whitespace or a number spelling changes the corresponding source and
artifact identity. This is a content/provenance fingerprint, separate from the
semantic-design document's exact-meaning normalization rules.

Changing a supplied Type URL from `syn://` to its `synny://` alias changes that source
spelling and can therefore change the source/snapshot fingerprint. It does not add
a new logical type. The effective catalog normalizes both spellings, and alternate
resolution requests within the same snapshot select the same stored Type source,
artifact, and vector references.

The stored effective envelope pins the catalog used by that snapshot. Replacing a
bundled seed in a future release cannot silently reinterpret an existing import.
Embedding source keys contain the namespace UUID, snapshot UUID, and exact reference;
source metadata records the exact reference and effective snapshot hash. Identical
artifact bytes can be shared without merging these distinct source occurrences.

### Explicit limits

| Boundary | Limit |
| --- | --- |
| Input envelope, canonical input envelope, and effective stored envelope | Each at most 64 MiB, or 67,108,864 UTF-8 bytes. |
| Each public payload or metadata string | 2,000,000 UTF-8 bytes. |
| Supplied Type records | 10,000. |
| Effective catalog after adding protected representation types | 10,000 total types. |
| Instance objects | 100,000. |
| Materialized ancestry | 1,000,000 unique descendant/ancestor pairs. |
| JSON container nesting | 128 levels, checked before decoding. |
| Inventory page size | 1–1,000 objects. |

The effective envelope includes the merged definitions, so an input file near the
size or type-count limit can still fail after merge. The importer and exporter fail
explicitly at limits; they do not truncate the catalog or drop records to succeed.
The limits are operational bounds, not measured claims about the size of a live SYN
installation.

The additive runtime wire contracts are:

| Contract | Schema ID |
| --- | --- |
| [Import snapshot](../vocab/syn-snapshot.schema.json) | `urn:potbot:schema:syn-snapshot:1` |
| [Effective stored envelope](../vocab/syn-effective-snapshot.schema.json) | `urn:potbot:schema:syn-effective-snapshot:1` |
| [Resolved record and scoped vector references](../vocab/syn-resolved-record.schema.json) | `urn:potbot:schema:syn-resolved-record:1` |

[`example-syn-snapshot.json`](../vocab/example-syn-snapshot.json) is a complete
synthetic input fixture. Runtime validation additionally checks duplicate keys and references,
including duplicate Type aliases, payload identity, recursive private fields, exact
byte limits, and graph consistency. A public payload's `id` must match its object
reference exactly. Its `typeRef` and the outer `type_ref` must identify the same type;
their scheme spellings may differ, and both are preserved.
JSON Schema's `contentSchema` is descriptive; it does not replace those checks.

## Resolve and enumerate exact snapshot records

The database commands share `--workspace-id`, `--principal-id`, `--schema`, and
`--artifact-root`. Every operation after import requires `--snapshot-id`; there is
no implicit latest snapshot.

Inspect the complete effective type catalog pinned by an import:

```bash
uv run --locked potbot syn catalog \
  --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  --snapshot-id REGISTRY_SNAPSHOT_UUID \
  --artifact-root artifacts/objects --schema potbot
```

With `--snapshot-id`, `catalog` reads the authenticated database snapshot instead of
the bundled catalog. Add `--summary` for counts/provenance or `--type-ref` for one
definition. This supplies exact access to custom database classes imported through
the types-only exporter. Either Type URL spelling selects the same definition;
registered `dt:*` compatibility IDs also work for representation-type catalog lookup.

Resolve a fixture object or its class definition:

```bash
uv run --locked potbot syn resolve \
  --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  --snapshot-id DEMO_SNAPSHOT_UUID --ref syn://thing/readme \
  --artifact-root artifacts/objects --schema potbot

uv run --locked potbot syn resolve \
  --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  --snapshot-id DEMO_SNAPSHOT_UUID --ref syn://type/example-document \
  --artifact-root artifacts/objects --schema potbot
```

Resolution checks the stored source and artifact bytes and returns their provenance.
The response includes the caller's exact `requested_ref` and the canonical stored
`ref`. For a Type, switching between equivalent schemes changes `requested_ref` while
selecting the same record. Object references remain exact native SYN identities.
Named vector references are restricted to that exact source occurrence, even when
the same artifact bytes appear in another namespace or snapshot. An unavailable
reference fails without creating a SYN placeholder or a new source record.

Enumerate all objects whose direct declared assignment is `example-document`:

```bash
uv run --locked potbot syn list \
  --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  --snapshot-id DEMO_SNAPSHOT_UUID --type-ref syn://type/example-document \
  --page-size 100 --artifact-root artifacts/objects --schema potbot
```

Add `--include-subtypes` to include assignments to descendants in that snapshot's
declared graph. Exact equality is the default. When `next_cursor` is returned, repeat
the same command and filters with `--cursor CURSOR_UUID` until it is null. The opaque
cursor is bound to the requesting principal, snapshot, query, and page size. The
service orders references with PostgreSQL `COLLATE "C"`; duplicate ancestry paths
do not duplicate objects.

Type equality uses the common identity, so `--type-ref synny://type/example-document`
selects the same objects as the native spelling above. Alias spelling does not add
objects, create a second inventory, or bypass the selected snapshot's graph.

`--data-type` filters the derived representation type independently of the declared
class. For this instance inventory, `--data-type dt:syn.object_record` selects public
object records; `--data-type dt:record --include-subtypes` selects the same records
through the protected representation hierarchy. Semantic-class and data filters can be
combined, and both must match.

`list` enumerates instance objects only. `--data-type dt:syn.type_definition` therefore
selects no objects. Type definitions are enumerated with `catalog --snapshot-id`,
resolve by exact reference, and can be ranked using `search --record-kind types`.
Their full definitions also remain in the effective snapshot artifact. An
agent-definition alias does not turn an imported Cog into a checked agent definition,
so `--data-type dt:agent.definition` does not
infer such occurrences from native assertions.

The result's coverage is **`imported_snapshot_only`**. A types-only import has a
complete supplied type catalog and an explicitly empty instance inventory.
Exhaustive inventory means
every matching object in that selected imported snapshot, within current access
scope. It does not establish completeness of an entire SYN network, omitted export
tables, unparsed descendants, or the running source database after export. A newer
import leaves an older snapshot's page sequence unchanged. Current workspace access
is checked on each request.

## Index and rank within that scope

Indexing and search use the existing pinned local encoder, registered embedding
name and exact space UUID, running repository task, and budget ledger. Follow
[the local-provider setup](embeddings.md) first. Register the intended name/profile
and retain its returned space UUID:

```bash
uv run --locked potbot embedding register-space \
  --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  --embedding-name general_text \
  --model-dir models/qwen3-embedding-0.6b --max-input-tokens 2048 \
  --artifact-root artifacts/objects --schema potbot

uv run --locked potbot syn index \
  --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  --snapshot-id DEMO_SNAPSHOT_UUID --ref syn://thing/readme \
  --episode-id TASK_UUID --space-id SPACE_UUID --embedding-name general_text \
  --runtime-budget-ms 60000 \
  --model-dir models/qwen3-embedding-0.6b --max-input-tokens 2048 \
  --artifact-root artifacts/objects --schema potbot

uv run --locked potbot syn search \
  --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  --snapshot-id DEMO_SNAPSHOT_UUID --query "project documentation" \
  --type-ref syn://type/example-document --record-kind objects \
  --episode-id TASK_UUID --space-id SPACE_UUID --embedding-name general_text \
  --runtime-budget-ms 60000 \
  --model-dir models/qwen3-embedding-0.6b --max-input-tokens 2048 \
  --artifact-root artifacts/objects --schema potbot
```

Use identical profile options for registration, indexing, and querying. To rank Type
definitions, index either accepted Type URL spelling and search with
`--record-kind types`, omitting `--type-ref`, `--data-type`, and `--include-subtypes`.
Those filters apply to object search. Use `catalog --snapshot-id --type-ref` for an
exact Type-definition lookup.

Object embedding input is the exact `payload_json` text. Type embedding input is the
canonical Type record. The currently supported `source_text_v1` format and name such
as `general_text` identify this faithful text view. An additional name does not
generate a custom semantic projection or choose another model automatically.

Snapshot and type filtering select candidate source IDs **before vector ranking**.
The embedding service then applies current source eligibility and the selected
name/space binding. Search returns a bounded relevance result, so unindexed records,
retired sources, and lower-ranked matches need not appear. Source retirement changes
vector visibility while the immutable snapshot inventory remains available under
its own access rules. Use `list` for an exhaustive type inventory.

This initial path uses exact pgvector search. It does not implement global SYN
resolution, ordinary-relationship expansion, full-text/vector rank fusion, automatic
agent execution, general parsed-data classification, or a trained semantic encoder.
Those capabilities retain their separate implementation and evaluation boundaries.

## Verification and implementation map

The normal test entry point remains:

```bash
uv run --locked python scripts/check.py --with-postgres
```

It requires the explicitly configured disposable PostgreSQL fixture documented in
the project README. Encoder qualification and live SYN-database export are separate
operational checks; deterministic service tests do not establish retrieval quality
or prove that a particular user's source database has been exported.

| Component | Contract and tests |
| --- | --- |
| Reference parsing and immutable wire records | [`domain/syn.py`](../src/potbot/domain/syn.py), [`test_syn.py`](../tests/unit/test_syn.py) |
| Bundled catalog, aliases, merge, and ancestry | [`semantics/catalog.py`](../src/potbot/semantics/catalog.py), [`catalog.json`](../src/potbot/semantics/catalog.json), [`test_syn_catalog.py`](../tests/unit/test_syn_catalog.py) |
| Read-only public export | [`export_syn_snapshot.py`](../scripts/export_syn_snapshot.py), [`test_syn_export.py`](../tests/unit/test_syn_export.py) |
| Static supplied-dump type extraction | [`extract_syn_types_dump.py`](../scripts/extract_syn_types_dump.py), [`syn-database-types.provenance.json`](../vocab/syn-database-types.provenance.json) |
| Append-only publication and scoped SQL | [`syn_schema.py`](../src/potbot/persistence/syn_schema.py), [`test_syn_migration.py`](../tests/integration/test_syn_migration.py) |
| Resolution, inventory, and embedding selection | [`semantics/service.py`](../src/potbot/semantics/service.py), [`test_syn_service.py`](../tests/integration/test_syn_service.py) |
| Public command dispatch | [`semantics/cli.py`](../src/potbot/semantics/cli.py), [`test_syn_cli.py`](../tests/integration/test_syn_cli.py) |
| Source vector identity and artifact associations | [Named-embedding guide](named-embeddings.md), [`artifact-embedding-manifest.schema.json`](../vocab/artifact-embedding-manifest.schema.json) |

The historical [`validation-report.json`](../vocab/validation-report.json) continues
to describe the 0.1.0 design package. Current runtime verification belongs to the
implementation tests and CI results.
