//! Info-only, practice-only response bound (`hand_challenge.jsonl`).
//!
//! Reuses `evidence::pattern_for` and `evidence::match_pattern` against button rising edges from the
//! sealed `hand_timeline.v0.jsonl` windows (or sealed `inputs.jsonl` when `hand_timeline` is absent and no
//! hand head was bound), and checks the challenge-time hand-chunk head binding (`hand_chunk_seq`,
//! `hand_head`) committed in `checkpoints.jsonl`.
//!
//! Practice window is server-set and taken only from the challenge checkpoint: `issued_ms` equals
//! `received_wall_ms`, `deadline_ms` equals `issued_ms + PRACTICE_DEADLINE_MS` (90_000 ms). A row that
//! disagrees, a missing checkpoint, a stale (older-than-receipt) head, or a press before
//! `received_mono_ms` cannot produce `answered_in_bound`. Official challenge rows keep the previous
//! behaviour (window from the row; official POST body is unchanged).
//!
//! Advisory only: never alters `receipt.draft.json`, `bundle.json`, or `receipt.json`. Reports only
//! whether the sealed pad stream answered the nonce within the bound (MEASURED / UNMEASURED), never who
//! held the controller.

use super::check::check_dir_heads;
use super::timeline::{CHUNK_US, FILE_NAME as HAND_FILE, SEAL_GRACE_US};
use crate::evidence::{self as ev, GENESIS};
use crate::hid;
use crate::inputs;
use serde::Serialize;
use serde_json::Value;
use std::collections::{BTreeMap, HashMap};
use std::path::Path;

pub const FILE_NAME: &str = "hand_challenge.jsonl";
pub const SCHEMA: &str = "qorcap-hand-response-bound/v0-info";
pub const PRACTICE_CHALLENGES_FILE: &str = "practice_challenges.jsonl";
pub const CHALLENGES_FILE: &str = "challenges.jsonl";
/// Fixed practice-lane answer window, set by the server at `POST /challenge`.
/// `issued_ms` is the server's `received_wall_ms`; `deadline_ms` is `issued_ms + PRACTICE_DEADLINE_MS`.
/// Client `issued_ms` / `deadline_ms` are ignored. Official challenges are unchanged (still 90 s from the POST body).
pub const PRACTICE_DEADLINE_MS: u64 = 90_000;

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ChallengeBound {
    pub schema: &'static str,
    pub advisory_only: bool,
    pub receipt_status_changed: bool,
    pub source: String,
    pub id: String,
    pub phase: String,
    pub role: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub game: Option<String>,
    pub nonce: String,
    pub pattern: Vec<String>,
    pub issued_ms: i64,
    pub deadline_ms: i64,
    pub status: &'static str,
    pub outcome: &'static str,
    pub in_bound: Option<bool>,
    pub reason: String,
    pub stream_source: &'static str,
    pub bound_chunk_seq: Option<u64>,
    pub bound_head: Option<String>,
    pub bound_head_status: &'static str,
    pub checkpoint_seq: Option<u64>,
    pub response_start_ms: Option<i64>,
    pub response_done_ms: Option<i64>,
    pub response_span_ms: Option<i64>,
    pub late_by_ms: Option<i64>,
    pub statement: String,
}

#[derive(Debug, Default, Clone)]
pub struct Report {
    pub records: Vec<ChallengeBound>,
    pub integrity_errors: Vec<String>,
    pub warnings: Vec<String>,
    pub hand_timeline_ok: Option<bool>,
    pub inputs_ok: Option<bool>,
    pub checkpoints_ok: Option<bool>,
}

impl Report {
    pub fn has_tamper(&self) -> bool {
        !self.integrity_errors.is_empty()
            || self.records.iter().any(|r| {
                r.reason.starts_with("tamper_")
                    || matches!(
                        r.reason.as_str(),
                        "bound_chunk_missing"
                            | "missing_hand_timeline"
                            | "missing_challenge_checkpoint"
                            | "missing_checkpoint_window"
                            | "stale_bound_head"
                    )
            })
    }
}

#[derive(Debug, Clone)]
struct ChallengeEntry {
    source: String,
    val: Value,
}

#[derive(Debug, Clone)]
struct ChunkMeta {
    lo_ms: i64,
    hi_ms: i64,
    pad_connected: bool,
}

