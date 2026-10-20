//! Opt-in per-frame capture clock (default off).
//!
//! The manifest clock in `align.rs` is a straight line through fragment-close stamps: `mono_ms` is the
//! QorCap `Instant` when an fMP4 fragment is hashed, and `frames` is ffmpeg's progress counter at that
//! moment. Nothing in that pair is the time a frame was captured. This module records a real time for
//! each video frame on that same `Instant`: the moment this process reads the MPEG-TS packet that starts
//! the frame's PES. Encode delay is inside that arrival (the capture command is unchanged: `-bf 0`, CFR).
//! It is not a fragment-close time, and it is not written unless the flag is on.
//!
//! The file is additive evidence. It is not in the hash chain, not in the receipt, and not checked by
//! `verify-bundle`. Alignment uses it only when the file is present and a stamp is near the press;
//! otherwise the manifest fit is used and its JSON is unchanged.
use crate::ring::{self, TS};
use serde_json::{json, Value};
use std::fs::OpenOptions;
use std::io::Write;
use std::path::Path;
use std::sync::Mutex;

pub const SCHEMA: &str = "qorcap-frame-clock/v0";
pub const ENV: &str = "QORCAP_FRAME_CLOCK";
pub const FILE_NAME: &str = "frame_clock.jsonl";
/// A press farther than this from every stamp uses the manifest fit for that press.
pub const MEASURED_MAX_GAP_US: u64 = 500_000;

/// `cli` is `--frame-clock`. `env_val` is `QORCAP_FRAME_CLOCK` (same words as the Glimpse frame tap).
pub fn flag_enabled(cli: bool, env_val: Option<&str>) -> bool {
    cli || matches!(env_val.map(str::trim), Some("1" | "true" | "yes" | "on"))
}

/// One captured frame. `frame` is the arrival order in `file` (the CFR frame index).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FrameStamp {
    pub file: String,
    pub frame: i64,
    pub t_mono_us: u64,
    pub pts_90k: u64,
}

#[derive(Debug)]
struct State {
    out: Option<std::fs::File>,
    next_frame: i64,
    last_pts: Option<u64>,
    frames: u64,
}

/// Writer. Disabled: every method is a no-op and no file is created.
pub struct FrameClock {
    enabled: bool,
    state: Mutex<State>,
}

impl FrameClock {
    pub fn open(enabled: bool, bundle: Option<&Path>) -> Self {
        let path = bundle.map(|d| d.join(FILE_NAME));
        let out = if enabled {
            path.as_ref().and_then(|p| {
                if let Some(parent) = p.parent() { let _ = std::fs::create_dir_all(parent); }
                match OpenOptions::new().create(true).append(true).open(p) {
                    Ok(mut f) => {
                        let header = json!({
                            "type": "header", "schema": SCHEMA,
                            "clock": "qorcap-mono-us (same Instant as inputs t_us and manifest mono_ms)",
                            "source": "mpegts-video-pes-arrival",
                            "note": "t_mono_us is when QorCap read the PES that starts this CFR frame. It is not the fragment-close mono_ms. The capture command is unchanged."
                        });
                        let _ = writeln!(f, "{header}");
                        let _ = f.flush();
                        Some(f)
                    }
                    Err(e) => { log::error!("frame clock: cannot open {}: {e}", p.display()); None }
                }
            })
        } else { None };
        Self { enabled: enabled && out.is_some(), state: Mutex::new(State { out, next_frame: 0, last_pts: None, frames: 0 }) }
    }

    pub fn enabled(&self) -> bool { self.enabled }

    /// `data` is the TS bytes just written to the recording. `t_mono_us` is `Instant` elapsed at the read.
    pub fn observe(&self, data: &[u8], t_mono_us: u64, file: &str) {
        if !self.enabled { return; }
        let mut g = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if g.out.is_none() { return; }
        let mut lines = String::new();
        for pkt in data.chunks_exact(TS) {
            let Some(pts) = video_pes_pts(pkt) else { continue; };
            if g.last_pts == Some(pts) { continue; }
            if let Some(prev) = g.last_pts {
                if pts + 90_000 < prev {
                    let note = json!({"type": "discontinuity", "t_mono_us": t_mono_us, "pts_90k": pts, "file": file});
                    lines.push_str(&note.to_string());
                    lines.push('\n');
                }
            }
            g.last_pts = Some(pts);
            let frame = g.next_frame;
            g.next_frame += 1;
            g.frames += 1;
            let rec = json!({"type": "frame", "file": file, "frame": frame, "t_mono_us": t_mono_us, "pts_90k": pts});
            lines.push_str(&rec.to_string());
            lines.push('\n');
        }
        if lines.is_empty() { return; }
        if let Some(out) = g.out.as_mut() {
            let _ = out.write_all(lines.as_bytes());
            let _ = out.flush();
        }
    }
}

/// PTS of a video PES that starts in this TS packet (90 kHz). Audio and continuation packets are `None`.
pub fn video_pes_pts(pkt: &[u8]) -> Option<u64> {
    if pkt.len() < TS || pkt[0] != 0x47 || pkt[1] & 0x40 == 0 || pkt[3] & 0x10 == 0 { return None; }
    let mut i = 4usize;
    if pkt[3] & 0x20 != 0 {
        if pkt.len() <= 4 { return None; }
        i += 1 + pkt[4] as usize;
    }
    if i + 14 > pkt.len() { return None; }
    if pkt[i] != 0 || pkt[i + 1] != 0 || pkt[i + 2] != 1 { return None; }
    let sid = pkt[i + 3];
    if !(0xE0..=0xEF).contains(&sid) { return None; }
    if pkt[i + 7] & 0x80 == 0 { return None; }
    let hdr = pkt[i + 8] as usize;
    if hdr < 5 || i + 14 > pkt.len() { return None; }
    decode_pts(&pkt[i + 9..i + 14])
}

