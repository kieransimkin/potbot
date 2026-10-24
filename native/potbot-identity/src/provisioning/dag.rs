//! Protected DAG signatures require an independent operator reservation.
//! Intents disclose only a recoverable encrypted capsule, never an event signature.
use super::*;

const EVENT: &str = "syn.hashgraph-event/2";
const PROOF: &str = "syn-dag-event-proof-v1";
const INTENT: &str = "syn-dag-reservation-intent-v1";
const RECEIPT: &str = "syn-dag-anchor-receipt-v1";
const LEDGER: &str = "syn-dag-anchor-ledger-v1";

fn digest(profile: &str, value: &impl Serialize) -> Result<String> {
    let mut bytes = profile.as_bytes().to_vec();
    bytes.push(0);
    bytes.extend(canonical(value)?);
    Ok(sha256_hex(&bytes))
}
fn sign(key: &XPrv, profile: &str, value: &impl Serialize) -> Result<String> {
    Ok(b64(key
        .sign::<()>(&signed_bytes(
            profile.as_bytes(),
            &serde_json::to_value(value).map_err(|_| Error::Input)?,
        )?)
        .as_ref()))
}
fn sequence(value: &str) -> Result<u64> {
    if value.is_empty()
        || value.len() > 16
        || value.starts_with('0')
        || !value.bytes().all(|c| c.is_ascii_digit())
    {
        return Err(Error::Input);
    }
    value.parse().map_err(|_| Error::Input)
}

