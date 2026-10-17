//! Capture supervisor: runs the ffmpeg capture process, fans its MPEG-TS out
//! to the replay ring and the recorder, and reopens the device on exit,
//! error, unplug or freeze (watchdog).
use crate::cli::RecordArgs;
use crate::ffmpeg;
use crate::ring::{self, ReplayRing, TS};
use crate::evidence::{self as ev, CheckpointChain, Mp4Splitter, SegmentChain};
use serde_json::{json, Value};
use std::sync::mpsc::Sender;
use log::{error, info, warn};
use serde::Serialize;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

#[derive(Debug, Clone, Serialize, Default)]
pub struct Status {
    pub state: String, // starting | waiting_for_device | capturing | reopening | countersigning | finishing | stopped | disk_full
    /// seq of the sealed `stop` checkpoint once recording has stopped
    pub final_checkpoint_seq: Option<u64>,
    pub device: String,
    pub audio: Option<String>,
    pub res: String,
    pub fps_target: u32,
    pub fps_actual: f64,
    pub frames: u64,
    pub dup_frames: u64,
    pub drop_frames: u64,
    pub reopen_count: u64,
    pub last_error: Option<String>,
    /// Persistent non-fatal warning (e.g. video-only after audio open failed).
    pub warning: Option<String>,
    pub recording: Option<String>,
    pub recording_secs: f64,
    pub replay_secs_buffered: f64,
    pub replay_bytes: usize,
    pub replay_window: u64,
    pub uptime_secs: f64,
    pub disk_free_mb: Option<u64>,
    /// Controller (USB HID) input logger state; None when --no-controller.
    pub input: Option<crate::inputs::Live>,
}

pub struct Recorder { reader: Option<std::thread::JoinHandle<()>>, child: Child, stdin: Option<ChildStdin>, pub path: String, started: Instant, deadline: Option<Instant>, wait_key: bool, base: String, part: u32 }

pub struct Engine {
    pub args: RecordArgs,
    pub ffmpeg: String,
    pub ring: Mutex<ReplayRing>,
    pub rec: Mutex<Option<Recorder>>,
    pub status: Mutex<Status>,
    pub shutdown: AtomicBool,
    pub snapshot: PathBuf,
    pub exit_when_recording_ends: AtomicBool,
    started: Instant,
    last_data_ms: AtomicU64,
    pub ev: Mutex<MatchState>,
    pub subs: Mutex<Vec<Sender<String>>>,
    pub stamp_file: PathBuf,
    pub input: Arc<Mutex<crate::inputs::InputLog>>,
    /// The supervisor loop (`run`) is active; the API keeps serving until it has finished the bundle.
    pub running: AtomicBool,
    /// Set when `run` has returned (or /shutdown arrives while not running): the API thread exits.
    pub api_closed: AtomicBool,
    /// After the stop checkpoint: no further checkpoints may be appended (input proof / bundle work).
    pub sealed: AtomicBool,
    /// Highest checkpoint seq the local referee (Start Match) reports as countersigned; -1 = none attached.
    pub countersigned: std::sync::atomic::AtomicI64,
}

/// QorTime evidence state for this run.
#[derive(Default)]
pub struct MatchState {
    pub seg: SegmentChain,
    pub cps: CheckpointChain,
    pub checkpoints: Vec<Value>,
    pub challenges: Vec<Value>,
    pub timeline: Vec<Value>,
    pub clips: Vec<Value>,
    pub last_seg_sha: Option<String>,
    pub last_score: Option<(i64, i64, i64)>,
    pub last_clock: Option<(bool, bool)>, // (at_zero, red)
    pub score: Value,
    pub final_: Option<Value>,
    pub device_id: Option<String>,
    pub started_wall_ms: u64,
}

pub fn wall_ms() -> u64 { SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as u64 }

fn append_jsonl(path: &Path, v: &Value) {
    use std::fs::OpenOptions;
    if let Some(d) = path.parent() { if !d.as_os_str().is_empty() { let _ = std::fs::create_dir_all(d); } }
    match OpenOptions::new().create(true).append(true).open(path) {
        Ok(mut f) => {
            if let Err(e) = writeln!(f, "{v}") { error!("cannot write {}: {e}", path.display()); return; }
            let _ = f.flush();
            let _ = f.sync_all(); // durable append: a crash leaves at most one torn line
        }
        Err(e) => error!("cannot append {}: {e}", path.display()),
    }
}

