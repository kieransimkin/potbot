# Cross-agent compatibility: OpenClaw, Codex, Claude Code

**Status:** Proposed, 2026-11-01. This is a requirements and implementation plan, not a claim that adapters are qualified. Integrates with `unified-development-roadmap.md`, `agent-runtime-modernization.md`, the synchronized 48-stage tool plan and `10-syn-tool-cogs.md`.

## Goal and boundaries

Potbot should **consume and export** skills, commands, workflows and tools authored for OpenClaw, OpenAI Codex and Anthropic Claude Code. Preserve original source bundles and semantics where supported; translate to one canonical Potbot representation and run through **installed, typed SYN Cogs**, the controller's authority checks, existing tool gateway, PostgreSQL durability, workspace isolation and signed evidence. Do not emulate vendor security privileges, private APIs, proprietary services or undocumented host behavior. Never treat format parsing as permission to execute.

Compatibility has separate grades: **discoverable**, **parseable**, **translated**, **runnable with approved capabilities**, **behaviorally conformant**, **exportable**. Record unsupported constructs and required user decisions rather than silently dropping them. Pin upstream specification/release versions and fixtures. A native Codex/Claude model provider adapter is not a Codex/Claude *harness* compatibility layer.

## Research baseline and upstream surfaces

- [OpenClaw tools](https://docs.openclaw.ai/tools), [skills](https://docs.openclaw.ai/tools/skills), [browser agent tools](https://docs.openclaw.ai/tools/browser/agent-tools), [browser setup](https://docs.openclaw.ai/tools/browser/setup), [plugin bundles](https://docs.openclaw.ai/plugins/bundles), [plugins](https://docs.openclaw.ai/plugins). OpenClaw distinguishes tools, skills, plugins, hooks and workflows. Its browser has a single `browser` tool with actions including status, tabs, snapshot, screenshot, navigation and act; browser automation is a plugin with a lazily loaded skill. It also recognizes Codex and Claude bundles; its exact conversion limitations must be captured in fixtures.
- [OpenAI portable plugin packaging](https://developers.openai.com/plugins/build/plugins), [skills](https://developers.openai.com/plugins/concepts/skills), [Claude plugin conversion guidance](https://developers.openai.com/plugins/guides/submit-claude-plugin). Portable `plugin.json` plus `skills/` and `mcp.json`, and Codex `.codex-plugin/plugin.json` plus `.mcp.json` and optional hooks, are distinct layouts. Codex uses `AGENTS.md` and SKILL.md resources; hook availability varies by host.
- Claude Code: [plugin distribution](https://code.claude.com/docs/en/plugins), [skills](https://code.claude.com/docs/en/skills), [hooks](https://code.claude.com/docs/en/hooks), [MCP](https://code.claude.com/docs/en/mcp), [subagents](https://code.claude.com/docs/en/sub-agents). Confirm exact schema and lifecycle names against a pinned release before writing an adapter. Common surfaces include `.claude-plugin/plugin.json`, `.mcp.json`, `skills/`, `commands/`, `agents/`, `hooks/hooks.json`, `CLAUDE.md`, and project settings. Do not assume every vendor extension is portable.
- [Agent Skills](https://agentskills.io/specification) and [MCP](https://modelcontextprotocol.io/specification) are preferred vendor-neutral interchange contracts. MCP tool/resource/prompt availability does not authorize effects.

## Compatibility model

`Source package -> format detector -> bounded parser -> source-preserving intermediate representation -> dependency/capability resolver -> operator admission -> SYN Cog/tool binding -> qualified sandbox execution -> typed receipts -> optional vendor export`

Canonical objects: `SkillBundle`, `SkillInstruction`, `Command`, `AgentProfile`, `WorkflowGraph`, `ToolRequirement`, `McpServerDescriptor`, `HookSubscription`, `CapabilityBinding`, `PackageProvenance`, `CompatibilityReport`. Each includes source format/version, original paths, hashes, trust scope, unresolved requirements, and lossy-translation flags. Use existing semantic Types where available; add new Types through the signed catalog process. No generic Python import or shell hook may bypass the controller.

### Feature-by-feature translation

| Feature | OpenClaw | Codex | Claude Code | Potbot implementation / gap |
|---|---|---|---|---|
| Skills | SKILL.md, metadata gating, bundled/plugin/workspace precedence | SKILL.md, resources, project/user scopes | SKILL.md, command/skill frontmatter and resources | Shared loader, source-scoped precedence, environment checks, lazy resources, exact references; **partial/verify** |
| Instructions | AGENTS.md/workspace bootstrap | AGENTS.md hierarchy | CLAUDE.md hierarchy | Explicit precedence, authority and scope; map to PromptCompiler; **planned/verify** |
| Plugin bundles | openclaw.plugin.json and compatible bundles | portable plugin.json, `.codex-plugin`, MCP configs | `.claude-plugin`, commands/agents/hooks | Importer, manifest validation, dependency resolution, signing and rollback; **planned** |
| Tool calls | built-in tool profiles, plugin tools | shell, patch, MCP, provider tools | Bash, Read/Edit/Write, MCP, browser via MCP/plugin | Vendor alias facade to installed SYN Cogs; no direct host bypass; **partial/verify** |
| Hooks | runtime plugin lifecycle | host-dependent lifecycle hooks | hooks.json lifecycle and command hooks | Hook normalization, policy, ordering, timeout, cancellation, event mapping; **planned** |
| Subagents | routed agents and delegation | delegated agent tasks | agent definitions, subagents | AgentProfile mapping onto durable graph/worker; **partial/verify** |
| Workflows | skills, Lobster and plugin pipelines | skills, scripts and agent orchestration | skills, commands, hooks and agents | Typed DAG plus imperative shell-step compatibility; **planned** |
| MCP | client/server integrations | MCP tools and bundled servers | MCP server declarations | Bidirectional MCP facade with auth, resource/prompt support, streaming and change notifications; **partial/verify** |
| Browser | `browser` action dispatcher | browser via available harness/MCP | browser via MCP/plugin | OpenClaw-style facade, MCP browser provider, CLI skill shims, exact result shapes; **not qualified** |
| Context/approvals | profile, policy, sandbox and approvals | sandbox, tool permissions, approvals | permissions and hook decisions | Translate only restrictive intent; operator policy always authoritative; **partial/verify** |

## Tool provisioning gap inventory

Classify each operation separately as `existing-qualified`, `implemented-unqualified`, `planned`, `absent` or `unknown` against current source and tests. **Do not infer missing implementation from a missing README mention.** Minimum portable tool surface:

1. **Filesystem:** scoped read/write/edit, exact-match replacements, patch application, directory listing, glob and grep, binary transfer, file metadata, atomic writes, safe path resolution, structured diffs and undo/rollback. Map Codex/Claude shell and patch idioms to safe Cogs; keep separate permissions for read vs mutation.
2. **Shell/process:** bash execution inside workspace sandbox, long-running process sessions, stdout/stderr streaming, exit codes, environment injection via broker, working-directory control, signal/cancel, timeout, background tasks and command approval. Do not expose host shell or inherited credentials.
3. **Browser:** persistent browser/profile/tab sessions, list/open/focus/close, navigation, DOM/ARIA snapshots, stable element references, click/type/select/scroll/keyboard, screenshots, downloads/uploads, network/request diagnostics, wait, dialogs, frames, console and browser state. Maintain explicit browser-session and tab ownership, stale-ref recovery, domain/SSRF restrictions, secret/cookie isolation and audit. Never assume all vendor-specific browser actions are equivalent.
4. **Web/network:** search, fetch, extraction, safe redirects, domain allowlists, downloads and MIME/size limits; distinguish read-only fetch from browser actions and authenticated API effects.
5. **Git/code:** status, diff, patch, branches/worktrees, commits, repository search, tests, debugger and language-server operations; concurrency-aware locks and exact source revisions.
6. **Human/credentials:** ask/approve/deny, scoped credential requests, secret references, session interrupts and resumable confirmation; no plaintext secrets in model prompts.
7. **Media/artifacts:** images, PDFs, audio/video, screenshots, structured binary artifacts, stable handles, upload/download and content-type negotiation.
8. **Scheduling/messages:** timers, cron, webhooks, outbound messaging and event subscriptions only where a skill declares them; explicit side-effect authority.
9. **MCP completeness:** tools, resources, prompts, list-change handling, progress, cancellation, pagination, transport/auth negotiation and structured errors; both client and server surfaces.
10. **Developer ergonomics:** discover/search tool, inspect schemas, tool aliases, skill dependencies, dry-run/qualification, compatibility diagnostics and reproducible fixtures.

### Browser compatibility profile (first vertical slice)

Provide a single controller-backed browser session service with typed operations, and expose three adapters: (a) `browser` action envelope for OpenClaw-style skills, (b) MCP browser tools for Codex/Claude workflows, (c) optional CLI commands inside the sandbox for skills expecting commands. Adapter outputs should preserve page/tab IDs, snapshot refs, screenshot artifact references and action error categories. Map vendor-specific selectors or action names only when semantic equivalence is tested. On navigation or DOM mutation invalidate refs or re-snapshot. Do not let the CLI or MCP adapter bypass Cog admission. Test tab isolation, expired sessions, popup handling, redirects, credential boundaries, concurrent agents, crash recovery and blocked destinations.

## Implementation milestones (add to unified roadmap)

**C00 — Pin specifications and fixtures.** Capture official upstream schema revisions and representative permissively usable fixtures; make a feature/format matrix and test corpus. Gate: fixtures identify all supported and unsupported constructs, including browser workflows.

**C01 — Canonical compatibility IR.** Add immutable source package records, format detection, bounded parser, provenance, diagnostics and round-trip preservation. Gate: deterministic parsing, no code execution, hostile ZIP/path/symlink tests.

**C02 — Skills and instructions.** Support Agent Skills, AGENTS.md and CLAUDE.md, scoped precedence, frontmatter variants, bundled references/scripts, lazy loading and compatibility warnings. Gate: three representative skills per ecosystem load with preserved dependencies.

**C03 — Tool alias and schema bridge.** Resolve vendor tool names and argument/output shapes to installed SYN Cogs; register exact conversion versions and lossiness. Gate: filesystem, shell, Git and patch fixtures produce equivalent verified effects and denied requests cause zero dispatches.

**C04 — Browser compatibility vertical slice.** Implement OpenClaw `browser` action facade, MCP browser provider, and CLI shim where required. Gate: same bounded web task passes in all three harness-style fixture profiles; security and recovery tests pass.

**C05 — Bundle/plugin import and dependency admission.** Support portable Agent Plugins, Codex and Claude plugin layouts, OpenClaw bundle metadata; resolve MCP servers, binaries, env, credentials, hooks and resources. Gate: dry-run report, explicit authorization, quarantine, rollback and immutable package identity.

**C06 — Hooks, commands, agents and workflows.** Normalize supported lifecycle hooks, slash commands, subagent profiles, and resumable workflow DAGs. Unsupported hook semantics must fail explicitly. Gate: approved side effects, cancellation, restart and ordering tests.

**C07 — Bidirectional export and MCP interoperability.** Export native Potbot skills/tools as Agent Skills, MCP services and compatible plugin bundles where semantics permit; preserve manifest provenance and declare limitations. Gate: external clients can discover and invoke exported capabilities under operator policy.

**C08 — Cross-ecosystem conformance suite.** Golden skill/tool/workflow tests, upstream version matrix, browser regression fixtures, prompt-injection tests, permission boundaries, unsupported-feature diagnostics, cross-platform qualification. Gate: each published compatibility claim has retained fixture evidence.

**C09 — Ongoing ecosystem maintenance.** Scheduled upstream schema review, version negotiation, fixture refresh, deprecation windows and compatibility dashboard. Gate: incompatible changes are detected before release and do not silently alter execution.

## Dependencies and roadmap placement

C00 and C01 begin in unified Wave 0/1 alongside architecture audit, context and plugin contracts. C02–C04 belong in Wave 2 with tool-Cog, sandbox, browser and MCP work. C05–C06 depend on durable lifecycle hooks and approval/credential infrastructure. C07–C09 follow in Waves 4–5. Link to the 48-stage tool roadmap without renumbering or replacing its status. Extend T04 discovery/qualification, tool gap audits, browser plan and modernization prompt/plugin work; avoid duplicate schedulers, registries and event streams.

## Exit criteria

A Potbot user can select a published OpenClaw, Codex or Claude skill/plugin/workflow; Potbot inventories and explains its dependencies, reports unsupported features, requests only necessary authority, installs or maps its required tools to native Cogs, executes in the workspace sandbox, produces verified results, and can resume/replay safely. Exported Potbot capabilities work in representative external clients. Compatibility is graded per feature and upstream version, never asserted universally from a single smoke test.
