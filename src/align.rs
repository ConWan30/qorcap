//! Input <-> video alignment (v2, QorCap 0.3.2).
//!
//! v1 differenced a few 48x27 windows after 60 sampled presses and asked "did the picture change?". On a real
//! Madden 27 match that sat at chance (383 vs 345 permille): the broadcast camera is always moving, so a busy
//! frame difference says nothing about a press, and the video clock was taken from the *init segment arrival*,
//! which lands ~4-5 s after frame 0 on a real card (device open + encoder warm-up + first fragment).
//!
//! v2:
//! * **Clock**: frame index <-> QorCap monotonic clock from a least-squares fit of every media segment's
//!   (`mono_ms`, `frames`) in the manifest (per video file), not the init segment.
//! * **One decode pass**: the whole video is decoded once at 96x54 gray; motion energy per frame is the 2-frame
//!   mean absolute difference (robust to the duplicate frames CFR capture inserts).
//! * **Relative onset in static context**: a press is tested only when the screen was nearly still for the
//!   preceding 20 frames (play-call screens, pre-snap, menus, replays paused) - the places where a press
//!   causes a discrete change. It matches when motion energy within 100-667 ms jumps to >= 5x the local
//!   baseline. Mid-play presses (camera already moving) are counted but not scored.
//! * **Calibrated offset, held out**: the residual offset (pipeline latency + game response) is searched in
//!   +/-3 s on one half of the presses and scored on the other half (2-fold), so the reported rate is never
//!   evaluated at an offset picked on the same presses.
//! * **Honest chance**: the same press times shifted by a deterministic pseudo-random 3-8 s (and >= 700 ms from
//!   any press), through the same static-context filter and the same held-out offset.
//!
//! Score events from the scorebug reader (timeline.jsonl) are checked for controller activity in the 30 s
//! before each scoring change. Limits: this shows the inputs and the picture move together; it is a
//! statistical signal over many presses, not proof for any single press, and says nothing about who played.
use crate::frameclock::{self, FrameStamp};
use crate::hid;
use serde_json::{json, Value};
use std::io::Read;
use std::process::{Command, Stdio};

pub const FPS: f64 = 30.0;
/// Response window length in frames (window = frames 3 .. 3+n+2 after the press).
pub const DEFAULT_N_FRAMES: usize = 15;
const W: usize = 96; const H: usize = 54;
const PRE: usize = 20;          // baseline frames before the press
const A0: usize = 3;            // earliest response frame (100 ms)
const STATIC_MAX: f32 = 4.0;    // baseline 2-frame motion energy (gray levels) for a "still" screen
const RATIO: f32 = 5.0;         // onset: energy >= RATIO * (baseline + EPS)
const EPS: f32 = 0.5;
const SEARCH: i64 = 90;         // offset search +/- frames (3 s)
const STEP: i64 = 3;
const MIN_GAP_US: u64 = 400_000;
const CHANCE_MIN_US: u64 = 3_000_000;
const CHANCE_SPAN_US: u64 = 5_000_000;
const CHANCE_CLEAR_US: u64 = 700_000;
pub const MAX_EVENTS: usize = 5000;

/// Motion energy per frame for one video file (index = frame number at `FPS`).
#[derive(Debug, Clone, Default)]
pub struct Energy { pub e: Vec<f32> }

/// One press tested against the energy track: Some(lag frames) on a match, None if not tested/static.
pub fn press_response(e: &[f32], k: i64, n: usize) -> Option<Option<usize>> {
    let b = A0 + n + 2;
    if k < PRE as i64 || k as usize + b >= e.len() { return None; }
    let k = k as usize;
    let mut pre: Vec<f32> = e[k - PRE..k].to_vec();
    pre.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let base = pre[pre.len() / 2];
    if base >= STATIC_MAX { return None; }
    let thr = RATIO * (base + EPS);
    Some((A0..b).find(|&j| e[k + j] > thr))
}

/// Energy from consecutive gray frames: 2-frame mean absolute difference.
pub fn energy_from_frames(frames: &[&[u8]]) -> Vec<f32> {
    let d: Vec<f32> = std::iter::once(0.0).chain(frames.windows(2).map(|p| {
        p[0].iter().zip(p[1]).map(|(a, b)| (*a as i32 - *b as i32).unsigned_abs()).sum::<u32>() as f32 / p[0].len() as f32
    })).collect();
    (0..d.len()).map(|i| d[i] + if i > 0 { d[i - 1] } else { 0.0 }).collect()
}

