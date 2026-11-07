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
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SeenAtFact {
    pub ack_id: String,
    pub ack_key_id: String,
    pub challenge_key_id: String,
    pub trust_policy_id: String,
    pub commitment: Commitment,
    pub commitment_digest: String,
    pub server_boot_id: String,
    pub server_clock_epoch_id: String,
    pub received_mono_ns: u64,
    pub received_wall_us: String,
    pub ledger_seq: u64,
    pub prev_ledger_entry_digest: String,
    pub rtt_ns: u64,
}

impl SeenAtFact {
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
        Ok((fact, false)) => AnchorState::SeenAtRetained(fact),
        Ok((fact, true)) => AnchorState::SeenAtLate(fact),
        Err(r) => AnchorState::unknown(r),
    }
}

fn check_ack_inner(challenge: &SignedChallenge, ack_bytes: &[u8], sent: &Commitment, samples: &LocalSamples, policy: &ClientPolicy) -> Result<(SeenAtFact, bool), Reason> {
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

    Ok((SeenAtFact {
        ack_id: ack.ack_id.clone(),
        ack_key_id: u.key_id.clone(),
        challenge_key_id: ch.key_id.clone(),
        trust_policy_id: policy.trust.trust_policy_id.clone(),
        commitment: sent.clone(),
        commitment_digest: u.commitment_digest.clone(),
        server_boot_id: u.server_boot_id.clone(),
        server_clock_epoch_id: u.server_clock_epoch_id.clone(),
        received_mono_ns: u.received(),
        received_wall_us: u.received_wall_us.clone(),
        ledger_seq: u.ledger(),
        prev_ledger_entry_digest: u.prev_ledger_entry_digest.clone(),
        rtt_ns: rtt,
    }, late))
}
