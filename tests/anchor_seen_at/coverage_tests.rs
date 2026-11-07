//! Design section 9, coverage (and section 5): opening is not continuous coverage.

use crate::fixtures::*;
use qorcap::anchor_seen_at::coverage::{self, RangeStatus, Segment};
use qorcap::anchor_seen_at::types::Purpose;
use qorcap::anchor_seen_at::*;

fn states(w: &World, v: &[Exchange]) -> Vec<AnchorState> { v.iter().map(|e| e.state(&w.policy)).collect() }
fn unknown(first: u64, last: u64, r: Reason) -> Segment { Segment { first_seq: first, last_seq: last, status: RangeStatus::Unknown(r) } }

#[test]
fn complete_stream_with_close_is_fully_seen_at() {
    let w = World::new();
    let v = full_stream(&w, &Session::new("cov-full"));
    let view = coverage::reconstruct_archival(&states(&w, &v), 29);
    assert!(view.complete);
    assert_eq!(view.unknown_rows(), 0);
    assert_eq!(view.segments.len(), 3);
}

#[test]
fn opening_never_clears_later_intervals() {
    let w = World::new();
    let v = full_stream(&w, &Session::new("cov-open"));
    let view = coverage::reconstruct_archival(&states(&w, &v[..1]), 29);
    assert!(!view.complete);
    assert!(view.is_seen_at(9));
    assert_eq!(view.segments.last().unwrap(), &unknown(10, 29, Reason::MissingClose));
}

#[test]
fn missing_interval_stays_unknown_and_a_later_valid_close_does_not_clear_it() {
    let w = World::new();
    let v = full_stream(&w, &Session::new("cov-gap"));
    let view = coverage::reconstruct_archival(&states(&w, &[v[0].clone(), v[2].clone()]), 29);
    assert!(!view.complete);
    assert_eq!(view.segments.last().unwrap(), &unknown(10, 29, Reason::Gap));
    assert!(!view.is_seen_at(25), "the close's own range is not rescued past the gap");
}

#[test]
fn duplicate_ack_does_not_advance_coverage() {
    let w = World::new();
    let v = full_stream(&w, &Session::new("cov-dup"));
    let st = states(&w, &[v[0].clone(), v[0].clone(), v[0].clone()]);
    assert_eq!(coverage::reconstruct_archival(&st, 29), coverage::reconstruct_archival(&states(&w, &v[..1]), 29));
}

#[test]
fn archival_reconstruction_sorts_but_reversed_arrival_breaks_eligibility() {
    let w = World::new();
    let v = full_stream(&w, &Session::new("cov-order"));
    // Archival reconstruction is arrival-order independent by design: it only rebuilds
    // what the digests say, so forward and reversed sets reconstruct identically.
    let fwd = coverage::reconstruct_archival(&states(&w, &v), 29);
    let rev = coverage::reconstruct_archival(&states(&w, &[v[2].clone(), v[0].clone(), v[1].clone()]), 29);
    assert_eq!(fwd, rev);
    assert!(fwd.complete);
    // Eligibility is not: reversed arrival is a reorder, recorded permanently.
    let st = states(&w, &v);
    let in_order = coverage::StreamEligibility::from_arrival_order(&st, 29);
    assert!(in_order.complete());
    assert_eq!(in_order.break_reason(), None);
    let reversed = coverage::StreamEligibility::from_arrival_order(&[st[2].clone(), st[0].clone(), st[1].clone()], 29);
    assert_eq!(reversed.break_reason(), Some(Reason::Reordered));
    assert!(!reversed.complete());
    assert!(!reversed.is_eligible(0) && !reversed.is_eligible(15) && !reversed.is_eligible(25));
    assert_eq!(reversed.segments(), vec![unknown(0, 29, Reason::Reordered)]);
    // Even the later in-order facts in the same arrival sequence never cleared it.
    assert_eq!(reversed.unknown_rows(), 30);
}

#[test]
fn two_distinct_acks_at_one_index_are_a_fork() {
    let w = World::new();
    let v = full_stream(&w, &Session::new("cov-fork"));
    let mut st = states(&w, &v);
    let twin = checked_variant(&w, &v[1], |_, a| a.received_wall_us = "1".into());
    assert!(twin.retained().is_some());
    st.push(twin);
    let view = coverage::reconstruct_archival(&st, 29);
    assert!(!view.complete);
    assert_eq!(view.segments.last().unwrap(), &unknown(10, 29, Reason::Fork));
}

