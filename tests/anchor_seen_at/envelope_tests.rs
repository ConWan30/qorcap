//! Design section 6: one typed `AnchorEvidenceV1` envelope with exactly one `referee_seen_at` payload.

use crate::fixtures::*;
use qorcap::anchor_seen_at::envelope::{self, ExpectedExchange};
use qorcap::anchor_seen_at::types::Purpose;
use qorcap::anchor_seen_at::*;
use serde_json::{json, Value};

fn setup() -> (World, Exchange, ExpectedExchange) {
    let w = World::new();
    let s = Session::new("env");
    let ex = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    let b = ex.expected();
    (w, ex, b)
}

fn check(bytes: &[u8], b: &ExpectedExchange, w: &World) -> AnchorState { envelope::check_envelope(Some(bytes), b, &w.policy).state }

#[test]
fn valid_envelope_is_seen_at_and_verification_is_recomputed() {
    let (w, ex, b) = setup();
    let out = envelope::check_envelope(Some(&ex.envelope()), &b, &w.policy);
    assert!(out.state.retained().is_some());
    assert_eq!(out.verification.status, "seen_at_retained");
    assert_eq!(out.verification.label, PRACTICE_ONLY);
    let v: Value = serde_json::from_slice(&ex.envelope()).unwrap();
    assert_eq!(v["evidence_id"], json!(ex.ack_id()));
}

#[test]
fn no_envelope_is_unknown_never_a_pass() {
    let (w, _, b) = setup();
    let out = envelope::check_envelope(None, &b, &w.policy);
    assert_eq!(unknown_reasons(&out.state), [Reason::NoAck]);
    assert_eq!(out.verification.status, "unknown");
    assert_eq!(out.verification.reason_codes, ["no_ack"]);
}

#[test]
fn stored_verification_is_not_authority() {
    let (w, ex, b) = setup();
    let claim = json!({"local_check_version": "x", "trust_policy_id": TRUST_POLICY_ID, "status": "seen_at_retained", "reason_codes": [], "label": PRACTICE_ONLY});
    let bytes = tamper(&ex.envelope(), |v| {
        v["verification"] = claim.clone();
        v["payload"]["referee_seen_at"]["ack"]["signature"] = json!(qorcap::anchor_seen_at::canon::b64url(&[0u8; 64]));
    });
    let out = envelope::check_envelope(Some(&bytes), &b, &w.policy);
    assert_eq!(unknown_reasons(&out.state), [Reason::BadSignature]);
    assert_eq!(out.verification.status, "unknown");
    // and a stored verification on a good envelope changes nothing
    let ok = tamper(&ex.envelope(), |v| v["verification"] = claim.clone());
    assert!(check(&ok, &b, &w).retained().is_some());
}

#[test]
fn payload_count_other_than_one_is_rejected() {
    let (w, ex, b) = setup();
    let p = serde_json::to_value(ex.payload()).unwrap();
    let cases = vec![
        json!({}),
        json!({"referee_seen_at": p.clone(), "unsupported_example_a": p.clone()}),
        json!([p.clone()]),
        json!([p.clone(), p.clone()]),
        Value::Null,
    ];
    for c in cases {
        let bytes = tamper(&ex.envelope(), |v| v["payload"] = c.clone());
        assert_eq!(unknown_reasons(&check(&bytes, &b, &w)), [Reason::PayloadCount], "{c}");
    }
    let bytes = tamper(&ex.envelope(), |v| { v.as_object_mut().unwrap().remove("payload"); });
    assert_eq!(unknown_reasons(&check(&bytes, &b, &w)), [Reason::SchemaViolation]);
}

#[test]
fn other_kinds_cannot_stand_in_for_service_seen_at() {
    let (w, ex, b) = setup();
    let p = serde_json::to_value(ex.payload()).unwrap();
    // Any kind other than the one supported kind is rejected generically, with no
    // kind-specific handling for unsupported payloads.
    for k in ["unsupported_example_a", "unsupported_example_b", "trusted_anchor", "some_other_kind"] {
        let bytes = tamper(&ex.envelope(), |v| { v["kind"] = json!(k); let mut m = serde_json::Map::new(); m.insert(k.to_string(), p.clone()); v["payload"] = Value::Object(m); });
        assert_eq!(unknown_reasons(&check(&bytes, &b, &w)), [Reason::UnknownKind], "{k}");
    }
    // declared kind and payload kind disagree: no coercion
    let bytes = tamper(&ex.envelope(), |v| v["kind"] = json!("unsupported_example_a"));
    assert_eq!(unknown_reasons(&check(&bytes, &b, &w)), [Reason::EnvelopeMismatch]);
}

