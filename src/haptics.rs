//! Indirect haptic / rumble detection from the controller's own IMU.
//!
//! The PS5 sends rumble and haptic commands to the pad over Bluetooth, which the laptop cannot see. But the pad
//! is also USB-plugged into the laptop and every USB input report (0x01) carries its gyro + accelerometer, and a
//! running actuator shakes the pad. So QorCap looks for short bursts of vibration-band energy in the IMU:
//!
//! 1. Per axis, a 4th-order Butterworth high-pass at 20 Hz (two biquads, redesigned for the *measured* report
//!    rate) removes gravity and hand motion (< ~5 Hz). Above `0.45 * rate` the band is limited by Nyquist: at the
//!    usual ~250 Hz USB rate QorCap effectively sees 20..~125 Hz, which still covers DualSense rumble/haptics.
//! 2. Energy = mean of squared high-passed samples over a 32 ms window, separately for accel and gyro.
//! 3. Each is divided by its rolling baseline (EMA, ~3 s, frozen during events; seeded by a 3 s "hold still"
//!    calibration). score = max(accel ratio, gyro ratio). An event starts when score >= 8 and ends when it stays
//!    below 3 for 24 ms. Events shorter than 20 ms are dropped.
//! 4. Two kinds of non-rumble jolt are marked low-confidence (confidence <= 250), not dropped:
//!    a button/trigger change within 30 ms (pressing a button jolts the pad), and hand activity that
//!    does not look like an actuator. Grip shifts show up on the accelerometer while the gyro stays
//!    near its still-hand floor; swipes and flicks swing the whole pad and the accel RMS jumps past
//!    fire-haptic levels. Touch-contact bits are not part of this check: the DualSense Edge sessions
//!    this was tuned on keep `touch_active` false the entire time, including a touchpad-swipe recording.
//!
//! This is advisory corroborating evidence, never proof (see docs/HAPTICS.md). All output numbers are integers so
//! records stay inside QorTime's JCS no-float profile.
use crate::hid::Pad;
use serde_json::{json, Value};
use std::collections::VecDeque;

pub const DETECTOR: &str = "qorcap-imu-haptic/v0";
pub const HP_CUTOFF_HZ: f64 = 20.0;
pub const BAND_TOP_HZ: f64 = 200.0;
pub const WINDOW_US: u64 = 32_000;
pub const CALIB_US: u64 = 3_000_000;
pub const SETTLE_US: u64 = 300_000;
pub const K_ON: f64 = 8.0;
pub const K_OFF: f64 = 3.0;
pub const END_HOLD_US: u64 = 24_000;
pub const MIN_DUR_US: u64 = 20_000;
pub const COINCIDE_US: u64 = 30_000;
pub const BASELINE_TAU_US: f64 = 3_000_000.0;
pub const LOW_CONF_CAP: u32 = 250;
/// Peak gyro RMS (raw counts, square-root of windowed energy) below this is an accel jolt, not actuator
/// vibration. Idle-grip recordings on a DualSense Edge stayed at or under 18; fire haptics sit higher.
pub const GRIP_GYRO_RMS_MIN: u64 = 22;
/// Peak accel RMS above this is the shell being moved, not the voice-coil level seen in fire haptics.
pub const HAND_ACCEL_RMS_MAX: u64 = 450;
const FLOOR: f64 = 1.0;

