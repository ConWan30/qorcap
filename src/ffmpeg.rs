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
    pick_audio_device(video, want, devs).map(|d| d.name)
}

/// Like [`pick_audio`] but returns the enumerated device (with its DirectShow alternative name).
/// Only devices that ffmpeg listed as *audio* are returned for `auto`; an explicit name is matched
/// case-insensitively against the audio list (exact, then substring) so a slightly different spelling
/// still resolves to the real enumerated name. An explicit name not in the list is passed through.
pub fn pick_audio_device(video: &str, want: &str, devs: &[Device]) -> Option<Device> {
    let audio: Vec<&Device> = devs.iter().filter(|d| d.kind == "audio").collect();
    match want {
        "none" | "off" => None,
        "auto" => {
            let stem = video.split_whitespace().next().unwrap_or(video).to_lowercase(); // "usb3.0"
            audio.iter().find(|d| d.name.to_lowercase().contains(&video.to_lowercase()))
                .or_else(|| audio.iter().find(|d| d.name.to_lowercase().contains(&stem)))
                .or_else(|| audio.iter().find(|d| { let n = d.name.to_lowercase(); n.contains("capture") || n.contains("hdmi") || n.contains("digital audio interface") }))
                .map(|d| (*d).clone())
        }
        n => {
            let l = n.to_lowercase();
            audio.iter().find(|d| d.name == n)
                .or_else(|| audio.iter().find(|d| d.name.to_lowercase() == l))
                .copied()
                .or_else(|| {
                    // unambiguous substring only (e.g. "USB3.0 Audio" -> "Digital Audio Interface (USB3.0 Audio)")
                    let c: Vec<&&Device> = audio.iter().filter(|d| { let dn = d.name.to_lowercase(); dn.contains(&l) || l.contains(&dn) }).collect();
                    if l.len() >= 6 && c.len() == 1 { Some(*c[0]) } else { None }
                })
                .cloned()
                .or_else(|| Some(Device { name: n.to_string(), kind: "audio".into(), alt: None }))
        }
    }
}

/// How the card's audio is opened. Tried in order; after the last one fails QorCap records video-only.
/// Many USB capture cards (MS2130 "USB3.0 Video" + "Digital Audio Interface (USB3.0 Audio)") fail pin
/// negotiation when the audio endpoint is opened as a second, independent dshow graph
/// ("Could not find output pin from audio only capture device"), but work as one graph.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AudioStrategy {
    /// `-f dshow -i video=V` + `-f dshow -i audio=A` (default; best sync control)
    Separate,
    /// one dshow graph: `-i video=V:audio=A`
    Combined,
    /// second input by DirectShow alternative name (`@device_cm_...`), default buffer size
    SeparateAlt,
}
pub const AUDIO_STRATEGIES: [AudioStrategy; 3] = [AudioStrategy::Separate, AudioStrategy::Combined, AudioStrategy::SeparateAlt];

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AudioSpec { pub name: String, pub alt: Option<String>, pub strategy: AudioStrategy }

