//! Encoder progress lines for qorcap.log. Logging only.
//!
//! The capture ffmpeg already writes `-progress pipe:2` key=value blocks every second
//! (`-stats_period 1`), and the engine's stderr thread already drains them. This module
//! condenses those blocks into at most one log line per interval, so the output rate
//! (fps/speed) can be read next to any drop warnings from the same stretch. These lines
//! alone do not say whether a slow or dropping stretch came from the encoder, the capture
//! input, or QorCap reading the pipe late: the cause stays UNMEASURED. The `drop=` field is
//! ffmpeg's own output-side `drop_frames` counter. With `-vf fps=N -fps_mode cfr` input-side
//! loss is filled with duplicates before it, so `drop=` is not a frame-loss measure here.
//!
//! Values are copied from ffmpeg after a parse check. A missing or unparsable field
//! is left out of the line, never filled in. Nothing here panics on bad input.
//!
//! Pace telemetry (logging only, default on, all lanes). Between two logged lines at least
//! `MIN_WINDOW_MS` apart the line also carries `win_ms`, `win_fps` (frame delta / wall delta),
//! `pace` (media-time delta from `out_time_us` / wall delta, 1.000 = real time at any output rate)
//! and `lag_ms` (wall time minus media time since the first *logged* line whose block carried
//! `out_time_us`, so it is relative to that line, not absolute; normally that is the first block).
//! Wall time is when QorCap read the block from stderr, so a late reader looks the same as a slow
//! encoder: the WARN names output pace, not a cause.
//! Two windows in a row below `SLOW_PACE` (59 of 60 fps) covering at least `WARN_AFTER_MS` write one
//! WARN per episode; `RECOVER_MS` of windows at or above `OK_PACE` closes it with one summary line.
//! A window without a pace reading (no `out_time_us` at either end) neither extends nor breaks a
//! slow streak or a recovery; `WARN_AFTER_MS` and `RECOVER_MS` count measured windows only.
//! DirectShow "real-time buffer ... frame dropped!" warning lines and the `Last message repeated N
//! times` lines after them are counted (`observe_line`) and summarised every `DROP_SUMMARY_MS` when
//! any arrived. Those are warning messages, not a dropped-frame count (dropped frames stay
//! UNMEASURED; repeat sums are a lower bound, see src/capstop.rs). Nothing here changes the capture
//! command, its frame rate or its buffers, and no cause is inferred.

/// Minimum time between two progress lines.
pub const INTERVAL_MS: u64 = 5_000;
/// A rate/pace window shorter than this is not reported (a forced end line can follow 100 ms after the last).
pub const MIN_WINDOW_MS: u64 = 1_000;
/// Below this media pace a window is slow: 59 of 60 fps, the same ratio at any output rate.
pub const SLOW_PACE: f64 = 59.0 / 60.0;
/// At or above this media pace a window counts toward recovery: 59.5 of 60 fps.
pub const OK_PACE: f64 = 59.5 / 60.0;
/// Slow windows in a row before the episode WARN.
pub const WARN_MIN_WINDOWS: u32 = 2;
/// Wall time those slow windows must cover (two ~5 s windows; ffmpeg blocks land about 1 s apart).
pub const WARN_AFTER_MS: u64 = 9_500;
/// Wall time of windows at or above `OK_PACE` that closes an episode.
pub const RECOVER_MS: u64 = 30_000;
/// Minimum time between two drop-message summary lines.
pub const DROP_SUMMARY_MS: u64 = 30_000;

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

/// One measured window between two logged progress lines.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Window {
    pub ms: u64,
    pub frames: u64,
    pub fps: f64,
    /// Media-time delta / wall delta. `None` when `out_time_us` was missing at either end.
    pub pace: Option<f64>,
    /// Wall minus media time since the first logged line whose block had `out_time_us`. `None` when not measurable.
    pub lag_ms: Option<i64>,
}

/// DirectShow input-drop warning messages seen on the capture ffmpeg's stderr.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct DropCounts {
    pub video_lines: u64,
    pub audio_lines: u64,
    pub other_lines: u64,
    /// Sum of N from `Last message repeated N times` right after a drop line (lower bound).
    pub repeats: u64,
    /// Repeat lines after a drop line whose N could not be read.
    pub unread_repeats: u64,
}