pub fn now_stamp() -> String {
    let s = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs();
    // UTC yyyymmdd-hhmmss without chrono
    let (d, t) = (s / 86400, s % 86400);
    let (mut y, mut doy) = (1970i64, d as i64);
    loop { let n = if (y % 4 == 0 && y % 100 != 0) || y % 400 == 0 { 366 } else { 365 }; if doy < n { break; } doy -= n; y += 1; }
    let leap = (y % 4 == 0 && y % 100 != 0) || y % 400 == 0;
    let ml = [31, if leap { 29 } else { 28 }, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    let mut m = 0; while doy >= ml[m] { doy -= ml[m]; m += 1; }
    format!("{y:04}{:02}{:02}-{:02}{:02}{:02}Z", m + 1, doy + 1, t / 3600, t / 60 % 60, t % 60)
}

fn part_name(base: &str, part: u32) -> String {
    if part <= 1 { return base.to_string(); }
    match base.rfind('.') { Some(i) => format!("{}.part{}{}", &base[..i], part, &base[i..]), None => format!("{base}.part{part}") }
}

/// True when `path` exists and already has content (must not be truncated/reopened for write).
fn path_has_content(path: &str) -> bool {
    std::fs::metadata(path).map(|m| m.len() > 0).unwrap_or(false)
}

/// Pick the first part index >= `start` whose target path is absent or empty.
/// Never returns a path that would overwrite existing video bytes (MyTurn ENOSPC resume bug).
pub fn next_free_part(base: &str, start: u32) -> (u32, String) {
    let mut part = start.max(1);
    loop {
        let path = part_name(base, part);
        if !path_has_content(&path) { return (part, path); }
        part = part.saturating_add(1);
        if part > 10_000 { return (part, part_name(base, part)); } // absurd safeguard
    }
}

/// Stable run directory for snapshot/stamp (never %TEMP% — Windows clears it and ffmpeg image2 then storms).
fn run_dir(args: &RecordArgs) -> PathBuf {
    if let Some(b) = args.bundle.as_ref() {
        let d = PathBuf::from(b).join(".qorcap-run");
        let _ = std::fs::create_dir_all(&d);
        return d;
    }
    let root = if let Ok(h) = std::env::var("QORCAP_HOME") {
        PathBuf::from(h)
    } else if cfg!(windows) {
        std::env::var("LOCALAPPDATA").ok().map(|d| PathBuf::from(d).join("QorCap"))
            .unwrap_or_else(|| std::env::temp_dir().join("QorCap"))
    } else {
        std::env::var("HOME").ok().map(|d| PathBuf::from(d).join(".qorcap"))
            .unwrap_or_else(|| std::env::temp_dir().join("qorcap"))
    };
    let d = root.join(format!("run-{}", std::process::id()));
    let _ = std::fs::create_dir_all(&d);
    d
}

impl Engine {
    pub fn new(args: RecordArgs, ffmpeg: String) -> Arc<Self> {
        let window = args.replay.unwrap_or(3).max(3);
        // Cap memory: bitrate estimate * window * 1.5 (+ audio), at least 64 MiB.
        let bps = parse_bitrate(&args.bitrate).unwrap_or(8_000_000) + 200_000;
        let cap = ((bps / 8) as usize * window as usize * 3 / 2).max(64 << 20);
        let dir = run_dir(&args);
        let input_path = args.bundle.as_ref().map(|b| PathBuf::from(b).join("inputs.jsonl"))
            .or_else(|| args.out.as_ref().map(|o| PathBuf::from(format!("{o}.inputs.jsonl"))));
        let input = Arc::new(Mutex::new(crate::inputs::InputLog::new(input_path, !args.no_controller, wall_ms())));
        let st = Status { state: "starting".into(), device: args.device.clone(), res: format!("{}x{}", args.res.0, args.res.1), fps_target: args.fps, replay_window: args.replay.unwrap_or(0), ..Default::default() };
        Arc::new(Self { ffmpeg, ring: Mutex::new(ReplayRing::new(window, cap)), rec: Mutex::new(None), status: Mutex::new(st), shutdown: AtomicBool::new(false),
            ev: Mutex::new(MatchState { started_wall_ms: wall_ms(), score: Value::Null, ..Default::default() }), subs: Mutex::new(vec![]),
            stamp_file: dir.join("stamp.txt"),
            snapshot: dir.join("snapshot.jpg"), exit_when_recording_ends: AtomicBool::new(false), started: Instant::now(), last_data_ms: AtomicU64::new(0), input,
            running: AtomicBool::new(false), api_closed: AtomicBool::new(false), sealed: AtomicBool::new(false),
            countersigned: std::sync::atomic::AtomicI64::new(-1), args })
    }
    fn ms(&self) -> u64 { self.started.elapsed().as_millis() as u64 }
    /// The monotonic clock behind every mono_ms (manifest, checkpoints) and inputs.jsonl t_us.
    pub fn clock(&self) -> Instant { self.started }
    /// Start the USB HID controller logger (no-op with --no-controller).
    pub fn start_input_logger(&self) -> Option<std::thread::JoinHandle<()>> {
        if self.args.no_controller { return None; }
        let stop = Arc::new(AtomicBool::new(false));
        Some(crate::inputs::spawn_logger(self.input.clone(), self.started, stop))
    }

    pub fn status(&self) -> Status {
        let mut s = self.status.lock().unwrap().clone();
        { let r = self.ring.lock().unwrap(); s.replay_bytes = r.len_bytes(); s.replay_secs_buffered = r.span_ms() as f64 / 1000.0; }
        if self.args.replay.is_none() { s.replay_bytes = 0; s.replay_secs_buffered = 0.0; }
        if !self.args.no_controller { s.input = Some(self.input.lock().unwrap().live.clone()); }
        if let Some(r) = self.rec.lock().unwrap().as_ref() { s.recording = Some(r.path.clone()); s.recording_secs = r.started.elapsed().as_secs_f64(); }
        s.uptime_secs = self.started.elapsed().as_secs_f64();
        let probe = self.args.out.as_deref().or(self.args.bundle.as_deref()).unwrap_or(".");
        if let Some(free) = crate::disk::free_bytes(Path::new(probe)) { s.disk_free_mb = Some(free / (1024 * 1024)); }
        s
    }

    pub fn start_recording(self: &Arc<Self>, path: &str, duration: Option<u64>) -> Result<String, String> {
        let mut g = self.rec.lock().unwrap();
        if let Some(r) = g.as_ref() { return Err(format!("already recording to {}", r.path)); }
        if let Some(min) = self.args.min_free_bytes() {
            crate::disk::check_start(Path::new(path), min)?;
        }
        // Never overwrite an existing video*.mp4 that already has content (resume/reopen safety).
        let (part, _) = next_free_part(path, 1);
        if part > 1 {
            warn!("existing video content under {path}; starting at part {part} to avoid overwrite");
        }
        let r = self.spawn_recorder(path, part, duration.map(|d| Instant::now() + Duration::from_secs(d)))?;
        info!("recording started -> {}", r.path);
        let p = r.path.clone();
        *g = Some(r);
        drop(g);
        // Genesis checkpoint right away: the first segment checkpoint can be 5-10 s away (device open,
        // audio fallback retry, encoder warm-up) and the referee must see the chain start promptly.
        self.checkpoint("start", json!({}));
        // Until the first video segment exists (device open, audio fallbacks, encoder warm-up), keep the
        // referee fed every 4 s. Stops for good at the first segment, so mid-match gaps are still gaps.
        { let me = self.clone(); std::thread::spawn(move || {
            let t0 = Instant::now();
            while t0.elapsed() < Duration::from_secs(120) {
                std::thread::sleep(Duration::from_millis(4000));
                if me.shutdown.load(Ordering::SeqCst) || me.ev.lock().unwrap().seg.seq > 0 || me.rec.lock().unwrap().is_none() { break; }
                me.checkpoint("startup", json!({}));
            }
        }); }
        Ok(p)
    }
    fn spawn_recorder(self: &Arc<Self>, base: &str, part: u32, deadline: Option<Instant>) -> Result<Recorder, String> {
        let (part, path) = next_free_part(base, part);
        if let Some(d) = Path::new(&path).parent() { if !d.as_os_str().is_empty() { let _ = std::fs::create_dir_all(d); } }
        // create_new / refuse truncate: if a race created the file, bump again.
        let file = match std::fs::OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(f) => f,
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                let (part2, path2) = next_free_part(base, part + 1);
                let f = std::fs::OpenOptions::new().write(true).create_new(true).open(&path2)
                    .map_err(|e2| format!("cannot create {path2}: {e2} (also after {path}: {e})"))?;
                return self.spawn_recorder_with(base, part2, path2, f, deadline);
            }
            Err(e) => return Err(format!("cannot create {path}: {e}")),
        };
        self.spawn_recorder_with(base, part, path, file, deadline)
    }
    fn spawn_recorder_with(self: &Arc<Self>, base: &str, part: u32, path: String, mut file: std::fs::File, deadline: Option<Instant>) -> Result<Recorder, String> {
        let mut child = ffmpeg::command(&self.ffmpeg).args(ffmpeg::frag_mp4_args()).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::inherit())
            .spawn().map_err(|e| format!("cannot start ffmpeg ({}) for recording: {e}", self.ffmpeg))?;
        let stdin = child.stdin.take();
        let mut out = child.stdout.take().unwrap();
        let me = self.clone();
        let p2 = path.clone();
        // Write the exact fMP4 bytes to disk and hash each segment as it completes.
        let reader = std::thread::spawn(move || {
            let mut sp = Mp4Splitter::default();
            let mut buf = vec![0u8; 1 << 16];
            loop {
                let n = match out.read(&mut buf) { Ok(0) | Err(_) => break, Ok(n) => n };
                if let Err(e) = file.write_all(&buf[..n]) { error!("write {p2}: {e}"); break; }
                let _ = file.flush();
                for (off, seg) in sp.feed(&buf[..n]) { me.on_segment(&p2, off, &seg); }
            }
            if let Some((off, seg)) = sp.finish() { me.on_segment(&p2, off, &seg); }
            let _ = file.sync_all();
        });
        Ok(Recorder { reader: Some(reader), child, stdin, path, started: Instant::now(), deadline, wait_key: true, base: base.to_string(), part })
    }
    pub fn stop_recording(&self) -> Option<String> {
        let r = self.rec.lock().unwrap().take()?;
        Some(finish(r))
    }
    /// After a device reopen, timestamps restart: close the current part, open the next unused part name.
    fn rotate_recording(self: &Arc<Self>) {
        let mut g = self.rec.lock().unwrap();
        if let Some(r) = g.take() {
            let (base, part, deadline) = (r.base.clone(), r.part + 1, r.deadline);
            let old = finish(r);
            // Disk guard before opening a new part mid-match.
            if let Some(min) = self.args.min_free_bytes() {
                let need = min.min(crate::disk::CRITICAL_FREE_BYTES * 2);
                if let Err(e) = crate::disk::check_start(Path::new(&base), need) {
                    error!("not continuing recording after reopen: {e}");
                    self.set_state("disk_full", Some(e));
                    return;
                }
            }
            match self.spawn_recorder(&base, part, deadline) {
                Ok(n) => { warn!("device reopened: closed {old}, continuing in {}", n.path); *g = Some(n); }
                Err(e) => error!("could not continue recording after reopen: {e}"),
            }
        }
    }

    pub fn save_replay(&self, out: Option<&str>) -> Result<String, String> { self.save_replay_secs(out, None) }
    pub fn save_replay_secs(&self, out: Option<&str>, secs: Option<u64>) -> Result<String, String> {
        if self.args.replay.is_none() { return Err("replay buffer disabled (start with --replay SECS)".into()); }
        let data = self.ring.lock().unwrap().snapshot_last(secs.map(|s| s * 1000)).ok_or("replay buffer is empty (no keyframe yet)")?;
        let out = out.map(|s| s.to_string()).unwrap_or_else(|| Path::new(&self.args.replay_dir).join(format!("replay-{}.mp4", now_stamp())).to_string_lossy().into_owned());
        if let Some(d) = Path::new(&out).parent() { if !d.as_os_str().is_empty() { std::fs::create_dir_all(d).map_err(|e| format!("mkdir {}: {e}", d.display()))?; } }
        let ts = format!("{out}.ts");
        std::fs::write(&ts, &data).map_err(|e| format!("write {ts}: {e}"))?;
        let st = ffmpeg::command(&self.ffmpeg).args(ffmpeg::replay_mp4_args(&ts, &out)).stdin(Stdio::null()).status();
        match st {
            Ok(s) if s.success() => { let _ = std::fs::remove_file(&ts); info!("replay saved -> {out} ({} bytes TS)", data.len()); Ok(out) }
            Ok(s) => { warn!("replay remux failed ({s}); kept raw {ts}"); Ok(ts) }
            Err(e) => { warn!("cannot run ffmpeg for replay remux: {e}; kept raw {ts}"); Ok(ts) }
        }
    }

    fn set_state(&self, s: &str, err: Option<String>) {
        let mut st = self.status.lock().unwrap();
        st.state = s.into();
        if err.is_some() { st.last_error = err; }
    }

    /// Fan a block of TS packets out to ring + recorder.
    pub fn ingest(self: &Arc<Self>, data: &[u8]) {
        let t = self.ms();
        self.last_data_ms.store(t, Ordering::Relaxed);
        let (pat, pmt) = { let mut r = self.ring.lock().unwrap(); r.push(t, data); r.tables() };
        let mut g = self.rec.lock().unwrap();
        let mut done = false;
        if let Some(r) = g.as_mut() {
            let mut slice = data;
            if r.wait_key {
                match data.chunks_exact(TS).position(ring::is_random_access) {
                    Some(k) => { slice = &data[k * TS..]; r.wait_key = false;
                        if let Some(w) = r.stdin.as_mut() { for t in [pat, pmt].into_iter().flatten() { let _ = w.write_all(&t); } } }
                    None => slice = &[],
                }
            }
            if !slice.is_empty() {
                if let Some(w) = r.stdin.as_mut() { if let Err(e) = w.write_all(slice) { error!("recorder pipe broken ({e}); stopping recording {}", r.path); done = true; } }
            }
            if r.deadline.map(|d| Instant::now() >= d).unwrap_or(false) { info!("--duration reached"); done = true; }
        }
        if done {
            if let Some(r) = g.take() { finish(r); }
            if self.exit_when_recording_ends.load(Ordering::Relaxed) { self.shutdown.store(true, Ordering::SeqCst); }
        }
    }

    /// Main supervisor loop. Returns when shutdown is requested.
    pub fn run(self: &Arc<Self>) {
        self.running.store(true, Ordering::SeqCst);
        self.run_loop();
        self.finish_stop();
        self.running.store(false, Ordering::SeqCst);
        self.api_closed.store(true, Ordering::SeqCst);
    }
    fn run_loop(self: &Arc<Self>) {
        let mut backoff = 1u64;
        let mut fmt: Option<String> = None; // resolved input format (probed once per device appearance)
        let mut audio_off = false;          // audio failed to open: continue video-only
        let mut audio_fails = 0u32;
        let mut audio_try = 0usize;         // index into the audio open strategies for this device
        while !self.shutdown.load(Ordering::SeqCst) {
            let mut audio: Option<ffmpeg::AudioSpec> = None;
            if self.args.device == "testpattern" && self.args.audio != "none" && self.args.audio != "off" && !audio_off {
                audio = Some(ffmpeg::AudioSpec { name: "sine".into(), alt: None, strategy: ffmpeg::AudioStrategy::Separate });
            }
            let mut n_strategies = 1usize;
            if self.args.device != "testpattern" {
                match ffmpeg::list_devices(&self.ffmpeg) {
                    Err(e) => { self.set_state("waiting_for_device", Some(format!("cannot run ffmpeg `{}`: {e}", self.ffmpeg))); error!("cannot run ffmpeg `{}`: {e}", self.ffmpeg); self.sleep(5); continue; }
                    Ok(devs) => {
                        if !devs.iter().any(|d| d.kind == "video" && d.name == self.args.device) {
                            let msg = format!("video device \"{}\" not present (have: {})", self.args.device,
                                devs.iter().filter(|d| d.kind == "video").map(|d| d.name.as_str()).collect::<Vec<_>>().join(", "));
                            if self.status.lock().unwrap().state != "waiting_for_device" { warn!("{msg}; waiting for it to appear"); }
                            self.set_state("waiting_for_device", Some(msg));
                            fmt = None;
                            self.sleep(1);
                            continue;
                        }
                        audio = if audio_off { None } else {
                            ffmpeg::pick_audio_device(&self.args.device, &self.args.audio, &devs).and_then(|d| {
                                let strats = ffmpeg::audio_strategies(&d);
                                n_strategies = strats.len();
                                if audio_try == 0 {
                                    let names: Vec<&str> = devs.iter().filter(|x| x.kind == "audio").map(|x| x.name.as_str()).collect();
                                    if !names.iter().any(|n| *n == d.name) {
                                        warn!("audio device \"{}\" is not in the DirectShow audio list ({}); trying anyway", d.name, names.join(", "));
                                    }
                                }
                                strats.get(audio_try).map(|s| ffmpeg::AudioSpec { name: d.name.clone(), alt: d.alt.clone(), strategy: *s })
                            })
                        };
                        if self.args.audio != "none" && self.args.audio != "off" && audio.is_none() && !audio_off {
                            warn!("no audio device for \"{}\" found; recording video-only", self.args.device);
                            audio_off = true;
                            self.status.lock().unwrap().warning = Some("AUDIO UNAVAILABLE — recording video-only (no game sound).".into());
                        }
                        if fmt.is_none() {
                            let f = if self.args.input_format == "auto" {
                                match ffmpeg::list_options(&self.ffmpeg, &self.args.device) {
                                    Ok(m) => { let f = ffmpeg::choose_input_format(&m, self.args.res.0, self.args.res.1, self.args.fps);
                                        info!("card modes: {}; input format auto -> {f}", m.iter().map(|x| format!("{}:{}x{}@{}", x.format, x.w, x.h, x.max_fps.round())).collect::<Vec<_>>().join(" "));
                                        f }
                                    Err(e) => { warn!("could not list card modes ({e}); input format auto"); "auto".into() }
                                }
                            } else { self.args.input_format.clone() };
                            fmt = Some(f);
                        }
                        self.ev.lock().unwrap().device_id = devs.iter().find(|d| d.kind == "video" && d.name == self.args.device).and_then(|d| d.alt.clone());
                    }
                }
            }
            self.status.lock().unwrap().audio = audio.as_ref().map(|a| a.name.clone());
            // Disk guard: stop cleanly when critically low — never keep writing into ENOSPC.
            let probe = self.args.out.as_deref().or(self.args.bundle.as_deref()).unwrap_or(".");
            if let Some((crit, free)) = crate::disk::is_critical(Path::new(probe)) {
                self.status.lock().unwrap().disk_free_mb = Some(free / (1024 * 1024));
                if crit {
                    let msg = format!(
                        "disk space critically low ({} free on {}); stopping recording cleanly to avoid ENOSPC corruption",
                        crate::disk::fmt_bytes(free), probe
                    );
                    error!("{msg}");
                    self.set_state("disk_full", Some(msg.clone()));
                    self.status.lock().unwrap().warning = Some(msg);
                    if let Some(p) = self.stop_recording() { info!("finalised {p} due to low disk"); }
                    self.sleep(5);
                    if self.shutdown.load(Ordering::SeqCst) { break; }
                    continue;
                }
            }
            let t0 = Instant::now();
            let (ok, why) = self.run_capture_once(fmt.as_deref().unwrap_or("auto"), audio.as_ref());
            if self.shutdown.load(Ordering::SeqCst) { self.ring.lock().unwrap().clear(); break; }
            if why == ffmpeg::Failure::Snapshot && ok {
                // Snapshot mux path failed but video kept flowing — recreate dir, do not burn reopen budget.
                warn!("snapshot output failed; recreating run dir and continuing without counting as reopen");
                if let Some(d) = self.snapshot.parent() { let _ = std::fs::create_dir_all(d); }
                self.ring.lock().unwrap().clear();
                continue;
            }
            if !ok && audio.is_some() {
                if why == ffmpeg::Failure::AudioOpen && audio_try + 1 < n_strategies {
                    let cur = audio.as_ref().map(|a| a.strategy);
                    audio_try += 1;
                    warn!("audio device {:?} failed to open with {:?}; retrying with another DirectShow open strategy", audio.as_ref().map(|a| &a.name), cur);
                    self.ring.lock().unwrap().clear();
                    self.set_state("reopening", Some("audio open failed; trying another audio open method".into()));
                    continue; // immediate retry, no backoff / no reopen_count
                }
                audio_fails = if why == ffmpeg::Failure::AudioOpen { 99 } else { audio_fails + 1 };
                if audio_fails >= 2 {
                    warn!("audio device {:?} could not be opened{}; falling back to VIDEO-ONLY recording", audio.as_ref().map(|a| &a.name),
                        if why == ffmpeg::Failure::AudioOpen { "" } else { " (two failed opens with audio)" });
                    audio_off = true;
                    {
                        let mut st = self.status.lock().unwrap();
                        st.warning = Some("AUDIO FAILED — recording video-only (no game sound). Capture continues.".into());
                        st.last_error = Some("audio could not be opened; recording video-only".into());
                    }
                    self.ring.lock().unwrap().clear();
                    self.set_state("reopening", Some("audio could not be opened; retrying video-only".into()));
                    continue; // immediate retry, no backoff / no reopen_count
                }
            } else if ok { audio_fails = 0; }
            self.ring.lock().unwrap().clear();
            if self.shutdown.load(Ordering::SeqCst) { break; }
            self.status.lock().unwrap().reopen_count += 1;
            self.set_state("reopening", None);
            self.note_reopen();
            self.rotate_recording();
            if ok && t0.elapsed() > Duration::from_secs(10) { backoff = 1; } else { backoff = (backoff * 2).min(8); }
            warn!("capture ended; reopening in {backoff}s");
            self.sleep(backoff);
        }
    }

    /// Stop order (v0.3.3): finalise the video (its last segment checkpoints are written by the reader
    /// thread), write the `stop` checkpoint, seal the chain, give the local referee a few seconds to
    /// countersign everything, and only then run the slow input-proof / bundle work. Nothing after the
    /// seal adds checkpoints, so the referee stamps the tail within seconds of its t_ms.
    fn finish_stop(self: &Arc<Self>) {
        if let Some(p) = self.stop_recording() { info!("finalised {p}"); }
        crate::inputproof::clear_recording_marker(self.bundle_dir().as_deref());
        self.seal_and_wait_countersign(Duration::from_secs(self.args.countersign_wait));
        if self.args.bundle.is_some() {
            self.set_state("finishing", Some("checking controller input against the video (1-2 minutes)".into()));
            self.write_bundle();
        }
        self.set_state("stopped", None);
        let _ = std::fs::remove_file(&self.snapshot);
    }

    /// Write the final `stop` checkpoint, refuse any later checkpoint, and wait (bounded) until the local
    /// referee reports it countersigned through that seq. No referee attached -> no wait.
    pub fn seal_and_wait_countersign(&self, wait: Duration) -> Option<u64> {
        if self.sealed.load(Ordering::SeqCst) { return None; }
        let cp = self.checkpoint("stop", json!({}));
        self.sealed.store(true, Ordering::SeqCst);
        let last = cp["seq"].as_u64()?;
        self.status.lock().unwrap().final_checkpoint_seq = Some(last);
        info!("stop checkpoint {last} written; checkpoint chain sealed");
        if self.countersigned.load(Ordering::SeqCst) < 0 { return Some(last); }
        self.set_state("countersigning", Some("waiting for the referee to countersign the final checkpoints".into()));
        let end = Instant::now() + wait;
        while Instant::now() < end {
            if self.countersigned.load(Ordering::SeqCst) >= last as i64 { info!("final checkpoints countersigned through {last}"); return Some(last); }
            std::thread::sleep(Duration::from_millis(50));
        }
        warn!("referee did not confirm countersigning through checkpoint {last} within {}s (got {})", wait.as_secs(), self.countersigned.load(Ordering::SeqCst));
        Some(last)
    }

    fn sleep(&self, secs: u64) {
        let end = Instant::now() + Duration::from_secs(secs);
        while Instant::now() < end && !self.shutdown.load(Ordering::SeqCst) { std::thread::sleep(Duration::from_millis(100)); }
    }

    /// One capture session. Returns true if it produced data.
    fn run_capture_once(self: &Arc<Self>, input_format: &str, audio: Option<&ffmpeg::AudioSpec>) -> (bool, ffmpeg::Failure) {
        // Snapshot lives under the bundle / LOCALAPPDATA run dir — recreate every open (TEMP clear bug).
        if let Some(d) = self.snapshot.parent() { let _ = std::fs::create_dir_all(d); }
        if self.args.stamp && !self.stamp_file.exists() { let _ = std::fs::write(&self.stamp_file, "QorCap  no referee nonce yet"); }
        let stamp = if self.args.stamp { Some(self.stamp_file.to_string_lossy().into_owned()) } else { None };
        let args = ffmpeg::capture_args_with(&self.args, input_format, audio, &self.snapshot.to_string_lossy(), stamp.as_deref());
        info!("opening \"{}\" format={input_format} audio={:?} ({:?}) {}x{}@{} enc={}", self.args.device, audio.map(|a| &a.name), audio.map(|a| a.strategy), self.args.res.0, self.args.res.1, self.args.fps, self.args.encoder);
        let child = ffmpeg::command(&self.ffmpeg).args(&args).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn();
        let mut child = match child { Ok(c) => c, Err(e) => { let m = format!("cannot start ffmpeg `{}`: {e}", self.ffmpeg); error!("{m}"); self.set_state("reopening", Some(m)); return (false, ffmpeg::Failure::Other); } };
        let mut stdout = child.stdout.take().unwrap();
        let stderr = child.stderr.take().unwrap();
        let child = Arc::new(Mutex::new(child));
        let alive = Arc::new(AtomicBool::new(true));
        let last_err = Arc::new(Mutex::new(Vec::<String>::new()));
        // stderr: progress key=value -> status; everything else -> log
        { let me = self.clone(); let last_err = last_err.clone(); std::thread::spawn(move || {
            use std::io::BufRead;
            for line in std::io::BufReader::new(stderr).lines().map_while(Result::ok) {
                let l = line.trim();
                if let Some((k, v)) = l.split_once('=') {
                    if !k.contains(' ') {
                        let mut s = me.status.lock().unwrap();
                        match k { "frame" => s.frames = v.parse().unwrap_or(s.frames), "fps" => s.fps_actual = v.parse().unwrap_or(0.0),
                            "dup_frames" => s.dup_frames = v.parse().unwrap_or(0), "drop_frames" => s.drop_frames = v.parse().unwrap_or(0), _ => {} }
                        continue;
                    }
                }
                if l.is_empty() { continue; }
                warn!("ffmpeg: {l}");
                let mut e = last_err.lock().unwrap(); e.push(l.to_string()); if e.len() > 5 { e.remove(0); }
            }
        }); }
        // watchdog: kill if no data for N seconds (frozen driver / shared device / unplug)
        { let me = self.clone(); let child = child.clone(); let alive = alive.clone(); let start = self.ms(); std::thread::spawn(move || {
            let wd = me.args.watchdog.max(1) * 1000;
            while alive.load(Ordering::SeqCst) {
                std::thread::sleep(Duration::from_millis(250));
                let now = me.ms();
                let last = me.last_data_ms.load(Ordering::Relaxed).max(start);
                let grace = if last == start { 15_000 } else { wd };
                if me.shutdown.load(Ordering::SeqCst) || now - last > grace {
                    if !me.shutdown.load(Ordering::SeqCst) { let m = format!("no data from device for {}s: treating as frozen/unplugged, restarting", grace / 1000); error!("{m}"); me.set_state("reopening", Some(m)); }
                    let _ = child.lock().unwrap().kill();
                    break;
                }
            }
        }); }
        let mut buf = vec![0u8; TS * 512];
        let mut fill = 0usize;
        let mut got = false;
        loop {
            match stdout.read(&mut buf[fill..]) {
                Ok(0) => break,
                Ok(n) => {
                    fill += n;
                    let whole = fill / TS * TS;
                    if whole > 0 {
                        // resync if needed
                        if buf[0] != 0x47 { if let Some(p) = buf[..fill].iter().position(|&b| b == 0x47) { buf.copy_within(p..fill, 0); fill -= p; continue; } else { fill = 0; continue; } }
                        if !got { got = true; info!("capturing"); self.set_state("capturing", None); }
                        self.ingest(&buf[..whole]);
                        buf.copy_within(whole..fill, 0);
                        fill -= whole;
                    }
                }
                Err(e) => { error!("capture read error: {e}"); break; }
            }
        }
        alive.store(false, Ordering::SeqCst);
        let st = { let mut c = child.lock().unwrap(); let _ = c.kill(); c.wait() };
        let mut why = ffmpeg::Failure::Other;
        if !self.shutdown.load(Ordering::SeqCst) {
            std::thread::sleep(Duration::from_millis(200));
            let tail = last_err.lock().unwrap().join(" | ");
            why = ffmpeg::classify_failure(&tail);
            // Snapshot-only errors must not look like a fatal capture failure when video flowed.
            if why == ffmpeg::Failure::Snapshot && got {
                warn!("ffmpeg snapshot path issue (video capture was ok): {tail}");
                return (got, why);
            }
            let m = format!("ffmpeg capture exited ({:?}){}", st.ok(), if tail.is_empty() { String::new() } else { format!(": {tail}") });
            let apps = if got { Vec::new() } else { ffmpeg::capture_apps_running() };
            let hint = ffmpeg::failure_hint(why, !apps.is_empty());
            error!("{m}{hint}");
            self.set_state("reopening", Some(format!("{m}{hint}")));
        }
        (got, why)
    }
}

