//! Design section 9, readiness/snapshots; QorProof Track 1 control and cross-track stale-snapshot items.

use crate::fixtures::*;
use qorcap::anchor_seen_at::coverage::{self, StreamEligibility};
use qorcap::anchor_seen_at::envelope;
use qorcap::anchor_seen_at::readiness::{evaluate, CaptureValidity, FrameObservation, Readiness, ReadinessInputs};
use qorcap::anchor_seen_at::types::Purpose;
use qorcap::anchor_seen_at::*;

fn frame(s: &Session, seq: u64, polled: u64) -> FrameObservation {
    FrameObservation { frame_digest: hex64(&format!("{}-frame-{seq}", s.label)), capture_epoch_id: s.capture_epoch_id.clone(), acquisition_seq: seq, polled_at_mono_ns: polled }
}

struct Ready { w: World, s: Session, opening: AnchorState, view: StreamEligibility }

fn ready() -> Ready {
    let w = World::new();
    let s = Session::new("ready");
    let v = full_stream(&w, &s);
    let st: Vec<AnchorState> = v.iter().map(|e| e.state(&w.policy)).collect();
    let view = StreamEligibility::from_arrival_order(&st, 29);
    Ready { opening: st[0].clone(), w, s, view }
}

fn inputs<'a>(r: &'a Ready, frames: &'a [FrameObservation]) -> ReadinessInputs<'a> {
    ReadinessInputs {
        opening: &r.opening, eligibility: &r.view, capture_validity: CaptureValidity::SeparatelyEstablished, frames,
        min_distinct_frames: Some(3), exclusion_end_seq: Some(4), hook_lag_measured: true,
    }
}

fn withheld(x: Readiness) -> Vec<Reason> { match x { Readiness::Withheld { reasons } => reasons, Readiness::PracticeReadyForward => panic!("expected withheld") } }

#[test]
fn complete_synthetic_positive_control_is_practice_ready_forward() {
    let r = ready();
    let frames = [frame(&r.s, 12, 1), frame(&r.s, 18, 2), frame(&r.s, 25, 3)];
    assert_eq!(evaluate(&inputs(&r, &frames)), Readiness::PracticeReadyForward);
}

#[test]
fn no_real_ack_means_unknown_and_withheld_never_a_pass() {
    let r = ready();
    let none = AnchorState::default();
    assert_eq!(unknown_reasons(&none), [Reason::NoAck]);
    let empty = StreamEligibility::from_arrival_order(&[], 29);
    let frames = [frame(&r.s, 12, 1), frame(&r.s, 18, 2), frame(&r.s, 25, 3)];
    let mut i = inputs(&r, &frames);
    i.opening = &none;
    i.eligibility = &empty;
    let why = withheld(evaluate(&i));
    assert!(why.contains(&Reason::AnchorNotRetained) && why.contains(&Reason::StaleFrame));
    let retained_opening = exchange(&r.w, &Session::new("ready-missing"), Purpose::Opening, 0, None, 0, 9);
    let missing = envelope::check_envelope(None, &retained_opening.expected(), &r.w.policy);
    assert_eq!(missing.state, AnchorState::default());
}

#[test]
fn fabricated_host_commitment_that_gets_acked_is_not_validated_capture() {
    // The host never captured these echo bytes or rows; it simply commits made-up digests.
    let w = World::new();
    let fake = Session::with_echo("fabricated", "deadbeefdeadbeef");
    let v = full_stream(&w, &fake);
    let st: Vec<AnchorState> = v.iter().map(|e| e.state(&w.policy)).collect();
    let f = st[0].retained().expect("the service legitimately saw the fabricated commitment");
    assert_eq!(f.meaning(), ACK_MEANING);
    assert_eq!(f.capture_validity(), CaptureValidity::NotEstablishedByAck);
    let env = envelope::check_envelope(Some(&v[0].envelope()), &v[0].expected(), &w.policy);
    assert_eq!(env.verification.status, "seen_at_retained");
    let view = StreamEligibility::from_arrival_order(&st, 29);
    assert!(view.complete(), "every fabricated row was seen-at");
    let r = Ready { opening: st[0].clone(), w, s: fake.clone(), view };
    let frames = [frame(&fake, 12, 1), frame(&fake, 18, 2), frame(&fake, 25, 3)];
    let mut i = inputs(&r, &frames);
    i.capture_validity = f.capture_validity();
    assert_eq!(withheld(evaluate(&i)), [Reason::CaptureValidityNotEstablished]);
}

