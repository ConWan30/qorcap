//! Human-likeness score for a controller input log. Honest and probabilistic.
//!
//! Signals (each optional; a signal only counts when there is enough data for it):
//! * **press timing jitter**: human presses of one button are never metronomic; turbo/macros are
//!   (windows of 6 consecutive intervals with coefficient of variation < 3.5% are "periodic").
//! * **hold-duration jitter**: same test on how long each press is held.
//! * **stick ramps**: a thumb flick passes through intermediate deflections; injected input often jumps
//!   centre -> edge between two reports.
//! * **stick micro-drift**: a held stick wobbles by a few counts; injected holds sit on one exact value.
//! * **trigger pressure ramps**: real L2/R2 pulls pass through intermediate pressure over several ms.
//!
//! Output is a 0..1000 score, a label, a confidence that is never "high", and plain reasons. It can be fooled
//! (a good bot adds noise; a DualSense Edge with trigger stops or a short-throw stick shortens ramps) and it
//! never establishes who held the controller.
use crate::hid;
use serde_json::{json, Value};

pub const DISCLAIMER: &str = "Probabilistic heuristic, not proof. It flags metronomic or injected-looking input; a careful bot can add noise and unusual hardware settings can look robotic. It says nothing about who held the controller.";

const PERIODIC_CV: f64 = 0.035;
const WIN: usize = 6;

#[derive(Clone, Copy)]
struct S { t: u64, b: u32, lx: u8, ly: u8, rx: u8, ry: u8, l2: u8, r2: u8 }

fn samples(recs: &[Value]) -> Vec<S> {
    let mut v: Vec<S> = recs.iter().filter(|r| r["type"] == "in").map(|r| {
        let g = |k: &str| r[k].as_u64().unwrap_or(0) as u8;
        S { t: r["t_us"].as_u64().unwrap_or(0), b: r["b"].as_u64().unwrap_or(0) as u32, lx: g("lx"), ly: g("ly"), rx: g("rx"), ry: g("ry"), l2: g("l2"), r2: g("r2") }
    }).collect();
    v.sort_by_key(|s| s.t);
    v
}

fn cv(x: &[f64]) -> f64 {
    let n = x.len() as f64;
    let m = x.iter().sum::<f64>() / n;
    if m <= 0.0 { return 0.0; }
    (x.iter().map(|v| (v - m).powi(2)).sum::<f64>() / n).sqrt() / m
}

/// Fraction of sliding windows (WIN values, step 2) whose CV is below PERIODIC_CV, plus window count and median CV.
fn periodicity(series: &[Vec<f64>]) -> Option<(f64, usize, f64)> {
    let mut cvs = vec![];
    for s in series {
        if s.len() < WIN { continue; }
        let mut i = 0;
        while i + WIN <= s.len() { cvs.push(cv(&s[i..i + WIN])); i += 2; }
    }
    if cvs.is_empty() { return None; }
    let per = cvs.iter().filter(|c| **c < PERIODIC_CV).count() as f64 / cvs.len() as f64;
    cvs.sort_by(|a, b| a.partial_cmp(b).unwrap());
    Some((per, cvs.len(), cvs[cvs.len() / 2]))
}

fn defl(x: u8, y: u8) -> u8 { ((x as i16 - 128).unsigned_abs().max((y as i16 - 128).unsigned_abs())).min(127) as u8 }

