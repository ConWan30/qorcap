//! Opt-in live tap with a timing weave (PRACTICE lane only, default off).
//!
//! QorCap stays the only reader of the capture card. When the tap is on, the same MPEG-TS bytes that
//! reached the recording are also sent as UDP datagrams to a loopback port (QorStream listens there and
//! writes HLS with `-c copy`). Nothing is re-encoded and the capture command is unchanged.
//!
//! The weave: every `every` recorded frames the tap adds one timed ID3 cue (TS stream_type 0x15, PES
//! private_stream_1, PTS = that frame's PTS) carrying QorCap's own frame clock for that frame:
//! the recording part file, the frame index (same rule as frame_clock.jsonl), the 90 kHz PTS, the
//! QorCap monotonic time when the frame was read, the press clock (`wall_anchor_ms + t_us/1000`),
//! the session seal state and the newest controller press. Each cue links to the previous one with a
//! short sha256 chain so a watch page can tell a missing or reordered cue from a clean run.
//! The cues ride inside the HLS segments, so the live page and the replay read the same timing.
//!
//! What this is not: it is not in the hash chain, not in the receipt, not checked by `verify-bundle`
//! and not a referee submission. It shows timing line-up (skill-proof / fair-proof context only).
//! It does not say who holds the controller. The recording is byte-identical with the tap on or off.
use crate::ring::{self, TS};
use sha2::{Digest, Sha256};
use std::fs::OpenOptions;
use std::io::Write;
use std::net::{IpAddr, Ipv4Addr, SocketAddr, ToSocketAddrs, UdpSocket};
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;

pub const SCHEMA: &str = "qorweave/v0";
pub const ENV_URL: &str = "QORCAP_LIVE_TAP";
pub const ENV_LANE: &str = "QORCAP_LANE";
pub const ENV_EVERY: &str = "QORCAP_LIVE_TAP_EVERY";
pub const FILE_NAME: &str = "live_weave.jsonl";
/// PID of the added timed-ID3 stream. ffmpeg's mpegts muxer uses 0x100.. for A/V and 0x1000 for the PMT.
pub const ID3_PID: u16 = 0x01E0;
/// ID3 TXXX description that names a weave cue.
pub const CUE_KEY: &str = "qorweave";
pub const DEFAULT_EVERY: u32 = 15;
/// 7 TS packets per datagram (1316 bytes, `pkt_size=1316`), the usual MPEG-TS-over-UDP size.
/// The worker sends only full 1316-byte datagrams; a partial tail waits for the next slice and is
/// flushed after `PARTIAL_HOLD` with no new slice, or at the end of the recording.
const DGRAM_PKTS: usize = 7;
pub const PKT_SIZE: usize = TS * DGRAM_PKTS;
/// Longest a partial datagram waits for the next slice before it is sent short.
const PARTIAL_HOLD: Duration = Duration::from_millis(50);
/// Requested UDP send buffer. The OS may grant less (or, on Linux, report double); the granted size is logged.
pub const SNDBUF_WANT: usize = 8 << 20;
/// Tap counters go to qorcap.log this often while the tap is on, and once more when the recording ends.
const STATS_LOG_EVERY: Duration = Duration::from_secs(30);

/// Resolve the tap target. `Ok(None)`: tap off. `Err`: asked for but refused (logged by the caller).
/// Only `udp://` to a loopback address is accepted, and only on the practice lane.
pub fn resolve(cli_url: Option<&str>, env_url: Option<&str>, practice_flag: bool, env_lane: Option<&str>) -> Result<Option<SocketAddr>, String> {
    let url = cli_url.or(env_url).map(str::trim).filter(|s| !s.is_empty());
    let Some(url) = url else { return Ok(None) };
    if matches!(url.to_ascii_lowercase().as_str(), "0" | "off" | "false" | "no") { return Ok(None); }
    let practice = practice_flag || env_lane.map(str::trim).map(|s| s.eq_ignore_ascii_case("practice")).unwrap_or(false);
    if !practice {
        return Err("live tap refused: practice lane only (pass --practice-lane or set QORCAP_LANE=practice)".into());
    }
    let rest = url.strip_prefix("udp://").ok_or_else(|| format!("live tap refused: only udp://127.0.0.1:PORT is supported (got {url})"))?;
    let hostport = rest.split(['?', '/']).next().unwrap_or("");
    let addr = parse_loopback(hostport).ok_or_else(|| format!("live tap refused: target must be a loopback address with a port (got {hostport})"))?;
    Ok(Some(addr))
}

fn parse_loopback(hostport: &str) -> Option<SocketAddr> {
    let hp = hostport.replacen("localhost", "127.0.0.1", 1);
    let a: SocketAddr = hp.parse().ok().or_else(|| hp.to_socket_addrs().ok()?.next())?;
    if a.port() == 0 || !a.ip().is_loopback() { return None; }
    Some(a)
}

pub fn every_from_env(v: Option<&str>) -> u32 {
    v.and_then(|s| s.trim().parse::<u32>().ok()).filter(|n| (1..=600).contains(n)).unwrap_or(DEFAULT_EVERY)
}

/// What the cue says about the controller and the seal at the moment the cue is cut. Read-only view.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct WeaveCtx {
    pub wall_anchor_ms: u64,
    pub pad: String,
    pub seal: String,
    pub seal_hash: Option<String>,
    /// (seq, t_ms on the press clock, button)
    pub last_press: Option<(u64, u64, String)>,
}

#[derive(Debug, Clone, Default, serde::Serialize)]
pub struct TapStats {
    pub enabled: bool,
    pub target: String,
    pub every: u32,
    pub weave: bool,
    pub frames: u64,
    pub cues: u64,
    pub datagrams: u64,
    pub send_errors: u64,
    /// TS slices dropped because the tap worker was behind. Not dshow drops; the recording is unchanged.
    pub queue_drops: u64,
    /// TS packets inside those dropped slices (188 bytes each).
    pub queue_dropped_packets: u64,
    /// Recorded frames the worker never saw because their slice was dropped.
    pub skipped_frames: u64,
    /// Cue slots (every `every` frames) that got no cue because their frame was in a dropped slice.
    pub skipped_cue_slots: u64,
    /// TS packets in datagrams the socket refused (`send_errors` counts the datagrams).
    pub send_dropped_packets: u64,
    /// Of `send_errors`, how many were WouldBlock (send buffer full).
    pub send_would_block: u64,
    /// Bytes per full datagram (7 x 188).
    pub pkt_size: usize,
    /// UDP send buffer the OS reports after asking for `SNDBUF_WANT`. None: could not be read.
    pub sndbuf_bytes: Option<usize>,
    pub last_hash: Option<String>,
}

