//! `qorcap check-hand-timeline DIR`: re-hash every 1 s window, re-link the chain, re-hash each raw companion
//! file, and compare with the checkpoints that bind the hand chain (segment `hand_chunk_seq` / `hand_head`,
//! stop `hand_timeline_sha256` / `hand_timeline_head`). Read-only. Leaves `verify-bundle` untouched.

use super::timeline::{CHUNK_US, FILE_NAME, RAW_DIR, RAW_RECORD_BYTES, SUMMARY_FILE, UNMEASURED};
use crate::evidence::{self as ev, GENESIS};
use crate::hid::{BACK_LEFT, BACK_RIGHT, FN_LEFT, FN_RIGHT};
use serde_json::Value;
use std::collections::{BTreeMap, HashSet};
use std::path::Path;

const EDGE_BITS: u64 = (FN_LEFT | FN_RIGHT | BACK_LEFT | BACK_RIGHT) as u64;

#[derive(Debug, Default, Clone)]
pub struct Report {
    pub chunks: u64,
    pub head: String,
    pub file_sha256: String,
    pub device_models: Vec<String>,
    pub fallback: bool,
    pub raw_files: u64,
    pub segment_bindings: u64,
    pub stop_bound: bool,
    /// True when checkpoints record `hand_timeline: false` and there is no file (a normal off session).
    pub off: bool,
    pub errors: Vec<String>,
    pub warnings: Vec<String>,
}
impl Report { pub fn pass(&self) -> bool { self.errors.is_empty() } }

fn t_of(r: &Value) -> Option<u64> { r.get("t_us").and_then(Value::as_u64).or_else(|| r.get("t_arrival_us").and_then(Value::as_u64)) }

/// Flags recorded on checkpoints in `dir`: `(hand_timeline == false, raw_usb == false, a stop sha is bound)`.
pub fn checkpoint_flags(dir: &Path) -> (bool, bool, bool) {
    let mut hand_off = false;
    let mut raw_off = false;
    let mut has_sha = false;
    let Ok(cps) = std::fs::read_to_string(dir.join("checkpoints.jsonl")) else { return (false, false, false) };
    for line in cps.lines().filter(|l| !l.trim().is_empty()) {
        let Ok(cp) = serde_json::from_str::<Value>(line) else { continue };
        let q = &cp["qorcap"];
        if q.get("hand_timeline") == Some(&Value::Bool(false)) { hand_off = true; }
        if q.get("raw_usb") == Some(&Value::Bool(false)) { raw_off = true; }
        if q["hand_timeline_sha256"].is_string() { has_sha = true; }
    }
    (hand_off, raw_off, has_sha)
}

pub fn check_dir(dir: &Path) -> Result<Report, String> {
    check_dir_heads(dir).map(|(r, _)| r)
}

