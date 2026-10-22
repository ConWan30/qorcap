//! Real DualSense Edge excerpts through QorCap's input path.
//!
//! Stored features are rebuilt into a 64-byte USB report 0x01, parsed with `hid::parse`, and fed to
//! `InputLog::on_report` (the same 4 ms analog log plus the live haptic detector a recording uses).
//! See `tests/fixtures/haptics/ORIGIN.txt`.

use qorcap::{hid, inputs::InputLog};
use serde_json::Value;

struct Sample {
    t_ms: u64,
    gyro: [i16; 3],
    accel: [i16; 3],
    b0: u8,
    b1: u8,
    l2: u8,
    r2: u8,
}

fn load_qhf(xz: &[u8]) -> Vec<Sample> {
    let mut raw = Vec::new();
    lzma_rs::xz_decompress(&mut &xz[..], &mut raw).expect("xz");
    assert!(raw.starts_with(b"QHF1"), "bad fixture magic");
    let n = u32::from_le_bytes(raw[4..8].try_into().unwrap()) as usize;
    let mut out = Vec::with_capacity(n);
    let mut t = 0u64;
    let mut p = 8usize;
    for _ in 0..n {
        let dt = u16::from_le_bytes(raw[p..p + 2].try_into().unwrap()) as u64;
        t += dt;
        let i16at = |o: usize| i16::from_le_bytes(raw[p + o..p + o + 2].try_into().unwrap());
        out.push(Sample {
            t_ms: t,
            gyro: [i16at(2), i16at(4), i16at(6)],
            accel: [i16at(8), i16at(10), i16at(12)],
            b0: raw[p + 14],
            b1: raw[p + 15],
            l2: raw[p + 16],
            r2: raw[p + 17],
        });
        p += 18;
    }
    assert_eq!(p, raw.len());
    out
}

fn report(s: &Sample, counter: u8) -> Vec<u8> {
    let mut r = vec![0u8; 64];
    r[0] = 0x01;
    r[1] = 128;
    r[2] = 128;
    r[3] = 128;
    r[4] = 128;
    r[5] = s.l2;
    r[6] = s.r2;
    r[7] = counter;
    r[8] = s.b0;
    r[9] = s.b1;
    for (i, v) in s.gyro.iter().enumerate() {
        r[16 + 2 * i..18 + 2 * i].copy_from_slice(&v.to_le_bytes());
    }
    for (i, v) in s.accel.iter().enumerate() {
        r[22 + 2 * i..24 + 2 * i].copy_from_slice(&v.to_le_bytes());
    }
    r
}

struct Hap {
    events: usize,
    high: usize,
    dur_s: f64,
    /// Flags on each haptic event, in order.
    flags: Vec<Vec<String>>,
}

fn replay(xz: &[u8]) -> Hap {
    let samples = load_qhf(xz);
    let mut log = InputLog::new(None, true, 0);
    log.on_status(0, "connected", "haptic fixture");
    let mut last = 0u64;
    for (i, s) in samples.iter().enumerate() {
        let t_us = s.t_ms.saturating_mul(1000) + 1_000;
        let raw = report(s, (i & 0xff) as u8);
        let pad = hid::parse(&raw).expect("fixture report parses");
        log.on_report(t_us, &pad);
        last = t_us;
    }
    let mut recs: Vec<Value> = log.pending().to_vec();
    recs.extend(log.detector.flush(last));
    let ev: Vec<&Value> = recs.iter().filter(|r| r["type"] == "haptic").collect();
    let high = ev.iter().filter(|e| e["confidence_label"] == "high").count();
    let dur_s = samples.last().map(|s| s.t_ms as f64 / 1000.0).unwrap_or(0.0);
    let flags = ev.iter().map(|e| {
        e["flags"].as_array().unwrap().iter().map(|v| v.as_str().unwrap().to_string()).collect()
    }).collect();
    Hap { events: ev.len(), high, dur_s, flags }
}

fn per_min(h: &Hap) -> f64 {
    if h.dur_s <= 0.0 { 0.0 } else { h.high as f64 * 60.0 / h.dur_s }
}

#[test]
fn real_edge_excerpts_keep_fire_haptics_and_drop_hand_activity() {
    let firing = replay(include_bytes!("fixtures/haptics/firing_prefix_20s.qhf.xz"));
    let idle = replay(include_bytes!("fixtures/haptics/idle_grip_prefix_15s.qhf.xz"));
    let touch = replay(include_bytes!("fixtures/haptics/touchpad_swipes.qhf.xz"));
    let (fp, ip, tp) = (per_min(&firing), per_min(&idle), per_min(&touch));
    // Locked to these excerpts (same path as a live recording). Full 150 s sessions are reported
    // in docs/HAPTICS.md; the prefixes are the regression set.
    assert_eq!((firing.events, firing.high), (15, 7), "firing {fp:.2}/min");
    assert_eq!((idle.events, idle.high), (1, 0), "idle {ip:.2}/min");
    assert_eq!((touch.events, touch.high), (15, 1), "touch {tp:.2}/min");
    assert!(fp > 15.0 && tp < 8.0 && fp > tp * 3.0, "firing {fp:.1} idle {ip:.1} touch {tp:.1}");
    assert_eq!(idle.flags, vec![vec!["grip_jolt".to_string()]]);
    let handish = |f: &Vec<String>| f.iter().any(|s| s == "grip_jolt" || s == "hand_motion");
    // One swipe burst still clears both gates (gyro RMS 36, accel RMS 226) and stays high.
    // Every other touch event is capped as a grip jolt or a hard hand motion.
    assert_eq!(touch.flags.iter().filter(|f| !handish(f)).count(), 1, "touch flags {:?}", touch.flags);
}
