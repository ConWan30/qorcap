//! Draft per-frame and scorebug-reader binding. Off unless `--frame-bind` or `QORCAP_FRAME_BIND`.
//!
//! This does not redefine `frames_hash`. That field stays `SHA-256(JCS(qorcap))`, which is a
//! segment-level evidence object, not the per-frame hash QorTime's spec describes. See
//! `docs/design/frame-and-reader-binding.md`. When the flag is on, each checkpoint's `qorcap`
//! object gains `frame_bind` (the sidecar's running hash). The first checkpoint also gains
//! `readers` (the hash of `readers.json`, or `state: unbound`). Those keys sit inside the object
//! `frames_hash` already covers. They are not receipt fields. `body_of` in QorTime drops `qorcap`
//! before a checkpoint is copied into a receipt.
//!
//! `frames.jsonl` hashes encoded video access units (the sample bytes inside the sealed fMP4
//! fragment). A referee recomputes that from the fragment the segment hash already commits to,
//! with no decoder. Decoded pixels are not bit-stable across ffmpeg builds, so they are not the
//! commitment.
//!
//! Absence of `frame_bind` is not a frame-level pass. A missing sidecar on a bundle that did
//! commit `frame_bind` is a failed check, and the frame claim stays false.

use crate::evidence::{self as ev, GENESIS};
use crate::frameclock::video_pes_pts;
use serde_json::{json, Value};
use std::fs::OpenOptions;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

pub const SCHEMA: &str = "qorcap-frame-bind/v0";
pub const READERS_SCHEMA: &str = "qorcap-readers/v0";
pub const HASH_OF: &str = "encoded-video-sample";
pub const FRAMES_FILE: &str = "frames.jsonl";
pub const READERS_FILE: &str = "readers.json";
pub const ENV: &str = "QORCAP_FRAME_BIND";
/// How long `record` waits for the sidecar to post `readers` before sealing checkpoint 0 as unbound.
pub const READERS_WAIT: std::time::Duration = std::time::Duration::from_secs(8);

pub fn flag_enabled(cli: bool, env_val: Option<&str>) -> bool {
    cli || matches!(env_val.map(str::trim), Some("1" | "true" | "yes" | "on"))
}

/// Running hash of exact sidecar line bytes (no newline). `head_0` is 64 zero bytes.
/// `head_n = SHA-256(JCS({prev, sha256}))` where `sha256` is SHA-256 of line n's bytes.
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
    pub au_sha256: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SkewSample {
    pub frame: i64,
    pub mono_ms: u64,
    /// CFR time of this frame relative to the first sealed frame, in milliseconds.
    /// `clip_ms = (frame - frame_0) * 1000 * fps_den / fps_num`.
    pub clip_ms: i64,
    /// Capture clock relative to the first sealed frame: `mono_ms - mono_ms_0`.
    pub capture_ms: i64,
    /// `capture_ms - clip_ms`. Positive means the capture clock ran long relative to clip time.
    pub skew_ms: i64,
}

