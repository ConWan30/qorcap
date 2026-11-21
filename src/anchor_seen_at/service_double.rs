//! In-process double of the three proposed endpoints (design sections 2 and 4). No sockets, no
//! HTTP server, no network: requests are canonical bytes handed to a method call.
//!
//! * challenges: issues a signed, single-use challenge bound to the request; lifetime is measured
//!   on the double's own monotonic clock.
//! * commitments: under one lock, compare-and-set the challenge from unused to consumed, advance
//!   the stream, append the retention ledger and store the exact response bytes. An identical
//!   retry (same challenge, same commitment digest) returns those exact bytes, even after expiry or
//!   a restart; a different commitment on a consumed challenge is a conflict. Wrong index, prior
//!   ack, predecessor or seal is a fork. Injected crash or retention failure changes nothing.
//! * acks/{id}: returns the identical retained bytes.
//!
//! Nonces and ids come from a deterministic synthetic counter, not an unpredictable source: this
//! double is for tests only. Authorization, rate and size limits are not modelled.

use super::canon::{self, dec, ensure};
use super::types::*;
use super::{Reason, ALG_ED25519, DOMAIN_LEDGER_ENTRY};
use crate::evidence;
use ed25519_dalek::SigningKey;
use serde_json::json;
use std::collections::HashMap;
use std::sync::Mutex;

