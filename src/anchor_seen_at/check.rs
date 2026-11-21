//! Client-side checks of a challenge and an acknowledgment against the pinned trust policy and
//! the client's own record of what it sent. Every failure becomes [`AnchorState::Unknown`].

use super::canon::{self, dec_u64, ensure, is_id};
use super::keys::{KeyLookup, KeyScope, TrustPolicy};
use super::readiness::CaptureValidity;
use super::types::{ChallengeRequest, Commitment, Purpose, SignedAck, SignedChallenge};
use super::{AnchorState, Reason, ACK_MEANING, DOMAIN_ACK, DOMAIN_CHALLENGE};
use ed25519_dalek::{Signature, VerifyingKey};
use serde::{Deserialize, Serialize};

/// Client checking policy. Budgets are policy-versioned and none is established yet: an unset
/// budget leaves the acknowledgment unknown rather than assuming any number.
#[derive(Clone, Debug)]
pub struct ClientPolicy {
    pub trust: TrustPolicy,
    pub local_check_version: String,
    pub max_rtt_ns: Option<u64>,
    pub max_challenge_age_ns: Option<u64>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ClockRef { pub boot_id: String, pub clock_epoch_id: String }

/// Local exchange samples in the client's own monotonic domain. Never compared with server values.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LocalSamples {
    pub send_clock: ClockRef,
    pub receive_clock: ClockRef,
    pub challenge_received_mono_ns: String,
    pub echo_capture_mono_ns: String,
    pub send_mono_ns: String,
    pub receive_mono_ns: String,
    pub transport_result: String,
}

pub const TRANSPORT_OK: &str = "ok";

/// What a checked acknowledgment establishes: the service saw `commitment` at `received_mono_ns`
/// in its own clock domain. Nothing about whether the committed capture is genuine.
#[doc = include_str!("../../tests/anchor_seen_at/fact_api.md")]
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CheckedSeenAt {
    ack_id: String,
    ack_key_id: String,
    challenge_key_id: String,
    trust_policy_id: String,
    commitment: Commitment,
    commitment_digest: String,
    server_boot_id: String,
    server_clock_epoch_id: String,
    /// The checking client's own clock identity for this exchange, from its retained
    /// samples. Carried on the fact so a stream can tell a client clock-domain change
    /// between two exchanges from one continuous client clock.
    client_boot_id: String,
    client_clock_epoch_id: String,
    received_mono_ns: u64,
    received_wall_us: String,
    ledger_seq: u64,
    prev_ledger_entry_digest: String,
    rtt_ns: u64,
    // Immutable checker classification; an outer enum label cannot upgrade it.
    late: bool,
}

/// Compatibility name for the opaque checked token; there is no mutable raw fact API.
pub type SeenAtFact = CheckedSeenAt;

impl CheckedSeenAt {
    pub(crate) fn into_late(mut self) -> Self { self.late = true; self }
    /// The classification minted by `check_ack`, independent of any outer label.
    pub fn is_late(&self) -> bool { self.late }
    pub fn ack_id(&self) -> &String { &self.ack_id }
    pub fn ack_key_id(&self) -> &String { &self.ack_key_id }
    pub fn challenge_key_id(&self) -> &String { &self.challenge_key_id }
    pub fn trust_policy_id(&self) -> &String { &self.trust_policy_id }
    pub fn commitment(&self) -> &Commitment { &self.commitment }
    pub fn commitment_digest(&self) -> &String { &self.commitment_digest }
    pub fn server_boot_id(&self) -> &String { &self.server_boot_id }
    pub fn server_clock_epoch_id(&self) -> &String { &self.server_clock_epoch_id }
    pub fn client_boot_id(&self) -> &String { &self.client_boot_id }
    pub fn client_clock_epoch_id(&self) -> &String { &self.client_clock_epoch_id }
    pub fn received_mono_ns(&self) -> u64 { self.received_mono_ns }
    pub fn received_wall_us(&self) -> &String { &self.received_wall_us }
    pub fn ledger_seq(&self) -> u64 { self.ledger_seq }
    pub fn prev_ledger_entry_digest(&self) -> &String { &self.prev_ledger_entry_digest }
    pub fn rtt_ns(&self) -> u64 { self.rtt_ns }
    pub fn meaning(&self) -> &'static str { ACK_MEANING }
    pub fn capture_validity(&self) -> CaptureValidity { CaptureValidity::NotEstablishedByAck }
}

fn verify_sig<T: Serialize>(vk: &VerifyingKey, domain: &str, unsigned: &T, sig: &str) -> Result<(), Reason> {
    let raw = canon::un_b64url(sig).ok_or(Reason::BadSignature)?;
    let raw: [u8; 64] = raw.try_into().map_err(|_| Reason::BadSignature)?;
    let msg = canon::signing_message(domain, unsigned)?;
    vk.verify_strict(&msg, &Signature::from_bytes(&raw)).map_err(|_| Reason::BadSignature)
}

