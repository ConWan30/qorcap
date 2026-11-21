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
//! Every report is also kept, full rate, in a per-window raw companion file (`hand_raw/`) whose SHA-256 is
//! bound inside the sealed window. Raw logging is on by default when the logger runs and can be switched off
//! per session with `--no-raw-usb` (recorded as `raw_usb: false`).
//!
//! An off session is a normal session: it records `hand_timeline: false` on the start and stop checkpoints
//! (extra `qorcap` keys; no seal.rs / bundle.rs / receipt.v0 / contracts change), writes no hand file, and
//! is never treated as tampered or failed.
//!
//! Fail-soft: with no pad or the wrong device the logger records `missing` / `rejected_transport` (alignment
//! UNMEASURED) and capture continues.
//!
//! Listen-only: the Windows reader opens the HID gamepad collection with `GENERIC_READ` only and reads input
//! reports with overlapped `ReadFile`. It never sends output or feature reports, never touches firmware or
//! settings, and never opens an audio endpoint. `tests/hand_listen_only.rs` audits this module's source.
//!
//! Visible status (SCOPE-edge-logger §7.4): the live state (`connected` / `missing` / `unplugged` /
//! `stalled` / `rejected_transport` / `off`) is on `GET /status` as `hand` (practice lane only; omitted
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
#[cfg(windows)]
pub mod win;

/// `--hand-timeline` / `QORCAP_HAND_TIMELINE`. A true word is a no-op alias in practice; a false word is the off switch.
pub const ENV: &str = "QORCAP_HAND_TIMELINE";
/// `QORCAP_RAW_USB=0|false|no|off` switches raw logging off for one session (same as `--no-raw-usb`).
pub const ENV_RAW: &str = "QORCAP_RAW_USB";

fn env_is_off(env_val: Option<&str>) -> bool {
    matches!(env_val.map(|s| s.trim().to_ascii_lowercase()).as_deref(), Some("0" | "false" | "no" | "off"))
}
fn env_is_on(env_val: Option<&str>) -> bool {
    matches!(env_val.map(|s| s.trim().to_ascii_lowercase()).as_deref(), Some("1" | "true" | "yes" | "on"))
}

/// Live logger state for `GET /status` and the log. `state` is `off`, `connected`, `missing`,
/// `unplugged`, `stalled`, `rejected_transport` or `rejected_device`.
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
        }
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
}
