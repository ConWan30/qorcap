//! Sight-to-shot Stage S1: S2 -> S4 timing descriptors from USB `inputs.jsonl` rows (COUNTED-only rule;
//! COUNTED itself is disabled in this build, see below).
//!
//! Offline and read-only. It reads one bundle's `inputs.jsonl` (`header`, `in`, `hb`, `status` rows)
//! and nothing else: no video, no frame clock, no screen reader, no detector. It never writes inside
//! a bundle folder; the caller names an output path outside it ([`write_report`]).
//!
//! What it computes (PLAN-r10, sight-to-shot, sections 2.3-2.6, 13 row S1, R9-3 / R10-2):
//! * S4 anchors: R2 analog edges exactly as `trigger_edges()` in `sidecar/qorcap_cod.py` (an R2 jump of
//!   more than `edge_delta` between consecutive `in` rows). The R2 digital bit is a diagnostic only and
//!   never makes an edge. Edges with gaps <= B merge into one burst; the burst's first edge is the anchor.
//! * S2: in the S1-blind window [R2 - W, R2], onset rule (ii): the first `in` row with right-stick
//!   deflection d >= onset after a quiet span >= Q with d <= release throughout. Every qualifying onset
//!   is kept (`s2_candidates`); more than one sets `multi_onset`.
//! * One outcome per anchor: `onset`, `no_onset`, `left_censored_stick`, `onset_reused`, or
//!   `masked:<reason>` (hb coverage, epoch, transport, report-counter breaks).
//! * The headline is the S2 -> S4 distribution over every gate-passing "R2 press burst" with an onset,
//!   shown with the full outcome table. Nothing on the screen side filters it.
//!
//! COUNTED IS DISABLED IN THIS BUILD ([`COUNTED_ENABLED`] is `false`). Every output is INFO_ONLY or
//! DESCRIPTIVE_ONLY_HOLDOUT_REUSED. `--seal-status` and a config's `"status"` are recorded as caller
//! claims only. COUNTED needs a later change in which the tool itself recomputes the seal and the input
//! hash chain and checks threshold_id (which includes the freeze status) against the ledger.
//!
//! COUNTED (when a later build enables it) is a private descriptive tier: host-observed USB rows that passed preregistered gates
//! on this capture setup. It is not a rating, not a measure of the person, and it says nothing about who
//! held the controller or whether the input came from a person. The physical thumb onset is UNMEASURED.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

use crate::evidence as ev;

pub const SCHEMA: &str = "qorcap-s2s-counted/v0";
pub const HEADLINE_LABEL: &str = "R2 press bursts";
pub const LIMIT: &str = "Host-observed USB row timing on this capture setup. A private descriptive tier: not a rating, not a measure of the person, and it does not show who held the controller or that the input came from a person. The physical thumb onset is UNMEASURED.";
/// Label for (A) outputs after the held-out set was already touched (PLAN-r10 R10-3). Never COUNTED.
pub const DESCRIPTIVE_ONLY_HOLDOUT_REUSED: &str = "DESCRIPTIVE_ONLY_HOLDOUT_REUSED";
pub const TRANSPORT_USB: &str = "usb-hid";
/// Hard off (QorProof H1). No CLI flag or config value can produce COUNTED in this build: the seal
/// status and the freeze status are caller claims the tool does not check.
pub const COUNTED_ENABLED: bool = false;
pub const COUNTED_DISABLED_REASON: &str = "counted_disabled_in_this_build";
pub const COUNTED_DISABLED_NOTE: &str = "COUNTED is disabled in this build. --seal-status and the config status are caller claims and are not checked. A later change must have the tool recompute the seal and the input hash chain and check threshold_id (including freeze status) against the ledger before anything can be COUNTED.";
/// Calibration-set label (R1). Not COUNTED.
pub const CALIBRATION_SET_TIER: &str = "NOT_COUNTED_CALIBRATION_SET";
/// Files whose presence marks a match bundle folder. The report is never written into one.
pub const BUNDLE_MARKERS: &[&str] = &[
    "inputs.jsonl",
    "manifest.jsonl",
    "bundle.json",
    "receipt.json",
    "receipt.v0.json",
    "receipt.draft.json",
    "checkpoints.jsonl",
];

