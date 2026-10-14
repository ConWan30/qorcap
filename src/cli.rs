use clap::{Parser, Subcommand};

pub const DEFAULT_PORT: u16 = 8788;

#[derive(Parser, Debug)]
#[command(name = "qorcap", version, about = "Lean self-healing USB capture recorder")]
pub struct Cli {
    /// Control API port on 127.0.0.1
    #[arg(long, global = true, default_value_t = DEFAULT_PORT)]
    pub port: u16,
    /// Path to ffmpeg.exe (default: next to qorcap.exe, then PATH)
    #[arg(long, global = true)]
    pub ffmpeg: Option<String>,
    /// Log file (default: qorcap.log next to the exe / cwd)
    #[arg(long, global = true)]
    pub log: Option<String>,
    #[command(subcommand)]
    pub cmd: Cmd,
}

#[derive(Subcommand, Debug, Clone)]
pub enum Cmd {
    /// List DirectShow video and audio capture devices
    Devices,
    /// Capture from a device; record to file and/or keep a replay buffer
    Record(RecordArgs),
    /// Ask the running recorder to save its replay buffer
    SaveReplay {
        /// Output path (default: replay-<timestamp>.mp4 in the replay dir)
        #[arg(long)]
        out: Option<String>,
    },
    /// Stop the running recorder (finalises files and exits)
    Stop,
    /// Print the running recorder's status JSON
    Status,
    /// Re-hash a match bundle's video against its manifest and check the checkpoint chain
    VerifyBundle { dir: String },
}

#[derive(clap::Args, Debug, Clone, PartialEq)]
pub struct RecordArgs {
    /// Video device name (exact DirectShow friendly name), or `testpattern`
    #[arg(long, default_value = "USB3.0 Video")]
    pub device: String,
    /// Audio device name; `auto` picks the card's audio, `none` disables
    #[arg(long, default_value = "auto")]
    pub audio: String,
    #[arg(long, default_value = "1280x720", value_parser = parse_res)]
    pub res: (u32, u32),
    #[arg(long, default_value_t = 30, value_parser = clap::value_parser!(u32).range(1..=240))]
    pub fps: u32,
    /// Output MP4 (fragmented, crash-safe). Omit for replay-buffer-only mode.
    #[arg(long)]
    pub out: Option<String>,
    /// Stop recording after N seconds
    #[arg(long)]
    pub duration: Option<u64>,
    /// Keep the last SECS seconds in RAM for `save-replay`
    #[arg(long)]
    pub replay: Option<u64>,
    /// Directory for saved replays
    #[arg(long, default_value = "replays")]
    pub replay_dir: String,
    /// Video encoder: h264_mf (Media Foundation HW), h264_nvenc, h264_qsv, h264_amf, libx264
    #[arg(long, default_value = "h264_mf")]
    pub encoder: String,
    /// Video bitrate, e.g. 8M
    #[arg(long, default_value = "8M")]
    pub bitrate: String,
    /// Input pixel format / codec from the card: auto, mjpeg, yuyv422, nv12
    #[arg(long, default_value = "auto")]
    pub input_format: String,
    /// Seconds without data before the device is considered frozen and reopened
    #[arg(long, default_value_t = 4)]
    pub watchdog: u64,
    /// QorTime match bundle folder (video.mp4, manifest.jsonl, checkpoints.jsonl, clips/, receipt.draft.json)
    #[arg(long)]
    pub bundle: Option<String>,
    /// QorTime match id (issued by the referee); recorded in the draft receipt
    #[arg(long, default_value = "unassigned")]
    pub match_id: String,
    /// This console's QorTime role
    #[arg(long, default_value = "home", value_parser = ["home", "away"])]
    pub role: String,
    #[arg(long, default_value = "local:player")]
    pub player_id: String,
    /// Seconds saved for each automatic score-change clip
    #[arg(long, default_value_t = 20)]
    pub clip_secs: u64,
    /// Burn the active referee nonce/pattern into the recorded video (needs ffmpeg drawtext)
    #[arg(long)]
    pub stamp: bool,
}

pub fn parse_res(s: &str) -> Result<(u32, u32), String> {
    let (w, h) = s
        .to_lowercase()
        .split_once('x')
        .map(|(a, b)| (a.trim().to_string(), b.trim().to_string()))
        .ok_or_else(|| format!("resolution must look like 1280x720, got `{s}`"))?;
    let w: u32 = w.parse().map_err(|_| format!("bad width in `{s}`"))?;
    let h: u32 = h.parse().map_err(|_| format!("bad height in `{s}`"))?;
    if w < 16 || h < 16 || w > 7680 || h > 4320 || w % 2 != 0 || h % 2 != 0 {
        return Err(format!("resolution `{s}` out of range (even, 16..7680x4320)"));
    }
    Ok((w, h))
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::Parser;

    #[test]
    fn parses_record_full() {
        let c = Cli::try_parse_from([
            "qorcap", "record", "--device", "USB3.0 Video", "--res", "1920x1080", "--fps", "60",
            "--out", "a.mp4", "--duration", "10", "--replay", "30",
        ])
        .unwrap();
        match c.cmd {
            Cmd::Record(r) => {
                assert_eq!(r.device, "USB3.0 Video");
                assert_eq!(r.res, (1920, 1080));
                assert_eq!(r.fps, 60);
                assert_eq!(r.out.as_deref(), Some("a.mp4"));
                assert_eq!(r.duration, Some(10));
                assert_eq!(r.replay, Some(30));
                assert_eq!(r.encoder, "h264_mf");
            }
            _ => panic!(),
        }
        assert_eq!(c.port, DEFAULT_PORT);
    }
    #[test]
    fn defaults_and_subcommands() {
        let c = Cli::try_parse_from(["qorcap", "--port", "9000", "record"]).unwrap();
        assert_eq!(c.port, 9000);
        if let Cmd::Record(r) = c.cmd { assert_eq!(r.res, (1280, 720)); assert_eq!(r.fps, 30); assert!(r.out.is_none()); } else { panic!() }
        assert!(matches!(Cli::try_parse_from(["qorcap", "devices"]).unwrap().cmd, Cmd::Devices));
        assert!(matches!(Cli::try_parse_from(["qorcap", "stop"]).unwrap().cmd, Cmd::Stop));
        assert!(matches!(Cli::try_parse_from(["qorcap", "save-replay"]).unwrap().cmd, Cmd::SaveReplay { out: None }));
    }
    #[test]
    fn rejects_bad_input() {
        assert!(Cli::try_parse_from(["qorcap", "record", "--res", "720p"]).is_err());
        assert!(Cli::try_parse_from(["qorcap", "record", "--fps", "0"]).is_err());
        assert!(parse_res("1281x720").is_err());
        assert_eq!(parse_res("640X480").unwrap(), (640, 480));
    }
}
