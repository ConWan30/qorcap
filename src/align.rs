//! Input <-> video alignment.
//!
//! For a sample of key presses (face buttons, d-pad, L1/R1, OPTIONS, touchpad) decode a short low-res window of
//! the bundle's own video around the press and ask: does the picture change within N frames after the press,
//! relative to how much it was already changing just before? The same test is run at "chance" times with no press
//! nearby, so the match rate can be read against what a busy Madden screen gives by luck. The median press ->
//! change lag is the offset estimate (game response + capture-card latency + any clock offset).
//!
//! Score events from the scorebug reader (timeline.jsonl) are checked for controller activity in the 30 s
//! before each scoring change.
//!
//! Limits: menus and play-calling react visibly to presses; mid-play presses often do not. A low match rate
//! with a real controller is possible; a high match rate far above chance is the meaningful signal.
use crate::hid;
use serde_json::{json, Value};
use std::process::{Command, Stdio};

pub const FPS: f64 = 30.0;
pub const DEFAULT_N_FRAMES: usize = 15;
const W: usize = 48; const H: usize = 27;
const PRE_S: f64 = 1.0;
pub const MAX_EVENTS: usize = 60;

/// Per-frame mean absolute difference (0..255) for one decoded window; `event` = frame index of the event time.
#[derive(Debug, Clone)]
pub struct Window { pub diffs: Vec<f32>, pub event: usize }

/// Some(lag_frames) if the picture changes within `n` frames after the event.
pub fn responds(w: &Window, n: usize) -> Option<usize> {
    if w.event < 4 || w.diffs.len() <= w.event + 1 { return None; }
    let mut pre: Vec<f32> = w.diffs[..w.event].to_vec();
    pre.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let base = pre[pre.len() / 2];
    let thr = (base * 2.5).max(base + 4.0);
    (w.event + 1..=(w.event + n).min(w.diffs.len() - 1)).find(|&k| w.diffs[k] > thr).map(|k| k - w.event)
}

pub fn summarize(press: &[Window], chance: &[Window], n: usize) -> Value {
    let lags: Vec<usize> = press.iter().filter_map(|w| responds(w, n)).collect();
    let cm = chance.iter().filter(|w| responds(w, n).is_some()).count();
    let pm = |a: usize, b: usize| if b == 0 { Value::Null } else { json!((a as f64 * 1000.0 / b as f64).round() as i64) };
    let mut ms: Vec<i64> = lags.iter().map(|l| (*l as f64 * 1000.0 / FPS).round() as i64).collect();
    ms.sort();
    let offset = if ms.is_empty() { Value::Null } else { json!(ms[ms.len() / 2]) };
    let spread = if ms.len() >= 4 { json!(ms[ms.len() * 3 / 4] - ms[ms.len() / 4]) } else { Value::Null };
    json!({"presses_tested": press.len(), "presses_matched": lags.len(), "match_permille": pm(lags.len(), press.len()),
           "chance_tested": chance.len(), "chance_matched": cm, "chance_permille": pm(cm, chance.len()),
           "offset_ms": offset, "offset_iqr_ms": spread, "window_frames": n, "fps": FPS as i64})
}

/// Key presses (rising edges of KEY_BUTTONS) in mono microseconds.
pub fn key_presses(recs: &[Value]) -> Vec<u64> {
    let (mut prev, mut out) = (0u32, vec![]);
    for r in recs.iter().filter(|r| r["type"] == "in") {
        let b = r["b"].as_u64().unwrap_or(0) as u32;
        if (b & !prev) & hid::KEY_BUTTONS != 0 { out.push(r["t_us"].as_u64().unwrap_or(0)); }
        prev = b;
    }
    out
}

