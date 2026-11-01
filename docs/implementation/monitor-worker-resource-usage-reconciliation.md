# Monitor Worker Resource Usage — roadmap reconciliation

**Audit date:** 2026-11-01.

**Pinned repository:** `kieransimkin/potbot`, `main` at `ef9a48a9c46a3ccfabc1d969665073f57224117e`.

**Parent plan:** [unified development roadmap](unified-development-roadmap.md).

**Scope:** the monitoring and subsequent SYN identity/provisioning plans recovered from the conversation titled **Monitor Worker Resource Usage**, corroborated against the committed documents and selected current source files. This is a targeted requirements audit; it does not certify deployment or complete the repository-wide U00 inventory.

## Finding and evidence rules

The previous roadmap included broad U03 monitoring and U06 identity summaries. It did not explicitly preserve all the chat's timings, coverage requirements, corrected key hierarchy, later SYN phases or unfinished integration gates. This companion supplies that mapping and makes the remaining work part of the roadmap.

Source documents describe several implemented stages, but their historical test results are not fresh test results for this revision. No live kernel/sandbox, PostgreSQL, native OS-keystore, cross-host LAN or full CI qualification was executed by this documentation audit. Use **source present; qualification unverified**, **partial**, **planned** and **requested / unverified** below as evidence classifications, not invented release states. A skipped provider or host test is not a pass.

The complete original conversation transcript was not available as a repository artifact. Recovered decisions are cross-checked against their written contracts. No missing Phase 49 contents or source-chat URL is inferred. Historical deferrals may be superseded by later source implementations; preserve the requirement while taking the latest applicable contract as the implementation baseline.

## 1. Source chronology and numbering

The 48 canonical tool stages, numbered implementation documents, SYN phases, browser S milestones, MODEL.md phases and external-skill S00–S10 milestones are separate sequences. In particular, SYN Phase 48 is not browser S48 or MODEL.md Phase 48.

| Source | Scope and interpretation | U owners |
|---|---|---|
| [Monitoring 31](31-cog-process-resource-audit.md) | Operator syscall/cgroup audit, system sampler and sprite contract; sampled per-PID values were lower bounds. Preserve its later optimization and forensic-coverage requirements. | U02, U03, U13, U16 |
| [Monitoring 33](33-agent-attribution-and-forensic-file-index.md) | Agent-home attribution, private process/file indexes, incomplete-coverage handling. | U03 |
| [Monitoring 34](34-kernel-exit-accounting-and-isolated-cog-workers.md) | TASKSTATS final counters and sandbox-only executable worker dispatch; supersedes the earlier missing exit-counter implementation, not live-host qualification. | U02, U03, U16 |
| [Monitoring 35](35-worker-syn-nodes-and-qualification.md) | Per-worker in-operator heartbeat actors and qualification runner; expressly not independent sandbox SYN processes. | U03, U06, U16 |
| [Monitoring 38](38-operator-worker-qualification.md) | Real-host release gate, complete route enumeration, cancellation and requalification. | U03, U16 |
| [SYN 39](39-syn-agent-inheritance-and-independence.md) | Historical v1, trusted local discovery, separate agent installation, corrected hardened root and eight choices. Its temporary-key-hash inheritance is superseded for new enrollment by v2. | U06 |
| [SYN 40](40-syn-agent-hardware-inheritance.md) | Current v2 hardware-byte inheritance, independently random temporary signing key and confidential signed pledge contract. | U06 |
| [SYN 41](41-syn-operator-enrollment-ledger.md) | Signed pulse/offer/candidates/selection ledger and admission boundary; selection does not activate an agent. | U01, U06 |
| [SYN 42](42-syn-local-daemon-and-adoption-boundary.md) | Trusted local discovery, challenged daemon identity and public adoption receipts; Potbot remains a public client. | U06 |
| [SYN 43](43-syn-owned-local-service.md) | Independently installable Python service, local transport, receipt journal and temporary-key OS custody. Its legacy signer proposal is not a requirement to rewrite SYN in Rust. | U06, U16 |
| [SYN 44](44-syn-python-hd-operator-keystore.md) | Python CIP-3/Ed25519-BIP32 operator root/base derivation and OS credential installation. | U06 |
| [SYN 45](45-syn-python-recovery-and-provider-qualification.md) | BIP-39 recovery, provider compatibility, private console and no-overwrite restoration. | U06, U16 |
| [SYN 46](46-syn-ci-isolation-and-live-derivation.md) | Isolated standalone CI and real-provider derivation/signatures for all eight choices. | U06, U16 |
| [SYN 47](47-syn-selected-key-adoption.md) | Host-local encrypted selected-leaf handoff, key installation, possession proof and signed receipt; separate activation and real custody gates remain. | U01, U06, U16 |
| [SYN 48](48-syn-p2p-lan-discovery-and-encrypted-pledges.md) | Signed LAN discovery and confidential pledge inbox; explicitly stops at pending operator review and is not wired into selected-state enrollment/adoption. | U06, U16 |
| SYN 49 | Continuation requested in the recovered conversation; no matching SYN phase document or patch was verified. Do not confuse [MODEL.md 49](49-model-md-authored-skills.md) with it. Track required next integration as U06-E without claiming Phase 49 shipped. | U00, U06 |
| [Registered-worker 46](46-registered-worker-qualification.md), [sandbox-launch 47](47-shared-sandbox-launch-and-audit.md), [browser-provenance 48](48-browser-controller-provenance.md) | Related acceptance/provenance work from a separate sequence. Sequential worker smoke does not establish concurrent isolation; remaining nested-worker and browser gates still matter. | U02, U03, U12, U16 |
| [Identity and Ember runtime](../agent-identity-and-ember-runtime.md) | Hourly delegation, public verification, scoped signing, inherited Embers and legacy custody profiles; preserve contracts while reconciling newer SYN ownership. | U01, U06 |

