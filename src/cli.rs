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
    /// Re-score a bundle's input-to-frame clock with frame_clock.jsonl and without it.
    /// Prints both. Does not write the receipt, the proof, or the hash chain.
    AlignAb {
        dir: String,
        /// A screen change must follow a key press within this many frames (30 fps)
        #[arg(long, default_value_t = 15)]
        frames: usize,
        /// Clock residuals only (no video decode)
        #[arg(long)]
        no_video: bool,
    },
    /// Cut one range of a match bundle on the match clock (`qorcap clip export`).
    /// Content Credentials are off unless `--content-credentials` or `QORCAP_CONTENT_CREDENTIALS` is set.
    Clip {
        #[command(subcommand)]
        clip: ClipCmd,
    },
    /// Read a clip's Content Credentials, check the C2PA signature, and (with `--bundle`) recompute
    /// the receipt and segment hashes. Exit 0 pass, 1 fail, 2 the clip could not be opened.
    #[command(name = "clip-verify")]
    ClipVerify {
        /// Credentialed or plain clip (MP4)
        clip: String,
        /// Match bundle the clip claims to come from. Without it, hash checks fail closed.
        #[arg(long)]
        bundle: Option<String>,
        /// QorTime checkout (directory that contains qor_receipt/). Also $QORTIME.
        #[arg(long)]
        qortime: Option<String>,
        /// Python interpreter used to run `python -m qor_receipt.cli verify`
        #[arg(long)]
        python: Option<String>,
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
    /// Adversarial bundles from the in-tree fixtures. Prints which current check catches each attack.
    /// Does not change a verification rule. Exit 0: a MISSED row is a result, not a command failure.
    Redteam {
        /// Write the matrix JSON here
        #[arg(long)]
        out: Option<String>,
        /// Write docs/REDTEAM.md here
        #[arg(long)]
        markdown: Option<String>,
    },
}

