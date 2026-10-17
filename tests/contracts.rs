//! Contract constants vs the vendored QorTime registry (contracts/compat.json, see contracts/CONTRACTS_PIN).
use qorcap::{evidence as ev, inputproof, inputs};
use serde_json::Value;

fn compat() -> Value { serde_json::from_str(include_str!("../contracts/compat.json")).unwrap() }
fn has(v: &Value, k: &str, s: &str) -> bool { v[k].as_array().unwrap().iter().any(|x| x == s) }

#[test]
fn rust_constants_match_qortime_registry() {
    let c = compat();
    assert!(has(&c, "inputs_schemas", inputs::SCHEMA));
    assert!(has(&c, "input_proof_schemas", inputproof::SCHEMA));
    assert!(has(&c, "bundle_schemas", "qorcap-bundle/v0"));
    assert!(include_str!("../src/engine.rs").contains(r#""schema": "qorcap-bundle/v0""#));
    assert_eq!(c["hash_chain"]["genesis"], ev::GENESIS);
    // the recorder this repo builds must be at or past the first tail-timing-sound version
    let v: Vec<u32> = env!("CARGO_PKG_VERSION").split('.').map(|x| x.parse().unwrap()).collect();
    let min: Vec<u32> = c["recorder_versions"]["tail_timing_sound_from"].as_str().unwrap().split('.').map(|x| x.parse().unwrap()).collect();
    assert!(v >= min);
}

#[test]
fn checkpoint_rule_matches_registry() {
    // checkpoint hash = sha256(JCS({seq,t_ms,frames_hash,input_hash,prev})); segment link = sha256(JCS({prev,seq,sha256}))
    let l = ev::link(ev::GENESIS, 0, &"a".repeat(64));
    let want = ev::jcs_sha(&serde_json::json!({"prev": ev::GENESIS, "seq": 0, "sha256": "a".repeat(64)}));
    assert_eq!(l, want);
    assert_eq!(compat()["hash_chain"]["segment_link"], "head_n = sha256(JCS({prev, seq, sha256}))");
}
