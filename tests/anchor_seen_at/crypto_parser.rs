//! Design section 9, crypto/parser; QorProof Track 1 binding, forgery and key-substitution items.

use crate::fixtures::*;
use ed25519_dalek::Signer;
use qorcap::anchor_seen_at::canon::{self, frame};
use qorcap::anchor_seen_at::check::{self, SeenAtFact};
use qorcap::anchor_seen_at::keys::{KeyScope, KeyStatus};
use qorcap::anchor_seen_at::readiness::CaptureValidity;
use qorcap::anchor_seen_at::types::*;
use qorcap::anchor_seen_at::*;
use serde_json::{json, Value};
use std::collections::BTreeSet;

fn opening() -> (World, Session, Exchange) {
    let w = World::new();
    let s = Session::new("crypto");
    let ex = exchange(&w, &s, Purpose::Opening, 0, None, 0, 9);
    (w, s, ex)
}

fn resign(ex: &Exchange, key: &ed25519_dalek::SigningKey, f: impl FnOnce(&mut AckUnsigned)) -> Vec<u8> {
    let mut u = ex.ack().unsigned_payload;
    f(&mut u);
    enc(&SignedAck::sign(u, key).unwrap())
}

fn state_of(ex: &Exchange, w: &World, bytes: &[u8]) -> AnchorState { check::check_ack(&ex.ch, bytes, &ex.commitment, &ex.samples, &w.policy) }

#[test]
fn genuine_ack_is_seen_at_retained_and_means_only_seen_at() {
    let (w, _, ex) = opening();
    let st = ex.state(&w.policy);
    let f: &SeenAtFact = st.retained().expect("synthetic genuine ack is seen-at retained");
    assert_eq!(f.meaning(), ACK_MEANING);
    assert_eq!(f.capture_validity(), CaptureValidity::NotEstablishedByAck);
    assert_eq!(f.commitment, ex.commitment);
}

#[test]
fn framing_is_length_prefixed_and_reproducible() {
    let f = frame("AB", b"xyz");
    assert_eq!(f, [&2u64.to_be_bytes()[..], b"AB", &3u64.to_be_bytes()[..], b"xyz"].concat());
    let seal = seal_for("repro", "00ff");
    assert_eq!(seal.seal_hash().unwrap(), seal_for("repro", "00ff").seal_hash().unwrap());
    assert_ne!(seal.seal_hash().unwrap(), seal_for("repro", "00fe").seal_hash().unwrap());
    let want = qorcap::evidence::sha256_hex(&frame(DOMAIN_SEAL, &canon::canonical_bytes(&seal).unwrap()));
    assert_eq!(seal.seal_hash().unwrap(), want);
}

#[test]
fn the_same_object_digests_differently_under_every_domain() {
    let v = json!({"a": "b"});
    let ds: BTreeSet<String> = [DOMAIN_CHALLENGE, DOMAIN_ACK, DOMAIN_COMMITMENT, DOMAIN_ACK_ID, DOMAIN_SEAL, DOMAIN_LEDGER_ENTRY]
        .iter().map(|d| canon::digest(d, &v).unwrap()).collect();
    assert_eq!(ds.len(), 6);
}

#[test]
fn seal_rejects_prefixed_or_uppercase_echo_bytes() {
    for bad in ["0xa1b2", "A1B2", "a1b", ""] {
        assert_eq!(seal_for("seal", bad).seal_hash(), Err(Reason::FieldFormat), "{bad}");
    }
}

#[test]
fn ack_id_and_signature_are_outside_their_own_preimage() {
    let (_, _, ex) = opening();
    let a = ex.ack();
    let unsigned = String::from_utf8(enc(&a.unsigned_payload)).unwrap();
    assert!(!unsigned.contains(&a.ack_id));
    assert!(!unsigned.contains(&a.signature));
    assert_eq!(a.unsigned_payload.ack_id().unwrap(), a.ack_id);
    let interval_prior = ex.commitment.prior_ack_id.clone();
    assert!(interval_prior.is_none(), "opening has no prior ack");
}

