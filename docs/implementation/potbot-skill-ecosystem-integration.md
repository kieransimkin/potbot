# Potbot external skill ecosystem integration

Status: proposed research-driven plan, 2026-11-01. Complements `unified-development-roadmap.md`, `cross-agent-compatibility.md`, and `plugin-discovery-strategy.md`. No claim that a third-party skill was installed, executed, or qualified. The independent ClawSkills directory advertises 5,705+ indexed entries but exposes 71 named curated listings; listing descriptions are not executable package audits.

## Goal and non-goals

Run compatible OpenClaw, Codex and Claude Code skills and workflows *natively* on Potbot's operator-mediated SYN Cog runtime, without requiring the original host agent. Preserve author intent, declared dependencies, skill files, commands and workflows. Do not promise arbitrary unmodified compatibility with proprietary tool names, privileged host APIs, unqualified OS dependencies or unsafe instructions. Report unsupported requirements and exact substitutions. No silent execution of installer scripts or automatic credential disclosure.

## Sources and source-of-truth

1. Official ClawHub `https://clawhub.ai/` and OpenClaw `https://docs.openclaw.ai/clawhub`, `https://docs.openclaw.ai/skills`, `https://docs.openclaw.ai/cli/skills`: canonical for ClawHub packages, versioning, verification, manifests and installed origin state.
2. Independent `https://openclaw-skillshub.com/skills/`: discovery/review *leads*, not authority for package bytes or safety; reconcile names against canonical registry or publisher source.
3. `skills.sh` / GitHub public repositories: discover AgentSkills SKILL.md files and resolve immutable Git commit; ClawHub's skills-sh references may be mirrored with distinct not-scanned trust status.
4. MCP Registry `https://registry.modelcontextprotocol.io/`: MCP server discovery, not automatically a skill registry.
5. OpenClaw bundled/workspace skills, Codex `$CODEX_HOME/skills`, Claude Code plugin/skills marketplaces and user-approved local folders; use source-specific adapters and documented APIs only. Optional enterprise/internal registries and Git URL imports.
6. For each source retain provenance, retrieval time, namespace, publisher, version, commit, hashes, license, claimed capabilities, scan/verification evidence and limits. Prefer immutable artifact and lock metadata over mutable URLs.

## Sampled breadth: 71 directory entries / 10 categories

The named directory includes GPT Prompt Chainer, LLM Router, Prompt Optimizer, Context Window Manager, Token Counter, Embeddings Manager, RAG Pipeline, Vision Analyzer, Agent Orchestrator, Fine-Tune Studio, Model Evaluator; Deep Research, Web Scraper Pro, Academic Search, News Aggregator, Patent Search, Semantic Search, Knowledge Graph, ArXiv Explorer; React Scaffolder, API Mocker, CSS Generator, Component Library, Lighthouse Audit, Responsive Tester, Vercel Deploy; Docker Compose, Kubernetes Pilot, Terraform Manager, AWS Toolkit, Cloudflare Worker, CI/CD Pipeline, Log Analyzer; Browser Pilot, Form Filler, Screenshot Tool, PDF Generator, Email Automator, Test Runner; Notion Sync, Linear Integration, Todoist Sync, Calendar Manager, Slack Connector, Home Assistant Bridge, WhatsApp Connector, Notification Hub, Time Tracker; GA4 Analytics, Social Media Manager, SEO Optimizer, Email Campaign, Lead Scorer, CRM Connector; GitHub Manager, Code Reviewer, Refactor Agent, Debug Assistant, Test Generator, Docs Generator; Obsidian Vault, Roam Research, Logseq Bridge, Zettelkasten, Bookmark Manager; Workout Planner, Nutrition Tracker, Sleep Analyzer, Meditation Guide; Data Pipeline. This is a catalogue-level capability sampling, not inspection of 71 downloadable archives. No safety rating is treated as independent attestation.

### Capability gap matrix and actions

