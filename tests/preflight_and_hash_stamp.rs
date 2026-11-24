//! Tests for `qorcap preflight` (`src/preflight.rs`) and `qorcap hash-stamp` (`src/hash_stamp.rs`).

use ed25519_dalek::{Signer, SigningKey};
use qorcap::evidence;
use qorcap::hand::device::{DeviceInfo, DeviceModel};
use qorcap::hand::timeline::{Config, HandTimeline};
use qorcap::hand::DeclaredEdgeSettings;
use qorcap::hash_stamp::{
    self, verify_stamp_json, CLAIMS, DOMAIN_STAMP, DOMAIN_STAMP_ID, SCHEMA_STAMP, SERVICE_ID,
    STAMP_FILE,
};
use qorcap::hid::{self, Pad, CROSS};
use qorcap::preflight::{self, PreflightOpts};
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::path::PathBuf;
use std::thread;

fn tmp(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!(
        "qorcap-preflight-stamp-{name}-{}-{:?}",
        std::process::id(),
        std::thread::current().id()
    ));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn golden_stamp_value() -> Value {
    json!({
        "public_key": "d04ab232742bb4ab3a1368bd4615e4e6d0224ab71a016baf8520a332c9778737",
        "schema": "qortime.aux.hash_stamp.v1",
        "sig": "a8eac2cb31f697900a217dee08944021e92669bcf23b784c0f760f7846dcf8882b874a1374bcffc085e1cc090c8d5268f2dd5b0c61ee4ff8d13602058a28560e",
        "stamp_id": "64781e246bba612b3c835ca321465fa66a518ee9c7f0f603be929e7eba1ebcc6",
        "unsigned_payload": {
            "alg": "Ed25519",
            "claims": CLAIMS,
            "client_nonce": "aa".repeat(32),
            "digests": {
                "chunks": 6,
                "frame_chain_head": "44".repeat(32),
                "hand_timeline_head": "22".repeat(32),
                "hand_timeline_sha256": "33".repeat(32),
                "receipt_sha256": "55".repeat(32)
            },
            "issued_wall_ms": 1_764_000_000_000u64,
            "key_id": "10ba682c8ad13513",
            "ledger_seq": 0,
            "prev_ledger_sha256": "00".repeat(32),
            "schema": "qortime.aux.hash_stamp.v1",
            "service": "qortime-aux-hash-stamp"
        }
    })
}

#[test]
fn preflight_checks_storage_and_declared_edge_settings() {
    let d = tmp("preflight");
    let ok = preflight::run(&PreflightOpts {
        bundle_dir: Some(d.clone()),
        hand_dir: Some(d.clone()),
        min_free_gb: 0.0001,
        allow_no_controller: true,
        require_declared_edge: false,
        declared_settings: DeclaredEdgeSettings::default(),
    });
    assert!(ok.pass, "errors={:?}", ok.errors);
    assert_eq!(ok.controller_model, "SKIPPED");

    let low_disk = preflight::run(&PreflightOpts {
        bundle_dir: Some(d.clone()),
        hand_dir: Some(d.clone()),
        min_free_gb: 1_000_000.0,
        allow_no_controller: true,
        require_declared_edge: false,
        declared_settings: DeclaredEdgeSettings::default(),
    });
    assert!(!low_disk.pass);
    assert!(low_disk.errors.iter().any(|e| e.contains("bundle_storage")));

    let missing_edge = preflight::run(&PreflightOpts {
        bundle_dir: Some(d.clone()),
        hand_dir: Some(d.clone()),
        min_free_gb: 0.0001,
        allow_no_controller: true,
        require_declared_edge: true,
        declared_settings: DeclaredEdgeSettings {
            edge_profile: Some("FN+TRIANGLE".into()),
            ..Default::default()
        },
    });
    assert!(!missing_edge.pass);
    assert!(missing_edge.errors.iter().any(|e| e.contains("l2_stop")));

    let full_edge = preflight::run(&PreflightOpts {
        bundle_dir: Some(d),
        hand_dir: None,
        min_free_gb: 0.0001,
        allow_no_controller: true,
        require_declared_edge: true,
        declared_settings: DeclaredEdgeSettings {
            edge_profile: Some("FN+TRIANGLE".into()),
            l2_stop: Some("short".into()),
            r2_stop: Some("short".into()),
            stick_curve_left: Some("default".into()),
            stick_curve_right: Some("dynamic".into()),
            in_game_trigger_effect: Some("off".into()),
        },
    });
    assert!(full_edge.pass, "errors={:?}", full_edge.errors);
}

#[test]
fn verifies_t0037_deterministic_golden_vector_and_rejects_tamper() {
    let stamp = golden_stamp_value();
    let head = "22".repeat(32);
    let sha = "33".repeat(32);
    let fhead = "44".repeat(32);
    let rsha = "55".repeat(32);

    let ok = verify_stamp_json(
        &stamp,
        Some(&head),
        Some(&sha),
        Some(6),
        Some(&fhead),
        Some(&rsha),
        Some("10ba682c8ad13513"),
    )
    .expect("golden vector verifies");
    assert_eq!(ok.key_trusted, Some(true));
    assert_eq!(ok.stamp_id, stamp["stamp_id"].as_str().unwrap());

    let ok_pub = verify_stamp_json(
        &stamp,
        Some(&head),
        Some(&sha),
        Some(6),
        None,
        None,
        Some("d04ab232742bb4ab3a1368bd4615e4e6d0224ab71a016baf8520a332c9778737"),
    )
    .expect("64-hex public_key trust pin verifies");
    assert_eq!(ok_pub.key_trusted, Some(true));

    let mut bad_sig = stamp.clone();
    let flipped = if bad_sig["sig"].as_str().unwrap().starts_with('a') { "b" } else { "a" };
    bad_sig["sig"] = json!(format!("{flipped}{}", &bad_sig["sig"].as_str().unwrap()[1..]));
    assert!(verify_stamp_json(&bad_sig, None, None, None, None, None, None).is_err());

    let mut bad_extra = stamp.clone();
    bad_extra["unsigned_payload"]["digests"]["pad_id"] = json!("leak");
    assert!(verify_stamp_json(&bad_extra, None, None, None, None, None, None).is_err());

    assert!(verify_stamp_json(&stamp, Some(&"ff".repeat(32)), None, None, None, None, None).is_err());
    assert!(verify_stamp_json(&stamp, None, None, None, None, None, Some("0000000000000000")).is_err());
}

