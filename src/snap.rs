//! Publish a complete JPEG without ffmpeg's atomic rename.
//!
//! ffmpeg `-atomic_writing` renames over `snapshot.jpg` while the reader may still have that
//! file open. On Windows that rename fails and the live frame stops updating. Capture writes
//! `snapshot.incoming.jpg` instead. This publisher accepts a complete JPEG only when the
//! capture epoch supplies a newer sequence and presentation time. Poll time is not that
//! provenance. Bytes from before the producer starts are not a new frame. The same
//! pixels can be a later capture when the producer capture id is new. With no open
//! epoch, `/snapshot.jpg` keeps the file path the Madden reader already uses.
use std::path::Path;

pub fn jpeg_complete(bytes: &[u8]) -> bool {
    bytes.len() >= 4 && bytes[0] == 0xFF && bytes[1] == 0xD8 && bytes[bytes.len() - 2] == 0xFF && bytes[bytes.len() - 1] == 0xD9
}

/// Snapshot response the CoD reader requires. A response without this version is a different API.
pub const SNAPSHOT_API: u64 = 1;
pub const SNAPSHOT_SCHEMA: &str = "qorcap-snapshot/v1";

/// Capture-relative identity of one published JPEG. This is not a wall-clock stamp.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CaptureProvenance {
    pub epoch: u64,
    pub seq: u64,
    pub pts_us: u64,
    pub capture_id: String,
    pub mono_us: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct Accepted {
    epoch: u64,
    seq: u64,
    pts_us: u64,
    mono_us: u64,
    capture_id: String,
    hash: String,
}

#[derive(Debug)]
pub enum Serve {
    Fresh {
        bytes: Vec<u8>,
        epoch: u64,
        seq: u64,
        pts_us: u64,
        mono_us: u64,
        generation: u64,
        api: u64,
        capture_id: String,
        session_id: String,
        clock_id: String,
        clock_source: String,
        mono_origin_ns: u64,
        schema: &'static str,
    },
    /// An epoch is open and the new producer has not published a frame.
    AwaitingProducer,
    /// No capture epoch. The caller may read the file.
    Absent,
}

pub struct Publisher {
    pub frames: u64,
    pub failures: u64,
    memory: Option<Vec<u8>>,
    generation: u64,
    epoch: u64,
    epoch_open: Option<u64>,
    pending_seq: Option<u64>,
    pending_pts: Option<u64>,
    accepted: Option<Accepted>,
    producer_started: bool,
    session_id: String,
    clock_id: String,
    clock_source: String,
    mono_origin_ns: u64,
    file_matches: bool,
    noted_fail: bool,
}

impl Publisher {
    pub fn new() -> Self {
        Self {
            frames: 0, failures: 0, memory: None, generation: 0, epoch: 0, epoch_open: None,
            pending_seq: None, pending_pts: None, accepted: None, producer_started: false,
            session_id: "local".into(), clock_id: String::new(), clock_source: "producer_instant".into(),
            mono_origin_ns: 0, file_matches: true, noted_fail: false,
        }
    }
    pub fn memory(&self) -> Option<Vec<u8>> { self.memory.clone() }

    /// Open a capture epoch. The producer has not started. Old incoming bytes stay unpublished.
    pub fn begin_epoch(&mut self, prior: &[u8]) -> u64 {
        let _ = prior;
        self.epoch = self.epoch.saturating_add(1);
        self.epoch_open = Some(self.epoch);
        self.memory = None;
        self.accepted = None;
        self.pending_seq = None;
        self.pending_pts = None;
        self.producer_started = false;
        self.clock_id = format!("epoch-{}:producer", self.epoch);
        self.file_matches = false;
        self.epoch
    }

    /// The ffmpeg child for this epoch is running. Bytes read before this stay stale.
    pub fn mark_producer_started(&mut self) {
        if self.epoch_open.is_some() {
            self.producer_started = true;
        }
    }