pub fn check_dir_heads(dir: &Path) -> Result<(Report, BTreeMap<u64, String>), String> {
    let path = dir.join(FILE_NAME);
    if !path.exists() {
        let (hand_off, _, has_sha) = checkpoint_flags(dir);
        if hand_off && !has_sha {
            // A normal off session: no file, and the stop/start checkpoint says so. Not tampered, not a failure.
            return Ok((Report { off: true, ..Default::default() }, BTreeMap::new()));
        }
        if hand_off && has_sha {
            return Ok((Report { off: true, errors: vec!["checkpoints mark hand_timeline false but also bind a file that is missing".into()], ..Default::default() }, BTreeMap::new()));
        }
        return Err(format!("{FILE_NAME}: not found"));
    }
    let bytes = std::fs::read(&path).map_err(|e| format!("{FILE_NAME}: {e}"))?;
    let mut r = Report { file_sha256: ev::sha256_hex(&bytes), head: GENESIS.to_string(), ..Default::default() };
    let text = String::from_utf8(bytes).map_err(|_| format!("{FILE_NAME} is not UTF-8"))?;
    let mut pending: Vec<Value> = vec![];
    let mut last_seq: Option<u64> = None;
    let mut heads: BTreeMap<u64, String> = BTreeMap::new();
    let mut last_in_t: Option<u64> = None;
    let mut used_raw: HashSet<String> = HashSet::new();
    for (i, line) in text.split('\n').enumerate() {
        if line.is_empty() { continue; }
        let v: Value = match serde_json::from_str(line) { Ok(v) => v, Err(e) => { r.errors.push(format!("line {}: not JSON ({e})", i + 1)); continue; } };
        match ev::jcs(&v) { Ok(c) if c == line => {}, Ok(_) => r.errors.push(format!("line {}: not in JCS form", i + 1)), Err(e) => r.errors.push(format!("line {}: {e}", i + 1)) }
        if v["type"] != "chunk" {
            if v["type"] == "in" {
                if let Some(t) = t_of(&v) { if last_in_t.is_some_and(|p| t < p) { r.errors.push(format!("line {}: in t_us goes backwards", i + 1)); } last_in_t = Some(t); }
            }
            pending.push(v);
            continue;
        }
        let Some(seq) = v["seq"].as_u64() else { r.errors.push(format!("line {}: chunk without seq", i + 1)); pending.clear(); continue; };
        let records = std::mem::take(&mut pending);
        match last_seq {
            None => if records.first().map(|h| h["type"] != "header" || h["schema"] != super::timeline::SCHEMA).unwrap_or(true) {
                r.errors.push(format!("chunk {seq}: the first window must start with the {} header", super::timeline::SCHEMA));
            },
            Some(p) => if seq != p + 1 { r.errors.push(format!("chunk {seq}: expected seq {}", p + 1)); },
        }
        last_seq = Some(seq);
        let (lo, hi) = (seq * CHUNK_US, (seq + 1) * CHUNK_US);
        for rec in &records {
            if rec["chunk_seq"].as_u64() != Some(seq) { r.errors.push(format!("chunk {seq}: a {} record names chunk_seq {}", rec["type"], rec["chunk_seq"])); }
            if matches!(rec["type"].as_str(), Some("in" | "status" | "frame" | "profile_swap_candidate")) {
                if let Some(t) = t_of(rec) {
                    let late = rec["late"] == true;
                    if !late && !(lo..hi).contains(&t) { r.errors.push(format!("chunk {seq}: {} at {t} is outside the window", rec["type"])); }
                    if late && t >= lo { r.errors.push(format!("chunk {seq}: a late record at {t} is not from an earlier window")); }
                }
            }
        }
        let sha = match ev::try_jcs_sha(&Value::Array(records.clone())) { Ok(s) => s, Err(e) => { r.errors.push(format!("chunk {seq}: {e}")); continue; } };
        if v["sha256"].as_str() != Some(sha.as_str()) { r.errors.push(format!("chunk {seq}: records SHA-256 does not match")); }
        if v["n"].as_u64() != Some(records.len() as u64) { r.errors.push(format!("chunk {seq}: n does not match the record count")); }
        if v["prev"].as_str() != Some(r.head.as_str()) { r.errors.push(format!("chunk {seq}: prev does not match the previous head")); }
        let head = ev::link(&r.head, seq, &sha);
        if v["head"].as_str() != Some(head.as_str()) { r.errors.push(format!("chunk {seq}: head does not re-link")); }
        r.head = head.clone();
        heads.insert(seq, head);
        r.chunks += 1;
        let k = records.len();
        let (hb, rb) = if k >= 2 { (&records[k - 2], &records[k - 1]) } else { (&Value::Null, &Value::Null) };
        if hb["type"] != "hb" || rb["type"] != "raw_bind" { r.errors.push(format!("chunk {seq}: must end with hb then raw_bind")); continue; }
        if v["device_model"] != hb["device_model"] || v["fallback"] != hb["fallback"] { r.errors.push(format!("chunk {seq}: device_model differs from the sealed hb")); }
        if v["raw_sha256"] != rb["sha256"] || v["raw_file"] != rb["file"] { r.errors.push(format!("chunk {seq}: raw binding differs from the sealed raw_bind")); }
        if let Some(m) = hb["device_model"].as_str() { if !r.device_models.iter().any(|x| x == m) && !matches!(m, "none" | "UNKNOWN") { r.device_models.push(m.into()); } }
        if hb["fallback"] == true { r.fallback = true; }
        // Edge-only fields on a standard DualSense: UNMEASURED, never zero / false.
        for rec in &records {
            if rec["device_model"] == "dualsense" && rec["type"] == "in" {
                if rec["edge_bits"] != UNMEASURED || rec["buttons"].as_u64().unwrap_or(0) & EDGE_BITS != 0 {
                    r.errors.push(format!("chunk {seq}: a dualsense in row carries Edge-only bits"));
                }
            }
        }
        if hb["device_model"] == "dualsense" && records.iter().any(|x| x["type"] == "profile_swap_candidate") {
            r.errors.push(format!("chunk {seq}: profile_swap_candidate on a standard DualSense"));
        }
        // Raw companion file.
        if let Some(f) = rb["file"].as_str() {
            let want = format!("{RAW_DIR}/w{seq:010}.bin");
            if f != want { r.errors.push(format!("chunk {seq}: raw file name {f:?} is not {want}")); continue; }
            used_raw.insert(want.clone());
            match std::fs::read(dir.join(&want)) {
                Err(e) => r.errors.push(format!("chunk {seq}: raw file missing ({e})")),
                Ok(raw) => {
                    r.raw_files += 1;
                    if rb["sha256"].as_str() != Some(ev::sha256_hex(&raw).as_str()) { r.errors.push(format!("chunk {seq}: raw file SHA-256 does not match")); }
                    let nrep = rb["n_reports"].as_u64().unwrap_or(u64::MAX);
                    if raw.len() % RAW_RECORD_BYTES != 0 || raw.len() as u64 != nrep.saturating_mul(RAW_RECORD_BYTES as u64) || rb["bytes"].as_u64() != Some(raw.len() as u64) {
                        r.errors.push(format!("chunk {seq}: raw file size does not match n_reports x {RAW_RECORD_BYTES}"));
                    }
                    let shas: HashSet<String> = raw.chunks_exact(RAW_RECORD_BYTES).map(|c| ev::sha256_hex(&c[..RAW_RECORD_BYTES - 8])).collect();
                    for rec in records.iter().filter(|x| x["type"] == "in") {
                        if !rec["raw_sha256"].as_str().is_some_and(|s| shas.contains(s)) { r.errors.push(format!("chunk {seq}: an in row's raw_sha256 is not in the raw file")); break; }
                    }
                }
            }
        } else if rb["raw_usb"] == true && rb["paused"].is_null() && rb["n_reports"].as_u64().unwrap_or(0) > 0 {
            r.errors.push(format!("chunk {seq}: raw on with reports but no raw file"));
        }
    }
    if !pending.is_empty() { r.warnings.push(format!("{} record(s) after the last chunk are an unsealed tail (not counted)", pending.len())); }
    if let Ok(rd) = std::fs::read_dir(dir.join(RAW_DIR)) {
        let extra = rd.filter_map(Result::ok).filter(|e| !used_raw.contains(&format!("{RAW_DIR}/{}", e.file_name().to_string_lossy()))).count();
        if extra > 0 { r.warnings.push(format!("{extra} raw file(s) in {RAW_DIR}/ are not bound by any sealed window")); }
    }
    if let Ok(s) = std::fs::read_to_string(dir.join(SUMMARY_FILE)) {
        match serde_json::from_str::<Value>(&s) {
            Ok(sv) => {
                if sv["sha256"].as_str() != Some(r.file_sha256.as_str()) { r.errors.push(format!("{SUMMARY_FILE}: sha256 does not match {FILE_NAME}")); }
                if sv["head"].as_str() != Some(r.head.as_str()) { r.errors.push(format!("{SUMMARY_FILE}: head does not match the last chunk")); }
            }
            Err(e) => r.errors.push(format!("{SUMMARY_FILE}: not JSON ({e})")),
        }
    }
    // Checkpoint bindings (when the bundle's checkpoints.jsonl is beside the file).
    if let Ok(cps) = std::fs::read_to_string(dir.join("checkpoints.jsonl")) {
        for line in cps.lines().filter(|l| !l.trim().is_empty()) {
            let Ok(cp) = serde_json::from_str::<Value>(line) else { continue };
            let q = &cp["qorcap"];
            if let Some(h) = q["hand_head"].as_str() {
                r.segment_bindings += 1;
                match q["hand_chunk_seq"].as_u64().and_then(|s| heads.get(&s)) {
                    Some(want) if want == h => {}
                    _ => r.errors.push(format!("checkpoint {}: hand_head does not match chunk {}", cp["seq"], q["hand_chunk_seq"])),
                }
            }
            if let Some(s) = q["hand_timeline_sha256"].as_str() {
                r.stop_bound = true;
                if s != r.file_sha256 { r.errors.push(format!("checkpoint {}: hand_timeline_sha256 does not match {FILE_NAME}", cp["seq"])); }
                if q["hand_timeline_head"].as_str() != Some(r.head.as_str()) { r.errors.push(format!("checkpoint {}: hand_timeline_head does not match the last chunk", cp["seq"])); }
            }
        }
    }
    Ok((r, heads))
}