/// Decode a whole file once at 96x54 gray, streaming (constant memory).
pub fn decode_energy(ffmpeg: &str, file: &str) -> Result<Energy, String> {
    let mut child = Command::new(ffmpeg).args(["-v", "error", "-threads", "0", "-i", file, "-an",
        "-vf", &format!("fps={FPS},scale={W}:{H}:flags=fast_bilinear,format=gray"), "-f", "rawvideo", "-"])
        .stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().map_err(|e| format!("ffmpeg: {e}"))?;
    let mut out = child.stdout.take().unwrap();
    let (mut prev, mut cur) = (vec![0u8; W * H], vec![0u8; W * H]);
    let (mut d, mut first) = (Vec::<f32>::new(), true);
    loop {
        if out.read_exact(&mut cur).is_err() { break; }
        if first { d.push(0.0); first = false; } else {
            d.push(prev.iter().zip(&cur).map(|(a, b)| (*a as i32 - *b as i32).unsigned_abs()).sum::<u32>() as f32 / (W * H) as f32);
        }
        std::mem::swap(&mut prev, &mut cur);
    }
    let _ = child.wait();
    if d.len() < 2 * PRE { return Err("too few frames decoded".into()); }
    Ok(Energy { e: (0..d.len()).map(|i| d[i] + if i > 0 { d[i - 1] } else { 0.0 }).collect() })
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
    for &t in ev { if sp.last().map(|l| t >= l + MIN_GAP_US).unwrap_or(true) { sp.push(t); } }
    if sp.len() <= max { return sp; }
    (0..max).map(|i| sp[i * sp.len() / max]).collect()
}

/// Deterministic chance times: each press shifted by a pseudo-random +/-3..8 s, kept only if >= 700 ms
/// from every press (same game-state mix as the presses, no press-caused change nearby).
pub fn chance_times(presses: &[u64], all_presses: &[u64]) -> Vec<u64> {
    let mut sorted = all_presses.to_vec(); sorted.sort();
    let clear = |c: u64| { let i = sorted.partition_point(|&x| x < c);
        [i.checked_sub(1), Some(i)].iter().flatten().filter_map(|&j| sorted.get(j)).all(|&x| x.abs_diff(c) >= CHANCE_CLEAR_US) };
    let mut out = vec![];
    for &t in presses {
        let mut h = t.wrapping_mul(0x9E37_79B9_7F4A_7C15) ^ 0xD1B5_4A32_D192_ED03;
        for _ in 0..8 {
            h ^= h >> 33; h = h.wrapping_mul(0xFF51_AFD7_ED55_8CCD); h ^= h >> 29;
            let sh = CHANCE_MIN_US + h % CHANCE_SPAN_US;
            let c = if h & (1 << 40) != 0 { t + sh } else { t.saturating_sub(sh) };
            if c > 0 && clear(c) { out.push(c); break; }
        }
    }
    out
}

/// Video file -> mono_ms of frame 0 and ms per frame, from the manifest's media segments.
#[derive(Debug, Clone, PartialEq)]
pub struct FileClock { pub file: String, pub start_ms: f64, pub ms_per_frame: f64, pub end_ms: f64, pub fitted: bool }

