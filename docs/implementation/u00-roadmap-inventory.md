# U00 source inventory and reviewed reconciliation

**U00-A completed, 2 November 2026. Reviewed product main:
`95610037311c80cdf7163372998b08e1b8958926`.** See the
[complete closeout and next U01-A milestone](u00-a-closeout.md),
[authored requirement review](roadmap-review.json) and
[generated source inventory](roadmap-inventory.json).

![Retained documents and GitHub evidence lead to reviewed audit closure while runtime gates remain separate](media/roadmap-inventory.svg)

*Generated explanation, not a capture of runtime behavior. This audit changes no
runtime interface; a new screenshot/video would add no deployment evidence.*

## Sources and precedence

The first increment reviewed `63a7ba08d4c3a4fe9530add78dbcd371ea6e97b2`, whose
185 documentation assets did not include all the subsequent delivery guides.
The current inventory discovers every tracked/unignored document, including new
unstaged plans; its counts and hashes are reproducible. It includes all five Word
documents, text/structured plans, historical verification records and media.
Ignored private evidence and generated dependency storage are excluded.

Word sources are bounded OOXML archives. Document paragraphs, including table
cells, are extracted without Office, macros, embedded objects or example execution.
Locators identify `word/document.xml` paragraphs, not rendered page numbers.
Media hashes identify assets without certifying the behavior pictured.

| Source | Verified identity and interpretation |
| --- | --- |
| [Original plan](../Multi_Model_Coding_Agent_Implementation_Plan.docx) | Plan 1.0 P001–P153, historical claims retained. |
| [Revised plan](<../Multi_Model_Coding_Agent_Implementation_Plan (1).docx>) | Plan 1.1 P001–P204, added in dependency order. Numeric order is not execution order. |
| [Architecture](../Multi_Model_Coding_Agent_Architecture_Specification.docx) | Baseline 1.1 G01–G35, including semantic/protocol/uncertain-effect/reuse gates. |
| [Embedding addendum](../Local_Embedding_Addendum.docx) | E01–E09, standard before optional learned spaces. |
| [Semantic addendum](../Agent_Semantic_Vocabulary_and_Semantics_v0_1.docx) | Local S01–S12, distinct from sandbox and external-skill S IDs. |

Plan 1.1 reports 198 core and six optional steps, including optional P202.
P154–P157 are labelled planned there, while the later Phase 3 guide and current
source contain their foundations. Preserve chronology; neither an old planned
label nor an implementation paragraph supplies a current qualification verdict.

There are 731 explicit source rows across 21 namespaces, including historical
duplicates. Original U01–U15 and unified U00–U16, architecture G01–G35,
tool-gap G01–G18 and worker G1–G7 remain independent. Application context
migrations 29–33/Stage 10D take precedence over the standalone context prototype.
PostgreSQL is authoritative for Potbot application state; Syn custody and legacy
journal migration retain their separate ownership decisions.

## Repeatable checks and review boundary

```sh
uv run --locked python scripts/roadmap_inventory.py
uv run --locked python scripts/check_tool_plan.py
uv run --locked python scripts/check_memory_graph_plan.py
uv run --locked python -m pytest -q tests/unit/test_roadmap_inventory.py
```

The [policy](roadmap-inventory-policy.json) selects the named programmes. The
separately authored review supplies source dispositions, requirement assessments,
code/test symbols and evidence limits. Narrative coverage includes every textual
section/paragraph, not just explicit stage IDs or keyword matches. Unknown future
implementation and unavailable artifacts retain owners, blockers and next actions.
The closeout records all PRs, workflow scopes, source chronology and current CI.

After explicitly renewing affected review dispositions, regenerate the snapshot:

```sh
uv run --locked python scripts/roadmap_inventory.py --refresh
```

Refresh cannot author a review or repair stale source/code/test assessments.
The checker rejects lost/new sources or stages, duplicate IDs, unknown owners,
stale hashes/symbols, missing workflow mappings and wrong-revision/unsuccessful
baseline CI. Audit closure requires this complete separate review;
`runtime_qualified` remains false. Offline validation is not authentication of
arbitrary CI JSON and cannot establish that every reviewer judgement is correct.

## Historical CI recovery

At the first checkpoint, [run 41358430672](https://github.com/kieransimkin/potbot/actions/runs/41358430672)
failed mypy before full application acceptance. PR14 then repaired the 158 typing
errors, bounded Chromium capture recovery and stale test/migration expectations.
The capture fix permits one alternate read only after the engine's explicit
capture-unavailable error, sharing one six-second deadline; timeout/cancellation
does not start an overlapping capture or repeat an action. Linux CI's managed
Xvfb/Openbox fixture exercises real committed navigation and capture.

Later failures exposed nested AF_UNIX fixture paths, outdated context/service
fixtures and database-suite timeout limits. The fixes preserve production socket
permissions, checksum history, RLS and all mandatory assertions. Windows local
checks never qualify Linux namespaces, cgroups, Xpra or production identity.

These failures are historical, not the current-main verdict. The pinned closeout
baseline's [complete core CI](https://github.com/kieransimkin/potbot/actions/runs/41532663749)
passed, along with inventory, installation boundaries and KeywordMoves. Individual
workflow heads, skipped conditional gates and real-host limits are retained in
the review. U00-A is complete; U01-A implementation and U03/U06/U16 production
qualification remain work to do.