## 2. Monitoring requirements and coverage

| ID | Requirement retained from the chat and its implementation plans | Evidence at the pinned revision | Roadmap ownership / completion gate |
|---|---|---|---|
| MW01 | Every executable Cog worker operates inside its registered workspace sandbox; operator monitors its full process tree. | Monitoring 34 documents mandatory sandbox work dispatch; 38 still requires route enumeration. Trusted signing, credential, database and artifact mediators have a separate host-controller role. | U02/U03, G1: enumerate all work routes and reject unqualified or unaudited execution before dispatch. |
| MW02 | Account for descendants, threads, forks, execs, rapid exits and detached processes; confirm the whole tree stops. | Monitoring 33–35 describe task relationships, cycle/conflict checks and cgroup cleanup; a heartbeat or attempted kill is insufficient evidence. | U03, G1: stress PID reuse, rapid fork/exit, cancellation, timeout, SIGKILL and orphan cleanup; incomplete evidence prevents successful completion. |
| MW03 | Per-process file access ledger records access modes, successful and failed attempts, and syscall activity. | Private file/process indexes exist by source documentation. Lexical relative paths are not complete canonical inode identity; interrupted/resumed and rename/link operations require faithful reconstruction. | U03, G1: qualify descriptor/path/inode semantics, audit continuity, loss detection and bounded retention; never silently discard excess events. |
| MW04 | Record final user/system CPU, peak RSS, physical and character I/O, and faults per task, alongside separately scoped worker-cgroup totals. | Monitoring 34 supplies TASKSTATS exit accounting. Earlier `/proc` sampling is a lower bound; syscall wall duration is not CPU time; cgroup totals include monitoring overhead. | U03/U16, G1: correlate every traced task with final accounting, reject missing/duplicate/lost records, state units and scope, measure overhead; do not add physical and character I/O together. |
| MW05 | Monitor overall utilization and bottlenecks independently of UI polling. | Monitoring 31 defines a saturation indicator as maximum of busy CPU and CPU/memory/I/O PSI, not a sum of percentages or a GPU measure. | U03, G1/G6: preserve semantics and show unavailable, warming-up and incomplete states. Retain lower-overhead eBPF/LSM collector work where supported without requiring a rewrite of qualified TASKSTATS accounting. |
| MW06 | Central workspace-selector sprite pulses at **60 BPM**; glow brightens as the **three-minute** average approaches full load. | [Current component](../../frontend/src/components/ResourcePulseCore.tsx) consumes `load3m`; [CSS](../../frontend/src/components/ResourcePulseCore.module.css) has a one-second pulse and load-driven brightness. The contract specifies a time-weighted **180-second** sampler window. | U03/U13, G6: verify backend-to-UI wiring, placement, monotonic brightness, timing, warm-up and reduced-motion behavior. |
| MW07 | A separate short-average overlay flashes for sudden load spikes. | Monitoring 31 specifies a **2-second** window; current component uses `load2s` for a distinct surge flash. | U03/U13, G6: demonstrate brief-spike response and no repeated surge flashes during steady load; retain separate slow and fast signals. |
| MW08 | **Each agent** has a heat bar representing its contribution to system load. | **Partial:** current component sorts by `heat` and uses `.slice(0, 8)`; labels explicitly say share of **host busy CPU**. Broader resource attribution and every-agent access are not established. | U03/U13, G6: provide all-agent access, verified CPU/memory/I/O contributions or a documented aggregate model, explicit denominators and incomplete-coverage indicators. Do not relabel CPU share as total-system load or sum incompatible percentages. |
| MW09 | Attribute work to the correct agent, including multiple agents under one principal, with private durable evidence. | Monitoring 33 binds `AgentHomeRegistry` identity and lease authority; raw traces and paths remain operator-private, public views are bounded aggregates. | U03/U01, G1: registry revalidation, cross-workspace exclusion, overflow/stale-index handling, evidence hashes, permissions and retention/recovery. |
| MW10 | Independent SYN lifecycle in the agent/worker sandbox, bound to identity and monitored by the operator. | **Partial:** Monitoring 35 uses an in-operator async actor per invocation. The later standalone Python service exists, but LAN intake does not complete per-worker sandbox integration. | U03/U06, G2: actual sandbox process launch, wallet/agent binding, authenticated relay, incarnation/sequence replay handling, restart/cancellation and durable event replay. |

