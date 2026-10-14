//! ffmpeg discovery, DirectShow device listing and argument building.
use crate::cli::RecordArgs;
use std::path::PathBuf;
use std::process::{Command, Stdio};

#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct Device { pub name: String, pub kind: String, pub alt: Option<String> }

pub fn find_ffmpeg(explicit: Option<&str>) -> String {
    if let Some(p) = explicit { return p.to_string(); }
    let exe = if cfg!(windows) { "ffmpeg.exe" } else { "ffmpeg" };
    if let Ok(me) = std::env::current_exe() {
        for d in [me.parent().map(|p| p.to_path_buf()), me.parent().map(|p| p.join("ffmpeg"))].into_iter().flatten() {
            let c: PathBuf = d.join(exe);
            if c.exists() { return c.to_string_lossy().into_owned(); }
            let c = d.join("bin").join(exe);
            if c.exists() { return c.to_string_lossy().into_owned(); }
        }
    }
    exe.to_string()
}

pub fn command(ffmpeg: &str) -> Command {
    #[allow(unused_mut)]
    let mut c = Command::new(ffmpeg);
    #[cfg(windows)]
    { use std::os::windows::process::CommandExt; c.creation_flags(0x0800_0000); } // CREATE_NO_WINDOW
    c
}

/// Parse `ffmpeg -list_devices true -f dshow -i dummy` stderr (old and new formats).
pub fn parse_dshow_list(s: &str) -> Vec<Device> {
    let mut out: Vec<Device> = Vec::new();
    let mut section: Option<&str> = None;
    for line in s.lines() {
        let l = line.split_once("] ").map(|x| x.1).unwrap_or(line).trim();
        if l.contains("DirectShow video devices") { section = Some("video"); continue; }
        if l.contains("DirectShow audio devices") { section = Some("audio"); continue; }
        if let Some(rest) = l.strip_prefix("Alternative name ") {
            if let Some(d) = out.last_mut() { d.alt = Some(rest.trim_matches('"').to_string()); }
            continue;
        }
        if !l.starts_with('"') { continue; }
        let Some(end) = l[1..].find('"') else { continue };
        let name = l[1..1 + end].to_string();
        let tail = &l[2 + end..];
        let kind = if tail.contains("(video)") { "video" } else if tail.contains("(audio)") { "audio" }
            else if tail.contains("(none)") { continue } else if let Some(s) = section { s } else { continue };
        out.push(Device { name, kind: kind.into(), alt: None });
    }
    out
}

pub fn list_devices(ffmpeg: &str) -> std::io::Result<Vec<Device>> {
    let o = command(ffmpeg).args(["-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"])
        .stdin(Stdio::null()).output()?;
    Ok(parse_dshow_list(&String::from_utf8_lossy(&o.stderr)))
}

/// Choose the audio device that belongs to the capture card.
pub fn pick_audio(video: &str, want: &str, devs: &[Device]) -> Option<String> {
    match want {
        "none" | "off" => None,
        "auto" => {
            let audio: Vec<&Device> = devs.iter().filter(|d| d.kind == "audio").collect();
            let stem = video.split_whitespace().next().unwrap_or(video).to_lowercase(); // "usb3.0"
            audio.iter().find(|d| d.name.to_lowercase().contains(&video.to_lowercase()))
                .or_else(|| audio.iter().find(|d| d.name.to_lowercase().contains(&stem)))
                .or_else(|| audio.iter().find(|d| { let n = d.name.to_lowercase(); n.contains("capture") || n.contains("hdmi") || n.contains("digital audio interface") }))
                .map(|d| d.name.clone())
        }
        n => Some(n.to_string()),
    }
}

/// Escape a path for use inside an ffmpeg filter option value.
pub fn filter_path(p: &str) -> String { p.replace('\\', "/").replace(':', "\\:").replace('\'', "\\'") }

/// One DirectShow capture mode from `-list_options true`.
#[derive(Debug, Clone, PartialEq)]
pub struct Mode { pub format: String, pub raw: bool, pub w: u32, pub h: u32, pub max_fps: f64 }

