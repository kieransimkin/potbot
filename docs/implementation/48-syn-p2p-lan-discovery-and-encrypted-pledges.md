# Phase 48 — SYN signed LAN pulses, confidential agent pledges and encrypted operator inbox

**Baseline:** merged Potbot Phase 47 (`2685493c2f5c6b600371187730e4867765a3d24d`). **Ownership:** the independently installed, Python-only `syn-node` package. Neither Potbot nor a public SYN API owns a root/base private key or the raw agent hardware identifier. Phase 48 adds *P2P intake*, not unreviewed agent activation.

## Delivered

- **Real IPv4 LAN discovery:** operator SYN signs the original Phase 41 pulse contract `synny://type/syn-operator-pulse-v1` with its base address signing key (`m/6969'/420'/0'/0/0`). It separately signs the LAN endpoint/port and the complete signed pulse under `syn.p2p.advertisement.v1\0`. The UDP payload contains no hardware identifier, key material or agent fingerprint. An agent accepts only advertisements from an *independently pinned* operator SHA-256 base public-key hash and matching network ID. The UDP source must match the signed endpoint. This first transport supports RFC1918 IPv4 addresses; explicit loopback mode is for tests only. No unauthenticated DNS URLs, internet hosts, arbitrary redirects, multicast trust-on-first-use or cross-host peer consensus.
- **Authenticated ephemeral channel:** after a fresh broadcast, the agent opens bounded TCP to the *signed* endpoint. Both parties generate new X25519 keys. The operator signs the full server-hello transcript with the SYN base extended Ed25519-BIP32 V2 key; the agent verifies it against the pinned base public key **before sending its hardware ID**. HKDF-SHA-256 derives independent client/server AES-256-GCM keys and nonces, bound to the pulse, both ephemeral public keys and both nonces. Each connection carries precisely one encrypted agent offer and one encrypted outcome. TCP frame length, connect/read times, session concurrency and pulse validity are bounded. Unknown, stale, malformed or spoofed handshakes fail closed.
- **Phase 41/40 compatible signed hardware pledge:** the encrypted agent message contains the existing `synny://type/syn-agent-pledge-offer-v2`: raw 6–256 byte hardware ID encoded as lowercase hex, temporary Ed25519 public-key hash (only for signature verification), timestamp, target master hash and the operator's signed pulse challenge. SYN signs the canonical body with the *separately generated OS-CSPRNG temporary signing key* in its OS keystore. **Inheritance** remains `uint24_be(SHA256(hardware_id_bytes || operator_byte)[:3])`, but Phase 48 does not invent or reserve an inheritance before hardware attestation/owner approval.
- **SYN-private durable intake:** the operator's inbound message is AES-256-GCM encrypted into a private SQLite journal. Its independent 256-bit encryption key exists in the OS credential backend, not in a Potbot file. The stored handle for a claimed hardware ID is a keyed HMAC, not a raw MAC or predictable `SHA256(MAC)`. The signed offer digest and device handle are individually unique, so a replay or rotated temporary signing key cannot create a second pending claim for the same hardware. Encrypted records authenticate creation/expiry metadata, fail on tampering and remain readable only through the SYN operator's explicit local API. Expired ciphertext and nonce are cleared while the replay digest and opaque device tombstone remain reserved. This local journal is not a multi-host replicated database.
- **Fail-closed initial state:** the only successful reply is `pending-operator-review`; `activation_authorized=false`. The network transport cannot approve servitude, choose the operator's salt octet, issue derived private keys, sign an adoption grant, publish a Phase 42 receipt or unlock a keystore. Phase 47's private, operator-approved `syn-adopt` flow remains a distinct trusted operation after independent verification. No SYN/Potbot private-key export or root-key access method has been added to the public socket.
- **Installation and qualification:** a `syn-p2p` CLI exposes explicit operator inbox creation, operator signed broadcast and TCP listening, read-only pending digest listing and agent pulse discovery+pledge. The Python package version is `0.1.0a5`. A dedicated GitHub Actions gate tests real sockets, AEAD, replay/disk tampering, schema and a genuinely derived PyCardano operator base key. The operating-system keychain/Secret Service is still a host-specific gate; disposable credential fakes are used in unit tests, and a skipped real-library test never counts as an actual provider qualification.

## Operator configuration

First run `syn-node new-operator` (Phase 44) with a qualified real OS credential backend and store its `base_public_key_hash` independently. The operator's private v2 config must reference that identity and the actual OS credential service/account. For a private LAN interface, e.g. `192.168.1.22`:

```sh
syn-p2p operator-init \
  --config /var/lib/syn/private/operator.json \
  --inbox /var/lib/syn/private/pledges.sqlite \
  --network-id syn-home \
  --keyring-service syn.p2p --keyring-account operator-1 \
  --lock-dir /var/lib/syn/private/locks

syn-p2p operator-serve \
  --config /var/lib/syn/private/operator.json \
  --inbox /var/lib/syn/private/pledges.sqlite \
  --network-id syn-home \
  --keyring-service syn.p2p --keyring-account operator-1 \
  --bind-host 192.168.1.22 --advertised-host 192.168.1.22 \
  --tcp-port 45121 --broadcast-host 192.168.1.255 \
  --broadcast-port 45122 --broadcast-period 10
```

