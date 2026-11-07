//! Controller input log on QorCap's monotonic clock.
//!
//! Every HID report the laptop reads from the USB-connected DualSense is stamped with `t_us` from the SAME
//! `Instant` the engine uses for `mono_ms` in manifest.jsonl / checkpoints.jsonl, so inputs and 2-second video
//! chunks share one clock (the Qor Clock / Clock Notary rule from Qoresence: one shared clock, honest
//! absence). Records accumulate in memory and are *sealed* each time a video segment is hashed:
//!
//! * each record gets `seg` = that segment's seq and is appended to `inputs.jsonl`;
//! * the chunk hash `sha256 = SHA-256(JCS([records]))` becomes the checkpoint's `input_hash`;
//! * chunk hashes are chained `head_n = SHA-256(JCS({prev, seq, sha256}))` (same rule as the video chain) and a
//!   `{"type":"chunk",...}` line is written after the records.
//!
//! A chunk with no records hashes to SHA-256(JCS([])), exactly what QorCap <= 0.2.2 wrote, so bundles without a
//! controller stay byte-compatible. Controller presence changes (`connected` / `missing` / `unplugged` /
//! `stalled`) are themselves records, so absence is committed rather than hidden. Each `connected` record
//! on a new log carries `pad_id` (USB serial, or a device instance id / hidraw id). The reader never writes
//! an output report or a feature report.
//!
//! Since 0.3.1 the IMU haptic detector (`haptics.rs`) runs on the same reports. Its events are `{"type":"haptic"}`
//! records in the same chunks (so they are covered by `input_hash` and the inputs chain exactly like button
//! records), and `{"type":"imu"}` records commit whether IMU data was present and its measured rate. The same
//! haptic records (with `chunk_index`) are also appended to `haptics.jsonl` next to `inputs.jsonl` for easy
//! reading; `input_proof.json` records its SHA-256 and checks it against the committed records.
//!
//! Since 0.3.39 a `{"type":"hb"}` row is a heartbeat for pad-link continuity (skill-proof join).
//! The reader loop that receives HID reports writes one every 1000 ms of host monotonic time,
//! including when buttons and sticks do not change. The row is sealed in the same chunk hash
//! chain as the other input rows. `receipt.v0` is unchanged.
//!
//! Each `hb` row also commits the reports received in that second: `rx_delta`, the first and last
//! report counter (`seq0`/`seq1`, USB offset 7), how many steps were +1 (`seq_steps`) or not
//! (`seq_breaks`), the sensor timestamp span (`hw0`/`hw1`/`hw_advance`/`hw_stalls`, USB offset 28),
//! and `rx_bind` (SHA-256 of those counter and timestamp bytes in receive order). Sticks and
//! triggers stay raw 0..255 with center 128 and no deadzone. The counter-step rule is PROVISIONAL.
//! See `pad_link` for idle, drop, and unknown. Unknown is not idle.
//!
//! `dev_counter` is a diagnostic copy of one raw report byte (USB offset 7). Its semantics are unvalidated.
//! `max_rx_gap_us` is the largest gap between consecutive received reports in the interval, including the
//! gap from the previous interval's last report. It is measured before `last_rx_t_us` moves, so a stall
//! that recovers before the heartbeat stays visible. `stale_over_50ms`, `short_reads`, `parse_faults`,
//! and `first_rx_t_us` reset every heartbeat. Epoch increments on every HID handle open and on a device
//! identity change. Stale telemetry resets on reconnect.
use crate::evidence::{self as ev, GENESIS};
use crate::haptics::{Detector, HapticLive};
use crate::hid::{self, Pad, ReportSource};
use log::{info, warn};
use serde_json::{json, Value};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

pub const SCHEMA: &str = "qorcap-inputs/v0";
/// Analog-only changes are logged at most this often (buttons are always logged).
pub const ANALOG_MIN_GAP_US: u64 = 4_000;
/// Host monotonic gap between `hb` rows. Independent of whether a button or stick changed.
pub const HB_INTERVAL_US: u64 = 1_000_000;
/// An inter-report gap above this counts in `stale_over_50ms`.
pub const STALE_RX_GAP_US: u64 = 50_000;
/// USB input report 0x01 is 64 bytes. A shorter read is `short_reads`, not a parse failure.
const USB_REPORT_LEN: usize = 64;

pub fn empty_hash() -> String { ev::jcs_sha(&json!([])) }

/// Windows `ERROR_SHARING_VIOLATION`. A reader that does not share writes (the live sidecar re-reads
/// `inputs.jsonl` every second) makes the append fail; retrying keeps the hash chain intact.
pub fn sharing_violation(err: &std::io::Error) -> bool { err.raw_os_error() == Some(32) }

#[cfg(windows)]
const FILE_SHARE_READ_WRITE_DELETE: u32 = 0x1 | 0x2 | 0x4;

fn open_append(path: &Path) -> std::io::Result<std::fs::File> {
    let mut o = std::fs::OpenOptions::new();
    o.create(true).append(true);
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        o.share_mode(FILE_SHARE_READ_WRITE_DELETE);
    }
    o.open(path)
}

fn open_read(path: &Path) -> std::io::Result<std::fs::File> {
    let mut o = std::fs::OpenOptions::new();
    o.read(true);
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        o.share_mode(FILE_SHARE_READ_WRITE_DELETE);
    }
    o.open(path)
}

/// Read bytes without denying a concurrent append (Windows share read+write+delete).
pub fn read_shared_bytes(path: &Path) -> std::io::Result<Vec<u8>> {
    let mut f = open_read(path)?;
    let mut buf = Vec::new();
    f.read_to_end(&mut buf)?;
    Ok(buf)
}

pub fn read_shared_string(path: &Path) -> std::io::Result<String> {
    Ok(String::from_utf8_lossy(&read_shared_bytes(path)?).into_owned())
}

/// Run `op` until it succeeds. A sharing violation waits and tries again; any other error returns immediately.
fn retry_sharing<T>(mut op: impl FnMut() -> std::io::Result<T>, attempts: u32, mut pause: impl FnMut(Duration)) -> std::io::Result<T> {
    let mut delay = Duration::from_millis(25);
    let mut last = None;
    for i in 0..attempts {
        match op() {
            Ok(v) => return Ok(v),
            Err(e) if sharing_violation(&e) && i + 1 < attempts => {
                last = Some(e);
                pause(delay);
                delay = (delay * 2).min(Duration::from_millis(200));
            }
            Err(e) => return Err(e),
        }
    }
    Err(last.unwrap_or_else(|| std::io::Error::new(std::io::ErrorKind::Other, "sharing violation")))
}

fn append_bytes(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    // Retry the open only. A sharing violation is raised by CreateFile, before any byte is written.
    // Retrying a write that already appended would duplicate the chunk.
    let mut f = retry_sharing(|| open_append(path), 8, |d| std::thread::sleep(d))?;
    f.write_all(bytes)?;
    f.sync_all()
}

