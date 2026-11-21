//! `AnchorEvidenceV1`, the typed auxiliary envelope (design section 6). Not a receipt field.
//!
//! This phase accepts exactly one payload of kind `referee_seen_at`. A payload map with any other
//! count, a kind that differs from the declared `kind`, or any other (unsupported) kind stays
//! unknown generically: no other kind can stand in for service retention. `evidence_id` is the
//! ack id, the digest of the unsigned payload, and is outside its own preimage. `verification`
//! is local metadata: it is ignored on load and recomputed, never read as authority. Duplicated
//! bindings, coverage and clocks must equal what the signed acknowledgment commits to, and the
//! whole payload must equal the client's independently retained expected exchange: unsigned,
//! self-supplied values carried inside an envelope (including its timing samples) never decide
//! eligibility on their own.

use super::canon::{self, ensure};
use super::check::{self, ClientPolicy, LocalSamples};
use super::types::{CaptureRange, Commitment, SignedAck, SignedChallenge, MONO_UNIT};
use super::{AnchorState, Reason, PRACTICE_ONLY};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

pub const ENVELOPE_SCHEMA: &str = "proposed.anchor_evidence.v1";
pub const KIND_REFEREE_SEEN_AT: &str = "referee_seen_at";
const KEYS: [&str; 8] = ["schema", "kind", "evidence_id", "binding", "coverage", "clocks", "payload", "verification"];

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Binding { pub session_id: String, pub device_epoch_id: String, pub capture_epoch_id: String, pub seal_hash: String }

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Coverage {
    pub chain_id: String,
    pub algorithm: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub predecessor_seq: Option<String>,
    pub predecessor_head: String,
    pub first_seq: String,
    pub last_seq: String,
    pub end_head: String,
    pub capture_id_ranges: Vec<CaptureRange>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ClockDecl { pub domain_id: String, pub owner: String, pub source: String, pub unit: String, pub boot_id: String, pub clock_epoch_id: String }

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RefereeSeenAtPayload {
    pub challenge: SignedChallenge,
    pub ack: SignedAck,
    pub commitment: Commitment,
    pub service: String,
    pub audience: String,
    pub trust_policy_id: String,
    pub challenge_key_id: String,
    pub ack_key_id: String,
    pub ledger_seq: String,
    pub prev_ledger_entry_digest: String,
    pub local_samples: LocalSamples,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Verification { pub local_check_version: String, pub trust_policy_id: String, pub status: String, pub reason_codes: Vec<String>, pub label: String }

#[derive(Clone, Debug)]
pub struct EnvelopeCheck { pub state: AnchorState, pub verification: Verification }

pub fn binding_of(c: &Commitment) -> Binding {
    Binding { session_id: c.session_id.clone(), device_epoch_id: c.device_epoch_id.clone(), capture_epoch_id: c.capture_epoch_id.clone(), seal_hash: c.seal_hash.clone() }
}

pub fn coverage_of(c: &Commitment) -> Coverage {
    let ch = &c.chain;
    Coverage {
        chain_id: ch.chain_id.clone(), algorithm: ch.algorithm.clone(), predecessor_seq: ch.predecessor_seq.clone(), predecessor_head: ch.predecessor_head.clone(),
        first_seq: ch.first_seq.clone(), last_seq: ch.last_seq.clone(), end_head: ch.end_head.clone(), capture_id_ranges: c.capture_ranges.clone(),
    }
}

pub fn clocks_of(p: &RefereeSeenAtPayload) -> Vec<ClockDecl> {
    let a = &p.ack.unsigned_payload;
    let s = &p.local_samples.send_clock;
    vec![
        ClockDecl { domain_id: a.server_clock_epoch_id.clone(), owner: "referee_service".into(), source: a.mono_source.clone(), unit: MONO_UNIT.into(), boot_id: a.server_boot_id.clone(), clock_epoch_id: a.server_clock_epoch_id.clone() },
        ClockDecl { domain_id: s.clock_epoch_id.clone(), owner: "capture_host".into(), source: "host_monotonic".into(), unit: MONO_UNIT.into(), boot_id: s.boot_id.clone(), clock_epoch_id: s.clock_epoch_id.clone() },
    ]
}

/// The envelope as a JSON value, with every duplicated field derived from the payload.
pub fn envelope_value(p: &RefereeSeenAtPayload) -> Value {
    json!({
        "schema": ENVELOPE_SCHEMA,
        "kind": KIND_REFEREE_SEEN_AT,
        "evidence_id": p.ack.ack_id,
        "binding": binding_of(&p.commitment),
        "coverage": coverage_of(&p.commitment),
        "clocks": clocks_of(p),
        "payload": { (KIND_REFEREE_SEEN_AT): p },
    })
}

pub fn encode(v: &Value) -> Result<Vec<u8>, Reason> { canon::canonical_bytes(v) }

impl Verification {
    pub fn of(state: &AnchorState, policy: &ClientPolicy) -> Self {
        Verification {
            local_check_version: policy.local_check_version.clone(),
            trust_policy_id: policy.trust.trust_policy_id.clone(),
            status: state.status_code().into(),
            reason_codes: state.reasons().iter().map(Reason::code).collect(),
            label: PRACTICE_ONLY.into(),
        }
    }
}

/// The client's independently retained record of one exchange, kept apart from any envelope
/// presented later. Eligibility is decided against this record (including its own timing
/// samples), never against unsigned values supplied inside the envelope itself.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ExpectedExchange {
    pub binding: Binding,
    pub challenge: SignedChallenge,
    pub commitment: Commitment,
    pub ack_id: String,
    pub local_samples: LocalSamples,
}

impl ExpectedExchange {
    pub fn new(challenge: SignedChallenge, commitment: Commitment, ack_id: String, local_samples: LocalSamples) -> Self {
        let binding = binding_of(&commitment);
        ExpectedExchange { binding, challenge, commitment, ack_id, local_samples }
    }
}

/// Load and check one envelope against the independently retained expected exchange.
/// `None` (no envelope at all) is unknown, never a pass.
pub fn check_envelope(bytes: Option<&[u8]>, expected: &ExpectedExchange, policy: &ClientPolicy) -> EnvelopeCheck {
    let state = match bytes {
        None => AnchorState::default(),
        Some(b) => check_inner(b, expected, policy).unwrap_or_else(AnchorState::unknown),
    };
    EnvelopeCheck { verification: Verification::of(&state, policy), state }
}

fn typed<T: serde::de::DeserializeOwned>(v: &Value) -> Result<T, Reason> { serde_json::from_value(v.clone()).map_err(|_| Reason::SchemaViolation) }

fn check_inner(bytes: &[u8], expected: &ExpectedExchange, policy: &ClientPolicy) -> Result<AnchorState, Reason> {
    let v: Value = canon::parse_canonical(bytes)?;
    let o = v.as_object().ok_or(Reason::SchemaViolation)?;
    ensure(o.keys().all(|k| KEYS.contains(&k.as_str())), Reason::SchemaViolation)?;
    ensure(KEYS[..7].iter().all(|k| o.contains_key(*k)), Reason::SchemaViolation)?;
    ensure(o["schema"] == ENVELOPE_SCHEMA, Reason::WrongSchema)?;
    let kind = o["kind"].as_str().ok_or(Reason::SchemaViolation)?;
    let payload = o["payload"].as_object().ok_or(Reason::PayloadCount)?;
    ensure(payload.len() == 1, Reason::PayloadCount)?;
    let (pkind, pval) = payload.iter().next().ok_or(Reason::PayloadCount)?;
    ensure(pkind == kind, Reason::EnvelopeMismatch)?;
    if kind != KIND_REFEREE_SEEN_AT {
        return Err(Reason::UnknownKind);
    }
    let p: RefereeSeenAtPayload = typed(pval)?;
    let binding: Binding = typed(&o["binding"])?;
    let coverage: Coverage = typed(&o["coverage"])?;
    let clocks: Vec<ClockDecl> = typed(&o["clocks"])?;
    p.commitment.validate()?;

    ensure(o["evidence_id"] == p.ack.ack_id.as_str(), Reason::EnvelopeMismatch)?;
    ensure(binding == binding_of(&p.commitment), Reason::EnvelopeMismatch)?;
    ensure(coverage == coverage_of(&p.commitment), Reason::EnvelopeMismatch)?;
    ensure(clocks == clocks_of(&p), Reason::EnvelopeMismatch)?;
    let a = &p.ack.unsigned_payload;
    ensure(p.service == a.service && p.audience == a.audience, Reason::EnvelopeMismatch)?;
    ensure(p.ack_key_id == a.key_id && p.challenge_key_id == p.challenge.unsigned_payload.key_id, Reason::EnvelopeMismatch)?;
    ensure(p.ledger_seq == a.ledger_seq && p.prev_ledger_entry_digest == a.prev_ledger_entry_digest, Reason::EnvelopeMismatch)?;
    ensure(p.trust_policy_id == policy.trust.trust_policy_id, Reason::PolicyMismatch)?;
    ensure(binding == expected.binding, Reason::BindingMismatch)?;
    // Full expected exchange, not just the four binding fields: a different authentic
    // challenge/commitment/ack under the same binding is a substitution.
    ensure(p.challenge == expected.challenge && p.commitment == expected.commitment && p.ack.ack_id == expected.ack_id, Reason::BindingMismatch)?;
    ensure(p.local_samples == expected.local_samples, Reason::EnvelopeMismatch)?;

    let ack_bytes = canon::canonical_bytes(&p.ack)?;
    // Timing is taken from the independently retained samples, not the envelope's copy.
    Ok(check::check_ack(&expected.challenge, &ack_bytes, &expected.commitment, &expected.local_samples, policy))
}
