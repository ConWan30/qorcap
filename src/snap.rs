//! Publish a complete JPEG without ffmpeg's atomic rename.
//!
//! ffmpeg `-atomic_writing` renames over `snapshot.jpg` while the reader may still have that
//! file open. On Windows that rename fails and the live frame stops updating. Capture writes
//! `snapshot.incoming.jpg` instead. This publisher accepts a complete JPEG when the producer
//! for this epoch has started and the bytes are not the file left from the previous producer.
//!
//! The capture clock is the engine's `started` Instant, the same zero as `inputs.jsonl` `t_us`.
//! `mono_us` is that clock read when this process accepts the exact bytes it read. Each producer
//! start records its own offset on that clock (`producer_start_us`), so a nonzero start and a
//! reopen stay on one scale. `mono_origin_ns` names the domain and is written to the input header
//! too. Media PTS stays on its own field. Without a bound engine clock nothing is published.
//! With no open epoch, `/snapshot.jpg` keeps the file path the Madden reader already uses.
use std::io::Read;
use std::path::Path;
use std::time::{Instant, SystemTime};

pub fn jpeg_complete(bytes: &[u8]) -> bool {
    bytes.len() >= 4 && bytes[0] == 0xFF && bytes[1] == 0xD8 && bytes[bytes.len() - 2] == 0xFF && bytes[bytes.len() - 1] == 0xD9
}

/// Snapshot response the CoD reader requires. A response without this version is a different API.
pub const SNAPSHOT_API: u64 = 1;
pub const SNAPSHOT_SCHEMA: &str = "qorcap-snapshot/v1";
/// Microseconds since the engine `started` Instant. Same zero as `inputs.jsonl` `t_us`.
pub const ENGINE_CLOCK_SOURCE: &str = "engine_monotonic_ns";
/// No new completed JPEG for this long and the last one is stale. ffmpeg writes 4 per second.
pub const STALE_AFTER_US: u64 = 2_000_000;

/// CLOCK_MONOTONIC as nanoseconds. Zero means the sample could not be taken.
pub fn monotonic_ns() -> u64 {
    #[cfg(unix)]
    {
        #[repr(C)]
        struct Timespec {
            tv_sec: i64,
            tv_nsec: i64,
        }
        unsafe extern "C" {
            fn clock_gettime(clk: i32, tp: *mut Timespec) -> i32;
        }
        const CLOCK_MONOTONIC: i32 = 1;
        let mut ts = Timespec { tv_sec: 0, tv_nsec: 0 };
        if unsafe { clock_gettime(CLOCK_MONOTONIC, &mut ts) } != 0 || ts.tv_sec < 0 || ts.tv_nsec < 0 {
            return 0;
        }
        (ts.tv_sec as u64).saturating_mul(1_000_000_000).saturating_add(ts.tv_nsec as u64)
    }
    #[cfg(not(unix))]
    {
        use std::sync::OnceLock;
        static BASE: OnceLock<Instant> = OnceLock::new();
        let base = BASE.get_or_init(Instant::now);
        3_600_000_000_000u64.saturating_add(base.elapsed().as_nanos() as u64)
    }
}

/// `at` expressed on `monotonic_ns`. Zero when the clock could not be read.
pub fn instant_origin_ns(at: Instant) -> u64 {
    let before = monotonic_ns();
    let elapsed = at.elapsed().as_nanos() as u64;
    let after = monotonic_ns();
    if before == 0 || after < before { return 0; }
    (before + (after - before) / 2).saturating_sub(elapsed)
}

/// One read of `snapshot.incoming.jpg` whose bytes and metadata come from the same handle.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileRead {
    pub bytes: Vec<u8>,
    pub len: u64,
    pub modified: Option<SystemTime>,
}

/// Read the file once. If its length or mtime moved during the read, the bytes and the metadata
/// may belong to different writes, so there is no read.
pub fn read_coherent(path: &Path) -> Option<FileRead> {
    read_coherent_with(path, || {})
}

