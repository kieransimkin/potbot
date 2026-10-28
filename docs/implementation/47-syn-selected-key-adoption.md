# Phase 47 — SYN Python encrypted inherited-key adoption

**Status:** implemented host-local confidential handoff, durable one-use operator
reservation, actual agent-owned OS credential installation, Phase-42-compatible
signed proof/receipt and operator/agent CLI. Offline qualification is passing;
mandatory real PyCardano/Ed25519-BIP32 V2 end-to-end tests are in GitHub CI.
**Not a completed P2P enrollment, production hardware attestation or hardware
non-exportability certification.** No keys are issued from Potbot or a workspace
sandbox, and SYN's existing public Unix socket gains **no write/sign/export API**.

## Exact existing identity hierarchy

| Key | HD path |
|---|---|
| SYN operator root | `m/6969'/420'` |
| Stable operator base | `m/6969'/420'/0'/0/0` |
| Selected inherited agent leaf | `m/6969'/420'/inheritance'/independence/0` |

`inheritance` is the big-endian 24-bit SHA-256 prefix over the raw **signed
hardware ID bytes followed by the operator's byte**, **not** the temporary key
hash. `independence` is the selected 1–8 candidate index. The operator
installation is identified by SHA-256 of the raw base public key, not the root
private key, an extended public key, or a three-byte abbreviation.

The selected *leaf* Ed25519-BIP32 extended private key and its *leaf* chain
code are sealed for the agent. No operator root, base, parent xprv, parent
xpub or parent chain code is ever transmitted. The operator **retains the
ability to rederive the selected leaf from its root**. This protocol cannot
claim exclusive private-key control by the agent, a hardware-backed device
identity, or security against a compromised operator.

## Authoritative, non-bypassable admission

SYN's `SelectedPledgeAuthorizer` reads the operator-private Phase-41
`SynEnrollmentLedger` SQLite record, **read-only**, and requires `state=selected`,
the pinned master/network, the recorded temporary key, inheritance,
independence and full selected public-key hash, and an unexpired transaction.
It checks both independent signatures:

1. The operator's base key on the exact eight candidate hashes and pledge
   digest (`syn.agent.candidates.v2` domain).
2. The original agent's temporary key on the selected candidate index,
   candidate-offer digest and short public-key hash
   (`syn.agent.independence.v2` domain).

Only after those checks does SYN derive the selected public key again from the
real operator root and compare its 32-byte SHA-256 hash. The selected agent's
**signed selection packet** is an additional host-private input: Phase 41's
journal persists the selection and candidate data but **not the agent's original
selection signature**, so that signed packet must be retained when received.
A plaintext `"approved":true` or arbitrary file containing just an inheritance
number can never mint a signed grant. A fresh authorization lookup is also
required when sealing the key and when issuing the adoption receipt.

### Handoff and privacy

1. **Operator grants:** With explicit local controlling-terminal confirmation,
   operator SYN signs a time-limited one-use grant bound to the saved pledge,
   selected index and full key hash, agent temporary public key, signed
   selection SHA-256, operator identity and a fresh 32-byte challenge. The
   operator's private SQLite handoff journal reserves that offer.
2. **Agent requests:** Agent SYN verifies the operator grant, creates an
   independent OS-random X25519 transfer key, stores its private half in its
   native OS credential service, and signs the request with its **separate
   temporary Ed25519 key**. No hardware identifier is sent in this step.
3. **Operator seals:** The operator rechecks selected authority, durably
   reserves the request (no repeat or uncertain retry), derives exactly the
   chosen leaf in SYN-owned Python, and X25519/HKDF-SHA-256/AES-256-GCM-seals
   the leaf xprv/public key/chain code to the signed agent transfer key.
   Header and ciphertext receive an operator base-key signature. Only the
   ciphertext, never plaintext keys, is returned.
4. **Agent installs:** Agent SYN verifies master/grant/request/seal signatures,
   X25519 and authenticated encryption, checks the selected leaf's public-key
   identity and independently signs a challenge before any keystore write.
   With an OS-enforced lock, it writes leaf key slots and an **end-of-install
   manifest last**, reading every slot back. A partial or existing installation
   **blocks** re-enrollment; it is not overwritten or silently deleted.
5. **Agent proves:** The installed leaf signs a Phase-42-compatible selected-
   key proof, including operator challenge, pledge, full public key, inheritance
   and independence. The proof contains no secret bytes.
6. **Operator confirms:** SYN verifies the independently signed key proof,
   derives an authenticated signed adoption receipt, writes it to its immutable
   `PublicAdoptionJournal`, and marks its private handoff journal confirmed.
   Crash between these two writes is reconcilable by identical signed proof;
   conflicting receipts are rejected. This is **still not a production
   hardware custody or agent activation qualification**.

