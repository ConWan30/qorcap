//! Design section 9, durability and clocks/availability (and section 8 budgets).

use crate::fixtures::*;
use qorcap::anchor_seen_at::check::{self, ClockRef};
use qorcap::anchor_seen_at::local_store::{Fault, LocalStoreDouble, Persist};
use qorcap::anchor_seen_at::types::Purpose;
use qorcap::anchor_seen_at::*;

fn opening(label: &str) -> (World, Exchange) {
    let w = World::new();
    let ex = exchange(&w, &Session::new(label), Purpose::Opening, 0, None, 0, 9);
    (w, ex)
}

#[test]
fn write_flush_and_sync_failures_give_no_local_success_or_consumed_binding() {
    let (w, ex) = opening("dur");
    let binding = ex.commitment.challenge_id.clone();
    let mut store = LocalStoreDouble::default();
    for f in [Fault::Write, Fault::Flush, Fault::Sync] {
        store.inject(f);
        assert_eq!(store.persist(&ex.envelope(), &binding), Persist::Failed(Reason::PersistFailed), "{f:?}");
        assert!(!store.is_consumed(&binding));
        assert!(store.records().is_empty());
    }
    assert_eq!(store.persist(&ex.envelope(), &binding), Persist::Durable);
    assert!(store.is_consumed(&binding));
    assert_eq!(store.persist(&ex.envelope(), &binding), Persist::AlreadyDurable);
    assert_eq!(store.records().len(), 1, "no duplicate acknowledgment");
    let loaded = qorcap::anchor_seen_at::envelope::check_envelope(Some(&store.records()[0]), &ex.expected(), &w.policy);
    assert!(loaded.state.retained().is_some());
}

#[test]
fn torn_checkpoint_loads_as_unknown() {
    let (w, ex) = opening("torn");
    let mut store = LocalStoreDouble::default();
    store.inject(Fault::TornWrite);
    assert_eq!(store.persist(&ex.envelope(), "binding"), Persist::Failed(Reason::PersistFailed));
    assert!(!store.is_consumed("binding"));
    let loaded = qorcap::anchor_seen_at::envelope::check_envelope(Some(&store.records()[0]), &ex.expected(), &w.policy);
    assert_eq!(unknown_reasons(&loaded.state), [Reason::Malformed]);
    let torn_ack = &ex.ack_bytes[..ex.ack_bytes.len() / 2];
    assert!(matches!(check::check_ack(&ex.ch, torn_ack, &ex.commitment, &ex.samples, &w.policy), AnchorState::Unknown { .. }));
}

#[test]
fn negative_rtt_and_broken_local_ordering_are_unknown() {
    let (w, mut ex) = opening("rtt");
    ex.samples.receive_mono_ns = "2999".into();
    assert_eq!(unknown_reasons(&ex.state(&w.policy)), [Reason::NegativeRtt]);
    let (w, mut ex) = opening("order");
    ex.samples.echo_capture_mono_ns = "500".into();
    assert_eq!(unknown_reasons(&ex.state(&w.policy)), [Reason::LocalOrdering], "echo captured before the challenge arrived");
    ex.samples.echo_capture_mono_ns = "3500".into();
    assert_eq!(unknown_reasons(&ex.state(&w.policy)), [Reason::LocalOrdering], "echo after send");
}

#[test]
fn client_clock_domain_change_or_reset_is_unknown() {
    let (w, mut ex) = opening("qpc");
    ex.samples.receive_clock = ClockRef { boot_id: ex.samples.send_clock.boot_id.clone(), clock_epoch_id: id("qpc-reset") };
    assert_eq!(unknown_reasons(&ex.state(&w.policy)), [Reason::ClientClockDomainChanged]);
    ex.samples.receive_clock = ClockRef { boot_id: id("host-reboot"), clock_epoch_id: ex.samples.send_clock.clock_epoch_id.clone() };
    assert_eq!(unknown_reasons(&ex.state(&w.policy)), [Reason::ClientClockDomainChanged]);
}

#[test]
fn over_budget_is_late_evidence_not_usable_for_readiness() {
    let (w, mut ex) = opening("late");
    ex.samples.receive_mono_ns = (3000 + SYNTH_RTT_BUDGET_NS + 1).to_string();
    let st = ex.state(&w.policy);
    assert!(matches!(st, AnchorState::SeenAtLate(_)));
    assert!(st.retained().is_none());
    let (w, mut ex) = opening("late-challenge");
    ex.samples.echo_capture_mono_ns = (1000 + SYNTH_CHALLENGE_AGE_NS + 1).to_string();
    ex.samples.send_mono_ns = ex.samples.echo_capture_mono_ns.clone();
    ex.samples.receive_mono_ns = ex.samples.echo_capture_mono_ns.clone();
    assert!(matches!(ex.state(&w.policy), AnchorState::SeenAtLate(_)));
}

