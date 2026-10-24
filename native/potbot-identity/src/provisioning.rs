//! Operator-only hardened derivation and restricted, password-protected Agent vaults.
//! No public API/command exports a private key, parent seed or group-derivation secret.
use super::*;
pub mod dag;

pub const AGENT_PROFILE: &str = "syn-agent-account-v1";
pub const AGENT_VAULT_PROFILE: &str = "syn-restricted-agent-vault-v1";
const MAX_ALLOCATIONS: usize = 1024;

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Allocation {
    pub account_number: u32,
    pub agent_fingerprint: String,
    pub instance_id: String,
    pub group_id: String,
    pub group_epoch: u32,
}
pub(crate) fn validate_allocations(items: &[Allocation]) -> Result<()> {
    let (mut accounts, mut agents, mut instances) =
        (BTreeSet::new(), BTreeSet::new(), BTreeSet::new());
    if items.len() > MAX_ALLOCATIONS {
        return Err(Error::Storage);
    }
    for item in items {
        if item.account_number >= HARDENED
            || item.group_epoch >= HARDENED
            || !hash_valid(&item.agent_fingerprint)
            || !uuid_valid(&item.instance_id)
            || !uuid_valid(&item.group_id)
            || !accounts.insert(item.account_number)
            || !agents.insert(&item.agent_fingerprint)
            || !instances.insert(&item.instance_id)
        {
            return Err(Error::Identity);
        }
    }
    Ok(())
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProvisionRequest {
    pub account_number: u32,
    pub instance_id: String,
    pub group_id: String,
    pub group_epoch: u32,
}
impl ProvisionRequest {
    pub fn validate(&self) -> Result<()> {
        if self.account_number >= HARDENED
            || self.group_epoch >= HARDENED
            || !uuid_valid(&self.instance_id)
            || !uuid_valid(&self.group_id)
        {
            return Err(Error::Input);
        }
        Ok(())
    }
}

fn identity(key: &XPrv) -> PublicIdentity {
    let public = key.public().public_key();
    PublicIdentity {
        type_ref: "synny://type/public-identity-descriptor".into(),
        schema_version: 2,
        profile_id: IDENTITY_PROFILE.into(),
        hash_algorithm: "SHA-256".into(),
        signer_hash: sha256_hex(&public),
        public_key_algorithm: "Ed25519".into(),
        public_key_encoding: "base64".into(),
        public_key: b64(&public),
    }
}
fn account_key(base: &XPrv, account: u32) -> Result<XPrv> {
    if account >= HARDENED {
        return Err(Error::Input);
    }
    Ok(base
        .derive(DerivationScheme::V2, HARDENED + 2)
        .derive(DerivationScheme::V2, HARDENED + account))
}
fn group_key(base: &XPrv, group: &str, key_epoch: u32) -> Result<Zeroizing<[u8; 32]>> {
    if !uuid_valid(group) || key_epoch >= HARDENED {
        return Err(Error::Input);
    }
    let branch = base.derive(DerivationScheme::V2, HARDENED + 3);
    let info = canonical(&json!({"profile":"syn-private-pubsub-group-key-v1",
        "operator_fingerprint":identity(base).signer_hash,"group_id":group,"group_epoch":key_epoch}))?;
    let mut output = Zeroizing::new([0u8; 32]);
    Hkdf::<Sha256>::new(Some(b"syn-private-pubsub-group-key-v1"), branch.as_ref())
        .expand(&info, output.as_mut())
        .map_err(|_| Error::Protection)?;
    Ok(output)
}
fn verify_signature(
    public: &PublicIdentity,
    prefix: &[u8],
    value: &impl Serialize,
    signature: &str,
) -> Result<()> {
    public.validate()?;
    let mut extended = [0u8; 64];
    extended[..32].copy_from_slice(decode::<32>(&public.public_key)?.as_ref());
    let key = ed25519_bip32::XPub::from_bytes(extended);
    let signature = ed25519_bip32::Signature::<()>::from_slice(decode::<64>(signature)?.as_ref())
        .map_err(|_| Error::Identity)?;
    let value = serde_json::to_value(value).map_err(|_| Error::Input)?;
    if !key.verify(&signed_bytes(prefix, &value)?, &signature) {
        return Err(Error::Identity);
    }
    Ok(())
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Membership {
    pub schema_version: u32,
    pub profile: String,
    pub operator: PublicIdentity,
    pub agent: PublicIdentity,
    pub account_number: u32,
    pub instance_id: String,
    pub group_id: String,
    pub group_epoch: u32,
    pub signature: String,
}
impl Membership {
    pub fn validate(&self, expected_operator: &str) -> Result<()> {
        if self.schema_version != 1
            || self.profile != AGENT_PROFILE
            || !hash_valid(expected_operator)
            || self.operator.signer_hash != expected_operator
            || self.agent.signer_hash == expected_operator
        {
            return Err(Error::Identity);
        }
        self.agent.validate()?;
        ProvisionRequest {
            account_number: self.account_number,
            instance_id: self.instance_id.clone(),
            group_id: self.group_id.clone(),
            group_epoch: self.group_epoch,
        }
        .validate()?;
        verify_signature(
            &self.operator,
            b"syn-agent-membership-v1",
            self,
            &self.signature,
        )
    }
}

#[derive(Serialize, Deserialize, Zeroize, ZeroizeOnDrop)]
#[serde(deny_unknown_fields)]
struct AgentSecrets {
    account_private_key: String,
    group_key: String,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct AgentState {
    schema_version: u32,
    profile: String,
    vault_id: String,
    key_epoch: String,
    generation: u64,
    membership_sha256: String,
    secrets: AgentSecrets,
    epoch_high_water: u64,
    reservations: Vec<Reservation>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    dag_heads: Vec<dag::DagHead>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    dag_pending: Vec<dag::ReservationIntent>,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentVault {
    schema_version: u32,
    profile: String,
    pub membership: Membership,
    storage: Vault,
}
impl AgentVault {
    pub fn public(&self, expected_operator: &str) -> Result<Membership> {
        if self.schema_version != 1 || self.profile != AGENT_VAULT_PROFILE {
            return Err(Error::Unsupported);
        }
        self.membership.validate(expected_operator)?;
        if self.storage.public()? != self.membership.agent {
            return Err(Error::Identity);
        }
        Ok(self.membership.clone())
    }
    pub fn unlock(
        &self,
        password: &[u8],
        expected_operator: &str,
        expected_agent: &str,
    ) -> Result<AgentUnlocked> {
        self.public(expected_operator)?;
        if !hash_valid(expected_agent) || expected_agent != self.membership.agent.signer_hash {
            return Err(Error::Identity);
        }
        let (master, state): (_, AgentState) =
            self.storage.open_private(password, expected_agent)?;
        if state.schema_version != 1
            || state.profile != AGENT_VAULT_PROFILE
            || state.vault_id != self.storage.vault_id
            || state.key_epoch != self.storage.key_epoch
            || state.generation != self.storage.generation
            || state.membership_sha256 != sha256_hex(&canonical(&self.membership)?)
            || state.reservations.len() > 1024
            || state.epoch_high_water > i32::MAX as u64
            || state.dag_heads.len() > 64
            || state.dag_pending.len() > 16
        {
            return Err(Error::Identity);
        }
        let mut ids = BTreeSet::new();
        if state
            .reservations
            .iter()
            .any(|r| !uuid_valid(&r.request_id) || !ids.insert(&r.request_id))
        {
            return Err(Error::Identity);
        }
        let private = decode::<96>(&state.secrets.account_private_key)?;
        let account = XPrv::from_slice_verified(private.as_ref()).map_err(|_| Error::Identity)?;
        if identity(&account) != self.membership.agent {
            return Err(Error::Identity);
        }
        decode::<32>(&state.secrets.group_key)?;
        Ok(AgentUnlocked {
            master,
            state,
            account,
        })
    }
}

fn membership(base: &XPrv, account: &XPrv, request: &ProvisionRequest) -> Result<Membership> {
    let mut member = Membership {
        schema_version: 1,
        profile: AGENT_PROFILE.into(),
        operator: identity(base),
        agent: identity(account),
        account_number: request.account_number,
        instance_id: request.instance_id.clone(),
        group_id: request.group_id.clone(),
        group_epoch: request.group_epoch,
        signature: String::new(),
    };
    let value = serde_json::to_value(&member).map_err(|_| Error::Input)?;
    member.signature = b64(base
        .sign::<()>(&signed_bytes(b"syn-agent-membership-v1", &value)?)
        .as_ref());
    member.validate(&member.operator.signer_hash)?;
    Ok(member)
}
impl Unlocked {
    pub fn allocations(&self) -> &[Allocation] {
        &self.state.agent_allocations
    }
    fn group_epoch_allowed(&self, group: &str, requested: u32) -> Result<()> {
        if self
            .state
            .agent_allocations
            .iter()
            .any(|a| a.group_id == group && a.group_epoch > requested)
        {
            return Err(Error::Policy);
        }
        Ok(())
    }
    fn seal_allocations(&mut self, vault: &mut Vault) -> Result<()> {
        validate_allocations(&self.state.agent_allocations)?;
        self.state.schema_version = 2;
        vault.generation = vault
            .generation
            .checked_add(1)
            .filter(|g| *g <= 9_007_199_254_740_991)
            .ok_or(Error::Storage)?;
        self.state.generation = vault.generation;
        vault.seal(&self.master, &self.state)
    }
    /// Caller must persist the operator reservation BEFORE writing/releasing the Agent vault.
    pub fn provision_agent(
        &mut self,
        operator: &mut Vault,
        request: ProvisionRequest,
        password: &[u8],
        now: u64,
    ) -> Result<AgentVault> {
        request.validate()?;
        epoch(now)?;
        if password.len() < 12
            || self.state.agent_allocations.len() >= MAX_ALLOCATIONS
            || self.state.agent_allocations.iter().any(|a| {
                a.account_number == request.account_number || a.instance_id == request.instance_id
            })
        {
            return Err(Error::Policy);
        }
        self.group_epoch_allowed(&request.group_id, request.group_epoch)?;
        let account = account_key(&self.keys.base, request.account_number)?;
        let certificate = membership(&self.keys.base, &account, &request)?;
        if self
            .state
            .agent_allocations
            .iter()
            .any(|a| a.agent_fingerprint == certificate.agent.signer_hash)
        {
            return Err(Error::Identity);
        }
        let group = group_key(&self.keys.base, &request.group_id, request.group_epoch)?;
        let (mut storage, master) = Vault::with_identity(password, certificate.agent.clone())?;
        let state = AgentState {
            schema_version: 1,
            profile: AGENT_VAULT_PROFILE.into(),
            vault_id: storage.vault_id.clone(),
            key_epoch: storage.key_epoch.clone(),
            generation: 1,
            membership_sha256: sha256_hex(&canonical(&certificate)?),
            secrets: AgentSecrets {
                account_private_key: b64(account.as_ref()),
                group_key: b64(group.as_ref()),
            },
            epoch_high_water: epoch(now)?,
            reservations: Vec::new(),
            dag_heads: Vec::new(),
            dag_pending: Vec::new(),
        };
        storage.seal(&master, &state)?;
        self.state.agent_allocations.push(Allocation {
            account_number: request.account_number,
            agent_fingerprint: certificate.agent.signer_hash.clone(),
            instance_id: request.instance_id,
            group_id: request.group_id,
            group_epoch: request.group_epoch,
        });
        self.seal_allocations(operator)?;
        Ok(AgentVault {
            schema_version: 1,
            profile: AGENT_VAULT_PROFILE.into(),
            membership: certificate,
            storage,
        })
    }
    /// Persist operator state before replacing the Agent vault; an interrupted update can be retried.
    pub fn update_agent_group(
        &mut self,
        operator: &mut Vault,
        agent: &mut AgentVault,
        unlocked: &mut AgentUnlocked,
        next_epoch: u32,
    ) -> Result<()> {
        if !unlocked.state.dag_pending.is_empty() {
            return Err(Error::Policy);
        }
        let current = &agent.membership;
        current.validate(&operator.identity.signer_hash)?;
        if next_epoch >= HARDENED || next_epoch < current.group_epoch {
            return Err(Error::Policy);
        }
        self.group_epoch_allowed(&current.group_id, next_epoch)?;
        let index = self
            .state
            .agent_allocations
            .iter()
            .position(|a| {
                a.account_number == current.account_number
                    && a.agent_fingerprint == current.agent.signer_hash
                    && a.instance_id == current.instance_id
                    && a.group_id == current.group_id
            })
            .ok_or(Error::Identity)?;
        let account = account_key(&self.keys.base, current.account_number)?;
        if identity(&account) != current.agent || identity(&unlocked.account) != current.agent {
            return Err(Error::Identity);
        }
        let key = group_key(&self.keys.base, &current.group_id, next_epoch)?;
        let request = ProvisionRequest {
            account_number: current.account_number,
            instance_id: current.instance_id.clone(),
            group_id: current.group_id.clone(),
            group_epoch: next_epoch,
        };
        agent.membership = membership(&self.keys.base, &account, &request)?;
        unlocked.state.secrets.group_key.zeroize();
        unlocked.state.secrets.group_key = b64(key.as_ref());
        unlocked.state.membership_sha256 = sha256_hex(&canonical(&agent.membership)?);
        unlocked.reseal(agent)?;
        self.state.agent_allocations[index].group_epoch = next_epoch;
        self.seal_allocations(operator)
    }
}

pub struct AgentUnlocked {
    master: Zeroizing<[u8; 32]>,
    state: AgentState,
    account: XPrv,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentSignRequest {
    pub group_id: String,
    pub group_epoch: u32,
    pub request_id: String,
    pub payload_sha256: String,
    pub validity_seconds: u32,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentProof {
    pub schema_version: u32,
    pub profile: String,
    pub membership: Membership,
    pub request_id: String,
    pub payload_sha256: String,
    pub issued_at: u64,
    pub expires_at: u64,
    pub signature: String,
}
impl AgentProof {
    pub fn verify(
        &self,
        operator: &str,
        group: &str,
        group_epoch: u32,
        payload: &str,
        now: u64,
    ) -> Result<()> {
        self.membership.validate(operator)?;
        if self.schema_version != 1
            || self.profile != "syn-agent-proof-v1"
            || self.membership.group_id != group
            || self.membership.group_epoch != group_epoch
            || !uuid_valid(&self.request_id)
            || !hash_valid(&self.payload_sha256)
            || self.payload_sha256 != payload
            || self.expires_at <= self.issued_at
            || self.expires_at - self.issued_at > 300
            || now < self.issued_at
            || now >= self.expires_at
        {
            return Err(Error::Policy);
        }
        epoch(self.issued_at)?;
        epoch(self.expires_at)?;
        verify_signature(
            &self.membership.agent,
            b"syn-agent-proof-v1",
            self,
            &self.signature,
        )
    }
}
impl AgentUnlocked {
    fn reseal(&mut self, agent: &mut AgentVault) -> Result<()> {
        if self.state.vault_id != agent.storage.vault_id
            || self.state.key_epoch != agent.storage.key_epoch
            || identity(&self.account) != agent.membership.agent
            || self.state.membership_sha256 != sha256_hex(&canonical(&agent.membership)?)
        {
            return Err(Error::Identity);
        }
        agent.storage.generation = agent
            .storage
            .generation
            .checked_add(1)
            .filter(|g| *g <= 9_007_199_254_740_991)
            .ok_or(Error::Storage)?;
        self.state.generation = agent.storage.generation;
        agent.storage.seal(&self.master, &self.state)
    }
    /// Persist the Agent vault reservation before releasing the returned public proof.
    pub fn sign_reserved(
        &mut self,
        agent: &mut AgentVault,
        request: AgentSignRequest,
        now: u64,
    ) -> Result<AgentProof> {
        if request.group_id != agent.membership.group_id
            || request.group_epoch != agent.membership.group_epoch
            || !uuid_valid(&request.request_id)
            || !hash_valid(&request.payload_sha256)
            || request.validity_seconds == 0
            || request.validity_seconds > 300
        {
            return Err(Error::Policy);
        }
        let current = epoch(now)?;
        if current < self.state.epoch_high_water {
            return Err(Error::Clock);
        }
        self.state
            .reservations
            .retain(|r| r.expires.saturating_add(30) >= now);
        if self.state.reservations.len() >= 1024
            || self
                .state
                .reservations
                .iter()
                .any(|r| r.request_id == request.request_id)
        {
            return Err(Error::Replay);
        }
        let expires = now
            .checked_add(request.validity_seconds as u64)
            .ok_or(Error::Clock)?;
        epoch(expires)?;
        self.state.reservations.push(Reservation {
            request_id: request.request_id.clone(),
            expires,
        });
        self.state.epoch_high_water = current;
        self.reseal(agent)?;
        let mut proof = AgentProof {
            schema_version: 1,
            profile: "syn-agent-proof-v1".into(),
            membership: agent.membership.clone(),
            request_id: request.request_id,
            payload_sha256: request.payload_sha256,
            issued_at: now,
            expires_at: expires,
            signature: String::new(),
        };
        proof.signature = b64(self
            .account
            .sign::<()>(&signed_bytes(
                b"syn-agent-proof-v1",
                &serde_json::to_value(&proof).map_err(|_| Error::Input)?,
            )?)
            .as_ref());
        Ok(proof)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    const NOW: u64 = 1792584000;
    const PASSWORD: &[u8] = b"synthetic operator password";
    const AGENT_PASSWORD: &[u8] = b"synthetic agent password";
    const GROUP: &str = "11111111-1111-4111-8111-111111111111";
    fn operator() -> (Vault, Unlocked, String) {
        let policy = Policy {
            workspace_id: GROUP.into(),
            allowed_audiences: vec!["fixture".into()],
            allowed_payload_types: vec!["syn://type/ember".into()],
            allowed_domains: vec!["potbot.event.v2".into()],
        };
        let vault = Vault::create(PASSWORD, policy, NOW, Some(&[42; 32]), None).unwrap();
        let fingerprint = vault.identity.signer_hash.clone();
        let unlocked = vault.unlock(PASSWORD, &fingerprint).unwrap();
        (vault, unlocked, fingerprint)
    }
    fn request(account: u32, key_epoch: u32) -> ProvisionRequest {
        ProvisionRequest {
            account_number: account,
            instance_id: format!("00000000-0000-4000-8000-{account:012}"),
            group_id: GROUP.into(),
            group_epoch: key_epoch,
        }
    }
    fn sign_request() -> AgentSignRequest {
        AgentSignRequest {
            group_id: GROUP.into(),
            group_epoch: 0,
            request_id: "00000000-0000-4000-8000-000000000009".into(),
            payload_sha256: sha256_hex(b"synthetic fixture"),
            validity_seconds: 300,
        }
    }
    #[test]
    fn restricted_keys_and_agent_operation_without_operator() {
        let (mut operator, mut owner, pin) = operator();
        let words = owner.recovery_words().unwrap();
        let mut a = owner
            .provision_agent(&mut operator, request(7, 0), AGENT_PASSWORD, NOW)
            .unwrap();
        let b = owner
            .provision_agent(&mut operator, request(8, 0), AGENT_PASSWORD, NOW)
            .unwrap();
        let a_pin = a.membership.agent.signer_hash.clone();
        let b_pin = b.membership.agent.signer_hash.clone();
        let mut worker = a.unlock(AGENT_PASSWORD, &pin, &a_pin).unwrap();
        let other = b.unlock(AGENT_PASSWORD, &pin, &b_pin).unwrap();
        assert_ne!(a_pin, b_pin);
        assert_eq!(
            worker.state.secrets.group_key,
            other.state.secrets.group_key
        );
        assert_ne!(
            worker.state.secrets.account_private_key,
            other.state.secrets.account_private_key
        );
        let secrets = serde_json::to_value(&worker.state.secrets).unwrap();
        assert_eq!(secrets.as_object().unwrap().len(), 2);
        for field in [
            "entropy",
            "root_private_key",
            "base_private_key",
            "group_derivation_key",
        ] {
            assert!(secrets.get(field).is_none());
        }
        assert_ne!(
            worker.state.secrets.account_private_key,
            owner.state.secrets.root_private_key
        );
        assert_ne!(
            worker.state.secrets.account_private_key,
            owner.state.secrets.base_private_key
        );
        assert_eq!(operator.identity.signer_hash, pin);
        let restored = operator.unlock(PASSWORD, &pin).unwrap();
        assert_eq!(restored.allocations().len(), 2);
        assert_eq!(*restored.recovery_words().unwrap(), *words);
        assert!(parse::<Vault>(&canonical(&a).unwrap()).is_err());
        assert!(parse::<AgentVault>(&canonical(&operator).unwrap()).is_err());
        drop(restored);
        drop(owner);
        drop(operator);
        let proof = worker.sign_reserved(&mut a, sign_request(), NOW).unwrap();
        if let Ok(path) = std::env::var("POTBOT_TEST_AGENT_PUBLIC_FIXTURE") {
            std::fs::write(path, canonical(&proof).unwrap()).unwrap();
        }
        proof
            .verify(&pin, GROUP, 0, &sha256_hex(b"synthetic fixture"), NOW)
            .unwrap();
        let mut reopened = a.unlock(AGENT_PASSWORD, &pin, &a_pin).unwrap();
        assert!(matches!(
            reopened.sign_reserved(&mut a, sign_request(), NOW),
            Err(Error::Replay)
        ));
        assert!(proof
            .verify(&pin, GROUP, 1, &proof.payload_sha256, NOW)
            .is_err());
        assert!(proof
            .verify(&pin, GROUP, 0, &proof.payload_sha256, proof.expires_at)
            .is_err());
    }
    #[test]
    fn duplicate_assignments_rotation_authentication_and_binding() {
        let (mut operator, mut owner, pin) = operator();
        let mut agent = owner
            .provision_agent(&mut operator, request(7, 0), AGENT_PASSWORD, NOW)
            .unwrap();
        assert!(matches!(
            owner.provision_agent(&mut operator, request(7, 0), AGENT_PASSWORD, NOW),
            Err(Error::Policy)
        ));
        let mut duplicate_instance = request(8, 0);
        duplicate_instance.instance_id = agent.membership.instance_id.clone();
        assert!(owner
            .provision_agent(&mut operator, duplicate_instance, AGENT_PASSWORD, NOW)
            .is_err());
        assert!(request(HARDENED, 0).validate().is_err());
        let fingerprint = agent.membership.agent.signer_hash.clone();
        assert!(agent.unlock(b"wrong password", &pin, &fingerprint).is_err());
        assert!(agent
            .unlock(AGENT_PASSWORD, &"0".repeat(64), &fingerprint)
            .is_err());
        assert!(agent.unlock(AGENT_PASSWORD, &pin, &"0".repeat(64)).is_err());
        let old = agent.clone();
        let mut worker = agent.unlock(AGENT_PASSWORD, &pin, &fingerprint).unwrap();
        let old_group = worker.state.secrets.group_key.clone();
        let old_account = worker.state.secrets.account_private_key.clone();
        owner
            .update_agent_group(&mut operator, &mut agent, &mut worker, 1)
            .unwrap();
        assert_eq!(worker.state.secrets.account_private_key, old_account);
        assert_ne!(worker.state.secrets.group_key, old_group);
        assert_eq!(agent.membership.group_epoch, 1);
        assert!(owner
            .update_agent_group(&mut operator, &mut agent, &mut worker, 0)
            .is_err());
        assert!(owner
            .provision_agent(&mut operator, request(9, 0), AGENT_PASSWORD, NOW)
            .is_err());
        let mut wrong_binding = old.clone();
        wrong_binding.membership = agent.membership.clone(); // valid new certificate, old encrypted state
        assert!(wrong_binding
            .unlock(AGENT_PASSWORD, &pin, &fingerprint)
            .is_err());
        let mut forged = agent.clone();
        forged.membership.account_number = 99;
        assert!(forged.public(&pin).is_err());
        let mut tampered = agent.clone();
        let mut ciphertext = STANDARD
            .decode(&tampered.storage.payload.ciphertext_b64)
            .unwrap();
        ciphertext[0] ^= 1;
        tampered.storage.payload.ciphertext_b64 = b64(&ciphertext);
        assert!(tampered.unlock(AGENT_PASSWORD, &pin, &fingerprint).is_err());
        agent.unlock(AGENT_PASSWORD, &pin, &fingerprint).unwrap();
        assert!(parse::<AgentSecrets>(
            br#"{"account_private_key":"x","group_key":"y","entropy":"parent"}"#
        )
        .is_err());
    }
    #[test]
    fn interrupted_delivery_keeps_allocation_reserved_and_rotation_retryable() {
        let (mut operator, mut owner, pin) = operator();
        let delivered = owner
            .provision_agent(&mut operator, request(7, 0), AGENT_PASSWORD, NOW)
            .unwrap();
        // Crash after the operator write but before delivery: retain the sealed
        // operator bytes, discard the generated Agent vault, reopen the register.
        let persisted = canonical(&operator).unwrap();
        drop(delivered);
        drop(owner);
        let mut operator: Vault = parse(&persisted).unwrap();
        let mut owner = operator.unlock(PASSWORD, &pin).unwrap();
        assert!(owner
            .provision_agent(&mut operator, request(7, 0), AGENT_PASSWORD, NOW)
            .is_err());
        let mut agent = owner
            .provision_agent(&mut operator, request(8, 0), AGENT_PASSWORD, NOW)
            .unwrap();
        let old = canonical(&agent).unwrap();
        let agent_pin = agent.membership.agent.signer_hash.clone();
        let mut worker = agent.unlock(AGENT_PASSWORD, &pin, &agent_pin).unwrap();
        owner
            .update_agent_group(&mut operator, &mut agent, &mut worker, 1)
            .unwrap();
        // Operator reservation committed; Agent replacement lost. Retrying from
        // its old encrypted vault derives the same current epoch, not a new key.
        let expected_group = worker.state.secrets.group_key.clone();
        let mut agent: AgentVault = parse(&old).unwrap();
        let mut owner = operator.unlock(PASSWORD, &pin).unwrap();
        let mut worker = agent.unlock(AGENT_PASSWORD, &pin, &agent_pin).unwrap();
        owner
            .update_agent_group(&mut operator, &mut agent, &mut worker, 1)
            .unwrap();
        assert_eq!(worker.state.secrets.group_key, expected_group);
        assert_eq!(agent.membership.group_epoch, 1);
    }
    #[test]
    fn derivation_contexts_are_separate_and_bounded() {
        let keys = Keys::from_entropy(&[42; 32]).unwrap();
        // Independent Python HMAC/PBKDF2 + libsodium vectors; parent public
        // identity also agrees with the existing published Icarus fixture.
        for (account, expected) in [
            (
                0,
                "9956c109425afd964ba634c7f52632f082e2f5b81417b984ea7b76776c15d1a9",
            ),
            (
                7,
                "9782c5634c5ea8b79e5fc0d5fdc8e275c5b23ba64df8ee6446d66efaae1515a3",
            ),
            (
                8,
                "991e32316e15f9a54ff826eb593cf41eab78f489f017fe066c1a54c31ec0474d",
            ),
            (
                HARDENED - 1,
                "1dcc6ac00eff60f4e3e4fc119711b5caa5bb4879d491b968f943836412897fa4",
            ),
        ] {
            assert_eq!(
                hex(account_key(&keys.base, account)
                    .unwrap()
                    .public()
                    .public_key_slice()),
                expected
            );
        }
        assert_eq!(
            hex(group_key(&keys.base, GROUP, 0).unwrap().as_ref()),
            "bf0cdd53b99109d73e99d7f288d47b56ebfa632cda911e7d49fd5d0e9494ca74"
        );
        assert_eq!(
            hex(group_key(&keys.base, GROUP, 1).unwrap().as_ref()),
            "fe632cfa7d63448c47fd0993f90bb08f16d2980bc2448cbe9f002938350c38be"
        );
        assert_ne!(
            account_key(&keys.base, 7).unwrap().as_ref(),
            account_key(&keys.base, 8).unwrap().as_ref()
        );
        assert!(account_key(&keys.base, HARDENED).is_err());
        assert!(account_key(&keys.base, HARDENED - 1).is_ok());
        assert_ne!(
            *group_key(&keys.base, GROUP, 0).unwrap(),
            *group_key(&keys.base, GROUP, 1).unwrap()
        );
        assert_ne!(
            *group_key(&keys.base, GROUP, 0).unwrap(),
            *group_key(&keys.base, "22222222-2222-4222-8222-222222222222", 0).unwrap()
        );
        assert!(group_key(&keys.base, GROUP, HARDENED).is_err());
    }
}
