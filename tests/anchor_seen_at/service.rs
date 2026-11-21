//! Design section 9, replay/concurrency, against the in-process service double.

use crate::fixtures::*;
use qorcap::anchor_seen_at::check;
use qorcap::anchor_seen_at::service_double::*;
use qorcap::anchor_seen_at::types::*;
use qorcap::anchor_seen_at::*;
use serde_json::json;
use std::sync::Arc;

fn post(w: &World, ch: &SignedChallenge, c: &Commitment) -> Result<Vec<u8>, ServiceError> { w.svc.route("POST", PATH_COMMITMENTS, &commit_body(ch, c)) }

#[test]
fn challenge_is_single_use_and_double_consumption_is_rejected() {
    let w = World::new();
    let s = Session::new("single");
    let ch = challenge(&w, &s, Purpose::Opening);
    let a = s.commitment(&ch, 0, None, 0, 9);
    let first = post(&w, &ch, &a).unwrap();
    let mut b = a.clone();
    b.chain.last_seq = "8".into();
    b.chain.end_head = s.head(8);
    b.capture_ranges[0].last_seq = "8".into();
    assert_eq!(post(&w, &ch, &b), Err(ServiceError::Conflict));
    assert_eq!(post(&w, &ch, &a).unwrap(), first);
    assert_eq!(w.svc.ledger_len(), 1);
}

#[test]
fn identical_retry_returns_identical_bytes_even_after_expiry_and_restart() {
    let w = World::new();
    let s = Session::new("retry");
    let ex = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    let body = commit_body(&ex.ch, &ex.commitment);
    w.svc.advance_mono_ns(SYNTH_LIFETIME_NS * 10);
    assert_eq!(w.svc.post_commitments(&body).unwrap(), ex.ack_bytes);
    w.svc.restart();
    assert_eq!(w.svc.post_commitments(&body).unwrap(), ex.ack_bytes);
    assert_eq!(w.svc.route("GET", &format!("{PATH_ACKS}{}", ex.ack_id()), b"").unwrap(), ex.ack_bytes);
    assert_eq!(w.svc.ledger_len(), 1, "retries never append or advance coverage");
    // the recovered ack keeps its original boot and time, not a fresh timestamp
    let a = ex.ack().unsigned_payload;
    assert_ne!(a.server_boot_id, w.svc.boot_id());
    assert!(ex.state(&w.policy).retained().is_some());
}

#[test]
fn conflicting_retry_is_rejected_and_never_signed() {
    let w = World::new();
    let s = Session::new("conflict");
    let ex = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    let mut other = ex.commitment.clone();
    other.seal_hash = hex64("different-seal");
    assert_eq!(post(&w, &ex.ch, &other), Err(ServiceError::Conflict));
    assert_eq!(w.svc.ledger_len(), 1);
}

#[test]
fn expired_challenge_is_rejected_by_the_service() {
    let w = World::new();
    let s = Session::new("expire");
    let ch = challenge(&w, &s, Purpose::Opening);
    w.svc.advance_mono_ns(SYNTH_LIFETIME_NS);
    assert_eq!(post(&w, &ch, &s.commitment(&ch, 0, None, 0, 9)), Err(ServiceError::Expired));
    assert_eq!(w.svc.ledger_len(), 0);
}

#[test]
fn concurrent_equal_and_different_consumption_commits_exactly_one_candidate() {
    let w = Arc::new(World::new());
    let s = Session::new("race");
    let ch = challenge(&w, &s, Purpose::Opening);
    let a = commit_body(&ch, &s.commitment(&ch, 0, None, 0, 9));
    let b = commit_body(&ch, &s.commitment(&ch, 0, None, 0, 5));
    let handles: Vec<_> = (0..16).map(|i| {
        let (w, body) = (w.clone(), if i % 2 == 0 { a.clone() } else { b.clone() });
        std::thread::spawn(move || (i % 2, w.svc.post_commitments(&body)))
    }).collect();
    let results: Vec<_> = handles.into_iter().map(|h| h.join().unwrap()).collect();
    let winners: std::collections::BTreeSet<usize> = results.iter().filter(|(_, r)| r.is_ok()).map(|(k, _)| *k).collect();
    assert_eq!(winners.len(), 1, "only one candidate commits");
    let ok: std::collections::BTreeSet<Vec<u8>> = results.iter().filter_map(|(_, r)| r.clone().ok()).collect();
    assert_eq!(ok.len(), 1, "every success is the same retained bytes");
    assert!(results.iter().all(|(k, r)| if winners.contains(k) { r.is_ok() } else { *r == Err(ServiceError::Conflict) }));
    assert_eq!(w.svc.ledger_len(), 1);
}