fn decode_pts(b: &[u8]) -> Option<u64> {
    if b.len() < 5 { return None; }
    Some((((b[0] as u64) & 0x0e) << 29)
        | ((b[1] as u64) << 22)
        | (((b[2] as u64) & 0xfe) << 14)
        | ((b[3] as u64) << 7)
        | (((b[4] as u64) & 0xfe) >> 1))
}

pub fn encode_pts(pts: u64) -> [u8; 5] {
    let p = pts & ((1 << 33) - 1);
    [
        0x21 | (((p >> 29) as u8) & 0x0e),
        (p >> 22) as u8,
        (((p >> 14) as u8) & 0xfe) | 1,
        (p >> 7) as u8,
        ((p as u8) << 1) | 1,
    ]
}

/// A 188-byte TS packet whose payload is a video PES with `pts` (90 kHz).
pub fn video_ts_packet(pid: u16, pts: u64) -> [u8; TS] {
    let mut p = [0xffu8; TS];
    p[0] = 0x47;
    p[1] = 0x40 | ((pid >> 8) as u8 & 0x1f);
    p[2] = pid as u8;
    p[3] = 0x10; // payload, no adaptation
    let ptsb = encode_pts(pts);
    let pes = [
        0x00, 0x00, 0x01, 0xE0, 0x00, 0x00, 0x80, 0x80, 0x05,
        ptsb[0], ptsb[1], ptsb[2], ptsb[3], ptsb[4],
    ];
    p[4..4 + pes.len()].copy_from_slice(&pes);
    let _ = ring::pid(&p); // keep the ring helper linked for the packet layout
    p
}

pub fn load(path: &Path) -> Vec<FrameStamp> {
    let Ok(text) = std::fs::read_to_string(path) else { return vec![] };
    let mut out = vec![];
    for line in text.lines() {
        let Ok(v) = serde_json::from_str::<Value>(line) else { continue };
        if v["type"] != "frame" { continue; }
        let (Some(frame), Some(t), Some(file)) = (v["frame"].as_i64(), v["t_mono_us"].as_u64(), v["file"].as_str()) else { continue };
        out.push(FrameStamp { file: file.to_string(), frame, t_mono_us: t, pts_90k: v["pts_90k"].as_u64().unwrap_or(0) });
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn flag_words_match_the_glimpse_tap_and_default_off() {
        assert!(!flag_enabled(false, None));
        assert!(!flag_enabled(false, Some("0")));
        assert!(!flag_enabled(false, Some("")));
        assert!(!flag_enabled(false, Some("off")));
        for w in ["1", "true", "yes", "on", " true "] { assert!(flag_enabled(false, Some(w)), "{w}"); }
        assert!(flag_enabled(true, None));
    }

    #[test]
    fn pes_pts_roundtrip_and_ignores_audio() {
        let pkt = video_ts_packet(0x100, 90_000);
        assert_eq!(video_pes_pts(&pkt), Some(90_000));
        assert_eq!(video_pes_pts(&video_ts_packet(0x101, 3_000)), Some(3_000));
        let mut cont = pkt;
        cont[1] &= !0x40; // continuation: not the start of a PES
        assert_eq!(video_pes_pts(&cont), None);
        let mut audio = pkt;
        audio[7] = 0xC0; // this is inside the PES (offset 4 + 3 = stream id is pkt[7])
        // stream id is at payload offset 3 = packet byte 7. Yes.
        assert_eq!(video_pes_pts(&audio), None);
    }

    #[test]
    fn disabled_clock_creates_nothing_and_writes_nothing() {
        let dir = std::env::temp_dir().join(format!("qorcap-fc-off-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let fc = FrameClock::open(false, Some(&dir));
        assert!(!fc.enabled());
        fc.observe(&video_ts_packet(0x100, 0), 1_000, "video.mp4");
        assert!(!dir.join(FILE_NAME).exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn enabled_clock_records_arrival_order_on_the_given_instant() {
        let dir = std::env::temp_dir().join(format!("qorcap-fc-on-{}-{}", std::process::id(), line!()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let fc = FrameClock::open(true, Some(&dir));
        let mut blob = Vec::new();
        blob.extend_from_slice(&video_ts_packet(0x100, 0));
        blob.extend_from_slice(&video_ts_packet(0x100, 3_000));
        blob.extend_from_slice(&video_ts_packet(0x100, 3_000)); // duplicate PTS, one frame
        fc.observe(&blob, 5_000_000, "video.mp4");
        fc.observe(&video_ts_packet(0x100, 6_000), 5_033_000, "video.mp4");
        let stamps = load(&dir.join(FILE_NAME));
        assert_eq!(stamps.len(), 3);
        assert_eq!(stamps[0], FrameStamp { file: "video.mp4".into(), frame: 0, t_mono_us: 5_000_000, pts_90k: 0 });
        assert_eq!(stamps[1].frame, 1);
        assert_eq!(stamps[1].t_mono_us, 5_000_000); // same read as frame 0
        assert_eq!(stamps[2].frame, 2);
        assert_eq!(stamps[2].t_mono_us, 5_033_000);
        let text = std::fs::read_to_string(dir.join(FILE_NAME)).unwrap();
        assert!(text.lines().next().unwrap().contains(SCHEMA));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