/// Parse `ffmpeg -list_options true -f dshow -i video=NAME` stderr.
/// Lines look like `  vcodec=mjpeg  min s=1280x720 fps=10 max s=1280x720 fps=30` or
/// `  pixel_format=yuyv422  min s=1920x1080 fps=5 max s=1920x1080 fps=60.0002`.
pub fn parse_dshow_options(s: &str) -> Vec<Mode> {
    let mut out: Vec<Mode> = Vec::new();
    for line in s.lines() {
        let l = line.split_once("] ").map(|x| x.1).unwrap_or(line).trim();
        let (raw, rest) = if let Some(r) = l.strip_prefix("pixel_format=") { (true, r) } else if let Some(r) = l.strip_prefix("vcodec=") { (false, r) } else { continue };
        let format = rest.split_whitespace().next().unwrap_or("").to_string();
        let Some(maxp) = rest.find("max s=") else { continue };
        let m = &rest[maxp + 6..];
        let mut it = m.split_whitespace();
        let Some(size) = it.next() else { continue };
        let Some((w, h)) = size.split_once('x') else { continue };
        let (Ok(w), Ok(h)) = (w.parse::<u32>(), h.parse::<u32>()) else { continue };
        let fps = it.next().and_then(|f| f.strip_prefix("fps=")).and_then(|f| f.parse::<f64>().ok()).unwrap_or(0.0);
        let md = Mode { format, raw, w, h, max_fps: fps };
        if !out.contains(&md) { out.push(md); }
    }
    out
}

pub fn list_options(ffmpeg: &str, device: &str) -> std::io::Result<Vec<Mode>> {
    let o = command(ffmpeg).args(["-hide_banner", "-list_options", "true", "-f", "dshow", "-i", &format!("video={device}")])
        .stdin(Stdio::null()).output()?;
    Ok(parse_dshow_options(&String::from_utf8_lossy(&o.stderr)))
}

/// Resolve `--input-format auto` from the card's mode list. Prefers a raw format that offers the
/// exact size at the requested fps (nv12, then yuyv422, then any), otherwise mjpeg (many USB cards
/// only expose raw modes at 1080p and up). Returns "auto" (let DirectShow pick) if nothing matches.
pub fn choose_input_format(modes: &[Mode], w: u32, h: u32, fps: u32) -> String {
    let fits = |m: &&Mode| m.w == w && m.h == h && m.max_fps + 0.5 >= fps as f64;
    let raw: Vec<&Mode> = modes.iter().filter(|m| m.raw).filter(fits).collect();
    for pf in ["nv12", "yuyv422"] { if raw.iter().any(|m| m.format == pf) { return pf.into(); } }
    if let Some(m) = raw.first() { return m.format.clone(); }
    if modes.iter().filter(|m| !m.raw && m.format == "mjpeg").any(|m| fits(&m)) { return "mjpeg".into(); }
    // size exists in mjpeg at a lower fps, or only mjpeg offered at all: still mjpeg beats a failed open
    if modes.iter().any(|m| !m.raw && m.format == "mjpeg" && m.w == w && m.h == h) { return "mjpeg".into(); }
    if !modes.is_empty() && !modes.iter().any(|m| m.raw && m.w == w && m.h == h) && modes.iter().any(|m| m.format == "mjpeg") { return "mjpeg".into(); }
    "auto".into()
}

/// What an ffmpeg capture failure means (from its last stderr lines).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Failure { AudioOpen, VideoNotFound, FormatUnsupported, Busy, Other }

pub fn classify_failure(tail: &str) -> Failure {
    let t = tail.to_lowercase();
    if t.contains("could not find audio only device") || (t.contains("audio") && (t.contains("could not find") || t.contains("could not enumerate audio"))) {
        return Failure::AudioOpen;
    }
    // Only call it busy when ffmpeg/DirectShow actually says so. "Could not run graph (sometimes caused by
    // a device already in use ...)" is ffmpeg guessing, so it stays Other.
    if t.contains("could not run graph") { return Failure::Other; }
    if t.contains("device is busy") || t.contains("resource busy") || t.contains("in use by") || t.contains("already in use")
        || t.contains("being used by another") || t.contains("0x800700aa") {
        return Failure::Busy;
    }
    if t.contains("could not find video device") || t.contains("could not find video only device") { return Failure::VideoNotFound; }
    if t.contains("could not set video options") || t.contains("could not find output pin") || t.contains("unable to") && t.contains("format") {
        return Failure::FormatUnsupported;
    }
    Failure::Other
}

