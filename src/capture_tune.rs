//! Opt-in capture-path experiments. Every knob defaults off, and with them off the ffmpeg
//! command is unchanged. Nothing here touches scoring, receipts, or signing.
//!
//! The 2026-10-19 card teardown is treated as a set of hypotheses. These helpers are the
//! pieces that can be checked without the card: argument policy, USB-link text, real-time
//! buffer warnings, and black-frame detection.

use crate::cli::RecordArgs;
use std::collections::VecDeque;
use std::path::{Path, PathBuf};
use std::process::{Child, Stdio};
#[cfg(any(windows, test))]
use std::process::Command;
use std::time::{Duration, Instant};

/// YUY2 1280x720@60 is 1280*720*2 bytes/frame * 60 * 8 bits. About 885 Mbit/s.
pub const YUY2_720P60_MBIT: f64 = 1280.0 * 720.0 * 2.0 * 60.0 * 8.0 / 1_000_000.0;
/// Native card audio is 48 kHz, 16-bit, stereo (UAC1). ffmpeg's default dshow rtbuf is ~3,041,280 bytes.
pub const AUDIO_BYTES_PER_SEC: f64 = 48_000.0 * 2.0 * 2.0;

pub const TAG_BUFFER: &str = "BUFFER LAG:";
pub const TAG_NOSIGNAL: &str = "NO HDMI PICTURE:";
pub const TAG_USB: &str = "USB LINK:";

/// How long to wait for a killed ffmpeg before the supervisor gives up on it.
pub const KILL_BOUND: Duration = Duration::from_secs(3);
/// After the card reappears, wait this long so Windows can finish the devnode and HDMI can lock.
pub const ARRIVAL_SETTLE: Duration = Duration::from_millis(1500);
pub const BUFFER_RESTART_COOLDOWN_MS: u64 = 30_000;

pub fn apply_capture_profile(args: &mut RecordArgs) {
    // Restart implies watching, otherwise the flag would do nothing observable.
    if args.buffer_restart {
        args.buffer_watch = true;
    }
    if args.yuy2_720p60 {
        args.res = (1280, 720);
        args.fps = 60;
        args.input_format = "yuyv422".into();
    }
}

pub fn any_tune(args: &RecordArgs) -> bool {
    args.yuy2_720p60
        || args.check_usb_link
        || args.reader_snapshot != "q3"
        || args.rtbufsize != "512M"
        || args.audio_rtbufsize.is_some()
        || args.short_probe
        || args.buffer_watch
        || args.buffer_restart
        || args.no_signal
        || args.replug_resilient
}

/// `Some` when a re-plug settle delay should run before the next open.
pub fn arrival_settle(was_absent: bool, resilient: bool) -> Option<Duration> {
    if was_absent && resilient { Some(ARRIVAL_SETTLE) } else { None }
}

pub fn restart_due(persistent: bool, enabled: bool, now_ms: u64, last_ms: u64) -> bool {
    persistent && enabled && (last_ms == 0 || now_ms.saturating_sub(last_ms) > BUFFER_RESTART_COOLDOWN_MS)
}

/// Insert or replace one tagged clause in a status warning. Other clauses (audio, disk) stay.
pub fn upsert_tagged(current: &mut Option<String>, tag: &str, text: Option<&str>) {
    let mut parts: Vec<String> = current
        .as_deref()
        .unwrap_or("")
        .split(" | ")
        .filter(|p| !p.is_empty() && !p.starts_with(tag))
        .map(|s| s.to_string())
        .collect();
    if let Some(t) = text {
        if !t.is_empty() {
            parts.push(t.to_string());
        }
    }
    *current = if parts.is_empty() { None } else { Some(parts.join(" | ")) };
}

pub fn no_signal_warning() -> String {
    format!("{TAG_NOSIGNAL} the card is streaming black frames (PS5 off, HDMI unplugged, or HDCP). This recording is black.")
}

// --- USB link ----------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UsbLink {
    SuperSpeed,
    HighSpeed,
    Unknown,
}

impl UsbLink {
    pub fn as_str(self) -> &'static str {
        match self {
            UsbLink::SuperSpeed => "superspeed",
            UsbLink::HighSpeed => "highspeed",
            UsbLink::Unknown => "unknown",
        }
    }
}

