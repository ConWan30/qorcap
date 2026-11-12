//! Practice-lane capture stop record: `capture_stop.v0.json`.
//!
//! One record per capture writer (the capture ffmpeg that reads the card and encodes): how it ended,
//! the encoder's last `-progress` counters and whether ffmpeg's closing `progress=end` block arrived,
//! and a separate count of DirectShow input-side "real-time buffer ... frame dropped!" warnings, which
//! the encoder's dup/drop counters do not include. At stop, a frame-clock gap check per part is added,
//! because ffmpeg cannot see frames lost before it. The file is bound into the `stop` checkpoint by
//! SHA-256 (`capture_stop_sha256`), the same way `pad_link.v0.json` is.
//!
//! Practice lane only (`--practice-lane` or `QORCAP_LANE=practice`). Off: nothing here runs, no file is
//! written, and the stop checkpoint is unchanged. The capture command and the recording are unchanged.
//!
//! Wording rule: anything not seen is `UNMEASURED` or `UNKNOWN`, never 0. Values are integers or strings.
use crate::frameclock::FrameStamp;
use serde_json::{json, Map, Value};
use std::path::Path;

pub const SCHEMA: &str = "qorcap-capture-stop/v0";
pub const FILE_NAME: &str = "capture_stop.v0.json";
pub const UNMEASURED: &str = "UNMEASURED";
pub const UNKNOWN: &str = "UNKNOWN";
/// PROVISIONAL. A PTS step above 3/2 of the measured frame step is a PTS gap.
pub const PTS_GAP_NUM: u64 = 3;
pub const PTS_GAP_DEN: u64 = 2;
/// PROVISIONAL. Frame-clock stamps are taken per pipe read, so several frames share one arrival time.
/// An arrival gap above this many measured frame periods is counted.
pub const ARRIVAL_GAP_PERIODS: u64 = 3;

/// On for the practice lane only: `--practice-lane`, or `QORCAP_LANE=practice` (the live tap's rule).
pub fn enabled(practice_flag: bool, env_lane: Option<&str>) -> bool {
    practice_flag || env_lane.map(str::trim).map(|s| s.eq_ignore_ascii_case("practice")).unwrap_or(false)
}

fn uint(v: &str) -> Option<u64> { v.trim().parse().ok() }

/// Encoder counters from one `-progress` block (or one stats line). `None` = not in that block.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Counters { pub frame: Option<u64>, pub dup: Option<u64>, pub drop: Option<u64> }

impl Counters {
    fn is_empty(&self) -> bool { *self == Counters::default() }
}

/// Everything read from one capture writer's stderr.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct WriterTally {
    cur: Counters,
    /// The last closed block that carried at least one counter.
    pub last: Option<Counters>,
    /// ffmpeg's closing `progress=end` block arrived.
    pub end_seen: bool,
    pub blocks: u64,
    pub input_drop_lines: u64,
    pub input_drop_video_lines: u64,
    pub input_drop_audio_lines: u64,
    /// Sum of N from `Last message repeated N times` lines that directly follow an input-drop line.
    pub input_drop_repeats: u64,
    /// `Last message repeated` lines after an input-drop line whose N could not be read.
    pub input_drop_repeats_unread: u64,
    last_was_drop: bool,
}

/// `[in#0/dshow @ ...] real-time buffer [...] [video input] too full or near too full (...)! frame dropped!`
pub fn is_input_drop(line: &str) -> bool {
    let l = line.to_ascii_lowercase();
    l.contains("real-time buffer") && l.contains("frame dropped")
}

/// `Last message repeated 3 times` -> Some(Some(3)); an unreadable count -> Some(None); other lines -> None.
pub fn repeat_count(line: &str) -> Option<Option<u64>> {
    let l = line.trim();
    let i = l.find("Last message repeated")?;
    let rest = l[i + "Last message repeated".len()..].trim();
    Some(rest.split_whitespace().next().and_then(uint))
}