struct State {
    sock: UdpSocket,
    dest: SocketAddr,
    out: Vec<u8>,
    pmt_pid: Option<u16>,
    file: Option<String>,
    frame: i64,
    last_pts: Option<u64>,
    cc: u8,
    n: u64,
    prev: String,
    session: String,
    fps: u32,
    weave_ok: bool,
    log: Option<std::fs::File>,
    stats: TapStats,
    /// When the current partial datagram in `out` started waiting.
    out_since: Option<Instant>,
    last_log: Instant,
    /// Counters at the previous stats line, for the per-interval deltas.
    logged: (u64, u64, u64),
    queue_drops: Arc<AtomicU64>,
    queue_drop_pkts: Arc<AtomicU64>,
}

pub struct LiveTap {
    every: u32,
    state: Option<Arc<Mutex<State>>>,
    tx: Option<mpsc::SyncSender<TapWork>>,
    /// Frame numbering kept on the submitting (capture) thread. Only that thread locks it, so it never
    /// waits on the worker. A dropped slice still advances it, so later cue `f` values keep the
    /// frame_clock.jsonl rule instead of shifting by the frames that were dropped.
    count: Mutex<FrameCount>,
    /// TS slices dropped because the worker was behind. Atomic: the capture thread never takes `state`.
    queue_drops: Arc<AtomicU64>,
    /// TS packets in those dropped slices.
    queue_drop_pkts: Arc<AtomicU64>,
}

#[derive(Default)]
struct FrameCount { file: Option<String>, frame: i64, last_pts: Option<u64> }

/// Work item for the tap worker. The capture thread only try_sends this; it never waits on disk or UDP.
struct TapWork {
    data: Vec<u8>,
    t_mono_us: u64,
    file: String,
    pat: Option<Vec<u8>>,
    pmt: Option<Vec<u8>>,
    ctx: WeaveCtx,
    /// Frame index and last video PTS at the start of this slice, from `FrameCount`.
    start: (i64, Option<u64>),
}

/// Queued slices (each at most one 96 KB capture read). 8 was too few on a loaded PC: on
/// 2026-11-11 practice match-20261111-183311, 15.6% of cue slots were skipped. 32 bounds the queue at
/// about 3 MB. If the worker still falls behind we drop tap data, never the recording.
const TAP_QUEUE: usize = 32;

impl LiveTap {
    pub fn off() -> Self { Self { every: DEFAULT_EVERY, state: None, tx: None, count: Mutex::new(FrameCount::default()), queue_drops: Arc::new(AtomicU64::new(0)), queue_drop_pkts: Arc::new(AtomicU64::new(0)) } }