pub const PATH_CHALLENGES: &str = "/aux/v1/seen-at/challenges";
pub const PATH_COMMITMENTS: &str = "/aux/v1/seen-at/commitments";
pub const PATH_ACKS: &str = "/aux/v1/seen-at/acks/";
pub const MONO_SOURCE: &str = "service_double_monotonic";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ServiceError {
    Malformed(Reason),
    UnknownChallenge,
    ChallengeMismatch,
    BindingMismatch,
    Expired,
    InvalidatedByRestart,
    Conflict,
    Fork,
    StreamClosed,
    Unavailable,
    RetentionFailed,
    ResponseLost,
    NotFound,
    NotRouted,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ServiceFault { CrashBeforeCommit, RetentionFailure, ResponseLostAfterCommit }

#[derive(Clone, Debug)]
pub struct ServiceConfig {
    pub service: String,
    pub audience: String,
    pub policy_id: String,
    pub challenge_lifetime_ns: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum ChState { Unused, Consumed { commitment_digest: String, ack_id: String }, Invalidated }

struct ChallengeRecord { signed: SignedChallenge, state: ChState }

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
struct StreamKey { session: String, device_epoch: String, capture_epoch: String, chain: String }

struct Stream { next_index: u64, last_ack_id: String, last_seq: u64, end_head: String, seal_hash: String, closed: bool }

struct State {
    boot: u64,
    boot_id: String,
    clock_epoch_id: String,
    mono_ns: u64,
    wall_us: i64,
    counter: u64,
    challenges: HashMap<String, ChallengeRecord>,
    streams: HashMap<StreamKey, Stream>,
    acks: HashMap<String, Vec<u8>>,
    ledger_len: u64,
    ledger_head: String,
    fault: Option<ServiceFault>,
}

pub struct ServiceDouble {
    cfg: ServiceConfig,
    challenge_key: SigningKey,
    ack_key: SigningKey,
    state: Mutex<State>,
}

fn synthetic(label: &str, n: u64) -> String {
    evidence::sha256_hex(&canon::frame("SYNTHETIC-SERVICE-DOUBLE", format!("{label}:{n}").as_bytes()))
}

fn bad(r: Reason) -> ServiceError { ServiceError::Malformed(r) }

impl ServiceDouble {
    /// Keys are supplied by the caller (synthetic test keys); the double never generates or prints them.
    pub fn new(cfg: ServiceConfig, challenge_key: SigningKey, ack_key: SigningKey) -> Self {
        let state = State {
            boot: 0, boot_id: synthetic("boot", 0)[..32].into(), clock_epoch_id: synthetic("clock", 0)[..32].into(),
            mono_ns: 1_000_000, wall_us: 1_700_000_000_000_000, counter: 0,
            challenges: HashMap::new(), streams: HashMap::new(), acks: HashMap::new(),
            ledger_len: 0, ledger_head: canon::ZERO_HEAD.into(), fault: None,
        };
        ServiceDouble { cfg, challenge_key, ack_key, state: Mutex::new(state) }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, State> { self.state.lock().unwrap_or_else(|e| e.into_inner()) }

    pub fn advance_mono_ns(&self, ns: u64) { let mut s = self.lock(); s.mono_ns += ns; }
    /// Wall time is audit-only; jumping it must not change any acceptance decision.
    pub fn set_wall_us(&self, us: i64) { self.lock().wall_us = us; }
    pub fn inject(&self, f: ServiceFault) { self.lock().fault = Some(f); }
    pub fn boot_id(&self) -> String { self.lock().boot_id.clone() }
    pub fn ledger_len(&self) -> u64 { self.lock().ledger_len }
    pub fn ledger_head(&self) -> String { self.lock().ledger_head.clone() }

    /// New boot and monotonic domain. Outstanding challenges from the old boot become invalid;
    /// their expiry is never compared with the new clock. Committed acks stay retrievable.
    pub fn restart(&self) {
        let mut s = self.lock();
        s.boot += 1;
        s.boot_id = synthetic("boot", s.boot)[..32].into();
        s.clock_epoch_id = synthetic("clock", s.boot)[..32].into();
        s.mono_ns = 1_000_000;
        for r in s.challenges.values_mut() {
            if r.state == ChState::Unused { r.state = ChState::Invalidated; }
        }
    }

    pub fn route(&self, method: &str, path: &str, body: &[u8]) -> Result<Vec<u8>, ServiceError> {
        match (method, path) {
            ("POST", PATH_CHALLENGES) => self.post_challenges(body),
            ("POST", PATH_COMMITMENTS) => self.post_commitments(body),
            ("GET", p) if p.starts_with(PATH_ACKS) && body.is_empty() => self.get_ack(&p[PATH_ACKS.len()..]),
            _ => Err(ServiceError::NotRouted),
        }
    }

    pub fn post_challenges(&self, body: &[u8]) -> Result<Vec<u8>, ServiceError> {
        let req: ChallengeRequest = canon::parse_canonical(body).map_err(bad)?;
        req.validate().map_err(bad)?;
        let cfg = &self.cfg;
        if req.service != cfg.service || req.audience != cfg.audience || req.policy_id != cfg.policy_id { return Err(ServiceError::BindingMismatch); }
        let mut s = self.lock();
        s.counter += 1;
        let unsigned = ChallengeUnsigned {
            schema: SCHEMA_CHALLENGE.into(),
            key_id: super::keys::key_id_for(&self.challenge_key.verifying_key()),
            alg: ALG_ED25519.into(),
            purpose: req.purpose, service: req.service, audience: req.audience, policy_id: req.policy_id,
            challenge_id: synthetic("challenge", s.counter),
            server_nonce: synthetic("server_nonce", s.counter),
            client_nonce: req.client_nonce,
            session_id: req.session_id, device_epoch_id: req.device_epoch_id, capture_epoch_id: req.capture_epoch_id,
            server_boot_id: s.boot_id.clone(), server_clock_epoch_id: s.clock_epoch_id.clone(),
            mono_unit: MONO_UNIT.into(), wall_unit: WALL_UNIT.into(),
            issued_mono_ns: dec(s.mono_ns), expires_mono_ns: dec(s.mono_ns + cfg.challenge_lifetime_ns), issued_wall_us: s.wall_us.to_string(),
        };
        let signed = SignedChallenge::sign(unsigned, &self.challenge_key).map_err(bad)?;
        let bytes = canon::canonical_bytes(&signed).map_err(bad)?;
        s.challenges.insert(signed.unsigned_payload.challenge_id.clone(), ChallengeRecord { signed, state: ChState::Unused });
        Ok(bytes)
    }

    pub fn post_commitments(&self, body: &[u8]) -> Result<Vec<u8>, ServiceError> {
        let req: CommitmentRequest = canon::parse_canonical(body).map_err(bad)?;
        ensure(req.schema == SCHEMA_COMMITMENT_REQUEST, Reason::WrongSchema).map_err(bad)?;
        let c = &req.commitment;
        c.validate().map_err(bad)?;
        let cdigest = c.digest().map_err(bad)?;
        let mut s = self.lock();
        let (stored, state) = match s.challenges.get(&req.challenge.unsigned_payload.challenge_id) {
            None => return Err(ServiceError::UnknownChallenge),
            Some(r) => (r.signed.clone(), r.state.clone()),
        };
        if stored != req.challenge { return Err(ServiceError::ChallengeMismatch); }
        match state {
            ChState::Consumed { commitment_digest, ack_id } => {
                return if commitment_digest == cdigest { s.acks.get(&ack_id).cloned().ok_or(ServiceError::NotFound) } else { Err(ServiceError::Conflict) };
            }
            ChState::Invalidated => return Err(ServiceError::InvalidatedByRestart),
            ChState::Unused => {}
        }
        let ch = &stored.unsigned_payload;
        if ch.server_boot_id != s.boot_id { return Err(ServiceError::InvalidatedByRestart); }
        if s.mono_ns >= ch.expires() { return Err(ServiceError::Expired); }
        let bound = c.challenge_id == ch.challenge_id && c.challenge_digest == ch.digest().map_err(bad)?
            && c.client_nonce == ch.client_nonce && c.server_nonce == ch.server_nonce && c.purpose == ch.purpose
            && c.session_id == ch.session_id && c.device_epoch_id == ch.device_epoch_id && c.capture_epoch_id == ch.capture_epoch_id
            && c.service == ch.service && c.audience == ch.audience && c.policy_id == ch.policy_id;
        if !bound { return Err(ServiceError::BindingMismatch); }

        let key = StreamKey { session: c.session_id.clone(), device_epoch: c.device_epoch_id.clone(), capture_epoch: c.capture_epoch_id.clone(), chain: c.chain.chain_id.clone() };
        match s.streams.get(&key) {
            None if c.index() == 0 => {}
            None => return Err(ServiceError::Fork),
            Some(st) if st.closed => return Err(ServiceError::StreamClosed),
            Some(st) => {
                let next = c.index() == st.next_index && c.prior_ack_id.as_deref() == Some(st.last_ack_id.as_str())
                    && c.chain.predecessor() == Some(st.last_seq) && c.chain.predecessor_head == st.end_head && c.seal_hash == st.seal_hash;
                if !next { return Err(ServiceError::Fork); }
            }
        }

        match s.fault {
            Some(ServiceFault::CrashBeforeCommit) => { s.fault = None; return Err(ServiceError::Unavailable); }
            Some(ServiceFault::RetentionFailure) => { s.fault = None; return Err(ServiceError::RetentionFailed); }
            _ => {}
        }

        let unsigned = AckUnsigned {
            schema: SCHEMA_ACK.into(),
            key_id: super::keys::key_id_for(&self.ack_key.verifying_key()),
            alg: ALG_ED25519.into(),
            purpose: c.purpose, service: c.service.clone(), audience: c.audience.clone(), policy_id: c.policy_id.clone(),
            commitment: c.clone(), commitment_digest: cdigest.clone(),
            server_boot_id: s.boot_id.clone(), server_clock_epoch_id: s.clock_epoch_id.clone(),
            mono_source: MONO_SOURCE.into(), mono_unit: MONO_UNIT.into(), wall_unit: WALL_UNIT.into(), wall_quality: WALL_QUALITY.into(),
            received_mono_ns: dec(s.mono_ns), received_wall_us: s.wall_us.to_string(),
            ledger_seq: dec(s.ledger_len), prev_ledger_entry_digest: s.ledger_head.clone(),
        };
        let signed = SignedAck::sign(unsigned, &self.ack_key).map_err(bad)?;
        let bytes = canon::canonical_bytes(&signed).map_err(bad)?;
        let entry = json!({"ledger_seq": dec(s.ledger_len), "ack_id": signed.ack_id, "commitment_digest": cdigest, "prev": s.ledger_head});
        let entry_digest = canon::digest(DOMAIN_LEDGER_ENTRY, &entry).map_err(bad)?;

        let ack_id = signed.ack_id.clone();
        if let Some(r) = s.challenges.get_mut(&ch.challenge_id) {
            r.state = ChState::Consumed { commitment_digest: cdigest, ack_id: ack_id.clone() };
        }
        s.streams.insert(key, Stream {
            next_index: c.index() + 1, last_ack_id: ack_id.clone(), last_seq: c.chain.last(), end_head: c.chain.end_head.clone(),
            seal_hash: c.seal_hash.clone(), closed: c.purpose == Purpose::Close,
        });
        s.ledger_len += 1;
        s.ledger_head = entry_digest;
        s.acks.insert(ack_id, bytes.clone());

        if s.fault == Some(ServiceFault::ResponseLostAfterCommit) { s.fault = None; return Err(ServiceError::ResponseLost); }
        Ok(bytes)
    }

    pub fn get_ack(&self, ack_id: &str) -> Result<Vec<u8>, ServiceError> {
        if !canon::is_hex64(ack_id) { return Err(ServiceError::NotFound); }
        self.lock().acks.get(ack_id).cloned().ok_or(ServiceError::NotFound)
    }
}