struct HandStream {
    present: bool,
    ok: bool,
    off: bool,
    wall_anchor_ms: Option<i64>,
    heads: BTreeMap<u64, String>,
    chunks: Vec<ChunkMeta>,
    presses: Vec<(i64, &'static str)>,
    errors: Vec<String>,
    warnings: Vec<String>,
}

struct InputsStream {
    present: bool,
    ok: bool,
    enabled: bool,
    wall_anchor_ms: Option<i64>,
    presses: Vec<(i64, &'static str)>,
    errors: Vec<String>,
}

struct CheckpointInfo {
    ok: bool,
    by_nonce: HashMap<String, (u64, Value)>,
    errors: Vec<String>,
}

fn read_jsonl_values(path: &Path) -> Result<Vec<Value>, String> {
    let text = std::fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let mut out = Vec::new();
    for (i, line) in text.lines().enumerate() {
        if line.trim().is_empty() {
            continue;
        }
        let v: Value = serde_json::from_str(line)
            .map_err(|e| format!("{}:{}: invalid JSON ({e})", path.display(), i + 1))?;
        out.push(v);
    }
    Ok(out)
}

fn load_challenges(dir: &Path) -> Result<Vec<ChallengeEntry>, String> {
    let mut out = Vec::new();
    for (fname, src) in [
        (PRACTICE_CHALLENGES_FILE, "practice"),
        (CHALLENGES_FILE, "challenge"),
    ] {
        let p = dir.join(fname);
        if !p.exists() {
            continue;
        }
        for v in read_jsonl_values(&p)? {
            let source = if v["practice"].as_bool() == Some(true)
                || v["phase"].as_str() == Some("practice")
            {
                "practice".to_string()
            } else {
                src.to_string()
            };
            out.push(ChallengeEntry { source, val: v });
        }
    }
    Ok(out)
}

fn load_checkpoints(dir: &Path) -> CheckpointInfo {
    let p = dir.join("checkpoints.jsonl");
    if !p.exists() {
        return CheckpointInfo {
            ok: true,
            by_nonce: HashMap::new(),
            errors: Vec::new(),
        };
    }
    let text = match std::fs::read_to_string(&p) {
        Ok(t) => t,
        Err(e) => {
            return CheckpointInfo {
                ok: false,
                by_nonce: HashMap::new(),
                errors: vec![format!("checkpoints.jsonl: {e}")],
            };
        }
    };
    let mut prev = GENESIS.to_string();
    let mut expected_seq = 0u64;
    let mut errors = Vec::new();
    let mut by_nonce = HashMap::new();
    for (i, line) in text.lines().enumerate() {
        if line.trim().is_empty() {
            continue;
        }
        let cp: Value = match serde_json::from_str(line) {
            Ok(v) => v,
            Err(e) => {
                errors.push(format!("checkpoints.jsonl:{}: invalid JSON ({e})", i + 1));
                continue;
            }
        };
        if cp["seq"].as_u64() != Some(expected_seq) {
            errors.push(format!(
                "checkpoints.jsonl:{}: expected seq {expected_seq}, got {}",
                i + 1,
                cp["seq"]
            ));
        }
        expected_seq += 1;
        match ev::check_checkpoint(&cp, &prev) {
            Ok(h) => prev = h,
            Err(e) => errors.push(format!("checkpoints.jsonl:{}: {e}", i + 1)),
        }
        let q = &cp["qorcap"];
        if q["trigger"].as_str() == Some("challenge") {
            if let (Some(seq), Some(nonce)) = (cp["seq"].as_u64(), q["nonce"].as_str()) {
                by_nonce.insert(nonce.to_string(), (seq, q.clone()));
            }
        }
    }
    CheckpointInfo {
        ok: errors.is_empty(),
        by_nonce,
        errors,
    }
}

fn rising_edges_from_rows(rows: &[(u64, u32)], wall_anchor_ms: i64) -> Vec<(i64, &'static str)> {
    // Match qor_receipt.review.bundle.BUTTON_BITS (CROSS..TOUCHPAD: first 18 entries of hid::BUTTON_NAMES).
    let std_buttons = &hid::BUTTON_NAMES[..18];
    let mut prev = 0u32;
    let mut presses = Vec::new();
    for &(t_us, buttons) in rows {
        let rose = buttons & !prev;
        let t_ms = wall_anchor_ms + (t_us / 1000) as i64;
        for &(bit, name) in std_buttons {
            if rose & bit != 0 {
                presses.push((t_ms, name));
            }
        }
        prev = buttons;
    }
    presses
}

fn load_hand_stream(dir: &Path) -> HandStream {
    let path = dir.join(HAND_FILE);
    if !path.exists() {
        return match check_dir_heads(dir) {
            Ok((rep, _)) if rep.off => HandStream {
                present: false,
                ok: rep.pass(),
                off: true,
                wall_anchor_ms: None,
                heads: BTreeMap::new(),
                chunks: Vec::new(),
                presses: Vec::new(),
                errors: rep.errors,
                warnings: rep.warnings,
            },
            _ => HandStream {
                present: false,
                ok: false,
                off: false,
                wall_anchor_ms: None,
                heads: BTreeMap::new(),
                chunks: Vec::new(),
                presses: Vec::new(),
                errors: Vec::new(),
                warnings: Vec::new(),
            },
        };
    }

    let (rep, heads) = match check_dir_heads(dir) {
        Ok(pair) => pair,
        Err(e) => {
            return HandStream {
                present: true,
                ok: false,
                off: false,
                wall_anchor_ms: None,
                heads: BTreeMap::new(),
                chunks: Vec::new(),
                presses: Vec::new(),
                errors: vec![e],
                warnings: Vec::new(),
            };
        }
    };

    let mut errors = rep.errors.clone();
    let warnings = rep.warnings.clone();
    let text = match std::fs::read_to_string(&path) {
        Ok(t) => t,
        Err(e) => {
            errors.push(format!("{HAND_FILE}: {e}"));
            return HandStream {
                present: true,
                ok: false,
                off: false,
                wall_anchor_ms: None,
                heads,
                chunks: Vec::new(),
                presses: Vec::new(),
                errors,
                warnings,
            };
        }
    };

    // Only collect records belonging to sealed chunks; unsealed trailing records are ignored.
    let mut pending: Vec<Value> = Vec::new();
    let mut sealed_records: Vec<Value> = Vec::new();
    let mut wall_anchor_ms: Option<i64> = None;
    let mut chunks = Vec::new();

    for line in text.lines() {
        if line.is_empty() {
            continue;
        }
        let Ok(v) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        if v["type"] != "chunk" {
            pending.push(v);
            continue;
        }
        let Some(seq) = v["seq"].as_u64() else {
            pending.clear();
            continue;
        };
        let recs = std::mem::take(&mut pending);
        if wall_anchor_ms.is_none() {
            wall_anchor_ms = recs.iter().find(|r| r["type"] == "header").and_then(|h| {
                h["wall_anchor_ms"].as_i64().or_else(|| {
                    h["wall_anchor_ms"]
                        .as_u64()
                        .and_then(|u| i64::try_from(u).ok())
                })
            });
        }
        let hb = recs.iter().rev().find(|r| r["type"] == "hb");
        let pad_connected = hb
            .and_then(|h| h["pad_connected"].as_bool())
            .unwrap_or(false);
        if let Some(anchor) = wall_anchor_ms {
            let chunk_ms = (CHUNK_US / 1000) as i64;
            let lo_ms = anchor + (seq as i64) * chunk_ms;
            let hi_ms = anchor + (seq as i64 + 1) * chunk_ms;
            chunks.push(ChunkMeta {
                lo_ms,
                hi_ms,
                pad_connected,
            });
        }
        sealed_records.extend(recs);
    }

    let mut in_rows: Vec<(u64, usize, u32)> = Vec::new();
    for (idx, r) in sealed_records.iter().enumerate() {
        if r["type"] == "in" {
            if let (Some(t_us), Some(b)) = (r["t_us"].as_u64(), r["buttons"].as_u64()) {
                in_rows.push((t_us, idx, b as u32));
            }
        }
    }
    in_rows.sort_by_key(|&(t_us, idx, _)| (t_us, idx));
    let rows: Vec<(u64, u32)> = in_rows.into_iter().map(|(t_us, _, b)| (t_us, b)).collect();
    let presses = match wall_anchor_ms {
        Some(anchor) => rising_edges_from_rows(&rows, anchor),
        None => Vec::new(),
    };

    HandStream {
        present: true,
        ok: errors.is_empty(),
        off: rep.off,
        wall_anchor_ms,
        heads,
        chunks,
        presses,
        errors,
        warnings,
    }
}

fn load_inputs_stream(dir: &Path) -> InputsStream {
    let path = dir.join("inputs.jsonl");
    if !path.exists() {
        return InputsStream {
            present: false,
            ok: false,
            enabled: false,
            wall_anchor_ms: None,
            presses: Vec::new(),
            errors: Vec::new(),
        };
    }
    let (recs, chunks) = match inputs::read_file(&path) {
        Ok(pair) => pair,
        Err(e) => {
            return InputsStream {
                present: true,
                ok: false,
                enabled: false,
                wall_anchor_ms: None,
                presses: Vec::new(),
                errors: vec![e],
            };
        }
    };
    if chunks.is_empty() {
        return InputsStream {
            present: true,
            ok: false,
            enabled: false,
            wall_anchor_ms: None,
            presses: Vec::new(),
            errors: vec!["inputs.jsonl has no sealed chunks".into()],
        };
    }
    let (_chunk_shas, _head, errs) = inputs::verify_chunks(&recs, &chunks);
    let ok = errs.is_empty();
    let header = recs.iter().find(|r| r["type"] == "header");
    let enabled = header.and_then(|h| h["enabled"].as_bool()).unwrap_or(true);
    let wall_anchor_ms = header.and_then(|h| {
        h["wall_anchor_ms"].as_i64().or_else(|| {
            h["wall_anchor_ms"]
                .as_u64()
                .and_then(|u| i64::try_from(u).ok())
        })
    });
    let mut in_rows: Vec<(u64, usize, u32)> = Vec::new();
    if ok {
        for (idx, r) in recs.iter().enumerate() {
            if r["type"] == "in" {
                if let (Some(t_us), Some(b)) = (r["t_us"].as_u64(), r["b"].as_u64()) {
                    in_rows.push((t_us, idx, b as u32));
                }
            }
        }
        in_rows.sort_by_key(|&(t_us, idx, _)| (t_us, idx));
    }
    let rows: Vec<(u64, u32)> = in_rows.into_iter().map(|(t_us, _, b)| (t_us, b)).collect();
    let presses = match (ok, wall_anchor_ms) {
        (true, Some(anchor)) => rising_edges_from_rows(&rows, anchor),
        _ => Vec::new(),
    };
    InputsStream {
        present: true,
        ok,
        enabled,
        wall_anchor_ms,
        presses,
        errors: errs,
    }
}

fn statement_for(outcome: &str, reason: &str) -> String {
    match outcome {
        "answered_in_bound" => "MEASURED: the sealed pad stream answered the nonce within the bound. Advisory only; never changes receipt status. Does not establish who held the controller.".into(),
        "late" => "MEASURED: the sealed pad stream answered the nonce after the bound deadline. Advisory only; never changes receipt status. Does not establish who held the controller.".into(),
        "absent" => "MEASURED: the sealed pad stream did not answer the nonce within the bound. Advisory only; never changes receipt status. Does not establish who held the controller.".into(),
        _ => format!("UNMEASURED: the sealed pad stream or bound head could not be checked ({reason}). Advisory only; never changes receipt status. Does not establish who held the controller."),
    }
}

fn opt_i64(v: &Value) -> Option<i64> {
    v.as_i64()
        .or_else(|| v.as_u64().and_then(|u| i64::try_from(u).ok()))
}

fn is_practice_entry(entry: &ChallengeEntry) -> bool {
    entry.source == "practice"
        || entry.val["practice"].as_bool() == Some(true)
        || entry.val["phase"].as_str() == Some("practice")
}

/// Latest 1 s window that must already have been sealed at `received_mono_us` (tick rule, not stop/partial).
fn latest_sealed_seq_at(received_mono_us: u64, first_seq: u64) -> Option<u64> {
    let min_t = (first_seq + 1)
        .saturating_mul(CHUNK_US)
        .saturating_add(SEAL_GRACE_US);
    if received_mono_us < min_t {
        return None;
    }
    Some((received_mono_us - SEAL_GRACE_US) / CHUNK_US - 1)
}

fn field_mismatch(row: &Value, key: &str, want: i64) -> bool {
    row.get(key).is_some() && opt_i64(&row[key]) != Some(want)
}

fn evaluate_one(
    entry: &ChallengeEntry,
    hand: &HandStream,
    inp: &InputsStream,
    cps: &CheckpointInfo,
) -> ChallengeBound {
    let v = &entry.val;
    let nonce = v["nonce"].as_str().unwrap_or("").to_string();
    let id = v["id"]
        .as_str()
        .map(String::from)
        .unwrap_or_else(|| nonce.chars().take(8).collect());
    let phase = v["phase"].as_str().unwrap_or("practice").to_string();
    let role = v["role"].as_str().unwrap_or("home").to_string();
    let game = v["game"].as_str().map(String::from);
    let practice = is_practice_entry(entry);
    let mut issued_ms = opt_i64(&v["issued_ms"]).unwrap_or(0);
    let mut deadline_ms = opt_i64(&v["deadline_ms"]).unwrap_or(issued_ms);
    let mut received_mono_ms: Option<i64> = opt_i64(&v["received_mono_ms"]);

    let derived = match ev::pattern_for(&nonce) {
        Ok(p) => p,
        Err(_) => {
            let reason = "invalid_nonce".to_string();
            return ChallengeBound {
                schema: SCHEMA,
                advisory_only: true,
                receipt_status_changed: false,
                source: entry.source.clone(),
                id,
                phase,
                role,
                game,
                nonce,
                pattern: vec![],
                issued_ms,
                deadline_ms,
                status: "UNMEASURED",
                outcome: "UNMEASURED",
                in_bound: None,
                statement: statement_for("UNMEASURED", &reason),
                reason,
                stream_source: "none",
                bound_chunk_seq: v["hand_chunk_seq"].as_u64(),
                bound_head: v["hand_head"].as_str().map(String::from),
                bound_head_status: "unbound_no_hand_timeline",
                checkpoint_seq: None,
                response_start_ms: None,
                response_done_ms: None,
                response_span_ms: None,
                late_by_ms: None,
            };
        }
    };
    let pattern: Vec<String> = derived.iter().map(|s| (*s).to_string()).collect();

    // Determine bound head & checkpoint status.
    let cp_match = cps.by_nonce.get(&nonce);
    let cp_seq = cp_match.map(|(s, _)| *s);
    let c_seq = v["hand_chunk_seq"].as_u64();
    let c_head = v["hand_head"].as_str().map(String::from);
    let (cp_hand_seq, cp_hand_head) = match cp_match {
        Some((_, q)) => (
            q["hand_chunk_seq"].as_u64(),
            q["hand_head"].as_str().map(String::from),
        ),
        None => (None, None),
    };

    // Practice: the bound head is the checkpoint's latest sealed head at receipt. Official rows may
    // still fall back to the row when no head was bound.
    let bound_chunk_seq = if practice {
        cp_hand_seq
    } else {
        c_seq.or(cp_hand_seq)
    };
    let bound_head = if practice {
        cp_hand_head.clone()
    } else {
        c_head.clone().or_else(|| cp_hand_head.clone())
    };

    let mut unmeasured_reason: Option<String> = None;
    let mut bound_head_status: &'static str = "unbound_no_hand_timeline";

    if practice && cp_match.is_none() {
        bound_head_status = "unbound_no_checkpoint";
        unmeasured_reason = Some("missing_challenge_checkpoint".into());
    } else if practice {
        let q = &cp_match.unwrap().1;
        match (
            opt_i64(&q["issued_ms"]),
            opt_i64(&q["deadline_ms"]),
            opt_i64(&q["received_wall_ms"]),
            opt_i64(&q["received_mono_ms"]),
        ) {
            (Some(i), Some(d), Some(w), Some(m)) => {
                if field_mismatch(v, "issued_ms", i)
                    || field_mismatch(v, "deadline_ms", d)
                    || field_mismatch(v, "received_wall_ms", w)
                    || field_mismatch(v, "received_mono_ms", m)
                {
                    unmeasured_reason = Some("tamper_window_mismatch".into());
                } else if i != w || d != i + PRACTICE_DEADLINE_MS as i64 {
                    unmeasured_reason = Some("tamper_window_mismatch".into());
                } else {
                    issued_ms = i;
                    deadline_ms = d;
                    received_mono_ms = Some(m);
                }
            }
            _ => {
                unmeasured_reason = Some("missing_checkpoint_window".into());
            }
        }
    }

    if !cps.ok {
        bound_head_status = "tamper_checkpoint_chain";
        unmeasured_reason = Some("tamper_checkpoint_chain".into());
    } else if unmeasured_reason.as_deref() == Some("missing_challenge_checkpoint") {
        // Already set: a practice row with no checkpoint is never "matched".
    } else if cp_match.is_some()
        && (v.get("hand_head").is_some() || cp_match.unwrap().1.get("hand_head").is_some())
        && (c_seq != cp_hand_seq || c_head != cp_hand_head)
    {
        bound_head_status = "tamper_challenge_vs_checkpoint";
        unmeasured_reason = Some("tamper_challenge_vs_checkpoint".into());
    } else if let (Some(seq), Some(ref head)) = (bound_chunk_seq, &bound_head) {
        if !hand.present {
            bound_head_status = "missing_hand_timeline";
            unmeasured_reason = Some("missing_hand_timeline".into());
        } else {
            match hand.heads.get(&seq) {
                None => {
                    bound_head_status = "bound_chunk_missing";
                    unmeasured_reason = Some("bound_chunk_missing".into());
                }
                Some(actual) if actual != head => {
                    bound_head_status = "tamper_bound_head_mismatch";
                    unmeasured_reason = Some("tamper_bound_head_mismatch".into());
                }
                Some(_) => {
                    bound_head_status = "matched";
                }
            }
        }
    } else if v.get("hand_head").is_some()
        || cp_match.is_some_and(|(_, q)| q.get("hand_head").is_some())
    {
        // Explicit null hand_head before first chunk was sealed.
        bound_head_status = "genesis_before_first_chunk";
    } else if unmeasured_reason.as_deref() != Some("missing_challenge_checkpoint") {
        bound_head_status = "unbound_no_hand_timeline";
    }

    // Practice: reject a head older than the latest window that must already have been sealed at receipt.
    if practice && unmeasured_reason.is_none() && bound_head_status == "matched" {
        if let (Some(mono), Some(bound_seq)) = (received_mono_ms, bound_chunk_seq) {
            let first = hand.heads.keys().next().copied().unwrap_or(0);
            let mono_us = (mono.max(0) as u64).saturating_mul(1_000);
            if let Some(expected) = latest_sealed_seq_at(mono_us, first) {
                if bound_seq < expected {
                    bound_head_status = "stale_bound_head";
                    unmeasured_reason = Some("stale_bound_head".into());
                }
            }
        }
    }

    // Check if stored pattern matches derived pattern.
    if unmeasured_reason.is_none() {
        if let Some(arr) = v["pattern"].as_array() {
            let stored: Vec<&str> = arr.iter().filter_map(Value::as_str).collect();
            if stored.len() != arr.len() || stored != derived {
                unmeasured_reason = Some("tamper_pattern_mismatch".into());
            }
        }
    }

    if unmeasured_reason.is_none() && deadline_ms < issued_ms {
        unmeasured_reason = Some("invalid_window".into());
    }

    // Select sealed stream: hand_timeline.v0.jsonl first, or inputs.jsonl only if no hand head was bound and hand_timeline is not present.
    let (stream_source, presses_opt, stream_err): (
        &'static str,
        Option<&[(i64, &'static str)]>,
        Option<String>,
    ) = if hand.present {
        if !hand.ok {
            ("hand_timeline", None, Some("tamper_hand_timeline".into()))
        } else if hand.wall_anchor_ms.is_none() || hand.chunks.is_empty() {
            ("hand_timeline", None, Some("no_sealed_stream".into()))
        } else {
            ("hand_timeline", Some(&hand.presses), None)
        }
    } else if bound_head_status == "unbound_no_hand_timeline" || hand.off {
        if inp.present {
            if !inp.ok {
                ("inputs", None, Some("tamper_inputs_chain".into()))
            } else if !inp.enabled {
                ("inputs", None, Some("pad_not_connected".into()))
            } else if inp.wall_anchor_ms.is_none() {
                ("inputs", None, Some("missing_wall_anchor".into()))
            } else {
                ("inputs", Some(&inp.presses), None)
            }
        } else {
            ("none", None, Some("no_sealed_stream".into()))
        }
    } else {
        ("none", None, Some("missing_hand_timeline".into()))
    };

    if unmeasured_reason.is_none() {
        unmeasured_reason = stream_err;
    }

    if let Some(reason) = unmeasured_reason {
        return ChallengeBound {
            schema: SCHEMA,
            advisory_only: true,
            receipt_status_changed: false,
            source: entry.source.clone(),
            id,
            phase,
            role,
            game,
            nonce,
            pattern,
            issued_ms,
            deadline_ms,
            status: "UNMEASURED",
            outcome: "UNMEASURED",
            in_bound: None,
            statement: statement_for("UNMEASURED", &reason),
            reason,
            stream_source,
            bound_chunk_seq,
            bound_head,
            bound_head_status,
            checkpoint_seq: cp_seq,
            response_start_ms: None,
            response_done_ms: None,
            response_span_ms: None,
            late_by_ms: None,
        };
    }

    let presses = presses_opt.unwrap_or(&[]);
    let wall_anchor = if stream_source == "hand_timeline" {
        hand.wall_anchor_ms
    } else {
        inp.wall_anchor_ms
    };
    let after_receipt = |t: i64| match (received_mono_ms, wall_anchor) {
        (Some(m), Some(a)) => t.saturating_sub(a) >= m,
        _ => true,
    };

    // For hand_timeline, check sealed chunk coverage in [issued_ms, deadline_ms].
    let hand_window_gap: Option<&'static str> = if stream_source == "hand_timeline" {
        let overlapping: Vec<&ChunkMeta> = hand
            .chunks
            .iter()
            .filter(|c| c.hi_ms > issued_ms && c.lo_ms <= deadline_ms)
            .collect();
        if overlapping.is_empty() {
            Some("stream_gap_in_window")
        } else if !overlapping.iter().any(|c| c.pad_connected) {
            Some("pad_not_connected")
        } else {
            None
        }
    } else {
        None
    };

    // Evaluate within the bound [issued_ms, deadline_ms].
    let in_window: Vec<(i64, &'static str)> = presses
        .iter()
        .copied()
        .filter(|&(t, _)| t >= issued_ms && t <= deadline_ms && after_receipt(t))
        .collect();
    let m_in = ev::match_pattern(&derived, &in_window);
    if m_in.matched {
        // Ensure the chunks up to response_done_ms had pad_connected if hand_timeline.
        if let (Some(gap), Some(done_ms)) = (hand_window_gap, m_in.done_ms) {
            let _ = done_ms;
            let reason = gap.to_string();
            return ChallengeBound {
                schema: SCHEMA,
                advisory_only: true,
                receipt_status_changed: false,
                source: entry.source.clone(),
                id,
                phase,
                role,
                game,
                nonce,
                pattern,
                issued_ms,
                deadline_ms,
                status: "UNMEASURED",
                outcome: "UNMEASURED",
                in_bound: None,
                statement: statement_for("UNMEASURED", &reason),
                reason,
                stream_source,
                bound_chunk_seq,
                bound_head,
                bound_head_status,
                checkpoint_seq: cp_seq,
                response_start_ms: None,
                response_done_ms: None,
                response_span_ms: None,
                late_by_ms: None,
            };
        }
        let start_ms = m_in.start_ms.unwrap();
        let done_ms = m_in.done_ms.unwrap();
        let span = done_ms - issued_ms;
        return ChallengeBound {
            schema: SCHEMA,
            advisory_only: true,
            receipt_status_changed: false,
            source: entry.source.clone(),
            id,
            phase,
            role,
            game,
            nonce,
            pattern,
            issued_ms,
            deadline_ms,
            status: "MEASURED",
            outcome: "answered_in_bound",
            in_bound: Some(true),
            reason: "ok".into(),
            stream_source,
            bound_chunk_seq,
            bound_head,
            bound_head_status,
            checkpoint_seq: cp_seq,
            response_start_ms: Some(start_ms),
            response_done_ms: Some(done_ms),
            response_span_ms: Some(span),
            late_by_ms: None,
            statement: statement_for("answered_in_bound", "ok"),
        };
    }

    if let Some(gap) = hand_window_gap {
        let reason = gap.to_string();
        return ChallengeBound {
            schema: SCHEMA,
            advisory_only: true,
            receipt_status_changed: false,
            source: entry.source.clone(),
            id,
            phase,
            role,
            game,
            nonce,
            pattern,
            issued_ms,
            deadline_ms,
            status: "UNMEASURED",
            outcome: "UNMEASURED",
            in_bound: None,
            statement: statement_for("UNMEASURED", &reason),
            reason,
            stream_source,
            bound_chunk_seq,
            bound_head,
            bound_head_status,
            checkpoint_seq: cp_seq,
            response_start_ms: None,
            response_done_ms: None,
            response_span_ms: None,
            late_by_ms: None,
        };
    }

    // Check if answered late (after deadline_ms, starting at or after issued_ms).
    let after_issued: Vec<(i64, &'static str)> = presses
        .iter()
        .copied()
        .filter(|&(t, _)| t >= issued_ms && after_receipt(t))
        .collect();
    let m_after = ev::match_pattern(&derived, &after_issued);
    if m_after.matched {
        let start_ms = m_after.start_ms.unwrap();
        let done_ms = m_after.done_ms.unwrap();
        if done_ms > deadline_ms {
            let span = done_ms - issued_ms;
            let late_by = done_ms - deadline_ms;
            return ChallengeBound {
                schema: SCHEMA,
                advisory_only: true,
                receipt_status_changed: false,
                source: entry.source.clone(),
                id,
                phase,
                role,
                game,
                nonce,
                pattern,
                issued_ms,
                deadline_ms,
                status: "MEASURED",
                outcome: "late",
                in_bound: Some(false),
                reason: "late".into(),
                stream_source,
                bound_chunk_seq,
                bound_head,
                bound_head_status,
                checkpoint_seq: cp_seq,
                response_start_ms: Some(start_ms),
                response_done_ms: Some(done_ms),
                response_span_ms: Some(span),
                late_by_ms: Some(late_by),
                statement: statement_for("late", "late"),
            };
        }
    }

    let reason = m_in.reason.to_string();
    ChallengeBound {
        schema: SCHEMA,
        advisory_only: true,
        receipt_status_changed: false,
        source: entry.source.clone(),
        id,
        phase,
        role,
        game,
        nonce,
        pattern,
        issued_ms,
        deadline_ms,
        status: "MEASURED",
        outcome: "absent",
        in_bound: Some(false),
        statement: statement_for("absent", &reason),
        reason,
        stream_source,
        bound_chunk_seq,
        bound_head,
        bound_head_status,
        checkpoint_seq: cp_seq,
        response_start_ms: m_in.start_ms,
        response_done_ms: None,
        response_span_ms: None,
        late_by_ms: None,
    }
}

pub fn evaluate_dir(dir: &Path) -> Result<Report, String> {
    let entries = load_challenges(dir)?;
    let hand = load_hand_stream(dir);
    let inp = load_inputs_stream(dir);
    let cps = load_checkpoints(dir);

    let mut integrity_errors = Vec::new();
    integrity_errors.extend(hand.errors.iter().cloned());
    if inp.present && !inp.ok && (!hand.present || hand.off) {
        integrity_errors.extend(inp.errors.iter().cloned());
    }
    integrity_errors.extend(cps.errors.iter().cloned());

    let records = entries
        .iter()
        .map(|e| evaluate_one(e, &hand, &inp, &cps))
        .collect();

    Ok(Report {
        records,
        integrity_errors,
        warnings: hand.warnings,
        hand_timeline_ok: hand.present.then_some(hand.ok),
        inputs_ok: inp.present.then_some(inp.ok),
        checkpoints_ok: dir.join("checkpoints.jsonl").exists().then_some(cps.ok),
    })
}

pub fn write_dir(dir: &Path) -> Result<Report, String> {
    let rep = evaluate_dir(dir)?;
    let mut text = String::new();
    for rec in &rep.records {
        let line = serde_json::to_string(rec).map_err(|e| format!("serialize {FILE_NAME}: {e}"))?;
        text.push_str(&line);
        text.push('\n');
    }
    let out = dir.join(FILE_NAME);
    let tmp = dir.join(format!("{FILE_NAME}.tmp"));
    std::fs::write(&tmp, text.as_bytes()).map_err(|e| format!("write {FILE_NAME}: {e}"))?;
    std::fs::rename(&tmp, &out).map_err(|e| format!("rename {FILE_NAME}: {e}"))?;
    Ok(rep)
}

pub fn report_lines(r: &Report) -> (Vec<String>, i32) {
    let mut out = Vec::new();
    if r.records.is_empty() {
        out.push(format!(
            "response bound: UNMEASURED (0 challenges; integrity_errors={}; advisory only; receipt status unchanged)",
            r.integrity_errors.len()
        ));
    } else {
        for rec in &r.records {
            out.push(format!(
                "response bound [{} {}]: {} outcome={} in_bound={:?} bound_head={} stream={} reason={}",
                crate::bundle::escape_output(&rec.phase),
                crate::bundle::escape_output(&rec.id),
                rec.status,
                rec.outcome,
                rec.in_bound,
                rec.bound_head_status,
                rec.stream_source,
                crate::bundle::escape_output(&rec.reason),
            ));
        }
    }
    for e in &r.integrity_errors {
        out.push(format!("  error: {}", crate::bundle::escape_output(e)));
    }
    for w in &r.warnings {
        out.push(format!("  warning: {}", crate::bundle::escape_output(w)));
    }
    let code = if r.has_tamper() { 1 } else { 0 };
    (out, code)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::hand::device::{DeviceInfo, DeviceModel};
    use crate::hand::timeline::{Config, HandTimeline, SCHEMA as HAND_SCHEMA};
    use serde_json::json;
    use std::sync::{Arc, Mutex};
    use std::time::Duration;

    fn tmp_dir(name: &str) -> std::path::PathBuf {
        let d =
            std::env::temp_dir().join(format!("qorcap-respbound-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn button_bits(names: &[&str]) -> u32 {
        let mut mask = 0u32;
        for name in names {
            let bit = hid::BUTTON_NAMES
                .iter()
                .find(|(_, n)| n == name)
                .map(|(b, _)| *b)
                .unwrap();
            mask |= bit;
        }
        mask
    }

    fn usb_report(btn: Option<&str>, counter: u8) -> [u8; 64] {
        let mut r = [0u8; 64];
        r[0] = 0x01;
        r[1] = 128;
        r[2] = 128;
        r[3] = 128;
        r[4] = 128;
        r[7] = counter;
        r[8] = 0x08; // neutral hat
        if let Some(b) = btn {
            match b {
                "UP" => r[8] = 0,
                "RIGHT" => r[8] = 2,
                "DOWN" => r[8] = 4,
                "LEFT" => r[8] = 6,
                "SQUARE" => r[8] |= 0x10,
                "CROSS" => r[8] |= 0x20,
                "CIRCLE" => r[8] |= 0x40,
                "TRIANGLE" => r[8] |= 0x80,
                "OPTIONS" => r[9] |= 0x20,
                _ => panic!("unexpected test button {b}"),
            }
        }
        r
    }

    fn test_device() -> DeviceInfo {
        DeviceInfo {
            vid: 0x054C,
            pid: 0x0DF2,
            model: DeviceModel::DualSenseEdge,
            bcd_device: 0x0100,
            pad_id: "edge-test".into(),
        }
    }

    fn make_hand_bundle(
        dir: &Path,
        wall_anchor_ms: i64,
        presses: &[(u64, &str)],
        challenge_nonce: &str,
        issued_ms: i64,
        bind_after_chunk0: bool,
    ) -> (u64, String) {
        let deadline_ms = issued_ms + PRACTICE_DEADLINE_MS as i64;
        let received_mono_ms = issued_ms - wall_anchor_ms;
        let cfg = Config {
            dir: dir.to_path_buf(),
            raw_usb: false,
            raw_min_free_bytes: None,
            wall_anchor_ms: wall_anchor_ms as u64,
            qpc: None,
            frame_clock_on: false,
            initial_device: None,
            start_t_us: 0,
        };
        let mut tl = HandTimeline::open(cfg).unwrap();
        tl.on_connected(10_000, test_device());
        // Seal and capture chunk 0 before later reports, so bind_after_chunk0 stays chunk 0
        // even when the fixture's presses (and receipt) are several seconds later.
        tl.tick(1_300_000);
        let chunk0_binding = tl.binding().unwrap();

        let mut ctr = 1u8;
        for &(t_us, btn) in presses {
            let down = usb_report(Some(btn), ctr);
            ctr = ctr.wrapping_add(1);
            tl.on_report(t_us, None, &down);
            let up = usb_report(None, ctr);
            ctr = ctr.wrapping_add(1);
            tl.on_report(t_us + 20_000, None, &up);
        }
        let finish_us = presses
            .iter()
            .map(|(t, _)| *t)
            .max()
            .unwrap_or(0)
            .saturating_add(2_000_000)
            .max(4_600_000);
        let f = tl.finish(finish_us).unwrap();

        let (bind_seq, bind_head) = if bind_after_chunk0 {
            chunk0_binding
        } else {
            (0, f.head.clone())
        };

        let pattern = ev::pattern_for(challenge_nonce).unwrap();
        let ch = json!({
            "id": &challenge_nonce[..8],
            "role": "home",
            "phase": "practice",
            "practice": true,
            "game": "cod",
            "nonce": challenge_nonce,
            "pattern": pattern,
            "issued_ms": issued_ms,
            "deadline_ms": deadline_ms,
            "received_wall_ms": issued_ms,
            "received_mono_ms": received_mono_ms,
            "hand_chunk_seq": bind_seq,
            "hand_head": bind_head,
        });
        std::fs::write(
            dir.join(PRACTICE_CHALLENGES_FILE),
            format!("{}\n", serde_json::to_string(&ch).unwrap()),
        )
        .unwrap();

        let mut chain = ev::CheckpointChain::default();
        let cp0 = chain
            .next(
                1000,
                json!({
                    "trigger": "challenge",
                    "nonce": challenge_nonce,
                    "practice": true,
                    "game": "cod",
                    "issued_ms": issued_ms,
                    "deadline_ms": deadline_ms,
                    "received_wall_ms": issued_ms,
                    "received_mono_ms": received_mono_ms,
                    "hand_chunk_seq": bind_seq,
                    "hand_head": bind_head,
                }),
            )
            .unwrap();
        let cp1 = chain
            .next(
                4600,
                json!({
                    "trigger": "stop",
                    "hand_timeline_sha256": f.file_sha256,
                    "hand_timeline_head": f.head,
                    "hand_timeline_file": HAND_FILE,
                    "hand_timeline_chunks": f.chunks,
                    "raw_usb": false,
                }),
            )
            .unwrap();
        let cps_text = format!(
            "{}\n{}\n",
            serde_json::to_string(&cp0).unwrap(),
            serde_json::to_string(&cp1).unwrap()
        );
        std::fs::write(dir.join("checkpoints.jsonl"), cps_text).unwrap();

        let check = crate::hand::check::check_dir(dir).unwrap();
        assert!(check.pass(), "fixture check failed: {:?}", check.errors);
        assert!(check.chunks >= 4);
        let _ = HAND_SCHEMA;
        (bind_seq, bind_head)
    }

    #[test]
    fn answered_in_bound_vs_late_vs_absent() {
        let anchor = 1_700_000_000_000i64;
        let nonce = "0123456789abcdef0123456789abcdef";
        let pat = ev::pattern_for(nonce).unwrap();

        // 1. Answered within the server window [issued, issued + PRACTICE_DEADLINE_MS]
        let d_ok = tmp_dir("in-bound");
        let presses_ok = [
            (1_300_000, pat[0]),
            (1_400_000, pat[1]),
            (1_500_000, pat[2]),
            (1_600_000, pat[3]),
            (1_700_000, pat[4]),
        ];
        make_hand_bundle(&d_ok, anchor, &presses_ok, nonce, anchor + 1200, true);
        let rep_ok = write_dir(&d_ok).unwrap();
        assert!(!rep_ok.has_tamper());
        assert_eq!(rep_ok.records.len(), 1);
        let r = &rep_ok.records[0];
        assert_eq!(r.status, "MEASURED");
        assert_eq!(r.outcome, "answered_in_bound");
        assert_eq!(r.in_bound, Some(true));
        assert_eq!(r.bound_head_status, "matched");
        assert_eq!(r.stream_source, "hand_timeline");
        assert_eq!(r.response_start_ms, Some(anchor + 1300));
        assert_eq!(r.response_done_ms, Some(anchor + 1700));
        assert_eq!(r.response_span_ms, Some(500));
        assert!(!r.receipt_status_changed);
        assert!(r.advisory_only);
        assert!(d_ok.join(FILE_NAME).exists());
        assert!(!d_ok.join("receipt.draft.json").exists());
        let _ = std::fs::remove_dir_all(&d_ok);

        // 2. Late: starts after the 90 s server deadline (issued at +1200 ms, deadline +91_200 ms)
        let d_late = tmp_dir("late");
        let presses_late = [
            (91_300_000, pat[0]),
            (91_600_000, pat[1]),
            (91_900_000, pat[2]),
            (92_200_000, pat[3]),
            (92_600_000, pat[4]),
        ];
        make_hand_bundle(&d_late, anchor, &presses_late, nonce, anchor + 1200, true);
        let rep_late = evaluate_dir(&d_late).unwrap();
        assert_eq!(rep_late.records[0].status, "MEASURED");
        assert_eq!(rep_late.records[0].outcome, "late");
        assert_eq!(rep_late.records[0].in_bound, Some(false));
        assert_eq!(rep_late.records[0].late_by_ms, Some(1400));
        let _ = std::fs::remove_dir_all(&d_late);

        // 3. Absent: wrong button after OPTIONS
        let d_abs = tmp_dir("absent");
        let wrong = if pat[1] == "CROSS" { "CIRCLE" } else { "CROSS" };
        let presses_abs = [(1_300_000, pat[0]), (1_400_000, wrong)];
        make_hand_bundle(&d_abs, anchor, &presses_abs, nonce, anchor + 1200, true);
        let rep_abs = evaluate_dir(&d_abs).unwrap();
        assert_eq!(rep_abs.records[0].status, "MEASURED");
        assert_eq!(rep_abs.records[0].outcome, "absent");
        assert_eq!(rep_abs.records[0].in_bound, Some(false));
        assert_eq!(rep_abs.records[0].reason, "pattern_mismatch");
        let _ = std::fs::remove_dir_all(&d_abs);
    }

    #[test]
    fn tamper_on_bound_head_is_detected() {
        let anchor = 1_700_000_000_000i64;
        let nonce = "abcdef0123456789abcdef0123456789";
        let pat = ev::pattern_for(nonce).unwrap();
        let d = tmp_dir("tamper-head");
        let presses = [
            (1_300_000, pat[0]),
            (1_400_000, pat[1]),
            (1_500_000, pat[2]),
            (1_600_000, pat[3]),
            (1_700_000, pat[4]),
        ];
        make_hand_bundle(&d, anchor, &presses, nonce, anchor + 1200, true);

        // Tamper the bound head in practice_challenges.jsonl (checkpoint still has the real one -> mismatch).
        let mut ch: Value = serde_json::from_str(
            std::fs::read_to_string(d.join(PRACTICE_CHALLENGES_FILE))
                .unwrap()
                .trim(),
        )
        .unwrap();
        ch["hand_head"] = json!("ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff");
        std::fs::write(
            d.join(PRACTICE_CHALLENGES_FILE),
            format!("{}\n", serde_json::to_string(&ch).unwrap()),
        )
        .unwrap();

        let rep = evaluate_dir(&d).unwrap();
        assert!(rep.has_tamper());
        assert_eq!(rep.records[0].status, "UNMEASURED");
        assert_eq!(rep.records[0].outcome, "UNMEASURED");
        assert_eq!(rep.records[0].in_bound, None);
        assert_eq!(rep.records[0].reason, "tamper_challenge_vs_checkpoint");

        // Also test tampering both challenge and checkpoint to a bogus chunk head -> fails against hand_timeline!
        let cps: Vec<Value> = std::fs::read_to_string(d.join("checkpoints.jsonl"))
            .unwrap()
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect();
        let mut chain = ev::CheckpointChain::default();
        let mut q0 = cps[0]["qorcap"].clone();
        q0["hand_head"] = json!("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
        ch["hand_head"] = q0["hand_head"].clone();
        std::fs::write(
            d.join(PRACTICE_CHALLENGES_FILE),
            format!("{}\n", serde_json::to_string(&ch).unwrap()),
        )
        .unwrap();
        let new0 = chain.next(1000, q0).unwrap();
        let new1 = chain.next(4600, cps[1]["qorcap"].clone()).unwrap();
        std::fs::write(
            d.join("checkpoints.jsonl"),
            format!(
                "{}\n{}\n",
                serde_json::to_string(&new0).unwrap(),
                serde_json::to_string(&new1).unwrap()
            ),
        )
        .unwrap();

        let rep2 = evaluate_dir(&d).unwrap();
        assert!(rep2.has_tamper());
        assert_eq!(rep2.records[0].status, "UNMEASURED");
        assert_eq!(rep2.records[0].reason, "tamper_bound_head_mismatch");
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn fallback_to_sealed_inputs() {
        let anchor = 1_700_000_000_000u64;
        let nonce = "11112222333344445555666677778888";
        let pat = ev::pattern_for(nonce).unwrap();
        let d = tmp_dir("inputs-fallback");
        let log = Arc::new(Mutex::new(inputs::InputLog::new(
            Some(d.join("inputs.jsonl")),
            true,
            anchor,
        )));
        {
            let mut g = log.lock().unwrap();
            for (i, &btn) in pat.iter().enumerate() {
                let t_us = 1_300_000 + (i as u64) * 100_000;
                let p = hid::Pad {
                    buttons: button_bits(&[btn]),
                    ..hid::Pad::neutral()
                };
                g.on_report(t_us, &p);
                let z = hid::Pad::neutral();
                g.on_report(t_us + 20_000, &z);
            }
        }
        inputs::seal_shared(&log, 0, || 2_000_000);

        let issued = (anchor as i64) + 1200;
        let deadline = issued + PRACTICE_DEADLINE_MS as i64;
        let ch = json!({
            "id": &nonce[..8],
            "role": "home",
            "phase": "practice",
            "practice": true,
            "game": "cod",
            "nonce": nonce,
            "pattern": pat,
            "issued_ms": issued,
            "deadline_ms": deadline,
            "received_wall_ms": issued,
            "received_mono_ms": 1200,
        });
        std::fs::write(
            d.join(PRACTICE_CHALLENGES_FILE),
            format!("{}\n", serde_json::to_string(&ch).unwrap()),
        )
        .unwrap();
        let mut chain = ev::CheckpointChain::default();
        let cp0 = chain
            .next(
                1000,
                json!({
                    "trigger": "challenge",
                    "nonce": nonce,
                    "practice": true,
                    "game": "cod",
                    "issued_ms": issued,
                    "deadline_ms": deadline,
                    "received_wall_ms": issued,
                    "received_mono_ms": 1200,
                }),
            )
            .unwrap();
        std::fs::write(
            d.join("checkpoints.jsonl"),
            format!("{}\n", serde_json::to_string(&cp0).unwrap()),
        )
        .unwrap();

        let rep = evaluate_dir(&d).unwrap();
        assert!(!rep.has_tamper());
        assert_eq!(rep.records[0].stream_source, "inputs");
        assert_eq!(rep.records[0].status, "MEASURED");
        assert_eq!(rep.records[0].outcome, "answered_in_bound");
        assert_eq!(rep.records[0].bound_head_status, "unbound_no_hand_timeline");
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn wording_excludes_all_banned_terms() {
        let banned = [
            ["veri", "fied"].concat(),
            ["pro", "ven"].concat(),
            ["sk", "ill"].concat(),
            ["reaction ", "time"].concat(),
            ["anti-", "cheat"].concat(),
            ["hu", "man"].concat(),
            ["proof-of-", "human"].concat(),
        ];
        for (outcome, reason) in [
            ("answered_in_bound", "ok"),
            ("late", "late"),
            ("absent", "no_pause_press"),
            ("UNMEASURED", "tamper_bound_head_mismatch"),
        ] {
            let s = statement_for(outcome, reason);
            let low = s.to_ascii_lowercase();
            for b in &banned {
                assert!(!low.contains(b), "banned term {b:?} in {s:?}");
            }
            assert!(
                s.starts_with("MEASURED:") || s.starts_with("UNMEASURED:"),
                "must start with MEASURED/UNMEASURED: {s}"
            );
            assert!(s.contains("who held the controller"));
            assert!(s.contains("never changes receipt status"));
        }
        let _ = Duration::from_secs(0);
    }

    #[test]
    fn unsealed_tail_is_ignored_and_missing_bound_hand_timeline_never_falls_back_to_inputs() {
        let anchor = 1_700_000_000_000i64;
        let nonce = "22223333444455556666777788889999";
        let pat = ev::pattern_for(nonce).unwrap();

        // 1a. Mid-session crash (no stop checkpoint/summary yet) with unsealed chunk-5 answer rows after chunk 4:
        //     check_dir passes with an unsealed-tail warning, and unsealed rows must NOT answer the nonce.
        let d = tmp_dir("unsealed-tail");
        make_hand_bundle(&d, anchor, &[], nonce, anchor + 1200, true);
        let _ = std::fs::remove_file(d.join(crate::hand::timeline::SUMMARY_FILE));
        let cp0_line = std::fs::read_to_string(d.join("checkpoints.jsonl"))
            .unwrap()
            .lines()
            .next()
            .unwrap()
            .to_string();
        std::fs::write(d.join("checkpoints.jsonl"), format!("{cp0_line}\n")).unwrap();
        let mut raw = std::fs::read_to_string(d.join(HAND_FILE)).unwrap();
        for (i, &btn) in pat.iter().enumerate() {
            let row = json!({"type": "in", "chunk_seq": 5, "t_us": 5_100_000 + (i as u64) * 50_000, "buttons": button_bits(&[btn])});
            raw.push_str(&ev::jcs(&row).unwrap());
            raw.push('\n');
        }
        std::fs::write(d.join(HAND_FILE), raw).unwrap();
        let rep = evaluate_dir(&d).unwrap();
        assert_eq!(rep.records[0].status, "MEASURED");
        assert_eq!(
            rep.records[0].outcome, "absent",
            "unsealed tail rows must not answer the nonce"
        );
        assert_eq!(rep.records[0].in_bound, Some(false));
        let _ = std::fs::remove_dir_all(&d);

        // 2. If hand_head was bound at challenge time and hand_timeline.v0.jsonl is deleted, do NOT
        //    silently fall back to inputs.jsonl to invent a pass.
        let d2 = tmp_dir("missing-bound-hand");
        let (_, bound_head) = make_hand_bundle(&d2, anchor, &[], nonce, anchor + 1200, true);
        assert!(!bound_head.is_empty());
        let log = Arc::new(Mutex::new(inputs::InputLog::new(
            Some(d2.join("inputs.jsonl")),
            true,
            anchor as u64,
        )));
        {
            let mut g = log.lock().unwrap();
            for (i, &btn) in pat.iter().enumerate() {
                let t_us = 1_300_000 + (i as u64) * 100_000;
                g.on_report(
                    t_us,
                    &hid::Pad {
                        buttons: button_bits(&[btn]),
                        ..hid::Pad::neutral()
                    },
                );
                g.on_report(t_us + 20_000, &hid::Pad::neutral());
            }
        }
        inputs::seal_shared(&log, 0, || 2_000_000);
        std::fs::remove_file(d2.join(HAND_FILE)).unwrap();

        let rep2 = evaluate_dir(&d2).unwrap();
        assert!(rep2.has_tamper());
        assert_eq!(rep2.records[0].status, "UNMEASURED");
        assert_eq!(rep2.records[0].bound_head_status, "missing_hand_timeline");
        assert_eq!(rep2.records[0].reason, "missing_hand_timeline");
        let _ = std::fs::remove_dir_all(&d2);
    }

    #[test]
    fn backdated_presses_before_receipt_are_not_answered_in_bound() {
        // Server issued at +2.1 s (chunk 0 is still the latest sealed head). Presses exist only
        // in the first two seconds — the window a client would have claimed by backdating issued_ms.
        let nonce = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
        let pat = ev::pattern_for(nonce).unwrap();
        let d = tmp_dir("backdated-presses");
        let anchor: i64 = 1_700_000_000_000;
        let presses = [
            (1_300_000, pat[0]),
            (1_600_000, pat[1]),
            (1_900_000, pat[2]),
            (2_200_000, pat[3]),
            (2_600_000, pat[4]),
        ];
        make_hand_bundle(&d, anchor, &presses, nonce, anchor + 2_100, true);
        let r = &evaluate_dir(&d).unwrap().records[0];
        assert_ne!(r.outcome, "answered_in_bound");
        assert_eq!(r.in_bound, Some(false));
        assert_eq!(r.outcome, "absent");
        assert_eq!(r.issued_ms, anchor + 2_100);
        assert_eq!(r.deadline_ms, anchor + 2_100 + PRACTICE_DEADLINE_MS as i64);
    }

    #[test]
    fn edited_row_window_is_tamper_not_answered_in_bound() {
        let nonce = "cccccccccccccccccccccccccccccccc";
        let pat = ev::pattern_for(nonce).unwrap();
        let d = tmp_dir("edited-row");
        let anchor: i64 = 1_700_000_000_000;
        let presses = [
            (1_300_000, pat[0]),
            (1_600_000, pat[1]),
            (1_900_000, pat[2]),
            (2_200_000, pat[3]),
            (2_600_000, pat[4]),
        ];
        make_hand_bundle(&d, anchor, &presses, nonce, anchor + 1_200, true);
        let path = d.join(PRACTICE_CHALLENGES_FILE);
        let mut row: Value =
            serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        row["deadline_ms"] = json!(anchor + 1_200 + 5_000); // disagrees with the 90 s checkpoint window
        std::fs::write(&path, format!("{}\n", serde_json::to_string(&row).unwrap())).unwrap();
        let r = &evaluate_dir(&d).unwrap().records[0];
        assert_eq!(r.status, "UNMEASURED");
        assert_eq!(r.reason, "tamper_window_mismatch");
        assert_ne!(r.outcome, "answered_in_bound");
        assert_ne!(r.in_bound, Some(true));
    }

    #[test]
    fn practice_row_with_no_checkpoint_is_unmeasured_not_matched() {
        let nonce = "dddddddddddddddddddddddddddddddd";
        let pat = ev::pattern_for(nonce).unwrap();
        let d = tmp_dir("no-checkpoint");
        let anchor: i64 = 1_700_000_000_000;
        let presses = [
            (1_300_000, pat[0]),
            (1_600_000, pat[1]),
            (1_900_000, pat[2]),
            (2_200_000, pat[3]),
            (2_600_000, pat[4]),
        ];
        make_hand_bundle(&d, anchor, &presses, nonce, anchor + 1_200, true);
        std::fs::remove_file(d.join("checkpoints.jsonl")).unwrap();
        let r = &evaluate_dir(&d).unwrap().records[0];
        assert_eq!(r.status, "UNMEASURED");
        assert_eq!(r.reason, "missing_challenge_checkpoint");
        assert_eq!(r.bound_head_status, "unbound_no_checkpoint");
        assert_ne!(r.outcome, "answered_in_bound");
        assert_ne!(r.bound_head_status, "matched");
        assert_ne!(r.in_bound, Some(true));
    }

    #[test]
    fn stale_bound_head_is_not_answered_in_bound() {
        // Receipt at +5.2 s, when later chunks must already have been sealed, but the bound
        // head is still chunk 0. Even if the pattern is pressed after receipt, this is not in-bound.
        let nonce = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee";
        let pat = ev::pattern_for(nonce).unwrap();
        let d = tmp_dir("stale-head");
        let anchor: i64 = 1_700_000_000_000;
        let presses = [
            (5_300_000, pat[0]),
            (5_600_000, pat[1]),
            (5_900_000, pat[2]),
            (6_200_000, pat[3]),
            (6_600_000, pat[4]),
        ];
        make_hand_bundle(&d, anchor, &presses, nonce, anchor + 5_200, true);
        let r = &evaluate_dir(&d).unwrap().records[0];
        assert_eq!(r.status, "UNMEASURED");
        assert_eq!(r.reason, "stale_bound_head");
        assert_eq!(r.bound_head_status, "stale_bound_head");
        assert_ne!(r.outcome, "answered_in_bound");
        assert_ne!(r.in_bound, Some(true));
    }
}
