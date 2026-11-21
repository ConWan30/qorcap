//! Live pad-echo seal. Read-only: this watches input reports and never writes to the controller.
//!
//! The opening pattern is TRIANGLE, SQUARE, CIRCLE, CROSS, then OPTIONS held at least
//! [`HOLD_MIN_US`]. The window is hashed when OPTIONS releases. A solo OPTIONS hold, a
//! broken order, a short hold, or a pattern after [`OPENING_US`] is recorded as failed.
//! A missing hash is not invented. Thresholds are PROVISIONAL.
//!
//! Dual-bind: `session_seal.json` and `pad_session.v0.json` carry the same `seal_hash`,
//! and the next checkpoint's `qorcap.session_seal.hash` must match. The opening gate
//! passes only when the status is `locked`.
use crate::evidence::{self as ev};
use crate::hid::{CROSS, OPTIONS, SQUARE, TRIANGLE, CIRCLE};
use serde_json::{json, Value};
use std::path::Path;

pub const SCHEMA: &str = "qorcap-session-seal/v0";
pub const PAD_SCHEMA: &str = "qorcap-pad-session/v0";
pub const WINDOW_SCHEMA: &str = "qorcap-session-seal-window/v0";
/// PROVISIONAL. The asked hold is about 2 s. 1.5 s keeps the 1.886 s hold from
/// match-20261106-201029 a completed hold, not a short one.
pub const HOLD_MIN_US: u64 = 1_500_000;
/// PROVISIONAL opening window on the capture clock, measured from t_us 0.
pub const OPENING_US: u64 = 20_000_000;
/// PROVISIONAL max gap between the face-button edges.
pub const STEP_GAP_US: u64 = 2_000_000;
/// PROVISIONAL max gap from CROSS to the OPTIONS press.
pub const OPTIONS_GAP_US: u64 = 3_000_000;

const FACES: [(&str, u32); 4] = [("TRIANGLE", TRIANGLE), ("SQUARE", SQUARE), ("CIRCLE", CIRCLE), ("CROSS", CROSS)];
const FACE_MASK: u32 = TRIANGLE | SQUARE | CIRCLE | CROSS;

#[derive(Clone, Copy)]
enum Phase {
    Wait,
    /// `next` is how many face buttons are already accepted (0..4). 4 means OPTIONS is next.
    Faces { next: usize, last_us: u64 },
    Hold { press_us: u64 },
    Solo { _press_us: u64 },
}

pub struct SealWatch {
    enabled: bool,
    phase: Phase,
    prev: u32,
    events: Vec<Value>,
    attempts: Vec<Value>,
    pub status: String,
    pub reason: String,
    pub hash: Option<String>,
    pub hold_us: Option<u64>,
    pub opening_gate: bool,
    terminal: bool,
    bound: bool,
    pub line: String,
    session: String,
    echo: Vec<Value>,
    echo_epoch: Option<u64>,
    pub preimage: Option<Value>,
    anchor_ack: Option<String>,
    revision: u64,
}

impl SealWatch {
    pub fn new(enabled: bool) -> Self {
        Self {
            enabled,
            phase: Phase::Wait,
            prev: 0,
            events: vec![],
            attempts: vec![],
            status: if enabled { "waiting".into() } else { "off".into() },
            reason: if enabled { "waiting".into() } else { "off".into() },
            hash: None,
            hold_us: None,
            opening_gate: false,
            terminal: false,
            bound: false,
            line: if enabled { "Seal: waiting".into() } else { String::new() },
            session: String::new(),
            echo: vec![],
            echo_epoch: None,
            preimage: None,
            anchor_ack: None,
            revision: 0,
        }
    }

    pub fn enabled(&self) -> bool { self.enabled }

    pub fn set_session(&mut self, session: &str) {
        if !session.is_empty() { self.session = session.to_string(); }
    }