fn consensus_subset(value: &Value, depth: usize) -> bool {
    if depth > 16 {
        return false;
    }
    match value {
        Value::Number(n) => n.as_u64().is_some_and(|n| n <= 9_007_199_254_740_991),
        Value::String(s) => s.is_ascii(),
        Value::Array(a) => a.iter().all(|v| consensus_subset(v, depth + 1)),
        Value::Object(o) => o
            .iter()
            .all(|(k, v)| k.is_ascii() && consensus_subset(v, depth + 1)),
        Value::Bool(_) | Value::Null => true,
    }
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DagBody {
    pub profile: String,
    pub context_digest: String,
    pub creator: String,
    pub sequence: String,
    pub self_parent: Option<String>,
    pub other_parent: Option<String>,
    pub timestamp_ms: u64,
    pub proposal: Option<Value>,
}
impl DagBody {
    pub fn validate(&self, node: &str) -> Result<()> {
        let seq = sequence(&self.sequence)?;
        if self.profile != EVENT
            || self.creator != node
            || !hash_valid(node)
            || !hash_valid(&self.context_digest)
            || self.timestamp_ms > (1u64 << 52) - 1
            || (seq == 1) != self.self_parent.is_none()
            || self.self_parent.as_ref().is_some_and(|p| !hash_valid(p))
            || self.other_parent.as_ref().is_some_and(|p| !hash_valid(p))
            || canonical(self)?.len() > 16 * 1024
            || !consensus_subset(&serde_json::to_value(self).map_err(|_| Error::Input)?, 0)
        {
            return Err(Error::Policy);
        }
        Ok(())
    }
    pub fn payload_digest(&self) -> Result<String> {
        digest("syn.hashgraph-event-payload/2", self)
    }
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DagProof {
    pub schema_version: u32,
    pub profile: String,
    pub membership: Membership,
    pub reservation_id: String,
    pub payload_sha256: String,
    pub signature: String,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct UnsponsoredEvent {
    body: DagBody,
    proof: DagProof,
}
impl UnsponsoredEvent {
    fn digest(&self) -> Result<String> {
        digest(EVENT, self)
    }
    fn verify(&self) -> Result<()> {
        self.body
            .validate(&self.proof.membership.agent.signer_hash)?;
        self.proof
            .membership
            .validate(&self.proof.membership.operator.signer_hash)?;
        if self.proof.schema_version != 1
            || self.proof.profile != PROOF
            || !uuid_valid(&self.proof.reservation_id)
            || self.proof.payload_sha256 != self.body.payload_digest()?
        {
            return Err(Error::Identity);
        }
        verify_signature(
            &self.proof.membership.agent,
            PROOF.as_bytes(),
            &self.proof,
            &self.proof.signature,
        )
    }
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Capsule {
    pub nonce_b64: String,
    pub ciphertext_b64: String,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReservationIntent {
    pub schema_version: u32,
    pub profile: String,
    pub membership: Membership,
    pub reservation_id: String,
    pub context_digest: String,
    pub sequence: String,
    pub self_parent: Option<String>,
    pub timestamp_ms: u64,
    pub payload_sha256: String,
    pub event_digest: String,
    pub capsule: Capsule,
    pub signature: String,
}
impl ReservationIntent {
    pub fn verify(&self, issuer: &str) -> Result<()> {
        self.membership.validate(issuer)?;
        let seq = sequence(&self.sequence)?;
        let ciphertext = STANDARD
            .decode(&self.capsule.ciphertext_b64)
            .map_err(|_| Error::Input)?;
        decode::<12>(&self.capsule.nonce_b64)?;
        if self.schema_version != 1
            || self.profile != INTENT
            || !uuid_valid(&self.reservation_id)
            || !hash_valid(&self.context_digest)
            || !hash_valid(&self.payload_sha256)
            || !hash_valid(&self.event_digest)
            || self.timestamp_ms > (1u64 << 52) - 1
            || (seq == 1) != self.self_parent.is_none()
            || self.self_parent.as_ref().is_some_and(|p| !hash_valid(p))
            || !(16..=32 * 1024).contains(&ciphertext.len())
            || b64(&ciphertext) != self.capsule.ciphertext_b64
        {
            return Err(Error::Policy);
        }
        verify_signature(
            &self.membership.agent,
            INTENT.as_bytes(),
            self,
            &self.signature,
        )
    }
    fn aad(&self) -> Result<Vec<u8>> {
        canonical(
            &json!({"profile":INTENT,"membership_sha256":sha256_hex(&canonical(&self.membership)?),
            "reservation_id":self.reservation_id,"context_digest":self.context_digest,
            "sequence":self.sequence,"self_parent":self.self_parent,"timestamp_ms":self.timestamp_ms,
            "payload_sha256":self.payload_sha256,"event_digest":self.event_digest}),
        )
    }
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AnchorReceipt {
    pub schema_version: u32,
    pub profile: String,
    pub intent: ReservationIntent,
    pub signature: String,
}
impl AnchorReceipt {
    pub fn verify(&self, issuer: &str) -> Result<()> {
        self.intent.verify(issuer)?;
        if self.schema_version != 1 || self.profile != RECEIPT {
            return Err(Error::Policy);
        }
        verify_signature(
            &self.intent.membership.operator,
            RECEIPT.as_bytes(),
            self,
            &self.signature,
        )
    }
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DagEvent {
    pub body: DagBody,
    pub proof: DagProof,
    pub reservation: AnchorReceipt,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DagHead {
    pub context_digest: String,
    pub sequence: String,
    pub event_digest: String,
    pub timestamp_ms: u64,
}
fn next(head: Option<&DagHead>, intent: &ReservationIntent) -> Result<()> {
    let seq = sequence(&intent.sequence)?;
    if let Some(head) = head {
        if seq != sequence(&head.sequence)? + 1
            || intent.self_parent.as_ref() != Some(&head.event_digest)
            || intent.timestamp_ms <= head.timestamp_ms
        {
            return Err(Error::Replay);
        }
    } else if seq != 1 || intent.self_parent.is_some() {
        return Err(Error::Replay);
    }
    Ok(())
}
impl AgentUnlocked {
    fn capsule_key(&self, reservation: &str) -> Result<Zeroizing<[u8; 32]>> {
        let mut key = Zeroizing::new([0u8; 32]);
        Hkdf::<Sha256>::new(Some(b"syn-dag-recovery-v1"), self.account.as_ref())
            .expand(reservation.as_bytes(), key.as_mut())
            .map_err(|_| Error::Protection)?;
        Ok(key)
    }
    /// Caller fsyncs the vault before emitting this intent. No event signature escapes.
    pub fn prepare_dag(
        &mut self,
        vault: &mut AgentVault,
        body: DagBody,
    ) -> Result<ReservationIntent> {
        body.validate(&vault.membership.agent.signer_hash)?;
        let payload = body.payload_digest()?;
        if let Some(p) = self
            .state
            .dag_pending
            .iter()
            .find(|p| p.context_digest == body.context_digest)
        {
            if p.payload_sha256 != payload {
                return Err(Error::Replay);
            }
            return Ok(p.clone());
        }
        if self.state.dag_pending.len() >= 16 {
            return Err(Error::Replay);
        }
        let id = uuid()?;
        let mut proof = DagProof {
            schema_version: 1,
            profile: PROOF.into(),
            membership: vault.membership.clone(),
            reservation_id: id.clone(),
            payload_sha256: payload.clone(),
            signature: String::new(),
        };
        proof.signature = sign(&self.account, PROOF, &proof)?;
        let event = UnsponsoredEvent {
            body: body.clone(),
            proof,
        };
        let mut intent = ReservationIntent {
            schema_version: 1,
            profile: INTENT.into(),
            membership: vault.membership.clone(),
            reservation_id: id,
            context_digest: body.context_digest,
            sequence: body.sequence,
            self_parent: body.self_parent,
            timestamp_ms: body.timestamp_ms,
            payload_sha256: payload,
            event_digest: event.digest()?,
            capsule: Capsule {
                nonce_b64: String::new(),
                ciphertext_b64: String::new(),
            },
            signature: String::new(),
        };
        next(
            self.state
                .dag_heads
                .iter()
                .find(|h| h.context_digest == intent.context_digest),
            &intent,
        )?;
        let key = self.capsule_key(&intent.reservation_id)?;
        let nonce = random::<12>()?;
        let mut plaintext = Zeroizing::new(canonical(&event)?);
        let cipher = Aes256Gcm::new_from_slice(key.as_ref()).map_err(|_| Error::Protection)?;
        let tag = cipher
            .encrypt_in_place_detached(
                Nonce::from_slice(nonce.as_ref()),
                &intent.aad()?,
                &mut plaintext,
            )
            .map_err(|_| Error::Protection)?;
        plaintext.extend_from_slice(tag.as_ref());
        intent.capsule = Capsule {
            nonce_b64: b64(nonce.as_ref()),
            ciphertext_b64: b64(&plaintext),
        };
        intent.signature = sign(&self.account, INTENT, &intent)?;
        intent.verify(&vault.membership.operator.signer_hash)?;
        self.state.dag_pending.push(intent.clone());
        self.reseal(vault)?;
        Ok(intent)
    }
    /// Exact recovery from the external receipt works even after a pre-prepare vault restore.
    /// Caller fsyncs the resulting vault BEFORE emitting the recovered event.
    pub fn commit_dag(
        &mut self,
        vault: &mut AgentVault,
        receipt: AnchorReceipt,
    ) -> Result<DagEvent> {
        receipt.verify(&vault.membership.operator.signer_hash)?;
        let intent = &receipt.intent;
        if canonical(&intent.membership)? != canonical(&vault.membership)? {
            return Err(Error::Identity);
        }
        let key = self.capsule_key(&intent.reservation_id)?;
        let nonce = decode::<12>(&intent.capsule.nonce_b64)?;
        let mut plaintext = Zeroizing::new(
            STANDARD
                .decode(&intent.capsule.ciphertext_b64)
                .map_err(|_| Error::Input)?,
        );
        let tag_at = plaintext.len() - 16;
        let tag = plaintext.split_off(tag_at);
        Aes256Gcm::new_from_slice(key.as_ref())
            .map_err(|_| Error::Protection)?
            .decrypt_in_place_detached(
                Nonce::from_slice(nonce.as_ref()),
                &intent.aad()?,
                &mut plaintext,
                Tag::from_slice(&tag),
            )
            .map_err(|_| Error::Authentication)?;
        let event: UnsponsoredEvent = parse(&plaintext)?;
        event.verify()?;
        if event.digest()? != intent.event_digest
            || event.body.payload_digest()? != intent.payload_sha256
            || event.proof.reservation_id != intent.reservation_id
            || event.body.context_digest != intent.context_digest
            || event.body.sequence != intent.sequence
            || event.body.self_parent != intent.self_parent
            || event.body.timestamp_ms != intent.timestamp_ms
            || canonical(&event.proof.membership)? != canonical(&intent.membership)?
        {
            return Err(Error::Identity);
        }
        let head = self
            .state
            .dag_heads
            .iter()
            .find(|h| h.context_digest == intent.context_digest);
        let retry = head.is_some_and(|h| {
            h.event_digest == intent.event_digest && h.sequence == intent.sequence
        });
        if !retry {
            next(head, intent)?;
            if head.is_none() && self.state.dag_heads.len() >= 64 {
                return Err(Error::Replay);
            }
            self.state
                .dag_heads
                .retain(|h| h.context_digest != intent.context_digest);
            self.state.dag_heads.push(DagHead {
                context_digest: intent.context_digest.clone(),
                sequence: intent.sequence.clone(),
                event_digest: intent.event_digest.clone(),
                timestamp_ms: intent.timestamp_ms,
            });
        }
        self.state
            .dag_pending
            .retain(|p| p.context_digest != intent.context_digest || p.sequence != intent.sequence);
        self.reseal(vault)?;
        Ok(DagEvent {
            body: event.body,
            proof: event.proof,
            reservation: receipt,
        })
    }
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AnchorBinding {
    pub path: String,
    pub active: bool,
    pub ledger_digest: String,
    pub pending: Option<AnchorReceipt>,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AnchorLedger {
    pub schema_version: u32,
    pub profile: String,
    pub issuer: PublicIdentity,
    pub records: Vec<AnchorReceipt>,
    pub signature: String,
}
impl AnchorLedger {
    fn validate(&self, issuer: &str) -> Result<()> {
        if self.schema_version != 1
            || self.profile != LEDGER
            || self.issuer.signer_hash != issuer
            || self.records.len() > 128
        {
            return Err(Error::Identity);
        }
        verify_signature(&self.issuer, LEDGER.as_bytes(), self, &self.signature)?;
        let mut heads: std::collections::BTreeMap<(String, String), DagHead> =
            std::collections::BTreeMap::new();
        let mut ids = BTreeSet::new();
        for r in &self.records {
            r.verify(issuer)?;
            let i = &r.intent;
            if !ids.insert(&i.reservation_id) {
                return Err(Error::Replay);
            }
            let key = (
                i.membership.agent.signer_hash.clone(),
                i.context_digest.clone(),
            );
            next(heads.get(&key), i)?;
            heads.insert(
                key,
                DagHead {
                    context_digest: i.context_digest.clone(),
                    sequence: i.sequence.clone(),
                    event_digest: i.event_digest.clone(),
                    timestamp_ms: i.timestamp_ms,
                },
            );
        }
        Ok(())
    }
    pub fn digest(&self) -> Result<String> {
        digest(LEDGER, self)
    }
    fn append(&self, receipt: AnchorReceipt, key: &XPrv) -> Result<Self> {
        let mut new = self.clone();
        if new.records.len() >= 128 {
            return Err(Error::Replay);
        }
        new.records.push(receipt);
        new.signature = sign(key, LEDGER, &new)?;
        new.validate(&new.issuer.signer_hash)?;
        if canonical(&new)?.len() > MAX_JSON {
            return Err(Error::Storage);
        }
        Ok(new)
    }
}
impl Unlocked {
    /// Persist the bound root vault BEFORE creating the independent empty ledger.
    pub fn bind_dag_anchor(&mut self, vault: &mut Vault, path: &str) -> Result<AnchorLedger> {
        let mut ledger = AnchorLedger {
            schema_version: 1,
            profile: LEDGER.into(),
            issuer: vault.identity.clone(),
            records: Vec::new(),
            signature: String::new(),
        };
        ledger.signature = sign(&self.keys.base, LEDGER, &ledger)?;
        if let Some(b) = &self.state.dag_anchor {
            if b.path == path
                && !b.active
                && b.pending.is_none()
                && b.ledger_digest == ledger.digest()?
            {
                return Ok(ledger);
            }
            return Err(Error::Policy);
        }
        self.state.dag_anchor = Some(AnchorBinding {
            path: path.into(),
            active: false,
            ledger_digest: ledger.digest()?,
            pending: None,
        });
        self.seal_allocations(vault)?;
        Ok(ledger)
    }
    pub fn dag_anchor_binding(&self) -> Option<&AnchorBinding> {
        self.state.dag_anchor.as_ref()
    }
    pub fn activate_dag_anchor(
        &mut self,
        vault: &mut Vault,
        path: &str,
        ledger: &AnchorLedger,
    ) -> Result<()> {
        let b = self.state.dag_anchor.as_mut().ok_or(Error::Policy)?;
        if b.path != path
            || b.active
            || b.ledger_digest != ledger.digest()?
            || !ledger.records.is_empty()
        {
            return Err(Error::Policy);
        }
        ledger.validate(&vault.identity.signer_hash)?;
        b.active = true;
        self.seal_allocations(vault)
    }
    /// A durable root-vault write-ahead receipt fences the two-file crash window.
    pub fn stage_dag_reservation(
        &mut self,
        vault: &mut Vault,
        path: &str,
        ledger: &AnchorLedger,
        intent: ReservationIntent,
    ) -> Result<AnchorReceipt> {
        self.check_dag_anchor(path, ledger)?;
        if self.state.dag_anchor.as_ref().unwrap().pending.is_some() {
            return Err(Error::Storage);
        }
        if let Some(old) = ledger
            .records
            .iter()
            .find(|r| r.intent.reservation_id == intent.reservation_id)
        {
            if canonical(&old.intent)? != canonical(&intent)? {
                return Err(Error::Replay);
            }
            return Ok(old.clone());
        }
        intent.verify(&vault.identity.signer_hash)?;
        let m = &intent.membership;
        if !self.state.agent_allocations.iter().any(|a| {
            a.agent_fingerprint == m.agent.signer_hash
                && a.account_number == m.account_number
                && a.instance_id == m.instance_id
                && a.group_id == m.group_id
                && a.group_epoch == m.group_epoch
        }) {
            return Err(Error::Policy);
        }
        let mut r = AnchorReceipt {
            schema_version: 1,
            profile: RECEIPT.into(),
            intent,
            signature: String::new(),
        };
        r.signature = sign(&self.keys.base, RECEIPT, &r)?;
        ledger.append(r.clone(), &self.keys.base)?;
        self.state.dag_anchor.as_mut().unwrap().pending = Some(r.clone());
        self.seal_allocations(vault)?;
        Ok(r)
    }
    fn check_dag_anchor(&self, path: &str, ledger: &AnchorLedger) -> Result<()> {
        let b = self.state.dag_anchor.as_ref().ok_or(Error::Policy)?;
        if !b.active || b.path != path {
            return Err(Error::Policy);
        }
        ledger.validate(&identity(&self.keys.base).signer_hash)?;
        let actual = ledger.digest()?;
        if actual != b.ledger_digest {
            let r = b.pending.as_ref().ok_or(Error::Storage)?;
            if ledger
                .records
                .last()
                .is_none_or(|last| canonical(last).ok() != canonical(r).ok())
            {
                return Err(Error::Storage);
            }
            let mut old = ledger.clone();
            old.records.pop();
            old.signature = sign(&self.keys.base, LEDGER, &old)?;
            if old.digest()? != b.ledger_digest {
                return Err(Error::Storage);
            }
        }
        Ok(())
    }
    /// Caller writes returned ledger, then root vault, then and only then emits a receipt/export.
    pub fn reconcile_dag_anchor(
        &mut self,
        vault: &mut Vault,
        path: &str,
        ledger: &AnchorLedger,
    ) -> Result<AnchorLedger> {
        self.check_dag_anchor(path, ledger)?;
        let b = self.state.dag_anchor.as_ref().unwrap();
        let new = if let Some(r) = &b.pending {
            if ledger.digest()? == b.ledger_digest {
                ledger.append(r.clone(), &self.keys.base)?
            } else {
                ledger.clone()
            }
        } else {
            ledger.clone()
        };
        let b = self.state.dag_anchor.as_mut().unwrap();
        b.ledger_digest = new.digest()?;
        b.pending = None;
        self.seal_allocations(vault)?;
        Ok(new)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    const PASSWORD: &[u8] = b"synthetic operator password";
    const AGENT_PASSWORD: &[u8] = b"synthetic agent password";
    const NOW: u64 = 1792584000;
    const PATH: &str = "/independent-operator/anchor.json";

    fn setup() -> (Vault, Unlocked, AgentVault, String, AnchorLedger) {
        let group = "11111111-1111-4111-8111-111111111111";
        let policy = Policy {
            workspace_id: group.into(),
            allowed_audiences: vec!["fixture".into()],
            allowed_payload_types: vec!["syn://type/ember".into()],
            allowed_domains: vec!["potbot.event.v2".into()],
        };
        let mut root = Vault::create(PASSWORD, policy, NOW, Some(&[42; 32]), None).unwrap();
        let pin = root.identity.signer_hash.clone();
        let mut owner = root.unlock(PASSWORD, &pin).unwrap();
        let agent = owner
            .provision_agent(
                &mut root,
                ProvisionRequest {
                    account_number: 7,
                    instance_id: "22222222-2222-4222-8222-222222222222".into(),
                    group_id: group.into(),
                    group_epoch: 0,
                },
                AGENT_PASSWORD,
                NOW,
            )
            .unwrap();
        let ledger = owner.bind_dag_anchor(&mut root, PATH).unwrap();
        owner.activate_dag_anchor(&mut root, PATH, &ledger).unwrap();
        (root, owner, agent, pin, ledger)
    }
    fn body(vault: &AgentVault) -> DagBody {
        DagBody {
            profile: EVENT.into(),
            context_digest: "a".repeat(64),
            creator: vault.membership.agent.signer_hash.clone(),
            sequence: "1".into(),
            self_parent: None,
            other_parent: None,
            timestamp_ms: 1000,
            proposal: None,
        }
    }
    fn unlock(v: &AgentVault, pin: &str) -> AgentUnlocked {
        v.unlock(AGENT_PASSWORD, pin, &v.membership.agent.signer_hash)
            .unwrap()
    }
    fn reserve(
        root: &mut Vault,
        owner: &mut Unlocked,
        ledger: &mut AnchorLedger,
        i: ReservationIntent,
    ) -> AnchorReceipt {
        let r = owner.stage_dag_reservation(root, PATH, ledger, i).unwrap();
        *ledger = owner.reconcile_dag_anchor(root, PATH, ledger).unwrap();
        r
    }

    #[test]
    fn restored_agent_cannot_get_a_conflicting_branch_and_exact_capsule_recovers() {
        let (mut root, mut owner, mut agent, pin, mut ledger) = setup();
        let backup = canonical(&agent).unwrap();
        let b = body(&agent);
        let mut worker = unlock(&agent, &pin);
        let i = worker.prepare_dag(&mut agent, b.clone()).unwrap();
        let persisted = canonical(&agent).unwrap();
        let mut reopened: AgentVault = parse(&persisted).unwrap();
        assert_eq!(
            canonical(
                &unlock(&reopened, &pin)
                    .prepare_dag(&mut reopened, b.clone())
                    .unwrap()
            )
            .unwrap(),
            canonical(&i).unwrap()
        );
        let receipt = reserve(&mut root, &mut owner, &mut ledger, i.clone());
        assert_eq!(
            canonical(
                &owner
                    .stage_dag_reservation(&mut root, PATH, &ledger, i)
                    .unwrap()
            )
            .unwrap(),
            canonical(&receipt).unwrap()
        );
        let original = worker.commit_dag(&mut agent, receipt.clone()).unwrap();
        let mut restored: AgentVault = parse(&backup).unwrap();
        let mut after_restore = unlock(&restored, &pin);
        let mut fork = b.clone();
        fork.timestamp_ms += 1;
        let conflict = after_restore.prepare_dag(&mut restored, fork).unwrap();
        assert!(matches!(
            owner.stage_dag_reservation(&mut root, PATH, &ledger, conflict),
            Err(Error::Replay)
        ));
        let recovered = after_restore
            .commit_dag(&mut restored, receipt.clone())
            .unwrap();
        assert_eq!(
            canonical(&recovered).unwrap(),
            canonical(&original).unwrap()
        );
        let mut next_body = b;
        next_body.sequence = "2".into();
        next_body.self_parent = Some(receipt.intent.event_digest.clone());
        next_body.timestamp_ms += 2;
        let pending = after_restore
            .prepare_dag(&mut restored, next_body.clone())
            .unwrap();
        // Retrying the current head must NOT erase a pending next event.
        after_restore.commit_dag(&mut restored, receipt).unwrap();
        let mut different = next_body;
        different.timestamp_ms += 1;
        assert!(matches!(
            after_restore.prepare_dag(&mut restored, different),
            Err(Error::Replay)
        ));
        let second = reserve(&mut root, &mut owner, &mut ledger, pending);
        after_restore.commit_dag(&mut restored, second).unwrap();
        let public = canonical(&original).unwrap();
        assert!(!String::from_utf8_lossy(&public).contains("private_key"));
        if let Some(path) = std::env::var_os("SYN_DAG_PUBLIC_FIXTURE_OUT") {
            std::fs::write(path, public).unwrap();
        }
    }
    #[test]
    fn operator_wal_recovers_both_crash_windows_and_partial_rollback_holds() {
        let (mut root, mut owner, mut agent, pin, empty) = setup();
        let bound = canonical(&root).unwrap();
        let b = body(&agent);
        let i = unlock(&agent, &pin).prepare_dag(&mut agent, b).unwrap();
        let r = owner
            .stage_dag_reservation(&mut root, PATH, &empty, i)
            .unwrap();
        let write_ahead = canonical(&root).unwrap();
        let mut crashed: Vault = parse(&write_ahead).unwrap();
        let mut recovered = crashed.unlock(PASSWORD, &pin).unwrap();
        let committed = recovered
            .reconcile_dag_anchor(&mut crashed, PATH, &empty)
            .unwrap();
        assert_eq!(committed.records.len(), 1);
        assert_eq!(
            canonical(&committed.records[0]).unwrap(),
            canonical(&r).unwrap()
        );
        // Journal flushed, root completion write lost: replay must not append twice.
        let mut lost_completion: Vault = parse(&write_ahead).unwrap();
        let mut owner2 = lost_completion.unlock(PASSWORD, &pin).unwrap();
        let retry = owner2
            .reconcile_dag_anchor(&mut lost_completion, PATH, &committed)
            .unwrap();
        assert_eq!(canonical(&retry).unwrap(), canonical(&committed).unwrap());
        assert!(matches!(
            recovered.reconcile_dag_anchor(&mut crashed, PATH, &empty),
            Err(Error::Storage)
        ));
        let mut rolled_root: Vault = parse(&bound).unwrap();
        assert!(matches!(
            rolled_root
                .unlock(PASSWORD, &pin)
                .unwrap()
                .reconcile_dag_anchor(&mut rolled_root, PATH, &committed),
            Err(Error::Storage)
        ));
        assert!(owner2
            .bind_dag_anchor(&mut lost_completion, "/another/anchor.json")
            .is_err());
    }
    #[test]
    fn prepare_holds_conflicts_and_commit_authenticates_the_recovery_capsule() {
        let (mut root, mut owner, mut agent, pin, mut ledger) = setup();
        let b = body(&agent);
        let mut worker = unlock(&agent, &pin);
        let i = worker.prepare_dag(&mut agent, b.clone()).unwrap();
        let mut different = b;
        different.timestamp_ms += 1;
        assert!(worker.prepare_dag(&mut agent, different).is_err());
        let mut receipt = reserve(&mut root, &mut owner, &mut ledger, i);
        let mut bytes = STANDARD
            .decode(&receipt.intent.capsule.ciphertext_b64)
            .unwrap();
        bytes[0] ^= 1;
        receipt.intent.capsule.ciphertext_b64 = b64(&bytes);
        receipt.intent.signature = sign(&worker.account, INTENT, &receipt.intent).unwrap();
        receipt.signature = sign(&owner.keys.base, RECEIPT, &receipt).unwrap();
        assert!(matches!(
            worker.commit_dag(&mut agent, receipt),
            Err(Error::Authentication)
        ));
        assert!(worker.state.dag_heads.is_empty());
    }
    #[test]
    fn generic_signatures_gaps_and_pending_credential_rotation_cannot_bypass_the_guard() {
        let (mut root, mut owner, mut agent, pin, mut ledger) = setup();
        let b = body(&agent);
        let mut worker = unlock(&agent, &pin);
        let group_id = agent.membership.group_id.clone();
        let generic = worker
            .sign_reserved(
                &mut agent,
                AgentSignRequest {
                    group_id,
                    group_epoch: 0,
                    request_id: "33333333-3333-4333-8333-333333333333".into(),
                    payload_sha256: b.payload_digest().unwrap(),
                    validity_seconds: 300,
                },
                NOW,
            )
            .unwrap();
        let fake = UnsponsoredEvent {
            body: b.clone(),
            proof: DagProof {
                schema_version: 1,
                profile: PROOF.into(),
                membership: agent.membership.clone(),
                reservation_id: generic.request_id,
                payload_sha256: generic.payload_sha256,
                signature: generic.signature,
            },
        };
        assert!(fake.verify().is_err());
        let i = worker.prepare_dag(&mut agent, b.clone()).unwrap();
        assert!(owner
            .update_agent_group(&mut root, &mut agent, &mut worker, 1)
            .is_err());
        let r = reserve(&mut root, &mut owner, &mut ledger, i);
        let parent = r.intent.event_digest.clone();
        worker.commit_dag(&mut agent, r).unwrap();
        let mut gap = b.clone();
        gap.sequence = "3".into();
        gap.self_parent = Some(parent.clone());
        gap.timestamp_ms += 1;
        assert!(worker.prepare_dag(&mut agent, gap).is_err());
        let mut wrong = b;
        wrong.sequence = "2".into();
        wrong.self_parent = Some("b".repeat(64));
        wrong.timestamp_ms += 1;
        assert!(worker.prepare_dag(&mut agent, wrong).is_err());
        owner
            .update_agent_group(&mut root, &mut agent, &mut worker, 1)
            .unwrap();
    }
}