/// Preregistered rule values. Integers only, so the threshold_id hash is JCS-safe.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Params {
    /// Right-stick deflection d = max(|rx-128|, |ry-128|) clipped at 127 (`humanlike::defl`). SHARED (A)/(E).
    pub deflection: String,
    /// Onset when d >= this. (A): 26, design choice. (E) tunes it.
    pub onset_d: u8,
    /// Hysteresis release when d <= this; also the quiet level. (A): 13, design choice. (E) tunes it.
    pub release_d: u8,
    /// Quiet span Q (us) with d <= release_d throughout. (A): 100 ms, design choice. (E) tunes it.
    pub quiet_us: u64,
    /// S1-blind window W (us) before the burst-first R2 edge. 1.5 s, design choice. SHARED (A)/(E).
    pub window_us: u64,
    /// R2 edge: analog jump > this between consecutive `in` rows (sidecar TRIGGER_DELTA). SHARED (A)/(E).
    pub edge_delta: i32,
    /// Burst gap B (us): edges <= B apart merge. 250 ms, design choice. SHARED (A)/(E).
    pub burst_gap_us: u64,
    /// Onset rule id. "ii" = first onset after quiet >= Q inside the window. SHARED (A)/(E).
    pub onset_rule: String,
    /// hb gate: any covering hb row with max_rx_gap_us above this masks the span (PLAN 2.3).
    pub max_rx_gap_us: u64,
    /// Headline minimum: fewer onset bursts than this -> headline UNMEASURED (M4 floor, borrowed).
    pub min_bursts_for_headline: usize,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TuningSession {
    pub session_id: String,
    pub seal_status: String,
    pub inputs_sha256: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CodeRef {
    pub path: String,
    pub line: String,
    pub symbol: String,
    pub expect: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CodePins {
    pub main_sha: String,
    pub refs: Vec<CodeRef>,
    pub drift: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Excluded {
    pub session: String,
    pub reason: String,
    /// sha256 of the session's inputs.jsonl, when known. Also matched, so a renamed copy is caught.
    /// UNKNOWN (absent) for the listed sessions: this tool has not read their real files.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub inputs_sha256: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Config {
    /// "A_standby" (a-priori rule, no session data) or "E" (tuned; not built here).
    pub source: String,
    /// "standby_unfrozen" until a frozen, ledgered threshold exists. A caller claim: this build records it
    /// (and hashes it into threshold_id) but never derives COUNTED from it.
    pub status: String,
    pub params: Params,
    /// Empty for (A). Hashed into threshold_id with each seal status and inputs sha256 (R2).
    pub tuning_sessions: Vec<TuningSession>,
    /// Held-out sessions named before freeze (R8-3). Hashed into threshold_id.
    pub heldout_sessions: Vec<String>,
    pub code_pins: CodePins,
    /// Extra sessions to refuse. Add-only: the effective list is [`builtin_exclusions`] plus these
    /// ([`Config::effective_exclusions`]), so an empty or missing list removes nothing. The effective
    /// list is hashed into threshold_id.
    #[serde(default)]
    pub excluded_sessions: Vec<Excluded>,
}

/// Sessions this build always refuses to compute, whatever the config says (QorProof r2 R-M1b).
/// A config can add exclusions but never remove these. `inputs_sha256` is UNKNOWN (None) for all of
/// them: filling it needs read-only hashes of the real files, which this change does not read.
pub fn builtin_exclusions() -> Vec<Excluded> {
    vec![
            Excluded {
                session: "073204".into(),
                reason: "EXCLUDED per PLAN-r11 R11-3 (agent-relayed instruction, QorProof-unverified; excluding is the safe direction)".into(),
                inputs_sha256: None,
            },
            Excluded {
                session: "201029".into(),
                reason: "VOID".into(),
                inputs_sha256: None,
            },
            Excluded {
                session: "153536".into(),
                reason: "VOID".into(),
                inputs_sha256: None,
            },
            Excluded {
                session: "112342".into(),
                reason: "FAIL".into(),
                inputs_sha256: None,
            },
    ]
}

impl Config {
    /// PLAN-r10 R9-3/R10-2 standby (A) values. Chosen a priori from code and encoding facts, no session
    /// data. Not frozen: `status = standby_unfrozen`, so nothing it produces is COUNTED until a freeze.
    pub fn standby_a() -> Config {
        let r = |path: &str, line: &str, symbol: &str, expect: &str| CodeRef {
            path: path.into(),
            line: line.into(),
            symbol: symbol.into(),
            expect: expect.into(),
        };
        Config {
            source: "A_standby".into(),
            status: "standby_unfrozen".into(),
            params: Params {
                deflection: "max(|rx-128|,|ry-128|) clipped 127".into(),
                onset_d: 26, // design choice (~20% of 127); not a measured CoD deadzone (UNKNOWN)
                release_d: 13, // design choice (~10% of 127)
                quiet_us: 100_000, // Q = 100 ms, design choice
                window_us: 1_500_000, // W = 1.5 s, design choice (r1 example value)
                edge_delta: 60, // matches sidecar TRIGGER_DELTA and the haptics.rs trigger step
                burst_gap_us: 250_000, // B = 250 ms, design choice (not FIRE_WINDOW_US)
                onset_rule: "ii".into(),
                max_rx_gap_us: 50_000,
                min_bursts_for_headline: 30,
            },
            tuning_sessions: vec![],
            heldout_sessions: vec![],
            code_pins: CodePins {
                main_sha: "4888a6319a31885aad388c38001cb4b0adc8c4b5".into(),
                refs: vec![
                    r(
                        "src/humanlike.rs",
                        "56",
                        "defl",
                        "fn defl(x: u8, y: u8) -> u8",
                    ),
                    r(
                        "sidecar/qorcap_cod.py",
                        "62",
                        "TRIGGER_DELTA",
                        "TRIGGER_DELTA = 60",
                    ),
                    r(
                        "sidecar/qorcap_cod.py",
                        "526",
                        "trigger_edges",
                        "def trigger_edges(rows)",
                    ),
                    r(
                        "sidecar/qorcap_cod.py",
                        "537",
                        "trigger_edges step",
                        "if prev is not None and r2 - prev > TRIGGER_DELTA:",
                    ),
                    r(
                        "src/haptics.rs",
                        "149",
                        "trigger step",
                        "(r2 as i32 - p.r2 as i32).abs() > 60",
                    ),
                    r(
                        "src/inputs.rs",
                        "56",
                        "ANALOG_MIN_GAP_US",
                        "pub const ANALOG_MIN_GAP_US: u64 = 4_000;",
                    ),
                ],
                drift: "none: re-pinned at main 4888a63; same lines as the PLAN-r9 refs".into(),
            },
            excluded_sessions: builtin_exclusions(),
        }
    }

    /// sha256(JCS) of the rule, freeze status, tuning sessions (with seal status and inputs sha256),
    /// held-out names, the code pins (main SHA) and the effective exclusion list.
    pub fn threshold_id(&self) -> String {
        let v = json!({
            "schema": "qorcap-s2s-threshold/v0", "source": self.source, "status": self.status, "params": self.params,
            "tuning_sessions": self.tuning_sessions, "heldout_sessions": self.heldout_sessions, "code_pins": self.code_pins,
            "excluded_sessions": self.effective_exclusions(),
        });
        ev::try_jcs_sha(&v).expect("threshold config is integers and strings only")
    }

    pub fn provenance(&self) -> Value {
        json!({
            "source": self.source, "status_claim": self.status, "status_checked": false,
            "sessions": self.tuning_sessions.iter().map(|s| s.session_id.clone()).collect::<Vec<_>>(),
            "seal_status": self.tuning_sessions.iter().map(|s| s.seal_status.clone()).collect::<Vec<_>>(),
            "inputs_sha256": self.tuning_sessions.iter().map(|s| s.inputs_sha256.clone()).collect::<Vec<_>>(),
            "code_pins_main_sha": self.code_pins.main_sha,
        })
    }

    /// [`builtin_exclusions`] first, then config entries that are not exact duplicates of an earlier one.
    pub fn effective_exclusions(&self) -> Vec<Excluded> {
        let mut out = builtin_exclusions();
        for e in &self.excluded_sessions {
            if !out.contains(e) {
                out.push(e.clone());
            }
        }
        out
    }

    fn excluded(&self, session_id: &str, inputs_sha256: &str) -> Option<Excluded> {
        self.effective_exclusions().into_iter().find(|e| {
            session_id == e.session
                || session_id.ends_with(&format!("-{}", e.session))
                || e.inputs_sha256.as_deref() == Some(inputs_sha256)
        })
    }

    /// Reject values that make the rule meaningless or panic (QorProof L4).
    pub fn validate(&self) -> Result<(), String> {
        let p = &self.params;
        let mut bad = vec![];
        if p.onset_d > 127 || p.release_d >= p.onset_d {
            bad.push("need release_d < onset_d <= 127");
        }
        if p.quiet_us == 0 || p.window_us <= p.quiet_us {
            bad.push("need 0 < quiet_us < window_us");
        }
        if !(1..=254).contains(&p.edge_delta) {
            bad.push("need 1 <= edge_delta <= 254");
        }
        if p.burst_gap_us == 0 {
            bad.push("need burst_gap_us > 0");
        }
        if p.min_bursts_for_headline == 0 {
            bad.push("need min_bursts_for_headline >= 1");
        }
        if p.onset_rule != "ii" {
            bad.push("only onset_rule \"ii\" is implemented");
        }
        if bad.is_empty() {
            Ok(())
        } else {
            Err(format!("invalid s2s config: {}", bad.join("; ")))
        }
    }
}

/// Session facts. [`run`] replaces `session_id` with the bundle's own id (QorProof M1); the typed id
/// only has to agree with it. `seal_status` and `disposition` are caller claims: the tool does not
/// check seals, and no claim can produce COUNTED in this build.
#[derive(Debug, Clone)]
pub struct SessionMeta {
    pub session_id: String,
    /// Claim: rehashed_match | locked_not_rehashed | failed | missing
    pub seal_status: String,
    /// Claim: ok | UNKNOWN | VOID | FAIL (matched case-insensitively)
    pub disposition: String,
    pub in_calibration_set: bool,
    /// (A) triggered after held-out scoring started (R10-3): outputs are DESCRIPTIVE_ONLY_HOLDOUT_REUSED.
    pub holdout_reused: bool,
}

#[derive(Debug, Clone, Copy)]
struct InRow {
    t: u64,
    b: Option<u64>,
    rx: Option<u8>,
    ry: Option<u8>,
}

#[derive(Debug, Clone)]
struct Hb {
    t: u64,
    seq: Option<u64>,
    epoch: Option<u64>,
    bad: bool,
    max_gap: u64,
    seq_breaks: Option<u64>,
}

/// Right-stick Chebyshev deflection from centre 128, clipped at 127 (same as `humanlike::defl`).
pub fn defl(x: u8, y: u8) -> u8 {
    ((x as i16 - 128)
        .unsigned_abs()
        .max((y as i16 - 128).unsigned_abs()))
    .min(127) as u8
}

/// `trigger_edges()` from sidecar/qorcap_cod.py: rows without an integer r2 or t_us are skipped and
/// do not move `prev`.
pub fn r2_edges(rows: &[Value], delta: i32) -> Vec<u64> {
    let mut prev: Option<i64> = None;
    let mut out = vec![];
    for r in rows.iter().filter(|r| r["type"] == "in") {
        let (Some(r2), Some(t)) = (r["r2"].as_i64(), r["t_us"].as_u64()) else {
            continue;
        };
        if let Some(p) = prev {
            if r2 - p > delta as i64 {
                out.push(t);
            }
        }
        prev = Some(r2);
    }
    out
}

/// Burst-first edges: an edge starts a burst when the gap to the previous edge is > `gap_us`.
/// Returns (anchor time, edges in burst).
pub fn bursts(edges: &[u64], gap_us: u64) -> Vec<(u64, usize)> {
    let mut out: Vec<(u64, usize)> = vec![];
    let mut last: Option<u64> = None;
    for &e in edges {
        match last {
            Some(l) if e.saturating_sub(l) <= gap_us => {
                if let Some(b) = out.last_mut() {
                    b.1 += 1;
                }
            }
            _ => out.push((e, 1)),
        }
        last = Some(e);
    }
    out
}

fn sha_list(parts: &[Value]) -> String {
    ev::sha256_hex(
        serde_json::to_string(&Value::Array(parts.to_vec()))
            .unwrap()
            .as_bytes(),
    )
}

#[derive(Debug, Clone)]
struct Cand {
    t: u64,
    axis: &'static str,
    prev_t: Option<u64>,
    baseline_start: u64,
    quiet_us: u64,
    d: u8,
}

enum Stick {
    Onset(Vec<Cand>),
    NoOnset,
    LeftCensored,
    Unknown,
}

/// Onset rule (ii) over the S1-blind window [r - W, r]. Reads only `in` rows.
fn stick_window(rows: &[InRow], r: u64, p: &Params) -> Stick {
    let ws = r.saturating_sub(p.window_us);
    // last row at or before the window start gives the stick state there (rows are change-only)
    let i0 = rows.partition_point(|x| x.t <= ws);
    if i0 == 0 {
        return Stick::Unknown;
    }
    let (mut rx, mut ry) = (128u8, 128u8);
    let mut known = false;
    for x in &rows[..i0] {
        if let (Some(a), Some(b)) = (x.rx, x.ry) {
            rx = a;
            ry = b;
            known = true;
        }
    }
    if !known {
        return Stick::Unknown;
    }
    let d0 = defl(rx, ry);
    let mut active = d0 >= p.onset_d;
    let moving_at_start = d0 > p.release_d;
    let mut quiet_start: Option<u64> = if d0 <= p.release_d { Some(ws) } else { None };
    let mut last_quiet: Option<(u64, u64)> = None;
    let mut cands = vec![];
    let mut prev_t = Some(rows[i0 - 1].t);
    for x in rows[i0..].iter().take_while(|x| x.t <= r) {
        if let (Some(a), Some(b)) = (x.rx, x.ry) {
            rx = a;
            ry = b;
        }
        let d = defl(rx, ry);
        if active {
            if d <= p.release_d {
                active = false;
                quiet_start = Some(x.t);
                last_quiet = None;
            }
        } else if d >= p.onset_d {
            let q = quiet_start.map(|qs| (qs, x.t)).or(last_quiet);
            if let Some((qs, qe)) = q {
                if qe - qs >= p.quiet_us {
                    let (ax, ay) = (
                        (rx as i16 - 128).unsigned_abs().min(127) as u8 >= p.onset_d,
                        (ry as i16 - 128).unsigned_abs().min(127) as u8 >= p.onset_d,
                    );
                    let axis = match (ax, ay) {
                        (true, true) => "both",
                        (true, false) => "rx",
                        _ => "ry",
                    };
                    cands.push(Cand {
                        t: x.t,
                        axis,
                        prev_t,
                        baseline_start: qs,
                        quiet_us: qe - qs,
                        d,
                    });
                }
            }
            active = true;
            quiet_start = None;
            last_quiet = None;
        } else if d <= p.release_d {
            if quiet_start.is_none() {
                quiet_start = Some(x.t);
            }
        } else if let Some(qs) = quiet_start.take() {
            last_quiet = Some((qs, x.t));
        }
        prev_t = Some(x.t);
    }
    if !cands.is_empty() {
        Stick::Onset(cands)
    } else if moving_at_start {
        Stick::LeftCensored
    } else {
        Stick::NoOnset
    }
}

/// Gates for the span [start, end]: transport, hb coverage in one epoch with contiguous hb_seq,
/// clean hb rows, no report-counter breaks, no controller status change. Ok(hb_span) or Err(reason).
fn span_gate(
    start: u64,
    end: u64,
    hbs: &[Hb],
    header_transport: Option<&str>,
    rows: &[Value],
    max_gap: u64,
    flags: &mut Vec<String>,
) -> Result<Value, String> {
    if header_transport != Some(TRANSPORT_USB) {
        return Err(if header_transport.is_none() {
            "transport_unknown".into()
        } else {
            "transport_not_usb".into()
        });
    }
    for r in rows {
        let Some(t) = r["t_us"].as_u64() else {
            continue;
        };
        if t < start || t > end {
            continue;
        }
        if let Some(tr) = r["transport"].as_str() {
            if tr != TRANSPORT_USB {
                return Err("transport_not_usb".into());
            }
        }
        if r["type"] == "status" && r["state"] != "connected" {
            return Err("controller_status_change_in_window".into());
        }
    }
    let before = hbs.partition_point(|h| h.t <= start);
    if before == 0 {
        return Err("no_hb".into());
    }
    let last = hbs.partition_point(|h| h.t < end);
    if last >= hbs.len() {
        return Err("no_hb".into());
    }
    let set = &hbs[before - 1..=last];
    let e0 = set[0].epoch;
    if e0.is_none() || set.iter().any(|h| h.epoch != e0) {
        return Err("epoch_change_in_window".into());
    }
    if set.iter().any(|h| h.seq.is_none())
        || set.windows(2).any(|w| w[1].seq != w[0].seq.map(|s| s + 1))
    {
        return Err("hb_gap_in_window".into());
    }
    let cover = &set[1..];
    if cover.iter().any(|h| h.bad || h.max_gap > max_gap) {
        return Err("hb_gap_in_window".into());
    }
    if cover.iter().any(|h| h.seq_breaks.unwrap_or(0) > 0) {
        return Err("report_seq_break".into());
    }
    if cover.iter().any(|h| h.seq_breaks.is_none())
        && !flags.iter().any(|f| f == "seq_breaks_unrecorded")
    {
        flags.push("seq_breaks_unrecorded".into());
    }
    Ok(
        json!({"epoch": e0, "hb_seq_first": set[0].seq, "hb_seq_last": set[set.len() - 1].seq,
              "max_rx_gap_us": cover.iter().map(|h| h.max_gap).max().unwrap_or(0)}),
    )
}

/// Percentile by nearest rank on sorted values (same convention as `align::pct`).
fn pct(xs: &[i64], p: usize) -> i64 {
    if xs.is_empty() {
        return 0;
    }
    xs[(xs.len() - 1) * p / 100]
}

fn stats(vals_us: &[i64], min_n: usize) -> Value {
    let mut v = vals_us.to_vec();
    v.sort();
    if v.len() < min_n.max(1) {
        return json!({"status": "UNMEASURED", "n": v.len(), "reason": format!("{} R2 press bursts with an onset; the preregistered minimum is {min_n}", v.len())});
    }
    json!({"status": "ok", "n": v.len(), "unit": "us", "median_us": pct(&v, 50), "p25_us": pct(&v, 25), "p75_us": pct(&v, 75),
           "iqr_us": pct(&v, 75) - pct(&v, 25), "min_us": v[0], "max_us": v[v.len() - 1]})
}

/// Analyse parsed `inputs.jsonl` rows. Pure: no file access. Unknown row types and unknown fields
/// (any screen-side field) are ignored, so nothing screen-side can change the selection.
pub fn analyze(rows: &[Value], inputs_sha256: &str, meta: &SessionMeta, cfg: &Config) -> Value {
    let p = &cfg.params;
    let threshold_id = cfg.threshold_id();
    let prov = cfg.provenance();
    let mut out = json!({
        "schema": SCHEMA, "label": HEADLINE_LABEL, "limit": LIMIT,
        "session": {"session_id": meta.session_id, "inputs_file": "inputs.jsonl", "inputs_sha256": inputs_sha256,
                    "seal_status_claim": meta.seal_status, "seal_status_checked": false, "disposition_claim": meta.disposition,
                    "in_calibration_set": meta.in_calibration_set, "holdout_reused": meta.holdout_reused},
        "threshold": {"threshold_id": threshold_id, "provenance": prov, "params": p, "code_pins": cfg.code_pins},
        "gold": false, "referee_submit": false, "upload": false,
        "counted": {"enabled": COUNTED_ENABLED, "note": COUNTED_DISABLED_NOTE},
    });
    if let Some(x) = cfg.excluded(&meta.session_id, inputs_sha256) {
        out["status"] = json!("excluded");
        out["excluded_reason"] = json!(x.reason);
        return out;
    }
    if ["VOID", "FAIL"]
        .iter()
        .any(|d| meta.disposition.trim().eq_ignore_ascii_case(d))
    {
        out["status"] = json!("excluded");
        out["excluded_reason"] = json!(meta.disposition.trim().to_lowercase());
        return out;
    }
    let header_transport = rows
        .iter()
        .find(|r| r["type"] == "header")
        .and_then(|h| h["transport"].as_str());
    let mut ins: Vec<InRow> = rows
        .iter()
        .filter(|r| r["type"] == "in")
        .filter_map(|r| {
            Some(InRow {
                t: r["t_us"].as_u64()?,
                b: r["b"].as_u64(),
                rx: r["rx"].as_u64().and_then(|v| u8::try_from(v).ok()),
                ry: r["ry"].as_u64().and_then(|v| u8::try_from(v).ok()),
            })
        })
        .collect();
    ins.sort_by_key(|x| x.t);
    let mut hbs: Vec<Hb> = rows
        .iter()
        .filter(|r| r["type"] == "hb")
        .filter_map(|r| {
            let n = |k: &str| r[k].as_u64().unwrap_or(0);
            Some(Hb {
                t: r["t_us"].as_u64()?,
                seq: r["hb_seq"].as_u64(),
                epoch: r["epoch"].as_u64(),
                bad: n("faults") > 0
                    || n("short_reads") > 0
                    || n("parse_faults") > 0
                    || n("stale_over_50ms") > 0,
                max_gap: n("max_rx_gap_us"),
                seq_breaks: r["seq_breaks"].as_u64(),
            })
        })
        .collect();
    hbs.sort_by_key(|h| h.t);
    let edges = r2_edges(rows, p.edge_delta);
    let anchors = bursts(&edges, p.burst_gap_us);
    // R2 digital bit rises: diagnostic only, never an edge.
    let mut bit_rises = vec![];
    let mut prev_bit = false;
    for x in &ins {
        if let Some(b) = x.b {
            let on = b & crate::hid::R2 as u64 != 0;
            if on && !prev_bit {
                bit_rises.push(x.t);
            }
            prev_bit = on;
        }
    }
    out["counts"] = json!({"in_rows": ins.len(), "hb_rows": hbs.len(), "r2_edges": edges.len(), "r2_bursts": anchors.len(),
                           "r2_bit_rises_diagnostic": bit_rises.len()});

    // H1: COUNTED is unreachable here. The claims below only add reasons; none of them can lift a tier.
    let tier = if meta.holdout_reused {
        DESCRIPTIVE_ONLY_HOLDOUT_REUSED.to_string()
    } else {
        "INFO_ONLY".to_string()
    };
    let mut info_only: Vec<String> = vec![];
    if !COUNTED_ENABLED {
        info_only.push(COUNTED_DISABLED_REASON.into());
    }
    if meta.seal_status != "rehashed_match" {
        info_only.push(
            match meta.seal_status.as_str() {
                "failed" => "seal_failed",
                "missing" => "seal_missing",
                _ => "seal_not_rehashed",
            }
            .into(),
        );
    }
    if cfg.status != "frozen" {
        info_only.push("stick_onset_threshold_provisional".into());
    }

    let mut rows_out: Vec<Value> = vec![];
    let mut used: std::collections::BTreeSet<u64> = Default::default();
    let mut table: std::collections::BTreeMap<String, usize> = Default::default();
    for k in ["onset", "no_onset", "left_censored_stick", "onset_reused"] {
        table.insert(k.into(), 0);
    }
    let mut multi = 0usize;
    let mut gate_passing = 0usize;
    let mut vals: Vec<i64> = vec![];
    for &(r, n_edges) in &anchors {
        let mut flags: Vec<String> = vec![];
        let hb_at = hbs.iter().find(|h| h.t >= r).and_then(|h| h.epoch);
        let anchor_id = sha_list(&[
            json!(meta.session_id),
            json!(hb_at),
            json!(r),
            json!(format!("r2_analog_jump_gt_{}", p.edge_delta)),
        ]);
        let stick = stick_window(&ins, r, p);
        let ws = r.saturating_sub(p.window_us);
        let (span_start, cands) = match &stick {
            Stick::Onset(c) => (c[0].baseline_start, c.clone()),
            _ => (ws, vec![]),
        };
        if cands.len() > 1 {
            flags.push("multi_onset".into());
            multi += 1;
        }
        let cand_json: Vec<Value> = cands.iter().map(|c| json!({
            "onset_id": sha_list(&[json!(meta.session_id), json!(hb_at), json!(c.t), json!(threshold_id)]),
            "t_us": c.t, "axis": c.axis, "d": c.d, "prev_row_gap_us": c.prev_t.map(|pt| c.t - pt),
            "t_us_bracket": [c.prev_t, c.t], "baseline_start_us": c.baseline_start, "quiet_us": c.quiet_us,
        })).collect();
        let gate = if matches!(stick, Stick::Unknown) {
            Err("window_before_first_row".to_string())
        } else {
            span_gate(
                span_start,
                r,
                &hbs,
                header_transport,
                rows,
                p.max_rx_gap_us,
                &mut flags,
            )
        };
        let (outcome, hb_span) = match (&gate, &stick) {
            (Err(reason), _) => (format!("masked:{reason}"), Value::Null),
            (Ok(h), Stick::Onset(c)) => (
                if used.contains(&c[0].t) {
                    "onset_reused".to_string()
                } else {
                    used.insert(c[0].t);
                    "onset".to_string()
                },
                h.clone(),
            ),
            (Ok(h), Stick::NoOnset) => ("no_onset".into(), h.clone()),
            (Ok(h), Stick::LeftCensored) => ("left_censored_stick".into(), h.clone()),
            (Ok(_), Stick::Unknown) => unreachable!(),
        };
        if gate.is_ok() {
            gate_passing += 1;
        }
        *table.entry(outcome.clone()).or_default() += 1;
        let s2s4 = if outcome == "onset" {
            Some(r as i64 - cands[0].t as i64)
        } else {
            None
        };
        if let Some(v) = s2s4 {
            vals.push(v);
        }
        let bit = bit_rises
            .iter()
            .copied()
            .filter(|&b| b + p.burst_gap_us >= r && b <= r + p.burst_gap_us)
            .min_by_key(|&b| b.abs_diff(r));
        rows_out.push(json!({
            "anchor_id": anchor_id, "session_id": meta.session_id, "epoch": hb_at, "tier": tier, "outcome": outcome, "flags": flags,
            "s4_r2": {"t_us_analog": r, "t_us_bit_diagnostic": bit, "bit_minus_analog_us": bit.map(|b| b as i64 - r as i64),
                      "edge_rule_id": format!("r2_analog_jump_gt_{}", p.edge_delta)},
            "burst": {"burst_gap_us": p.burst_gap_us, "edges_in_burst": n_edges},
            "window_us": [ws, r], "s2_candidates": cand_json,
            "s2_selected": if matches!(stick, Stick::Onset(_)) { json!({"onset_id": cand_json[0]["onset_id"], "rule_id": format!("onset_rule_{}", p.onset_rule), "s2_selection": "s1_blind"}) } else { Value::Null },
            "s2_s4_us": s2s4, "hb_span": hb_span, "threshold_id": threshold_id, "threshold_provenance": prov,
        }));
    }
    let table_json = json!({"outcomes": table, "multi_onset": multi, "gate_passing_r2_bursts": gate_passing, "r2_bursts": anchors.len()});
    out["tier"] = json!(tier);
    out["info_only_reasons"] = json!(info_only);
    if meta.in_calibration_set {
        // R1: no value from a calibration session is ever reported. Only the threshold-free M4 count.
        out["tier"] = json!(CALIBRATION_SET_TIER);
        out["status"] = json!("in_calibration_set");
        out["m4_gate_passing_r2_bursts"] = json!(gate_passing);
        out["note"] = json!("in_calibration_set: no per-burst rows, no timing values (R1). Only the threshold-free burst count is shown.");
        return out;
    }
    out["status"] = json!("ok");
    out["outcome_table"] = table_json;
    let st = stats(&vals, p.min_bursts_for_headline);
    // Never a COUNTED headline in this build: the values sit under `descriptive` with not_counted true.
    out["headline"] = json!({"label": HEADLINE_LABEL, "status": "NOT_COUNTED", "tier": tier, "reasons": info_only});
    out["descriptive"] = json!({"tier": tier, "not_counted": true, "s2_s4": st, "outcome_table": out["outcome_table"].clone()});
    out["anchors"] = json!(rows_out);
    out
}

/// True when `out` would land inside `bundle` (after resolving the existing parts of both paths).
/// A bare relative `inputs.jsonl` has an empty bundle path, which contains every path: every write
/// is then refused (fail-safe; pass the bundle folder or a path with a folder instead).
pub fn inside(bundle: &Path, out: &Path) -> bool {
    let b = std::fs::canonicalize(bundle).unwrap_or_else(|_| bundle.to_path_buf());
    let o = resolved_out(out);
    o.starts_with(&b)
}

fn resolved_out(out: &Path) -> PathBuf {
    let parent = out
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let pc = std::fs::canonicalize(parent).unwrap_or_else(|_| parent.to_path_buf());
    pc.join(out.file_name().unwrap_or_default())
}

/// The nearest folder at or above `out`'s folder that holds a bundle marker file, if any (QorProof M2b).
/// Any match bundle is protected, not only the input's.
pub fn bundle_folder_above(out: &Path) -> Option<PathBuf> {
    let o = resolved_out(out);
    let mut d = o.parent();
    while let Some(dir) = d {
        if BUNDLE_MARKERS.iter().any(|m| dir.join(m).exists())
            || dir
                .file_name()
                .and_then(|n| n.to_str())
                .is_some_and(|n| n.starts_with("match-") && dir.join("inputs.jsonl").exists())
        {
            return Some(dir.to_path_buf());
        }
        d = dir.parent();
    }
    None
}

/// The bundle's own identity (QorProof M1): its folder name `match-<stamp>` and `bundle.json`
/// `match_id` `local-<stamp>`. Both must be present and name the same stamp; otherwise Err (fail closed:
/// exclusions are bound to the bundle, not to a typed id).
pub fn bundle_identity(bundle: &Path) -> Result<String, String> {
    let canon = std::fs::canonicalize(bundle)
        .map_err(|e| format!("bundle id UNKNOWN: {}: {e}", bundle.display()))?;
    let folder = canon
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("")
        .to_string();
    let Some(stamp_folder) = folder.strip_prefix("match-") else {
        return Err(format!("bundle id UNKNOWN: folder {folder:?} is not named match-<stamp>; refusing (exclusions are bound to the bundle's own id)"));
    };
    let text = std::fs::read_to_string(canon.join("bundle.json"))
        .map_err(|e| format!("bundle id UNKNOWN: bundle.json not readable ({e}); refusing (exclusions are bound to the bundle's own id)"))?;
    let v: Value =
        serde_json::from_str(&text).map_err(|e| format!("bundle id UNKNOWN: bundle.json: {e}"))?;
    let mid = v["match_id"]
        .as_str()
        .ok_or("bundle id UNKNOWN: bundle.json has no match_id")?;
    let stamp_mid = mid.strip_prefix("local-").unwrap_or(mid);
    if stamp_mid != stamp_folder {
        return Err(format!("bundle id mismatch: folder {folder:?} but bundle.json match_id {mid:?} (renamed or copied folder); refusing"));
    }
    Ok(folder)
}

/// A typed `--session-id` must name the bundle's own id: the folder name, `local-<stamp>`, `<stamp>`
/// or its time part. Empty means not typed.
fn typed_id_agrees(typed: &str, bundle_id: &str) -> bool {
    let t = typed.trim();
    let stamp = bundle_id.strip_prefix("match-").unwrap_or(bundle_id);
    t.is_empty()
        || t == bundle_id
        || t == stamp
        || t == format!("local-{stamp}")
        || stamp.ends_with(&format!("-{t}"))
}

/// `input` is a bundle folder or its inputs.jsonl. Reads it, analyses, writes `out` (never inside any
/// bundle folder). `meta.session_id` is the typed id (may be empty); the report uses the bundle's own id.
pub fn run(input: &Path, out: &Path, meta: &SessionMeta, cfg: &Config) -> Result<Value, String> {
    cfg.validate()?;
    let (bundle, inputs) = if input.is_dir() {
        (input.to_path_buf(), input.join("inputs.jsonl"))
    } else {
        (
            input
                .parent()
                .map(Path::to_path_buf)
                .unwrap_or_else(|| PathBuf::from(".")),
            input.to_path_buf(),
        )
    };
    check_out(&bundle, out)?;
    let bundle_id = bundle_identity(&bundle)?;
    if !typed_id_agrees(&meta.session_id, &bundle_id) {
        return Err(format!(
            "typed --session-id {:?} does not match the bundle's own id {bundle_id:?}; refusing",
            meta.session_id
        ));
    }
    let bytes = std::fs::read(&inputs).map_err(|e| format!("read {}: {e}", inputs.display()))?;
    let sha = ev::sha256_hex(&bytes);
    let text = String::from_utf8_lossy(&bytes);
    let mut bad = 0usize;
    let rows: Vec<Value> = text
        .lines()
        .filter(|l| !l.trim().is_empty())
        .filter_map(|l| match serde_json::from_str(l) {
            Ok(v) => Some(v),
            Err(_) => {
                bad += 1;
                None
            }
        })
        .collect();
    let mut m = meta.clone();
    m.session_id = bundle_id.clone();
    let mut rep = analyze(&rows, &sha, &m, cfg);
    rep["session"]["session_id_source"] = json!("bundle folder name + bundle.json match_id");
    rep["session"]["session_id_typed"] = if meta.session_id.trim().is_empty() {
        Value::Null
    } else {
        json!(meta.session_id)
    };
    rep["unparsed_lines"] = json!(bad);
    write_report(&bundle, out, &rep)?;
    Ok(rep)
}

/// Every refusal for an output path (QorProof M2): inside the input's bundle, inside any folder with
/// bundle markers, or an existing symlink / hard-linked file at `out`.
fn check_out(bundle: &Path, out: &Path) -> Result<(), String> {
    if inside(bundle, out) {
        return Err(format!("refusing to write {} inside the bundle folder {} (seal integrity); pick a path outside it", out.display(), bundle.display()));
    }
    if let Some(b) = bundle_folder_above(out) {
        return Err(format!("refusing to write {} inside the bundle folder {} (bundle marker files found; seal integrity)", out.display(), b.display()));
    }
    if let Ok(md) = std::fs::symlink_metadata(out) {
        if md.file_type().is_symlink() {
            return Err(format!(
                "refusing to write {}: it is a symlink",
                out.display()
            ));
        }
        if !md.is_file() {
            return Err(format!(
                "refusing to write {}: it exists and is not a regular file",
                out.display()
            ));
        }
        let links = link_count(out, &md)?;
        if links > 1 {
            return Err(format!(
                "refusing to write {}: it is a hard link ({links} links)",
                out.display()
            ));
        }
    }
    Ok(())
}

#[cfg(unix)]
fn link_count(_out: &Path, md: &std::fs::Metadata) -> Result<u64, String> {
    use std::os::unix::fs::MetadataExt;
    Ok(md.nlink())
}

/// `nNumberOfLinks` from GetFileInformationByHandle (std's `number_of_links` is unstable). Fails
/// closed: a file whose link count cannot be read is refused.
#[cfg(windows)]
fn link_count(out: &Path, _md: &std::fs::Metadata) -> Result<u64, String> {
    use std::os::windows::io::AsRawHandle;
    #[link(name = "kernel32")]
    extern "system" {
        // BY_HANDLE_FILE_INFORMATION is 13 DWORDs; nNumberOfLinks is the 11th.
        fn GetFileInformationByHandle(h: *mut core::ffi::c_void, info: *mut [u32; 13]) -> i32;
    }
    let refuse = |why: String| format!("refusing to write {}: cannot read its link count ({why})", out.display());
    let f = std::fs::File::open(out).map_err(|e| refuse(e.to_string()))?;
    let mut info = [0u32; 13];
    if unsafe { GetFileInformationByHandle(f.as_raw_handle() as _, &mut info) } == 0 {
        return Err(refuse(std::io::Error::last_os_error().to_string()));
    }
    Ok(info[10] as u64)
}

#[cfg(not(any(unix, windows)))]
fn link_count(out: &Path, _md: &std::fs::Metadata) -> Result<u64, String> {
    Err(format!("refusing to write {}: link count UNKNOWN on this platform", out.display()))
}

/// Checks [`check_out`], writes a temp file beside `out`, then renames it over `out`. The rename
/// replaces the directory entry, so a link at `out` is never written through.
pub fn write_report(bundle: &Path, out: &Path, rep: &Value) -> Result<(), String> {
    check_out(bundle, out)?;
    let dir = out
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let name = out
        .file_name()
        .ok_or("refusing an output path with no file name")?
        .to_string_lossy()
        .into_owned();
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let tmp = dir.join(format!(".{name}.tmp-{}-{nanos}", std::process::id()));
    let body = serde_json::to_string_pretty(rep).unwrap() + "\n";
    let res = (|| -> std::io::Result<()> {
        use std::io::Write;
        let mut f = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&tmp)?;
        f.write_all(body.as_bytes())?;
        f.sync_all()?;
        drop(f);
        std::fs::rename(&tmp, out)
    })();
    if let Err(e) = res {
        let _ = std::fs::remove_file(&tmp);
        return Err(format!("write {}: {e}", out.display()));
    }
    Ok(())
}

#[cfg(test)]
mod tests;
