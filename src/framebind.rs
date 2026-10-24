//! Draft per-frame and scorebug-reader binding. Off unless `--frame-bind` or `QORCAP_FRAME_BIND`.
//!
//! This does not redefine `frames_hash`. That field stays `SHA-256(JCS(qorcap))`, a segment-level
//! evidence object, not the per-frame hash QorTime's spec describes. See
//! `docs/design/frame-and-reader-binding.md`. When the flag is on, each checkpoint's `qorcap`
//! object gains `frame_bind`. Checkpoint 0 also gains `readers`. Those keys sit inside the object
//! `frames_hash` already covers. They are not receipt fields.
//!
//! `frames.jsonl` hashes encoded video access units and pairs each sample to a capture arrival by
//! exact 90 kHz PTS. `mono_ms` is the process clock when this process read the encoder's MPEG-TS,
//! after encode and pipe delay. It is not glass time. A gap withholds times for that stretch and
//! does not fail the match. A mismatched committed hash does.
//!
//! Absence, `unbound`, a gap, a write failure, or an unverified chain is not a frame-time claim
//! and not a reader claim. The wire format is a draft until QorTime publishes the optional
//! schemas. This module does not add a contract file.

use crate::evidence::{self as ev, GENESIS};
use crate::frameclock::video_pes_pts;
use serde_json::{json, Value};
use std::fs::OpenOptions;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

pub const SCHEMA: &str = "qorcap-frame-bind/v0";
pub const READERS_SCHEMA: &str = "qorcap-readers/v0";
pub const HASH_OF: &str = "encoded-video-sample";
pub const FRAMES_FILE: &str = "frames.jsonl";
pub const READERS_FILE: &str = "readers.json";
pub const ENV: &str = "QORCAP_FRAME_BIND";
pub const PTS_TIMESCALE: u64 = 90_000;
const MONO_STEP_MAX_MS: u64 = 600_000;
const MONO_ABS_MAX_MS: u64 = 48 * 60 * 60 * 1000;

pub fn flag_enabled(cli: bool, env_val: Option<&str>) -> bool {
    cli || matches!(env_val.map(str::trim), Some("1" | "true" | "yes" | "on"))
}

