//! Rumble Echo: game-driven controller rumble as a third, human-free timing clock (advisory, opt-in).
//!
//! The console sends rumble to the pad at about the time it renders the frame that caused it (a hit, a
//! tackle). QorCap never sees that command. It only sees the pad shake, through the IMU in the USB input
//! reports it already reads (`haptics.rs`, `haptics.jsonl`). Nothing here talks to the controller: no
//! output report, no feature report, no Bluetooth. It reads files in a finished bundle.
//!
//! Method: a rumble *onset* is a high-confidence haptic event after at least 1 s of no haptic events.
//! A *spike* is a frame whose 2-frame motion energy jumps to >= 4x the median of the 10 frames before it
//! (and >= 6 gray levels) from below that line. For each onset, every spike 0-1500 ms later is a candidate
//! lag (frame time - onset time). A 1-frame histogram of all candidates, smoothed over 3 bins, peaks at the
//! capture latency. Each onset's candidate nearest that peak (within 50 ms) is its anchor.
//! `latency_ms` is the median anchor, `iqr_ms` their spread. Chance: the same onsets shifted by 4.1 s,
//! with their own peak search. Scorable needs >= 3 anchors, IQR <= 100 ms, >= 30 % of onsets paired, and
//! at least twice the chance pairs.
//!
//! Frame time is the frame_clock.jsonl arrival when present (`clock_source: frame_clock`), otherwise the
//! manifest fit. The IMU detector adds its own small delay (32 ms window, actuator rise), so this
//! latency reads slightly short of the true pipeline latency. The press offset also includes the game's
//! response to a button, so the two clocks can disagree by that response time.
//!
//! Output is only `rumble_echo.json`, written by `qorcap align-ab DIR --rumble-echo`. It is not in the
//! hash chain, the receipt, or the input proof. Its status is `ok`, `partial` (disagrees with the press
//! offset by more than 100 ms), or `unscorable`. It can never raise anything and never fails a bundle.
use crate::align::{Energy, FileClock, FPS};
use crate::frameclock::FrameStamp;
use serde_json::{json, Value};

pub const SCHEMA: &str = "qorcap-rumble-echo/v0";
pub const FILE_NAME: &str = "rumble_echo.json";
pub const QUIET_US: u64 = 1_000_000;
pub const MAX_LAG_MS: f64 = 1500.0;
pub const PAIR_TOL_MS: f64 = 50.0;
pub const MIN_ANCHORS: usize = 3;
pub const MAX_IQR_MS: i64 = 100;
pub const AGREE_MS: i64 = 100;
/// At least this share of onsets (permille) must pair at the peak.
pub const MIN_PAIRED_PERMILLE: usize = 300;
pub const CHANCE_SHIFT_US: u64 = 4_100_000;
const SPIKE_PRE: usize = 10;
const SPIKE_RATIO: f32 = 4.0;
const SPIKE_MIN: f32 = 6.0;
const EPS: f32 = 0.5;

/// Rumble onsets (mono us): high-confidence haptic events with no haptic event of any confidence in the
/// second before. Records come from haptics.jsonl or the `haptic` lines of inputs.jsonl.
pub fn onsets(haptics: &[Value]) -> Vec<u64> {
    let mut ev: Vec<(u64, bool)> = haptics.iter().filter(|r| r["type"] == "haptic")
        .filter_map(|r| Some((r["t_us"].as_u64()?, r["confidence_label"] == "high" || r["confidence"].as_u64().unwrap_or(0) >= 500)))
        .collect();
    ev.sort();
    let mut out = vec![];
    let mut last: Option<u64> = None;
    for (t, high) in ev {
        if high && last.map(|l| t >= l + QUIET_US).unwrap_or(true) { out.push(t); }
        last = Some(t);
    }
    out
}

/// Rising-edge visual spikes: frame k where energy crosses 4x the local 10-frame median (and >= 6).
pub fn spikes(e: &[f32]) -> Vec<usize> {
    let mut out = vec![];
    let mut above = false;
    for k in SPIKE_PRE..e.len() {
        let mut pre: Vec<f32> = e[k - SPIKE_PRE..k].to_vec();
        pre.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let thr = (SPIKE_RATIO * (pre[pre.len() / 2] + EPS)).max(SPIKE_MIN);
        let hit = e[k] >= thr;
        if hit && !above { out.push(k); }
        above = hit;
    }
    out
}