#[test]
fn reordered_or_relinked_interval_is_rejected() {
    let w = World::new();
    let v = full_stream(&w, &Session::new("cov-relink"));
    let base = states(&w, &v);
    let edits: Vec<(Reason, Box<dyn Fn(&mut qorcap::anchor_seen_at::types::AckUnsigned)>)> = vec![
        (Reason::Fork, Box::new(|a| a.commitment.prior_ack_id = Some(hex64("other-prior")))),
        (Reason::Fork, Box::new(|a| a.commitment.chain.predecessor_head = hex64("changed-predecessor"))),
        (Reason::LedgerRollback, Box::new(|a| a.ledger_seq = "0".into())),
        (Reason::BindingMismatch, Box::new(|a| a.commitment.capture_epoch_id = id("new-capture-epoch"))),
    ];
    for (want, edit) in edits {
        let mut st = base.clone();
        st[1] = checked_variant(&w, &v[1], |_, a| edit(a));
        assert!(st[1].retained().is_some());
        let view = coverage::reconstruct_archival(&st, 29);
        assert_eq!(view.segments.last().unwrap(), &unknown(10, 29, want));
        assert!(!view.complete);
    }
}

#[test]
fn late_interval_leaves_its_range_and_everything_after_unknown() {
    let w = World::new();
    let v = full_stream(&w, &Session::new("cov-late"));
    let mut st = states(&w, &v);
    st[1] = AnchorState::SeenAtLate(st[1].retained().unwrap().clone());
    let view = coverage::reconstruct_archival(&st, 29);
    assert_eq!(view.segments.last().unwrap(), &unknown(10, 29, Reason::LateAck));
}

#[test]
fn rows_after_close_stay_unknown_and_the_close_trailer_is_outside_its_prefix() {
    let w = World::new();
    let v = full_stream(&w, &Session::new("cov-after"));
    let view = coverage::reconstruct_archival(&states(&w, &v), 35);
    assert!(!view.complete);
    assert_eq!(view.segments.last().unwrap(), &unknown(30, 35, Reason::RowsAfterClose));
    // the close commitment covers rows up to 29 and does not reference its own ack
    let close = &v[2].commitment;
    assert_eq!(close.chain.last_seq, "29");
    assert!(!qorcap::evidence::jcs(&serde_json::to_value(close).unwrap()).unwrap().contains(&v[2].ack_id()));
}

#[test]
fn missing_close_leaves_an_incomplete_tail() {
    let w = World::new();
    let v = full_stream(&w, &Session::new("cov-noclose"));
    let view = coverage::reconstruct_archival(&states(&w, &v[..2]), 29);
    assert!(!view.complete);
    assert_eq!(view.segments.last().unwrap(), &unknown(20, 29, Reason::MissingClose));
}

#[test]
fn no_ack_at_all_is_unknown_over_the_whole_prefix() {
    let view = coverage::reconstruct_archival(&[AnchorState::default()], 9);
    assert_eq!(view.segments, vec![unknown(0, 9, Reason::NoAck)]);
    assert!(!view.complete);
}

#[test]
fn opening_that_starts_mid_chain_leaves_earlier_rows_unknown() {
    let w = World::new();
    let s = Session::new("cov-mid");
    let o = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    let mid = checked_variant(&w, &o, |_, a| {
        a.commitment.chain.predecessor_seq = Some("4".into());
        a.commitment.chain.predecessor_head = s.head(4);
        a.commitment.chain.first_seq = "5".into();
    });
    assert!(mid.retained().is_some());
    let view = coverage::reconstruct_archival(&[mid], 9);
    assert_eq!(view.segments[0], unknown(0, 4, Reason::Gap));
    assert!(view.is_seen_at(5));
}

#[test]
fn lost_ack_stays_unknown_and_recovery_is_forward_only() {
    // Loss: the interval ack is simply absent (an explicit unknown state is supplied too).
    let w = World::new();
    let v = full_stream(&w, &Session::new("cov-loss"));
    let mut st = states(&w, &[v[0].clone(), v[2].clone()]);
    st.insert(1, AnchorState::default());
    let view = coverage::reconstruct_archival(&st, 29);
    assert!(!view.complete);
    assert_eq!(view.segments.last().unwrap(), &unknown(10, 29, Reason::Gap));
    assert!(!view.is_seen_at(25), "a later close never recovers the lost range");
}