    pub fn open(dest: Option<SocketAddr>, every: u32, session: &str, fps: u32, bundle: Option<&Path>) -> Self {
        let Some(dest) = dest else { return Self::off() };
        let bind: SocketAddr = if dest.is_ipv4() { SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 0) } else { "[::1]:0".parse().unwrap() };
        let sock = match UdpSocket::bind(bind) { Ok(s) => s, Err(e) => { log::error!("live tap: cannot bind {bind}: {e}"); return Self::off(); } };
        let _ = sock.set_nonblocking(true);
        let sndbuf = set_send_buffer(&sock, SNDBUF_WANT);
        match sndbuf {
            Some(n) => log::info!("live tap: UDP send buffer asked {} bytes, OS reports {n} bytes; datagrams {PKT_SIZE} bytes (pkt_size, 7 TS packets)", SNDBUF_WANT),
            None => log::warn!("live tap: could not set or read the UDP send buffer (asked {} bytes); using the OS default", SNDBUF_WANT),
        }
        let queue_drops = Arc::new(AtomicU64::new(0));
        let queue_drop_pkts = Arc::new(AtomicU64::new(0));
        let log = bundle.and_then(|d| {
            let _ = std::fs::create_dir_all(d);
            OpenOptions::new().create(true).append(true).open(d.join(FILE_NAME)).ok()
        });
        let mut st = State { sock, dest, out: Vec::with_capacity(TS * DGRAM_PKTS), pmt_pid: None, file: None, frame: 0, last_pts: None, cc: 0, n: 0,
            prev: "0".repeat(16), session: session.to_string(), fps, weave_ok: true, log,
            stats: TapStats { enabled: true, target: format!("udp://{dest}"), every, weave: true, pkt_size: PKT_SIZE, sndbuf_bytes: sndbuf, ..Default::default() },
            out_since: None, last_log: Instant::now(), logged: (0, 0, 0), queue_drops: queue_drops.clone(), queue_drop_pkts: queue_drop_pkts.clone() };
        if let Some(f) = st.log.as_mut() {
            let h = serde_json::json!({"type": "header", "schema": SCHEMA, "every": every, "id3_pid": ID3_PID, "target": format!("udp://{dest}"),
                "note": "Practice-lane live tap. Each cue is also sent in-band as timed ID3 on the live stream. Not in the hash chain or the receipt."});
            let _ = writeln!(f, "{h}");
        }
        let state = Arc::new(Mutex::new(st));
        let (tx, rx) = mpsc::sync_channel::<TapWork>(TAP_QUEUE);
        let state_w = state.clone();
        let spawned = thread::Builder::new().name("qorcap-livetap".into()).spawn(move || {
            loop {
                match rx.recv_timeout(PARTIAL_HOLD) {
                    Ok(w) => {
                        let mut g = state_w.lock().unwrap_or_else(|e| e.into_inner());
                        let ctx = w.ctx;
                        ingest(&mut g, every, &w.data, w.t_mono_us, &w.file, w.pat.as_deref(), w.pmt.as_deref(), &|| ctx.clone(), Some(w.start));
                        maybe_log_stats(&mut g, Instant::now());
                    }
                    Err(mpsc::RecvTimeoutError::Timeout) => {
                        let mut g = state_w.lock().unwrap_or_else(|e| e.into_inner());
                        if g.out_since.map(|t| t.elapsed() >= PARTIAL_HOLD).unwrap_or(false) { flush(&mut g); }
                        maybe_log_stats(&mut g, Instant::now());
                    }
                    Err(mpsc::RecvTimeoutError::Disconnected) => break,
                }
            }
        });
        if let Err(e) = spawned {
            log::error!("live tap: cannot start the tap worker: {e}; tap stays off (recording unchanged)");
            return Self::off();
        }
        log::info!("live tap ON -> udp://{dest} (practice lane, weave cue every {every} frames, recording unchanged)");
        Self { every, state: Some(state), tx: Some(tx), count: Mutex::new(FrameCount::default()), queue_drops, queue_drop_pkts }
    }

    /// End of a recording: send any partial datagram and write the tap counters to qorcap.log.
    pub fn log_summary(&self, why: &str) {
        let Some(m) = &self.state else { return };
        let mut g = m.lock().unwrap_or_else(|e| e.into_inner());
        flush(&mut g);
        let line = stats_line(&g);
        log::info!("live tap summary ({why}): {line}");
    }

    pub fn enabled(&self) -> bool { self.state.is_some() }

    pub fn stats(&self) -> Option<TapStats> {
        self.state.as_ref().map(|m| {
            let mut s = m.lock().unwrap_or_else(|e| e.into_inner()).stats.clone();
            s.queue_drops = self.queue_drops.load(Ordering::Relaxed);
            s.queue_dropped_packets = self.queue_drop_pkts.load(Ordering::Relaxed);
            s
        })
    }

    /// `data` is exactly the TS bytes that just reached the recording. `pat`/`pmt` are the cached tables
    /// (sent first whenever a new part file starts). `ctx` is called only when a cue is due.
    /// Synchronous; used by unit tests. The capture loop calls `submit` so this work cannot stall ffmpeg.
    pub fn observe(&self, data: &[u8], t_mono_us: u64, file: &str, pat: Option<&[u8]>, pmt: Option<&[u8]>, ctx: &dyn Fn() -> WeaveCtx) {
        let Some(m) = &self.state else { return };
        let mut g = m.lock().unwrap_or_else(|e| e.into_inner());
        ingest(&mut g, self.every, data, t_mono_us, file, pat, pmt, ctx, None);
    }

    /// Non-blocking enqueue for the capture thread. `ctx` must already be a snapshot: the caller must not
    /// take the input-log mutex. Never locks `state` and never waits on the worker, UDP or disk. If the
    /// worker is behind, the slice is dropped and counted in `queue_drops`; the recording is unchanged.
    pub fn submit(&self, data: &[u8], t_mono_us: u64, file: &str, pat: Option<&[u8]>, pmt: Option<&[u8]>, ctx: WeaveCtx) {
        let Some(tx) = &self.tx else { return };
        let start = self.count.lock().unwrap_or_else(|e| e.into_inner()).advance(data, file);
        let work = TapWork {
            data: data.to_vec(),
            t_mono_us,
            file: file.to_string(),
            pat: pat.map(|p| p.to_vec()),
            pmt: pmt.map(|p| p.to_vec()),
            ctx,
            start,
        };
        if tx.try_send(work).is_err() {
            self.queue_drops.fetch_add(1, Ordering::Relaxed);
            self.queue_drop_pkts.fetch_add((data.len() / TS) as u64, Ordering::Relaxed);
        }
    }
}

impl FrameCount {
    /// Same frame rule as `ingest` (new part file restarts at 0; one frame per new video PES PTS;
    /// our own ID3 PID is skipped). Returns the position at the start of `data`.
    fn advance(&mut self, data: &[u8], file: &str) -> (i64, Option<u64>) {
        if self.file.as_deref() != Some(file) {
            self.file = Some(file.to_string());
            self.frame = 0;
            self.last_pts = None;
        }
        let start = (self.frame, self.last_pts);
        for pkt in data.chunks_exact(TS) {
            if ring::pid(pkt) == ID3_PID { continue; }
            let Some(pts) = crate::frameclock::video_pes_pts(pkt) else { continue };
            if self.last_pts == Some(pts) { continue; }
            self.last_pts = Some(pts);
            self.frame += 1;
        }
        start
    }
}

#[allow(clippy::too_many_arguments)]
fn ingest(st: &mut State, every: u32, data: &[u8], t_mono_us: u64, file: &str, pat: Option<&[u8]>, pmt: Option<&[u8]>, ctx: &dyn Fn() -> WeaveCtx, start: Option<(i64, Option<u64>)>) {
    if st.file.as_deref() != Some(file) {
        st.file = Some(file.to_string());
        st.frame = 0;
        st.last_pts = None;
        if let Some(p) = pat { if p.len() == TS { st.pmt_pid = ring::pat_pmt_pid(p); push(st, p); } }
        if let Some(p) = pmt { if p.len() == TS { let q = rewrite_or_pass(st, p); push(st, &q); } }
    }
    // Worker path: the submitting thread's count, so slices dropped before this one do not shift `f`.
    // Frames the count moved past without this worker seeing them were in dropped slices; count
    // them and the cue slots they held.
    if let Some((frame, last_pts)) = start {
        if frame > st.frame {
            st.stats.skipped_frames += (frame - st.frame) as u64;
            st.stats.skipped_cue_slots += cue_slots(st.frame, frame, every);
        }
        st.frame = frame;
        st.last_pts = last_pts;
    }
    let mut cached_ctx: Option<WeaveCtx> = None;
    for pkt in data.chunks_exact(TS) {
        let pid = ring::pid(pkt);
        if pid == 0 && pkt[1] & 0x40 != 0 { if let Some(pp) = ring::pat_pmt_pid(pkt) { st.pmt_pid = Some(pp); } }
        if Some(pid) == st.pmt_pid && pkt[1] & 0x40 != 0 {
            let q = rewrite_or_pass(st, pkt);
            push(st, &q);
            continue;
        }
        if pid == ID3_PID { continue; } // never forward a foreign packet on our PID
        push(st, pkt);
        let Some(pts) = crate::frameclock::video_pes_pts(pkt) else { continue };
        if st.last_pts == Some(pts) { continue; }
        st.last_pts = Some(pts);
        let frame = st.frame;
        st.frame += 1;
        st.stats.frames += 1;
        if !st.weave_ok || frame % every as i64 != 0 { continue; }
        let c = cached_ctx.get_or_insert_with(ctx).clone();
        let (line, h) = cue_json(st, &c, file, frame, pts, t_mono_us);
        for p in id3_pes_packets(&mut st.cc, &id3_txxx(CUE_KEY, &line), pts) { push(st, &p); }
        st.prev = h.clone();
        st.n += 1;
        st.stats.cues += 1;
        st.stats.last_hash = Some(h);
        if let Some(f) = st.log.as_mut() { let _ = writeln!(f, "{line}"); }
    }
    // Worker path (`start` set): keep a partial datagram for the next slice so datagrams stay
    // PKT_SIZE; the worker flushes it after PARTIAL_HOLD. Synchronous `observe` sends it now.
    if start.is_none() { flush(st); }
}

