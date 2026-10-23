//! The local password custody profile. Public output contains only allowlisted keys/signatures.
//! Production uses the binary's locked allocator and locked operation stack. Unit tests are
//! cryptographic/conformance tests with synthetic secrets; they do not qualify OS custody.
use aes_gcm::{
    aead::{AeadInPlace, KeyInit},
    Aes256Gcm, Nonce, Tag,
};
use argon2::{Algorithm, Argon2, Params, Version};
use base64::{engine::general_purpose::STANDARD, Engine};
use ed25519_bip32::{DerivationScheme, XPrv};
use hkdf::Hkdf;
use serde::{
    de::{MapAccess, SeqAccess, Visitor},
    Deserialize, Deserializer, Serialize,
};
use serde_json::{json, Value};
use sha2::{Digest, Sha256, Sha512};
use std::{
    collections::BTreeSet,
    fmt,
    time::{Instant, SystemTime, UNIX_EPOCH},
};
use subtle::ConstantTimeEq;
use time::{macros::format_description, OffsetDateTime};
use zeroize::{Zeroize, ZeroizeOnDrop, Zeroizing};

pub mod files;
pub mod provisioning;
pub mod secure;
pub type Result<T> = std::result::Result<T, Error>;
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Error {
    Input,
    Authentication,
    Policy,
    Clock,
    Storage,
    Identity,
    Protection,
    Replay,
    Unsupported,
}
impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{}",
            match self {
                Self::Input => "invalid or unsupported input",
                Self::Authentication => "vault authentication failed",
                Self::Policy => "signing request is outside the stored policy",
                Self::Clock => "clock confidence unavailable or epoch regression",
                Self::Storage => "private storage operation failed",
                Self::Identity => "identity consistency check failed",
                Self::Protection => "required native memory or filesystem protection unavailable",
                Self::Replay => "request already reserved or replay capacity reached",
                Self::Unsupported => "unsupported profile or platform",
            }
        )
    }
}
impl std::error::Error for Error {}

pub const IDENTITY_PROFILE: &str = "potbot-identity-v1";
pub const SIGNING_PROFILE: &str = "potbot-hourly-signing-v1";
pub const PROTECTION_PROFILE: &str = "potbot-native-password-v1";
pub const MAX_JSON: usize = 1024 * 1024;
const HARDENED: u32 = 1 << 31;
const BASE_PATH: [u32; 5] = [HARDENED + 6969, HARDENED + 420, HARDENED, 0, 0];

