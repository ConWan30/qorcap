//! Hand timeline (`qorcap-hand-timeline/v0`): a read-only controller input log clocked to the video.
//!
//! PRACTICE lane only, default ON. Turned off for one session with `--no-hand-timeline` (or
//! `QORCAP_HAND_TIMELINE=0|false|no|off`). `--hand-timeline` (or a true env word) is a no-op alias in
//! practice (already on) and is refused outside the practice lane. Outside practice (REAL MATCH and the
//! default lane) nothing is opened or written and no checkpoint key is added. REAL MATCH runs stay
//! byte-identical. `--no-controller` also leaves it off.
//!
//! What it records: host-observed DualSense Edge (0x0DF2) or, as a labelled fallback, standard DualSense
//! (0x0CE6) USB input reports (report id 0x01, 64 B) and capture-frame arrivals, on the same engine clock as
//! `inputs.jsonl` and `frame_clock.jsonl`, cut into fixed 1 s windows. Each window is sealed as a JCS no-float
//! SHA-256 chain link (`head_k = SHA-256(JCS({prev, seq, sha256}))` from 64 x "0", the `inputs.rs` rule).
//! Every report is also kept, full rate, in one append-only raw file per session (`hand_raw.v0.bin`) whose
//! per-window byte slices (`raw_file`, `raw_offset`, `raw_len`, `raw_sha256`) are bound inside the sealed
//! window. Raw logging is on by default when the logger runs and can be switched off per session with
//! `--no-raw-usb` (recorded as `raw_usb: false`).
//!
//! Storage routing (practice only): defaults to `D:\QorCap\practice-hand\<session>\` on Windows, with
//! `--hand-dir` / `QORCAP_HAND_DIR` override and fail-soft fallback to the session's bundle folder when `D:`
//! (or the override) is missing or unwritable. The actual directory used and any fallback reason are bound
//! in `checkpoints.jsonl` and in the sealed `hand_timeline.v0.jsonl` header.
//!
//! An off session is a normal session: it records `hand_timeline: false` on the start and stop checkpoints
//! (extra `qorcap` keys; no seal.rs / bundle.rs / receipt.v0 / contracts change), writes no hand file, and
//! is never treated as tampered or failed.
//!
//! Fail-soft: with no pad or the wrong device the logger records `missing` / `rejected_transport` / `open_denied`
//! (alignment UNMEASURED) and capture continues. A failed `GENERIC_READ` open is `open_denied`, not `missing`.
//!
//! Listen-only: the Windows reader opens the HID gamepad collection with `GENERIC_READ` only and reads input
//! reports with overlapped `ReadFile`. It never sends output or feature reports, never touches firmware or
//! settings, and never opens an audio endpoint. `tests/hand_listen_only.rs` audits this module's source.
//!
//! Visible status (SCOPE-edge-logger §7.4): the live state (`connected` / `missing` / `unplugged` /
//! `stalled` / `rejected_transport` / `open_denied` / `clock_rollback` / `off`) is on `GET /status` as `hand` (practice lane only; omitted
//! outside practice so REAL MATCH /status is unchanged) and is written to the log as `HAND TIMELINE: …`.
//! There is no silent background run: the logger starts only from an explicit practice capture.
//!
//! What it does NOT establish: who held the controller. Edge profile, dead zones, curves, remap table and
//! stick modules are UNKNOWN (they live in feature reports this module never reads). Adaptive-trigger
//! resistance state is UNMEASURED. The real-device report rate and disk use per hour are UNMEASURED.
//! See docs/HAND_TIMELINE.md.

pub mod device;
pub mod source;
pub mod timeline;
pub mod check;
pub mod logger;
pub mod response;
#[cfg(windows)]
pub mod win;

use std::fs::OpenOptions;
use std::path::{Path, PathBuf};

/// `--hand-timeline` / `QORCAP_HAND_TIMELINE`. A true word is a no-op alias in practice; a false word is the off switch.
pub const ENV: &str = "QORCAP_HAND_TIMELINE";
/// `QORCAP_RAW_USB=0|false|no|off` switches raw logging off for one session (same as `--no-raw-usb`).
pub const ENV_RAW: &str = "QORCAP_RAW_USB";
/// `QORCAP_HAND_DIR` overrides the practice hand + raw output directory (CLI `--hand-dir` wins if both are set).
pub const ENV_HAND_DIR: &str = "QORCAP_HAND_DIR";
/// Default Windows root for practice hand and raw output (`D:\QorCap\practice-hand\<session>\`).
pub const DEFAULT_WIN_HAND_ROOT: &str = r"D:\QorCap\practice-hand";