/// Cue slots (frame indexes divisible by `every`) in `[from, to)`.
pub fn cue_slots(from: i64, to: i64, every: u32) -> u64 {
    let e = every.max(1) as i64;
    let (a, b) = (from.max(0), to.max(0));
    if b <= a { return 0; }
    ((b + e - 1) / e - (a + e - 1) / e) as u64
}

fn stats_line(st: &State) -> String {
    let s = &st.stats;
    let qd = st.queue_drops.load(Ordering::Relaxed);
    let qp = st.queue_drop_pkts.load(Ordering::Relaxed);
    let slots = s.cues + s.skipped_cue_slots;
    let pct = if slots > 0 { 100.0 * s.skipped_cue_slots as f64 / slots as f64 } else { 0.0 };
    format!("frames {} cues {} skipped_cue_slots {} ({pct:.1}% of {slots}) skipped_frames {} queue_drops {qd} slices / {qp} TS packets \
             send_errors {} datagrams / {} TS packets (would_block {}) datagrams_sent {} pkt_size {} sndbuf {}",
        s.frames, s.cues, s.skipped_cue_slots, s.skipped_frames, s.send_errors, s.send_dropped_packets, s.send_would_block,
        s.datagrams, s.pkt_size, s.sndbuf_bytes.map(|n| n.to_string()).unwrap_or_else(|| "unknown".into()))
}

fn maybe_log_stats(st: &mut State, now: Instant) {
    if now.duration_since(st.last_log) < STATS_LOG_EVERY { return; }
    st.last_log = now;
    let cur = (st.stats.skipped_cue_slots, st.queue_drops.load(Ordering::Relaxed), st.stats.send_errors);
    let d = (cur.0 - st.logged.0.min(cur.0), cur.1 - st.logged.1.min(cur.1), cur.2 - st.logged.2.min(cur.2));
    st.logged = cur;
    let line = stats_line(st);
    if d == (0, 0, 0) {
        log::info!("live tap stats: {line}");
    } else {
        log::warn!("live tap stats: last {}s +{} skipped cue slots, +{} queue drops, +{} send errors; {line}", STATS_LOG_EVERY.as_secs(), d.0, d.1, d.2);
    }
}

fn rewrite_or_pass(st: &mut State, pkt: &[u8]) -> Vec<u8> {
    if !st.weave_ok { return pkt.to_vec(); }
    match add_id3_to_pmt(pkt, ID3_PID) {
        Some(q) => q,
        None => { log::warn!("live tap: PMT does not fit one more stream; video continues without weave cues"); st.weave_ok = false; st.stats.weave = false; pkt.to_vec() }
    }
}

fn push(st: &mut State, pkt: &[u8]) {
    if st.out.is_empty() { st.out_since = Some(Instant::now()); }
    st.out.extend_from_slice(pkt);
    if st.out.len() >= PKT_SIZE { flush(st); }
}

fn flush(st: &mut State) {
    st.out_since = None;
    if st.out.is_empty() { return; }
    match st.sock.send_to(&st.out, st.dest) {
        Ok(_) => st.stats.datagrams += 1,
        Err(e) => {
            st.stats.send_errors += 1;
            st.stats.send_dropped_packets += (st.out.len() / TS) as u64;
            if e.kind() == std::io::ErrorKind::WouldBlock { st.stats.send_would_block += 1; }
        }
    }
    st.out.clear();
}

/// Ask for a `want`-byte UDP send buffer (SO_SNDBUF) and return what the OS then reports.
/// Failure is logged by the caller and never turns the tap off.
#[cfg(unix)]
fn set_send_buffer(sock: &UdpSocket, want: usize) -> Option<usize> {
    use std::os::unix::io::AsRawFd;
    let fd = sock.as_raw_fd();
    let v: libc::c_int = want.min(libc::c_int::MAX as usize) as libc::c_int;
    // SAFETY: fd is a live socket owned by `sock`; the option buffers are local and sized correctly.
    unsafe {
        let _ = libc::setsockopt(fd, libc::SOL_SOCKET, libc::SO_SNDBUF, &v as *const _ as *const libc::c_void,
            std::mem::size_of::<libc::c_int>() as libc::socklen_t);
        let mut got: libc::c_int = 0;
        let mut len = std::mem::size_of::<libc::c_int>() as libc::socklen_t;
        if libc::getsockopt(fd, libc::SOL_SOCKET, libc::SO_SNDBUF, &mut got as *mut _ as *mut libc::c_void, &mut len) != 0 { return None; }
        usize::try_from(got).ok()
    }
}