pub fn file_clocks(manifest: &[Value]) -> Vec<FileClock> {
    let mut out: Vec<FileClock> = vec![];
    let segs: Vec<&Value> = manifest.iter().filter(|m| m["type"] == "segment").collect();
    let mut files: Vec<String> = vec![];
    for s in &segs { let f = s["file"].as_str().unwrap_or("video.mp4").to_string(); if !files.contains(&f) { files.push(f); } }
    for f in files {
        let mine: Vec<&&Value> = segs.iter().filter(|s| s["file"].as_str().unwrap_or("video.mp4") == f).collect();
        let pts: Vec<(f64, f64)> = mine.iter().filter(|s| s["kind"] == "media")
            .filter_map(|s| Some((s["frames"].as_f64()?, s["mono_ms"].as_f64()?))).filter(|(fr, _)| *fr > 0.0).collect();
        let init = mine.iter().find(|s| s["kind"] == "init").and_then(|s| s["mono_ms"].as_f64())
            .or_else(|| mine.first().and_then(|s| s["mono_ms"].as_f64())).unwrap_or(0.0);
        let end = mine.iter().filter_map(|s| s["mono_ms"].as_f64()).fold(init, f64::max);
        let nominal = 1000.0 / FPS;
        let fit = if pts.len() >= 5 && pts.last().unwrap().0 - pts[0].0 >= 10.0 * FPS {
            let n = pts.len() as f64;
            let (mx, my) = (pts.iter().map(|p| p.0).sum::<f64>() / n, pts.iter().map(|p| p.1).sum::<f64>() / n);
            let sxy: f64 = pts.iter().map(|p| (p.0 - mx) * (p.1 - my)).sum();
            let sxx: f64 = pts.iter().map(|p| (p.0 - mx).powi(2)).sum();
            let slope = if sxx > 0.0 { sxy / sxx } else { nominal };
            // a sane capture runs within 5 % of nominal fps; otherwise use nominal through the mean point
            let slope = if (slope / nominal - 1.0).abs() <= 0.05 { slope } else { nominal };
            Some((my - slope * mx, slope))
        } else { None };
        let (start, mpf, fitted) = match fit { Some((s, m)) => (s, m, true), None => (init, nominal, false) };
        out.push(FileClock { file: f, start_ms: start, ms_per_frame: mpf, end_ms: end, fitted });
    }
    out
}

fn locate(clocks: &[FileClock], t_us: u64) -> Option<(usize, i64)> {
    let t = t_us as f64 / 1000.0;
    let i = clocks.iter().rposition(|c| c.start_ms <= t + 3000.0)?;
    Some((i, ((t - clocks[i].start_ms) / clocks[i].ms_per_frame).round() as i64))
}

/// Nearest captured frame on the measured clock, if a stamp for one of `clocks` is within
/// [`frameclock::MEASURED_MAX_GAP_US`]. `None` means "use the manifest fit".
pub fn measured_locate(clocks: &[FileClock], stamps: &[FrameStamp], t_us: u64) -> Option<(usize, i64)> {
    if stamps.is_empty() { return None; }
    let i = stamps.partition_point(|s| s.t_mono_us < t_us);
    let mut best: Option<(u64, usize, i64)> = None;
    for j in [i.wrapping_sub(1), i] {
        let Some(s) = stamps.get(j) else { continue };
        let d = s.t_mono_us.abs_diff(t_us);
        if d > frameclock::MEASURED_MAX_GAP_US { continue; }
        let Some(ci) = clocks.iter().position(|c| c.file == s.file) else { continue; };
        if best.as_ref().map(|(bd, _, _)| d < *bd).unwrap_or(true) { best = Some((d, ci, s.frame)); }
    }
    best.map(|(_, ci, fr)| (ci, fr))
}

fn locate_frame(clocks: &[FileClock], stamps: &[FrameStamp], t_us: u64) -> Option<(usize, i64)> {
    measured_locate(clocks, stamps, t_us).or_else(|| locate(clocks, t_us))
}

/// Score a set of event times at offset `off` frames: (tested, matched, lags of matches).
fn score(tracks: &[Energy], clocks: &[FileClock], stamps: &[FrameStamp], ts: &[u64], off: i64, n: usize) -> (usize, usize, Vec<usize>) {
    let (mut t, mut m, mut lags) = (0, 0, vec![]);
    for &x in ts {
        let Some((i, k)) = locate_frame(clocks, stamps, x) else { continue };
        let Some(tr) = tracks.get(i) else { continue };
        if tr.e.is_empty() { continue; }
        if let Some(r) = press_response(&tr.e, k + off, n) { t += 1; if let Some(l) = r { m += 1; lags.push(l); } }
    }
    (t, m, lags)
}

/// Offset (frames) maximising the match rate of `ts`; ties -> smallest |offset|.
fn calibrate(tracks: &[Energy], clocks: &[FileClock], stamps: &[FrameStamp], ts: &[u64], n: usize) -> i64 {
    let mut best = (f64::MIN, 0i64);
    let mut o = -SEARCH;
    while o <= SEARCH {
        let (t, m, _) = score(tracks, clocks, stamps, ts, o, n);
        let r = if t >= 3 { m as f64 / t as f64 } else { -1.0 };
        if r > best.0 + 1e-9 || ((r - best.0).abs() <= 1e-9 && o.abs() < best.1.abs()) { best = (r, o); }
        o += STEP;
    }
    best.1
}

