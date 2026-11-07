//! Read-only pad-link continuity.
//!
//! Heartbeat rows already carry the interval safeguards from the reader: epoch, the largest
//! receive gap, stale gaps, faults, short reads, parse faults, and how old the newest report
//! is. Those gates stay unknown. A report count can add support only after every gate passes.
//! It cannot turn a hole, a reconnect, or a fault into idle.
//!
//! `rx_bind` is a digest of bytes this process already read. It is not an anchor.
//! Idle also needs a neutral span, including L2 and R2 at rest, and one independent
//! anchor that an authority has already validated. A free-form string is not that
//! anchor, and neither is a token that only has the `independent-v1:` shape.
//! This process does not run an authority and does not register one. Without
//! that evidence the span stays unknown. A missing row or a zero count is unknown, not a drop.
//!
//! `unknown` is not idle. Counter-step and sensor-tick rules are PROVISIONAL.
use crate::inputs::{HB_INTERVAL_US, STALE_RX_GAP_US};
use serde_json::{json, Value};

pub const SCHEMA: &str = "qorcap-pad-link/v0";
const ANCHOR_PREFIX: &str = "independent-v1:";

/// Anchors this process has accepted from an independent boundary.
/// The capture logger does not insert into this set.
#[derive(Debug, Clone, Default)]
pub struct TrustedBoundary { verified: std::collections::BTreeSet<String> }

impl TrustedBoundary {
    pub fn new() -> Self { Self { verified: std::collections::BTreeSet::new() } }

    /// Refuse every token. A matching `independent-v1:` shape is not validation evidence,
    /// and this process does not run an authority that could supply one.
    pub fn note_independent(&mut self, evidence: &str) -> bool {
        let _ = (evidence, independent_anchor(evidence));
        false
    }

    pub fn accepts(&self, evidence: &str) -> bool {
        independent_anchor(evidence) && self.verified.contains(evidence)
    }
}