    /// One input report. `raw` is the exact HID input bytes. A changed epoch aborts a pending attempt
    /// before a release can finish a hold that was not observed.
    pub fn observe(&mut self, t_us: u64, buttons: u32, epoch: u64, raw: &[u8]) -> bool {
        if !self.enabled || self.terminal { self.prev = buttons; return false; }
        if self.echo_epoch.is_some() && self.echo_epoch != Some(epoch) {
            let changed = self.abort_pending("epoch_reset");
            self.prev = buttons;
            return changed;
        }
        let rise = buttons & !self.prev;
        let fall = self.prev & !buttons;
        self.prev = buttons;
        let before = self.line.clone();
        let pending = !matches!(self.phase, Phase::Wait);
        if pending { self.push_echo(t_us, epoch, raw); }
        self.step(t_us, rise, fall);
        if !matches!(self.phase, Phase::Wait) && self.echo.is_empty() {
            self.echo_epoch = Some(epoch);
            self.push_echo(t_us, epoch, raw);
        }
        self.line != before
    }

    fn push_echo(&mut self, t_us: u64, epoch: u64, raw: &[u8]) {
        self.echo.push(json!({
            "t_us": t_us,
            "epoch": epoch,
            "bytes": hex_encode(raw),
        }));
    }

    /// Drop a hold or a partial pattern. The echo bytes stay on the attempt.
    pub fn abort_pending(&mut self, reason: &str) -> bool {
        if !self.enabled || self.terminal { return false; }
        let pending = !matches!(self.phase, Phase::Wait) || !self.echo.is_empty();
        if !pending { return false; }
        let before = self.line.clone();
        self.fail_retry(reason);
        before != self.line || self.reason == reason
    }

    /// An outside acknowledgment. The seal hash itself is not an anchor.
    pub fn acknowledge_anchor(&mut self, anchor: &str) -> bool {
        if anchor.is_empty() || self.hash.as_deref() == Some(anchor) { return false; }
        if !self.terminal || self.status != "locked" { return false; }
        self.anchor_ack = Some(anchor.to_string());
        true
    }

    fn step(&mut self, t_us: u64, rise: u32, fall: u32) {
        match self.phase {
            Phase::Hold { press_us } => {
                if fall & OPTIONS != 0 {
                    let hold = t_us.saturating_sub(press_us);
                    if hold >= HOLD_MIN_US {
                        self.events.push(edge(t_us, "release", "OPTIONS"));
                        self.finish_hold(t_us, hold);
                    } else {
                        self.fail_retry("options_hold_short");
                    }
                }
            }
            Phase::Solo { .. } => {
                if fall & OPTIONS != 0 { self.fail_retry("options_without_pattern"); }
            }
            Phase::Faces { next, last_us } => {
                let limit = if next >= 4 { OPTIONS_GAP_US } else { STEP_GAP_US };
                if t_us.saturating_sub(last_us) > limit {
                    self.fail_retry("pattern_broken");
                    self.restart_if_triangle(t_us, rise);
                    return;
                }
                if next >= 4 {
                    if rise & OPTIONS != 0 && rise & FACE_MASK == 0 {
                        self.events.push(edge(t_us, "press", "OPTIONS"));
                        self.phase = Phase::Hold { press_us: t_us };
                    } else if rise & (FACE_MASK | OPTIONS) != 0 {
                        self.fail_retry("pattern_broken");
                        self.restart_if_triangle(t_us, rise);
                    }
                    return;
                }
                let (name, bit) = FACES[next];
                if rise & bit != 0 && rise & (FACE_MASK | OPTIONS) == bit {
                    self.events.push(edge(t_us, "press", name));
                    self.phase = Phase::Faces { next: next + 1, last_us: t_us };
                } else if rise & (FACE_MASK | OPTIONS) != 0 {
                    self.fail_retry("pattern_broken");
                    self.restart_if_triangle(t_us, rise);
                }
            }
            Phase::Wait => {
                if rise & TRIANGLE != 0 && rise & (FACE_MASK | OPTIONS) == TRIANGLE {
                    self.events.push(edge(t_us, "press", "TRIANGLE"));
                    self.phase = Phase::Faces { next: 1, last_us: t_us };
                } else if rise & OPTIONS != 0 && rise & FACE_MASK == 0 {
                    self.phase = Phase::Solo { _press_us: t_us };
                } else if rise & FACE_MASK != 0 {
                    self.fail_retry("pattern_broken");
                }
            }
        }
    }

    fn restart_if_triangle(&mut self, t_us: u64, rise: u32) {
        if self.terminal { return; }
        if rise & TRIANGLE != 0 && rise & (FACE_MASK | OPTIONS) == TRIANGLE {
            self.events.push(edge(t_us, "press", "TRIANGLE"));
            self.phase = Phase::Faces { next: 1, last_us: t_us };
        }
    }

