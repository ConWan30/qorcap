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

/// Arguments for the capture process: device -> CFR H.264/AAC MPEG-TS on stdout,
/// plus a periodically refreshed snapshot JPEG.
/// Escape a path for use inside an ffmpeg filter option value.
pub fn filter_path(p: &str) -> String { p.replace('\\', "/").replace(':', "\\:").replace('\'', "\\'") }

pub fn capture_args(a: &RecordArgs, audio: Option<&str>, snapshot: &str, stamp_file: Option<&str>) -> Vec<String> {
    let (w, h) = a.res;
    let fps = a.fps.to_string();
    let mut v: Vec<String> = ["-hide_banner", "-nostdin", "-loglevel", "warning", "-stats_period", "1", "-progress", "pipe:2"]
        .iter().map(|s| s.to_string()).collect();
    let push = |v: &mut Vec<String>, xs: &[&str]| v.extend(xs.iter().map(|s| s.to_string()));
    let has_audio;
    if a.device == "testpattern" {
        push(&mut v, &["-re", "-f", "lavfi", "-i", &format!("testsrc2=size={w}x{h}:rate={fps}")]);
        push(&mut v, &["-re", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]);
        has_audio = true;
    } else {
        push(&mut v, &["-f", "dshow", "-rtbufsize", "512M", "-video_size", &format!("{w}x{h}"), "-framerate", &fps]);
        match a.input_format.as_str() {
            "auto" => {}
            "mjpeg" => push(&mut v, &["-vcodec", "mjpeg"]),
            pf => push(&mut v, &["-pixel_format", pf]),
        }
        let mut spec = format!("video={}", a.device);
        if let Some(au) = audio { spec.push_str(&format!(":audio={au}")); push(&mut v, &["-audio_buffer_size", "50"]); }
        push(&mut v, &["-i", &spec]);
        has_audio = audio.is_some();
    }
    let ai = if a.device == "testpattern" { "1:a" } else { "0:a" };
    push(&mut v, &["-map", "0:v"]);
    if has_audio { push(&mut v, &["-map", ai]); }
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
    if has_audio { push(&mut v, &["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-af", "aresample=async=1000"]); }
    push(&mut v, &["-f", "mpegts", "-mpegts_flags", "+resend_headers", "-pat_period", "0.2", "-muxdelay", "0", "pipe:1"]);
    // Snapshot output (2 fps, atomic rewrite so readers never see a partial JPEG)
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
        let a = capture_args(&r, Some("USB3.0 Audio"), "snap.jpg", Some(r"C:\t\s.txt")).join(" ");
        assert!(a.contains("-f dshow"));
        assert!(a.contains("-i video=USB3.0 Video:audio=USB3.0 Audio"));
        assert!(a.contains("-fps_mode cfr -r 60"));
        assert!(a.contains("-c:v h264_mf -hw_encoding 1"));
        assert!(a.contains("-g 60"));
        assert!(a.contains(r"drawtext=textfile='C\:/t/s.txt':reload=1"), "{a}");
        assert!(a.contains("-f mpegts"));
        assert!(frag_mp4_args().join(" ").contains("+frag_keyframe+empty_moov"));
    }
}
