//! `qorcap-hand-timeline/v0` writer: 1 s windows on the engine clock, each sealed as one JCS no-float
//! SHA-256 chain link, plus ONE append-only raw USB report file (`hand_raw.v0.bin`) per session.
//! Platform-neutral (no OS calls besides plain file writes), so every rule here runs in the Linux and
//! Windows unit tests.
//!
//! Window `k` holds every record whose time is in `[k * 1_000_000, (k + 1) * 1_000_000)` us. A window is sealed
//! once the logger's clock is `SEAL_GRACE_US` past its end (frame arrivals come from the capture thread over
//! a channel and may trail by a few ms), or at stop (the trailing window is `partial: true`). A record that
//! arrives for an already sealed window goes into the oldest open window with `late: true`, never into a
//! sealed one, and is counted in that window's `hb`.
//!
//! Seal of window `k` (in this order): the `hb` row and the `raw_bind` row (naming the slice
//! `raw_file`, `raw_offset`, `raw_len`, `raw_sha256` in `hand_raw.v0.bin`) are appended to the window's
//! records; the raw slice (if non-empty) is appended to `hand_raw.v0.bin` and synced; then
//! `records_sha256 = SHA-256(JCS([records...]))`, `head_k = SHA-256(JCS({prev, seq: k, sha256}))`; the
//! records and the `chunk` row are appended to the JSONL and synced. If either write fails, both files
//! are cut back to their last durable lengths and the head does not move: the chain never names bytes
//! that are not on disk.

use super::device::{DeviceInfo, DeviceModel, USB_REPORT_LEN};
use crate::evidence::{self as ev, GENESIS};
use crate::frameclock::FrameStamp;
use crate::hid::{self, CIRCLE, CROSS, SQUARE, TRIANGLE, FN_LEFT, FN_RIGHT, BACK_LEFT, BACK_RIGHT};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

/// Create a new file, refusing a pre-existing path (write-once). On Unix the mode is 0600
/// (owner read/write only). On Windows the process default ACL is used (no extra restriction).
fn open_private_new(path: &Path, append: bool) -> std::io::Result<File> {
    let mut opts = OpenOptions::new();
    opts.create_new(true).write(true);
    if append { opts.append(true); }
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600);
    }
    opts.open(path)
}

pub const SCHEMA: &str = "qorcap-hand-timeline/v0";
pub const SUMMARY_SCHEMA: &str = "qorcap-hand-timeline-summary/v0";
pub const FILE_NAME: &str = "hand_timeline.v0.jsonl";
pub const SUMMARY_FILE: &str = "hand_timeline_summary.v0.json";
/// Single append-only raw USB report file per session (no binary header; pure 80 B v1 fixed records from offset 0).
pub const RAW_FILE: &str = "hand_raw.v0.bin";
pub const RAW_FILE_HEADER_BYTES: u64 = 0;
pub const CHUNK_US: u64 = 1_000_000;
pub const SEAL_GRACE_US: u64 = 250_000;
/// One raw record (v1, 80 B): u64_le qpc_ticks (0 = UNMEASURED) + u64_le t_us (engine Instant) + 64 B 0x01 report.
/// Storing t_us alongside QPC ticks enables measuring the Instant-vs-QPC residual once benchmarked (until then UNMEASURED).
pub const RAW_RECORD_BYTES: usize = 8 + 8 + USB_REPORT_LEN;
pub const RAW_LAYOUT: &str = "v1";
pub const RAW_LAYOUT_SPEC: &str = "u64_le qpc_ticks (0 = UNMEASURED) + u64_le t_us + 64 B USB input report 0x01; 0 B file header";
pub const STALE_GAP_US: u64 = crate::inputs::STALE_RX_GAP_US;
pub const STALL_US: u64 = 2_000_000;
pub const ANALOG_MIN_GAP_US: u64 = crate::inputs::ANALOG_MIN_GAP_US;
/// Per-epoch cap on `bytes_42_43_change` rows (each change emits one `in` + one `status` row) so a flapping byte cannot flood the JSONL.
pub const BYTES_42_43_CHANGE_CAP_PER_EPOCH: u64 = 16;
pub const UNMEASURED: &str = "UNMEASURED";
pub const UNKNOWN: &str = "UNKNOWN";
pub const CLAIMS: &str = "Read-only controller input log clocked to the video: host-observed USB HID input reports and capture frame arrivals on one monotonic clock. It does not identify who held the controller.";
const JCS_MAX: u64 = (1u64 << 53) - 1;
const EDGE_BITS: u32 = FN_LEFT | FN_RIGHT | BACK_LEFT | BACK_RIGHT;
const FACE: [(u32, &str); 4] = [(TRIANGLE, "TRIANGLE"), (CIRCLE, "CIRCLE"), (CROSS, "CROSS"), (SQUARE, "SQUARE")];
const FN: [(u32, &str); 2] = [(FN_LEFT, "FN_L"), (FN_RIGHT, "FN_R")];

/// Windows QPC facts taken at one moment together with the engine-clock `t_us` of that moment.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Qpc { pub freq_hz: u64, pub origin_ticks: u64, pub origin_t_us: u64 }