/// How many rising edges `/status` keeps for the challenge card. ~10 s of fast tapping.
pub const RECENT_PRESSES: usize = 64;

/// One rising edge for the live challenge card. `seq` counts every edge since the log opened.
#[derive(Debug, Clone, Default, PartialEq, Eq, serde::Serialize)]
pub struct LivePress {
    pub seq: u64,
    pub t_ms: u64,
    pub button: String,
}

/// Live controller state for /status and `input-test`.
#[derive(Debug, Clone, Default, serde::Serialize)]
pub struct Live {
    pub enabled: bool,
    /// off | searching | connected | missing | unplugged | stalled
    pub state: String,
    pub device: Option<String>,
    /// USB serial, device instance id, or hidraw id of the connected pad. Cleared when it drops.
    pub pad_id: Option<String>,
    pub reports: u64,
    pub logged: u64,
    pub presses: u64,
    /// Newest rising edges, oldest first, at most `RECENT_PRESSES` (since 0.3.37). Display only: the console
    /// reads these so a tap shorter than its /status poll still shows. `t_ms` is the referee's press clock
    /// (`wall_anchor_ms + t_us / 1000`, the same value `press_events` derives from inputs.jsonl).
    pub recent_presses: Vec<LivePress>,
    pub buttons: Vec<String>,
    pub l2: u8,
    pub r2: u8,
    pub sticks: [u8; 4],
    pub warning: Option<String>,
    pub last_report_mono_ms: Option<u64>,
    /// `Seal: waiting`, `Seal: locked`, or `Seal: failed (reason)`. Empty when the seal watch is off.
    pub seal_line: String,
    /// IMU haptic detector state (since 0.3.1).
    pub haptic: HapticLive,
}

impl Live {
    fn presses_seq_next(&self) -> u64 {
        self.recent_presses.last().map(|p| p.seq + 1).unwrap_or(1)
    }
}

#[derive(Default)]
struct RxSpan {
    n: u64,
    seq0: Option<u8>,
    seq1: Option<u8>,
    steps: u64,
    breaks: u64,
    hw0: Option<u32>,
    hw1: Option<u32>,
    stalls: u64,
    bytes: Vec<u8>,
}

pub struct InputLog {
    path: Option<PathBuf>,
    pending: Vec<Value>,
    pub head: String,
    pub chunks: u64,
    pub total_records: u64,
    last_logged: Option<(u64, Pad)>,
    pub live: Live,
    pub detector: Detector,
    haptic_path: Option<PathBuf>,
    wall_anchor_ms: u64,
    imu_committed: Option<(bool, u32)>,
    /// Next `hb_seq` to write. Starts at 0. `u64` saturates; it does not wrap.
    hb_seq: u64,
    /// Connection epoch. Starts at 0. Increments on every HID handle open and on a device identity change.
    epoch: u64,
    last_hb_t_us: Option<u64>,
    rx_at_last_hb: u64,
    last_rx_t_us: Option<u64>,
    /// Raw report counter byte from the newest parsed report. Diagnostic only; semantics are unvalidated.
    dev_counter: Option<u64>,
    faults_since_hb: u64,
    max_rx_gap_us: u64,
    stale_over_50ms: u64,
    short_reads: u64,
    parse_faults: u64,
    first_rx_t_us: Option<u64>,
    span: RxSpan,
    prev_counter: Option<u8>,
    prev_hw: Option<u32>,
    seal: crate::seal::SealWatch,
}

impl InputLog {
    pub fn new(path: Option<PathBuf>, enabled: bool, wall_anchor_ms: u64) -> Self {
        let haptic_path = path.as_ref().map(|p| p.with_file_name(haptics_file_name(p)));
        let mut l = InputLog { path, pending: vec![], head: GENESIS.into(), chunks: 0, total_records: 0, last_logged: None,
            live: Live { enabled, state: if enabled { "searching".into() } else { "off".into() }, ..Default::default() },
            detector: Detector::new(wall_anchor_ms), haptic_path, wall_anchor_ms, imu_committed: None,
            hb_seq: 0, epoch: 0, last_hb_t_us: None, rx_at_last_hb: 0, last_rx_t_us: None,
            dev_counter: None, faults_since_hb: 0, max_rx_gap_us: 0, stale_over_50ms: 0,
            short_reads: 0, parse_faults: 0, first_rx_t_us: None,
            span: RxSpan::default(), prev_counter: None, prev_hw: None,
            seal: crate::seal::SealWatch::new(false) };
        if enabled {
            l.pending.push(json!({"type": "header", "schema": SCHEMA, "t_us": 0,
                "clock": "qorcap-mono-us (same Instant as manifest/checkpoint mono_ms)", "wall_anchor_ms": wall_anchor_ms,
                "transport": "usb-hid", "haptics": Detector::params(),
                "pad_identity": "required",
                "axes": "raw 0..255, center 128, no deadzone",
                "report_counter": "usb offset 7, copied, step rule PROVISIONAL",
                "sensor_timestamp": "usb offset 28, u32 le, 0.33 us ticks, copied from the input report",
                "limit": "Records the USB controller inputs on the same clock as the video."}));
        }
        l
    }

    /// Call of Duty opening seal. Off for every other game. Console line only; no controller write.
    pub fn enable_seal(&mut self) {
        if self.seal.enabled() { return; }
        self.seal = crate::seal::SealWatch::new(true);
        self.live.seal_line = self.seal.line.clone();
        info!("{}", self.live.seal_line);
    }

    pub fn close_seal(&mut self) {
        let before = self.seal.line.clone();
        self.seal.close_at_stop();
        self.live.seal_line = self.seal.line.clone();
        if self.seal.enabled() && self.live.seal_line != before {
            info!("{}", self.live.seal_line);
            self.persist_seal();
        }
    }

    pub fn take_seal_for_checkpoint(&mut self) -> Option<Value> {
        self.seal.take_for_checkpoint()
    }

    fn persist_seal(&self) {
        let Some(dir) = self.path.as_ref().and_then(|p| p.parent()) else { return };
        if let Err(e) = self.seal.write_files(dir) {
            warn!("cannot write session seal: {e}");
        }
    }

    fn observe_seal(&mut self, t_us: u64, buttons: u32) {
        if !self.seal.enabled() { return; }
        if self.seal.observe(t_us, buttons) {
            self.live.seal_line = self.seal.line.clone();
            info!("{}", self.live.seal_line);
            self.persist_seal();
        }
    }