fn env_is_off(env_val: Option<&str>) -> bool {
    matches!(env_val.map(|s| s.trim().to_ascii_lowercase()).as_deref(), Some("0" | "false" | "no" | "off"))
}
fn env_is_on(env_val: Option<&str>) -> bool {
    matches!(env_val.map(|s| s.trim().to_ascii_lowercase()).as_deref(), Some("1" | "true" | "yes" | "on"))
}

/// Live logger state for `GET /status` and the log. `state` is `off`, `connected`, `missing`,
/// `unplugged`, `stalled`, `rejected_transport`, `open_denied`, `clock_rollback` or `rejected_device`.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct LiveStatus {
    pub state: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub device_model: Option<String>,
    pub fallback: bool,
    pub raw_usb: bool,
    pub epoch: u64,
    /// Frame arrivals dropped because the bounded hand-logger queue was full or the logger had stopped.
    pub frame_drops: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hand_dir: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hand_dir_source: Option<String>,
}

impl LiveStatus {
    /// Practice session with the logger switched off. Shown on `/status`; no file is written.
    pub fn off() -> Self {
        Self {
            state: "off".into(),
            detail: Some("off for this session (--no-hand-timeline or QORCAP_HAND_TIMELINE=0)".into()),
            device_model: None,
            fallback: false,
            raw_usb: false,
            epoch: 0,
            frame_drops: 0,
            hand_dir: None,
            hand_dir_source: None,
        }
    }
}

/// Where practice hand and raw files are stored for a session.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HandDirChoice {
    pub dir: PathBuf,
    /// `"override"` | `"default_d"` | `"fallback_bundle"`
    pub source: &'static str,
    pub fallback_reason: Option<String>,
}

/// Derive a safe `<session>` folder name from the bundle directory (fallback to `match_id`).
pub fn session_name(bundle_dir: &Path, match_id: &str) -> String {
    if let Some(name) = bundle_dir.file_name().and_then(|s| s.to_str()) {
        let trimmed = name.trim();
        if !trimmed.is_empty() && trimmed != "." && trimmed != ".." {
            return trimmed.to_string();
        }
    }
    let clean: String = match_id
        .trim()
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.') { c } else { '_' })
        .collect();
    if clean.is_empty() { "session".into() } else { clean }
}

/// Format the bundle-relative pointer if `hand_dir` is the bundle dir (or inside it), else the full path.
pub fn bundle_pointer(bundle_dir: &Path, hand_dir: &Path) -> String {
    if hand_dir == bundle_dir {
        return ".".to_string();
    }
    if let Ok(rel) = hand_dir.strip_prefix(bundle_dir) {
        let s = rel.to_string_lossy();
        if !s.is_empty() {
            return s.into_owned();
        }
    }
    hand_dir.to_string_lossy().into_owned()
}

fn ensure_writable_dir(dir: &Path) -> Result<(), String> {
    std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    let probe = dir.join(format!(".qorcap-probe-{}-{:?}", std::process::id(), std::thread::current().id()));
    let f = OpenOptions::new().create_new(true).write(true).open(&probe).map_err(|e| e.to_string())?;
    drop(f);
    let _ = std::fs::remove_file(&probe);
    Ok(())
}

