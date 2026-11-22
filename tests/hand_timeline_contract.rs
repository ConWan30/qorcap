//! Frozen contract tests for `hand_timeline.v0` + `hand_raw.v0.bin` (`schemas/hand_timeline` + `tests/fixtures/hand_timeline`).
//! Verifies every golden fixture hash, `#187` + `#189` layouts, crash-tail vs truncated slice rules,
//! practice-off zero hand keys, absent hand files behavior, LF vs CRLF hash divergence, and `ERRATA-1-mi03.md`.

use qorcap::evidence as ev;
use qorcap::hand::check::check_dir;
use qorcap::hand::device::{gate, Candidate, DeviceModel, Reject};
use serde_json::Value;
use std::path::{Path, PathBuf};

fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests").join("fixtures").join("hand_timeline")
}

#[test]
fn manifest_golden_hashes_and_lf_only() {
    let base = root();
    let manifest: Value = serde_json::from_str(&std::fs::read_to_string(base.join("MANIFEST.json")).unwrap()).unwrap();
    let files = manifest["files"].as_array().expect("manifest files array");
    assert!(files.len() >= 20, "expected >= 20 fixture files");
    for entry in files {
        let rel = entry["path"].as_str().unwrap();
        let want_len = entry["bytes"].as_u64().unwrap() as usize;
        let want_sha = entry["sha256"].as_str().unwrap();
        let bytes = std::fs::read(base.join(rel)).unwrap_or_else(|e| panic!("missing {rel}: {e}"));
        assert_eq!(bytes.len(), want_len, "length mismatch for {rel}");
        assert_eq!(ev::sha256_hex(&bytes), want_sha, "sha256 mismatch for {rel}");
        if rel.ends_with(".json") || rel.ends_with(".jsonl") {
            assert!(!bytes.contains(&b'\r'), "CR byte in LF-pinned fixture {rel}");
        }
    }
}

#[test]
fn current_checker_against_frozen_fixture_pack() {
    let base = root();
    let manifest: Value = serde_json::from_str(&std::fs::read_to_string(base.join("MANIFEST.json")).unwrap()).unwrap();
    let fixtures = manifest["fixtures"].as_object().expect("fixtures map");

    for (name, spec) in fixtures {
        let dir = base.join(name);
        match spec["mode"].as_str().unwrap() {
            "pass" => {
                let rep = check_dir(&dir).unwrap_or_else(|e| panic!("{name}: unexpected Err: {e}"));
                assert!(rep.pass(), "{name}: expected PASS, got errors={:?}", rep.errors);
                assert_eq!(rep.off, spec["off"].as_bool().unwrap(), "{name}: off flag");
                assert_eq!(rep.chunks, spec["chunks"].as_u64().unwrap(), "{name}: chunk count");
                if let Some(h) = spec["head"].as_str() {
                    assert_eq!(rep.head, h, "{name}: head");
                }
                if let Some(s) = spec["file_sha256"].as_str() {
                    assert_eq!(rep.file_sha256, s, "{name}: file_sha256");
                }
                if let Some(fb) = spec["fallback"].as_bool() {
                    assert_eq!(rep.fallback, fb, "{name}: fallback flag");
                }
                if let Some(wmin) = spec["warnings_min"].as_u64() {
                    assert!(rep.warnings.len() >= wmin as usize, "{name}: expected >= {wmin} warnings, got {:?}", rep.warnings);
                }
                if let Some(wc) = spec["warning_contains"].as_str() {
                    assert!(rep.warnings.iter().any(|w| w.contains(wc)), "{name}: expected warning containing {wc:?}, got {:?}", rep.warnings);
                }
            }
            "fail" => {
                let rep = check_dir(&dir).unwrap_or_else(|e| panic!("{name}: expected Ok(FAIL report), got Err: {e}"));
                assert!(!rep.pass(), "{name}: expected FAIL");
                if let Some(emin) = spec["errors_min"].as_u64() {
                    assert!(rep.errors.len() >= emin as usize, "{name}: expected >= {emin} errors");
                }
                if let Some(ec) = spec["error_contains"].as_str() {
                    assert!(rep.errors.iter().any(|e| e.contains(ec)), "{name}: expected error containing {ec:?}, got {:?}", rep.errors);
                }
            }
            "err" => {
                let err = check_dir(&dir).expect_err(&format!("{name}: expected Err"));
                if let Some(ec) = spec["err_contains"].as_str() {
                    assert!(err.contains(ec), "{name}: expected Err containing {ec:?}, got {err:?}");
                }
            }
            other => panic!("unknown fixture mode {other:?}"),
        }
    }
}

#[test]
fn crlf_conversion_changes_hash_and_fails_closed() {
    let src = root().join("189_edge");
    let tmp = std::env::temp_dir().join(format!("qorcap-crlf-contract-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&tmp);
    std::fs::create_dir_all(&tmp).unwrap();
    for name in ["hand_timeline.v0.jsonl", "hand_raw.v0.bin", "hand_timeline_summary.v0.json", "checkpoints.jsonl"] {
        std::fs::copy(src.join(name), tmp.join(name)).unwrap();
    }
    let lf = std::fs::read_to_string(tmp.join("hand_timeline.v0.jsonl")).unwrap();
    let crlf = lf.replace('\n', "\r\n");
    assert_ne!(ev::sha256_hex(lf.as_bytes()), ev::sha256_hex(crlf.as_bytes()));
    std::fs::write(tmp.join("hand_timeline.v0.jsonl"), crlf.as_bytes()).unwrap();

    let rep = check_dir(&tmp).unwrap();
    assert!(!rep.pass(), "CRLF-corrupted JSONL must fail closed");
    assert!(!rep.errors.is_empty());
    let _ = std::fs::remove_dir_all(&tmp);
}

#[test]
fn errata1_mi03_gamepad_collection_accepted() {
    let edge_mi03 = r"\\?\hid#vid_054c&pid_0df2&mi_03#8&1234abcd&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}";
    let ds_mi03 = r"\\?\hid#vid_054c&pid_0ce6&mi_03#8&1234abcd&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}";
    assert_eq!(
        gate(&Candidate { path: edge_mi03.into(), vid: 0x054C, pid: 0x0DF2, bcd_device: 0x0408, usage_page: 1, usage: 5, input_report_len: 64, serial: Some("SYNTH_EDGE_1".into()) }),
        Ok(DeviceModel::DualSenseEdge)
    );
    assert_eq!(
        gate(&Candidate { path: ds_mi03.into(), vid: 0x054C, pid: 0x0CE6, bcd_device: 0x0100, usage_page: 1, usage: 5, input_report_len: 64, serial: Some("SYNTH_DS_1".into()) }),
        Ok(DeviceModel::DualSense)
    );
    let bt_path = r"\\?\hid#{00001124-0000-1000-8000-00805f9b34fb}_vid&0002054c_pid&0df2#9&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}";
    assert!(matches!(
        gate(&Candidate { path: bt_path.into(), vid: 0x054C, pid: 0x0DF2, bcd_device: 0x0408, usage_page: 1, usage: 5, input_report_len: 78, serial: None }),
        Err(Reject::Transport(_))
    ));
}
