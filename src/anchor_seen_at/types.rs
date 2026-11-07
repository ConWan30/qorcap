//! Proposed wire types: seal input, challenge request, signed challenge, commitment, signed ack.
//! Every struct rejects unknown fields; optional fields are absent, never `null`.

use super::canon::{self, dec_i64, dec_u64, ensure, is_hex64, is_id};
use super::{Reason, ALG_ED25519, DOMAIN_ACK, DOMAIN_ACK_ID, DOMAIN_CHALLENGE, DOMAIN_COMMITMENT, DOMAIN_SEAL};
use ed25519_dalek::{Signer, SigningKey};
use serde::{Deserialize, Serialize};

pub const SCHEMA_CHALLENGE_REQUEST: &str = "proposed.aux.seen_at.challenge_request.v1";
pub const SCHEMA_CHALLENGE: &str = "proposed.aux.seen_at.challenge.v1";
pub const SCHEMA_COMMITMENT_REQUEST: &str = "proposed.aux.seen_at.commitment_request.v1";
pub const SCHEMA_COMMITMENT: &str = "proposed.aux.seen_at.commitment.v1";
pub const SCHEMA_ACK: &str = "proposed.aux.seen_at.ack.v1";
pub const HASH_ALG: &str = "sha256";
pub const MONO_UNIT: &str = "ns";
pub const WALL_UNIT: &str = "unix_us";
/// The service's wall time is an audit observation, never a synchrony claim.
pub const WALL_QUALITY: &str = "unsynchronized_audit_only";

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Purpose { Opening, Interval, Close }

fn short_text(s: &str) -> bool {
    !s.is_empty() && s.len() <= 128 && s.bytes().all(|c| c.is_ascii_alphanumeric() || b"._:-/".contains(&c))
}

/// One genuine capture position: capture id, sequence, and its clock domain.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CaptureRef { pub capture_id: String, pub seq: String, pub clock_domain_id: String }

/// `seal_hash = H("QOR-SEAL-v1", {...})`. The challenge nonce is deliberately not in here: it binds
/// through the opening commitment, so the seal has no circular dependency.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SealInput {
    pub session_id: String,
    pub device_epoch_id: String,
    pub capture_epoch_id: String,
    pub echo_encoding_id: String,
    /// Exact recorded echo bytes, lowercase hex, no prefix. Never rebuilt from a later file.
    pub echo_bytes_hex: String,
    pub opening_start_ref: CaptureRef,
    pub opening_end_ref: CaptureRef,
}

impl CaptureRef {
    fn validate(&self) -> Result<(), Reason> {
        ensure(is_id(&self.capture_id) && dec_u64(&self.seq).is_some() && is_id(&self.clock_domain_id), Reason::FieldFormat)
    }
}

impl SealInput {
    pub fn validate(&self) -> Result<(), Reason> {
        ensure(is_id(&self.session_id) && is_id(&self.device_epoch_id) && is_id(&self.capture_epoch_id), Reason::FieldFormat)?;
        ensure(short_text(&self.echo_encoding_id), Reason::FieldFormat)?;
        let e = &self.echo_bytes_hex;
        ensure(!e.is_empty() && e.len() % 2 == 0 && canon::is_hex_len(e, e.len()), Reason::FieldFormat)?;
        self.opening_start_ref.validate()?;
        self.opening_end_ref.validate()?;
        let (a, b) = (dec_u64(&self.opening_start_ref.seq), dec_u64(&self.opening_end_ref.seq));
        ensure(a <= b, Reason::FieldFormat)
    }
    pub fn seal_hash(&self) -> Result<String, Reason> {
        self.validate()?;
        canon::digest(DOMAIN_SEAL, self)
    }
}

/// Body of `POST /aux/v1/seen-at/challenges`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChallengeRequest {
    pub schema: String,
    pub purpose: Purpose,
    pub service: String,
    pub audience: String,
    pub policy_id: String,
    pub session_id: String,
    pub device_epoch_id: String,
    pub capture_epoch_id: String,
    pub client_nonce: String,
}

