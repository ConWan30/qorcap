//! Hand timeline end to end with a scripted ReportSource (no hardware): chunking, the JCS chain, the raw
//! companion files, the DualSense fallback label, the raw off switch, gap / fault accounting, and the checker.

use qorcap::evidence::{self as ev, GENESIS};
use qorcap::frameclock::FrameStamp;
use qorcap::hand::check::check_dir;
use qorcap::hand::device::{DeviceInfo, DeviceModel};
use qorcap::hand::logger;
use qorcap::hand::source::{MockSource, MockStep, OpenOutcome, ReadOutcome, ReportSource};
use qorcap::hand::timeline::{Config, HandTimeline, BYTES_42_43_CHANGE_CAP_PER_EPOCH, CHUNK_US, FILE_NAME, RAW_FILE, RAW_LAYOUT, RAW_RECORD_BYTES, SEAL_GRACE_US, SUMMARY_FILE};
use qorcap::hid::{self, Pad, CIRCLE, CROSS, FN_LEFT, BACK_RIGHT, R2};
use serde_json::Value;
use std::path::PathBuf;
use std::time::{Duration, Instant};

fn tmp(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("qorcap-hand-{name}-{}-{:?}", std::process::id(), std::thread::current().id()));
    let _ = std::fs::remove_dir_all(&d);
    d
}
fn edge() -> DeviceInfo { DeviceInfo { model: DeviceModel::DualSenseEdge, vid: 0x054C, pid: 0x0DF2, bcd_device: 0x0408, pad_id: "serial:EDGE1".into() } }
fn dualsense() -> DeviceInfo { DeviceInfo { model: DeviceModel::DualSense, vid: 0x054C, pid: 0x0CE6, bcd_device: 0x0100, pad_id: "serial:DS1".into() } }
fn cfg(dir: &PathBuf, raw: bool, dev: Option<DeviceInfo>) -> Config {
    Config { dir: dir.clone(), hand_dir_source: "direct".into(), hand_dir_fallback_reason: None, raw_usb: raw, raw_min_free_bytes: None, wall_anchor_ms: 1_795_144_560_000, qpc: None, frame_clock_on: true, initial_device: dev, start_t_us: 0 }
}
fn report(counter: u8, buttons: u32, lx: u8, ts: u32) -> Vec<u8> {
    let mut p = Pad::neutral();
    p.counter = counter; p.buttons = buttons; p.lx = lx; p.sensor_ts = ts;
    hid::pack_usb(&p)
}
fn lines(dir: &PathBuf) -> Vec<Value> {
    std::fs::read_to_string(dir.join(FILE_NAME)).unwrap().lines().map(|l| serde_json::from_str(l).unwrap()).collect()
}