/// Running hash of exact sidecar line bytes (no newline). `head_0` is 64 zero bytes.
pub fn chain_head(lines: &[&[u8]]) -> String {
    let mut head = GENESIS.to_string();
    for line in lines {
        head = ev::jcs_sha(&json!({"prev": head, "sha256": ev::sha256_hex(line)}));
    }
    head
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FrameRow {
    pub seg: u64,
    pub frame: i64,
    pub mono_ms: u64,
    /// 90 kHz PTS of the encoded sample. Clip time for skew is derived from this, not from `--fps`.
    pub pts: u64,
    pub au_sha256: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SkewSample {
    pub frame: i64,
    pub mono_ms: u64,
    /// Media time of this PTS relative to the first sealed frame, in milliseconds.
    pub clip_ms: i64,
    pub capture_ms: i64,
    /// `capture_ms - clip_ms`. This is arrival-time minus PTS time, after a constant offset drops out.
    pub skew_ms: i64,
}

/// Skew between PTS media time and sidecar `mono_ms`, anchored at the first frame row.
/// The frame rate is the PTS spacing (90 kHz), not the nominal `--fps` flag.
pub fn measure_skew(rows: &[FrameRow]) -> Vec<SkewSample> {
    if rows.is_empty() {
        return vec![];
    }
    let origin_pts = rows[0].pts as i128;
    let origin_mono = rows[0].mono_ms as i64;
    rows.iter()
        .map(|r| {
            let clip_ms = ((r.pts as i128 - origin_pts) * 1000) / PTS_TIMESCALE as i128;
            let capture_ms = r.mono_ms as i64 - origin_mono;
            SkewSample {
                frame: r.frame,
                mono_ms: r.mono_ms,
                clip_ms: clip_ms as i64,
                capture_ms,
                skew_ms: capture_ms - clip_ms as i64,
            }
        })
        .collect()
}

/// Skew of sidecar `mono_ms` against a fragment-close clip clock.
/// `points` are `(frame_index, mono_ms)` in frame order, the same pairs `manifest.jsonl` stores.
pub fn measure_skew_against_clip_clock(rows: &[FrameRow], points: &[(i64, u64)]) -> Vec<SkewSample> {
    rows.iter()
        .map(|r| {
            let clip = clip_mono_at(points, r.frame);
            let capture_ms = r.mono_ms as i64;
            SkewSample {
                frame: r.frame,
                mono_ms: r.mono_ms,
                clip_ms: clip,
                capture_ms,
                skew_ms: capture_ms - clip,
            }
        })
        .collect()
}

fn clip_mono_at(points: &[(i64, u64)], frame: i64) -> i64 {
    if points.is_empty() {
        return 0;
    }
    if frame <= points[0].0 {
        return points[0].1 as i64;
    }
    for w in points.windows(2) {
        let (f0, m0) = w[0];
        let (f1, m1) = w[1];
        if frame <= f1 {
            if f1 == f0 {
                return m1 as i64;
            }
            let num = (frame - f0) as i128 * (m1 as i128 - m0 as i128);
            let den = (f1 - f0) as i128;
            return m0 as i64 + (num / den) as i64;
        }
    }
    points[points.len() - 1].1 as i64
}

pub fn frame_rows_from_lines(lines: &[&[u8]]) -> Vec<FrameRow> {
    let mut out = Vec::new();
    for line in lines {
        let Ok(v) = serde_json::from_slice::<Value>(line) else {
            continue;
        };
        if v["type"] != "frame" {
            continue;
        }
        let (Some(seg), Some(frame), Some(mono), Some(pts), Some(hash)) = (
            v["seg"].as_u64(),
            v["frame"].as_i64(),
            v["mono_ms"].as_u64(),
            v["pts"].as_u64(),
            v["au_sha256"].as_str(),
        ) else {
            continue;
        };
        out.push(FrameRow {
            seg,
            frame,
            mono_ms: mono,
            pts,
            au_sha256: hash.to_string(),
        });
    }
    out
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Coverage {
    pub timed_frames: u64,
    pub gap_samples: u64,
    pub samples: u64,
    /// `checked`, `unavailable`, or `mismatch`.
    pub au: String,
}

impl Default for Coverage {
    fn default() -> Self {
        Self {
            timed_frames: 0,
            gap_samples: 0,
            samples: 0,
            au: "unavailable".into(),
        }
    }
}

/// Machine-readable result for a QorTime gate.
///
/// `frame_status` and `reader_status` are each `verified`, `missing`, `stripped`, or `broken`.
/// `tail` is `sealed` (the last checkpoint commits a seal line), `open` (a consistent prefix with
/// no seal; dropping the last checkpoint and its lines looks like this), or `checkpoints_missing`.
/// `frame_claim` is true only for a sealed, gap-free, verified chain with at least one timed frame.
/// Anything else is no frame-time claim. `reader_claim` is true only when `readers.json` matches
/// checkpoint 0. `unbound` is `missing`, not a claim.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BindReport {
    pub frame_status: String,
    pub reader_status: String,
    pub frame_chain_head: String,
    pub coverage: Coverage,
    pub tail: String,
    pub frame_claim: bool,
    pub frame_reason: String,
    pub reader_claim: bool,
    pub reader_reason: String,
    pub errors: Vec<String>,
    pub skew: Vec<SkewSample>,
}

impl BindReport {
    fn absent() -> Self {
        Self {
            frame_status: "missing".into(),
            reader_status: "missing".into(),
            frame_chain_head: String::new(),
            coverage: Coverage::default(),
            tail: "open".into(),
            frame_claim: false,
            frame_reason: "no frame_bind on any checkpoint".into(),
            reader_claim: false,
            reader_reason: "no readers commitment on the first checkpoint".into(),
            errors: Vec::new(),
            skew: Vec::new(),
        }
    }

    fn finish(&mut self) {
        self.frame_claim = self.frame_status == "verified"
            && self.tail == "sealed"
            && self.coverage.gap_samples == 0
            && self.coverage.timed_frames > 0
            && self.errors.is_empty();
    }
}

struct Arrival {
    pts: u64,
    mono_ms: u64,
    gen: u64,
}

struct Inner {
    file: Option<std::fs::File>,
    dir: Option<PathBuf>,
    head: String,
    n: u64,
    video_track: Option<u32>,
    timescale: Option<u32>,
    next_frame: i64,
    arrivals: Vec<Arrival>,
    last_pts: Option<u64>,
    generation: u64,
    failed: bool,
    sealed: bool,
    readers_sha: Option<String>,
    sealed_readers: Option<String>,
    opening_claimed: bool,
}

pub struct FrameBind {
    enabled: bool,
    inner: Mutex<Inner>,
}

impl FrameBind {
    pub fn open(enabled: bool, bundle: Option<&Path>, fps: u32) -> Self {
        let fps_num = fps.max(1);
        if !enabled {
            return Self {
                enabled: false,
                inner: Mutex::new(Inner::empty(None)),
            };
        }
        let Some(dir) = bundle else {
            log::error!("frame bind: no bundle directory, so no sidecar will be written");
            return Self {
                enabled: false,
                inner: Mutex::new(Inner::empty(None)),
            };
        };
        let _ = std::fs::create_dir_all(dir);
        let path = dir.join(FRAMES_FILE);
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(file) => {
                let mut inner = Inner::empty(Some(dir.to_path_buf()));
                inner.file = Some(file);
                let header = json!({
                    "type": "header",
                    "schema": SCHEMA,
                    "hash_of": HASH_OF,
                    "clock": "qorcap-mono-ms",
                    "pts_timescale": PTS_TIMESCALE,
                    "nominal_fps_num": fps_num,
                    "nominal_fps_den": 1,
                });
                if let Err(e) = inner.push_line(&header) {
                    log::error!("frame bind: header was not written ({e}); no frame claim");
                    let _ = std::fs::remove_file(&path);
                    return Self {
                        enabled: false,
                        inner: Mutex::new(Inner::empty(Some(dir.to_path_buf()))),
                    };
                }
                Self {
                    enabled: true,
                    inner: Mutex::new(inner),
                }
            }
            Err(e) => {
                log::error!("frame bind: cannot create {}: {e}", path.display());
                Self {
                    enabled: false,
                    inner: Mutex::new(Inner::empty(Some(dir.to_path_buf()))),
                }
            }
        }
    }

    pub fn enabled(&self) -> bool {
        self.enabled
    }

    pub fn opening_claimed(&self) -> bool {
        self.inner
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .opening_claimed
    }

    /// First caller emits the opening checkpoint. The readers hash is frozen here.
    pub fn claim_opening(&self) -> bool {
        let mut g = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        if g.opening_claimed {
            return false;
        }
        g.opening_claimed = true;
        g.sealed_readers = g.readers_sha.clone();
        true
    }

    /// Fields merged into `qorcap`. `None` when the flag is off.
    /// A write failure commits `state: failed` and the last line that actually landed.
    /// That is not a frame claim.
    pub fn commitment(&self, first: bool) -> Option<Value> {
        if !self.enabled {
            return None;
        }
        let g = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        let readers_sha = if g.opening_claimed {
            g.sealed_readers.clone()
        } else {
            g.readers_sha.clone()
        };
        let mut frame_bind = json!({
            "schema": SCHEMA,
            "n": g.n,
            "head": g.head,
        });
        if g.failed {
            frame_bind["state"] = json!("failed");
        }
        let mut v = json!({ "frame_bind": frame_bind });
        if first {
            v["readers"] = match readers_sha {
                Some(sha) => json!({"schema": READERS_SCHEMA, "sha256": sha}),
                None => json!({"schema": READERS_SCHEMA, "state": "unbound"}),
            };
        }
        Some(v)
    }

    /// `mono_ms` is the process clock when this TS packet was read from the encoder pipe.
    pub fn observe_ts(&self, data: &[u8], mono_ms: u64) {
        if !self.enabled {
            return;
        }
        let mut g = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        if g.failed {
            return;
        }
        for pkt in data.chunks_exact(crate::ring::TS) {
            let Some(pts) = video_pes_pts(pkt) else {
                continue;
            };
            if g.last_pts == Some(pts) {
                continue;
            }
            if let Some(prev) = g.last_pts {
                if pts + PTS_TIMESCALE < prev {
                    g.generation = g.generation.saturating_add(1);
                }
            }
            g.last_pts = Some(pts);
            let gen = g.generation;
            g.arrivals.push(Arrival { pts, mono_ms, gen });
        }
    }

    /// Seal one fMP4 segment. Samples are paired to arrivals by exact 90 kHz PTS.
    /// A miss writes `align_gap` for that stretch only. The next segment pairs on its own.
    /// A gap does not fail the match. A write error sticks `state: failed` and stops the claim.
    pub fn on_segment(&self, seq: u64, kind: &str, bytes: &[u8]) {
        if !self.enabled {
            return;
        }
        let mut g = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        if g.failed {
            return;
        }
        if kind != "media" {
            if g.video_track.is_none() {
                if let Some((id, ts)) = video_timing(bytes) {
                    g.video_track = Some(id);
                    g.timescale = Some(ts);
                }
            }
            return;
        }
        let read = match (g.video_track, g.timescale) {
            (Some(id), ts) => video_samples(bytes, id, ts),
            _ => SampleRead::Unreadable("video track id or timescale was not in the init segment".into()),
        };
        match read {
            SampleRead::NoVideo => {}
            SampleRead::Unreadable(reason) => {
                let from = g.next_frame;
                if let Err(e) = g.push_line(&json!({
                    "type": "align_gap",
                    "seg": seq,
                    "from_frame": from,
                    "samples": 0,
                    "pts": 0,
                    "reason": reason,
                })) {
                    log::error!("frame bind: {e}");
                }
            }
            SampleRead::Frames(samples) => {
                if let Err(e) = pair_samples(&mut g, seq, &samples) {
                    log::error!("frame bind: {e}");
                }
            }
        }
    }

    /// Append one already-identified frame. The live path uses `on_segment`.
    pub fn append_frame(
        &self,
        seg: u64,
        frame: i64,
        mono_ms: u64,
        pts: u64,
        au: &[u8],
    ) -> Result<(), String> {
        if !self.enabled {
            return Err("frame bind is off".into());
        }
        let mut g = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        g.push_line(&json!({
            "type": "frame",
            "seg": seg,
            "frame": frame,
            "mono_ms": mono_ms,
            "pts": pts,
            "au_sha256": ev::sha256_hex(au),
        }))
    }

    /// Last line of a finished recording. The stop checkpoint must commit it.
    /// Dropping that checkpoint and this line leaves a consistent prefix with `tail: open`.
    pub fn seal(&self) -> Result<(), String> {
        if !self.enabled {
            return Ok(());
        }
        let mut g = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        if g.sealed {
            return Ok(());
        }
        g.push_line(&json!({"type": "seal"}))?;
        g.sealed = true;
        Ok(())
    }

    #[cfg(test)]
    fn retarget_writes_for_test(&self, path: &Path) {
        let mut g = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        g.file = Some(OpenOptions::new().write(true).open(path).unwrap());
    }

    /// Write `readers.json` as JCS plus a newline. Refused after the opening checkpoint is claimed.
    /// A write error leaves no hash and no file claim.
    pub fn install_readers(&self, manifest: &Value) -> Result<String, String> {
        if !self.enabled {
            return Err("frame bind is off".into());
        }
        validate_readers(manifest)?;
        let mut bytes = ev::jcs(manifest)?.into_bytes();
        bytes.push(b'\n');
        let sha = ev::sha256_hex(&bytes);
        let mut g = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        if g.opening_claimed {
            return Err(
                "readers arrived after the first checkpoint; no reader claim was recorded".into(),
            );
        }
        let Some(dir) = g.dir.clone() else {
            return Err("frame bind has no bundle directory".into());
        };
        let path = dir.join(READERS_FILE);
        let tmp = dir.join(".readers.json.tmp");
        if let Err(e) = std::fs::write(&tmp, &bytes) {
            let _ = std::fs::remove_file(&tmp);
            return Err(format!("cannot write {}: {e}", path.display()));
        }
        if let Err(e) = std::fs::rename(&tmp, &path) {
            let _ = std::fs::remove_file(&tmp);
            return Err(format!("cannot write {}: {e}", path.display()));
        }
        g.readers_sha = Some(sha.clone());
        Ok(sha)
    }
}

fn pair_samples(g: &mut Inner, seq: u64, samples: &[VideoSample]) -> Result<(), String> {
    let gen = g.generation;
    let mut used = vec![false; g.arrivals.len()];
    let mut gap_from: Option<i64> = None;
    let mut gap_n: i64 = 0;
    let mut gap_pts: u64 = 0;
    let flush_gap = |g: &mut Inner, gap_from: &mut Option<i64>, gap_n: &mut i64, gap_pts: &mut u64| -> Result<(), String> {
        if *gap_n <= 0 {
            return Ok(());
        }
        g.push_line(&json!({
            "type": "align_gap",
            "seg": seq,
            "from_frame": gap_from.unwrap_or(g.next_frame),
            "samples": *gap_n,
            "pts": *gap_pts,
            "reason": "no arrival with this sample PTS",
        }))?;
        *gap_from = None;
        *gap_n = 0;
        *gap_pts = 0;
        Ok(())
    };
    for sample in samples {
        if g.failed {
            return Err("frame sidecar write already failed; no further claim".into());
        }
        let frame = g.next_frame;
        let hit = sample.pts90.and_then(|pts| {
            g.arrivals.iter().enumerate().find_map(|(i, a)| {
                if !used[i] && a.gen == gen && a.pts == pts {
                    Some((i, a.mono_ms, pts))
                } else {
                    None
                }
            })
        });
        match hit {
            Some((i, mono, pts)) => {
                flush_gap(g, &mut gap_from, &mut gap_n, &mut gap_pts)?;
                g.push_line(&json!({
                    "type": "frame",
                    "seg": seq,
                    "frame": frame,
                    "mono_ms": mono,
                    "pts": pts,
                    "au_sha256": sample.hash,
                }))?;
                used[i] = true;
                g.next_frame += 1;
            }
            None => {
                if gap_from.is_none() {
                    gap_from = Some(frame);
                    gap_pts = sample.pts90.unwrap_or(0);
                }
                gap_n += 1;
                g.next_frame += 1;
            }
        }
    }
    flush_gap(g, &mut gap_from, &mut gap_n, &mut gap_pts)?;
    Ok(())
}

impl Inner {
    fn empty(dir: Option<PathBuf>) -> Self {
        Self {
            file: None,
            dir,
            head: GENESIS.into(),
            n: 0,
            video_track: None,
            timescale: None,
            next_frame: 0,
            arrivals: Vec::new(),
            last_pts: None,
            generation: 0,
            failed: false,
            sealed: false,
            readers_sha: None,
            sealed_readers: None,
            opening_claimed: false,
        }
    }

    /// Write the line, then advance the running hash. A write error does not move `head`.
    fn push_line(&mut self, v: &Value) -> Result<(), String> {
        if self.failed {
            return Err("frame sidecar write already failed; no further claim".into());
        }
        let line = ev::jcs(v)?;
        let sha = ev::sha256_hex(line.as_bytes());
        let next = ev::jcs_sha(&json!({"prev": &self.head, "sha256": sha}));
        let Some(file) = self.file.as_mut() else {
            self.failed = true;
            return Err("frame sidecar has no file; no frame claim".into());
        };
        if let Err(e) = writeln!(file, "{line}").and_then(|_| file.flush()) {
            self.failed = true;
            return Err(format!("frame sidecar write failed: {e}; no frame claim"));
        }
        self.head = next;
        self.n += 1;
        Ok(())
    }
}

struct VideoSample {
    hash: String,
    pts90: Option<u64>,
}

enum SampleRead {
    Frames(Vec<VideoSample>),
    NoVideo,
    Unreadable(String),
}

fn video_samples(data: &[u8], track: u32, timescale: Option<u32>) -> SampleRead {
    let moof = match find_box(data, b"moof") {
        Ok(Some(b)) => b,
        Ok(None) => return SampleRead::Unreadable("fragment has no moof".into()),
        Err(e) => return SampleRead::Unreadable(e),
    };
    let moof_payload = &data[moof.payload..moof.end];
    let trafs = match iter_boxes(moof_payload) {
        Ok(v) => v,
        Err(e) => return SampleRead::Unreadable(e),
    };
    let mut out = Vec::new();
    for traf in trafs.into_iter().filter(|b| &b.ty == b"traf") {
        let traf_bytes = &moof_payload[traf.payload..traf.end];
        let tfhd = match find_payload(traf_bytes, b"tfhd") {
            Ok(Some(p)) => p,
            Ok(None) => continue,
            Err(e) => return SampleRead::Unreadable(e),
        };
        let parsed = match parse_tfhd(tfhd) {
            Ok(v) => v,
            Err(e) => return SampleRead::Unreadable(e),
        };
        if parsed.track != track {
            continue;
        }
        let base = match find_payload(traf_bytes, b"tfdt") {
            Ok(Some(p)) => parse_tfdt(p).ok(),
            _ => None,
        };
        let truns = match iter_boxes(traf_bytes) {
            Ok(v) => v,
            Err(e) => return SampleRead::Unreadable(e),
        };
        for trun in truns.into_iter().filter(|b| &b.ty == b"trun") {
            let parsed_trun = parse_trun(
                &traf_bytes[trun.payload..trun.end],
                parsed.default_size,
                parsed.default_duration,
            );
            let (offset, samples) = match parsed_trun {
                Ok(v) => v,
                Err(e) => return SampleRead::Unreadable(e),
            };
            let Some(off) = offset else {
                return SampleRead::Unreadable("trun has no data_offset".into());
            };
            let start = match (moof.header as i64).checked_add(off as i64) {
                Some(s) if s >= 0 => s as usize,
                _ => return SampleRead::Unreadable("data_offset is outside the fragment".into()),
            };
            let mut at = start;
            let mut cursor = base.unwrap_or(0);
            let mut pts_ok = base.is_some();
            for sample in samples {
                let Some(end) = at.checked_add(sample.size as usize) else {
                    return SampleRead::Unreadable("sample size overflow".into());
                };
                if end > data.len() {
                    return SampleRead::Unreadable("sample extends past the fragment".into());
                }
                let pts90 = if pts_ok {
                    timescale.and_then(|ts| to_90k(cursor, ts))
                } else {
                    None
                };
                if pts90.is_none() {
                    pts_ok = false;
                }
                out.push(VideoSample {
                    hash: ev::sha256_hex(&data[at..end]),
                    pts90,
                });
                at = end;
                match sample.duration {
                    Some(d) if pts_ok => cursor = cursor.saturating_add(d as u64),
                    _ => pts_ok = false,
                }
            }
        }
    }
    if out.is_empty() {
        SampleRead::NoVideo
    } else {
        SampleRead::Frames(out)
    }
}

fn to_90k(pts: u64, timescale: u32) -> Option<u64> {
    let ts = timescale as u64;
    if ts == 0 {
        return None;
    }
    let ticks = pts.checked_mul(PTS_TIMESCALE)?;
    if ticks % ts != 0 {
        return None;
    }
    Some(ticks / ts)
}

struct BoxRef {
    ty: [u8; 4],
    header: usize,
    payload: usize,
    end: usize,
}

fn iter_boxes(data: &[u8]) -> Result<Vec<BoxRef>, String> {
    let mut out = Vec::new();
    let mut i = 0usize;
    while data.len().saturating_sub(i) >= 8 {
        let mut size = u32::from_be_bytes(data[i..i + 4].try_into().unwrap()) as u64;
        let ty: [u8; 4] = data[i + 4..i + 8].try_into().unwrap();
        let mut header = 8usize;
        if size == 1 {
            if data.len().saturating_sub(i) < 16 {
                return Err("truncated 64-bit box size".into());
            }
            size = u64::from_be_bytes(data[i + 8..i + 16].try_into().unwrap());
            header = 16;
        }
        if size == 0 || size < header as u64 {
            return Err("box size is not usable".into());
        }
        if size > (data.len() - i) as u64 {
            return Err("box extends past its parent".into());
        }
        let end = i + size as usize;
        out.push(BoxRef {
            ty,
            header: i,
            payload: i + header,
            end,
        });
        i = end;
    }
    Ok(out)
}

fn find_box(data: &[u8], ty: &[u8; 4]) -> Result<Option<BoxRef>, String> {
    for b in iter_boxes(data)? {
        if &b.ty == ty {
            return Ok(Some(b));
        }
    }
    Ok(None)
}

fn find_payload<'a>(data: &'a [u8], ty: &[u8; 4]) -> Result<Option<&'a [u8]>, String> {
    Ok(find_box(data, ty)?.map(|b| &data[b.payload..b.end]))
}

