# Phase 45 — SYN Python identity recovery and real-provider qualification

**Code status:** SYN-owned Python recovery, public functional smoke and regression
repairs implemented. **Production status:** real host Secret Service / Windows
Credential Manager / macOS Keychain certification, hostile-process protection,
complete sealed inheritance and P2P adoption are **not** yet qualified. Potbot
remains a read-only public client; no private operator or agent keys cross its API.

## What changed

- **Compatibility gate:** Phase 44's PyCardano 0.19.2 pulled in `cbor2` 6.1.5,
  incompatible with the simultaneously installed `cbor2pure` 5.8.0
  (`CBORDecodeValueError` is no longer exported). The `syn-node[hd]` extra
  now selects and validates exactly `pycardano==0.19.2`, `cbor2==5.8.0`,
  `cbor2pure==5.8.0`, `mnemonic==0.21`. This is a dependency compatibility
  fix, NOT a cryptographic algorithm change; CI must run the real provider
  with its own dependency environment. A future version bump requires a new
  compatibility and signature qualification gate.
- **Operator BIP-39 recovery:** `operator_recovery_words` and
  `recover_operator_identity` operate only inside SYN and maintain the exact
  Icarus entropy-to-HD mapping. The input is *32 original entropy bytes*, NOT
  the separate 64-byte BIP-39 seed. A 24-word checksum-validated English BIP-39
  phrase is required for recovery. The original installation's independently
  pinned 32-byte base SHA-256 MUST match before any keyring writes.
- **No identity replacement:** restoration requires every OS credential slot to
  be absent. A partially written installation blocks new creation and
  restoration. Both create and recover use the same OS filesystem installation
  lock and write the manifest last. A keystore backend error remains an error;
  **no fallback plaintext vault** and no deletion-based "repair".
- **Private console:** `syn-node show-recovery` and `recover-operator` demand a
  real `/dev/tty`, explicit typed confirmation, and read secrets through a
  no-echo prompt or write them to that controlling terminal. Recovery words
  never appear in structured stdout, environment variables, CLI arguments,
  public receipt JSON or daemon socket commands. Even unknown CLI argument
  error messages redact offending values. Disable session recording while
  explicitly displaying recovery words; ordinary Python strings are not
  guaranteed to be erased from process memory.
- **Public functional check:** `syn-node qualify-operator` signs a 32-byte
  CSPRNG challenge using the existing pinned operator HD base signer, verifies
  the Ed25519 signature and reports only the base-public-key hash and
  `local-software-smoke-only` status. The command does not modify keyring
  records, offer a new signing endpoint or authorize adoption.
- **Socket test reliability:** SYN's Linux test fixture directories are now
  short, owner-private, and disposable, avoiding `AF_UNIX path too long` under
  long GitHub runner paths. The dedicated CI workflow additionally sets an
  explicit short `--basetemp`. Production socket path policy is unchanged.

## Exact key hierarchy

- Root: `m/6969'/420'` (both indexes hardened).
- Base: `m/6969'/420'/0'/0/0`.
- Stable SYN operator installation identity:
  `SHA256(raw 32-byte base Ed25519 public key)`.
- Inheritance: `uint24_be(SHA256(signed raw hardware ID || operator_byte)[:3])`.
  The temporary CSPRNG Ed25519 signing key is **only** for pledge signature
  verification and has no role in inheritance.
- Eight candidate paths: `m/6969'/420'/inheritance'/N/0`, `N=1..8`.
  Their 3-byte hash truncations are public abbreviations, not trusted identities.

## Host commands (SYN-only, Linux/qualified WSL)

Install in SYN's independent Python environment, **never as a Potbot plugin**:

```bash
python -m pip install -e './syn-node[hd]'
```

Create a new operator only after explicitly confirming that no trusted local
operator installation already exists. The directory for `--lock-dir` must
already exist, be absolute, owner-owned `0700`, and reside **outside** every
agent sandbox:

```bash
syn-node new-operator \
  --keyring-service syn.operator --keyring-account operator-one \
  --lock-dir /absolute/operator-private/syn-locks
```

Save the **public** `base_public_key_hash` independently. Export the optional
24-word recovery phrase only with an explicit, private, local controlling
terminal. This command does **not** write the phrase to stdout:

```bash
syn-node show-recovery \
  --keyring-service syn.operator --keyring-account operator-one \
  --expected-base-public-key-hash "<independently-pinned-64-lowercase-hex>"
```

For a genuinely empty destination OS keystore account after independent
operator authorization, enter all 24 words in a local no-echo prompt. The
phrase is **never** supplied using flags, stdin pipes, logs or environment:

```bash
syn-node recover-operator \
  --keyring-service syn.operator --keyring-account recovered-operator \
  --lock-dir /absolute/operator-private/syn-locks \
  --expected-base-public-key-hash "<original-independent-64-lowercase-hex>"
```

The recovered operator must return **exactly the same** root and base public
keys and base SHA-256 as the original. If any destination slot exists, the
operation refuses it even if the old installation was incomplete. Keystore
migration across OS accounts/hosts is a privileged operator operation requiring
an independent backup/restore and access-control review.

A real existing `schema_version=2` SYN operator daemon configuration supports
read-only signing qualification:

```bash
syn-node qualify-operator --config /absolute/operator-private/operator.json
```

This **does not certify hardware-key nonexportability**, filesystem or
process isolation, malware resistance, secure inter-agent transfer, physical
hardware attestation, or authority to activate a new agent.

## Qualification gates

1. Offline: `PYTHONPATH=syn-node/src:src python -m pytest -q syn-node/tests`
   includes intentionally fake HD fixtures *only* to test fail-closed
   orchestration. Fake wallets are not cryptographic evidence.
2. Real pinned provider: `.github/workflows/syn-python-hd.yml` installs the
   exact Python compatibility set, checks actual root/base derivation and
   public signature verification and asserts the BIP-39 all-zero entropy
   24-word checksum vector. No mock may replace these checks; run on a
   verified commit with the appropriate network/dependency environment.
3. Local transport: `.github/workflows/syn-node-service.yml` exercises real
   Linux Unix sockets under a short, private temporary path; other Potbot
   suites also use the short socket fixtures.
4. OS custody: provision an unlocked **genuine** Linux Secret Service,
   macOS Keychain or Windows Credential Manager on an authorized host;
   perform creation, readback, process restart, signer challenge and recovery,
   including locked-keyring, permissions, crash/replay and backup cases. No
   fake keyring qualifies this gate. Linux's POSIX `flock` and `/dev/tty`
   recovery path do not qualify a Windows or macOS implementation.
5. Agent adoption: P2P peer authentication, hardware-ID collection/attestation,
   multi-node replay fencing, secure selected-private-key transfer and proof of
   exclusive agent custody remain **separate future work**.

A clean CI result on this patch is not asserted until a full GitHub Actions run
for the merged commit actually succeeds. The Phase 44 first-run failures are
recorded in workflows `40547166260` and `40547166253`.
