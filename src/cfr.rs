//! Constant-frame-rate pacing: maps irregular source frame arrivals onto a
//! fixed output grid, repeating the last frame on gaps and dropping extras.
//! ffmpeg does the pixel work (`-fps_mode cfr`); this module is the model we
//! use for expected-frame accounting in /status and is unit-tested directly.

#[derive(Debug, Clone)]
pub struct CfrPacer {
    period_us: u64,
    next_slot: u64, // index of next output slot
    origin_us: Option<u64>,
    pub emitted: u64,
    pub duplicated: u64,
    pub dropped: u64,
}

impl CfrPacer {
    pub fn new(fps: u32) -> Self {
        Self { period_us: 1_000_000 / fps.max(1) as u64, next_slot: 0, origin_us: None, emitted: 0, duplicated: 0, dropped: 0 }
    }
    /// A source frame arrived at `t_us`. Returns how many output frames to
    /// emit: 0 = drop, 1 = normal, >1 = previous frame repeated (n-1) times first.
    pub fn on_frame(&mut self, t_us: u64) -> u64 {
        let origin = *self.origin_us.get_or_insert(t_us);
        let slot = (t_us.saturating_sub(origin) + self.period_us / 2) / self.period_us;
        if slot < self.next_slot { self.dropped += 1; return 0; }
        let n = slot - self.next_slot + 1;
        self.duplicated += n - 1;
        self.emitted += n;
        self.next_slot = slot + 1;
        n
    }
    /// Frames that should exist by `t_us` (for "behind by" stats / repeat on stall).
    pub fn expected_by(&self, t_us: u64) -> u64 {
        self.origin_us.map(|o| (t_us.saturating_sub(o)) / self.period_us + 1).unwrap_or(0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn steady() { let mut p = CfrPacer::new(30); for i in 0..90 { assert_eq!(p.on_frame(i * 33_333), 1); } assert_eq!(p.emitted, 90); }
    #[test]
    fn gap_repeats_last_frame() {
        let mut p = CfrPacer::new(30);
        p.on_frame(0); p.on_frame(33_333);
        assert_eq!(p.on_frame(166_666), 4); // slots 2,3 repeated, 4 = new
        assert_eq!(p.duplicated, 3 - 0); assert_eq!(p.emitted, 6);
    }
    #[test]
    fn burst_drops() {
        let mut p = CfrPacer::new(30);
        p.on_frame(0); assert_eq!(p.on_frame(5_000), 0); assert_eq!(p.dropped, 1);
        assert_eq!(p.on_frame(33_000), 1);
        assert_eq!(p.expected_by(1_000_000), 31);
    }
}