#[test]
fn stale_snapshot_with_a_fresh_valid_ack_stays_withheld() {
    let r = ready();
    let fresh_ack = r.opening.retained().unwrap();
    assert!(r.view.complete() && fresh_ack.received_mono_ns > 0);
    // frame acquired before the exclusion endpoint, polled long after
    let frames = [frame(&r.s, 3, 9_000_000), frame(&r.s, 18, 2), frame(&r.s, 25, 3)];
    assert_eq!(withheld(evaluate(&inputs(&r, &frames))), [Reason::StaleFrame]);
    // frame from beyond the seen-at prefix (no anchored acquisition record), however recently polled
    let frames = [frame(&r.s, 12, 1), frame(&r.s, 18, 2), frame(&r.s, 40, 9_000_000)];
    assert_eq!(withheld(evaluate(&inputs(&r, &frames))), [Reason::StaleFrame]);
}

#[test]
fn frame_left_behind_by_a_reopen_cannot_take_a_new_identity() {
    let r = ready();
    let mut old = frame(&r.s, 12, 50_000_000);
    old.capture_epoch_id = id("capture-epoch-before-reopen");
    let frames = [old, frame(&r.s, 18, 2), frame(&r.s, 25, 3)];
    assert_eq!(withheld(evaluate(&inputs(&r, &frames))), [Reason::StaleFrame]);
}

#[test]
fn relabeled_copies_cannot_satisfy_distinct_frame_consensus() {
    let r = ready();
    let base = frame(&r.s, 12, 1);
    let frames: Vec<_> = (0..5).map(|k| FrameObservation { acquisition_seq: 12 + k, polled_at_mono_ns: k, ..base.clone() }).collect();
    let why = withheld(evaluate(&inputs(&r, &frames)));
    assert!(why.contains(&Reason::DuplicateFrame) && why.contains(&Reason::TooFewDistinctFrames));
}

#[test]
fn repeated_polling_does_not_advance_consensus() {
    let r = ready();
    let frames: Vec<_> = (0..10).map(|k| frame(&r.s, 12, 1_000 * k)).collect();
    assert_eq!(withheld(evaluate(&inputs(&r, &frames))), [Reason::TooFewDistinctFrames]);
}

#[test]
fn every_missing_gate_withholds() {
    let r = ready();
    let frames = [frame(&r.s, 12, 1), frame(&r.s, 18, 2), frame(&r.s, 25, 3)];
    let cases: Vec<(Reason, Box<dyn Fn(&mut ReadinessInputs)>)> = vec![
        (Reason::CaptureValidityNotEstablished, Box::new(|i| i.capture_validity = CaptureValidity::Unknown)),
        (Reason::ExclusionUnresolved, Box::new(|i| i.exclusion_end_seq = None)),
        (Reason::LagUnmeasured, Box::new(|i| i.hook_lag_measured = false)),
        (Reason::FrameBudgetUnset, Box::new(|i| i.min_distinct_frames = None)),
    ];
    for (want, f) in cases {
        let mut i = inputs(&r, &frames);
        f(&mut i);
        assert!(withheld(evaluate(&i)).contains(&want), "{want:?}");
    }
}