/// Core of v2 on precomputed energy tracks (unit-testable without ffmpeg).
pub fn align_tracks(tracks: &[Energy], clocks: &[FileClock], presses: &[u64], n: usize) -> Value {
    align_tracks_with(tracks, clocks, presses, n, &[])
}

/// Like [`align_tracks`]. Non-empty `stamps` locate a press on the measured frame clock when a stamp
/// is within 500 ms; every other press uses the manifest fit. An empty stamp list is the fit alone,
/// and the JSON is the same object [`align_tracks`] has always returned.
pub fn align_tracks_with(tracks: &[Energy], clocks: &[FileClock], presses: &[u64], n: usize, stamps: &[FrameStamp]) -> Value {
    let pts = sample(presses, MAX_EVENTS);
    let ch = chance_times(&pts, presses);
    let (a, b): (Vec<u64>, Vec<u64>) = (pts.iter().step_by(2).copied().collect(), pts.iter().skip(1).step_by(2).copied().collect());
    let (ca, cb): (Vec<u64>, Vec<u64>) = (ch.iter().step_by(2).copied().collect(), ch.iter().skip(1).step_by(2).copied().collect());
    let (oa, ob) = (calibrate(tracks, clocks, stamps, &a, n), calibrate(tracks, clocks, stamps, &b, n));
    // held out: B scored at A's offset, A at B's offset; same for chance
    let (t1, m1, l1) = score(tracks, clocks, stamps, &b, oa, n);
    let (t2, m2, l2) = score(tracks, clocks, stamps, &a, ob, n);
    let (ct1, cm1, _) = score(tracks, clocks, stamps, &cb, oa, n);
    let (ct2, cm2, _) = score(tracks, clocks, stamps, &ca, ob, n);
    let (pt, pm_, ct, cm) = (t1 + t2, m1 + m2, ct1 + ct2, cm1 + cm2);
    let pm = |a: usize, b: usize| if b == 0 { Value::Null } else { json!((a as f64 * 1000.0 / b as f64).round() as i64) };
    let fms = |f: f64| (f * 1000.0 / FPS).round() as i64;
    let mut ms: Vec<i64> = l1.iter().map(|l| fms(*l as f64 + oa as f64)).chain(l2.iter().map(|l| fms(*l as f64 + ob as f64))).collect();
    ms.sort();
    let offset = if ms.is_empty() { Value::Null } else { json!(ms[ms.len() / 2]) };
    let spread = if ms.len() >= 4 { json!(ms[ms.len() * 3 / 4] - ms[ms.len() / 4]) } else { Value::Null };
    // two-proportion z (x100) of press vs chance rate; integer so the receipt stays float-free
    let z = if pt > 0 && ct > 0 {
        let (p1, p2) = (pm_ as f64 / pt as f64, cm as f64 / ct as f64);
        let p = (pm_ + cm) as f64 / (pt + ct) as f64;
        let se = (p * (1.0 - p) * (1.0 / pt as f64 + 1.0 / ct as f64)).sqrt();
        if se > 0.0 { json!(((p1 - p2) / se * 100.0).round() as i64) } else { Value::Null }
    } else { Value::Null };
    let (all_t, _, _) = score(tracks, clocks, stamps, &pts, 0, n);
    let _ = all_t;
    let mut out = json!({"presses_tested": pt, "presses_matched": pm_, "match_permille": pm(pm_, pt),
           "chance_tested": ct, "chance_matched": cm, "chance_permille": pm(cm, ct),
           "z_x100": z, "offset_ms": offset, "offset_iqr_ms": spread,
           "calibration_offsets_ms": [fms(oa as f64), fms(ob as f64)],
           "presses_spaced": pts.len(), "window_frames": n, "fps": FPS as i64,
           "clock_fitted": clocks.iter().all(|c| c.fitted)});
    // Absent unless a measured clock was supplied. Adding the key on the fit path would change
    // input_proof.json for every bundle recorded with the flag off.
    if !stamps.is_empty() { out["clock_source"] = json!("frame_clock"); out["frame_clock_frames"] = json!(stamps.len()); }
    out
}

