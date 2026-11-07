//! SYNTHETIC test material only. Keys are derived at test time from fixed labels, are not real
//! service keys, are never written to disk or printed, and sign nothing outside these tests.
//! Budgets below are synthetic test values, not calibrated numbers.

use ed25519_dalek::SigningKey;
use super::qorcap::anchor_seen_at::canon::{self, ZERO_HEAD};
use super::qorcap::anchor_seen_at::check::{self, ClientPolicy, ClockRef, LocalSamples};
use super::qorcap::anchor_seen_at::envelope::{self, RefereeSeenAtPayload};
use super::qorcap::anchor_seen_at::keys::{KeyScope, KeyStatus, PinnedKey, TrustPolicy};
use super::qorcap::anchor_seen_at::service_double::{ServiceConfig, ServiceDouble, PATH_CHALLENGES, PATH_COMMITMENTS};
use super::qorcap::anchor_seen_at::types::*;
use super::qorcap::anchor_seen_at::AnchorState;
use super::qorcap::evidence;
use serde_json::Value;

pub const SERVICE: &str = "synthetic.referee.aux";
pub const AUDIENCE: &str = "synthetic.qorcap.practice";
pub const POLICY: &str = "synthetic-policy-1";
pub const TRUST_POLICY_ID: &str = "synthetic-trust-1";
pub const SYNTH_LIFETIME_NS: u64 = 30_000_000_000;
pub const SYNTH_RTT_BUDGET_NS: u64 = 2_000_000_000;
pub const SYNTH_CHALLENGE_AGE_NS: u64 = 5_000_000_000;

pub fn synthetic_key(label: &str) -> SigningKey {
    let seed = evidence::sha256_hex(&canon::frame("SYNTHETIC-TEST-KEY-NOT-FOR-USE", label.as_bytes()));
    let mut b = [0u8; 32];
    for (i, x) in b.iter_mut().enumerate() { *x = u8::from_str_radix(&seed[2 * i..2 * i + 2], 16).unwrap(); }
    SigningKey::from_bytes(&b)
}
pub fn hex64(label: &str) -> String { evidence::sha256_hex(label.as_bytes()) }
pub fn id(label: &str) -> String { hex64(label)[..32].to_string() }

pub fn pinned(k: &SigningKey, scope: KeyScope, status: KeyStatus) -> PinnedKey { PinnedKey::new(k.verifying_key(), scope, SERVICE, AUDIENCE, status) }

pub fn policy_with(keys: Vec<PinnedKey>) -> ClientPolicy {
    ClientPolicy {
        trust: TrustPolicy::new(TRUST_POLICY_ID, SERVICE, AUDIENCE, &[POLICY], keys).unwrap(),
        local_check_version: "synthetic-check-1".into(),
        max_rtt_ns: Some(SYNTH_RTT_BUDGET_NS),
        max_challenge_age_ns: Some(SYNTH_CHALLENGE_AGE_NS),
    }
}

pub struct World { pub svc: ServiceDouble, pub ch_key: SigningKey, pub ack_key: SigningKey, pub policy: ClientPolicy }

impl World {
    pub fn new() -> Self { Self::with_keys("challenge-a", "ack-a") }
    pub fn with_keys(ch: &str, ack: &str) -> Self {
        let (ch_key, ack_key) = (synthetic_key(ch), synthetic_key(ack));
        let policy = policy_with(vec![pinned(&ch_key, KeyScope::Challenge, KeyStatus::Active), pinned(&ack_key, KeyScope::Ack, KeyStatus::Active)]);
        let cfg = ServiceConfig { service: SERVICE.into(), audience: AUDIENCE.into(), policy_id: POLICY.into(), challenge_lifetime_ns: SYNTH_LIFETIME_NS };
        World { svc: ServiceDouble::new(cfg, ch_key.clone(), ack_key.clone()), ch_key, ack_key, policy }
    }
}

#[derive(Clone)]
pub struct Session {
    pub label: String,
    pub session_id: String,
    pub device_epoch_id: String,
    pub capture_epoch_id: String,
    pub chain_id: String,
    pub seal_hash: String,
    pub client_clock: ClockRef,
    pub nonce_counter: std::cell::Cell<u64>,
}

pub fn seal_for(label: &str, echo_hex: &str) -> SealInput {
    let r = |seq: &str| CaptureRef { capture_id: id(&format!("{label}-capture")), seq: seq.into(), clock_domain_id: id(&format!("{label}-clock")) };
    SealInput {
        session_id: id(&format!("{label}-session")), device_epoch_id: id(&format!("{label}-device")), capture_epoch_id: id(&format!("{label}-capepoch")),
        echo_encoding_id: "synthetic.dualsense.echo.v1".into(), echo_bytes_hex: echo_hex.into(),
        opening_start_ref: r("0"), opening_end_ref: r("3"),
    }
}