#[test]
fn unset_budget_is_unknown_not_assumed() {
    let (mut w, ex) = opening("nobudget");
    w.policy.max_rtt_ns = None;
    assert_eq!(unknown_reasons(&ex.state(&w.policy)), [Reason::TimingBudgetUnset]);
    w.policy.max_rtt_ns = Some(SYNTH_RTT_BUDGET_NS);
    w.policy.max_challenge_age_ns = None;
    assert_eq!(unknown_reasons(&ex.state(&w.policy)), [Reason::TimingBudgetUnset]);
}

#[test]
fn transport_failure_is_unknown() {
    let (w, mut ex) = opening("transport");
    ex.samples.transport_result = "timeout".into();
    assert_eq!(unknown_reasons(&ex.state(&w.policy)), [Reason::TransportFailed]);
}

#[test]
fn service_wall_clock_jumps_change_no_decision() {
    for wall in [-1_000_000_000_000i64, 0, 4_000_000_000_000_000] {
        let w = World::new();
        w.svc.set_wall_us(wall);
        let ex = exchange(&w, &Session::new("wall"), Purpose::Opening, 0, None, 0, 9);
        assert_eq!(ex.ack().unsigned_payload.received_wall_us, wall.to_string());
        assert!(ex.state(&w.policy).retained().is_some(), "{wall}");
    }
}

#[test]
fn server_epoch_mismatch_between_challenge_and_ack_is_unknown() {
    let (w, ex) = opening("epoch");
    let mut u = ex.ack().unsigned_payload;
    u.server_clock_epoch_id = id("other-server-clock");
    let bytes = enc(&qorcap::anchor_seen_at::types::SignedAck::sign(u, &w.ack_key).unwrap());
    assert_eq!(unknown_reasons(&check::check_ack(&ex.ch, &bytes, &ex.commitment, &ex.samples, &w.policy)), [Reason::ServerEpochMismatch]);
}

#[test]
fn echo_ordering_is_opening_only_and_later_purposes_use_send_timing() {
    let w = World::new();
    let s = Session::new("purpose-timing");
    let o = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    // Interval: an echo value from before the challenge, or after the send, is not an
    // ordering failure — there is no echo capture for this purpose.
    let mut iv = exchange(&w, &s, Purpose::Interval, 1, Some(&o.ack_id()), 10, 19);
    iv.samples.echo_capture_mono_ns = "500".into();
    assert!(iv.state(&w.policy).retained().is_some(), "interval ignores echo ordering");
    iv.samples.echo_capture_mono_ns = "3500".into();
    assert!(iv.state(&w.policy).retained().is_some(), "interval echo after send is not ordered");
    // But the interval challenge must still be received before its commitment is sent,
    // and its age is measured at that send.
    iv.samples.echo_capture_mono_ns = "2000".into();
    iv.samples.challenge_received_mono_ns = "3001".into();
    assert_eq!(unknown_reasons(&iv.state(&w.policy)), [Reason::LocalOrdering], "interval send before challenge receipt");
    iv.samples.challenge_received_mono_ns = "1000".into();
    iv.samples.send_mono_ns = (1000 + SYNTH_CHALLENGE_AGE_NS + 1).to_string();
    iv.samples.receive_mono_ns = iv.samples.send_mono_ns.clone();
    assert!(matches!(iv.state(&w.policy), AnchorState::SeenAtLate(_)), "interval age is measured at send");
    // Close follows the same purpose-specific rule as interval.
    let mut cl = exchange(&w, &s, Purpose::Close, 2, Some(&iv.ack_id()), 20, 29);
    cl.samples.echo_capture_mono_ns = "500".into();
    assert!(cl.state(&w.policy).retained().is_some(), "close ignores echo ordering");
    cl.samples.challenge_received_mono_ns = "3001".into();
    assert_eq!(unknown_reasons(&cl.state(&w.policy)), [Reason::LocalOrdering], "close send before challenge receipt");
    // The opening keeps its strict echo ordering (covered by
    // `negative_rtt_and_broken_local_ordering_are_unknown`).
}

#[test]
fn challenge_expiry_is_compared_only_inside_one_server_boot() {
    // A challenge from boot 0 consumed after a restart is refused, never compared with the new clock.
    let w = World::new();
    let s = Session::new("boot");
    let ch = challenge(&w, &s, Purpose::Opening);
    w.svc.restart();
    let c = s.commitment(&ch, 0, None, 0, 9);
    assert_eq!(w.svc.post_commitments(&commit_body(&ch, &c)), Err(qorcap::anchor_seen_at::service_double::ServiceError::InvalidatedByRestart));
}
