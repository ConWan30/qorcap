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

pub struct Publisher {
    pub frames: u64,
    pub failures: u64,
    memory: Option<Vec<u8>>,
    file_matches: bool,
    noted_fail: bool,
}

impl Publisher {
    pub fn new() -> Self { Self { frames: 0, failures: 0, memory: None, file_matches: true, noted_fail: false } }
    pub fn memory(&self) -> Option<Vec<u8>> { self.memory.clone() }

    /// Store a new complete JPEG and try to replace `dest`. A partial buffer is ignored.
    pub fn consider(&mut self, incoming: &[u8], dest: &Path) -> bool {
        if !jpeg_complete(incoming) { return false; }
        let changed = self.memory.as_deref() != Some(incoming);
        if changed {
            self.memory = Some(incoming.to_vec());
            self.frames = self.frames.saturating_add(1);
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
        assert!(!p.consider(&[0xFF, 0xD8, 0x01], &dest));
        assert!(p.memory().is_none());
        assert!(!dest.exists());
        let jpeg = [0xFF, 0xD8, 0x22, 0xFF, 0xD9];
        assert!(p.consider(&jpeg, &dest));
        assert_eq!(std::fs::read(&dest).unwrap(), jpeg);
        assert_eq!(p.memory().unwrap(), jpeg);
        assert_eq!(p.frames, 1);
        assert_eq!(p.failures, 0);
        assert!(!p.consider(&jpeg, &dest), "the same complete frame is not a new publish");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
