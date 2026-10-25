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
/// +/-50 ms is 1.5 frames at 30 fps. A wider tolerance admits more chance pairs at the same rate as
/// real ones and cannot pass the 100 ms IQR gate anyway, so the default stays 50 (tune with --re-sweep).
pub const PAIR_TOL_MS: f64 = 50.0;
pub const MIN_ANCHORS: usize = 3;
pub const MAX_IQR_MS: i64 = 100;
pub const AGREE_MS: i64 = 100;
/// At least this share of onsets (permille) must pair at the peak.
pub const MIN_PAIRED_PERMILLE: usize = 300;
pub const CHANCE_SHIFT_US: u64 = 4_100_000;
/// Haptic events at or above this confidence (0-1000) can be onsets ("high" is >= 500).
pub const ONSET_MIN_CONF: u64 = 500;
const SPIKE_PRE: usize = 10;
pub const SPIKE_RATIO: f32 = 4.0;
const SPIKE_MIN: f32 = 6.0;
const EPS: f32 = 0.5;

/// How each onset contributes candidate lags.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    /// Every spike in 0..max_lag is a candidate; the histogram peak across onsets picks the lag.
    Hist,
    /// Only the strongest spike in 0..max_lag is a candidate for each onset.
    Strongest,
}

impl std::str::FromStr for Mode {
    type Err = String;
    fn from_str(s: &str) -> Result<Self, String> {
        match s { "hist" => Ok(Mode::Hist), "strongest" => Ok(Mode::Strongest), _ => Err(format!("mode {s}: use hist or strongest")) }
    }
}
impl Mode { pub fn name(self) -> &'static str { match self { Mode::Hist => "hist", Mode::Strongest => "strongest" } } }

/// Tunable parameters. [`Params::default`] is the v0.3.35 behaviour.
#[derive(Debug, Clone, PartialEq)]
pub struct Params {
    pub pair_tol_ms: f64,
    pub max_lag_ms: f64,
    pub quiet_ms: u64,
    /// Minimum haptic confidence (0-1000) for an onset.
    pub onset_min_conf: u64,
    /// Rising edge when 2-frame energy >= ratio x the median of the 10 frames before (and >= 6).
    pub spike_ratio: f32,
    /// Keep only spikes whose strength (peak ratio of the run) is at or above this percentile of all
    /// detected spikes (e.g. 95 keeps the top 5 %). None keeps every spike.
    pub spike_pct: Option<f64>,
    pub mode: Mode,
}

impl Default for Params {
    fn default() -> Self {
        Params { pair_tol_ms: PAIR_TOL_MS, max_lag_ms: MAX_LAG_MS, quiet_ms: QUIET_US / 1000, onset_min_conf: ONSET_MIN_CONF,
                 spike_ratio: SPIKE_RATIO, spike_pct: None, mode: Mode::Hist }
    }
}

impl Params {
    pub fn to_json(&self) -> Value {
        json!({"quiet_ms": self.quiet_ms, "max_lag_ms": self.max_lag_ms.round() as i64, "pair_tol_ms": self.pair_tol_ms.round() as i64,
               "onset_min_confidence": self.onset_min_conf, "spike_ratio_x10": (self.spike_ratio * 10.0).round() as i64,
               "spike_pct": self.spike_pct.map(|p| p.round() as i64), "mode": self.mode.name(),
               "min_anchors": MIN_ANCHORS, "min_paired_permille": MIN_PAIRED_PERMILLE, "max_iqr_ms": MAX_IQR_MS,
               "agree_ms": AGREE_MS, "chance_shift_ms": CHANCE_SHIFT_US / 1000})
    }
    /// The --re-sweep grid: pair tolerance 50/100/150 ms x quiet 500/1000 ms x spike percentile 90/95/98,
    /// each in both modes. Other fields come from `self`.
    pub fn sweep_grid(&self) -> Vec<Params> {
        let mut out = vec![];
        for mode in [Mode::Hist, Mode::Strongest] { for tol in [50.0, 100.0, 150.0] { for quiet in [500u64, 1000] { for pct in [90.0, 95.0, 98.0] {
            out.push(Params { pair_tol_ms: tol, quiet_ms: quiet, spike_pct: Some(pct), mode, ..self.clone() });
        } } } }
        out
    }
}

/// Rumble onsets (mono us) with the default parameters.
pub fn onsets(haptics: &[Value]) -> Vec<u64> { onsets_with(haptics, &Params::default()) }

/// Rumble onsets (mono us): haptic events at or above `onset_min_conf` (or labelled "high" when the
/// threshold is the default 500) with no haptic event of any confidence in the `quiet_ms` before.
/// Records come from haptics.jsonl or the `haptic` lines of inputs.jsonl.
pub fn onsets_with(haptics: &[Value], p: &Params) -> Vec<u64> {
    let mut ev: Vec<(u64, bool)> = haptics.iter().filter(|r| r["type"] == "haptic")
        .filter_map(|r| {
            let conf = r["confidence"].as_u64().unwrap_or(if r["confidence_label"] == "high" { 500 } else { 0 });
            Some((r["t_us"].as_u64()?, conf >= p.onset_min_conf))
        })
        .collect();
    ev.sort();
    let mut out = vec![];
    let mut last: Option<u64> = None;
    for (t, ok) in ev {
        if ok && last.map(|l| t >= l + p.quiet_ms * 1000).unwrap_or(true) { out.push(t); }
        last = Some(t);
    }
    out
}

/// Rising-edge visual spikes with the default ratio (frame indices).
pub fn spikes(e: &[f32]) -> Vec<usize> { spikes_scored(e, SPIKE_RATIO).into_iter().map(|(k, _)| k).collect() }

/// Rising-edge visual spikes: frame k where energy crosses `ratio` x the local 10-frame median (and >= 6).
/// Each comes with its strength: the highest energy / (median + eps) reached before the run ends.
pub fn spikes_scored(e: &[f32], ratio: f32) -> Vec<(usize, f32)> {
    let mut out: Vec<(usize, f32)> = vec![];
    let mut above = false;
    let mut base = 0.0f32;
    for k in SPIKE_PRE..e.len() {
        let mut pre: Vec<f32> = e[k - SPIKE_PRE..k].to_vec();
        pre.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let med = pre[pre.len() / 2] + EPS;
        let thr = (ratio * med).max(SPIKE_MIN);
        let hit = if above { e[k] >= (ratio * base).max(SPIKE_MIN) } else { e[k] >= thr };
        if hit && !above { base = med; out.push((k, e[k] / med)); }
        else if hit { if let Some(l) = out.last_mut() { l.1 = l.1.max(e[k] / base); } }
        above = hit;
    }
    out
}

/// Keep spikes at or above the `pct` percentile of strength.
pub fn keep_top(sp: Vec<(f64, f32)>, pct: Option<f64>) -> Vec<(f64, f32)> {
    let Some(p) = pct else { return sp };
    if sp.is_empty() { return sp; }
    let mut s: Vec<f32> = sp.iter().map(|x| x.1).collect();
    s.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let i = (((s.len() - 1) as f64) * p.clamp(0.0, 100.0) / 100.0).round() as usize;
    let thr = s[i.min(s.len() - 1)];
    sp.into_iter().filter(|x| x.1 >= thr).collect()
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

/// Candidate lags (ms) per onset from the spikes 0..=max_lag after it (all of them, or the strongest).
fn candidates(on: &[u64], spikes: &[(f64, f32)], p: &Params) -> Vec<Vec<f64>> {
    on.iter().map(|&t| {
        let t = t as f64 / 1000.0;
        let i = spikes.partition_point(|s| s.0 < t);
        let win = spikes[i..].iter().take_while(|s| s.0 - t <= p.max_lag_ms);
        match p.mode {
            Mode::Hist => win.map(|s| s.0 - t).collect(),
            Mode::Strongest => win.max_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal)).map(|s| vec![s.0 - t]).unwrap_or_default(),
        }
    }).collect()
}