fn video_timing(init: &[u8]) -> Option<(u32, u32)> {
    let moov = find_payload(init, b"moov").ok().flatten()?;
    let traks = iter_boxes(moov).ok()?;
    for trak in traks.into_iter().filter(|b| &b.ty == b"trak") {
        let body = &moov[trak.payload..trak.end];
        let tkhd = find_payload(body, b"tkhd").ok().flatten()?;
        let id = track_id_of(tkhd)?;
        let mdia = find_payload(body, b"mdia").ok().flatten()?;
        let hdlr = find_payload(mdia, b"hdlr").ok().flatten()?;
        if hdlr.len() < 12 || &hdlr[8..12] != b"vide" {
            continue;
        }
        let mdhd = find_payload(mdia, b"mdhd").ok().flatten()?;
        let ts = media_timescale(mdhd)?;
        return Some((id, ts));
    }
    None
}

#[cfg(test)]
fn video_track_id(init: &[u8]) -> Option<u32> {
    video_timing(init).map(|(id, _)| id).or_else(|| {
        let moov = find_payload(init, b"moov").ok().flatten()?;
        let traks = iter_boxes(moov).ok()?;
        for trak in traks.into_iter().filter(|b| &b.ty == b"trak") {
            let body = &moov[trak.payload..trak.end];
            let tkhd = find_payload(body, b"tkhd").ok().flatten()?;
            let id = track_id_of(tkhd)?;
            let mdia = find_payload(body, b"mdia").ok().flatten()?;
            let hdlr = find_payload(mdia, b"hdlr").ok().flatten()?;
            if hdlr.len() >= 12 && &hdlr[8..12] == b"vide" {
                return Some(id);
            }
        }
        None
    })
}