#[derive(Clone, Copy, Default)]
struct Biquad { b0: f64, b1: f64, b2: f64, a1: f64, a2: f64, z1: f64, z2: f64 }
impl Biquad {
    fn highpass(fs: f64, fc: f64, q: f64) -> Self {
        let w = 2.0 * std::f64::consts::PI * fc / fs; let (c, s) = (w.cos(), w.sin()); let a = s / (2.0 * q);
        let a0 = 1.0 + a;
        Biquad { b0: (1.0 + c) / 2.0 / a0, b1: -(1.0 + c) / a0, b2: (1.0 + c) / 2.0 / a0, a1: -2.0 * c / a0, a2: (1.0 - a) / a0, z1: 0.0, z2: 0.0 }
    }
    fn lowpass(fs: f64, fc: f64, q: f64) -> Self {
        let w = 2.0 * std::f64::consts::PI * fc / fs; let (c, s) = (w.cos(), w.sin()); let a = s / (2.0 * q);
        let a0 = 1.0 + a;
        Biquad { b0: (1.0 - c) / 2.0 / a0, b1: (1.0 - c) / a0, b2: (1.0 - c) / 2.0 / a0, a1: -2.0 * c / a0, a2: (1.0 - a) / a0, z1: 0.0, z2: 0.0 }
    }
    fn run(&mut self, x: f64) -> f64 {
        let y = self.b0 * x + self.z1;
        self.z1 = self.b1 * x - self.a1 * y + self.z2;
        self.z2 = self.b2 * x - self.a2 * y;
        y
    }
}

/// Band filter for one axis: 4th-order Butterworth HP (+ 2nd-order LP at 200 Hz when the rate allows it).
#[derive(Clone, Default)]
struct Band { st: Vec<Biquad>, primed: bool }
impl Band {
    fn new(fs: f64) -> Self {
        let mut st = vec![Biquad::highpass(fs, HP_CUTOFF_HZ, 0.5412), Biquad::highpass(fs, HP_CUTOFF_HZ, 1.3066)];
        if BAND_TOP_HZ < 0.45 * fs { st.push(Biquad::lowpass(fs, BAND_TOP_HZ, std::f64::consts::FRAC_1_SQRT_2)); }
        Band { st, primed: false }
    }
    fn run(&mut self, x: f64) -> f64 {
        if !self.primed {
            // Start the high-pass at steady state for a DC input x (no start-up transient from gravity).
            self.primed = true;
            for _ in 0..400 { let mut v = x; for b in self.st.iter_mut() { v = b.run(v); } }
        }
        let mut v = x; for b in self.st.iter_mut() { v = b.run(v); } v
    }
}

#[derive(Clone)]
struct Open { t0: u64, last_above: u64, peak: f64, peak_a: f64, peak_g: f64, energy: f64 }

/// Live numbers for `input-test` and /status.
#[derive(Debug, Clone, Default, serde::Serialize)]
pub struct HapticLive {
    pub imu_present: bool,
    pub imu_reports: u64,
    /// Measured IMU report rate (reports/s, integer) over the whole run.
    pub imu_rate_hz: u32,
    pub calibrating: bool,
    pub events: u64,
    pub low_confidence: u64,
    /// (peak x baseline, duration ms) of the most recent event.
    pub last: Option<(u32, u32)>,
}

pub struct Detector {
    fs: f64,
    axes: Vec<Band>,
    win: VecDeque<(u64, f64, f64)>,
    sum: (f64, f64),
    base: Option<(f64, f64)>,
    calib: (f64, f64, u64),
    first_t: Option<u64>,
    last_t: Option<u64>,
    rate_block: (u64, u64),
    open: Option<Open>,
    pending: VecDeque<(Open, u64)>,
    changes: VecDeque<u64>,
    last_pad: Option<(u32, u8, u8)>,
    quiet_until: u64,
    wall_anchor_ms: u64,
    pub live: HapticLive,
}

impl Detector {
    pub fn new(wall_anchor_ms: u64) -> Self {
        Detector { fs: 250.0, axes: (0..6).map(|_| Band::new(250.0)).collect(), win: VecDeque::new(), sum: (0.0, 0.0), base: None,
            calib: (0.0, 0.0, 0), first_t: None, last_t: None, rate_block: (0, 0), open: None, pending: VecDeque::new(),
            changes: VecDeque::new(), last_pad: None, quiet_until: 0, wall_anchor_ms,
            live: HapticLive { calibrating: true, ..Default::default() } }
    }