fn hist(cands: &[Vec<f64>], max_lag_ms: f64) -> Vec<usize> {
    let frame_ms = 1000.0 / FPS;
    let bins = (max_lag_ms / frame_ms).ceil() as usize + 1;
    let mut h = vec![0usize; bins];
    for c in cands { for &l in c { h[((l / frame_ms).round().max(0.0) as usize).min(bins - 1)] += 1; } }
    h
}

/// Peak of a 1-frame lag histogram smoothed over 3 bins (ms), ties -> shortest lag.
fn peak(cands: &[Vec<f64>], max_lag_ms: f64) -> Option<f64> {
    let h = hist(cands, max_lag_ms);
    let bins = h.len();
    let sm: Vec<usize> = (0..bins).map(|i| h[i] + if i > 0 { h[i - 1] } else { 0 } + h.get(i + 1).copied().unwrap_or(0)).collect();
    (0..bins).max_by_key(|&i| (sm[i], std::cmp::Reverse(i))).filter(|&i| sm[i] > 0).map(|b| b as f64 * 1000.0 / FPS)
}

fn anchors(cands: &[Vec<f64>], peak: f64, tol: f64) -> Vec<i64> {
    let mut a: Vec<i64> = cands.iter().filter_map(|c| {
        c.iter().copied().filter(|l| (l - peak).abs() <= tol)
            .min_by(|x, y| (x - peak).abs().partial_cmp(&(y - peak).abs()).unwrap_or(std::cmp::Ordering::Equal))
    }).map(|l| l.round() as i64).collect();
    a.sort();
    a
}