/// Frame -> time (ms) on the measured clock (arrival) when stamps exist for the file, else the fit line.
fn frame_times(c: &FileClock, stamps: &[FrameStamp], n: usize) -> (Vec<Option<f64>>, bool) {
    let mine: Vec<&FrameStamp> = stamps.iter().filter(|s| s.file == c.file).collect();
    if mine.is_empty() {
        return ((0..n).map(|k| Some(c.start_ms + c.ms_per_frame * k as f64)).collect(), false);
    }
    let mut t = vec![None; n];
    for s in mine { if s.frame >= 0 && (s.frame as usize) < n { t[s.frame as usize] = Some(s.t_mono_us as f64 / 1000.0); } }
    (t, true)
}

/// Candidate lags (ms) per onset, from every spike 0..=1500 ms after it, over all files.
fn candidates(on: &[u64], spike_times: &[f64]) -> Vec<Vec<f64>> {
    on.iter().map(|&t| {
        let t = t as f64 / 1000.0;
        let i = spike_times.partition_point(|&s| s < t);
        spike_times[i..].iter().take_while(|&&s| s - t <= MAX_LAG_MS).map(|&s| s - t).collect()
    }).collect()
}

/// Peak of a 1-frame lag histogram smoothed over 3 bins (ms), ties -> shortest lag.
fn peak(cands: &[Vec<f64>]) -> Option<f64> {
    let frame_ms = 1000.0 / FPS;
    let bins = (MAX_LAG_MS / frame_ms).ceil() as usize + 1;
    let mut h = vec![0usize; bins];
    for c in cands { for &l in c { h[((l / frame_ms).round() as usize).min(bins - 1)] += 1; } }
    let sm: Vec<usize> = (0..bins).map(|i| h[i] + if i > 0 { h[i - 1] } else { 0 } + h.get(i + 1).copied().unwrap_or(0)).collect();
    (0..bins).max_by_key(|&i| (sm[i], std::cmp::Reverse(i))).filter(|&i| sm[i] > 0).map(|b| b as f64 * frame_ms)
}

fn anchors(cands: &[Vec<f64>], peak: f64) -> Vec<i64> {
    let mut a: Vec<i64> = cands.iter().filter_map(|c| {
        c.iter().copied().filter(|l| (l - peak).abs() <= PAIR_TOL_MS)
            .min_by(|x, y| (x - peak).abs().partial_cmp(&(y - peak).abs()).unwrap_or(std::cmp::Ordering::Equal))
    }).map(|l| l.round() as i64).collect();
    a.sort();
    a
}

/// Core of Rumble Echo on precomputed energy tracks (no ffmpeg). `press_offset_ms` is the press-based
/// latency on the same clock (measured latency with stamps, fit offset_ms without), if there is one.
pub fn echo(tracks: &[Energy], clocks: &[FileClock], stamps: &[FrameStamp], haptics: &[Value], press_offset_ms: Option<i64>) -> Value {
    let on = onsets(haptics);
    let mut spike_times: Vec<f64> = vec![];
    let mut measured = false;
    for (c, tr) in clocks.iter().zip(tracks) {
        let (ft, m) = frame_times(c, stamps, tr.e.len());
        measured |= m;
        spike_times.extend(spikes(&tr.e).into_iter().filter_map(|k| ft.get(k).copied().flatten()));
    }
    spike_times.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let cands = candidates(&on, &spike_times);
    // Chance gets its own peak search, so the max-of-histogram selection bias is on both sides.
    let shifted: Vec<u64> = on.iter().map(|t| t + CHANCE_SHIFT_US).collect();
    let chance_cands = candidates(&shifted, &spike_times);
    let a = peak(&cands).map(|p| anchors(&cands, p)).unwrap_or_default();
    let chance = peak(&chance_cands).map(|p| anchors(&chance_cands, p).len()).unwrap_or(0);
    let n = a.len();
    let latency = if n == 0 { None } else { Some(a[n / 2]) };
    let iqr = if n >= 4 { Some(a[n * 3 / 4] - a[n / 4]) } else { None };
    let mut why: Vec<String> = vec![];
    if n < MIN_ANCHORS { why.push(format!("{n} paired onsets (< {MIN_ANCHORS})")); }
    match iqr { Some(q) if q > MAX_IQR_MS => why.push(format!("IQR {q} ms > {MAX_IQR_MS} ms")), None if n >= MIN_ANCHORS => why.push("too few anchors for an IQR".into()), _ => {} }
    if n > 0 && n < 2 * chance.max(1) { why.push(format!("{n} paired vs {chance} at chance (needs 2x)")); }
    if !on.is_empty() && n * 1000 < MIN_PAIRED_PERMILLE * on.len() { why.push(format!("{n} of {} onsets paired (< {MIN_PAIRED_PERMILLE} permille)", on.len())); }
    let scorable = why.is_empty();
    let disagree = match (latency, press_offset_ms) { (Some(l), Some(p)) => Some((l - p).abs()), _ => None };
    let agree = disagree.map(|d| d <= AGREE_MS);
    let status = if !scorable { "unscorable" } else if agree == Some(false) { "partial" } else { "ok" };
    if agree == Some(false) { why.push(format!("rumble latency and press offset {} ms apart (> {AGREE_MS} ms)", disagree.unwrap())); }
    json!({
        "schema": SCHEMA,
        "clock_source": if measured { "frame_clock" } else { "fit" },
        "n_onsets": on.len(), "n_spikes": spike_times.len(), "n_paired": n, "chance_paired": chance,
        "latency_ms": latency, "iqr_ms": iqr,
        "press_offset_ms": press_offset_ms, "disagreement_ms": disagree, "agree_with_press_offset": agree,
        "status": status, "reasons": why,
        "params": {"quiet_ms": QUIET_US / 1000, "max_lag_ms": MAX_LAG_MS as i64, "pair_tol_ms": PAIR_TOL_MS as i64,
                   "min_anchors": MIN_ANCHORS, "min_paired_permille": MIN_PAIRED_PERMILLE, "max_iqr_ms": MAX_IQR_MS, "agree_ms": AGREE_MS, "chance_shift_ms": CHANCE_SHIFT_US / 1000},
        "advisory": "Advisory only. Not in the receipt, the input proof, or the hash chain. Can mark the clock unscorable or partial; never raises or fails anything. The IMU detector delay makes this read slightly short; the press offset also includes the game's button response.",
    })
}