impl WriterTally {
    /// One `-progress` key from the capture ffmpeg's stderr. A `frame` value holding `=` is a whole stats line.
    pub fn observe_kv(&mut self, key: &str, value: &str) {
        self.last_was_drop = false;
        if key == "frame" && value.contains('=') {
            let r = crate::encprog::parse_stats_line(&format!("{key}={value}"));
            let c = Counters { frame: r.frame, dup: r.dup, drop: r.drop };
            if !c.is_empty() { self.last = Some(c); }
            return;
        }
        match key {
            "frame" => self.cur.frame = uint(value),
            "dup_frames" | "dup" => self.cur.dup = uint(value),
            "drop_frames" | "drop" => self.cur.drop = uint(value),
            "progress" => {
                let c = std::mem::take(&mut self.cur);
                self.blocks += 1;
                if !c.is_empty() { self.last = Some(c); }
                if value.trim() == "end" { self.end_seen = true; }
            }
            _ => {}
        }
    }

    /// One stderr line that is not a `-progress` key.
    pub fn observe_line(&mut self, line: &str) {
        if is_input_drop(line) {
            self.input_drop_lines += 1;
            let l = line.to_ascii_lowercase();
            if l.contains("[video input]") { self.input_drop_video_lines += 1; }
            if l.contains("[audio input]") { self.input_drop_audio_lines += 1; }
            self.last_was_drop = true;
            return;
        }
        if let Some(n) = repeat_count(line) {
            if self.last_was_drop {
                match n { Some(n) => self.input_drop_repeats = self.input_drop_repeats.saturating_add(n), None => self.input_drop_repeats_unread += 1 }
            }
            // A repeat line keeps the run open: the repeated message is still the drop warning.
            return;
        }
        if !line.trim().is_empty() { self.last_was_drop = false; }
    }
}

/// How the capture writer ended, as QorCap saw it.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct WriterEnd {
    /// The OS exit code (Windows: the DWORD as i32, so ENOSPC -28 reads -28). None: no code (a signal, or no status).
    pub exit_code: Option<i32>,
    /// `wait()` returned a status.
    pub status_known: bool,
    /// Why QorCap sent a kill, if it did, before the exit was seen: `stop`, `watchdog_no_data`, `stdout_closed`.
    pub kill_sent: Option<String>,
    /// The writer never started.
    pub spawn_error: Option<String>,
    /// The stderr reader finished, so the counts below cover the whole stream.
    pub stderr_drained: bool,
}

fn num_or(v: Option<u64>, missing: &str) -> Value { v.map(Value::from).unwrap_or_else(|| json!(missing)) }

/// One writer's record. `seq` is the capture writer's order in this session (0-based).
pub fn writer_record(seq: u64, part_file: Option<&str>, part_basis: &str, end: &WriterEnd, t: &WriterTally) -> Value {
    let ended_by = if end.spawn_error.is_some() { "spawn_failed" }
        else if !end.status_known { UNKNOWN }
        else if end.kill_sent.is_some() { "qorcap_kill" }
        else { "self_exit" };
    let exit_code = match (end.status_known, end.exit_code) { (true, Some(c)) => json!(c), _ => json!(UNKNOWN) };
    let mut writer = Map::new();
    writer.insert("ended_by".into(), json!(ended_by));
    writer.insert("exit_code".into(), exit_code);
    writer.insert("kill_sent".into(), end.kill_sent.as_deref().map(Value::from).unwrap_or(Value::Null));
    if let Some(e) = &end.spawn_error { writer.insert("spawn_error".into(), json!(e)); }
    writer.insert("stderr_drained".into(), json!(end.stderr_drained));
    let basis = if t.last.is_none() { "none" } else if t.end_seen { "end_block" } else { "last_progress_block" };
    let c = t.last.clone().unwrap_or_default();
    let encoder = json!({
        "source": "ffmpeg -progress pipe:2",
        "end_seen": t.end_seen,
        "counters_basis": basis,
        "frame": num_or(c.frame, UNMEASURED),
        "dup": num_or(c.dup, UNMEASURED),
        "drop": num_or(c.drop, UNMEASURED),
        "progress_blocks": t.blocks,
    });
    // Counts are complete only when the whole stderr stream was read; otherwise they are lower bounds.
    let full = end.stderr_drained && end.spawn_error.is_none();
    let count = |n: u64| if full { json!(n) } else { json!(UNKNOWN) };
    let input = json!({
        "source": "ffmpeg dshow real-time buffer warnings (input side, before the encoder)",
        "warning_lines": count(t.input_drop_lines),
        "warning_lines_seen": t.input_drop_lines,
        "video_input_lines": count(t.input_drop_video_lines),
        "audio_input_lines": count(t.input_drop_audio_lines),
        "repeated_after_warning": count(t.input_drop_repeats),
        "repeat_lines_unread": t.input_drop_repeats_unread,
        "dropped_frames": UNMEASURED,
        "note": "A warning line is not a frame count: one line per dropped frame was not checked.",
    });
    json!({
        "writer_seq": seq,
        "part_file": part_file.map(Value::from).unwrap_or(Value::Null),
        "part_basis": part_basis,
        "writer": Value::Object(writer),
        "encoder": encoder,
        "input_drops": input,
    })
}