/// Full alignment over a bundle directory. `ffmpeg = None` skips the video pass (reports why).
pub fn align_bundle(dir: &std::path::Path, recs: &[Value], manifest: &[Value], timeline: &[Value], wall_anchor_ms: Option<u64>, ffmpeg: Option<&str>, n: usize) -> Value {
    let presses = key_presses(recs);
    let all: Vec<u64> = recs.iter().filter(|r| r["type"] == "in").filter_map(|r| r["t_us"].as_u64()).collect();
    let clocks = file_clocks(manifest);
    let stamps = frameclock::load(&dir.join(frameclock::FILE_NAME));
    let mut notes: Vec<String> = vec![];
    let mut out = if let (Some(ff), false) = (ffmpeg, clocks.is_empty()) {
        let mut tracks = vec![];
        for c in &clocks {
            match crate::bundle::contained_file(dir, &c.file).and_then(|p| decode_energy(ff, &p.to_string_lossy()).map_err(|e| e)) {
                Ok(e) => tracks.push(e),
                Err(e) => { notes.push(format!("{}: {e}", c.file)); tracks.push(Energy::default()); }
            }
        }
        if clocks.iter().any(|c| !c.fitted) { notes.push("video clock from init segment (too few segments to fit frames vs time)".into()); }
        if !stamps.is_empty() { notes.push("frame times from frame_clock.jsonl where a stamp is within 500 ms of the press; manifest fit otherwise".into()); }
        align_tracks_with(&tracks, &clocks, &presses, n, &stamps)
    } else {
        notes.push(if clocks.is_empty() { "no video segments in manifest".into() } else { "ffmpeg not available: video pass skipped".into() });
        align_tracks(&[], &[], &[], n)
    };
    if out["presses_tested"].as_u64().unwrap_or(0) < 20 {
        notes.push("fewer than 20 presses landed on a still screen: alignment is low-confidence".into());
    }
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
    out["method"] = json!("v2: one-pass 96x54 motion energy; presses on a still screen (20-frame baseline) match when energy jumps >=5x within 100-667 ms; offset calibrated on half the presses, scored on the other half (2-fold); chance = presses shifted 3-8 s through the same filter");
    out
}

fn pct(xs: &mut [i64], p: usize) -> i64 {
    if xs.is_empty() { return 0; }
    xs.sort();
    xs[(xs.len() - 1) * p / 100]
}

/// Clock A/B. `fit` is the manifest straight line (fragment close vs frame count): typical / p95 / worst
/// absolute residual, in ms. `measured` is present only when `stamps` is non-empty: those same percentiles
/// for |stamp time − fitted time of that frame|, plus how far consecutive stamp gaps sit from one nominal frame.
/// This does not write a receipt.
pub fn clock_ab(manifest: &[Value], stamps: &[FrameStamp]) -> Value {
    let clocks = file_clocks(manifest);
    let Some(c) = clocks.iter().find(|c| c.fitted).or_else(|| clocks.first()) else {
        return json!({"fit": Value::Null, "measured": Value::Null});
    };
    let segs: Vec<&Value> = manifest.iter().filter(|m| m["type"] == "segment" && m["kind"] == "media" && m["file"] == c.file).collect();
    let mut resid: Vec<i64> = segs.iter().filter_map(|s| {
        let fr = s["frames"].as_f64()?;
        let mono = s["mono_ms"].as_f64()?;
        if fr <= 0.0 { return None; }
        Some((mono - (c.start_ms + c.ms_per_frame * fr)).round() as i64)
    }).map(|r| r.abs()).collect();
    let fit = json!({
        "file": c.file, "fitted": c.fitted,
        "ms_per_frame_milli": (c.ms_per_frame * 1000.0).round() as i64,
        "n": resid.len(),
        "resid_abs_p50_ms": pct(&mut resid, 50),
        "resid_abs_p95_ms": pct(&mut resid, 95),
        "resid_abs_max_ms": resid.iter().copied().max().unwrap_or(0),
    });
    let mine: Vec<&FrameStamp> = stamps.iter().filter(|s| s.file == c.file).collect();
    if mine.is_empty() { return json!({"fit": fit, "measured": Value::Null}); }
    let mut vs: Vec<i64> = mine.iter().map(|s| {
        let fitted = c.start_ms + c.ms_per_frame * s.frame as f64;
        (s.t_mono_us as f64 / 1000.0 - fitted).round().abs() as i64
    }).collect();
    let nominal_us = (1_000_000.0 / FPS).round() as i64;
    let mut gaps: Vec<i64> = mine.windows(2).map(|w| {
        ((w[1].t_mono_us as i64 - w[0].t_mono_us as i64) - nominal_us).abs() / 1000
    }).collect();
    json!({
        "fit": fit,
        "measured": {
            "n": mine.len(),
            "vs_fit_abs_p50_ms": pct(&mut vs, 50),
            "vs_fit_abs_p95_ms": pct(&mut vs, 95),
            "vs_fit_abs_max_ms": vs.iter().copied().max().unwrap_or(0),
            "interval_abs_dev_p50_ms": pct(&mut gaps, 50),
            "interval_abs_dev_p95_ms": pct(&mut gaps, 95),
            "interval_abs_dev_max_ms": gaps.iter().copied().max().unwrap_or(0),
        }
    })
}