/// Classify a PnP / location dump. The product string "USB3.0 Video" is not a link speed.
/// A legacy hub IOCTL reports HighSpeed for SuperSpeed devices, so any SuperSpeed token wins.
/// `ROOT_HUB30` / `ROOT_HUB20` are the Windows parent hubs (USB 3 vs the USB 2 companion).
pub fn classify_usb_link(text: &str) -> UsbLink {
    if text.trim().is_empty() {
        return UsbLink::Unknown;
    }
    let lower = text.to_ascii_lowercase();
    if has_port_token(text, "SS")
        || lower.contains("superspeed")
        || lower.contains("usb300")
        || lower.contains("root_hub30")
    {
        return UsbLink::SuperSpeed;
    }
    if has_port_token(text, "HS")
        || lower.contains("highspeed")
        || lower.contains("high-speed")
        || lower.contains("rev_2100")
        || lower.contains("root_hub20")
    {
        return UsbLink::HighSpeed;
    }
    UsbLink::Unknown
}

fn has_port_token(text: &str, prefix: &str) -> bool {
    text.split(|c: char| !c.is_ascii_alphanumeric()).any(|tok| {
        let u = tok.to_ascii_uppercase();
        u.len() >= prefix.len() + 1
            && u.starts_with(prefix)
            && u[prefix.len()..].chars().all(|c| c.is_ascii_digit())
    })
}

/// What `probe_usb_link` managed to read. Empty `text` is not a measured "unknown" link:
/// `error` says why the probe produced nothing.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UsbProbe {
    pub text: String,
    pub error: String,
}

/// Warning to show for a USB link check. An empty dump is a failed probe, not a classified link.
pub fn usb_link_warning(text: &str, error: &str, link: UsbLink, modes: &[crate::ffmpeg::Mode], requesting_raw60: bool) -> Option<String> {
    if text.trim().is_empty() {
        let why = if error.is_empty() { "probe returned no PnP text" } else { error };
        return Some(format!("{TAG_USB} USB link probe failed ({why}); link speed was not read"));
    }
    raw60_warning(link, modes, requesting_raw60)
}

/// PowerShell 5.1 script. Registry first (fast, no module), then location/parent for that one instance.
/// The old probe enumerated every present device with `Get-PnpDevice -PresentOnly` and hid errors.
/// That walk often outlived the 8s kill, PowerShell block-buffers stdout when it is not a console,
/// and stderr was discarded, so the log showed "0 bytes of pnp text" next to a successful mode list.
pub const USB_PROBE_SCRIPT: &str = r#"
$ProgressPreference='SilentlyContinue'
$ErrorActionPreference='Continue'
try { [Console]::Out.AutoFlush = $true } catch {}
function Emit([string]$s) { [Console]::Out.WriteLine($s); try { [Console]::Out.Flush() } catch {} }
Emit 'PROBE=start'
$base = 'HKLM:\SYSTEM\CurrentControlSet\Enum\USB\VID_345F&PID_2131'
if (-not (Test-Path -LiteralPath $base)) {
  Emit 'REGISTRY=missing'
} else {
  Get-ChildItem -LiteralPath $base -ErrorAction Continue | ForEach-Object {
    $id = 'USB\VID_345F&PID_2131\' + $_.PSChildName
    Emit ('INSTANCE=' + $id)
    try {
      $p = Get-ItemProperty -LiteralPath $_.PSPath -ErrorAction Stop
      foreach ($h in @($p.HardwareID)) { if ($h) { Emit ('HW=' + $h) } }
      if ($p.LocationInformation) { Emit ('LOCINFO=' + $p.LocationInformation) }
      if ($p.FriendlyName) { Emit ('NAME=' + $p.FriendlyName) }
    } catch {
      Emit ('REGERR=' + $_.Exception.Message)
    }
    try {
      Import-Module PnpDevice -ErrorAction Stop
      foreach ($k in @('DEVPKEY_Device_LocationPaths','DEVPKEY_Device_LocationInfo','DEVPKEY_Device_Parent','DEVPKEY_Device_HardwareIds')) {
        $d = (Get-PnpDeviceProperty -InstanceId $id -KeyName $k -ErrorAction Stop).Data
        if ($null -ne $d) { @($d) | ForEach-Object { if ($_) { Emit ('PROP=' + $k + '=' + $_) } } }
      }
    } catch {
      Emit ('PNPERR=' + $_.Exception.Message)
    }
  }
}
Emit 'PROBE=done'
"#;