#[test]
fn wrong_domain_signatures_are_rejected() {
    let (w, _, ex) = opening();
    // genuine ack key, but signed under the challenge domain
    let u = ex.ack().unsigned_payload;
    let sig = w.ack_key.sign(&canon::signing_message(DOMAIN_CHALLENGE, &u).unwrap());
    let bytes = enc(&SignedAck { ack_id: u.ack_id().unwrap(), signature: canon::b64url(&sig.to_bytes()), unsigned_payload: u.clone() });
    assert_eq!(unknown_reasons(&state_of(&ex, &w, &bytes)), [Reason::BadSignature]);
    // genuine ack key over raw JCS with no domain frame
    let sig = w.ack_key.sign(&canon::canonical_bytes(&u).unwrap());
    let bytes = enc(&SignedAck { ack_id: u.ack_id().unwrap(), signature: canon::b64url(&sig.to_bytes()), unsigned_payload: u.clone() });
    assert_eq!(unknown_reasons(&state_of(&ex, &w, &bytes)), [Reason::BadSignature]);
    // ack id computed under the commitment domain instead of the ack-id domain
    let bytes = tamper(&ex.ack_bytes, |v| v["ack_id"] = json!(canon::digest(DOMAIN_COMMITMENT, &u).unwrap()));
    assert_eq!(unknown_reasons(&state_of(&ex, &w, &bytes)), [Reason::AckIdMismatch]);
    // commitment digest computed under the seal domain, re-signed by the genuine key
    let bytes = resign(&ex, &w.ack_key, |u| u.commitment_digest = canon::digest(DOMAIN_SEAL, &u.commitment).unwrap());
    assert_eq!(unknown_reasons(&state_of(&ex, &w, &bytes)), [Reason::DigestMismatch]);
}

#[test]
fn every_unsigned_field_mutation_without_resigning_is_rejected() {
    let (w, _, ex) = opening();
    let fields = ["key_id", "purpose", "service", "audience", "policy_id", "commitment_digest", "server_boot_id", "server_clock_epoch_id",
        "received_mono_ns", "received_wall_us", "ledger_seq", "prev_ledger_entry_digest"];
    for k in fields {
        let bytes = tamper(&ex.ack_bytes, |v| {
            let cur = v["unsigned_payload"][k].as_str().unwrap().to_string();
            let new = match k {
                "purpose" => "interval".to_string(),
                "service" | "audience" | "policy_id" => format!("{cur}x"),
                "received_mono_ns" | "ledger_seq" => "7".to_string(),
                "received_wall_us" => "-5".to_string(),
                "server_boot_id" | "server_clock_epoch_id" => id("other"),
                _ => hex64("other"),
            };
            v["unsigned_payload"][k] = json!(new);
        });
        assert!(matches!(state_of(&ex, &w, &bytes), AnchorState::Unknown { .. }), "{k}");
    }
    let commitment_fields = ["session_id", "device_epoch_id", "capture_epoch_id", "seal_hash", "client_nonce", "server_nonce", "challenge_id", "challenge_digest"];
    for k in commitment_fields {
        let bytes = tamper(&ex.ack_bytes, |v| {
            let new = if k.ends_with("epoch_id") || k == "session_id" { id("swap") } else { hex64("swap") };
            v["unsigned_payload"]["commitment"][k] = json!(new);
        });
        assert!(matches!(state_of(&ex, &w, &bytes), AnchorState::Unknown { .. }), "commitment.{k}");
    }
}

#[test]
fn every_binding_mutation_resigned_by_the_genuine_key_still_fails_closed() {
    // A service bug or a second authentic ack: signature is valid, bindings are not ours.
    let (w, _, ex) = opening();
    let muts: Vec<(&str, Box<dyn Fn(&mut AckUnsigned)>)> = vec![
        ("session", Box::new(|u| u.commitment.session_id = id("other-session"))),
        ("device_epoch", Box::new(|u| u.commitment.device_epoch_id = id("other-device"))),
        ("capture_epoch", Box::new(|u| u.commitment.capture_epoch_id = id("other-capture"))),
        ("seal", Box::new(|u| u.commitment.seal_hash = hex64("other-seal"))),
        ("client_nonce", Box::new(|u| u.commitment.client_nonce = hex64("other-cn"))),
        ("server_nonce", Box::new(|u| u.commitment.server_nonce = hex64("other-sn"))),
        ("challenge_id", Box::new(|u| u.commitment.challenge_id = hex64("other-ch"))),
        ("end_head", Box::new(|u| u.commitment.chain.end_head = hex64("other-head"))),
        ("last_seq", Box::new(|u| u.commitment.chain.last_seq = "8".into())),
        ("server_boot", Box::new(|u| u.server_boot_id = id("other-boot"))),
        ("received_after_expiry", Box::new(|u| u.received_mono_ns = "999999999999999".into())),
        ("received_before_issue", Box::new(|u| u.received_mono_ns = "0".into())),
        ("top_level_policy", Box::new(|u| u.policy_id = "synthetic-policy-2".into())),
    ];
    for (name, m) in muts {
        let bytes = resign(&ex, &w.ack_key, |u| { m(u); u.commitment_digest = u.commitment.digest().unwrap(); });
        assert!(matches!(state_of(&ex, &w, &bytes), AnchorState::Unknown { .. }), "{name}");
    }
}