#[test]
fn loss_then_recovery_cannot_clear_earlier_unknown_rows() {
    let w = World::new();
    let v = full_stream(&w, &Session::new("cov-loss-recovery"));
    let st = states(&w, &v);
    // The interval ack is lost when expected: that loss is recorded as an eligibility
    // break before the interval and close ever arrive.
    let mut elig = coverage::StreamEligibility::new();
    elig.record(&st[0], 29);
    elig.record(&AnchorState::unknown(Reason::Gap), 29);
    assert_eq!(elig.break_reason(), Some(Reason::Gap));
    assert!(elig.is_eligible(9));
    let at_break = elig.segments();
    assert_eq!(at_break.last().unwrap(), &unknown(10, 29, Reason::Gap));
    // Recovery: the missing interval and the close now arrive, perfectly valid.
    elig.record(&st[1], 29);
    elig.record(&st[2], 29);
    // Archival reconstruction now shows a complete stream...
    assert!(coverage::reconstruct_archival(&st, 29).complete);
    // ...but eligibility is unchanged: the break and the unknown rows are preserved,
    // and nothing after the loss became eligible retroactively.
    assert_eq!(elig.break_reason(), Some(Reason::Gap));
    assert_eq!(elig.segments(), at_break);
    assert!(!elig.complete());
    assert!(elig.is_eligible(9));
    assert!(!elig.is_eligible(15) && !elig.is_eligible(25), "recovered acks do not clear the lost range");
}

#[test]
fn deadline_miss_then_valid_close_cannot_clear_the_break() {
    let w = World::new();
    let v = full_stream(&w, &Session::new("cov-late-recovery"));
    let st = states(&w, &v);
    let mut elig = coverage::StreamEligibility::new();
    elig.record(&st[0], 29);
    elig.record(&AnchorState::SeenAtLate(st[1].retained().unwrap().clone()), 29);
    assert_eq!(elig.break_reason(), Some(Reason::LateAck));
    elig.record(&st[2], 29);
    assert_eq!(elig.break_reason(), Some(Reason::LateAck));
    assert!(!elig.complete());
    assert_eq!(elig.segments().last().unwrap(), &unknown(10, 29, Reason::LateAck));
    assert!(!elig.is_eligible(25));
}

#[test]
fn source_bound_regression_is_rejected_before_dedup_and_latches_a_break() {
    let w = World::new();
    let v = full_stream(&w, &Session::new("cov-bound"));
    let st = states(&w, &v);
    let mut elig = coverage::StreamEligibility::from_arrival_order(&st, 29);
    assert!(elig.complete());
    assert_eq!(elig.break_reason(), None);
    // The exact trace: a duplicate of an already-accepted ack arrives, this time
    // carrying a lowered source bound. Dedup must not swallow it first.
    elig.record(&st[0], 9);
    assert_eq!(elig.break_reason(), Some(Reason::SourceBoundRegression));
    assert!(!elig.complete());
    // The break is sticky, the higher bound is kept, and a later well-formed
    // observation at the correct bound does not clear it.
    elig.record(&st[2], 29);
    assert_eq!(elig.break_reason(), Some(Reason::SourceBoundRegression));
    assert!(!elig.complete());
    assert_eq!(elig.unknown_rows(), 0, "the recorded bound was not lowered to 9");
    assert!(elig.segments().iter().all(|s| s.last_seq <= 29));
}

#[test]
fn cross_exchange_client_clock_change_breaks_the_stream() {
    use qorcap::anchor_seen_at::check::ClockRef;
    let w = World::new();
    let s = Session::new("cov-clock");
    let opening = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    // The same session, but the interval is checked under a different client clock.
    let mut s_b = s.clone();
    s_b.client_clock = ClockRef { boot_id: id("clock-b-boot"), clock_epoch_id: id("clock-b-epoch") };
    let interval = exchange(&w, &s_b, Purpose::Interval, 1, Some(&opening.ack_id()), 10, 19);
    let st_o = opening.state(&w.policy);
    let st_i = interval.state(&w.policy);
    assert!(st_o.retained().is_some() && st_i.retained().is_some(), "each exchange is retained under its own clock");
    let mut elig = coverage::StreamEligibility::new();
    elig.record(&st_o, 29);
    elig.record(&st_i, 29);
    assert_eq!(elig.break_reason(), Some(Reason::ClientClockDomainChanged));
    assert!(!elig.complete());
    assert!(elig.is_eligible(9));
    assert!(!elig.is_eligible(15));
    assert_eq!(elig.segments().last().unwrap(), &unknown(10, 29, Reason::ClientClockDomainChanged));
    // A later close checked under the original clock does not clear the break.
    let close = exchange(&w, &s, Purpose::Close, 2, Some(&interval.ack_id()), 20, 29);
    elig.record(&close.state(&w.policy), 29);
    assert_eq!(elig.break_reason(), Some(Reason::ClientClockDomainChanged));
    assert!(!elig.is_eligible(25));
}