/// Read-only PnP dump for VID 345F PID 2131. On non-Windows, `error` explains that the probe did not run.
pub fn probe_usb_link() -> UsbProbe {
    #[cfg(windows)]
    {
        let (stdout, stderr, timed_out) = run_capture(
            "powershell",
            &["-NoProfile", "-NonInteractive", "-Command", USB_PROBE_SCRIPT],
            Duration::from_secs(12),
        );
        let error = if timed_out {
            format!("timed out after 12s; stderr: {}", clip(&stderr))
        } else if stdout.trim().is_empty() {
            if stderr.trim().is_empty() {
                "returned no PnP text and no stderr".into()
            } else {
                format!("returned no PnP text; stderr: {}", clip(&stderr))
            }
        } else {
            String::new()
        };
        return UsbProbe { text: stdout, error };
    }
    #[cfg(not(windows))]
    {
        UsbProbe { text: String::new(), error: "usb link probe only runs on Windows".into() }
    }
}

#[cfg_attr(not(windows), allow(dead_code))]
fn clip(s: &str) -> String {
    let t = s.split_whitespace().collect::<Vec<_>>().join(" ");
    let n = t.chars().count();
    if n <= 240 {
        t
    } else {
        format!("{}...", t.chars().take(240).collect::<String>())
    }
}

#[cfg(windows)]
fn run_capture(program: &str, args: &[&str], bound: Duration) -> (String, String, bool) {
    let mut cmd = hidden(program);
    cmd.args(args).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => return (String::new(), format!("failed to start {program}: {e}"), false),
    };
    let mut stdout = match child.stdout.take() {
        Some(p) => p,
        None => return (String::new(), "probe stdout was not piped".into(), false),
    };
    let mut stderr = match child.stderr.take() {
        Some(p) => p,
        None => return (String::new(), "probe stderr was not piped".into(), false),
    };
    let (tx_out, rx_out) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = std::io::Read::read_to_end(&mut stdout, &mut buf);
        let _ = tx_out.send(buf);
    });
    let (tx_err, rx_err) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = std::io::Read::read_to_end(&mut stderr, &mut buf);
        let _ = tx_err.send(buf);
    });
    let start = Instant::now();
    let mut timed_out = false;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if start.elapsed() > bound => {
                let _ = child.kill();
                let _ = wait_until(&mut child, Duration::from_secs(2));
                timed_out = true;
                break;
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(30)),
            Err(_) => break,
        }
    }
    let out_bytes = rx_out.recv_timeout(Duration::from_secs(2)).unwrap_or_default();
    let err_bytes = rx_err.recv_timeout(Duration::from_secs(2)).unwrap_or_default();
    (
        String::from_utf8_lossy(&out_bytes).into_owned(),
        String::from_utf8_lossy(&err_bytes).into_owned(),
        timed_out,
    )
}

#[cfg(windows)]
fn hidden(program: &str) -> Command {
    #[allow(unused_mut)]
    let mut c = Command::new(program);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        c.creation_flags(0x0800_0000);
    }
    c
}

pub fn yuy2_720p60_listed(modes: &[crate::ffmpeg::Mode]) -> bool {
    modes.iter().any(|m| m.raw && m.format == "yuyv422" && m.w == 1280 && m.h == 720 && m.max_fps + 0.5 >= 60.0)
}

/// Warning when uncompressed 720p60 cannot be carried. `None` means nothing to warn about.
pub fn raw60_warning(link: UsbLink, modes: &[crate::ffmpeg::Mode], requesting_raw60: bool) -> Option<String> {
    let mut parts: Vec<String> = Vec::new();
    if !modes.is_empty() && !yuy2_720p60_listed(modes) {
        parts.push("card mode list has no YUY2 1280x720@60".into());
    }
    match link {
        UsbLink::HighSpeed => parts.push(format!(
            "USB link is high-speed (USB 2). YUY2 720p60 needs ~{YUY2_720P60_MBIT:.0} Mbit/s, which does not fit USB 2. Plug the card into a blue USB 3 port"
        )),
        UsbLink::Unknown if requesting_raw60 => parts.push(format!(
            "could not confirm a USB 3 SuperSpeed link. YUY2 720p60 needs ~{YUY2_720P60_MBIT:.0} Mbit/s; on USB 2 this capture will not keep up"
        )),
        _ => {}
    }
    if parts.is_empty() { None } else { Some(format!("{TAG_USB} {}", parts.join("; "))) }
}

// --- real-time buffer --------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BufferKind {
    Audio,
    Video,
    Unknown,
}

#[derive(Debug, Clone, PartialEq)]
pub struct BufferWarning {
    pub stream: String,
    pub percent: Option<u32>,
    pub size_bytes: Option<u64>,
    pub kind: BufferKind,
    pub queued_bytes: u64,
    pub lag_s: Option<f64>,
    pub text: String,
}