All identities use full 32-byte public hashes in authority decisions; the
three-byte labels are UI abbreviations only. Public frames are strict
versioned canonical JSON, domain separated, bounded and retained only in
owner-private operator/agent directories. The raw hardware identifier is
never recorded in the handoff journal or sent in grant/request/envelope/proof.

## Host-local CLI (after Phase 41 has selected the agent)

Install the independent Python SYN wheel with the pinned HD optional extras:

```bash
python -m pip install './syn-node[hd]'
```

Prepare operator-private `0700` directories, `0600` canonical JSON documents,
SYN's Phase-44 **operator-mode** configuration, a Phase-41 private SQLite
ledger and the exact signed agent selection packet. Run **from the protected
operator host, not an agent sandbox**. The operator must explicitly type the
approval phrase on its controlling terminal. These are schematic commands;
replace all sample paths and identities with the real pinned ones.

```bash
syn-adopt operator-grant --config /private/operator.json \
  --phase41-ledger /private/phase41.sqlite \
  --signed-selection /private/selection.json --network-id syn-local \
  --handoff-ledger /private/handoffs.sqlite \
  --offer-sha256 "$OFFER_SHA256" --output /private/grant.json

# In the independently installed agent-mode SYN environment:
syn-adopt agent-request --keyring-service syn.agent \
  --keyring-account "$AGENT_ID" --lock-dir /private/agent-locks \
  --expected-master-hash "$OPERATOR_BASE_HASH" \
  --grant /private/grant.json --output /private/request.json

# Back at the operator, after receiving the signed request:
syn-adopt operator-seal --config /private/operator.json \
  --phase41-ledger /private/phase41.sqlite \
  --signed-selection /private/selection.json --network-id syn-local \
  --handoff-ledger /private/handoffs.sqlite \
  --grant /private/grant.json --request /private/request.json \
  --output /private/sealed.json

# Agent SYN receives and verifies the sealed ciphertext:
syn-adopt agent-adopt --keyring-service syn.agent \
  --keyring-account "$AGENT_ID" --lock-dir /private/agent-locks \
  --expected-master-hash "$OPERATOR_BASE_HASH" \
  --grant /private/grant.json --request /private/request.json \
  --envelope /private/sealed.json --output /private/proof.json

# Operator finalizes, with an immutable public receipt journal:
syn-adopt operator-confirm --config /private/operator.json \
  --phase41-ledger /private/phase41.sqlite \
  --signed-selection /private/selection.json --network-id syn-local \
  --handoff-ledger /private/handoffs.sqlite \
  --grant /private/grant.json --proof /private/proof.json \
  --receipt-journal /private/adoptions.sqlite \
  --output /private/signed-receipt.json
```

All CLI files are create-only; an existing output is never overwritten.
Console status output contains **only public digests and `agent_activation_authorized=false`**.
No key/phrase is accepted in a command-line argument, environment variable,
public SYN socket request or Potbot callback. Transfer packet files contain
sensitive linkable enrollment metadata despite being publicly verifiable:
keep them owner-private and apply retention controls.

## Tests and remaining mandatory gates

- `syn-node/tests/test_inherited_adoption.py` — real X25519/AES-GCM with a
  **test-only fake HD provider**, durable rollback/fencing, host CLI flow,
  live signed Phase-41 SQLite adapter, wrong operator, wrong signer,
  tampering, keystore mid-write interruption and replay.
- `syn-node/tests/test_inherited_adoption_live.py` — **mandatory in the new
  GitHub workflow**: pinned real PyCardano 0.19.2 plus real leaf
  Ed25519-BIP32 signatures at inheritance `0`, `172941`, `16777215`; secure
  X25519/AES-GCM transport, OS-keyring test-double write/readback and verified
  Phase-42 operator receipts. A skipped PyCardano gate is not success.
- JSON Schema `syn-node/schema/inherited-key-handoff-v1.schema.json` validates
  only public signed wire frames, not private key custody or approval.

**Not yet supplied or qualified:** a native discovery/P2P transport for the
operator pulse and encrypted agent pledge; trusted/attested hardware-ID
collection; durable signed-selection retention within SYN proper rather than
its Phase-41 adapter; live native OS keychain/Secret Service integration under
Linux/WSL2/Windows with adversarial failure and backup/rollback tests; scoped
agent process authorization after receipt; device-identity rotation and
revocation; and robust cleanup/reconciliation of incomplete keystore writes.
No mock, successful public signature, or successful CI run establishes those
security properties. Python extended-key material necessarily exists
transiently in process memory; this software-only profile does not claim
hardware enclave protection or secret zeroization guarantees.