    fn note_span(&mut self, p: &Pad) {
        let c = p.counter;
        let hw = p.sensor_ts;
        if self.span.n == 0 {
            if let Some(prev) = self.prev_counter {
                if c.wrapping_sub(prev) == 1 { self.span.steps = self.span.steps.saturating_add(1); }
                else { self.span.breaks = self.span.breaks.saturating_add(1); }
                if let Some(prev_hw) = self.prev_hw {
                    if hw.wrapping_sub(prev_hw) == 0 { self.span.stalls = self.span.stalls.saturating_add(1); }
                }
            }
            self.span.seq0 = Some(c);
            self.span.hw0 = Some(hw);
        } else {
            let prev = self.span.seq1.unwrap_or(c);
            let prev_hw = self.span.hw1.unwrap_or(hw);
            if c.wrapping_sub(prev) == 1 { self.span.steps = self.span.steps.saturating_add(1); }
            else { self.span.breaks = self.span.breaks.saturating_add(1); }
            if hw.wrapping_sub(prev_hw) == 0 { self.span.stalls = self.span.stalls.saturating_add(1); }
        }
        self.span.seq1 = Some(c);
        self.span.hw1 = Some(hw);
        self.span.n = self.span.n.saturating_add(1);
        self.span.bytes.push(c);
        self.span.bytes.extend_from_slice(&hw.to_le_bytes());
    }

    fn take_span(&mut self) -> RxSpan {
        let span = std::mem::take(&mut self.span);
        if span.n > 0 {
            self.prev_counter = span.seq1;
            self.prev_hw = span.hw1;
        }
        span
    }

    /// Feed one parsed report received at `t_us`. Returns true if the change-only logger stored an `in` row.
    /// A heartbeat is considered here too, so identical reports still advance `hb`.
    pub fn on_report(&mut self, t_us: u64, p: &Pad) -> bool {
        self.live.reports += 1;
        self.observe_rx(t_us);
        self.note_span(p);
        self.observe_seal(t_us, p.buttons);
        // USB 0x01 and Bluetooth 0x31 both carry this byte (offset 7, or +1 on BT). Copied, not interpreted.
        self.dev_counter = Some(p.counter as u64);
        self.live.last_report_mono_ms = Some(t_us / 1000);
        self.live.l2 = p.l2; self.live.r2 = p.r2; self.live.sticks = [p.lx, p.ly, p.rx, p.ry];
        self.live.buttons = hid::names(p.buttons).into_iter().map(String::from).collect();
        let ev = self.detector.feed(t_us, p);
        self.pending.extend(ev);
        self.live.haptic = self.detector.live.clone();
        let log = match self.last_logged {
            None => true,
            Some((t0, q)) => {
                if q.buttons != p.buttons {
                    let rise = p.buttons & !q.buttons;
                    self.live.presses += rise.count_ones() as u64;
                    let t_ms = self.wall_anchor_ms + t_us / 1000;
                    for name in hid::names(rise) {
                        self.live.recent_presses.push(LivePress { seq: self.live.presses_seq_next(), t_ms, button: name.to_string() });
                    }
                    let n = self.live.recent_presses.len();
                    if n > RECENT_PRESSES { self.live.recent_presses.drain(..n - RECENT_PRESSES); }
                    true
                } else {
                    let analog = (q.lx, q.ly, q.rx, q.ry, q.l2, q.r2) != (p.lx, p.ly, p.rx, p.ry, p.l2, p.r2);
                    analog && t_us.saturating_sub(t0) >= ANALOG_MIN_GAP_US
                }
            }
        };
        if log {
            self.pending.push(record(t_us, p));
            self.last_logged = Some((t_us, *p));
            self.live.logged += 1;
        }
        self.poll_heartbeat(t_us);
        log
    }

    /// A read timeout or a failed read in the HID loop. Does not invent a received report.
    pub fn note_fault(&mut self, t_us: u64) {
        self.faults_since_hb = self.faults_since_hb.saturating_add(1);
        self.poll_heartbeat(t_us);
    }

    /// `read` returned fewer bytes than a USB 0x01 report.
    pub fn note_short_read(&mut self, t_us: u64) {
        self.short_reads = self.short_reads.saturating_add(1);
        self.poll_heartbeat(t_us);
    }

    /// `read` returned a full-sized buffer that was not a DualSense input report.
    pub fn note_parse_fault(&mut self, t_us: u64) {
        self.parse_faults = self.parse_faults.saturating_add(1);
        self.poll_heartbeat(t_us);
    }

    /// Gap is taken from the previous received report before `last_rx_t_us` is replaced.
    fn observe_rx(&mut self, t_us: u64) {
        if let Some(prev) = self.last_rx_t_us {
            let gap = t_us.saturating_sub(prev);
            if gap > self.max_rx_gap_us { self.max_rx_gap_us = gap; }
            if gap > STALE_RX_GAP_US { self.stale_over_50ms = self.stale_over_50ms.saturating_add(1); }
        }
        if self.first_rx_t_us.is_none() { self.first_rx_t_us = Some(t_us); }
        self.last_rx_t_us = Some(t_us);
    }

    fn reset_stale_telemetry(&mut self) {
        self.max_rx_gap_us = 0;
        self.stale_over_50ms = 0;
        self.short_reads = 0;
        self.parse_faults = 0;
        self.faults_since_hb = 0;
        self.first_rx_t_us = None;
        self.last_rx_t_us = None;
        self.dev_counter = None;
        self.span = RxSpan::default();
        self.prev_counter = None;
        self.prev_hw = None;
    }

    fn poll_heartbeat(&mut self, t_us: u64) {
        if !self.live.enabled { return; }
        let due = match self.last_hb_t_us {
            None => t_us >= HB_INTERVAL_US,
            Some(prev) => t_us.saturating_sub(prev) >= HB_INTERVAL_US,
        };
        if !due { return; }
        let rx_total = self.live.reports;
        let rx_delta = rx_total.saturating_sub(self.rx_at_last_hb);
        let span = self.take_span();
        let (seq0, seq1, hw0, hw1, hw_advance, rx_bind) = if span.n == 0 {
            (Value::Null, Value::Null, Value::Null, Value::Null, Value::Null, Value::Null)
        } else {
            let advance = span.hw1.unwrap_or(0).wrapping_sub(span.hw0.unwrap_or(0)) as u64;
            (json!(span.seq0.unwrap_or(0)), json!(span.seq1.unwrap_or(0)),
             json!(span.hw0.unwrap_or(0)), json!(span.hw1.unwrap_or(0)),
             json!(advance), json!(ev::sha256_hex(&span.bytes)))
        };
        self.pending.push(json!({
            "type": "hb",
            "t_us": t_us,
            "hb_seq": self.hb_seq,
            "epoch": self.epoch,
            "rx_total": rx_total,
            "rx_delta": rx_delta,
            "last_rx_t_us": self.last_rx_t_us.map(Value::from).unwrap_or(Value::Null),
            "dev_counter": self.dev_counter.map(Value::from).unwrap_or(Value::Null),
            "faults": self.faults_since_hb,
            "max_rx_gap_us": self.max_rx_gap_us,
            "stale_over_50ms": self.stale_over_50ms,
            "short_reads": self.short_reads,
            "parse_faults": self.parse_faults,
            "first_rx_t_us": self.first_rx_t_us.map(Value::from).unwrap_or(Value::Null),
            "seq0": seq0,
            "seq1": seq1,
            "seq_steps": span.steps,
            "seq_breaks": span.breaks,
            "hw0": hw0,
            "hw1": hw1,
            "hw_advance": hw_advance,
            "hw_stalls": span.stalls,
            "rx_bind": rx_bind,
        }));
        self.hb_seq = self.hb_seq.saturating_add(1);
        self.rx_at_last_hb = rx_total;
        self.faults_since_hb = 0;
        self.max_rx_gap_us = 0;
        self.stale_over_50ms = 0;
        self.short_reads = 0;
        self.parse_faults = 0;
        self.first_rx_t_us = None;
        self.last_hb_t_us = Some(t_us);
    }

