# Native Potbot identity broker

This package implements the local password custody tier and public Ed25519 verification-key publication. Python sends public requests to a short-lived native process. The process reads passwords itself and returns an allowlisted public descriptor or signature bundle. It has no private-key export command and no resident unlocked session.

Protected DAG operations now persist an encrypted intent, require an independent
operator reservation, and durably commit/recover the exact event before release.
The operator anchor stays outside Agent backups; missing or mismatched history
holds. Generic signatures cannot bypass the dedicated event domain. See the
[signing and recovery guide](../../docs/implementation/18-protected-dag-signing.md)
for commands, crash ordering, rollback assumptions and bounded storage.

![Prepare, independent operator reservation and durable commit protect DAG events after Agent vault restore](../../docs/media/syn-dag-signing.svg)

*Generated implementation illustration; it is not a live consensus screenshot.*

The public contracts are `potbot-identity-v1`, `potbot-hourly-signing-v1`, descriptor version 2, delegation version 1, envelope version 2, and bundle version 1. The private storage format is **`potbot-native-password-v1`**, version 1. It is deliberately a separate profile from the larger design-only vault format: one password slot is implemented; hardware credentials, passkeys, resident vault handles and their qualification are not claimed.

## Build and checks

Use the pinned Rust toolchain in `rust-toolchain.toml` and the committed dependency lock:

```sh
cargo fmt --check
cargo test --locked --release --lib
cargo build --locked --release
cargo check --locked --target x86_64-pc-windows-gnu
```

The Windows check requires the corresponding `rustup target add` command. Cross-compilation checks Rust types and Windows API declarations; it does not qualify Windows runtime locking, console handling, ACLs or filesystem crash durability. Linux and Windows need their own host acceptance evidence.

The release executable is `target/release/potbot-identity` on Linux and `target/release/potbot-identity.exe` on Windows. A source checkout can also build it through Potbot's Python identity CLI. The surrounding application publishes the native Rust source, Python verifier source, `Cargo.toml`, `Cargo.lock` and `rust-toolchain.toml` as inherited implementation Embers. These are inspectable source files and pinned, rebuildable inputs; they do not include the compiler's source code or promise identical binaries across build environments. Semantic signing requires the inherited Ember reference/revision set to match the selected local sources before invoking the broker. This source-content policy does not attest the selected executable or replace a security review and host qualification.

## Memory and process boundary

The production binary registers a guarded native allocator for **every Rust heap allocation**, including provider allocations. Pages are locked before the allocator hands them to callers, and the entire usable allocation is overwritten before release. Argon2 uses one explicitly allocated 65,536-block workspace, exactly 64 MiB, through the provider's `hash_password_into_with_memory` API. The workspace is zeroized before release.

Sensitive work runs on a separate native stack. Linux locks that stack, excludes it and heap pages from ordinary core dumps, sets the process non-dumpable, and disables core files. Windows locks the committed stack pages and converts stack-growth guards to fixed no-access boundaries, so growth cannot silently introduce unlocked pages. Windows heap allocation uses `VirtualAlloc`/`VirtualLock`; the broker may increase its own working-set limits to accommodate the required locks. Lock failure is fatal; there is no production switch to disable these protections.

The Linux implementation currently requires 4 KiB pages. `preflight` requires a soft `RLIMIT_MEMLOCK` of at least **100,663,296 bytes (96 MiB)** and probes a locked 64 MiB mapping. An administrator or service manager must configure a sufficient limit before launching the broker. A shell cannot raise its hard limit without the appropriate authority. Windows must successfully lock the corresponding pages. A successful preflight is a memory capability check; it is not an assertion that a supplied vault directory is private or the whole machine has passed a security audit.

There is no long-running unlocked broker. Init, sign, recovery and password rotation each run in a new process and exit after the operation. Decrypted wallet state is not retained between operations. The design's resident AES-256 idle-memory sealing and session-handle APIs are **not** implemented by this one-shot profile. New password input is collected before decrypting an existing wallet. Operating-system administration, same-user process compromise, hibernation and external privileged dump tools remain outside the guarantees of these checks; Windows crash-report behavior still requires host qualification.

## Identity and published keys

Initialization draws exactly 32 bytes from the OS CSPRNG. Recovery uses the 24-word English BIP39 representation of those original bytes. The derivation password is always empty and is unrelated to the vault password.

| Component | Fixed rule |
| --- | --- |
| Root derivation | CIP-3 Icarus: PBKDF2-HMAC-SHA-512, empty derivation password, original entropy as salt, 4,096 iterations, 96 output bytes, Icarus bit normalization |
| Base path | `m/6969'/420'/0'/0/0` under Ed25519-BIP32 V2 |
| Stable identity | Lowercase hexadecimal SHA-256 of the raw 32-byte base verification key |
| Hourly child | Base path followed by `/1'/E'`, where `E = floor(POSIX_seconds / 3600)` |
| Published material | Raw 32-byte base and hourly verification keys, padded standard base64, certificates and signatures |
| Private material | Original entropy, mnemonic, root/base/hourly private keys, derivation chain codes, password-derived factors and wrapping keys |