fn media_timescale(mdhd: &[u8]) -> Option<u32> {
    if mdhd.is_empty() {
        return None;
    }
    let off = if mdhd[0] == 1 { 20 } else { 12 };
    if mdhd.len() < off + 4 {
        return None;
    }
    let ts = u32::from_be_bytes(mdhd[off..off + 4].try_into().ok()?);
    if ts == 0 {
        None
    } else {
        Some(ts)
    }
}

fn track_id_of(tkhd: &[u8]) -> Option<u32> {
    if tkhd.is_empty() {
        return None;
    }
    let off = if tkhd[0] == 1 { 20 } else { 12 };
    if tkhd.len() < off + 4 {
        return None;
    }
    Some(u32::from_be_bytes(tkhd[off..off + 4].try_into().ok()?))
}

struct Tfhd {
    track: u32,
    default_size: Option<u32>,
    default_duration: Option<u32>,
}

fn parse_tfhd(p: &[u8]) -> Result<Tfhd, String> {
    if p.len() < 8 {
        return Err("tfhd is short".into());
    }
    let flags = u32::from_be_bytes(p[0..4].try_into().unwrap()) & 0x00ff_ffff;
    let track = u32::from_be_bytes(p[4..8].try_into().unwrap());
    let mut i = 8usize;
    if flags & 0x020000 == 0 {
        return Err("tfhd is not default-base-is-moof".into());
    }
    if flags & 0x000001 != 0 {
        return Err("tfhd base-data-offset is not used".into());
    }
    if flags & 0x000002 != 0 {
        if p.len().saturating_sub(i) < 4 {
            return Err("tfhd is short".into());
        }
        i += 4;
    }
    let default_duration = if flags & 0x000008 != 0 {
        if p.len().saturating_sub(i) < 4 {
            return Err("tfhd is short".into());
        }
        let d = u32::from_be_bytes(p[i..i + 4].try_into().unwrap());
        i += 4;
        Some(d)
    } else {
        None
    };
    let default_size = if flags & 0x000010 != 0 {
        if p.len().saturating_sub(i) < 4 {
            return Err("tfhd is short".into());
        }
        Some(u32::from_be_bytes(p[i..i + 4].try_into().unwrap()))
    } else {
        None
    };
    Ok(Tfhd {
        track,
        default_size,
        default_duration,
    })
}

fn parse_tfdt(p: &[u8]) -> Result<u64, String> {
    if p.is_empty() {
        return Err("tfdt is short".into());
    }
    if p[0] == 1 {
        if p.len() < 12 {
            return Err("tfdt is short".into());
        }
        Ok(u64::from_be_bytes(p[4..12].try_into().unwrap()))
    } else {
        if p.len() < 8 {
            return Err("tfdt is short".into());
        }
        Ok(u32::from_be_bytes(p[4..8].try_into().unwrap()) as u64)
    }
}

#[derive(Debug)]
struct TrunSample {
    size: u32,
    duration: Option<u32>,
}