#[test]
fn forged_but_well_formed_token_is_rejected() {
    let (w, _, ex) = opening();
    let attacker = synthetic_key("attacker");
    // claims the genuine pinned key id, signed by an unpinned key
    let bytes = resign(&ex, &attacker, |_| {});
    let a: SignedAck = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(a.unsigned_payload.key_id, ex.ack().unsigned_payload.key_id);
    assert_eq!(unknown_reasons(&state_of(&ex, &w, &bytes)), [Reason::BadSignature]);
    // names its own key id
    let bytes = resign(&ex, &attacker, |u| u.key_id = qorcap::anchor_seen_at::keys::key_id_for(&attacker.verifying_key()));
    assert_eq!(unknown_reasons(&state_of(&ex, &w, &bytes)), [Reason::UnknownKey]);
    // a forged challenge to go with it
    let mut ch = ex.ch.clone();
    ch.unsigned_payload.server_nonce = hex64("forged");
    let forged = SignedChallenge::sign(ch.unsigned_payload, &attacker).unwrap();
    let req = Session::new("crypto").request(Purpose::Opening);
    assert_eq!(check::check_challenge(&enc(&forged), &req, &w.policy.trust).unwrap_err(), Reason::BadSignature);
}

#[test]
fn ack_cannot_supply_its_own_key_or_url() {
    let (w, _, ex) = opening();
    let pk = canon::b64url(synthetic_key("attacker").verifying_key().as_bytes());
    for (k, v) in [("public_key", json!(pk)), ("key_url", json!("https://example.invalid/key")), ("trusted_anchor", json!(true))] {
        let bytes = tamper(&ex.ack_bytes, |x| x["unsigned_payload"][k] = v.clone());
        assert_eq!(unknown_reasons(&state_of(&ex, &w, &bytes)), [Reason::SchemaViolation], "{k}");
        let bytes = tamper(&ex.ack_bytes, |x| x[k] = v.clone());
        assert_eq!(unknown_reasons(&state_of(&ex, &w, &bytes)), [Reason::SchemaViolation], "top-level {k}");
    }
}

#[test]
fn key_outside_its_allowlist_scope_is_rejected() {
    let (w, _, ex) = opening();
    // the pinned challenge key signs an ack
    let ch_kid = qorcap::anchor_seen_at::keys::key_id_for(&w.ch_key.verifying_key());
    let bytes = resign(&ex, &w.ch_key, |u| u.key_id = ch_kid.clone());
    assert_eq!(unknown_reasons(&state_of(&ex, &w, &bytes)), [Reason::KeyScopeMismatch]);
    // the pinned ack key signs a challenge
    let s = Session::new("scope");
    let req = s.request(Purpose::Opening);
    let real = check::check_challenge(&w.svc.post_challenges(&enc(&req)).unwrap(), &req, &w.policy.trust).unwrap();
    let mut u = real.unsigned_payload.clone();
    u.key_id = qorcap::anchor_seen_at::keys::key_id_for(&w.ack_key.verifying_key());
    let swapped = SignedChallenge::sign(u, &w.ack_key).unwrap();
    assert_eq!(check::check_challenge(&enc(&swapped), &req, &w.policy.trust).unwrap_err(), Reason::KeyScopeMismatch);
    // pinned key used for another service or audience
    for f in [|u: &mut AckUnsigned| { u.service = "other.service".into(); u.commitment.service = "other.service".into(); },
              |u: &mut AckUnsigned| { u.audience = "other.audience".into(); u.commitment.audience = "other.audience".into(); }] {
        let bytes = resign(&ex, &w.ack_key, |u| { f(u); u.commitment_digest = u.commitment.digest().unwrap(); });
        assert_eq!(unknown_reasons(&state_of(&ex, &w, &bytes)), [Reason::KeyServiceMismatch]);
    }
}

