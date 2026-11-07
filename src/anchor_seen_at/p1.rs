//! Local P1 context and append-only lineage. Not a production trust channel or final gate.
//!
//! File operations belong on a storage worker, never on capture/HID. Creation is exclusive;
//! there is deliberately no resume/import/reset API. Missing prior history cannot be repaired
//! by opening a fresh object. The host controls source, clock claims and filesystem: this
//! journal is not independent retention or protection against a hostile administrator.

use super::check::{self, ClientPolicy, ClockRef, LocalSamples};
use super::coverage::StreamEligibility;
use super::types::{Commitment, Purpose, SignedChallenge};
use super::{canon, AnchorState, Reason};
use serde::Serialize;
use serde_json::json;
use std::collections::BTreeMap;
use std::fs::{File, OpenOptions};
use std::io::{self, Write};
use std::path::Path;
use std::sync::Arc;
use std::time::Instant;

/// A host-supplied observation, not authenticated time. Captured before the response.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ExchangeOrigin {
    pub clock: ClockRef,
    pub challenge_received_mono_ns: String,
    pub echo_capture_mono_ns: String,
    pub send_mono_ns: String,
}

/// Untrusted arrival input; the first observation is retained, never replaced by a retry.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Arrival {
    pub clock: ClockRef,
    pub receive_mono_ns: String,
    pub transport_result: String,
}

/// Immutable expectation, minted only after its local journal append succeeds.
/// All clones retain the same owner and original data, not independent authority.
#[doc = include_str!("../../tests/anchor_seen_at/p1_api.md")]
#[derive(Clone, Debug)]
pub struct OriginalExchange {
    owner: Arc<()>,
    event: u64,
    challenge: SignedChallenge,
    commitment: Commitment,
    origin: ExchangeOrigin,
    policy: ClientPolicy,
    enqueued: Instant,
}
impl OriginalExchange {
    pub fn event(&self) -> u64 {
        self.event
    }
    pub fn challenge(&self) -> &SignedChallenge {
        &self.challenge
    }
    pub fn commitment(&self) -> &Commitment {
        &self.commitment
    }
    pub fn origin(&self) -> &ExchangeOrigin {
        &self.origin
    }
    pub fn policy(&self) -> &ClientPolicy {
        &self.policy
    }
}

/// First accepted opening plus its exact owned context; never a caller's substitute.
#[derive(Debug)]
pub struct OpeningRecord {
    original: OriginalExchange,
    state: AnchorState,
}
impl OpeningRecord {
    pub fn original(&self) -> &OriginalExchange {
        &self.original
    }
    pub fn state(&self) -> &AnchorState {
        &self.state
    }
}

/// Local observation of a policy claim. No endpoint, signature or trust-root enrollment.
/// It cannot be used with another lineage. Host policy origin remains a prerequisite.
#[derive(Debug)]
pub struct PolicyObservation {
    owner: Arc<()>,
    generation: u64,
    digest: String,
    observed: Instant,
}

/// Diagnostic qualification at an earlier sample, not a publication capability.
#[derive(Debug)]
pub struct DecisionTimeRecord {
    policy_digest: String,
    generation: u64,
    journal_head: String,
    opening_event: u64,
    age_ns: u128,
    sample_offset_ns: u128,
}
impl DecisionTimeRecord {
    pub fn label(&self) -> &'static str {
        "checked at decision time"
    }
    pub fn policy_digest(&self) -> &str {
        &self.policy_digest
    }
    pub fn generation(&self) -> u64 {
        self.generation
    }
    pub fn journal_head(&self) -> &str {
        &self.journal_head
    }
    pub fn opening_event(&self) -> u64 {
        self.opening_event
    }
    pub fn age_ns(&self) -> u128 {
        self.age_ns
    }
    pub fn sample_offset_ns(&self) -> u128 {
        self.sample_offset_ns
    }
    pub fn clock_scope(&self) -> &'static str {
        "process-local Instant; no restart mapping"
    }
    /// Arbitrary pause/expiry after the sample is allowed by this diagnostic claim.
    pub fn publication_time_freshness_asserted(&self) -> bool {
        false
    }
}

#[derive(Debug)]
struct FirstArrival {
    arrival: Arrival,
    bytes: Vec<u8>,
    state: AnchorState,
}