fn parse_trun(
    p: &[u8],
    default_size: Option<u32>,
    default_duration: Option<u32>,
) -> Result<(Option<i32>, Vec<TrunSample>), String> {
    if p.len() < 8 {
        return Err("trun is short".into());
    }
    let flags = u32::from_be_bytes(p[0..4].try_into().unwrap()) & 0x00ff_ffff;
    let count = u32::from_be_bytes(p[4..8].try_into().unwrap());
    let mut i = 8usize;
    let need = |i: usize, n: usize| -> Result<(), String> {
        if p.len().saturating_sub(i) < n {
            Err("trun is short".into())
        } else {
            Ok(())
        }
    };
    let data_offset = if flags & 0x000001 != 0 {
        need(i, 4)?;
        let off = i32::from_be_bytes(p[i..i + 4].try_into().unwrap());
        i += 4;
        Some(off)
    } else {
        None
    };
    if flags & 0x000004 != 0 {
        need(i, 4)?;
        i += 4;
    }
    let has_dur = flags & 0x000100 != 0;
    let has_size = flags & 0x000200 != 0;
    let has_flags = flags & 0x000400 != 0;
    let has_cto = flags & 0x000800 != 0;
    if !has_size && default_size.is_none() {
        return Err("trun has no sample size".into());
    }
    let mut samples = Vec::with_capacity(count as usize);
    for _ in 0..count {
        let duration = if has_dur {
            need(i, 4)?;
            let d = u32::from_be_bytes(p[i..i + 4].try_into().unwrap());
            i += 4;
            Some(d)
        } else {
            default_duration
        };
        let size = if has_size {
            need(i, 4)?;
            let s = u32::from_be_bytes(p[i..i + 4].try_into().unwrap());
            i += 4;
            s
        } else {
            default_size.unwrap()
        };
        if has_flags {
            need(i, 4)?;
            i += 4;
        }
        if has_cto {
            need(i, 4)?;
            let cto = i32::from_be_bytes(p[i..i + 4].try_into().unwrap());
            i += 4;
            if cto != 0 {
                return Err(
                    "composition offset is not zero; sample order is not capture order".into(),
                );
            }
        }
        samples.push(TrunSample { size, duration });
    }
    Ok((data_offset, samples))
}

fn validate_readers(v: &Value) -> Result<(), String> {
    ev::jcs(v)?;
    reject_unknown(
        v,
        &[
            "schema",
            "qorcap_version",
            "python",
            "numpy",
            "opencv",
            "ffmpeg",
            "glimpse",
            "readers",
        ],
        "readers manifest",
    )?;
    if v["schema"] != READERS_SCHEMA {
        return Err(format!("readers manifest schema must be {READERS_SCHEMA}"));
    }
    let ver = req_str(v, "qorcap_version", "readers manifest")?;
    if ver.is_empty() || ver.len() > 80 {
        return Err("qorcap_version is empty or too long".into());
    }
    let py = req_str(v, "python", "readers manifest")?;
    if py.is_empty() || py.len() > 80 {
        return Err("python version is empty or too long".into());
    }
    check_opt_version(&v["numpy"], "numpy")?;
    check_opencv(&v["opencv"])?;
    check_ffmpeg(&v["ffmpeg"])?;
    check_glimpse(&v["glimpse"])?;
    let readers = v["readers"]
        .as_array()
        .ok_or("readers manifest needs a readers array")?;
    if readers.is_empty() || readers.len() > 32 {
        return Err("readers manifest has no readers or too many".into());
    }
    for (i, r) in readers.iter().enumerate() {
        reject_unknown(r, &["id", "version", "files"], &format!("readers[{i}]"))?;
        let id = req_str(r, "id", &format!("readers[{i}]"))?;
        if id.is_empty() || id.len() > 80 {
            return Err(format!("readers[{i}].id is empty or too long"));
        }
        let ver = req_str(r, "version", &format!("readers[{i}]"))?;
        if ver.is_empty() || ver.len() > 80 {
            return Err(format!("readers[{i}].version is empty or too long"));
        }
        let files = r["files"]
            .as_array()
            .ok_or(format!("readers[{i}].files must be an array"))?;
        if files.is_empty() || files.len() > 64 {
            return Err(format!("readers[{i}] lists no files or too many"));
        }
        for (j, f) in files.iter().enumerate() {
            reject_unknown(
                f,
                &["kind", "path", "sha256"],
                &format!("readers[{i}].files[{j}]"),
            )?;
            let kind = req_str(f, "kind", &format!("readers[{i}].files[{j}]"))?;
            if kind != "code" && kind != "pack" {
                return Err(format!("readers[{i}].files[{j}].kind must be code or pack"));
            }
            let path = req_str(f, "path", &format!("readers[{i}].files[{j}]"))?;
            if !safe_rel_path(path) {
                return Err(format!(
                    "readers[{i}].files[{j}].path is not a relative code or pack path"
                ));
            }
            let sha = req_str(f, "sha256", &format!("readers[{i}].files[{j}]"))?;
            check_hex(sha, &format!("readers[{i}].files[{j}].sha256"))?;
        }
    }
    Ok(())
}

fn req_str<'a>(v: &'a Value, key: &str, what: &str) -> Result<&'a str, String> {
    v[key]
        .as_str()
        .ok_or_else(|| format!("{what}.{key} must be a string"))
}

fn check_hex(sha: &str, what: &str) -> Result<(), String> {
    if sha.len() == 64
        && sha
            .bytes()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
    {
        Ok(())
    } else {
        Err(format!("{what} must be 64 lowercase hex chars"))
    }
}

fn check_opt_version(v: &Value, what: &str) -> Result<(), String> {
    if v.is_null() {
        return Ok(());
    }
    let s = v
        .as_str()
        .ok_or_else(|| format!("{what} must be a string or null"))?;
    if s.is_empty() || s.len() > 80 {
        return Err(format!("{what} is empty or too long"));
    }
    Ok(())
}

fn check_opencv(v: &Value) -> Result<(), String> {
    if v.is_null() {
        return Ok(());
    }
    reject_unknown(v, &["version", "build_sha256"], "opencv")?;
    let ver = req_str(v, "version", "opencv")?;
    if ver.is_empty() || ver.len() > 80 {
        return Err("opencv.version is empty or too long".into());
    }
    check_hex(req_str(v, "build_sha256", "opencv")?, "opencv.build_sha256")
}

fn check_ffmpeg(v: &Value) -> Result<(), String> {
    if v.get("state").is_some() {
        reject_unknown(v, &["state"], "ffmpeg")?;
        if v["state"] != "unbound" {
            return Err("ffmpeg.state must be unbound".into());
        }
        return Ok(());
    }
    reject_unknown(v, &["version", "sha256"], "ffmpeg")?;
    let ver = req_str(v, "version", "ffmpeg")?;
    if ver.is_empty() || ver.len() > 240 {
        return Err("ffmpeg.version is empty or too long".into());
    }
    check_hex(req_str(v, "sha256", "ffmpeg")?, "ffmpeg.sha256")
}