#[derive(Debug, Clone)]
pub struct Config {
    pub dir: PathBuf,
    /// `"default_d"` | `"override"` | `"fallback_bundle"` | `"direct"` (tests).
    pub hand_dir_source: String,
    pub hand_dir_fallback_reason: Option<String>,
    pub raw_usb: bool,
    /// Raw logging pauses for the rest of the session when free space drops below this (checked every 30 windows).
    pub raw_min_free_bytes: Option<u64>,
    pub wall_anchor_ms: u64,
    pub qpc: Option<Qpc>,
    pub frame_clock_on: bool,
    pub initial_device: Option<DeviceInfo>,
    pub start_t_us: u64,
}

#[derive(Debug, Default)]
struct Hb {
    rx_delta: u64, first_rx: Option<u64>, last_rx: Option<u64>, max_gap: u64, stale: u64,
    short_reads: u64, parse_faults: u64, bt_reports: u64, timeouts: u64, read_errors: u64,
    seq0: Option<u8>, seq1: Option<u8>, seq_steps: u64, seq_breaks: u64,
    hw0: Option<u32>, hw1: Option<u32>, hw_advance: u64, hw_stalls: u64,
    in_rows: u64, frames: u64, pes_reads: u64, late: u64, frame_drops: u64, bytes_42_43_suppressed: u64, models: Vec<&'static str>,
}

#[derive(Debug)]
struct Window { seq: u64, records: Vec<Value>, raw: Vec<u8>, raw_n: u64, hb: Hb }
impl Window { fn new(seq: u64) -> Self { Self { seq, records: vec![], raw: vec![], raw_n: 0, hb: Hb::default() } } }

/// What `finish` returns for the stop checkpoint.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Finish {
    pub file: String,
    pub file_sha256: String,
    pub head: String,
    pub first_seq: Option<u64>,
    pub last_seq: Option<u64>,
    pub chunks: u64,
    pub summary_sha256: Option<String>,
    pub raw_usb: bool,
    pub raw_file: Option<String>,
    pub raw_bytes: u64,
    pub frame_drops: u64,
}

pub struct HandTimeline {
    cfg: Config,
    path: PathBuf,
    file: File,
    durable_len: u64,
    raw_file: Option<File>,
    raw_offset: u64,
    open: BTreeMap<u64, Window>,
    next_seal: u64,
    first_seq: u64,
    head: String,
    last_sealed: Option<u64>,
    chunks: u64,
    epoch: u64,
    device: Option<DeviceInfo>,
    connected: bool,
    connected_at: u64,
    stalled: bool,
    last_rx: Option<u64>,
    last_counter: Option<u8>,
    last_hw: Option<u32>,
    last_in_t: Option<u64>,
    last_in_key: Option<(u32, Option<u8>, [u8; 6])>,
    last_bytes_42_43: Option<[u8; 2]>,
    bytes_42_43_changes_epoch: u64,
    bytes_42_43_changes_suppressed: u64,
    prev_combos: u32,
    disk_faults: u64,
    raw_pause: Option<&'static str>,
    latest_t: u64,
    closed: bool,
    models_seen: Vec<&'static str>,
    reports: u64,
    in_rows: u64,
    frames: u64,
    last_state: String,
    last_detail: String,
    frame_drops: u64,
}

fn n(v: u64) -> Value { if v <= JCS_MAX { json!(v) } else { Value::Null } }
fn opt(v: Option<u64>) -> Value { v.map(n).unwrap_or(Value::Null) }

impl HandTimeline {
    /// Create `hand_timeline.v0.jsonl` (refuses to overwrite an existing one) and queue the header in the first window.
    pub fn open(cfg: Config) -> Result<Self, String> {
        std::fs::create_dir_all(&cfg.dir).map_err(|e| format!("hand timeline: cannot create {}: {e}", cfg.dir.display()))?;
        let path = cfg.dir.join(FILE_NAME);
        let file = open_private_new(&path, true)
            .map_err(|e| format!("hand timeline: cannot create {}: {e}", path.display()))?;
        let raw_file = if cfg.raw_usb {
            let rp = cfg.dir.join(RAW_FILE);
            match open_private_new(&rp, true) {
                Ok(f) => Some(f),
                Err(e) => {
                    let _ = std::fs::remove_file(&path);
                    return Err(format!("hand raw file {RAW_FILE}: cannot create {}: {e}", rp.display()));
                }
            }
        } else {
            None
        };
        let first = cfg.start_t_us / CHUNK_US;
        let mut me = Self {
            path, file, durable_len: 0, raw_file, raw_offset: 0, open: BTreeMap::new(), next_seal: first, first_seq: first, head: GENESIS.to_string(),
            last_sealed: None, chunks: 0, epoch: 0, device: None, connected: false, connected_at: 0, stalled: false,
            last_rx: None, last_counter: None, last_hw: None, last_in_t: None, last_in_key: None,
            last_bytes_42_43: None, bytes_42_43_changes_epoch: 0, bytes_42_43_changes_suppressed: 0, prev_combos: 0,
            disk_faults: 0, raw_pause: None, latest_t: cfg.start_t_us, closed: false, models_seen: vec![], reports: 0, in_rows: 0, frames: 0, last_state: String::new(), last_detail: String::new(), frame_drops: 0,
            cfg,
        };
        let header = me.header();
        me.window(me.cfg.start_t_us).0.records.push(header);
        if let Some(d) = me.cfg.initial_device.clone() { me.on_connected(me.cfg.start_t_us, d); }
        Ok(me)
    }