/// Spikes (time ms, strength) on the clock in use, sorted by time; and whether that clock is measured.
pub fn spike_times(tracks: &[Energy], clocks: &[FileClock], stamps: &[FrameStamp], p: &Params) -> (Vec<(f64, f32)>, bool) {
    let mut sp: Vec<(f64, f32)> = vec![];
    let mut measured = false;
    for (c, tr) in clocks.iter().zip(tracks) {
        let (ft, m) = frame_times(c, stamps, tr.e.len());
        measured |= m;
        sp.extend(spikes_scored(&tr.e, p.spike_ratio).into_iter().filter_map(|(k, s)| ft.get(k).copied().flatten().map(|t| (t, s))));
    }
    sp.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
    (keep_top(sp, p.spike_pct), measured)
}

/// Core of Rumble Echo with the default parameters.
pub fn echo(tracks: &[Energy], clocks: &[FileClock], stamps: &[FrameStamp], haptics: &[Value], press_offset_ms: Option<i64>) -> Value {
    echo_with(tracks, clocks, stamps, haptics, press_offset_ms, &Params::default())
}

/// Core of Rumble Echo on precomputed energy tracks (no ffmpeg). `press_offset_ms` is the press-based
/// latency on the same clock (measured latency with stamps, fit offset_ms without), if there is one.
pub fn echo_with(tracks: &[Energy], clocks: &[FileClock], stamps: &[FrameStamp], haptics: &[Value], press_offset_ms: Option<i64>, p: &Params) -> Value {
    let on = onsets_with(haptics, p);
    let (sp, measured) = spike_times(tracks, clocks, stamps, p);
    let cands = candidates(&on, &sp, p);
    // Chance gets its own peak search, so the max-of-histogram selection bias is on both sides.
    let shifted: Vec<u64> = on.iter().map(|t| t + CHANCE_SHIFT_US).collect();
    let chance_cands = candidates(&shifted, &sp, p);
    let a = peak(&cands, p.max_lag_ms).map(|pk| anchors(&cands, pk, p.pair_tol_ms)).unwrap_or_default();
    let chance = peak(&chance_cands, p.max_lag_ms).map(|pk| anchors(&chance_cands, pk, p.pair_tol_ms).len()).unwrap_or(0);
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
        "n_onsets": on.len(), "n_spikes": sp.len(), "n_paired": n, "chance_paired": chance,
        "latency_ms": latency, "iqr_ms": iqr,
        "press_offset_ms": press_offset_ms, "disagreement_ms": disagree, "agree_with_press_offset": agree,
        "status": status, "reasons": why,
        "params": p.to_json(),
        "advisory": "Advisory only. Not in the receipt, the input proof, or the hash chain. Can mark the clock unscorable or partial; never raises or fails anything. The IMU detector delay makes this read slightly short; the press offset also includes the game's button response.",
    })
}

/// Lag histogram diagnostic: for every onset, every spike in 0..max_lag (whatever `p.mode` is), in
/// 33 ms bins, next to the same count for the chance-shifted onsets. Rows: [bin_ms, onsets, chance].
pub fn lag_histogram(tracks: &[Energy], clocks: &[FileClock], stamps: &[FrameStamp], haptics: &[Value], p: &Params) -> Vec<[i64; 3]> {
    let all = Params { mode: Mode::Hist, ..p.clone() };
    let on = onsets_with(haptics, &all);
    let (sp, _) = spike_times(tracks, clocks, stamps, &all);
    let shifted: Vec<u64> = on.iter().map(|t| t + CHANCE_SHIFT_US).collect();
    let (h, c) = (hist(&candidates(&on, &sp, &all), all.max_lag_ms), hist(&candidates(&shifted, &sp, &all), all.max_lag_ms));
    (0..h.len()).map(|i| [(i as f64 * 1000.0 / FPS).round() as i64, h[i] as i64, c[i] as i64]).collect()
}