#[derive(Subcommand, Debug, Clone)]
pub enum ClipCmd {
    /// Cut `[start, end)` on the match clock out of the bundle's video.
    /// With credentials off, the file is the ffmpeg cut and nothing else.
    Export {
        /// Match bundle folder (video.mp4, manifest.jsonl, and receipt.json when signing)
        #[arg(long)]
        bundle: String,
        /// First millisecond of the clip on the match's monotonic clock (inclusive)
        #[arg(long)]
        start_mono_ms: u64,
        /// End millisecond on that clock (exclusive)
        #[arg(long)]
        end_mono_ms: u64,
        /// Output MP4
        #[arg(long)]
        out: String,
        /// Embed a C2PA manifest bound to the signed receipt. Default off.
        /// Also on when QORCAP_CONTENT_CREDENTIALS is 1, true, yes, or on.
        /// Off: the output is the ffmpeg cut, byte for byte.
        #[arg(long)]
        content_credentials: bool,
        /// QorTime checkout (directory that contains qor_receipt/). Also $QORTIME.
        #[arg(long)]
        qortime: Option<String>,
        /// Python interpreter used to run `python -m qor_receipt.cli verify`
        #[arg(long)]
        python: Option<String>,
    },
    /// Sign an existing MP4 (a highlight clip) in place. Does not re-cut it.
    /// Refuses, and leaves the file unchanged, when the receipt does not verify
    /// or while the bundle is still being recorded.
    /// This command does not follow `QORCAP_CONTENT_CREDENTIALS`. `qorcap record`
    /// and `clip export` stay default-off.
    Sign {
        /// Match bundle folder (receipt.json, manifest.jsonl, video)
        #[arg(long)]
        bundle: String,
        /// MP4 to sign in place
        #[arg(long)]
        clip: String,
        /// First millisecond of the source span on the match clock (inclusive)
        #[arg(long)]
        start_mono_ms: u64,
        /// End millisecond on that clock (exclusive)
        #[arg(long)]
        end_mono_ms: u64,
        /// The clip was stream-copied. The default is a re-encode (a burned-in highlight).
        #[arg(long)]
        repackaged: bool,
        /// QorTime checkout (directory that contains qor_receipt/). Also $QORTIME.
        #[arg(long)]
        qortime: Option<String>,
        /// Python interpreter used to run `python -m qor_receipt.cli verify`
        #[arg(long)]
        python: Option<String>,
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
    /// Opt-in per-frame capture clock (default off). Also on with QORCAP_FRAME_CLOCK=1.
    /// Writes frame_clock.jsonl beside the bundle: the QorCap monotonic time when each video frame's
    /// MPEG-TS packet was read. Not in the hash chain or the receipt. Input-to-frame alignment uses
    /// it only when the file is present; with the flag off the capture command and the proof are unchanged.
    /// Does not change scoring, signing, or verification. Promote only after a real-game A/B
    /// (`qorcap align-ab`, docs/FRAME_CLOCK_AB.md).
    #[arg(long)]
    pub frame_clock: bool,
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
        if let Cmd::Record(r) = c.cmd { assert_eq!(r.res, (1280, 720)); assert_eq!(r.fps, 30); assert!(r.out.is_none()); } else { panic!() }
        assert!(matches!(Cli::try_parse_from(["qorcap", "devices"]).unwrap().cmd, Cmd::Devices));
        assert!(matches!(Cli::try_parse_from(["qorcap", "stop"]).unwrap().cmd, Cmd::Stop));
        assert!(matches!(Cli::try_parse_from(["qorcap", "save-replay"]).unwrap().cmd, Cmd::SaveReplay { out: None }));
        assert!(matches!(Cli::try_parse_from(["qorcap", "input-test"]).unwrap().cmd, Cmd::InputTest { secs: None }));
        assert!(matches!(Cli::try_parse_from(["qorcap", "submit", "b", "--full"]).unwrap().cmd, Cmd::Submit { full: true, .. }));
        assert!(matches!(Cli::try_parse_from(["qorcap", "submit"]).unwrap().cmd, Cmd::Submit { bundle: None, full: false, yes: false, .. }));
        assert!(matches!(Cli::try_parse_from(["qorcap", "input-proof", "b", "--frames", "10"]).unwrap().cmd, Cmd::InputProof { frames: 10, .. }));
        if let Cmd::Record(r) = Cli::try_parse_from(["qorcap", "record", "--no-controller"]).unwrap().cmd { assert!(r.no_controller); assert!(!r.frame_clock); } else { panic!() }
        if let Cmd::Record(r) = Cli::try_parse_from(["qorcap", "record", "--frame-clock"]).unwrap().cmd { assert!(r.frame_clock); } else { panic!() }
        assert!(matches!(Cli::try_parse_from(["qorcap", "align-ab", "b", "--no-video"]).unwrap().cmd, Cmd::AlignAb { no_video: true, frames: 15, .. }));
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
    fn parses_clip_export_with_credentials_off() {
        match Cli::try_parse_from([
            "qorcap", "clip", "export", "--bundle", "D:/m", "--start-mono-ms", "0",
            "--end-mono-ms", "1000", "--out", "c.mp4",
        ]).unwrap().cmd {
            Cmd::Clip { clip: ClipCmd::Export { bundle, start_mono_ms, end_mono_ms, out, content_credentials, qortime, .. } } => {
                assert_eq!(bundle, "D:/m");
                assert_eq!((start_mono_ms, end_mono_ms, out.as_str(), content_credentials), (0, 1000, "c.mp4", false));
                assert!(qortime.is_none());
            }
            _ => panic!(),
        }
        match Cli::try_parse_from(["qorcap", "clip-verify", "c.mp4", "--bundle", "D:/m"]).unwrap().cmd {
            Cmd::ClipVerify { clip, bundle, qortime, .. } => {
                assert_eq!(clip, "c.mp4");
                assert_eq!(bundle.as_deref(), Some("D:/m"));
                assert!(qortime.is_none());
            }
            _ => panic!(),
        }
        match Cli::try_parse_from([
            "qorcap", "clip", "sign", "--bundle", "D:/m", "--clip", "h.mp4",
            "--start-mono-ms", "0", "--end-mono-ms", "1000",
        ]).unwrap().cmd {
            Cmd::Clip { clip: ClipCmd::Sign { bundle, clip, start_mono_ms, end_mono_ms, repackaged, .. } } => {
                assert_eq!((bundle.as_str(), clip.as_str(), start_mono_ms, end_mono_ms, repackaged), ("D:/m", "h.mp4", 0, 1000, false));
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