impl Session {
    pub fn new(label: &str) -> Self { Self::with_echo(label, "a1b2c3d4e5f6") }
    pub fn with_echo(label: &str, echo_hex: &str) -> Self {
        let seal = seal_for(label, echo_hex);
        Session {
            label: label.into(),
            session_id: seal.session_id.clone(), device_epoch_id: seal.device_epoch_id.clone(), capture_epoch_id: seal.capture_epoch_id.clone(),
            chain_id: id(&format!("{label}-chain")), seal_hash: seal.seal_hash().unwrap(),
            client_clock: ClockRef { boot_id: id(&format!("{label}-hostboot")), clock_epoch_id: id(&format!("{label}-hostclock")) },
            nonce_counter: std::cell::Cell::new(0),
        }
    }
    pub fn head(&self, seq: u64) -> String { hex64(&format!("{}-head-{seq}", self.label)) }
    pub fn request(&self, purpose: Purpose) -> ChallengeRequest {
        let n = self.nonce_counter.get() + 1;
        self.nonce_counter.set(n);
        ChallengeRequest {
            schema: SCHEMA_CHALLENGE_REQUEST.into(), purpose, service: SERVICE.into(), audience: AUDIENCE.into(), policy_id: POLICY.into(),
            session_id: self.session_id.clone(), device_epoch_id: self.device_epoch_id.clone(), capture_epoch_id: self.capture_epoch_id.clone(),
            client_nonce: hex64(&format!("{}-client-nonce-{n}", self.label)),
        }
    }
    pub fn commitment(&self, ch: &SignedChallenge, index: u64, prior: Option<&str>, first: u64, last: u64) -> Commitment {
        let u = &ch.unsigned_payload;
        let (pred_seq, pred_head) = if first == 0 { (None, ZERO_HEAD.to_string()) } else { (Some((first - 1).to_string()), self.head(first - 1)) };
        Commitment {
            schema: SCHEMA_COMMITMENT.into(), purpose: u.purpose, service: u.service.clone(), audience: u.audience.clone(), policy_id: u.policy_id.clone(),
            session_id: self.session_id.clone(), device_epoch_id: self.device_epoch_id.clone(), capture_epoch_id: self.capture_epoch_id.clone(),
            seal_hash: self.seal_hash.clone(), challenge_id: u.challenge_id.clone(), challenge_digest: u.digest().unwrap(),
            client_nonce: u.client_nonce.clone(), server_nonce: u.server_nonce.clone(),
            commitment_index: index.to_string(), prior_ack_id: prior.map(str::to_string),
            chain: ChainRange {
                chain_id: self.chain_id.clone(), chain_schema: "synthetic.aux_chain.v1".into(), algorithm: HASH_ALG.into(),
                predecessor_seq: pred_seq, predecessor_head: pred_head, first_seq: first.to_string(), last_seq: last.to_string(),
                end_head: self.head(last), checkpoint_id: hex64(&format!("{}-cp-{last}", self.label)),
            },
            capture_ranges: vec![CaptureRange { capture_id: id(&format!("{}-capture", self.label)), first_seq: first.to_string(), last_seq: last.to_string(), clock_domain_id: id(&format!("{}-clock", self.label)) }],
            close: (u.purpose == Purpose::Close).then(|| CloseFields { final_head: self.head(last), final_last_seq: last.to_string(), total_rows: (last + 1).to_string(), final_interval_index: index.to_string() }),
        }
    }
    pub fn samples(&self) -> LocalSamples {
        LocalSamples {
            send_clock: self.client_clock.clone(), receive_clock: self.client_clock.clone(),
            challenge_received_mono_ns: "1000".into(), echo_capture_mono_ns: "2000".into(), send_mono_ns: "3000".into(), receive_mono_ns: "4000".into(),
            transport_result: check::TRANSPORT_OK.into(),
        }
    }
}

pub fn enc<T: serde::Serialize>(v: &T) -> Vec<u8> { canon::canonical_bytes(v).unwrap() }
pub fn commit_body(ch: &SignedChallenge, c: &Commitment) -> Vec<u8> {
    enc(&CommitmentRequest { schema: SCHEMA_COMMITMENT_REQUEST.into(), challenge: ch.clone(), commitment: c.clone() })
}

/// Re-encode a JSON value as canonical bytes after a mutation (no re-signing).
pub fn tamper(bytes: &[u8], f: impl FnOnce(&mut Value)) -> Vec<u8> {
    let mut v: Value = serde_json::from_slice(bytes).unwrap();
    f(&mut v);
    evidence::jcs(&v).unwrap().into_bytes()
}

#[derive(Clone)]
pub struct Exchange { pub ch: SignedChallenge, pub commitment: Commitment, pub ack_bytes: Vec<u8>, pub samples: LocalSamples }