    pub fn on_status(&mut self, t_us: u64, state: &str, detail: &str) {
        self.on_status_with_id(t_us, state, detail, None);
    }

    /// One successful HID `open`, including a reopen that keeps the same id string.
    pub fn on_handle_open(&mut self, t_us: u64, detail: &str, pad_id: &str) {
        self.live.state = "opening".into();
        let id = if pad_id.is_empty() { None } else { Some(pad_id) };
        self.on_status_with_id(t_us, "connected", detail, id);
    }

    /// `pad_id` is the USB serial (`serial:…`), a device instance id (`instance:…`), or a hidraw id
    /// (`hidraw:…`). A new recording requires it on every `connected` line. Passing `None` does not
    /// clear an id already stored for this same `connected` stretch.
    pub fn on_status_with_id(&mut self, t_us: u64, state: &str, detail: &str, pad_id: Option<&str>) {
        let id = pad_id.map(str::trim).filter(|s| !s.is_empty()).map(str::to_string);
        let same_id = match &id {
            None => true,
            Some(new) => self.live.pad_id.as_ref() == Some(new),
        };
        if self.live.state == state && same_id { return; }
        // A stall that recovers on the same handle is not a new epoch: resetting here would hide the gap.
        let handle_open = state == "connected" && !matches!(self.live.state.as_str(), "connected" | "stalled");
        let identity = state == "connected" && self.live.state == "connected" && self.live.pad_id.is_some() && !same_id;
        if handle_open || identity {
            self.epoch = self.epoch.saturating_add(1);
            self.reset_stale_telemetry();
        }
        self.live.state = state.into();
        self.live.warning = match state {
            "connected" => None,
            "missing" => Some("Controller not detected over USB: input proof is OFF for this stretch. Check the USB cable into the laptop (Bluetooth to the PS5 stays as is).".into()),
            "unplugged" => Some("Controller USB link dropped: input proof paused, retrying.".into()),
            "stalled" => Some("Controller connected but no HID reports for 2 s: input proof paused.".into()),
            _ => None,
        };
        if state == "connected" {
            self.live.device = Some(detail.into());
            if id.is_some() { self.live.pad_id = id.clone(); }
            // Fresh filters + calibration after every (re)connect; keep the run's event counters.
            let (e, lc) = (self.detector.live.events, self.detector.live.low_confidence);
            self.detector = Detector::new(self.wall_anchor_ms);
            self.detector.live.events = e; self.detector.live.low_confidence = lc;
        } else {
            self.live.pad_id = None;
        }
        let mut rec = json!({"type": "status", "t_us": t_us, "state": state, "detail": detail});
        if state == "connected" {
            if let Some(id) = &id { rec["pad_id"] = json!(id); }
        }
        self.pending.push(rec);
        if state != "connected" { self.last_logged = None; }
    }

    /// Human-readable connect line. The structured `pad_id` is what the proof compares.
    pub fn connect_detail(name: &str, pad_id: &str) -> String {
        if let Some(serial) = pad_id.strip_prefix("serial:") {
            format!("{name} serial={serial}")
        } else if pad_id.is_empty() {
            name.to_string()
        } else {
            format!("{name} id={pad_id}")
        }
    }

    #[cfg(test)]
    pub(crate) fn test_clear_pad_identity_requirement(&mut self) {
        if let Some(h) = self.pending.iter_mut().find(|r| r["type"] == "header") {
            if let Some(o) = h.as_object_mut() { o.remove("pad_identity"); }
        }
    }

    /// Records not yet sealed (used by `input-test`, which never seals).
    pub fn pending(&self) -> &[Value] { &self.pending }

    pub fn path(&self) -> Option<&Path> { self.path.as_deref() }

    /// Seal everything pending into chunk `seg`. Returns (chunk_sha256, chain_head, n_records).
    pub fn seal(&mut self, seg: u64, t_us: u64) -> (String, String, usize) {
        if self.live.reports > 0 {
            let now = (self.detector.live.imu_present, self.detector.live.imu_rate_hz);
            let changed = match self.imu_committed {
                None => true,
                Some((p, r)) => p != now.0 || (r as i64 - now.1 as i64).abs() * 10 > r.max(1) as i64,
            };
            if changed {
                self.pending.push(json!({"type": "imu", "t_us": t_us, "present": now.0, "rate_hz": now.1, "reports": self.detector.live.imu_reports}));
                self.imu_committed = Some(now);
            }
        }
        let mut recs = std::mem::take(&mut self.pending);
        for r in recs.iter_mut() {
            r["seg"] = json!(seg);
            if r["type"] == "haptic" { r["chunk_index"] = json!(seg); }
        }
        let sha = ev::jcs_sha(&Value::Array(recs.clone()));
        let n = recs.len();
        if n == 0 { return (sha, self.head.clone(), 0); }
        let prev = self.head.clone();
        let head = ev::link(&prev, self.chunks, &sha);
        let chunk = json!({"type": "chunk", "seq": self.chunks, "seg": seg, "n": n, "sha256": sha, "prev": prev, "head": head, "t_us": t_us});
        let mut body = String::new();
        for r in recs.iter().chain(std::iter::once(&chunk)) { body.push_str(&r.to_string()); body.push('\n'); }
        if let Some(p) = &self.path {
            if let Some(d) = p.parent() { let _ = std::fs::create_dir_all(d); }
            if let Err(e) = append_bytes(p, body.as_bytes()) {
                // The chunk is not on disk, so the chain must not move. The records stay pending and
                // the next segment seals them. A checkpoint for this segment then commits the empty hash.
                warn!("cannot write {}: {e}; input chunk not sealed (chain unchanged)", p.display());
                self.pending = recs;
                return (empty_hash(), self.head.clone(), 0);
            }
        }
        if let Some(hp) = &self.haptic_path {
            let hs: String = recs.iter().filter(|r| r["type"] == "haptic").map(|r| format!("{r}\n")).collect();
            if !hs.is_empty() {
                if let Err(e) = append_bytes(hp, hs.as_bytes()) {
                    warn!("cannot write {}: {e}", hp.display());
                }
            }
        }
        self.head = head.clone(); self.chunks += 1; self.total_records += n as u64;
        (sha, head, n)
    }
}