/// Text table for the lag histogram (stderr in `align-ab`).
pub fn histogram_text(rows: &[[i64; 3]]) -> String {
    let mut s = String::from("rumble echo lag histogram (33 ms bins; every spike 0..max_lag after each onset)\n  lag_ms  onsets  chance\n");
    for r in rows { s.push_str(&format!("  {:>6}  {:>6}  {:>6}  {}\n", r[0], r[1], r[2], "#".repeat(r[1].clamp(0, 60) as usize))); }
    s
}

/// One compact row per sweep result.
pub fn sweep_text(rows: &[Value]) -> String {
    let mut s = String::from("rumble echo sweep (diagnostic only; rumble_echo.json uses the selected params)\n  mode       tol  quiet  pct  onsets  spikes  paired  chance  lat_ms  iqr_ms  status\n");
    let f = |v: &Value| v.as_i64().map(|x| x.to_string()).unwrap_or_else(|| "-".into());
    for r in rows {
        let p = &r["params"];
        s.push_str(&format!("  {:<9} {:>4} {:>6} {:>4} {:>7} {:>7} {:>7} {:>7} {:>7} {:>7}  {}\n",
            p["mode"].as_str().unwrap_or("?"), f(&p["pair_tol_ms"]), f(&p["quiet_ms"]), f(&p["spike_pct"]),
            f(&r["n_onsets"]), f(&r["n_spikes"]), f(&r["n_paired"]), f(&r["chance_paired"]), f(&r["latency_ms"]), f(&r["iqr_ms"]),
            r["status"].as_str().unwrap_or("?")));
    }
    s
}

/// What `align-ab --rumble-echo` runs: the selected params (written to rumble_echo.json), plus the
/// optional diagnostics. Sweep and histogram results go to the report only, never to the sidecar.
#[derive(Debug, Clone, Default)]
pub struct Opts { pub params: Params, pub sweep: bool, pub histogram: bool }