/// Parse an ffmpeg dshow "real-time buffer ... too full" line. `None` if it is not one.
pub fn parse_buffer_warning(line: &str) -> Option<BufferWarning> {
    let raw = line.trim();
    let lower = raw.to_ascii_lowercase();
    if !lower.contains("real-time buffer") || !lower.contains("full") {
        return None;
    }
    let stream = bracket_name(raw).unwrap_or("").to_string();
    let percent = first_percent(&lower);
    let size_bytes = last_size(raw);
    let kind = buffer_kind(&stream, size_bytes);
    let queued_bytes = match (percent, size_bytes) {
        (Some(p), Some(sz)) => (sz as u128 * u128::from(p) / 100) as u64,
        _ => 0,
    };
    let lag_s = if kind == BufferKind::Audio && queued_bytes > 0 {
        Some(queued_bytes as f64 / AUDIO_BYTES_PER_SEC)
    } else {
        None
    };
    let text = buffer_text(&stream, kind, percent, queued_bytes, lag_s);
    Some(BufferWarning { stream, percent, size_bytes, kind, queued_bytes, lag_s, text })
}

fn bracket_name(s: &str) -> Option<&str> {
    let i = s.find('[')?;
    let j = s[i + 1..].find(']')?;
    let name = s[i + 1..i + 1 + j].trim();
    if name.is_empty() { None } else { Some(name) }
}

fn first_percent(lower: &str) -> Option<u32> {
    let i = lower.find('%')?;
    let b = lower.as_bytes();
    let mut start = i;
    while start > 0 && b[start - 1].is_ascii_digit() {
        start -= 1;
    }
    if start == i { None } else { lower[start..i].parse().ok() }
}

/// Last integer in the line that is bigger than a bare percent (so "62%" is not the buffer size).
fn last_size(s: &str) -> Option<u64> {
    let mut last: Option<u64> = None;
    let mut cur = String::new();
    let flush = |cur: &mut String, last: &mut Option<u64>| {
        if cur.is_empty() {
            return;
        }
        if let Ok(n) = cur.parse::<u64>() {
            if n > 100 {
                *last = Some(n);
            }
        }
        cur.clear();
    };
    for c in s.chars() {
        if c.is_ascii_digit() {
            cur.push(c);
        } else if c == ',' && !cur.is_empty() {
            // thousands separator
        } else {
            flush(&mut cur, &mut last);
        }
    }
    flush(&mut cur, &mut last);
    last
}

fn buffer_kind(stream: &str, size: Option<u64>) -> BufferKind {
    let s = stream.to_ascii_lowercase();
    if s.contains("audio") {
        BufferKind::Audio
    } else if s.contains("video") {
        BufferKind::Video
    } else {
        match size {
            Some(n) if n <= 16 * 1024 * 1024 => BufferKind::Audio,
            Some(_) => BufferKind::Video,
            None => BufferKind::Unknown,
        }
    }
}

fn buffer_text(stream: &str, kind: BufferKind, percent: Option<u32>, queued: u64, lag_s: Option<f64>) -> String {
    let who = if stream.is_empty() {
        match kind {
            BufferKind::Audio => "audio",
            BufferKind::Video => "video",
            BufferKind::Unknown => "capture",
        }
        .to_string()
    } else {
        stream.to_string()
    };
    let mb = queued as f64 / (1024.0 * 1024.0);
    match (kind, percent, lag_s) {
        (BufferKind::Audio, Some(p), Some(lag)) => format!(
            "{TAG_BUFFER} {who} real-time buffer {p}% full, about {lag:.1}s of audio queued ({mb:.1} MB). Capture is behind the card."
        ),
        (BufferKind::Video, Some(p), _) => format!(
            "{TAG_BUFFER} {who} real-time buffer {p}% full, {mb:.0} MB queued. Downstream encode is behind the card."
        ),
        _ => format!("{TAG_BUFFER} {who} real-time buffer is full. Capture is behind the card."),
    }
}

#[derive(Debug)]
pub struct BufferPersist {
    window_ms: u64,
    need: u32,
    hits: VecDeque<u64>,
}

impl BufferPersist {
    pub fn new() -> Self {
        Self { window_ms: 10_000, need: 6, hits: VecDeque::new() }
    }
    /// True once `need` warnings have landed inside the window.
    pub fn observe(&mut self, now_ms: u64) -> bool {
        self.hits.push_back(now_ms);
        let cut = now_ms.saturating_sub(self.window_ms);
        while self.hits.front().copied().unwrap_or(u64::MAX) < cut {
            self.hits.pop_front();
        }
        self.hits.len() as u32 >= self.need
    }
}