/// `between` runs after the first stat and before the bytes are read. Tests use it to rewrite.
pub fn read_coherent_with(path: &Path, mut between: impl FnMut()) -> Option<FileRead> {
    let mut f = std::fs::File::open(path).ok()?;
    let before = f.metadata().ok()?;
    between();
    let mut bytes = Vec::with_capacity(before.len() as usize);
    f.read_to_end(&mut bytes).ok()?;
    let after = f.metadata().ok()?;
    let (t0, t1) = (before.modified().ok(), after.modified().ok());
    if before.len() != after.len() || t0 != t1 || bytes.len() as u64 != after.len() {
        return None;
    }
    Some(FileRead { bytes, len: after.len(), modified: t1 })
}

/// Optional media PTS carried beside a JPEG. It is not the capture clock.
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
    pts_us: Option<u64>,
    mono_us: u64,
    capture_id: String,
    hash: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct DiskSeen {
    len: u64,
    modified: Option<SystemTime>,
    hash: String,
}

#[derive(Debug)]
pub enum Serve {
    Fresh {
        bytes: Vec<u8>,
        epoch: u64,
        seq: u64,
        pts_us: Option<u64>,
        mono_us: u64,
        /// The capture generation. It is the epoch: it changes only when a producer is reopened.
        generation: u64,
        /// Frames accepted by this publisher. A counter, not a generation.
        accepted: u64,
        api: u64,
        capture_id: String,
        session_id: String,
        clock_id: String,
        clock_source: String,
        mono_origin_ns: u64,
        producer_start_us: u64,
        schema: &'static str,
    },
    /// The producer has not completed a frame for `STALE_AFTER_US`. The bytes carry no capture provenance.
    Stale { bytes: Vec<u8>, epoch: u64, age_us: u64 },
    /// An epoch is open and the new producer has not published a frame.
    AwaitingProducer,
    /// No capture epoch. The caller may read the file.
    Absent,
}

/// What was on disk for the incoming file when a capture epoch opened.
///
/// `Known` is a successful read of the prior bytes (held back until a rewrite).
/// `ConfirmedAbsent` is positive evidence there were no prior bytes (a missing
/// file). `Unknown` is any other read outcome: the prior bytes could not be
/// read, so they are not known to be absent and nothing on disk may take a new
/// identity until this producer is positively seen to rewrite it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Baseline {
    ConfirmedAbsent,
    Known(Vec<u8>),
    Unknown,
}

impl Baseline {
    /// A missing file is a confirmed-absent baseline. Every other read error is
    /// unknown, never absent: swallowing it into empty bytes would let the old
    /// JPEG be re-accepted under a new epoch id and a current timestamp.
    pub fn from_read(read: std::io::Result<Vec<u8>>) -> Self {
        match read {
            Ok(bytes) => Baseline::Known(bytes),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Baseline::ConfirmedAbsent,
            Err(_) => Baseline::Unknown,
        }
    }
}

pub struct Publisher {
    pub frames: u64,
    pub failures: u64,
    memory: Option<Vec<u8>>,
    accepted_count: u64,
    epoch: u64,
    epoch_open: Option<u64>,
    pending_seq: Option<u64>,
    pending_pts: Option<u64>,
    completion_seq: u64,
    accepted: Option<Accepted>,
    producer_started: bool,
    producer_start_us: Option<u64>,
    session_id: Option<String>,
    engine_clock: Option<(Instant, u64)>,
    retained_hash: Option<String>,
    baseline_unknown: bool,
    disk_seen: Option<DiskSeen>,
    rewrite_ready: bool,
    file_matches: bool,
    noted_fail: bool,
}

impl Publisher {
    pub fn new() -> Self {
        Self {
            frames: 0,
            failures: 0,
            memory: None,
            accepted_count: 0,
            epoch: 0,
            epoch_open: None,
            pending_seq: None,
            pending_pts: None,
            completion_seq: 0,
            accepted: None,
            producer_started: false,
            producer_start_us: None,
            session_id: None,
            engine_clock: None,
            retained_hash: None,
            baseline_unknown: false,
            disk_seen: None,
            rewrite_ready: false,
            file_matches: true,
            noted_fail: false,
        }
    }
    pub fn memory(&self) -> Option<Vec<u8>> { self.memory.clone() }

    /// The match id for this recorder. A placeholder name is not a session.
    pub fn bind_session(&mut self, session_id: &str) {
        let trimmed = session_id.trim();
        if trimmed.is_empty() || trimmed == "local" { return; }
        self.session_id = Some(trimmed.to_string());
    }