/// Haptic records for a bundle: haptics.jsonl, else the `haptic` lines of inputs.jsonl.
pub fn load_haptics(dir: &std::path::Path) -> Vec<Value> {
    let read = |name: &str| -> Vec<Value> {
        std::fs::read_to_string(dir.join(name)).map(|s| s.lines().filter_map(|l| serde_json::from_str::<Value>(l).ok())
            .filter(|v| v["type"] == "haptic").collect()).unwrap_or_default()
    };
    let h = read("haptics.jsonl");
    if h.is_empty() { read("inputs.jsonl") } else { h }
}

/// Press-based latency on the clock Rumble Echo will use, from an `align-ab` report.
pub fn press_offset(report: &Value, measured: bool) -> Option<i64> {
    if measured { report["measured_alignment"]["latency"]["latency_ms"].as_i64() } else { report["fit_alignment"]["offset_ms"].as_i64() }
}

/// Write `rumble_echo.json` beside the bundle. Only fields from [`echo`]; no device paths or names.
pub fn write(dir: &std::path::Path, v: &Value) -> std::io::Result<()> {
    std::fs::write(dir.join(FILE_NAME), serde_json::to_string_pretty(v).unwrap_or_else(|_| "{}".into()) + "\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Busy-ish synthetic session. Frame f is rendered at f * 33.3 ms + wander; it arrives `lat` ms later
    /// with +/-4 ms jitter. A rumble at render time r is felt by the IMU at r (+ `imu_ms`). The hit shows
    /// as a spike on the frame rendered at r. Unrelated spikes every ~1.7 s add noise.
    fn session(lat: f64, n_hits: usize) -> (Vec<Energy>, Vec<FileClock>, Vec<FrameStamp>, Vec<Value>) {
        let mpf = 1000.0 / FPS;
        let len = 600 + n_hits * 130;
        let g = |f: f64| f * mpf + 300.0 * (2.0 * std::f64::consts::PI * f / 7000.0).sin();
        let mut st = 0x9E37_79B9_7F4A_7C15u64;
        let mut rnd = || { st ^= st << 13; st ^= st >> 7; st ^= st << 17; st };
        let stamps: Vec<FrameStamp> = (0..len as i64).map(|f| FrameStamp {
            file: "video.mp4".into(), frame: f, t_mono_us: ((g(f as f64) + lat + (rnd() % 9) as f64 - 4.0) * 1000.0).round() as u64, pts_90k: 0,
        }).collect();
        let mut e: Vec<f32> = (0..len).map(|i| 1.0 + 0.2 * ((i * 7 % 5) as f32)).collect();
        let mut put = |k: usize| for j in 0..3 { if k + j < len { e[k + j] = 40.0; } };
        let mut hap = vec![];
        let mut f = 500usize;
        for i in 0..n_hits {
            f += 100 + (i * 53) % 61;
            put(f);
            hap.push(json!({"type": "haptic", "t_us": (g(f as f64) * 1000.0).round() as u64, "confidence": 800, "confidence_label": "high"}));
            // a low-confidence jolt right after: not an onset, and it must not count as one
            hap.push(json!({"type": "haptic", "t_us": (g(f as f64) * 1000.0).round() as u64 + 200_000, "confidence": 200, "confidence_label": "low"}));
            put(f + 50 + (i * 17) % 23); // unrelated visual spike
        }
        let clocks = vec![FileClock { file: "video.mp4".into(), start_ms: lat, ms_per_frame: mpf, end_ms: 1e9, fitted: true }];
        (vec![Energy { e }], clocks, stamps, hap)
    }

    #[test]
    fn recovers_a_520ms_latency_within_one_frame() {
        let (tr, cl, st, hap) = session(520.0, 60);
        let v = echo(&tr, &cl, &st, &hap, Some(533));
        let l = v["latency_ms"].as_i64().unwrap();
        assert!((l - 520).abs() <= 34, "{v:#}");
        assert!(v["iqr_ms"].as_i64().unwrap() <= 34, "{v:#}");
        assert_eq!(v["n_onsets"], 60);
        assert!(v["n_paired"].as_u64().unwrap() >= 55, "{v:#}");
        assert_eq!(v["clock_source"], "frame_clock");
        assert_eq!(v["agree_with_press_offset"], true);
        assert_eq!(v["status"], "ok", "{v:#}");
    }

    #[test]
    fn disagreeing_with_the_press_offset_is_partial_never_more() {
        let (tr, cl, st, hap) = session(520.0, 60);
        let v = echo(&tr, &cl, &st, &hap, Some(767));
        assert_eq!(v["agree_with_press_offset"], false);
        assert!(v["disagreement_ms"].as_i64().unwrap() > AGREE_MS);
        assert_eq!(v["status"], "partial", "{v:#}");
    }

    #[test]
    fn no_rumble_or_unrelated_rumble_is_unscorable() {
        let (tr, cl, st, _) = session(520.0, 60);
        let v = echo(&tr, &cl, &st, &[], Some(520));
        assert_eq!(v["status"], "unscorable");
        assert_eq!(v["n_onsets"], 0);
        // rumble times unrelated to the picture
        let hap: Vec<Value> = (0..40u64).map(|i| json!({"type": "haptic", "t_us": 20_000_000 + i * 2_917_000, "confidence": 900, "confidence_label": "high"})).collect();
        let w = echo(&tr, &cl, &st, &hap, Some(520));
        assert_eq!(w["status"], "unscorable", "{w:#}");
    }

    #[test]
    fn onsets_need_high_confidence_after_a_quiet_second() {
        let h = vec![
            json!({"type": "haptic", "t_us": 1_000_000, "confidence_label": "high"}),
            json!({"type": "haptic", "t_us": 1_500_000, "confidence_label": "high"}), // not quiet
            json!({"type": "haptic", "t_us": 5_000_000, "confidence_label": "low"}),
            json!({"type": "haptic", "t_us": 5_400_000, "confidence_label": "high"}), // low one 400 ms before
            json!({"type": "haptic", "t_us": 9_000_000, "confidence": 700}),
            json!({"type": "in", "t_us": 12_000_000}),
        ];
        assert_eq!(onsets(&h), vec![1_000_000, 9_000_000]);
    }

    #[test]
    fn spikes_are_rising_edges_over_the_local_median() {
        let mut e = vec![1.0f32; 60];
        for k in 20..24 { e[k] = 30.0; }
        e[40] = 3.0; // small bump: not a spike
        assert_eq!(spikes(&e), vec![20]);
    }

    #[test]
    fn align_ab_without_video_skips_and_writes_nothing() {
        let dir = std::env::temp_dir().join(format!("qorcap-re-{}-{}", std::process::id(), line!()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let r = crate::align::ab_report_with(&dir, None, 15, true);
        assert!(r["rumble_echo"].as_str().unwrap().starts_with("skipped"));
        assert!(!dir.join(FILE_NAME).exists());
        let plain = crate::align::ab_report(&dir, None, 15);
        assert!(plain.get("rumble_echo").is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn sidecar_has_no_device_identifiers() {
        let (tr, cl, st, hap) = session(520.0, 10);
        let s = echo(&tr, &cl, &st, &hap, None).to_string();
        for bad in ["HID\\\\", "VID_", "PID_", "device", "\\\\?\\"] { assert!(!s.contains(bad), "{bad}"); }
    }
}