    fn finish_hold(&mut self, t_us: u64, hold_us: u64) {
        let bytes_ok = !self.echo.is_empty()
            && self.echo.iter().all(|e| e.get("bytes").and_then(|v| v.as_str()).is_some_and(|s| !s.is_empty()));
        let epoch = self.echo_epoch;
        let epoch_ok = epoch.is_some()
            && self.echo.iter().all(|e| e.get("epoch").and_then(|v| v.as_u64()) == epoch);
        if self.session.is_empty() || !bytes_ok || !epoch_ok {
            self.fail_retry(if !bytes_ok { "echo_bytes_missing" } else { "echo_unbound" });
            return;
        }
        let preimage = seal_preimage(&self.session, epoch.unwrap_or(0), &self.echo);
        let hash = window_hash(&preimage);
        self.preimage = Some(preimage);
        self.hold_us = Some(hold_us);
        let late = t_us > OPENING_US;
        if late {
            self.hash = Some(hash);
            self.opening_gate = false;
            self.set_status("failed", "late", true);
            self.note_attempt("late", true);
        } else {
            self.hash = Some(hash);
            self.opening_gate = true;
            self.set_status("locked", "pattern_held", true);
            self.note_attempt("pattern_held", false);
        }
        self.phase = Phase::Wait;
        self.events.clear();
        self.echo.clear();
        self.echo_epoch = None;
    }

    fn fail_retry(&mut self, reason: &str) {
        self.note_attempt(reason, true);
        self.hash = None;
        self.opening_gate = false;
        self.hold_us = None;
        self.set_status("failed", reason, false);
        self.phase = Phase::Wait;
        self.events.clear();
        self.echo.clear();
        self.echo_epoch = None;
    }

    fn note_attempt(&mut self, reason: &str, partial: bool) {
        if self.attempts.len() >= 16 { self.attempts.remove(0); }
        self.attempts.push(json!({
            "reason": reason,
            "partial": partial,
            "events": self.events.len(),
            "echo": self.echo.clone(),
        }));
    }

    fn set_status(&mut self, status: &str, reason: &str, terminal: bool) {
        self.status = status.into();
        self.reason = reason.into();
        self.terminal = terminal;
        if terminal { self.revision = self.revision.saturating_add(1); }
        self.line = match status {
            "locked" => "Seal: locked".into(),
            "failed" => format!("Seal: failed ({reason})"),
            _ => "Seal: waiting".into(),
        };
    }

    /// Freeze a still-open watch at stop. Does not invent a hash.
    pub fn close_at_stop(&mut self) {
        if !self.enabled || self.terminal { return; }
        if self.status == "failed" {
            self.terminal = true;
            self.revision = self.revision.saturating_add(1);
            return;
        }
        let reason = match self.phase {
            Phase::Hold { .. } => "options_still_held",
            Phase::Solo { .. } => "options_without_pattern",
            Phase::Faces { next, .. } if next > 0 => "pattern_incomplete",
            _ => "pattern_not_seen",
        };
        self.fail_retry(reason);
        self.terminal = true;
        self.revision = self.revision.saturating_add(1);
    }

    /// A finalized revision only. Retryable failures are not offered, and this does not mark them bound.
    pub fn binding(&self) -> Option<Value> {
        if !self.enabled || self.bound || !self.terminal || self.status == "waiting" || self.status == "off" {
            return None;
        }
        Some(json!({
            "hash": self.hash.clone().map(Value::from).unwrap_or(Value::Null),
            "status": self.status,
            "reason": self.reason,
            "opening_gate": self.opening_gate,
            "anchor_ack": self.anchor_ack.clone().map(Value::from).unwrap_or(Value::Null),
            "revision": self.revision,
        }))
    }

    /// Call only after the checkpoint line is durable. A refused write leaves the revision unbound.
    pub fn acknowledge(&mut self) -> bool {
        if self.binding().is_none() { return false; }
        self.bound = true;
        true
    }