pub fn record(t_us: u64, p: &Pad) -> Value {
    json!({"type": "in", "t_us": t_us, "b": p.buttons, "lx": p.lx, "ly": p.ly, "rx": p.rx, "ry": p.ry, "l2": p.l2, "r2": p.r2,
           "c": p.counter, "hw": p.sensor_ts})
}

/// `inputs.jsonl` -> `haptics.jsonl`; `<out>.inputs.jsonl` -> `<out>.haptics.jsonl`.
pub fn haptics_file_name(inputs: &Path) -> String {
    let n = inputs.file_name().map(|s| s.to_string_lossy().to_string()).unwrap_or_default();
    match n.strip_suffix("inputs.jsonl") { Some(pre) => format!("{pre}haptics.jsonl"), None => format!("{n}.haptics.jsonl") }
}

/// Background reader: opens the pad, stamps reports on `clock`, retries forever while `stop` is false.
pub fn spawn_logger(log: Arc<Mutex<InputLog>>, clock: Instant, stop: Arc<AtomicBool>) -> std::thread::JoinHandle<()> {
    spawn_logger_with(log, clock, stop, hid::open_dualsense)
}

pub fn spawn_logger_with<F>(log: Arc<Mutex<InputLog>>, clock: Instant, stop: Arc<AtomicBool>, open: F) -> std::thread::JoinHandle<()>
where F: Fn() -> Result<Box<dyn ReportSource>, String> + Send + 'static {
    std::thread::spawn(move || {
        let us = || clock.elapsed().as_micros() as u64;
        let mut last_warn: Option<Instant> = None;
        let mut buf = [0u8; 128];
        while !stop.load(Ordering::SeqCst) {
            match open() {
                Err(e) => {
                    let first = log.lock().unwrap_or_else(|e| e.into_inner()).live.state != "missing";
                    log.lock().unwrap_or_else(|e| e.into_inner()).on_status(us(), "missing", &e);
                    if first || last_warn.map(|t| t.elapsed() > Duration::from_secs(60)).unwrap_or(true) {
                        warn!("CONTROLLER: {e}. Input proof is OFF until the DualSense is visible over USB (recording continues).");
                        last_warn = Some(Instant::now());
                    }
                    sleep_unless(&stop, 1000);
                }
                Ok(mut dev) => {
                    let name = dev.name();
                    let pad_id = dev.pad_id();
                    let detail = InputLog::connect_detail(&name, &pad_id);
                    info!("CONTROLLER: reading {name} id={pad_id} HID reports on the QorCap clock");
                    log.lock().unwrap_or_else(|e| e.into_inner()).on_handle_open(us(), &detail, &pad_id);
                    let mut last_rx = Instant::now();
                    while !stop.load(Ordering::SeqCst) {
                        match dev.read(&mut buf, 100) {
                            Ok(Some(n)) => {
                                let t = us();
                                if let Some(p) = hid::parse(&buf[..n]) {
                                    let mut l = log.lock().unwrap_or_else(|e| e.into_inner());
                                    if l.live.state != "connected" { l.on_status_with_id(t, "connected", &detail, Some(&pad_id)); }
                                    l.on_report(t, &p);
                                    last_rx = Instant::now();
                                } else {
                                    let mut l = log.lock().unwrap_or_else(|e| e.into_inner());
                                    if n < USB_REPORT_LEN { l.note_short_read(t); } else { l.note_parse_fault(t); }
                                }
                            }
                            Ok(None) => {
                                let t = us();
                                let mut l = log.lock().unwrap_or_else(|e| e.into_inner());
                                l.note_fault(t);
                                if last_rx.elapsed() > Duration::from_secs(2) && l.live.state == "connected" {
                                    warn!("CONTROLLER: no HID reports for 2 s");
                                    l.on_status(t, "stalled", "no reports for 2 s");
                                }
                            }
                            Err(e) => {
                                warn!("CONTROLLER: USB HID read failed ({e}); retrying");
                                let t = us();
                                let mut l = log.lock().unwrap_or_else(|e| e.into_inner());
                                l.note_fault(t);
                                l.on_status(t, "unplugged", &e);
                                break;
                            }
                        }
                    }
                    sleep_unless(&stop, 500);
                }
            }
        }
    })
}

fn sleep_unless(stop: &AtomicBool, ms: u64) {
    let end = Instant::now() + Duration::from_millis(ms);
    while Instant::now() < end && !stop.load(Ordering::SeqCst) { std::thread::sleep(Duration::from_millis(50)); }
}

/// Parse inputs.jsonl into (records, chunks). Tolerates up to 3 corrupt lines like the other bundle files.
pub fn read_file(p: &Path) -> Result<(Vec<Value>, Vec<Value>), String> {
    let s = read_shared_string(p).map_err(|e| format!("{}: {e}", p.display()))?;
    let (mut recs, mut chunks, mut bad) = (vec![], vec![], 0);
    for l in s.lines().filter(|l| !l.trim().is_empty()) {
        match serde_json::from_str::<Value>(l) {
            Ok(v) if v["type"] == "chunk" => chunks.push(v),
            Ok(v) => recs.push(v),
            Err(_) => { bad += 1; if bad > 3 { return Err(format!("{}: too many corrupt lines", p.display())); } }
        }
    }
    Ok((recs, chunks))
}