#[test]
fn late_unretained_or_non_opening_anchor_withholds() {
    let r = ready();
    let frames = [frame(&r.s, 12, 1), frame(&r.s, 18, 2), frame(&r.s, 25, 3)];
    let late = AnchorState::SeenAtLate(r.opening.retained().unwrap().clone());
    let mut i = inputs(&r, &frames);
    i.opening = &late;
    assert!(withheld(evaluate(&i)).contains(&Reason::AnchorNotRetained));
    let interval = exchange(&r.w, &Session::new("ready-interval-src"), Purpose::Opening, 0, None, 0, 1);
    let mut f = interval.state(&r.w.policy).retained().unwrap().clone();
    f.commitment.purpose = Purpose::Interval;
    let not_opening = AnchorState::SeenAtRetained(f);
    let mut i = inputs(&r, &frames);
    i.opening = &not_opening;
    assert!(withheld(evaluate(&i)).contains(&Reason::AnchorNotRetained));
}

#[test]
fn opening_from_one_stream_paired_with_coverage_from_another_withholds() {
    let w = World::new();
    let a = Session::new("ready-a");
    let b = Session::new("ready-b");
    let va = full_stream(&w, &a);
    let vb = full_stream(&w, &b);
    let st_a: Vec<AnchorState> = va.iter().map(|e| e.state(&w.policy)).collect();
    let st_b: Vec<AnchorState> = vb.iter().map(|e| e.state(&w.policy)).collect();
    let r = Ready { opening: st_a[0].clone(), view: StreamEligibility::from_arrival_order(&st_b, 29), w, s: a.clone() };
    assert!(r.view.complete(), "stream B is complete on its own");
    let frames = [frame(&a, 12, 1), frame(&a, 18, 2), frame(&a, 25, 3)];
    let why = withheld(evaluate(&inputs(&r, &frames)));
    assert!(why.contains(&Reason::BindingMismatch), "{why:?}");
    // The same opening with its own coverage is the positive control.
    let r2 = Ready { opening: st_a[0].clone(), view: StreamEligibility::from_arrival_order(&st_a, 29), w: r.w, s: a };
    assert_eq!(evaluate(&inputs(&r2, &frames)), Readiness::PracticeReadyForward);
}

#[test]
fn rows_in_an_unknown_range_withhold_even_when_later_rows_are_seen_at() {
    let w = World::new();
    let s = Session::new("ready-gap");
    let v = full_stream(&w, &s);
    let st: Vec<AnchorState> = [&v[0], &v[2]].iter().map(|e| e.state(&w.policy)).collect();
    let r = Ready { opening: st[0].clone(), view: StreamEligibility::from_arrival_order(&st, 29), w, s: s.clone() };
    let frames = [frame(&s, 5, 1), frame(&s, 6, 2), frame(&s, 15, 3)];
    assert_eq!(withheld(evaluate(&inputs(&r, &frames))), [Reason::Gap, Reason::StaleFrame]);
}

#[test]
fn source_bound_regression_after_a_complete_stream_stays_withheld() {
    let mut r = ready();
    let frames = [frame(&r.s, 12, 1), frame(&r.s, 18, 2), frame(&r.s, 25, 3)];
    assert_eq!(evaluate(&inputs(&r, &frames)), Readiness::PracticeReadyForward);
    assert!(r.view.is_eligible(12) && r.view.is_eligible(18) && r.view.is_eligible(25));
    // A duplicate of the accepted opening arrives with the source bound lowered
    // from 29 to 9. The rows remain archivally present, but readiness must fall.
    r.view.record(&r.opening.clone(), 9);
    assert_eq!(r.view.break_reason(), Some(Reason::SourceBoundRegression));
    assert!(!r.view.complete());
    let why = withheld(evaluate(&inputs(&r, &frames)));
    assert!(why.contains(&Reason::SourceBoundRegression), "{why:?}");
}