    fn header(&self) -> Value {
        let c = &self.cfg;
        let model = c.initial_device.as_ref().map(|d| d.model.label()).unwrap_or(UNKNOWN);
        let edge_fields = match c.initial_device.as_ref() { Some(d) if d.model.edge_fields_measured() => "MEASURED", Some(_) => UNMEASURED, None => UNKNOWN };
        let qpc = match c.qpc {
            Some(q) => json!({"status": "MEASURED", "freq_hz": n(q.freq_hz), "origin_ticks": n(q.origin_ticks), "origin_t_us": n(q.origin_t_us)}),
            None => json!({"status": UNMEASURED}),
        };
        let mut h = json!({
            "type": "header", "schema": SCHEMA, "chunk_seq": c.start_t_us / CHUNK_US, "t_us": n(c.start_t_us),
            "clock_domain": "qorcap-engine-instant-us",
            "clock_note": "t_us is the QorCap engine Instant (Windows: QueryPerformanceCounter), the same clock as inputs.jsonl t_us and frame_clock.jsonl t_mono_us. wall_anchor_ms is a label only.",
            "qpc": qpc, "wall_anchor_ms": n(c.wall_anchor_ms), "chunk_duration_us": CHUNK_US, "seal_grace_us": SEAL_GRACE_US,
            "lane": "practice", "transport": "usb-hid", "report_id": 1, "report_bytes": USB_REPORT_LEN as u64,
            "raw_usb": c.raw_usb,
            "raw_file": if c.raw_usb { json!(RAW_FILE) } else { Value::Null },
            "raw_file_header_bytes": RAW_FILE_HEADER_BYTES,
            "raw_record_bytes": RAW_RECORD_BYTES as u64, "raw_layout": RAW_LAYOUT, "raw_layout_spec": RAW_LAYOUT_SPEC,
            "hand_dir": c.dir.to_string_lossy(),
            "hand_dir_source": if c.hand_dir_source.is_empty() { "direct" } else { c.hand_dir_source.as_str() },
            "hand_dir_fallback_reason": c.hand_dir_fallback_reason,
            "in_rows": "change-logged: every button change, analog changes at most every analog_min_gap_us; the raw file has every report",
            "analog_min_gap_us": ANALOG_MIN_GAP_US,
            "frame_clock": if c.frame_clock_on { "on" } else { "off: frame alignment UNMEASURED" },
            "device_model": model,
            "device_policy": {"primary": "dualsense_edge 054c:0df2", "fallback": "dualsense 054c:0ce6 (labelled, never relabelled as edge)", "other": "fail_closed"},
            "edge_fields": edge_fields,
            "edge_bits_note": "On device_model dualsense, FN/BACK bits are omitted from buttons and edge_bits is UNMEASURED. Read edge_bits, not buttons, for Edge-only state; do not treat those bit positions as measured-off.",
            "edge_metadata": {"active_profile": UNKNOWN, "dead_zones": UNKNOWN, "sensitivity_curves": UNKNOWN, "remaps": UNKNOWN,
                "stick_modules": UNKNOWN, "firmware_patch": UNKNOWN, "usb_bcd_device": "MEASURED in status rows"},
            "adaptive_trigger_state": UNMEASURED,
            "report_rate_hz": "UNMEASURED (per-window rx_delta is the count actually seen)",
            "clock_uncertainty_us": {"status": "ESTIMATED", "bound_us": 1000, "basis": "usb_polling_plus_thread_wake"},
            "listen_only": {"access": "GENERIC_READ", "reads": "input report 0x01 via overlapped ReadFile", "output_reports": false,
                "feature_reports": false, "firmware": false, "audio": false},
            "claims": CLAIMS,
        });
        let physical_face_status = match c.initial_device.as_ref() {
            Some(d) if d.model.edge_fields_measured() => "ESTIMATED",
            Some(_) => UNMEASURED,
            None => UNKNOWN,
        };
        h["physical_face_status"] = json!(physical_face_status);
        h["physical_face_note"] = json!("On device_model dualsense_edge, raw full byte 51 of USB report 0x01 on in rows (ESTIMATED; low nibble mirrors the d-pad, consumers must mask 0xF0 for face buttons; buttons stays unmodified post-remap). On device_model dualsense, physical_face is UNMEASURED.");
        h["bytes_42_43_raw"] = json!(UNKNOWN);
        h["bytes_42_43_status"] = json!("ESTIMATED");
        h["bytes_42_43_change_cap_per_epoch"] = json!(BYTES_42_43_CHANGE_CAP_PER_EPOCH);
        h["bytes_42_43_note"] = json!("raw bytes 42-43 of USB report 0x01 (n=1 hypothesis: trigger-setting snapshot, ESTIMATED); UNKNOWN before the first report after connect, snapshotted on the first in row and emitted on change (in + status rows, capped per epoch); adaptive_trigger_state stays UNMEASURED");
        h
    }

