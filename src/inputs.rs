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
    pub buttons: Vec<String>,
    pub l2: u8,
    pub r2: u8,
    pub sticks: [u8; 4],
    pub warning: Option<String>,
    pub last_report_mono_ms: Option<u64>,
    /// IMU haptic detector state (since 0.3.1).
    pub haptic: HapticLive,
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
}

impl InputLog {
    pub fn new(path: Option<PathBuf>, enabled: bool, wall_anchor_ms: u64) -> Self {
        let haptic_path = path.as_ref().map(|p| p.with_file_name(haptics_file_name(p)));
        let mut l = InputLog { path, pending: vec![], head: GENESIS.into(), chunks: 0, total_records: 0, last_logged: None,
            live: Live { enabled, state: if enabled { "searching".into() } else { "off".into() }, ..Default::default() },
            detector: Detector::new(wall_anchor_ms), haptic_path, wall_anchor_ms, imu_committed: None };
        if enabled {
            l.pending.push(json!({"type": "header", "schema": SCHEMA, "t_us": 0,
                "clock": "qorcap-mono-us (same Instant as manifest/checkpoint mono_ms)", "wall_anchor_ms": wall_anchor_ms,
                "transport": "usb-hid", "haptics": Detector::params(),
                "pad_identity": "required",
                "limit": "Proves inputs came from the USB-connected controller in sync with the video, not who held it."}));
        }
        l
    }

    /// Feed one parsed report received at `t_us`. Returns true if it was logged.
    pub fn on_report(&mut self, t_us: u64, p: &Pad) -> bool {
        self.live.reports += 1;
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
                    self.live.presses += (p.buttons & !q.buttons).count_ones() as u64;
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
        log
    }

    pub fn on_status(&mut self, t_us: u64, state: &str, detail: &str) {
        self.on_status_with_id(t_us, state, detail, None);
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
                    log.lock().unwrap_or_else(|e| e.into_inner()).on_status_with_id(us(), "connected", &detail, Some(&pad_id));
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
                                }
                            }
                            Ok(None) => {
                                if last_rx.elapsed() > Duration::from_secs(2) {
                                    let mut l = log.lock().unwrap_or_else(|e| e.into_inner());
                                    if l.live.state == "connected" { warn!("CONTROLLER: no HID reports for 2 s"); l.on_status(us(), "stalled", "no reports for 2 s"); }
                                }
                            }
                            Err(e) => {
                                warn!("CONTROLLER: USB HID read failed ({e}); retrying");
                                log.lock().unwrap_or_else(|e| e.into_inner()).on_status(us(), "unplugged", &e);
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
}