#[test]
fn cross_exchange_client_clock_change_stays_withheld_in_readiness() {
    use qorcap::anchor_seen_at::check::ClockRef;
    let w = World::new();
    let s = Session::new("ready-clock");
    let opening = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    let mut s_b = s.clone();
    s_b.client_clock = ClockRef { boot_id: id("ready-clock-b-boot"), clock_epoch_id: id("ready-clock-b-epoch") };
    let interval = exchange(&w, &s_b, Purpose::Interval, 1, Some(&opening.ack_id()), 10, 19);
    let close = exchange(&w, &s, Purpose::Close, 2, Some(&interval.ack_id()), 20, 29);
    let st_o = opening.state(&w.policy);
    let st_i = interval.state(&w.policy);
    let st_c = close.state(&w.policy);
    assert!(st_o.retained().is_some() && st_i.retained().is_some() && st_c.retained().is_some());
    let mut elig = StreamEligibility::new();
    for st in [&st_o, &st_i, &st_c] { elig.record(st, 29); }
    assert_eq!(elig.break_reason(), Some(Reason::ClientClockDomainChanged));
    let r = Ready { opening: st_o, view: elig, w, s: s.clone() };
    let frames = [frame(&s, 12, 1), frame(&s, 18, 2), frame(&s, 25, 3)];
    let why = withheld(evaluate(&inputs(&r, &frames)));
    assert!(why.contains(&Reason::ClientClockDomainChanged), "{why:?}");
}

#[test]
fn changed_clock_duplicate_stays_withheld_and_identical_duplicate_is_harmless() {
    use qorcap::anchor_seen_at::check::ClockRef;
    let w = World::new();
    let s = Session::new("ready-dup-clock");
    let v = full_stream(&w, &s);
    let st: Vec<AnchorState> = v.iter().map(|e| e.state(&w.policy)).collect();
    let elig = StreamEligibility::from_arrival_order(&st, 29);
    let mut r = Ready { opening: st[0].clone(), view: elig, w, s: s.clone() };
    let frames = [frame(&s, 12, 1), frame(&s, 18, 2), frame(&s, 25, 3)];
    assert_eq!(evaluate(&inputs(&r, &frames)), Readiness::PracticeReadyForward);
    // An identical-context duplicate of the interval is a harmless retry.
    r.view.record(&st[1], 29);
    assert_eq!(r.view.break_reason(), None);
    assert_eq!(evaluate(&inputs(&r, &frames)), Readiness::PracticeReadyForward);
    // The same ack rechecked under a different client boot is not that retry: the
    // duplicate latches a clock break before dedup and readiness falls.
    let mut dup_ex = v[1].clone();
    dup_ex.samples.send_clock = ClockRef { boot_id: id("ready-dup-other-boot"), clock_epoch_id: s.client_clock.clock_epoch_id.clone() };
    dup_ex.samples.receive_clock = dup_ex.samples.send_clock.clone();
    let dup = dup_ex.state(&r.w.policy);
    assert!(dup.retained().is_some());
    r.view.record(&dup, 29);
    assert_eq!(r.view.break_reason(), Some(Reason::ClientClockDomainChanged));
    let why = withheld(evaluate(&inputs(&r, &frames)));
    assert!(why.contains(&Reason::ClientClockDomainChanged), "{why:?}");
}

#[test]
fn post_close_duplicate_extension_stays_withheld_in_readiness() {
    let mut r = ready();
    let frames = [frame(&r.s, 12, 1), frame(&r.s, 18, 2), frame(&r.s, 25, 3)];
    assert_eq!(evaluate(&inputs(&r, &frames)), Readiness::PracticeReadyForward);
    // A duplicate of the accepted opening, after the close sealed row 29, carrying a
    // source bound extended to 35.
    r.view.record(&r.opening.clone(), 35);
    assert_eq!(r.view.break_reason(), Some(Reason::RowsAfterClose));
    assert!(!r.view.complete());
    let why = withheld(evaluate(&inputs(&r, &frames)));
    assert!(why.contains(&Reason::RowsAfterClose), "{why:?}");
}