fn check_glimpse(v: &Value) -> Result<(), String> {
    if v.get("state").is_some() {
        reject_unknown(v, &["state"], "glimpse")?;
        if v["state"] != "unbound" {
            return Err("glimpse.state must be unbound".into());
        }
        return Ok(());
    }
    reject_unknown(
        v,
        &["model", "version", "prompt_sha256", "veto_ran"],
        "glimpse",
    )?;
    let model = req_str(v, "model", "glimpse")?;
    let ver = req_str(v, "version", "glimpse")?;
    if model.is_empty() || model.len() > 120 || ver.is_empty() || ver.len() > 80 {
        return Err("glimpse model or version is empty or too long".into());
    }
    check_hex(
        req_str(v, "prompt_sha256", "glimpse")?,
        "glimpse.prompt_sha256",
    )?;
    if !v["veto_ran"].is_boolean() {
        return Err("glimpse.veto_ran must be a boolean".into());
    }
    Ok(())
}

fn reject_unknown(v: &Value, allowed: &[&str], what: &str) -> Result<(), String> {
    let Some(map) = v.as_object() else {
        return Err(format!("{what} must be an object"));
    };
    for k in map.keys() {
        if !allowed.contains(&k.as_str()) {
            return Err(format!("{what} has unknown key {k}"));
        }
    }
    Ok(())
}

fn safe_rel_path(path: &str) -> bool {
    if path.is_empty() || path.len() > 240 || path.contains('\0') {
        return false;
    }
    if path.starts_with('/') || path.starts_with('\\') || path.contains(':') {
        return false;
    }
    let lower = path.to_ascii_lowercase();
    if lower.ends_with(".key")
        || lower.ends_with(".pem")
        || lower.ends_with(".p12")
        || lower.ends_with(".pfx")
    {
        return false;
    }
    for part in path.split(['/', '\\']) {
        if part.is_empty() || part == "." || part == ".." || part.eq_ignore_ascii_case("keys") {
            return false;
        }
    }
    true
}

fn sidecar_lines(bytes: &[u8]) -> Vec<&[u8]> {
    let mut lines: Vec<&[u8]> = bytes.split(|b| *b == b'\n').collect();
    if lines.last().is_some_and(|l| l.is_empty()) {
        lines.pop();
    }
    lines
}

/// Check `frames.jsonl` and `readers.json` against the checkpoints in a bundle.
pub fn check_dir(dir: &Path, checkpoints: &[Value]) -> BindReport {
    let mut report = BindReport::absent();
    let sidecars = dir.join(FRAMES_FILE).exists() || dir.join(READERS_FILE).exists();
    if checkpoints.is_empty() {
        if sidecars {
            report.frame_status = "broken".into();
            report.reader_status = "broken".into();
            report.tail = "checkpoints_missing".into();
            report.frame_reason = "checkpoints.jsonl is missing; sidecars are not a claim".into();
            report.errors.push(report.frame_reason.clone());
        }
        report.finish();
        return report;
    }
    let flags: Vec<bool> = checkpoints
        .iter()
        .map(|c| c["qorcap"].get("frame_bind").is_some())
        .collect();
    let any = flags.iter().any(|b| *b);
    if !any {
        if sidecars {
            report.frame_status = "stripped".into();
            report.reader_status = "stripped".into();
            report.frame_reason =
                "sidecar files are present but no checkpoint commits frame_bind".into();
            report.reader_reason = report.frame_reason.clone();
        }
        report.finish();
        return report;
    }
    check_readers(dir, checkpoints, true, &mut report);
    if flags.iter().any(|b| !*b) {
        report.frame_status = "broken".into();
        report.frame_reason = "frame_bind is missing from a checkpoint".into();
        report.errors.push(report.frame_reason.clone());
        report.finish();
        return report;
    }
    let bytes = match std::fs::read(dir.join(FRAMES_FILE)) {
        Ok(b) => b,
        Err(_) => {
            report.frame_status = "broken".into();
            report.frame_reason = "frame_bind is committed but frames.jsonl is missing".into();
            report.errors.push(report.frame_reason.clone());
            report.finish();
            return report;
        }
    };
    let lines = sidecar_lines(&bytes);
    let mut head = GENESIS.to_string();
    let mut idx = 0u64;
    let mut prev_n = 0u64;
    let mut failed_state = false;
    for (ci, cp) in checkpoints.iter().enumerate() {
        let fb = &cp["qorcap"]["frame_bind"];
        if let Err(e) = reject_unknown(fb, &["schema", "n", "head", "state"], "frame_bind") {
            report.frame_status = "broken".into();
            report.frame_reason = format!("checkpoint {ci}: {e}");
            report.errors.push(report.frame_reason.clone());
            report.finish();
            return report;
        }
        if fb["schema"] != SCHEMA {
            report.frame_status = "broken".into();
            report.frame_reason = format!("checkpoint {ci}: frame_bind schema is not {SCHEMA}");
            report.errors.push(report.frame_reason.clone());
            report.finish();
            return report;
        }
        if let Some(state) = fb.get("state") {
            if state != "failed" {
                report.frame_status = "broken".into();
                report.frame_reason = format!("checkpoint {ci}: frame_bind.state is not failed");
                report.errors.push(report.frame_reason.clone());
                report.finish();
                return report;
            }
            failed_state = true;
        }
        let Some(n) = fb["n"].as_u64() else {
            report.frame_status = "broken".into();
            report.frame_reason = format!("checkpoint {ci}: frame_bind.n is not an integer");
            report.errors.push(report.frame_reason.clone());
            report.finish();
            return report;
        };
        let Some(want) = fb["head"].as_str() else {
            report.frame_status = "broken".into();
            report.frame_reason = format!("checkpoint {ci}: frame_bind.head is missing");
            report.errors.push(report.frame_reason.clone());
            report.finish();
            return report;
        };
        if n < prev_n {
            report.frame_status = "broken".into();
            report
                .errors
                .push(format!("checkpoint {ci}: frame_bind.n went backwards"));
            report.frame_reason = "frame sidecar commitment is not a growing prefix".into();
            report.finish();
            return report;
        }
        if n as usize > lines.len() {
            report.frame_status = "broken".into();
            report.errors.push(format!(
                "checkpoint {ci}: frame_bind commits {n} lines but frames.jsonl has {}",
                lines.len()
            ));
            report.frame_reason = "frames.jsonl is shorter than a checkpoint commitment".into();
            report.finish();
            return report;
        }
        while idx < n {
            let sha = ev::sha256_hex(lines[idx as usize]);
            head = match ev::try_jcs_sha(&json!({"prev": head, "sha256": sha})) {
                Ok(h) => h,
                Err(e) => {
                    report.frame_status = "broken".into();
                    report.errors.push(format!(
                        "checkpoint {ci}: sidecar hash is not JCS-safe ({e})"
                    ));
                    report.frame_reason = "sidecar hash failed".into();
                    report.finish();
                    return report;
                }
            };
            idx += 1;
        }
        if want != head {
            report.frame_status = "broken".into();
            report.errors.push(format!(
                "checkpoint {ci}: frame_bind.head does not match frames.jsonl"
            ));
            report.frame_reason = "a frames.jsonl line does not match the checkpoint".into();
            report.finish();
            return report;
        }
        prev_n = n;
    }
    report.frame_chain_head = head;
    if idx as usize != lines.len() {
        report.frame_status = "broken".into();
        report.errors.push(format!(
            "frames.jsonl has {} lines but the last checkpoint commits {idx} (frames added or left unsealed)",
            lines.len()
        ));
        report.frame_reason = "frames were added or left unsealed after the last checkpoint".into();
        report.finish();
        return report;
    }
    if let Err(e) = validate_rows(dir, checkpoints, &lines) {
        report.frame_status = "broken".into();
        report.frame_reason = e.clone();
        report.errors.push(e);
        report.finish();
        return report;
    }
    report.coverage = coverage_of(&lines);
    match au_check(dir, &lines) {
        AuCheck::Unavailable => report.coverage.au = "unavailable".into(),
        AuCheck::Checked => report.coverage.au = "checked".into(),
        AuCheck::Mismatch(e) => {
            report.coverage.au = "mismatch".into();
            report.frame_status = "broken".into();
            report.frame_reason = e.clone();
            report.errors.push(e);
            report.finish();
            return report;
        }
    }
    report.tail = if lines.last().is_some_and(|l| {
        serde_json::from_slice::<Value>(l)
            .ok()
            .is_some_and(|v| v["type"] == "seal")
    }) {
        "sealed".into()
    } else {
        "open".into()
    };
    if failed_state {
        report.frame_status = "broken".into();
        report.frame_reason = "a frames.jsonl write failed; no frame claim".into();
        report.finish();
        return report;
    }
    if report.frame_status == "broken" || !report.errors.is_empty() {
        report.finish();
        return report;
    }
    report.frame_status = "verified".into();
    report.frame_reason = format!(
        "frames.jsonl matches {} committed lines; tail {}",
        idx, report.tail
    );
    if report.coverage.timed_frames > 0 && report.coverage.gap_samples == 0 {
        report.skew = measure_skew(&frame_rows_from_lines(&lines));
    }
    report.finish();
    report
}

