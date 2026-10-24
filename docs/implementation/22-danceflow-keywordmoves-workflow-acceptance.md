# Stage 22: DanceFlow / KeywordMoves real workflow acceptance

This is an **integration and capability-evaluation increment** for Potbot. It
orchestrates canonical KeywordMoves operations, but does **not** reimplement the
KeywordMoves plugin/monitoring ecosystem or qualify public-site data collection.

## Exact upstream sources

- DanceFlow tool guide: `kieransimkin/DanceFlow` at `8963a2f270ec5264ab78974ec22a77258e1e2161`.
- KeywordMoves engine: `kieransimkin/keywordmoves` at
  `9044ba40460af6e6165f6b2c49a0d2454704fdd7`.
- The CI workflow checks out both sources by SHA, checks their Git identities,
  and uses the actual KeywordMoves `PluginRegistry`, `PluginRequest` and
  `PluginResult` interfaces. Source checkouts are separate from the Potbot wheel.
- The pinned external code runs **only in the dedicated test job**, not in
  Potbot's untrusted model-driven controller. Running the manual demonstration
  requires `--trust-local-source`, which explicitly runs local external source
  code in the current process; it is **not** a qualified sandbox.

## Atomic workflow demonstration

`examples/keywordmoves-workflow.json` is the reference DAG. Its steps are:

1. Literal Unicode term extraction from supplied lyrics, using the real
   `text-library/extract-literal` module (no model inference).
2. A reviewed/synthetic Google Trends CSV import using
   `google-trends/import-interest`. Indices remain `index_0_100`; they are not
   converted to search counts or volume estimates.
3. Explicit `keywordmoves-observations/v1` import. The source-specific metric,
   geography, platform, date, availability and limitations survive.
4. `research-batch/validate-plan`, using an actual bounded KeywordMoves plan.
   Validation does **not** execute or collect the plan's external requests.
5. Potbot `merge-candidates`: exact normalized phrases are deduplicated;
   source-specific evidence is concatenated without converting metric units or
   averaging scores. Each original score/metadata record retains a source hash.
6. Real `candidate-focus/related` for literal phrase fit. It neither judges
   user intent nor promotes a search-demand claim.
7. Potbot `bind-target`: attaches an explicitly supplied campaign subject and
   platform to the candidate pool with the exact upstream hash.
8. **Pause for editorial review** if `review.json` is absent. A human/analyst
   must independently inspect the pool and provide the exact
   `keywordmoves-shortlist-review/v1` schema including the pool SHA-256.
9. Real `candidate-shortlist/select` after resume. Prior completed stages are
   checked against their receipts and are never re-executed. Final suggestions
   are **not** publication permissions or automatically verified suitability.

Every executed stage writes an immutable-identity JSON result and a digest-bound
receipt. A possible execution is recorded before dispatch; crashes and uncertain
calls cannot be automatically replayed. The output directory and input root
must be selected independently. Missing editorial input pauses cleanly without
manufacturing a review. Changed source files or altered output hashes prevent
resume. Completed receipts can be exposed through R4's `EvidenceFinding`
contract, with source and receipt digests and no raw private prompt disclosure.

## Run the offline example manually

First obtain the KeywordMoves checkout at the exact pinned SHA and install Potbot
and its optional dependencies in your normal operator environment. Then:

```bash
python scripts/run_keywordmoves_workflow.py \
  --keywordmoves-checkout /path/to/keywordmoves \
  --plan examples/keywordmoves-workflow.json \
  --inputs examples/keywordmoves \
  --output .potbot/keywordmoves-demo \
  --trust-local-source
```

The run intentionally returns status `awaiting_input` because
`examples/keywordmoves/review.json` does not exist. Read the candidate pool in
`.potbot/keywordmoves-demo/results/pool.json`, perform a real editorial review,
and create a review file with `subject="Example Song"`,
`platform="YouTube"`, `source_sha256=SHA256(the exact pool bytes)`, and a
`decisions` array following the KeywordMoves review contract. Then rerun with
`--resume`. This does not implicitly send any prompt to OpenAI/Gemini.

For a wholly synthetic example of constructing a review, see the pinned
KeywordMoves `examples/candidate_shortlist.py`; its automated fixture review is
**not** an approval of real content.

## Coverage and boundaries

| Area | Executable scope | Additional qualification |
| --- | --- | --- |
| Literal/lexical keyword discovery | Real offline plugin exercised in CI | NLP model downloads and language packs are separate |
| Trends, native observations and research plans | Real offline imports and plan validation in CI | Source authenticity, query scope and rights remain external |
| Multi-source candidate composition | Real Potbot merge with distinct evidence lineage | No cross-source normalization or global demand score |
| Campaign fit and editorial shortlist | Real KeywordMoves filters and hash-bound review | Actual human approval for real deployments |
| Other registered KeywordMoves plugins | Generic typed DAG binding and capability inventory | Each plugin/operation needs separate tools, credentials and tests |
| Monitoring/watchlist/alert store | Separate KeywordMoves CLI contract, not yet orchestrated by this DAG | Scoped SQLite store, native coverage and historical tests |
| YouTube/Google/Bing/Reddit/social live collectors | Host-policy gated; disabled by default | Explicit scope, disclosure, consent, cost ceilings, live receipts |
| Native post editing and publishing | No publication/persistence action granted | Review, provenance, publish permission and exact native readback |
| Potbot autonomous tool execution | R4 evidence receipts from completed workflow stages only | Qualified Linux sandbox/Cog registration required for live use |

The generic runner can express any trusted installed plugin operation, but that
is **not** equivalent to having installed, tested, or permissioned every source.
Model-generated workflow manifests cannot grant themselves paid/public-site
capabilities: a trusted host must explicitly bind those permissions. A key in
an input manifest is rejected. The offline set is conservative; `keybert`
model download is not silently authorised.

## Acceptance gates

- `python -m pytest -q tests/unit/test_keywordmoves_workflows.py`
  checks bounded DAG ordering, path/symlink isolation, secret exclusion, review
  pause/resume, digest validation, no replay and host-policy rejection.
- The [dedicated pinned-source GitHub Actions job](../../.github/workflows/keywordmoves-acceptance.yml)
  checks out the two exact external repos and executes real KeywordMoves module
  operations using an entirely synthetic, credential-free corpus and reviewed
  example data. Source version drift fails closed.
- The real test asserts distinct evidence kinds, preserved metric units,
  grouped source lineage, a hash-bound review and successful resumption.
- The CI job uploads a sanitized synthetic receipt and complete discovered plugin-operation matrix under `keywordmoves-offline-acceptance`.
- No live access, paid request, publication, model download or undocumented
  platform scraping occurs in the acceptance suite.

Future work should add the monitoring CLI/store as a separate admitted stage,
then broader source-specific offline fixture packs, genuine sandbox mediation,
and the full operator-controlled authorization/availability matrix. Those
capabilities are intentionally not declared complete here.
