//! Encoder progress lines for qorcap.log. Logging only.
//!
//! The capture ffmpeg already writes `-progress pipe:2` key=value blocks every second
//! (`-stats_period 1`), and the engine's stderr thread already drains them. This module
//! condenses those blocks into at most one log line per interval so a frame-drop
//! episode can be read as encoder slowness (fps/speed falling) or as a capture-side
//! stall (fps/speed steady while frames drop).
//!
//! Values are copied from ffmpeg after a parse check. A missing or unparsable field
//! is left out of the line, never filled in. Nothing here panics on bad input.

/// Minimum time between two progress lines.
pub const INTERVAL_MS: u64 = 5_000;

/// Fields from one ffmpeg progress report.
#[derive(Debug, Default, Clone, PartialEq)]
pub struct Report {
    pub frame: Option<u64>,
    pub fps: Option<String>,
    pub speed: Option<String>,
    pub dup: Option<u64>,
    pub drop: Option<u64>,
}

fn uint(v: &str) -> Option<u64> { v.trim().parse().ok() }

fn fps(v: &str) -> Option<String> {
    let v = v.trim();
    match v.parse::<f64>() { Ok(f) if f.is_finite() && f >= 0.0 => Some(v.to_string()), _ => None }
}

/// `1.00x` (or ` 1.00x`). `N/A` and anything else that is not a finite number before `x` is dropped.
fn speed(v: &str) -> Option<String> {
    let v = v.trim();
    let n = v.strip_suffix('x').unwrap_or(v).trim();
    match n.parse::<f64>() { Ok(f) if f.is_finite() && f >= 0.0 => Some(format!("{n}x")), _ => None }
}

impl Report {
    /// Apply one `-progress` key. Returns true for a key this report uses.
    pub fn set(&mut self, key: &str, value: &str) -> bool {
        match key {
            "frame" => self.frame = uint(value),
            "fps" => self.fps = fps(value),
            "speed" => self.speed = speed(value),
            "dup_frames" | "dup" => self.dup = uint(value),
            "drop_frames" | "drop" => self.drop = uint(value),
            _ => return false,
        }
        true
    }

    pub fn is_empty(&self) -> bool { *self == Report::default() }

    /// `frame=1234 fps=30.0 speed=1.00x dup=3 drop=1`, present fields only.
    pub fn render(&self) -> String {
        let mut p: Vec<String> = Vec::new();
        if let Some(v) = self.frame { p.push(format!("frame={v}")); }
        if let Some(v) = &self.fps { p.push(format!("fps={v}")); }
        if let Some(v) = &self.speed { p.push(format!("speed={v}")); }
        if let Some(v) = self.dup { p.push(format!("dup={v}")); }
        if let Some(v) = self.drop { p.push(format!("drop={v}")); }
        p.join(" ")
    }
}

/// Parse an ffmpeg `-stats` line such as
/// `frame= 1234 fps= 30 q=23.0 size=  1024kB time=00:00:41.13 bitrate=204.0kbits/s dup=3 drop=1 speed=1.00x`.
/// Values may be separated from their key by spaces. Unknown keys are ignored.
pub fn parse_stats_line(line: &str) -> Report {
    let mut r = Report::default();
    // Join `key= value` into `key=value`, then split on whitespace.
    let mut joined = String::with_capacity(line.len());
    let mut after_eq = false;
    for c in line.chars() {
        if c == '=' { after_eq = true; joined.push(c); continue; }
        if after_eq && c.is_whitespace() { continue; }
        after_eq = false;
        joined.push(c);
    }
    for tok in joined.split_whitespace() {
        if let Some((k, v)) = tok.split_once('=') { r.set(k, v); }
    }
    r
}

/// Collects `-progress` blocks and stats lines; hands back a log line at most once per interval.
/// Time is injected (milliseconds on any monotonic clock) so tests need no sleep.
#[derive(Debug, Default)]
pub struct ProgressLog {
    cur: Report,
    last_ms: Option<u64>,
    interval_ms: u64,
}

impl ProgressLog {
    pub fn new(interval_ms: u64) -> Self { Self { interval_ms, ..Default::default() } }

    fn due(&mut self, now_ms: u64, force: bool) -> bool {
        let ok = force || self.last_ms.is_none_or(|t| now_ms.saturating_sub(t) >= self.interval_ms);
        if ok { self.last_ms = Some(now_ms); }
        ok
    }

    fn take_line(&mut self, now_ms: u64, force: bool, end: bool) -> Option<String> {
        let r = std::mem::take(&mut self.cur);
        if r.is_empty() || !self.due(now_ms, force) { return None; }
        Some(format!("encoder progress: {}{}", r.render(), if end { " (end)" } else { "" }))
    }