/// Re-hash inputs.jsonl chunks and their chain. Returns (chunk sha by seg, final head, errors).
pub fn verify_chunks(recs: &[Value], chunks: &[Value]) -> (std::collections::HashMap<u64, String>, String, Vec<String>) {
    let mut by_seg: std::collections::BTreeMap<u64, Vec<Value>> = Default::default();
    for r in recs { by_seg.entry(r["seg"].as_u64().unwrap_or(u64::MAX)).or_default().push(r.clone()); }
    let (mut head, mut errs, mut out) = (GENESIS.to_string(), vec![], std::collections::HashMap::new());
    for (i, c) in chunks.iter().enumerate() {
        let seg = c["seg"].as_u64().unwrap_or(u64::MAX);
        let rs = by_seg.remove(&seg).unwrap_or_default();
        let sha = ev::jcs_sha(&Value::Array(rs.clone()));
        if c["sha256"].as_str() != Some(sha.as_str()) { errs.push(format!("inputs chunk {i} (segment {seg}): SHA-256 mismatch (input records altered)")); }
        if c["n"].as_u64() != Some(rs.len() as u64) { errs.push(format!("inputs chunk {i}: record count mismatch")); }
        if c["prev"].as_str() != Some(head.as_str()) { errs.push(format!("inputs chunk {i}: prev link broken")); }
        let h = ev::link(&head, i as u64, &sha);
        if c["head"].as_str() != Some(h.as_str()) { errs.push(format!("inputs chunk {i}: chain head mismatch")); }
        head = h; out.insert(seg, sha);
    }
    for (seg, rs) in by_seg { errs.push(format!("inputs: {} record(s) for segment {seg} not covered by any chunk", rs.len())); }
    (out, head, errs)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn pad(b: u32, l2: u8) -> Pad { Pad { buttons: b, l2, ..Pad::neutral() } }
    #[test]
    fn recent_presses_keep_short_taps_for_the_console() {
        let mut l = InputLog::new(None, true, 1_700_000_000_000);
        l.on_report(1_000, &pad(0, 0));
        // A 40 ms tap a 125 ms /status poll would miss, then a rolled d-pad (UP then UP+RIGHT).
        l.on_report(100_000, &pad(hid::LEFT, 0));
        l.on_report(140_000, &pad(0, 0));
        l.on_report(300_000, &pad(hid::UP, 0));
        l.on_report(333_000, &pad(hid::UP | hid::RIGHT, 0));
        l.on_report(400_000, &pad(0, 0));
        assert!(l.live.buttons.is_empty(), "nothing held at the end, so held-state polling sees no taps");
        let got: Vec<(u64, u64, &str)> = l.live.recent_presses.iter().map(|p| (p.seq, p.t_ms, p.button.as_str())).collect();
        assert_eq!(got, vec![(1, 1_700_000_000_100, "LEFT"), (2, 1_700_000_000_300, "UP"), (3, 1_700_000_000_333, "RIGHT")]);
        assert_eq!(l.live.presses, 3);
        for i in 0..(RECENT_PRESSES as u64 + 10) {
            l.on_report(500_000 + i * 2_000, &pad(hid::CROSS, 0));
            l.on_report(501_000 + i * 2_000, &pad(0, 0));
        }
        assert_eq!(l.live.recent_presses.len(), RECENT_PRESSES);
        assert_eq!(l.live.recent_presses.last().unwrap().seq, 3 + RECENT_PRESSES as u64 + 10);
    }

    #[test]
    fn seal_hashes_records_and_chains() {
        let dir = std::env::temp_dir().join(format!("qorcap-inputs-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let p = dir.join("inputs.jsonl");
        let mut l = InputLog::new(Some(p.clone()), true, 1_700_000_000_000);
        l.on_status(10, "connected", "DualSense (USB)");
        assert!(l.on_report(1_000, &pad(0, 0)));
        assert!(!l.on_report(2_000, &pad(0, 0)), "identical report not logged");
        assert!(l.on_report(3_000, &pad(hid::CROSS, 0)), "button change always logged");
        assert!(!l.on_report(4_000, &pad(hid::CROSS, 5)), "analog change within 4 ms is decimated");
        assert!(l.on_report(8_000, &pad(hid::CROSS, 9)));
        let (sha0, head0, n0) = l.seal(1, 9_000);
        assert_eq!(n0, 6); // header, status, 3 records, imu presence
        assert_ne!(sha0, empty_hash());
        let (sha1, head1, n1) = l.seal(2, 11_000);
        assert_eq!((sha1.as_str(), head1.as_str(), n1), (empty_hash().as_str(), head0.as_str(), 0), "empty chunk = legacy hash, chain unchanged");
        let (recs, chunks) = read_file(&p).unwrap();
        assert_eq!((recs.len(), chunks.len()), (6, 1));
        assert_eq!((recs[5]["type"].as_str(), recs[5]["present"].as_bool()), (Some("imu"), Some(false)));
        let (m, head, errs) = verify_chunks(&recs, &chunks);
        assert!(errs.is_empty(), "{errs:?}");
        assert_eq!((m[&1].as_str(), head.as_str()), (sha0.as_str(), head0.as_str()));
        // tamper: change one button value
        let mut bad = recs.clone(); bad[3]["b"] = json!(0);
        assert!(!verify_chunks(&bad, &chunks).2.is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn sharing_violation_retries_then_other_errors_do_not() {
        let e = std::io::Error::from_raw_os_error(32);
        assert!(sharing_violation(&e));
        assert!(!sharing_violation(&std::io::Error::new(std::io::ErrorKind::NotFound, "missing")));
        let mut n = 0u32;
        let mut waits = 0u32;
        let got = retry_sharing(|| {
            n += 1;
            if n < 3 { Err(std::io::Error::from_raw_os_error(32)) } else { Ok(7u8) }
        }, 8, |_| { waits += 1; });
        assert_eq!((got.unwrap(), n, waits), (7, 3, 2));
        let mut once = 0u32;
        let err = retry_sharing(|| { once += 1; Err::<(), _>(std::io::Error::new(std::io::ErrorKind::PermissionDenied, "no")) }, 8, |_| {});
        assert!(err.is_err());
        assert_eq!(once, 1);
    }

    #[test]
    fn failed_seal_keeps_the_chain_and_the_records() {
        let dir = std::env::temp_dir().join(format!("qorcap-inputs-busy-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        // A directory where the file should be: the append fails, and it is not a sharing violation.
        let bad = dir.join("inputs.jsonl");
        std::fs::create_dir(&bad).unwrap();
        let mut l = InputLog::new(Some(bad), true, 0);
        l.on_status(1, "connected", "DualSense");
        let pending_before = l.pending().len();
        let (sha, head, n) = l.seal(1, 10);
        assert_eq!((n, l.chunks, sha.as_str(), head.as_str()), (0, 0, empty_hash().as_str(), GENESIS));
        assert_eq!(l.pending().len(), pending_before, "records stay pending for the next seal");
        let _ = std::fs::remove_dir_all(&dir);
    }
    #[test]
    fn missing_controller_is_recorded_and_warned() {
        let log = Arc::new(Mutex::new(InputLog::new(None, true, 0)));
        let stop = Arc::new(AtomicBool::new(false));
        let h = spawn_logger_with(log.clone(), Instant::now(), stop.clone(), || Err("no DualSense found on USB".into()));
        std::thread::sleep(Duration::from_millis(150));
        stop.store(true, Ordering::SeqCst); h.join().unwrap();
        let mut l = log.lock().unwrap_or_else(|e| e.into_inner());
        assert_eq!(l.live.state, "missing");
        assert!(l.live.warning.as_deref().unwrap().contains("not detected"));
        let (_, _, n) = l.seal(0, 1);
        assert_eq!(n, 2); // header + missing status: absence is committed
    }
    struct Fake { reps: Vec<Vec<u8>>, i: usize }
    impl ReportSource for Fake {
        fn read(&mut self, buf: &mut [u8], _t: i32) -> Result<Option<usize>, String> {
            if self.i >= self.reps.len() { return Err("unplugged".into()); }
            let r = &self.reps[self.i]; self.i += 1; buf[..r.len()].copy_from_slice(r);
            std::thread::sleep(Duration::from_millis(1)); Ok(Some(r.len()))
        }
        fn name(&self) -> String { "Fake DualSense (USB)".into() }
        fn pad_id(&self) -> String { "serial:FAKE0001".into() }
    }
    #[test]
    fn logger_reads_reports_then_handles_unplug() {
        let log = Arc::new(Mutex::new(InputLog::new(None, true, 0)));
        let stop = Arc::new(AtomicBool::new(false));
        let opened = Arc::new(AtomicBool::new(false));
        let o2 = opened.clone();
        let h = spawn_logger_with(log.clone(), Instant::now(), stop.clone(), move || {
            if o2.swap(true, Ordering::SeqCst) { return Err("no DualSense found on USB".into()); }
            Ok(Box::new(Fake { reps: vec![hid::pack_usb(&pad(0, 0)), hid::pack_usb(&pad(hid::CROSS, 0)), hid::pack_usb(&pad(0, 0))], i: 0 }))
        });
        std::thread::sleep(Duration::from_millis(800));
        stop.store(true, Ordering::SeqCst); h.join().unwrap();
        let l = log.lock().unwrap_or_else(|e| e.into_inner());
        assert_eq!((l.live.reports, l.live.presses), (3, 1));
        assert!(l.live.state == "missing" || l.live.state == "unplugged");
        let states: Vec<&str> = l.pending.iter().filter_map(|r| r["state"].as_str()).collect();
        assert_eq!(&states[..2], &["connected", "unplugged"]);
        let connected = l.pending.iter().find(|r| r["state"] == "connected").unwrap();
        assert_eq!(connected["pad_id"], "serial:FAKE0001");
        assert!(connected["detail"].as_str().unwrap().contains("serial=FAKE0001"), "{}", connected["detail"]);
        assert!(l.pending.iter().any(|r| r["type"] == "header" && r["pad_identity"] == "required"));
    }

    #[test]
    fn heartbeat_follows_received_reports_not_logged_changes() {
        let dir = std::env::temp_dir().join(format!("qorcap-inputs-hb-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let path = dir.join("inputs.jsonl");
        let mut l = InputLog::new(Some(path.clone()), true, 0);
        l.on_status_with_id(0, "connected", "DualSense Edge (USB)", Some("serial:EDGE1"));
        let mut quiet = pad(0, 0);
        quiet.counter = 9;
        let mut t = 0u64;
        while t <= 5 * HB_INTERVAL_US {
            l.on_report(t, &quiet);
            t += 4_000;
        }
        let hbs: Vec<Value> = l.pending().iter().filter(|r| r["type"] == "hb").cloned().collect();
        assert_eq!(hbs.len(), 5, "five seconds of unchanged reports still write one hb per second: {hbs:?}");
        assert!(hbs.iter().all(|h| h["rx_delta"].as_u64().unwrap_or(0) > 0));
        assert_eq!((hbs[0]["hb_seq"].as_u64(), hbs[4]["hb_seq"].as_u64()), (Some(0), Some(4)));
        assert_eq!(hbs[0]["epoch"], 1);
        assert_eq!(hbs[0]["dev_counter"], 9);
        assert_eq!(l.pending().iter().filter(|r| r["type"] == "in").count(), 1);
        let last_rx = hbs[4]["last_rx_t_us"].as_u64().unwrap();
        l.note_fault(last_rx + HB_INTERVAL_US);
        l.note_fault(last_rx + 2 * HB_INTERVAL_US);
        let stall = l.pending().iter().rev().find(|r| r["type"] == "hb").unwrap();
        assert_eq!(stall["rx_delta"], 0);
        assert_eq!(stall["last_rx_t_us"], last_rx);
        assert!(stall["t_us"].as_u64().unwrap() > last_rx);
        assert!(stall["faults"].as_u64().unwrap() >= 1);
        let epoch_before = stall["epoch"].as_u64().unwrap();
        let stall_t = stall["t_us"].as_u64().unwrap();
        l.on_status(stall_t + 1_000, "unplugged", "usb disconnect");
        l.on_status_with_id(stall_t + 2_000, "connected", "DualSense Edge (USB)", Some("serial:EDGE1"));
        l.note_fault(stall_t + HB_INTERVAL_US);
        let rejoined = l.pending().iter().rev().find(|r| r["type"] == "hb").unwrap();
        assert!(rejoined["epoch"].as_u64().unwrap() > epoch_before, "reconnect bumps epoch");
        let states: Vec<&str> = l.pending().iter().filter_map(|r| r["state"].as_str()).collect();
        assert!(states.windows(2).any(|w| w == ["unplugged", "connected"]), "{states:?}");
        let (sha, head, n) = l.seal(3, stall_t + HB_INTERVAL_US);
        assert!(n > 5);
        assert_ne!(sha, empty_hash());
        let (recs, chunks) = read_file(&path).unwrap();
        let sealed: Vec<&Value> = recs.iter().filter(|r| r["type"] == "hb").collect();
        assert!(sealed.len() >= 5);
        assert!(sealed.iter().all(|r| r["seg"] == 3));
        let (_, chain, errs) = verify_chunks(&recs, &chunks);
        assert!(errs.is_empty(), "{errs:?}");
        assert_eq!(chain, head);
        for row in sealed.iter().take(3) {
            println!("{row}");
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn heartbeat_keeps_a_recovered_gap_and_resets_on_reconnect() {
        let mut l = InputLog::new(None, true, 0);
        l.on_handle_open(0, "DualSense Edge (USB)", "serial:EDGE1");
        assert_eq!(l.epoch, 1);
        let mut quiet = pad(0, 0);
        quiet.counter = 9;
        let mut t = 0u64;
        while t <= HB_INTERVAL_US {
            l.on_report(t, &quiet);
            t += 4_000;
        }
        // 300 ms with no reports, then the link recovers inside the next interval.
        // The gap starts at the previous interval's last report.
        let resume = HB_INTERVAL_US + 300_000;
        l.on_report(resume, &quiet);
        t = resume + 4_000;
        while t <= 2 * HB_INTERVAL_US {
            l.on_report(t, &quiet);
            t += 4_000;
        }
        let recovered = l.pending().iter().rev().find(|r| r["type"] == "hb").unwrap().clone();
        assert_eq!(recovered["t_us"], 2 * HB_INTERVAL_US);
        assert!(recovered["max_rx_gap_us"].as_u64().unwrap() >= 300_000, "{recovered}");
        assert!(recovered["stale_over_50ms"].as_u64().unwrap() >= 1, "{recovered}");
        assert_eq!(recovered["last_rx_t_us"], 2 * HB_INTERVAL_US, "last_rx is fresh and must not hide the gap");
        assert_eq!(recovered["first_rx_t_us"], resume);
        assert_eq!(recovered["dev_counter"], 9);
        assert_eq!(recovered["short_reads"], 0);
        assert_eq!(recovered["parse_faults"], 0);
        // Same handle, stalled, then a report: the gap stays. This is not a new epoch.
        let epoch_at_recovery = l.epoch;
        l.on_status(2 * HB_INTERVAL_US + 1_000, "stalled", "no reports for 2 s");
        l.on_status_with_id(2 * HB_INTERVAL_US + 350_000, "connected", "DualSense Edge (USB)", Some("serial:EDGE1"));
        assert_eq!(l.epoch, epoch_at_recovery, "stall recovery on the same handle does not bump epoch");
        l.note_short_read(2 * HB_INTERVAL_US + 10);
        l.note_parse_fault(2 * HB_INTERVAL_US + 20);
        l.note_fault(2 * HB_INTERVAL_US + 30);
        l.note_fault(3 * HB_INTERVAL_US);
        let poisoned = l.pending().iter().rev().find(|r| r["type"] == "hb").unwrap().clone();
        assert_eq!(poisoned["short_reads"], 1);
        assert_eq!(poisoned["parse_faults"], 1);
        assert!(poisoned["faults"].as_u64().unwrap() >= 1);
        l.on_status(3 * HB_INTERVAL_US + 1_000, "unplugged", "usb disconnect");
        l.on_handle_open(3 * HB_INTERVAL_US + 2_000, "DualSense Edge (USB)", "serial:EDGE1");
        assert!(l.epoch > epoch_at_recovery, "reconnect bumps epoch");
        let after_reopen = l.epoch;
        l.on_handle_open(3 * HB_INTERVAL_US + 3_000, "DualSense Edge (USB)", "serial:EDGE1");
        assert!(l.epoch > after_reopen, "a new handle bumps epoch even when the id string is unchanged");
        let after_same_id = l.epoch;
        l.on_status_with_id(3 * HB_INTERVAL_US + 4_000, "connected", "DualSense Edge (USB)", Some("serial:EDGE2"));
        assert!(l.epoch > after_same_id, "a device identity change bumps epoch");
        let base = 4 * HB_INTERVAL_US;
        t = base;
        while t <= base + HB_INTERVAL_US {
            l.on_report(t, &quiet);
            t += 4_000;
        }
        let after: Vec<Value> = l.pending().iter().filter(|r| r["type"] == "hb" && r["t_us"].as_u64().unwrap_or(0) >= base).cloned().collect();
        assert!(after.len() >= 2, "{after:?}");
        assert_eq!(after[0]["t_us"], base);
        assert_eq!(after[0]["max_rx_gap_us"], 0, "the gap across reconnect is not a stall: {after:?}");
        assert_eq!(after[0]["first_rx_t_us"], base);
        assert_eq!(after[0]["short_reads"], 0);
        assert_eq!(after[0]["parse_faults"], 0);
        assert_eq!(after[0]["faults"], 0);
        assert_eq!(after[0]["stale_over_50ms"], 0);
        let clean = after.last().unwrap();
        assert_eq!(clean["max_rx_gap_us"], 4_000, "only the post-reconnect report spacing remains: {clean}");
        assert_eq!(clean["stale_over_50ms"], 0);
        assert_eq!(clean["short_reads"], 0);
        assert_eq!(clean["parse_faults"], 0);
        assert_eq!(clean["faults"], 0);
        assert_eq!(clean["last_rx_t_us"], base + HB_INTERVAL_US);
        let first = l.pending().iter().find(|r| r["type"] == "hb" && r["t_us"] == HB_INTERVAL_US).unwrap();
        println!("{first}");
        println!("{recovered}");
        println!("{clean}");
    }

    #[test]
    fn quiet_span_with_a_ticking_report_sequence_supports_idle_and_a_zero_does_not() {
        let mut l = InputLog::new(None, true, 0);
        l.on_handle_open(0, "DualSense Edge (USB)", "serial:EDGE1");
        let mut t = 0u64;
        let mut counter: u8 = 0;
        let mut hw: u32 = 1_000;
        while t <= 4 * HB_INTERVAL_US {
            let mut quiet = pad(0, 0);
            quiet.counter = counter;
            quiet.sensor_ts = hw;
            l.on_report(t, &quiet);
            counter = counter.wrapping_add(1);
            hw = hw.wrapping_add(12_000);
            t += 4_000;
        }
        let hbs: Vec<Value> = l.pending().iter().filter(|r| r["type"] == "hb").cloned().collect();
        assert!(hbs.len() >= 4, "{hbs:?}");
        assert!(hbs.iter().all(|h| h["rx_bind"].as_str().unwrap_or("").len() == 64));
        assert!(hbs.iter().all(|h| h["seq_breaks"] == 0), "{hbs:?}");
        assert!(hbs.iter().all(|h| h["hw_advance"].as_u64().unwrap_or(0) > 0));
        let idle = crate::pad_link::classify_span(&hbs, 1_100_000, 3_800_000);
        assert!(idle.is_idle(), "{idle:?}");
        l.note_fault(5 * HB_INTERVAL_US);
        let with_zero: Vec<Value> = l.pending().iter().filter(|r| r["type"] == "hb").cloned().collect();
        let dropped = crate::pad_link::classify_span(&with_zero, 4_100_000, 5 * HB_INTERVAL_US);
        assert_eq!(dropped.class, "drop", "{dropped:?}");
        let unknown = crate::pad_link::classify_span(&hbs, 20 * HB_INTERVAL_US, 21 * HB_INTERVAL_US);
        assert_eq!(unknown.class, "unknown");
        assert!(!unknown.is_idle());
        let header = l.pending().iter().find(|r| r["type"] == "header").unwrap();
        assert!(header["axes"].as_str().unwrap().contains("no deadzone"));
    }

    #[test]
    fn cod_seal_binds_once_and_a_solo_hold_does_not() {
        let dir = std::env::temp_dir().join(format!("qorcap-seal-log-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let mut l = InputLog::new(Some(dir.join("inputs.jsonl")), true, 0);
        l.enable_seal();
        assert_eq!(l.live.seal_line, "Seal: waiting");
        l.on_report(1_000_000, &pad(hid::OPTIONS, 0));
        l.on_report(3_200_000, &pad(0, 0));
        assert!(l.live.seal_line.starts_with("Seal: failed"));
        assert!(dir.join("session_seal.json").is_file());
        assert!(dir.join("pad_session.v0.json").is_file());
        let session: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("session_seal.json")).unwrap()).unwrap();
        let pad_doc: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("pad_session.v0.json")).unwrap()).unwrap();
        assert!(session["seal_hash"].is_null());
        assert_eq!(session["seal_hash"], pad_doc["seal_hash"]);
        assert!(!crate::seal::dual_bind_passes(&session, &pad_doc, None));
        let bound = l.take_seal_for_checkpoint().unwrap();
        assert!(bound["hash"].is_null());
        assert!(l.take_seal_for_checkpoint().is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
