//! Publish a complete JPEG without ffmpeg's atomic rename.
//!
//! ffmpeg `-atomic_writing` renames over `snapshot.jpg` while the reader may still have that
//! file open. On Windows that rename fails and the live frame stops updating. Capture writes
//! `snapshot.incoming.jpg` instead. This publisher accepts only a complete JPEG (SOI through
//! EOI), keeps those bytes in memory for `/snapshot.jpg`, and replaces the public file with
//! a short retry. A failed replace does not drop the in-memory frame.
use std::path::Path;

pub fn jpeg_complete(bytes: &[u8]) -> bool {
    bytes.len() >= 4 && bytes[0] == 0xFF && bytes[1] == 0xD8 && bytes[bytes.len() - 2] == 0xFF && bytes[bytes.len() - 1] == 0xD9
}

/// A frozen frame older than this is not a fresh capture.
pub const SNAPSHOT_MAX_AGE_MS: u64 = 2_000;

pub enum Serve {
    Fresh { bytes: Vec<u8>, source_ms: u64, generation: u64 },
    Stale,
    /// A capture reopen cleared the previous frame. The old file is not served as new.
    Superseded,
    /// The publisher has never held a frame. The caller may read the file, as before.
    Absent,
}

pub struct Publisher {
    pub frames: u64,
    pub failures: u64,
    memory: Option<Vec<u8>>,
    source_ms: Option<u64>,
    generation: u64,
    require_new: bool,
    file_matches: bool,
    noted_fail: bool,
}

impl Publisher {
    pub fn new() -> Self {
        Self { frames: 0, failures: 0, memory: None, source_ms: None, generation: 0, require_new: false, file_matches: true, noted_fail: false }
    }
    pub fn memory(&self) -> Option<Vec<u8>> { self.memory.clone() }

    /// Drop the cached frame. The next serve stays closed until a new complete JPEG arrives.
    pub fn invalidate(&mut self) {
        self.memory = None;
        self.source_ms = None;
        self.generation = self.generation.saturating_add(1);
        self.require_new = true;
        self.file_matches = false;
    }

    /// Store a new complete JPEG. Identical bytes keep their original source time.
    pub fn consider(&mut self, incoming: &[u8], dest: &Path, source_ms: u64) -> bool {
        if !jpeg_complete(incoming) { return false; }
        let changed = self.memory.as_deref() != Some(incoming);
        if changed {
            self.memory = Some(incoming.to_vec());
            self.source_ms = Some(source_ms);
            self.generation = self.generation.saturating_add(1);
            self.frames = self.frames.saturating_add(1);
            self.require_new = false;
            self.file_matches = false;
            self.noted_fail = false;
        }
        if !self.file_matches {
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
        changed
    }

    pub fn serve(&self, now_ms: u64) -> Serve {
        if self.require_new && self.memory.is_none() { return Serve::Superseded; }
        let Some(bytes) = self.memory.clone() else { return Serve::Absent; };
        let Some(source_ms) = self.source_ms else { return Serve::Stale; };
        let ahead = source_ms > now_ms.saturating_add(250);
        let old = now_ms.saturating_sub(source_ms) > SNAPSHOT_MAX_AGE_MS;
        if ahead || old { return Serve::Stale; }
        Serve::Fresh { bytes, source_ms, generation: self.generation }
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

    #[test]
    fn partial_jpeg_is_not_published_and_a_complete_one_replaces_the_file() {
        assert!(!jpeg_complete(&[0xFF, 0xD8, 0x00]));
        assert!(!jpeg_complete(&[0xFF, 0xD8, 0x00, 0xD9]));
        assert!(jpeg_complete(&[0xFF, 0xD8, 0x11, 0xFF, 0xD9]));
        let dir = std::env::temp_dir().join(format!("qorcap-snap-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let dest = dir.join("snapshot.jpg");
        let mut p = Publisher::new();
        assert!(!p.consider(&[0xFF, 0xD8, 0x01], &dest, 1_000));
        assert!(p.memory().is_none());
        assert!(matches!(p.serve(1_000), Serve::Absent));
        assert!(!dest.exists());
        let jpeg = [0xFF, 0xD8, 0x22, 0xFF, 0xD9];
        assert!(p.consider(&jpeg, &dest, 1_000));
        assert_eq!(std::fs::read(&dest).unwrap(), jpeg);
        assert_eq!(p.memory().unwrap(), jpeg);
        assert_eq!(p.frames, 1);
        assert_eq!(p.failures, 0);
        match p.serve(1_500) {
            Serve::Fresh { source_ms, generation, .. } => { assert_eq!(source_ms, 1_000); assert_eq!(generation, 1); }
            _ => panic!("expected a fresh frame, got a non-fresh serve"),
        }
        assert!(!p.consider(&jpeg, &dest, 5_000), "the same bytes do not get a new source time");
        match p.serve(5_000) {
            Serve::Stale => {}
            _ => panic!("a repeated frame must not look fresh"),
        }
        p.invalidate();
        assert!(matches!(p.serve(5_000), Serve::Superseded));
        assert!(dest.exists(), "invalidation does not delete the file; it stops serving it");
        let newer = [0xFF, 0xD8, 0x33, 0xFF, 0xD9];
        assert!(p.consider(&newer, &dest, 5_100));
        match p.serve(5_200) {
            Serve::Fresh { source_ms, bytes, .. } => { assert_eq!(source_ms, 5_100); assert_eq!(bytes, newer); }
            _ => panic!("a new capture is fresh"),
        }
        let _ = std::fs::remove_dir_all(&dir);
    }
}