/// Resolve the practice hand/raw output directory for this OS.
/// Windows defaults to `D:\QorCap\practice-hand\<session>\`; non-Windows has no `D:` drive and falls back
/// to `bundle_dir` unless `--hand-dir` or `QORCAP_HAND_DIR` is set.
pub fn resolve_hand_dir(bundle_dir: &Path, session: &str, flag: Option<&str>, env: Option<&str>) -> HandDirChoice {
    #[cfg(windows)]
    {
        resolve_hand_dir_with_anchor(
            bundle_dir,
            session,
            flag,
            env,
            Some(Path::new(r"D:\")),
            Some(Path::new(DEFAULT_WIN_HAND_ROOT)),
        )
    }
    #[cfg(not(windows))]
    {
        resolve_hand_dir_with_anchor(bundle_dir, session, flag, env, None, None)
    }
}

/// Testable resolver parameterized by drive anchor (`D:\`) and default root (`D:\QorCap\practice-hand`).
pub fn resolve_hand_dir_with_anchor(
    bundle_dir: &Path,
    session: &str,
    flag: Option<&str>,
    env: Option<&str>,
    drive_anchor: Option<&Path>,
    default_root: Option<&Path>,
) -> HandDirChoice {
    let ov = flag.map(str::trim).filter(|s| !s.is_empty())
        .or_else(|| env.map(str::trim).filter(|s| !s.is_empty()));
    if let Some(raw) = ov {
        let cand = PathBuf::from(raw);
        return match ensure_writable_dir(&cand) {
            Ok(()) => HandDirChoice { dir: cand, source: "override", fallback_reason: None },
            Err(why) => HandDirChoice {
                dir: bundle_dir.to_path_buf(),
                source: "fallback_bundle",
                fallback_reason: Some(format!("override {} is not writable ({why}); using bundle folder", cand.display())),
            },
        };
    }
    let Some(root) = default_root else {
        return HandDirChoice {
            dir: bundle_dir.to_path_buf(),
            source: "fallback_bundle",
            fallback_reason: Some("D:\\ drive is unavailable on non-Windows host; using bundle folder".into()),
        };
    };
    if let Some(anchor) = drive_anchor {
        if !anchor.is_dir() {
            return HandDirChoice {
                dir: bundle_dir.to_path_buf(),
                source: "fallback_bundle",
                fallback_reason: Some(format!("{} is missing or not a directory; using bundle folder", anchor.display())),
            };
        }
    }
    let cand = root.join(session);
    match ensure_writable_dir(&cand) {
        Ok(()) => HandDirChoice { dir: cand, source: "default_d", fallback_reason: None },
        Err(why) => HandDirChoice {
            dir: bundle_dir.to_path_buf(),
            source: "fallback_bundle",
            fallback_reason: Some(format!("{} is not writable ({why}); using bundle folder", cand.display())),
        },
    }
}

/// Default ON in the practice lane. `--no-hand-timeline` or a false `QORCAP_HAND_TIMELINE` word turns it off
/// for one session. `--hand-timeline` (or a true env word) is a no-op alias in practice (already on) and is
/// refused outside the practice lane. `--no-controller` leaves it off. The off switch wins if both flags are set.
/// Returns `(on, note)`: `note` is a log line when it is switched off or when it was asked for but refused.
pub fn enabled(on_alias: bool, off_flag: bool, env_val: Option<&str>, practice: bool, no_controller: bool) -> (bool, Option<String>) {
    let asked = on_alias || env_is_on(env_val);
    let off = off_flag || env_is_off(env_val);
    if !practice {
        let note = if asked {
            Some("hand timeline refused: it runs in the practice lane only (--practice-lane or QORCAP_LANE=practice)".into())
        } else { None };
        return (false, note);
    }
    if no_controller {
        return (false, Some("hand timeline off: --no-controller".into()));
    }
    if off {
        return (false, Some("hand timeline off for this session (--no-hand-timeline or QORCAP_HAND_TIMELINE=0)".into()));
    }
    (true, None)
}

/// Raw USB logging: default ON when the hand timeline runs; off when `--no-raw-usb` or `QORCAP_RAW_USB` is a false word.
pub fn raw_enabled(no_raw_flag: bool, env_val: Option<&str>) -> bool {
    if no_raw_flag { return false; }
    !env_is_off(env_val)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn practice_default_on_with_a_per_session_off_switch() {
        assert!(enabled(false, false, None, true, false).0, "default on in practice");
        assert!(enabled(true, false, None, true, false).0, "--hand-timeline is a no-op in practice");
        assert!(enabled(false, false, Some("1"), true, false).0);
        assert!(!enabled(false, true, None, true, false).0, "--no-hand-timeline");
        assert!(!enabled(true, true, None, true, false).0, "off switch wins over the alias");
        assert!(!enabled(false, false, Some("0"), true, false).0);
        assert!(!enabled(false, false, Some("OFF"), true, false).0);
        assert!(!enabled(false, false, None, false, false).0, "off outside practice, no log");
        let (on, why) = enabled(true, false, None, false, false);
        assert!(!on && why.unwrap().contains("practice lane only"), "alias refused outside practice");
        assert!(!enabled(false, false, None, true, true).0, "--no-controller wins");
        assert_eq!(LiveStatus::off().state, "off");
    }
    #[test]
    fn raw_defaults_on_with_a_per_session_off_switch() {
        assert!(raw_enabled(false, None));
        assert!(raw_enabled(false, Some("1")));
        assert!(!raw_enabled(true, None));
        for off in ["0", "false", "NO", " off "] { assert!(!raw_enabled(false, Some(off)), "{off}"); }
    }
    #[test]
    fn d_drive_routing_override_and_fail_soft_fallback() {
        let base = std::env::temp_dir().join(format!("qorcap-hand-dir-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        let bundle = base.join("matches").join("sess-1");
        std::fs::create_dir_all(&bundle).unwrap();

        // 1. Default D: anchor present and writable -> routes to <root>/<session>.
        let d_drive = base.join("fake-d");
        std::fs::create_dir_all(&d_drive).unwrap();
        let d_root = d_drive.join("QorCap").join("practice-hand");
        let c1 = resolve_hand_dir_with_anchor(&bundle, "sess-1", None, None, Some(&d_drive), Some(&d_root));
        assert_eq!(c1.dir, d_root.join("sess-1"));
        assert_eq!(c1.source, "default_d");
        assert!(c1.fallback_reason.is_none());

        // 2. D: anchor missing -> fail-soft fallback to bundle dir.
        let missing_drive = base.join("missing-d");
        let c2 = resolve_hand_dir_with_anchor(&bundle, "sess-1", None, None, Some(&missing_drive), Some(&missing_drive.join("QorCap").join("practice-hand")));
        assert_eq!(c2.dir, bundle);
        assert_eq!(c2.source, "fallback_bundle");
        assert!(c2.fallback_reason.as_deref().unwrap().contains("missing"));

        // 3. D: anchor exists but default root is unwritable (occupied by a regular file) -> fallback to bundle dir.
        let ro_drive = base.join("ro-d");
        std::fs::create_dir_all(&ro_drive).unwrap();
        let blocked_root = ro_drive.join("QorCap");
        std::fs::write(&blocked_root, b"not-a-dir").unwrap();
        let c3 = resolve_hand_dir_with_anchor(&bundle, "sess-1", None, None, Some(&ro_drive), Some(&blocked_root.join("practice-hand")));
        assert_eq!(c3.dir, bundle);
        assert_eq!(c3.source, "fallback_bundle");
        assert!(c3.fallback_reason.as_deref().unwrap().contains("not writable"));

        // 4. Override via env and CLI flag (CLI wins over env).
        let env_dir = base.join("env-hand");
        let cli_dir = base.join("cli-hand");
        let env_s = env_dir.to_string_lossy().into_owned();
        let cli_s = cli_dir.to_string_lossy().into_owned();
        let c4 = resolve_hand_dir_with_anchor(&bundle, "sess-1", None, Some(&env_s), Some(&missing_drive), Some(&d_root));
        assert_eq!(c4.dir, env_dir);
        assert_eq!(c4.source, "override");
        assert!(c4.fallback_reason.is_none());
        let c5 = resolve_hand_dir_with_anchor(&bundle, "sess-1", Some(&cli_s), Some(&env_s), Some(&missing_drive), Some(&d_root));
        assert_eq!(c5.dir, cli_dir);
        assert_eq!(c5.source, "override");

        // 5. Unwritable override falls back to bundle dir.
        let bad_ov = blocked_root.join("sub");
        let c6 = resolve_hand_dir_with_anchor(&bundle, "sess-1", Some(&bad_ov.to_string_lossy()), None, Some(&d_drive), Some(&d_root));
        assert_eq!(c6.dir, bundle);
        assert_eq!(c6.source, "fallback_bundle");
        assert!(c6.fallback_reason.as_deref().unwrap().contains("override"));

        assert_eq!(bundle_pointer(&bundle, &bundle), ".");
        assert_eq!(bundle_pointer(&bundle, &d_root.join("sess-1")), d_root.join("sess-1").to_string_lossy());
        let _ = std::fs::remove_dir_all(&base);
    }
}
