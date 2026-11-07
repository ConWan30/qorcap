//! Synthetic local P1 controls. No transport, capture, current-policy authority or final sink.
use super::fixtures::*;
use qorcap::anchor_seen_at::canon;
use qorcap::anchor_seen_at::p1::*;
use qorcap::anchor_seen_at::types::Purpose;
use qorcap::anchor_seen_at::{AnchorState, Reason};
use std::{
    fs,
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
};
const LOCAL_LIMIT: u64 = 120_000_000_000; // Synthetic only, not an eligibility recommendation.
static NEXT: AtomicU64 = AtomicU64::new(0);
fn path() -> PathBuf {
    std::env::temp_dir().join(format!(
        "qorcap-p1-{}-{}.jsonl",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ))
}
fn origin(e: &Exchange) -> ExchangeOrigin {
    ExchangeOrigin {
        clock: e.samples.send_clock.clone(),
        challenge_received_mono_ns: e.samples.challenge_received_mono_ns.clone(),
        echo_capture_mono_ns: e.samples.echo_capture_mono_ns.clone(),
        send_mono_ns: e.samples.send_mono_ns.clone(),
    }
}
fn arrival(e: &Exchange) -> Arrival {
    Arrival {
        clock: e.samples.receive_clock.clone(),
        receive_mono_ns: e.samples.receive_mono_ns.clone(),
        transport_result: e.samples.transport_result.clone(),
    }
}
fn lineage(w: &World) -> (DurableLineage, PathBuf) {
    let p = path();
    (
        DurableLineage::create(&p, w.policy.clone(), Some(LOCAL_LIMIT), Some(LOCAL_LIMIT)).unwrap(),
        p,
    )
}
fn observe(l: &mut DurableLineage, e: &Exchange, bound: u64) -> AnchorState {
    let x = l.expect(&e.ch, &e.commitment, &origin(e)).unwrap();
    l.observe(&x, &e.ack_bytes, &arrival(e), bound)
}
#[test]
fn p1_full_originals_are_durable_and_hash_linked() {
    let w = World::new();
    let s = Session::new("p1-full");
    let stream = full_stream(&w, &s);
    let (mut l, p) = lineage(&w);
    for e in &stream {
        assert_eq!(observe(&mut l, e, 29).status_code(), "seen_at_retained");
    }
    assert!(l.diagnostic_coverage().complete());
    let o = l.opening().unwrap();
    assert_eq!(o.original().commitment(), &stream[0].commitment);
    assert_eq!(o.state().retained().unwrap().ack_id(), &stream[0].ack_id());
    let mut previous = canon::ZERO_HEAD.to_string();
    let text = fs::read(&p).unwrap();
    for (seq, line) in text
        .split(|b| *b == b'\n')
        .filter(|b| !b.is_empty())
        .enumerate()
    {
        let row: serde_json::Value = canon::parse_canonical(line).unwrap();
        assert_eq!(row["seq"], seq.to_string());
        assert_eq!(row["previous"], previous);
        previous = canon::digest("QOR-LOCAL-P1-JOURNAL-v1", &row).unwrap();
    }
    assert_eq!(previous, l.journal_head());
    assert!(String::from_utf8(text).unwrap().contains("raw_ack"));
    assert!(
        DurableLineage::create(&p, w.policy.clone(), Some(LOCAL_LIMIT), Some(LOCAL_LIMIT)).is_err()
    );
}
#[test]
fn p1_original_context_survives_caller_mutation() {
    let w = World::new();
    let s = Session::new("p1-owned");
    let e = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    let (mut l, _) = lineage(&w);
    let mut ch = e.ch.clone();
    let mut c = e.commitment.clone();
    let mut o = origin(&e);
    let x = l.expect(&ch, &c, &o).unwrap();
    ch.unsigned_payload.session_id = id("other");
    c.seal_hash = hex64("other");
    o.clock.boot_id = id("other");
    assert_eq!(
        l.observe(&x, &e.ack_bytes, &arrival(&e), 9).status_code(),
        "seen_at_retained"
    );
    assert_eq!(l.opening().unwrap().original().origin(), &origin(&e));
}
#[test]
fn p1_detached_context_cannot_cross_owners() {
    let w = World::new();
    let s = Session::new("p1-owner");
    let e = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    let (mut a, _) = lineage(&w);
    let (mut b, _) = lineage(&w);
    let x = a.expect(&e.ch, &e.commitment, &origin(&e)).unwrap();
    assert!(matches!(
        b.observe(&x, &e.ack_bytes, &arrival(&e), 9),
        AnchorState::Unknown { .. }
    ));
    assert!(b.opening().is_none());
}
#[test]
fn p1_exact_retry_does_not_advance_twice() {
    let w = World::new();
    let s = Session::new("p1-retry");
    let e = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    let (mut l, _) = lineage(&w);
    let x = l.expect(&e.ch, &e.commitment, &origin(&e)).unwrap();
    let first = l.observe(&x, &e.ack_bytes, &arrival(&e), 9);
    let segments = l.diagnostic_coverage().segments();
    assert_eq!(l.observe(&x.clone(), &e.ack_bytes, &arrival(&e), 9), first);
    assert_eq!(l.diagnostic_coverage().segments(), segments);
}
#[test]
fn p1_changed_arrival_is_not_an_identical_retry() {
    let w = World::new();
    let s = Session::new("p1-recheck");
    let e = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    let (mut l, _) = lineage(&w);
    let x = l.expect(&e.ch, &e.commitment, &origin(&e)).unwrap();
    l.observe(&x, &e.ack_bytes, &arrival(&e), 9);
    let mut a = arrival(&e);
    a.receive_mono_ns = "3001".into();
    assert!(matches!(
        l.observe(&x, &e.ack_bytes, &a, 9),
        AnchorState::Unknown { .. }
    ));
    assert!(l.diagnostic_coverage().break_reason().is_some());
    l.observe(&x, &e.ack_bytes, &arrival(&e), 9);
    assert!(l.diagnostic_coverage().break_reason().is_some());
}
#[test]
fn p1_same_ack_under_new_expectation_cannot_replace_original() {
    let w = World::new();
    let s = Session::new("p1-new-expect");
    let e = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    let (mut l, _) = lineage(&w);
    observe(&mut l, &e, 9);
    let mut o = origin(&e);
    o.send_mono_ns = "3999".into();
    let y = l.expect(&e.ch, &e.commitment, &o).unwrap();
    assert!(matches!(
        l.observe(&y, &e.ack_bytes, &arrival(&e), 9),
        AnchorState::Unknown { .. }
    ));
    assert_eq!(l.opening().unwrap().original().origin(), &origin(&e));
}
#[test]
fn p1_total_deadline_late_cannot_be_relabelled() {
    let w = World::new();
    let s = Session::new("p1-late");
    let e = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    let mut l =
        DurableLineage::create(&path(), w.policy.clone(), Some(0), Some(LOCAL_LIMIT)).unwrap();
    let x = l.expect(&e.ch, &e.commitment, &origin(&e)).unwrap();
    let state = l.observe(&x, &e.ack_bytes, &arrival(&e), 9);
    let AnchorState::SeenAtLate(f) = state else {
        panic!("expected late")
    };
    assert_eq!(AnchorState::SeenAtRetained(f).status_code(), "seen_at_late");
    assert!(l.opening().is_none());
    assert_eq!(
        l.diagnostic_coverage().break_reason(),
        Some(Reason::LateAck)
    );
    assert_eq!(
        l.observe(&x, &e.ack_bytes, &arrival(&e), 9).status_code(),
        "seen_at_late"
    );
}
#[test]
fn p1_rtt_late_wins_over_faster_recheck() {
    let w = World::new();
    let s = Session::new("p1-rtt-late");
    let mut e = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    e.samples.receive_mono_ns = (SYNTH_RTT_BUDGET_NS + 3001).to_string();
    let (mut l, _) = lineage(&w);
    assert_eq!(observe(&mut l, &e, 9).status_code(), "seen_at_late");
    e.samples.receive_mono_ns = "4000".into();
    assert!(matches!(
        observe(&mut l, &e, 9),
        AnchorState::Unknown { .. }
    ));
    assert_eq!(
        l.diagnostic_coverage().break_reason(),
        Some(Reason::LateAck)
    );
}
#[test]
fn p1_missing_budget_withholds() {
    let w = World::new();
    let s = Session::new("p1-unset");
    let e = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    let mut l = DurableLineage::create(&path(), w.policy.clone(), None, None).unwrap();
    assert!(matches!(
        observe(&mut l, &e, 9),
        AnchorState::Unknown { .. }
    ));
    assert!(l.opening().is_none());
    let p = l.note_policy(&hex64("policy")).unwrap();
    assert!(l.check_at_decision_time(&p).is_err());
}
#[test]
fn p1_absent_opening_cannot_mint_decision_check() {
    let w = World::new();
    let (mut l, _) = lineage(&w);
    let p = l.note_policy(&hex64("policy")).unwrap();
    assert_eq!(l.check_at_decision_time(&p).unwrap_err(), Reason::NoAck);
}
#[test]
fn p1_decision_label_is_exact_and_never_publication_freshness() {
    let w = World::new();
    let s = Session::new("p1-decision");
    let e = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    let (mut l, _) = lineage(&w);
    observe(&mut l, &e, 9);
    let p = l.note_policy(&hex64("policy")).unwrap();
    let d = l.check_at_decision_time(&p).unwrap();
    assert_eq!(d.label(), "checked at decision time");
    assert!(!d.publication_time_freshness_asserted());
    assert_eq!(d.opening_event(), l.opening().unwrap().original().event());
    assert_eq!(d.policy_digest(), hex64("policy"));
    assert!(d.clock_scope().contains("no restart mapping"));
    // Even when retained for arbitrarily later inspection, this is still only the earlier check.
    assert!(!d.publication_time_freshness_asserted());
}
#[test]
fn p1_expired_at_decision_is_refused() {
    let w = World::new();
    let s = Session::new("p1-expired");
    let e = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    let mut l =
        DurableLineage::create(&path(), w.policy.clone(), Some(LOCAL_LIMIT), Some(0)).unwrap();
    observe(&mut l, &e, 9);
    let p = l.note_policy(&hex64("policy")).unwrap();
    assert_eq!(
        l.check_at_decision_time(&p).unwrap_err(),
        Reason::OverBudget
    );
}
#[test]
fn p1_policy_generation_and_owner_are_bound() {
    let w = World::new();
    let s = Session::new("p1-policy");
    let e = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    let (mut l, _) = lineage(&w);
    observe(&mut l, &e, 9);
    let old = l.note_policy(&hex64("a")).unwrap();
    let current = l.note_policy(&hex64("b")).unwrap();
    assert_eq!(
        l.check_at_decision_time(&old).unwrap_err(),
        Reason::PolicyMismatch
    );
    assert!(l.check_at_decision_time(&current).is_ok());
    let (mut b, _) = lineage(&w);
    assert_eq!(
        b.check_at_decision_time(&current).unwrap_err(),
        Reason::PolicyMismatch
    );
}
#[test]
fn p1_sticky_history_precedes_later_success() {
    let w = World::new();
    let s = Session::new("p1-gap");
    let e = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    let (mut l, p) = lineage(&w);
    l.record_failure(Reason::Gap, 9);
    observe(&mut l, &e, 9);
    assert_eq!(l.diagnostic_coverage().break_reason(), Some(Reason::Gap));
    assert!(l.opening().is_none());
    assert!(fs::read_to_string(p).unwrap().contains("failure"));
}
#[test]
fn p1_clock_change_source_regression_and_postclose_growth_break() {
    for mode in 0..3 {
        let w = World::new();
        let s = Session::new(&format!("p1-guards-{mode}"));
        let stream = full_stream(&w, &s);
        let (mut l, _) = lineage(&w);
        let x = l
            .expect(&stream[0].ch, &stream[0].commitment, &origin(&stream[0]))
            .unwrap();
        l.observe(&x, &stream[0].ack_bytes, &arrival(&stream[0]), 29);
        if mode == 0 {
            let mut a = arrival(&stream[1]);
            a.clock.clock_epoch_id = id("reset");
            let y = l
                .expect(&stream[1].ch, &stream[1].commitment, &origin(&stream[1]))
                .unwrap();
            l.observe(&y, &stream[1].ack_bytes, &a, 29);
        }
        if mode == 1 {
            l.observe(&x, &stream[0].ack_bytes, &arrival(&stream[0]), 28);
        }
        if mode == 2 {
            observe(&mut l, &stream[1], 29);
            observe(&mut l, &stream[2], 29);
            l.observe(&x, &stream[0].ack_bytes, &arrival(&stream[0]), 30);
        }
        assert!(l.diagnostic_coverage().break_reason().is_some());
        assert!(!l.diagnostic_coverage().complete());
    }
}
#[test]
fn p1_wrong_ack_and_canonical_mutations_never_supply_opening() {
    let w = World::new();
    let a = Session::new("p1-scope-a");
    let b = Session::new("p1-scope-b");
    let ea = exchange(&w, &a, Purpose::Opening, 0, None, 0, 9);
    let eb = exchange(&w, &b, Purpose::Opening, 0, None, 0, 9);
    let cases = vec![
        eb.ack_bytes,
        tamper(&ea.ack_bytes, |v| {
            v["unsigned_payload"]["service"] = "other".into()
        }),
        b"{}".to_vec(),
        b"{ \"x\":0}".to_vec(),
    ];
    for bytes in cases {
        let (mut l, _) = lineage(&w);
        let x = l.expect(&ea.ch, &ea.commitment, &origin(&ea)).unwrap();
        assert!(matches!(
            l.observe(&x, &bytes, &arrival(&ea), 9),
            AnchorState::Unknown { .. }
        ));
        assert!(l.opening().is_none());
    }
}
#[test]
fn p1_max_source_unknown_tail_does_not_wrap() {
    let w = World::new();
    let s = Session::new("p1-max");
    let e = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    let (mut l, _) = lineage(&w);
    observe(&mut l, &e, u64::MAX);
    assert!(!l.diagnostic_coverage().complete());
    assert!(l.diagnostic_coverage().unknown_rows() > 0);
}