fn check_challenge_sig(ch: &SignedChallenge, trust: &TrustPolicy, ack_id: Option<&str>) -> Result<(), Reason> {
    let u = &ch.unsigned_payload;
    u.validate()?;
    let vk = trust.resolve(&KeyLookup {
        key_id: &u.key_id, alg: &u.alg, scope: KeyScope::Challenge, service: &u.service, audience: &u.audience, policy_id: &u.policy_id, ack_id,
    })?;
    verify_sig(&vk, DOMAIN_CHALLENGE, u, &ch.signature)
}

/// Check a challenge response against the request this client made.
pub fn check_challenge(bytes: &[u8], req: &ChallengeRequest, trust: &TrustPolicy) -> Result<SignedChallenge, Reason> {
    let ch: SignedChallenge = canon::parse_canonical(bytes)?;
    check_challenge_sig(&ch, trust, None)?;
    let u = &ch.unsigned_payload;
    let same = u.purpose == req.purpose && u.service == req.service && u.audience == req.audience && u.policy_id == req.policy_id
        && u.session_id == req.session_id && u.device_epoch_id == req.device_epoch_id && u.capture_epoch_id == req.capture_epoch_id
        && u.client_nonce == req.client_nonce;
    ensure(same, Reason::BindingMismatch)?;
    Ok(ch)
}

/// Check an acknowledgment. `sent` is the client's own durable copy of the commitment it sent and
/// `challenge` the challenge it consumed; an authentic ack for anything else is rejected.
pub fn check_ack(challenge: &SignedChallenge, ack_bytes: &[u8], sent: &Commitment, samples: &LocalSamples, policy: &ClientPolicy) -> AnchorState {
    match check_ack_inner(challenge, ack_bytes, sent, samples, policy) {
        Ok(fact) if fact.is_late() => AnchorState::SeenAtLate(fact),
        Ok(fact) => AnchorState::SeenAtRetained(fact),
        Err(r) => AnchorState::unknown(r),
    }
}