/// Exclusive local owner. Not Clone, serializable, publicly constructible from a snapshot,
/// or a source of current final authority. Existing readiness remains a diagnostic only.
#[derive(Debug)]
pub struct DurableLineage {
    file: File,
    clock_origin: Instant,
    owner: Arc<()>,
    policy: ClientPolicy,
    total_deadline_ns: Option<u64>,
    decision_use_age_ns: Option<u64>,
    seq: u64,
    head: String,
    failed: bool,
    eligibility: StreamEligibility,
    expected: BTreeMap<u64, OriginalExchange>,
    arrivals: BTreeMap<u64, FirstArrival>,
    ack_events: BTreeMap<String, u64>,
    opening: Option<OpeningRecord>,
    policy_generation: u64,
    source_last_seq: u64,
}

impl DurableLineage {
    /// New local history only. No claim that an absent earlier history was complete.
    /// Existing paths are refused; no truncation, deletion, overwrite or recovery-to-ready.
    pub fn create(
        path: &Path,
        policy: ClientPolicy,
        total_deadline_ns: Option<u64>,
        decision_use_age_ns: Option<u64>,
    ) -> io::Result<Self> {
        let mut options = OpenOptions::new();
        options.write(true).append(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let file = options.open(path)?;
        let mut s = Self {
            file,
            clock_origin: Instant::now(),
            owner: Arc::new(()),
            policy,
            total_deadline_ns,
            decision_use_age_ns,
            seq: 0,
            head: canon::ZERO_HEAD.into(),
            failed: false,
            eligibility: StreamEligibility::new(),
            expected: BTreeMap::new(),
            arrivals: BTreeMap::new(),
            ack_events: BTreeMap::new(),
            opening: None,
            policy_generation: 0,
            source_last_seq: 0,
        };
        s.append(json!({"kind":"new_local_lineage", "policy":s.policy_snapshot(),
            "total_deadline_ns":total_deadline_ns.map(|n|n.to_string()), "decision_use_age_ns":decision_use_age_ns.map(|n|n.to_string())}))?;
        #[cfg(unix)]
        {
            let parent = path
                .parent()
                .filter(|p| !p.as_os_str().is_empty())
                .unwrap_or_else(|| Path::new("."));
            File::open(parent)?.sync_all()?;
        }
        Ok(s)
    }

    fn policy_snapshot(&self) -> serde_json::Value {
        json!({"trust":self.policy.trust.archive(), "checker":self.policy.local_check_version,
            "max_rtt_ns":self.policy.max_rtt_ns.map(|n|n.to_string()),
            "max_challenge_age_ns":self.policy.max_challenge_age_ns.map(|n|n.to_string())})
    }
    fn append(&mut self, payload: serde_json::Value) -> io::Result<()> {
        if self.failed {
            return Err(io::Error::other("lineage unavailable"));
        }
        let Some(next) = self.seq.checked_add(1) else {
            self.failed = true;
            self.eligibility.record(
                &AnchorState::unknown(Reason::PersistFailed),
                self.source_bound(),
            );
            return Err(io::Error::other("journal sequence exhausted"));
        };
        let record = json!({"schema":"local.p1.journal.v1", "seq":self.seq.to_string(), "previous":self.head, "payload":payload});
        let result = (|| {
            let bytes = canon::canonical_bytes(&record)
                .map_err(|_| io::Error::other("journal encoding"))?;
            let head = canon::digest("QOR-LOCAL-P1-JOURNAL-v1", &record)
                .map_err(|_| io::Error::other("journal digest"))?;
            self.file.write_all(&bytes)?;
            self.file.write_all(b"\n")?;
            self.file.flush()?;
            self.file.sync_all()?;
            Ok(head)
        })();
        match result {
            Ok(head) => {
                self.head = head;
                self.seq = next;
                Ok(())
            }
            Err(e) => {
                self.failed = true;
                self.eligibility.record(
                    &AnchorState::unknown(Reason::PersistFailed),
                    self.source_bound(),
                );
                Err(e)
            }
        }
    }
    fn source_bound(&self) -> u64 {
        self.source_last_seq
    }
    fn fail(&mut self, reason: Reason, bound: u64) -> AnchorState {
        self.source_last_seq = self.source_last_seq.max(bound);
        let state = AnchorState::unknown(reason);
        self.eligibility.record(&state, bound);
        if self.append(json!({"kind":"failure", "reason":reason.code(), "source_last_seq":bound.to_string()})).is_err() { self.failed = true; }
        state
    }

    /// Persist the exact original request/origin/policy before any arrival is admitted.
    /// Caller-provided source and clock claims are not made authentic by this operation.
    pub fn expect(
        &mut self,
        challenge: &SignedChallenge,
        commitment: &Commitment,
        origin: &ExchangeOrigin,
    ) -> Result<OriginalExchange, Reason> {
        if self.failed {
            return Err(Reason::PersistFailed);
        }
        if let Err(r) = commitment.validate() {
            self.fail(r, self.source_bound());
            return Err(r);
        }
        let original = OriginalExchange {
            owner: self.owner.clone(),
            event: self.seq,
            challenge: challenge.clone(),
            commitment: commitment.clone(),
            origin: origin.clone(),
            policy: self.policy.clone(),
            enqueued: Instant::now(),
        };
        self.append(
            json!({"kind":"expected", "event":original.event.to_string(), "challenge":challenge,
            "commitment":commitment, "origin":origin, "policy":self.policy_snapshot(),
            "enqueued_offset_ns":original.enqueued.duration_since(self.clock_origin).as_nanos().to_string(),
            "clock_scope":"process-local Instant; no restart mapping"}),
        )
        .map_err(|_| Reason::PersistFailed)?;
        self.expected.insert(original.event, original.clone());
        Ok(original)
    }

    /// Persist all original bytes and observations before advancing diagnostic coverage.
    /// Exact retry retains the first classification; different original context breaks.
    pub fn observe(
        &mut self,
        original: &OriginalExchange,
        bytes: &[u8],
        arrival: &Arrival,
        source_last_seq: u64,
    ) -> AnchorState {
        self.source_last_seq = self.source_last_seq.max(source_last_seq);
        if self.failed {
            return self.fail(Reason::PersistFailed, source_last_seq);
        }
        if !Arc::ptr_eq(&original.owner, &self.owner)
            || !self.expected.contains_key(&original.event)
        {
            return self.fail(Reason::BindingMismatch, source_last_seq);
        }
        if let Some(first) = self.arrivals.get(&original.event) {
            let same = first.arrival == *arrival && first.bytes == bytes;
            let state = first.state.clone();
            if !same {
                return self.fail(Reason::BindingMismatch, source_last_seq);
            }
            if self.append(json!({"kind":"identical_retry", "event":original.event.to_string(), "source_last_seq":source_last_seq.to_string()})).is_err() { return self.fail(Reason::PersistFailed, source_last_seq); }
            self.eligibility.record(&state, source_last_seq);
            return state;
        }
        let samples = LocalSamples {
            send_clock: original.origin.clock.clone(),
            receive_clock: arrival.clock.clone(),
            challenge_received_mono_ns: original.origin.challenge_received_mono_ns.clone(),
            echo_capture_mono_ns: original.origin.echo_capture_mono_ns.clone(),
            send_mono_ns: original.origin.send_mono_ns.clone(),
            receive_mono_ns: arrival.receive_mono_ns.clone(),
            transport_result: arrival.transport_result.clone(),
        };
        let mut state = check::check_ack(
            &original.challenge,
            bytes,
            &original.commitment,
            &samples,
            &original.policy,
        );
        if let AnchorState::SeenAtRetained(f) | AnchorState::SeenAtLate(f) = &state {
            if self
                .ack_events
                .get(f.ack_id())
                .is_some_and(|event| *event != original.event)
            {
                state = AnchorState::unknown(Reason::BindingMismatch);
            }
        }
        // Candidate classification is not authority. Restart never imports it as eligibility.
        if self.append(json!({"kind":"arrival_candidate", "event":original.event.to_string(), "raw_ack":canon::b64url(bytes),
            "arrival":arrival, "source_last_seq":source_last_seq.to_string(), "classification":state.status_code(), "reasons":state.reasons()})).is_err() {
            return self.fail(Reason::PersistFailed, source_last_seq);
        }
        let elapsed = original.enqueued.elapsed().as_nanos();
        match self.total_deadline_ns {
            None => {
                state = AnchorState::unknown(Reason::TimingBudgetUnset);
            }
            Some(limit) if elapsed >= u128::from(limit) => {
                state = match state {
                    AnchorState::SeenAtRetained(f) | AnchorState::SeenAtLate(f) => {
                        AnchorState::SeenAtLate(f.into_late())
                    }
                    other => other,
                };
            }
            Some(_) => {}
        }
        // A first late outcome never gets replaced, even by a faster later attempt.
        if self.append(json!({"kind":"resolution", "event":original.event.to_string(), "elapsed_ns":elapsed.to_string(),
            "classification":state.status_code(), "reasons":state.reasons()})).is_err() { return self.fail(Reason::PersistFailed, source_last_seq); }
        // Durability time includes the resolution append too. If it crosses the original
        // deadline, no retained result escapes; the late observation is appended instead.
        if self
            .total_deadline_ns
            .is_some_and(|n| original.enqueued.elapsed().as_nanos() >= u128::from(n))
        {
            if let AnchorState::SeenAtRetained(f) = state {
                state = AnchorState::SeenAtLate(f.into_late());
            }
            if self
                .append(
                    json!({"kind":"deadline_after_resolution", "event":original.event.to_string()}),
                )
                .is_err()
            {
                return self.fail(Reason::PersistFailed, source_last_seq);
            }
        }
        if let AnchorState::SeenAtRetained(f) | AnchorState::SeenAtLate(f) = &state {
            self.ack_events.insert(f.ack_id().clone(), original.event);
        }
        self.eligibility.record(&state, source_last_seq);
        if self.opening.is_none()
            && original.commitment.purpose == Purpose::Opening
            && state.retained().is_some()
            && self.eligibility.accepted_opening().is_some()
            && self.eligibility.break_reason().is_none()
        {
            self.opening = Some(OpeningRecord {
                original: original.clone(),
                state: state.clone(),
            });
        }
        self.arrivals.insert(
            original.event,
            FirstArrival {
                arrival: arrival.clone(),
                bytes: bytes.to_vec(),
                state: state.clone(),
            },
        );
        state
    }

    pub fn record_failure(&mut self, reason: Reason, source_last_seq: u64) {
        self.fail(reason, source_last_seq);
    }
    pub fn opening(&self) -> Option<&OpeningRecord> {
        self.opening.as_ref()
    }
    pub fn diagnostic_coverage(&self) -> &StreamEligibility {
        &self.eligibility
    }
    pub fn journal_head(&self) -> &str {
        &self.head
    }
    pub fn unavailable(&self) -> bool {
        self.failed
    }

    /// Record a host policy claim locally. This does not authenticate any remote authority.
    pub fn note_policy(&mut self, digest: &str) -> Result<PolicyObservation, Reason> {
        let Some(generation) = self.policy_generation.checked_add(1) else {
            self.fail(Reason::SchemaViolation, self.source_bound());
            return Err(Reason::SchemaViolation);
        };
        if !canon::is_hex64(digest) {
            return Err(Reason::FieldFormat);
        }
        self.append(json!({"kind":"policy_observation", "generation":generation.to_string(), "digest":digest})).map_err(|_|Reason::PersistFailed)?;
        self.policy_generation = generation;
        Ok(PolicyObservation {
            owner: self.owner.clone(),
            generation,
            digest: digest.into(),
            observed: Instant::now(),
        })
    }
    /// Qualify only the earlier local sample. Expiry/pause after it is NOT bounded here.
    /// No later effect, current-at-publication status or network permission is supplied.
    pub fn check_at_decision_time(
        &mut self,
        policy: &PolicyObservation,
    ) -> Result<DecisionTimeRecord, Reason> {
        if self.failed {
            return Err(Reason::PersistFailed);
        }
        if !Arc::ptr_eq(&policy.owner, &self.owner) || policy.generation != self.policy_generation {
            return Err(Reason::PolicyMismatch);
        }
        if let Some(r) = self.eligibility.break_reason() {
            return Err(r);
        }
        // A registered original without an outcome is not silently filtered away.
        if let Some(pending) = self
            .expected
            .values()
            .find(|x| !self.arrivals.contains_key(&x.event))
        {
            let event = pending.event;
            let elapsed = pending.enqueued.elapsed().as_nanos();
            let expired = self
                .total_deadline_ns
                .is_none_or(|limit| elapsed >= u128::from(limit));
            if expired {
                let reason = if self.total_deadline_ns.is_none() {
                    Reason::TimingBudgetUnset
                } else {
                    Reason::NoAck
                };
                self.append(json!({"kind":"missing_outcome", "event":event.to_string(), "elapsed_ns":elapsed.to_string(), "reason":reason.code()})).map_err(|_| Reason::PersistFailed)?;
                self.fail(reason, self.source_bound());
                if self.failed {
                    return Err(Reason::PersistFailed);
                }
                return Err(reason);
            }
            return Err(Reason::NoAck);
        }
        let opening_event = self.opening.as_ref().ok_or(Reason::NoAck)?.original.event;
        let sample = Instant::now();
        let age_ns = sample.duration_since(policy.observed).as_nanos();
        let sample_offset_ns = sample.duration_since(self.clock_origin).as_nanos();
        qualify_age(age_ns, self.decision_use_age_ns)?;
        self.append(json!({"kind":"decision_time_check", "label":"checked at decision time", "policy":policy.digest,
            "generation":policy.generation.to_string(), "age_ns":age_ns.to_string(),
            "clock_scope":"process-local Instant; no restart mapping", "sample_offset_ns":sample_offset_ns.to_string(), "opening_event":opening_event.to_string(),
            "publication_time_freshness":"not_asserted"})).map_err(|_|Reason::PersistFailed)?;
        Ok(DecisionTimeRecord {
            policy_digest: policy.digest.clone(),
            generation: policy.generation,
            journal_head: self.head.clone(),
            opening_event,
            age_ns,
            sample_offset_ns,
        })
    }
}

fn qualify_age(age_ns: u128, limit: Option<u64>) -> Result<(), Reason> {
    let limit = limit.ok_or(Reason::TimingBudgetUnset)?;
    if age_ns >= u128::from(limit) {
        Err(Reason::OverBudget)
    } else {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn decision_age_equality_and_max_are_strict() {
        assert_eq!(qualify_age(0, None), Err(Reason::TimingBudgetUnset));
        assert_eq!(
            qualify_age(u128::MAX, Some(u64::MAX)),
            Err(Reason::OverBudget)
        );
        for (age, limit, accepted) in [
            (0, 1, true),
            (1, 1, false),
            (u64::MAX - 1, u64::MAX, true),
            (u64::MAX, u64::MAX, false),
        ] {
            assert_eq!(qualify_age(u128::from(age), Some(limit)).is_ok(), accepted);
        }
    }
    #[test]
    fn journal_failure_and_sequence_exhaustion_never_recover() {
        let policy = ClientPolicy {
            trust: super::super::keys::TrustPolicy::new(
                "synthetic",
                "synthetic",
                "synthetic",
                &[],
                vec![],
            )
            .unwrap(),
            local_check_version: "synthetic".into(),
            max_rtt_ns: None,
            max_challenge_age_ns: None,
        };
        let generation_path = std::env::temp_dir().join(format!(
            "qorcap-p1-generation-{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let mut generations =
            DurableLineage::create(&generation_path, policy.clone(), None, None).unwrap();
        generations.policy_generation = u64::MAX;
        assert_eq!(
            generations.note_policy(&"a".repeat(64)).unwrap_err(),
            Reason::SchemaViolation
        );
        assert_eq!(
            generations.diagnostic_coverage().break_reason(),
            Some(Reason::SchemaViolation)
        );
        for overflow in [false, true] {
            let path = std::env::temp_dir().join(format!(
                "qorcap-p1-fault-{}-{overflow}",
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_nanos()
            ));
            let mut l = DurableLineage::create(&path, policy.clone(), None, None).unwrap();
            if overflow {
                l.seq = u64::MAX;
            } else {
                l.file = File::open(&path).unwrap();
            }
            assert!(l.append(json!({"kind":"fault_control"})).is_err());
            assert!(l.unavailable());
            assert_eq!(
                l.diagnostic_coverage().break_reason(),
                Some(Reason::PersistFailed)
            );
            assert!(l.append(json!({"kind":"cannot_recover"})).is_err());
            assert!(l.opening().is_none());
        }
    }
}