    pub fn params() -> Value {
        json!({"detector": DETECTOR, "hp_cutoff_hz": HP_CUTOFF_HZ as u64, "band_top_hz": BAND_TOP_HZ as u64, "window_ms": WINDOW_US / 1000,
            "calibration_ms": CALIB_US / 1000, "on_x_baseline": K_ON as u64, "off_x_baseline": K_OFF as u64,
            "min_duration_ms": MIN_DUR_US / 1000, "button_coincidence_ms": COINCIDE_US / 1000,
            "grip_gyro_rms_min": GRIP_GYRO_RMS_MIN, "hand_accel_rms_max": HAND_ACCEL_RMS_MAX})
    }

    fn redesign(&mut self, fs: f64) {
        self.fs = fs;
        self.axes = (0..6).map(|_| Band::new(fs)).collect();
        self.win.clear(); self.sum = (0.0, 0.0);
    }

    /// Feed one parsed report stamped `t_us` on the QorCap clock. Returns finished haptic event records.
    pub fn feed(&mut self, t_us: u64, p: &Pad) -> Vec<Value> {
        // Button / trigger changes (jolts) for the coincidence check.
        let key = (p.buttons, p.l2, p.r2);
        if let Some((b, l2, r2)) = self.last_pad {
            if b != p.buttons || (l2 as i32 - p.l2 as i32).abs() > 60 || (r2 as i32 - p.r2 as i32).abs() > 60 { self.changes.push_back(t_us); }
        }
        self.last_pad = Some(key);
        while self.changes.front().map(|&c| c + 2_000_000 < t_us).unwrap_or(false) { self.changes.pop_front(); }
        if !p.has_imu() { return self.drain(t_us); }

        self.live.imu_present = true; self.live.imu_reports += 1;
        let first = *self.first_t.get_or_insert(t_us);
        if t_us > first { self.live.imu_rate_hz = (((self.live.imu_reports - 1) as f64) * 1e6 / (t_us - first) as f64).round() as u32; }
        if let Some(lt) = self.last_t { if t_us + 1_000_000 < lt || t_us > lt + 500_000 { self.win.clear(); self.sum = (0.0, 0.0); } }
        self.last_t = Some(t_us);
        // Re-measure the rate every 2 s; redesign the filters when it moved by > 15 %.
        if self.rate_block.1 == 0 { self.rate_block = (t_us, 1); } else { self.rate_block.1 += 1; }
        if t_us >= self.rate_block.0 + 2_000_000 {
            let fs = (self.rate_block.1 - 1) as f64 * 1e6 / (t_us - self.rate_block.0) as f64;
            if fs > 40.0 && ((fs - self.fs) / self.fs).abs() > 0.15 { self.redesign(fs); }
            self.rate_block = (t_us, 1);
        }

        let mut ea = 0.0; let mut eg = 0.0;
        for k in 0..3 {
            let a = self.axes[k].run(p.accel[k] as f64); ea += a * a;
            let g = self.axes[3 + k].run(p.gyro[k] as f64); eg += g * g;
        }
        self.win.push_back((t_us, ea, eg)); self.sum.0 += ea; self.sum.1 += eg;
        while self.win.front().map(|w| w.0 + WINDOW_US <= t_us).unwrap_or(false) {
            let (_, a, g) = self.win.pop_front().unwrap(); self.sum.0 -= a; self.sum.1 -= g;
        }
        let n = self.win.len().max(1) as f64;
        let (wa, wg) = ((self.sum.0 / n).max(0.0), (self.sum.1 / n).max(0.0));

        // Calibration: hold still for the first 3 s (after a 0.3 s settle).
        let since = t_us - first;
        if self.base.is_none() {
            if since >= SETTLE_US { self.calib.0 += wa; self.calib.1 += wg; self.calib.2 += 1; }
            if since >= SETTLE_US + CALIB_US && self.calib.2 > 0 {
                let c = self.calib.2 as f64;
                self.base = Some(((self.calib.0 / c).max(FLOOR), (self.calib.1 / c).max(FLOOR)));
                self.live.calibrating = false;
            }
            return self.drain(t_us);
        }
        let (ba, bg) = self.base.unwrap();
        let (ra, rg) = (wa / ba, wg / bg);
        let score = ra.max(rg);
        match &mut self.open {
            None => {
                if score >= K_ON {
                    self.open = Some(Open { t0: t_us, last_above: t_us, peak: score, peak_a: wa, peak_g: wg, energy: 0.0 });
                } else if t_us >= self.quiet_until {
                    let a = 1.0 - (-(1e6 / self.fs) / BASELINE_TAU_US).exp();
                    self.base = Some(((ba + a * (wa - ba)).max(FLOOR), (bg + a * (wg - bg)).max(FLOOR)));
                }
            }
            Some(o) => {
                o.energy += score * (1e3 / self.fs); // x-baseline * ms
                if score >= K_OFF { o.last_above = t_us; }
                if score > o.peak { o.peak = score; }
                o.peak_a = o.peak_a.max(wa); o.peak_g = o.peak_g.max(wg);
                if t_us >= o.last_above + END_HOLD_US {
                    let o = self.open.take().unwrap();
                    self.quiet_until = t_us + 300_000;
                    if o.last_above - o.t0 >= MIN_DUR_US { self.pending.push_back((o, t_us)); }
                }
            }
        }
        self.drain(t_us)
    }