/// Printable lines and the exit code (0 PASS, 1 FAIL).
pub fn report_lines(r: &Report) -> (Vec<String>, i32) {
    if r.off && r.chunks == 0 {
        let mut out = vec![format!("hand timeline: {} (off for this session; not a failure)", if r.pass() { "PASS" } else { "FAIL" })];
        out.extend(r.errors.iter().map(|e| format!("  error: {}", crate::bundle::escape_output(e))));
        out.extend(r.warnings.iter().map(|w| format!("  warning: {}", crate::bundle::escape_output(w))));
        return (out, if r.pass() { 0 } else { 1 });
    }
    let mut out = vec![format!("hand timeline: {} chunks={} head={} file_sha256={} raw_files={} segment_bindings={} stop_bound={}{}",
        if r.pass() { "PASS" } else { "FAIL" }, r.chunks, r.head, r.file_sha256, r.raw_files, r.segment_bindings, r.stop_bound,
        if r.fallback { " device=dualsense (fallback)" } else { "" })];
    out.extend(r.errors.iter().map(|e| format!("  error: {}", crate::bundle::escape_output(e))));
    out.extend(r.warnings.iter().map(|w| format!("  warning: {}", crate::bundle::escape_output(w))));
    (out, if r.pass() { 0 } else { 1 })
}