Both `operator-init` and `operator-serve` fail if the Python HD base signer or native credential store is absent or mismatched. All operator files/directories and the lock directory must be owner-private, on a qualified local filesystem, outside any agent's guest mount. Do not run this service with an unconfined listener on a public internet interface. Use OS firewall controls and source-network restrictions.

On a separate machine/agent, explicitly pin the operator's full 32-byte SHA-256 base public-key hash from a trusted channel and first run `syn-node new-agent-temporary-key` with its own OS credential store. The hardware-identity source is **a private binary file** of 6–256 bytes created by an authorized SYN host collector; do not pass hardware identifiers in command arguments, environment variables, model prompts, stdout or broadcast messages. A MAC address alone is **not** hardware attestation.

```sh
syn-p2p agent-pledge \
  --network-id syn-home \
  --pinned-master-hash <64-lowercase-hex-operator-base-public-key-hash> \
  --keyring-service syn.agent --keyring-account agent-1 \
  --pinned-temp-hash <64-lowercase-hex-temporary-key-hash> \
  --hardware-id-file /var/lib/syn/private/agent-hardware-id.bin \
  --udp-port 45122 --timeout 10
```

The only returned data is a public offer digest, pending status and `activation_authorized=false`. Operators can list non-sensitive pending offer digests with `syn-p2p operator-list` and the same operator config/inbox/network/keystore parameters as above. The *raw* signed offer is available **only** through the private local SYN `PledgeInbox.read_private` method to an independently authorized hardware-attestation/enrollment handler; there is intentionally no public or normal CLI plaintext-export route.

**Important operator requirement:** the server P2P inbox is not yet automatically wired into the Phase 41 `SynEnrollmentLedger` or Phase 47 `SelectedPledgeAuthorizer`. A received pledge cannot silently become a selected/adopted identity. Additional native SYN operator checks/approval and the already implemented Phase 47 selected-key procedure must occur behind that trust boundary.

## Wire protocol and security

- The public advertisement includes the immutable v1 signed pulse, endpoint host/port and its own operator signature. Pledge binding retains Phase 41's signed-byte rules exactly: canonical ASCII JSON with sorted keys, and `b"syn.agent.pledge.offer.v2\0"` domain separation.
- Operator base and root derivation paths are unchanged: `m/6969'/420'/0'/0/0` and `m/6969'/420'`; inheritance candidates are `m/6969'/420'/I'/1/0` through `/8/0`. A three-byte hash is never a trusted identity. All remote operator identification uses the **full** 32-byte hash and full Ed25519 signature verification.
- TCP client hello carries fresh X25519 public key, nonce and exact advertised-pulse commitment. Server hello carries its own fresh X25519 public key/nonce plus an operator base-key signature binding the request. Each side independently derives directional AEAD key/nonce pairs and validates the transcript. No private key or hardware identifier appears in plaintext frames. No unsigned error detail is returned to untrusted peers.
- Broadcast pulses are intentionally shareable: distinct agents may answer one signed pulse. The **offer transcript** and OS-keystore-blinded hardware identity are uniquely reserved, rather than globally consuming the pulse challenge.
- A private inbox is create-once. A failed keyring write, partial SQLite creation, missing keyring entry, owner/mode/symlink mismatch, corrupted ciphertext, clock rollback, replay or duplicate hardware ID fails closed; no fallback plaintext storage, key regeneration, identity activation or destructive auto-cleanup.
- Each inbox permits at most 4,096 pending offers and 65,536 total reserved digests. Operator maintenance/recovery is required when quotas are exhausted. UDP scanning inspects at most 64 packets per discovery; the listener processes at most 16 concurrent TCP connections, additionally limits unauthenticated handshakes per source IP/global minute, and imposes four-second I/O deadlines. This is bounded *ingress*, not an independently qualified DDoS mitigation system.

## Tests and still required

Run offline in the standalone package:

```sh
PYTHONPATH=syn-node/src python -m pytest -q --confcutdir=syn-node/tests \
  syn-node/tests/test_p2p.py
```

Run the actual provider test only in the isolated Python HD environment with pinned `pycardano==0.19.2`, `cbor2==5.8.0`, `cbor2pure==5.8.0` and the security dependencies. The dedicated `syn-p2p-lan.yml` workflow installs and runs it on Linux with fresh socket paths.

Before enabling fully automatic P2P servitude, complete: (1) operator-approved hardware-ID source selection and device attestation (MAC spoofing and virtual machine clones are not solved here); (2) a qualified native SYN enrollment handler that transforms encrypted private inbox entries into independently approved, fenced selected-state records; (3) securely authenticated owner/user approval and request/rate/CPU quotas on the real host; (4) a review of OS-keyring lifecycle, memory exposure, key backup/rollback, root-key custody and host network security; (5) actual cross-host LAN, disconnection, clock skew, router rebroadcast and concurrent hostile-agent tests; and (6) strict handling of adoption and revocation, including operator/agent restart and confirmed cleanup. A local loopback test or simulated OS credential backend is not production certification.