/// The frame period in 90 kHz ticks for a measured rate, rounded. None when it is not a positive integer.
fn step_ticks(r: crate::align::Rate) -> Option<u64> {
    if r.num == 0 { return None; }
    let s = (90_000u128 * r.den as u128 + r.num as u128 / 2) / r.num as u128;
    (s > 0).then_some(s as u64)
}

/// Frame-clock gap check for one part file. `stamps` is every row from frame_clock.jsonl (any order).
pub fn frame_clock_gaps(stamps: &[FrameStamp], file: &str, discontinuities: u64) -> Value {
    let mut mine: Vec<&FrameStamp> = stamps.iter().filter(|s| s.file == file).collect();
    if mine.is_empty() {
        return json!({"part_file": file, "status": UNMEASURED, "reason": "no frame_clock rows for this part"});
    }
    mine.sort_by_key(|s| s.frame);
    let frames = mine.len() as u64;
    let mut largest_arrival_us: u64 = 0;
    for w in mine.windows(2) { largest_arrival_us = largest_arrival_us.max(w[1].t_mono_us.saturating_sub(w[0].t_mono_us)); }
    let rate = crate::align::frame_clock_rate(stamps, file);
    let Some((rate, step)) = rate.and_then(|r| step_ticks(r).map(|s| (r, s))) else {
        return json!({
            "part_file": file, "status": UNMEASURED, "reason": "frame rate not measured from frame_clock PTS steps (fewer than 10 steps)",
            "frames": frames, "largest_arrival_gap_us": if frames > 1 { json!(largest_arrival_us) } else { json!(UNMEASURED) },
            "pts_discontinuities": discontinuities,
        });
    };
    let pts_limit = step * PTS_GAP_NUM / PTS_GAP_DEN;
    let period_us = step * 1_000_000 / 90_000;
    let arrival_limit = period_us * ARRIVAL_GAP_PERIODS;
    let (mut pts_gaps, mut missing_slots, mut largest_pts) = (0u64, 0u64, 0u64);
    let mut arrival_gaps = 0u64;
    for w in mine.windows(2) {
        if w[1].pts_90k > w[0].pts_90k {
            let d = w[1].pts_90k - w[0].pts_90k;
            largest_pts = largest_pts.max(d);
            if d > pts_limit { pts_gaps += 1; missing_slots += ((d + step / 2) / step).saturating_sub(1); }
        }
        if w[1].t_mono_us.saturating_sub(w[0].t_mono_us) > arrival_limit { arrival_gaps += 1; }
    }
    json!({
        "part_file": file, "status": "measured",
        "frames": frames,
        "rate": rate.filter(),
        "rate_source": "frame_clock modal PTS step",
        "step_90k": step,
        "pts_gap_threshold_90k": pts_limit,
        "pts_gaps": pts_gaps,
        "pts_missing_slots": missing_slots,
        "largest_pts_step_90k": largest_pts,
        "arrival_gap_threshold_us": arrival_limit,
        "arrival_gaps": arrival_gaps,
        "largest_arrival_gap_us": largest_arrival_us,
        "pts_discontinuities": discontinuities,
        "note": "Arrival is when QorCap read the frame's MPEG-TS packet (after the encoder). Thresholds are PROVISIONAL.",
    })
}