pub fn failure_hint(f: Failure, obs_running: bool) -> String {
    let obs = if obs_running { " OBS (obs64.exe) is running. Close OBS. It's using the capture card." } else { "" };
    match f {
        Failure::AudioOpen => " (audio device could not be opened; continuing video-only)".into(),
        Failure::VideoNotFound => " (video device not found: unplugged or wrong name?)".into(),
        Failure::FormatUnsupported => " (card refused this size/fps/format: try --input-format mjpeg or another --res)".into(),
        Failure::Busy => format!(" (device busy: another app is using it){obs}"),
        Failure::Other => if obs_running { format!(" ({})", obs.trim()) } else { String::new() },
    }
}

/// Names of known apps that grab the capture card exclusively, if running (Windows only).
pub fn capture_apps_running() -> Vec<String> {
    #[cfg(windows)]
    {
        let mut c = Command::new("tasklist");
        { use std::os::windows::process::CommandExt; c.creation_flags(0x0800_0000); }
        if let Ok(o) = c.args(["/FO", "CSV", "/NH"]).stdin(Stdio::null()).output() {
            let s = String::from_utf8_lossy(&o.stdout).to_lowercase();
            return ["obs64.exe", "obs32.exe", "obs.exe"].iter().filter(|n| s.contains(&format!("\"{n}\""))).map(|n| n.to_string()).collect();
        }
    }
    Vec::new()
}

/// Arguments for the capture process: device -> CFR H.264/AAC MPEG-TS on stdout,
/// plus a periodically refreshed snapshot JPEG. `input_format` is already resolved
/// (auto/mjpeg/<pixel_format>). Video and audio are separate dshow inputs (a capture card's
/// audio is its own DirectShow audio device), both wall-clock stamped and resampled to sync.
pub fn capture_args(a: &RecordArgs, input_format: &str, audio: Option<&str>, snapshot: &str, stamp_file: Option<&str>) -> Vec<String> {
    let (w, h) = a.res;
    let fps = a.fps.to_string();
    let mut v: Vec<String> = ["-hide_banner", "-nostdin", "-loglevel", "warning", "-stats_period", "1", "-progress", "pipe:2"]
        .iter().map(|s| s.to_string()).collect();
    let push = |v: &mut Vec<String>, xs: &[&str]| v.extend(xs.iter().map(|s| s.to_string()));
    let has_audio;
    if a.device == "testpattern" {
        push(&mut v, &["-re", "-f", "lavfi", "-i", &format!("testsrc2=size={w}x{h}:rate={fps}")]);
        if audio.is_some() { push(&mut v, &["-re", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]); }
        has_audio = audio.is_some();
    } else {
        push(&mut v, &["-f", "dshow", "-rtbufsize", "512M", "-thread_queue_size", "1024", "-use_wallclock_as_timestamps", "1",
            "-video_size", &format!("{w}x{h}"), "-framerate", &fps]);
        match input_format {
            "auto" | "" => {}
            "mjpeg" => push(&mut v, &["-vcodec", "mjpeg"]),
            pf => push(&mut v, &["-pixel_format", pf]),
        }
        push(&mut v, &["-i", &format!("video={}", a.device)]);
        if let Some(au) = audio {
            push(&mut v, &["-f", "dshow", "-thread_queue_size", "1024", "-use_wallclock_as_timestamps", "1", "-audio_buffer_size", "50",
                "-i", &format!("audio={au}")]);
        }
        has_audio = audio.is_some();
    }
    push(&mut v, &["-map", "0:v"]);
    if has_audio { push(&mut v, &["-map", "1:a"]); }
    let mut vf = format!("fps={fps}");
    if let Some(t) = stamp_file {
        let font = if cfg!(windows) { ":fontfile='C\\:/Windows/Fonts/consola.ttf'" } else { "" };
        vf.push_str(&format!(",drawtext=textfile='{}':reload=1{font}:fontsize=18:fontcolor=white:box=1:boxcolor=black@0.6:x=8:y=8", filter_path(t)));
    }
    vf.push_str(",format=nv12");
    push(&mut v, &["-vf", &vf, "-fps_mode", "cfr", "-r", &fps]);
    push(&mut v, &["-c:v", &a.encoder]);
    match a.encoder.as_str() {
        "h264_mf" => push(&mut v, &["-hw_encoding", "1", "-rate_control", "cbr", "-scenario", "live_streaming"]),
        "libx264" => push(&mut v, &["-preset", "veryfast", "-tune", "zerolatency"]),
        _ => {}
    }
    push(&mut v, &["-b:v", &a.bitrate, "-maxrate", &a.bitrate, "-bufsize", &a.bitrate, "-g", &fps, "-bf", "0"]);
    if has_audio { push(&mut v, &["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-af", "aresample=async=1000:first_pts=0"]); }
    push(&mut v, &["-f", "mpegts", "-mpegts_flags", "+resend_headers", "-pat_period", "0.2", "-muxdelay", "0", "pipe:1"]);
    // Snapshot output (4 fps, atomic rewrite so readers never see a partial JPEG)
    push(&mut v, &["-map", "0:v", "-vf", "fps=4", "-q:v", "3", "-f", "image2", "-update", "1", "-atomic_writing", "1", "-y", snapshot]);
    v
}

