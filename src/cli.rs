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
    /// Offline: run QorTime's scorebug reader over a recorded bundle's video.mp4, write score
    /// checkpoints + final detection and sign receipt.json (self-attested local referee).
    /// Runs sidecar/qorcap_match.py finalize (needs Python with numpy, opencv, cryptography).
    Finalize {
        /// Match bundle folder (contains video.mp4). Default: the newest folder in Documents/QorCap Matches
        bundle: Option<String>,
        /// Python interpreter (default: QorCap's venv, then py/python3/python)
        #[arg(long)]
        python: Option<String>,
        /// Rebuild receipt.draft.json and bundle.json from the on-disk hash chain, checkpoints,
        /// inputs, challenges and timeline. Video bytes after the last hashed segment are excluded.
        #[arg(long)]
        recover: bool,
        /// Extra arguments passed to the finalizer (e.g. --qortime DIR)
        #[arg(last = true)]
        rest: Vec<String>,
    },
    /// Submit for Verification: send a signed match bundle to the QorTime official referee, which re-checks it
    /// independently (hash chains, video bytes, end-of-game re-read, challenges) and countersigns with the
    /// official key. Spot-check by default (init + recorded tail + random segments); --full uploads every segment.
    Submit {
        /// Match bundle folder (contains receipt.json). Default: the newest folder in Documents/QorCap Matches
        bundle: Option<String>,
        /// Python interpreter (default: QorCap's venv, then py/python3/python)
        #[arg(long)]
        python: Option<String>,
        /// Upload every video segment (1-3 GB) instead of the referee's spot-check sample
        #[arg(long)]
        full: bool,
        /// Referee service URL (default: the official QorTime referee)
        #[arg(long)]
        referee: Option<String>,
        /// Do not ask before uploading
        #[arg(long)]
        yes: bool,
    },
    /// Highlight clips (off by default, post-match only): cut a short clip around each locked score change of a
    /// FINISHED bundle. The burned-in panel says SCOREBUG LOCK (a scorebug read; a referee VERIFIED seal is not
    /// known when the clip is cut) plus a .vtt sidecar and highlights/manifest.json linking every clip
    /// to its checkpoint lock and the receipt hash. Refuses while the bundle is still being recorded.
    /// Runs sidecar/qorcap_highlights.py (needs Python + ffmpeg).
    Highlights {
        /// Match bundle folder (contains receipt.json). Default: the newest folder in Documents/QorCap Matches
        bundle: Option<String>,
        /// Python interpreter (default: QorCap's venv, then py/python3/python)
        #[arg(long)]
        python: Option<String>,
        /// Seconds of video before each lock
        #[arg(long, default_value_t = 8.0)]
        pre: f64,
        /// Seconds of video after each lock
        #[arg(long, default_value_t = 4.0)]
        post: f64,
        /// Stream-copy clips; the overlay goes only into the .vtt sidecar
        #[arg(long)]
        no_burn: bool,
        /// Check an existing highlights/manifest.json against the bundle instead of cutting clips
        #[arg(long)]
        check: Option<String>,
    },
    /// On-screen PSN names (post-match only): read the PSN names Madden shows in an ONLINE game (player banners at the top,
    /// pause/end-of-game menu cards) from the recorded video and write psn_names.json. On-screen evidence, not account
    /// proof; unreadable characters fail closed. 'finalize' then adds them to the receipt as match.observed_handles.
    /// Refuses while the bundle is still being recorded. Runs sidecar/qorcap_psn.py (needs Python + ffmpeg).
    PsnNames {
        /// Match bundle folder. Default: the newest folder in Documents/QorCap Matches
        bundle: Option<String>,
        /// Python interpreter (default: QorCap's venv, then py/python3/python)
        #[arg(long)]
        python: Option<String>,
        /// Frames to sample across the match
        #[arg(long, default_value_t = 60)]
        samples: u32,
    },
    /// Live controller check: confirms the laptop is reading the DualSense over USB HID and shows presses
    InputTest {
        /// Stop after N seconds (default: until Ctrl+C)
        #[arg(long)]
        secs: Option<u64>,
    },
    /// (Re)compute a bundle's controller-input proof: human-likeness, input-video match rate and offset;
    /// writes input_proof.json and adds signals.input to receipt.draft.json
    InputProof {
        dir: String,
        /// A screen change must follow a key press within this many frames (30 fps)
        #[arg(long, default_value_t = 15)]
        frames: usize,
        /// Skip frame differencing (no ffmpeg)
        #[arg(long)]
        no_video: bool,
        /// Run even though a recorder still seems to be writing this bundle (result gets a warning)
        #[arg(long)]
        force: bool,
    },
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
    /// Opt-in: capture YUY2 1280x720 at 60 fps (forces res, fps, and input format). Off keeps today's settings.
    #[arg(long, default_value_t = false)]
    pub yuy2_720p60: bool,
    /// Opt-in: at startup, read the USB link and the card's mode list and warn if raw 720p60 cannot fit
    /// (USB 2, or the mode is not listed). Does not change the capture command.
    #[arg(long, default_value_t = false)]
    pub check_usb_link: bool,
    /// Live scorebug snapshot only. The recording and the receipt are unchanged.
    /// `q3` is today's JPEG. `hq` is JPEG q:v 2. `png` is a lossless PNG served at the same /snapshot.jpg URL.
    #[arg(long, default_value = "q3", value_parser = ["q3", "hq", "png"])]
    pub reader_snapshot: String,
    /// dshow real-time buffer for the video input. 512M is today's value.
    #[arg(long, default_value = "512M")]
    pub rtbufsize: String,
    /// Opt-in: set -rtbufsize on the separate audio input. Unset keeps ffmpeg's default (~3 MB).
    #[arg(long)]
    pub audio_rtbufsize: Option<String>,
    /// Opt-in: -probesize 32 -analyzeduration 0 on each dshow input.
    #[arg(long, default_value_t = false)]
    pub short_probe: bool,
    /// Opt-in: count persistent "real-time buffer too full" lines and put the lag on /status.
    #[arg(long, default_value_t = false)]
    pub buffer_watch: bool,
    /// Opt-in: restart the capture child when buffer-full warnings stay persistent. Implies --buffer-watch.
    #[arg(long, default_value_t = false)]
    pub buffer_restart: bool,
    /// Opt-in: warn when snapshots stay black (the card streams black with no HDMI picture).
    #[arg(long, default_value_t = false)]
    pub no_signal: bool,
    /// Black samples required before --no-signal warns. The watcher samples about twice a second.
    #[arg(long, default_value_t = 2)]
    pub no_signal_secs: u64,
    /// Opt-in: bound the wait after killing ffmpeg, and wait 1.5s after the card reappears before reopening.
    #[arg(long, default_value_t = false)]
    pub replug_resilient: bool,
    /// Seconds without data before the device is considered frozen and reopened
    #[arg(long, default_value_t = 4)]
    pub watchdog: u64,
    /// At stop: seconds to wait for the local referee (Start Match) to countersign the final checkpoints
    /// before the input proof runs. No referee attached -> no wait.
    #[arg(long, default_value_t = 20)]
    pub countersign_wait: u64,
    /// QorTime match bundle folder (video.mp4, manifest.jsonl, checkpoints.jsonl, clips/, receipt.draft.json)
    #[arg(long)]
    pub bundle: Option<String>,
    /// QorTime match id (issued by the referee); recorded in the draft receipt
    #[arg(long, default_value = "unassigned")]
    pub match_id: String,
    /// This console's QorTime role
    #[arg(long, default_value = "home", value_parser = ["home", "away"])]
    pub role: String,
    /// How the side was chosen (v0.3.26 side pick): prompt | flag | file. Shown in /status and committed in the
    /// start checkpoint and bundle.json. Omitted = not recorded (older launchers, tests).
    #[arg(long, value_parser = ["prompt", "flag", "file"])]
    pub side_source: Option<String>,
    #[arg(long, default_value = "local:player")]
    pub player_id: String,
    /// Seconds saved for each automatic score-change clip
    #[arg(long, default_value_t = 20)]
    pub clip_secs: u64,
    /// Burn the active referee nonce/pattern into the recorded video (needs ffmpeg drawtext)
    #[arg(long)]
    pub stamp: bool,
    /// Refuse to start (and stop cleanly mid-record) when free space is below this many GiB (default 2).
    /// Pass 0 to disable the start-time check.
    #[arg(long, default_value_t = 2.0)]
    pub min_free_gb: f64,
    /// Do not read the DualSense over USB HID (no inputs.jsonl / input proof)
    #[arg(long)]
    pub no_controller: bool,
    /// Opt-in: serve the reader's newest snapshot at GET /frame/scorebug for the advisory Glimpse
    /// shadow watcher (read-only, rate-limited, never touches capture). Also on with QORCAP_GLIMPSE_FRAME=1.
    #[arg(long)]
    pub glimpse_frame: bool,
}