## 3. SYN identity, provisioning and security requirements

| ID | Required contract | Evidence / remaining work | Roadmap ownership / completion gate |
|---|---|---|---|
| MW11 | SYN owns generation, derivation, signing, recovery and private custody; it remains an independently installed **Python** service. Potbot consumes public identities/receipts. | SYN 43–48 and `syn-node/` are the current service baseline. The legacy native broker remains a separate profile, not a new SYN language requirement. | U06, G2/G3/G5: no Potbot private-key authority or unqualified fallback. |
| MW12 | Discover and pledge to an existing trusted local operator SYN; create a fresh operator only after establishing none exists. Every new agent has a separate agent-mode installation. | SYN 39/42 define trusted registry discovery, ambiguity/locked-store refusal and independent agent setup. | U06/U02, G2: installer and runtime lifecycle qualification; registry failure cannot silently create a replacement identity. |
| MW13 | Genuine OS CSPRNG entropy, BIP-39 recovery, CIP-3 Icarus / Ed25519-BIP32 V2, exact root/base and stable base-public-key hash. | Python HD and 24-word recovery are source-present in SYN 44–46. Real-provider checks and actual OS custody remain distinct gates. | U06, G3/G5: fixed vectors, original-identity restoration, no overwrite or plaintext fallback; preserve the exact profile below. |
| MW14 | Each agent's temporary Ed25519 signing key is independently random and OS-protected. Hardware ID participates in the signed pledge, not entropy generation. | SYN 40 supersedes v1; 43 supplies temporary-key bootstrap; 48 transports the confidential v2 offer. | U06, G3/G4: reject weak hardware-derived entropy, mismatched signatures, wrong target, stale/replayed challenge and unqualified hardware source. |
| MW15 | Inheritance is the first three SHA-256 bytes of **raw signed hardware bytes plus one operator byte**, interpreted unsigned big-endian; reserve atomically. | Current v2 contract and later SYN docs preserve this formula. Temporary-key-hash inheritance is historical only. | U06/U01, G3/G4/G7: deterministic vectors, byte-order and collision tests, no duplicate allocation, durable reservation/replay state. |
| MW16 | Operator offers eight signed root-derived choices; agent selects its independence index; full hashes remain authoritative. | SYN 40–47 use indices 1–8 and distinct three-byte labels; signed selection must be retained durably inside SYN. | U06, G3/G4: actual derivation, distinct labels, signed candidate/selection commitments, full-hash comparison and no authority from abbreviations. |
| MW17 | Authenticated operator pulses and encrypted signed servitude offers bind hardware, temporary verification key, time, target and challenge. | **Partial end-to-end:** SYN 48 implements bounded LAN discovery/intake with `pending-operator-review` and `activation_authorized=false`. | U06, G4/G5: hardware-source/attestation policy, real cross-host tests, owner approval and bridge into selected-state enrollment. |
| MW18 | Deliver only the selected leaf confidentially to the separate agent, install in its OS keystore, prove possession, record signed receipt, then authorize scoped execution separately. | SYN 47 provides host-local sealed handoff and receipt. SYN 48 is explicitly not connected to `SynEnrollmentLedger` / `SelectedPledgeAuthorizer`. | U06/U01/U02, G4: approved/fenced bridge, durable original signatures, uncertain-outcome reconciliation, independent process identity checks and activation policy. |
| MW19 | Retain hourly derived signing, stable verification identity, inherited/default/required Embers, locking-Cog permissions and scoped replay rejection. | Identity/Ember runtime describes the existing contracts; the newer SYN custody path must preserve them without silently reusing the legacy Potbot vault as authority. | U06/U01, G3/G5: delegation and scope checks, epoch rollover/clock rollback, revision binding, replay and explicit migration compatibility. |
| MW20 | Protect keys at rest and during use, preserve AES-256 protection requirements and recovery; plan TPM/HSM integration. | Legacy broker and Python SYN have different custody profiles. Python keyring use exposes transient secret bytes; it does not establish locked/zeroized memory or hardware non-exportability. | U06/U16, G5: explicit memory-exposure assessment, idle-state sealing/locked-memory requirements or documented unsupported profile, real OS custody tests; TPM/HSM remains future until qualified. |
| MW21 | Support safe restart, partial-install reconciliation, backup/recovery, rotation, revocation and retention. | SYN 44–48 intentionally block partial installs and uncertain effects; comprehensive live lifecycle and device replacement gates remain. | U01/U06/U16, G4/G5/G7: recover without identity replacement, replay-window loss or silent destructive retries. |
| MW22 | Reconcile durable identity/enrollment records with the roadmap's PostgreSQL-authoritative policy. | **Unresolved policy/integration gap:** SYN 41/43/47/48 use private SQLite journals; legacy identity replay also uses SQLite. They must not be reported as PostgreSQL implementations. | U00/U01/U06, G7: inventory ownership, migrate authoritative records or explicitly constrain non-authoritative local caches; preserve private key custody and a single authority per record. |

