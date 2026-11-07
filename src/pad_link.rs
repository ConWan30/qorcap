//! Read-only pad-link continuity.
//!
//! Each heartbeat already counts reports the host received. This module classifies a quiet
//! stretch from those rows plus the report-counter and sensor-timestamp fields copied off
//! the normal HID input report and sealed in the same chunk chain.
//!
//! * `idle_supported`: every covering heartbeat is present, each `rx_delta` is above zero,
//!   the report sequence did not break, and the sensor timestamp advanced when more than
//!   one report arrived.
//! * `drop`: a covering row has `rx_delta == 0`, or a `hb_seq` hole says a row is missing.
//! * `unknown`: no heartbeat covers the stretch, the anchor fields are absent, or the
//!   sequence/timestamp does not support idle.
//!
//! `unknown` is not idle. Counter-step and sensor-tick rules are PROVISIONAL.
use crate::inputs::HB_INTERVAL_US;
use serde_json::{json, Value};

pub const SCHEMA: &str = "qorcap-pad-link/v0";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Verdict {
    pub class: &'static str,
    pub reason: &'static str,
}

impl Verdict {
    fn new(class: &'static str, reason: &'static str) -> Self { Self { class, reason } }
    pub fn is_idle(&self) -> bool { self.class == "idle_supported" }
}

/// One heartbeat's own mark. This is not a span verdict.
pub fn row_mark(row: &Value) -> &'static str {
    match row.get("rx_delta").and_then(|v| v.as_u64()) {
        None => "no_anchor",
        Some(0) => "zero",
        Some(n) => {
            if row.get("rx_bind").and_then(|v| v.as_str()).map(|s| s.is_empty()).unwrap_or(true) {
                return "no_anchor";
            }
            if row.get("seq_breaks").and_then(|v| v.as_u64()).unwrap_or(1) > 0 { return "sequence_break"; }
            if n > 1 && (row.get("hw_stalls").and_then(|v| v.as_u64()).unwrap_or(1) > 0
                || row.get("hw_advance").and_then(|v| v.as_u64()).unwrap_or(0) == 0)
            {
                return "sensor_clock_stalled";
            }
            "ticking"
        }
    }
}

/// Classify the open interval `(start_us, end_us]` from sealed `hb` rows.
pub fn classify_span(rows: &[Value], start_us: u64, end_us: u64) -> Verdict {
    if end_us <= start_us { return Verdict::new("unknown", "empty_span"); }
    let mut sorted: Vec<&Value> = rows.iter().filter(|r| {
        r.get("type").and_then(|t| t.as_str()) == Some("hb") || r.get("hb_seq").is_some()
    }).collect();
    sorted.retain(|r| r.get("t_us").and_then(|v| v.as_u64()).is_some());
    sorted.sort_by_key(|r| r["t_us"].as_u64().unwrap());
    let mut prev_t: Option<u64> = None;
    let mut selected: Vec<(u64, &Value)> = vec![];
    for row in &sorted {
        let t = row["t_us"].as_u64().unwrap();
        let lo = prev_t.unwrap_or_else(|| t.saturating_sub(HB_INTERVAL_US));
        if t > start_us && lo < end_us { selected.push((lo, *row)); }
        prev_t = Some(t);
    }
    if selected.is_empty() { return Verdict::new("unknown", "no_heartbeat"); }
    let first_lo = selected[0].0;
    let last_t = selected.last().unwrap().1["t_us"].as_u64().unwrap();
    if first_lo > start_us || last_t < end_us { return Verdict::new("unknown", "no_heartbeat"); }
    for w in selected.windows(2) {
        let a = w[0].1;
        let b = w[1].1;
        match (a.get("hb_seq").and_then(|v| v.as_u64()), b.get("hb_seq").and_then(|v| v.as_u64())) {
            (Some(sa), Some(sb)) if sb == sa.saturating_add(1) => {}
            (Some(_), Some(_)) => return Verdict::new("drop", "missing_row"),
            _ => return Verdict::new("unknown", "no_anchor"),
        }
        let ta = a["t_us"].as_u64().unwrap();
        let tb = b["t_us"].as_u64().unwrap();
        if tb.saturating_sub(ta) > HB_INTERVAL_US + 500_000 {
            return Verdict::new("unknown", "heartbeat_gap");
        }
    }
    for (_, row) in &selected {
        match row.get("rx_delta").and_then(|v| v.as_u64()) {
            Some(0) => return Verdict::new("drop", "zero_count"),
            None => return Verdict::new("unknown", "no_anchor"),
            Some(_) => {}
        }
        match row_mark(row) {
            "ticking" => {}
            "zero" => return Verdict::new("drop", "zero_count"),
            "sequence_break" => return Verdict::new("unknown", "report_sequence_break"),
            "sensor_clock_stalled" => return Verdict::new("unknown", "sensor_clock_stalled"),
            _ => return Verdict::new("unknown", "no_anchor"),
        }
    }
    Verdict::new("idle_supported", "heartbeat_ticked")
}

