//! `qorcap check-hand-timeline DIR`: re-hash every 1 s window, re-link the chain, verify every raw slice
//! (`raw_file`, `raw_offset`, `raw_len`, `raw_sha256`) inside `hand_raw.v0.bin`, and compare with the
//! checkpoints that bind the hand chain (start/stop `hand_timeline_dir`, segment `hand_chunk_seq` / `hand_head`,
//! stop `hand_timeline_sha256` / `hand_timeline_head`).
//!
//! When `hand_timeline.v0.jsonl` and `hand_raw.v0.bin` are stored off-bundle (e.g. `D:\QorCap\practice-hand\<session>\`
//! or `--hand-dir`), passing the bundle directory reads `hand_timeline_dir` from `checkpoints.jsonl` to locate them;
//! passing the hand directory directly also works. Read-only. Leaves `verify-bundle` untouched.

use super::timeline::{CHUNK_US, FILE_NAME, RAW_FILE, RAW_LAYOUT, RAW_RECORD_BYTES, SUMMARY_FILE, UNMEASURED};
use crate::evidence::{self as ev, GENESIS};
use crate::hid::{BACK_LEFT, BACK_RIGHT, FN_LEFT, FN_RIGHT};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashSet};
use std::io::{BufRead, BufReader, ErrorKind, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};

const EDGE_BITS: u64 = (FN_LEFT | FN_RIGHT | BACK_LEFT | BACK_RIGHT) as u64;

#[derive(Debug, Default, Clone)]
pub struct Report {
    pub chunks: u64,
    pub head: String,
    pub file_sha256: String,
    pub device_models: Vec<String>,
    pub fallback: bool,
    pub raw_files: u64,
    pub raw_slices: u64,
    pub raw_missing: bool,
    pub segment_bindings: u64,
    pub stop_bound: bool,
    /// True when checkpoints record `hand_timeline: false` and there is no file (a normal off session).
    pub off: bool,
    pub hand_dir: Option<PathBuf>,
    pub hand_dir_source: Option<String>,
    pub hand_dir_fallback_reason: Option<String>,
    pub errors: Vec<String>,
    pub warnings: Vec<String>,
}
impl Report { pub fn pass(&self) -> bool { self.errors.is_empty() } }

#[derive(Debug, Clone)]
pub struct ResolvedHandDir {
    pub hand_dir: PathBuf,
    pub bound_in_checkpoints: bool,
}

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

/// Resolve the hand output directory for `dir` (bundle folder or direct hand folder).
/// If `dir/hand_timeline.v0.jsonl` exists, it is used directly; otherwise `dir/checkpoints.jsonl`
/// is inspected for a bound `hand_timeline_dir` (e.g. `D:\QorCap\practice-hand\<session>\` or a relative pointer).
pub fn resolve_hand_dir(dir: &Path) -> ResolvedHandDir {
    if dir.join(FILE_NAME).exists() {
        return ResolvedHandDir { hand_dir: dir.to_path_buf(), bound_in_checkpoints: false };
    }
    let Ok(cps) = std::fs::read_to_string(dir.join("checkpoints.jsonl")) else {
        return ResolvedHandDir { hand_dir: dir.to_path_buf(), bound_in_checkpoints: false };
    };
    let mut ptr: Option<String> = None;
    let mut bound = false;
    for line in cps.lines().filter(|l| !l.trim().is_empty()) {
        let Ok(cp) = serde_json::from_str::<Value>(line) else { continue };
        let q = &cp["qorcap"];
        if q["hand_timeline_sha256"].is_string() || q["hand_head"].is_string() {
            bound = true;
        }
        if let Some(s) = q["hand_timeline_dir"].as_str().filter(|s| !s.trim().is_empty()) {
            ptr = Some(s.to_string());
            bound = true;
        }
    }
    if let Some(p_str) = ptr {
        let p = Path::new(&p_str);
        let resolved = if p.is_relative() { dir.join(p) } else { p.to_path_buf() };
        return ResolvedHandDir { hand_dir: resolved, bound_in_checkpoints: true };
    }
    ResolvedHandDir { hand_dir: dir.to_path_buf(), bound_in_checkpoints: bound }
}

pub fn check_dir(dir: &Path) -> Result<Report, String> {
    check_dir_heads(dir).map(|(r, _)| r)
}