impl ChallengeRequest {
    pub fn validate(&self) -> Result<(), Reason> {
        ensure(self.schema == SCHEMA_CHALLENGE_REQUEST, Reason::WrongSchema)?;
        ensure(short_text(&self.service) && short_text(&self.audience) && short_text(&self.policy_id), Reason::FieldFormat)?;
        ensure(is_id(&self.session_id) && is_id(&self.device_epoch_id) && is_id(&self.capture_epoch_id), Reason::FieldFormat)?;
        ensure(is_hex64(&self.client_nonce), Reason::FieldFormat)
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChallengeUnsigned {
    pub schema: String,
    pub key_id: String,
    pub alg: String,
    pub purpose: Purpose,
    pub service: String,
    pub audience: String,
    pub policy_id: String,
    pub challenge_id: String,
    pub server_nonce: String,
    pub client_nonce: String,
    pub session_id: String,
    pub device_epoch_id: String,
    pub capture_epoch_id: String,
    pub server_boot_id: String,
    pub server_clock_epoch_id: String,
    pub mono_unit: String,
    pub wall_unit: String,
    pub issued_mono_ns: String,
    pub expires_mono_ns: String,
    pub issued_wall_us: String,
}

impl ChallengeUnsigned {
    pub fn validate(&self) -> Result<(), Reason> {
        ensure(self.schema == SCHEMA_CHALLENGE, Reason::WrongSchema)?;
        ensure(self.alg == ALG_ED25519, Reason::AlgorithmMismatch)?;
        ensure(is_hex64(&self.key_id) && is_hex64(&self.challenge_id), Reason::FieldFormat)?;
        ensure(is_hex64(&self.server_nonce) && is_hex64(&self.client_nonce), Reason::FieldFormat)?;
        ensure(short_text(&self.service) && short_text(&self.audience) && short_text(&self.policy_id), Reason::FieldFormat)?;
        ensure(is_id(&self.session_id) && is_id(&self.device_epoch_id) && is_id(&self.capture_epoch_id), Reason::FieldFormat)?;
        ensure(is_id(&self.server_boot_id) && is_id(&self.server_clock_epoch_id), Reason::FieldFormat)?;
        ensure(self.mono_unit == MONO_UNIT && self.wall_unit == WALL_UNIT, Reason::FieldFormat)?;
        let (i, e) = (dec_u64(&self.issued_mono_ns), dec_u64(&self.expires_mono_ns));
        ensure(i.is_some() && e.is_some() && i < e && dec_i64(&self.issued_wall_us).is_some(), Reason::FieldFormat)
    }
    pub fn issued(&self) -> u64 { dec_u64(&self.issued_mono_ns).unwrap_or(u64::MAX) }
    pub fn expires(&self) -> u64 { dec_u64(&self.expires_mono_ns).unwrap_or(0) }
    /// Digest of the exact signed challenge message; the commitment carries it.
    pub fn digest(&self) -> Result<String, Reason> { canon::digest(DOMAIN_CHALLENGE, self) }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SignedChallenge { pub unsigned_payload: ChallengeUnsigned, pub signature: String }

impl SignedChallenge {
    pub fn sign(unsigned: ChallengeUnsigned, key: &SigningKey) -> Result<Self, Reason> {
        let msg = canon::signing_message(DOMAIN_CHALLENGE, &unsigned)?;
        Ok(SignedChallenge { signature: canon::b64url(&key.sign(&msg).to_bytes()), unsigned_payload: unsigned })
    }
}

/// Exact chain coverage of one commitment. `predecessor_seq` is absent only when the prefix starts
/// at sequence 0 from the zero head. No "latest hash": every head names its sequence.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChainRange {
    pub chain_id: String,
    pub chain_schema: String,
    pub algorithm: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub predecessor_seq: Option<String>,
    pub predecessor_head: String,
    pub first_seq: String,
    pub last_seq: String,
    pub end_head: String,
    pub checkpoint_id: String,
}

impl ChainRange {
    pub fn validate(&self) -> Result<(), Reason> {
        ensure(is_id(&self.chain_id) && short_text(&self.chain_schema) && self.algorithm == HASH_ALG, Reason::FieldFormat)?;
        ensure(is_hex64(&self.predecessor_head) && is_hex64(&self.end_head) && is_hex64(&self.checkpoint_id), Reason::FieldFormat)?;
        let (f, l) = (dec_u64(&self.first_seq).ok_or(Reason::FieldFormat)?, dec_u64(&self.last_seq).ok_or(Reason::FieldFormat)?);
        // A range ending at u64::MAX is unrepresentable downstream: its successor row
        // and its row count do not fit in a u64. Reject it here instead of letting
        // coverage arithmetic overflow or wrap.
        ensure(f <= l && l.checked_add(1).is_some(), Reason::FieldFormat)?;
        match &self.predecessor_seq {
            None => ensure(f == 0 && self.predecessor_head == canon::ZERO_HEAD, Reason::FieldFormat),
            Some(p) => ensure(dec_u64(p).and_then(|p| p.checked_add(1)) == Some(f), Reason::FieldFormat),
        }
    }
    pub fn first(&self) -> u64 { dec_u64(&self.first_seq).unwrap_or(0) }
    pub fn last(&self) -> u64 { dec_u64(&self.last_seq).unwrap_or(0) }
    pub fn predecessor(&self) -> Option<u64> { self.predecessor_seq.as_deref().and_then(dec_u64) }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CaptureRange { pub capture_id: String, pub first_seq: String, pub last_seq: String, pub clock_domain_id: String }

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CloseFields { pub final_head: String, pub final_last_seq: String, pub total_rows: String, pub final_interval_index: String }

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Commitment {
    pub schema: String,
    pub purpose: Purpose,
    pub service: String,
    pub audience: String,
    pub policy_id: String,
    pub session_id: String,
    pub device_epoch_id: String,
    pub capture_epoch_id: String,
    pub seal_hash: String,
    pub challenge_id: String,
    pub challenge_digest: String,
    pub client_nonce: String,
    pub server_nonce: String,
    pub commitment_index: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub prior_ack_id: Option<String>,
    pub chain: ChainRange,
    pub capture_ranges: Vec<CaptureRange>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub close: Option<CloseFields>,
}

impl Commitment {
    pub fn validate(&self) -> Result<(), Reason> {
        ensure(self.schema == SCHEMA_COMMITMENT, Reason::WrongSchema)?;
        ensure(short_text(&self.service) && short_text(&self.audience) && short_text(&self.policy_id), Reason::FieldFormat)?;
        ensure(is_id(&self.session_id) && is_id(&self.device_epoch_id) && is_id(&self.capture_epoch_id), Reason::FieldFormat)?;
        ensure(is_hex64(&self.seal_hash) && is_hex64(&self.challenge_id) && is_hex64(&self.challenge_digest), Reason::FieldFormat)?;
        ensure(is_hex64(&self.client_nonce) && is_hex64(&self.server_nonce), Reason::FieldFormat)?;
        let idx = dec_u64(&self.commitment_index).ok_or(Reason::FieldFormat)?;
        let opening = self.purpose == Purpose::Opening;
        ensure(opening == (idx == 0) && opening == self.prior_ack_id.is_none(), Reason::SchemaViolation)?;
        if let Some(p) = &self.prior_ack_id { ensure(is_hex64(p), Reason::FieldFormat)?; }
        self.chain.validate()?;
        ensure(!self.capture_ranges.is_empty(), Reason::SchemaViolation)?;
        for r in &self.capture_ranges {
            let (f, l) = (dec_u64(&r.first_seq), dec_u64(&r.last_seq));
            ensure(is_id(&r.capture_id) && is_id(&r.clock_domain_id) && f.is_some() && l.is_some() && f <= l
                && l.and_then(|l| l.checked_add(1)).is_some(), Reason::FieldFormat)?;
        }
        match (&self.close, self.purpose) {
            (Some(c), Purpose::Close) => {
                ensure(c.final_head == self.chain.end_head && c.final_last_seq == self.chain.last_seq, Reason::SchemaViolation)?;
                ensure(dec_u64(&c.total_rows).is_some() && dec_u64(&c.final_interval_index) == Some(idx), Reason::SchemaViolation)
            }
            (None, Purpose::Close) | (Some(_), _) => Err(Reason::SchemaViolation),
            (None, _) => Ok(()),
        }
    }
    pub fn index(&self) -> u64 { dec_u64(&self.commitment_index).unwrap_or(u64::MAX) }
    pub fn digest(&self) -> Result<String, Reason> { canon::digest(DOMAIN_COMMITMENT, self) }
}

/// Body of `POST /aux/v1/seen-at/commitments`: the exact challenge plus the commitment.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CommitmentRequest { pub schema: String, pub challenge: SignedChallenge, pub commitment: Commitment }

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AckUnsigned {
    pub schema: String,
    pub key_id: String,
    pub alg: String,
    pub purpose: Purpose,
    pub service: String,
    pub audience: String,
    pub policy_id: String,
    pub commitment: Commitment,
    pub commitment_digest: String,
    pub server_boot_id: String,
    pub server_clock_epoch_id: String,
    pub mono_source: String,
    pub mono_unit: String,
    pub wall_unit: String,
    pub wall_quality: String,
    pub received_mono_ns: String,
    pub received_wall_us: String,
    pub ledger_seq: String,
    pub prev_ledger_entry_digest: String,
}

impl AckUnsigned {
    pub fn validate(&self) -> Result<(), Reason> {
        ensure(self.schema == SCHEMA_ACK, Reason::WrongSchema)?;
        ensure(self.alg == ALG_ED25519, Reason::AlgorithmMismatch)?;
        ensure(is_hex64(&self.key_id) && is_hex64(&self.commitment_digest) && is_hex64(&self.prev_ledger_entry_digest), Reason::FieldFormat)?;
        ensure(is_id(&self.server_boot_id) && is_id(&self.server_clock_epoch_id) && short_text(&self.mono_source), Reason::FieldFormat)?;
        ensure(self.mono_unit == MONO_UNIT && self.wall_unit == WALL_UNIT && self.wall_quality == WALL_QUALITY, Reason::FieldFormat)?;
        ensure(dec_u64(&self.received_mono_ns).is_some() && dec_i64(&self.received_wall_us).is_some(), Reason::FieldFormat)?;
        ensure(dec_u64(&self.ledger_seq).is_some(), Reason::FieldFormat)?;
        self.commitment.validate()?;
        let c = &self.commitment;
        ensure(self.purpose == c.purpose && self.service == c.service && self.audience == c.audience && self.policy_id == c.policy_id, Reason::BindingMismatch)
    }
    pub fn received(&self) -> u64 { dec_u64(&self.received_mono_ns).unwrap_or(u64::MAX) }
    pub fn ledger(&self) -> u64 { dec_u64(&self.ledger_seq).unwrap_or(0) }
    /// `ack_id = H("QOR-AUX-ACK-ID-v1", unsigned_payload)`; it is not part of its own preimage.
    pub fn ack_id(&self) -> Result<String, Reason> { canon::digest(DOMAIN_ACK_ID, self) }
}

/// Response wrapper `{unsigned_payload, signature, ack_id}`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SignedAck { pub unsigned_payload: AckUnsigned, pub signature: String, pub ack_id: String }

impl SignedAck {
    pub fn sign(unsigned: AckUnsigned, key: &SigningKey) -> Result<Self, Reason> {
        let msg = canon::signing_message(DOMAIN_ACK, &unsigned)?;
        let ack_id = unsigned.ack_id()?;
        Ok(SignedAck { signature: canon::b64url(&key.sign(&msg).to_bytes()), unsigned_payload: unsigned, ack_id })
    }
}