impl RecordArgs {
    /// Bytes required free to start recording (`None` = disabled when min_free_gb == 0).
    pub fn min_free_bytes(&self) -> Option<u64> {
        if self.min_free_gb <= 0.0 { None } else { Some((self.min_free_gb * 1024.0 * 1024.0 * 1024.0) as u64) }
    }
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
        if let Cmd::Record(r) = c.cmd {
            assert_eq!(r.res, (1280, 720));
            assert_eq!(r.fps, 30);
            assert!(r.out.is_none());
            assert!(!r.yuy2_720p60 && !r.check_usb_link && !r.short_probe && !r.buffer_watch && !r.buffer_restart && !r.no_signal && !r.replug_resilient);
            assert_eq!(r.reader_snapshot, "q3");
            assert_eq!(r.rtbufsize, "512M");
            assert!(r.audio_rtbufsize.is_none());
            assert_eq!(r.no_signal_secs, 2);
        } else { panic!() }
        assert!(matches!(Cli::try_parse_from(["qorcap", "devices"]).unwrap().cmd, Cmd::Devices));
        assert!(matches!(Cli::try_parse_from(["qorcap", "stop"]).unwrap().cmd, Cmd::Stop));
        assert!(matches!(Cli::try_parse_from(["qorcap", "save-replay"]).unwrap().cmd, Cmd::SaveReplay { out: None }));
        assert!(matches!(Cli::try_parse_from(["qorcap", "input-test"]).unwrap().cmd, Cmd::InputTest { secs: None }));
        assert!(matches!(Cli::try_parse_from(["qorcap", "submit", "b", "--full"]).unwrap().cmd, Cmd::Submit { full: true, .. }));
        assert!(matches!(Cli::try_parse_from(["qorcap", "submit"]).unwrap().cmd, Cmd::Submit { bundle: None, full: false, yes: false, .. }));
        assert!(matches!(Cli::try_parse_from(["qorcap", "input-proof", "b", "--frames", "10"]).unwrap().cmd, Cmd::InputProof { frames: 10, .. }));
        if let Cmd::Record(r) = Cli::try_parse_from(["qorcap", "record", "--no-controller"]).unwrap().cmd { assert!(r.no_controller); } else { panic!() }
    }
    #[test]
    fn parses_highlights() {
        let c = Cli::try_parse_from(["qorcap", "highlights", "D:/m", "--pre", "6", "--no-burn"]).unwrap();
        match c.cmd {
            Cmd::Highlights { bundle, pre, post, no_burn, check, .. } => {
                assert_eq!(bundle.as_deref(), Some("D:/m"));
                assert_eq!((pre, post, no_burn, check), (6.0, 4.0, true, None));
            }
            _ => panic!(),
        }
    }

    #[test]
    fn parses_psn_names() {
        match Cli::try_parse_from(["qorcap", "psn-names", "D:/m", "--samples", "40"]).unwrap().cmd {
            Cmd::PsnNames { bundle, samples, .. } => assert_eq!((bundle.as_deref(), samples), (Some("D:/m"), 40)),
            _ => panic!(),
        }
        assert!(matches!(Cli::try_parse_from(["qorcap", "psn-names"]).unwrap().cmd, Cmd::PsnNames { bundle: None, samples: 60, .. }));
    }

    #[test]
    fn rejects_bad_input() {
        assert!(Cli::try_parse_from(["qorcap", "record", "--res", "720p"]).is_err());
        assert!(Cli::try_parse_from(["qorcap", "record", "--fps", "0"]).is_err());
        assert!(parse_res("1281x720").is_err());
        assert_eq!(parse_res("640X480").unwrap(), (640, 480));
    }
}