    /// Emit events whose button-coincidence window has fully passed.
    fn drain(&mut self, t_us: u64) -> Vec<Value> {
        let mut out = vec![];
        while self.pending.front().map(|(o, _)| t_us >= o.last_above + COINCIDE_US).unwrap_or(false) {
            let (o, _) = self.pending.pop_front().unwrap();
            out.push(self.event(&o));
        }
        out
    }

    /// Close any open event (end of recording / test).
    pub fn flush(&mut self, t_us: u64) -> Vec<Value> {
        if let Some(o) = self.open.take() { if o.last_above - o.t0 >= MIN_DUR_US { self.pending.push_back((o, t_us)); } }
        let mut out = vec![];
        while let Some((o, _)) = self.pending.pop_front() { out.push(self.event(&o)); }
        out
    }

    fn event(&mut self, o: &Open) -> Value {
        let dur_ms = ((o.last_above - o.t0) / 1000) as u32;
        let near = self.changes.iter().any(|&c| c + COINCIDE_US >= o.t0 && c <= o.last_above + COINCIDE_US);
        // Stronger and longer bursts are more convincing; saturates at 1000.
        let strength = (1.0 - K_ON / o.peak.max(K_ON)).clamp(0.0, 1.0);
        let length = (dur_ms as f64 / 100.0).min(1.0);
        let mut conf = (300.0 + 450.0 * strength + 250.0 * length).round().min(1000.0) as u32;
        let mut flags: Vec<&str> = vec![];
        if near { conf = conf.min(LOW_CONF_CAP); flags.push("button_coincident"); }
        // Same cap as a button jolt. The burst is still recorded; it is just not treated as convincing rumble.
        let peak_accel_rms = o.peak_a.sqrt().round().min(u64::MAX as f64) as u64;
        let peak_gyro_rms = o.peak_g.sqrt().round().min(u64::MAX as f64) as u64;
        if peak_gyro_rms < GRIP_GYRO_RMS_MIN { conf = conf.min(LOW_CONF_CAP); flags.push("grip_jolt"); }
        if peak_accel_rms > HAND_ACCEL_RMS_MAX { conf = conf.min(LOW_CONF_CAP); flags.push("hand_motion"); }
        let label = if conf >= 500 { "high" } else { "low" };
        self.live.events += 1;
        if label == "low" { self.live.low_confidence += 1; }
        let peak = o.peak.round().min(u32::MAX as f64) as u32;
        self.live.last = Some((peak, dur_ms));
        json!({"type": "haptic", "t_us": o.t0, "t_mono_ms": o.t0 / 1000, "t_wall_ms": self.wall_anchor_ms + o.t0 / 1000,
            "duration_ms": dur_ms, "peak": peak, "peak_accel_rms": peak_accel_rms, "peak_gyro_rms": peak_gyro_rms,
            "energy": o.energy.round() as u64, "confidence": conf, "confidence_label": label, "flags": flags, "imu_rate_hz": self.fs.round() as u64})
    }
}