/// Offline A/B for one bundle. With a video and ffmpeg, scores the input proof's alignment twice
/// (manifest fit, then measured stamps) on one decode. Never writes the receipt or the proof.
pub fn ab_report(dir: &std::path::Path, ffmpeg: Option<&str>, n: usize) -> Value {
    let read = |name: &str| frameclock_read(dir, name);
    let manifest = read("manifest.jsonl");
    let inputs = read("inputs.jsonl");
    let stamps = frameclock::load(&dir.join(frameclock::FILE_NAME));
    let mut out = clock_ab(&manifest, &stamps);
    out["frame_clock_file"] = json!(dir.join(frameclock::FILE_NAME).exists());
    out["frame_clock_frames"] = json!(stamps.len());
    let presses = key_presses(&inputs);
    let clocks = file_clocks(&manifest);
    if let Some(ff) = ffmpeg {
        if clocks.is_empty() {
            out["video"] = json!("no segments");
        } else {
            let mut tracks = vec![];
            for c in &clocks {
                match decode_energy(ff, &dir.join(&c.file).to_string_lossy()) {
                    Ok(e) => tracks.push(e),
                    Err(e) => { out["video_error"] = json!(e); tracks.push(Energy::default()); }
                }
            }
            out["fit_alignment"] = align_tracks_with(&tracks, &clocks, &presses, n, &[]);
            out["measured_alignment"] = if stamps.is_empty() { Value::Null } else { align_tracks_with(&tracks, &clocks, &presses, n, &stamps) };
            out["video"] = json!("decoded");
        }
    } else {
        out["video"] = json!("skipped");
    }
    out["how_to_read"] = json!("fit.resid_* is the fragment-close straight line (flag off). measured.vs_fit_* is how far real frame arrivals sit from that line. measured.interval_* is frame-to-frame jitter of the arrivals (one frame is 33 ms). fit_alignment / measured_alignment are the input-proof scores. This command does not write a receipt.");
    out
}