fn finish(mut r: Recorder) -> String {
    let rd = r.reader.take();
    drop(r.stdin.take()); // EOF -> ffmpeg flushes last fragment
    let end = Instant::now() + Duration::from_secs(10);
    loop {
        match r.child.try_wait() { Ok(Some(_)) => break, Ok(None) if Instant::now() < end => std::thread::sleep(Duration::from_millis(50)), _ => { let _ = r.child.kill(); let _ = r.child.wait(); break; } }
    }
    if let Some(h) = rd { let _ = h.join(); }
    info!("recording finalised -> {}", r.path);
    r.path
}

pub fn parse_bitrate(s: &str) -> Option<u64> {
    let s = s.trim();
    let (n, m) = match s.chars().last()? { 'k' | 'K' => (&s[..s.len() - 1], 1_000), 'm' | 'M' => (&s[..s.len() - 1], 1_000_000), _ => (s, 1) };
    n.parse::<f64>().ok().map(|v| (v * m as f64) as u64)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn names() {
        assert_eq!(part_name("a/b.mp4", 1), "a/b.mp4");
        assert_eq!(part_name("a/b.mp4", 3), "a/b.part3.mp4");
        assert_eq!(parse_bitrate("8M"), Some(8_000_000));
        assert_eq!(parse_bitrate("2500k"), Some(2_500_000));
        assert_eq!(now_stamp().len(), 16);
    }
    #[test]
    fn no_overwrite_picks_next_part() {
        let dir = std::env::temp_dir().join(format!("qorcap-part-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let base = dir.join("video.mp4").to_string_lossy().into_owned();
        assert_eq!(next_free_part(&base, 1).0, 1);
        std::fs::write(&base, b"already recorded").unwrap();
        let (p, path) = next_free_part(&base, 1);
        assert_eq!(p, 2);
        assert!(path.ends_with("video.part2.mp4"), "{path}");
        std::fs::write(&path, b"part2").unwrap();
        assert_eq!(next_free_part(&base, 1).0, 3);
        assert_eq!(next_free_part(&base, 2).0, 3);
        let _ = std::fs::remove_dir_all(&dir);
    }
    #[test]
    fn draft_score_falls_back_to_last_timeline() {
        use clap::Parser;
        let dir = std::env::temp_dir().join(format!("qorcap-draft-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let d = dir.to_string_lossy().into_owned();
        let crate::cli::Cmd::Record(r) = crate::cli::Cli::try_parse_from(["q", "record", "--device", "testpattern", "--bundle", &d]).unwrap().cmd else { panic!() };
        let e = Engine::new(r, "ffmpeg-x".into());
        // Simulate scorebug reads ending 7-6 with no final event.
        let body = r#"{"type":"score","home":7,"away":6,"quarter":4,"clock_s":12}"#;
        let v: Value = serde_json::from_str(body).unwrap();
        e.event(&v).unwrap();
        let out = e.write_bundle().unwrap();
        let draft: Value = serde_json::from_str(&std::fs::read_to_string(out.join("receipt.draft.json")).unwrap()).unwrap();
        assert_eq!(draft["result"]["home_score"], 7);
        assert_eq!(draft["result"]["away_score"], 6);
        assert_eq!(draft["result"]["final_confirmed"], false);
        assert!(draft["result"].get("final_basis").is_none() || draft["result"]["final_basis"].is_null());
        let bundle: Value = serde_json::from_str(&std::fs::read_to_string(out.join("bundle.json")).unwrap()).unwrap();
        assert_eq!(bundle["final_confirmed"], false);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn end_menu_final_amends_scorebug_final_and_confirms() {
        use clap::Parser;
        let dir = std::env::temp_dir().join(format!("qorcap-endmenu-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let d = dir.to_string_lossy().into_owned();
        let crate::cli::Cmd::Record(r) = crate::cli::Cli::try_parse_from(["q", "record", "--device", "testpattern", "--bundle", &d]).unwrap().cmd else { panic!() };
        let e = Engine::new(r, "ffmpeg-x".into());
        let ev = |s: &str| e.event(&serde_json::from_str::<Value>(s).unwrap());
        ev(r#"{"type":"score","home":17,"away":0,"quarter":4,"clock_s":0}"#).unwrap();
        assert!(ev(r#"{"type":"final","basis":"end_menu","home":20,"away":0,"quarter":4}"#).is_err()); // method required
        ev(r#"{"type":"final","basis":"clock_zero","home":17,"away":0,"quarter":4,"t_ms":1000}"#).unwrap();
        let out = e.write_bundle().unwrap();
        let draft: Value = serde_json::from_str(&std::fs::read_to_string(out.join("receipt.draft.json")).unwrap()).unwrap();
        assert_eq!(draft["result"]["final_confirmed"], false); // scorebug final alone is not confirmed
        assert_eq!(draft["result"]["final_basis"], "clock_zero");
        let r = ev(r#"{"type":"final","basis":"end_menu","method":"end_menu_read","home":20,"away":0,"quarter":4,"t_ms":2000,
                      "end_menu":{"away":0,"home":20,"frames":3},"scorebug_last":{"home":17,"away":0,"quarter":4,"t_ms":900},
                      "notes":["final play score not shown on scorebug"]}"#).unwrap();
        assert!(r.get("duplicate").is_none());
        assert_eq!(r["final"]["amends"]["home"], 17);
        assert_eq!(ev(r#"{"type":"final","basis":"clock_zero","home":17,"away":0,"quarter":4}"#).unwrap()["duplicate"], true);
        let out = e.write_bundle().unwrap();
        let draft: Value = serde_json::from_str(&std::fs::read_to_string(out.join("receipt.draft.json")).unwrap()).unwrap();
        let res = &draft["result"];
        assert_eq!((res["away_score"].as_i64(), res["home_score"].as_i64()), (Some(0), Some(20)));
        assert_eq!(res["method"], "end_menu_read");
        assert_eq!(res["final_basis"], "end_menu");
        assert_eq!(res["final_confirmed"], true);
        assert_eq!(res["scorebug_last"]["home_score"], 17);
        assert_eq!(res["notes"][0], "final play score not shown on scorebug");
        let bundle: Value = serde_json::from_str(&std::fs::read_to_string(out.join("bundle.json")).unwrap()).unwrap();
        assert_eq!(bundle["final_confirmed"], true);
        assert_eq!(bundle["final"]["basis"], "end_menu");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn end_menu_final_respects_sidecar_final_confirmed_false() {
        use clap::Parser;
        let dir = std::env::temp_dir().join(format!("qorcap-uncorroborated-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let d = dir.to_string_lossy().into_owned();
        let crate::cli::Cmd::Record(r) = crate::cli::Cli::try_parse_from(["q", "record", "--device", "testpattern", "--bundle", &d]).unwrap().cmd else { panic!() };
        let e = Engine::new(r, "ffmpeg-x".into());
        let ev = |s: &str| e.event(&serde_json::from_str::<Value>(s).unwrap());
        ev(r#"{"type":"score","home":17,"away":0,"quarter":4,"clock_s":0}"#).unwrap();
        ev(r#"{"type":"final","basis":"end_menu","method":"end_menu_read","home":20,"away":0,"quarter":4,"t_ms":2000,
               "final_confirmed":false,
               "delta_corroboration":null,
               "scorebug_last":{"home":17,"away":0,"quarter":4,"t_ms":900},
               "notes":["one-legal-step delta NOT corroborated"]}"#).unwrap();
        let out = e.write_bundle().unwrap();
        let draft: Value = serde_json::from_str(&std::fs::read_to_string(out.join("receipt.draft.json")).unwrap()).unwrap();
        assert_eq!(draft["result"]["method"], "end_menu_read");
        assert_eq!(draft["result"]["final_confirmed"], false);
        assert_eq!(draft["result"]["home_score"], 20);
        assert_eq!(draft["result"]["notes"][0], "one-legal-step delta NOT corroborated");
        let bundle: Value = serde_json::from_str(&std::fs::read_to_string(out.join("bundle.json")).unwrap()).unwrap();
        assert_eq!(bundle["final_confirmed"], false);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn end_menu_final_with_haptic_corroboration_confirms() {
        use clap::Parser;
        let dir = std::env::temp_dir().join(format!("qorcap-haptic-corr-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let d = dir.to_string_lossy().into_owned();
        let crate::cli::Cmd::Record(r) = crate::cli::Cli::try_parse_from(["q", "record", "--device", "testpattern", "--bundle", &d]).unwrap().cmd else { panic!() };
        let e = Engine::new(r, "ffmpeg-x".into());
        let ev = |s: &str| e.event(&serde_json::from_str::<Value>(s).unwrap());
        ev(r#"{"type":"final","basis":"end_menu","method":"end_menu_read","home":20,"away":0,"quarter":4,"t_ms":2000,
               "final_confirmed":true,
               "delta_corroboration":{"kind":"haptic","t_ms":1500},
               "scorebug_last":{"home":17,"away":0,"quarter":4,"t_ms":900},
               "notes":["one-legal-step delta corroborated via haptic"]}"#).unwrap();
        let out = e.write_bundle().unwrap();
        let draft: Value = serde_json::from_str(&std::fs::read_to_string(out.join("receipt.draft.json")).unwrap()).unwrap();
        assert_eq!(draft["result"]["final_confirmed"], true);
        assert_eq!(draft["result"]["delta_corroboration"]["kind"], "haptic");
        let bundle: Value = serde_json::from_str(&std::fs::read_to_string(out.join("bundle.json")).unwrap()).unwrap();
        assert_eq!(bundle["final_confirmed"], true);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn disk_guard_refuses_start_when_min_free_huge() {
        use clap::Parser;
        let dir = std::env::temp_dir().join(format!("qorcap-disk-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let d = dir.to_string_lossy().into_owned();
        let out = dir.join("video.mp4").to_string_lossy().into_owned();
        // Demand an absurd amount of free space so check always fails.
        let crate::cli::Cmd::Record(r) = crate::cli::Cli::try_parse_from([
            "q", "record", "--device", "testpattern", "--bundle", &d, "--min-free-gb", "999999", "--out", &out,
        ]).unwrap().cmd else { panic!() };
        let e = Engine::new(r, "ffmpeg-x".into());
        let err = e.start_recording(&out, None).unwrap_err();
        assert!(err.contains("not enough free disk space"), "{err}");
        let _ = std::fs::remove_dir_all(&dir);
    }
}

impl Engine {
    pub fn bundle_dir(&self) -> Option<PathBuf> { self.args.bundle.as_ref().map(PathBuf::from) }
    fn ev_path(&self, name: &str) -> PathBuf {
        match self.bundle_dir() {
            Some(d) => d.join(name),
            None => PathBuf::from(format!("{}.{name}", self.args.out.clone().unwrap_or_else(|| "qorcap".into()))),
        }
    }
    fn broadcast(&self, kind: &str, v: &Value) {
        let msg = format!("event: {kind}\ndata: {v}\n\n");
        self.subs.lock().unwrap().retain(|s| s.send(msg.clone()).is_ok());
    }
    fn note_reopen(&self) {
        let st = self.ev.lock().unwrap();
        let v = json!({"type": "reopen", "mono_ms": self.ms(), "wall_ms": wall_ms(), "chain_head": st.seg.head, "segment_seq": st.seg.seq});
        drop(st);
        append_jsonl(&self.ev_path("manifest.jsonl"), &v);
    }

    /// Called for every completed fMP4 segment (exact bytes on disk).
    pub fn on_segment(&self, file: &str, offset: u64, bytes: &[u8]) {
        let frames = self.status.lock().unwrap().frames;
        let mut st = self.ev.lock().unwrap();
        let (seq, sha, prev, head) = st.seg.push(bytes);
        st.last_seg_sha = Some(sha.clone());
        let kind = if bytes.len() >= 8 && &bytes[4..8] == b"moof" { "media" } else { "init" };
        let fname = Path::new(file).file_name().map(|f| f.to_string_lossy().into_owned()).unwrap_or_default();
        let m = json!({"type": "segment", "seq": seq, "kind": kind, "file": fname, "offset": offset, "bytes": bytes.len(),
            "sha256": sha, "prev": prev, "chain_head": head, "mono_ms": self.ms(), "wall_ms": wall_ms(), "frames": frames,
            "device": self.args.device, "device_id": st.device_id});
        drop(st);
        append_jsonl(&self.ev_path("manifest.jsonl"), &m);
        crate::inputproof::touch_recording_marker(self.bundle_dir().as_deref());
        self.broadcast("segment", &m);
        // Seal controller inputs received up to now into this segment's chunk (same clock, same checkpoint).
        let (ih, ihead, n) = self.input.lock().unwrap().seal(seq, self.started.elapsed().as_micros() as u64);
        if self.args.no_controller { self.checkpoint("segment", json!({})); }
        else { self.checkpoint_with("segment", json!({"input_seg": seq, "input_n": n, "input_head": ihead}), Some(ih)); }
    }

    /// Append a QorTime-shaped checkpoint binding the current chain head + score/clock state.
    pub fn checkpoint(&self, trigger: &str, extra: Value) -> Value { self.checkpoint_with(trigger, extra, None) }
    fn checkpoint_with(&self, trigger: &str, extra: Value, input_hash: Option<String>) -> Value {
        if self.sealed.load(Ordering::SeqCst) {
            warn!("checkpoint '{trigger}' after the stop checkpoint refused (chain is sealed)");
            return Value::Null;
        }
        let mut st = self.ev.lock().unwrap();
        let mut ext = json!({"trigger": trigger, "segment_seq": st.seg.seq, "segment_hash": st.last_seg_sha, "chain_head": st.seg.head,
            "score": st.score, "mono_ms": self.ms(), "match_id": self.args.match_id, "role": self.args.role});
        if let (Value::Object(e), Value::Object(x)) = (&mut ext, extra) { e.extend(x); }
        let cp = st.cps.next_with_input(wall_ms(), ext, input_hash);
        st.checkpoints.push(cp.clone());
        drop(st);
        append_jsonl(&self.ev_path("checkpoints.jsonl"), &cp);
        self.broadcast("checkpoint", &cp);
        cp
    }

    /// Referee nonce challenge: record receipt time against the chain and (optionally) stamp it on video.
    pub fn challenge(&self, b: &Value) -> Result<Value, String> {
        let nonce = b["nonce"].as_str().ok_or("missing nonce")?;
        let pattern = ev::pattern_for(nonce)?;
        let now = wall_ms();
        let issued = b["issued_ms"].as_u64().unwrap_or(now);
        let (seq, head) = { let st = self.ev.lock().unwrap(); (st.seg.seq, st.seg.head.clone()) };
        let c = json!({"id": b["id"].as_str().map(String::from).unwrap_or_else(|| nonce[..8].to_string()),
            "role": b["role"].as_str().unwrap_or(&self.args.role), "phase": b["phase"].as_str().unwrap_or("kickoff"),
            "nonce": nonce, "pattern": pattern, "issued_ms": issued, "deadline_ms": b["deadline_ms"].as_u64().unwrap_or(issued + 90_000),
            "received_wall_ms": now, "received_mono_ms": self.ms(), "segment_seq_at_receipt": seq, "chain_head_at_receipt": head});
        if self.args.stamp {
            let text = format!("QorCap nonce {}  {}", &nonce[..12], pattern.join(" "));
            let tmp = self.stamp_file.with_extension("tmp");
            if std::fs::write(&tmp, text).is_ok() { let _ = std::fs::rename(&tmp, &self.stamp_file); }
        }
        self.ev.lock().unwrap().challenges.push(c.clone());
        append_jsonl(&self.ev_path("challenges.jsonl"), &c);
        let cp = self.checkpoint("challenge", json!({"nonce": nonce}));
        info!("referee challenge {nonce} received; pattern {pattern:?}; bound at checkpoint {}", cp["seq"]);
        Ok(json!({"ok": true, "challenge": c, "checkpoint": cp}))
    }

    /// Scorebug reader events (from the sidecar): score | obs | final.
    pub fn event(self: &Arc<Self>, b: &Value) -> Result<Value, String> {
        let ty = b["type"].as_str().ok_or("missing type")?;
        let t_ms = b["t_ms"].as_u64().unwrap_or_else(wall_ms);
        let int = |k: &str| b[k].as_i64().ok_or(format!("{k} must be an integer"));
        match ty {
            "score" => {
                let (h, a, q) = (int("home")?, int("away")?, int("quarter")?);
                let clock_s = b["clock_s"].as_i64();
                let red = b["clock_red"].as_bool().unwrap_or(false);
                let read = json!({"t_ms": t_ms, "home": h, "away": a, "quarter": q, "clock_s": clock_s, "clock_red": red});
                let mut st = self.ev.lock().unwrap();
                st.timeline.push(read.clone());
                st.score = json!({"home": h, "away": a, "quarter": q, "clock_s": clock_s, "clock_red": red});
                let score_changed = st.last_score.map(|p| p != (h, a, q)).unwrap_or(true);
                let prev_score = st.last_score;
                st.last_score = Some((h, a, q));
                let clock_state = (clock_s == Some(0), red);
                let clock_changed = st.last_clock.map(|c| c != clock_state).unwrap_or(false);
                st.last_clock = Some(clock_state);
                drop(st);
                append_jsonl(&self.ev_path("timeline.jsonl"), &read);
                let mut out = json!({"ok": true});
                if score_changed {
                    let cp = self.checkpoint("score_change", json!({"prev_score": prev_score.map(|p| json!({"home": p.0, "away": p.1, "quarter": p.2}))}));
                    out["checkpoint"] = cp;
                    // Only clip actual scoring plays (not the first read or a quarter change).
                    if let Some(p) = prev_score { if (p.0, p.1) != (h, a) { self.auto_clip(&format!("score-a{a}-h{h}-q{q}"), self.args.clip_secs); } }
                } else if clock_changed {
                    out["checkpoint"] = self.checkpoint("clock_state", json!({}));
                }
                Ok(out)
            }
            "obs" => {
                let o = b["obs"].as_str().ok_or("missing obs")?;
                if o != "scorebug_absent" && o != "final_screen" { return Err("obs must be scorebug_absent|final_screen".into()); }
                let v = json!({"t_ms": t_ms, "until_ms": b["until_ms"].as_u64().unwrap_or(t_ms), "obs": o, "n": b["n"].as_u64().unwrap_or(0)});
                self.ev.lock().unwrap().timeline.push(v.clone());
                append_jsonl(&self.ev_path("timeline.jsonl"), &v);
                Ok(json!({"ok": true}))
            }
            "final" => {
                let basis = b["basis"].as_str().ok_or("missing basis")?;
                if !["clock_zero", "scorebug_gone", "final_screen", "end_menu"].contains(&basis) { return Err("basis must be clock_zero|scorebug_gone|final_screen|end_menu".into()); }
                let (h, a, q) = (int("home")?, int("away")?, int("quarter")?);
                if h == a { return Err("tied score cannot be final (overtime expected)".into()); }
                // v0.3.4: the end-of-game menu score (read on consecutive frames by the sidecar) is the authoritative
                // final. It may arrive after a scorebug final and then amends it (once); a scorebug final never
                // replaces an end-menu final.
                let end_menu = basis == "end_menu";
                if end_menu && b["method"].as_str() != Some("end_menu_read") { return Err("end_menu final needs method end_menu_read".into()); }
                let mut fin = json!({"home": h, "away": a, "quarter": q, "t_ms": t_ms, "basis": basis});
                if end_menu {
                    fin["method"] = json!("end_menu_read");
                    for k in ["end_menu", "scorebug_last", "notes", "final_confirmed", "delta_corroboration"] {
                        if !b[k].is_null() { fin[k] = b[k].clone(); }
                    }
                }
                let amended = {
                    let mut st = self.ev.lock().unwrap();
                    let prev = st.final_.clone();
                    match &prev {
                        Some(p) if p["basis"] == "end_menu" || !end_menu => return Ok(json!({"ok": true, "duplicate": true})),
                        Some(p) => { fin["amends"] = json!({"basis": p["basis"], "home": p["home"], "away": p["away"], "t_ms": p["t_ms"]}); }
                        None => {}
                    }
                    st.final_ = Some(fin.clone());
                    prev.is_some()
                };
                let cp = self.checkpoint("final", json!({"final": fin}));
                info!("FINAL via {basis}: away {a} - home {h} (Q{q}){}", if amended { " (amends the scorebug final)" } else { "" });
                if !amended { self.auto_clip("final", self.args.replay.unwrap_or(60)); }
                let me = self.clone();
                // Draft only: the input proof decodes the whole video and must wait for the recorder to stop.
                std::thread::spawn(move || { std::thread::sleep(Duration::from_millis(500)); me.write_bundle_opts(false); });
                Ok(json!({"ok": true, "final": fin, "checkpoint": cp}))
            }
            _ => Err(format!("unknown event type {ty}")),
        }
    }

    fn auto_clip(self: &Arc<Self>, name: &str, secs: u64) {
        if self.args.replay.is_none() { warn!("auto clip {name} skipped: replay buffer disabled"); return; }
        let dir = self.bundle_dir().map(|d| d.join("clips")).unwrap_or_else(|| PathBuf::from(&self.args.replay_dir));
        let out = dir.join(format!("{}-{name}.mp4", now_stamp())).to_string_lossy().into_owned();
        let me = self.clone();
        let name = name.to_string();
        std::thread::spawn(move || match me.save_replay_secs(Some(&out), Some(secs)) {
            Ok(p) => {
                let sha = std::fs::read(&p).map(|d| ev::sha256_hex(&d)).unwrap_or_default();
                let c = json!({"type": "clip", "name": name, "file": p, "sha256": sha, "secs": secs, "wall_ms": wall_ms()});
                me.ev.lock().unwrap().clips.push(c.clone());
                append_jsonl(&me.ev_path("manifest.jsonl"), &c);
                me.broadcast("clip", &c);
            }
            Err(e) => warn!("auto clip {name} failed: {e}"),
        });
    }

    /// Write bundle.json + receipt.draft.json (unsigned).
    /// If no `final` event, use the last timeline scorebug read (not 0–0) and label final unconfirmed.
    pub fn write_bundle(&self) -> Option<PathBuf> { self.write_bundle_opts(true) }
    pub fn write_bundle_opts(&self, input_proof: bool) -> Option<PathBuf> {
        let st = self.ev.lock().unwrap();
        let unconfirmed = st.final_.is_none();
        let fin = st.final_.clone().unwrap_or_else(|| {
            if let Some((h, a, q)) = st.last_score {
                json!({"home": h, "away": a, "quarter": q, "t_ms": wall_ms(), "basis": Value::Null, "unconfirmed": true})
            } else if let Some(sc) = st.score.as_object() {
                json!({"home": sc.get("home").and_then(|v| v.as_i64()),
                       "away": sc.get("away").and_then(|v| v.as_i64()),
                       "quarter": sc.get("quarter").and_then(|v| v.as_i64()),
                       "t_ms": wall_ms(), "basis": Value::Null, "unconfirmed": true})
            } else {
                // Nothing was read: say so (scores/quarter null, winner "unknown") instead of inventing 0-0 in Q4.
                json!({"home": Value::Null, "away": Value::Null, "quarter": Value::Null, "t_ms": wall_ms(), "basis": Value::Null, "unconfirmed": true})
            }
        });
        let mut receipt = ev::draft_receipt(&self.args.match_id, &self.args.role, &self.args.player_id, st.started_wall_ms, wall_ms(), &fin, &st.challenges);
        // End-of-game-menu read is required to confirm (v0.3.4). From v0.3.8 the sidecar may set
        // final_confirmed=false when a one-legal-step delta lacks corroboration; honor that field.
        let is_end_menu = !unconfirmed && fin["basis"] == "end_menu";
        let confirmed = if is_end_menu {
            match fin.get("final_confirmed") {
                Some(v) if v.is_boolean() => v.as_bool().unwrap_or(true),
                _ => true, // legacy posts without the field: confirm (exact-match / pre-gate behavior)
            }
        } else {
            false
        };
        if let (false, Some(r)) = (unconfirmed, receipt["result"].as_object_mut()) {
            r.insert("final_confirmed".into(), json!(confirmed));
            if is_end_menu {
                r.insert("method".into(), json!("end_menu_read"));
                r.insert("final_basis".into(), json!("end_menu"));
                if let Some(sb) = fin.get("scorebug_last").filter(|v| v.is_object()) {
                    r.insert("scorebug_last".into(), json!({"home_score": sb["home"], "away_score": sb["away"], "quarter": sb["quarter"], "t_ms": sb["t_ms"]}));
                }
                if let Some(n) = fin.get("notes").filter(|v| v.as_array().map(|a| !a.is_empty()).unwrap_or(false)) { r.insert("notes".into(), n.clone()); }
                if let Some(d) = fin.get("delta_corroboration").filter(|v| !v.is_null()) {
                    r.insert("delta_corroboration".into(), d.clone());
                }
            } else {
                r.insert("notes".into(), json!(["end-of-game menu score not read: scorebug final only, NOT confirmed"]));
            }
        }
        if unconfirmed {
            if let Some(r) = receipt["result"].as_object_mut() {
                r.remove("final_basis");
                r.insert("final_confirmed".into(), json!(false));
                r.insert("method".into(), json!("last_scorebug_read_unconfirmed"));
            }
            info!("draft receipt uses last scorebug read AWAY {} - HOME {} (final not confirmed)", fin["away"], fin["home"]);
        }
        let bundle = json!({
            "schema": "qorcap-bundle/v0", "qorcap_version": env!("CARGO_PKG_VERSION"),
            "match_id": self.args.match_id, "role": self.args.role, "device": self.args.device, "device_id": st.device_id,
            "segments": st.seg.seq, "chain_head": st.seg.head, "checkpoints": st.checkpoints.len(),
            "checkpoint_head": st.cps.prev, "challenges": st.challenges.len(), "clips": st.clips, "final": st.final_,
            "final_confirmed": confirmed,
            "receipt_draft_sha256": ev::jcs_sha(&receipt),
            "claims": "Proves the integrity and timing of these video bytes (hash chain + checkpoints, referee co-signable). Does NOT prove who was playing.",
        });
        drop(st);
        let dir = self.bundle_dir().unwrap_or_else(|| PathBuf::from("."));
        let _ = std::fs::create_dir_all(&dir);
        let w = |n: &str, v: &Value| std::fs::write(dir.join(n), serde_json::to_string_pretty(v).unwrap()).map_err(|e| error!("write {n}: {e}"));
        let _ = w("receipt.draft.json", &receipt);
        let _ = w("bundle.json", &bundle);
        // Test hook: simulate the slow input-proof decode (a full match takes ~105 s on MyTurn).
        if input_proof { if let Some(ms) = std::env::var("QORCAP_TEST_BUNDLE_DELAY_MS").ok().and_then(|v| v.parse::<u64>().ok()) { std::thread::sleep(Duration::from_millis(ms)); } }
        if input_proof && self.bundle_dir().is_some() && !self.args.no_controller {
            match crate::inputproof::apply(&dir, Some(&self.ffmpeg), crate::align::DEFAULT_N_FRAMES) {
                Ok(p) => {
                    let h = &p["human"]; let a = &p["alignment"];
                    info!("input proof: status {} | human-likeness {} ({}, confidence {}) | input-video match {} permille (chance {}) | offset {} ms",
                        p["status"], h["score_permille"], h["label"], h["confidence"], a["match_permille"], a["chance_permille"], a["offset_ms"]);
                    for w in p["warnings"].as_array().into_iter().flatten() { warn!("input proof: {}", w.as_str().unwrap_or("")); }
                }
                Err(e) => warn!("input proof not written: {e}"),
            }
        }
        info!("match bundle written to {}", dir.display());
        Some(dir)
    }
}