struct Feature { name: &'static str, score: f64, n: usize, weight: f64, reason: String }

pub fn score(recs: &[Value]) -> Value {
    let s = samples(recs);
    let mut feats: Vec<Feature> = vec![];
    // ---- presses / holds per digital button (L2/R2 digital bits come from the analog trigger; skip them)
    let digital = hid::BUTTON_NAMES.iter().filter(|(b, _)| *b != hid::L2 && *b != hid::R2);
    let (mut intervals, mut holds, mut total_presses, mut worst): (Vec<Vec<f64>>, Vec<Vec<f64>>, usize, Option<(f64, &str, f64)>) = (vec![], vec![], 0, None);
    for (bit, name) in digital {
        let (mut downs, mut hs, mut prev, mut down_at) = (vec![], vec![], false, 0u64);
        for x in &s {
            let on = x.b & bit != 0;
            if on && !prev { downs.push(x.t); down_at = x.t; }
            if !on && prev { let d = (x.t - down_at) as f64 / 1000.0; if d < 1000.0 { hs.push(d); } }
            prev = on;
        }
        total_presses += downs.len();
        // split into bursts (gaps > 2 s break rhythm)
        let mut cur: Vec<f64> = vec![];
        for w in downs.windows(2) {
            let d = (w[1] - w[0]) as f64 / 1000.0;
            if d > 2000.0 { if cur.len() >= WIN { intervals.push(std::mem::take(&mut cur)); } else { cur.clear(); } } else { cur.push(d); }
        }
        if cur.len() >= WIN {
            if let Some((p, _, _)) = periodicity(std::slice::from_ref(&cur)) {
                if p > 0.0 && worst.map(|w| p > w.0).unwrap_or(true) { worst = Some((p, name, cur.iter().sum::<f64>() / cur.len() as f64)); }
            }
            intervals.push(cur);
        }
        if hs.len() >= WIN { holds.push(hs); }
    }
    if let Some((per, n, med)) = periodicity(&intervals) {
        let mut reason = format!("press timing: {:.0}% of {n} press-rhythm windows are metronomic (median interval variation {:.1}%)", per * 100.0, med * 100.0);
        if let Some((_, name, mean)) = worst.filter(|w| w.0 >= 0.5) { reason.push_str(&format!("; {name} repeats every ~{mean:.0} ms almost exactly, which looks like turbo/macro")); }
        feats.push(Feature { name: "press_timing_jitter", score: (1.0 - per * 1.6).clamp(0.0, 1.0), n, weight: 1.5, reason });
    }
    if let Some((per, n, med)) = periodicity(&holds) {
        feats.push(Feature { name: "hold_duration_jitter", score: (1.0 - per * 1.6).clamp(0.0, 1.0), n, weight: 1.0,
            reason: format!("hold durations: {:.0}% of {n} windows are identical-length holds (median variation {:.1}%)", per * 100.0, med * 100.0) });
    }
    // ---- sticks: flick ramps and held micro-drift
    let (mut ramps, mut jumps, mut held, mut wobbly) = (0usize, 0usize, 0usize, 0usize);
    for stick in 0..2 {
        let d = |x: &S| if stick == 0 { defl(x.lx, x.ly) } else { defl(x.rx, x.ry) };
        let xy = |x: &S| if stick == 0 { (x.lx, x.ly) } else { (x.rx, x.ry) };
        let mut i = 1;
        while i < s.len() {
            if d(&s[i - 1]) < 20 && d(&s[i]) >= 20 {
                // walk to >= 100 or back below 20
                let (mut j, mut mid) = (i, 0usize);
                while j < s.len() && d(&s[j]) >= 20 && d(&s[j]) < 100 { mid += 1; j += 1; }
                if j < s.len() && d(&s[j]) >= 100 {
                    if mid > 0 { ramps += 1; } else { jumps += 1; }
                    // held deflection: collect values while >= 100
                    let (t0, mut k, mut vals) = (s[j].t, j, std::collections::HashSet::new());
                    while k < s.len() && d(&s[k]) >= 100 { vals.insert(xy(&s[k])); k += 1; }
                    let dur = s[k.min(s.len() - 1)].t.saturating_sub(t0);
                    if dur >= 150_000 && k - j >= 3 { held += 1; if vals.len() >= 2 { wobbly += 1; } }
                    i = k.max(i + 1); continue;
                }
                i = j.max(i + 1); continue;
            }
            i += 1;
        }
    }
    if ramps + jumps >= 4 {
        let f = ramps as f64 / (ramps + jumps) as f64;
        feats.push(Feature { name: "stick_ramps", score: ((f - 0.2) / 0.5).clamp(0.0, 1.0), n: ramps + jumps, weight: 1.0,
            reason: format!("stick flicks: {ramps} of {} pass through intermediate positions{}", ramps + jumps, if f < 0.3 { " (most jump straight to the edge, unlike a thumb)" } else { "" }) });
    }
    if held >= 3 {
        let f = wobbly as f64 / held as f64;
        feats.push(Feature { name: "stick_micro_drift", score: ((f - 0.2) / 0.5).clamp(0.0, 1.0), n: held, weight: 1.0,
            reason: format!("stick holds: {wobbly} of {held} show thumb micro-drift{}", if f < 0.3 { " (held on one exact value)" } else { "" }) });
    }
    // ---- trigger pressure ramps
    let (mut tr, mut tj) = (0usize, 0usize);
    for trig in 0..2 {
        let v = |x: &S| if trig == 0 { x.l2 } else { x.r2 };
        let mut i = 1;
        while i < s.len() {
            if v(&s[i - 1]) < 10 && v(&s[i]) >= 10 {
                let t0 = s[i - 1].t;
                let (mut j, mut mid) = (i, 0usize);
                while j < s.len() && v(&s[j]) >= 10 && v(&s[j]) < 200 { mid += 1; j += 1; }
                if j < s.len() && v(&s[j]) >= 200 {
                    if mid > 0 && s[j].t.saturating_sub(t0) >= 8_000 { tr += 1; } else { tj += 1; }
                }
                i = j.max(i + 1); continue;
            }
            i += 1;
        }
    }
    if tr + tj >= 3 {
        let f = tr as f64 / (tr + tj) as f64;
        feats.push(Feature { name: "trigger_pressure_ramps", score: ((f - 0.2) / 0.5).clamp(0.0, 1.0), n: tr + tj, weight: 1.0,
            reason: format!("triggers: {tr} of {} full pulls ramp through partial pressure{}", tr + tj, if f < 0.3 { " (instant 0 -> full, like a digital injector; Edge trigger stops can also do this)" } else { "" }) });
    }
    // ---- combine
    let mut wsum = 0.0; let mut acc = 0.0;
    let mut out_feats = serde_json::Map::new();
    let mut reasons = vec![];
    for f in &feats {
        let w = f.weight * (f.n.min(20) as f64 / 20.0).max(0.15);
        wsum += w; acc += w * f.score;
        out_feats.insert(f.name.into(), json!({"score_permille": (f.score * 1000.0).round() as i64, "samples": f.n}));
        reasons.push(f.reason.clone());
    }
    let (score, label, confidence) = if feats.is_empty() || wsum < 0.3 {
        reasons.push(format!("not enough input to judge ({total_presses} presses, {} reports logged)", s.len()));
        (Value::Null, "insufficient input", "none")
    } else {
        // Never report certainty either way: clamp to 50..950.
        let sc = (acc / wsum).clamp(0.05, 0.95);
        let label = if sc >= 0.7 { "consistent with human play" } else if sc >= 0.4 { "inconclusive" } else { "bot-like patterns (macro/turbo/injection suspected)" };
        let conf = if wsum >= 2.5 && total_presses >= 40 { "medium" } else { "low" };
        (json!((sc * 1000.0).round() as i64), label, conf)
    };
    json!({"score_permille": score, "label": label, "confidence": confidence, "reasons": reasons, "features": out_feats,
           "presses": total_presses, "samples": s.len(), "disclaimer": DISCLAIMER})
}

pub mod synth {
    //! Deterministic synthetic sessions used by tests and `qorcap redteam`.
    use crate::hid::{self, Pad};
    use crate::inputs::record;
    use serde_json::Value;
    pub struct Lcg(pub u64);
    impl Lcg { pub fn f(&mut self) -> f64 { self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407); ((self.0 >> 11) as f64) / ((1u64 << 53) as f64) }
               pub fn n(&mut self) -> f64 { (0..6).map(|_| self.f()).sum::<f64>() - 3.0 } }
    fn push(v: &mut Vec<Value>, t: u64, p: &Pad) { v.push(record(t, p)); }
    /// Human-ish: jittered taps, held sticks with drift, ramped triggers, ramped flicks. Sampled on a 4 ms USB grid.
    pub fn human(seed: u64) -> (Vec<Value>, Vec<u64>) {
        let mut r = Lcg(seed); let mut v = vec![]; let mut presses = vec![];
        let mut t: u64 = 1_000_000;
        let grid = |t: u64| t / 4000 * 4000;
        let mut p = Pad::neutral();
        for i in 0..120 {
            let btn = [hid::CROSS, hid::SQUARE, hid::CIRCLE, hid::R1, hid::UP][i % 5];
            t += (180_000.0 + 60_000.0 * r.n().abs() + 150_000.0 * r.f()) as u64;
            p.buttons |= btn; push(&mut v, grid(t), &p); presses.push(grid(t));
            let hold = (70_000.0 + 25_000.0 * r.n().abs() + 40_000.0 * r.f()) as u64;
            p.buttons &= !btn; push(&mut v, grid(t + hold), &p);
            t += hold;
            if i % 6 == 0 { // trigger pull with ramp
                for k in 1..=6 { p.r2 = (k * 42).min(255) as u8; t += 4000 + (r.f() * 4000.0) as u64; push(&mut v, grid(t), &p); }
                t += 200_000; for k in (0..6).rev() { p.r2 = (k * 42) as u8; t += 4000; push(&mut v, grid(t), &p); }
            }
            if i % 30 == 29 { // mash burst: CROSS tapped fast, human rhythm (~120 ms +- jitter)
                for _ in 0..12 {
                    t += (95_000.0 + 25_000.0 * r.n().abs() + 30_000.0 * r.f()) as u64;
                    p.buttons |= hid::CROSS; push(&mut v, grid(t), &p); presses.push(grid(t));
                    let hd = (35_000.0 + 15_000.0 * r.f()) as u64; p.buttons &= !hid::CROSS; push(&mut v, grid(t + hd), &p); t += hd;
                }
            }
            if i % 4 == 0 { // stick flick + held with drift
                for k in 1..=5 { p.lx = (128 + k * 25).min(255) as u8; t += 4000 + (r.f() * 3000.0) as u64; push(&mut v, grid(t), &p); }
                for _ in 0..60 { p.lx = (250.0 + r.n() * 2.0).clamp(229.0, 255.0) as u8; p.ly = (128.0 + r.n() * 3.0) as u8; t += 8000; push(&mut v, grid(t), &p); }
                for k in (0..5).rev() { p.lx = (128 + k * 25) as u8; p.ly = 128; t += 4000; push(&mut v, grid(t), &p); }
            }
        }
        (v, presses)
    }
    /// Bot/macro: CROSS turbo every exactly 100 ms held 40 ms, triggers 0 -> 255 instantly, sticks jump and hold one value.
    pub fn macro_bot() -> (Vec<Value>, Vec<u64>) {
        let mut v = vec![]; let mut presses = vec![]; let mut p = Pad::neutral();
        let mut t: u64 = 1_000_000;
        for i in 0..150 {
            p.buttons = hid::CROSS; push(&mut v, t, &p); presses.push(t);
            p.buttons = 0; push(&mut v, t + 40_000, &p);
            if i % 10 == 0 { p.r2 = 255; push(&mut v, t + 48_000, &p); p.r2 = 0; push(&mut v, t + 80_000, &p); }
            if i % 8 == 0 { p.lx = 255; push(&mut v, t + 52_000, &p); for k in 1..5 { push(&mut v, t + 52_000 + k * 8000, &p.clone()); } }
            if i % 8 == 1 { p.lx = 128; push(&mut v, t + 52_000, &p); }
            t += 100_000;
        }
        (v, presses)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn human_session_scores_high() {
        for seed in [1, 7, 42] {
            let (recs, _) = synth::human(seed);
            let r = score(&recs);
            let sc = r["score_permille"].as_i64().unwrap();
            assert!(sc >= 700, "seed {seed}: {r:#}");
            assert_ne!(r["confidence"], "high");
        }
    }
    #[test]
    fn macro_bot_scores_low_with_reasons() {
        let (recs, _) = synth::macro_bot();
        let r = score(&recs);
        let sc = r["score_permille"].as_i64().unwrap();
        assert!(sc < 400, "{r:#}");
        assert!(r["label"].as_str().unwrap().starts_with("bot-like"));
        let reasons = r["reasons"].to_string();
        assert!(reasons.contains("CROSS repeats every ~100 ms"), "{reasons}");
        assert!(reasons.contains("instant 0 -> full"), "{reasons}");
    }
    #[test]
    fn macro_with_usb_polling_jitter_still_flagged() {
        let (mut recs, _) = synth::macro_bot();
        let mut r = synth::Lcg(9);
        for x in recs.iter_mut() { let t = x["t_us"].as_u64().unwrap(); x["t_us"] = serde_json::json!(t + (r.f() * 3999.0) as u64); }
        let out = score(&recs);
        assert!(out["features"]["press_timing_jitter"]["score_permille"].as_i64().unwrap() <= 300, "{out:#}");
        assert!(out["score_permille"].as_i64().unwrap() < 400, "{out:#}");
    }
    #[test]
    fn empty_log_is_insufficient_not_a_verdict() {
        let r = score(&[]);
        assert!(r["score_permille"].is_null());
        assert_eq!(r["label"], "insufficient input");
    }
}