    /// The engine clock that also stamps `inputs.jsonl` `t_us`. A zero origin is not a binding.
    pub fn bind_engine_clock(&mut self, started: Instant, origin_ns: u64) {
        if origin_ns == 0 { return; }
        self.engine_clock = Some((started, origin_ns));
    }

    pub fn mono_origin_ns(&self) -> Option<u64> { self.engine_clock.map(|(_, o)| o) }

    fn now_us(&self) -> Option<u64> {
        self.engine_clock.map(|(started, _)| started.elapsed().as_micros() as u64)
    }

    /// Open a capture epoch. Bytes already on disk stay unpublished until this producer rewrites them.
    pub fn begin_epoch(&mut self, baseline: Baseline) -> u64 {
        self.epoch = self.epoch.saturating_add(1);
        self.epoch_open = Some(self.epoch);
        self.memory = None;
        self.accepted = None;
        self.pending_seq = None;
        self.pending_pts = None;
        self.completion_seq = 0;
        self.producer_started = false;
        self.producer_start_us = None;
        self.disk_seen = None;
        self.rewrite_ready = false;
        self.baseline_unknown = matches!(baseline, Baseline::Unknown);
        self.retained_hash = match &baseline {
            Baseline::Known(prior) if jpeg_complete(prior) => Some(crate::evidence::sha256_hex(prior)),
            _ => None,
        };
        self.file_matches = false;
        self.epoch
    }

    /// The ffmpeg child for this epoch is running. Its start is recorded on the engine clock.
    pub fn mark_producer_started(&mut self) {
        if let Some(now) = self.now_us() { self.mark_producer_started_at(now); }
    }

    pub fn mark_producer_started_at(&mut self, now_us: u64) {
        if self.epoch_open.is_none() || self.engine_clock.is_none() { return; }
        self.producer_started = true;
        self.producer_start_us = Some(now_us);
    }

    /// Latest `frame` and `out_time_us` from ffmpeg progress. This is media PTS, not the capture clock.
    pub fn note_progress(&mut self, seq: u64, pts_us: u64) {
        if self.epoch_open.is_none() { return; }
        if let Some(prev) = self.pending_seq {
            if seq < prev { return; }
            if seq == prev && self.pending_pts.is_some_and(|p| pts_us <= p) { return; }
        }
        self.pending_seq = Some(seq);
        self.pending_pts = Some(pts_us);
    }

    /// The first coherent read is the baseline. A later change of length, mtime, or bytes is a rewrite.
    fn observe(&mut self, read: &FileRead, hash: &str) {
        if !self.producer_started { return; }
        let seen = DiskSeen { len: read.len, modified: read.modified, hash: hash.to_string() };
        match &self.disk_seen {
            None => self.disk_seen = Some(seen),
            Some(prev) if prev == &seen => {}
            Some(_) => {
                self.disk_seen = Some(seen);
                self.rewrite_ready = true;
            }
        }
    }

    /// Media PTS currently reported by ffmpeg, when one has arrived. Not an accept permit.
    pub fn pending_provenance(&self) -> Option<CaptureProvenance> {
        let epoch = self.epoch_open?;
        let seq = self.pending_seq?;
        let pts_us = self.pending_pts?;
        Some(CaptureProvenance {
            epoch,
            seq,
            pts_us,
            mono_us: 0,
            capture_id: String::new(),
        })
    }

    /// Production path. Identity, freshness, and the published bytes all come from this one read.
    pub fn consider_read(&mut self, read: &FileRead, dest: &Path) -> bool {
        let Some(now) = self.now_us() else { return false; };
        self.consider_read_at(read, dest, now)
    }

    pub fn consider_read_at(&mut self, read: &FileRead, dest: &Path, now_us: u64) -> bool {
        if read.len != read.bytes.len() as u64 { return false; }
        let hash = crate::evidence::sha256_hex(&read.bytes);
        self.observe(read, &hash);
        let prov = self.pending_provenance();
        self.accept(&read.bytes, hash, dest, prov, now_us)
    }