/// `align-ab --rumble-echo`: optional sweep and histogram into `out` (report only), then the selected
/// params into `out["rumble_echo"]` and the `rumble_echo.json` sidecar. Nothing else is written.
#[allow(clippy::too_many_arguments)]
pub fn run(dir: &std::path::Path, tracks: &[Energy], clocks: &[FileClock], stamps: &[FrameStamp], hap: &[Value], press: Option<i64>, o: &Opts, out: &mut Value) {
    if o.sweep {
        out["rumble_echo_sweep"] = json!(o.params.sweep_grid().iter().map(|p| echo_with(tracks, clocks, stamps, hap, press, p)).collect::<Vec<_>>());
    }
    if o.histogram {
        out["rumble_echo_histogram"] = json!(lag_histogram(tracks, clocks, stamps, hap, &o.params));
    }
    let v = echo_with(tracks, clocks, stamps, hap, press, &o.params);
    out["rumble_echo_file"] = match write(dir, &v) { Ok(()) => json!(FILE_NAME), Err(e) => json!(format!("not written: {e}")) };
    out["rumble_echo"] = v;
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

/// Write `rumble_echo.json` beside the bundle. Only fields from [`echo_with`]; no device paths or names.
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
        let r = crate::align::ab_report_with(&dir, None, 15, Some(&Opts::default()));
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

    #[test]
    fn default_params_are_the_v0335_behaviour() {
        let p = Params::default();
        assert_eq!((p.pair_tol_ms, p.max_lag_ms, p.quiet_ms, p.onset_min_conf, p.spike_ratio, p.spike_pct, p.mode), (50.0, 1500.0, 1000, 500, 4.0, None, Mode::Hist));
        let (tr, cl, st, hap) = session(520.0, 40);
        assert_eq!(echo(&tr, &cl, &st, &hap, Some(520)), echo_with(&tr, &cl, &st, &hap, Some(520), &p));
        assert_eq!("strongest".parse::<Mode>().unwrap(), Mode::Strongest);
        assert!("nearest".parse::<Mode>().is_err());
    }

    #[test]
    fn strongest_mode_and_top_percentile_recover_520ms_through_clutter() {
        // Clutter: 4 weaker unrelated spikes after every hit. Hits are stronger (energy 40 vs 15).
        let (mut tr, cl, st, hap) = session(520.0, 60);
        let len = tr[0].e.len();
        for (i, k) in (700..len - 10).step_by(37).enumerate() { if tr[0].e[k] < 5.0 && i % 2 == 0 { for j in 0..2 { tr[0].e[k + j] = 15.0; } } }
        for mode in [Mode::Hist, Mode::Strongest] {
            let p = Params { mode, spike_pct: Some(50.0), ..Params::default() };
            let v = echo_with(&tr, &cl, &st, &hap, Some(533), &p);
            let l = v["latency_ms"].as_i64().unwrap();
            assert!((l - 520).abs() <= 34, "{mode:?} {v:#}");
            assert_eq!(v["status"], "ok", "{mode:?} {v:#}");
            assert_eq!(v["params"]["mode"], mode.name());
        }
        let all = echo_with(&tr, &cl, &st, &hap, None, &Params::default());
        let top = echo_with(&tr, &cl, &st, &hap, None, &Params { spike_pct: Some(50.0), ..Params::default() });
        assert!(top["n_spikes"].as_u64().unwrap() < all["n_spikes"].as_u64().unwrap());
    }

    #[test]
    fn onset_threshold_and_quiet_time_change_the_onsets() {
        let h = vec![
            json!({"type": "haptic", "t_us": 1_000_000, "confidence": 600}),
            json!({"type": "haptic", "t_us": 1_700_000, "confidence": 900}),
            json!({"type": "haptic", "t_us": 5_000_000, "confidence": 800}),
        ];
        assert_eq!(onsets_with(&h, &Params::default()), vec![1_000_000, 5_000_000]);
        assert_eq!(onsets_with(&h, &Params { quiet_ms: 500, ..Params::default() }), vec![1_000_000, 1_700_000, 5_000_000]);
        assert_eq!(onsets_with(&h, &Params { onset_min_conf: 700, ..Params::default() }), vec![5_000_000]);
    }

    #[test]
    fn spike_strength_and_ratio() {
        let mut e = vec![1.0f32; 60];
        for k in 20..24 { e[k] = 30.0; }
        e[22] = 60.0;
        for k in 40..42 { e[k] = 7.0; }
        let s = spikes_scored(&e, 4.0);
        assert_eq!(s.iter().map(|x| x.0).collect::<Vec<_>>(), vec![20, 40]);
        assert!(s[0].1 > s[1].1 && (s[0].1 - 40.0).abs() < 0.01, "{s:?}");
        assert_eq!(spikes_scored(&e, 6.0).len(), 1);
        let kept = keep_top(vec![(1.0, 2.0), (2.0, 9.0), (3.0, 5.0), (4.0, 7.0)], Some(75.0));
        assert_eq!(kept.iter().map(|x| x.0 as i64).collect::<Vec<_>>(), vec![2, 4]);
        assert_eq!(keep_top(vec![(1.0, 2.0)], None).len(), 1);
    }

    #[test]
    fn sweep_and_histogram_are_report_only_and_the_sidecar_has_the_selected_params() {
        let dir = std::env::temp_dir().join(format!("qorcap-re-sweep-{}-{}", std::process::id(), line!()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let (tr, cl, st, hap) = session(520.0, 40);
        let sel = Params { pair_tol_ms: 100.0, ..Params::default() };
        let mut out = json!({});
        run(&dir, &tr, &cl, &st, &hap, Some(533), &Opts { params: sel.clone(), sweep: true, histogram: true }, &mut out);
        let rows = out["rumble_echo_sweep"].as_array().unwrap();
        assert_eq!(rows.len(), 36);
        let t = sweep_text(rows);
        assert_eq!(t.lines().count(), 38);
        assert!(t.contains("strongest") && t.contains("hist"));
        let h = out["rumble_echo_histogram"].as_array().unwrap();
        assert_eq!(h.len(), 46);
        // the histogram peaks at about 520 ms, well above chance
        let best = h.iter().max_by_key(|r| r[1].as_i64().unwrap()).unwrap();
        assert!((best[0].as_i64().unwrap() - 520).abs() <= 34 && best[1].as_i64().unwrap() > 3 * best[2].as_i64().unwrap().max(1), "{best}");
        assert!(histogram_text(&[[0, 1, 0], [33, 5, 1]]).contains("     33       5       1  #####"));
        let file: Value = serde_json::from_str(&std::fs::read_to_string(dir.join(FILE_NAME)).unwrap()).unwrap();
        assert_eq!(file["params"]["pair_tol_ms"], 100);
        assert_eq!(file, out["rumble_echo"]);
        assert!(file.get("rumble_echo_sweep").is_none());
        assert_eq!(std::fs::read_dir(&dir).unwrap().count(), 1);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
