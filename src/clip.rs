//! `qorcap clip export` and `qorcap clip-verify`.
//!
//! A clip is a trim of one video file in a match bundle. Content Credentials are optional and
//! default off. When they are on, the manifest's `org.qorcap.match-receipt-v0` assertion stores
//! hashes and ids only, and `verified` is true only when QorTime's verifier returns VERIFIED and
//! `verify-bundle` is clean. A FAIL receipt is not signed. This module does not download QorTime
//! and does not add fields to `receipt.json`.

use crate::align::FPS;
use crate::bundle;
use crate::credentials::{self, MatchReceipt, SignRequest};
use crate::evidence;
use crate::inputproof;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

/// Same pin as `sidecar/qorcap_match.py`. Used only to find a checkout that was already installed.
pub const QORTIME_PIN: &str = "0204e6d8082f8c40ea1fe5e09bf16402aaee3ff2";

#[derive(Debug)]
pub enum ExportError {
    /// A recorder is still writing this bundle. Exit 3.
    Recording(String),
    /// Credentials were requested and the receipt or the bundle does not verify. Exit 4.
    Refuse(String),
    /// Anything else. Exit 2.
    Other(String),
}

impl std::fmt::Display for ExportError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Recording(s) | Self::Refuse(s) | Self::Other(s) => f.write_str(s),
        }
    }
}

impl ExportError {
    pub fn code(&self) -> i32 {
        match self {
            Self::Recording(_) => 3,
            Self::Refuse(_) => 4,
            Self::Other(_) => 2,
        }
    }
}

#[derive(Debug)]
pub struct ExportRequest {
    pub bundle: PathBuf,
    pub start_mono_ms: u64,
    pub end_mono_ms: u64,
    pub out: PathBuf,
    pub ffmpeg: String,
    pub credentials: bool,
    pub qortime: Option<PathBuf>,
    pub python: Option<String>,
    pub key_dir: Option<PathBuf>,
}

#[derive(Debug)]
pub struct ExportDone {
    pub out: PathBuf,
    pub credentials: bool,
    pub transcoded: bool,
    pub assertion: Option<MatchReceipt>,
}

/// What the existing verifiers said. `sign` is false for FAIL, an unknown status, or a broken bundle.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Verdict {
    pub verification_status: &'static str,
    pub verifier_status: String,
    pub verified: bool,
    pub sign: bool,
}

/// Map QorTime's status and verify-bundle onto the assertion. Never upgrades a status.
pub fn map_verdict(qt_status: &str, bundle_ok: bool) -> Verdict {
    let known = matches!(qt_status, "VERIFIED" | "SELF_ATTESTED" | "PARTIAL" | "FAIL");
    if !bundle_ok || qt_status == "FAIL" || !known {
        return Verdict {
            verification_status: "fail",
            verifier_status: if known { qt_status.to_string() } else { "FAIL".into() },
            verified: false,
            sign: false,
        };
    }
    match qt_status {
        "VERIFIED" => Verdict { verification_status: "verified", verifier_status: qt_status.into(), verified: true, sign: true },
        "SELF_ATTESTED" => Verdict { verification_status: "self_attested", verifier_status: qt_status.into(), verified: false, sign: true },
        "PARTIAL" => Verdict { verification_status: "partial", verifier_status: qt_status.into(), verified: false, sign: true },
        _ => Verdict { verification_status: "fail", verifier_status: "FAIL".into(), verified: false, sign: false },
    }
}

#[derive(Debug, Clone)]
struct Seg {
    seq: u64,
    kind: String,
    file: String,
    offset: u64,
    len: u64,
    sha256: String,
    mono_ms: u64,
    frames: i64,
}

#[derive(Debug, Clone)]
pub struct Span {
    pub start_mono_ms: u64,
    pub end_mono_ms: u64,
    pub start_frame: i64,
    pub end_frame: i64,
    pub start_sec: f64,
    pub dur_sec: f64,
    pub video_span_sha256: String,
    pub video_segment_seqs: Vec<u64>,
    pub video_chain_head: String,
    pub input_span_sha256: String,
    pub source: PathBuf,
    pub bundle_ok: bool,
}

pub fn export(req: &ExportRequest) -> Result<ExportDone, ExportError> {
    if inputproof::recorder_running(&req.bundle) {
        return Err(ExportError::Recording(format!(
            "QorCap is still recording into {}. Clips are cut only after the match stops.",
            req.bundle.display()
        )));
    }
    if req.end_mono_ms <= req.start_mono_ms {
        return Err(ExportError::Other("end-mono-ms must be after start-mono-ms".into()));
    }
    let span = load_span(&req.bundle, req.start_mono_ms, req.end_mono_ms).map_err(ExportError::Other)?;
    let prepared = if req.credentials {
        let judge = judge_bundle(&req.bundle, req.qortime.as_deref(), req.python.as_deref()).map_err(|e| {
            if e.starts_with("refusing") { ExportError::Refuse(e) } else { ExportError::Refuse(format!("refusing to attach content credentials: {e}")) }
        })?;
        if !span.bundle_ok {
            let _ = eprint_bundle(&req.bundle);
            return Err(ExportError::Refuse("refusing to attach content credentials: the match bundle did not verify".into()));
        }
        if !judge.sign {
            return Err(ExportError::Refuse("refusing to attach content credentials: receipt did not verify".into()));
        }
        if judge.chain_head != span.video_chain_head {
            return Err(ExportError::Refuse("refusing to attach content credentials: the match bundle did not verify".into()));
        }
        Some(judge)
    } else {
        None
    };

    let tmp = std::env::temp_dir().join(format!(
        "qorcap-clip-{}-{}.mp4",
        std::process::id(),
        std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0)
    ));
    let transcoded = cut_span(&req.ffmpeg, &span.source, span.start_sec, span.dur_sec, &tmp).map_err(|e| {
        let _ = std::fs::remove_file(&tmp);
        ExportError::Other(e)
    })?;
    let plain = std::fs::read(&tmp).map_err(|e| {
        let _ = std::fs::remove_file(&tmp);
        ExportError::Other(format!("read cut: {e}"))
    })?;
    let _ = std::fs::remove_file(&tmp);

    let (bytes, assertion) = if let Some(judge) = prepared {
        let assertion = receipt_assertion(&span, &judge, transcoded).map_err(ExportError::Other)?;
        let key_dir = req.key_dir.clone().unwrap_or_else(credentials::default_key_dir);
        let signed = credentials::apply_credentials(&plain, Some(SignRequest {
            assertion: &assertion,
            key_dir: &key_dir,
            transcoded,
        })).map_err(|e| {
            if e.starts_with("refusing") { ExportError::Refuse(e) } else { ExportError::Other(e) }
        })?;
        (signed, Some(assertion))
    } else {
        // Flag off: the file is the ffmpeg cut, with no manifest and no extra bytes.
        (plain, None)
    };
    write_atomic(&req.out, &bytes).map_err(ExportError::Other)?;
    Ok(ExportDone { out: req.out.clone(), credentials: req.credentials, transcoded, assertion })
}