fn check_ack_inner(challenge: &SignedChallenge, ack_bytes: &[u8], sent: &Commitment, samples: &LocalSamples, policy: &ClientPolicy) -> Result<CheckedSeenAt, Reason> {
    let ack: SignedAck = canon::parse_canonical(ack_bytes)?;
    let u = &ack.unsigned_payload;
    u.validate()?;
    ensure(u.ack_id()? == ack.ack_id, Reason::AckIdMismatch)?;
    let vk = policy.trust.resolve(&KeyLookup {
        key_id: &u.key_id, alg: &u.alg, scope: KeyScope::Ack, service: &u.service, audience: &u.audience, policy_id: &u.policy_id, ack_id: Some(&ack.ack_id),
    })?;
    verify_sig(&vk, DOMAIN_ACK, u, &ack.signature)?;
    check_challenge_sig(challenge, &policy.trust, Some(&ack.ack_id))?;

    sent.validate()?;
    ensure(u.commitment == *sent, Reason::BindingMismatch)?;
    ensure(u.commitment_digest == sent.digest()?, Reason::DigestMismatch)?;
    let ch = &challenge.unsigned_payload;
    let bound = sent.challenge_id == ch.challenge_id && sent.challenge_digest == ch.digest()?
        && sent.client_nonce == ch.client_nonce && sent.server_nonce == ch.server_nonce
        && sent.session_id == ch.session_id && sent.device_epoch_id == ch.device_epoch_id && sent.capture_epoch_id == ch.capture_epoch_id
        && sent.purpose == ch.purpose && sent.service == ch.service && sent.audience == ch.audience && sent.policy_id == ch.policy_id;
    ensure(bound, Reason::BindingMismatch)?;
    ensure(u.server_boot_id == ch.server_boot_id && u.server_clock_epoch_id == ch.server_clock_epoch_id, Reason::ServerEpochMismatch)?;
    ensure(ch.issued() <= u.received() && u.received() < ch.expires(), Reason::ChallengeExpired)?;

    ensure(samples.transport_result == TRANSPORT_OK, Reason::TransportFailed)?;
    for c in [&samples.send_clock, &samples.receive_clock] {
        ensure(is_id(&c.boot_id) && is_id(&c.clock_epoch_id), Reason::FieldFormat)?;
    }
    ensure(samples.send_clock == samples.receive_clock, Reason::ClientClockDomainChanged)?;
    let n = |s: &str| dec_u64(s).ok_or(Reason::FieldFormat);
    let (got_ch, echo, send, recv) = (n(&samples.challenge_received_mono_ns)?, n(&samples.echo_capture_mono_ns)?, n(&samples.send_mono_ns)?, n(&samples.receive_mono_ns)?);
    // Purpose-specific local timing. The echo capture exists only for the opening (it is the
    // sealed echo), so `challenge_received < echo_capture <= send` and the challenge age at the
    // echo are opening-only. Interval and close have no echo to order: for them the challenge
    // must have been received before the commitment was sent, and the challenge age is measured
    // at that send. The echo field is still parsed above, so a malformed value fails closed.
    // These samples are the checking client's own retained record (see `envelope`): values
    // supplied inside a presented envelope never decide eligibility by themselves.
    let age = match sent.purpose {
        Purpose::Opening => {
            ensure(got_ch < echo && echo <= send, Reason::LocalOrdering)?;
            echo - got_ch
        }
        Purpose::Interval | Purpose::Close => {
            ensure(got_ch <= send, Reason::LocalOrdering)?;
            send - got_ch
        }
    };
    ensure(recv >= send, Reason::NegativeRtt)?;
    let (max_rtt, max_age) = (policy.max_rtt_ns.ok_or(Reason::TimingBudgetUnset)?, policy.max_challenge_age_ns.ok_or(Reason::TimingBudgetUnset)?);
    let rtt = recv - send;
    let late = rtt > max_rtt || age > max_age;

    Ok(CheckedSeenAt {
        ack_id: ack.ack_id.clone(),
        ack_key_id: u.key_id.clone(),
        challenge_key_id: ch.key_id.clone(),
        trust_policy_id: policy.trust.trust_policy_id.clone(),
        commitment: sent.clone(),
        commitment_digest: u.commitment_digest.clone(),
        server_boot_id: u.server_boot_id.clone(),
        server_clock_epoch_id: u.server_clock_epoch_id.clone(),
        client_boot_id: samples.send_clock.boot_id.clone(),
        client_clock_epoch_id: samples.send_clock.clock_epoch_id.clone(),
        received_mono_ns: u.received(),
        received_wall_us: u.received_wall_us.clone(),
        ledger_seq: u.ledger(),
        prev_ledger_entry_digest: u.prev_ledger_entry_digest.clone(),
        rtt_ns: rtt,
        late,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use super::super::coverage::{self, RangeStatus, StreamEligibility};

    #[test]
    fn internal_malformed_max_range_remains_unknown_without_overflow() {
        // Only this unit test can build a malformed token. Production construction
        // remains inside check_ack_inner, after signature/binding/timing checks.
        let commitment: Commitment = serde_json::from_value(serde_json::json!({
            "schema": "synthetic", "purpose": "opening", "service": "synthetic",
            "audience": "synthetic", "policy_id": "synthetic", "session_id": "synthetic",
            "device_epoch_id": "synthetic", "capture_epoch_id": "synthetic",
            "seal_hash": "synthetic", "challenge_id": "synthetic", "challenge_digest": "synthetic",
            "client_nonce": "synthetic", "server_nonce": "synthetic", "commitment_index": "0",
            "chain": { "chain_id": "synthetic", "chain_schema": "synthetic", "algorithm": "synthetic",
                "predecessor_head": "synthetic", "first_seq": "0", "last_seq": u64::MAX.to_string(),
                "end_head": "synthetic", "checkpoint_id": "synthetic" }, "capture_ranges": []
        })).unwrap();
        let fact = SeenAtFact {
            ack_id: String::new(), ack_key_id: String::new(), challenge_key_id: String::new(),
            trust_policy_id: String::new(), commitment, commitment_digest: String::new(),
            server_boot_id: String::new(), server_clock_epoch_id: String::new(),
            client_boot_id: String::new(), client_clock_epoch_id: String::new(),
            received_mono_ns: 0, received_wall_us: String::new(), ledger_seq: 0,
            prev_ledger_entry_digest: String::new(), rtt_ns: 0, late: false,
        };
        let state = AnchorState::SeenAtRetained(fact);
        let eligibility = StreamEligibility::from_arrival_order(&[state.clone()], u64::MAX);
        assert_eq!(eligibility.break_reason(), Some(Reason::SchemaViolation));
        assert!(!eligibility.complete() && !eligibility.is_eligible(0));
        assert_eq!(eligibility.unknown_rows(), u64::MAX);
        let archival = coverage::reconstruct_archival(&[state], u64::MAX);
        assert!(!archival.complete);
        assert_eq!(archival.unknown_rows(), u64::MAX);
        assert_eq!(archival.segments[0].status, RangeStatus::Unknown(Reason::SchemaViolation));
    }
}