#[test]
fn ack_replayed_into_another_session_epoch_or_service_is_rejected() {
    let w = World::new();
    let a = Session::new("replay-a");
    let ex = exchange(&w, &a, Purpose::Opening, 0, None, 0, 9);
    // the client of session B (or a new device/capture epoch) is handed A's authentic ack
    for b in ["replay-b", "replay-b-device", "replay-b-capture"] {
        let sb = Session::new(b);
        let ch_b = challenge(&w, &sb, Purpose::Opening);
        let mine = sb.commitment(&ch_b, 0, None, 0, 9);
        let st = check::check_ack(&ch_b, &ex.ack_bytes, &mine, &sb.samples(), &w.policy);
        assert_eq!(unknown_reasons(&st), [Reason::BindingMismatch], "{b}");
        // even if B wrongly kept A's challenge
        let st = check::check_ack(&ex.ch, &ex.ack_bytes, &mine, &sb.samples(), &w.policy);
        assert!(matches!(st, AnchorState::Unknown { .. }), "{b}");
    }
    let mut new_device = a.clone();
    new_device.device_epoch_id = id("reconnected-device");
    let mut c = ex.commitment.clone();
    c.device_epoch_id = new_device.device_epoch_id.clone();
    assert_eq!(unknown_reasons(&check::check_ack(&ex.ch, &ex.ack_bytes, &c, &ex.samples, &w.policy)), [Reason::BindingMismatch]);
    let mut c = ex.commitment.clone();
    c.capture_epoch_id = id("new-capture-epoch");
    assert_eq!(unknown_reasons(&check::check_ack(&ex.ch, &ex.ack_bytes, &c, &ex.samples, &w.policy)), [Reason::BindingMismatch]);
    // the same authentic ack checked under another service's pinned policy
    let other = World::with_keys("challenge-a", "ack-a");
    let mut p = other.policy.clone();
    p.trust = qorcap::anchor_seen_at::keys::TrustPolicy::new("other-trust", "other.service", AUDIENCE, &[POLICY], vec![]).unwrap();
    assert_eq!(unknown_reasons(&ex.state(&p)), [Reason::KeyServiceMismatch]);
}

#[test]
fn mitm_swap_of_another_authentic_ack_is_rejected() {
    let w = World::new();
    let s = Session::new("mitm");
    let o = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    let i = exchange(&w, &s, Purpose::Interval, 1, Some(&o.ack_id()), 10, 19);
    // the interval's authentic ack delivered as the reply to the opening
    assert!(matches!(check::check_ack(&o.ch, &i.ack_bytes, &o.commitment, &o.samples, &w.policy), AnchorState::Unknown { .. }));
    assert!(matches!(check::check_ack(&i.ch, &o.ack_bytes, &i.commitment, &i.samples, &w.policy), AnchorState::Unknown { .. }));
}

#[test]
fn commitment_bound_to_another_session_or_nonce_is_refused_by_the_service() {
    let w = World::new();
    let a = Session::new("bind-a");
    let ch = challenge(&w, &a, Purpose::Opening);
    let good = a.commitment(&ch, 0, None, 0, 9);
    let swaps: Vec<Box<dyn Fn(&mut Commitment)>> = vec![
        Box::new(|c| c.session_id = id("bind-b")),
        Box::new(|c| c.device_epoch_id = id("bind-b-dev")),
        Box::new(|c| c.capture_epoch_id = id("bind-b-cap")),
        Box::new(|c| c.client_nonce = hex64("wrong-cn")),
        Box::new(|c| c.server_nonce = hex64("wrong-sn")),
        Box::new(|c| c.challenge_digest = hex64("wrong-cd")),
        Box::new(|c| c.policy_id = "synthetic-policy-2".into()),
    ];
    for f in swaps {
        let mut c = good.clone();
        f(&mut c);
        assert_eq!(post(&w, &ch, &c), Err(ServiceError::BindingMismatch));
    }
    let mut interval_purpose = good.clone();
    interval_purpose.purpose = Purpose::Interval;
    assert!(post(&w, &ch, &interval_purpose).is_err());
    assert_eq!(w.svc.ledger_len(), 0);
    assert!(post(&w, &ch, &good).is_ok());
}