/// Sign `plain` against a bundle, or return it unchanged when `credentials` is false.
/// Tests use this so a byte-identity check does not depend on two ffmpeg runs.
pub fn seal_plain(
    bundle: &Path,
    start_mono_ms: u64,
    end_mono_ms: u64,
    plain: &[u8],
    credentials: bool,
    qortime: Option<&Path>,
    python: Option<&str>,
    key_dir: Option<&Path>,
) -> Result<(Vec<u8>, Option<MatchReceipt>), ExportError> {
    let span = load_span(bundle, start_mono_ms, end_mono_ms).map_err(ExportError::Other)?;
    if !credentials {
        return Ok((plain.to_vec(), None));
    }
    let judge = judge_bundle(bundle, qortime, python).map_err(|e| {
        if e.starts_with("refusing") { ExportError::Refuse(e) } else { ExportError::Refuse(format!("refusing to attach content credentials: {e}")) }
    })?;
    if !span.bundle_ok || judge.chain_head != span.video_chain_head {
        return Err(ExportError::Refuse("refusing to attach content credentials: the match bundle did not verify".into()));
    }
    if !judge.sign {
        return Err(ExportError::Refuse("refusing to attach content credentials: receipt did not verify".into()));
    }
    let assertion = receipt_assertion(&span, &judge, false).map_err(ExportError::Other)?;
    let dir = key_dir.map(Path::to_path_buf).unwrap_or_else(credentials::default_key_dir);
    let signed = credentials::apply_credentials(plain, Some(SignRequest { assertion: &assertion, key_dir: &dir, transcoded: false }))
        .map_err(ExportError::Other)?;
    Ok((signed, Some(assertion)))
}

fn receipt_assertion(span: &Span, judge: &Judge, transcoded: bool) -> Result<MatchReceipt, String> {
    let a = MatchReceipt {
        schema: credentials::SCHEMA.into(),
        receipt_sha256: judge.receipt_sha256.clone(),
        match_id: judge.match_id.clone(),
        start_mono_ms: span.start_mono_ms,
        end_mono_ms: span.end_mono_ms,
        start_frame: span.start_frame,
        end_frame: span.end_frame,
        video_span_sha256: span.video_span_sha256.clone(),
        video_segment_seqs: span.video_segment_seqs.clone(),
        video_chain_head: span.video_chain_head.clone(),
        input_span_sha256: span.input_span_sha256.clone(),
        qorcap_version: env!("CARGO_PKG_VERSION").into(),
        verification_status: judge.verification_status.to_string(),
        verifier_status: judge.verifier_status.clone(),
        verified: judge.verified,
        edit: if transcoded { "trimmed+transcoded" } else { "trimmed+repackaged" }.into(),
    };
    a.consistent()?;
    if a.verification_status == "fail" || !judge.sign {
        return Err("refusing to attach content credentials: receipt did not verify".into());
    }
    Ok(a)
}

#[derive(Debug)]
struct Judge {
    verification_status: &'static str,
    verifier_status: String,
    verified: bool,
    sign: bool,
    receipt_sha256: String,
    match_id: String,
    chain_head: String,
}

fn judge_bundle(dir: &Path, qortime: Option<&Path>, python: Option<&str>) -> Result<Judge, String> {
    let receipt_path = dir.join("receipt.json");
    if !receipt_path.is_file() {
        return Err("receipt.json is required to attach content credentials".into());
    }
    let receipt_sha256 = credentials::file_sha256(&receipt_path)?;
    let raw = std::fs::read(&receipt_path).map_err(|e| format!("read receipt: {e}"))?;
    let receipt: Value = serde_json::from_slice(&raw).map_err(|_| "receipt.json is not JSON".to_string())?;
    let match_id = receipt["match"]["match_id"].as_str().unwrap_or("").to_string();
    if match_id.is_empty() {
        return Err("receipt has no match id".into());
    }
    let pub_hex = referee_pub(dir, &receipt)?;
    let qt = find_qortime(qortime)?;
    let verdict_json = run_verifier(&qt, &receipt_path, &pub_hex, python)?;
    let qt_status = verdict_json["status"].as_str().unwrap_or("");
    if qt_status.is_empty() {
        return Err("QorTime verifier returned no status".into());
    }
    let report = bundle::verify(dir)?;
    let bundle_ok = report.errors.is_empty();
    if !bundle_ok {
        eprint_bundle_errors(&report.errors);
    }
    let safe = safe_error_tokens(&verdict_json);
    if !safe.is_empty() {
        eprintln!("qor_receipt verify: {qt_status}{safe}");
    }
    let v = map_verdict(qt_status, bundle_ok);
    Ok(Judge {
        verification_status: v.verification_status,
        verifier_status: v.verifier_status,
        verified: v.verified,
        sign: v.sign,
        receipt_sha256,
        match_id,
        chain_head: report.chain_head,
    })
}

fn eprint_bundle(dir: &Path) -> Result<(), String> {
    let report = bundle::verify(dir)?;
    eprint_bundle_errors(&report.errors);
    Ok(())
}

fn eprint_bundle_errors(errors: &[String]) {
    for e in errors {
        // verify-bundle messages can contain file names. They stay on stderr, never in the manifest.
        eprintln!("verify-bundle: {e}");
    }
}