/// The same exchange rechecked through the real checker, but with both local clocks
/// moved to a different client clock identity. The ack bytes and commitment are
/// untouched, so the resulting fact carries the same ack id under a new clock.
fn rechecked_under_clock(e: &Exchange, boot: &str, epoch: &str) -> Exchange {
    use qorcap::anchor_seen_at::check::ClockRef;
    let mut e = e.clone();
    e.samples.send_clock = ClockRef { boot_id: id(boot), clock_epoch_id: id(epoch) };
    e.samples.receive_clock = e.samples.send_clock.clone();
    e
}

#[test]
fn changed_clock_duplicate_latches_a_break_before_dedup() {
    let w = World::new();
    let v = full_stream(&w, &Session::new("cov-dup-clock"));
    let st = states(&w, &v);
    // Boot-only and epoch-only changes are both a different clock context.
    for (boot, epoch) in [("dup-other-boot", "cov-dup-clock-hostclock"), ("cov-dup-clock-hostboot", "dup-other-epoch")] {
        let mut elig = coverage::StreamEligibility::from_arrival_order(&st, 29);
        assert!(elig.complete());
        let dup = rechecked_under_clock(&v[1], boot, epoch).state(&w.policy);
        let f = dup.retained().expect("rechecked under its own clock, the ack is retained");
        assert_eq!(f.ack_id(), st[1].retained().unwrap().ack_id(), "same ack id as the accepted fact");
        elig.record(&dup, 29);
        assert_eq!(elig.break_reason(), Some(Reason::ClientClockDomainChanged), "boot={boot} epoch={epoch}");
        assert!(!elig.complete());
        // Sticky: an identical-context duplicate afterwards does not clear it.
        elig.record(&st[2], 29);
        assert_eq!(elig.break_reason(), Some(Reason::ClientClockDomainChanged));
    }
}

#[test]
fn identical_context_duplicate_is_a_harmless_retry() {
    let w = World::new();
    let v = full_stream(&w, &Session::new("cov-dup-same"));
    let st = states(&w, &v);
    let mut elig = coverage::StreamEligibility::from_arrival_order(&st, 29);
    assert!(elig.complete());
    let before = elig.segments();
    // Rechecking the very same exchanges and re-recording them changes nothing.
    for e in &v { elig.record(&e.state(&w.policy), 29); }
    for s in &st { elig.record(s, 29); }
    assert_eq!(elig.break_reason(), None);
    assert!(elig.complete());
    assert_eq!(elig.segments(), before);
    assert_eq!(elig.unknown_rows(), 0);
}

#[test]
fn post_close_duplicate_with_extended_bound_latches_rows_after_close() {
    let w = World::new();
    let v = full_stream(&w, &Session::new("cov-after-dup"));
    let st = states(&w, &v);
    let mut elig = coverage::StreamEligibility::from_arrival_order(&st, 29);
    assert!(elig.complete());
    // A duplicate of the sealed close, now carrying source rows past the seal.
    elig.record(&st[2], 35);
    assert_eq!(elig.break_reason(), Some(Reason::RowsAfterClose));
    assert!(!elig.complete());
    assert_eq!(elig.segments().last().unwrap(), &unknown(30, 35, Reason::RowsAfterClose));
    assert_eq!(elig.unknown_rows(), 6);
    elig.record(&st[0], 35);
    assert_eq!(elig.break_reason(), Some(Reason::RowsAfterClose));
}

#[test]
fn range_ending_at_u64_max_is_rejected_and_fails_closed_without_overflow() {
    let w = World::new();
    let s = Session::new("cov-max");
    // Validation, through the real service: an opening range ending at u64::MAX is
    // unrepresentable (no successor row, no representable row count) and is refused.
    let ch = challenge(&w, &s, Purpose::Opening);
    let c = s.commitment(&ch, 0, None, 0, u64::MAX);
    assert_eq!(c.chain.validate(), Err(Reason::FieldFormat));
    assert_eq!(c.validate(), Err(Reason::FieldFormat));
    assert!(w.svc.post_commitments(&commit_body(&ch, &c)).is_err());
    // Even signed synthetic material cannot mint an out-of-range checked fact.
    // The internal malformed-token arithmetic regression is a cfg(test) unit test.
    let v = full_stream(&w, &Session::new("cov-max-fact"));
    let st = checked_variant(&w, &v[0], |_, a| a.commitment.chain.last_seq = u64::MAX.to_string());
    assert_eq!(unknown_reasons(&st), [Reason::FieldFormat]);
    let elig = coverage::StreamEligibility::from_arrival_order(&[st], u64::MAX);
    assert!(!elig.complete());
    assert!(!elig.is_eligible(0));
    assert_eq!(elig.unknown_rows(), u64::MAX);
}