#[test]
fn unknown_and_revoked_keys_fail_closed() {
    let (w, _, ex) = opening();
    let only_challenge = policy_with(vec![pinned(&w.ch_key, KeyScope::Challenge, KeyStatus::Active)]);
    assert_eq!(unknown_reasons(&ex.state(&only_challenge)), [Reason::UnknownKey]);
    let revoked = policy_with(vec![pinned(&w.ch_key, KeyScope::Challenge, KeyStatus::Active), pinned(&w.ack_key, KeyScope::Ack, KeyStatus::Revoked)]);
    assert_eq!(unknown_reasons(&ex.state(&revoked)), [Reason::KeyRevoked]);
    let revoked_ch = policy_with(vec![pinned(&w.ch_key, KeyScope::Challenge, KeyStatus::Revoked), pinned(&w.ack_key, KeyScope::Ack, KeyStatus::Active)]);
    assert_eq!(unknown_reasons(&ex.state(&revoked_ch)), [Reason::KeyRevoked]);
}

#[test]
fn rotation_needs_an_archived_record_not_the_services_own_wall_time() {
    let (w, _, ex) = opening();
    let ack_id = ex.ack_id();
    let archived = |ids: &[&str]| KeyStatus::Retired { archived_ack_ids: ids.iter().map(|s| s.to_string()).collect() };
    let new_ack = synthetic_key("ack-b");
    let p = |status_ack: KeyStatus, status_ch: KeyStatus| policy_with(vec![
        pinned(&w.ch_key, KeyScope::Challenge, status_ch), pinned(&w.ack_key, KeyScope::Ack, status_ack), pinned(&new_ack, KeyScope::Ack, KeyStatus::Active)]);
    // bounded overlap: old and new key both active
    assert!(ex.state(&p(KeyStatus::Active, KeyStatus::Active)).retained().is_some());
    // retired and archived while current: historical evidence still checks
    assert!(ex.state(&p(archived(&[ack_id.as_str()]), archived(&[ack_id.as_str()]))).retained().is_some());
    // retired without an archive record: the signed wall time cannot rescue it
    assert_eq!(unknown_reasons(&ex.state(&p(archived(&[]), KeyStatus::Active))), [Reason::KeyRetiredNotArchived]);
    assert_eq!(unknown_reasons(&ex.state(&p(archived(&[hex64("some-other-ack").as_str()]), KeyStatus::Active))), [Reason::KeyRetiredNotArchived]);
    // a new key the policy has not pinned is not trusted silently
    let w2 = World::with_keys("challenge-a", "ack-c");
    let ex2 = exchange(&w2, &Session::new("rot"), Purpose::Opening, 0, None, 0, 3);
    assert_eq!(unknown_reasons(&ex2.state(&w.policy)), [Reason::UnknownKey]);
}

#[test]
fn trust_policy_rejects_duplicate_or_foreign_keys() {
    let k = synthetic_key("dup");
    let e = qorcap::anchor_seen_at::keys::TrustPolicy::new("t", SERVICE, AUDIENCE, &[POLICY],
        vec![pinned(&k, KeyScope::Ack, KeyStatus::Active), pinned(&k, KeyScope::Challenge, KeyStatus::Active)]);
    assert!(e.is_err());
    let foreign = qorcap::anchor_seen_at::keys::PinnedKey::new(k.verifying_key(), KeyScope::Ack, "other.service", AUDIENCE, KeyStatus::Active);
    assert!(qorcap::anchor_seen_at::keys::TrustPolicy::new("t", SERVICE, AUDIENCE, &[POLICY], vec![foreign]).is_err());
}

#[test]
fn wrong_algorithm_and_wrong_schema_are_rejected() {
    let (w, _, ex) = opening();
    let bytes = resign(&ex, &w.ack_key, |u| u.alg = "Ed25519ph".into());
    assert_eq!(unknown_reasons(&state_of(&ex, &w, &bytes)), [Reason::AlgorithmMismatch]);
    let bytes = resign(&ex, &w.ack_key, |u| u.schema = "proposed.aux.seen_at.ack.v2".into());
    assert_eq!(unknown_reasons(&state_of(&ex, &w, &bytes)), [Reason::WrongSchema]);
}

