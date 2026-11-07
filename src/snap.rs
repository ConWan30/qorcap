//! Publish a complete JPEG without ffmpeg's atomic rename.
//!
//! ffmpeg `-atomic_writing` renames over `snapshot.jpg` while the reader may still have that
//! file open. On Windows that rename fails and the live frame stops updating. Capture writes
//! `snapshot.incoming.jpg` instead. This publisher accepts a complete JPEG when the producer
//! for this epoch has started and the bytes are not the file left from the previous producer.
//! Media PTS stays on its own field. The capture clock is CLOCK_MONOTONIC taken when this
//! process accepts the completed JPEG, measured from this epoch's recorder origin.
//! With no open epoch, `/snapshot.jpg` keeps the file path the Madden reader already uses.
use std::path::Path;
use std::time::SystemTime;

pub fn jpeg_complete(bytes: &[u8]) -> bool {
    bytes.len() >= 4 && bytes[0] == 0xFF && bytes[1] == 0xD8 && bytes[bytes.len() - 2] == 0xFF && bytes[bytes.len() - 1] == 0xD9
}

/// Snapshot response the CoD reader requires. A response without this version is a different API.
pub const SNAPSHOT_API: u64 = 1;
pub const SNAPSHOT_SCHEMA: &str = "qorcap-snapshot/v1";

/// CLOCK_MONOTONIC as nanoseconds. Zero means the sample could not be taken.
pub fn monotonic_ns() -> u64 {
    #[cfg(unix)]
    {
        #[repr(C)]
        struct Timespec { tv_sec: i64, tv_nsec: i64 }
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
        use std::time::Instant;
        static BASE: OnceLock<Instant> = OnceLock::new();
        let base = BASE.get_or_init(Instant::now);
        3_600_000_000_000u64.saturating_add(base.elapsed().as_nanos() as u64)
    }
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
    completion_seq: u64,
    accepted: Option<Accepted>,
    producer_started: bool,
    session_id: Option<String>,
    clock_id: String,
    clock_source: String,
    mono_origin_ns: Option<u64>,
    pending_origin_ns: Option<u64>,
    retained_hash: Option<String>,
    disk_seen: Option<DiskSeen>,
    rewrite_ready: bool,
    file_matches: bool,
    noted_fail: bool,
}

impl Publisher {
    pub fn new() -> Self {
        Self {
            frames: 0, failures: 0, memory: None, generation: 0, epoch: 0, epoch_open: None,
            pending_seq: None, pending_pts: None, completion_seq: 0, accepted: None,
            producer_started: false, session_id: None, clock_id: String::new(),
            clock_source: String::new(), mono_origin_ns: None, pending_origin_ns: None,
            retained_hash: None, disk_seen: None, rewrite_ready: false,
            file_matches: true, noted_fail: false,
        }
    }
    pub fn memory(&self) -> Option<Vec<u8>> { self.memory.clone() }

    /// The match id for this recorder. A placeholder name is not a session.
    pub fn bind_session(&mut self, session_id: &str) {
        let trimmed = session_id.trim();
        if trimmed.is_empty() || trimmed == "local" { return; }
        self.session_id = Some(trimmed.to_string());
    }

    /// Recorder origin for the next producer start, in CLOCK_MONOTONIC nanoseconds.
    /// A later `begin_epoch` drops it so a reopen samples again unless the caller sets a new one.
    pub fn set_recorder_origin_ns(&mut self, origin_ns: u64) {
        if origin_ns == 0 { return; }
        self.pending_origin_ns = Some(origin_ns);
    }

    /// Open a capture epoch. Bytes already on disk stay unpublished until this producer rewrites them.
    pub fn begin_epoch(&mut self, prior: &[u8]) -> u64 {
        self.epoch = self.epoch.saturating_add(1);
        self.epoch_open = Some(self.epoch);
        self.memory = None;
        self.accepted = None;
        self.pending_seq = None;
        self.pending_pts = None;
        self.completion_seq = 0;
        self.producer_started = false;
        self.mono_origin_ns = None;
        self.pending_origin_ns = None;
        self.clock_id.clear();
        self.clock_source.clear();
        self.disk_seen = None;
        self.rewrite_ready = false;
        self.retained_hash = if jpeg_complete(prior) {
            Some(crate::evidence::sha256_hex(prior))
        } else {
            None
        };
        self.file_matches = false;
        self.epoch
    }