    /// The window a record at `t` belongs to, and whether it is late (its own window is already sealed).
    fn window(&mut self, t: u64) -> (&mut Window, bool) {
        let s = t / CHUNK_US;
        let (s, late) = if s < self.next_seal { (self.next_seal, true) } else { (s, false) };
        (self.open.entry(s).or_insert_with(|| Window::new(s)), late)
    }

    fn push(&mut self, t: u64, mut rec: Value) {
        let (w, late) = self.window(t);
        rec["chunk_seq"] = json!(w.seq);
        if late { rec["late"] = json!(true); w.hb.late += 1; }
        w.records.push(rec);
    }

    fn model_label(&self) -> &'static str { self.device.as_ref().map(|d| d.model.label()).unwrap_or(UNKNOWN) }

    /// A pad was opened for reading. Every open is a new epoch.
    pub fn on_connected(&mut self, t: u64, d: DeviceInfo) {
        if self.closed { return; }
        self.advance(t);
        self.epoch += 1;
        self.connected = true;
        self.connected_at = t;
        self.stalled = false;
        self.last_counter = None;
        self.last_hw = None;
        self.last_in_key = None;
        self.last_bytes_42_43 = None;
        self.bytes_42_43_changes_epoch = 0;
        self.prev_combos = 0;
        if !self.models_seen.contains(&d.model.label()) { self.models_seen.push(d.model.label()); }
        let rec = json!({"type": "status", "t_us": n(t), "epoch": self.epoch, "state": "connected",
            "device_model": d.model.label(), "fallback": d.model.fallback(),
            "edge_fields": if d.model.edge_fields_measured() { "MEASURED" } else { UNMEASURED },
            "bytes_42_43_raw": UNKNOWN, "bytes_42_43_status": "ESTIMATED",
            "vid": d.vid, "pid": d.pid, "usb_bcd_device": d.bcd_device, "pad_id": d.pad_id});
        self.device = Some(d);
        self.last_state = "connected".into();
        self.last_detail.clear();
        self.push(t, rec);
    }

    /// `missing`, `unplugged`, `stalled`, `rejected_transport`, `rejected_device` (the logger dedupes repeats).
    pub fn on_status(&mut self, t: u64, state: &str, detail: &str) {
        if self.closed { return; }
        self.advance(t);
        if matches!(state, "unplugged" | "missing") { self.connected = false; }
        self.last_state = state.to_string();
        self.last_detail = detail.to_string();
        let rec = json!({"type": "status", "t_us": n(t), "epoch": self.epoch, "state": state, "detail": detail});
        self.push(t, rec);
    }

    pub fn on_timeout(&mut self, t: u64) {
        if self.closed { return; }
        self.advance(t);
        self.window(t).0.hb.timeouts += 1;
        let since = self.last_rx.filter(|&r| r >= self.connected_at).unwrap_or(self.connected_at);
        if self.connected && !self.stalled && t.saturating_sub(since) > STALL_US {
            self.stalled = true;
            self.on_status(t, "stalled", "no input report for 2 s on an open handle");
        }
    }

    pub fn on_read_error(&mut self, t: u64, err: &str) {
        if self.closed { return; }
        self.advance(t);
        self.window(t).0.hb.read_errors += 1;
        self.on_status(t, "unplugged", err);
    }