#[test]
fn end_to_end_loopback_stamp_and_offline_verify() {
    let dir = tmp("e2e-stamp");
    let dev = DeviceInfo {
        model: DeviceModel::DualSenseEdge,
        vid: 0x054C,
        pid: 0x0DF2,
        bcd_device: 0x0408,
        pad_id: "serial:EDGE_STAMP_TEST".into(),
    };
    let mut tl = HandTimeline::open(Config {
        dir: dir.clone(),
        hand_dir_source: "direct".into(),
        hand_dir_fallback_reason: None,
        raw_usb: true,
        raw_min_free_bytes: None,
        wall_anchor_ms: 1_764_000_000_000,
        qpc: None,
        frame_clock_on: false,
        initial_device: Some(dev),
        start_t_us: 0,
        declared_settings: DeclaredEdgeSettings::default(),
    })
    .unwrap();

    let mut p = Pad::neutral();
    for step in 0..3 {
        p.counter = step as u8;
        p.buttons = if step == 1 { CROSS } else { 0 };
        let rep = hid::pack_usb(&p);
        tl.on_report(10_000 + step * 500_000, None, &rep);
    }
    let _fin = tl.finish(2_200_000).unwrap();

    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let sk = SigningKey::from_bytes(&[0x11u8; 32]);
    let pk_bytes = sk.verifying_key().to_bytes();
    let pub_hex = evidence::hex(&pk_bytes);
    let key_id = evidence::sha256_hex(&pk_bytes)[..16].to_string();

    let server = thread::spawn(move || {
        let (stream, _) = listener.accept().unwrap();
        let mut reader = BufReader::new(stream);
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        let mut cl = 0usize;
        loop {
            line.clear();
            let n = reader.read_line(&mut line).unwrap();
            if n == 0 || line == "\r\n" || line == "\n" {
                break;
            }
            if let Some((k, v)) = line.split_once(':') {
                if k.trim().eq_ignore_ascii_case("content-length") {
                    cl = v.trim().parse().unwrap();
                }
            }
        }
        let mut body = vec![0u8; cl];
        reader.read_exact(&mut body).unwrap();
        let req: Value = serde_json::from_slice(&body).unwrap();
        let unsigned = json!({
            "alg": "Ed25519",
            "claims": CLAIMS,
            "client_nonce": req["client_nonce"],
            "digests": req["digests"],
            "issued_wall_ms": 1_764_000_000_000u64,
            "key_id": key_id,
            "ledger_seq": 0,
            "prev_ledger_sha256": evidence::GENESIS,
            "schema": SCHEMA_STAMP,
            "service": SERVICE_ID,
        });
        let u_canon = evidence::jcs(&unsigned).unwrap();
        let stamp_id = evidence::sha256_hex(&hash_stamp::frame(DOMAIN_STAMP_ID, u_canon.as_bytes()));
        let sig_bytes = sk.sign(&hash_stamp::frame(DOMAIN_STAMP, u_canon.as_bytes())).to_bytes();
        let stamp = json!({
            "public_key": pub_hex,
            "schema": SCHEMA_STAMP,
            "sig": evidence::hex(&sig_bytes),
            "stamp_id": stamp_id,
            "unsigned_payload": unsigned,
        });
        let resp_bytes = evidence::jcs(&stamp).unwrap();
        let mut stream = reader.into_inner();
        let hdr = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
            resp_bytes.len()
        );
        stream.write_all(hdr.as_bytes()).unwrap();
        stream.write_all(resp_bytes.as_bytes()).unwrap();
        stream.flush().unwrap();
    });

    let url = format!("http://{addr}");
    let nonce = "bb".repeat(32);
    let stamped = hash_stamp::run_stamp(
        &dir,
        Some(&url),
        Some("10ba682c8ad13513"),
        None,
        false,
        false,
        Some(&nonce),
    )
    .expect("loopback stamp succeeds");
    server.join().unwrap();

    assert!(dir.join(STAMP_FILE).exists());
    // Refuses overwrite without --force.
    assert!(hash_stamp::run_stamp(&dir, Some(&url), None, None, false, false, Some(&nonce)).is_err());
    // Offline verify passes cleanly.
    let verified = hash_stamp::run_stamp(&dir, None, Some("10ba682c8ad13513"), None, false, true, None)
        .expect("offline verify passes");
    assert_eq!(verified.stamp_id, stamped.stamp_id);

    // Tampering hash_stamp.v1.json fails closed.
    let mut disk_stamp: Value = serde_json::from_str(&std::fs::read_to_string(dir.join(STAMP_FILE)).unwrap()).unwrap();
    disk_stamp["unsigned_payload"]["ledger_seq"] = json!(99);
    std::fs::write(dir.join(STAMP_FILE), serde_json::to_string(&disk_stamp).unwrap()).unwrap();
    assert!(hash_stamp::run_stamp(&dir, None, None, None, false, true, None).is_err());
}