An hourly key cannot be derived publicly because both additional indices are hardened. Instead, the base key signs a domain-separated delegation certificate that contains the hourly key, stable signer hash and exact hourly validity interval. The hourly key signs the message envelope. Publishing the base verification key does not change the stable signer hash.

The provider is pinned to `ed25519-bip32` 0.5.0. That release removes formatting implementations for private keys and strengthens its private-key memory wiping. `Cargo.lock` fixes Cryptoxide 0.6.6, the newest compatible 0.6 release for the pinned Rust 1.90 toolchain at implementation time; 0.6.7 requires Rust 1.95. The code implements protocol composition, not new elliptic-curve, AES, KDF or hash algorithms. The registry-checksummed 0.6.7 release was reviewed: its changelog describes an ML-DSA prehash refactor, Rust edition/MSRV changes and removal of a SHA-3 allocation; the relevant Ed25519/curve source changes reorder imports, and SHA-2 SIMD edits add explicit unsafe scopes for the new edition. It reports no relevant Ed25519 or SHA-2 security fix. This package's PBKDF2 implementation is supplied by the separate RustCrypto crate.

## Encrypted vault

The closed `Vault` and private-state structures in `src/lib.rs` are the executable format definition. Unknown fields and recursive duplicate JSON keys are rejected.

Each snapshot has a new random 32-byte data key and 12-byte nonce. AES-256-GCM encrypts the private state with a full 16-byte tag. All public vault headers, slot metadata, wrapped keys and the nonce are authenticated as additional data. An independent random 32-byte vault key wraps the data key using AES-256-KW. The password slot protects the vault key with:

1. Argon2id v19, 65,536 KiB, three passes, four lanes, 16-byte random salt, 32-byte result.
2. HKDF-SHA-256 with an independent 32-byte salt and domain-separated context bound to the identity, vault, key epoch and slot.
3. AES-256-KW wrapping of the vault key.

Password change creates a new vault key, key epoch, slot identifier, salts, data key and nonce. The previous password cannot unlock the replacement snapshot. Old encrypted backups remain unlockable with their old password; replacing a local file cannot revoke a previously copied backup.

On unlock, the broker authenticates and decrypts the entire state, reconstructs the root/base keys from the original entropy and compares the stored key material and public identity. A caller-supplied, independently pinned signer hash must match before any private operation proceeds. The password is never an identity-derivation input.

Linux storage requires a current-user-owned private parent directory, no group/other permissions, regular files with mode 0600, no symlinks and no hard-linked vault files. Windows creates an owner-only protected directory DACL and rejects reparse points and ACL entries that grant another SID access. All operations hold an exclusive native lock. Snapshots are written to a fresh private temporary file, flushed, atomically replaced, and made durable before returning a signature. Linux also fsyncs the containing directory; Windows uses `MoveFileExW` with `MOVEFILE_WRITE_THROUGH` and requires runtime filesystem qualification.

## Commands

All filesystem arguments are absolute paths. The final vault directory can be absent: the native broker creates it with private permissions. Its parent must already exist. Do not pre-create the final Windows vault directory with a broad inherited DACL.

```text
potbot-identity preflight
potbot-identity init --vault ABS_PATH --policy ABS_PATH
potbot-identity public --vault ABS_PATH [--expect-signer HASH]
potbot-identity sign --vault ABS_PATH --expect-signer HASH
potbot-identity recovery --vault ABS_PATH --expect-signer HASH
potbot-identity recover --vault ABS_PATH --policy ABS_PATH --expect-signer HASH
potbot-identity change-password --vault ABS_PATH --expect-signer HASH
potbot-identity backup --vault ABS_PATH --output ABS_PATH
```

`init`, `public` and `recover` return only a public identity descriptor. `sign` reads bounded public JSON from stdin and returns a public signature bundle. The other lifecycle operations return `{schema_version:1, operation, status:"complete", identity}`. `backup` copies existing encrypted bytes; it cannot authenticate their private contents without an unlock.

The password prompt uses the controlling terminal directly. No password, recovery phrase, private key or entropy is accepted in command arguments, environment variables, public JSON stdin or stdout. New passwords must contain 12–1,024 UTF-8 bytes without control characters and are confirmed. Linux `init` and `sign` additionally support `--password-fd N` for an already-open private descriptor at least 3, with one newline-terminated password per native prompt. This option is useful for a native protected supervisor and the disposable acceptance test. The Python wrapper does not offer a password argument or read plaintext secrets.

`recovery` requires the local confirmation `SHOW RECOVERY` and the vault password. It writes the private recovery words directly to the controlling terminal, never stdout. `recover` reads 24 words locally, requires the existing independently recorded signer hash, and creates a new encrypted vault under a new password and explicitly supplied policy. It does not overwrite an existing vault or restore old replay/high-water state. Recovery must therefore be treated as an operator action, with replay protection also enforced by recipients.

`public` checks the descriptor's key hash and the public header format. It does not decrypt the vault or authenticate its ciphertext. Pass `--expect-signer` or validate the descriptor against a previously trusted hash in the calling application. A self-consistent key/hash pair alone does not establish an application's trust in that identity.

