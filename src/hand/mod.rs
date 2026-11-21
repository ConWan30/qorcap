//! Hand timeline (`qorcap-hand-timeline/v0`): a read-only controller input log clocked to the video.
//!
//! PRACTICE lane only, default off. Turned on with `--hand-timeline` (or `QORCAP_HAND_TIMELINE=1`) together
//! with `--practice-lane` (or `QORCAP_LANE=practice`). Outside the practice lane it is refused and nothing is
//! opened or written. REAL MATCH runs are unchanged.
//!
//! What it records: host-observed DualSense Edge (0x0DF2) or, as a labelled fallback, standard DualSense
//! (0x0CE6) USB input reports (report id 0x01, 64 B) and capture-frame arrivals, on the same engine clock as
//! `inputs.jsonl` and `frame_clock.jsonl`, cut into fixed 1 s windows. Each window is sealed as a JCS no-float
//! SHA-256 chain link (`head_k = SHA-256(JCS({prev, seq, sha256}))` from 64 x "0", the `inputs.rs` rule).
//! Every report is also kept, full rate, in a per-window raw companion file (`hand_raw/`) whose SHA-256 is
//! bound inside the sealed window. Raw logging is on by default in the practice lane and can be switched off
//! per session with `--no-raw-usb` (recorded as `raw_usb: false`).
//!
//! Listen-only: the Windows reader opens the HID gamepad collection with `GENERIC_READ` only and reads input
//! reports with overlapped `ReadFile`. It never sends output or feature reports, never touches firmware or
//! settings, and never opens an audio endpoint. `tests/hand_listen_only.rs` audits this module's source.
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
#[cfg(windows)]
pub mod win;

/// `--hand-timeline` / `QORCAP_HAND_TIMELINE`. `env_val` uses the frame clock's words.
pub const ENV: &str = "QORCAP_HAND_TIMELINE";
/// `QORCAP_RAW_USB=0|false|no|off` switches raw logging off for one session (same as `--no-raw-usb`).
pub const ENV_RAW: &str = "QORCAP_RAW_USB";

/// The hand timeline runs only when asked for AND in the practice lane AND with the controller on.
/// Returns `(on, refusal)`: `refusal` is a log line when it was asked for but is not allowed.
pub fn enabled(flag: bool, env_val: Option<&str>, practice: bool, no_controller: bool) -> (bool, Option<String>) {
    let asked = crate::frameclock::flag_enabled(flag, env_val);
    if !asked { return (false, None); }
    if !practice { return (false, Some("hand timeline refused: it runs in the practice lane only (--practice-lane or QORCAP_LANE=practice)".into())); }
    if no_controller { return (false, Some("hand timeline off: --no-controller".into())); }
    (true, None)
}

/// Raw USB logging: default ON in the practice lane; off when `--no-raw-usb` or `QORCAP_RAW_USB` is a false word.
pub fn raw_enabled(no_raw_flag: bool, env_val: Option<&str>) -> bool {
    if no_raw_flag { return false; }
    !matches!(env_val.map(|s| s.trim().to_ascii_lowercase()).as_deref(), Some("0" | "false" | "no" | "off"))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn practice_only_and_default_off() {
        assert_eq!(enabled(false, None, true, false), (false, None), "default off even in practice");
        assert!(enabled(true, None, true, false).0);
        assert!(enabled(false, Some("1"), true, false).0);
        let (on, why) = enabled(true, None, false, false);
        assert!(!on && why.unwrap().contains("practice lane only"), "refused outside practice");
        assert!(!enabled(true, None, true, true).0, "--no-controller wins");
    }
    #[test]
    fn raw_defaults_on_with_a_per_session_off_switch() {
        assert!(raw_enabled(false, None));
        assert!(raw_enabled(false, Some("1")));
        assert!(!raw_enabled(true, None));
        for off in ["0", "false", "NO", " off "] { assert!(!raw_enabled(false, Some(off)), "{off}"); }
    }
}