impl Default for BufferPersist {
    fn default() -> Self { Self::new() }
}

// --- black frames ------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct BlackDetect {
    pub max_mean: f64,
    /// A pixel at or above this luma counts as lit.
    pub lit_at: u8,
    pub max_lit_frac: f64,
}

impl Default for BlackDetect {
    fn default() -> Self {
        // Pure HDMI-black (the MS2131 streams it at full rate with no signal) decodes near 0.
        // A real Madden frame, even a dark one, has far more lit pixels than 2%.
        Self { max_mean: 10.0, lit_at: 24, max_lit_frac: 0.02 }
    }
}

pub fn luma_is_black(samples: &[u8], cfg: &BlackDetect) -> bool {
    if samples.is_empty() {
        return false;
    }
    let mut sum: u64 = 0;
    let mut lit: u64 = 0;
    for &p in samples {
        sum += u64::from(p);
        if p >= cfg.lit_at {
            lit += 1;
        }
    }
    let n = samples.len() as f64;
    let mean = sum as f64 / n;
    let frac = lit as f64 / n;
    mean <= cfg.max_mean && frac <= cfg.max_lit_frac
}

/// Even bytes of YUY2 are luma.
pub fn yuy2_luma(packed: &[u8]) -> Vec<u8> {
    packed.iter().step_by(2).copied().collect()
}

pub fn parse_pgm_p2(text: &str) -> Result<Vec<u8>, String> {
    let toks: Vec<&str> = text.split_whitespace().collect();
    if toks.first().copied() != Some("P2") {
        return Err("not a P2 pgm".into());
    }
    if toks.len() < 4 {
        return Err("short pgm".into());
    }
    let w: usize = toks[1].parse().map_err(|_| "bad width")?;
    let h: usize = toks[2].parse().map_err(|_| "bad height")?;
    let maxv: u32 = toks[3].parse().map_err(|_| "bad maxval")?;
    if maxv == 0 || maxv > 255 {
        return Err("maxval out of range".into());
    }
    let px = &toks[4..];
    if px.len() != w * h {
        return Err(format!("expected {} pixels, got {}", w * h, px.len()));
    }
    px.iter()
        .map(|p| {
            let v: u32 = p.parse().map_err(|_| "bad pixel".to_string())?;
            if v > maxv {
                return Err("pixel above maxval".into());
            }
            Ok((v * 255 / maxv) as u8)
        })
        .collect()
}

#[derive(Debug)]
pub struct BlackStreak {
    need: u32,
    run: u32,
}

impl BlackStreak {
    pub fn new(need: u32) -> Self {
        Self { need: need.max(1), run: 0 }
    }
    /// True while the black run is at least `need` samples long.
    pub fn push(&mut self, black: bool) -> bool {
        if black {
            self.run = self.run.saturating_add(1);
        } else {
            self.run = 0;
        }
        self.run >= self.need
    }
}

/// Copy `path` to a temp file. The no-signal watcher must not open the live snapshot:
/// ffmpeg image2 `-atomic_writing` renames `snapshot.jpg.tmp` onto `snapshot.jpg`, and Windows
/// returns "Operation not permitted" while another ffmpeg holds the destination open.
fn copy_aside(path: &Path) -> Option<PathBuf> {
    let bytes = std::fs::read(path).ok()?;
    if bytes.len() < 8 {
        return None;
    }
    let mut tmp = std::env::temp_dir();
    let nanos = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
    let leaf = path.file_name().and_then(|s| s.to_str()).unwrap_or("snap");
    tmp.push(format!("qorcap-luma-{}-{}-{}", std::process::id(), nanos, leaf));
    std::fs::write(&tmp, &bytes).ok()?;
    Some(tmp)
}

/// Decode one snapshot to 160x90 gray. Only used by the opt-in no-signal watcher.
/// Decodes a private copy so the live `snapshot.jpg` can still be renamed.
pub fn decode_luma_file(ffmpeg: &str, path: &Path) -> Option<Vec<u8>> {
    let tmp = copy_aside(path)?;
    let out = decode_luma_path(ffmpeg, &tmp);
    let _ = std::fs::remove_file(&tmp);
    out
}