fn check_readers(dir: &Path, checkpoints: &[Value], frame_bind: bool, report: &mut BindReport) {
    let first = checkpoints
        .iter()
        .find(|c| c["seq"].as_u64() == Some(0))
        .unwrap_or(&checkpoints[0]);
    let readers = &first["qorcap"]["readers"];
    if readers.is_null() || !readers.is_object() {
        if frame_bind {
            report.reader_status = "broken".into();
            report.frame_status = "broken".into();
            report.reader_reason =
                "frame_bind is present but readers is absent on checkpoint 0".into();
            report.errors.push(report.reader_reason.clone());
        }
        return;
    }
    if let Err(e) = reject_unknown(readers, &["schema", "sha256", "state"], "readers commitment") {
        report.reader_status = "broken".into();
        report.reader_reason = e.clone();
        report.errors.push(e);
        return;
    }
    if readers["schema"] != READERS_SCHEMA {
        report.reader_status = "broken".into();
        report.reader_reason = "readers commitment schema is not qorcap-readers/v0".into();
        report.errors.push(report.reader_reason.clone());
        return;
    }
    if readers["state"] == "unbound" {
        if readers.get("sha256").is_some() {
            report.reader_status = "broken".into();
            report.reader_reason = "readers commitment is both unbound and hashed".into();
            report.errors.push(report.reader_reason.clone());
            return;
        }
        report.reader_status = "missing".into();
        report.reader_reason = "readers were not bound before the first checkpoint".into();
        return;
    }
    let Some(want) = readers["sha256"].as_str() else {
        report.reader_status = "broken".into();
        report.reader_reason = "readers commitment has no sha256".into();
        report.errors.push(report.reader_reason.clone());
        return;
    };
    match std::fs::read(dir.join(READERS_FILE)) {
        Err(_) => {
            report.reader_status = "broken".into();
            report.reader_reason = "readers.json is committed but missing".into();
            report.errors.push(report.reader_reason.clone());
        }
        Ok(bytes) => {
            let got = ev::sha256_hex(&bytes);
            if got != want {
                report.reader_status = "broken".into();
                report.reader_reason = "readers.json does not match the first checkpoint".into();
                report.errors.push(report.reader_reason.clone());
            } else {
                report.reader_status = "verified".into();
                report.reader_claim = true;
                report.reader_reason = "readers.json matches the first checkpoint".into();
            }
        }
    }
}

fn coverage_of(lines: &[&[u8]]) -> Coverage {
    let mut c = Coverage::default();
    for line in lines {
        let Ok(v) = serde_json::from_slice::<Value>(line) else {
            continue;
        };
        if v["type"] == "frame" {
            c.timed_frames += 1;
            c.samples += 1;
        } else if v["type"] == "align_gap" {
            let n = v["samples"].as_u64().unwrap_or(0);
            c.gap_samples += n;
            c.samples += n;
        }
    }
    c
}

fn validate_rows(dir: &Path, checkpoints: &[Value], lines: &[&[u8]]) -> Result<(), String> {
    if lines.is_empty() {
        return Err("frames.jsonl has no header".into());
    }
    let header: Value =
        serde_json::from_slice(lines[0]).map_err(|_| "frames.jsonl header is not JSON".to_string())?;
    reject_unknown(
        &header,
        &[
            "type",
            "schema",
            "hash_of",
            "clock",
            "pts_timescale",
            "nominal_fps_num",
            "nominal_fps_den",
        ],
        "frames.jsonl header",
    )?;
    if header["type"] != "header"
        || header["schema"] != SCHEMA
        || header["hash_of"] != HASH_OF
        || header["clock"] != "qorcap-mono-ms"
        || header["pts_timescale"] != PTS_TIMESCALE
    {
        return Err("frames.jsonl header is not qorcap-frame-bind/v0".into());
    }
    let mut prev_n = 0usize;
    let mut prev_frame: Option<i64> = None;
    let mut prev_mono: Option<u64> = None;
    let mut prev_pts_in_seg: Option<(u64, u64)> = None;
    let mut saw_seal = false;
    for (ci, cp) in checkpoints.iter().enumerate() {
        let n = cp["qorcap"]["frame_bind"]["n"].as_u64().unwrap_or(0) as usize;
        let expected = expected_seg(dir, cp);
        for line in lines.iter().take(n).skip(prev_n) {
            let v: Value = serde_json::from_slice(line)
                .map_err(|_| format!("checkpoint {ci}: a frames.jsonl line is not JSON"))?;
            let ty = v["type"].as_str().unwrap_or("");
            if saw_seal {
                return Err("a line follows the seal".into());
            }
            match ty {
                "header" => {
                    if prev_n != 0 || line.as_ptr() != lines[0].as_ptr() {
                        return Err("header is not the first frames.jsonl line".into());
                    }
                }
                "seal" => {
                    reject_unknown(&v, &["type"], "seal line")?;
                    saw_seal = true;
                }
                "frame" => {
                    reject_unknown(
                        &v,
                        &["type", "seg", "frame", "mono_ms", "pts", "au_sha256"],
                        "frame line",
                    )?;
                    let seg = v["seg"].as_u64().ok_or("frame line seg is not an integer")?;
                    let frame = v["frame"].as_i64().ok_or("frame line frame is not an integer")?;
                    let mono = v["mono_ms"]
                        .as_u64()
                        .ok_or("frame line mono_ms is not an integer")?;
                    let pts = v["pts"].as_u64().ok_or("frame line pts is not an integer")?;
                    let hash = v["au_sha256"]
                        .as_str()
                        .ok_or("frame line au_sha256 is missing")?;
                    check_hex(hash, "au_sha256")?;
                    let want = expected.ok_or("frame line has no checkpoint segment to match")?;
                    if seg != want {
                        return Err(format!(
                            "frame {frame} seg {seg} does not match checkpoint segment {want}"
                        ));
                    }
                    if frame < 0 || prev_frame.is_some_and(|p| frame <= p) {
                        return Err(format!("frame numbers are not strictly increasing at {frame}"));
                    }
                    if mono > MONO_ABS_MAX_MS {
                        return Err(format!("mono_ms {mono} is not a sane capture read time"));
                    }
                    if let Some(prev) = prev_mono {
                        if mono < prev || mono - prev > MONO_STEP_MAX_MS {
                            return Err(format!("mono_ms {mono} is not sane after {prev}"));
                        }
                    }
                    if let Some((pseg, ppts)) = prev_pts_in_seg {
                        if pseg == seg && pts <= ppts {
                            return Err(format!("pts went backwards inside segment {seg}"));
                        }
                    }
                    if pts > (1u64 << 33) - 1 {
                        return Err("pts is outside the 33-bit MPEG range".into());
                    }
                    prev_frame = Some(frame);
                    prev_mono = Some(mono);
                    prev_pts_in_seg = Some((seg, pts));
                }
                "align_gap" => {
                    reject_unknown(
                        &v,
                        &["type", "seg", "from_frame", "samples", "pts", "reason"],
                        "align_gap",
                    )?;
                    let seg = v["seg"].as_u64().ok_or("align_gap seg is not an integer")?;
                    let want = expected.ok_or("align_gap has no checkpoint segment to match")?;
                    if seg != want {
                        return Err(format!(
                            "align_gap seg {seg} does not match checkpoint segment {want}"
                        ));
                    }
                    if v["from_frame"].as_i64().is_none_or(|n| n < 0) {
                        return Err("align_gap from_frame is not a sane index".into());
                    }
                    if v["samples"].as_u64().is_none() {
                        return Err("align_gap samples is not an integer".into());
                    }
                    if v["pts"].as_u64().is_none() {
                        return Err("align_gap pts is not an integer".into());
                    }
                    let reason = v["reason"].as_str().unwrap_or("");
                    if reason.is_empty() || reason.len() > 200 {
                        return Err("align_gap reason is empty or too long".into());
                    }
                }
                _ => return Err(format!("unknown frames.jsonl line type {ty}")),
            }
        }
        prev_n = n;
    }
    Ok(())
}