fn safe_error_tokens(v: &Value) -> String {
    let Some(arr) = v.get("errors").and_then(|e| e.as_array()) else { return String::new() };
    let mut out = Vec::new();
    for e in arr {
        let Some(s) = e.as_str() else { continue };
        if s.len() < 80 && !s.contains('/') && !s.contains('\\') && !s.contains(':') {
            out.push(s);
        }
    }
    if out.is_empty() { String::new() } else { format!(" ({})", out.join(", ")) }
}

fn referee_pub(dir: &Path, receipt: &Value) -> Result<String, String> {
    let p = dir.join("referee.pub");
    if p.is_file() {
        let s = std::fs::read_to_string(&p).map_err(|_| "could not read referee.pub".to_string())?;
        let h = s.trim();
        if is_hex64(h) {
            return Ok(h.to_string());
        }
        if !h.is_empty() {
            return Err("referee.pub is not a 64-character hex key".into());
        }
    }
    let h = receipt["referee"]["pubkey"].as_str().unwrap_or("");
    if is_hex64(h) {
        return Ok(h.to_string());
    }
    Err("receipt has no referee public key".into())
}

fn is_hex64(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|c| c.is_ascii_hexdigit())
}

pub fn find_qortime(explicit: Option<&Path>) -> Result<PathBuf, String> {
    let mut cands: Vec<PathBuf> = Vec::new();
    if let Some(p) = explicit {
        cands.push(p.to_path_buf());
    }
    if let Ok(e) = std::env::var("QORTIME") {
        if !e.is_empty() {
            cands.push(PathBuf::from(e));
        }
    }
    if let Ok(me) = std::env::current_exe() {
        if let Some(dir) = me.parent() {
            cands.push(dir.join("qortime"));
            cands.push(dir.join("..").join("qortime"));
            cands.push(dir.join("..").join("..").join("qortime"));
        }
    }
    cands.push(PathBuf::from("qortime"));
    cands.push(data_dir().join(format!("qortime-{QORTIME_PIN}")));
    for c in &cands {
        if c.join("qor_receipt").join("receipt.py").is_file() {
            return Ok(c.clone());
        }
    }
    Err("QorTime verifier not found. Pass --qortime DIR (a checkout that contains qor_receipt). Clip credentials do not download it.".into())
}

fn data_dir() -> PathBuf {
    if let Ok(h) = std::env::var("QORCAP_HOME") {
        return PathBuf::from(h);
    }
    if cfg!(windows) {
        if let Ok(d) = std::env::var("LOCALAPPDATA") {
            return PathBuf::from(d).join("QorCap");
        }
    }
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".into());
    PathBuf::from(home).join(".qorcap")
}

fn run_verifier(qortime: &Path, receipt: &Path, referee_pub: &str, python: Option<&str>) -> Result<Value, String> {
    let mut last = String::from("Python not found");
    for c in python_candidates(python) {
        let mut cmd = Command::new(&c[0]);
        hide_window(&mut cmd);
        cmd.args(&c[1..]);
        cmd.arg("-m").arg("qor_receipt.cli").arg("verify");
        cmd.arg(receipt);
        cmd.arg("--referee-pub").arg(referee_pub);
        cmd.env("PYTHONPATH", pythonpath(qortime));
        cmd.stdin(Stdio::null());
        match cmd.output() {
            Err(e) => last = e.to_string(),
            Ok(o) => {
                let stdout = String::from_utf8_lossy(&o.stdout);
                if let Some(v) = parse_json_object(&stdout) {
                    return Ok(v);
                }
                let err = String::from_utf8_lossy(&o.stderr);
                let tail: String = err.chars().filter(|c| *c != '\n').take(180).collect();
                if !tail.is_empty() {
                    eprintln!("qor_receipt verify: {tail}");
                }
                return Err(format!("QorTime verifier could not be run (exit {})", o.status.code().unwrap_or(-1)));
            }
        }
    }
    Err(format!("QorTime verifier could not be run ({last})"))
}

fn pythonpath(qortime: &Path) -> String {
    let sep = if cfg!(windows) { ";" } else { ":" };
    match std::env::var("PYTHONPATH") {
        Ok(p) if !p.is_empty() => format!("{}{sep}{p}", qortime.display()),
        _ => qortime.display().to_string(),
    }
}

fn python_candidates(explicit: Option<&str>) -> Vec<Vec<String>> {
    let mut cands: Vec<Vec<String>> = Vec::new();
    if let Some(p) = explicit.map(str::to_string).or_else(|| std::env::var("QORCAP_PYTHON").ok()) {
        if !p.is_empty() {
            cands.push(vec![p]);
        }
    }
    let home = data_dir();
    let v = if cfg!(windows) { home.join("venv").join("Scripts").join("python.exe") } else { home.join("venv").join("bin").join("python") };
    if v.exists() {
        cands.push(vec![v.to_string_lossy().into_owned()]);
    }
    if cfg!(windows) {
        cands.push(vec!["py".into(), "-3".into()]);
        cands.push(vec!["python".into()]);
    } else {
        cands.push(vec!["python3".into()]);
        cands.push(vec!["python".into()]);
    }
    cands
}

fn hide_window(cmd: &mut Command) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000);
    }
    let _ = cmd;
}

fn parse_json_object(s: &str) -> Option<Value> {
    let i = s.find('{')?;
    serde_json::from_str(&s[i..]).ok()
}

fn load_span(dir: &Path, start: u64, end: u64) -> Result<Span, String> {
    if end <= start {
        return Err("end-mono-ms must be after start-mono-ms".into());
    }
    let segs = read_segments(&dir.join("manifest.jsonl"))?;
    let chosen = overlapping(&segs, start, end)?;
    let file = &chosen[0].file;
    if chosen.iter().any(|s| &s.file != file) {
        return Err("clip range covers more than one video file".into());
    }
    plain_file_name(file)?;
    let source = dir.join(file);
    let parts: Vec<(PathBuf, u64, u64, String)> = chosen.iter().map(|s| {
        (source.clone(), s.offset, s.len, s.sha256.clone())
    }).collect();
    let video_span_sha256 = sha256_concat(&parts)?;
    let (start_frame, end_frame) = frames_for(&segs, start, end)?;
    let (start_sec, dur_sec) = cut_window(start_frame, end_frame, start, end);
    if dur_sec <= 0.0 {
        return Err("clip duration is zero".into());
    }
    let report = bundle::verify(dir)?;
    let input_span_sha256 = input_span_hash(dir, start, end)?;
    Ok(Span {
        start_mono_ms: start,
        end_mono_ms: end,
        start_frame,
        end_frame,
        start_sec,
        dur_sec,
        video_span_sha256,
        video_segment_seqs: chosen.iter().map(|s| s.seq).collect(),
        video_chain_head: report.chain_head,
        input_span_sha256,
        source,
        bundle_ok: report.errors.is_empty(),
    })
}