    /// Bytes with no file observation. A repeat of the same bytes is never a new capture here.
    pub fn consider(&mut self, incoming: &[u8], dest: &Path, provenance: Option<CaptureProvenance>) -> bool {
        let Some(now) = self.now_us() else { return false; };
        self.consider_at(incoming, dest, provenance, now)
    }

    pub fn consider_at(&mut self, incoming: &[u8], dest: &Path, provenance: Option<CaptureProvenance>, now_us: u64) -> bool {
        let hash = crate::evidence::sha256_hex(incoming);
        self.accept(incoming, hash, dest, provenance, now_us)
    }

    fn accept(&mut self, incoming: &[u8], hash: String, dest: &Path, provenance: Option<CaptureProvenance>, now_us: u64) -> bool {
        if !self.producer_started || self.engine_clock.is_none() { return false; }
        let Some(start) = self.producer_start_us else { return false; };
        let Some(session) = self.session_id.as_deref() else { return false; };
        if session.is_empty() { return false; }
        let Some(epoch) = self.epoch_open else { return false; };
        if !jpeg_complete(incoming) { return false; }
        // Unknown baseline: whatever was on disk may be the previous producer's
        // bytes. Only a positively observed rewrite (disk state changing after
        // the first coherent read established what was there) earns a new id;
        // the first read itself never does, whatever bytes it holds.
        if self.baseline_unknown && self.accepted.is_none() && !self.rewrite_ready { return false; }
        let retained = self.retained_hash.as_deref() == Some(hash.as_str());
        let same = self.accepted.as_ref().is_some_and(|a| a.epoch == epoch && a.hash == hash);
        if (retained && self.accepted.is_none() && !self.rewrite_ready) || (same && !self.rewrite_ready) {
            if same && !self.file_matches { self.try_replace(dest, incoming); }
            return false;
        }
        if now_us < start { return false; }
        let pts_us = provenance.as_ref().map(|p| p.pts_us).or(self.pending_pts);
        self.completion_seq = self.completion_seq.saturating_add(1);
        let seq = self.completion_seq;
        let capture_id = format!("{epoch}:{seq}:{}", &hash[..16]);
        self.rewrite_ready = false;
        self.memory = Some(incoming.to_vec());
        self.accepted = Some(Accepted { epoch, seq, pts_us, mono_us: now_us, capture_id, hash });
        self.accepted_count = self.accepted_count.saturating_add(1);
        self.frames = self.frames.saturating_add(1);
        self.file_matches = false;
        self.noted_fail = false;
        self.try_replace(dest, incoming);
        true
    }

    fn try_replace(&mut self, dest: &Path, incoming: &[u8]) {
        match replace_file(dest, incoming) {
            Ok(()) => {
                self.file_matches = true;
                self.noted_fail = false;
            }
            Err(_) => {
                if !self.noted_fail {
                    self.failures = self.failures.saturating_add(1);
                    self.noted_fail = true;
                }
            }
        }
    }

    pub fn serve(&self) -> Serve {
        match self.now_us() {
            Some(now) => self.serve_at(now),
            None if self.epoch_open.is_some() => Serve::AwaitingProducer,
            None => Serve::Absent,
        }
    }

