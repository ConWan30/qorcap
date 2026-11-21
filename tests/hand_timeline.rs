//! Hand timeline end to end with a scripted ReportSource (no hardware): chunking, the JCS chain, the raw
//! companion files, the DualSense fallback label, the raw off switch, gap / fault accounting, and the checker.

use qorcap::evidence::{self as ev, GENESIS};
use qorcap::frameclock::FrameStamp;
use qorcap::hand::check::check_dir;
use qorcap::hand::device::{DeviceInfo, DeviceModel};
use qorcap::hand::logger;
use qorcap::hand::source::{MockSource, MockStep, OpenOutcome};
use qorcap::hand::timeline::{Config, HandTimeline, CHUNK_US, FILE_NAME, RAW_DIR, RAW_RECORD_BYTES, SEAL_GRACE_US};
use qorcap::hid::{self, Pad, CROSS, FN_LEFT, BACK_RIGHT, R2};
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
    Config { dir: dir.clone(), raw_usb: raw, raw_min_free_bytes: None, wall_anchor_ms: 1_795_144_560_000, qpc: None, frame_clock_on: true, initial_device: dev, start_t_us: 0 }
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
    // Raw: every report kept, 72 B each.
    let raw0 = std::fs::read(dir.join(RAW_DIR).join("w0000000000.bin")).unwrap();
    assert_eq!(raw0.len() % RAW_RECORD_BYTES, 0);
    assert_eq!(raw0.len() / RAW_RECORD_BYTES, 250, "1 s of 4 ms reports");
    assert_eq!(&raw0[64..72], &10_000u64.to_le_bytes(), "QPC ticks follow the 64 B report");
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
    assert_eq!(rep.raw_files, 3);
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
    // 2) flip one raw byte
    let rp = dir.join(RAW_DIR).join("w0000000001.bin");
    let mut raw = std::fs::read(&rp).unwrap();
    raw[5] ^= 1;
    std::fs::write(&rp, &raw).unwrap();
    let r = check_dir(&dir).unwrap();
    assert!(r.errors.iter().any(|e| e.contains("raw file SHA-256 does not match")), "{:?}", r.errors);
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
    assert!(recs.iter().filter(|r| r["type"] == "raw_bind").all(|r| r["raw_usb"] == false && r["file"].is_null()));
    assert!(!dir.join(RAW_DIR).exists());
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