fn b64(value: &[u8]) -> String {
    STANDARD.encode(value)
}
pub fn sha256_hex(value: &[u8]) -> String {
    hex(&Sha256::digest(value))
}
fn hex(value: &[u8]) -> String {
    value.iter().map(|b| format!("{b:02x}")).collect()
}
fn decode<const N: usize>(value: &str) -> Result<Zeroizing<[u8; N]>> {
    let mut out = Zeroizing::new([0u8; N]);
    let decoded = STANDARD
        .decode_slice(value, out.as_mut())
        .map_err(|_| Error::Input)?;
    if decoded != N || STANDARD.encode(out.as_ref()) != value {
        return Err(Error::Input);
    }
    Ok(out)
}
fn random<const N: usize>() -> Result<Zeroizing<[u8; N]>> {
    let mut out = Zeroizing::new([0u8; N]);
    getrandom::getrandom(out.as_mut()).map_err(|_| Error::Protection)?;
    Ok(out)
}
fn uuid() -> Result<String> {
    let mut bytes = random::<16>()?;
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    let s = hex(bytes.as_ref());
    Ok(format!(
        "{}-{}-{}-{}-{}",
        &s[..8],
        &s[8..12],
        &s[12..16],
        &s[16..20],
        &s[20..]
    ))
}
fn uuid_valid(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(i, b)| {
            if matches!(i, 8 | 13 | 18 | 23) {
                b == b'-'
            } else {
                b.is_ascii_digit() || (b'a'..=b'f').contains(&b)
            }
        })
}
fn hash_valid(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn audience_valid(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 255
        && value.as_bytes()[0].is_ascii_alphanumeric()
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._:/-".contains(&b))
}
pub fn canonical_type(value: &str) -> Result<String> {
    let slug = value
        .strip_prefix("synny://type/")
        .or_else(|| value.strip_prefix("syn://type/"))
        .ok_or(Error::Input)?;
    if slug.is_empty()
        || slug.len() > 255
        || !slug.as_bytes()[0].is_ascii_alphanumeric()
        || !slug.as_bytes()[slug.len() - 1].is_ascii_alphanumeric()
    {
        return Err(Error::Input);
    }
    let mut sep = false;
    for byte in slug.bytes() {
        if byte == b'-' || byte == b'_' {
            if sep {
                return Err(Error::Input);
            }
            sep = true;
        } else if byte.is_ascii_digit() || byte.is_ascii_lowercase() {
            sep = false;
        } else {
            return Err(Error::Input);
        }
    }
    Ok(format!("synny://type/{slug}"))
}

/// Closed ASCII protocol objects with integer values have the same compact sorted
/// representation under serde_json's BTreeMap and RFC 8785. Floats are rejected.
pub fn canonical<T: Serialize>(value: &T) -> Result<Vec<u8>> {
    let value = serde_json::to_value(value).map_err(|_| Error::Input)?;
    fn check(v: &Value) -> bool {
        match v {
            Value::String(s) => s.is_ascii(),
            Value::Array(a) => a.iter().all(check),
            Value::Object(o) => o.iter().all(|(k, v)| k.is_ascii() && check(v)),
            Value::Number(n) => n.is_u64(),
            Value::Bool(_) | Value::Null => true,
        }
    }
    if !check(&value) {
        return Err(Error::Input);
    }
    serde_json::to_vec(&value).map_err(|_| Error::Input)
}
fn signed_bytes(prefix: &[u8], value: &Value) -> Result<Vec<u8>> {
    let mut unsigned = value.clone();
    unsigned
        .as_object_mut()
        .ok_or(Error::Input)?
        .remove("signature");
    let mut data = prefix.to_vec();
    data.push(0);
    data.extend(canonical(&unsigned)?);
    Ok(data)
}

// serde_json::Value alone overwrites duplicate object keys. Reject them recursively.
struct Strict(Value);
impl<'de> Deserialize<'de> for Strict {
    fn deserialize<D: Deserializer<'de>>(d: D) -> std::result::Result<Self, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = Strict;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("bounded JSON without duplicate keys or floats")
            }
            fn visit_bool<E: serde::de::Error>(self, v: bool) -> std::result::Result<Strict, E> {
                Ok(Strict(Value::Bool(v)))
            }
            fn visit_unit<E: serde::de::Error>(self) -> std::result::Result<Strict, E> {
                Ok(Strict(Value::Null))
            }
            fn visit_u64<E: serde::de::Error>(self, v: u64) -> std::result::Result<Strict, E> {
                Ok(Strict(v.into()))
            }
            fn visit_i64<E: serde::de::Error>(self, v: i64) -> std::result::Result<Strict, E> {
                if v < 0 {
                    Err(E::custom("negative integer"))
                } else {
                    Ok(Strict(v.into()))
                }
            }
            fn visit_str<E: serde::de::Error>(self, v: &str) -> std::result::Result<Strict, E> {
                Ok(Strict(v.into()))
            }
            fn visit_string<E: serde::de::Error>(
                self,
                v: String,
            ) -> std::result::Result<Strict, E> {
                Ok(Strict(v.into()))
            }
            fn visit_seq<A: SeqAccess<'de>>(
                self,
                mut a: A,
            ) -> std::result::Result<Strict, A::Error> {
                let mut out = Vec::new();
                while let Some(v) = a.next_element::<Strict>()? {
                    out.push(v.0);
                }
                Ok(Strict(out.into()))
            }
            fn visit_map<A: MapAccess<'de>>(
                self,
                mut a: A,
            ) -> std::result::Result<Strict, A::Error> {
                let mut out = serde_json::Map::new();
                while let Some(k) = a.next_key::<String>()? {
                    if out.contains_key(&k) {
                        return Err(serde::de::Error::custom("duplicate key"));
                    }
                    let v = a.next_value::<Strict>()?;
                    out.insert(k, v.0);
                }
                Ok(Strict(out.into()))
            }
        }
        d.deserialize_any(V)
    }
}
pub fn parse<T: serde::de::DeserializeOwned>(data: &[u8]) -> Result<T> {
    if data.is_empty() || data.len() > MAX_JSON {
        return Err(Error::Input);
    }
    let value: Strict = serde_json::from_slice(data).map_err(|_| Error::Input)?;
    fn depth(v: &Value, n: usize) -> bool {
        if n > 32 {
            return false;
        }
        match v {
            Value::Array(a) => a.iter().all(|x| depth(x, n + 1)),
            Value::Object(o) => o.values().all(|x| depth(x, n + 1)),
            _ => true,
        }
    }
    if !depth(&value.0, 0) {
        return Err(Error::Input);
    }
    serde_json::from_value(value.0).map_err(|_| Error::Input)
}