#[test]
fn wrong_seal_or_echo_is_rejected() {
    let w = World::new();
    let s = Session::new("seal");
    let ex = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    // client recorded a different echo than the one it committed
    let other_echo = Session::with_echo("seal", "ffffffff");
    let mut mine = ex.commitment.clone();
    mine.seal_hash = other_echo.seal_hash.clone();
    assert_eq!(unknown_reasons(&check::check_ack(&ex.ch, &ex.ack_bytes, &mine, &ex.samples, &w.policy)), [Reason::BindingMismatch]);
    // an interval cannot switch seal mid-stream
    let ch = challenge(&w, &s, Purpose::Interval);
    let mut c = s.commitment(&ch, 1, Some(&ex.ack_id()), 10, 19);
    c.seal_hash = other_echo.seal_hash.clone();
    assert_eq!(post(&w, &ch, &c), Err(ServiceError::Fork));
}

#[test]
fn forks_holes_overlaps_and_wrong_prior_are_rejected() {
    let w = World::new();
    let s = Session::new("fork");
    let o = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    let cases: Vec<(&str, Box<dyn Fn(&Session, &SignedChallenge) -> Commitment>)> = vec![
        ("skip index", Box::new(|s, ch| s.commitment(ch, 2, Some(&o.ack_id()), 10, 19))),
        ("hole", Box::new(|s, ch| s.commitment(ch, 1, Some(&o.ack_id()), 11, 19))),
        ("overlap", Box::new(|s, ch| s.commitment(ch, 1, Some(&o.ack_id()), 9, 19))),
        ("wrong prior", Box::new(|s, ch| s.commitment(ch, 1, Some(&hex64("not-the-prior")), 10, 19))),
        ("changed predecessor head", Box::new(|s, ch| { let mut c = s.commitment(ch, 1, Some(&o.ack_id()), 10, 19); c.chain.predecessor_head = hex64("rewritten"); c })),
    ];
    for (name, f) in cases {
        let ch = challenge(&w, &s, Purpose::Interval);
        assert_eq!(post(&w, &ch, &f(&s, &ch)), Err(ServiceError::Fork), "{name}");
    }
    let i1 = exchange(&w, &s, Purpose::Interval, 1, Some(&o.ack_id()), 10, 19);
    // a second branch at index 1 after the first committed
    let ch = challenge(&w, &s, Purpose::Interval);
    let mut fork = s.commitment(&ch, 1, Some(&o.ack_id()), 10, 15);
    fork.chain.end_head = hex64("fork-head");
    assert_eq!(post(&w, &ch, &fork), Err(ServiceError::Fork));
    // a second opening on the same stream
    let ch = challenge(&w, &s, Purpose::Opening);
    assert_eq!(post(&w, &ch, &s.commitment(&ch, 0, None, 0, 9)), Err(ServiceError::Fork));
    assert!(exchange(&w, &s, Purpose::Interval, 2, Some(&i1.ack_id()), 20, 29).state(&w.policy).retained().is_some());
}

#[test]
fn source_rows_after_close_are_refused() {
    let w = World::new();
    let s = Session::new("closed");
    let v = full_stream(&w, &s);
    let ch = challenge(&w, &s, Purpose::Interval);
    assert_eq!(post(&w, &ch, &s.commitment(&ch, 3, Some(&v[2].ack_id()), 30, 39)), Err(ServiceError::StreamClosed));
}

#[test]
fn crash_before_commit_gives_no_success_and_changes_nothing() {
    let w = World::new();
    let s = Session::new("crash");
    let ch = challenge(&w, &s, Purpose::Opening);
    let c = s.commitment(&ch, 0, None, 0, 9);
    w.svc.inject(ServiceFault::CrashBeforeCommit);
    assert_eq!(post(&w, &ch, &c), Err(ServiceError::Unavailable));
    assert_eq!(w.svc.ledger_len(), 0);
    w.svc.inject(ServiceFault::RetentionFailure);
    assert_eq!(post(&w, &ch, &c), Err(ServiceError::RetentionFailed));
    assert_eq!(w.svc.ledger_len(), 0);
    assert!(post(&w, &ch, &c).is_ok(), "the challenge was never consumed");
}