#[test]
fn malformed_signatures_are_rejected() {
    let (w, _, ex) = opening();
    let good = ex.ack().signature;
    let raw = canon::un_b64url(&good).unwrap();
    use base64::Engine;
    let std_alpha = base64::engine::general_purpose::STANDARD_NO_PAD.encode(&raw);
    let padded = format!("{good}==");
    let mut last_bits = good.clone().into_bytes();
    // the final char of a 64-byte value carries 4 unused bits; flipping one is a non-canonical alias
    let lc = last_bits.last_mut().unwrap();
    *lc = if *lc == b'A' { b'B' } else { b'A' };
    let alias = String::from_utf8(last_bits).unwrap();
    let mut flipped = raw.clone();
    flipped[10] ^= 1;
    let cands = vec![
        padded,
        if std_alpha != good { std_alpha } else { "+".repeat(86) },
        canon::b64url(&raw[..63]),
        String::new(),
        canon::b64url(&flipped),
        alias,
        hex64("x"),
    ];
    for c in cands {
        let bytes = tamper(&ex.ack_bytes, |v| v["signature"] = json!(c));
        assert_eq!(unknown_reasons(&state_of(&ex, &w, &bytes)), [Reason::BadSignature], "{c}");
    }
}

#[test]
fn duplicate_noncanonical_and_number_fields_are_rejected() {
    let (w, _, ex) = opening();
    let text = String::from_utf8(ex.ack_bytes.clone()).unwrap();
    let st = |b: &[u8]| unknown_reasons(&state_of(&ex, &w, b));
    // duplicate key (last-wins parsers would accept the second)
    let dup = text.replacen("{\"ack_id\":", &format!("{{\"ack_id\":\"{}\",\"ack_id\":", hex64("dup")), 1);
    assert_eq!(st(dup.as_bytes()), [Reason::NonCanonical]);
    // whitespace, reordered keys, alternate escape
    assert_eq!(st(format!(" {text}").as_bytes()), [Reason::NonCanonical]);
    let v: Value = serde_json::from_str(&text).unwrap();
    assert_eq!(st(serde_json::to_string_pretty(&v).unwrap().as_bytes()), [Reason::NonCanonical]);
    let reordered = format!("{{\"unsigned_payload\":{},\"signature\":{},\"ack_id\":{}}}", v["unsigned_payload"], v["signature"], v["ack_id"]);
    assert_eq!(st(reordered.as_bytes()), [Reason::NonCanonical]);
    assert_eq!(st(text.replacen("\"schema\"", "\"sch\\u0065ma\"", 1).as_bytes()), [Reason::NonCanonical]);
    // numbers, floats, explicit null for an absent field
    assert_eq!(st(&tamper(&ex.ack_bytes, |v| v["unsigned_payload"]["ledger_seq"] = json!(0))), [Reason::NumberNotAllowed]);
    assert_eq!(st(text.replacen("\"ledger_seq\":\"0\"", "\"ledger_seq\":0.0", 1).as_bytes()), [Reason::NumberNotAllowed]);
    assert_eq!(st(&tamper(&ex.ack_bytes, |v| v["unsigned_payload"]["commitment"]["prior_ack_id"] = Value::Null)), [Reason::NonCanonical]);
    // leading zeros and uppercase hex, re-signed so only the format is wrong
    let lz = resign(&ex, &w.ack_key, |u| u.ledger_seq = "00".into());
    assert_eq!(st(&lz), [Reason::FieldFormat]);
    let up = resign(&ex, &w.ack_key, |u| u.prev_ledger_entry_digest = u.prev_ledger_entry_digest.to_uppercase().replace('0', "A"));
    assert_eq!(st(&up), [Reason::FieldFormat]);
    // invalid UTF-8, truncated, oversized
    assert_eq!(st(&[0xff, 0xfe]), [Reason::Malformed]);
    assert_eq!(st(&ex.ack_bytes[..ex.ack_bytes.len() - 1]), [Reason::Malformed]);
    let big = vec![b' '; canon::MAX_MESSAGE_BYTES + 1];
    assert_eq!(st(&big), [Reason::Oversized]);
}

#[test]
fn decimal_strings_are_strict() {
    assert_eq!(canon::dec_u64("0"), Some(0));
    assert_eq!(canon::dec_u64("18446744073709551615"), Some(u64::MAX));
    for bad in ["", "01", "+1", "-1", "1.0", "1e3", "18446744073709551616", " 1"] { assert_eq!(canon::dec_u64(bad), None, "{bad}"); }
    assert_eq!(canon::dec_i64("-5"), Some(-5));
    for bad in ["-0", "-01", "--1"] { assert_eq!(canon::dec_i64(bad), None, "{bad}"); }
}
