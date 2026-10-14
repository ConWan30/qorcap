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
    pub state: String, // starting | waiting_for_device | capturing | reopening | stopped
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
    pub recording: Option<String>,
    pub recording_secs: f64,
    pub replay_secs_buffered: f64,
    pub replay_bytes: usize,
    pub replay_window: u64,
    pub uptime_secs: f64,
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
    match OpenOptions::new().create(true).append(true).open(path) {
        Ok(mut f) => { let _ = writeln!(f, "{v}"); let _ = f.flush(); }
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

impl Engine {
    pub fn new(args: RecordArgs, ffmpeg: String) -> Arc<Self> {
        let window = args.replay.unwrap_or(3).max(3);
        // Cap memory: bitrate estimate * window * 1.5 (+ audio), at least 64 MiB.
        let bps = parse_bitrate(&args.bitrate).unwrap_or(8_000_000) + 200_000;
        let cap = ((bps / 8) as usize * window as usize * 3 / 2).max(64 << 20);
        let dir = std::env::temp_dir().join({ static N: AtomicU64 = AtomicU64::new(0); format!("qorcap-{}-{}", std::process::id(), N.fetch_add(1, Ordering::Relaxed)) });
        let _ = std::fs::create_dir_all(&dir);
        let st = Status { state: "starting".into(), device: args.device.clone(), res: format!("{}x{}", args.res.0, args.res.1), fps_target: args.fps, replay_window: args.replay.unwrap_or(0), ..Default::default() };
        Arc::new(Self { ffmpeg, ring: Mutex::new(ReplayRing::new(window, cap)), rec: Mutex::new(None), status: Mutex::new(st), shutdown: AtomicBool::new(false),
            ev: Mutex::new(MatchState { started_wall_ms: wall_ms(), score: Value::Null, ..Default::default() }), subs: Mutex::new(vec![]),
            stamp_file: dir.join("stamp.txt"),
            snapshot: dir.join("snapshot.jpg"), exit_when_recording_ends: AtomicBool::new(false), started: Instant::now(), last_data_ms: AtomicU64::new(0), args })
    }
    fn ms(&self) -> u64 { self.started.elapsed().as_millis() as u64 }

    pub fn status(&self) -> Status {
        let mut s = self.status.lock().unwrap().clone();
        { let r = self.ring.lock().unwrap(); s.replay_bytes = r.len_bytes(); s.replay_secs_buffered = r.span_ms() as f64 / 1000.0; }
        if self.args.replay.is_none() { s.replay_bytes = 0; s.replay_secs_buffered = 0.0; }
        if let Some(r) = self.rec.lock().unwrap().as_ref() { s.recording = Some(r.path.clone()); s.recording_secs = r.started.elapsed().as_secs_f64(); }
        s.uptime_secs = self.started.elapsed().as_secs_f64();
        s
    }

    pub fn start_recording(self: &Arc<Self>, path: &str, duration: Option<u64>) -> Result<String, String> {
        let mut g = self.rec.lock().unwrap();
        if let Some(r) = g.as_ref() { return Err(format!("already recording to {}", r.path)); }
        let r = self.spawn_recorder(path, 1, duration.map(|d| Instant::now() + Duration::from_secs(d)))?;
        info!("recording started -> {}", r.path);
        let p = r.path.clone();
        *g = Some(r);
        Ok(p)
    }
    fn spawn_recorder(self: &Arc<Self>, base: &str, part: u32, deadline: Option<Instant>) -> Result<Recorder, String> {
        let path = part_name(base, part);
        if let Some(d) = Path::new(&path).parent() { if !d.as_os_str().is_empty() { let _ = std::fs::create_dir_all(d); } }
        let mut file = std::fs::File::create(&path).map_err(|e| format!("cannot create {path}: {e}"))?;
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
    /// After a device reopen, timestamps restart: close the current part, open the next.
    fn rotate_recording(self: &Arc<Self>) {
        let mut g = self.rec.lock().unwrap();
        if let Some(r) = g.take() {
            let (base, part, deadline) = (r.base.clone(), r.part + 1, r.deadline);
            let old = finish(r);
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
        let mut backoff = 1u64;
        while !self.shutdown.load(Ordering::SeqCst) {
            let mut audio = None;
            if self.args.device != "testpattern" {
                match ffmpeg::list_devices(&self.ffmpeg) {
                    Err(e) => { self.set_state("waiting_for_device", Some(format!("cannot run ffmpeg `{}`: {e}", self.ffmpeg))); error!("cannot run ffmpeg `{}`: {e}", self.ffmpeg); self.sleep(5); continue; }
                    Ok(devs) => {
                        if !devs.iter().any(|d| d.kind == "video" && d.name == self.args.device) {
                            let msg = format!("video device \"{}\" not present (have: {})", self.args.device,
                                devs.iter().filter(|d| d.kind == "video").map(|d| d.name.as_str()).collect::<Vec<_>>().join(", "));
                            if self.status.lock().unwrap().state != "waiting_for_device" { warn!("{msg}; waiting for it to appear"); }
                            self.set_state("waiting_for_device", Some(msg));
                            self.sleep(1);
                            continue;
                        }
                        audio = ffmpeg::pick_audio(&self.args.device, &self.args.audio, &devs);
                        self.ev.lock().unwrap().device_id = devs.iter().find(|d| d.kind == "video" && d.name == self.args.device).and_then(|d| d.alt.clone());
                    }
                }
            }
            self.status.lock().unwrap().audio = audio.clone();
            let t0 = Instant::now();
            let ok = self.run_capture_once(audio.as_deref());
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
        if let Some(p) = self.stop_recording() { info!("finalised {p}"); }
        if self.args.bundle.is_some() { self.write_bundle(); }
        self.set_state("stopped", None);
        let _ = std::fs::remove_file(&self.snapshot);
    }

    fn sleep(&self, secs: u64) {
        let end = Instant::now() + Duration::from_secs(secs);
        while Instant::now() < end && !self.shutdown.load(Ordering::SeqCst) { std::thread::sleep(Duration::from_millis(100)); }
    }

    /// One capture session. Returns true if it produced data.
    fn run_capture_once(self: &Arc<Self>, audio: Option<&str>) -> bool {
        if self.args.stamp && !self.stamp_file.exists() { let _ = std::fs::write(&self.stamp_file, "QorCap  no referee nonce yet"); }
        let stamp = if self.args.stamp { Some(self.stamp_file.to_string_lossy().into_owned()) } else { None };
        let args = ffmpeg::capture_args(&self.args, audio, &self.snapshot.to_string_lossy(), stamp.as_deref());
        info!("opening \"{}\" audio={:?} {}x{}@{} enc={}", self.args.device, audio, self.args.res.0, self.args.res.1, self.args.fps, self.args.encoder);
        let child = ffmpeg::command(&self.ffmpeg).args(&args).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn();
        let mut child = match child { Ok(c) => c, Err(e) => { let m = format!("cannot start ffmpeg `{}`: {e}", self.ffmpeg); error!("{m}"); self.set_state("reopening", Some(m)); return false; } };
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
        if !self.shutdown.load(Ordering::SeqCst) {
            std::thread::sleep(Duration::from_millis(200));
            let tail = last_err.lock().unwrap().join(" | ");
            let m = format!("ffmpeg capture exited ({:?}){}", st.ok(), if tail.is_empty() { String::new() } else { format!(": {tail}") });
            let hint = if tail.contains("Could not run graph") || tail.contains("I/O error") || tail.contains("busy") { " (device busy/shared by another app, or unplugged)" } else { "" };
            error!("{m}{hint}");
            self.set_state("reopening", Some(format!("{m}{hint}")));
        }
        got
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
        self.broadcast("segment", &m);
        self.checkpoint("segment", json!({}));
    }

    /// Append a QorTime-shaped checkpoint binding the current chain head + score/clock state.
    pub fn checkpoint(&self, trigger: &str, extra: Value) -> Value {
        let mut st = self.ev.lock().unwrap();
        let mut ext = json!({"trigger": trigger, "segment_seq": st.seg.seq, "segment_hash": st.last_seg_sha, "chain_head": st.seg.head,
            "score": st.score, "mono_ms": self.ms(), "match_id": self.args.match_id, "role": self.args.role});
        if let (Value::Object(e), Value::Object(x)) = (&mut ext, extra) { e.extend(x); }
        let cp = st.cps.next(wall_ms(), ext);
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
            "nonce": nonce, "pattern": pattern, "issued_ms": issued, "deadline_ms": b["deadline_ms"].as_u64().unwrap_or(issued + 20_000),
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
                if !["clock_zero", "scorebug_gone", "final_screen"].contains(&basis) { return Err("basis must be clock_zero|scorebug_gone|final_screen".into()); }
                let (h, a, q) = (int("home")?, int("away")?, int("quarter")?);
                if h == a { return Err("tied score cannot be final (overtime expected)".into()); }
                let fin = json!({"home": h, "away": a, "quarter": q, "t_ms": t_ms, "basis": basis});
                { let mut st = self.ev.lock().unwrap(); if st.final_.is_some() { return Ok(json!({"ok": true, "duplicate": true})); } st.final_ = Some(fin.clone()); }
                let cp = self.checkpoint("final", json!({"final": fin}));
                info!("FINAL via {basis}: away {a} - home {h} (Q{q})");
                self.auto_clip("final", self.args.replay.unwrap_or(60));
                let me = self.clone();
                std::thread::spawn(move || { std::thread::sleep(Duration::from_millis(500)); me.write_bundle(); });
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
    pub fn write_bundle(&self) -> Option<PathBuf> {
        let st = self.ev.lock().unwrap();
        let fin = st.final_.clone().unwrap_or(json!({"home": 0, "away": 0, "quarter": 4, "t_ms": wall_ms(), "basis": Value::Null}));
        let mut receipt = ev::draft_receipt(&self.args.match_id, &self.args.role, &self.args.player_id, st.started_wall_ms, wall_ms(), &fin, &st.challenges);
        if st.final_.is_none() { receipt["result"].as_object_mut().unwrap().remove("final_basis"); }
        let bundle = json!({
            "schema": "qorcap-bundle/v0", "qorcap_version": env!("CARGO_PKG_VERSION"),
            "match_id": self.args.match_id, "role": self.args.role, "device": self.args.device, "device_id": st.device_id,
            "segments": st.seg.seq, "chain_head": st.seg.head, "checkpoints": st.checkpoints.len(),
            "checkpoint_head": st.cps.prev, "challenges": st.challenges.len(), "clips": st.clips, "final": st.final_,
            "receipt_draft_sha256": ev::jcs_sha(&receipt),
            "claims": "Proves the integrity and timing of these video bytes (hash chain + checkpoints, referee co-signable). Does NOT prove who was playing.",
        });
        drop(st);
        let dir = self.bundle_dir().unwrap_or_else(|| PathBuf::from("."));
        let _ = std::fs::create_dir_all(&dir);
        let w = |n: &str, v: &Value| std::fs::write(dir.join(n), serde_json::to_string_pretty(v).unwrap()).map_err(|e| error!("write {n}: {e}"));
        let _ = w("receipt.draft.json", &receipt);
        let _ = w("bundle.json", &bundle);
        info!("match bundle written to {}", dir.display());
        Some(dir)
    }
}