#[cfg(windows)]
fn set_send_buffer(sock: &UdpSocket, want: usize) -> Option<usize> {
    use std::os::windows::io::AsRawSocket;
    const SOL_SOCKET: i32 = 0xffff;
    const SO_SNDBUF: i32 = 0x1001;
    #[link(name = "ws2_32")]
    extern "system" {
        fn setsockopt(s: usize, level: i32, optname: i32, optval: *const u8, optlen: i32) -> i32;
        fn getsockopt(s: usize, level: i32, optname: i32, optval: *mut u8, optlen: *mut i32) -> i32;
    }
    let s = sock.as_raw_socket() as usize;
    let v: i32 = want.min(i32::MAX as usize) as i32;
    // SAFETY: `s` is a live socket owned by `sock`; the option buffers are local and sized correctly.
    unsafe {
        let _ = setsockopt(s, SOL_SOCKET, SO_SNDBUF, &v as *const i32 as *const u8, 4);
        let mut got: i32 = 0;
        let mut len: i32 = 4;
        if getsockopt(s, SOL_SOCKET, SO_SNDBUF, &mut got as *mut i32 as *mut u8, &mut len) != 0 { return None; }
        usize::try_from(got).ok()
    }
}

#[cfg(not(any(unix, windows)))]
fn set_send_buffer(_sock: &UdpSocket, _want: usize) -> Option<usize> { None }

/// The text the chain hash covers. A watch page recomputes the same string.
pub fn preimage(prev: &str, n: u64, sid: &str, file: &str, f: i64, pts: u64, t: u64, w: u64, seal: &str, lp_seq: Option<u64>) -> String {
    format!("{SCHEMA}|{n}|{sid}|{file}|{f}|{pts}|{t}|{w}|{seal}|{}|{prev}", lp_seq.map(|s| s.to_string()).unwrap_or_default())
}

pub fn chain_hash(pre: &str) -> String {
    let d = Sha256::digest(pre.as_bytes());
    d.iter().take(8).map(|b| format!("{b:02x}")).collect()
}

fn cue_json(st: &State, c: &WeaveCtx, file: &str, frame: i64, pts: u64, t: u64) -> (String, String) {
    let w = c.wall_anchor_ms + t / 1000;
    let pre = preimage(&st.prev, st.n, &st.session, file, frame, pts, t, w, &c.seal, c.last_press.as_ref().map(|p| p.0));
    let h = chain_hash(&pre);
    let lp = c.last_press.as_ref().map(|(s, tm, b)| serde_json::json!({"s": s, "t": tm, "b": b})).unwrap_or(serde_json::Value::Null);
    let v = serde_json::json!({"v": SCHEMA, "n": st.n, "sid": st.session, "file": file, "f": frame, "pts": pts, "t": t, "w": w, "fps": st.fps,
        "every": st.stats.every, "seal": c.seal, "sh": c.seal_hash, "pad": c.pad, "lp": lp, "ph": st.prev, "h": h});
    (v.to_string(), h)
}

/// MPEG-2 CRC-32 (poly 0x04C11DB7, no reflection), as used by PSI sections.
pub fn crc32_mpeg(d: &[u8]) -> u32 {
    let mut crc: u32 = 0xffff_ffff;
    for &b in d {
        crc ^= (b as u32) << 24;
        for _ in 0..8 { crc = if crc & 0x8000_0000 != 0 { (crc << 1) ^ 0x04C1_1DB7 } else { crc << 1 }; }
    }
    crc
}

/// A copy of a single-packet PMT with one timed-ID3 elementary stream added (stream_type 0x15 and the
/// metadata_descriptor ffmpeg's own muxer writes). `None` when the section does not fit one packet or
/// is not a PMT. An existing entry for `id3_pid` is left as is.
pub fn add_id3_to_pmt(pkt: &[u8], id3_pid: u16) -> Option<Vec<u8>> {
    if pkt.len() != TS || pkt[0] != 0x47 || pkt[1] & 0x40 == 0 || pkt[3] & 0x10 == 0 { return None; }
    let mut i = 4usize;
    if pkt[3] & 0x20 != 0 { i += 1 + pkt[4] as usize; }
    if i >= TS { return None; }
    i += 1 + pkt[i] as usize; // pointer field
    if i + 12 > TS || pkt[i] != 0x02 { return None; }
    let sec_len = (((pkt[i + 1] & 0x0f) as usize) << 8) | pkt[i + 2] as usize;
    if i + 3 + sec_len > TS || sec_len < 13 { return None; }
    let mut sec = pkt[i..i + 3 + sec_len - 4].to_vec();
    let crc_old = u32::from_be_bytes(pkt[i + 3 + sec_len - 4..i + 3 + sec_len].try_into().ok()?);
    if crc32_mpeg(&sec) != crc_old { return None; }
    // already present?
    let pil = (((sec[10] & 0x0f) as usize) << 8) | sec[11] as usize;
    let mut j = 12 + pil;
    while j + 5 <= sec.len() {
        let epid = (((sec[j + 1] & 0x1f) as u16) << 8) | sec[j + 2] as u16;
        let eil = (((sec[j + 3] & 0x0f) as usize) << 8) | sec[j + 4] as usize;
        if epid == id3_pid { return Some(pkt.to_vec()); }
        j += 5 + eil;
    }
    let mut desc = vec![0x26u8, 13, 0xff, 0xff];
    desc.extend_from_slice(b"ID3 ");
    desc.push(0xff);
    desc.extend_from_slice(b"ID3 ");
    desc.extend_from_slice(&[0x00, 0x0f]);
    sec.extend_from_slice(&[0x15, 0xe0 | ((id3_pid >> 8) as u8 & 0x1f), (id3_pid & 0xff) as u8, 0xf0, desc.len() as u8]);
    sec.extend_from_slice(&desc);
    let new_len = sec.len() - 3 + 4;
    if new_len > 1021 { return None; }
    sec[1] = (sec[1] & 0xf0) | ((new_len >> 8) as u8 & 0x0f);
    sec[2] = (new_len & 0xff) as u8;
    let crc = crc32_mpeg(&sec);
    sec.extend_from_slice(&crc.to_be_bytes());
    let mut out = pkt[..4].to_vec();
    out[3] = (out[3] & 0xcf) | 0x10; // payload only, keep the continuity counter
    out.push(0x00); // pointer field
    out.extend_from_slice(&sec);
    if out.len() > TS { return None; }
    out.resize(TS, 0xff);
    Some(out)
}

fn syncsafe(n: usize) -> [u8; 4] { [((n >> 21) & 0x7f) as u8, ((n >> 14) & 0x7f) as u8, ((n >> 7) & 0x7f) as u8, (n & 0x7f) as u8] }