fn plain_file_name(file: &str) -> Result<(), String> {
    if file.is_empty() || file.contains('/') || file.contains('\\') || file.contains("..") {
        return Err("manifest segment file name is not a single file in the bundle".into());
    }
    Ok(())
}

fn read_segments(path: &Path) -> Result<Vec<Seg>, String> {
    let s = std::fs::read_to_string(path).map_err(|_| "manifest.jsonl is missing".to_string())?;
    let mut out = Vec::new();
    for (i, line) in s.lines().filter(|l| !l.trim().is_empty()).enumerate() {
        let v: Value = serde_json::from_str(line).map_err(|_| format!("manifest.jsonl line {} is not JSON", i + 1))?;
        if v["type"] != "segment" {
            continue;
        }
        let file = v["file"].as_str().unwrap_or("").to_string();
        plain_file_name(&file)?;
        out.push(Seg {
            seq: v["seq"].as_u64().ok_or_else(|| format!("manifest segment on line {} has no seq", i + 1))?,
            kind: v["kind"].as_str().unwrap_or("media").to_string(),
            file,
            offset: v["offset"].as_u64().unwrap_or(0),
            len: v["bytes"].as_u64().ok_or_else(|| format!("manifest segment on line {} has no length", i + 1))?,
            sha256: v["sha256"].as_str().unwrap_or("").to_string(),
            mono_ms: v["mono_ms"].as_u64().unwrap_or(0),
            frames: v["frames"].as_i64().unwrap_or(0),
        });
    }
    if out.is_empty() {
        return Err("manifest.jsonl has no video segments".into());
    }
    Ok(out)
}

fn media<'a>(segs: &'a [Seg]) -> Vec<&'a Seg> {
    segs.iter().filter(|s| s.kind != "init").collect()
}

/// Media segments whose `(previous clock, this clock]` overlaps `[start, end)`.
fn overlapping(segs: &[Seg], start: u64, end: u64) -> Result<Vec<Seg>, String> {
    let media = media(segs);
    if media.is_empty() {
        return Err("manifest.jsonl has no media segments".into());
    }
    let anchor = anchor(segs, media[0]);
    let mut chosen = Vec::new();
    for (i, m) in media.iter().enumerate() {
        let prev_t = if i == 0 { anchor.0 } else { media[i - 1].mono_ms };
        let this_t = m.mono_ms;
        if prev_t < end && start < this_t {
            chosen.push((*m).clone());
        }
    }
    if chosen.is_empty() {
        return Err("no video segment overlaps that match-clock range".into());
    }
    let file = &chosen[0].file;
    if chosen.iter().any(|s| &s.file != file) {
        return Err("clip range covers more than one video file".into());
    }
    Ok(chosen)
}

fn anchor(segs: &[Seg], first_media: &Seg) -> (u64, i64) {
    let init = segs.iter().rev().find(|s| s.kind == "init" && s.seq < first_media.seq);
    if let Some(init) = init {
        if init.mono_ms <= first_media.mono_ms {
            return (init.mono_ms, init.frames);
        }
    }
    if first_media.frames > 0 && first_media.mono_ms > 0 {
        (0, 0)
    } else {
        (first_media.mono_ms, first_media.frames)
    }
}

fn frames_for(segs: &[Seg], start: u64, end: u64) -> Result<(i64, i64), String> {
    let media = media(segs);
    if media.is_empty() {
        return Err("manifest.jsonl has no media segments".into());
    }
    let mut points = vec![anchor(segs, media[0])];
    for m in &media {
        points.push((m.mono_ms, m.frames));
    }
    Ok((frame_at(&points, start), frame_at(&points, end)))
}

fn frame_at(points: &[(u64, i64)], t: u64) -> i64 {
    if points.is_empty() {
        return 0;
    }
    if t <= points[0].0 {
        return points[0].1;
    }
    for w in points.windows(2) {
        let (t0, f0) = w[0];
        let (t1, f1) = w[1];
        if t <= t1 {
            if t1 == t0 {
                return f1;
            }
            let num = (t - t0) as i128 * (f1 as i128 - f0 as i128);
            let den = (t1 - t0) as i128;
            return f0 + (num / den) as i64;
        }
    }
    points[points.len() - 1].1
}

fn cut_window(start_frame: i64, end_frame: i64, start_ms: u64, end_ms: u64) -> (f64, f64) {
    let by_frames = (end_frame - start_frame) as f64 / FPS;
    if by_frames > 0.0 {
        (start_frame.max(0) as f64 / FPS, by_frames)
    } else {
        (start_ms as f64 / 1000.0, end_ms.saturating_sub(start_ms) as f64 / 1000.0)
    }
}

fn sha256_concat(parts: &[(PathBuf, u64, u64, String)]) -> Result<String, String> {
    let mut all = Sha256::new();
    let mut buf = [0u8; 1 << 16];
    for (path, off, len, expect) in parts {
        let mut f = std::fs::File::open(path).map_err(|_| "video segment could not be read".to_string())?;
        f.seek(SeekFrom::Start(*off)).map_err(|_| "video segment could not be read".to_string())?;
        let mut part = Sha256::new();
        let mut left = *len;
        while left > 0 {
            let want = std::cmp::min(buf.len() as u64, left) as usize;
            let n = f.read(&mut buf[..want]).map_err(|_| "video segment could not be read".to_string())?;
            if n == 0 {
                return Err("video segment ended early".into());
            }
            all.update(&buf[..n]);
            part.update(&buf[..n]);
            left -= n as u64;
        }
        let got = evidence::hex(part.finalize().as_slice());
        if got != *expect {
            return Err("segment hash does not match the video bytes".into());
        }
    }
    Ok(evidence::hex(all.finalize().as_slice()))
}