/// Remux a TS stream (stdin) into crash-safe fragmented MP4.
/// Output goes to stdout so QorCap writes and hashes the exact bytes (2 s fragments).
pub fn frag_mp4_args() -> Vec<String> {
    ["-hide_banner", "-nostdin", "-loglevel", "error", "-f", "mpegts", "-i", "pipe:0", "-map", "0", "-c", "copy",
     "-bsf:a", "aac_adtstoasc", "-movflags", "+frag_keyframe+empty_moov+default_base_moof", "-min_frag_duration", "2000000",
     "-flush_packets", "1", "-f", "mp4", "pipe:1"]
        .iter().map(|s| s.to_string()).collect()
}

/// Remux a saved TS replay file into a normal (faststart) MP4.
pub fn replay_mp4_args(ts: &str, out: &str) -> Vec<String> {
    ["-hide_banner", "-nostdin", "-loglevel", "error", "-f", "mpegts", "-i", ts, "-map", "0", "-c", "copy",
     "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", "-y", out].iter().map(|s| s.to_string()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    const NEW: &str = r#"[dshow @ 000001] "USB3.0 Video" (video)
[dshow @ 000001]   Alternative name "@device_pnp_\\?\usb#vid_534d&pid_2109"
[dshow @ 000001] "Integrated Camera" (video)
[dshow @ 000001] "Digital Audio Interface (USB3.0 Audio)" (audio)
[dshow @ 000001]   Alternative name "@device_cm_{33D9A762}\wave_{X}"
[dshow @ 000001] "Microphone Array (Realtek(R) Audio)" (audio)
dummy: Immediate exit requested"#;
    const OLD: &str = r#"[dshow @ 0] DirectShow video devices (some may be both video and audio devices)
[dshow @ 0]  "USB3.0 Video"
[dshow @ 0]     Alternative name "@device_pnp_x"
[dshow @ 0] DirectShow audio devices
[dshow @ 0]  "USB3.0 Audio""#;
    #[test]
    fn parse_new_and_old() {
        let d = parse_dshow_list(NEW);
        assert_eq!(d.len(), 4);
        assert_eq!(d[0], Device { name: "USB3.0 Video".into(), kind: "video".into(), alt: Some(r"@device_pnp_\\?\usb#vid_534d&pid_2109".into()) });
        assert_eq!(d[2].kind, "audio");
        let o = parse_dshow_list(OLD);
        assert_eq!(o.iter().map(|d| (d.name.as_str(), d.kind.as_str())).collect::<Vec<_>>(), vec![("USB3.0 Video", "video"), ("USB3.0 Audio", "audio")]);
    }
    #[test]
    fn audio_autopick() {
        let d = parse_dshow_list(NEW);
        assert_eq!(pick_audio("USB3.0 Video", "auto", &d).as_deref(), Some("Digital Audio Interface (USB3.0 Audio)"));
        assert_eq!(pick_audio("USB3.0 Video", "none", &d), None);
        assert_eq!(pick_audio("X", "Mic", &d).as_deref(), Some("Mic"));
        assert_eq!(pick_audio("Elgato", "auto", &d[3..]), None);
    }
    #[test]
    fn args_shape() {
        use clap::Parser;
        let c = crate::cli::Cli::try_parse_from(["q", "record", "--fps", "60"]).unwrap();
        let crate::cli::Cmd::Record(r) = c.cmd else { panic!() };
        let a = capture_args(&r, "mjpeg", Some("Digital Audio Interface (USB3.0 Audio)"), "snap.jpg", Some(r"C:\t\s.txt")).join(" ");
        assert!(a.contains("-f dshow"));
        assert!(!a.contains(":audio="), "{a}");
        assert!(a.contains("-vcodec mjpeg -i video=USB3.0 Video -f dshow"), "{a}");
        assert!(a.contains("-i audio=Digital Audio Interface (USB3.0 Audio)"));
        assert!(a.contains("-map 0:v -map 1:a"));
        assert_eq!(a.matches("-use_wallclock_as_timestamps 1").count(), 2);
        let n = capture_args(&r, "yuyv422", None, "snap.jpg", None).join(" ");
        assert!(n.contains("-pixel_format yuyv422") && !n.contains("1:a") && !n.contains("-c:a"));
        assert!(a.contains("-fps_mode cfr -r 60"));
        assert!(a.contains("-c:v h264_mf -hw_encoding 1"));
        assert!(a.contains("-g 60"));
        assert!(a.contains(r"drawtext=textfile='C\:/t/s.txt':reload=1"), "{a}");
        assert!(a.contains("-f mpegts"));
        assert!(frag_mp4_args().join(" ").contains("+frag_keyframe+empty_moov"));
    }

    const OPTS: &str = r#"[dshow @ 0000020] DirectShow video device options (from video devices)
[dshow @ 0000020]  Pin "Capture" (alternative pin name "0")
[dshow @ 0000020]   vcodec=mjpeg  min s=1920x1080 fps=5 max s=1920x1080 fps=30
[dshow @ 0000020]   vcodec=mjpeg  min s=1280x720 fps=5 max s=1280x720 fps=30
[dshow @ 0000020]   vcodec=mjpeg  min s=640x480 fps=5 max s=640x480 fps=30
[dshow @ 0000020]   pixel_format=yuyv422  min s=1920x1080 fps=5 max s=1920x1080 fps=60.0002
[dshow @ 0000020]   pixel_format=yuyv422  min s=2560x1440 fps=5 max s=2560x1440 fps=30
video=USB3.0 Video: Immediate exit requested"#;
    #[test]
    fn options_and_auto_format() {
        let m = parse_dshow_options(OPTS);
        assert_eq!(m.len(), 5);
        assert_eq!(m[1], Mode { format: "mjpeg".into(), raw: false, w: 1280, h: 720, max_fps: 30.0 });
        assert!(m[3].raw && m[3].max_fps > 60.0);
        // 720p only in mjpeg -> mjpeg (the real MyTurn card)
        assert_eq!(choose_input_format(&m, 1280, 720, 30), "mjpeg");
        // 1080p60 exists raw -> yuyv422
        assert_eq!(choose_input_format(&m, 1920, 1080, 60), "yuyv422");
        assert_eq!(choose_input_format(&[], 1280, 720, 30), "auto");
        let nv = parse_dshow_options("  pixel_format=nv12  min s=1280x720 fps=5 max s=1280x720 fps=60\n  pixel_format=yuyv422  min s=1280x720 fps=5 max s=1280x720 fps=60");
        assert_eq!(choose_input_format(&nv, 1280, 720, 30), "nv12");
    }
    #[test]
    fn failure_classes() {
        let a = "[dshow @ 01] Could not find audio only device with name [Digital Audio Interface (USB3.0 Audio)] among source devices of type video. | video=USB3.0 Video:audio=Digital Audio Interface (USB3.0 Audio): I/O error";
        assert_eq!(classify_failure(a), Failure::AudioOpen);
        assert_eq!(classify_failure("Could not set video options | video=USB3.0 Video: I/O error"), Failure::FormatUnsupported);
        assert_eq!(classify_failure("I/O error"), Failure::Other); // a bare I/O error is NOT 'busy'
        assert_eq!(classify_failure("The device is busy"), Failure::Busy);
        assert_eq!(classify_failure("Could not run graph (sometimes caused by a device already in use by other application)"), Failure::Other);
        assert_eq!(classify_failure("Could not find video device with name [X] among source devices of type video."), Failure::VideoNotFound);
        assert!(failure_hint(Failure::Busy, true).contains("Close OBS. It's using the capture card"));
        assert!(!failure_hint(Failure::Other, false).contains("busy"));
    }
}
