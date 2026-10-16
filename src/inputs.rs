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
//! `stalled`) are themselves records, so absence is committed rather than hidden.
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
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

pub const SCHEMA: &str = "qorcap-inputs/v0";
/// Analog-only changes are logged at most this often (buttons are always logged).
pub const ANALOG_MIN_GAP_US: u64 = 4_000;

pub fn empty_hash() -> String { ev::jcs_sha(&json!([])) }

/// Live controller state for /status and `input-test`.
#[derive(Debug, Clone, Default, serde::Serialize)]
pub struct Live {
    pub enabled: bool,
    /// off | searching | connected | missing | unplugged | stalled
    pub state: String,
    pub device: Option<String>,
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
                "transport": "usb-hid", "haptics": Detector::params(), "limit": "Proves inputs came from the USB-connected controller in sync with the video, not who held it."}));
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
        if self.live.state == state { return; }
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
            // Fresh filters + calibration after every (re)connect; keep the run's event counters.
            let (e, lc) = (self.detector.live.events, self.detector.live.low_confidence);
            self.detector = Detector::new(self.wall_anchor_ms);
            self.detector.live.events = e; self.detector.live.low_confidence = lc;
        }
        self.pending.push(json!({"type": "status", "t_us": t_us, "state": state, "detail": detail}));
        if state != "connected" { self.last_logged = None; }
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
        if let Some(p) = &self.path {
            if let Some(d) = p.parent() { let _ = std::fs::create_dir_all(d); }
            match std::fs::OpenOptions::new().create(true).append(true).open(p) {
                Ok(mut f) => {
                    let mut s = String::new();
                    for r in recs.iter().chain(std::iter::once(&chunk)) { s.push_str(&r.to_string()); s.push('\n'); }
                    if let Err(e) = f.write_all(s.as_bytes()) { warn!("cannot write {}: {e}", p.display()); }
                    let _ = f.sync_all();
                }
                Err(e) => warn!("cannot open {}: {e}", p.display()),
            }
        }
        if let Some(hp) = &self.haptic_path {
            let hs: String = recs.iter().filter(|r| r["type"] == "haptic").map(|r| format!("{r}\n")).collect();
            if !hs.is_empty() {
                match std::fs::OpenOptions::new().create(true).append(true).open(hp) {
                    Ok(mut f) => { if let Err(e) = f.write_all(hs.as_bytes()) { warn!("cannot write {}: {e}", hp.display()); } let _ = f.sync_all(); }
                    Err(e) => warn!("cannot open {}: {e}", hp.display()),
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
                    let first = log.lock().unwrap().live.state != "missing";
                    log.lock().unwrap().on_status(us(), "missing", &e);
                    if first || last_warn.map(|t| t.elapsed() > Duration::from_secs(60)).unwrap_or(true) {
                        warn!("CONTROLLER: {e}. Input proof is OFF until the DualSense is visible over USB (recording continues).");
                        last_warn = Some(Instant::now());
                    }
                    sleep_unless(&stop, 1000);
                }
                Ok(mut dev) => {
                    let name = dev.name();
                    info!("CONTROLLER: reading {name} HID reports on the QorCap clock");
                    log.lock().unwrap().on_status(us(), "connected", &name);
                    let mut last_rx = Instant::now();
                    while !stop.load(Ordering::SeqCst) {
                        match dev.read(&mut buf, 100) {
                            Ok(Some(n)) => {
                                let t = us();
                                if let Some(p) = hid::parse(&buf[..n]) {
                                    let mut l = log.lock().unwrap();
                                    if l.live.state != "connected" { l.on_status(t, "connected", &name); }
                                    l.on_report(t, &p);
                                    last_rx = Instant::now();
                                }
                            }
                            Ok(None) => {
                                if last_rx.elapsed() > Duration::from_secs(2) {
                                    let mut l = log.lock().unwrap();
                                    if l.live.state == "connected" { warn!("CONTROLLER: no HID reports for 2 s"); l.on_status(us(), "stalled", "no reports for 2 s"); }
                                }
                            }
                            Err(e) => {
                                warn!("CONTROLLER: USB HID read failed ({e}); retrying");
                                log.lock().unwrap().on_status(us(), "unplugged", &e);
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
    let s = std::fs::read_to_string(p).map_err(|e| format!("{}: {e}", p.display()))?;
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
    fn missing_controller_is_recorded_and_warned() {
        let log = Arc::new(Mutex::new(InputLog::new(None, true, 0)));
        let stop = Arc::new(AtomicBool::new(false));
        let h = spawn_logger_with(log.clone(), Instant::now(), stop.clone(), || Err("no DualSense found on USB".into()));
        std::thread::sleep(Duration::from_millis(150));
        stop.store(true, Ordering::SeqCst); h.join().unwrap();
        let mut l = log.lock().unwrap();
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
        let l = log.lock().unwrap();
        assert_eq!((l.live.reports, l.live.presses), (3, 1));
        assert!(l.live.state == "missing" || l.live.state == "unplugged");
        let states: Vec<&str> = l.pending.iter().filter_map(|r| r["state"].as_str()).collect();
        assert_eq!(&states[..2], &["connected", "unplugged"]);
    }
}
