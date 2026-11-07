//! Test-only deterministic schedules, not elapsed-time or platform durability measurements.
use super::*;
use crate as qorcap;
#[path = "../../tests/anchor_seen_at/fixtures.rs"]
mod fixtures;
use fixtures::*;

#[derive(Debug)]
pub(super) struct SyncSchedule {
    target: &'static str,
    after: u128,
    pub(super) elapsed: u128,
    trace: Vec<(String, &'static str, u128)>,
}
impl SyncSchedule {
    pub(super) fn before_sync(&mut self, kind: &str) {
        self.trace.push((kind.into(), "before", self.elapsed));
    }
    pub(super) fn after_sync(&mut self, kind: &str) {
        if kind == self.target {
            self.elapsed = self.after;
        }
        self.trace.push((kind.into(), "after", self.elapsed));
    }
}
fn path(label: &str) -> std::path::PathBuf {
    std::env::temp_dir().join(format!(
        "qorcap-p1-followup-{label}-{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ))
}
fn scheduled_sync(target: &'static str, crosses: bool) {
    let w = World::new();
    let s = Session::new("p1-sync-schedule");
    let e = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    let limit = 1_000_000_000u64; // Synthetic policy only; not a measured budget.
    let after = u128::from(if crosses { limit } else { limit - 1 });
    let mut l =
        DurableLineage::create(&path(target), w.policy.clone(), Some(limit), Some(u64::MAX))
            .unwrap();
    l.sync_schedule = Some(SyncSchedule {
        target,
        after,
        elapsed: 0,
        trace: vec![],
    });
    let origin = ExchangeOrigin {
        clock: e.samples.send_clock.clone(),
        challenge_received_mono_ns: e.samples.challenge_received_mono_ns.clone(),
        echo_capture_mono_ns: e.samples.echo_capture_mono_ns.clone(),
        send_mono_ns: e.samples.send_mono_ns.clone(),
    };
    let arrival = Arrival {
        clock: e.samples.receive_clock.clone(),
        receive_mono_ns: e.samples.receive_mono_ns.clone(),
        transport_result: e.samples.transport_result.clone(),
    };
    let original = l.expect(&e.ch, &e.commitment, &origin).unwrap();
    let state = l.observe(&original, &e.ack_bytes, &arrival, 9);
    assert_eq!(l.total_deadline_ns, Some(limit));
    let schedule = l.sync_schedule.as_ref().unwrap();
    let boundary: Vec<_> = schedule
        .trace
        .iter()
        .filter(|(kind, _, _)| kind == target)
        .collect();
    assert_eq!(boundary.len(), 2);
    assert_eq!(boundary[0].1, "before");
    assert_eq!(boundary[0].2, 0);
    assert_eq!(boundary[1].1, "after");
    assert_eq!(boundary[1].2, after);
    let has_marker = schedule
        .trace
        .iter()
        .any(|(kind, _, _)| kind == "deadline_after_resolution");
    assert_eq!(has_marker, crosses);
    if crosses {
        let AnchorState::SeenAtLate(f) = state else {
            panic!("late result required")
        };
        assert!(f.is_late());
        assert!(AnchorState::SeenAtRetained(f).retained().is_none());
        assert!(l.opening().is_none());
        assert_eq!(l.eligibility.break_reason(), Some(Reason::LateAck));
    } else {
        assert!(state.retained().is_some());
        assert!(l.opening().is_some());
        assert_eq!(l.eligibility.break_reason(), None);
    }
    println!("synthetic sync schedule target={target} before=0 after={after} limit={limit} crosses={crosses}; real sync invoked; no sleep");
}
#[test]
fn candidate_sync_predeadline_positive() {
    scheduled_sync("arrival_candidate", false)
}
#[test]
fn candidate_sync_crossing_latches_late() {
    scheduled_sync("arrival_candidate", true)
}
#[test]
fn resolution_sync_predeadline_positive() {
    scheduled_sync("resolution", false)
}
#[test]
fn resolution_sync_crossing_latches_late() {
    scheduled_sync("resolution", true)
}
#[test]
fn journal_max_minus_one_append_succeeds_then_exhausts() {
    let w = World::new();
    let mut l =
        DurableLineage::create(&path("journal-near-limit"), w.policy.clone(), None, None).unwrap();
    // Private synthetic near-limit setup, not an imported complete historical journal.
    l.seq = u64::MAX - 1;
    let prior = l.head.clone();
    l.append(json!({"kind":"near_limit_positive"})).unwrap();
    assert_eq!(l.seq, u64::MAX);
    assert_ne!(l.head, prior);
    assert!(!l.failed);
    let accepted = l.head.clone();
    assert!(l.append(json!({"kind":"exhaustion_negative"})).is_err());
    assert_eq!(l.seq, u64::MAX);
    assert_eq!(l.head, accepted);
    assert!(l.failed);
    assert_eq!(l.eligibility.break_reason(), Some(Reason::PersistFailed));
}
#[test]
fn generation_max_minus_one_succeeds_then_exhausts() {
    let w = World::new();
    let mut l =
        DurableLineage::create(&path("generation-near-limit"), w.policy.clone(), None, None)
            .unwrap();
    l.policy_generation = u64::MAX - 1;
    let observation = l.note_policy(&hex64("policy")).unwrap();
    assert_eq!(observation.generation, u64::MAX);
    assert_eq!(l.policy_generation, u64::MAX);
    assert_eq!(l.eligibility.break_reason(), None);
    assert_eq!(
        l.note_policy(&hex64("next-policy")).unwrap_err(),
        Reason::SchemaViolation
    );
    assert_eq!(l.eligibility.break_reason(), Some(Reason::SchemaViolation));
    assert_eq!(
        l.check_at_decision_time(&observation).unwrap_err(),
        Reason::SchemaViolation
    );
}