/// Summary of haptic records (from inputs.jsonl) for `input_proof.json` `haptics`.
pub fn summarize(recs: &[Value], timeline: &[Value], wall_anchor_ms: Option<u64>, haptics_sha256: Option<String>, file_consistent: Option<bool>) -> Value {
    let ev: Vec<&Value> = recs.iter().filter(|r| r["type"] == "haptic").collect();
    let ins: Vec<u64> = recs.iter().filter(|r| r["type"] == "in").filter_map(|r| r["t_us"].as_u64()).collect();
    let span_us = match (ins.first(), ins.last()) { (Some(a), Some(b)) if b > a => b - a, _ => 0 };
    let per_min_x10 = if span_us > 0 { (ev.len() as u64 * 600_000_000) / span_us } else { 0 };
    let high = ev.iter().filter(|e| e["confidence_label"] == "high").count();
    let imu = recs.iter().rev().find(|r| r["type"] == "imu");
    // score changes from the scorebug timeline: haptic events in [-30 s, +5 s] around each change
    let (mut changes, mut with) = (0usize, 0usize);
    if let Some(anchor) = wall_anchor_ms {
        let mut last: Option<(i64, i64)> = None;
        for r in timeline.iter().filter(|r| r.get("home").is_some()) {
            let hs = (r["home"].as_i64().unwrap_or(0), r["away"].as_i64().unwrap_or(0));
            if let Some(p) = last { if p != hs {
                changes += 1;
                let t = r["t_ms"].as_u64().unwrap_or(0).saturating_sub(anchor) * 1000;
                if ev.iter().filter_map(|e| e["t_us"].as_u64()).any(|x| x + 30_000_000 >= t && x <= t + 5_000_000) { with += 1; }
            } }
            last = Some(hs);
        }
    }
    json!({
        "detector": DETECTOR, "params": Detector::params(), "events": ev.len(), "high_confidence": high, "low_confidence": ev.len() - high,
        "events_per_min_x10": per_min_x10,
        "imu_present": imu.map(|i| i["present"].clone()).unwrap_or(Value::Null),
        "imu_rate_hz": imu.map(|i| i["rate_hz"].clone()).unwrap_or(Value::Null),
        "score_changes": changes, "score_changes_with_haptic": with,
        "haptics_file": "haptics.jsonl", "haptics_sha256": haptics_sha256, "haptics_file_matches_inputs": file_consistent,
        "claims": "Advisory: vibration-like IMU bursts consistent with console rumble/haptics. Not proof of any specific game event.",
    })
}

#[cfg(test)]
pub mod synth {
    //! Synthetic IMU streams for tests.
    use crate::hid::Pad;
    pub const FS: f64 = 250.0;
    /// Deterministic pseudo-noise in [-1, 1].
    pub fn noise(i: u64, ch: u64) -> f64 {
        let mut x = i.wrapping_mul(6364136223846793005).wrapping_add(ch.wrapping_mul(1442695040888963407)) ^ 0x9E3779B97F4A7C15;
        x ^= x >> 33; x = x.wrapping_mul(0xff51afd7ed558ccd); x ^= x >> 33;
        (x % 20001) as f64 / 10000.0 - 1.0
    }
    /// `secs` of reports at 250 Hz. `f(t_s)` returns (accel add, gyro add) per axis.
    pub fn stream(secs: f64, f: impl Fn(f64) -> ([f64; 3], [f64; 3], u32)) -> Vec<(u64, Pad)> {
        let n = (secs * FS) as u64;
        (0..n).map(|i| {
            let t = i as f64 / FS;
            let (a, g, b) = f(t);
            let mut p = Pad { buttons: b, ..Pad::neutral() };
            for k in 0..3 {
                let grav = [0.0, 8192.0, 0.0][k];
                p.accel[k] = (grav + 12.0 * noise(i, k as u64) + a[k]).round() as i16;
                p.gyro[k] = (6.0 * noise(i, 3 + k as u64) + g[k]).round() as i16;
            }
            ((t * 1e6) as u64 + 1000, p)
        }).collect()
    }
    pub fn run(s: &[(u64, Pad)]) -> Vec<serde_json::Value> {
        let mut d = super::Detector::new(0);
        let mut out = vec![];
        for (t, p) in s { out.extend(d.feed(*t, p)); }
        out.extend(d.flush(s.last().map(|x| x.0).unwrap_or(0)));
        out
    }
}