fn independent_anchor(evidence: &str) -> bool {
    let Some(hex) = evidence.strip_prefix(ANCHOR_PREFIX) else { return false; };
    hex.len() == 64 && hex.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Verdict {
    pub class: &'static str,
    pub reason: &'static str,
}

impl Verdict {
    fn new(class: &'static str, reason: &'static str) -> Self { Self { class, reason } }
    pub fn is_idle(&self) -> bool { self.class == "idle_supported" }
}

/// One heartbeat's own mark. This is not a span verdict and it is not an anchor.
pub fn row_mark(row: &Value) -> &'static str {
    match row.get("rx_delta").and_then(|v| v.as_u64()) {
        None => "no_count",
        Some(0) => "zero",
        Some(n) => {
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

fn u64_field(row: &Value, key: &str) -> Option<u64> {
    row.get(key).and_then(|v| v.as_u64())
}

fn trusted_anchor(row: &Value, boundary: &TrustedBoundary) -> Option<String> {
    let anchor = row.get("trusted_anchor").and_then(|v| v.as_str()).filter(|s| !s.is_empty())?;
    let local = row.get("rx_bind").and_then(|v| v.as_str()).unwrap_or("");
    if anchor == local || !boundary.accepts(anchor) { return None; }
    Some(anchor.to_string())
}

fn counts_support(row: &Value) -> bool {
    match row_mark(row) {
        "ticking" => row.get("rx_delta").and_then(|v| v.as_u64()).unwrap_or(0) > 0,
        _ => false,
    }
}

/// Classify the open interval `(start_us, end_us]` from sealed `hb` rows.
pub fn classify_span(rows: &[Value], start_us: u64, end_us: u64, boundary: &TrustedBoundary) -> Verdict {
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
            _ => return Verdict::new("unknown", "missing_row"),
        }
        let ta = a["t_us"].as_u64().unwrap();
        let tb = b["t_us"].as_u64().unwrap();
        if tb.saturating_sub(ta) > HB_INTERVAL_US + 500_000 {
            return Verdict::new("unknown", "heartbeat_gap");
        }
        match (u64_field(a, "epoch"), u64_field(b, "epoch")) {
            (Some(ea), Some(eb)) if ea == eb => {}
            _ => return Verdict::new("unknown", "epoch_change"),
        }
    }
    let mut anchor: Option<String> = None;
    for (lo, row) in &selected {
        if interval_blocked(row, *lo) { return Verdict::new("unknown", "interval_blocked"); }
        if row.get("neutral") != Some(&json!(true)) { return Verdict::new("unknown", "not_neutral"); }
        match trusted_anchor(row, boundary) {
            None => return Verdict::new("unknown", "no_trusted_anchor"),
            Some(a) => match &anchor {
                None => anchor = Some(a),
                Some(prev) if prev == &a => {}
                Some(_) => return Verdict::new("unknown", "no_trusted_anchor"),
            },
        }
    }
    for (_, row) in &selected {
        if !counts_support(row) { return Verdict::new("unknown", "counts_not_supporting"); }
    }
    Verdict::new("idle_supported", "counts_support")
}

fn interval_blocked(row: &Value, lo: u64) -> bool {
    let t = match u64_field(row, "t_us") { Some(t) => t, None => return true };
    for key in ["faults", "short_reads", "parse_faults", "stale_over_50ms"] {
        match u64_field(row, key) {
            Some(0) => {}
            _ => return true,
        }
    }
    match u64_field(row, "max_rx_gap_us") {
        Some(gap) if gap <= STALE_RX_GAP_US => {}
        _ => return true,
    }
    match u64_field(row, "epoch") {
        Some(_) => {}
        None => return true,
    }
    let last = match u64_field(row, "last_rx_t_us") { Some(v) => v, None => return true };
    if t.saturating_sub(last) > STALE_RX_GAP_US { return true; }
    let first = match u64_field(row, "first_rx_t_us") { Some(v) => v, None => return true };
    if first < lo || first > lo.saturating_add(STALE_RX_GAP_US) { return true; }
    false
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
        "anchor": "an independent anchor is registered on a trusted boundary. A free-form string is not an anchor. rx_bind is a local digest of the input reports and does not make a span idle",
        "rule": "epoch, receive gap, stale, faults, short reads, parse faults, and receive age stay unknown when they fail. A report count can add support only after those gates, a neutral span including L2 and R2 at rest, and one independent anchor. A missing row or a zero count stays unknown",
        "rows": marks,
        "note": "This file is a reading of the sealed hb rows. It does not relabel unknown as idle. Editing an hb row breaks the input chunk hash."
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const TOKEN: &str = "independent-v1:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    fn boundary() -> TrustedBoundary {
        let mut b = TrustedBoundary::new();
        assert!(!b.note_independent(TOKEN), "a formatted token is not validation evidence");
        b
    }

    fn ticking(t: u64, seq: u64) -> Value {
        json!({
            "type": "hb", "t_us": t, "hb_seq": seq, "epoch": 1, "rx_delta": 250,
            "rx_bind": "local-digest", "seq_breaks": 0, "hw_advance": 3_000_000, "hw_stalls": 0,
            "faults": 0, "short_reads": 0, "parse_faults": 0, "stale_over_50ms": 0,
            "max_rx_gap_us": 4_000, "last_rx_t_us": t, "first_rx_t_us": t - crate::inputs::HB_INTERVAL_US + 4_000,
            "neutral": true, "trusted_anchor": TOKEN
        })
    }

    #[test]
    fn a_bare_string_is_not_a_trusted_anchor() {
        let mut open = TrustedBoundary::new();
        assert!(!open.note_independent("x"));
        assert!(!open.note_independent("external-anchor"));
        assert!(!open.note_independent("independent-v1:0123456789ABCDEF0123456789abcdef0123456789abcdef0123456789abcdef"));
        assert!(!open.accepts(TOKEN));
        let rows = vec![ticking(1_000_000, 0), ticking(2_000_000, 1), ticking(3_000_000, 2), ticking(4_000_000, 3)];
        let unregistered = classify_span(&rows, 1_100_000, 3_900_000, &open);
        assert_eq!((unregistered.class, unregistered.reason), ("unknown", "no_trusted_anchor"));
        let mut bare = rows.clone();
        for row in &mut bare { row["trusted_anchor"] = json!("x"); }
        let word = classify_span(&bare, 1_100_000, 3_900_000, &boundary());
        assert_eq!((word.class, word.reason), ("unknown", "no_trusted_anchor"));
        let mut local = rows.clone();
        for row in &mut local { row["rx_bind"] = json!(TOKEN); }
        let digest = classify_span(&local, 1_100_000, 3_900_000, &boundary());
        assert_eq!(digest.reason, "no_trusted_anchor");
        let forged = "independent-v1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        assert!(!open.note_independent(forged));
        assert!(!open.accepts(forged));
        let mut shaped = rows.clone();
        for row in &mut shaped { row["trusted_anchor"] = json!(forged); }
        let format_only = classify_span(&shaped, 1_100_000, 3_900_000, &open);
        assert_eq!((format_only.class, format_only.reason), ("unknown", "no_trusted_anchor"));
        let idle = classify_span(&rows, 1_100_000, 3_900_000, &boundary());
        assert!(!idle.is_idle(), "{idle:?}");
        assert_eq!(idle.reason, "no_trusted_anchor");
    }

    #[test]
    fn counts_do_not_override_interval_gates_and_a_local_digest_is_not_an_anchor() {
        let rows = vec![ticking(1_000_000, 0), ticking(2_000_000, 1), ticking(3_000_000, 2), ticking(4_000_000, 3)];
        let gate = boundary();
        let idle = classify_span(&rows, 1_100_000, 3_900_000, &gate);
        assert!(!idle.is_idle(), "{idle:?}");
        assert_eq!(idle.reason, "no_trusted_anchor");
        let mut local = rows.clone();
        for row in &mut local { row["trusted_anchor"] = json!("local-digest"); }
        let no_anchor = classify_span(&local, 1_100_000, 3_900_000, &gate);
        assert_eq!(no_anchor.class, "unknown");
        assert_eq!(no_anchor.reason, "no_trusted_anchor");
        assert!(!no_anchor.is_idle());
        let mut bare = rows.clone();
        for row in &mut bare { bare_fields(row); }
        assert_eq!(classify_span(&bare, 1_100_000, 3_900_000, &gate).class, "unknown");
        let mut faulted = rows.clone();
        faulted[1]["faults"] = json!(1);
        assert_eq!(classify_span(&faulted, 1_100_000, 3_900_000, &gate).reason, "interval_blocked");
        let mut stale = rows.clone();
        stale[1]["max_rx_gap_us"] = json!(300_000);
        stale[1]["stale_over_50ms"] = json!(1);
        assert_eq!(classify_span(&stale, 1_100_000, 3_900_000, &gate).reason, "interval_blocked");
        let mut reopened = rows.clone();
        reopened[2]["epoch"] = json!(2);
        assert_eq!(classify_span(&reopened, 1_100_000, 3_900_000, &gate).reason, "epoch_change");
        let mut held = rows.clone();
        held[1]["neutral"] = json!(false);
        assert_eq!(classify_span(&held, 1_100_000, 3_900_000, &gate).reason, "not_neutral");
        let mut zero = rows.clone();
        zero[2]["rx_delta"] = json!(0);
        let z = classify_span(&zero, 1_100_000, 3_900_000, &gate);
        assert_eq!(z.class, "unknown");
        assert_ne!(z.reason, "zero_count");
        assert!(!z.is_idle());
        let hole = vec![ticking(1_000_000, 0), ticking(2_000_000, 1), ticking(4_000_000, 3)];
        let missing = classify_span(&hole, 500_000, 3_500_000, &gate);
        assert_eq!((missing.class, missing.reason), ("unknown", "missing_row"));
        assert_eq!(classify_span(&[], 0, 5_000_000, &gate).reason, "no_heartbeat");
        let naked = json!({
            "type": "hb", "t_us": 1_000_000, "hb_seq": 1, "epoch": 1, "neutral": true,
            "trusted_anchor": "made-up-external", "rx_bind": "different-local",
            "rx_delta": 10, "seq_breaks": 0, "hw_advance": 3_000_000, "hw_stalls": 0,
            "faults": 0, "short_reads": 0, "parse_faults": 0, "stale_over_50ms": 0,
            "max_rx_gap_us": 4000, "first_rx_t_us": 4000, "last_rx_t_us": 1_000_000
        });
        let made_up = classify_span(&[naked], 0, 1_000_000, &TrustedBoundary::new());
        assert_eq!((made_up.class, made_up.reason), ("unknown", "no_trusted_anchor"));
        assert!(!made_up.is_idle());
    }

    fn bare_fields(row: &mut Value) {
        for key in ["trusted_anchor", "neutral", "epoch", "faults", "max_rx_gap_us", "last_rx_t_us", "first_rx_t_us"] {
            row[key] = Value::Null;
        }
    }
}