/// Count `discontinuity` rows per file in frame_clock.jsonl text.
pub fn discontinuities(text: &str, file: &str) -> u64 {
    text.lines().filter_map(|l| serde_json::from_str::<Value>(l).ok())
        .filter(|v| v["type"] == "discontinuity" && v["file"] == file).count() as u64
}

/// The frame-clock section for the given part files. `frame_clock` is the path of frame_clock.jsonl.
pub fn frame_clock_section(frame_clock: Option<&Path>, files: &[String]) -> Value {
    let text = frame_clock.and_then(|p| std::fs::read_to_string(p).ok());
    let Some(text) = text else {
        return json!({"status": UNMEASURED, "reason": "frame_clock.jsonl not present (frame clock off or not written)", "parts": []});
    };
    let stamps = crate::frameclock::load(frame_clock.unwrap_or(Path::new("")));
    let mut names: Vec<String> = files.to_vec();
    for s in &stamps { if !names.contains(&s.file) { names.push(s.file.clone()); } }
    let parts: Vec<Value> = names.iter().map(|f| frame_clock_gaps(&stamps, f, discontinuities(&text, f))).collect();
    json!({"status": "present", "file": crate::frameclock::FILE_NAME, "parts": parts})
}

/// The whole document. `state` is `open` while recording (not bound) and `stop` when written for the stop checkpoint.
pub fn document(session: &str, state: &str, stop_reason: Option<&str>, writers: &[Value], frame_clock: Value) -> Value {
    json!({
        "schema": SCHEMA,
        "lane": "practice",
        "session": session,
        "state": state,
        "stop_reason": stop_reason.map(Value::from).unwrap_or_else(|| json!(UNKNOWN)),
        "writers": writers,
        "frame_clock": frame_clock,
        "unmeasured": [
            "input-side dropped frame count (warning lines are not frames)",
            "encoder counters after the last progress block when progress=end was not seen",
            "the fMP4 remux ffmpeg's exit status",
        ],
        "binding": "state=stop: the stop checkpoint's qorcap.capture_stop_sha256 is SHA-256 of this file's bytes. state=open: not bound.",
    })
}

/// Pretty JSON plus a newline: the exact bytes that are hashed.
pub fn render(doc: &Value) -> String { format!("{}\n", serde_json::to_string_pretty(doc).unwrap_or_else(|_| "{}".into())) }