#[test]
fn reordered_chain_range_is_a_fork_and_restart_invalidates_the_stream() {
    let w = World::new();
    let v = full_stream(&w, &Session::new("cov-reorder-restart"));
    let base = states(&w, &v);
    // Reorder: the interval claims a range that does not start where the opening ended.
    let mut st = base.clone();
    st[1] = checked_variant(&w, &v[1], |_, a| {
        a.commitment.chain.first_seq = "11".into();
        a.commitment.chain.predecessor_seq = Some("10".into());
    });
    assert!(st[1].retained().is_some());
    let view = coverage::reconstruct_archival(&st, 29);
    assert_eq!(view.segments.last().unwrap(), &unknown(10, 29, Reason::Fork));
    assert!(!view.complete);
    // Restart: a new service boot/clock epoch mid-stream breaks the link, and the
    // otherwise-valid close after it does not clear the break.
    let mut st = base.clone();
    st[1] = checked_variant(&w, &v[1], |_, a| {
        a.server_boot_id = id("restarted-boot");
        a.server_clock_epoch_id = id("restarted-clock");
    });
    assert!(st[1].retained().is_some());
    let view = coverage::reconstruct_archival(&st, 29);
    assert_eq!(view.segments.last().unwrap(), &unknown(10, 29, Reason::ServerEpochMismatch));
    assert!(!view.complete);
    assert!(!view.is_seen_at(25));
}

#[test]
fn ledger_entries_recompute_and_link_the_stream() {
    use qorcap::anchor_seen_at::canon::{self, ZERO_HEAD};
    use qorcap::anchor_seen_at::DOMAIN_LEDGER_ENTRY;
    use serde_json::json;
    let w = World::new();
    let v = full_stream(&w, &Session::new("cov-ledger"));
    assert_eq!(w.svc.ledger_len(), 3);
    // Independently recompute each entry from the ack facts and walk the chain.
    let mut prev_head = ZERO_HEAD.to_string();
    for ex in &v {
        let a = ex.ack().unsigned_payload;
        assert_eq!(a.prev_ledger_entry_digest, prev_head, "each ack names the recomputed prior entry");
        let entry = json!({"ledger_seq": a.ledger_seq, "ack_id": ex.ack_id(), "commitment_digest": a.commitment_digest, "prev": a.prev_ledger_entry_digest});
        prev_head = canon::digest(DOMAIN_LEDGER_ENTRY, &entry).unwrap();
        // The coverage helper must recompute the same digest from the checked fact.
        let fact = ex.state(&w.policy).retained().unwrap().clone();
        assert_eq!(coverage::ledger_entry_digest(&fact).as_deref(), Some(prev_head.as_str()));
    }
    assert_eq!(prev_head, w.svc.ledger_head(), "recomputed head matches the service ledger");
    // A fact whose ledger linkage does not recompute breaks coverage from that point.
    let mut st = states(&w, &v);
    st[2] = checked_variant(&w, &v[2], |_, a| a.prev_ledger_entry_digest = hex64("not-the-ledger-entry"));
    assert!(st[2].retained().is_some());
    let view = coverage::reconstruct_archival(&st, 29);
    assert!(!view.complete);
    assert_eq!(view.segments.last().unwrap(), &unknown(20, 29, Reason::LedgerRollback));
}

#[test]
fn count_only_continuity_cannot_be_declared() {
    let w = World::new();
    let s = Session::new("cov-count");
    let o = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    let ch = challenge(&w, &s, Purpose::Interval);
    let c = s.commitment(&ch, 1, Some(&o.ack_id()), 10, 19);
    let body = tamper(&commit_body(&ch, &c), |v| v["commitment"]["row_count"] = serde_json::json!("10"));
    assert!(w.svc.post_commitments(&body).is_err());
    let body = tamper(&commit_body(&ch, &c), |v| { v["commitment"]["capture_ranges"] = serde_json::json!([]); });
    assert!(w.svc.post_commitments(&body).is_err());
}