    /// One read completed at `t` (engine clock, taken right after the read returned). `ticks` is the QPC count.
    pub fn on_report(&mut self, t: u64, ticks: Option<u64>, bytes: &[u8]) {
        if self.closed { return; }
        if let Some(prev) = self.last_rx {
            if t < prev {
                self.on_status(t, "clock_rollback", &format!("t_us {t} is before the previous report {prev}"));
            }
        }
        self.advance(t);
        if bytes.len() < USB_REPORT_LEN { self.window(t).0.hb.short_reads += 1; return; }
        if bytes[0] != 0x01 {
            let w = self.window(t).0;
            w.hb.parse_faults += 1;
            if bytes[0] == 0x31 { w.hb.bt_reports += 1; }
            return;
        }
        let raw = &bytes[..USB_REPORT_LEN];
        let Some(p) = hid::parse(raw) else { self.window(t).0.hb.parse_faults += 1; return; };
        self.reports += 1;
        if self.stalled {
            self.stalled = false;
            let rec = json!({"type": "status", "t_us": n(t), "epoch": self.epoch, "state": "connected", "detail": "reports resumed on the same handle"});
            self.push(t, rec);
        }
        let model = self.device.as_ref().map(|d| d.model);
        let edge = model.map(DeviceModel::edge_fields_measured).unwrap_or(false);
        let label = self.model_label();
        let raw_on = self.cfg.raw_usb && self.raw_pause.is_none();
        let gap = self.last_rx.map(|p0| t.saturating_sub(p0));
        let (last_counter, last_hw) = (self.last_counter, self.last_hw);
        {
            let w = self.window(t).0;
            if raw_on {
                w.raw.extend_from_slice(&ticks.unwrap_or(0).to_le_bytes());
                w.raw.extend_from_slice(&t.to_le_bytes());
                w.raw.extend_from_slice(raw);
                w.raw_n += 1;
            }
            let hb = &mut w.hb;
            hb.rx_delta += 1;
            hb.first_rx.get_or_insert(t);
            hb.last_rx = Some(t);
            if let Some(g) = gap { hb.max_gap = hb.max_gap.max(g); if g > STALE_GAP_US { hb.stale += 1; } }
            if let Some(pc) = last_counter { if p.counter == pc.wrapping_add(1) { hb.seq_steps += 1; } else { hb.seq_breaks += 1; } }
            hb.seq0.get_or_insert(p.counter);
            hb.seq1 = Some(p.counter);
            if let Some(ph) = last_hw { if p.sensor_ts == ph { hb.hw_stalls += 1; } else { hb.hw_advance += 1; } }
            hb.hw0.get_or_insert(p.sensor_ts);
            hb.hw1 = Some(p.sensor_ts);
            if !hb.models.contains(&label) { hb.models.push(label); }
        }
        self.last_rx = Some(t);
        self.last_counter = Some(p.counter);
        self.last_hw = Some(p.sensor_ts);
        // Change-logged `in` row. Edge-only bits and `physical_face` are UNMEASURED (not zero) on a standard DualSense.
        // `buttons` is never rewritten; `physical_face` carries full raw byte 51 on Edge (ESTIMATED; low nibble mirrors
        // the d-pad, mask 0xF0 for face buttons) and is excluded from the change key when `!edge`.
        // `bytes_42_43_raw` carries raw bytes 42-43 (ESTIMATED, n=1 trigger-setting hypothesis; change emissions capped per
        // epoch; `adaptive_trigger_state` stays UNMEASURED).
        let buttons = if edge { p.buttons } else { p.buttons & !EDGE_BITS };
        let physical_face: Option<u8> = edge.then_some(raw[51]);
        let b42_43 = [raw[42], raw[43]];
        let prev_b42_43 = self.last_bytes_42_43;
        let raw_b42_43_changed = prev_b42_43.is_some_and(|old| old != b42_43);
        self.last_bytes_42_43 = Some(b42_43);
        let b42_43_change_emit = if raw_b42_43_changed {
            if self.bytes_42_43_changes_epoch < BYTES_42_43_CHANGE_CAP_PER_EPOCH {
                self.bytes_42_43_changes_epoch += 1;
                true
            } else {
                self.bytes_42_43_changes_suppressed += 1;
                self.window(t).0.hb.bytes_42_43_suppressed += 1;
                false
            }
        } else {
            false
        };
        let key = (buttons, physical_face, [p.lx, p.ly, p.rx, p.ry, p.l2, p.r2]);
        let emit = b42_43_change_emit || match (self.last_in_key, self.last_in_t) {
            (Some(k), Some(t0)) => k.0 != key.0 || k.1 != key.1 || (k.2 != key.2 && t.saturating_sub(t0) >= ANALOG_MIN_GAP_US),
            _ => true,
        };
        if emit {
            let touch: Vec<Value> = [33usize, 37].iter().map(|&o| {
                let c = raw[o];
                let x = u64::from(raw[o + 1]) | (u64::from(raw[o + 2] & 0x0F) << 8);
                let y = (u64::from(raw[o + 2]) >> 4) | (u64::from(raw[o + 3]) << 4);
                json!([u64::from(c & 0x80 == 0), u64::from(c & 0x7F), x, y])
            }).collect();
            let mut rec = json!({"type": "in", "epoch": self.epoch, "t_us": n(t), "t_qpc_ticks": opt(ticks), "rx_gap_us": opt(gap),
                "report_id": 1, "counter": p.counter, "sensor_ts": p.sensor_ts,
                "lx": p.lx, "ly": p.ly, "rx": p.rx, "ry": p.ry, "l2": p.l2, "r2": p.r2, "buttons": buttons,
                "edge_bits": if edge { json!((p.buttons & EDGE_BITS) >> 19) } else { json!(UNMEASURED) },
                "gyro": p.gyro, "accel": p.accel, "touch": touch,
                "trigger_fx_status": {"l2_raw": raw[42], "r2_raw": raw[43], "status": UNMEASURED},
                "battery_raw": raw[53], "device_model": label, "raw_sha256": ev::sha256_hex(raw)});
            rec["physical_face"] = physical_face.map(|v| json!(v)).unwrap_or_else(|| json!(UNMEASURED));
            rec["bytes_42_43_raw"] = json!([b42_43[0], b42_43[1]]);
            self.push(t, rec);
            self.window(t).0.hb.in_rows += 1;
            self.in_rows += 1;
            self.last_in_key = Some(key);
            self.last_in_t = Some(t);
        }
        if b42_43_change_emit {
            if let Some(old) = prev_b42_43 {
                let rec = json!({
                    "type": "status", "t_us": n(t), "epoch": self.epoch, "state": "bytes_42_43_change",
                    "bytes_42_43_raw": [b42_43[0], b42_43[1]], "prev_bytes_42_43_raw": [old[0], old[1]],
                    "epoch_change_seq": self.bytes_42_43_changes_epoch,
                    "epoch_change_cap": BYTES_42_43_CHANGE_CAP_PER_EPOCH,
                    "status": "ESTIMATED", "adaptive_trigger_state": UNMEASURED,
                    "detail": "USB 0x01 bytes 42-43 changed (ESTIMATED; n=1 trigger-setting hypothesis; adaptive_trigger_state stays UNMEASURED)"
                });
                self.push(t, rec);
            }
        }
        // Profile-swap candidate: Fn held with a face button (rising edge only). Edge only; which profile became
        // active is UNKNOWN (it lives in feature reports this logger never reads).
        if edge {
            let mut combos = 0u32;
            for (i, (fb, _)) in FN.iter().enumerate() { for (j, (face, _)) in FACE.iter().enumerate() {
                if p.buttons & fb != 0 && p.buttons & face != 0 { combos |= 1 << (i * 4 + j); }
            } }
            let rising = combos & !self.prev_combos;
            self.prev_combos = combos;
            for (i, (_, fname)) in FN.iter().enumerate() { for (j, (_, face)) in FACE.iter().enumerate() {
                if rising & (1 << (i * 4 + j)) != 0 {
                    let rec = json!({"type": "profile_swap_candidate", "epoch": self.epoch, "t_us": n(t), "fn_bit": fname, "face_bit": face,
                        "resulting_profile": UNKNOWN, "status": "ESTIMATED", "basis": "input_0x01_combo_only"});
                    self.push(t, rec);
                }
            } }
        }
    }