pub fn document(rows: &[Value]) -> Value {
    let marks: Vec<Value> = rows.iter().filter(|r| r.get("type").and_then(|t| t.as_str()) == Some("hb") || r.get("hb_seq").is_some()).map(|r| json!({
        "t_us": r.get("t_us").cloned().unwrap_or(Value::Null),
        "hb_seq": r.get("hb_seq").cloned().unwrap_or(Value::Null),
        "mark": row_mark(r),
        "rx_delta": r.get("rx_delta").cloned().unwrap_or(Value::Null),
    })).collect();
    json!({
        "schema": SCHEMA,
        "unknown_is_not_idle": true,
        "label": "PROVISIONAL",
        "anchor": "hid input report counter (usb offset 7) and sensor timestamp (usb offset 28), copied on the read path and sealed in the input chunk",
        "rule": "a heartbeat that keeps ticking through a no-change stretch can support idle; a zero count or a missing hb_seq is a drop; no heartbeat, a missing anchor, or a sequence or sensor-timestamp break stays unknown",
        "rows": marks,
        "note": "This file is a reading of the sealed hb rows. It does not relabel unknown as idle. Editing an hb row breaks the input chunk hash."
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ticking(t: u64, seq: u64) -> Value {
        json!({
            "type": "hb", "t_us": t, "hb_seq": seq, "rx_delta": 250,
            "rx_bind": "abc", "seq_breaks": 0, "hw_advance": 3_000_000, "hw_stalls": 0
        })
    }

    #[test]
    fn ticking_span_is_idle_zero_is_drop_and_no_heartbeat_stays_unknown() {
        let rows = vec![ticking(1_000_000, 0), ticking(2_000_000, 1), ticking(3_000_000, 2), ticking(4_000_000, 3)];
        let idle = classify_span(&rows, 1_100_000, 3_900_000);
        assert!(idle.is_idle(), "{idle:?}");
        assert_eq!(idle.reason, "heartbeat_ticked");
        let mut dropped = rows.clone();
        dropped[2]["rx_delta"] = json!(0);
        dropped[2]["rx_bind"] = Value::Null;
        let d = classify_span(&dropped, 1_100_000, 3_900_000);
        assert_eq!(d.class, "drop");
        assert_eq!(d.reason, "zero_count");
        assert!(!d.is_idle());
        let hole = vec![ticking(1_000_000, 0), ticking(2_000_000, 1), ticking(4_000_000, 3)];
        let missing = classify_span(&hole, 500_000, 3_500_000);
        assert_eq!((missing.class, missing.reason), ("drop", "missing_row"));
        let none = classify_span(&[], 0, 5_000_000);
        assert_eq!(none.class, "unknown");
        assert_eq!(none.reason, "no_heartbeat");
        assert!(!none.is_idle());
        let old = json!({"type": "hb", "t_us": 1_000_000, "hb_seq": 0, "rx_delta": 250});
        let bare = classify_span(&[old], 100_000, 900_000);
        assert_eq!(bare.class, "unknown");
        assert!(!bare.is_idle(), "unknown is not relabeled idle");
        let mut broken = rows.clone();
        broken[1]["seq_breaks"] = json!(2);
        let seq = classify_span(&broken, 1_100_000, 3_900_000);
        assert_eq!(seq.class, "unknown");
        assert_eq!(seq.reason, "report_sequence_break");
    }
}