pub fn check_dir_heads(dir: &Path) -> Result<(Report, BTreeMap<u64, String>), String> {
    let resolved = resolve_hand_dir(dir);
    let hand_dir = resolved.hand_dir;
    let path = hand_dir.join(FILE_NAME);
    if !path.exists() {
        let (hand_off, _, has_sha) = checkpoint_flags(dir);
        if hand_off && !has_sha {
            // A normal off session: no file, and the stop/start checkpoint says so. Not tampered, not a failure.
            return Ok((Report { off: true, ..Default::default() }, BTreeMap::new()));
        }
        if hand_off && has_sha {
            return Ok((Report { off: true, errors: vec!["checkpoints mark hand_timeline false but also bind a file that is missing".into()], ..Default::default() }, BTreeMap::new()));
        }
        return Err(format!("{FILE_NAME}: not found (looked in {})", hand_dir.display()));
    }
    let file = std::fs::File::open(&path).map_err(|e| format!("{FILE_NAME}: {e}"))?;
    let mut reader = BufReader::with_capacity(64 * 1024, file);
    let mut file_hasher = Sha256::new();
    let mut r = Report {
        file_sha256: String::new(),
        head: GENESIS.to_string(),
        hand_dir: Some(hand_dir.clone()),
        ..Default::default()
    };
    let mut pending: Vec<Value> = vec![];
    let mut last_seq: Option<u64> = None;
    let mut heads: BTreeMap<u64, String> = BTreeMap::new();
    let mut last_in_t: Option<u64> = None;

    struct RawFileHandle {
        file: std::fs::File,
        len: u64,
    }
    let raw_path = hand_dir.join(RAW_FILE);
    let mut raw_handle: Option<Result<RawFileHandle, String>> = None;
    let mut raw_missing_reported = false;
    let mut raw_active_seen = false;
    let mut raw_off_only = false;
    let mut expected_raw_offset: u64 = 0;
    let mut last_raw_t_us: Option<u64> = None;

    let mut header_hand_dir: Option<String> = None;
    let mut header_source: Option<String> = None;
    let mut header_reason: Value = Value::Null;
    let mut header_raw_file: Value = Value::Null;

    let mut line_buf: Vec<u8> = Vec::with_capacity(4096);
    let mut i: usize = 0;
    loop {
        line_buf.clear();
        let n = reader.read_until(b'\n', &mut line_buf).map_err(|e| format!("{FILE_NAME}: {e}"))?;
        if n == 0 {
            break;
        }
        file_hasher.update(&line_buf);
        let raw_line = if line_buf.ends_with(b"\n") { &line_buf[..line_buf.len() - 1] } else { &line_buf[..] };
        let line_no = i + 1;
        i += 1;
        let line = std::str::from_utf8(raw_line).map_err(|_| format!("{FILE_NAME} is not UTF-8"))?;
        if line.is_empty() { continue; }
        let v: Value = match serde_json::from_str(line) { Ok(v) => v, Err(e) => { r.errors.push(format!("line {line_no}: not JSON ({e})")); continue; } };
        match ev::jcs(&v) { Ok(c) if c == line => {}, Ok(_) => r.errors.push(format!("line {line_no}: not in JCS form")), Err(e) => r.errors.push(format!("line {line_no}: {e}")) }
        if v["type"] != "chunk" {
            if v["type"] == "in" {
                if let Some(t) = t_of(&v) { if last_in_t.is_some_and(|p| t < p) { r.errors.push(format!("line {line_no}: in t_us goes backwards")); } last_in_t = Some(t); }
            }
            pending.push(v);
            continue;
        }
        let Some(seq) = v["seq"].as_u64() else { r.errors.push(format!("line {line_no}: chunk without seq")); pending.clear(); continue; };
        let records = std::mem::take(&mut pending);
        match last_seq {
            None => {
                if records.first().map(|h| h["type"] != "header" || h["schema"] != super::timeline::SCHEMA).unwrap_or(true) {
                    r.errors.push(format!("chunk {seq}: the first window must start with the {} header", super::timeline::SCHEMA));
                } else if let Some(h) = records.first() {
                    if h["raw_layout"].as_str() != Some(RAW_LAYOUT)
                        || h["raw_record_bytes"].as_u64() != Some(RAW_RECORD_BYTES as u64)
                    {
                        r.errors.push(format!(
                            "chunk {seq}: unsupported raw layout {:?} ({} B), expected {RAW_LAYOUT:?} ({RAW_RECORD_BYTES} B)",
                            h["raw_layout"], h["raw_record_bytes"]
                        ));
                    }
                    header_hand_dir = h["hand_dir"].as_str().map(String::from);
                    header_source = h["hand_dir_source"].as_str().map(String::from);
                    header_reason = h["hand_dir_fallback_reason"].clone();
                    header_raw_file = h["raw_file"].clone();
                    r.hand_dir_source = header_source.clone();
                    r.hand_dir_fallback_reason = header_reason.as_str().map(String::from);
                }
            }
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
        if v["raw_sha256"] != rb["sha256"] || v["raw_file"] != rb["file"]
            || v["raw_offset"] != rb["offset"] || v["raw_len"] != rb["len"]
        {
            r.errors.push(format!("chunk {seq}: raw binding differs from the sealed raw_bind"));
        }
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

        // Single append-only raw slice verification.
        if rb["layout"].as_str() != Some(RAW_LAYOUT)
            || rb["record_bytes"].as_u64() != Some(RAW_RECORD_BYTES as u64)
        {
            r.errors.push(format!(
                "chunk {seq}: unsupported raw_bind layout {:?} ({} B), expected {RAW_LAYOUT:?} ({RAW_RECORD_BYTES} B)",
                rb["layout"], rb["record_bytes"]
            ));
            continue;
        }
        let raw_usb_on = rb["raw_usb"].as_bool() == Some(true);
        let nrep = rb["n_reports"].as_u64().unwrap_or(0);
        if !raw_usb_on {
            raw_off_only = true;
            if !rb["file"].is_null() || !rb["offset"].is_null() || !rb["len"].is_null() || !rb["sha256"].is_null() {
                r.errors.push(format!("chunk {seq}: raw_usb is false but raw slice fields are not null"));
            }
            continue;
        }
        if !rb["paused"].is_null() && nrep == 0 {
            continue;
        }
        raw_active_seen = true;
        let Some(f) = rb["file"].as_str() else {
            r.errors.push(format!("chunk {seq}: raw on with no raw_file"));
            continue;
        };
        if f != RAW_FILE {
            r.errors.push(format!("chunk {seq}: raw file name {f:?} is not {RAW_FILE}"));
            continue;
        }
        let (Some(offset), Some(len), Some(want_sha)) = (rb["offset"].as_u64(), rb["len"].as_u64(), rb["sha256"].as_str()) else {
            r.errors.push(format!("chunk {seq}: raw_bind missing offset, len, or sha256"));
            continue;
        };
        if len % (RAW_RECORD_BYTES as u64) != 0
            || len != nrep.saturating_mul(RAW_RECORD_BYTES as u64)
            || rb["bytes"].as_u64() != Some(len)
        {
            r.errors.push(format!("chunk {seq}: raw slice length {len} does not match n_reports x {RAW_RECORD_BYTES}"));
        }
        if offset < expected_raw_offset {
            r.errors.push(format!("chunk {seq}: raw_offset {offset} overlaps previous slice ending at {expected_raw_offset}"));
            continue;
        }
        if offset > expected_raw_offset {
            r.errors.push(format!("chunk {seq}: raw_offset {offset} leaves a gap after {expected_raw_offset}"));
            continue;
        }
        expected_raw_offset = offset.saturating_add(len);

        let cached = raw_handle.get_or_insert_with(|| match std::fs::File::open(&raw_path) {
            Ok(file) => match file.metadata() {
                Ok(meta) => Ok(RawFileHandle { file, len: meta.len() }),
                Err(e) => Err(format!("raw file {RAW_FILE} unreadable ({e})")),
            },
            Err(e) => {
                if e.kind() == ErrorKind::NotFound {
                    r.raw_missing = true;
                    Err(format!("raw file {RAW_FILE} missing ({e})"))
                } else {
                    Err(format!("raw file {RAW_FILE} unreadable ({e})"))
                }
            }
        });
        match cached {
            Err(msg) => {
                if !raw_missing_reported {
                    r.errors.push(format!("chunk {seq}: {msg}"));
                    raw_missing_reported = true;
                }
            }
            Ok(rh) => {
                let Some(end) = offset.checked_add(len) else {
                    r.errors.push(format!("chunk {seq}: raw slice offset overflow"));
                    continue;
                };
                if end > rh.len {
                    r.errors.push(format!("chunk {seq}: {RAW_FILE} truncated (slice {offset}..{end} exceeds file length {})", rh.len));
                    continue;
                }
                let Ok(len_usize) = usize::try_from(len) else {
                    r.errors.push(format!("chunk {seq}: raw slice length {len} exceeds addressable memory"));
                    continue;
                };
                r.raw_slices += 1;
                let mut slice = vec![0u8; len_usize];
                if len_usize > 0 {
                    if let Err(e) = rh.file.seek(SeekFrom::Start(offset)).and_then(|_| rh.file.read_exact(&mut slice)) {
                        r.errors.push(format!("chunk {seq}: raw file {RAW_FILE} unreadable ({e})"));
                        continue;
                    }
                }
                if ev::sha256_hex(&slice) != want_sha {
                    r.errors.push(format!("chunk {seq}: raw slice SHA-256 does not match"));
                }
                if len > 0 {
                    let mut shas = HashSet::new();
                    let mut t_backwards_reported = false;
                    for (idx, c) in slice.chunks_exact(RAW_RECORD_BYTES).enumerate() {
                        let rec_t_us = u64::from_le_bytes(c[8..16].try_into().unwrap());
                        if !t_backwards_reported {
                            if let Some(prev_t) = last_raw_t_us {
                                if rec_t_us < prev_t {
                                    r.errors.push(format!(
                                        "chunk {seq}: raw record {idx} t_us {rec_t_us} decreases below previous {prev_t}"
                                    ));
                                    t_backwards_reported = true;
                                }
                            }
                        }
                        last_raw_t_us = Some(rec_t_us);
                        shas.insert(ev::sha256_hex(&c[16..RAW_RECORD_BYTES]));
                    }
                    for rec in records.iter().filter(|x| x["type"] == "in") {
                        if !rec["raw_sha256"].as_str().is_some_and(|s| shas.contains(s)) {
                            r.errors.push(format!("chunk {seq}: an in row's raw_sha256 is not in the raw slice"));
                            break;
                        }
                    }
                }
            }
        }
    }
    r.file_sha256 = ev::hex(&file_hasher.finalize());
    if raw_active_seen {
        if let Some(Ok(rh)) = &raw_handle {
            r.raw_files = 1;
            if rh.len > expected_raw_offset {
                r.warnings.push(format!("{RAW_FILE}: {} trailing byte(s) after sealed offset {expected_raw_offset} (UNMEASURED unsealed tail, not counted, never padded)", rh.len - expected_raw_offset));
            }
        }
    } else if raw_off_only && raw_path.exists() {
        r.warnings.push(format!("{RAW_FILE} exists although raw_usb is false"));
    }
    if !pending.is_empty() { r.warnings.push(format!("{} record(s) after the last chunk are an unsealed tail (not counted)", pending.len())); }
    if let Ok(s) = std::fs::read_to_string(hand_dir.join(SUMMARY_FILE)) {
        match serde_json::from_str::<Value>(&s) {
            Ok(sv) => {
                if sv["sha256"].as_str() != Some(r.file_sha256.as_str()) { r.errors.push(format!("{SUMMARY_FILE}: sha256 does not match {FILE_NAME}")); }
                if sv["head"].as_str() != Some(r.head.as_str()) { r.errors.push(format!("{SUMMARY_FILE}: head does not match the last chunk")); }
            }
            Err(e) => r.errors.push(format!("{SUMMARY_FILE}: not JSON ({e})")),
        }
    }
    // Checkpoint bindings (checkpoints.jsonl lives in the bundle directory `dir`).
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
                if let (Some(cp_dir), Some(hdr_dir)) = (q["hand_timeline_dir_actual"].as_str(), header_hand_dir.as_deref()) {
                    if cp_dir != hdr_dir { r.errors.push(format!("checkpoint {}: hand_timeline_dir_actual differs from sealed header", cp["seq"])); }
                }
                if let (Some(cp_src), Some(hdr_src)) = (q["hand_dir_source"].as_str(), header_source.as_deref()) {
                    if cp_src != hdr_src { r.errors.push(format!("checkpoint {}: hand_dir_source differs from sealed header", cp["seq"])); }
                }
                if !q["hand_dir_fallback_reason"].is_null() && q["hand_dir_fallback_reason"] != header_reason {
                    r.errors.push(format!("checkpoint {}: hand_dir_fallback_reason differs from sealed header", cp["seq"]));
                }
                if q.get("hand_raw_file").is_some() && !header_raw_file.is_null() && q["hand_raw_file"] != header_raw_file {
                    r.errors.push(format!("checkpoint {}: hand_raw_file differs from sealed header", cp["seq"]));
                }
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
    let dir_note = r.hand_dir.as_ref().map(|d| format!(" dir={}", crate::bundle::escape_output(&d.to_string_lossy()))).unwrap_or_default();
    let src_note = r.hand_dir_source.as_deref().map(|s| format!(" source={}", crate::bundle::escape_output(s))).unwrap_or_default();
    let mut out = vec![format!("hand timeline: {} chunks={} head={} file_sha256={} raw_files={} raw_slices={} segment_bindings={} stop_bound={}{}{}{}",
        if r.pass() { "PASS" } else { "FAIL" }, r.chunks, r.head, r.file_sha256, r.raw_files, r.raw_slices, r.segment_bindings, r.stop_bound,
        if r.fallback { " device=dualsense (fallback)" } else { "" }, dir_note, src_note)];
    if let Some(ref why) = r.hand_dir_fallback_reason {
        out.push(format!("  hand_dir_fallback: {}", crate::bundle::escape_output(why)));
    }
    out.extend(r.errors.iter().map(|e| format!("  error: {}", crate::bundle::escape_output(e))));
    out.extend(r.warnings.iter().map(|w| format!("  warning: {}", crate::bundle::escape_output(w))));
    (out, if r.pass() { 0 } else { 1 })
}