    /// Feed one `key=value` pair from `-progress`. A `progress=continue|end` key closes the block.
    /// A `frame` value that itself holds `=` is a whole stats line and is reported on its own.
    pub fn observe(&mut self, key: &str, value: &str, now_ms: u64) -> Option<String> {
        if key == "frame" && value.contains('=') {
            self.cur = parse_stats_line(&format!("{key}={value}"));
            return self.take_line(now_ms, false, false);
        }
        if key == "progress" {
            let end = value.trim() == "end";
            return self.take_line(now_ms, end, end);
        }
        self.cur.set(key, value);
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn block(p: &mut ProgressLog, frame: u64, now: u64, tail: &str) -> Option<String> {
        let text = format!("frame={frame}\nfps=59.94\nstream_0_0_q=23.0\nbitrate=6000.1kbits/s\ntotal_size=1048576\nout_time_us=41133000\nout_time=00:00:41.133000\ndup_frames=3\ndrop_frames=1\nspeed=0.998x\nprogress={tail}\n");
        let mut out = None;
        for l in text.lines() { let (k, v) = l.split_once('=').unwrap(); if let Some(s) = p.observe(k, v, now) { out = Some(s); } }
        out
    }

    #[test]
    fn progress_block_renders_one_line() {
        let mut p = ProgressLog::new(INTERVAL_MS);
        assert_eq!(block(&mut p, 1234, 0, "continue").as_deref(), Some("encoder progress: frame=1234 fps=59.94 speed=0.998x dup=3 drop=1"));
    }

    #[test]
    fn at_most_one_line_per_interval_with_injected_clock() {
        let mut p = ProgressLog::new(INTERVAL_MS);
        let mut lines = Vec::new();
        for s in 0..=20u64 { if let Some(l) = block(&mut p, s * 60, s * 1000, "continue") { lines.push((s, l)); } }
        let at: Vec<u64> = lines.iter().map(|(s, _)| *s).collect();
        assert_eq!(at, vec![0, 5, 10, 15, 20]);
        assert!(lines[1].1.contains("frame=300 "));
        // 4.999 s later is not due.
        let mut q = ProgressLog::new(INTERVAL_MS);
        assert!(block(&mut q, 1, 10_000, "continue").is_some());
        assert!(block(&mut q, 2, 14_999, "continue").is_none());
        assert!(block(&mut q, 3, 15_000, "continue").is_some());
    }

    #[test]
    fn end_block_is_always_logged() {
        let mut p = ProgressLog::new(INTERVAL_MS);
        assert!(block(&mut p, 1, 0, "continue").is_some());
        assert_eq!(block(&mut p, 9, 100, "end").as_deref(), Some("encoder progress: frame=9 fps=59.94 speed=0.998x dup=3 drop=1 (end)"));
    }

    #[test]
    fn missing_fields_are_omitted_and_blocks_do_not_carry_over() {
        let mut p = ProgressLog::new(0);
        assert!(p.observe("frame", "10", 0).is_none());
        assert!(p.observe("speed", "N/A", 0).is_none());
        assert!(p.observe("fps", "nan", 0).is_none());
        assert_eq!(p.observe("progress", "continue", 0).as_deref(), Some("encoder progress: frame=10"));
        p.observe("fps", "30.00", 1);
        assert_eq!(p.observe("progress", "continue", 1).as_deref(), Some("encoder progress: fps=30.00"), "frame from the old block must not reappear");
        // A block with nothing usable writes nothing.
        p.observe("bitrate", "N/A", 2);
        assert!(p.observe("progress", "continue", 2).is_none());
    }

    #[test]
    fn stats_line_is_parsed() {
        let r = parse_stats_line("frame= 1234 fps= 30 q=23.0 size=    2048kB time=00:00:41.13 bitrate= 408.0kbits/s dup=3 drop=1 speed=1.00x    ");
        assert_eq!(r, Report { frame: Some(1234), fps: Some("30".into()), speed: Some("1.00x".into()), dup: Some(3), drop: Some(1) });
        let r = parse_stats_line("frame=   60 fps=0.0 q=-1.0 Lsize=N/A time=00:00:02.00 bitrate=N/A speed=N/A");
        assert_eq!(r.render(), "frame=60 fps=0.0");
        let mut p = ProgressLog::new(INTERVAL_MS);
        let l = "frame= 1234 fps= 30 q=23.0 size=1kB time=00:00:41.13 bitrate=1kbits/s dup=3 drop=1 speed=1.00x";
        let (k, v) = l.split_once('=').unwrap();
        assert_eq!(p.observe(k, v, 0).as_deref(), Some("encoder progress: frame=1234 fps=30 speed=1.00x dup=3 drop=1"));
    }

    #[test]
    fn garbage_never_panics_or_invents() {
        let mut p = ProgressLog::new(0);
        for (k, v) in [("frame", "-1"), ("frame", "1e9"), ("fps", "-3"), ("fps", "inf"), ("speed", "x"), ("speed", "-1x"), ("speed", "1.0xx"),
                       ("dup_frames", "abc"), ("drop_frames", ""), ("frame", "= = ="), ("", ""), ("progress", "")] {
            if let Some(l) = p.observe(k, v, 0) { assert_eq!(l, "encoder progress: ", "nothing invented: {k}={v}"); }
        }
        assert!(p.observe("progress", "continue", 0).is_none());
        for s in ["", "=", "frame=", "frame= fps= speed=", "\u{fffd}=\u{0}", "frame=99999999999999999999999"] {
            assert!(parse_stats_line(s).is_empty(), "{s:?}");
        }
        // Large time jumps and a clock that goes backwards do not panic.
        let mut q = ProgressLog::new(INTERVAL_MS);
        q.observe("frame", "1", u64::MAX); assert!(q.observe("progress", "continue", u64::MAX).is_some());
        q.observe("frame", "2", 0); assert!(q.observe("progress", "continue", 0).is_none());
    }
}