fn decode_luma_path(ffmpeg: &str, path: &Path) -> Option<Vec<u8>> {
    const N: usize = 160 * 90;
    let mut cmd = crate::ffmpeg::command(ffmpeg);
    cmd.args([
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        &path.to_string_lossy(),
        "-frames:v",
        "1",
        "-vf",
        "scale=160:90",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "pipe:1",
    ]);
    cmd.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null());
    let mut child = cmd.spawn().ok()?;
    let mut stdout = child.stdout.take()?;
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = std::io::Read::read_to_end(&mut stdout, &mut buf);
        let _ = tx.send(buf);
    });
    let buf = match rx.recv_timeout(Duration::from_secs(3)) {
        Ok(b) => b,
        Err(_) => {
            let _ = child.kill();
            let _ = wait_until(&mut child, Duration::from_secs(1));
            return None;
        }
    };
    let _ = wait_until(&mut child, Duration::from_secs(1));
    if buf.len() == N { Some(buf) } else { None }
}

/// Kill `child` and wait. Unbounded wait matches today's `kill(); wait()` and returns its status.
/// Bounded wait returns `None` if the process is still alive after [`KILL_BOUND`] instead of blocking.
pub fn stop_child(child: &mut Child, bounded: bool) -> Option<std::process::ExitStatus> {
    let _ = child.kill();
    if !bounded {
        return child.wait().ok();
    }
    let end = Instant::now() + KILL_BOUND;
    loop {
        match child.try_wait() {
            Ok(Some(st)) => return Some(st),
            Ok(None) if Instant::now() < end => std::thread::sleep(Duration::from_millis(20)),
            _ => return None,
        }
    }
}