    pub fn write_files(&self, dir: &Path) -> Result<(), String> {
        if !self.enabled { return Ok(()); }
        let _ = std::fs::create_dir_all(dir);
        write_json(&dir.join("session_seal.json"), &self.session_doc())?;
        write_json(&dir.join("pad_session.v0.json"), &self.pad_doc())?;
        Ok(())
    }

    fn session_doc(&self) -> Value {
        json!({
            "schema": SCHEMA,
            "status": self.status,
            "reason": self.reason,
            "opening_gate": self.opening_gate,
            "session": self.session,
            "seal_hash": self.hash.clone().map(Value::from).unwrap_or(Value::Null),
            "preimage": self.preimage.clone().unwrap_or(Value::Null),
            "anchor_ack": self.anchor_ack.clone().map(Value::from).unwrap_or(Value::Null),
            "hold_us": self.hold_us.map(Value::from).unwrap_or(Value::Null),
            "pattern": ["TRIANGLE", "SQUARE", "CIRCLE", "CROSS", "OPTIONS"],
            "late": self.reason == "late",
            "partial": self.status == "failed" && self.hash.is_none(),
            "attempts": self.attempts,
            "thresholds": {
                "hold_min_us": HOLD_MIN_US,
                "opening_us": OPENING_US,
                "step_gap_us": STEP_GAP_US,
                "options_gap_us": OPTIONS_GAP_US,
                "label": "PROVISIONAL"
            },
            "note": "Hashed when OPTIONS releases after TRIANGLE, SQUARE, CIRCLE, CROSS. A late or partial pattern is recorded and does not pass the opening gate. Nothing is sent to the controller."
        })
    }

    fn pad_doc(&self) -> Value {
        json!({
            "schema": PAD_SCHEMA,
            "seal_hash": self.hash.clone().map(Value::from).unwrap_or(Value::Null),
            "seal_status": self.status,
            "seal_reason": self.reason,
            "opening_gate": self.opening_gate,
            "anchor_ack": self.anchor_ack.clone().map(Value::from).unwrap_or(Value::Null),
            "note": "Dual-bind: this seal_hash must equal session_seal.json and the checkpoint session_seal.hash. A missing hash does not pass. The seal hash is not an anchor. Nothing is sent to the controller."
        })
    }
}

fn edge(t_us: u64, edge: &str, button: &str) -> Value {
    json!({"t_us": t_us, "edge": edge, "button": button})
}

pub fn seal_preimage(session: &str, epoch: u64, echo: &[Value]) -> Value {
    json!({"schema": WINDOW_SCHEMA, "session": session, "epoch": epoch, "echo": echo})
}

pub fn window_hash(preimage: &Value) -> String { ev::jcs_sha(preimage) }

fn hex_encode(raw: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(raw.len() * 2);
    for b in raw {
        out.push(HEX[(b >> 4) as usize] as char);
        out.push(HEX[(b & 0x0f) as usize] as char);
    }
    out
}

/// Opening dual-bind. A late hash can match and still fail. The seal hash is not the anchor.
pub fn dual_bind_passes(session: &Value, pad: &Value, checkpoint_hash: Option<&str>, anchor: Option<&str>) -> bool {
    let hash = match session.get("seal_hash").and_then(|v| v.as_str()) {
        Some(h) if !h.is_empty() => h,
        _ => return false,
    };
    if pad.get("seal_hash").and_then(|v| v.as_str()) != Some(hash) { return false; }
    if checkpoint_hash != Some(hash) { return false; }
    let Some(ack) = anchor.filter(|s| !s.is_empty() && *s != hash) else { return false; };
    if session.get("anchor_ack").and_then(|v| v.as_str()) != Some(ack) { return false; }
    if pad.get("anchor_ack").and_then(|v| v.as_str()) != Some(ack) { return false; }
    session.get("opening_gate") == Some(&json!(true))
        && session.get("status").and_then(|v| v.as_str()) == Some("locked")
        && pad.get("opening_gate") == Some(&json!(true))
}

fn write_json(path: &Path, v: &Value) -> Result<(), String> {
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, format!("{v}\n")).map_err(|e| e.to_string())?;
    std::fs::rename(&tmp, path).map_err(|e| e.to_string())
}

/// Console confirmation only. This module does not write an output report.
pub fn read_only_note() -> &'static str {
    "controller access stays read-only; seal confirmation is a console line"
}

#[cfg(test)]
mod tests {
    use super::*;

