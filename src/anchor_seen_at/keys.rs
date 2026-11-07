//! Pinned, scoped key allowlist (design section 4, key policy).
//!
//! The allowlist arrives through a separately authenticated release/trust channel (not built
//! here; tests construct it from synthetic keys). Keys or URLs inside an acknowledgment are never
//! trusted: an ack names only a `key_id`, and the wire types reject any other key material.
//!
//! Historical validity is not taken from the service's own signed wall time. A retired key is
//! accepted only for ack ids the trust channel archived while that key was current; a revoked key
//! is never accepted and leaves its evidence unknown pending explicit archival re-review.

use super::{canon::ensure, Reason, ALG_ED25519};
use crate::evidence;
use ed25519_dalek::VerifyingKey;
use std::collections::BTreeSet;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum KeyScope { Challenge, Ack }

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum KeyStatus {
    Active,
    Retired { archived_ack_ids: BTreeSet<String> },
    Revoked,
}

#[derive(Clone, Debug)]
pub struct PinnedKey {
    pub key_id: String,
    pub alg: String,
    pub public_key: VerifyingKey,
    pub scope: KeyScope,
    pub service: String,
    pub audience: String,
    pub status: KeyStatus,
}

pub fn key_id_for(pk: &VerifyingKey) -> String { evidence::sha256_hex(pk.as_bytes()) }

impl PinnedKey {
    pub fn new(public_key: VerifyingKey, scope: KeyScope, service: &str, audience: &str, status: KeyStatus) -> Self {
        PinnedKey { key_id: key_id_for(&public_key), alg: ALG_ED25519.into(), public_key, scope, service: service.into(), audience: audience.into(), status }
    }
}

#[derive(Clone, Debug)]
pub struct TrustPolicy {
    pub trust_policy_id: String,
    pub service: String,
    pub audience: String,
    pub policy_ids: BTreeSet<String>,
    keys: Vec<PinnedKey>,
}

pub struct KeyLookup<'a> {
    pub key_id: &'a str,
    pub alg: &'a str,
    pub scope: KeyScope,
    pub service: &'a str,
    pub audience: &'a str,
    pub policy_id: &'a str,
    /// Set for acks so a retired key can be matched against the archive.
    pub ack_id: Option<&'a str>,
}

impl TrustPolicy {
    /// Rejects duplicate key ids and a key whose service/audience differ from the policy's.
    pub fn new(trust_policy_id: &str, service: &str, audience: &str, policy_ids: &[&str], keys: Vec<PinnedKey>) -> Result<Self, Reason> {
        let mut seen = BTreeSet::new();
        for k in &keys {
            ensure(seen.insert(k.key_id.clone()), Reason::SchemaViolation)?;
            ensure(k.service == service && k.audience == audience && k.alg == ALG_ED25519, Reason::KeyServiceMismatch)?;
            ensure(k.key_id == key_id_for(&k.public_key), Reason::SchemaViolation)?;
        }
        Ok(TrustPolicy {
            trust_policy_id: trust_policy_id.into(),
            service: service.into(),
            audience: audience.into(),
            policy_ids: policy_ids.iter().map(|s| s.to_string()).collect(),
            keys,
        })
    }

    pub(crate) fn archive(&self) -> serde_json::Value {
        let keys: Vec<_> = self.keys.iter().map(|k| {
            let status = match &k.status {
                KeyStatus::Active => serde_json::json!({"kind":"active"}),
                KeyStatus::Revoked => serde_json::json!({"kind":"revoked"}),
                KeyStatus::Retired { archived_ack_ids } => serde_json::json!({"kind":"retired", "archived_ack_ids":archived_ack_ids}),
            };
            serde_json::json!({"key_id":k.key_id, "alg":k.alg, "public_key":super::canon::b64url(k.public_key.as_bytes()),
                "scope":match k.scope { KeyScope::Challenge => "challenge", KeyScope::Ack => "ack" },
                "service":k.service, "audience":k.audience, "status":status})
        }).collect();
        serde_json::json!({"trust_policy_id":self.trust_policy_id, "service":self.service,
            "audience":self.audience, "policy_ids":self.policy_ids, "keys":keys})
    }

    pub fn resolve(&self, q: &KeyLookup) -> Result<VerifyingKey, Reason> {
        ensure(q.service == self.service && q.audience == self.audience, Reason::KeyServiceMismatch)?;
        ensure(self.policy_ids.contains(q.policy_id), Reason::PolicyMismatch)?;
        ensure(q.alg == ALG_ED25519, Reason::AlgorithmMismatch)?;
        let k = self.keys.iter().find(|k| k.key_id == q.key_id).ok_or(Reason::UnknownKey)?;
        ensure(k.scope == q.scope, Reason::KeyScopeMismatch)?;
        match &k.status {
            KeyStatus::Active => Ok(k.public_key),
            KeyStatus::Revoked => Err(Reason::KeyRevoked),
            KeyStatus::Retired { archived_ack_ids } => match q.ack_id {
                Some(id) if archived_ack_ids.contains(id) => Ok(k.public_key),
                _ => Err(Reason::KeyRetiredNotArchived),
            },
        }
    }
}