fn wait_until(child: &mut Child, bound: Duration) -> bool {
    let end = Instant::now() + bound;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return true,
            Ok(None) if Instant::now() < end => std::thread::sleep(Duration::from_millis(20)),
            _ => return false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn profile_is_a_noop_until_a_flag_is_on() {
        use clap::Parser;
        let crate::cli::Cmd::Record(mut r) = crate::cli::Cli::try_parse_from(["q", "record"]).unwrap().cmd else { panic!() };
        let before = (r.res, r.fps, r.input_format.clone(), r.buffer_watch, r.reader_snapshot.clone(), r.rtbufsize.clone());
        apply_capture_profile(&mut r);
        assert_eq!(before, (r.res, r.fps, r.input_format.clone(), r.buffer_watch, r.reader_snapshot.clone(), r.rtbufsize.clone()));
        assert!(!any_tune(&r));
        r.yuy2_720p60 = true;
        apply_capture_profile(&mut r);
        assert_eq!((r.res, r.fps, r.input_format.as_str()), ((1280, 720), 60, "yuyv422"));
        r.buffer_restart = true;
        r.buffer_watch = false;
        apply_capture_profile(&mut r);
        assert!(r.buffer_watch);
    }

    #[test]
    fn arrival_and_restart_policy() {
        assert_eq!(arrival_settle(false, true), None);
        assert_eq!(arrival_settle(true, false), None);
        assert_eq!(arrival_settle(true, true), Some(ARRIVAL_SETTLE));
        assert!(restart_due(true, true, 5_000, 0));
        assert!(!restart_due(true, false, 5_000, 0));
        assert!(!restart_due(false, true, 5_000, 0));
        assert!(!restart_due(true, true, 10_000, 1));
        assert!(restart_due(true, true, 40_000, 1));
    }

    #[test]
    fn tagged_warnings_do_not_clobber_audio() {
        let mut w = Some("AUDIO FAILED — recording video-only (no game sound). Capture continues.".into());
        upsert_tagged(&mut w, TAG_NOSIGNAL, Some(&no_signal_warning()));
        let s = w.clone().unwrap();
        assert!(s.starts_with("AUDIO FAILED"));
        assert!(s.contains(TAG_NOSIGNAL));
        upsert_tagged(&mut w, TAG_NOSIGNAL, None);
        assert_eq!(w.as_deref(), Some("AUDIO FAILED — recording video-only (no game sound). Capture continues."));
        upsert_tagged(&mut w, TAG_BUFFER, Some("BUFFER LAG: audio 62%"));
        upsert_tagged(&mut w, TAG_BUFFER, Some("BUFFER LAG: audio 70%"));
        assert_eq!(w.as_ref().unwrap().matches("BUFFER LAG:").count(), 1);
        assert!(w.as_ref().unwrap().contains("70%"));
    }

    #[test]
    fn usb_link_from_teardown_text() {
        let ss = "INSTANCE=USB\\VID_345F&PID_2131\\93493208\nHW=USB\\VID_345F&PID_2131&REV_3100\nLOC=ACPI(_SB_)#PCI0#XHC#RHUB.SS02\nprotocols=USB300 operating at SuperSpeed\n";
        assert_eq!(classify_usb_link(ss), UsbLink::SuperSpeed);
        // The legacy EX IOCTL says HighSpeed even on a SuperSpeed port. SS wins.
        let lied = "HighSpeed\nLOC=RHUB.SS02\nUSB300\n";
        assert_eq!(classify_usb_link(lied), UsbLink::SuperSpeed);
        let hs = "INSTANCE=USB\\VID_345F&PID_2131\\80239439\nHW=USB\\VID_345F&PID_2131&REV_2100\nLOC=ACPI(_SB_)#PCI0#XHC#RHUB.HS03\n";
        assert_eq!(classify_usb_link(hs), UsbLink::HighSpeed);
        // The product string is not a link speed.
        assert_eq!(classify_usb_link("FriendlyName=USB3.0 Video\n"), UsbLink::Unknown);
        assert_eq!(classify_usb_link(""), UsbLink::Unknown);
        assert_eq!(classify_usb_link("PROP=DEVPKEY_Device_Parent=USB\\ROOT_HUB30\\4&x\n"), UsbLink::SuperSpeed);
        assert_eq!(classify_usb_link("PROP=DEVPKEY_Device_Parent=USB\\ROOT_HUB20\\4&x\n"), UsbLink::HighSpeed);
        let empty = usb_link_warning("", "returned no PnP text; stderr: timed out", UsbLink::Unknown, &[], true).unwrap();
        assert!(empty.contains("probe failed"));
        assert!(empty.contains("timed out"));
        assert!(usb_link_warning(ss, "", UsbLink::SuperSpeed, &[], false).is_none());
        assert!((YUY2_720P60_MBIT - 884.736).abs() < 0.01);
    }

    #[test]
    fn raw60_warning_only_when_the_link_or_the_mode_list_says_so() {
        let yuy = crate::ffmpeg::parse_dshow_options(
            "  pixel_format=yuyv422  min s=1280x720 fps=10 max s=1280x720 fps=60\n  vcodec=mjpeg  min s=1280x720 fps=10 max s=1280x720 fps=60\n",
        );
        assert!(yuy2_720p60_listed(&yuy));
        assert!(raw60_warning(UsbLink::SuperSpeed, &yuy, true).is_none());
        let hs = raw60_warning(UsbLink::HighSpeed, &yuy, false).unwrap();
        assert!(hs.starts_with(TAG_USB));
        assert!(hs.contains("USB 2"));
        assert!(hs.contains("885") || hs.contains("884"));
        let unknown = raw60_warning(UsbLink::Unknown, &yuy, true).unwrap();
        assert!(unknown.contains("could not confirm"));
        assert!(raw60_warning(UsbLink::Unknown, &yuy, false).is_none());
        let mjpeg_only = crate::ffmpeg::parse_dshow_options("  vcodec=mjpeg  min s=1280x720 fps=10 max s=1280x720 fps=30\n");
        let missing = raw60_warning(UsbLink::SuperSpeed, &mjpeg_only, false).unwrap();
        assert!(missing.contains("no YUY2 1280x720@60"));
        assert!(raw60_warning(UsbLink::SuperSpeed, &[], true).is_none());
    }

    #[test]
    fn usb_probe_script_reads_one_instance_and_keeps_errors() {
        let s = USB_PROBE_SCRIPT;
        assert!(s.contains(r"VID_345F&PID_2131"));
        assert!(s.contains("AutoFlush"));
        assert!(s.contains("DEVPKEY_Device_LocationPaths"));
        assert!(s.contains("DEVPKEY_Device_Parent"));
        assert!(s.contains("$ProgressPreference='SilentlyContinue'"));
        assert!(!s.contains("$ErrorActionPreference='SilentlyContinue'"));
        assert!(!s.contains("-PresentOnly"));
        let non_win = if cfg!(windows) { None } else { Some(probe_usb_link()) };
        if let Some(p) = non_win {
            assert!(p.text.is_empty());
            assert!(p.error.contains("Windows"));
        }
    }

    #[test]
    fn luma_decode_copies_aside_and_does_not_keep_the_live_snapshot_open() {
        let dir = std::env::temp_dir().join(format!("qorcap-luma-dir-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let live = dir.join("snapshot.jpg");
        std::fs::write(&live, b"0123456789abcdef").unwrap();
        let copied = copy_aside(&live).unwrap();
        assert_ne!(copied, live);
        assert_eq!(std::fs::read(&copied).unwrap(), b"0123456789abcdef");
        let _ = std::fs::remove_file(&copied);
        assert!(decode_luma_file("qorcap-no-such-ffmpeg", &live).is_none());
        std::fs::write(&live, b"replaced-bytes!!").unwrap();
        assert_eq!(std::fs::read(&live).unwrap(), b"replaced-bytes!!");
        let prefix = format!("qorcap-luma-{}", std::process::id());
        let left: Vec<_> = std::fs::read_dir(std::env::temp_dir())
            .unwrap()
            .filter_map(|e| e.ok())
            .filter(|e| e.file_name().to_string_lossy().starts_with(&prefix) && e.path() != dir)
            .map(|e| e.path())
            .collect();
        assert!(left.is_empty(), "decoder left snapshot copies behind: {left:?}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn audio_buffer_line_from_the_match_logs_is_about_ten_seconds() {
        let w = parse_buffer_warning("real-time buffer [USB3.0 Audio] too full (62% of 3,041,280)").unwrap();
        assert_eq!(w.stream, "USB3.0 Audio");
        assert_eq!(w.percent, Some(62));
        assert_eq!(w.size_bytes, Some(3_041_280));
        assert_eq!(w.kind, BufferKind::Audio);
        let lag = w.lag_s.unwrap();
        assert!((lag - 9.82).abs() < 0.02, "{lag}");
        assert!(w.text.starts_with(TAG_BUFFER));
        assert!(w.text.contains("9.8s"));
        let video = parse_buffer_warning(
            "real-time buffer [USB3.0 Video] too full or near too full (62% of size: 536870912)! frame dropped!",
        )
        .unwrap();
        assert_eq!(video.kind, BufferKind::Video);
        assert!(video.lag_s.is_none());
        let mb = video.queued_bytes as f64 / (1024.0 * 1024.0);
        assert!((mb - 317.44).abs() < 0.1, "{mb}");
        assert!(parse_buffer_warning("ffmpeg: opening device").is_none());
        assert!(parse_buffer_warning("real-time buffer 62% full").unwrap().size_bytes.is_none());
    }

    #[test]
    fn buffer_persistence_needs_a_burst() {
        let mut p = BufferPersist::new();
        let mut hot = false;
        for i in 0..5 {
            hot = p.observe(i * 100);
        }
        assert!(!hot);
        assert!(p.observe(600));
        let mut later = BufferPersist::new();
        for i in 0..6 {
            later.observe(i * 100);
        }
        assert!(!later.observe(20_000));
    }

    #[test]
    fn black_fixtures_and_a_bright_patch() {
        let cfg = BlackDetect::default();
        let black = parse_pgm_p2(include_str!("../tests/fixtures/black.pgm")).unwrap();
        let spot = parse_pgm_p2(include_str!("../tests/fixtures/not_black.pgm")).unwrap();
        assert!(luma_is_black(&black, &cfg));
        assert!(!luma_is_black(&spot, &cfg));
        assert!(luma_is_black(&vec![4u8; 1000], &cfg));
        assert!(!luma_is_black(&vec![18u8; 1000], &cfg));
        assert!(!luma_is_black(&[], &cfg));
        // One hot pixel in a large black field is still "no picture".
        let mut noisy = vec![0u8; 10_000];
        noisy[10] = 255;
        assert!(luma_is_black(&noisy, &cfg));
        // A bright block about the size of a scorebug digit row on a 160x90 sample is not black.
        // (A couple of hot pixels in a black field still counts as no picture; see `noisy` above.)
        let mut digit = vec![0u8; 160 * 90];
        for y in 60..90 {
            for x in 8..48 {
                digit[y * 160 + x] = 220;
            }
        }
        assert!(!luma_is_black(&digit, &cfg));
        let yuy = yuy2_luma(&[0, 128, 0, 128, 255, 128, 255, 128]);
        assert_eq!(yuy, vec![0, 0, 255, 255]);
        assert!(luma_is_black(&yuy2_luma(&vec![0, 128].repeat(64)), &cfg));
        let mut streak = BlackStreak::new(3);
        assert!(!streak.push(true));
        assert!(!streak.push(true));
        assert!(streak.push(true));
        assert!(!streak.push(false));
    }

    #[cfg(unix)]
    #[test]
    fn bounded_wait_gives_up_while_the_child_is_alive() {
        let mut c = Command::new("sleep").arg("30").stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn().unwrap();
        let t = Instant::now();
        assert!(!wait_until(&mut c, Duration::from_millis(250)));
        assert!(t.elapsed() < Duration::from_secs(2), "bounded wait blocked: {:?}", t.elapsed());
        assert!(t.elapsed() >= Duration::from_millis(200));
        assert!(stop_child(&mut c, true).is_some());
    }
}