/// Write `text` to `path` through a temp file and a rename, synced. Returns the SHA-256 of the bytes.
pub fn write_file(path: &Path, text: &str) -> Result<String, String> {
    use std::io::Write;
    let tmp = path.with_extension("json.tmp");
    let mut f = std::fs::File::create(&tmp).map_err(|e| format!("create {}: {e}", tmp.display()))?;
    f.write_all(text.as_bytes()).and_then(|_| f.sync_all()).map_err(|e| { let _ = std::fs::remove_file(&tmp); format!("write {}: {e}", tmp.display()) })?;
    drop(f);
    std::fs::rename(&tmp, path).map_err(|e| { let _ = std::fs::remove_file(&tmp); format!("rename to {}: {e}", path.display()) })?;
    Ok(crate::evidence::sha256_hex(text.as_bytes()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn feed_block(t: &mut WriterTally, frame: u64, dup: u64, drop: u64, tail: &str) {
        for (k, v) in [("frame", frame.to_string()), ("fps", "59.94".into()), ("out_time_us", "1000".into()),
                       ("dup_frames", dup.to_string()), ("drop_frames", drop.to_string()), ("speed", "1x".into()), ("progress", tail.into())] {
            t.observe_kv(k, &v);
        }
    }

    fn clean() -> WriterEnd { WriterEnd { exit_code: Some(0), status_known: true, kill_sent: None, spawn_error: None, stderr_drained: true } }

    #[test]
    fn lane_gate_is_practice_only() {
        assert!(!enabled(false, None));
        assert!(!enabled(false, Some("official")));
        assert!(!enabled(false, Some("")));
        assert!(enabled(false, Some(" Practice ")));
        assert!(enabled(true, None));
    }

    #[test]
    fn end_block_present_is_the_basis() {
        let mut t = WriterTally::default();
        feed_block(&mut t, 100, 0, 0, "continue");
        feed_block(&mut t, 27669, 0, 0, "end");
        let r = writer_record(0, Some("video.mp4"), "recorder_at_end", &clean(), &t);
        assert_eq!(r["encoder"]["end_seen"], true);
        assert_eq!(r["encoder"]["counters_basis"], "end_block");
        assert_eq!(r["encoder"]["frame"], 27669);
        assert_eq!(r["encoder"]["dup"], 0);
        assert_eq!(r["encoder"]["drop"], 0);
        assert_eq!(r["encoder"]["progress_blocks"], 2);
        assert_eq!(r["writer"]["exit_code"], 0);
        assert_eq!(r["writer"]["ended_by"], "self_exit");
    }

    #[test]
    fn progress_only_is_labelled_and_end_absent() {
        let mut t = WriterTally::default();
        feed_block(&mut t, 42712, 0, 0, "continue");
        let end = WriterEnd { exit_code: Some(1), status_known: true, kill_sent: Some("stop".into()), ..clean() };
        let r = writer_record(0, Some("video.mp4"), "recorder_at_end", &end, &t);
        assert_eq!(r["encoder"]["end_seen"], false);
        assert_eq!(r["encoder"]["counters_basis"], "last_progress_block");
        assert_eq!(r["encoder"]["frame"], 42712);
        assert_eq!(r["writer"]["ended_by"], "qorcap_kill");
        assert_eq!(r["writer"]["kill_sent"], "stop");
    }

    #[test]
    fn no_progress_at_all_is_unmeasured_not_zero() {
        let t = WriterTally::default();
        let r = writer_record(3, None, "none", &clean(), &t);
        assert_eq!(r["encoder"]["counters_basis"], "none");
        for k in ["frame", "dup", "drop"] { assert_eq!(r["encoder"][k], UNMEASURED, "{k}"); }
        assert_eq!(r["part_file"], Value::Null);
        // A block that is missing one counter does not borrow it from an earlier block.
        let mut t = WriterTally::default();
        feed_block(&mut t, 10, 2, 1, "continue");
        t.observe_kv("frame", "20"); t.observe_kv("progress", "continue");
        let r = writer_record(0, None, "none", &clean(), &t);
        assert_eq!(r["encoder"]["frame"], 20);
        assert_eq!(r["encoder"]["dup"], UNMEASURED);
        assert_eq!(r["encoder"]["drop"], UNMEASURED);
    }

    #[test]
    fn exit_codes_0_1_minus_28_and_unknown() {
        let t = WriterTally::default();
        for (code, by) in [(0, "self_exit"), (1, "self_exit"), (-28, "self_exit")] {
            let r = writer_record(0, None, "none", &WriterEnd { exit_code: Some(code), ..clean() }, &t);
            assert_eq!(r["writer"]["exit_code"], code);
            assert_eq!(r["writer"]["ended_by"], by);
        }
        // Windows reports ENOSPC as 4294967268: the same bits as i32 are -28.
        assert_eq!(4294967268u32 as i32, -28);
        let r = writer_record(0, None, "none", &WriterEnd { exit_code: Some(1), kill_sent: Some("watchdog_no_data".into()), ..clean() }, &t);
        assert_eq!(r["writer"]["ended_by"], "qorcap_kill");
        assert_eq!(r["writer"]["kill_sent"], "watchdog_no_data");
        let r = writer_record(0, None, "none", &WriterEnd { exit_code: None, status_known: false, ..clean() }, &t);
        assert_eq!(r["writer"]["exit_code"], UNKNOWN);
        assert_eq!(r["writer"]["ended_by"], UNKNOWN);
        let r = writer_record(0, None, "none", &WriterEnd { exit_code: None, status_known: true, ..clean() }, &t);
        assert_eq!(r["writer"]["exit_code"], UNKNOWN, "a signal death has no code");
        let r = writer_record(0, None, "none", &WriterEnd { spawn_error: Some("not found".into()), status_known: false, stderr_drained: false, ..clean() }, &t);
        assert_eq!(r["writer"]["ended_by"], "spawn_failed");
        assert_eq!(r["input_drops"]["warning_lines"], UNKNOWN);
    }

    #[test]
    fn input_drop_warnings_are_counted_separately_from_encoder_counters() {
        let mut t = WriterTally::default();
        let v = "[in#0/dshow @ 000001d3d0b13480] real-time buffer [Elgato Game Capture Neo] [video input] too full or near too full (62% of size: 512000000 [rtbufsize parameter])! frame dropped!";
        let a = "[in#1/dshow @ 0000021ead925e00] real-time buffer [Elgato Game Capture Neo (Elgato Game Capture Neo)] [audio input] too full or near too full (70% of size: 3041280 [rtbufsize parameter])! frame dropped!";
        t.observe_line(v);
        t.observe_line("    Last message repeated 4 times");
        t.observe_line(a);
        t.observe_line("Last message repeated 1 times");
        t.observe_line("Last message repeated x times");
        t.observe_line("[out#1/image2 @ 0x1] Error muxing a packet");
        t.observe_line("Last message repeated 9 times"); // repeats a non-drop line: not counted
        feed_block(&mut t, 500, 0, 0, "continue");
        t.observe_line("Last message repeated 7 times"); // after progress keys: not counted
        let r = writer_record(0, Some("video.mp4"), "recorder_at_end", &clean(), &t);
        assert_eq!(r["input_drops"]["warning_lines"], 2);
        assert_eq!(r["input_drops"]["video_input_lines"], 1);
        assert_eq!(r["input_drops"]["audio_input_lines"], 1);
        assert_eq!(r["input_drops"]["repeated_after_warning"], 5);
        assert_eq!(r["input_drops"]["repeat_lines_unread"], 1);
        assert_eq!(r["input_drops"]["dropped_frames"], UNMEASURED);
        assert_eq!(r["encoder"]["drop"], 0, "encoder counters are their own");
        // A writer whose stderr was not fully read: counts are UNKNOWN, the seen count stays as a lower bound.
        let r = writer_record(0, None, "none", &WriterEnd { stderr_drained: false, ..clean() }, &t);
        assert_eq!(r["input_drops"]["warning_lines"], UNKNOWN);
        assert_eq!(r["input_drops"]["warning_lines_seen"], 2);
    }

    #[test]
    fn garbage_lines_never_panic_or_invent() {
        let mut t = WriterTally::default();
        for (k, v) in [("frame", "-1"), ("frame", "abc"), ("dup_frames", ""), ("drop_frames", "1e3"), ("progress", ""), ("", ""), ("frame", "= = =")] {
            t.observe_kv(k, v);
        }
        for l in ["", "=", "\u{fffd}\u{0}", "Last message repeated", "frame dropped!", "real-time buffer", "Last message repeated 99999999999999999999999 times"] {
            t.observe_line(l);
        }
        assert_eq!(t.last, None);
        assert!(!t.end_seen);
        assert_eq!(t.input_drop_lines, 0);
        let r = writer_record(0, None, "none", &clean(), &t);
        assert_eq!(r["encoder"]["frame"], UNMEASURED);
        // A stats line (`frame=` with more keys) is parsed like encprog does.
        let mut t = WriterTally::default();
        let l = "frame= 1234 fps= 30 q=23.0 size=1kB time=00:00:41.13 bitrate=1kbits/s dup=3 drop=1 speed=1.00x";
        let (k, v) = l.split_once('=').unwrap();
        t.observe_kv(k, v);
        assert_eq!(t.last, Some(Counters { frame: Some(1234), dup: Some(3), drop: Some(1) }));
        assert!(!t.end_seen);
    }

    fn stamps(file: &str, pts: &[u64], t: &[u64]) -> Vec<FrameStamp> {
        pts.iter().zip(t).enumerate().map(|(i, (&p, &t))| FrameStamp { file: file.into(), frame: i as i64, t_mono_us: t, pts_90k: p }).collect()
    }

    #[test]
    fn frame_clock_gap_check_counts_pts_and_arrival_gaps_at_the_measured_rate() {
        // 60 fps: step 1500. Frames 0..20, then a 4-step PTS jump (3 missing slots) and a 100 ms arrival stall.
        let mut pts: Vec<u64> = (0..20).map(|i| i * 1500).collect();
        let mut t: Vec<u64> = (0..20).map(|i| 1_000_000 + i * 16_667).collect();
        pts.push(19 * 1500 + 4 * 1500); t.push(t[19] + 100_000);
        pts.push(pts[20] + 1500); t.push(t[20] + 16_667);
        let s = stamps("video.mp4", &pts, &t);
        let g = frame_clock_gaps(&s, "video.mp4", 0);
        assert_eq!(g["status"], "measured");
        assert_eq!(g["rate"], "60/1");
        assert_eq!(g["step_90k"], 1500);
        assert_eq!(g["frames"], 22);
        assert_eq!(g["pts_gaps"], 1);
        assert_eq!(g["pts_missing_slots"], 3);
        assert_eq!(g["largest_pts_step_90k"], 6000);
        assert_eq!(g["arrival_gap_threshold_us"], 16_666 * 3);
        assert_eq!(g["arrival_gaps"], 1);
        assert_eq!(g["largest_arrival_gap_us"], 100_000);
        // 30 fps at the same check: step 3000, no gaps.
        let pts: Vec<u64> = (0..15).map(|i| i * 3000).collect();
        let t: Vec<u64> = (0..15).map(|i| i * 33_333).collect();
        let g = frame_clock_gaps(&stamps("p2.mp4", &pts, &t), "p2.mp4", 2);
        assert_eq!(g["rate"], "30/1");
        assert_eq!(g["pts_gaps"], 0);
        assert_eq!(g["arrival_gaps"], 0);
        assert_eq!(g["pts_discontinuities"], 2);
    }

    #[test]
    fn frame_clock_missing_or_too_short_is_unmeasured() {
        let g = frame_clock_gaps(&[], "video.mp4", 0);
        assert_eq!(g["status"], UNMEASURED);
        let s = stamps("video.mp4", &[0, 1500, 3000], &[0, 10, 20]);
        let g = frame_clock_gaps(&s, "video.mp4", 0);
        assert_eq!(g["status"], UNMEASURED);
        assert_eq!(g["frames"], 3);
        assert!(g.get("pts_gaps").is_none(), "no gap count without a measured rate");
        let sec = frame_clock_section(None, &["video.mp4".into()]);
        assert_eq!(sec["status"], UNMEASURED);
        let sec = frame_clock_section(Some(Path::new("/nonexistent/frame_clock.jsonl")), &["video.mp4".into()]);
        assert_eq!(sec["status"], UNMEASURED);
    }

    #[test]
    fn document_renders_stably_and_write_returns_the_sha_of_the_bytes() {
        let dir = std::env::temp_dir().join(format!("qorcap-capstop-{}-{}", std::process::id(), line!()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let fc = dir.join("frame_clock.jsonl");
        let mut text = String::from("{\"type\":\"header\"}\n");
        for i in 0..12u64 { text.push_str(&format!("{{\"type\":\"frame\",\"file\":\"video.mp4\",\"frame\":{i},\"t_mono_us\":{},\"pts_90k\":{}}}\n", i * 16_667, i * 1500)); }
        text.push_str("{\"type\":\"discontinuity\",\"file\":\"video.mp4\",\"t_mono_us\":1,\"pts_90k\":0}\n");
        std::fs::write(&fc, text).unwrap();
        let mut t = WriterTally::default();
        feed_block(&mut t, 12, 0, 0, "continue");
        let w = writer_record(0, Some("video.mp4"), "recorder_at_end", &clean(), &t);
        let doc = document("m1", "stop", Some("enter"), &[w], frame_clock_section(Some(&fc), &["video.mp4".into(), "video.part2.mp4".into()]));
        assert_eq!(doc["frame_clock"]["parts"][0]["pts_discontinuities"], 1);
        assert_eq!(doc["frame_clock"]["parts"][1]["status"], UNMEASURED);
        let a = render(&doc);
        assert_eq!(a, render(&doc));
        assert!(a.ends_with("}\n"));
        let p = dir.join(FILE_NAME);
        let sha = write_file(&p, &a).unwrap();
        assert_eq!(sha, crate::evidence::sha256_hex(&std::fs::read(&p).unwrap()));
        assert!(!dir.join("capture_stop.v0.json.tmp").exists());
        for w in ["verified", "proven", "skill", "reaction time", "anti-cheat"] { assert!(!a.to_lowercase().contains(w), "{w}"); }
        let _ = std::fs::remove_dir_all(&dir);
    }
}