fn frameclock_read(dir: &std::path::Path, name: &str) -> Vec<Value> {
    std::fs::read_to_string(dir.join(name)).map(|s| s.lines().filter_map(|l| serde_json::from_str(l).ok()).collect()).unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;
    /// Synthetic track: still screen (energy ~1) with a spike `lag` frames after each listed frame.
    fn track(len: usize, spikes: &[usize], lag: usize) -> Energy {
        let mut e: Vec<f32> = (0..len).map(|i| 1.0 + 0.2 * ((i * 7 % 5) as f32)).collect();
        for &s in spikes { for j in 0..4 { if s + lag + j < len { e[s + lag + j] = 40.0; } } }
        Energy { e }
    }
    fn clock(start_ms: f64) -> Vec<FileClock> { vec![FileClock { file: "video.mp4".into(), start_ms, ms_per_frame: 1000.0 / FPS, end_ms: 1e9, fitted: true }] }

    #[test]
    fn responsive_presses_beat_chance_and_offset_is_recovered() {
        // presses every ~1.37 s; picture reacts 6 frames later, but the clock is 1 s (30 frames) off
        let frames: Vec<usize> = (0..120).map(|i| 200 + i * 41).collect();
        let tr = track(6000, &frames, 6 + 30);
        let presses: Vec<u64> = frames.iter().map(|&f| (f as f64 * 1000.0 / FPS * 1000.0) as u64).collect();
        let s = align_tracks(&[tr], &clock(0.0), &presses, 15);
        assert!(s["match_permille"].as_i64().unwrap() >= 900, "{s:#}");
        assert!(s["chance_permille"].as_i64().unwrap() <= 300, "{s:#}");
        let off = s["offset_ms"].as_i64().unwrap();
        assert!((1150..=1300).contains(&off), "{s:#}");
        assert!(s["z_x100"].as_i64().unwrap() > 300);
    }
    #[test]
    fn unresponsive_video_is_at_chance() {
        // random spikes unrelated to presses
        let mut st = 12345u64; let mut r = || { st ^= st << 13; st ^= st >> 7; st ^= st << 17; st };
        let spikes: Vec<usize> = (0..300).map(|_| (r() % 5900) as usize).collect();
        let tr = track(6000, &spikes, 0);
        let presses: Vec<u64> = (0..120).map(|i| ((200 + i * 41) as f64 * 1000.0 / FPS * 1000.0) as u64).collect();
        let s = align_tracks(&[tr], &clock(0.0), &presses, 15);
        let (m, c) = (s["match_permille"].as_i64().unwrap(), s["chance_permille"].as_i64().unwrap());
        assert!((m - c).abs() <= 200, "{s:#}");
    }
    #[test]
    fn busy_screen_is_not_scored() {
        let tr = Energy { e: vec![20.0; 2000] };
        assert_eq!(press_response(&tr.e, 500, 15), None);
        let still = track(2000, &[500], 4);
        assert_eq!(press_response(&still.e, 500, 15), Some(Some(4)));
        assert_eq!(press_response(&still.e, 500 + 100, 15), Some(None));
    }
    #[test]
    fn clock_fit_uses_segments_not_init() {
        // frame 0 at mono 4000 ms; init segment arrives at 9300 ms with 160 frames already captured
        let mut m = vec![json!({"type":"segment","kind":"init","file":"video.mp4","mono_ms":9300,"frames":160})];
        for i in 0..50 { let fr = 160 + i * 60; m.push(json!({"type":"segment","kind":"media","file":"video.mp4","mono_ms": 4000 + fr * 1000 / 30,"frames":fr})); }
        let c = file_clocks(&m);
        assert!(c[0].fitted && (c[0].start_ms - 4000.0).abs() < 40.0, "{c:?}");
        let short = vec![json!({"type":"segment","kind":"init","file":"video.mp4","mono_ms":0})];
        assert_eq!(file_clocks(&short)[0].start_ms, 0.0);
    }
    #[test]
    fn sampling_and_chance_times() {
        let ev: Vec<u64> = (0..200).map(|i| i * 100_000).collect();
        let s = sample(&ev, 60);
        assert!(s.len() <= 60 && s.windows(2).all(|w| w[1] - w[0] >= 400_000));
        let p: Vec<u64> = vec![10_000_000, 30_000_000, 50_000_000];
        let c = chance_times(&p, &p);
        assert_eq!(c, chance_times(&p, &p)); // deterministic
        for (x, t) in c.iter().zip(&p) { let d = x.abs_diff(*t); assert!((3_000_000..8_000_000).contains(&d)); }
        assert!(c.iter().all(|x| p.iter().all(|t| x.abs_diff(*t) >= 700_000)));
    }
    #[test]
    fn energy_is_two_frame_and_dup_robust() {
        let a = vec![0u8; 16]; let b = vec![100u8; 16];
        let e = energy_from_frames(&[&a, &a, &b, &b]);
        assert_eq!(e, vec![0.0, 0.0, 100.0, 100.0]);
    }

    /// Flag off: no stamps. The alignment object is the one `align_tracks` has always returned.
    #[test]
    fn flag_off_alignment_json_matches_the_fit_byte_for_byte() {
        let frames: Vec<usize> = (0..40).map(|i| 200 + i * 41).collect();
        let tr = track(3000, &frames, 6);
        let presses: Vec<u64> = frames.iter().map(|&f| (f as f64 * 1000.0 / FPS * 1000.0) as u64).collect();
        let clocks = clock(0.0);
        let fit = align_tracks(&[tr.clone()], &clocks, &presses, 15);
        let with_empty = align_tracks_with(&[tr], &clocks, &presses, 15, &[]);
        assert_eq!(serde_json::to_string(&fit).unwrap(), serde_json::to_string(&with_empty).unwrap());
        assert!(fit.get("clock_source").is_none());
        assert!(fit.get("frame_clock_frames").is_none());
    }

    #[test]
    fn measured_stamp_overrides_an_800ms_fit_and_falls_back_past_500ms() {
        let clocks = clock(0.0);
        let t = 800_000u64;
        assert_eq!(locate(&clocks, t).unwrap().1, 24); // 800 ms / (1000/30) = frame 24
        let near = FrameStamp { file: "video.mp4".into(), frame: 0, t_mono_us: t, pts_90k: 0 };
        assert_eq!(measured_locate(&clocks, &[near.clone()], t).unwrap().1, 0);
        assert_eq!(locate_frame(&clocks, &[near], t).unwrap().1, 0);
        let far = FrameStamp { file: "video.mp4".into(), frame: 0, t_mono_us: t + 600_000, pts_90k: 0 };
        assert!(measured_locate(&clocks, &[far.clone()], t).is_none());
        assert_eq!(locate_frame(&clocks, &[far], t).unwrap().1, 24);
    }

    /// One offset cannot absorb a clock that is early in one stretch and late in the next.
    /// Stamps on the same Instant as the presses recover the picture.
    #[test]
    fn wandering_fit_misses_and_measured_stamps_recover() {
        let n = 40i64;
        let mut presses = Vec::new();
        let mut spikes = Vec::new();
        let mut stamps = Vec::new();
        for i in 0..n {
            // Farther apart than the +/-3 s offset search, so a press cannot match its neighbour.
            // The two halves sit 40 frames on opposite sides of the fit, outside one response window.
            let fit_frame = 400 + i * 120;
            let t_us = (fit_frame as f64 * 1_000_000.0 / FPS) as u64;
            let bias: i64 = if i < n / 2 { 40 } else { -40 };
            let true_frame = fit_frame + bias;
            presses.push(t_us);
            spikes.push((true_frame + 6) as usize);
            stamps.push(FrameStamp { file: "video.mp4".into(), frame: true_frame, t_mono_us: t_us, pts_90k: true_frame as u64 * 3000 });
        }
        let len = spikes.iter().copied().max().unwrap() + 40;
        let tr = track(len, &spikes, 0);
        let clocks = clock(0.0);
        let fit = align_tracks(&[tr.clone()], &clocks, &presses, 15);
        let measured = align_tracks_with(&[tr], &clocks, &presses, 15, &stamps);
        let (fm, mm) = (fit["match_permille"].as_i64().unwrap(), measured["match_permille"].as_i64().unwrap());
        assert!(mm >= 800, "measured {measured:#}");
        assert!(mm - fm >= 300, "fit {fm} measured {mm}\n{fit:#}");
        assert_eq!(measured["clock_source"], "frame_clock");
        assert!(fit.get("clock_source").is_none());
    }

    #[test]
    fn clock_ab_reports_fit_residuals_and_leaves_measured_null_without_stamps() {
        let mut m = vec![json!({"type":"segment","kind":"init","file":"video.mp4","mono_ms":0,"frames":0})];
        for i in 1..20 {
            let fr = i * 60;
            m.push(json!({"type":"segment","kind":"media","file":"video.mp4","mono_ms": fr * 1000 / 30, "frames": fr}));
        }
        let z = clock_ab(&m, &[]);
        assert!(z["measured"].is_null());
        assert!(z["fit"]["resid_abs_max_ms"].as_i64().unwrap() <= 1, "{z:#}");
        let mut late = m.clone();
        let n = late.len() - 1;
        late[n]["mono_ms"] = json!(late[n]["mono_ms"].as_i64().unwrap() + 800);
        let bad = clock_ab(&late, &[]);
        assert!(bad["fit"]["resid_abs_max_ms"].as_i64().unwrap() >= 400, "{bad:#}");
        let stamps: Vec<FrameStamp> = (0..10).map(|f| FrameStamp {
            file: "video.mp4".into(),
            frame: f,
            t_mono_us: (f as f64 * 1_000_000.0 / FPS).round() as u64,
            pts_90k: f as u64 * 3000,
        }).collect();
        let ok = clock_ab(&m, &stamps);
        assert!(ok["measured"]["vs_fit_abs_max_ms"].as_i64().unwrap() <= 2, "{ok:#}");
        assert_eq!(ok["measured"]["interval_abs_dev_max_ms"], 0);
    }
}