#[cfg(test)]
mod tests {
    use super::synth::*;
    use std::f64::consts::PI;
    fn burst(t: f64, t0: f64, dur: f64, amp: f64, hz: f64) -> f64 { if t >= t0 && t < t0 + dur { amp * (2.0 * PI * hz * t).sin() } else { 0.0 } }

    #[test]
    fn idle_noise_gives_no_events() {
        let ev = run(&stream(20.0, |_| ([0.0; 3], [0.0; 3], 0)));
        assert!(ev.is_empty(), "{ev:?}");
    }
    #[test]
    fn sixty_hz_bursts_are_detected_with_correct_timing() {
        let starts = [5.0, 8.5, 12.0];
        let s = stream(16.0, |t| {
            let v: f64 = starts.iter().map(|&t0| burst(t, t0, 0.2, 150.0, 60.0)).sum();
            ([v, 0.5 * v, 0.0], [0.6 * v, 0.0, 0.0], 0)
        });
        let ev = run(&s);
        assert_eq!(ev.len(), 3, "{ev:#?}");
        for (e, t0) in ev.iter().zip(starts) {
            let t = e["t_mono_ms"].as_i64().unwrap();
            assert!((t - (t0 * 1000.0) as i64).abs() <= 30, "start {t} vs {t0}");
            let d = e["duration_ms"].as_i64().unwrap();
            assert!((150..=260).contains(&d), "duration {d}");
            assert_eq!(e["confidence_label"], "high", "{e}");
            assert!(e["peak"].as_u64().unwrap() >= 8 && e["energy"].as_u64().unwrap() > 0);
        }
    }
    #[test]
    fn weak_short_tick_below_threshold_is_ignored() {
        let ev = run(&stream(10.0, |t| ([burst(t, 5.0, 0.2, 15.0, 60.0), 0.0, 0.0], [0.0; 3], 0)));
        assert!(ev.is_empty(), "{ev:?}");
    }
    #[test]
    fn hand_motion_swings_give_no_events() {
        // Large slow swings / tilts (0.5..3 Hz), gyro rotation and gravity shifting between axes.
        let ev = run(&stream(25.0, |t| {
            if t < 3.5 { return ([0.0; 3], [0.0; 3], 0); }
            let env = ((t - 3.5) / 1.5).min(1.0); // a hand starts moving smoothly, not as a step
            let env = env * env * (3.0 - 2.0 * env);
            let sw = 2500.0 * (2.0 * PI * 1.3 * t).sin() + 1200.0 * (2.0 * PI * 2.7 * t).sin();
            let tilt = 3000.0 * (2.0 * PI * 0.5 * t).sin();
            ([env * sw, -env * tilt * 0.3, env * tilt], [env * 4000.0 * (2.0 * PI * 1.8 * t).cos(), env * 1500.0 * (2.0 * PI * 3.0 * t).sin(), 0.0], 0)
        }));
        assert!(ev.is_empty(), "{ev:?}");
    }
    #[test]
    fn button_coincident_jolt_is_low_confidence() {
        // A sharp 50 ms jolt right when CROSS goes down at t=6.0 s, and a real burst at 10 s with no press.
        let s = stream(13.0, |t| {
            let jolt = if (6.0..6.05).contains(&t) { 400.0 * (2.0 * PI * 45.0 * (t - 6.0)).sin() * (1.0 - (t - 6.0) / 0.05) } else { 0.0 };
            let real = burst(t, 10.0, 0.3, 150.0, 60.0);
            let b = if (6.01..6.2).contains(&t) { crate::hid::CROSS } else { 0 };
            ([jolt + real, 0.0, jolt], [real, 0.0, 0.0], b)
        });
        let ev = run(&s);
        assert_eq!(ev.len(), 2, "{ev:#?}");
        assert_eq!(ev[0]["confidence_label"], "low");
        assert!(ev[0]["confidence"].as_u64().unwrap() <= super::LOW_CONF_CAP as u64);
        assert_eq!(ev[0]["flags"][0], "button_coincident");
        assert_eq!(ev[1]["confidence_label"], "high");
        assert!(ev[1]["flags"].as_array().unwrap().is_empty());
    }
    #[test]
    fn accel_only_burst_is_a_grip_jolt() {
        // Same shape as a detected rumble burst, but the gyro is quiet: a grip shift, not an actuator.
        let ev = run(&stream(12.0, |t| {
            let v = burst(t, 6.0, 0.25, 180.0, 60.0);
            ([v, 0.4 * v, 0.0], [0.0; 3], 0)
        }));
        assert_eq!(ev.len(), 1, "{ev:#?}");
        assert_eq!(ev[0]["confidence_label"], "low");
        assert!(ev[0]["confidence"].as_u64().unwrap() <= super::LOW_CONF_CAP as u64);
        assert!(ev[0]["flags"].as_array().unwrap().iter().any(|f| f == "grip_jolt"), "{ev:#?}");
        assert!(ev[0]["peak_gyro_rms"].as_u64().unwrap() < super::GRIP_GYRO_RMS_MIN);
    }
    #[test]
    fn violent_burst_is_hand_motion() {
        // A hard swing of the whole pad: accel RMS far above fire-haptic levels, gyro moving with it.
        let ev = run(&stream(12.0, |t| {
            let v = burst(t, 6.0, 0.2, 3000.0, 35.0);
            ([v, 0.5 * v, 0.0], [0.35 * v, 0.0, 0.0], 0)
        }));
        assert_eq!(ev.len(), 1, "{ev:#?}");
        assert_eq!(ev[0]["confidence_label"], "low");
        assert!(ev[0]["flags"].as_array().unwrap().iter().any(|f| f == "hand_motion"), "{ev:#?}");
        assert!(ev[0]["peak_accel_rms"].as_u64().unwrap() > super::HAND_ACCEL_RMS_MAX);
    }
    #[test]
    fn no_imu_means_no_events_and_imu_absent() {
        let mut d = super::Detector::new(0);
        for i in 0..1000u64 { assert!(d.feed(i * 4000, &crate::hid::Pad::neutral()).is_empty()); }
        assert!(!d.live.imu_present);
    }
    #[test]
    fn measures_report_rate_and_adapts_to_1khz() {
        let mut d = super::Detector::new(0);
        let mut ev = vec![];
        for i in 0..12_000u64 {
            let t = i as f64 / 1000.0;
            let mut p = crate::hid::Pad::neutral();
            p.accel = [(12.0 * noise(i, 0) + burst(t, 8.0, 0.2, 150.0, 150.0)) as i16, 8192, 0];
            p.gyro = [(6.0 * noise(i, 3)) as i16, 1, 0];
            ev.extend(d.feed(i * 1000 + 1000, &p));
        }
        assert!((990..=1010).contains(&d.live.imu_rate_hz), "{}", d.live.imu_rate_hz);
        assert_eq!(ev.len(), 1, "{ev:?}");
        assert!((ev[0]["t_mono_ms"].as_i64().unwrap() - 8000).abs() <= 30);
    }
}