#[test]
fn dropped_response_after_commit_is_recovered_not_regenerated() {
    let w = World::new();
    let s = Session::new("dropped");
    let ch = challenge(&w, &s, Purpose::Opening);
    let c = s.commitment(&ch, 0, None, 0, 9);
    w.svc.inject(ServiceFault::ResponseLostAfterCommit);
    assert_eq!(post(&w, &ch, &c), Err(ServiceError::ResponseLost));
    w.svc.advance_mono_ns(1_000_000);
    w.svc.set_wall_us(1_800_000_000_000_000);
    let again = post(&w, &ch, &c).unwrap();
    let a: SignedAck = serde_json::from_slice(&again).unwrap();
    assert_eq!(a.unsigned_payload.received_mono_ns, "1000000", "original receipt time, not the retry time");
    assert_eq!(w.svc.get_ack(&a.ack_id).unwrap(), again);
    assert_eq!(w.svc.ledger_len(), 1);
}

#[test]
fn restart_invalidates_outstanding_challenges_but_keeps_committed_acks() {
    let w = World::new();
    let s = Session::new("restart");
    let o = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    let pending = challenge(&w, &s, Purpose::Interval);
    w.svc.restart();
    assert_eq!(post(&w, &pending, &s.commitment(&pending, 1, Some(&o.ack_id()), 10, 19)), Err(ServiceError::InvalidatedByRestart));
    assert_eq!(w.svc.get_ack(&o.ack_id()).unwrap(), o.ack_bytes);
    let fresh = exchange(&w, &s, Purpose::Interval, 1, Some(&o.ack_id()), 10, 19);
    assert_eq!(fresh.ack().unsigned_payload.server_boot_id, w.svc.boot_id());
    assert!(fresh.state(&w.policy).retained().is_some());
}

#[test]
fn unknown_or_altered_challenge_and_unrouted_requests_are_refused() {
    let w = World::new();
    let s = Session::new("unknown");
    let ch = challenge(&w, &s, Purpose::Opening);
    let c = s.commitment(&ch, 0, None, 0, 9);
    let mut altered = ch.clone();
    altered.unsigned_payload.expires_mono_ns = "999999999999999".into();
    assert_eq!(post(&w, &altered, &c), Err(ServiceError::ChallengeMismatch));
    let mut unknown = ch.clone();
    unknown.unsigned_payload.challenge_id = hex64("never-issued");
    assert_eq!(post(&w, &unknown, &c), Err(ServiceError::UnknownChallenge));
    assert_eq!(w.svc.route("PUT", PATH_COMMITMENTS, b""), Err(ServiceError::NotRouted));
    assert_eq!(w.svc.route("GET", &format!("{PATH_ACKS}{}", hex64("x")), b"x"), Err(ServiceError::NotRouted));
    assert_eq!(w.svc.get_ack("../etc"), Err(ServiceError::NotFound));
    assert!(matches!(w.svc.route("POST", PATH_CHALLENGES, b"{}"), Err(ServiceError::Malformed(_))));
    let wrong_service = json!({"schema": SCHEMA_CHALLENGE_REQUEST, "purpose": "opening", "service": "other.service", "audience": AUDIENCE,
        "policy_id": POLICY, "session_id": s.session_id, "device_epoch_id": s.device_epoch_id, "capture_epoch_id": s.capture_epoch_id, "client_nonce": hex64("n")});
    assert_eq!(w.svc.post_challenges(&enc(&wrong_service)), Err(ServiceError::BindingMismatch));
}

#[test]
fn client_rejects_a_challenge_not_bound_to_its_request() {
    let w = World::new();
    let s = Session::new("chbind");
    let req = s.request(Purpose::Opening);
    let bytes = w.svc.post_challenges(&enc(&req)).unwrap();
    let mut other = req.clone();
    other.client_nonce = hex64("someone-else");
    assert_eq!(check::check_challenge(&bytes, &other, &w.policy.trust).unwrap_err(), Reason::BindingMismatch);
    let mut purpose = req.clone();
    purpose.purpose = Purpose::Close;
    assert_eq!(check::check_challenge(&bytes, &purpose, &w.policy.trust).unwrap_err(), Reason::BindingMismatch);
}