### Exact identity invariants

| Item | Value to retain |
|---|---|
| New operator entropy | 32 bytes / 256 bits from OS CSPRNG or an equivalently qualified hardware source; not hardware identifiers or timestamps. |
| Recovery / derivation | Original entropy represented by a checksum-validated 24-word English BIP-39 phrase; CIP-3 Icarus with empty spending passphrase and Ed25519-BIP32 V2. The separate BIP-39 64-byte seed is not substituted for the original entropy. |
| Operator root | `m/6969'/420'` — both steps hardened. |
| Stable operator base | `m/6969'/420'/0'/0/0`. |
| Installation identity | `SHA256(raw 32-byte base public key)`; not root hash, extended key bytes or a three-byte label. Public verification keys accompany signatures where required and must match the independently pinned identity. |
| Hardware inheritance | `I = uint24_be(SHA256(raw_signed_hardware_id \|\| operator_byte)[:3])`; the operator byte is one octet in 0–255. |
| Agent choices | `m/6969'/420'/I'/N/0`, with `N=1..8`; agent chooses the independence index. The inherited index is hardened; the final two indices are not. |
| Temporary key role | Independently generated Ed25519 key proves the pledge/selection signer. Its public-key hash never supplies inheritance or hardware attestation. |
| Short labels | First three bytes of each full candidate public-key SHA-256, all eight distinct; no authorization or global uniqueness claim from a short label. |
| Existing hourly profile | Base leaf followed by hardened `1'/E'`, `E=floor(POSIX_seconds/3600)`, `0 <= E <= 2^31-1`; broker selects epoch, with base-signed delegation and scoped signatures. Map agent identities to a versioned compatible signing profile rather than inventing an unrecorded derivation path. |