    pub fn serve_at(&self, now_us: u64) -> Serve {
        let bound = (&self.memory, &self.accepted, &self.session_id, self.engine_clock, self.producer_start_us);
        match bound {
            (Some(bytes), Some(a), Some(session), Some((_, origin)), Some(start)) if !session.is_empty() && origin != 0 => {
                let age_us = now_us.saturating_sub(a.mono_us);
                if age_us > STALE_AFTER_US {
                    return Serve::Stale { bytes: bytes.clone(), epoch: a.epoch, age_us };
                }
                Serve::Fresh {
                    bytes: bytes.clone(),
                    epoch: a.epoch,
                    seq: a.seq,
                    pts_us: a.pts_us,
                    mono_us: a.mono_us,
                    generation: a.epoch,
                    accepted: self.accepted_count,
                    api: SNAPSHOT_API,
                    capture_id: a.capture_id.clone(),
                    session_id: session.clone(),
                    clock_id: format!("engine:{origin}"),
                    clock_source: ENGINE_CLOCK_SOURCE.to_string(),
                    mono_origin_ns: origin,
                    producer_start_us: start,
                    schema: SNAPSHOT_SCHEMA,
                }
            }
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

    const ORIGIN: u64 = 7_000_000_000;

    fn bound() -> Publisher {
        let mut p = Publisher::new();
        p.bind_session("match-session");
        p.bind_engine_clock(Instant::now(), ORIGIN);
        p
    }

    fn tmp(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("qorcap-snap-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn read(bytes: &[u8], secs: u64) -> FileRead {
        FileRead {
            bytes: bytes.to_vec(),
            len: bytes.len() as u64,
            modified: Some(SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(secs)),
        }
    }

    #[test]
    fn old_jpeg_before_the_producer_starts_is_not_a_capture_and_a_rewrite_can_repeat_pixels() {
        assert!(!jpeg_complete(&[0xFF, 0xD8, 0x00]));
        assert!(!jpeg_complete(&[0xFF, 0xD8, 0x00, 0xD9]));
        assert!(jpeg_complete(&[0xFF, 0xD8, 0x11, 0xFF, 0xD9]));
        let dir = tmp("base");
        let dest = dir.join("snapshot.jpg");
        let mut p = bound();
        assert!(matches!(p.serve_at(0), Serve::Absent));
        let jpeg = [0xFF, 0xD8, 0x22, 0xFF, 0xD9];
        assert!(!p.consider_at(&jpeg, &dest, None, 10), "no open epoch");
        assert!(matches!(p.serve_at(10), Serve::Absent), "no epoch stays on the file path");
        p.begin_epoch(Baseline::Known(jpeg.to_vec()));
        assert!(matches!(p.serve_at(10), Serve::AwaitingProducer), "an open epoch is not the old file");
        p.note_progress(1, 10_000);
        assert!(!p.consider_read_at(&read(&jpeg, 1), &dest, 20), "the producer has not started");
        assert!(p.memory().is_none());
        p.mark_producer_started_at(100);
        let newer = [0xFF, 0xD8, 0x33, 0xFF, 0xD9];
        p.note_progress(2, 20_000);
        assert!(p.consider_read_at(&read(&newer, 2), &dest, 150));
        assert_eq!(std::fs::read(&dest).unwrap(), newer);
        match p.serve_at(160) {
            Serve::Fresh { epoch, seq, pts_us, mono_us, generation, accepted, api, capture_id, bytes, session_id, clock_source, mono_origin_ns, producer_start_us, .. } => {
                assert_eq!((epoch, seq, generation, accepted, api), (1, 1, 1, 1, SNAPSHOT_API));
                assert_eq!(pts_us, Some(20_000));
                assert_eq!(mono_us, 150);
                assert_eq!(producer_start_us, 100);
                assert_eq!(session_id, "match-session");
                assert_eq!(clock_source, ENGINE_CLOCK_SOURCE);
                assert_eq!(mono_origin_ns, ORIGIN);
                assert!(capture_id.starts_with("1:1:"));
                assert_ne!(capture_id, crate::evidence::sha256_hex(&newer));
                assert_eq!(bytes, newer);
            }
            other => panic!("a new generation is fresh, got {other:?}"),
        }
        p.note_progress(3, 30_000);
        assert!(!p.consider_read_at(&read(&newer, 2), &dest, 200), "the same read is not a new id from a later progress line");
        assert!(p.consider_read_at(&read(&newer, 3), &dest, 250), "a producer rewrite of the same pixels is a new capture");
        match p.serve_at(260) {
            Serve::Fresh { seq, capture_id, pts_us, mono_us, generation, accepted, .. } => {
                assert_eq!((seq, generation, accepted), (2, 1, 2));
                assert!(capture_id.starts_with("1:2:"));
                assert_eq!(pts_us, Some(30_000));
                assert_eq!(mono_us, 250);
            }
            other => panic!("the rewrite keeps its own capture id, got {other:?}"),
        }
        p.begin_epoch(Baseline::Known(newer.to_vec()));
        assert!(matches!(p.serve_at(300), Serve::AwaitingProducer));
        p.note_progress(4, 40_000);
        assert!(!p.consider_read_at(&read(&newer, 4), &dest, 310), "reopen does not accept bytes before the new producer");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn baseline_read_outcomes_distinguish_absent_from_unknown() {
        let missing = std::io::Error::from(std::io::ErrorKind::NotFound);
        assert_eq!(Baseline::from_read(Err(missing)), Baseline::ConfirmedAbsent);
        for kind in [std::io::ErrorKind::PermissionDenied, std::io::ErrorKind::Other] {
            assert_eq!(Baseline::from_read(Err(std::io::Error::from(kind))), Baseline::Unknown, "{kind:?}");
        }
        assert_eq!(Baseline::from_read(Ok(vec![1, 2, 3])), Baseline::Known(vec![1, 2, 3]));
    }

    #[test]
    fn error_then_recovery_after_reopen_does_not_reissue_the_old_jpeg() {
        // Exact scenario: epoch 1 published a JPEG; on reopen the baseline read
        // fails transiently (unknown, not confirmed-absent) while that old JPEG
        // is still the incoming file. It must not get a new epoch id or a
        // current timestamp. Only a positively observed producer rewrite may.
        let dir = tmp("err-recovery");
        let dest = dir.join("snapshot.jpg");
        let old = [0xFF, 0xD8, 0x51, 0xFF, 0xD9];
        let mut p = bound();
        p.begin_epoch(Baseline::ConfirmedAbsent);
        p.mark_producer_started_at(100);
        assert!(p.consider_read_at(&read(&old, 1), &dest, 150));
        let first = match p.serve_at(160) {
            Serve::Fresh { capture_id, epoch, mono_us, .. } => { assert_eq!((epoch, mono_us), (1, 150)); capture_id }
            other => panic!("{other:?}"),
        };
        // Reopen: baseline read errored, so the baseline is unknown.
        p.begin_epoch(Baseline::Unknown);
        p.mark_producer_started_at(1_000);
        p.note_progress(7, 70_000);
        assert!(!p.consider_read_at(&read(&old, 1), &dest, 1_100), "the old JPEG is not a new capture after an unknown baseline");
        assert!(!p.consider_read_at(&read(&old, 1), &dest, 1_200), "re-polling the same disk state is not a rewrite");
        assert!(p.memory().is_none());
        assert!(matches!(p.serve_at(1_200), Serve::AwaitingProducer), "no new epoch id and no current timestamp for the old bytes");
        // Recovery: the new producer rewrites the incoming file (same pixels,
        // new disk state). That rewrite, at its own time, is the new capture.
        assert!(p.consider_read_at(&read(&old, 2), &dest, 1_300), "a positively observed rewrite recovers the epoch");
        match p.serve_at(1_310) {
            Serve::Fresh { capture_id, epoch, generation, seq, mono_us, .. } => {
                assert_eq!((epoch, generation, seq), (2, 2, 1));
                assert_eq!(mono_us, 1_300, "the timestamp belongs to the rewrite, not the old bytes");
                assert!(capture_id.starts_with("2:1:"));
                assert_ne!(capture_id, first);
            }
            other => panic!("{other:?}"),
        }
        // And a confirmed-absent reopen (missing file, not an error) still
        // accepts the producer's first frame without waiting for a second one.
        let mut q = bound();
        q.begin_epoch(Baseline::ConfirmedAbsent);
        q.mark_producer_started_at(10);
        assert!(q.consider_read_at(&read(&old, 1), &dest, 20));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn an_unbound_or_zero_clock_publishes_nothing() {
        let dir = tmp("unbound");
        let dest = dir.join("snapshot.jpg");
        for origin in [None, Some(0u64)] {
            let mut p = Publisher::new();
            p.bind_session("match-session");
            if let Some(o) = origin { p.bind_engine_clock(Instant::now(), o); }
            assert_eq!(p.mono_origin_ns(), None);
            p.begin_epoch(Baseline::ConfirmedAbsent);
            p.mark_producer_started_at(5);
            assert!(!p.consider_at(&[0xFF, 0xD8, 1, 0xFF, 0xD9], &dest, None, 10));
            assert!(matches!(p.serve_at(10), Serve::AwaitingProducer));
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn retained_bytes_do_not_take_a_new_id_from_unrelated_progress() {
        let dir = tmp("stale-id");
        let dest = dir.join("snapshot.jpg");
        let old = [0xFF, 0xD8, 0x44, 0xFF, 0xD9];
        let mut p = bound();
        p.begin_epoch(Baseline::Known(old.to_vec()));
        p.mark_producer_started_at(1);
        p.note_progress(9, 90_000);
        assert!(!p.consider_read_at(&read(&old, 1), &dest, 10));
        p.note_progress(10, 100_000);
        assert!(!p.consider_read_at(&read(&old, 1), &dest, 20));
        assert!(p.memory().is_none());
        assert!(matches!(p.serve_at(30), Serve::AwaitingProducer));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn four_distinct_jpegs_publish_on_one_progress_line() {
        let dir = tmp("rate");
        let dest = dir.join("snapshot.jpg");
        let mut p = bound();
        p.begin_epoch(Baseline::ConfirmedAbsent);
        p.mark_producer_started_at(1_000);
        p.note_progress(1, 40_000);
        let mut published = 0u64;
        for marker in 1u8..=4 {
            let jpeg = [0xFF, 0xD8, marker, 0xFF, 0xD9];
            if p.consider_read_at(&read(&jpeg, marker as u64), &dest, 1_000 + 250_000 * marker as u64) { published += 1; }
        }
        assert_eq!(published, 4, "a 1 Hz progress line must not collapse 4 Hz snapshots");
        assert_eq!(p.frames, 4);
        match p.serve_at(1_000_010) {
            Serve::Fresh { seq, pts_us, mono_us, capture_id, clock_source, session_id, mono_origin_ns, generation, accepted, .. } => {
                assert_eq!((seq, generation, accepted), (4, 1, 4));
                assert_eq!(pts_us, Some(40_000));
                assert_eq!(mono_us, 1_001_000);
                assert_eq!(clock_source, ENGINE_CLOCK_SOURCE);
                assert_eq!(session_id, "match-session");
                assert_eq!(mono_origin_ns, ORIGIN);
                assert!(capture_id.starts_with("1:4:"));
            }
            other => panic!("the fourth snapshot is the one being served, got {other:?}"),
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_nonzero_producer_offset_and_a_reopen_stay_on_the_engine_clock() {
        let dir = tmp("offset");
        let dest = dir.join("snapshot.jpg");
        let mut p = bound();
        let first = [0xFF, 0xD8, 0x61, 0xFF, 0xD9];
        let second = [0xFF, 0xD8, 0x62, 0xFF, 0xD9];
        // The child starts 8 s after the engine. Its first frame has media PTS 40 ms.
        p.begin_epoch(Baseline::ConfirmedAbsent);
        p.mark_producer_started_at(8_000_000);
        p.note_progress(1, 40_000);
        assert!(p.consider_read_at(&read(&first, 1), &dest, 8_040_000));
        let a = match p.serve_at(8_050_000) {
            Serve::Fresh { mono_us, mono_origin_ns, pts_us, producer_start_us, epoch, generation, clock_id, .. } => {
                assert_eq!((epoch, generation), (1, 1));
                assert_eq!(pts_us, Some(40_000));
                assert_eq!(producer_start_us, 8_000_000);
                assert_eq!(mono_us, 8_040_000, "mono is engine time, the same scale as inputs t_us");
                assert_eq!(mono_us - producer_start_us, pts_us.unwrap());
                assert_eq!(mono_origin_ns, ORIGIN);
                (mono_origin_ns, clock_id)
            }
            other => panic!("first epoch did not publish, got {other:?}"),
        };
        // Reopen 18 s after the engine. PTS restarts at 40 ms; engine time does not.
        p.begin_epoch(Baseline::Known(first.to_vec()));
        p.mark_producer_started_at(18_000_000);
        p.note_progress(1, 40_000);
        assert!(!p.consider_read_at(&read(&first, 1), &dest, 18_010_000), "reopen still holds the previous JPEG");
        assert!(p.consider_read_at(&read(&second, 2), &dest, 18_040_000));
        match p.serve_at(18_050_000) {
            Serve::Fresh { mono_us, mono_origin_ns, pts_us, producer_start_us, epoch, generation, clock_id, .. } => {
                assert_eq!((epoch, generation), (2, 2));
                assert_eq!(pts_us, Some(40_000));
                assert_eq!(producer_start_us, 18_000_000);
                assert_eq!(mono_us, 18_040_000);
                assert_eq!((mono_origin_ns, clock_id), a, "a reopen keeps the engine clock domain");
            }
            other => panic!("reopened epoch did not publish the new JPEG, got {other:?}"),
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_stalled_producer_is_served_stale_without_provenance() {
        let dir = tmp("stall");
        let dest = dir.join("snapshot.jpg");
        let mut p = bound();
        p.begin_epoch(Baseline::ConfirmedAbsent);
        p.mark_producer_started_at(0);
        let jpeg = [0xFF, 0xD8, 0x71, 0xFF, 0xD9];
        assert!(p.consider_read_at(&read(&jpeg, 1), &dest, 1_000_000));
        assert!(matches!(p.serve_at(1_000_000 + STALE_AFTER_US), Serve::Fresh { .. }));
        match p.serve_at(1_000_001 + STALE_AFTER_US) {
            Serve::Stale { bytes, epoch, age_us } => {
                assert_eq!(bytes, jpeg);
                assert_eq!(epoch, 1);
                assert_eq!(age_us, STALE_AFTER_US + 1);
            }
            other => panic!("a stalled producer must not be fresh, got {other:?}"),
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn interleaved_rewrite_keeps_bytes_and_metadata_together() {
        let dir = tmp("coherent");
        let path = dir.join("snapshot.incoming.jpg");
        let dest = dir.join("snapshot.jpg");
        let old = [0xFF, 0xD8, 0x0A, 0xFF, 0xD9];
        let new_same_len = [0xFF, 0xD8, 0x0B, 0xFF, 0xD9];
        let new_longer = [0xFF, 0xD8, 0x0C, 0x0C, 0xFF, 0xD9];
        let t1 = SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(1_000);
        let t2 = SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(2_000);
        let write = |bytes: &[u8], t: SystemTime| {
            std::fs::write(&path, bytes).unwrap();
            std::fs::OpenOptions::new().write(true).open(&path).unwrap().set_modified(t).unwrap();
        };
        write(&old, t1);
        let base = read_coherent(&path).unwrap();
        assert_eq!((base.bytes.as_slice(), base.modified), (&old[..], Some(t1)));
        // A rewrite lands between the first stat and the read: no read, so no mixed identity.
        assert!(read_coherent_with(&path, || write(&new_same_len, t2)).is_none());
        write(&old, t1);
        assert!(read_coherent_with(&path, || write(&new_longer, t2)).is_none());

        let mut p = bound();
        p.begin_epoch(Baseline::ConfirmedAbsent);
        p.mark_producer_started_at(0);
        write(&old, t1);
        assert!(p.consider_read_at(&read_coherent(&path).unwrap(), &dest, 100));
        let first_id = match p.serve_at(100) { Serve::Fresh { capture_id, .. } => capture_id, other => panic!("{other:?}") };
        // Interleaved rewrite: the torn poll is dropped, so the old bytes never take the new mtime.
        write(&old, t1);
        let torn = read_coherent_with(&path, || write(&new_same_len, t2));
        assert!(torn.is_none());
        assert_eq!(p.frames, 1, "a dropped poll publishes nothing");
        let next = read_coherent(&path).unwrap();
        assert_eq!((next.bytes.as_slice(), next.modified), (&new_same_len[..], Some(t2)));
        assert!(p.consider_read_at(&next, &dest, 200));
        match p.serve_at(200) {
            Serve::Fresh { capture_id, bytes, seq, .. } => {
                assert_eq!(seq, 2);
                assert_ne!(capture_id, first_id);
                assert_eq!(bytes, new_same_len);
                let digest = crate::evidence::sha256_hex(&bytes);
                assert!(capture_id.ends_with(&digest[..16]), "the id names the bytes that are served");
            }
            other => panic!("{other:?}"),
        }
        assert!(!p.consider_read_at(&next, &dest, 250), "the same coherent read is one capture");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_engine_origin_is_nonzero_and_stable_for_one_instant() {
        let started = Instant::now();
        std::thread::sleep(std::time::Duration::from_millis(5));
        let a = instant_origin_ns(started);
        std::thread::sleep(std::time::Duration::from_millis(5));
        let b = instant_origin_ns(started);
        assert_ne!(a, 0);
        assert!(a.abs_diff(b) < 2_000_000, "{a} vs {b}");
    }
}