fn input_span_hash(dir: &Path, start_ms: u64, end_ms: u64) -> Result<String, String> {
    let start_us = start_ms.checked_mul(1_000).ok_or("match clock is out of range")?;
    let end_us = end_ms.checked_mul(1_000).ok_or("match clock is out of range")?;
    let path = dir.join("inputs.jsonl");
    let records = if path.is_file() {
        let (recs, _) = crate::inputs::read_file(&path)?;
        let mut kept = Vec::new();
        for r in recs {
            if let Some(t) = r["t_us"].as_u64() {
                if t >= start_us && t < end_us {
                    kept.push(r);
                }
            }
        }
        Value::Array(kept)
    } else {
        json!([])
    };
    evidence::try_jcs_sha(&records).map_err(|_| "input span is not JCS-safe".to_string())
}

fn cut_span(ffmpeg: &str, src: &Path, start_sec: f64, dur_sec: f64, dest: &Path) -> Result<bool, String> {
    if dur_sec <= 0.0 {
        return Err("clip duration is zero".into());
    }
    if let Some(parent) = dest.parent() {
        std::fs::create_dir_all(parent).map_err(|e| format!("create cut dir: {e}"))?;
    }
    let start = format!("{start_sec:.3}");
    let dur = format!("{dur_sec:.3}");
    let src_s = src.display().to_string();
    let dest_s = dest.display().to_string();
    let copy = vec![
        "-hide_banner", "-loglevel", "error", "-y", "-ss", &start, "-i", &src_s, "-t", &dur,
        "-c", "copy", "-movflags", "+faststart", &dest_s,
    ];
    if ff_ok(ffmpeg, &copy) && nonempty(dest) {
        return Ok(false);
    }
    let _ = std::fs::remove_file(dest);
    let tr = vec![
        "-hide_banner", "-loglevel", "error", "-y", "-ss", &start, "-i", &src_s, "-t", &dur,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", &dest_s,
    ];
    if ff_ok(ffmpeg, &tr) && nonempty(dest) {
        return Ok(true);
    }
    let _ = std::fs::remove_file(dest);
    let silent = vec![
        "-hide_banner", "-loglevel", "error", "-y", "-ss", &start, "-i", &src_s, "-t", &dur,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-an", "-movflags", "+faststart", &dest_s,
    ];
    if ff_ok(ffmpeg, &silent) && nonempty(dest) {
        return Ok(true);
    }
    Err("ffmpeg could not cut the clip".into())
}

fn ff_ok(ffmpeg: &str, args: &[&str]) -> bool {
    crate::ffmpeg::command(ffmpeg).args(args).stdin(Stdio::null()).output().map(|o| o.status.success()).unwrap_or(false)
}

fn nonempty(path: &Path) -> bool {
    std::fs::metadata(path).map(|m| m.len() > 0).unwrap_or(false)
}

fn write_atomic(dest: &Path, bytes: &[u8]) -> Result<(), String> {
    if let Some(parent) = dest.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent).map_err(|e| format!("create {}: {e}", parent.display()))?;
        }
    }
    let tmp = dest.with_extension("partial");
    {
        let mut f = std::fs::File::create(&tmp).map_err(|e| format!("create {}: {e}", tmp.display()))?;
        f.write_all(bytes).map_err(|e| format!("write {}: {e}", tmp.display()))?;
        f.sync_all().map_err(|e| format!("sync {}: {e}", tmp.display()))?;
    }
    if dest.exists() {
        std::fs::remove_file(dest).map_err(|e| format!("replace {}: {e}", dest.display()))?;
    }
    std::fs::rename(&tmp, dest).map_err(|e| format!("rename into {}: {e}", dest.display()))?;
    Ok(())
}

#[derive(Debug)]
pub struct ClipReport {
    pub pass: bool,
    pub text: String,
}

/// Read the manifest and, when `bundle` is set, recompute every hash against that bundle.
/// A missing clip path is an error (exit 2). A clip that fails the check is `pass: false` (exit 1).
pub fn verify_clip(clip: &Path, bundle: Option<&Path>, qortime: Option<&Path>, python: Option<&str>) -> Result<ClipReport, String> {
    if !clip.is_file() {
        return Err(format!("cannot open {}", clip.display()));
    }
    let bytes = std::fs::read(clip).map_err(|e| format!("cannot open {}: {e}", clip.display()))?;
    let read = match credentials::read_mp4(&bytes) {
        Ok(r) => r,
        Err(e) => {
            return Ok(ClipReport { pass: false, text: fail_text(&e) });
        }
    };
    let mut reasons: Vec<String> = Vec::new();
    if !read.signature_ok {
        reasons.push("c2pa signature does not validate".into());
    }
    if let Some(e) = &read.assertion_error {
        reasons.push(e.clone());
    }
    let assertion = read.assertion.as_ref();
    if assertion.is_none() && read.assertion_error.is_none() {
        reasons.push(format!("manifest has no usable {} assertion", credentials::LABEL));
    }

    let mut receipt_line = "not checked".to_string();
    let mut video_line = "not checked".to_string();
    let mut input_line = "not checked".to_string();
    let mut chain_line = "not checked".to_string();
    let mut status_line = assertion.map(|a| a.verification_status.clone()).unwrap_or_else(|| "absent".into());
    let mut verified_line = assertion.map(|a| a.verified.to_string()).unwrap_or_else(|| "absent".into());
    let match_line = assertion.map(|a| a.match_id.clone()).unwrap_or_else(|| "absent".into());

    if bundle.is_none() {
        reasons.push("hashes were not recomputed (pass --bundle)".into());
    } else if let (Some(dir), Some(a)) = (bundle, assertion) {
        match check_bundle(dir, a, qortime, python) {
            Ok(lines) => {
                receipt_line = lines.receipt;
                video_line = lines.video;
                input_line = lines.input;
                chain_line = lines.chain;
                reasons.extend(lines.reasons);
                if lines.fresh_status.is_some() {
                    status_line = a.verification_status.clone();
                }
                verified_line = a.verified.to_string();
            }
            Err(e) => reasons.push(e),
        }
    }

    let pass = reasons.is_empty() && read.signature_ok;
    let mut text = String::new();
    text.push_str(if pass { "RESULT: PASS\n" } else { "RESULT: FAIL\n" });
    text.push_str(&format!("c2pa_signature: {}\n", if read.signature_ok { "ok" } else { "fail" }));
    text.push_str("signer: self-signed local key (not on the C2PA trust list)\n");
    text.push_str(&format!("verification_status: {status_line}\n"));
    text.push_str(&format!("verified: {verified_line}\n"));
    let seq_line = assertion
        .map(|a| a.video_segment_seqs.iter().map(|n| n.to_string()).collect::<Vec<_>>().join(","))
        .unwrap_or_else(|| "absent".into());
    text.push_str(&format!("match_id: {match_line}\n"));
    text.push_str(&format!("video_segment_seqs: {seq_line}\n"));
    text.push_str(&format!("receipt_sha256: {receipt_line}\n"));
    text.push_str(&format!("video_span_sha256: {video_line}\n"));
    text.push_str(&format!("input_span_sha256: {input_line}\n"));
    text.push_str(&format!("video_chain_head: {chain_line}\n"));
    for r in &reasons {
        text.push_str(&format!("reason: {r}\n"));
    }
    if pass && verified_line != "true" {
        text.push_str("note: PASS means the clip's signature and the receipt binding check out. It does not mean the match is referee-VERIFIED. verified is true only when QorTime's verifier returns VERIFIED and verify-bundle is clean.\n");
    }
    Ok(ClipReport { pass, text })
}