#[test]
fn p1_unresolved_original_cannot_be_filtered_from_decision_check() {
    let w = World::new();
    let s = Session::new("p1-pending");
    let stream = full_stream(&w, &s);
    let (mut l, _) = lineage(&w);
    observe(&mut l, &stream[0], 9);
    let _pending = l
        .expect(&stream[1].ch, &stream[1].commitment, &origin(&stream[1]))
        .unwrap();
    let policy = l.note_policy(&hex64("policy")).unwrap();
    assert_eq!(
        l.check_at_decision_time(&policy).unwrap_err(),
        Reason::NoAck
    );
    assert_eq!(l.diagnostic_coverage().break_reason(), None);
}

#[test]
fn p1_no_response_at_original_deadline_latches_missing_history() {
    let w = World::new();
    let s = Session::new("p1-no-response");
    let e = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    let p = path();
    let mut l = DurableLineage::create(&p, w.policy.clone(), Some(0), Some(LOCAL_LIMIT)).unwrap();
    let x = l.expect(&e.ch, &e.commitment, &origin(&e)).unwrap();
    let policy = l.note_policy(&hex64("policy")).unwrap();
    assert_eq!(
        l.check_at_decision_time(&policy).unwrap_err(),
        Reason::NoAck
    );
    assert_eq!(l.diagnostic_coverage().break_reason(), Some(Reason::NoAck));
    l.observe(&x, &e.ack_bytes, &arrival(&e), 9);
    assert_eq!(l.diagnostic_coverage().break_reason(), Some(Reason::NoAck));
    assert_eq!(
        l.check_at_decision_time(&policy).unwrap_err(),
        Reason::NoAck
    );
    assert!(fs::read_to_string(p).unwrap().contains("missing_outcome"));
}