    /// Latest `frame` and `out_time_us` from ffmpeg progress. Not a wall clock.
    pub fn note_progress(&mut self, seq: u64, pts_us: u64) {
        if self.epoch_open.is_none() { return; }
        if let Some(prev) = self.pending_seq {
            if seq < prev { return; }
            if seq == prev && self.pending_pts.is_some_and(|p| pts_us <= p) { return; }
        }
        self.pending_seq = Some(seq);
        self.pending_pts = Some(pts_us);
    }

    /// Provenance for the pending progress pair, when it is strictly newer than the last accept.
    pub fn pending_provenance(&self) -> Option<CaptureProvenance> {
        let epoch = self.epoch_open?;
        let seq = self.pending_seq?;
        let pts_us = self.pending_pts?;
        if self.accepted.as_ref().is_some_and(|a| a.epoch == epoch && (seq <= a.seq || pts_us <= a.pts_us)) {
            return None;
        }
        Some(CaptureProvenance {
            epoch,
            seq,
            pts_us,
            mono_us: pts_us,
            capture_id: format!("{epoch}:{seq}"),
        })
    }

    /// Store a new complete JPEG for this epoch.
    ///
    /// Nothing is published before the producer starts, and no wall time is stored.
    /// Identical pixels are a new capture when the producer capture id is new.
    pub fn consider(&mut self, incoming: &[u8], dest: &Path, provenance: Option<CaptureProvenance>) -> bool {
        if !self.producer_started { return false; }
        if !jpeg_complete(incoming) { return false; }
        let Some(prov) = provenance else { return false; };
        if self.epoch_open != Some(prov.epoch) { return false; }
        if prov.capture_id.is_empty() { return false; }
        let hash = crate::evidence::sha256_hex(incoming);
        if self.accepted.as_ref().is_some_and(|a| a.capture_id == prov.capture_id) {
            if !self.file_matches { self.try_replace(dest, incoming); }
            return false;
        }
        if self.accepted.as_ref().is_some_and(|a| a.epoch == prov.epoch && (prov.seq <= a.seq || prov.mono_us <= a.mono_us)) {
            return false;
        }
        self.memory = Some(incoming.to_vec());
        self.accepted = Some(Accepted {
            epoch: prov.epoch,
            seq: prov.seq,
            pts_us: prov.pts_us,
            mono_us: prov.mono_us,
            capture_id: prov.capture_id,
            hash,
        });
        self.generation = self.generation.saturating_add(1);
        self.frames = self.frames.saturating_add(1);
        self.file_matches = false;
        self.noted_fail = false;
        self.try_replace(dest, incoming);
        true
    }

    fn try_replace(&mut self, dest: &Path, incoming: &[u8]) {
        match replace_file(dest, incoming) {
            Ok(()) => { self.file_matches = true; self.noted_fail = false; }
            Err(_) => {
                if !self.noted_fail {
                    self.failures = self.failures.saturating_add(1);
                    self.noted_fail = true;
                }
            }
        }
    }

    pub fn serve(&self) -> Serve {
        match (&self.memory, &self.accepted) {
            (Some(bytes), Some(a)) => Serve::Fresh {
                bytes: bytes.clone(),
                epoch: a.epoch,
                seq: a.seq,
                pts_us: a.pts_us,
                mono_us: a.mono_us,
                generation: self.generation,
                api: SNAPSHOT_API,
                capture_id: a.capture_id.clone(),
                session_id: self.session_id.clone(),
                clock_id: self.clock_id.clone(),
                clock_source: self.clock_source.clone(),
                mono_origin_ns: self.mono_origin_ns,
                schema: SNAPSHOT_SCHEMA,
            },
            _ if self.epoch_open.is_some() => Serve::AwaitingProducer,
            _ => Serve::Absent,
        }
    }
}

impl Default for Publisher { fn default() -> Self { Self::new() } }