struct BundleLines {
    receipt: String,
    video: String,
    input: String,
    chain: String,
    reasons: Vec<String>,
    fresh_status: Option<String>,
}

fn check_bundle(dir: &Path, a: &MatchReceipt, qortime: Option<&Path>, python: Option<&str>) -> Result<BundleLines, String> {
    let span = load_span(dir, a.start_mono_ms, a.end_mono_ms)?;
    let mut reasons = Vec::new();
    let receipt_path = dir.join("receipt.json");
    let receipt_sha = if receipt_path.is_file() {
        credentials::file_sha256(&receipt_path)?
    } else {
        reasons.push("receipt did not verify".into());
        String::new()
    };
    let receipt = cmp_hash(&receipt_sha, &a.receipt_sha256, "receipt hash does not match");
    if receipt == "mismatch" {
        reasons.push("receipt hash does not match".into());
    }
    let video = cmp_hash(&span.video_span_sha256, &a.video_span_sha256, "video span hash does not match");
    if video == "mismatch" {
        reasons.push("video span hash does not match".into());
    }
    let input = cmp_hash(&span.input_span_sha256, &a.input_span_sha256, "input span hash does not match");
    if input == "mismatch" {
        reasons.push("input span hash does not match".into());
    }
    let chain = cmp_hash(&span.video_chain_head, &a.video_chain_head, "video chain head does not match");
    if chain == "mismatch" {
        reasons.push("video chain head does not match".into());
    }
    if span.video_segment_seqs != a.video_segment_seqs {
        reasons.push("video segment sequence does not match".into());
    }

    let mut fresh_status = None;
    if receipt_path.is_file() {
        match judge_bundle(dir, qortime, python) {
            Ok(j) => {
                fresh_status = Some(j.verification_status.to_string());
                if j.match_id != a.match_id {
                    reasons.push("match id does not match the receipt".into());
                }
                if !j.sign || j.verification_status == "fail" {
                    reasons.push("receipt did not verify".into());
                }
                if j.verification_status != a.verification_status {
                    reasons.push("manifest verification_status does not match a fresh verifier run".into());
                }
                if a.verified && !j.verified {
                    reasons.push("manifest says verified but the receipt verifier does not".into());
                }
                if a.verifier_status == "VERIFIED" && j.verifier_status != "VERIFIED" {
                    reasons.push("manifest says verified but the receipt verifier does not".into());
                }
            }
            Err(e) => {
                eprintln!("clip-verify: {e}");
                reasons.push("receipt did not verify".into());
            }
        }
    }
    Ok(BundleLines { receipt, video, input, chain, reasons, fresh_status })
}

fn cmp_hash(got: &str, want: &str, _why: &str) -> String {
    if !got.is_empty() && got == want { "match".into() } else { "mismatch".into() }
}