    fn feed(w: &mut SealWatch, t: u64, buttons: u32) {
        let pad = crate::hid::Pad { buttons, ..crate::hid::Pad::neutral() };
        let raw = crate::hid::pack_usb(&pad);
        w.observe(t, buttons, 1, &raw);
    }

    #[test]
    fn solo_options_does_not_lock_and_a_full_opening_pattern_does() {
        let mut w = SealWatch::new(true);
        w.set_session("capture-1");
        assert_eq!(w.line, "Seal: waiting");
        feed(&mut w, 1_451_199, OPTIONS);
        feed(&mut w, 3_733_481, 0);
        assert_eq!(w.status, "failed");
        assert_eq!(w.reason, "options_without_pattern");
        assert!(w.hash.is_none());
        assert!(!w.opening_gate);
        assert!(w.line.starts_with("Seal: failed"));
        // TRIANGLE SQUARE CIRCLE CROSS, then OPTIONS held 1.9 s, still inside the opening window.
        feed(&mut w, 4_000_000, TRIANGLE);
        feed(&mut w, 4_300_000, 0);
        feed(&mut w, 4_300_100, SQUARE);
        feed(&mut w, 4_600_000, 0);
        feed(&mut w, 4_600_100, CIRCLE);
        feed(&mut w, 4_900_000, 0);
        feed(&mut w, 4_900_100, CROSS);
        feed(&mut w, 5_200_000, 0);
        feed(&mut w, 6_000_000, OPTIONS);
        feed(&mut w, 7_900_000, 0);
        assert_eq!(w.status, "locked", "{}", w.reason);
        assert_eq!(w.reason, "pattern_held");
        assert!(w.opening_gate);
        assert_eq!(w.line, "Seal: locked");
        let hash = w.hash.clone().unwrap();
        assert_eq!(hash.len(), 64);
        let preimage = w.preimage.clone().unwrap();
        assert_eq!(preimage["session"], "capture-1");
        assert_eq!(preimage["epoch"], 1);
        assert!(preimage["echo"].as_array().unwrap().len() >= 8, "the timed echo bytes stay on the preimage");
        assert_eq!(window_hash(&preimage), hash);
        assert_ne!(window_hash(&seal_preimage("capture-1", 1, &[])), hash);
        let dir = std::env::temp_dir().join(format!("qorcap-seal-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        w.write_files(&dir).unwrap();
        let session: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("session_seal.json")).unwrap()).unwrap();
        let pad: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("pad_session.v0.json")).unwrap()).unwrap();
        assert_eq!(session["seal_hash"], pad["seal_hash"]);
        assert!(session["preimage"]["echo"].as_array().unwrap().len() >= 8);
        assert!(!dual_bind_passes(&session, &pad, Some(&hash), None), "a local hash is not an anchor");
        assert!(w.acknowledge_anchor(&hash) == false, "the seal hash is not an anchor");
        assert!(w.acknowledge_anchor("external-anchor"));
        w.write_files(&dir).unwrap();
        let session: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("session_seal.json")).unwrap()).unwrap();
        let pad: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("pad_session.v0.json")).unwrap()).unwrap();
        let cp = w.binding().unwrap();
        assert!(!w.bound);
        assert!(dual_bind_passes(&session, &pad, cp["hash"].as_str(), Some("external-anchor")));
        assert!(w.acknowledge());
        assert!(w.binding().is_none(), "the revision binds after it is acknowledged");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_release_after_reconnect_does_not_finish_the_hold_and_a_retry_can_still_bind() {
        let mut w = SealWatch::new(true);
        w.set_session("capture-1");
        feed(&mut w, 4_000_000, TRIANGLE);
        feed(&mut w, 4_200_000, 0);
        feed(&mut w, 4_200_100, SQUARE);
        feed(&mut w, 4_400_000, 0);
        feed(&mut w, 4_400_100, CIRCLE);
        feed(&mut w, 4_600_000, 0);
        feed(&mut w, 4_600_100, CROSS);
        feed(&mut w, 4_800_000, 0);
        feed(&mut w, 5_000_000, OPTIONS);
        assert_eq!(w.line, "Seal: waiting");
        assert!(w.abort_pending("fault_ambiguous"));
        assert_eq!(w.reason, "fault_ambiguous");
        assert!(w.hash.is_none());
        assert!(w.binding().is_none(), "a retryable failure is not a finalized binding");
        assert!(!w.attempts.is_empty());
        assert!(!w.attempts.last().unwrap()["echo"].as_array().unwrap().is_empty(), "the echo bytes stay on the attempt");
        let up = crate::hid::pack_usb(&crate::hid::Pad::neutral());
        w.observe(8_000_000, 0, 2, &up);
        assert!(w.hash.is_none(), "the unobserved release does not complete the hold");
        assert_ne!(w.status, "locked");
        feed(&mut w, 4_000_000, TRIANGLE);
        feed(&mut w, 4_300_000, 0);
        feed(&mut w, 4_300_100, SQUARE);
        feed(&mut w, 4_600_000, 0);
        feed(&mut w, 4_600_100, CIRCLE);
        feed(&mut w, 4_900_000, 0);
        feed(&mut w, 4_900_100, CROSS);
        feed(&mut w, 5_200_000, 0);
        feed(&mut w, 6_000_000, OPTIONS);
        feed(&mut w, 7_900_000, 0);
        assert_eq!(w.status, "locked", "{}", w.reason);
        assert!(w.binding().is_some());
        assert!(w.acknowledge());
        assert!(w.bound);
        assert!(!w.acknowledge());
        assert!(w.binding().is_none());
    }

    #[test]
    fn late_pattern_is_hashed_and_does_not_pass_and_a_short_hold_is_not_invented() {
        let mut w = SealWatch::new(true);
        w.set_session("capture-201029");
        // The match-20261106-201029 echo, on that capture clock.
        feed(&mut w, 73_372_949, TRIANGLE);
        feed(&mut w, 73_672_919, SQUARE);
        feed(&mut w, 73_961_004, CIRCLE);
        feed(&mut w, 74_253_912, CROSS);
        feed(&mut w, 75_730_941, OPTIONS);
        feed(&mut w, 77_617_166, 0);
        assert_eq!(w.status, "failed");
        assert_eq!(w.reason, "late");
        assert!(w.hash.as_ref().unwrap().len() == 64);
        assert!(!w.opening_gate);
        assert_eq!(w.line, "Seal: failed (late)");
        let session = json!({"seal_hash": w.hash, "status": "failed", "opening_gate": false});
        let pad = json!({"seal_hash": w.hash, "opening_gate": false});
        assert!(!dual_bind_passes(&session, &pad, w.hash.as_deref(), Some("external-anchor")));
        assert!(w.preimage.as_ref().unwrap()["echo"].as_array().unwrap().len() >= 5);
        assert!(!dual_bind_passes(&json!({"seal_hash": null, "status": "locked", "opening_gate": true, "anchor_ack": "external-anchor"}), &json!({"seal_hash": null, "opening_gate": true, "anchor_ack": "external-anchor"}), None, Some("external-anchor")));

        let mut short = SealWatch::new(true);
        feed(&mut short, 1_000_000, TRIANGLE);
        feed(&mut short, 1_200_000, SQUARE);
        feed(&mut short, 1_400_000, CIRCLE);
        feed(&mut short, 1_600_000, CROSS);
        feed(&mut short, 2_000_000, OPTIONS);
        feed(&mut short, 2_400_000, 0);
        assert_eq!(short.reason, "options_hold_short");
        assert!(short.hash.is_none(), "a short hold does not invent a seal hash");
        short.close_at_stop();
        assert!(short.hash.is_none());
        short.close_at_stop();
        assert!(short.binding().unwrap()["hash"].is_null());
        assert!(!short.bound);
        assert!(short.acknowledge());
    }

    #[test]
    fn wrong_order_and_a_stop_with_no_pattern_stay_failed() {
        let mut w = SealWatch::new(true);
        feed(&mut w, 100_000, SQUARE);
        assert_eq!(w.reason, "pattern_broken");
        assert!(w.hash.is_none());
        let mut quiet = SealWatch::new(true);
        quiet.close_at_stop();
        assert_eq!(quiet.reason, "pattern_not_seen");
        assert!(quiet.hash.is_none());
        assert!(!quiet.opening_gate);
        assert_eq!(read_only_note().contains("read-only"), true);
    }
}