    /// Frames read in one capture-pipe read (`pes_batch_count` = how many shared that read).
    pub fn on_frames(&mut self, stamps: &[FrameStamp]) {
        if self.closed || stamps.is_empty() { return; }
        let batch = stamps.len() as u64;
        let mut seen_windows: Vec<u64> = vec![];
        for s in stamps {
            self.frames += 1;
            let rec = json!({"type": "frame", "file": s.file, "frame_idx": s.frame, "pts_90k": n(s.pts_90k), "t_arrival_us": n(s.t_mono_us),
                "pes_batch_count": batch, "clock_source": "mpegts-video-pes-arrival", "pipeline_latency": UNMEASURED});
            self.push(s.t_mono_us, rec);
            let w = self.window(s.t_mono_us).0;
            w.hb.frames += 1;
            if !seen_windows.contains(&w.seq) { seen_windows.push(w.seq); w.hb.pes_reads += 1; }
        }
    }

    /// Count frame arrivals dropped because the bounded capture-to-logger queue was full (or the logger had stopped).
    /// Never called from the capture thread; the logger samples an atomic and records the count here.
    pub fn on_frame_drops(&mut self, t: u64, n: u64) {
        if self.closed || n == 0 { return; }
        self.advance(t);
        self.frame_drops = self.frame_drops.saturating_add(n);
        let w = self.window(t).0;
        w.hb.frame_drops = w.hb.frame_drops.saturating_add(n);
    }

    /// Session total of dropped frame arrivals already written into windows.
    pub fn frame_drops(&self) -> u64 { self.frame_drops }

    /// Seal every window the clock has passed (plus grace). Called by the logger on every loop.
    pub fn tick(&mut self, now: u64) { if !self.closed { self.advance(now); } }

    fn advance(&mut self, now: u64) {
        self.latest_t = self.latest_t.max(now);
        while (self.next_seal + 1) * CHUNK_US + SEAL_GRACE_US <= self.latest_t {
            if self.seal(self.next_seal, false).is_err() { break; }
        }
    }

    /// `(seq, head)` of the newest durable chunk, for segment checkpoints. `None` before the first seal.
    pub fn binding(&self) -> Option<(u64, String)> { self.last_sealed.map(|s| (s, self.head.clone())) }

    /// Live state for `/status` and the log (`connected` / `missing` / `unplugged` / `stalled` / …).
    pub fn live(&self) -> crate::hand::LiveStatus {
        crate::hand::LiveStatus {
            state: if self.last_state.is_empty() { "missing".into() } else { self.last_state.clone() },
            detail: if self.last_detail.is_empty() { None } else { Some(self.last_detail.clone()) },
            device_model: self.device.as_ref().map(|d| d.model.label().to_string()),
            fallback: self.device.as_ref().map(|d| d.model.fallback()).unwrap_or(false),
            raw_usb: self.cfg.raw_usb && self.raw_pause.is_none(),
            epoch: self.epoch,
            frame_drops: self.frame_drops,
            hand_dir: Some(self.cfg.dir.to_string_lossy().into_owned()),
            hand_dir_source: Some(if self.cfg.hand_dir_source.is_empty() { "direct".into() } else { self.cfg.hand_dir_source.clone() }),
        }
    }
    pub fn disk_faults(&self) -> u64 { self.disk_faults }
    pub fn path(&self) -> &Path { &self.path }