fn fail_text(reason: &str) -> String {
    format!(
        "RESULT: FAIL\nc2pa_signature: fail\nsigner: self-signed local key (not on the C2PA trust list)\nverification_status: absent\nverified: absent\nmatch_id: absent\nvideo_segment_seqs: absent\nreceipt_sha256: not checked\nvideo_span_sha256: not checked\ninput_span_sha256: not checked\nvideo_chain_head: not checked\nreason: {reason}\n"
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::evidence::{CheckpointChain, SegmentChain};

    #[test]
    fn verdict_is_verified_only_when_both_checks_say_so() {
        let v = map_verdict("VERIFIED", true);
        assert!(v.verified && v.sign && v.verification_status == "verified");
        let s = map_verdict("SELF_ATTESTED", true);
        assert!(!s.verified && s.sign && s.verification_status == "self_attested");
        let p = map_verdict("PARTIAL", true);
        assert!(!p.verified && p.sign && p.verification_status == "partial");
        for (st, ok) in [("VERIFIED", false), ("FAIL", true), ("FAIL", false), ("NOPE", true), ("SELF_ATTESTED", false)] {
            let v = map_verdict(st, ok);
            assert!(!v.sign && !v.verified && v.verification_status == "fail", "{st} {ok}");
        }
    }

    #[test]
    fn span_overlap_uses_the_match_clock_and_rejects_a_second_file() {
        let segs = vec![
            seg(0, "init", "video.mp4", 0, 0),
            seg(1, "media", "video.mp4", 1000, 30),
            seg(2, "media", "video.mp4", 2000, 60),
            seg(3, "media", "other.mp4", 3000, 90),
        ];
        let one = overlapping(&segs, 0, 1000).unwrap();
        assert_eq!(one.iter().map(|s| s.seq).collect::<Vec<_>>(), vec![1]);
        let (a, b) = frames_for(&segs, 500, 1000).unwrap();
        assert_eq!((a, b), (15, 30));
        let both = overlapping(&segs, 0, 2000).unwrap();
        assert_eq!(both.len(), 2);
        let err = overlapping(&segs, 1500, 3000).unwrap_err();
        assert!(err.contains("more than one video file"), "{err}");
        let mut bad = segs.clone();
        bad[1].file = "../video.mp4".into();
        // overlapping itself does not check the name; the loader does
        assert!(plain_file_name("../video.mp4").is_err());
        assert!(plain_file_name("a/b.mp4").is_err());
        assert!(plain_file_name(r"a\b.mp4").is_err());
    }

    fn seg(seq: u64, kind: &str, file: &str, mono: u64, frames: i64) -> Seg {
        Seg { seq, kind: kind.into(), file: file.into(), offset: 0, len: 1, sha256: "ab".repeat(32), mono_ms: mono, frames }
    }

    #[test]
    fn flag_off_seal_matches_the_plain_cut_byte_for_byte() {
        let dir = temp("seal-off");
        write_video_bundle(&dir, include_bytes!("../tests/fixtures/tiny_clip.mp4"), "DEVICE-SENTINEL-9f3a", "GAMERTAG-SENTINEL-9f3a");
        let plain = include_bytes!("../tests/fixtures/tiny_clip.mp4");
        let (out, assertion) = seal_plain(&dir, 0, 1000, plain, false, None, None, None).unwrap();
        assert!(assertion.is_none());
        assert_eq!(out, plain);
        assert!(!out.windows(credentials::LABEL.len()).any(|w| w == credentials::LABEL.as_bytes()));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn credentialed_clip_round_trips_and_the_other_three_fail() {
        let Some(qt) = qortime_dir() else { return };
        let dir = temp("round");
        let video = include_bytes!("../tests/fixtures/tiny_clip.mp4");
        write_video_bundle(&dir, video, "DEVICE-SENTINEL-9f3a", "GAMERTAG-SENTINEL-9f3a");
        let minted = mint(&qt, "m-qorcap-clip-1", &dir.join("receipt.json"), false);
        assert_eq!(minted["status"], "SELF_ATTESTED", "{minted}");
        std::fs::write(dir.join("referee.pub"), format!("{}\n", minted["pubkey"].as_str().unwrap())).unwrap();
        let report = bundle::verify(&dir).unwrap();
        assert!(report.errors.is_empty(), "{:?}", report.errors);

        let key = temp("keys");
        let (signed, assertion) = seal_plain(&dir, 0, 1000, video, true, Some(&qt), None, Some(&key)).unwrap();
        let assertion = assertion.unwrap();
        assert_eq!(assertion.verification_status, "self_attested");
        assert!(!assertion.verified);
        assert_eq!(assertion.verifier_status, "SELF_ATTESTED");
        assert_eq!(assertion.match_id, "m-qorcap-clip-1");
        let signed_s = String::from_utf8_lossy(&signed);
        assert!(signed_s.contains(credentials::LABEL));
        assert!(signed_s.contains("m-qorcap-clip-1"));
        assert!(signed_s.contains("qorcap match video span"));
        for leak in ["local:player", "DEVICE-SENTINEL-9f3a", "GAMERTAG-SENTINEL-9f3a", "DEVICE-ID-SENTINEL-9f3a"] {
            assert!(!signed_s.contains(leak), "clip embeds {leak}");
        }
        let clip = dir.join("clip.mp4");
        std::fs::write(&clip, &signed).unwrap();
        let ok = verify_clip(&clip, Some(&dir), Some(&qt), None).unwrap();
        assert!(ok.pass, "{}", ok.text);
        assert!(ok.text.contains("RESULT: PASS"));
        assert!(ok.text.contains("verified: false"));
        assert!(ok.text.contains("verification_status: self_attested"));
        assert!(ok.text.contains("c2pa_signature: ok"));

        let bad = credentials::tamper_media_byte(&signed);
        let bad_path = dir.join("tampered.mp4");
        std::fs::write(&bad_path, &bad).unwrap();
        let tampered = verify_clip(&bad_path, Some(&dir), Some(&qt), None).unwrap();
        assert!(!tampered.pass, "{}", tampered.text);
        assert!(tampered.text.contains("c2pa signature does not validate") || tampered.text.contains("could not be read"), "{}", tampered.text);

        let other = temp("wrong");
        write_video_bundle(&other, &flip(video), "DEVICE-SENTINEL-9f3a", "GAMERTAG-SENTINEL-9f3a");
        let other_mint = mint(&qt, "m-other", &other.join("receipt.json"), false);
        std::fs::write(other.join("referee.pub"), format!("{}\n", other_mint["pubkey"].as_str().unwrap())).unwrap();
        let wrong = verify_clip(&clip, Some(&other), Some(&qt), None).unwrap();
        assert!(!wrong.pass, "{}", wrong.text);
        assert!(wrong.text.contains("does not match"), "{}", wrong.text);

        let broken_dir = temp("broken");
        write_video_bundle(&broken_dir, video, "DEVICE-SENTINEL-9f3a", "GAMERTAG-SENTINEL-9f3a");
        let broken = mint(&qt, "m-qorcap-clip-1", &broken_dir.join("receipt.json"), true);
        assert_eq!(broken["status"], "FAIL", "{broken}");
        std::fs::write(broken_dir.join("referee.pub"), format!("{}\n", broken["pubkey"].as_str().unwrap())).unwrap();
        let refused = seal_plain(&broken_dir, 0, 1000, video, true, Some(&qt), None, Some(&key));
        let msg = refused.err().map(|e| e.to_string()).unwrap_or_default();
        assert!(msg.contains("refusing to attach content credentials"), "{msg}");
        assert!(msg.contains("receipt did not verify"), "{msg}");
        assert!(!broken_dir.join("clip.mp4").exists());

        // A manifest that claims self_attested while the receipt file fails verification.
        let lie = lying_clip(&broken_dir, &key, video);
        let lie_path = broken_dir.join("lie.mp4");
        std::fs::write(&lie_path, &lie).unwrap();
        let unverified = verify_clip(&lie_path, Some(&broken_dir), Some(&qt), None).unwrap();
        assert!(!unverified.pass, "{}", unverified.text);
        assert!(unverified.text.contains("receipt did not verify"), "{}", unverified.text);

        let no_bundle = verify_clip(&clip, None, Some(&qt), None).unwrap();
        assert!(!no_bundle.pass, "{}", no_bundle.text);
        assert!(no_bundle.text.contains("hashes were not recomputed"), "{}", no_bundle.text);

        let _ = std::fs::remove_dir_all(&dir);
        let _ = std::fs::remove_dir_all(&other);
        let _ = std::fs::remove_dir_all(&broken_dir);
        let _ = std::fs::remove_dir_all(&key);
    }

    #[test]
    fn flag_off_export_file_has_no_credentials() {
        let Some(ff) = ffmpeg_bin() else {
            if std::env::var_os("CI").is_some() && !cfg!(windows) {
                panic!("ffmpeg is required on Linux CI");
            }
            return;
        };
        let dir = temp("export-off");
        write_video_bundle(&dir, include_bytes!("../tests/fixtures/tiny_clip.mp4"), "DEVICE-SENTINEL-9f3a", "GAMERTAG-SENTINEL-9f3a");
        let out = dir.join("out.mp4");
        let done = export(&ExportRequest {
            bundle: dir.clone(),
            start_mono_ms: 0,
            end_mono_ms: 1000,
            out: out.clone(),
            ffmpeg: ff,
            credentials: false,
            qortime: None,
            python: None,
            key_dir: None,
        }).unwrap();
        assert!(!done.credentials);
        let bytes = std::fs::read(&out).unwrap();
        assert!(!bytes.windows(credentials::LABEL.len()).any(|w| w == credentials::LABEL.as_bytes()));
        assert!(!bytes.windows(4).any(|w| w == b"c2pa"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    fn lying_clip(dir: &Path, key: &Path, video: &[u8]) -> Vec<u8> {
        let span = load_span(dir, 0, 1000).unwrap();
        let receipt_sha256 = credentials::file_sha256(&dir.join("receipt.json")).unwrap();
        let assertion = MatchReceipt {
            schema: credentials::SCHEMA.into(),
            receipt_sha256,
            match_id: "m-qorcap-clip-1".into(),
            start_mono_ms: 0,
            end_mono_ms: 1000,
            start_frame: span.start_frame,
            end_frame: span.end_frame,
            video_span_sha256: span.video_span_sha256,
            video_segment_seqs: span.video_segment_seqs,
            video_chain_head: span.video_chain_head,
            input_span_sha256: span.input_span_sha256,
            qorcap_version: env!("CARGO_PKG_VERSION").into(),
            verification_status: "self_attested".into(),
            verifier_status: "SELF_ATTESTED".into(),
            verified: false,
            edit: "trimmed+repackaged".into(),
        };
        credentials::sign_mp4(video, SignRequest { assertion: &assertion, key_dir: key, transcoded: false }).unwrap()
    }

    fn flip(video: &[u8]) -> Vec<u8> {
        let mut v = video.to_vec();
        let i = v.len() / 2;
        v[i] ^= 0x5a;
        v
    }

    fn write_video_bundle(dir: &Path, video: &[u8], device: &str, gamertag: &str) {
        let _ = std::fs::remove_dir_all(dir);
        std::fs::create_dir_all(dir).unwrap();
        std::fs::write(dir.join("video.mp4"), video).unwrap();
        let mut chain = SegmentChain::default();
        let (seq, sha, prev, head) = chain.push(video);
        let manifest = json!({
            "type": "segment", "seq": seq, "kind": "media", "file": "video.mp4",
            "offset": 0, "bytes": video.len(), "sha256": sha, "prev": prev, "chain_head": head,
            "mono_ms": 1000, "wall_ms": 1, "frames": 30,
            "device": device, "device_id": "DEVICE-ID-SENTINEL-9f3a"
        });
        std::fs::write(dir.join("manifest.jsonl"), format!("{manifest}\n")).unwrap();
        let rec = json!({"type": "in", "t_us": 500_000, "seg": 0, "detail": gamertag});
        let input_sha = evidence::jcs_sha(&json!([&rec]));
        let input_head = evidence::link(evidence::GENESIS, 0, &input_sha);
        let chunk = json!({"type": "chunk", "seg": 0, "n": 1, "sha256": &input_sha, "prev": evidence::GENESIS, "head": &input_head});
        std::fs::write(dir.join("inputs.jsonl"), format!("{rec}\n{chunk}\n")).unwrap();
        let mut cps = CheckpointChain::default();
        let ext = json!({"trigger": "segment", "chain_head": head, "input_seg": 0, "segment_seq": seq});
        let cp = cps.next_with_input(1000, ext, Some(input_sha)).unwrap();
        std::fs::write(dir.join("checkpoints.jsonl"), format!("{cp}\n")).unwrap();
    }

    fn mint(qortime: &Path, match_id: &str, out: &Path, broken: bool) -> Value {
        let script = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/mint_receipt.py");
        let py = std::env::var("QORCAP_PYTHON").unwrap_or_else(|_| "python3".into());
        let mut cmd = Command::new(py);
        cmd.env("PYTHONPATH", pythonpath(qortime));
        cmd.arg(script).arg(match_id).arg(out);
        if broken {
            cmd.arg("break");
        }
        let o = cmd.output().expect("python");
        assert!(o.status.success(), "mint failed: {}", String::from_utf8_lossy(&o.stderr));
        serde_json::from_slice(&o.stdout).expect("mint json")
    }

    fn qortime_dir() -> Option<PathBuf> {
        let mut cands = Vec::new();
        if let Ok(p) = std::env::var("QORTIME") {
            cands.push(PathBuf::from(p));
        }
        cands.push(PathBuf::from("qortime"));
        cands.push(PathBuf::from("../qortime"));
        cands.push(PathBuf::from("/tmp/qortime-full"));
        for c in cands {
            if c.join("qor_receipt").join("receipt.py").is_file() {
                return Some(c);
            }
        }
        if std::env::var_os("CI").is_some() {
            panic!("QORTIME checkout required in CI (pin {QORTIME_PIN})");
        }
        eprintln!("skip: QorTime checkout not found");
        None
    }

    fn ffmpeg_bin() -> Option<String> {
        let o = Command::new("ffmpeg").arg("-version").output().ok()?;
        if o.status.success() { Some("ffmpeg".into()) } else { None }
    }

    fn temp(name: &str) -> PathBuf {
        let p = std::env::temp_dir().join(format!("qorcap-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&p);
        std::fs::create_dir_all(&p).unwrap();
        p
    }
}