impl DropCounts {
    pub fn lines(&self) -> u64 { self.video_lines.saturating_add(self.audio_lines).saturating_add(self.other_lines) }
    /// Lines plus repeat sums: warning messages, not frames.
    pub fn messages(&self) -> u64 { self.lines().saturating_add(self.repeats) }
    fn since(&self, o: &DropCounts) -> DropCounts {
        DropCounts {
            video_lines: self.video_lines.saturating_sub(o.video_lines),
            audio_lines: self.audio_lines.saturating_sub(o.audio_lines),
            other_lines: self.other_lines.saturating_sub(o.other_lines),
            repeats: self.repeats.saturating_sub(o.repeats),
            unread_repeats: self.unread_repeats.saturating_sub(o.unread_repeats),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
struct Anchor { frame: u64, out_us: Option<u64>, now_ms: u64 }

#[derive(Debug, Clone, PartialEq)]
struct Episode { n: u64, start_ms: u64, min_fps: f64, min_pace: f64, max_lag_ms: Option<i64>, drops_at_start: u64 }

fn max_lag(a: Option<i64>, b: Option<i64>) -> Option<i64> {
    match (a, b) { (Some(x), Some(y)) => Some(x.max(y)), (x, None) => x, (None, y) => y }
}

fn lag_text(l: Option<i64>) -> String { l.map(|v| v.to_string()).unwrap_or_else(|| "UNMEASURED".into()) }

/// Collects `-progress` blocks and stats lines; hands back a log line at most once per interval.
/// Time is injected (milliseconds on any monotonic clock) so tests need no sleep.
/// The pace WARN, the episode summary and the drop-message summary are written with `log` directly.
#[derive(Debug, Default)]
pub struct ProgressLog {
    cur: Report,
    cur_out_us: Option<u64>,
    last_ms: Option<u64>,
    interval_ms: u64,
    prev: Option<Anchor>,
    lag0: Option<(u64, u64)>,
    slow_windows: u32,
    slow_ms: u64,
    streak_min_fps: f64,
    streak_min_pace: f64,
    streak_max_lag: Option<i64>,
    ok_ms: u64,
    episode: Option<Episode>,
    episodes: u64,
    drops: DropCounts,
    drops_at_summary: DropCounts,
    summary_ms: Option<u64>,
    last_was_drop: bool,
    #[cfg(test)]
    notes: Vec<(bool, String)>,
}

impl ProgressLog {
    pub fn new(interval_ms: u64) -> Self { Self { interval_ms, ..Default::default() } }

    /// Input-drop warning messages counted so far.
    pub fn drops(&self) -> &DropCounts { &self.drops }

    fn due(&mut self, now_ms: u64, force: bool) -> bool {
        let ok = force || self.last_ms.is_none_or(|t| now_ms.saturating_sub(t) >= self.interval_ms);
        if ok { self.last_ms = Some(now_ms); }
        ok
    }

    fn note(&mut self, warn: bool, text: String) {
        if warn { log::warn!("{text}"); } else { log::info!("{text}"); }
        #[cfg(test)]
        self.notes.push((warn, text));
    }

    fn take_line(&mut self, now_ms: u64, force: bool, end: bool) -> Option<String> {
        let r = std::mem::take(&mut self.cur);
        let out_us = self.cur_out_us.take();
        if r.is_empty() || !self.due(now_ms, force) { return None; }
        let mut line = format!("encoder progress: {}", r.render());
        if let Some(frame) = r.frame {
            if let Some(w) = self.window(frame, out_us, now_ms) {
                line.push_str(&format!(" win_ms={} win_fps={:.1}", w.ms, w.fps));
                if let Some(p) = w.pace { line.push_str(&format!(" pace={p:.3}")); }
                if let Some(l) = w.lag_ms { line.push_str(&format!(" lag_ms={l}")); }
                self.track(&w, now_ms);
            }
        }
        self.drop_summary(now_ms, end);
        if end {
            if let Some(e) = self.episode.take() { self.close_episode(e, now_ms, "still below real time when the encoder ended"); }
            line.push_str(" (end)");
        }
        Some(line)
    }

    /// The window since the last reported anchor. A window shorter than `MIN_WINDOW_MS` keeps the old anchor.
    fn window(&mut self, frame: u64, out_us: Option<u64>, now_ms: u64) -> Option<Window> {
        if self.lag0.is_none() { if let Some(o) = out_us { self.lag0 = Some((now_ms, o)); } }
        let a = Anchor { frame, out_us, now_ms };
        let Some(p) = self.prev else { self.prev = Some(a); return None; };
        let Some(ms) = now_ms.checked_sub(p.now_ms) else { self.prev = Some(a); return None; };
        if ms < MIN_WINDOW_MS { return None; }
        self.prev = Some(a);
        // A frame counter that went backwards is a new writer or garbage: no window.
        let frames = frame.checked_sub(p.frame)?;
        let fps = frames as f64 * 1000.0 / ms as f64;
        if !fps.is_finite() { return None; }
        let pace = match (p.out_us, out_us) {
            (Some(x), Some(y)) if y >= x => Some((y - x) as f64 / 1000.0 / ms as f64).filter(|v| v.is_finite()),
            _ => None,
        };
        let lag_ms = match (self.lag0, out_us) {
            (Some((t0, o0)), Some(o)) if o >= o0 => {
                let wall = i64::try_from(now_ms.saturating_sub(t0)).ok();
                let media = i64::try_from((o - o0) / 1000).ok();
                match (wall, media) { (Some(w), Some(m)) => w.checked_sub(m), _ => None }
            }
            _ => None,
        };
        Some(Window { ms, frames, fps, pace, lag_ms })
    }

    /// A window without a pace reading returns here: it neither extends nor breaks a streak or a recovery.
    fn track(&mut self, w: &Window, now_ms: u64) {
        let Some(p) = w.pace else { return };
        if let Some(e) = self.episode.as_mut() {
            e.min_fps = e.min_fps.min(w.fps);
            e.min_pace = e.min_pace.min(p);
            e.max_lag_ms = max_lag(e.max_lag_ms, w.lag_ms);
        }
        if p < SLOW_PACE {
            if self.slow_windows == 0 {
                self.streak_min_fps = w.fps;
                self.streak_min_pace = p;
                self.streak_max_lag = w.lag_ms;
            } else {
                self.streak_min_fps = self.streak_min_fps.min(w.fps);
                self.streak_min_pace = self.streak_min_pace.min(p);
                self.streak_max_lag = max_lag(self.streak_max_lag, w.lag_ms);
            }
            self.slow_windows = self.slow_windows.saturating_add(1);
            self.slow_ms = self.slow_ms.saturating_add(w.ms);
            self.ok_ms = 0;
            if self.episode.is_none() && self.slow_windows >= WARN_MIN_WINDOWS && self.slow_ms >= WARN_AFTER_MS {
                self.episodes = self.episodes.saturating_add(1);
                let e = Episode {
                    n: self.episodes, start_ms: now_ms.saturating_sub(self.slow_ms), min_fps: self.streak_min_fps,
                    min_pace: self.streak_min_pace, max_lag_ms: self.streak_max_lag, drops_at_start: self.drops.messages(),
                };
                let text = format!(
                    "output pace below real time (cause UNMEASURED; capture settings unchanged): episode {} after {} slow windows over {} ms (pace below {:.3}); min win_fps={:.1} min pace={:.3} lag_ms={}.",
                    e.n, self.slow_windows, self.slow_ms, SLOW_PACE, e.min_fps, e.min_pace, lag_text(e.max_lag_ms));
                self.episode = Some(e);
                self.note(true, text);
            }
        } else {
            self.slow_windows = 0;
            self.slow_ms = 0;
            if p >= OK_PACE { self.ok_ms = self.ok_ms.saturating_add(w.ms); } else { self.ok_ms = 0; }
            if self.ok_ms >= RECOVER_MS {
                if let Some(e) = self.episode.take() { self.close_episode(e, now_ms, "back to real time"); }
            }
        }
    }

    fn close_episode(&mut self, e: Episode, now_ms: u64, why: &str) {
        let drops = self.drops.messages().saturating_sub(e.drops_at_start);
        let text = format!(
            "output pace episode {} closed ({why}; cause UNMEASURED; capture settings unchanged): {} ms from first slow window to this line; min win_fps={:.1} min pace={:.3} max lag_ms={} input-drop messages={} (messages, not frames).",
            e.n, now_ms.saturating_sub(e.start_ms), e.min_fps, e.min_pace, lag_text(e.max_lag_ms), drops);
        self.note(false, text);
    }

    fn drop_summary(&mut self, now_ms: u64, end: bool) {
        let t = match self.summary_ms {
            Some(t) => t,
            None if end => now_ms,
            None => { self.summary_ms = Some(now_ms); return; }
        };
        if !end && now_ms.saturating_sub(t) < DROP_SUMMARY_MS { return; }
        self.summary_ms = Some(now_ms);
        let d = self.drops.since(&self.drops_at_summary);
        self.drops_at_summary = self.drops.clone();
        if d.messages() == 0 && d.unread_repeats == 0 { return; }
        let all = self.drops.clone();
        let text = format!(
            "encoder input-drop messages in the last {} ms: video_lines={} audio_lines={} other_lines={} repeats={} unread_repeats={}; since start: lines={} repeats={} messages={}. Warning messages, not frames: dropped frames UNMEASURED, repeats a lower bound.",
            now_ms.saturating_sub(t), d.video_lines, d.audio_lines, d.other_lines, d.repeats, d.unread_repeats, all.lines(), all.repeats, all.messages());
        self.note(false, text);
    }

    /// Feed one `key=value` pair from `-progress`. A `progress=continue|end` key closes the block.
    /// A `frame` value that itself holds `=` is a whole stats line and is reported on its own.
    pub fn observe(&mut self, key: &str, value: &str, now_ms: u64) -> Option<String> {
        if key == "frame" && value.contains('=') {
            // A stats line ends a drop-warning repeat run, as in src/capstop.rs.
            self.last_was_drop = false;
            self.cur = parse_stats_line(&format!("{key}={value}"));
            self.cur_out_us = None;
            return self.take_line(now_ms, false, false);
        }
        if key == "progress" {
            let end = value.trim() == "end";
            return self.take_line(now_ms, end, end);
        }
        if key == "out_time_us" { self.cur_out_us = uint(value); return None; }
        self.cur.set(key, value);
        None
    }

    /// One stderr line that is not a `-progress` key: counts DirectShow input-drop warnings and the
    /// `Last message repeated N times` lines that follow them. Counting only; nothing is logged here.
    pub fn observe_line(&mut self, line: &str) {
        if crate::capstop::is_input_drop(line) {
            let l = line.to_ascii_lowercase();
            if l.contains("[video input]") { self.drops.video_lines = self.drops.video_lines.saturating_add(1); }
            else if l.contains("[audio input]") { self.drops.audio_lines = self.drops.audio_lines.saturating_add(1); }
            else { self.drops.other_lines = self.drops.other_lines.saturating_add(1); }
            self.last_was_drop = true;
            return;
        }
        if let Some(n) = crate::capstop::repeat_count(line) {
            if self.last_was_drop {
                match n {
                    Some(n) => self.drops.repeats = self.drops.repeats.saturating_add(n),
                    None => self.drops.unread_repeats = self.drops.unread_repeats.saturating_add(1),
                }
            }
            return;
        }
        if !line.trim().is_empty() { self.last_was_drop = false; }
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

    // ---- pace telemetry ----

    /// One `-progress` block: frame, optional out_time_us, speed, then `progress=<tail>`.
    fn pb(p: &mut ProgressLog, frame: u64, out_us: Option<u64>, now: u64, tail: &str) -> Option<String> {
        assert!(p.observe("frame", &frame.to_string(), now).is_none());
        if let Some(o) = out_us { assert!(p.observe("out_time_us", &o.to_string(), now).is_none()); }
        assert!(p.observe("speed", "1x", now).is_none());
        p.observe("progress", tail, now)
    }

    /// One block per wall second from `from_s` to `to_s`, `per_s` frames each, media time at 60 fps CFR.
    fn run(p: &mut ProgressLog, f: &mut u64, from_s: u64, to_s: u64, per_s: u64) -> Vec<String> {
        let mut out = Vec::new();
        for s in from_s..=to_s {
            *f += per_s;
            if let Some(l) = pb(p, *f, Some(*f * 1_000_000 / 60), s * 1000, "continue") { out.push(l); }
        }
        out
    }

    fn warns(p: &ProgressLog) -> Vec<&str> { p.notes.iter().filter(|n| n.0).map(|n| n.1.as_str()).collect() }
    fn infos(p: &ProgressLog) -> Vec<&str> { p.notes.iter().filter(|n| !n.0).map(|n| n.1.as_str()).collect() }

    #[test]
    fn real_time_window_reports_rate_pace_and_zero_lag() {
        let mut p = ProgressLog::new(INTERVAL_MS);
        let mut f = 0;
        let lines = run(&mut p, &mut f, 0, 10, 60);
        assert_eq!(lines[0], "encoder progress: frame=60 speed=1x", "first line has no window yet");
        assert_eq!(lines[1], "encoder progress: frame=360 speed=1x win_ms=5000 win_fps=60.0 pace=1.000 lag_ms=0");
        assert_eq!(lines.len(), 3);
        assert!(p.notes.is_empty());
    }

    #[test]
    fn slow_window_line_matches_hand_numbers() {
        let mut p = ProgressLog::new(INTERVAL_MS);
        let mut f = 0;
        let lines = run(&mut p, &mut f, 0, 5, 55);
        // 275 frames in 5000 ms; media 5500000-916666 us = 4583 ms; lag 5000-4583 = 417 ms.
        assert_eq!(lines[1], "encoder progress: frame=330 speed=1x win_ms=5000 win_fps=55.0 pace=0.917 lag_ms=417");
        assert!(p.notes.is_empty(), "one slow window does not warn");
    }

    #[test]
    fn warn_once_per_episode_after_two_slow_windows_then_recover_and_warn_again() {
        let mut p = ProgressLog::new(INTERVAL_MS);
        let mut f = 0;
        run(&mut p, &mut f, 0, 9, 55);
        assert!(warns(&p).is_empty(), "one slow window (0-5 s) is not enough");
        run(&mut p, &mut f, 10, 10, 55);
        assert_eq!(warns(&p).len(), 1, "second slow window at 10 s opens the episode");
        assert!(warns(&p)[0].starts_with("output pace below real time (cause UNMEASURED; capture settings unchanged): episode 1 after 2 slow windows over 10000 ms"), "{}", warns(&p)[0]);
        assert!(!warns(&p)[0].contains("encoder below"));
        run(&mut p, &mut f, 11, 60, 55);
        assert_eq!(warns(&p).len(), 1, "still one WARN for the whole episode");
        // Catch up at 65 fps: 30 s of windows at or above OK_PACE closes it.
        run(&mut p, &mut f, 61, 85, 65);
        assert!(infos(&p).is_empty(), "25 s of recovery is not enough");
        run(&mut p, &mut f, 86, 90, 65);
        assert_eq!(infos(&p).len(), 1);
        assert!(infos(&p)[0].starts_with("output pace episode 1 closed (back to real time; cause UNMEASURED; capture settings unchanged)"), "{}", infos(&p)[0]);
        assert!(infos(&p)[0].contains("min win_fps=55.0"));
        // A new slow stretch is a new episode.
        run(&mut p, &mut f, 91, 101, 50);
        assert_eq!(warns(&p).len(), 2);
        assert!(warns(&p)[1].starts_with("output pace below real time (cause UNMEASURED; capture settings unchanged): episode 2 "));
    }

    #[test]
    fn a_middle_or_fast_window_breaks_the_slow_streak() {
        let mut p = ProgressLog::new(INTERVAL_MS);
        let mut f = 0;
        run(&mut p, &mut f, 0, 5, 55);   // slow
        run(&mut p, &mut f, 6, 10, 60);  // real time: streak reset
        run(&mut p, &mut f, 11, 15, 55); // slow, 1 in a row
        assert!(warns(&p).is_empty());
        // 59.2 fps sits between SLOW_PACE and OK_PACE: not slow, not recovery.
        let mut q = ProgressLog::new(INTERVAL_MS);
        let mut g = 0u64;
        for s in 0..=40u64 {
            g += if s % 5 == 0 { 56 } else { 60 }; // 296 frames per 5 s = 59.2 fps
            pb(&mut q, g, Some(g * 1_000_000 / 60), s * 1000, "continue");
        }
        assert!(q.notes.is_empty());
    }

    #[test]
    fn short_windows_and_missing_out_time_do_not_warn() {
        let mut p = ProgressLog::new(INTERVAL_MS);
        for s in 0..=30u64 {
            if let Some(l) = pb(&mut p, s * 50, None, s * 1000, "continue") {
                if s > 0 { assert!(l.contains("win_fps=50.0") && !l.contains("pace=") && !l.contains("lag_ms="), "{l}"); }
            }
        }
        assert!(p.notes.is_empty(), "no pace, no WARN");
        // A forced end line 100 ms after the last line carries no window.
        let mut q = ProgressLog::new(INTERVAL_MS);
        pb(&mut q, 60, Some(1_000_000), 0, "continue");
        assert_eq!(pb(&mut q, 66, Some(1_100_000), 100, "end").as_deref(), Some("encoder progress: frame=66 speed=1x (end)"));
    }

    #[test]
    fn end_closes_an_open_episode_honestly() {
        let mut p = ProgressLog::new(INTERVAL_MS);
        let mut f = 0;
        run(&mut p, &mut f, 0, 20, 55);
        assert_eq!(warns(&p).len(), 1);
        f += 55;
        let l = pb(&mut p, f, Some(f * 1_000_000 / 60), 21_000, "end").unwrap();
        assert!(l.ends_with(" (end)"), "{l}");
        assert_eq!(infos(&p).len(), 1);
        assert!(infos(&p)[0].contains("still below real time when the encoder ended"), "{}", infos(&p)[0]);
    }

    #[test]
    fn frame_counter_going_back_or_time_going_back_gives_no_window() {
        let mut p = ProgressLog::new(0);
        pb(&mut p, 1000, Some(10_000_000), 10_000, "continue");
        let l = pb(&mut p, 5, Some(20_000_000), 20_000, "continue").unwrap();
        assert!(!l.contains("win_"), "{l}");
        let l = pb(&mut p, 10, Some(30_000_000), 5_000, "continue").unwrap();
        assert!(!l.contains("win_"), "{l}");
        assert!(p.notes.is_empty());
        let mut q = ProgressLog::new(0);
        pb(&mut q, u64::MAX, Some(u64::MAX), u64::MAX - 5000, "continue");
        pb(&mut q, u64::MAX, Some(u64::MAX), u64::MAX, "continue");
        pb(&mut q, 0, Some(0), 0, "continue");
    }

    const DROP_V: &str = "[in#0/dshow @ 0000000000000000] real-time buffer [Elgato Game Capture Neo] [video input] too full or near too full (62% of size: 512000000 [rtbufsize parameter])! frame dropped!";
    const DROP_A: &str = "[in#1/dshow @ 0000000000000000] real-time buffer [Elgato Game Capture Neo (Elgato Game Capture Neo)] [audio input] too full or near too full (70% of size: 3041280 [rtbufsize parameter])! frame dropped!";

    #[test]
    fn drop_lines_and_repeats_are_counted_as_messages() {
        let mut p = ProgressLog::new(INTERVAL_MS);
        let mut f = 0;
        run(&mut p, &mut f, 0, 0, 60);
        p.observe_line(DROP_V);
        p.observe_line("Last message repeated 5 times");
        // -progress keys are written outside ffmpeg's log: the run stays open across them.
        p.observe("frame", "61", 500);
        p.observe_line("Last message repeated 3 times");
        p.observe_line(DROP_A);
        p.observe_line("Last message repeated x times");
        p.observe_line("[h264_mf @ 0000000000000000] stream format change");
        p.observe_line("Last message repeated 7 times");
        p.observe_line(DROP_V);
        p.observe("frame", "= 1234 fps= 30 q=23.0 size=1kB time=00:00:41.13 bitrate=1kbits/s dup=0 drop=0 speed=1.00x", 600);
        p.observe_line("Last message repeated 9 times");
        p.observe_line("   ");
        assert_eq!(p.drops(), &DropCounts { video_lines: 2, audio_lines: 1, other_lines: 0, repeats: 8, unread_repeats: 1 });
        assert_eq!(p.drops().lines(), 3);
        assert_eq!(p.drops().messages(), 11);
        run(&mut p, &mut f, 1, 29, 60);
        assert!(infos(&p).is_empty(), "summary waits 30 s");
        run(&mut p, &mut f, 30, 30, 60);
        assert_eq!(infos(&p).len(), 1);
        let s = infos(&p)[0];
        assert!(s.starts_with("encoder input-drop messages in the last 30000 ms: video_lines=2 audio_lines=1 other_lines=0 repeats=8 unread_repeats=1; since start: lines=3 repeats=8 messages=11."), "{s}");
        assert!(s.contains("not frames") && s.contains("UNMEASURED"));
        // A quiet 30 s writes no summary.
        run(&mut p, &mut f, 31, 60, 60);
        assert_eq!(infos(&p).len(), 1);
        assert!(warns(&p).is_empty());
    }

    #[test]
    fn non_drop_lines_count_nothing() {
        let mut p = ProgressLog::new(INTERVAL_MS);
        for l in ["", "Last message repeated 4 times", "[aist#1:0/pcm_s16le @ 0] Guessed Channel Layout: stereo",
                  "[out_#0:0 @ 0] 100 buffers queued in out_#0:0, something may be wrong.", "\u{fffd}\u{0}", "frame dropped!"] {
            p.observe_line(l);
        }
        assert_eq!(p.drops(), &DropCounts::default());
    }
}
