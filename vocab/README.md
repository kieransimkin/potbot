# Agent Semantic Vocabulary 0.1.0

This is the machine-readable design companion to Agent_Semantic_Vocabulary_and_Semantics_v0_1.docx. It defines a bounded first version; it is not an implemented agent, evaluator or migration.

## Files

- vocabulary.json: 25 entity types, 16 directed predicates, controlled concepts/enums, guard fields/operators and limits.
- semantic-bundle.schema.json: JSON Schema 2020-12 for ground semantic frames and their evidence bindings. Procedure templates retain the existing architecture's schema; their instantiated ground views use these frames.
- feature-encoding.json: inspectable feature families, dictionary rules and an initial ranking profile.
- retrieval-profiles.json and retrieval-profiles.schema.json: seven question contracts, candidate channels, decision gates and failure semantics, including exhaustive type inventory.
- retrieval-request.schema.json and example-retrieval-request.json: a question-owned request with explicit profile selection and checked statement/entity references.
- data-types.json: primary data types, explicit subtype relationships, agent/workflow families, parser policies and exact enumeration rules.
- typed-data-item.schema.json and type-query.schema.json: checked item occurrence and deterministic type-query shapes.
- example-typed-items.json and example-type-queries.json: file, parsed-value, agent/workflow and enumeration fixtures.
- type-conformance-cases.json: expected type classification, snapshot, coverage and pagination behavior.
- example-bundle.json and fixture-entities.json: illustrative values only, not claims about a real repository.
- invalid-schema-examples.json: examples that must fail shape validation.
- semantic-validation-rules.json: service-level obligations that JSON Schema does not establish.
- conformance-cases.json: expected semantic/runtime behavior to implement and test.
- validation-report.json: actual checks performed on this design package.

## Validation boundary

Validate JSON shape first, then vocabulary/type rules, then checked database references, scope, evidence, time and authority through the existing services. A schema-valid frame cannot promote memory, claim a test passed, grant a capability or execute an action. The workspace and producer identity are supplied by the authenticated enclosing command and existing immutable revision/event records.

The design keeps memory_items, memory_revisions, memory_evidence, lifecycle, applicability and capability ownership from the existing plan. New semantic frames/features are checked views and indexes over those records. New extracted meaning follows the normal proposal/admission path. Questions retain need_revisions ownership through separate need_query_views; query embeddings key the retrieval run, need revision, exact encoding-input hash and compatible space generation. Query unknowns are not wildcards.

## Numeric and missing-value conventions

Integers and decimals use canonical strings; zero is "0" and negative zero is rejected. Versions, digests and timestamps are typed separately. UTC timestamp canonicalization and pinned version parsing are service requirements. Unknown is explicit and is neither null, absent, false nor a query wildcard.

## Hashes and exact matching

Apply declared safe guard normalization, then RFC 8785 canonical JSON and SHA-256. A proposition digest includes vocabulary release and its signed typed atom. A statement digest adds mode, force, when, scope, applicability and valid-time qualifiers. Evidence, producer and lifecycle do not enter meaning identity. Retain canonical bytes. Partial/unknown frames remain ineligible for a confirmed exact-meaning result even if their serialized bytes match.

## Integration

See the document's S01-S12 plan mapping, and the P references in conformance-cases.json. Guards preserve applicable / inapplicable / unknown_requires_check. Four support-state labels are diagnostics, not a replacement applicability engine. Learned projections remain optional under P149-P150 and do not train current facts/preferences into weights automatically.

## Deterministic types

Each catalogued occurrence has one registered primary type within its source revision and classifier generation. Type identity is different from item identity. Exact type equality is the default; subtype inclusion follows the published graph. Files, decoded/parsed representations and their contained values are linked items, not interchangeable types. Agent/workflow definitions, deployments and runs remain separate typed objects with checked relationships.

Type inventory bypasses relevance ranking and revision-view deduplication. It preserves every matching occurrence in a frozen ordered enumeration manifest. Source ingestion, classification, nested itemisation, exact selection and delivery completeness are reported separately. Missing parsing or descendant coverage prevents an unqualified all-examples claim. Current access/deletion checks govern each page; changed eligibility invalidates continuation.

## Schema resolution

All schemas use JSON Schema Draft 2020-12. Register their exact $id values with the validator: retrieval-request.schema.json references urn:agent-sem:schema:type-query:0.1.0. Shape-valid illustrative records do not prove live ownership, authority, parser coverage or database conformance. The validation report lists actual static checks and planned runtime work separately.
## Runtime named-embedding schema

`artifact-embedding-manifest.schema.json` adds the versioned runtime contract
`urn:potbot:schema:artifact-embedding-manifest:1`, with a complete example in
`example-artifact-embedding-manifest.json`. Its `named_embeddings` map contains
source-qualified references grouped by representation name. Every reference pins an
immutable view and encoding space; names do not replace exact data types or semantic
predicates. Lists preserve multiple source occurrences and space generations.

This additive runtime contract does not revise the published `0.1.0` vocabulary or
its existing schemas. The original `validation-report.json` remains the historical
design-package validation result. Current implementation tests are run by Potbot's CI.
See [named embeddings](../docs/named-embeddings.md) for the migration and API contract.