/// ID3v2.4 tag with one UTF-8 TXXX frame (`desc` = key, `value` = JSON).
pub fn id3_txxx(desc: &str, value: &str) -> Vec<u8> {
    let mut body = vec![0x03u8];
    body.extend_from_slice(desc.as_bytes());
    body.push(0);
    body.extend_from_slice(value.as_bytes());
    let mut fr = b"TXXX".to_vec();
    fr.extend_from_slice(&syncsafe(body.len()));
    fr.extend_from_slice(&[0, 0]);
    fr.extend_from_slice(&body);
    let mut tag = b"ID3\x04\x00\x00".to_vec();
    tag.extend_from_slice(&syncsafe(fr.len()));
    tag.extend_from_slice(&fr);
    tag
}

/// One PES (stream_id 0xBD, PTS only) split into TS packets on `ID3_PID`. The last packet is padded
/// with an adaptation field so every packet is 188 bytes.
pub fn id3_pes_packets(cc: &mut u8, payload: &[u8], pts: u64) -> Vec<Vec<u8>> {
    let p = [0x21 | (((pts >> 29) & 0x0e) as u8), ((pts >> 22) & 0xff) as u8, 0x01 | (((pts >> 14) & 0xfe) as u8), ((pts >> 7) & 0xff) as u8, 0x01 | (((pts << 1) & 0xfe) as u8)];
    let plen = 3 + 5 + payload.len();
    let mut pes = vec![0, 0, 1, 0xbd, ((plen >> 8) & 0xff) as u8, (plen & 0xff) as u8, 0x84, 0x80, 0x05];
    pes.extend_from_slice(&p);
    pes.extend_from_slice(payload);
    let mut out = Vec::new();
    let mut first = true;
    let mut rest = &pes[..];
    while !rest.is_empty() {
        let take = rest.len().min(184);
        let (chunk, tail) = rest.split_at(take);
        rest = tail;
        let mut pkt = vec![0x47, (if first { 0x40 } else { 0 }) | ((ID3_PID >> 8) as u8 & 0x1f), (ID3_PID & 0xff) as u8, 0x10 | (*cc & 0x0f)];
        *cc = (*cc + 1) & 0x0f;
        if chunk.len() < 184 {
            let stuff = 184 - chunk.len();
            pkt[3] |= 0x20;
            if stuff == 1 { pkt.push(0); } else { pkt.push((stuff - 1) as u8); pkt.push(0); pkt.extend(std::iter::repeat(0xff).take(stuff - 2)); }
        }
        pkt.extend_from_slice(chunk);
        debug_assert_eq!(pkt.len(), TS);
        out.push(pkt);
        first = false;
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolve_is_off_by_default_practice_only_and_loopback_only() {
        assert_eq!(resolve(None, None, true, None), Ok(None));
        assert_eq!(resolve(None, Some(""), true, None), Ok(None));
        assert_eq!(resolve(None, Some("off"), true, Some("practice")), Ok(None));
        assert!(resolve(Some("udp://127.0.0.1:1937"), None, false, None).unwrap_err().contains("practice lane only"));
        assert!(resolve(Some("udp://127.0.0.1:1937"), None, false, Some("match")).is_err());
        assert_eq!(resolve(Some("udp://127.0.0.1:1937"), None, true, None).unwrap().unwrap().port(), 1937);
        assert_eq!(resolve(None, Some("udp://localhost:1937?pkt_size=1316"), false, Some("PRACTICE")).unwrap().unwrap().port(), 1937);
        assert!(resolve(Some("udp://192.168.1.5:1937"), None, true, None).unwrap_err().contains("loopback"));
        assert!(resolve(Some("udp://0.0.0.0:1937"), None, true, None).is_err());
        assert!(resolve(Some("rtmp://127.0.0.1:1935/live/x"), None, true, None).unwrap_err().contains("udp://"));
        assert!(resolve(Some("udp://127.0.0.1"), None, true, None).is_err());
        assert_eq!(every_from_env(None), DEFAULT_EVERY);
        assert_eq!(every_from_env(Some("1")), 1);
        assert_eq!(every_from_env(Some("0")), DEFAULT_EVERY);
    }

    fn pmt_packet() -> Vec<u8> {
        // ffmpeg-style PMT on PID 0x1000: program 1, PCR 0x100, h264 0x100, aac 0x101
        let mut sec = vec![0x02, 0xb0, 0, 0x00, 0x01, 0xc1, 0x00, 0x00, 0xe1, 0x00, 0xf0, 0x00,
            0x1b, 0xe1, 0x00, 0xf0, 0x00, 0x0f, 0xe1, 0x01, 0xf0, 0x00];
        let l = sec.len() - 3 + 4; sec[1] = 0xb0 | ((l >> 8) as u8); sec[2] = l as u8;
        let crc = crc32_mpeg(&sec); sec.extend_from_slice(&crc.to_be_bytes());
        let mut p = vec![0x47, 0x50, 0x00, 0x13, 0x00];
        p.extend_from_slice(&sec); p.resize(TS, 0xff); p
    }

    #[test]
    fn pmt_rewrite_adds_timed_id3_and_a_valid_crc() {
        let p = pmt_packet();
        let q = add_id3_to_pmt(&p, ID3_PID).unwrap();
        assert_eq!(q.len(), TS);
        assert_eq!(&q[..4], &p[..4]);
        let sec_len = (((q[6] & 0x0f) as usize) << 8) | q[7] as usize;
        let sec = &q[5..5 + 3 + sec_len];
        assert_eq!(crc32_mpeg(&sec[..sec.len() - 4]).to_be_bytes(), sec[sec.len() - 4..]);
        assert!(sec.windows(3).any(|w| w == [0x15, 0xe1, 0xe0]));
        // idempotent
        assert_eq!(add_id3_to_pmt(&q, ID3_PID).unwrap(), q);
        // not a PMT / bad CRC
        let mut bad = p.clone(); bad[20] ^= 1;
        assert!(add_id3_to_pmt(&bad, ID3_PID).is_none());
    }

    #[test]
    fn id3_pes_is_whole_packets_with_pts() {
        let mut cc = 0u8;
        let tag = id3_txxx(CUE_KEY, &"x".repeat(300));
        let pk = id3_pes_packets(&mut cc, &tag, 123_456_789);
        assert!(pk.len() >= 2 && pk.iter().all(|p| p.len() == TS && p[0] == 0x47 && ring::pid(p) == ID3_PID));
        assert!(pk[0][1] & 0x40 != 0 && pk[1][1] & 0x40 == 0);
        assert_eq!(cc as usize, pk.len());
        assert_eq!(&pk[0][4..8], &[0, 0, 1, 0xbd]);
        let b = &pk[0][13..18];
        let pts = (((b[0] as u64) & 0x0e) << 29) | ((b[1] as u64) << 22) | (((b[2] as u64) & 0xfe) << 14) | ((b[3] as u64) << 7) | (((b[4] as u64) & 0xfe) >> 1);
        assert_eq!(pts, 123_456_789);
        // short payload: one packet with stuffing
        let one = id3_pes_packets(&mut cc, b"ID3", 0);
        assert_eq!(one.len(), 1);
        assert!(one[0][3] & 0x20 != 0);
    }

    #[test]
    fn chain_hash_is_stable_and_covers_every_field() {
        let a = preimage("0000000000000000", 0, "1700", "video.mp4", 0, 900, 1000, 1701, "locked", Some(3));
        assert_eq!(a, "qorweave/v0|0|1700|video.mp4|0|900|1000|1701|locked|3|0000000000000000");
        let h = chain_hash(&a);
        assert_eq!(h.len(), 16);
        assert_ne!(h, chain_hash(&preimage("0000000000000000", 0, "1700", "video.mp4", 1, 900, 1000, 1701, "locked", Some(3))));
    }

    #[test]
    fn off_tap_is_a_no_op_and_on_tap_sends_cues_on_loopback() {
        let off = LiveTap::off();
        assert!(!off.enabled() && off.stats().is_none());
        off.observe(&[0x47; TS], 0, "video.mp4", None, None, &|| WeaveCtx::default());

        let rx = UdpSocket::bind("127.0.0.1:0").unwrap();
        rx.set_read_timeout(Some(std::time::Duration::from_secs(2))).unwrap();
        let dir = std::env::temp_dir().join(format!("qorcap-livetap-{}", std::process::id()));
        let tap = LiveTap::open(Some(rx.local_addr().unwrap()), 2, "1700", 60, Some(&dir));
        // one video PES start packet per frame, PTS 3000 apart
        let mut data = Vec::new();
        for k in 0..4u64 {
            let pts = 900 + 1500 * k;
            let mut p = vec![0x47, 0x41, 0x00, 0x10, 0, 0, 1, 0xe0, 0, 0, 0x80, 0x80, 0x05];
            p.extend_from_slice(&[0x21 | (((pts >> 29) & 0x0e) as u8), ((pts >> 22) & 0xff) as u8, 0x01 | (((pts >> 14) & 0xfe) as u8), ((pts >> 7) & 0xff) as u8, 0x01 | (((pts << 1) & 0xfe) as u8)]);
            p.resize(TS, 0xff);
            data.extend_from_slice(&p);
        }
        let ctx = || WeaveCtx { wall_anchor_ms: 1_700_000_000_000, pad: "connected".into(), seal: "locked".into(), seal_hash: Some("abc".into()), last_press: Some((7, 1_700_000_000_400, "cross".into())) };
        tap.observe(&data, 500_000, "video.mp4", None, Some(&pmt_packet()), &ctx);
        let mut got = Vec::new();
        let mut buf = [0u8; 65536];
        while let Ok(n) = rx.recv(&mut buf) { got.extend_from_slice(&buf[..n]); if got.len() >= TS * 7 { break; } }
        let s = tap.stats().unwrap();
        assert_eq!((s.frames, s.cues), (4, 2));
        assert!(got.len() % TS == 0);
        let pkts: Vec<&[u8]> = got.chunks_exact(TS).collect();
        assert!(pkts.iter().any(|p| ring::pid(p) == 0x1000 && p.windows(3).any(|w| w == [0x15, 0xe1, 0xe0])), "rewritten PMT first");
        assert_eq!(pkts.iter().filter(|p| ring::pid(p) == ID3_PID && p[1] & 0x40 != 0).count(), 2);
        let log = std::fs::read_to_string(dir.join(FILE_NAME)).unwrap();
        let cues: Vec<serde_json::Value> = log.lines().skip(1).map(|l| serde_json::from_str(l).unwrap()).collect();
        assert_eq!(cues.len(), 2);
        assert_eq!(cues[0]["f"], 0); assert_eq!(cues[1]["f"], 2);
        assert_eq!(cues[1]["ph"], cues[0]["h"]);
        assert_eq!(cues[0]["w"], 1_700_000_000_500u64);
        assert_eq!(cues[0]["lp"]["s"], 7);
        let pre = preimage(cues[1]["ph"].as_str().unwrap(), 1, "1700", "video.mp4", 2, 3900, 500_000, 1_700_000_000_500, "locked", Some(7));
        assert_eq!(cues[1]["h"].as_str().unwrap(), chain_hash(&pre));
        let _ = std::fs::remove_dir_all(&dir);
    }

    fn frame_pkt(k: u64) -> [u8; TS] { crate::frameclock::video_ts_packet(0x100, 900 + 1500 * k) }

    fn wait_for(mut done: impl FnMut() -> bool) {
        let end = std::time::Instant::now() + std::time::Duration::from_secs(5);
        while !done() {
            assert!(std::time::Instant::now() < end, "tap worker did not catch up");
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
    }

    #[test]
    fn submit_never_waits_on_a_busy_worker_and_counts_queue_drops() {
        let rx = UdpSocket::bind("127.0.0.1:0").unwrap();
        let dir = std::env::temp_dir().join(format!("qorcap-livetap-q-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let tap = std::sync::Arc::new(LiveTap::open(Some(rx.local_addr().unwrap()), 1, "1700", 60, Some(&dir)));
        const N: u64 = 40;
        // Stand in for a worker stuck on UDP / disk: hold its state lock.
        let busy = tap.state.as_ref().unwrap().lock().unwrap();
        let (done_tx, done_rx) = mpsc::channel();
        let t2 = tap.clone();
        std::thread::spawn(move || {
            for k in 0..N { t2.submit(&frame_pkt(k), 1_000 + k, "video.mp4", None, None, WeaveCtx::default()); }
            let _ = done_tx.send(());
        });
        done_rx.recv_timeout(std::time::Duration::from_secs(2)).expect("submit blocked on the tap worker");
        let drops = tap.queue_drops.load(Ordering::Relaxed);
        assert!(drops >= N - TAP_QUEUE as u64 - 1, "queue holds at most {TAP_QUEUE} (+1 in the worker); dropped {drops}");
        drop(busy);
        wait_for(|| { let s = tap.stats().unwrap(); s.frames + s.queue_drops == N });
        // Slices after the gap: their cues must still carry the recording's frame index.
        for k in N..N + 5 {
            tap.submit(&frame_pkt(k), 1_000 + k, "video.mp4", None, None, WeaveCtx::default());
            wait_for(|| { let s = tap.stats().unwrap(); s.frames + s.queue_drops == k + 1 });
        }
        let s = tap.stats().unwrap();
        assert_eq!(s.queue_drops, drops);
        assert!(drops > 0);
        // One frame (one TS packet) per slice and a cue every frame: each dropped slice is one
        // skipped frame, one skipped cue slot and one dropped TS packet.
        assert_eq!((s.skipped_frames, s.skipped_cue_slots, s.queue_dropped_packets), (drops, drops, drops));
        assert_eq!(s.cues + s.skipped_cue_slots, N + 5);
        // Every cue that was cut still names its true frame: dropped slices did not shift `f`.
        let log = std::fs::read_to_string(dir.join(FILE_NAME)).unwrap();
        let cues: Vec<serde_json::Value> = log.lines().skip(1).map(|l| serde_json::from_str(l).unwrap()).collect();
        assert_eq!(cues.len() as u64, N + 5 - drops);
        assert_eq!(cues.last().unwrap()["f"], N + 4);
        for c in &cues {
            let k = (c["pts"].as_u64().unwrap() - 900) / 1500;
            assert_eq!(c["f"].as_u64().unwrap(), k, "{c}");
            assert_eq!(c["t"].as_u64().unwrap(), 1_000 + k);
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn submit_matches_observe_when_nothing_is_dropped() {
        let rx = UdpSocket::bind("127.0.0.1:0").unwrap();
        let base = std::env::temp_dir().join(format!("qorcap-livetap-eq-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        let ctx = WeaveCtx { wall_anchor_ms: 1_700_000_000_000, pad: "connected".into(), seal: "off".into(), seal_hash: None, last_press: Some((1, 1_700_000_000_100, "CROSS".into())) };
        let mut logs = vec![];
        for (name, async_) in [("sync", false), ("async", true)] {
            let dir = base.join(name);
            let tap = LiveTap::open(Some(rx.local_addr().unwrap()), 3, "1700", 60, Some(&dir));
            for k in 0..20u64 {
                let file = if k < 12 { "video.mp4" } else { "video_part2.mp4" };
                let data: Vec<u8> = [frame_pkt(k), frame_pkt(k)].concat(); // repeated PTS counts once
                if async_ {
                    tap.submit(&data, 10 * k, file, None, Some(&pmt_packet()), ctx.clone());
                    wait_for(|| tap.stats().unwrap().frames == k + 1);
                } else {
                    tap.observe(&data, 10 * k, file, None, Some(&pmt_packet()), &|| ctx.clone());
                }
            }
            assert_eq!(tap.stats().unwrap().queue_drops, 0);
            logs.push(std::fs::read_to_string(dir.join(FILE_NAME)).unwrap());
        }
        assert_eq!(logs[0], logs[1]);
        assert!(logs[0].lines().count() > 5);
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn cue_slots_counts_multiples_of_every_in_the_gap() {
        assert_eq!(cue_slots(0, 15, 15), 1);  // frame 0
        assert_eq!(cue_slots(1, 15, 15), 0);
        assert_eq!(cue_slots(1, 16, 15), 1);  // frame 15
        assert_eq!(cue_slots(16, 46, 15), 2); // 30, 45
        assert_eq!(cue_slots(10, 10, 15), 0);
        assert_eq!(cue_slots(12, 3, 15), 0);
        assert_eq!(cue_slots(0, 5, 1), 5);
    }

    #[test]
    fn worker_sends_full_1316_byte_datagrams_and_reports_the_send_buffer() {
        let rx = UdpSocket::bind("127.0.0.1:0").unwrap();
        rx.set_read_timeout(Some(std::time::Duration::from_secs(2))).unwrap();
        let dir = std::env::temp_dir().join(format!("qorcap-livetap-pk-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let tap = LiveTap::open(Some(rx.local_addr().unwrap()), 1000, "1700", 60, Some(&dir));
        let s0 = tap.stats().unwrap();
        assert_eq!(s0.pkt_size, 1316);
        assert!(s0.sndbuf_bytes.map(|n| n > 0).unwrap_or(false), "{:?}", s0.sndbuf_bytes);
        // 3 slices of 5 packets = 15 packets, plus the frame-0 cue (2 ID3 packets) = 17:
        // two full datagrams of 7, and 3 packets held.
        for k in 0..3u64 {
            let data: Vec<u8> = (0..5).flat_map(|i| frame_pkt(5 * k + i)).collect();
            tap.submit(&data, k, "video.mp4", None, None, WeaveCtx::default());
        }
        let mut sizes = vec![];
        let mut buf = [0u8; 65536];
        while sizes.iter().sum::<usize>() < 17 * TS { let n = rx.recv(&mut buf).unwrap(); sizes.push(n); }
        assert_eq!(&sizes[..2], &[PKT_SIZE, PKT_SIZE], "{sizes:?}");
        // The held tail goes out short only after the hold time with no new slice.
        assert_eq!(sizes.len(), 3, "{sizes:?}");
        assert_eq!(sizes[2], 3 * TS);
        let s = tap.stats().unwrap();
        assert_eq!((s.datagrams, s.send_errors, s.queue_drops), (3, 0, 0));
        tap.log_summary("test");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