/// Evenly sample up to `max` events, skipping ones within 400 ms of the previous pick.
pub fn sample(ev: &[u64], max: usize) -> Vec<u64> {
    let mut sp: Vec<u64> = vec![];
    for &t in ev { if sp.last().map(|l| t >= l + 400_000).unwrap_or(true) { sp.push(t); } }
    if sp.len() <= max { return sp; }
    (0..max).map(|i| sp[i * sp.len() / max]).collect()
}

/// Deterministic "chance" times: midpoints of gaps >= 2.5 s between any two inputs.
pub fn chance_times(all_input_us: &[u64], max: usize) -> Vec<u64> {
    let mut c = vec![];
    for w in all_input_us.windows(2) { if w[1] - w[0] >= 2_500_000 { c.push((w[0] + w[1]) / 2); } }
    sample(&c, max)
}

/// Decode a window of `file` around `t_s` (video seconds) into per-frame diffs.
pub fn decode_window(ffmpeg: &str, file: &str, t_s: f64, n: usize) -> Result<Window, String> {
    let start = (t_s - PRE_S).max(0.0);
    let dur = (t_s - start) + (n as f64 + 6.0) / FPS;
    let out = Command::new(ffmpeg).args(["-v", "error", "-ss", &format!("{start:.3}"), "-i", file, "-t", &format!("{dur:.3}"),
        "-vf", &format!("fps={FPS},scale={W}:{H},format=gray"), "-f", "rawvideo", "-"]).stdin(Stdio::null()).stderr(Stdio::piped()).output()
        .map_err(|e| format!("ffmpeg: {e}"))?;
    if !out.status.success() { return Err(format!("ffmpeg failed: {}", String::from_utf8_lossy(&out.stderr).trim())); }
    let fr: Vec<&[u8]> = out.stdout.chunks_exact(W * H).collect();
    if fr.len() < 6 { return Err("too few frames decoded".into()); }
    let mut diffs = vec![0f32];
    for p in fr.windows(2) { diffs.push(p[0].iter().zip(p[1]).map(|(a, b)| (*a as i32 - *b as i32).unsigned_abs()).sum::<u32>() as f32 / (W * H) as f32); }
    Ok(Window { diffs, event: ((t_s - start) * FPS).round() as usize })
}

/// Video files and the mono_ms their init segment arrived (frame 0 of that file is taken to be ~that time).
pub fn video_starts(manifest: &[Value]) -> Vec<(String, u64)> {
    manifest.iter().filter(|m| m["type"] == "segment" && m["kind"] == "init")
        .map(|m| (m["file"].as_str().unwrap_or("video.mp4").to_string(), m["mono_ms"].as_u64().unwrap_or(0))).collect()
}

fn locate(starts: &[(String, u64)], t_us: u64) -> Option<(String, f64)> {
    let t_ms = t_us / 1000;
    starts.iter().rev().find(|(_, s)| *s <= t_ms).map(|(f, s)| (f.clone(), (t_ms - s) as f64 / 1000.0))
}