| Skill class | Required native Potbot Cogs / services | Compatibility and safety qualification |
|---|---|---|
| Browser Pilot / Form Filler / Screenshot / Test Runner | Persistent browser sessions, navigation, tabs, DOM/accessibility snapshot, element refs, click/type/scroll, screenshot, upload/download, dialogs, waits, test runner and artifact retention | Map OpenClaw browser actions and Codex/Claude browser-MCP conventions to same browser Cogs; invalidate stale refs, enforce origin/network policy, deny CAPTCHA bypass and unsafe auth automation |
| GitHub / code review / refactor / debugger | Repository status, read/search, git diff, patch application, tests, debugger launch/inspect, process/terminal lifecycle | Map `exec`, `apply_patch`, `read_file`, `grep` and equivalent vendor tools; preserve line endings, cwd, exit status, streams and approvals |
| RAG / research / academic / patent | Search, fetch, source provenance, citations, parser, document chunking, hybrid retrieval, embedding service | Explicit web and provider coverage; distinguish search snippets from retrieved evidence, observe robots/rate limits |
| Prompt chains / router / orchestration | Typed workflow DAG, subagent handoff, context scopes, model catalogue, prompt compiler, budgets, checkpoint and interrupt | Avoid recursive unbounded delegation; prevent skill instructions from granting new authority |
| Docker / Kubernetes / Terraform / cloud | Sandboxed CLI runners and admitted external endpoints, credential broker, dry-run/plan/apply, target identity and rollback | No host Docker socket or cloud-admin privilege by default; isolate target credentials; irreversible actions require policy |
| Notion / Linear / Todoist / calendar / Slack / WhatsApp / email | OAuth/token broker, provider connector Cogs, webhook/polling triggers, paging, attachments, message receipts | Connector presence != authorization; per-action consent and deduplication; no assumed availability |
| Obsidian / Logseq / Zettelkasten | Scoped vault files, markdown, links, search, index and bidirectional references | Safe paths, conflict resolution, sync version checks |
| PDFs / vision / media / data pipelines | Artifact conversions, document rendering, OCR optional, image models, CSV/JSON/SQL, streaming chunks | Resource limits, provenance and output validation |
| Health/fitness and high-impact advice | Optional authorized personal-data connectors and evidence-aware response pipeline | Never claim medical data access or medical validation; elevated privacy requirements |

## Required architecture changes

### S01: Source adapters and catalogue federation

Implement `SkillSource` interface with `search`, `inspect`, `resolve`, `fetch`, `verify`, `update_status`; implementations for official ClawHub, skills.sh/Git, local folders, OpenClaw bundled skills, Codex and Claude plugin repositories, plus independent discovery index. Source records are untrusted until validated. Add bounded pagination, TTL, ETag/cache, rate-limit handling, tombstones, publisher identity and duplicate resolution. Distinguish *skill*, *plugin*, *MCP server*, *workflow* and *tool* packages. A search result never implies installed or executable.

### S02: Portable skill intermediate representation

Preserve original SKILL.md and supporting assets, YAML frontmatter, version, name, description, command aliases, `user-invocable`, `disable-model-invocation`, `command-dispatch`, `command-tool`, `command-arg-mode`, `{baseDir}`, platform gates, required `bins`/`anyBins`/`env`/`config`, `primaryEnv`, installer specs and source-specific metadata. Parse safely with bounded sizes, path containment and no executable evaluation. Add source-versioned dialect adapters and a loss-report for unsupported features. Support direct tool dispatch without involving an LLM where declared and authorized.

### S03: Readiness, visibility and invocation

Separate discovered / installed / verified / ready / visible / active / qualified statuses. Check dependencies inside the *actual sandbox or admitted remote runtime*, not the host. Per-agent allowlists and workspace overlays; deterministic precedence/collision resolution; immutable per-run skill snapshot and refresh-on-next-turn semantics. Progressive disclosure: skill index -> relevant SKILL.md -> supporting files on demand. Install updates must not silently alter running workflows.

### S04: Dependencies and secret broker

Translate brew/node/go/uv/download installer hints into reviewed, version-pinned sandbox package plans; deny unsupported installers rather than executing host shell. Store dependencies in versioned images or isolated tool environments with SBOM and rollback. Resolve environment requirements through operator-owned credential broker and scoped ephemeral injection; never put secrets in prompts, install manifests, logs or uncontrolled subprocess environments.

### S05: Native Cog compatibility shims

Maintain one canonical `syn://cog/...` input/output contract per operation and expose alias/schema adapters for OpenClaw `exec`/browser and other tool conventions, Codex shell/patch/file operations, Claude Bash/Read/Write/Edit/Glob/Grep/Task and MCP tools. Ensure output parity (text/images/artifacts, errors, streams, exit codes, tool-use IDs). Adapter may translate but never mint authority. Publish capability coverage report for each skill and provide explicit unsupported/partial results.