#[test]
fn substituted_opening_presented_directly_to_evaluate_withholds() {
    use qorcap::anchor_seen_at::check::ClockRef;
    let w = World::new();
    let s = Session::new("ready-sub-opening");
    let v = full_stream(&w, &s);
    let st: Vec<AnchorState> = v.iter().map(|e| e.state(&w.policy)).collect();
    let r = Ready { opening: st[0].clone(), view: StreamEligibility::from_arrival_order(&st, 29), w, s: s.clone() };
    let frames = [frame(&s, 12, 1), frame(&s, 18, 2), frame(&s, 25, 3)];
    // The valid original opening stays ready. None of the substitutes below is ever
    // passed through `record`; each goes straight to `evaluate`.
    assert_eq!(evaluate(&inputs(&r, &frames)), Readiness::PracticeReadyForward);

    // (a) The opening rechecked under a different client clock: same ack, same
    // commitment, same stream context — but not the accepted opening.
    let mut clocked = v[0].clone();
    clocked.samples.send_clock = ClockRef { boot_id: id("sub-other-boot"), clock_epoch_id: id("sub-other-epoch") };
    clocked.samples.receive_clock = clocked.samples.send_clock.clone();
    let clocked = clocked.state(&r.w.policy);
    assert!(clocked.retained().is_some());
    let mut i = inputs(&r, &frames);
    i.opening = &clocked;
    let why = withheld(evaluate(&i));
    assert!(why.contains(&Reason::ClientClockDomainChanged), "{why:?}");

    // (b) A different ack id over the accepted opening's context.
    let mut relabeled = st[0].retained().unwrap().clone();
    relabeled.ack_id = hex64("substituted-opening-ack");
    let relabeled = AnchorState::SeenAtRetained(relabeled);
    let mut i = inputs(&r, &frames);
    i.opening = &relabeled;
    let why = withheld(evaluate(&i));
    assert!(why.contains(&Reason::BindingMismatch), "{why:?}");

    // (c) A changed commitment (here the bound client nonce) under the accepted ack id.
    let mut rebound = st[0].retained().unwrap().clone();
    rebound.commitment.client_nonce = hex64("substituted-nonce");
    let rebound = AnchorState::SeenAtRetained(rebound);
    let mut i = inputs(&r, &frames);
    i.opening = &rebound;
    let why = withheld(evaluate(&i));
    assert!(why.contains(&Reason::BindingMismatch), "{why:?}");

    // The eligibility state was never touched by the substitutes, and the original
    // opening is still ready afterwards.
    assert_eq!(evaluate(&inputs(&r, &frames)), Readiness::PracticeReadyForward);
}

#[test]
fn loss_then_recovery_and_reversed_arrival_stay_withheld_in_readiness() {
    let w = World::new();
    let s = Session::new("ready-forward-only");
    let v = full_stream(&w, &s);
    let st: Vec<AnchorState> = v.iter().map(|e| e.state(&w.policy)).collect();
    let frames = [frame(&s, 12, 1), frame(&s, 18, 2), frame(&s, 25, 3)];
    // Loss then recovery: the interval is lost when expected, then both missing acks
    // arrive valid. Archival reconstruction is complete, readiness is not.
    let mut elig = StreamEligibility::new();
    elig.record(&st[0], 29);
    elig.record(&AnchorState::unknown(Reason::Gap), 29);
    elig.record(&st[1], 29);
    elig.record(&st[2], 29);
    assert!(coverage::reconstruct_archival(&st, 29).complete);
    let r = Ready { opening: st[0].clone(), view: elig, w, s: s.clone() };
    let why = withheld(evaluate(&inputs(&r, &frames)));
    assert!(why.contains(&Reason::Gap) && why.contains(&Reason::StaleFrame), "{why:?}");
    // Out-of-order arrival: the same facts fed reversed break eligibility at once,
    // even though the identical set reconstructs archivally as complete.
    let rev = StreamEligibility::from_arrival_order(&[st[2].clone(), st[0].clone(), st[1].clone()], 29);
    assert_eq!(rev.break_reason(), Some(Reason::Reordered));
    let r = Ready { opening: st[0].clone(), view: rev, w: r.w, s };
    let why = withheld(evaluate(&inputs(&r, &frames)));
    assert!(why.contains(&Reason::Reordered), "{why:?}");
}