#[test]
fn windows_chain_raw_files_and_the_checker_pass() {
    let dir = tmp("chain");
    let mut tl = HandTimeline::open(cfg(&dir, true, Some(edge()))).unwrap();
    // 2.5 s of reports every 4 ms with one CROSS press and a stick move; one frame batch per 16.667 ms.
    let mut t = 1_000u64;
    let mut c = 0u8;
    while t < 2_500_000 {
        let b = if (600_000..700_000).contains(&t) { CROSS } else { 0 };
        let lx = if t > 1_200_000 { 200 } else { 128 };
        tl.on_report(t, Some(t * 10), &report(c, b, lx, t as u32));
        if t % 16_000 < 4_000 {
            tl.on_frames(&[FrameStamp { file: "video.ts".into(), frame: (t / 16_667) as i64, t_mono_us: t, pts_90k: t * 9 / 100 }]);
        }
        c = c.wrapping_add(1);
        t += 4_000;
    }
    assert_eq!(tl.binding().unwrap().0, 1, "windows seal once the clock passes their end + grace");
    let fin = tl.finish(2_600_000).unwrap();
    assert_eq!((fin.first_seq, fin.last_seq, fin.chunks), (Some(0), Some(2), 3));
    let recs = lines(&dir);
    let chunks: Vec<&Value> = recs.iter().filter(|r| r["type"] == "chunk").collect();
    assert_eq!(chunks.len(), 3);
    assert_eq!(chunks[0]["prev"], GENESIS);
    assert_eq!(chunks[2]["partial"], true);
    assert_eq!(chunks[0]["partial"], false);
    assert_eq!(chunks[0]["device_model"], "dualsense_edge");
    // head_k = SHA-256(JCS({prev, seq, sha256})) re-links by hand.
    assert_eq!(chunks[1]["head"].as_str().unwrap(), ev::link(chunks[0]["head"].as_str().unwrap(), 1, chunks[1]["sha256"].as_str().unwrap()));
    // Raw: ONE append-only session file, 80 B v1 records (u64_le qpc_ticks + u64_le t_us + 64 B report).
    assert_eq!(RAW_RECORD_BYTES, 80);
    let raw_all = std::fs::read(dir.join(RAW_FILE)).unwrap();
    assert_eq!(raw_all.len() % RAW_RECORD_BYTES, 0);
    assert_eq!(fin.raw_file.as_deref(), Some(RAW_FILE));
    assert_eq!(fin.raw_bytes, raw_all.len() as u64);
    assert_eq!(chunks[0]["raw_file"], RAW_FILE);
    assert_eq!(chunks[0]["raw_offset"], 0);
    assert_eq!(chunks[0]["raw_len"], 250 * RAW_RECORD_BYTES as u64);
    assert_eq!(chunks[1]["raw_offset"], 250 * RAW_RECORD_BYTES as u64);
    assert_eq!(chunks[1]["raw_len"], 250 * RAW_RECORD_BYTES as u64);
    assert_eq!(chunks[2]["raw_offset"], 500 * RAW_RECORD_BYTES as u64);
    assert_eq!(&raw_all[0..8], &10_000u64.to_le_bytes(), "qpc_ticks at bytes 0..8");
    assert_eq!(&raw_all[8..16], &1_000u64.to_le_bytes(), "t_us at bytes 8..16");
    assert_eq!(raw_all[16], 0x01, "64 B 0x01 report at bytes 16..80");
    // Change-logged in rows: far fewer than reports, but every button edge is there.
    let ins: Vec<&Value> = recs.iter().filter(|r| r["type"] == "in").collect();
    assert!(ins.len() < 20, "{} in rows", ins.len());
    assert!(ins.iter().any(|r| r["buttons"] == CROSS && r["t_us"] == 601_000), "press edge logged at its report");
    assert!(ins.iter().any(|r| r["buttons"] == 0 && r["t_us"] == 701_000), "release edge logged");
    assert!(ins.iter().all(|r| r["edge_bits"].is_u64()), "Edge bits MEASURED on an Edge");
    let hb0 = recs.iter().find(|r| r["type"] == "hb" && r["chunk_seq"] == 0).unwrap();
    assert_eq!(hb0["rx_delta"], 250);
    assert_eq!(hb0["seq_breaks"], 0);
    assert_eq!(hb0["max_rx_gap_us"], 4_000);
    assert!(hb0["frames_in_chunk"].as_u64().unwrap() > 0);
    let rep = check_dir(&dir).unwrap();
    assert!(rep.pass(), "{:?}", rep.errors);
    assert_eq!(rep.chunks, 3);
    assert_eq!(rep.head, fin.head);
    assert_eq!(rep.file_sha256, fin.file_sha256);
    assert_eq!(rep.raw_files, 1);
    assert_eq!(rep.raw_slices, 3);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn checker_catches_tampered_records_raw_bytes_and_a_broken_link() {
    let dir = tmp("tamper");
    let mut tl = HandTimeline::open(cfg(&dir, true, Some(edge()))).unwrap();
    for i in 0..600u64 { tl.on_report(i * 4_000, None, &report(i as u8, if i % 50 == 0 { CROSS } else { 0 }, 128, i as u32)); }
    tl.finish(2_500_000).unwrap();
    assert!(check_dir(&dir).unwrap().pass());
    let path = dir.join(FILE_NAME);
    let orig = std::fs::read_to_string(&path).unwrap();
    // 1) change one in row's stick value
    let bad = orig.replacen("\"lx\":128", "\"lx\":129", 1);
    std::fs::write(&path, &bad).unwrap();
    let r = check_dir(&dir).unwrap();
    assert!(r.errors.iter().any(|e| e.contains("records SHA-256 does not match")), "{:?}", r.errors);
    std::fs::write(&path, &orig).unwrap();
    // 2) flip one raw byte in the single append-only raw file
    let rp = dir.join(RAW_FILE);
    let mut raw = std::fs::read(&rp).unwrap();
    raw[RAW_RECORD_BYTES * 250 + 20] ^= 1;
    std::fs::write(&rp, &raw).unwrap();
    let r = check_dir(&dir).unwrap();
    assert!(r.errors.iter().any(|e| e.contains("raw slice SHA-256 does not match")), "{:?}", r.errors);
    raw[5] ^= 1;
    std::fs::write(&rp, &raw).unwrap();
    // 3) drop a whole window (chain must break)
    let mut out = vec![];
    for l in orig.lines() {
        let v: Value = serde_json::from_str(l).unwrap();
        let in_window_1 = v["chunk_seq"] == 1 || (v["type"] == "chunk" && v["seq"] == 1);
        if !in_window_1 { out.push(l); }
    }
    std::fs::write(&path, out.join("\n") + "\n").unwrap();
    let r = check_dir(&dir).unwrap();
    assert!(!r.pass());
    assert!(r.errors.iter().any(|e| e.contains("expected seq 1") || e.contains("prev does not match")), "{:?}", r.errors);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn standard_dualsense_is_a_labelled_fallback_with_edge_fields_unmeasured() {
    let dir = tmp("fallback");
    let mut tl = HandTimeline::open(cfg(&dir, true, Some(dualsense()))).unwrap();
    // FN_LEFT + CROSS + BACK_RIGHT bits set in the wire bytes: on a standard DualSense they are not reported.
    for i in 0..300u64 { tl.on_report(i * 4_000, None, &report(i as u8, CROSS | FN_LEFT | BACK_RIGHT, 128, i as u32)); }
    tl.finish(1_300_000).unwrap();
    let recs = lines(&dir);
    let header = &recs[0];
    assert_eq!(header["type"], "header");
    assert_eq!(header["device_model"], "dualsense");
    assert_eq!(header["edge_fields"], "UNMEASURED");
    let st = recs.iter().find(|r| r["type"] == "status" && r["state"] == "connected").unwrap();
    assert_eq!((st["device_model"].as_str(), st["fallback"].as_bool(), st["edge_fields"].as_str()), (Some("dualsense"), Some(true), Some("UNMEASURED")));
    for r in recs.iter().filter(|r| r["type"] == "in") {
        assert_eq!(r["edge_bits"], "UNMEASURED");
        assert_eq!(r["buttons"], CROSS, "Edge-only bits are not written as false or zero, they are left out and marked UNMEASURED");
        assert_eq!(r["device_model"], "dualsense");
    }
    assert!(!recs.iter().any(|r| r["type"] == "profile_swap_candidate"));
    for c in recs.iter().filter(|r| r["type"] == "chunk") { assert_eq!((c["device_model"].as_str(), c["fallback"].as_bool()), (Some("dualsense"), Some(true))); }
    let rep = check_dir(&dir).unwrap();
    assert!(rep.pass() && rep.fallback, "{:?}", rep.errors);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn edge_profile_swap_candidate_is_estimated_and_the_profile_stays_unknown() {
    let dir = tmp("swap");
    let mut tl = HandTimeline::open(cfg(&dir, false, Some(edge()))).unwrap();
    tl.on_report(10_000, None, &report(0, FN_LEFT, 128, 1));
    tl.on_report(14_000, None, &report(1, FN_LEFT | CROSS, 128, 2));
    tl.on_report(18_000, None, &report(2, FN_LEFT | CROSS, 128, 3));
    tl.finish(100_000).unwrap();
    let recs = lines(&dir);
    let sw: Vec<&Value> = recs.iter().filter(|r| r["type"] == "profile_swap_candidate").collect();
    assert_eq!(sw.len(), 1, "rising edge only");
    assert_eq!((sw[0]["fn_bit"].as_str(), sw[0]["face_bit"].as_str(), sw[0]["resulting_profile"].as_str(), sw[0]["status"].as_str()),
        (Some("FN_L"), Some("CROSS"), Some("UNKNOWN"), Some("ESTIMATED")));
    assert!(check_dir(&dir).unwrap().pass());
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn raw_off_switch_is_recorded_and_writes_no_raw_files() {
    let dir = tmp("rawoff");
    let mut tl = HandTimeline::open(cfg(&dir, false, Some(edge()))).unwrap();
    for i in 0..400u64 { tl.on_report(i * 4_000, None, &report(i as u8, 0, 128, i as u32)); }
    tl.finish(1_700_000).unwrap();
    let recs = lines(&dir);
    assert_eq!(recs[0]["raw_usb"], false);
    assert!(recs.iter().filter(|r| r["type"] == "raw_bind").all(|r| r["raw_usb"] == false && r["file"].is_null() && r["offset"].is_null() && r["len"].is_null() && r["sha256"].is_null()));
    assert!(recs.iter().filter(|r| r["type"] == "chunk").all(|r| r["raw_file"].is_null() && r["raw_offset"].is_null() && r["raw_len"].is_null() && r["raw_sha256"].is_null()));
    assert!(!dir.join(RAW_FILE).exists());
    assert!(check_dir(&dir).unwrap().pass());
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn faults_gaps_and_late_frames_are_counted_not_filled() {
    let dir = tmp("faults");
    let mut tl = HandTimeline::open(cfg(&dir, true, Some(edge()))).unwrap();
    tl.on_report(100_000, None, &report(0, 0, 128, 1));
    tl.on_report(104_000, None, &report(5, 0, 128, 1)); // counter jump, hw stall
    tl.on_report(105_000, None, &[0x01; 10]); // short read
    tl.on_report(106_000, None, &{ let mut b = vec![0u8; 78]; b[0] = 0x31; b }); // Bluetooth report on the USB stream
    tl.on_timeout(206_000);
    tl.on_report(300_000, None, &report(6, 0, 128, 2)); // 196 ms gap
    tl.on_timeout(2_500_000); // > 2 s without reports on an open handle -> stalled
    tl.tick(1_000_000 + SEAL_GRACE_US + 1); // window 0 already sealed by the timeout above
    tl.on_frames(&[FrameStamp { file: "video.ts".into(), frame: 1, t_mono_us: 900_000, pts_90k: 81_000 }]); // late
    tl.finish(2_600_000).unwrap();
    let recs = lines(&dir);
    let hb0 = recs.iter().find(|r| r["type"] == "hb" && r["chunk_seq"] == 0).unwrap();
    assert_eq!((hb0["rx_delta"].as_u64(), hb0["short_reads"].as_u64(), hb0["parse_faults"].as_u64(), hb0["bt_reports"].as_u64()), (Some(3), Some(1), Some(1), Some(1)));
    assert_eq!((hb0["seq_breaks"].as_u64(), hb0["hw_stalls"].as_u64(), hb0["timeouts"].as_u64()), (Some(1), Some(1), Some(1)));
    assert_eq!(hb0["max_rx_gap_us"], 196_000);
    assert_eq!(hb0["stale_over_50ms"], 1);
    assert_eq!(hb0["raw_reports"], 3, "short and Bluetooth reads are discarded, never padded");
    assert!(recs.iter().any(|r| r["type"] == "status" && r["state"] == "stalled"));
    let late = recs.iter().find(|r| r["type"] == "frame").unwrap();
    assert_eq!(late["late"], true);
    assert!(late["chunk_seq"].as_u64().unwrap() >= 1);
    assert!(check_dir(&dir).unwrap().pass(), "{:?}", check_dir(&dir).unwrap().errors);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn empty_windows_seal_with_the_empty_set_and_no_pad_is_marked() {
    let dir = tmp("empty");
    let mut tl = HandTimeline::open(cfg(&dir, true, None)).unwrap();
    tl.on_status(0, "missing", "no pad");
    tl.tick(3 * CHUNK_US + SEAL_GRACE_US);
    tl.finish(3 * CHUNK_US + SEAL_GRACE_US).unwrap();
    let recs = lines(&dir);
    let c1 = recs.iter().find(|r| r["type"] == "chunk" && r["seq"] == 1).unwrap();
    assert_eq!(c1["device_model"], "none");
    assert_eq!(c1["health"]["pad_connected"], false);
    assert_eq!(c1["n"], 2, "hb + raw_bind only");
    assert_eq!(recs[0]["device_model"], "UNKNOWN");
    assert!(check_dir(&dir).unwrap().pass());
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn refuses_to_overwrite_an_existing_timeline() {
    let dir = tmp("exists");
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join(FILE_NAME), "keep\n").unwrap();
    assert!(HandTimeline::open(cfg(&dir, true, None)).is_err());
    assert_eq!(std::fs::read_to_string(dir.join(FILE_NAME)).unwrap(), "keep\n");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn logger_thread_with_a_mock_source_reconnects_as_a_new_epoch_and_finishes() {
    let dir = tmp("logger");
    let clock = Instant::now();
    let mut opens = 0u32;
    let open = move || {
        opens += 1;
        match opens {
            1 => OpenOutcome::Missing("no pad yet".into()),
            2 => {
                let mut steps: Vec<MockStep> = (0..50u8).map(|i| MockStep::Report(report(i, if i == 10 { R2 } else { 0 }, 128, u32::from(i)))).collect();
                steps.push(MockStep::Disconnect("Win32 error 1167".into()));
                logger::opened(Box::new(MockSource::new(steps)), edge())
            }
            3 => logger::opened(Box::new(MockSource::new((0..20u8).map(|i| MockStep::Report(report(i, 0, 128, u32::from(i)))))), dualsense()),
            _ => OpenOutcome::Missing("gone".into()),
        }
    };
    let mut c = cfg(&dir, true, None);
    c.start_t_us = clock.elapsed().as_micros() as u64;
    let h = logger::spawn(c, clock, open, || None).unwrap();
    h.frames(vec![FrameStamp { file: "video.ts".into(), frame: 0, t_mono_us: clock.elapsed().as_micros() as u64, pts_90k: 0 }]);
    let end = Instant::now() + Duration::from_secs(10);
    while h.binding().is_none() && Instant::now() < end { std::thread::sleep(Duration::from_millis(20)); }
    assert!(h.binding().is_some(), "a window sealed while running");
    let fin = h.finish(Duration::from_secs(5)).unwrap();
    assert_eq!(h.finish(Duration::from_secs(1)).unwrap(), fin, "finish is idempotent");
    let recs = lines(&dir);
    let states: Vec<&str> = recs.iter().filter(|r| r["type"] == "status").filter_map(|r| r["state"].as_str()).collect();
    assert_eq!(&states[..4], &["missing", "connected", "unplugged", "connected"], "{states:?}");
    let epochs: Vec<u64> = recs.iter().filter(|r| r["type"] == "status" && r["state"] == "connected").filter_map(|r| r["epoch"].as_u64()).collect();
    assert_eq!(epochs, vec![1, 2]);
    assert!(recs.iter().any(|r| r["type"] == "frame"));
    let rep = check_dir(&dir).unwrap();
    assert!(rep.pass(), "{:?}", rep.errors);
    assert_eq!(rep.head, fin.head);
    assert!(rep.device_models.contains(&"dualsense".to_string()) || rep.fallback);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn check_treats_an_off_session_as_a_pass_not_a_failure() {
    let dir = tmp("check-off");
    std::fs::create_dir_all(&dir).unwrap();
    // Durable off flag on a stop checkpoint (the engine's extra `qorcap` key). No hand file.
    let cp = ev::jcs(&serde_json::json!({
        "seq": 0, "t_ms": 1, "frames_hash": "00".repeat(32), "input_hash": null, "prev": ev::GENESIS,
        "qorcap": {"trigger": "stop", "hand_timeline": false}
    })).unwrap();
    // The real checkpoint hash is not needed: the hand checker only reads the qorcap extras.
    std::fs::write(dir.join("checkpoints.jsonl"), format!("{cp}\n")).unwrap();
    let rep = check_dir(&dir).unwrap();
    assert!(rep.pass() && rep.off, "off session must not be treated as tampered or failed: {:?}", rep.errors);
    let (lines, code) = qorcap::hand::check::report_lines(&rep);
    assert_eq!(code, 0);
    assert!(lines[0].contains("off for this session"), "{lines:?}");
    // A dir with no file and no off flag is still an error (not silently passed).
    let empty = tmp("check-missing");
    std::fs::create_dir_all(&empty).unwrap();
    let err = check_dir(&empty).unwrap_err();
    assert!(err.contains("not found"), "{err}");
    let _ = std::fs::remove_dir_all(&dir);
    let _ = std::fs::remove_dir_all(&empty);
}

fn spawn_failsoft(name: &str, open: impl Fn() -> OpenOutcome + Send + 'static) -> (PathBuf, qorcap::hand::timeline::Finish, Vec<Value>) {
    let dir = tmp(&format!("failsoft-{name}"));
    let clock = Instant::now();
    let mut c = cfg(&dir, true, None);
    c.start_t_us = 0;
    let h = logger::spawn(c, clock, open, || None).unwrap();
    assert_eq!(h.live().state, name, "{name} is visible on the live status at once");
    let end = Instant::now() + Duration::from_secs(5);
    while h.binding().is_none() && Instant::now() < end { std::thread::sleep(Duration::from_millis(20)); }
    let fin = h.finish(Duration::from_secs(5)).unwrap();
    let recs = lines(&dir);
    (dir, fin, recs)
}

#[test]
fn missing_or_rejected_device_is_recorded_and_never_blocks_the_session() {
    let (dir, fin, recs) = spawn_failsoft("missing", || OpenOutcome::Missing("no DualSense on wired USB".into()));
    assert!(fin.chunks >= 1, "missing still seals so capture can stop");
    assert!(recs.iter().any(|r| r["type"] == "status" && r["state"] == "missing"), "{recs:?}");
    assert!(recs.iter().any(|r| r["type"] == "hb" && r["pad_connected"] == false));
    assert!(check_dir(&dir).unwrap().pass());
    let _ = std::fs::remove_dir_all(&dir);

    let (dir, fin, recs) = spawn_failsoft("rejected_transport", || OpenOutcome::Rejected {
        state: "rejected_transport", detail: "dualsense_edge over Bluetooth; wired USB only".into(),
    });
    assert!(fin.chunks >= 1, "rejected still seals so capture can stop");
    assert!(recs.iter().any(|r| r["type"] == "status" && r["state"] == "rejected_transport"), "{recs:?}");
    assert!(recs.iter().any(|r| r["type"] == "hb" && r["pad_connected"] == false));
    assert!(check_dir(&dir).unwrap().pass());
    let _ = std::fs::remove_dir_all(&dir);
}


#[test]
fn clock_rollback_is_a_first_class_status() {
    let dir = tmp("rollback");
    let mut tl = HandTimeline::open(cfg(&dir, false, Some(edge()))).unwrap();
    tl.on_report(10_000, Some(1), &report(0, 0, 128, 1));
    tl.on_report(5_000, Some(2), &report(1, 0, 128, 2));
    assert_eq!(tl.live().state, "clock_rollback");
    let fin = tl.finish(CHUNK_US + SEAL_GRACE_US + 1).unwrap();
    let recs = lines(&dir);
    assert!(recs.iter().any(|r| r["type"] == "status" && r["state"] == "clock_rollback"), "{recs:?}");
    assert!(fin.chunks >= 1);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn refuses_to_overwrite_an_existing_raw_companion() {
    let dir = tmp("raw-exists");
    std::fs::create_dir_all(&dir).unwrap();
    let p = dir.join(RAW_FILE);
    std::fs::write(&p, b"KEEP-ME-DO-NOT-OVERWRITE").unwrap();
    let err = match HandTimeline::open(cfg(&dir, true, Some(edge()))) {
        Ok(_) => panic!("expected open to refuse pre-existing raw file"),
        Err(e) => e,
    };
    assert!(err.contains("hand raw file") || err.contains("already exists") || err.contains(RAW_FILE), "{err}");
    assert_eq!(std::fs::read(&p).unwrap(), b"KEEP-ME-DO-NOT-OVERWRITE");
    assert!(!dir.join(FILE_NAME).exists(), "partial jsonl cleaned up when raw create_new fails");
    let _ = std::fs::remove_dir_all(&dir);
}

#[cfg(unix)]
#[test]
fn hand_files_are_mode_0600_on_unix() {
    use std::os::unix::fs::PermissionsExt;
    let dir = tmp("mode");
    let mut tl = HandTimeline::open(cfg(&dir, true, Some(edge()))).unwrap();
    tl.on_report(1_000, Some(10), &report(0, 0, 128, 1));
    tl.finish(CHUNK_US + SEAL_GRACE_US + 1).unwrap();
    let mode = |p: &std::path::Path| std::fs::metadata(p).unwrap().permissions().mode() & 0o777;
    assert_eq!(mode(&dir.join(FILE_NAME)), 0o600, "jsonl");
    assert_eq!(mode(&dir.join(RAW_FILE)), 0o600, "raw companion");
    assert_eq!(mode(&dir.join(SUMMARY_FILE)), 0o600, "summary");
    let _ = std::fs::remove_dir_all(&dir);
}

struct SlowSource;
impl ReportSource for SlowSource {
    fn read(&mut self, _buf: &mut [u8], _timeout_ms: u32) -> ReadOutcome {
        std::thread::sleep(Duration::from_millis(400));
        ReadOutcome::Timeout
    }
}

#[test]
fn bounded_frame_queue_counts_drops_and_never_blocks_capture() {
    let dir = tmp("drops");
    let clock = Instant::now();
    let mut c = cfg(&dir, false, None);
    c.start_t_us = 0;
    let h = logger::spawn_with_queue(c, clock, || logger::opened(Box::new(SlowSource), edge()), || None, 1).unwrap();
    let stamp = || FrameStamp { file: "video.ts".into(), frame: 0, t_mono_us: clock.elapsed().as_micros() as u64, pts_90k: 0 };
    let start = Instant::now();
    const N: usize = 64;
    const BATCH: usize = 3;
    for _ in 0..N {
        h.frames(vec![stamp(), stamp(), stamp()]);
    }
    let elapsed = start.elapsed();
    assert!(elapsed < Duration::from_millis(200), "capture must not block on a full queue: {elapsed:?}");
    let drops_after_pump = h.frame_drops();
    assert!(drops_after_pump >= BATCH as u64, "cap=1 must drop some of {N} batches of {BATCH}: {drops_after_pump}");
    assert_eq!(h.live().frame_drops, drops_after_pump);
    let fin = h.finish(Duration::from_secs(5)).unwrap();
    assert_eq!(fin.frame_drops, h.frame_drops(), "finish samples remaining drops into the timeline");
    assert!(fin.frame_drops >= drops_after_pump);
    let summary: Value = serde_json::from_str(&std::fs::read_to_string(dir.join(SUMMARY_FILE)).unwrap()).unwrap();
    assert_eq!(summary["frame_drops"], fin.frame_drops);
    let recs = lines(&dir);
    let in_windows: u64 = recs.iter().filter(|r| r["type"] == "hb").filter_map(|r| r["frame_drops"].as_u64()).sum();
    assert_eq!(in_windows, fin.frame_drops, "hb.frame_drops must sum to the session total");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn open_denied_is_not_recorded_as_missing() {
    let (dir, fin, recs) = spawn_failsoft("open_denied", || OpenOutcome::Rejected {
        state: "open_denied", detail: "GENERIC_READ open failed (win32 5)".into(),
    });
    assert!(fin.chunks >= 1, "open_denied still seals so capture can stop");
    assert!(recs.iter().any(|r| r["type"] == "status" && r["state"] == "open_denied"), "{recs:?}");
    assert!(recs.iter().all(|r| r["type"] != "status" || r["state"] != "missing"), "must not be labelled missing: {recs:?}");
    assert!(recs.iter().any(|r| r["type"] == "hb" && r["pad_connected"] == false));
    assert!(check_dir(&dir).unwrap().pass());
    let _ = std::fs::remove_dir_all(&dir);
}

fn rehash_timeline_for_test(dir: &PathBuf, mut edit: impl FnMut(&mut Value)) {
    let path = dir.join(FILE_NAME);
    let rows: Vec<Value> = std::fs::read_to_string(&path)
        .unwrap()
        .lines()
        .filter(|l| !l.is_empty())
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    let mut rebuilt = String::new();
    let mut pending: Vec<Value> = Vec::new();
    let mut head = GENESIS.to_string();
    for mut row in rows {
        edit(&mut row);
        if row["type"] != "chunk" {
            pending.push(row);
            continue;
        }
        let seq = row["seq"].as_u64().unwrap();
        let recs = std::mem::take(&mut pending);
        let sha = ev::try_jcs_sha(&Value::Array(recs.clone())).unwrap();
        let new_head = ev::link(&head, seq, &sha);
        row["sha256"] = serde_json::json!(sha);
        row["prev"] = serde_json::json!(head);
        row["head"] = serde_json::json!(new_head);
        head = new_head;
        for r in recs.iter().chain(std::iter::once(&row)) {
            rebuilt.push_str(&ev::jcs(r).unwrap());
            rebuilt.push('\n');
        }
    }
    std::fs::write(&path, rebuilt).unwrap();
}

#[test]
fn imu_noise_does_not_create_in_rows_while_sticks_triggers_buttons_paddles_do() {
    let dir = tmp("imu-noise");
    let mut tl = HandTimeline::open(cfg(&dir, true, Some(edge()))).unwrap();
    // 250 reports across 1 s where gyro, accel, sensor_ts, and counter change every 4 ms,
    // while sticks, triggers, buttons, and paddles stay idle.
    for i in 0..250u64 {
        let mut p = Pad::neutral();
        p.counter = i as u8;
        p.sensor_ts = (i as u32) * 1_000;
        p.gyro = [(i as i16) * 3, -(i as i16) * 5, (i as i16) * 7];
        p.accel = [100 + (i as i16), -200 - (i as i16), 8192 + (i as i16)];
        tl.on_report(10_000 + i * 4_000, Some(100_000 + i * 40_000), &hid::pack_usb(&p));
    }
    // Now change stick, trigger, face button, and Edge paddle past ANALOG_MIN_GAP_US.
    let mut p_stick = Pad::neutral();
    p_stick.counter = 250;
    p_stick.lx = 210;
    tl.on_report(1_100_000, Some(11_000_000), &hid::pack_usb(&p_stick));

    let mut p_trig = p_stick.clone();
    p_trig.counter = 251;
    p_trig.l2 = 180;
    tl.on_report(1_200_000, Some(12_000_000), &hid::pack_usb(&p_trig));

    let mut p_btn = p_trig.clone();
    p_btn.counter = 252;
    p_btn.buttons = CROSS;
    tl.on_report(1_210_000, Some(12_100_000), &hid::pack_usb(&p_btn));

    let mut p_pad = p_btn.clone();
    p_pad.counter = 253;
    p_pad.buttons = CROSS | BACK_RIGHT;
    tl.on_report(1_220_000, Some(12_200_000), &hid::pack_usb(&p_pad));

    let fin = tl.finish(2_500_000).unwrap();
    let recs = lines(&dir);
    let ins: Vec<&Value> = recs.iter().filter(|r| r["type"] == "in").collect();
    // 1 initial row during the 250 noisy IMU frames + 4 input changes = 5 `in` rows total.
    assert_eq!(ins.len(), 5, "IMU noise must not create extra in rows: {ins:?}");
    assert_eq!(fin.raw_bytes, 254 * RAW_RECORD_BYTES as u64, "raw file still holds all 254 reports");
    assert!(check_dir(&dir).unwrap().pass());
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn checker_rejects_truncation_missing_raw_offset_gap_overlap_layout_and_t_us_rollback() {
    let make_valid = |tag: &str| -> PathBuf {
        let dir = tmp(tag);
        let mut tl = HandTimeline::open(cfg(&dir, true, Some(edge()))).unwrap();
        for i in 0..550u64 {
            let b = if i % 100 == 0 { CROSS } else { 0 };
            tl.on_report(1_000 + i * 4_000, Some(10_000 + i * 40_000), &report(i as u8, b, 128, i as u32));
        }
        tl.finish(2_500_000).unwrap();
        assert!(check_dir(&dir).unwrap().pass());
        dir
    };

    // 1. Truncated raw file
    let d_trunc = make_valid("v1-trunc");
    let rp = d_trunc.join(RAW_FILE);
    let orig = std::fs::read(&rp).unwrap();
    std::fs::write(&rp, &orig[..orig.len() - 12]).unwrap();
    let rep = check_dir(&d_trunc).unwrap();
    assert!(rep.errors.iter().any(|e| e.contains("truncated (slice") || e.contains("exceeds file length")), "{:?}", rep.errors);

    // 2. Missing raw file
    let d_miss = make_valid("v1-miss");
    std::fs::remove_file(d_miss.join(RAW_FILE)).unwrap();
    let rep = check_dir(&d_miss).unwrap();
    assert!(rep.raw_missing);
    assert!(rep.errors.iter().any(|e| e.contains("missing")), "{:?}", rep.errors);

    // 3. Offset gap (re-linked JSONL chain)
    let d_gap = make_valid("v1-gap");
    rehash_timeline_for_test(&d_gap, |row| {
        if row["type"] == "raw_bind" && row["chunk_seq"] == 1 {
            row["offset"] = serde_json::json!(row["offset"].as_u64().unwrap() + RAW_RECORD_BYTES as u64);
        } else if row["type"] == "chunk" && row["seq"] == 1 {
            row["raw_offset"] = serde_json::json!(row["raw_offset"].as_u64().unwrap() + RAW_RECORD_BYTES as u64);
        }
    });
    let rep = check_dir(&d_gap).unwrap();
    assert!(rep.errors.iter().any(|e| e.contains("leaves a gap")), "{:?}", rep.errors);

    // 4. Offset overlap (re-linked JSONL chain)
    let d_ov = make_valid("v1-overlap");
    rehash_timeline_for_test(&d_ov, |row| {
        if row["type"] == "raw_bind" && row["chunk_seq"] == 1 {
            row["offset"] = serde_json::json!(row["offset"].as_u64().unwrap() - RAW_RECORD_BYTES as u64);
        } else if row["type"] == "chunk" && row["seq"] == 1 {
            row["raw_offset"] = serde_json::json!(row["raw_offset"].as_u64().unwrap() - RAW_RECORD_BYTES as u64);
        }
    });
    let rep = check_dir(&d_ov).unwrap();
    assert!(rep.errors.iter().any(|e| e.contains("overlaps")), "{:?}", rep.errors);

    // 5. Wrong/v0 layout (72 B / v0 rejected by check-hand)
    let d_lay = make_valid("v1-bad-layout");
    rehash_timeline_for_test(&d_lay, |row| {
        if row["type"] == "header" {
            row["raw_layout"] = serde_json::json!("v0");
            row["raw_record_bytes"] = serde_json::json!(72);
        }
    });
    let rep = check_dir(&d_lay).unwrap();
    assert!(rep.errors.iter().any(|e| e.contains("unsupported raw layout")), "{:?}", rep.errors);

    // 6. Backwards t_us inside raw file (with matching slice sha256 + re-linked chain)
    let d_tus = make_valid("v1-t-backwards");
    let mut raw_bytes = std::fs::read(d_tus.join(RAW_FILE)).unwrap();
    // Overwrite record 1's t_us (bytes 80+8..80+16) to 500 (< record 0's 1_000).
    let rec1_off = RAW_RECORD_BYTES;
    raw_bytes[rec1_off + 8..rec1_off + 16].copy_from_slice(&500u64.to_le_bytes());
    std::fs::write(d_tus.join(RAW_FILE), &raw_bytes).unwrap();
    let chunk0_len = 250 * RAW_RECORD_BYTES;
    let new_sha0 = ev::sha256_hex(&raw_bytes[..chunk0_len]);
    rehash_timeline_for_test(&d_tus, |row| {
        if row["type"] == "raw_bind" && row["chunk_seq"] == 0 {
            row["sha256"] = serde_json::json!(new_sha0);
        } else if row["type"] == "chunk" && row["seq"] == 0 {
            row["raw_sha256"] = serde_json::json!(new_sha0);
        }
    });
    let rep = check_dir(&d_tus).unwrap();
    assert!(rep.errors.iter().any(|e| e.contains("decreases below previous")), "{:?}", rep.errors);

    for d in [d_trunc, d_miss, d_gap, d_ov, d_lay, d_tus] {
        let _ = std::fs::remove_dir_all(&d);
    }
}

#[test]
fn checker_resolves_off_bundle_hand_dir_from_checkpoints() {
    let bundle = tmp("off-bundle-b");
    let hand = tmp("off-bundle-h");
    std::fs::create_dir_all(&bundle).unwrap();
    let mut c = cfg(&hand, true, Some(edge()));
    c.hand_dir_source = "override".into();
    let mut tl = HandTimeline::open(c).unwrap();
    for i in 0..300u64 {
        tl.on_report(1_000 + i * 4_000, Some(10_000 + i * 40_000), &report(i as u8, 0, 128, i as u32));
    }
    let fin = tl.finish(1_500_000).unwrap();
    let mut chain = ev::CheckpointChain::default();
    let cp_start = chain.next(100, serde_json::json!({
        "trigger": "start",
        "hand_timeline_dir": hand.to_string_lossy(),
        "hand_timeline_dir_actual": hand.to_string_lossy(),
        "hand_dir_source": "override",
        "hand_dir_fallback_reason": Value::Null,
        "hand_timeline_file": FILE_NAME,
        "hand_raw_file": RAW_FILE,
    })).unwrap();
    let cp_stop = chain.next(200, serde_json::json!({
        "trigger": "stop",
        "hand_timeline_dir": hand.to_string_lossy(),
        "hand_timeline_dir_actual": hand.to_string_lossy(),
        "hand_dir_source": "override",
        "hand_dir_fallback_reason": Value::Null,
        "hand_timeline_file": fin.file,
        "hand_timeline_sha256": fin.file_sha256,
        "hand_timeline_head": fin.head,
        "hand_timeline_chunks": fin.chunks,
        "hand_raw_file": fin.raw_file,
        "hand_raw_bytes": fin.raw_bytes,
    })).unwrap();
    std::fs::write(
        bundle.join("checkpoints.jsonl"),
        format!("{}\n{}\n", ev::jcs(&cp_start).unwrap(), ev::jcs(&cp_stop).unwrap()),
    ).unwrap();

    assert!(!bundle.join(FILE_NAME).exists());
    assert_eq!(RAW_LAYOUT, "v1");
    let rep = check_dir(&bundle).unwrap();
    assert!(rep.pass(), "{:?}", rep.errors);
    assert!(rep.stop_bound);
    assert_eq!(rep.hand_dir.as_deref(), Some(hand.as_path()));
    assert_eq!(rep.hand_dir_source.as_deref(), Some("override"));
    let _ = std::fs::remove_dir_all(&bundle);
    let _ = std::fs::remove_dir_all(&hand);
}

/// A crash mid-write of the next (not-yet-sealed) raw slice can leave leftover bytes after
/// the last sealed offset that are not a multiple of 80. Those bytes are an UNMEASURED
/// unsealed tail (warning, never padded). Sealed slices still have to hash. A 12-byte cut
/// from a fully sealed file remains an error (see checker_rejects_truncation_...).
#[test]
fn checker_treats_mid_record_unsealed_tail_as_unmeasured_warning() {
    let make = |tag: &str| -> (PathBuf, u64) {
        let dir = tmp(tag);
        let mut tl = HandTimeline::open(cfg(&dir, true, Some(edge()))).unwrap();
        for i in 0..300u64 {
            tl.on_report(1_000 + i * 4_000, Some(10_000 + i * 40_000), &report(i as u8, 0, 128, i as u32));
        }
        let fin = tl.finish(1_500_000).unwrap();
        assert!(check_dir(&dir).unwrap().pass());
        (dir, fin.raw_bytes)
    };

    // Contrast: a complete-record unsealed tail (80 B) already passes with a warning.
    let (d_full, sealed_len) = make("crash-tail-full-rec");
    let rp_full = d_full.join(RAW_FILE);
    let mut raw_full = std::fs::read(&rp_full).unwrap();
    assert_eq!(raw_full.len() as u64, sealed_len);
    raw_full.extend_from_slice(&[0xAAu8; RAW_RECORD_BYTES]);
    std::fs::write(&rp_full, &raw_full).unwrap();
    let rep_full = check_dir(&d_full).unwrap();
    assert!(rep_full.pass(), "complete-record unsealed tail should pass: {:?}", rep_full.errors);
    assert!(rep_full.warnings.iter().any(|w| w.contains("unsealed tail")), "{:?}", rep_full.warnings);
    let _ = std::fs::remove_dir_all(&d_full);

    // Mid-record unsealed tail (40 B): must pass; sealed slices still hash; never padded.
    let (d_mid, sealed_len) = make("crash-tail-mid-rec");
    let rp = d_mid.join(RAW_FILE);
    let mut raw = std::fs::read(&rp).unwrap();
    assert_eq!(raw.len() as u64, sealed_len);
    assert_eq!(raw.len() % RAW_RECORD_BYTES, 0);
    raw.extend_from_slice(&[0xAAu8; 40]);
    std::fs::write(&rp, &raw).unwrap();
    assert_eq!(raw.len() % RAW_RECORD_BYTES, 40);
    let rep = check_dir(&d_mid).unwrap();
    assert!(rep.pass(), "mid-record unsealed tail must not fail once sealed slices hash: {:?}", rep.errors);
    assert!(rep.errors.iter().all(|e| !e.contains("not a multiple of")), "{:?}", rep.errors);
    assert!(!rep.errors.iter().any(|e| e.contains("SHA-256 does not match") || e.contains("exceeds file length")), "sealed slices should still hash and fit the file: {:?}", rep.errors);
    assert!(rep.warnings.iter().any(|w| w.contains("UNMEASURED unsealed tail") && w.contains("never padded") && w.contains("40 trailing")), "{:?}", rep.warnings);
    let _ = std::fs::remove_dir_all(&d_mid);
}

#[test]
fn physical_face_records_raw_byte_51_for_physical_vs_paddle_without_rewriting_buttons() {
    let dir = tmp("physical-face");
    let mut tl = HandTimeline::open(cfg(&dir, true, Some(edge()))).unwrap();

    let make = |counter: u8, buttons: u32, byte51: u8| -> Vec<u8> {
        let mut r = report(counter, buttons, 128, u32::from(counter) * 100);
        r[51] = byte51;
        r
    };

    // Idle -> physical CROSS (b51=0x28) -> idle -> paddle CROSS (b51=0x08) ->
    // physical CROSS while still held (b51=0x28, buttons unchanged) -> idle ->
    // physical CIRCLE (b51=0x48) -> idle -> paddle CIRCLE (b51=0x08).
    let seq: &[(u64, u32, u8)] = &[
        (10_000, 0, 0x08),
        (20_000, CROSS, 0x28),
        (30_000, 0, 0x08),
        (40_000, CROSS, 0x08),
        (41_000, CROSS, 0x28),
        (50_000, 0, 0x08),
        (60_000, CIRCLE, 0x48),
        (70_000, 0, 0x08),
        (80_000, CIRCLE, 0x08),
    ];
    for (idx, &(t, btns, b51)) in seq.iter().enumerate() {
        tl.on_report(t, Some(t * 10), &make(idx as u8, btns, b51));
    }
    tl.finish(1_500_000).unwrap();

    let recs = lines(&dir);
    assert_eq!(recs[0]["physical_face_status"], "ESTIMATED");
    let ins: Vec<&Value> = recs.iter().filter(|r| r["type"] == "in").collect();
    assert_eq!(ins.len(), seq.len(), "each button or physical_face change emits an in row: {ins:?}");

    let observed: Vec<(u64, u64, u64)> = ins
        .iter()
        .map(|r| (r["t_us"].as_u64().unwrap(), r["buttons"].as_u64().unwrap(), r["physical_face"].as_u64().unwrap()))
        .collect();
    let expected: Vec<(u64, u64, u64)> = seq.iter().map(|&(t, b, b51)| (t, u64::from(b), u64::from(b51))).collect();
    assert_eq!(observed, expected, "buttons stays unmodified post-remap and physical_face records raw byte 51");

    let rep = check_dir(&dir).unwrap();
    assert!(rep.pass(), "{:?}", rep.errors);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn dualsense_fallback_keeps_physical_face_unmeasured_and_out_of_change_key() {
    let dir = tmp("ds-physical-face");
    let mut tl = HandTimeline::open(cfg(&dir, true, Some(dualsense()))).unwrap();

    // Standard DualSense (0x0CE6): changing byte 51 across reports with identical controls
    // must keep physical_face == "UNMEASURED" and must not emit extra in rows.
    for (idx, &b51) in [0x08u8, 0x28, 0x48, 0x88].iter().enumerate() {
        let mut r = report(idx as u8, CROSS, 128, (idx as u32) * 100);
        r[51] = b51;
        tl.on_report(10_000 + (idx as u64) * 4_000, Some((idx as u64 + 1) * 100_000), &r);
    }
    tl.finish(1_500_000).unwrap();

    let recs = lines(&dir);
    assert_eq!(recs[0]["physical_face_status"], "UNMEASURED");
    let ins: Vec<&Value> = recs.iter().filter(|r| r["type"] == "in").collect();
    assert_eq!(ins.len(), 1, "byte 51 must stay out of the change key on standard DualSense: {ins:?}");
    assert_eq!(ins[0]["physical_face"], "UNMEASURED");
    assert_eq!(ins[0]["edge_bits"], "UNMEASURED");
    assert_eq!(ins[0]["device_model"], "dualsense");

    let rep = check_dir(&dir).unwrap();
    assert!(rep.pass() && rep.fallback, "{:?}", rep.errors);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn bytes_42_43_raw_snapshots_at_connect_and_logs_change_while_keeping_adaptive_unmeasured() {
    let dir = tmp("bytes-42-43");
    let mut tl = HandTimeline::open(cfg(&dir, true, Some(edge()))).unwrap();

    let make = |counter: u8, b42: u8, b43: u8| -> Vec<u8> {
        let mut r = report(counter, 0, 128, u32::from(counter) * 100);
        r[42] = b42;
        r[43] = b43;
        r[51] = 0x08;
        r
    };

    // Initial connect snapshot [0, 9] (raw[42]=0=R2, raw[43]=9=L2), unchanged follow-up, then mid-epoch change to [25, 9] within < ANALOG_MIN_GAP_US,
    // unchanged follow-up at [25, 9], then new epoch reconnect with [0, 0].
    tl.on_report(10_000, Some(100_000), &make(0, 0, 9));
    tl.on_report(11_000, Some(110_000), &make(1, 0, 9));
    tl.on_report(12_000, Some(120_000), &make(2, 25, 9));
    tl.on_report(13_000, Some(130_000), &make(3, 25, 9));
    assert_eq!(tl.live().state, "connected", "bytes_42_43_change status must not clobber live connected state");

    tl.on_connected(200_000, edge());
    tl.on_report(201_000, Some(2_010_000), &make(4, 0, 0));
    tl.finish(1_500_000).unwrap();

    let recs = lines(&dir);
    let header = &recs[0];
    assert_eq!(header["bytes_42_43_raw"], "UNKNOWN");
    assert_eq!(header["bytes_42_43_status"], "ESTIMATED");
    assert_eq!(header["bytes_42_43_change_cap_per_epoch"], BYTES_42_43_CHANGE_CAP_PER_EPOCH);
    assert_eq!(header["adaptive_trigger_state"], "UNMEASURED");

    let conn_rows: Vec<&Value> = recs.iter().filter(|r| r["type"] == "status" && r["state"] == "connected").collect();
    assert_eq!(conn_rows.len(), 2);
    assert!(conn_rows.iter().all(|r| r["bytes_42_43_raw"] == "UNKNOWN" && r["bytes_42_43_status"] == "ESTIMATED"));

    let ins: Vec<&Value> = recs.iter().filter(|r| r["type"] == "in").collect();
    assert_eq!(ins.len(), 3, "connect snapshot [0,9], mid-epoch change [25,9], reconnect snapshot [0,0]: {ins:?}");
    assert_eq!(ins[0]["bytes_42_43_raw"], serde_json::json!([0, 9]));
    assert_eq!(ins[0]["trigger_fx_status"]["r2_raw"], 0, "r2_raw is raw[42]");
    assert_eq!(ins[0]["trigger_fx_status"]["l2_raw"], 9, "l2_raw is raw[43]");
    assert_eq!(ins[1]["bytes_42_43_raw"], serde_json::json!([25, 9]));
    assert_eq!(ins[1]["trigger_fx_status"]["r2_raw"], 25, "r2_raw is raw[42]");
    assert_eq!(ins[1]["trigger_fx_status"]["l2_raw"], 9, "l2_raw is raw[43]");
    assert_eq!(ins[1]["t_us"], 12_000);
    assert_eq!(ins[2]["bytes_42_43_raw"], serde_json::json!([0, 0]));

    let b_status: Vec<&Value> = recs.iter().filter(|r| r["type"] == "status" && r["state"] == "bytes_42_43_change").collect();
    assert_eq!(b_status.len(), 1, "status entry emitted on mid-epoch change only: {b_status:?}");
    assert_eq!(b_status[0]["bytes_42_43_raw"], serde_json::json!([25, 9]));
    assert_eq!(b_status[0]["prev_bytes_42_43_raw"], serde_json::json!([0, 9]));
    assert_eq!(b_status[0]["epoch_change_seq"], 1);
    assert_eq!(b_status[0]["epoch_change_cap"], BYTES_42_43_CHANGE_CAP_PER_EPOCH);
    assert_eq!(b_status[0]["status"], "ESTIMATED");
    assert_eq!(b_status[0]["adaptive_trigger_state"], "UNMEASURED");

    let summary: Value = serde_json::from_str(&std::fs::read_to_string(dir.join(SUMMARY_FILE)).unwrap()).unwrap();
    assert_eq!(summary["bytes_42_43_raw_last"], serde_json::json!([0, 0]));
    assert_eq!(summary["bytes_42_43_status"], "ESTIMATED");
    assert_eq!(summary["bytes_42_43_changes_suppressed"], 0);
    assert_eq!(summary["adaptive_trigger_state"], "UNMEASURED");

    let rep = check_dir(&dir).unwrap();
    assert!(rep.pass(), "{:?}", rep.errors);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn bytes_42_43_flapping_is_bounded_by_per_epoch_cap() {
    let dir = tmp("bytes-42-43-flap");
    let mut tl = HandTimeline::open(cfg(&dir, true, Some(edge()))).unwrap();

    let cap = BYTES_42_43_CHANGE_CAP_PER_EPOCH;
    let total_reports: u64 = cap + 40; // 1 initial + (cap + 39) flapping transitions within < ANALOG_MIN_GAP_US (1_000 us step)
    for i in 0..total_reports {
        let mut r = report(i as u8, 0, 128, (i as u32) * 100);
        let v = if i % 2 == 0 { 0u8 } else { 9u8 };
        r[42] = v;
        r[43] = 9;
        r[51] = 0x08;
        tl.on_report(10_000 + i * 1_000, Some((i + 1) * 10_000), &r);
    }
    // Normal button press after hitting the status-row cap still emits an `in` row with the current bytes 42-43.
    let last_v = if (total_reports - 1) % 2 == 0 { 0u8 } else { 9u8 };
    let mut btn = report(total_reports as u8, CROSS, 128, (total_reports as u32) * 100);
    btn[42] = last_v;
    btn[43] = 9;
    btn[51] = 0x28;
    tl.on_report(10_000 + total_reports * 1_000, Some((total_reports + 1) * 10_000), &btn);

    // Reconnect (epoch 2) resets the per-epoch cap so new changes are logged again.
    let ep2_t0 = 10_000 + (total_reports + 10) * 1_000;
    tl.on_connected(ep2_t0, edge());
    let mut r2a = report(0, 0, 128, 1);
    r2a[42] = 1;
    r2a[43] = 1;
    r2a[51] = 0x08;
    tl.on_report(ep2_t0 + 1_000, Some((ep2_t0 + 1_000) * 10), &r2a);
    let mut r2b = report(1, 0, 128, 2);
    r2b[42] = 2;
    r2b[43] = 3;
    r2b[51] = 0x08;
    tl.on_report(ep2_t0 + 2_000, Some((ep2_t0 + 2_000) * 10), &r2b);

    tl.finish(ep2_t0 + 500_000).unwrap();

    let recs = lines(&dir);
    let ep1_changes: Vec<&Value> = recs
        .iter()
        .filter(|r| r["type"] == "status" && r["state"] == "bytes_42_43_change" && r["epoch"] == 1)
        .collect();
    assert_eq!(ep1_changes.len() as u64, cap, "flapping status rows must stay capped per epoch");

    let ep1_ins: Vec<&Value> = recs.iter().filter(|r| r["type"] == "in" && r["epoch"] == 1).collect();
    // 1 initial snapshot + (total_reports - 1) raw[42..43] transitions (each emits an `in` row even within analog_min_gap_us) + 1 normal CROSS press = total_reports + 1.
    assert_eq!(ep1_ins.len() as u64, total_reports + 1, "every raw[42..43] change emits an in row even within analog_min_gap_us");
    assert_eq!(ep1_ins.last().unwrap()["buttons"], CROSS);
    assert_eq!(ep1_ins.last().unwrap()["bytes_42_43_raw"], serde_json::json!([last_v, 9]));
    assert_eq!(ep1_ins.last().unwrap()["trigger_fx_status"]["r2_raw"], last_v);
    assert_eq!(ep1_ins.last().unwrap()["trigger_fx_status"]["l2_raw"], 9);

    let ep2_changes: Vec<&Value> = recs
        .iter()
        .filter(|r| r["type"] == "status" && r["state"] == "bytes_42_43_change" && r["epoch"] == 2)
        .collect();
    assert_eq!(ep2_changes.len(), 1, "reconnect resets the per-epoch cap");
    assert_eq!(ep2_changes[0]["bytes_42_43_raw"], serde_json::json!([2, 3]));
    let ep2_ins: Vec<&Value> = recs.iter().filter(|r| r["type"] == "in" && r["epoch"] == 2).collect();
    assert_eq!(ep2_ins.last().unwrap()["trigger_fx_status"]["r2_raw"], 2);
    assert_eq!(ep2_ins.last().unwrap()["trigger_fx_status"]["l2_raw"], 3);

    let expected_suppressed = (total_reports - 1) - cap;
    let hb_suppressed: u64 = recs
        .iter()
        .filter(|r| r["type"] == "hb")
        .filter_map(|r| r["bytes_42_43_suppressed"].as_u64())
        .sum();
    assert_eq!(hb_suppressed, expected_suppressed);

    let summary: Value = serde_json::from_str(&std::fs::read_to_string(dir.join(SUMMARY_FILE)).unwrap()).unwrap();
    assert_eq!(summary["bytes_42_43_changes_suppressed"], expected_suppressed);
    assert_eq!(summary["bytes_42_43_raw_last"], serde_json::json!([2, 3]));

    let rep = check_dir(&dir).unwrap();
    assert!(rep.pass(), "{:?}", rep.errors);
    let _ = std::fs::remove_dir_all(&dir);
}