#[test]
fn naked_trusted_anchor_or_unknown_fields_are_rejected() {
    let (w, ex, b) = setup();
    for (path, key) in [(None, "trusted_anchor"), (None, "anchored"), (Some("binding"), "extra"), (Some("coverage"), "row_count")] {
        let bytes = tamper(&ex.envelope(), |v| match path { None => v[key] = json!(true), Some(p) => v[p][key] = json!("1") });
        assert_eq!(unknown_reasons(&check(&bytes, &b, &w)), [Reason::SchemaViolation], "{key}");
    }
    let bytes = tamper(&ex.envelope(), |v| v["payload"]["referee_seen_at"]["trusted_anchor"] = json!(true));
    assert_eq!(unknown_reasons(&check(&bytes, &b, &w)), [Reason::SchemaViolation]);
    let bytes = tamper(&ex.envelope(), |v| v["schema"] = json!("proposed.anchor_evidence.v2"));
    assert_eq!(unknown_reasons(&check(&bytes, &b, &w)), [Reason::WrongSchema]);
}

#[test]
fn duplicated_bindings_coverage_and_clocks_must_match_the_signed_variant() {
    let (w, ex, b) = setup();
    let edits: Vec<Box<dyn Fn(&mut Value)>> = vec![
        Box::new(|v| v["evidence_id"] = json!(hex64("other"))),
        Box::new(|v| v["binding"]["seal_hash"] = json!(hex64("other-seal"))),
        Box::new(|v| v["binding"]["session_id"] = json!(id("other-session"))),
        Box::new(|v| v["coverage"]["last_seq"] = json!("99")),
        Box::new(|v| v["coverage"]["end_head"] = json!(hex64("other-head"))),
        Box::new(|v| v["clocks"][0]["boot_id"] = json!(id("other-boot"))),
        Box::new(|v| v["clocks"][1]["owner"] = json!("referee_service")),
        Box::new(|v| { v["clocks"].as_array_mut().unwrap().pop(); }),
        Box::new(|v| v["payload"]["referee_seen_at"]["ledger_seq"] = json!("5")),
        Box::new(|v| v["payload"]["referee_seen_at"]["ack_key_id"] = json!(hex64("k"))),
        Box::new(|v| v["payload"]["referee_seen_at"]["service"] = json!("other.service")),
    ];
    for (i, e) in edits.into_iter().enumerate() {
        let bytes = tamper(&ex.envelope(), |v| e(v));
        assert_eq!(unknown_reasons(&check(&bytes, &b, &w)), [Reason::EnvelopeMismatch], "edit {i}");
    }
    let bytes = tamper(&ex.envelope(), |v| v["payload"]["referee_seen_at"]["trust_policy_id"] = json!("other-trust"));
    assert_eq!(unknown_reasons(&check(&bytes, &b, &w)), [Reason::PolicyMismatch]);
}

#[test]
fn authentic_envelope_for_another_session_is_rejected() {
    let (w, ex, _) = setup();
    let other = exchange(&w, &Session::new("env-other"), Purpose::Opening, 0, None, 0, 9);
    assert_eq!(unknown_reasons(&check(&ex.envelope(), &other.expected(), &w)), [Reason::BindingMismatch]);
}

#[test]
fn same_binding_substitution_is_rejected_against_the_retained_exchange() {
    let (w, ex, b) = setup();
    // A second authentic exchange for the same session (same four binding fields) but a
    // different challenge/commitment/ack: a second service instance with the same pinned
    // keys signed it too, so binding-only checking would accept it. Against the
    // independently retained exchange it must not.
    let w2 = World::new();
    let sub = exchange(&w2, &Session::new("env"), Purpose::Opening, 0, None, 0, 5);
    assert_eq!(envelope::binding_of(&sub.commitment), b.binding);
    assert_ne!(sub.ack_id(), b.ack_id);
    assert_eq!(unknown_reasons(&check(&sub.envelope(), &b, &w)), [Reason::BindingMismatch]);
    // Unsigned timing carried inside the envelope cannot decide eligibility either:
    // samples that differ from the retained record are rejected, not consulted.
    let bytes = tamper(&ex.envelope(), |v| v["payload"]["referee_seen_at"]["local_samples"]["receive_mono_ns"] = json!("3001"));
    assert_eq!(unknown_reasons(&check(&bytes, &b, &w)), [Reason::EnvelopeMismatch]);
    assert!(check(&ex.envelope(), &b, &w).retained().is_some());
}

#[test]
fn envelope_that_is_not_canonical_is_rejected() {
    let (w, ex, b) = setup();
    let mut pretty = serde_json::to_vec_pretty(&serde_json::from_slice::<Value>(&ex.envelope()).unwrap()).unwrap();
    assert_eq!(unknown_reasons(&check(&pretty, &b, &w)), [Reason::NonCanonical]);
    pretty.truncate(40);
    assert_eq!(unknown_reasons(&check(&pretty, &b, &w)), [Reason::Malformed]);
}