### S06: Workflows, hooks and commands

Import slash commands, prompt chains, workflow DAGs, subagents and vendor-specific hooks into declarative task contracts; support triggers, approvals, idempotency, checkpoint/resume and cancellation. Preserve source files and show normalized graph before activation. Untrusted hook shell scripts require separate explicit admission, never implicit import. Use deterministic workflow and hook ordering.

### S07: Supply-chain verification and update policy

Record ClawHub `.clawhub/origin.json`, workspace `.clawhub/lock.json` semantics where importing existing installs; support legacy `.clawdhub` only through explicit migration. Verify exact version/registry, content digest and available ClawHub trust envelope, preserve 'not scanned' for skills-sh mirrors. Static scan, archive traversal/symlink checks, dependency audit, quarantine test, operator approval, atomic install, rollback, revocation and drift detection. Publisher signatures and directory trust scores are not safety proofs.

### S08: Discovery UI and CLI

Expose task-led search across sources, inspect raw skill, dependencies, permission diff, compatibility score and *evidence*, choose version/source, dry-run install, qualify, enable/disable, update/rollback. Add CLI `potbot skills search|inspect|plan|install|check|list|verify|update|remove|test` (proposed names). Show per-agent readiness, missing Cogs, unsupported operations, security scan, origin and exact installed revision.

### S09: Compatibility qualification suite

Pin representative real packages after verifying canonical source: at least 3 per category across 10 categories, with at least one complex multi-file script skill, one vendor-specific command/hook, one OAuth integration, one browser workflow, one coding workflow, one cloud plan/apply workflow (mocked external target), one workflow with subagents and one package requiring binaries. Run parsing, planning and simulated execution on all; run authorized live execution only where safe and available. Matrix by source and version: import fidelity, readiness, capability coverage, execution outcome, security denial, recovery, update rollback and evidence. Add malicious fixtures: path traversal, symlinks, prompt injection, installer command, credential exfiltration, spoofed origin, poisoned tool descriptions and unbounded recursion. Set a baseline before target thresholds; never substitute directory summaries for package fixtures.

### S10: Native authoring and publication

Allow Potbot to generate portable AgentSkills-compliant SKILL.md and optional vendor adapters, validate portability, export deterministic bundles, publish only with explicit user approval to supported registries. Generated skills remain drafts until tested and approved. Support workshop revisions with immutable history and rollback.

## Delivery order and integration

1. S00 inventory/pin official specifications and sample package fixtures, mapping existing T01–T48 and C00–C09 milestones (no duplicate infrastructure).
2. S01+S02 registry adapters, importer and loss reporting (parallel with existing C01).
3. S03+S04 runtime readiness, dependency and credential policies.
4. S05 browser, filesystem, shell, patch and GitHub Cog parity (prioritize existing browser controller milestone C04).
5. S06 declarative workflows, slash commands and hooks.
6. S07 trusted install/update and revocation; stage admission before any executable package rollout.
7. S08 UI/CLI discovery, source provenance and inspectable install plan.
8. S09 real skill qualification, CI fixtures, platform matrix.
9. S10 authoring/export and continuous ecosystem drift checks.

Critical gates: no arbitrary third-party execution during metadata search; exact immutable package identity; host/sandbox boundary preserved; denied effects dispatch zero operations; version drift invalidates qualification; skill instructions never outrank operator policy; real browser/coding fixture successfully executes via native Cogs; no unverified 'universal compatibility' claims.

## Documentation references

- https://openclaw-skillshub.com/skills/ (independent curated catalogue)
- https://openclaw-skillshub.com/about (non-affiliation)
- https://docs.openclaw.ai/skills (format, gating, snapshots, security)
- https://docs.openclaw.ai/clawhub (official registry)
- https://docs.openclaw.ai/clawhub/skill-format (package layout and metadata)
- https://docs.openclaw.ai/cli/skills (CLI install, update, verify)
- https://docs.openclaw.ai/clawhub/quickstart (registry workflows)
- https://agentskills.io/ (portable skill format)
- https://modelcontextprotocol.io/registry/quickstart (MCP server registry)