/// Skew between nominal clip time (frame index / fps) and the sidecar's capture `mono_ms`.
/// Both clocks are anchored at the first frame row, so a constant offset drops out and what
/// remains is the drift highlight cuts were seeing (clip time vs capture clock).
pub fn measure_skew(rows: &[FrameRow], fps_num: u64, fps_den: u64) -> Vec<SkewSample> {
    if rows.is_empty() || fps_num == 0 || fps_den == 0 {
        return vec![];
    }
    let origin_frame = rows[0].frame;
    let origin_mono = rows[0].mono_ms as i64;
    rows.iter()
        .map(|r| {
            let frames = (r.frame - origin_frame) as i128;
            let clip_ms = frames * 1000 * fps_den as i128 / fps_num as i128;
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
/// `points` are `(frame_index, mono_ms)` in frame order, the same pairs `manifest.jsonl` stores
/// and the highlight cutter interpolates. `skew_ms = mono_ms - interpolated_clip_mono_ms`.
pub fn measure_skew_against_clip_clock(
    rows: &[FrameRow],
    points: &[(i64, u64)],
) -> Vec<SkewSample> {
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
        let (Some(seg), Some(frame), Some(mono), Some(hash)) = (
            v["seg"].as_u64(),
            v["frame"].as_i64(),
            v["mono_ms"].as_u64(),
            v["au_sha256"].as_str(),
        ) else {
            continue;
        };
        out.push(FrameRow {
            seg,
            frame,
            mono_ms: mono,
            au_sha256: hash.to_string(),
        });
    }
    out
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BindReport {
    /// True only when every checkpoint committed `frame_bind` and the sidecar matched.
    pub frame_claim: bool,
    pub frame_reason: String,
    /// True only when checkpoint 0 committed a `readers.json` hash and the file matched.
    pub reader_claim: bool,
    pub reader_reason: String,
    pub errors: Vec<String>,
    /// Filled only when `frame_claim` is true. A failed or absent sidecar does not report skew.
    pub skew: Vec<SkewSample>,
}

struct Inner {
    file: Option<std::fs::File>,
    dir: Option<PathBuf>,
    head: String,
    n: u64,
    video_track: Option<u32>,
    next_frame: i64,
    arrivals: Vec<u64>,
    last_pts: Option<u64>,
    clock_lost: bool,
    readers_sha: Option<String>,
    /// Snapshot taken when the opening checkpoint is claimed, so a late post cannot change it.
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
                    "fps_num": fps_num,
                    "fps_den": 1,
                });
                if let Err(e) = inner.push_line(&header) {
                    log::error!("frame bind: header was not written ({e}); no frame claim");
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

    pub fn readers_ready(&self) -> bool {
        self.inner
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .readers_sha
            .is_some()
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

    /// Fields merged into `qorcap` before `frames_hash` is computed. `None` when the flag is off,
    /// so a flag-off checkpoint is byte-for-byte the pre-binding shape.
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
        let mut v = json!({
            "frame_bind": {"schema": SCHEMA, "n": g.n, "head": g.head},
        });
        if first {
            v["readers"] = match readers_sha {
                Some(sha) => json!({"schema": READERS_SCHEMA, "sha256": sha}),
                None => json!({"schema": READERS_SCHEMA, "state": "unbound"}),
            };
        }
        Some(v)
    }

    /// `mono_ms` is the QorCap monotonic clock at the read that contained this TS packet.
    pub fn observe_ts(&self, data: &[u8], mono_ms: u64) {
        if !self.enabled {
            return;
        }
        let mut g = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        for pkt in data.chunks_exact(crate::ring::TS) {
            let Some(pts) = video_pes_pts(pkt) else {
                continue;
            };
            if g.last_pts == Some(pts) {
                continue;
            }
            if let Some(prev) = g.last_pts {
                if pts + 90_000 < prev {
                    g.clock_lost = true;
                }
            }
            g.last_pts = Some(pts);
            g.arrivals.push(mono_ms);
        }
    }

    /// Seal one fMP4 segment. Init segments teach the video track id. Media segments append one
    /// sidecar line per video sample, or one `align_gap` line when the capture clock cannot be
    /// paired with those samples. A gap is not a per-frame time claim.
    pub fn on_segment(&self, seq: u64, kind: &str, bytes: &[u8]) {
        if !self.enabled {
            return;
        }
        let mut g = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        if kind != "media" {
            if g.video_track.is_none() {
                g.video_track = video_track_id(bytes);
            }
            return;
        }
        let read = match g.video_track {
            Some(id) => video_sample_hashes(bytes, id),
            None => SampleRead::Unreadable("video track id was not in the init segment".into()),
        };
        match read {
            SampleRead::NoVideo => {}
            SampleRead::Unreadable(reason) => {
                let from = g.next_frame;
                g.clock_lost = true;
                let _ = g.push_line(&json!({
                    "type": "align_gap", "seg": seq, "from_frame": from, "reason": reason,
                }));
            }
            SampleRead::Frames(hashes) => {
                let n = hashes.len() as i64;
                let from = g.next_frame;
                let have = g.arrivals.len() as i64 - from;
                if g.clock_lost || have < n {
                    g.clock_lost = true;
                    g.next_frame += n;
                    let _ = g.push_line(&json!({
                        "type": "align_gap", "seg": seq, "from_frame": from,
                        "samples": n, "arrivals": have.max(0),
                        "reason": "capture clock does not line up with encoded samples",
                    }));
                } else {
                    for (i, hash) in hashes.iter().enumerate() {
                        let frame = from + i as i64;
                        let mono = g.arrivals[frame as usize];
                        let _ = g.push_line(&json!({
                            "type": "frame", "seg": seq, "frame": frame,
                            "mono_ms": mono, "au_sha256": hash,
                        }));
                    }
                    g.next_frame += n;
                }
            }
        }
    }

    /// Test and tooling helper: append one already-identified frame. The live path uses `on_segment`.
    pub fn append_frame(
        &self,
        seg: u64,
        frame: i64,
        mono_ms: u64,
        au: &[u8],
    ) -> Result<(), String> {
        if !self.enabled {
            return Err("frame bind is off".into());
        }
        let mut g = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        g.push_line(&json!({
            "type": "frame", "seg": seg, "frame": frame,
            "mono_ms": mono_ms, "au_sha256": ev::sha256_hex(au),
        }))
    }

    /// Write `readers.json` as JCS plus a newline and remember its SHA-256.
    /// Refused after the opening checkpoint has been claimed: a late manifest is not a claim.
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
        std::fs::write(&path, &bytes)
            .map_err(|e| format!("cannot write {}: {e}", path.display()))?;
        g.readers_sha = Some(sha.clone());
        Ok(sha)
    }
}

impl Inner {
    fn empty(dir: Option<PathBuf>) -> Self {
        Self {
            file: None,
            dir,
            head: GENESIS.into(),
            n: 0,
            video_track: None,
            next_frame: 0,
            arrivals: Vec::new(),
            last_pts: None,
            clock_lost: false,
            readers_sha: None,
            sealed_readers: None,
            opening_claimed: false,
        }
    }

    fn push_line(&mut self, v: &Value) -> Result<(), String> {
        let line = ev::jcs(v)?;
        let sha = ev::sha256_hex(line.as_bytes());
        self.head = ev::jcs_sha(&json!({"prev": self.head, "sha256": sha}));
        self.n += 1;
        if let Some(file) = self.file.as_mut() {
            writeln!(file, "{line}").map_err(|e| format!("frame sidecar write failed: {e}"))?;
            file.flush()
                .map_err(|e| format!("frame sidecar flush failed: {e}"))?;
        }
        Ok(())
    }
}

enum SampleRead {
    Frames(Vec<String>),
    NoVideo,
    Unreadable(String),
}

fn video_sample_hashes(data: &[u8], track: u32) -> SampleRead {
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
    let mut hashes = Vec::new();
    for traf in trafs.into_iter().filter(|b| &b.ty == b"traf") {
        let traf_bytes = &moof_payload[traf.payload..traf.end];
        let tfhd = match find_payload(traf_bytes, b"tfhd") {
            Ok(Some(p)) => p,
            Ok(None) => continue,
            Err(e) => return SampleRead::Unreadable(e),
        };
        let (tid, default_size) = match parse_tfhd(tfhd) {
            Ok(v) => v,
            Err(e) => return SampleRead::Unreadable(e),
        };
        if tid != track {
            continue;
        }
        let truns = match iter_boxes(traf_bytes) {
            Ok(v) => v,
            Err(e) => return SampleRead::Unreadable(e),
        };
        for trun in truns.into_iter().filter(|b| &b.ty == b"trun") {
            let parsed = parse_trun(&traf_bytes[trun.payload..trun.end], default_size);
            let (offset, sizes) = match parsed {
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
            for size in sizes {
                let Some(end) = at.checked_add(size as usize) else {
                    return SampleRead::Unreadable("sample size overflow".into());
                };
                if end > data.len() {
                    return SampleRead::Unreadable("sample extends past the fragment".into());
                }
                hashes.push(ev::sha256_hex(&data[at..end]));
                at = end;
            }
        }
    }
    if hashes.is_empty() {
        SampleRead::NoVideo
    } else {
        SampleRead::Frames(hashes)
    }
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

fn video_track_id(init: &[u8]) -> Option<u32> {
    let moov = find_payload(init, b"moov").ok().flatten()?;
    let traks = iter_boxes(moov).ok()?;
    for trak in traks.into_iter().filter(|b| &b.ty == b"trak") {
        let body = &moov[trak.payload..trak.end];
        let tkhd = match find_payload(body, b"tkhd") {
            Ok(Some(p)) => p,
            _ => continue,
        };
        let id = track_id_of(tkhd)?;
        let mdia = match find_payload(body, b"mdia") {
            Ok(Some(p)) => p,
            _ => continue,
        };
        let hdlr = match find_payload(mdia, b"hdlr") {
            Ok(Some(p)) => p,
            _ => continue,
        };
        if hdlr.len() >= 12 && &hdlr[8..12] == b"vide" {
            return Some(id);
        }
    }
    None
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

fn parse_tfhd(p: &[u8]) -> Result<(u32, Option<u32>), String> {
    if p.len() < 8 {
        return Err("tfhd is short".into());
    }
    let flags = u32::from_be_bytes(p[0..4].try_into().unwrap()) & 0x00ff_ffff;
    let track = u32::from_be_bytes(p[4..8].try_into().unwrap());
    let mut i = 8usize;
    let take = |i: &mut usize, n: usize| -> Result<(), String> {
        if p.len().saturating_sub(*i) < n {
            return Err("tfhd is short".into());
        }
        *i += n;
        Ok(())
    };
    if flags & 0x020000 == 0 {
        return Err("tfhd is not default-base-is-moof".into());
    }
    if flags & 0x000001 != 0 {
        return Err("tfhd base-data-offset is not used".into());
    }
    if flags & 0x000002 != 0 {
        take(&mut i, 4)?;
    }
    if flags & 0x000008 != 0 {
        take(&mut i, 4)?;
    }
    let default_size = if flags & 0x000010 != 0 {
        if p.len().saturating_sub(i) < 4 {
            return Err("tfhd is short".into());
        }
        let s = u32::from_be_bytes(p[i..i + 4].try_into().unwrap());
        Some(s)
    } else {
        None
    };
    Ok((track, default_size))
}

fn parse_trun(p: &[u8], default_size: Option<u32>) -> Result<(Option<i32>, Vec<u32>), String> {
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
    let mut sizes = Vec::with_capacity(count as usize);
    for _ in 0..count {
        if has_dur {
            need(i, 4)?;
            i += 4;
        }
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
        sizes.push(size);
    }
    Ok((data_offset, sizes))
}

fn validate_readers(v: &Value) -> Result<(), String> {
    ev::jcs(v)?;
    if v["schema"] != READERS_SCHEMA {
        return Err(format!("readers manifest schema must be {READERS_SCHEMA}"));
    }
    let readers = v["readers"]
        .as_array()
        .ok_or("readers manifest needs a readers array")?;
    if readers.is_empty() {
        return Err("readers manifest has no readers".into());
    }
    for (i, r) in readers.iter().enumerate() {
        let id = r["id"]
            .as_str()
            .ok_or(format!("readers[{i}].id must be a string"))?;
        if id.is_empty() || id.len() > 80 {
            return Err(format!("readers[{i}].id is empty or too long"));
        }
        let ver = r["version"]
            .as_str()
            .ok_or(format!("readers[{i}].version must be a string"))?;
        if ver.is_empty() || ver.len() > 80 {
            return Err(format!("readers[{i}].version is empty or too long"));
        }
        let files = r["files"]
            .as_array()
            .ok_or(format!("readers[{i}].files must be an array"))?;
        if files.is_empty() {
            return Err(format!("readers[{i}] lists no files"));
        }
        for (j, f) in files.iter().enumerate() {
            let kind = f["kind"]
                .as_str()
                .ok_or(format!("readers[{i}].files[{j}].kind must be a string"))?;
            if kind != "code" && kind != "pack" {
                return Err(format!("readers[{i}].files[{j}].kind must be code or pack"));
            }
            let path = f["path"]
                .as_str()
                .ok_or(format!("readers[{i}].files[{j}].path must be a string"))?;
            if !safe_rel_path(path) {
                return Err(format!(
                    "readers[{i}].files[{j}].path is not a relative code or pack path"
                ));
            }
            let sha = f["sha256"]
                .as_str()
                .ok_or(format!("readers[{i}].files[{j}].sha256 must be a string"))?;
            if sha.len() != 64
                || !sha
                    .bytes()
                    .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
            {
                return Err(format!(
                    "readers[{i}].files[{j}].sha256 must be 64 lowercase hex chars"
                ));
            }
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

fn header_fps(lines: &[&[u8]]) -> Option<(u64, u64)> {
    let first = lines.first()?;
    let v: Value = serde_json::from_slice(first).ok()?;
    if v["type"] != "header" {
        return None;
    }
    let num = v["fps_num"].as_u64()?;
    let den = v["fps_den"].as_u64()?;
    if num == 0 || den == 0 {
        None
    } else {
        Some((num, den))
    }
}

/// Check `frames.jsonl` and `readers.json` against the checkpoints in a bundle.
///
/// No `frame_bind` on any checkpoint: `frame_claim` is false and there is no error, whether or
/// not a stray sidecar file is sitting in the directory. A committed `frame_bind` whose file is
/// missing, short, long, or altered is an error, and `frame_claim` stays false.
pub fn check_dir(dir: &Path, checkpoints: &[Value]) -> BindReport {
    let mut report = BindReport {
        frame_claim: false,
        frame_reason: "no frame_bind on any checkpoint".into(),
        reader_claim: false,
        reader_reason: "no readers commitment on the first checkpoint".into(),
        errors: Vec::new(),
        skew: Vec::new(),
    };
    if checkpoints.is_empty() {
        return report;
    }
    check_readers(dir, checkpoints, &mut report);
    let flags: Vec<bool> = checkpoints
        .iter()
        .map(|c| c["qorcap"].get("frame_bind").is_some())
        .collect();
    let any = flags.iter().any(|b| *b);
    if !any {
        return report;
    }
    if flags.iter().any(|b| !*b) {
        report.frame_reason = "frame_bind is missing from a checkpoint".into();
        report.errors.push(report.frame_reason.clone());
        return report;
    }
    let path = dir.join(FRAMES_FILE);
    let bytes = match std::fs::read(&path) {
        Ok(b) => b,
        Err(_) => {
            report.frame_reason = "frame_bind is committed but frames.jsonl is missing".into();
            report.errors.push(report.frame_reason.clone());
            return report;
        }
    };
    let lines = sidecar_lines(&bytes);
    let mut head = GENESIS.to_string();
    let mut idx = 0u64;
    let mut prev_n = 0u64;
    for (ci, cp) in checkpoints.iter().enumerate() {
        let fb = &cp["qorcap"]["frame_bind"];
        if fb["schema"] != SCHEMA {
            report.frame_reason = format!("checkpoint {ci}: frame_bind schema is not {SCHEMA}");
            report.errors.push(report.frame_reason.clone());
            return report;
        }
        let Some(n) = fb["n"].as_u64() else {
            report.frame_reason = format!("checkpoint {ci}: frame_bind.n is not an integer");
            report.errors.push(report.frame_reason.clone());
            return report;
        };
        let Some(want) = fb["head"].as_str() else {
            report.frame_reason = format!("checkpoint {ci}: frame_bind.head is missing");
            report.errors.push(report.frame_reason.clone());
            return report;
        };
        if n < prev_n {
            report
                .errors
                .push(format!("checkpoint {ci}: frame_bind.n went backwards"));
            report.frame_reason = "frame sidecar commitment is not a growing prefix".into();
            return report;
        }
        if n as usize > lines.len() {
            report.errors.push(format!(
                "checkpoint {ci}: frame_bind commits {n} lines but frames.jsonl has {}",
                lines.len()
            ));
            report.frame_reason = "frames.jsonl is shorter than a checkpoint commitment".into();
            return report;
        }
        while idx < n {
            let sha = ev::sha256_hex(lines[idx as usize]);
            head = match ev::try_jcs_sha(&json!({"prev": head, "sha256": sha})) {
                Ok(h) => h,
                Err(e) => {
                    report.errors.push(format!(
                        "checkpoint {ci}: sidecar hash is not JCS-safe ({e})"
                    ));
                    report.frame_reason = "sidecar hash failed".into();
                    return report;
                }
            };
            idx += 1;
        }
        if want != head {
            report.errors.push(format!(
                "checkpoint {ci}: frame_bind.head does not match frames.jsonl"
            ));
            report.frame_reason = "a frames.jsonl line does not match the checkpoint".into();
            return report;
        }
        prev_n = n;
    }
    if idx as usize != lines.len() {
        report.errors.push(format!(
            "frames.jsonl has {} lines but the last checkpoint commits {idx} (frames added or left unsealed)",
            lines.len()
        ));
        report.frame_reason = "frames were added or left unsealed after the last checkpoint".into();
        return report;
    }
    report.frame_claim = true;
    report.frame_reason = format!("frames.jsonl matches {idx} committed lines");
    if let Some((num, den)) = header_fps(&lines) {
        report.skew = measure_skew(&frame_rows_from_lines(&lines), num, den);
    }
    report
}

fn check_readers(dir: &Path, checkpoints: &[Value], report: &mut BindReport) {
    let first = checkpoints
        .iter()
        .find(|c| c["seq"].as_u64() == Some(0))
        .unwrap_or(&checkpoints[0]);
    let readers = &first["qorcap"]["readers"];
    if readers.is_null() || !readers.is_object() {
        return;
    }
    if readers["schema"] != READERS_SCHEMA {
        report.reader_reason = "readers commitment schema is not qorcap-readers/v0".into();
        report.errors.push(report.reader_reason.clone());
        return;
    }
    if readers["state"] == "unbound" {
        if readers.get("sha256").is_some() {
            report.reader_reason = "readers commitment is both unbound and hashed".into();
            report.errors.push(report.reader_reason.clone());
            return;
        }
        report.reader_reason = "readers were not bound before the first checkpoint".into();
        return;
    }
    let Some(want) = readers["sha256"].as_str() else {
        report.reader_reason = "readers commitment has no sha256".into();
        report.errors.push(report.reader_reason.clone());
        return;
    };
    let path = dir.join(READERS_FILE);
    match std::fs::read(&path) {
        Err(_) => {
            report.reader_reason = "readers.json is committed but missing".into();
            report.errors.push(report.reader_reason.clone());
        }
        Ok(bytes) => {
            let got = ev::sha256_hex(&bytes);
            if got != want {
                report.reader_reason = "readers.json does not match the first checkpoint".into();
                report.errors.push(report.reader_reason.clone());
            } else {
                report.reader_claim = true;
                report.reader_reason = "readers.json matches the first checkpoint".into();
            }
        }
    }
}

fn box_of(ty: &[u8; 4], payload: &[u8]) -> Vec<u8> {
    let mut v = ((8 + payload.len()) as u32).to_be_bytes().to_vec();
    v.extend_from_slice(ty);
    v.extend_from_slice(payload);
    v
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::evidence::CheckpointChain;

    fn temp_dir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("qorcap-bind-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn manifest() -> Value {
        json!({
            "schema": READERS_SCHEMA,
            "readers": [{
                "id": "scorebug",
                "version": "0.3.32",
                "files": [{
                    "kind": "code",
                    "path": "sidecar/qorcap_standard.py",
                    "sha256": "ab".repeat(32),
                }, {
                    "kind": "pack",
                    "path": "sidecar/packs/madden27_standard_score_110028.npz",
                    "sha256": "cd".repeat(32),
                }]
            }]
        })
    }

    fn seal_two(dir: &Path) -> (FrameBind, Vec<Value>) {
        let b = FrameBind::open(true, Some(dir), 30);
        b.append_frame(1, 0, 0, b"au-0").unwrap();
        b.append_frame(1, 30, 1120, b"au-30").unwrap();
        let mut ext0 = json!({"trigger": "start"});
        merge(&mut ext0, b.commitment(true).unwrap());
        let mut chain = CheckpointChain::default();
        let cp0 = chain.next(10, ext0).unwrap();
        b.append_frame(2, 90, 3719, b"au-90").unwrap();
        let mut ext1 = json!({"trigger": "segment"});
        merge(&mut ext1, b.commitment(false).unwrap());
        let cp1 = chain.next(20, ext1).unwrap();
        (b, vec![cp0, cp1])
    }

    fn merge(dst: &mut Value, src: Value) {
        if let (Some(d), Some(s)) = (dst.as_object_mut(), src.as_object()) {
            for (k, v) in s {
                d.insert(k.clone(), v.clone());
            }
        }
    }

    #[test]
    fn flag_defaults_off() {
        assert!(!flag_enabled(false, None));
        assert!(!flag_enabled(false, Some("0")));
        assert!(flag_enabled(false, Some("1")));
        assert!(flag_enabled(true, None));
        let dir = temp_dir("off");
        let b = FrameBind::open(false, Some(&dir), 30);
        assert!(!b.enabled());
        assert!(b.commitment(true).is_none());
        assert!(!dir.join(FRAMES_FILE).exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn tampering_any_sidecar_line_breaks_checkpoint_verification() {
        let dir = temp_dir("tamper");
        let (_b, cps) = seal_two(&dir);
        let ok = check_dir(&dir, &cps);
        assert!(ok.errors.is_empty(), "{:?}", ok.errors);
        assert!(ok.frame_claim);
        let original = std::fs::read(dir.join(FRAMES_FILE)).unwrap();
        let lines = sidecar_lines(&original);
        assert!(lines.len() >= 4, "header plus three frames");
        for i in 0..lines.len() {
            let mut broken = original.clone();
            let start = lines[i].as_ptr() as usize - original.as_ptr() as usize;
            let at = start + lines[i].len() / 2;
            broken[at] ^= 0x01;
            std::fs::write(dir.join(FRAMES_FILE), &broken).unwrap();
            let report = check_dir(&dir, &cps);
            assert!(!report.frame_claim, "line {i} still claimed");
            assert!(
                !report.errors.is_empty(),
                "line {i} did not fail verification"
            );
        }
        std::fs::write(dir.join(FRAMES_FILE), &original).unwrap();
        // Retime one frame: same keys, different mono_ms. The line bytes change, so the head breaks.
        let mut text = String::from_utf8(original.clone()).unwrap();
        assert!(text.contains("\"mono_ms\":1120"));
        text = text.replacen("\"mono_ms\":1120", "\"mono_ms\":1121", 1);
        std::fs::write(dir.join(FRAMES_FILE), text.as_bytes()).unwrap();
        let retimed = check_dir(&dir, &cps);
        assert!(!retimed.frame_claim);
        assert!(retimed.errors.iter().any(|e| e.contains("does not match")));
        // A line appended after the last checkpoint is not a claim and does not verify.
        std::fs::write(dir.join(FRAMES_FILE), &original).unwrap();
        let mut extra = original.clone();
        extra.extend_from_slice(b"{\"type\":\"frame\"}\n");
        std::fs::write(dir.join(FRAMES_FILE), &extra).unwrap();
        let added = check_dir(&dir, &cps);
        assert!(!added.frame_claim);
        assert!(added
            .errors
            .iter()
            .any(|e| e.contains("added") || e.contains("unsealed")));
        // Dropping a line fails the same way.
        let dropped = original[..original.len().saturating_sub(2)].to_vec();
        std::fs::write(dir.join(FRAMES_FILE), &dropped).unwrap();
        let short = check_dir(&dir, &cps);
        assert!(!short.frame_claim && !short.errors.is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn missing_sidecar_degrades_to_no_frame_claim() {
        let dir = temp_dir("missing");
        let mut chain = CheckpointChain::default();
        let old = vec![chain.next(5, json!({"trigger": "segment"})).unwrap()];
        let absent = check_dir(&dir, &old);
        assert!(absent.errors.is_empty(), "{:?}", absent.errors);
        assert!(!absent.frame_claim);
        assert!(!absent.reader_claim);
        assert!(absent.skew.is_empty());
        std::fs::write(
            dir.join(FRAMES_FILE),
            "{\"type\":\"frame\",\"frame\":0,\"mono_ms\":1}\n",
        )
        .unwrap();
        let stray = check_dir(&dir, &old);
        assert!(
            stray.errors.is_empty(),
            "an uncommitted sidecar must not become a claim or a failure"
        );
        assert!(!stray.frame_claim);
        assert!(stray.skew.is_empty());

        // The stray file is not a commitment. Remove it before sealing a real sidecar:
        // open() refuses to append a second header onto an existing frames.jsonl.
        std::fs::remove_file(dir.join(FRAMES_FILE)).unwrap();
        let (_b, cps) = seal_two(&dir);
        std::fs::remove_file(dir.join(FRAMES_FILE)).unwrap();
        let gone = check_dir(&dir, &cps);
        assert!(
            !gone.frame_claim,
            "a missing committed sidecar must not pass a frame claim"
        );
        assert!(gone.errors.iter().any(|e| e.contains("missing")));
        assert!(gone.skew.is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn timing_skew_between_clip_time_and_mono_ms_is_measurable_from_the_sidecar() {
        let rows = vec![
            FrameRow {
                seg: 1,
                frame: 0,
                mono_ms: 5_000,
                au_sha256: "a".repeat(64),
            },
            FrameRow {
                seg: 1,
                frame: 30,
                mono_ms: 6_120,
                au_sha256: "b".repeat(64),
            },
            FrameRow {
                seg: 2,
                frame: 90,
                mono_ms: 8_719,
                au_sha256: "c".repeat(64),
            },
        ];
        let skew = measure_skew(&rows, 30, 1);
        assert_eq!(skew[0].skew_ms, 0);
        assert_eq!(skew[1].clip_ms, 1_000);
        assert_eq!(skew[1].skew_ms, 120);
        assert_eq!(skew[2].clip_ms, 3_000);
        assert_eq!(skew[2].skew_ms, 719);

        // Fragment-close clip clock: 300 frames span 10_000 ms of manifest mono, starting at 5_000.
        // The sidecar's own mono_ms sits 120 ms and 719 ms off that interpolation.
        let points = [(0i64, 5_000u64), (300, 15_000u64)];
        let against = measure_skew_against_clip_clock(&rows, &points);
        assert_eq!(against[1].clip_ms, 6_000);
        assert_eq!(against[1].skew_ms, 120);
        assert_eq!(against[2].clip_ms, 8_000);
        assert_eq!(against[2].skew_ms, 719);

        let dir = temp_dir("skew");
        let (_b, cps) = seal_two(&dir);
        let report = check_dir(&dir, &cps);
        assert!(report.frame_claim, "{:?}", report.errors);
        let by_frame: Vec<(i64, i64)> = report.skew.iter().map(|s| (s.frame, s.skew_ms)).collect();
        assert!(by_frame.contains(&(30, 120)), "{by_frame:?}");
        assert!(by_frame.contains(&(90, 719)), "{by_frame:?}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn readers_hash_is_committed_only_by_the_first_checkpoint() {
        let dir = temp_dir("readers");
        let b = FrameBind::open(true, Some(&dir), 30);
        let sha = b.install_readers(&manifest()).unwrap();
        let file = std::fs::read(dir.join(READERS_FILE)).unwrap();
        assert_eq!(ev::sha256_hex(&file), sha);
        assert!(b.claim_opening());
        let mut ext = json!({"trigger": "start"});
        merge(&mut ext, b.commitment(true).unwrap());
        assert_eq!(ext["readers"]["sha256"], sha);
        let mut chain = CheckpointChain::default();
        let cp0 = chain.next(1, ext).unwrap();
        let mut later = json!({"trigger": "segment"});
        merge(&mut later, b.commitment(false).unwrap());
        assert!(later.get("readers").is_none());
        let cp1 = chain.next(2, later).unwrap();
        let ok = check_dir(&dir, &[cp0.clone(), cp1]);
        assert!(ok.reader_claim, "{:?}", ok.errors);
        assert!(ok.frame_claim, "{:?}", ok.errors);

        let mut flipped = file.clone();
        let last = flipped.len() - 2;
        flipped[last] ^= 0x01;
        std::fs::write(dir.join(READERS_FILE), &flipped).unwrap();
        let bad = check_dir(&dir, &[cp0.clone()]);
        assert!(!bad.reader_claim);
        assert!(bad.errors.iter().any(|e| e.contains("readers.json")));

        std::fs::remove_file(dir.join(READERS_FILE)).unwrap();
        let missing = check_dir(&dir, &[cp0]);
        assert!(!missing.reader_claim);
        assert!(missing.errors.iter().any(|e| e.contains("missing")));

        let unbound_dir = temp_dir("readers-unbound");
        let u = FrameBind::open(true, Some(&unbound_dir), 30);
        let mut ext = json!({"trigger": "start"});
        merge(&mut ext, u.commitment(true).unwrap());
        assert_eq!(ext["readers"]["state"], "unbound");
        let cp = CheckpointChain::default().next(1, ext).unwrap();
        let report = check_dir(&unbound_dir, &[cp]);
        assert!(!report.reader_claim);
        assert!(report.errors.is_empty(), "{:?}", report.errors);
        assert!(report.frame_claim);
        let _ = std::fs::remove_dir_all(&dir);
        let _ = std::fs::remove_dir_all(&unbound_dir);
    }

    #[test]
    fn readers_manifest_rejects_key_paths_and_late_binding() {
        let mut bad = manifest();
        bad["readers"][0]["files"][0]["path"] = json!("keys/referee.key");
        assert!(validate_readers(&bad).is_err());
        bad["readers"][0]["files"][0]["path"] = json!("../sidecar/qorcap_standard.py");
        assert!(validate_readers(&bad).is_err());
        let dir = temp_dir("late");
        let b = FrameBind::open(true, Some(&dir), 30);
        assert!(b.claim_opening());
        let err = b.install_readers(&manifest()).unwrap_err();
        assert!(err.contains("after the first checkpoint"));
        assert!(!dir.join(READERS_FILE).exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn encoded_video_samples_are_hashed_and_audio_is_not() {
        let init = {
            let moov = box_of(b"moov", &[trak(1, b"vide"), trak(2, b"soun")].concat());
            [box_of(b"ftyp", b"iso5"), moov].concat()
        };
        assert_eq!(video_track_id(&init), Some(1));
        let v0 = b"FRAME0";
        let v1 = b"FRAME1!!";
        let audio = b"AUD";
        let tfhd_v = tfhd(1);
        let tfhd_a = tfhd(2);
        let trun_v = trun(&[v0.len() as u32, v1.len() as u32], 0);
        let trun_a = trun(&[audio.len() as u32], 0);
        let traf_v = box_of(b"traf", &[tfhd_v, trun_v].concat());
        let traf_a = box_of(b"traf", &[tfhd_a, trun_a].concat());
        let moof_len = 8 + traf_v.len() + traf_a.len();
        let video_off = moof_len as i32 + 8;
        let audio_off = video_off + (v0.len() + v1.len()) as i32;
        let trun_v = trun(&[v0.len() as u32, v1.len() as u32], video_off);
        let trun_a = trun(&[audio.len() as u32], audio_off);
        let traf_v = box_of(b"traf", &[tfhd(1), trun_v].concat());
        let traf_a = box_of(b"traf", &[tfhd(2), trun_a].concat());
        let moof = box_of(b"moof", &[traf_v, traf_a].concat());
        assert_eq!(moof.len(), moof_len);
        let mdat = box_of(
            b"mdat",
            &[v0.as_slice(), v1.as_slice(), audio.as_slice()].concat(),
        );
        let seg = [moof, mdat].concat();
        match video_sample_hashes(&seg, 1) {
            SampleRead::Frames(h) => {
                assert_eq!(h, vec![ev::sha256_hex(v0), ev::sha256_hex(v1)]);
            }
            SampleRead::NoVideo => panic!("no video"),
            SampleRead::Unreadable(e) => panic!("{e}"),
        }
        let bad = trun_with_cto();
        let err = parse_trun(&bad, None).unwrap_err();
        assert!(err.contains("composition offset"));
    }

    #[test]
    fn receipt_schema_does_not_gain_sidecar_fields() {
        let schema: Value =
            serde_json::from_str(include_str!("../contracts/receipt.v0.json")).unwrap();
        assert_eq!(schema["additionalProperties"], false);
        let top: Vec<&str> = schema["properties"]
            .as_object()
            .unwrap()
            .keys()
            .map(|s| s.as_str())
            .collect();
        for forbidden in ["frame_bind", "readers", "frames_hash_v2", "qorcap"] {
            assert!(!top.contains(&forbidden), "{forbidden} is a receipt field");
        }
        let cp = &schema["$defs"]["checkpoint"]["properties"];
        assert!(cp.get("frame_bind").is_none());
        assert!(cp.get("readers").is_none());
        assert!(cp.get("qorcap").is_none());
        assert_eq!(schema["$defs"]["checkpoint"]["additionalProperties"], false);
        let draft = ev::draft_receipt(
            "m",
            "home",
            "p",
            1,
            2,
            &json!({"home": 1, "away": 0, "quarter": 4, "t_ms": 2, "basis": "clock_zero"}),
            &[],
        );
        let allowed: std::collections::BTreeSet<&String> =
            schema["properties"].as_object().unwrap().keys().collect();
        for k in draft.as_object().unwrap().keys() {
            assert!(allowed.contains(k), "draft receipt gained {k}");
        }
        assert!(draft["checkpoints"].as_object().unwrap().is_empty());
    }

    fn trak(id: u32, kind: &[u8; 4]) -> Vec<u8> {
        let mut tkhd_p = vec![0u8; 16];
        tkhd_p[12..16].copy_from_slice(&id.to_be_bytes());
        let mut hdlr_p = vec![0u8; 12];
        hdlr_p[8..12].copy_from_slice(kind);
        let mdia = box_of(b"mdia", &box_of(b"hdlr", &hdlr_p));
        box_of(b"trak", &[box_of(b"tkhd", &tkhd_p), mdia].concat())
    }

    fn tfhd(track: u32) -> Vec<u8> {
        let mut p = vec![0u8; 8];
        p[0..4].copy_from_slice(&0x020000u32.to_be_bytes());
        p[4..8].copy_from_slice(&track.to_be_bytes());
        box_of(b"tfhd", &p)
    }

    fn trun(sizes: &[u32], data_offset: i32) -> Vec<u8> {
        let mut p = Vec::new();
        p.extend_from_slice(&0x201u32.to_be_bytes());
        p.extend_from_slice(&(sizes.len() as u32).to_be_bytes());
        p.extend_from_slice(&data_offset.to_be_bytes());
        for s in sizes {
            p.extend_from_slice(&s.to_be_bytes());
        }
        box_of(b"trun", &p)
    }

    fn trun_with_cto() -> Vec<u8> {
        // data-offset | sample-size | sample-composition-time-offset = 0x1 | 0x200 | 0x800
        let mut p = Vec::new();
        p.extend_from_slice(&0xA01u32.to_be_bytes());
        p.extend_from_slice(&1u32.to_be_bytes());
        p.extend_from_slice(&0i32.to_be_bytes());
        p.extend_from_slice(&4u32.to_be_bytes());
        p.extend_from_slice(&1i32.to_be_bytes());
        p
    }
}