#[derive(Clone, Serialize, Deserialize, Debug, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Policy {
    pub workspace_id: String,
    pub allowed_audiences: Vec<String>,
    pub allowed_payload_types: Vec<String>,
    pub allowed_domains: Vec<String>,
}
impl Policy {
    pub fn validate(&mut self) -> Result<()> {
        if !uuid_valid(&self.workspace_id)
            || self.allowed_audiences.is_empty()
            || self.allowed_audiences.len() > 128
            || self.allowed_payload_types.is_empty()
            || self.allowed_payload_types.len() > 512
            || self.allowed_domains.is_empty()
            || self.allowed_domains.len() > 3
        {
            return Err(Error::Policy);
        }
        if !self.allowed_audiences.iter().all(|a| audience_valid(a)) {
            return Err(Error::Policy);
        }
        for t in &mut self.allowed_payload_types {
            *t = canonical_type(t)?;
        }
        if !self.allowed_domains.iter().all(|d| {
            matches!(
                d.as_str(),
                "potbot.artifact.v2" | "potbot.event.v2" | "potbot.challenge.v2"
            )
        }) {
            return Err(Error::Policy);
        }
        for values in [
            &mut self.allowed_audiences,
            &mut self.allowed_payload_types,
            &mut self.allowed_domains,
        ] {
            values.sort();
            if values.windows(2).any(|v| v[0] == v[1]) {
                return Err(Error::Policy);
            }
        }
        Ok(())
    }
}
#[derive(Serialize, Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct SignRequest {
    pub workspace_id: String,
    pub audience: String,
    pub domain: String,
    pub payload_type_ref: String,
    pub payload_sha256: String,
    pub request_id: String,
    pub validity_seconds: u32,
}
impl SignRequest {
    fn validate(&mut self, policy: &Policy) -> Result<()> {
        self.payload_type_ref = canonical_type(&self.payload_type_ref)?;
        if self.workspace_id != policy.workspace_id
            || !uuid_valid(&self.request_id)
            || !hash_valid(&self.payload_sha256)
            || self.validity_seconds == 0
            || self.validity_seconds > 300
            || !policy.allowed_audiences.contains(&self.audience)
            || !policy.allowed_domains.contains(&self.domain)
            || !policy
                .allowed_payload_types
                .contains(&self.payload_type_ref)
        {
            return Err(Error::Policy);
        }
        Ok(())
    }
}
#[derive(Clone, Serialize, Deserialize, Debug, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PublicIdentity {
    pub type_ref: String,
    pub schema_version: u32,
    pub profile_id: String,
    pub hash_algorithm: String,
    pub signer_hash: String,
    pub public_key_algorithm: String,
    pub public_key_encoding: String,
    pub public_key: String,
}
impl PublicIdentity {
    pub fn validate(&self) -> Result<()> {
        if self.type_ref != "synny://type/public-identity-descriptor"
            || self.schema_version != 2
            || self.profile_id != IDENTITY_PROFILE
            || self.hash_algorithm != "SHA-256"
            || self.public_key_algorithm != "Ed25519"
            || self.public_key_encoding != "base64"
        {
            return Err(Error::Identity);
        }
        if sha256_hex(decode::<32>(&self.public_key)?.as_ref()) != self.signer_hash {
            return Err(Error::Identity);
        }
        Ok(())
    }
}
pub struct Keys {
    pub root: XPrv,
    pub base: XPrv,
}
impl Keys {
    pub fn from_entropy(entropy: &[u8]) -> Result<Self> {
        if entropy.len() != 32 {
            return Err(Error::Identity);
        }
        let mut bytes = Zeroizing::new([0u8; 96]);
        pbkdf2::pbkdf2_hmac::<Sha512>(b"", entropy, 4096, bytes.as_mut());
        bytes[0] &= 0xf8;
        bytes[31] &= 0x1f;
        bytes[31] |= 0x40;
        let root = XPrv::from_slice_verified(bytes.as_ref()).map_err(|_| Error::Identity)?;
        let mut base = XPrv::from_slice_verified(root.as_ref()).map_err(|_| Error::Identity)?;
        for index in BASE_PATH {
            base = base.derive(DerivationScheme::V2, index);
        }
        Ok(Self { root, base })
    }
    pub fn public(&self) -> PublicIdentity {
        let key = self.base.public().public_key();
        PublicIdentity {
            type_ref: "synny://type/public-identity-descriptor".into(),
            schema_version: 2,
            profile_id: IDENTITY_PROFILE.into(),
            hash_algorithm: "SHA-256".into(),
            signer_hash: sha256_hex(&key),
            public_key_algorithm: "Ed25519".into(),
            public_key_encoding: "base64".into(),
            public_key: b64(&key),
        }
    }
    pub fn hourly(&self, epoch: u64) -> Result<XPrv> {
        if epoch > i32::MAX as u64 {
            return Err(Error::Clock);
        }
        Ok(self
            .base
            .derive(DerivationScheme::V2, HARDENED + 1)
            .derive(DerivationScheme::V2, HARDENED + epoch as u32))
    }
}
#[derive(Serialize, Deserialize, Zeroize, ZeroizeOnDrop)]
#[serde(deny_unknown_fields)]
struct Secrets {
    entropy: String,
    root_private_key: String,
    root_public_key: String,
    base_private_key: String,
    base_public_key: String,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Reservation {
    request_id: String,
    expires: u64,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PrivateState {
    schema_version: u32,
    vault_id: String,
    key_epoch: String,
    generation: u64,
    secrets: Secrets,
    pub policy: Policy,
    signing_profile: String,
    epoch_high_water: u64,
    reservations: Vec<Reservation>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    agent_allocations: Vec<provisioning::Allocation>,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Slot {
    slot_id: String,
    auth_key_id: String,
    salt_b64: String,
    hkdf_salt_b64: String,
    wrapped_vault_key_b64: String,
    memory_kib: u32,
    iterations: u32,
    parallelism: u32,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Payload {
    nonce_b64: String,
    ciphertext_b64: String,
    tag_b64: String,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Vault {
    pub type_ref: String,
    pub schema_version: u32,
    pub protection_profile: String,
    pub vault_id: String,
    pub key_epoch: String,
    pub generation: u64,
    pub identity: PublicIdentity,
    slot: Slot,
    wrapped_data_key_b64: String,
    payload: Payload,
}

fn bind(vault: &Vault) -> Result<Vec<u8>> {
    canonical(
        &json!({"protection_profile":vault.protection_profile,"identity_profile":IDENTITY_PROFILE,"vault_id":vault.vault_id,"key_epoch":vault.key_epoch,"wallet_public_key_hash":vault.identity.signer_hash,"slot":{"slot_id":vault.slot.slot_id,"salt_b64":vault.slot.salt_b64,"hkdf_salt_b64":vault.slot.hkdf_salt_b64,"memory_kib":65536,"iterations":3,"parallelism":4,"method":"password","algorithm":"Argon2id-v19/HKDF-SHA-256/AES-256-KW"}}),
    )
}
fn auth_id(vault: &Vault) -> Result<[u8; 32]> {
    let mut h = Sha256::new();
    h.update(b"potbot-native-auth-v1\0");
    h.update(bind(vault)?);
    Ok(h.finalize().into())
}
fn slot_key(vault: &Vault, password: &[u8]) -> Result<Zeroizing<[u8; 32]>> {
    if password.is_empty() || password.len() > 1024 {
        return Err(Error::Input);
    }
    let salt = decode::<16>(&vault.slot.salt_b64)?;
    let mut factor = Zeroizing::new([0u8; 32]);
    let params = Params::new(65536, 3, 4, Some(32)).map_err(|_| Error::Protection)?;
    let argon = Argon2::new(Algorithm::Argon2id, Version::V0x13, params);
    // Production allocator locks, guards and wipes all 64 MiB before KDF use.
    let mut blocks = vec![argon2::Block::default(); 65536];
    let derived = argon
        .hash_password_into_with_memory(password, salt.as_ref(), factor.as_mut(), &mut blocks)
        .map_err(|_| Error::Authentication);
    blocks.zeroize();
    drop(blocks);
    derived?;
    let mut info = b"potbot-native-slot-v1\0".to_vec();
    info.extend(auth_id(vault)?);
    let mut key = Zeroizing::new([0u8; 32]);
    Hkdf::<Sha256>::new(
        Some(decode::<32>(&vault.slot.hkdf_salt_b64)?.as_ref()),
        factor.as_ref(),
    )
    .expand(&info, key.as_mut())
    .map_err(|_| Error::Authentication)?;
    Ok(key)
}
fn wrap(key: &[u8; 32], value: &[u8; 32]) -> Result<String> {
    let kek = aes_kw::KekAes256::from(*key);
    let mut out = [0u8; 40];
    kek.wrap(value, &mut out)
        .map_err(|_| Error::Authentication)?;
    Ok(b64(&out))
}
fn unwrap(key: &[u8; 32], value: &str) -> Result<Zeroizing<[u8; 32]>> {
    let kek = aes_kw::KekAes256::from(*key);
    let bytes = decode::<40>(value)?;
    let mut out = Zeroizing::new([0u8; 32]);
    kek.unwrap(bytes.as_ref(), out.as_mut())
        .map_err(|_| Error::Authentication)?;
    Ok(out)
}
fn aad(vault: &Vault) -> Result<Vec<u8>> {
    let mut value = serde_json::to_value(vault).map_err(|_| Error::Input)?;
    let payload = value
        .get_mut("payload")
        .and_then(Value::as_object_mut)
        .ok_or(Error::Input)?;
    payload.remove("ciphertext_b64");
    payload.remove("tag_b64");
    let mut bytes = b"potbot-native-vault-v1\0".to_vec();
    bytes.extend(canonical(&value)?);
    Ok(bytes)
}

impl Vault {
    fn validate(&self) -> Result<()> {
        self.identity.validate()?;
        if self.type_ref != "synny://type/keystore-envelope"
            || self.schema_version != 1
            || self.protection_profile != PROTECTION_PROFILE
            || !uuid_valid(&self.vault_id)
            || !uuid_valid(&self.key_epoch)
            || self.generation == 0
            || self.generation > 9_007_199_254_740_991
            || self.slot.memory_kib != 65536
            || self.slot.iterations != 3
            || self.slot.parallelism != 4
            || !hash_valid(&self.slot.slot_id)
            || hex(&auth_id(self)?) != self.slot.auth_key_id
        {
            return Err(Error::Input);
        }
        decode::<16>(&self.slot.salt_b64)?;
        decode::<32>(&self.slot.hkdf_salt_b64)?;
        decode::<40>(&self.slot.wrapped_vault_key_b64)?;
        decode::<40>(&self.wrapped_data_key_b64)?;
        decode::<12>(&self.payload.nonce_b64)?;
        decode::<16>(&self.payload.tag_b64)?;
        Ok(())
    }
    pub fn public(&self) -> Result<PublicIdentity> {
        self.validate()?;
        Ok(self.identity.clone())
    }
    fn with_identity(
        password: &[u8],
        identity: PublicIdentity,
    ) -> Result<(Self, Zeroizing<[u8; 32]>)> {
        identity.validate()?;
        let mut vault = Self {
            type_ref: "synny://type/keystore-envelope".into(),
            schema_version: 1,
            protection_profile: PROTECTION_PROFILE.into(),
            vault_id: uuid()?,
            key_epoch: uuid()?,
            generation: 1,
            identity,
            slot: Slot {
                slot_id: hex(random::<32>()?.as_ref()),
                auth_key_id: String::new(),
                salt_b64: b64(random::<16>()?.as_ref()),
                hkdf_salt_b64: b64(random::<32>()?.as_ref()),
                wrapped_vault_key_b64: String::new(),
                memory_kib: 65536,
                iterations: 3,
                parallelism: 4,
            },
            wrapped_data_key_b64: String::new(),
            payload: Payload {
                nonce_b64: String::new(),
                ciphertext_b64: String::new(),
                tag_b64: String::new(),
            },
        };
        vault.slot.auth_key_id = hex(&auth_id(&vault)?);
        let master = random::<32>()?;
        vault.slot.wrapped_vault_key_b64 = wrap(&*slot_key(&vault, password)?, &master)?;
        Ok((vault, master))
    }
    pub fn create(
        password: &[u8],
        mut policy: Policy,
        now: u64,
        entropy: Option<&[u8; 32]>,
        expected_hash: Option<&str>,
    ) -> Result<Self> {
        policy.validate()?;
        epoch(now)?;
        let owned = if let Some(supplied) = entropy {
            Zeroizing::new(*supplied)
        } else {
            random::<32>()?
        };
        let keys = Keys::from_entropy(owned.as_ref())?;
        let identity = keys.public();
        if expected_hash.is_some_and(|h| h != identity.signer_hash) {
            return Err(Error::Identity);
        }
        let (mut vault, master) = Self::with_identity(password, identity)?;
        let state = PrivateState {
            schema_version: 1,
            vault_id: vault.vault_id.clone(),
            key_epoch: vault.key_epoch.clone(),
            generation: 1,
            secrets: Secrets {
                entropy: b64(owned.as_ref()),
                root_private_key: b64(keys.root.as_ref()),
                root_public_key: b64(keys.root.public().as_ref()),
                base_private_key: b64(keys.base.as_ref()),
                base_public_key: b64(keys.base.public().public_key_slice()),
            },
            policy,
            signing_profile: SIGNING_PROFILE.into(),
            epoch_high_water: epoch(now)?,
            reservations: Vec::new(),
            agent_allocations: Vec::new(),
        };
        vault.seal(&master, &state)?;
        vault.validate()?;
        Ok(vault)
    }
    pub fn unlock(&self, password: &[u8], expected_hash: &str) -> Result<Unlocked> {
        let (master, mut state): (_, PrivateState) = self.open_private(password, expected_hash)?;
        state.policy.validate()?;
        if !matches!(state.schema_version, 1 | 2)
            || (state.schema_version == 1 && !state.agent_allocations.is_empty())
            || state.vault_id != self.vault_id
            || state.key_epoch != self.key_epoch
            || state.generation != self.generation
            || state.signing_profile != SIGNING_PROFILE
            || state.epoch_high_water > i32::MAX as u64
            || state.reservations.len() > 1024
        {
            return Err(Error::Identity);
        }
        provisioning::validate_allocations(&state.agent_allocations)?;
        let mut ids = BTreeSet::new();
        if state
            .reservations
            .iter()
            .any(|r| !uuid_valid(&r.request_id) || !ids.insert(&r.request_id))
        {
            return Err(Error::Identity);
        }
        let entropy = decode::<32>(&state.secrets.entropy)?;
        let keys = Keys::from_entropy(entropy.as_ref())?;
        let root = decode::<96>(&state.secrets.root_private_key)?;
        let base = decode::<96>(&state.secrets.base_private_key)?;
        let rootpub = decode::<64>(&state.secrets.root_public_key)?;
        let basepub = decode::<32>(&state.secrets.base_public_key)?;
        if !bool::from(keys.root.as_ref().ct_eq(root.as_ref()))
            || !bool::from(keys.base.as_ref().ct_eq(base.as_ref()))
            || !bool::from(keys.root.public().as_ref().ct_eq(rootpub.as_ref()))
            || !bool::from(
                keys.base
                    .public()
                    .public_key_slice()
                    .ct_eq(basepub.as_ref()),
            )
            || keys.public() != self.identity
        {
            return Err(Error::Identity);
        }
        Ok(Unlocked {
            master,
            state,
            keys,
        })
    }
    fn open_private<T: for<'de> Deserialize<'de>>(
        &self,
        password: &[u8],
        expected_hash: &str,
    ) -> Result<(Zeroizing<[u8; 32]>, T)> {
        self.validate()?;
        if !hash_valid(expected_hash) || expected_hash != self.identity.signer_hash {
            return Err(Error::Identity);
        }
        let master = unwrap(
            &*slot_key(self, password)?,
            &self.slot.wrapped_vault_key_b64,
        )?;
        let key = unwrap(&master, &self.wrapped_data_key_b64)?;
        let mut plaintext = Zeroizing::new(
            STANDARD
                .decode(&self.payload.ciphertext_b64)
                .map_err(|_| Error::Input)?,
        );
        if b64(&plaintext) != self.payload.ciphertext_b64 || plaintext.len() > MAX_JSON {
            return Err(Error::Input);
        }
        let nonce = decode::<12>(&self.payload.nonce_b64)?;
        let tag = decode::<16>(&self.payload.tag_b64)?;
        Aes256Gcm::new_from_slice(key.as_ref())
            .map_err(|_| Error::Authentication)?
            .decrypt_in_place_detached(
                Nonce::from_slice(nonce.as_ref()),
                &aad(self)?,
                plaintext.as_mut(),
                Tag::from_slice(tag.as_ref()),
            )
            .map_err(|_| Error::Authentication)?;
        Ok((master, parse(&plaintext)?))
    }
    fn seal(&mut self, master: &[u8; 32], state: &impl Serialize) -> Result<()> {
        let key = random::<32>()?;
        self.wrapped_data_key_b64 = wrap(master, &key)?;
        let nonce = random::<12>()?;
        self.payload.nonce_b64 = b64(nonce.as_ref());
        let mut plaintext = Zeroizing::new(canonical(state)?);
        let tag = Aes256Gcm::new_from_slice(key.as_ref())
            .map_err(|_| Error::Authentication)?
            .encrypt_in_place_detached(
                Nonce::from_slice(nonce.as_ref()),
                &aad(self)?,
                plaintext.as_mut(),
            )
            .map_err(|_| Error::Authentication)?;
        self.payload.ciphertext_b64 = b64(&plaintext);
        self.payload.tag_b64 = b64(&tag);
        Ok(())
    }
}
pub struct Unlocked {
    master: Zeroizing<[u8; 32]>,
    state: PrivateState,
    keys: Keys,
}
impl Unlocked {
    pub fn recovery_words(&self) -> Result<Zeroizing<String>> {
        let entropy = decode::<32>(&self.state.secrets.entropy)?;
        let mnemonic =
            bip39::Mnemonic::from_entropy(entropy.as_ref()).map_err(|_| Error::Identity)?;
        Ok(Zeroizing::new(mnemonic.to_string()))
    }
    /// Reserves before returning an envelope to the caller. The caller MUST durably
    /// persist the resulting encrypted vault before signature release.
    pub fn sign_reserved(
        &mut self,
        vault: &mut Vault,
        mut request: SignRequest,
        now: u64,
    ) -> Result<Value> {
        request.validate(&self.state.policy)?;
        let e = epoch(now)?;
        if e < self.state.epoch_high_water {
            return Err(Error::Clock);
        }
        self.state
            .reservations
            .retain(|r| r.expires.saturating_add(30) >= now);
        if self
            .state
            .reservations
            .iter()
            .any(|r| r.request_id == request.request_id)
            || self.state.reservations.len() >= 1024
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
        self.state.epoch_high_water = e;
        vault.generation = vault
            .generation
            .checked_add(1)
            .filter(|g| *g <= 9_007_199_254_740_991)
            .ok_or(Error::Storage)?;
        self.state.generation = vault.generation;
        vault.seal(&self.master, &self.state)?;
        let hourly = self.keys.hourly(e)?;
        let mut delegation = json!({"type_ref":"synny://type/hourly-key-delegation","schema_version":1,"profile_id":IDENTITY_PROFILE,"signing_profile_id":SIGNING_PROFILE,"signer_hash":vault.identity.signer_hash,"signing_epoch":e,"valid_from":e*3600,"valid_until":(e+1)*3600,"public_key_algorithm":"Ed25519","public_key_encoding":"base64","public_key":b64(hourly.public().public_key_slice()),"signature_algorithm":"Ed25519","signature_encoding":"base64"});
        delegation["signature"] = b64(self
            .keys
            .base
            .sign::<()>(&signed_bytes(b"potbot-hourly-delegation-v1", &delegation)?)
            .as_ref())
        .into();
        let mut envelope = json!({"type_ref":"synny://type/signature-envelope","schema_version":2,"profile_id":IDENTITY_PROFILE,"signer_hash":vault.identity.signer_hash,"domain":request.domain,"workspace_id":request.workspace_id,"payload_type_ref":request.payload_type_ref,"payload_sha256":request.payload_sha256,"audience":request.audience,"request_id":request.request_id,"issued_at":utc(now)?,"expires_at":utc(expires)?,"signature_algorithm":"Ed25519","signature_encoding":"base64","signing_profile_id":SIGNING_PROFILE,"signing_epoch":e});
        envelope["signature"] = b64(hourly
            .sign::<()>(&signed_bytes(b"potbot-signature-v2", &envelope)?)
            .as_ref())
        .into();
        Ok(
            json!({"type_ref":"synny://type/public-signature-bundle","schema_version":1,"identity":vault.identity,"delegation":delegation,"envelope":envelope}),
        )
    }
    pub fn change_password(&mut self, vault: &mut Vault, new_password: &[u8]) -> Result<()> {
        vault.key_epoch = uuid()?;
        self.state.key_epoch = vault.key_epoch.clone();
        vault.generation = vault.generation.checked_add(1).ok_or(Error::Storage)?;
        self.state.generation = vault.generation;
        vault.slot.slot_id = hex(random::<32>()?.as_ref());
        vault.slot.salt_b64 = b64(random::<16>()?.as_ref());
        vault.slot.hkdf_salt_b64 = b64(random::<32>()?.as_ref());
        vault.slot.auth_key_id = hex(&auth_id(vault)?);
        self.master = random::<32>()?;
        vault.slot.wrapped_vault_key_b64 = wrap(&*slot_key(vault, new_password)?, &self.master)?;
        vault.seal(&self.master, &self.state)
    }
}

pub fn entropy_from_words(words: &str) -> Result<Zeroizing<[u8; 32]>> {
    let mnemonic = bip39::Mnemonic::parse_in_normalized(bip39::Language::English, words)
        .map_err(|_| Error::Input)?;
    if mnemonic.word_count() != 24 {
        return Err(Error::Input);
    }
    let mut bytes = mnemonic.to_entropy();
    let mut out = Zeroizing::new([0u8; 32]);
    if bytes.len() != 32 {
        return Err(Error::Input);
    }
    out.copy_from_slice(&bytes);
    bytes.zeroize();
    Ok(out)
}
pub fn epoch(now: u64) -> Result<u64> {
    if now > 253_402_300_799 {
        return Err(Error::Clock);
    }
    let e = now / 3600;
    if e > i32::MAX as u64 {
        Err(Error::Clock)
    } else {
        Ok(e)
    }
}
fn utc(now: u64) -> Result<String> {
    epoch(now)?;
    OffsetDateTime::from_unix_timestamp(now as i64)
        .map_err(|_| Error::Clock)?
        .format(format_description!(
            "[year]-[month]-[day]T[hour]:[minute]:[second]Z"
        ))
        .map_err(|_| Error::Clock)
}
pub fn now() -> Result<u64> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| Error::Clock)
        .map(|v| v.as_secs())
}
pub struct ClockGuard {
    wall: u64,
    mono: Instant,
}
impl ClockGuard {
    pub fn new() -> Result<Self> {
        Ok(Self {
            wall: now()?,
            mono: Instant::now(),
        })
    }
    pub fn observed(&self) -> Result<u64> {
        let current = now()?;
        let expected = self
            .wall
            .checked_add(self.mono.elapsed().as_secs())
            .ok_or(Error::Clock)?;
        if current.abs_diff(expected) > 30 {
            Err(Error::Clock)
        } else {
            Ok(current)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn policy() -> Policy {
        Policy {
            workspace_id: "00000000-0000-4000-8000-000000000001".into(),
            allowed_audiences: vec!["test".into()],
            allowed_payload_types: vec!["syn://type/ember".into()],
            allowed_domains: vec!["potbot.event.v2".into()],
        }
    }
    fn request(id: &str) -> SignRequest {
        SignRequest {
            workspace_id: policy().workspace_id,
            audience: "test".into(),
            domain: "potbot.event.v2".into(),
            payload_type_ref: "syn://type/ember".into(),
            payload_sha256: sha256_hex(b"fixture"),
            request_id: id.into(),
            validity_seconds: 300,
        }
    }
    #[test]
    fn strict_json_rejects_duplicates_floats_and_negative() {
        assert!(parse::<Value>(br#"{"x":1,"x":2}"#).is_err());
        assert!(parse::<Value>(br#"{"x":1.5}"#).is_err());
        assert!(parse::<Value>(br#"{"x":-1}"#).is_err());
    }
    #[test]
    fn aliases_and_property_scope() {
        assert_eq!(
            canonical_type("syn://type/ember").unwrap(),
            "synny://type/ember"
        );
        assert!(canonical_type("syn://type/../x").is_err());
        let mut p = policy();
        p.validate().unwrap();
        let mut r = request("00000000-0000-4000-8000-000000000002");
        r.validate(&p).unwrap();
        r.audience = "other".into();
        assert_eq!(r.validate(&p), Err(Error::Policy));
    }
    #[test]
    fn epoch_boundaries() {
        assert_eq!(epoch(3599).unwrap(), 0);
        assert_eq!(epoch(3600).unwrap(), 1);
        assert_eq!(utc(1792584000).unwrap(), "2026-10-21T12:00:00Z");
        assert!(epoch(u64::MAX).is_err());
    }
    #[test]
    fn icarus_official_vector() {
        // Public CIP-3 Icarus no-passphrase test vector (15 words, 20-byte entropy).
        let words="eight country switch draw meat scout mystery blade tip drift useless good keep usage title";
        let mnemonic = bip39::Mnemonic::parse(words).unwrap();
        let entropy = mnemonic.to_entropy();
        let mut out = [0u8; 96];
        pbkdf2::pbkdf2_hmac::<Sha512>(b"", &entropy, 4096, &mut out);
        out[0] &= 0xf8;
        out[31] &= 0x1f;
        out[31] |= 0x40;
        assert_eq!(hex(&out),"c065afd2832cd8b087c4d9ab7011f481ee1e0721e78ea5dd609f3ab3f156d245d176bd8fd4ec60b4731c3918a2a72a0226c0cd119ec35b47e4d55884667f552a23f7fdcd4a10c6cd2c7393ac61d877873e248f417634aa3d812af327ffe9d620");
    }
    #[test]
    fn vault_roundtrip_tamper_rotation_and_replay() {
        let entropy = [42u8; 32];
        let timestamp = 1792584000u64;
        let mut vault = Vault::create(
            b"test-only password",
            policy(),
            timestamp,
            Some(&entropy),
            None,
        )
        .unwrap();
        let expected = vault.identity.signer_hash.clone();
        assert!(matches!(
            vault.unlock(b"wrong", &expected),
            Err(Error::Authentication)
        ));
        assert!(matches!(
            vault.unlock(b"test-only password", &"0".repeat(64)),
            Err(Error::Identity)
        ));
        let mut unlocked = vault.unlock(b"test-only password", &expected).unwrap();
        let words = unlocked.recovery_words().unwrap();
        assert_eq!(*entropy_from_words(&words).unwrap(), entropy);
        let signed = unlocked
            .sign_reserved(
                &mut vault,
                request("00000000-0000-4000-8000-000000000002"),
                timestamp,
            )
            .unwrap();
        assert_eq!(signed["envelope"]["signing_epoch"], 497940);
        assert_eq!(signed["envelope"]["payload_type_ref"], "synny://type/ember");
        let cert = &signed["delegation"];
        let certsig = ed25519_bip32::Signature::<()>::from_slice(
            &STANDARD
                .decode(cert["signature"].as_str().unwrap())
                .unwrap(),
        )
        .unwrap();
        assert!(unlocked.keys.base.public().verify(
            &signed_bytes(b"potbot-hourly-delegation-v1", cert).unwrap(),
            &certsig
        ));
        let leaf = unlocked.keys.hourly(497940).unwrap();
        let envsig = ed25519_bip32::Signature::<()>::from_slice(
            &STANDARD
                .decode(signed["envelope"]["signature"].as_str().unwrap())
                .unwrap(),
        )
        .unwrap();
        assert!(leaf.public().verify(
            &signed_bytes(b"potbot-signature-v2", &signed["envelope"]).unwrap(),
            &envsig
        ));
        let mut reopened = vault.unlock(b"test-only password", &expected).unwrap();
        assert_eq!(
            reopened.sign_reserved(
                &mut vault,
                request("00000000-0000-4000-8000-000000000002"),
                timestamp
            ),
            Err(Error::Replay)
        );
        assert_eq!(
            reopened.sign_reserved(
                &mut vault,
                request("00000000-0000-4000-8000-000000000003"),
                timestamp - 3600
            ),
            Err(Error::Clock)
        );
        let mut tampered = vault.clone();
        tampered.generation += 1;
        assert!(tampered.unlock(b"test-only password", &expected).is_err());
        reopened
            .change_password(&mut vault, b"replacement test password")
            .unwrap();
        assert!(vault.unlock(b"test-only password", &expected).is_err());
        vault
            .unlock(b"replacement test password", &expected)
            .unwrap();
        let public = canonical(&signed).unwrap();
        assert!(!String::from_utf8(public).unwrap().contains(&b64(&entropy)));
    }
    #[test]
    fn public_fixture_export_for_independent_verifier() {
        let timestamp = 1792584000u64;
        let mut vault = Vault::create(
            b"test-only password",
            policy(),
            timestamp,
            Some(&[42u8; 32]),
            None,
        )
        .unwrap();
        let mut state = vault
            .unlock(b"test-only password", &vault.identity.signer_hash)
            .unwrap();
        let bundle = state
            .sign_reserved(
                &mut vault,
                request("00000000-0000-4000-8000-000000000002"),
                timestamp,
            )
            .unwrap();
        if let Ok(path) = std::env::var("POTBOT_TEST_PUBLIC_FIXTURE") {
            std::fs::write(path, canonical(&bundle).unwrap()).unwrap();
        }
    }
}
