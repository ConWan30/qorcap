//! The hand logger thread. It owns the `HandTimeline` outright: reports come from its own `ReportSource`,
//! frame arrivals come over a bounded channel with `try_send` (the capture thread never waits on the
//! logger's disk writes or locks), and the newest sealed `(seq, head)` is published through a small
//! snapshot mutex that is never held across I/O. Drops from a full queue are counted into the timeline,
//! the summary, and `GET /status`.

use super::device::DeviceInfo;
use super::source::{OpenOutcome, ReadOutcome};
use super::timeline::{Config, Finish, HandTimeline};
use crate::frameclock::FrameStamp;
use crate::hand::LiveStatus;
use log::{info, warn};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{sync_channel, Receiver, RecvTimeoutError, SyncSender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

pub const READ_TIMEOUT_MS: u32 = 100;
pub const MISSING_RETRY_MS: u64 = 1000;
pub const UNPLUGGED_RETRY_MS: u64 = 500;
/// Bounded capture-to-logger queue, in outstanding frame batches (one `frames()` call each).
pub const FRAME_QUEUE_CAP: usize = 32;

enum Msg { Frames(Vec<FrameStamp>), Stop }

/// Engine-side handle. Every method is cheap and never waits on the logger's disk writes.
pub struct HandHandle {
    tx: SyncSender<Msg>,
    stop: Arc<AtomicBool>,
    drops: Arc<AtomicU64>,
    snap: Arc<Mutex<Option<(u64, String)>>>,
    live: Arc<Mutex<LiveStatus>>,
    join: Mutex<Option<std::thread::JoinHandle<Result<Finish, String>>>>,
    done: Mutex<Option<Result<Finish, String>>>,
}

impl HandHandle {
    /// Frames from one capture-pipe read. `try_send` only: never blocks the capture thread.
    /// A full queue or a stopped logger increments the drop count (one per stamp).
    pub fn frames(&self, stamps: Vec<FrameStamp>) {
        if stamps.is_empty() { return; }
        let n = stamps.len() as u64;
        if self.stop.load(Ordering::Relaxed) {
            self.drops.fetch_add(n, Ordering::Relaxed);
            return;
        }
        if self.tx.try_send(Msg::Frames(stamps)).is_err() {
            self.drops.fetch_add(n, Ordering::Relaxed);
        }
    }
    /// Newest durable hand chunk `(seq, head)` for a segment checkpoint.
    pub fn binding(&self) -> Option<(u64, String)> { self.snap.lock().unwrap_or_else(|e| e.into_inner()).clone() }
    /// Live logger state for `GET /status` (never waits on disk). `frame_drops` is the atomic total.
    pub fn live(&self) -> LiveStatus {
        let mut st = self.live.lock().unwrap_or_else(|e| e.into_inner()).clone();
        st.frame_drops = self.drops.load(Ordering::Relaxed);
        st
    }
    /// Total frame arrivals dropped because the queue was full or the logger had stopped.
    pub fn frame_drops(&self) -> u64 { self.drops.load(Ordering::Relaxed) }
    /// Stop the logger, seal the trailing window and return the file digest. Bounded wait; idempotent.
    pub fn finish(&self, wait: Duration) -> Result<Finish, String> {
        if let Some(r) = self.done.lock().unwrap_or_else(|e| e.into_inner()).clone() { return r; }
        self.stop.store(true, Ordering::SeqCst);
        let _ = self.tx.try_send(Msg::Stop);
        let Some(j) = self.join.lock().unwrap_or_else(|e| e.into_inner()).take() else { return Err("hand logger already joined".into()) };
        let end = Instant::now() + wait;
        while !j.is_finished() && Instant::now() < end { std::thread::sleep(Duration::from_millis(10)); }
        if !j.is_finished() { return Err(format!("hand logger did not stop within {} ms", wait.as_millis())); }
        let r = j.join().unwrap_or_else(|_| Err("hand logger thread panicked".into()));
        *self.done.lock().unwrap_or_else(|e| e.into_inner()) = Some(r.clone());
        r
    }
}

/// Start the logger. `open` enumerates and opens one pad (listen-only); `qpc_now` returns the QPC tick count
/// (None off Windows). The timeline file is created before this returns, so a create error is reported here.
pub fn spawn<O, Q>(cfg: Config, clock: Instant, open: O, qpc_now: Q) -> Result<HandHandle, String>
where O: FnMut() -> OpenOutcome + Send + 'static, Q: Fn() -> Option<u64> + Send + 'static {
    spawn_with_queue(cfg, clock, open, qpc_now, FRAME_QUEUE_CAP)
}

/// As `spawn`, with an explicit queue cap (tests use a small cap to force drops).
pub fn spawn_with_queue<O, Q>(cfg: Config, clock: Instant, mut open: O, qpc_now: Q, cap: usize) -> Result<HandHandle, String>
where O: FnMut() -> OpenOutcome + Send + 'static, Q: Fn() -> Option<u64> + Send + 'static {
    let us = move || clock.elapsed().as_micros() as u64;
    // Probe once so the header can name the device found at start (or UNKNOWN).
    let first = open();
    let mut cfg = cfg;
    let mut first_src = None;
    let mut first_status: Option<(&'static str, String)> = None;
    match first {
        OpenOutcome::Open(src, info) => { cfg.initial_device = Some(info.clone()); first_src = Some((src, info)); }
        OpenOutcome::Missing(e) => first_status = Some(("missing", e)),
        OpenOutcome::Rejected { state, detail } => first_status = Some((state, detail)),
    }
    let mut tl = HandTimeline::open(cfg)?;
    if let Some((s, d)) = &first_status { tl.on_status(us(), s, d); }
    log_state(&tl.live());
    let live = Arc::new(Mutex::new(tl.live()));
    let live2 = Arc::clone(&live);
    let cap = cap.max(1);
    let (tx, rx) = sync_channel::<Msg>(cap);
    let stop = Arc::new(AtomicBool::new(false));
    let stop2 = Arc::clone(&stop);
    let drops = Arc::new(AtomicU64::new(0));
    let drops2 = Arc::clone(&drops);
    let snap = Arc::new(Mutex::new(None));
    let snap2 = Arc::clone(&snap);
    let join = std::thread::Builder::new().name("qorcap-hand".into()).spawn(move || {
        #[cfg(windows)]
        super::win::raise_thread_priority();
        run(&mut tl, rx, &snap2, &live2, &stop2, &drops2, us, &mut open, qpc_now, first_src.map(|(s, _)| s), first_status.map(|(s, _)| s))
    }).map_err(|e| format!("hand logger thread: {e}"))?;
    Ok(HandHandle { tx, stop, drops, snap, live, join: Mutex::new(Some(join)), done: Mutex::new(None) })
}

fn log_state(st: &LiveStatus) {
    let extra = st.detail.as_deref().map(|d| format!(" ({d})")).unwrap_or_default();
    let msg = format!("HAND TIMELINE: {}{extra}", st.state);
    if matches!(st.state.as_str(), "connected") { info!("{msg}"); } else { warn!("{msg}"); }
}

fn publish_live(tl: &HandTimeline, live: &Mutex<LiveStatus>, drops: &AtomicU64) {
    let mut st = tl.live();
    st.frame_drops = drops.load(Ordering::Relaxed);
    let mut g = live.lock().unwrap_or_else(|e| e.into_inner());
    if *g != st {
        if g.state != st.state || g.detail != st.detail { log_state(&st); }
        *g = st;
    }
}

fn ingest_drops(tl: &mut HandTimeline, drops: &AtomicU64, sampled: &mut u64, t: u64) {
    let now = drops.load(Ordering::Relaxed);
    if now > *sampled {
        tl.on_frame_drops(t, now - *sampled);
        *sampled = now;
    }
}

fn run<O, Q, U>(tl: &mut HandTimeline, rx: Receiver<Msg>, snap: &Mutex<Option<(u64, String)>>, live: &Mutex<LiveStatus>,
    stop: &AtomicBool, drops: &AtomicU64, us: U, open: &mut O, qpc_now: Q,
    mut src: Option<Box<dyn super::source::ReportSource>>, first_state: Option<&'static str>) -> Result<Finish, String>
where O: FnMut() -> OpenOutcome, Q: Fn() -> Option<u64>, U: Fn() -> u64 {
    let mut buf = [0u8; 128];
    let mut last_state: Option<String> = first_state.map(str::to_string);
    let mut sampled = 0u64;
    let publish = |tl: &HandTimeline| {
        let b = tl.binding();
        let mut g = snap.lock().unwrap_or_else(|e| e.into_inner());
        if *g != b { *g = b; }
        publish_live(tl, live, drops);
    };
    // Drain frames; true = stop asked (flag or Stop message, or the sender went away).
    let drain = |tl: &mut HandTimeline, rx: &Receiver<Msg>, wait: Duration, sampled: &mut u64| -> bool {
        ingest_drops(tl, drops, sampled, us());
        let stopping = stop.load(Ordering::Relaxed);
        let mut first = true;
        loop {
            let m = if first && !wait.is_zero() && !stopping {
                first = false;
                match rx.recv_timeout(wait) {
                    Ok(m) => m,
                    Err(RecvTimeoutError::Timeout) => return stop.load(Ordering::Relaxed),
                    Err(RecvTimeoutError::Disconnected) => return true,
                }
            } else {
                match rx.try_recv() {
                    Ok(m) => m,
                    Err(std::sync::mpsc::TryRecvError::Empty) => return stopping || stop.load(Ordering::Relaxed),
                    Err(_) => return true,
                }
            };
            match m { Msg::Frames(f) => tl.on_frames(&f), Msg::Stop => { stop.store(true, Ordering::Relaxed); return true; } }
        }
    };
    'outer: loop {
        match src.as_mut() {
            None => match open() {
                OpenOutcome::Open(s, info) => { tl.on_connected(us(), info); src = Some(s); last_state = Some("connected".into()); ingest_drops(tl, drops, &mut sampled, us()); publish(tl); }
                other => {
                    let (state, detail) = match other { OpenOutcome::Missing(e) => ("missing", e), OpenOutcome::Rejected { state, detail } => (state, detail), OpenOutcome::Open(..) => unreachable!() };
                    if last_state.as_deref() != Some(state) { tl.on_status(us(), state, &detail); last_state = Some(state.into()); ingest_drops(tl, drops, &mut sampled, us()); publish(tl); }
                    let end = Instant::now() + Duration::from_millis(MISSING_RETRY_MS);
                    while Instant::now() < end {
                        if drain(tl, &rx, Duration::from_millis(50), &mut sampled) { break 'outer; }
                        tl.tick(us()); publish(tl);
                    }
                }
            },
            Some(s) => match s.read(&mut buf, READ_TIMEOUT_MS) {
                ReadOutcome::Report(n) => {
                    // Stamp first, before parsing or any other work. When QPC origin is configured and qpc_now()
                    // returns a sample, derive t_us from that single atomic QPC tick read so userspace preemption
                    // between two separate clock calls cannot introduce positive skew spikes.
                    let ticks = qpc_now();
                    let t = ticks.and_then(|tk| tl.qpc().and_then(|q| q.ticks_to_t_us(tk))).unwrap_or_else(&us);
                    if drain(tl, &rx, Duration::ZERO, &mut sampled) { tl.on_report(t, ticks, &buf[..n]); break 'outer; }
                    tl.on_report(t, ticks, &buf[..n]);
                }
                ReadOutcome::Timeout => { let t = us(); if drain(tl, &rx, Duration::ZERO, &mut sampled) { break 'outer; } tl.on_timeout(t); ingest_drops(tl, drops, &mut sampled, t); publish(tl); }
                ReadOutcome::Disconnected(e) => {
                    let t = us();
                    tl.on_read_error(t, &e);
                    last_state = Some("unplugged".into());
                    ingest_drops(tl, drops, &mut sampled, t);
                    publish(tl);
                    src = None;
                    let end = Instant::now() + Duration::from_millis(UNPLUGGED_RETRY_MS);
                    while Instant::now() < end { if drain(tl, &rx, Duration::from_millis(50), &mut sampled) { break 'outer; } }
                }
            },
        }
        if drain(tl, &rx, Duration::ZERO, &mut sampled) { break; }
        tl.tick(us());
        ingest_drops(tl, drops, &mut sampled, us());
        publish(tl);
    }
    drop(src);
    // Frames already queued before Stop are kept; then record any last drops.
    while let Ok(Msg::Frames(f)) = rx.try_recv() { tl.on_frames(&f); }
    ingest_drops(tl, drops, &mut sampled, us());
    let r = tl.finish(us());
    publish(tl);
    r
}

/// The real-device opener for this platform.
pub fn platform_open() -> OpenOutcome {
    #[cfg(windows)]
    { super::win::open_pad() }
    #[cfg(not(windows))]
    { OpenOutcome::Missing("the hand logger's USB reader is Windows-only".into()) }
}

/// QPC ticks now (Windows), None elsewhere.
pub fn platform_qpc_now() -> Option<u64> {
    #[cfg(windows)]
    { super::win::qpc_now() }
    #[cfg(not(windows))]
    { None }
}

/// QPC frequency and the tick count paired with an engine-clock `t_us` taken at the same moment.
pub fn platform_qpc_origin(clock: Instant) -> Option<super::timeline::Qpc> {
    #[cfg(windows)]
    {
        let freq = super::win::qpc_freq()?;
        let ticks = super::win::qpc_now()?;
        let t = clock.elapsed().as_micros() as u64;
        Some(super::timeline::Qpc { freq_hz: freq, origin_ticks: ticks, origin_t_us: t })
    }
    #[cfg(not(windows))]
    { let _ = clock; None }
}

/// Convenience for callers that already hold device facts (tests).
pub fn opened(src: Box<dyn super::source::ReportSource>, info: DeviceInfo) -> OpenOutcome { OpenOutcome::Open(src, info) }