The operator can rederive a selected child from its root. A child signature proves possession; it cannot prove exclusive agent control, hardware non-exportability or protection from a compromised operator. Qualify the independent agent's protected installation/use without making that impossible exclusivity claim. Never transfer the operator root/base, parent keys, other candidates or parent chain codes; Phase 47 transfers only the selected leaf material and its leaf chain code.

## 4. Required acceptance gates and next work

### G1 — Complete monitored worker execution (U03-Q)

Inventory executable Cog routes and distinguish trusted host controllers. Require a registered agent/UID/GID, valid lease, pinned admitted executable, qualified image, active collector and operator-private audit destination before work begins. Exercise real Bubblewrap/cgroup v2/TASKSTATS on Linux and each supported WSL2 deployment, including forks/threads/exec, short tasks, PID reuse, concurrent agents, CPU hotplug where supported, trace loss/overflow, cancellation and orphan cleanup. Missing final task records or unconfirmed stopped descendants cannot yield a successful completion receipt.

Include namespace-escape, mount, UID and cross-agent file-access isolation probes. Concurrent-load tests must preserve existing exclusive workspace leases, testing rejection where simultaneous execution is inadmissible.

Run the existing operator and registered-worker qualification runners on the actual target host, retain evidence bound to commit, image, binary hashes, kernel and capabilities, and repeat after relevant changes. The registered-worker 46 runner's sequential agent smoke is not evidence of concurrent isolation. Resolve nested-work fixture/provenance gaps identified in sandbox-launch 47 and browser-provenance 48. Preserve the optional lower-overhead collector plan and measure forensic completeness and overhead before changing collector profiles.

### G2 — Independent sandbox SYN lifecycle (U03-S)

Bind the independently installed Python SYN runtime to each agent installation and actual worker lifecycle. Pin source/build, launch within the qualified UID/workspace boundary, keep operator root custody outside guest mounts, and verify live process identity as well as authenticated heartbeats. Preserve workspace isolation, incarnation/sequence validation, missed-heartbeat handling, bounded status and cancellation/restart/replay semantics. The in-operator actor and the separate LAN pledge listener are useful components but neither closes this gate by itself.

### G3 — Exact identity and compatibility (U06-I)

Test real pinned derivation/signing/recovery against independently checked vectors, including all eight choices and inheritance boundaries. Verify stable base identity, the hardware-byte formula, collision handling, signed candidate selection, hourly rollover/delegation, allowed scope/types and Ember revision bindings. A fake HD wallet, skipped optional provider test or successful schema validation is not cryptographic qualification. Preserve historical signed v1 bytes; use v2 for new enrollment. Fingerprint-preserving migration must not turn a legacy file vault into an implicit substitute for SYN custody.