    fn raw_paused_now(&mut self, seq: u64) -> Option<&'static str> {
        if !self.cfg.raw_usb || self.raw_pause.is_some() { return self.raw_pause; }
        if let Some(floor) = self.cfg.raw_min_free_bytes {
            if (seq - self.first_seq) % 30 == 0 {
                if let Some(free) = crate::disk::free_bytes(&self.cfg.dir) { if free < floor { self.raw_pause = Some("disk_low"); } }
            }
        }
        self.raw_pause
    }

    fn seal(&mut self, seq: u64, partial: bool) -> Result<(), String> {
        let pause = self.raw_paused_now(seq);
        let w = self.open.remove(&seq).unwrap_or_else(|| Window::new(seq));
        let hb = &w.hb;
        let pad_connected = hb.rx_delta > 0 || self.connected;
        let model = match hb.models.len() { 0 => if self.connected { self.model_label() } else { "none" }, 1 => hb.models[0], _ => "mixed" };
        let fallback = hb.models.contains(&"dualsense") || (hb.models.is_empty() && self.connected && model == "dualsense");
        let faults = hb.timeouts + hb.read_errors;
        let mut hb_rec = json!({"type": "hb", "chunk_seq": seq, "hb_seq": seq, "epoch": self.epoch, "device_model": model, "fallback": fallback,
            "pad_connected": pad_connected, "rx_delta": hb.rx_delta, "first_rx_t_us": opt(hb.first_rx), "last_rx_t_us": opt(hb.last_rx),
            "max_rx_gap_us": hb.max_gap, "stale_over_50ms": hb.stale, "short_reads": hb.short_reads, "parse_faults": hb.parse_faults,
            "bt_reports": hb.bt_reports, "faults": faults, "timeouts": hb.timeouts, "read_errors": hb.read_errors,
            "seq0": hb.seq0, "seq1": hb.seq1, "seq_steps": hb.seq_steps, "seq_breaks": hb.seq_breaks,
            "hw0": hb.hw0, "hw1": hb.hw1, "hw_advance": hb.hw_advance, "hw_stalls": hb.hw_stalls,
            "in_rows": hb.in_rows, "frames_in_chunk": hb.frames, "pes_reads": hb.pes_reads, "late_records": hb.late,
            "frame_drops": hb.frame_drops, "raw_reports": w.raw_n, "disk_faults_total": self.disk_faults,
            "counter_note": "seq_breaks / hw_stalls are diagnostic only, not packet-loss evidence on their own"});
        hb_rec["bytes_42_43_suppressed"] = json!(hb.bytes_42_43_suppressed);
        let prev_raw_offset = self.raw_offset;
        let active_raw = self.cfg.raw_usb && (pause.is_none() || w.raw_n > 0);
        let (raw_file_name, raw_offset, raw_len, raw_sha) = if active_raw {
            let len = w.raw.len() as u64;
            if len > 0 {
                let Some(rf) = self.raw_file.as_mut() else {
                    self.disk_faults += 1;
                    self.open.insert(seq, w);
                    return Err(format!("hand raw file {RAW_FILE}: handle not open"));
                };
                let written = rf.write_all(&w.raw).and_then(|_| rf.flush()).and_then(|_| rf.sync_data());
                if let Err(e) = written {
                    self.disk_faults += 1;
                    let _ = rf.set_len(prev_raw_offset);
                    self.open.insert(seq, w);
                    return Err(format!("hand raw file {RAW_FILE}: {e}"));
                }
            }
            (Some(RAW_FILE), Some(prev_raw_offset), Some(len), Some(ev::sha256_hex(&w.raw)))
        } else {
            (None, None, None, None)
        };
        let raw_bind = json!({
            "type": "raw_bind", "chunk_seq": seq, "raw_usb": self.cfg.raw_usb, "paused": pause,
            "file": raw_file_name, "offset": raw_offset, "len": raw_len, "sha256": raw_sha,
            "n_reports": w.raw_n, "bytes": w.raw.len() as u64, "record_bytes": RAW_RECORD_BYTES as u64, "layout": RAW_LAYOUT,
        });
        let mut records = w.records.clone();
        records.push(hb_rec);
        records.push(raw_bind);
        let sha = match ev::try_jcs_sha(&Value::Array(records.clone())) {
            Ok(s) => s,
            Err(e) => {
                self.disk_faults += 1;
                if raw_len.unwrap_or(0) > 0 {
                    if let Some(rf) = self.raw_file.as_mut() { let _ = rf.set_len(prev_raw_offset); let _ = rf.sync_data(); }
                }
                self.open.insert(seq, w);
                return Err(format!("hand window {seq} not JCS-safe: {e}"));
            }
        };
        let prev = self.head.clone();
        let head = ev::link(&prev, seq, &sha);
        let chunk = json!({
            "type": "chunk", "seq": seq, "t_start_us": seq * CHUNK_US, "t_end_us": (seq + 1) * CHUNK_US, "n": records.len() as u64,
            "sha256": sha, "prev": prev, "head": head, "partial": partial, "device_model": model, "fallback": fallback,
            "raw_file": raw_file_name, "raw_offset": raw_offset, "raw_len": raw_len, "raw_sha256": raw_sha,
            "health": {"pad_connected": pad_connected, "epoch": self.epoch, "rx_delta": w.hb.rx_delta, "max_rx_gap_us": w.hb.max_gap,
                "faults": faults, "frame_clock_present": self.cfg.frame_clock_on, "frames_count": w.hb.frames, "frame_drops": w.hb.frame_drops}
        });
        let mut text = String::new();
        for r in records.iter().chain(std::iter::once(&chunk)) {
            match ev::jcs(r) {
                Ok(s) => { text.push_str(&s); text.push('\n'); }
                Err(e) => {
                    self.disk_faults += 1;
                    if raw_len.unwrap_or(0) > 0 {
                        if let Some(rf) = self.raw_file.as_mut() { let _ = rf.set_len(prev_raw_offset); let _ = rf.sync_data(); }
                    }
                    self.open.insert(seq, w);
                    return Err(e);
                }
            }
        }
        let written = self.file.write_all(text.as_bytes()).and_then(|_| self.file.flush()).and_then(|_| self.file.sync_data());
        if let Err(e) = written {
            self.disk_faults += 1;
            let _ = self.file.set_len(self.durable_len);
            if raw_len.unwrap_or(0) > 0 {
                if let Some(rf) = self.raw_file.as_mut() { let _ = rf.set_len(prev_raw_offset); let _ = rf.sync_data(); }
            }
            self.open.insert(seq, w);
            return Err(format!("hand timeline write: {e}"));
        }
        if let Some(len) = raw_len {
            self.raw_offset = prev_raw_offset + len;
        }
        self.durable_len += text.len() as u64;
        self.head = head;
        self.last_sealed = Some(seq);
        self.next_seal = seq + 1;
        self.chunks += 1;
        Ok(())
    }

    /// Seal everything up to `t` (the trailing window is `partial: true`), write the summary file, and close.
    pub fn finish(&mut self, t: u64) -> Result<Finish, String> {
        if self.closed { return Err("hand timeline already closed".into()); }
        self.latest_t = self.latest_t.max(t);
        let last = (self.latest_t / CHUNK_US).max(self.open.keys().next_back().copied().unwrap_or(0));
        while self.next_seal <= last {
            let partial = self.next_seal == last;
            self.seal(self.next_seal, partial)?;
        }
        self.closed = true;
        let bytes = std::fs::read(&self.path).map_err(|e| format!("hand timeline re-read: {e}"))?;
        let file_sha256 = ev::sha256_hex(&bytes);
        let raw_file_str = if self.cfg.raw_usb { Some(RAW_FILE.to_string()) } else { None };
        let mut summary = json!({"schema": SUMMARY_SCHEMA, "file": FILE_NAME, "sha256": file_sha256, "head": self.head,
            "first_seq": self.first_seq, "last_seq": self.last_sealed, "chunks": self.chunks, "raw_usb": self.cfg.raw_usb, "raw_paused": self.raw_pause,
            "raw_file": raw_file_str, "raw_bytes": self.raw_offset, "raw_record_bytes": RAW_RECORD_BYTES as u64, "raw_layout": RAW_LAYOUT,
            "hand_dir": self.cfg.dir.to_string_lossy(),
            "hand_dir_source": if self.cfg.hand_dir_source.is_empty() { "direct" } else { self.cfg.hand_dir_source.as_str() },
            "hand_dir_fallback_reason": self.cfg.hand_dir_fallback_reason,
            "device_models_seen": self.models_seen, "fallback": self.models_seen.contains(&"dualsense"), "epochs": self.epoch,
            "reports": self.reports, "in_rows": self.in_rows, "frames": self.frames, "frame_drops": self.frame_drops, "disk_faults": self.disk_faults,
            "frame_clock": if self.cfg.frame_clock_on { "on" } else { "off: frame alignment UNMEASURED" },
            "adaptive_trigger_state": UNMEASURED, "report_rate_hz": UNMEASURED, "claims": CLAIMS});
        summary["bytes_42_43_raw_last"] = self.last_bytes_42_43.map(|s| json!([s[0], s[1]])).unwrap_or_else(|| json!(UNKNOWN));
        summary["bytes_42_43_status"] = json!("ESTIMATED");
        summary["bytes_42_43_changes_suppressed"] = json!(self.bytes_42_43_changes_suppressed);
        let text = format!("{}\n", serde_json::to_string_pretty(&summary).unwrap_or_else(|_| "{}".into()));
        let summary_path = self.cfg.dir.join(SUMMARY_FILE);
        let summary_sha256 = open_private_new(&summary_path, false).and_then(|mut f| { f.write_all(text.as_bytes())?; f.sync_all() })
            .ok().map(|_| ev::sha256_hex(text.as_bytes()));
        Ok(Finish { file: FILE_NAME.into(), file_sha256, head: self.head.clone(), first_seq: Some(self.first_seq).filter(|_| self.chunks > 0),
            last_seq: self.last_sealed, chunks: self.chunks, summary_sha256, raw_usb: self.cfg.raw_usb && self.raw_pause.is_none(),
            raw_file: raw_file_str, raw_bytes: self.raw_offset, frame_drops: self.frame_drops })
    }
}