/// Strategies worth trying for this device (SeparateAlt only if an alternative name is known).
pub fn audio_strategies(d: &Device) -> Vec<AudioStrategy> {
    AUDIO_STRATEGIES.iter().copied().filter(|s| *s != AudioStrategy::SeparateAlt || d.alt.is_some()).collect()
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
/// exact size at the requested fps (nv12, then yuyv422, then any), otherwise mjpeg. Returns "auto"
/// (let DirectShow pick) if nothing matches.
///
/// A 2026-10-19 teardown of the MS2131 lists YUY2 at 1280x720@60, so `auto` would pick `yuyv422`
/// for that listing. Start Match still passes `--input-format mjpeg`, and this preference order
/// is unchanged. The opt-in `--yuy2-720p60` flag is the measured path.
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
pub enum Failure { AudioOpen, VideoNotFound, FormatUnsupported, Busy, Snapshot, Other }

pub fn classify_failure(tail: &str) -> Failure {
    let t = tail.to_lowercase();
    // Snapshot/image2 path gone (TEMP cleared) — must not burn reopen budget when video is fine.
    if (t.contains("snapshot") || t.contains("image2") || t.contains(".jpg") || t.contains(".png"))
        && (t.contains("no such file") || t.contains("error opening output") || t.contains("permission denied") || t.contains("failed to open"))
    {
        return Failure::Snapshot;
    }
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
        Failure::Snapshot => " (snapshot path missing/unwritable; recreating — video capture continues)".into(),
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
    let spec = audio.map(|n| AudioSpec { name: n.to_string(), alt: None, strategy: AudioStrategy::Separate });
    capture_args_with(a, input_format, spec.as_ref(), snapshot, stamp_file)
}

pub fn capture_args_with(a: &RecordArgs, input_format: &str, audio: Option<&AudioSpec>, snapshot: &str, stamp_file: Option<&str>) -> Vec<String> {
    let (w, h) = a.res;
    let fps = a.fps.to_string();
    let mut v: Vec<String> = ["-hide_banner", "-nostdin", "-loglevel", "warning", "-stats_period", "1", "-progress", "pipe:2"]
        .iter().map(|s| s.to_string()).collect();
    let push = |v: &mut Vec<String>, xs: &[&str]| v.extend(xs.iter().map(|s| s.to_string()));
    let has_audio;
    let mut audio_idx = "1:a";
    if a.device == "testpattern" {
        push(&mut v, &["-re", "-f", "lavfi", "-i", &format!("testsrc2=size={w}x{h}:rate={fps}")]);
        if audio.is_some() { push(&mut v, &["-re", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]); }
        has_audio = audio.is_some();
    } else {
        let mut vin: Vec<&str> = vec!["-f", "dshow", "-rtbufsize"];
        let rtbuf = a.rtbufsize.as_str();
        vin.push(rtbuf);
        if a.short_probe { vin.extend(["-probesize", "32", "-analyzeduration", "0"]); }
        vin.extend(["-thread_queue_size", "1024", "-use_wallclock_as_timestamps", "1", "-video_size"]);
        let size = format!("{w}x{h}");
        vin.push(&size);
        vin.extend(["-framerate", &fps]);
        push(&mut v, &vin);
        match input_format {
            "auto" | "" => {}
            "mjpeg" => push(&mut v, &["-vcodec", "mjpeg"]),
            pf => push(&mut v, &["-pixel_format", pf]),
        }
        match audio {
            Some(au) if au.strategy == AudioStrategy::Combined => {
                push(&mut v, &["-i", &format!("video={}:audio={}", a.device, au.name)]);
                audio_idx = "0:a";
            }
            Some(au) => {
                push(&mut v, &["-i", &format!("video={}", a.device)]);
                let mut ain: Vec<&str> = vec!["-f", "dshow"];
                let audio_rt = a.audio_rtbufsize.clone().unwrap_or_default();
                if a.audio_rtbufsize.is_some() { ain.extend(["-rtbufsize", audio_rt.as_str()]); }
                if a.short_probe { ain.extend(["-probesize", "32", "-analyzeduration", "0"]); }
                ain.extend(["-thread_queue_size", "1024", "-use_wallclock_as_timestamps", "1"]);
                push(&mut v, &ain);
                let name = match (au.strategy, &au.alt) {
                    (AudioStrategy::SeparateAlt, Some(alt)) => alt.clone(),
                    _ => { push(&mut v, &["-audio_buffer_size", "50"]); au.name.clone() }
                };
                push(&mut v, &["-i", &format!("audio={name}")]);
            }
            None => push(&mut v, &["-i", &format!("video={}", a.device)]),
        }
        has_audio = audio.is_some();
    }
    push(&mut v, &["-map", "0:v"]);
    if has_audio { push(&mut v, &["-map", audio_idx]); }
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
    // Snapshot output (4 fps). `q3` is the historical JPEG and keeps `-atomic_writing 1`
    // so the default command stays byte-for-byte. hq is the same pattern at q:v 2.
    // PNG does not use atomic rename: on Windows, MoveFileEx cannot replace snapshot.png
    // while GET /snapshot.jpg has it open, and the 2026-10-20 reader_png take logged
    // "failed to rename ...snapshot.png.tmp to ...snapshot.png: Operation not permitted".
    // The PNG is written in place; the API only serves a buffer that ends with IEND.
    push(&mut v, &["-map", "0:v", "-vf", "fps=4"]);
    match a.reader_snapshot.as_str() {
        "hq" => push(&mut v, &["-q:v", "2", "-f", "image2", "-update", "1", "-atomic_writing", "1", "-y", snapshot]),
        "png" => push(&mut v, &["-c:v", "png", "-compression_level", "1", "-f", "image2", "-update", "1", "-y", snapshot]),
        _ => push(&mut v, &["-q:v", "3", "-f", "image2", "-update", "1", "-atomic_writing", "1", "-y", snapshot]),
    }
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
        // explicit names resolve to the enumerated audio device (case-insensitive / substring)
        assert_eq!(pick_audio("X", "digital audio interface (usb3.0 audio)", &d).as_deref(), Some("Digital Audio Interface (USB3.0 Audio)"));
        assert_eq!(pick_audio("X", "USB3.0 Audio", &d).as_deref(), Some("Digital Audio Interface (USB3.0 Audio)"));
        // never pick a video device as audio
        assert_eq!(pick_audio("Integrated Camera", "auto", &d[..2]), None);
        let dev = pick_audio_device("USB3.0 Video", "auto", &d).unwrap();
        assert_eq!(dev.alt.as_deref(), Some(r"@device_cm_{33D9A762}\wave_{X}"));
        assert_eq!(audio_strategies(&dev), vec![AudioStrategy::Separate, AudioStrategy::Combined, AudioStrategy::SeparateAlt]);
    }
    #[test]
    fn audio_strategy_args() {
        use clap::Parser;
        let c = crate::cli::Cli::try_parse_from(["q", "record"]).unwrap();
        let crate::cli::Cmd::Record(r) = c.cmd else { panic!() };
        let mk = |s| AudioSpec { name: "Digital Audio Interface (USB3.0 Audio)".into(), alt: Some("@device_cm_X".into()), strategy: s };
        let comb = capture_args_with(&r, "mjpeg", Some(&mk(AudioStrategy::Combined)), "s.jpg", None).join(" ");
        assert!(comb.contains("-i video=USB3.0 Video:audio=Digital Audio Interface (USB3.0 Audio)"), "{comb}");
        assert!(comb.contains("-map 0:v -map 0:a") && comb.contains("-c:a aac"), "{comb}");
        assert_eq!(comb.matches("-f dshow").count(), 1);
        let alt = capture_args_with(&r, "mjpeg", Some(&mk(AudioStrategy::SeparateAlt)), "s.jpg", None).join(" ");
        assert!(alt.contains("-i audio=@device_cm_X") && !alt.contains("-audio_buffer_size") && alt.contains("-map 1:a"), "{alt}");
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
        assert_eq!(classify_failure("Could not open file : C:\\Users\\x\\AppData\\Local\\Temp\\qorcap-1\\snapshot.jpg: No such file or directory"), Failure::Snapshot);
        assert_eq!(classify_failure("Error opening output file snapshot.jpg."), Failure::Snapshot);
        assert_eq!(classify_failure("Could not open file : C:\\t\\snapshot.png: No such file or directory"), Failure::Snapshot);
        assert!(failure_hint(Failure::Busy, true).contains("Close OBS. It's using the capture card"));
        assert!(!failure_hint(Failure::Other, false).contains("busy"));
    }

    /// Today's capture command, flags off, input mjpeg (what Start Match passes). Byte-for-byte.
    const GOLDEN_MJPEG_720P30: &str = "-hide_banner -nostdin -loglevel warning -stats_period 1 -progress pipe:2 -f dshow -rtbufsize 512M -thread_queue_size 1024 -use_wallclock_as_timestamps 1 -video_size 1280x720 -framerate 30 -vcodec mjpeg -i video=USB3.0 Video -f dshow -thread_queue_size 1024 -use_wallclock_as_timestamps 1 -audio_buffer_size 50 -i audio=Digital Audio Interface (USB3.0 Audio) -map 0:v -map 1:a -vf fps=30,format=nv12 -fps_mode cfr -r 30 -c:v h264_mf -hw_encoding 1 -rate_control cbr -scenario live_streaming -b:v 8M -maxrate 8M -bufsize 8M -g 30 -bf 0 -c:a aac -b:a 160k -ar 48000 -af aresample=async=1000:first_pts=0 -f mpegts -mpegts_flags +resend_headers -pat_period 0.2 -muxdelay 0 pipe:1 -map 0:v -vf fps=4 -q:v 3 -f image2 -update 1 -atomic_writing 1 -y snap.jpg";

    fn record(extra: &[&str]) -> crate::cli::RecordArgs {
        use clap::Parser;
        let mut a = vec!["q", "record"];
        a.extend_from_slice(extra);
        let crate::cli::Cmd::Record(r) = crate::cli::Cli::try_parse_from(a).unwrap().cmd else { panic!() };
        r
    }
    fn joined(r: &crate::cli::RecordArgs, fmt: &str, snap: &str) -> String {
        capture_args(r, fmt, Some("Digital Audio Interface (USB3.0 Audio)"), snap, None).join(" ")
    }
    fn until_pipe(s: &str) -> &str { s.split(" pipe:1 ").next().unwrap_or(s) }
    fn from_encoder(s: &str) -> &str {
        let i = s.find("-c:v ").unwrap();
        let j = s.find(" pipe:1 ").unwrap();
        &s[i..j + " pipe:1".len()]
    }

    #[test]
    fn default_capture_command_is_byte_for_byte_todays() {
        let r = record(&[]);
        assert_eq!(joined(&r, "mjpeg", "snap.jpg"), GOLDEN_MJPEG_720P30);
        let mut tuned = r.clone();
        crate::capture_tune::apply_capture_profile(&mut tuned);
        assert_eq!(joined(&tuned, "mjpeg", "snap.jpg"), GOLDEN_MJPEG_720P30);
    }

    #[test]
    fn opt_in_flags_only_change_their_own_args() {
        let base = joined(&record(&[]), "mjpeg", "snap.jpg");
        let enc = from_encoder(&base);
        let hq = joined(&record(&["--reader-snapshot", "hq"]), "mjpeg", "snap.jpg");
        assert_eq!(until_pipe(&hq), until_pipe(&base));
        assert!(hq.contains("-q:v 2") && !hq.contains("-q:v 3"), "{hq}");
        let png = joined(&record(&["--reader-snapshot", "png"]), "mjpeg", "snap.png");
        assert_eq!(until_pipe(&png), until_pipe(&base));
        assert!(png.contains("-c:v png -compression_level 1") && png.ends_with("-y snap.png"), "{png}");
        assert!(!png.contains("atomic_writing"), "{png}");
        assert_eq!(from_encoder(&hq), enc);
        assert_eq!(from_encoder(&png), enc);
        let small = joined(&record(&["--rtbufsize", "128M"]), "mjpeg", "snap.jpg");
        assert!(small.contains("-rtbufsize 128M") && !small.contains("-rtbufsize 512M"), "{small}");
        assert_eq!(from_encoder(&small), enc);
        let probe = joined(&record(&["--short-probe"]), "mjpeg", "snap.jpg");
        assert_eq!(probe.matches("-probesize 32 -analyzeduration 0").count(), 2, "{probe}");
        assert_eq!(from_encoder(&probe), enc);
        let art = joined(&record(&["--audio-rtbufsize", "512K"]), "mjpeg", "snap.jpg");
        assert!(art.contains("-f dshow -rtbufsize 512K -thread_queue_size 1024"), "{art}");
        assert!(art.contains("-rtbufsize 512M"), "{art}");
        assert_eq!(from_encoder(&art), enc);
        let mut yuy = record(&["--yuy2-720p60"]);
        crate::capture_tune::apply_capture_profile(&mut yuy);
        let y = capture_args(&yuy, &yuy.input_format, Some("Digital Audio Interface (USB3.0 Audio)"), "snap.jpg", None).join(" ");
        assert!(y.contains("-pixel_format yuyv422") && y.contains("-video_size 1280x720 -framerate 60") && y.contains("-r 60") && y.contains("-g 60"), "{y}");
        assert!(y.contains("-c:v h264_mf -hw_encoding 1 -rate_control cbr"), "{y}");
    }

    #[test]
    fn auto_picks_yuy2_at_720p_when_the_card_lists_it() {
        // The unit fixture above still models the older "raw only at 1080p" listing.
        // The 2026-10-19 teardown lists YUY2 at 720p60. `auto` already prefers that raw mode.
        // Start Match does not use auto; it passes mjpeg. This test locks the existing preference.
        let listed = "  vcodec=mjpeg  min s=1280x720 fps=10 max s=1280x720 fps=60\n  pixel_format=yuyv422  min s=1280x720 fps=10 max s=1280x720 fps=60.0002\n  pixel_format=yuyv422  min s=1920x1080 fps=10 max s=1920x1080 fps=60\n";
        let m = parse_dshow_options(listed);
        assert_eq!(choose_input_format(&m, 1280, 720, 60), "yuyv422");
        assert_eq!(choose_input_format(&m, 1280, 720, 30), "yuyv422");
    }
}