    /// The ffmpeg child for this epoch is running. The origin is this start, not the previous epoch.
    pub fn mark_producer_started(&mut self) {
        if self.epoch_open.is_none() { return; }
        self.producer_started = true;
        let origin = self.pending_origin_ns.take().unwrap_or_else(monotonic_ns);
        if origin == 0 {
            self.mono_origin_ns = None;
            return;
        }
        self.mono_origin_ns = Some(origin);
        self.clock_source = "monotonic_ns".into();
        self.clock_id = format!("epoch-{}:{origin}", self.epoch);
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

    /// A poll of `snapshot.incoming.jpg`. The first sight is the baseline. A later change is a rewrite.
    pub fn observe_file(&mut self, len: u64, modified: Option<SystemTime>, bytes: &[u8]) {
        if !self.producer_started { return; }
        let seen = DiskSeen { len, modified, hash: crate::evidence::sha256_hex(bytes) };
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

    /// Store a new complete JPEG for this epoch.
    ///
    /// `provenance` may carry media PTS. It does not supply `mono_us`, and it does not
    /// turn retained bytes into a new capture. Distinct JPEGs are accepted as they
    /// complete, including several between one progress line. The same pixels are a
    /// new capture only after this producer rewrites the file.
    pub fn consider(&mut self, incoming: &[u8], dest: &Path, provenance: Option<CaptureProvenance>) -> bool {
        if !self.producer_started { return false; }
        let Some(origin) = self.mono_origin_ns else { return false; };
        let Some(session) = self.session_id.clone() else { return false; };
        if session.is_empty() { return false; }
        let Some(epoch) = self.epoch_open else { return false; };
        if !jpeg_complete(incoming) { return false; }
        let hash = crate::evidence::sha256_hex(incoming);
        let retained = self.retained_hash.as_deref() == Some(hash.as_str());
        let same = self.accepted.as_ref().is_some_and(|a| a.epoch == epoch && a.hash == hash);
        if (retained && self.accepted.is_none() && !self.rewrite_ready) || (same && !self.rewrite_ready) {
            if same && !self.file_matches { self.try_replace(dest, incoming); }
            return false;
        }
        let sample = monotonic_ns();
        if sample < origin { return false; }
        let mono_us = (sample - origin) / 1_000;
        let pts_us = provenance.as_ref().map(|p| p.pts_us).or(self.pending_pts);
        self.completion_seq = self.completion_seq.saturating_add(1);
        let seq = self.completion_seq;
        let capture_id = format!("{epoch}:{seq}:{}", &hash[..16]);
        self.rewrite_ready = false;
        self.memory = Some(incoming.to_vec());
        self.accepted = Some(Accepted {
            epoch,
            seq,
            pts_us,
            mono_us,
            capture_id,
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
        match (&self.memory, &self.accepted, &self.session_id, self.mono_origin_ns) {
            (Some(bytes), Some(a), Some(session), Some(origin)) if !session.is_empty() && self.clock_source == "monotonic_ns" => Serve::Fresh {
                bytes: bytes.clone(),
                epoch: a.epoch,
                seq: a.seq,
                pts_us: a.pts_us,
                mono_us: a.mono_us,
                generation: self.generation,
                api: SNAPSHOT_API,
                capture_id: a.capture_id.clone(),
                session_id: session.clone(),
                clock_id: self.clock_id.clone(),
                clock_source: self.clock_source.clone(),
                mono_origin_ns: origin,
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
    fn old_jpeg_before_the_producer_starts_is_not_a_capture_and_a_rewrite_can_repeat_pixels() {
        assert!(!jpeg_complete(&[0xFF, 0xD8, 0x00]));
        assert!(!jpeg_complete(&[0xFF, 0xD8, 0x00, 0xD9]));
        assert!(jpeg_complete(&[0xFF, 0xD8, 0x11, 0xFF, 0xD9]));
        let dir = std::env::temp_dir().join(format!("qorcap-snap-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let dest = dir.join("snapshot.jpg");
        let mut p = Publisher::new();
        p.bind_session("match-session");
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
            Serve::Fresh { epoch, seq, pts_us, mono_us, generation, api, capture_id, bytes, session_id, clock_source, mono_origin_ns, .. } => {
                assert_eq!((epoch, seq, generation, api), (1, 1, 1, SNAPSHOT_API));
                assert_eq!(pts_us, Some(20_000));
                assert_ne!(mono_us, 20_000, "media PTS is not the monotonic sample");
                assert_eq!(session_id, "match-session");
                assert_eq!(clock_source, "monotonic_ns");
                assert_ne!(mono_origin_ns, 0);
                assert!(capture_id.starts_with("1:1:"));
                assert_ne!(capture_id, crate::evidence::sha256_hex(&newer));
                assert_eq!(bytes, newer);
            }
            other => panic!("a new generation is fresh, got {other:?}"),
        }
        assert!(!publish(&mut p, &dest, &newer, 3, 30_000), "the same bytes are not a new id from a later progress line");
        let later = SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(50);
        p.observe_file(newer.len() as u64, Some(SystemTime::UNIX_EPOCH), &newer);
        p.observe_file(newer.len() as u64, Some(later), &newer);
        assert!(p.consider(&newer, &dest, p.pending_provenance()), "a producer rewrite of the same pixels is a new capture");
        match p.serve() {
            Serve::Fresh { seq, capture_id, pts_us, mono_us, .. } => {
                assert_eq!(seq, 2);
                assert!(capture_id.starts_with("1:2:"));
                assert_eq!(pts_us, Some(30_000));
                assert_ne!(mono_us, pts_us.unwrap_or(0));
            }
            other => panic!("the rewrite keeps its own capture id, got {other:?}"),
        }
        p.begin_epoch(&newer);
        assert!(matches!(p.serve(), Serve::AwaitingProducer));
        p.note_progress(4, 40_000);
        let stale = p.pending_provenance();
        assert!(!p.consider(&newer, &dest, stale), "reopen does not accept bytes before the new producer");
        assert!(matches!(p.serve(), Serve::AwaitingProducer));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn retained_bytes_do_not_take_a_new_id_from_unrelated_progress() {
        let dir = std::env::temp_dir().join(format!("qorcap-snap-stale-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let dest = dir.join("snapshot.jpg");
        let old = [0xFF, 0xD8, 0x44, 0xFF, 0xD9];
        let mut p = Publisher::new();
        p.bind_session("match-session");
        p.begin_epoch(&old);
        p.mark_producer_started();
        p.note_progress(9, 90_000);
        assert!(!p.consider(&old, &dest, p.pending_provenance()));
        assert!(p.memory().is_none());
        assert!(matches!(p.serve(), Serve::AwaitingProducer));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn four_distinct_jpegs_publish_on_one_progress_line() {
        let dir = std::env::temp_dir().join(format!("qorcap-snap-rate-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let dest = dir.join("snapshot.jpg");
        let mut p = Publisher::new();
        p.bind_session("match-session");
        p.begin_epoch(&[]);
        p.mark_producer_started();
        p.note_progress(1, 40_000);
        let mut published = 0u64;
        for marker in 1u8..=4 {
            let jpeg = [0xFF, 0xD8, marker, 0xFF, 0xD9];
            if p.consider(&jpeg, &dest, p.pending_provenance()) { published += 1; }
        }
        assert_eq!(published, 4, "a 1 Hz progress line must not collapse 4 Hz snapshots");
        assert_eq!(p.frames, 4);
        match p.serve() {
            Serve::Fresh { seq, pts_us, mono_us, capture_id, clock_source, session_id, mono_origin_ns, .. } => {
                assert_eq!(seq, 4);
                assert_eq!(pts_us, Some(40_000));
                assert_ne!(mono_us, 40_000);
                assert_eq!(clock_source, "monotonic_ns");
                assert_eq!(session_id, "match-session");
                assert_ne!(mono_origin_ns, 0);
                assert!(capture_id.starts_with("1:4:"));
            }
            other => panic!("the fourth snapshot is the one being served, got {other:?}"),
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_nonzero_recorder_origin_survives_reopen_and_is_not_media_pts() {
        let now = monotonic_ns();
        assert!(now > 20_000_000_000, "CLOCK_MONOTONIC did not yield a usable origin: {now}");
        let dir = std::env::temp_dir().join(format!("qorcap-snap-origin-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let dest = dir.join("snapshot.jpg");
        let mut p = Publisher::new();
        p.bind_session("match-session");
        let first = [0xFF, 0xD8, 0x61, 0xFF, 0xD9];
        let second = [0xFF, 0xD8, 0x62, 0xFF, 0xD9];
        p.begin_epoch(&[]);
        p.set_recorder_origin_ns(now - 8_000_000_000);
        p.mark_producer_started();
        p.note_progress(1, 40_000);
        assert!(p.consider(&first, &dest, p.pending_provenance()));
        let (mono_a, origin_a) = match p.serve() {
            Serve::Fresh { mono_us, mono_origin_ns, pts_us, .. } => {
                assert_eq!(pts_us, Some(40_000));
                assert_ne!(mono_us, 40_000);
                assert_eq!(mono_origin_ns, now - 8_000_000_000);
                (mono_us, mono_origin_ns)
            }
            other => panic!("first epoch did not publish, got {other:?}"),
        };
        p.begin_epoch(&first);
        p.set_recorder_origin_ns(now - 1_000_000_000);
        p.mark_producer_started();
        p.note_progress(1, 40_000);
        assert!(!p.consider(&first, &dest, p.pending_provenance()), "reopen still holds the previous JPEG");
        assert!(p.consider(&second, &dest, p.pending_provenance()));
        match p.serve() {
            Serve::Fresh { mono_us, mono_origin_ns, pts_us, epoch, .. } => {
                assert_eq!(epoch, 2);
                assert_eq!(pts_us, Some(40_000));
                assert_ne!(mono_us, 40_000);
                assert_eq!(mono_origin_ns, now - 1_000_000_000);
                assert_ne!(mono_origin_ns, origin_a);
                assert_ne!(mono_us, mono_a);
            }
            other => panic!("reopened epoch did not publish the new JPEG, got {other:?}"),
        }
        let _ = std::fs::remove_dir_all(&dir);
    }
}