### G4 — Approved intake through adoption and activation (U06-E)

Implement the missing SYN-owned bridge from Phase 48's encrypted private inbox to approved, fenced Phase 41 selected-state records and Phase 47's grant/request/seal/install/prove/confirm flow. Revalidate the original signed offer, owner decision, attested hardware source, operator-byte reservation, exact signed candidates and original signed selection at the required boundaries. Retain those signed transcripts durably inside SYN, with bounded quotas, expiry and private metadata handling.

Keep `pending`, `offered`, `selected`, `adopted` and execution authorization distinct. Reconcile crash or disconnection at every boundary before retrying a possibly completed effect. After a signed adoption receipt, independently verify the agent installation and grant only scoped, expiring execution authority under operator policy. Test denial, duplicate delivery, replay, concurrent offers, changed owner/device, locked keystore and revoked authority. This is the next concrete integration work associated with the unverified Phase 49 request; it is not evidence that Phase 49 has already been implemented.

### G5 — Custody, recovery and real network qualification (U06-K)

Qualify native Secret Service / Credential Manager / Keychain only on platforms actually claimed, with process restart, locked stores, permission failures, partial writes, backup/restore, revocation/rotation and device replacement. Keep Python transient-memory limitations and the stronger planned AES-256 memory/idle-sealing, locked-memory and TPM/HSM profiles explicit; a software keyring or encrypted transport is not proof of those protections. Avoid imposing a new Rust implementation on the Python SYN service.

Run actual separate-host LAN tests for disconnection, skew/rollback, rebroadcast, hostile concurrent agents and host quotas. Define trusted hardware-ID collection/attestation and clone/spoof/replacement handling. MAC addresses and temporary-key signatures alone cannot establish physical-device identity. Keep raw hardware metadata and keys out of broadcasts, model context, general logs and public receipts. Preserve recovery/retention controls without erasing replay or custody evidence.

### G6 — Exact monitoring UI and complete attribution (U03-V)

Verify the 60 BPM pulse, 180-second time-weighted brightness, separate 2-second surge signal and accessible reduced-motion/offline/warm-up behavior using real backend telemetry. Expose every agent's contribution through an appropriate complete view, while retaining bounded summaries if useful. Define CPU, memory and I/O attribution and denominators; distinguish host saturation, share of busy CPU and other resource measures. Do not represent the current top-eight CPU-only component as complete total-system attribution. Test multiple agents under one principal, more than eight agents, concurrent work, incomplete monitoring and cross-workspace filtering.

### G7 — Durable storage reconciliation (U06-D)

Inventory the existing enrollment, public-receipt, handoff, encrypted-inbox and legacy replay journals and their ownership. Reconcile authoritative durable state with PostgreSQL, retaining SYN as the sole identity/custody authority and OS keystores for secret keys. Any retained local cache/journal needs an explicit non-competing authority, durability and recovery contract. Do not claim the existing SQLite paths satisfy PostgreSQL integration.

Preserve signed bytes, unique hardware/offer/inheritance reservations, fencing epochs, encrypted metadata, immutable receipts and uncertain-outcome handling across migration and rollback. Test concurrent writers, outages, partial migrations, restart/replay and backup restoration against a real database. Migration must not place private keys or unredacted hardware metadata into ordinary Potbot tables or public telemetry.

## 5. Closure and verification scope

The roadmap mapping is closed only when MW01–MW22 remain assigned to the work packages above, with these source links and gates retained. Product completion still requires actual implementation and deployment evidence. Track each gate with commit/build, host/profile, command/job, result, retained report and unresolved limitations; absence of evidence remains unverified.

This change is documentation-only. Validate patch application against the pinned source, Markdown/local-path references against the repository tree, source blob identity and preservation of the original tool-stage definitions. Existing runtime suites and `scripts/check_tool_plan.py` remain their respective implementation/plan gates; do not report a full repository or live-host pass from this targeted document check.