impl Exchange {
    pub fn ack(&self) -> SignedAck { serde_json::from_slice(&self.ack_bytes).unwrap() }
    pub fn ack_id(&self) -> String { self.ack().ack_id }
    pub fn state(&self, policy: &ClientPolicy) -> AnchorState { check::check_ack(&self.ch, &self.ack_bytes, &self.commitment, &self.samples, policy) }
    pub fn payload(&self) -> RefereeSeenAtPayload {
        let a = self.ack();
        RefereeSeenAtPayload {
            challenge_key_id: self.ch.unsigned_payload.key_id.clone(), ack_key_id: a.unsigned_payload.key_id.clone(),
            ledger_seq: a.unsigned_payload.ledger_seq.clone(), prev_ledger_entry_digest: a.unsigned_payload.prev_ledger_entry_digest.clone(),
            challenge: self.ch.clone(), ack: a, commitment: self.commitment.clone(),
            service: SERVICE.into(), audience: AUDIENCE.into(), trust_policy_id: TRUST_POLICY_ID.into(), local_samples: self.samples.clone(),
        }
    }
    pub fn envelope(&self) -> Vec<u8> { envelope::encode(&envelope::envelope_value(&self.payload())).unwrap() }
    /// The independently retained record of this exchange, kept apart from the envelope bytes.
    pub fn expected(&self) -> envelope::ExpectedExchange {
        envelope::ExpectedExchange::new(self.ch.clone(), self.commitment.clone(), self.ack_id(), self.samples.clone())
    }
}

pub fn challenge(w: &World, s: &Session, purpose: Purpose) -> SignedChallenge {
    let req = s.request(purpose);
    let bytes = w.svc.route("POST", PATH_CHALLENGES, &enc(&req)).unwrap();
    check::check_challenge(&bytes, &req, &w.policy.trust).unwrap()
}

pub fn exchange(w: &World, s: &Session, purpose: Purpose, index: u64, prior: Option<&str>, first: u64, last: u64) -> Exchange {
    let ch = challenge(w, s, purpose);
    let commitment = s.commitment(&ch, index, prior, first, last);
    let ack_bytes = w.svc.route("POST", PATH_COMMITMENTS, &commit_body(&ch, &commitment)).unwrap();
    Exchange { ch, commitment, ack_bytes, samples: s.samples() }
}

/// Opening rows 0..=9, interval 10..=19, close 20..=29.
pub fn full_stream(w: &World, s: &Session) -> Vec<Exchange> {
    let o = exchange(w, s, Purpose::Opening, 0, None, 0, 9);
    let i = exchange(w, s, Purpose::Interval, 1, Some(&o.ack_id()), 10, 19);
    let c = exchange(w, s, Purpose::Close, 2, Some(&i.ack_id()), 20, 29);
    vec![o, i, c]
}

pub fn unknown_reasons(s: &AnchorState) -> Vec<super::qorcap::anchor_seen_at::Reason> {
    match s { AnchorState::Unknown { reasons } => reasons.clone(), other => panic!("expected unknown, got {}", other.status_code()) }
}

/// Synthetic signed variants for link/binding tests. All resulting facts still go
/// through the public checker; this helper never constructs or edits a fact.
pub fn checked_variant(w: &World, ex: &Exchange, edit: impl FnOnce(&mut SignedChallenge, &mut AckUnsigned)) -> AnchorState {
    let mut ch = ex.ch.clone();
    let mut ack = ex.ack().unsigned_payload;
    edit(&mut ch, &mut ack);
    ch.unsigned_payload.purpose = ack.commitment.purpose;
    ch.unsigned_payload.session_id = ack.commitment.session_id.clone();
    ch.unsigned_payload.device_epoch_id = ack.commitment.device_epoch_id.clone();
    ch.unsigned_payload.capture_epoch_id = ack.commitment.capture_epoch_id.clone();
    ch.unsigned_payload.client_nonce = ack.commitment.client_nonce.clone();
    ch.unsigned_payload.server_nonce = ack.commitment.server_nonce.clone();
    ch.unsigned_payload.server_boot_id = ack.server_boot_id.clone();
    ch.unsigned_payload.server_clock_epoch_id = ack.server_clock_epoch_id.clone();
    let ch = SignedChallenge::sign(ch.unsigned_payload, &w.ch_key).unwrap();
    ack.commitment.challenge_digest = ch.unsigned_payload.digest().unwrap();
    ack.commitment_digest = ack.commitment.digest().unwrap();
    ack.purpose = ack.commitment.purpose;
    let sent = ack.commitment.clone();
    let bytes = enc(&SignedAck::sign(ack, &w.ack_key).unwrap());
    check::check_ack(&ch, &bytes, &sent, &ex.samples, &w.policy)
}