## Stored signing policy and request

A policy is supplied locally at initialization or recovery and then stored inside the authenticated encrypted state:

```json
{
  "workspace_id": "00000000-0000-4000-8000-000000000001",
  "allowed_audiences": ["example-local-consumer"],
  "allowed_payload_types": ["syn://type/ember"],
  "allowed_domains": ["potbot.event.v2"]
}
```

The public signing request contains exactly:

```json
{
  "workspace_id": "00000000-0000-4000-8000-000000000001",
  "audience": "example-local-consumer",
  "domain": "potbot.event.v2",
  "payload_type_ref": "synny://type/ember",
  "payload_sha256": "64-lowercase-hex-digest-of-the-payload-bytes",
  "request_id": "00000000-0000-4000-8000-000000000002",
  "validity_seconds": 300
}
```

The digest placeholder above must be replaced with an actual SHA-256 digest. `syn://` and `synny://` resolve to the same allowed type and serialize canonically as `synny://`. The request cannot choose an arbitrary derivation path, hourly epoch, issued time, key, signature profile or lifetime over 300 seconds. Only the three domains `potbot.artifact.v2`, `potbot.event.v2` and `potbot.challenge.v2` are recognized, and each still requires explicit inclusion in the stored allowlist. Native input limits are 16 KiB for signing requests, 64 KiB for policy input, and 1 MiB for vault JSON.

The native process chooses wall-clock time and compares wall-clock progress with a monotonic clock during the operation. It rejects a divergence over 30 seconds and any hourly epoch below the stored high-water mark. After policy and clock checks, a signing attempt prunes reservations only when current time is strictly later than their signed expiration plus 30 seconds; equality remains live. It then rejects a duplicate live ID or a new request if 1,024 live reservations remain. The request reservation is durably stored before any public signature is released, so duplicate rejection survives restarting the process. This bounded window permits an ID to be reused after its previous reservation has expired and been pruned. A signature is withheld if the epoch changes before release.

These are **local software controls**. An administrator who can roll back the entire encrypted vault can also roll back its high-water and reservation state. This tier has no hardware monotonic counter, trusted external clock, witness or distributed anti-rollback anchor. Verifiers must maintain their own replay state and pin the expected signer, workspace, audience, domain, payload digest and allowed types.

## Public signing bytes

Every signed public field is an ASCII string or a nonnegative integer; keys sort lexicographically and JSON is compact UTF-8. This restricted form has the RFC 8785 representation. Only `signature` is omitted from the corresponding signed object.

- Delegation: ASCII `potbot-hourly-delegation-v1`, a zero byte, then the canonical certificate.
- Message: ASCII `potbot-signature-v2`, a zero byte, then the canonical version-2 envelope.

All keys and signatures use canonical padded standard base64. Delegation validity is the exact half-open hourly interval. The message's issued time must belong to that interval; its permitted 300-second lifetime can cross the hour boundary. The Python public verifier independently checks these rules using the published raw verification keys.

## Acceptance evidence and remaining gates

Library tests use synthetic secrets and the normal test allocator. They cover the published Icarus vector, encrypted-vault round trips, wrong passwords, authenticated tampering, password rotation, recovery identity, alias/policy checks, hourly signing, replay persistence, epoch regression, private file permissions, exclusive locks and atomic replacement. They do **not** qualify the production allocator or OS custody.

The real process gate is retained at `tests/production_boundary.rs`:

```sh
cargo test --locked --release --test production_boundary -- --ignored
```

It uses the actual release executable, native protected allocator, native stack, random disposable identity, private file descriptor password input, encrypted files and durable signing path. It verifies exported keys/certificates/signatures, private modes, replay rejection, policy rejection, wrong-password rejection and authenticated ciphertext tampering. It asserts successful native preflight first and fails when the host is unqualified. It never substitutes the test allocator or skips a failed host check. An administrator can configure the Linux CI process's locked-memory allowance before this command; no such privileged configuration is embedded in the broker.

The implementation environment had an 8 MiB hard memory-lock limit. The real release executable was built and its correct refusal was observed there. Successful private-operation custody acceptance on a host meeting the 96 MiB requirement remains a separate deployment gate. Windows API cross-compilation is also separate from Windows runtime acceptance. Hardware, passkey, biometric and unattended resident tiers require their own implementations and checks.

## Primary references

- [CIP-3 Icarus derivation and published vectors](https://cips.cardano.org/cip/CIP-3/annex/Icarus).
- [Ed25519-BIP32 provider and changelog](https://github.com/typed-io/rust-ed25519-bip32/blob/master/CHANGELOG.md).
- [RustCrypto Argon2 explicit workspace API](https://docs.rs/argon2/latest/argon2/struct.Argon2.html#method.hash_password_into_with_memory).
- [Microsoft VirtualLock contract](https://learn.microsoft.com/en-us/windows/win32/api/memoryapi/nf-memoryapi-virtuallock).
- [Microsoft thread stack allocation](https://learn.microsoft.com/en-us/windows/win32/procthread/thread-stack-size).