pub fn replace_file(dest: &Path, bytes: &[u8]) -> std::io::Result<()> {
    if let Some(dir) = dest.parent() { if !dir.as_os_str().is_empty() { std::fs::create_dir_all(dir)?; } }
    let tmp = dest.with_extension("jpg.pub");
    std::fs::write(&tmp, bytes)?;
    let mut last = None;
    for _ in 0..8 {
        match std::fs::rename(&tmp, dest) {
            Ok(()) => return Ok(()),
            Err(e) => {
                let sharing = e.raw_os_error() == Some(32);
                let exists = e.kind() == std::io::ErrorKind::AlreadyExists;
                if sharing || exists {
                    let _ = std::fs::remove_file(dest);
                    last = Some(e);
                    std::thread::sleep(std::time::Duration::from_millis(15));
                    continue;
                }
                let _ = std::fs::remove_file(&tmp);
                return Err(e);
            }
        }
    }
    Err(last.unwrap_or_else(|| std::io::Error::new(std::io::ErrorKind::Other, "snapshot replace failed")))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn publish(p: &mut Publisher, dest: &Path, bytes: &[u8], seq: u64, pts_us: u64) -> bool {
        p.mark_producer_started();
        p.note_progress(seq, pts_us);
        let prov = p.pending_provenance();
        p.consider(bytes, dest, prov)
    }

    #[test]
    fn old_jpeg_before_the_producer_starts_is_not_a_capture_and_same_pixels_can_be() {
        assert!(!jpeg_complete(&[0xFF, 0xD8, 0x00]));
        assert!(!jpeg_complete(&[0xFF, 0xD8, 0x00, 0xD9]));
        assert!(jpeg_complete(&[0xFF, 0xD8, 0x11, 0xFF, 0xD9]));
        let dir = std::env::temp_dir().join(format!("qorcap-snap-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let dest = dir.join("snapshot.jpg");
        let mut p = Publisher::new();
        assert!(matches!(p.serve(), Serve::Absent));
        assert!(!p.consider(&[0xFF, 0xD8, 0x01], &dest, None));
        let jpeg = [0xFF, 0xD8, 0x22, 0xFF, 0xD9];
        assert!(!p.consider(&jpeg, &dest, Some(CaptureProvenance {
            epoch: 1, seq: 1, pts_us: 1_000, mono_us: 1_000, capture_id: "1:1".into(),
        })), "no open epoch");
        assert!(matches!(p.serve(), Serve::Absent), "no epoch stays on the file path");
        p.begin_epoch(&jpeg);
        assert!(matches!(p.serve(), Serve::AwaitingProducer), "an open epoch is not the old file");
        assert!(!p.consider(&jpeg, &dest, None), "a poll with no provenance is not a capture");
        p.note_progress(1, 10_000);
        let early = p.pending_provenance();
        assert!(!p.consider(&jpeg, &dest, early), "the producer has not started");
        assert!(p.memory().is_none());
        assert!(matches!(p.serve(), Serve::AwaitingProducer));
        let newer = [0xFF, 0xD8, 0x33, 0xFF, 0xD9];
        assert!(publish(&mut p, &dest, &newer, 2, 20_000));
        assert_eq!(std::fs::read(&dest).unwrap(), newer);
        match p.serve() {
            Serve::Fresh { epoch, seq, pts_us, mono_us, generation, api, capture_id, bytes, .. } => {
                assert_eq!((epoch, seq, pts_us, mono_us, generation, api), (1, 2, 20_000, 20_000, 1, SNAPSHOT_API));
                assert_eq!(capture_id, "1:2");
                assert_ne!(capture_id, crate::evidence::sha256_hex(&newer));
                assert_eq!(bytes, newer);
            }
            other => panic!("a new generation is fresh, got {other:?}"),
        }
        assert!(publish(&mut p, &dest, &newer, 3, 30_000), "same pixels with a new capture id are a new capture");
        match p.serve() {
            Serve::Fresh { pts_us, seq, capture_id, .. } => {
                assert_eq!((seq, pts_us), (3, 30_000));
                assert_eq!(capture_id, "1:3");
            }
            other => panic!("the new capture keeps its own pts, got {other:?}"),
        }
        p.begin_epoch(&newer);
        assert!(matches!(p.serve(), Serve::AwaitingProducer));
        p.note_progress(4, 40_000);
        let stale = p.pending_provenance();
        assert!(!p.consider(&newer, &dest, stale), "reopen does not accept bytes before the new producer");
        assert!(matches!(p.serve(), Serve::AwaitingProducer));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