fn expected_seg(dir: &Path, cp: &Value) -> Option<u64> {
    if let Some(hash) = cp["qorcap"]["segment_hash"].as_str() {
        if let Some(seq) = manifest_seq_for_hash(dir, hash) {
            return Some(seq);
        }
    }
    cp["qorcap"]["segment_seq"].as_u64()
}

fn manifest_seq_for_hash(dir: &Path, hash: &str) -> Option<u64> {
    let text = std::fs::read_to_string(dir.join("manifest.jsonl")).ok()?;
    for line in text.lines() {
        let Ok(v) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        if v["type"] == "segment" && v["sha256"].as_str() == Some(hash) {
            return v["seq"].as_u64();
        }
    }
    None
}

enum AuCheck {
    Unavailable,
    Checked,
    Mismatch(String),
}

fn au_check(dir: &Path, lines: &[&[u8]]) -> AuCheck {
    let Some(text) = std::fs::read_to_string(dir.join("manifest.jsonl")).ok() else {
        return AuCheck::Unavailable;
    };
    let mut segs: Vec<(u64, Vec<u8>)> = Vec::new();
    for line in text.lines() {
        let Ok(v) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        if v["type"] != "segment" {
            continue;
        }
        let (Some(seq), Some(file), Some(off), Some(len)) = (
            v["seq"].as_u64(),
            v["file"].as_str(),
            v["offset"].as_u64(),
            v["bytes"].as_u64(),
        ) else {
            continue;
        };
        let Some(bytes) = read_contained(dir, file, off, len) else {
            return AuCheck::Unavailable;
        };
        segs.push((seq, bytes));
    }
    if segs.is_empty() {
        return AuCheck::Unavailable;
    }
    let Some((track, timescale)) = segs.iter().find_map(|(_, b)| video_timing(b)) else {
        return AuCheck::Unavailable;
    };
    let mut checked = false;
    for (seq, bytes) in &segs {
        let framed = frame_hashes_for_seg(lines, *seq);
        if framed.is_empty() && !gap_for_seg(lines, *seq) {
            continue;
        }
        if find_box(bytes, b"moof").ok().flatten().is_none() {
            if framed.is_empty() {
                continue;
            }
            return AuCheck::Unavailable;
        }
        let parsed = video_samples(bytes, track, Some(timescale));
        let SampleRead::Frames(samples) = parsed else {
            if framed.is_empty() {
                continue;
            }
            return AuCheck::Unavailable;
        };
        if framed.is_empty() {
            continue;
        }
        checked = true;
        if samples.len() < framed.len() {
            return AuCheck::Mismatch(format!(
                "segment {seq} has {} samples but frames.jsonl names {}",
                samples.len(),
                framed.len()
            ));
        }
        for (idx, hash) in framed {
            let Some(sample) = samples.get(idx) else {
                return AuCheck::Mismatch(format!(
                    "segment {seq} has no sample at index {idx}"
                ));
            };
            if sample.hash != hash {
                return AuCheck::Mismatch(format!(
                    "segment {seq} sample {idx} hash does not match frames.jsonl"
                ));
            }
        }
    }
    if checked {
        AuCheck::Checked
    } else {
        AuCheck::Unavailable
    }
}

fn frame_hashes_for_seg(lines: &[&[u8]], seg: u64) -> Vec<(usize, String)> {
    let mut base: Option<i64> = None;
    let mut out = Vec::new();
    for line in lines {
        let Ok(v) = serde_json::from_slice::<Value>(line) else {
            continue;
        };
        if v["seg"].as_u64() != Some(seg) {
            continue;
        }
        if v["type"] == "align_gap" {
            if let Some(from) = v["from_frame"].as_i64() {
                base = Some(base.map(|b| b.min(from)).unwrap_or(from));
            }
        }
        if v["type"] != "frame" {
            continue;
        }
        let Some(frame) = v["frame"].as_i64() else {
            continue;
        };
        base = Some(base.map(|b| b.min(frame)).unwrap_or(frame));
        if let Some(hash) = v["au_sha256"].as_str() {
            out.push((frame, hash.to_string()));
        }
    }
    let Some(base) = base else {
        return vec![];
    };
    out.into_iter()
        .filter_map(|(frame, hash)| {
            let idx = frame - base;
            if idx < 0 {
                None
            } else {
                Some((idx as usize, hash))
            }
        })
        .collect()
}

fn gap_for_seg(lines: &[&[u8]], seg: u64) -> bool {
    lines.iter().any(|l| {
        serde_json::from_slice::<Value>(l)
            .ok()
            .is_some_and(|v| v["type"] == "align_gap" && v["seg"].as_u64() == Some(seg))
    })
}

fn read_contained(dir: &Path, file: &str, off: u64, len: u64) -> Option<Vec<u8>> {
    if file.is_empty()
        || file.contains("..")
        || file.starts_with('/')
        || file.starts_with('\\')
        || file.contains(':')
        || len > 64 * 1024 * 1024
    {
        return None;
    }
    let mut f = std::fs::File::open(dir.join(file)).ok()?;
    f.seek(SeekFrom::Start(off)).ok()?;
    let mut buf = vec![0u8; len as usize];
    f.read_exact(&mut buf).ok()?;
    Some(buf)
}

#[cfg(test)]
fn box_of(ty: &[u8; 4], payload: &[u8]) -> Vec<u8> {
    let mut v = ((8 + payload.len()) as u32).to_be_bytes().to_vec();
    v.extend_from_slice(ty);
    v.extend_from_slice(payload);
    v
}

#[cfg(test)]
#[path = "framebind_tests.rs"]
mod tests;