/// Full alignment over a bundle directory. `ffmpeg = None` skips frame differencing (reports why).
pub fn align_bundle(dir: &std::path::Path, recs: &[Value], manifest: &[Value], timeline: &[Value], wall_anchor_ms: Option<u64>, ffmpeg: Option<&str>, n: usize) -> Value {
    let presses = key_presses(recs);
    let all: Vec<u64> = recs.iter().filter(|r| r["type"] == "in").filter_map(|r| r["t_us"].as_u64()).collect();
    let starts = video_starts(manifest);
    let mut notes: Vec<String> = vec![];
    let mut out = if let (Some(ff), false) = (ffmpeg, starts.is_empty()) {
        let (mut pw, mut cw, mut errs) = (vec![], vec![], 0);
        for (ts, dst) in [(sample(&presses, MAX_EVENTS), &mut pw), (chance_times(&all, MAX_EVENTS / 2), &mut cw)] {
            for t in ts {
                if let Some((f, vt)) = locate(&starts, t) {
                    match decode_window(ff, &dir.join(&f).to_string_lossy(), vt, n) { Ok(w) => dst.push(w), Err(_) => errs += 1 }
                }
            }
        }
        if errs > 0 { notes.push(format!("{errs} video window(s) could not be decoded")); }
        summarize(&pw, &cw, n)
    } else {
        notes.push(if starts.is_empty() { "no video segments in manifest".into() } else { "ffmpeg not available: frame differencing skipped".into() });
        summarize(&[], &[], n)
    };
    // score events (scorebug reader) -> controller activity in the preceding 30 s
    if let Some(anchor) = wall_anchor_ms {
        let mut last: Option<(i64, i64)> = None;
        let (mut ev, mut with) = (0usize, 0usize);
        for r in timeline.iter().filter(|r| r.get("home").is_some()) {
            let hs = (r["home"].as_i64().unwrap_or(0), r["away"].as_i64().unwrap_or(0));
            if let Some(p) = last { if p != hs {
                ev += 1;
                let t_us = r["t_ms"].as_u64().unwrap_or(0).saturating_sub(anchor) * 1000;
                if all.iter().any(|&x| x <= t_us && x + 30_000_000 >= t_us) { with += 1; }
            } }
            last = Some(hs);
        }
        out["score_events"] = json!(ev);
        out["score_events_with_input_30s"] = json!(with);
    }
    out["key_presses"] = json!(presses.len());
    out["notes"] = json!(notes);
    out["method"] = json!("frame differencing (48x27 gray, 30 fps) after sampled key presses vs chance times; offset = median press->change lag");
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    fn win(spike_at: Option<usize>, event: usize, noise: f32) -> Window {
        let mut d: Vec<f32> = (0..event + 20).map(|i| 1.0 + noise * ((i * 7 % 5) as f32)).collect();
        if let Some(s) = spike_at { d[event + s] = 40.0; }
        Window { diffs: d, event }
    }
    #[test]
    fn matched_presses_give_rate_and_offset() {
        let press: Vec<Window> = (0..20).map(|i| win(if i < 16 { Some(3 + i % 2) } else { None }, 30, 0.3)).collect();
        let chance: Vec<Window> = (0..10).map(|i| win(if i == 0 { Some(8) } else { None }, 30, 0.3)).collect();
        let s = summarize(&press, &chance, 15);
        assert_eq!(s["match_permille"], 800);
        assert_eq!(s["chance_permille"], 100);
        assert_eq!(s["offset_ms"], 133); // median lag 4 frames at 30 fps
    }
    #[test]
    fn late_change_outside_window_is_not_a_match() {
        assert_eq!(responds(&win(Some(18), 30, 0.0), 15), None);
        assert_eq!(responds(&win(Some(2), 30, 0.0), 15), Some(2));
    }
    #[test]
    fn sampling_and_chance_times() {
        let ev: Vec<u64> = (0..200).map(|i| i * 100_000).collect();
        let s = sample(&ev, 60);
        assert!(s.len() <= 60 && s.windows(2).all(|w| w[1] - w[0] >= 400_000));
        assert_eq!(chance_times(&[0, 1_000_000, 5_000_000], 10), vec![3_000_000]);
    }
    #[test]
    fn decodes_testsrc_if_ffmpeg_present() {
        let Ok(st) = Command::new("ffmpeg").arg("-version").stdout(Stdio::null()).status() else { return; };
        if !st.success() { return; }
        let dir = std::env::temp_dir().join(format!("qorcap-align-{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let f = dir.join("v.mp4");
        let ok = Command::new("ffmpeg").args(["-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30", "-t", "4", "-pix_fmt", "yuv420p", f.to_str().unwrap()]).status().map(|s| s.success()).unwrap_or(false);
        if !ok { return; }
        let w = decode_window("ffmpeg", f.to_str().unwrap(), 2.0, 15).unwrap();
        assert_eq!(w.event, 30);
        assert!(w.diffs.len() >= 40);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
