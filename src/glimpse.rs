//! Opt-in, read-only frame tap for the advisory Glimpse shadow watcher (v0.3.21).
//!
//! `GET /frame/scorebug` hands out the newest snapshot JPEG that QorCap's own scorebug reader already
//! pulled through `GET /snapshot.jpg`. The tap opens no files, starts no thread, decodes nothing, and
//! never blocks: the reader's request stores its bytes with `try_lock` (a busy tap just skips that copy),
//! and the watcher's request only clones an `Arc`. A slow, crashed or absent watcher therefore cannot touch
//! capture, the snapshot file's atomic rename, or the reader. The watcher crops the scorebug itself.
//!
//! Off unless `record --glimpse-frame` or `QORCAP_GLIMPSE_FRAME=1`. Rate-limited (one frame per
//! `MIN_INTERVAL_MS` across all callers) and stale frames are refused, so an old frame is never presented
//! as live. Nothing the watcher sends back can reach QorCap: the endpoint is GET-only and has no body.
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

pub const ENV: &str = "QORCAP_GLIMPSE_FRAME";
/// At most one frame per this many ms, across all callers.
pub const MIN_INTERVAL_MS: u64 = 2_000;
/// A frame older than this is refused (the reader stopped pulling: menu pause, stop, no signal).
pub const MAX_AGE_MS: u64 = 3_000;

#[derive(Clone, Debug)]
pub struct Frame { pub jpeg: Arc<Vec<u8>>, pub wall_ms: u64, pub seq: u64 }

pub struct FrameTap {
    pub enabled: bool,
    cur: Mutex<Option<Frame>>,
    seq: AtomicU64,
    last_served_ms: AtomicU64,
    pub served: AtomicU64,
    pub throttled: AtomicU64,
}

#[derive(Debug, PartialEq, Eq)]
pub enum TapErr { Off, Throttled { retry_ms: u64 }, NoFrame, Stale { age_ms: u64 } }

pub fn env_enabled() -> bool {
    std::env::var(ENV).map(|v| matches!(v.trim(), "1" | "true" | "yes" | "on")).unwrap_or(false)
}

impl FrameTap {
    pub fn new(enabled: bool) -> Self {
        Self { enabled, cur: Mutex::new(None), seq: AtomicU64::new(0), last_served_ms: AtomicU64::new(0),
               served: AtomicU64::new(0), throttled: AtomicU64::new(0) }
    }
    /// Called by the `/snapshot.jpg` handler after it has already read a valid JPEG. Never blocks.
    pub fn offer(&self, jpeg: &[u8], now_ms: u64) {
        if !self.enabled { return; }
        if let Ok(mut g) = self.cur.try_lock() {
            let seq = self.seq.fetch_add(1, Ordering::Relaxed) + 1;
            *g = Some(Frame { jpeg: Arc::new(jpeg.to_vec()), wall_ms: now_ms, seq });
        }
    }
    /// The newest frame for the watcher, or why not.
    pub fn take(&self, now_ms: u64) -> Result<Frame, TapErr> {
        if !self.enabled { return Err(TapErr::Off); }
        let f = match self.cur.try_lock() { Ok(g) => g.clone(), Err(_) => None }.ok_or(TapErr::NoFrame)?;
        let age = now_ms.saturating_sub(f.wall_ms);
        if age > MAX_AGE_MS { return Err(TapErr::Stale { age_ms: age }); }
        let last = self.last_served_ms.load(Ordering::SeqCst);
        if last != 0 && now_ms.saturating_sub(last) < MIN_INTERVAL_MS {
            self.throttled.fetch_add(1, Ordering::Relaxed);
            return Err(TapErr::Throttled { retry_ms: MIN_INTERVAL_MS - now_ms.saturating_sub(last) });
        }
        if self.last_served_ms.compare_exchange(last, now_ms.max(1), Ordering::SeqCst, Ordering::SeqCst).is_err() {
            self.throttled.fetch_add(1, Ordering::Relaxed);
            return Err(TapErr::Throttled { retry_ms: MIN_INTERVAL_MS });
        }
        self.served.fetch_add(1, Ordering::Relaxed);
        Ok(f)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    const J: [u8; 4] = [0xFF, 0xD8, 0xFF, 0xD9];
    #[test]
    fn off_by_default_and_stores_nothing() {
        let t = FrameTap::new(false);
        t.offer(&J, 1_000);
        assert_eq!(t.take(1_000).unwrap_err(), TapErr::Off);
        assert!(t.cur.lock().unwrap().is_none());
    }
    #[test]
    fn serves_latest_rate_limited_and_refuses_stale() {
        let t = FrameTap::new(true);
        assert_eq!(t.take(1_000).unwrap_err(), TapErr::NoFrame);
        t.offer(&J, 10_000);
        t.offer(&[0xFF, 0xD8, 1, 0xFF, 0xD9], 10_250);
        let f = t.take(10_300).unwrap();
        assert_eq!((f.seq, f.wall_ms, f.jpeg.len()), (2, 10_250, 5));
        assert!(matches!(t.take(10_900), Err(TapErr::Throttled { .. })));
        t.offer(&J, 12_200);
        assert_eq!(t.take(12_300).unwrap().seq, 3);
        assert_eq!(t.take(12_300 + MIN_INTERVAL_MS + MAX_AGE_MS).unwrap_err(), TapErr::Stale { age_ms: MIN_INTERVAL_MS + MAX_AGE_MS + 100 });
        assert_eq!(t.served.load(Ordering::Relaxed), 2);
        assert_eq!(t.throttled.load(Ordering::Relaxed), 1);
    }
    #[test]
    fn a_held_tap_never_blocks_the_reader() {
        let t = FrameTap::new(true);
        t.offer(&J, 5_000);
        let g = t.cur.lock().unwrap(); // watcher side mid-clone
        t.offer(&[0xFF, 0xD8, 9, 0xFF, 0xD9], 5_250); // returns immediately, skips this copy
        drop(g);
        assert_eq!(t.take(5_300).unwrap().seq, 1);
    }
}
