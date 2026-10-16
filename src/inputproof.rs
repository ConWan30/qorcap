//! `input_proof.json`: the controller-input summary of a match bundle, plus the backward-compatible receipt fields.
//!
//! Consumers (QorTime `qortime referee-ai`, Verified Match) read `input_proof.json` (schema
//! `qorcap-input-proof/v0`) or the compact `bundle.json.input` / `receipt.draft.json.signals.input` copies.
//! Every number is an integer (permille / ms) so the receipt stays inside QorTime's JCS no-float profile.
use crate::{align, evidence as ev, haptics, humanlike, inputs};
use serde_json::{json, Value};
use std::path::Path;

pub const SCHEMA: &str = "qorcap-input-proof/v0";
pub const LIMIT: &str = "Shows these inputs came from the USB-connected controller in sync with this video. It does not show who was holding the controller.";

fn jsonl(p: &Path) -> Vec<Value> {
    std::fs::read_to_string(p).map(|s| s.lines().filter_map(|l| serde_json::from_str(l).ok()).collect()).unwrap_or_default()
}

/// Build the proof for bundle `dir`. `ffmpeg = None` skips frame differencing.
pub fn build(dir: &Path, ffmpeg: Option<&str>, n_frames: usize) -> Value {
    let p = dir.join("inputs.jsonl");
    if !p.exists() {
        return json!({"schema": SCHEMA, "status": "no_input_log", "warning": "No controller input log in this bundle (recorded without a controller or with QorCap < 0.3.0).", "limit": LIMIT});
    }
    let (recs, chunks) = match inputs::read_file(&p) { Ok(x) => x, Err(e) => return json!({"schema": SCHEMA, "status": "unreadable", "warning": e, "limit": LIMIT}) };
    let (_, head, errs) = inputs::verify_chunks(&recs, &chunks);
    let file_sha = std::fs::read(&p).map(|b| ev::sha256_hex(&b)).unwrap_or_default();
    let header = recs.iter().find(|r| r["type"] == "header");
    let anchor = header.and_then(|h| h["wall_anchor_ms"].as_u64());
    let n_in = recs.iter().filter(|r| r["type"] == "in").count();
    let states: Vec<&Value> = recs.iter().filter(|r| r["type"] == "status").collect();
    let device = states.iter().rev().find(|s| s["state"] == "connected").and_then(|s| s["detail"].as_str()).map(String::from);
    let gaps = states.iter().filter(|s| s["state"] != "connected").count();
    let human = humanlike::score(&recs);
    let manifest = jsonl(&dir.join("manifest.jsonl"));
    let timeline = jsonl(&dir.join("timeline.jsonl"));
    let alignment = if n_in > 0 { align::align_bundle(dir, &recs, &manifest, &timeline, anchor, ffmpeg, n_frames) } else { Value::Null };
    // Haptics (0.3.1+): records live in inputs.jsonl chunks (committed by input_hash); haptics.jsonl is a copy.
    let hp = dir.join("haptics.jsonl");
    let committed: Vec<String> = recs.iter().filter(|r| r["type"] == "haptic").map(|r| r.to_string()).collect();
    let (hsha, hmatch) = match std::fs::read(&hp) {
        Ok(b) => {
            let lines: Vec<String> = String::from_utf8_lossy(&b).lines().filter(|l| !l.trim().is_empty())
                .filter_map(|l| serde_json::from_str::<Value>(l).ok()).map(|v| v.to_string()).collect();
            (Some(ev::sha256_hex(&b)), Some(lines == committed))
        }
        Err(_) => (None, if committed.is_empty() { None } else { Some(false) }),
    };
    let haptics_v = haptics::summarize(&recs, &timeline, anchor, hsha, hmatch);
    let status = if n_in == 0 { "no_controller_reports" } else if !errs.is_empty() { "integrity_errors" } else { "ok" };
    let mut warnings: Vec<String> = errs.clone();
    if n_in == 0 { warnings.push("The controller was never read over USB during this recording, so there is no input proof.".into()); }
    if hmatch == Some(false) { warnings.push("haptics.jsonl does not match the haptic records committed in inputs.jsonl".into()); }
    if haptics_v["imu_present"] == json!(false) { warnings.push("Controller reports carried no IMU data: haptic detection was not possible.".into()); }
    if gaps > 0 { warnings.push(format!("{gaps} controller dropout/missing period(s) recorded; see status lines in inputs.jsonl")); }
    json!({
        "schema": SCHEMA, "status": status, "device": device,
        "inputs_file": "inputs.jsonl", "inputs_sha256": file_sha, "inputs_chain_head": head, "chunks": chunks.len(), "reports_logged": n_in,
        "clock": "inputs t_us and manifest/checkpoint mono_ms come from the same QorCap monotonic clock",
        "human": human, "alignment": alignment, "haptics": haptics_v, "warnings": warnings, "limit": LIMIT,
    })
}

/// Compact integer-only summary for the receipt draft (`signals.input`) and bundle.json (`input`).
pub fn receipt_fields(proof: &Value) -> Value {
    let a = &proof["alignment"];
    json!({
        "status": proof["status"], "device": proof["device"],
        "human_permille": proof["human"]["score_permille"], "human_label": proof["human"]["label"],
        "match_permille": a["match_permille"], "chance_permille": a["chance_permille"], "offset_ms": a["offset_ms"],
        "inputs_sha256": proof["inputs_sha256"], "inputs_chain_head": proof["inputs_chain_head"],
        "haptic_events": proof["haptics"]["events"], "haptic_high_confidence": proof["haptics"]["high_confidence"],
        "haptics_sha256": proof["haptics"]["haptics_sha256"],
        "claims": LIMIT,
    })
}

/// Compute the proof and write it into the bundle: input_proof.json, receipt.draft.json `signals.input`,
/// bundle.json `input` (+ refreshed `receipt_draft_sha256`). Existing fields are untouched.
pub fn apply(dir: &Path, ffmpeg: Option<&str>, n_frames: usize) -> Result<Value, String> {
    let proof = build(dir, ffmpeg, n_frames);
    let w = |n: &str, v: &Value| std::fs::write(dir.join(n), serde_json::to_string_pretty(v).unwrap()).map_err(|e| format!("write {n}: {e}"));
    w("input_proof.json", &proof)?;
    let fields = receipt_fields(&proof);
    let rp = dir.join("receipt.draft.json");
    let mut draft_sha = None;
    if let Ok(s) = std::fs::read_to_string(&rp) {
        if let Ok(mut r) = serde_json::from_str::<Value>(&s) {
            if !r["signals"].is_object() { r["signals"] = json!({}); }
            r["signals"]["input"] = fields.clone();
            ev::jcs(&r).map_err(|e| format!("receipt draft would not be JCS-safe: {e}"))?;
            draft_sha = Some(ev::jcs_sha(&r));
            w("receipt.draft.json", &r)?;
        }
    }
    let bp = dir.join("bundle.json");
    if let Ok(s) = std::fs::read_to_string(&bp) {
        if let Ok(mut b) = serde_json::from_str::<Value>(&s) {
            b["input"] = fields;
            b["input_proof_sha256"] = json!(ev::sha256_hex(serde_json::to_string_pretty(&proof).unwrap().as_bytes()));
            if let Some(h) = draft_sha { b["receipt_draft_sha256"] = json!(h); }
            w("bundle.json", &b)?;
        }
    }
    Ok(proof)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::hid::{self, Pad};
    use crate::inputs::InputLog;
    #[test]
    fn apply_adds_fields_backward_compatibly() {
        let dir = std::env::temp_dir().join(format!("qorcap-proof-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir); std::fs::create_dir_all(&dir).unwrap();
        let mut l = InputLog::new(Some(dir.join("inputs.jsonl")), true, 1_000);
        l.on_status(5, "connected", "DualSense Edge (USB)");
        let (recs, _) = humanlike::synth::human(3);
        for r in &recs {
            let p = Pad { buttons: r["b"].as_u64().unwrap() as u32, lx: r["lx"].as_u64().unwrap() as u8, ly: r["ly"].as_u64().unwrap() as u8,
                r2: r["r2"].as_u64().unwrap() as u8, ..Pad::neutral() };
            l.on_report(r["t_us"].as_u64().unwrap(), &p);
        }
        l.seal(1, 99);
        let draft = ev::draft_receipt("m1", "home", "local:p", 1, 2, &json!({"home": 7, "away": 3, "quarter": 4, "t_ms": 2, "basis": "clock_zero"}), &[]);
        std::fs::write(dir.join("receipt.draft.json"), serde_json::to_string(&draft).unwrap()).unwrap();
        std::fs::write(dir.join("bundle.json"), r#"{"schema":"qorcap-bundle/v0","receipt_draft_sha256":"x"}"#).unwrap();
        let proof = apply(&dir, None, 15).unwrap();
        assert_eq!(proof["status"], "ok");
        assert_eq!(proof["device"], "DualSense Edge (USB)");
        let r: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("receipt.draft.json")).unwrap()).unwrap();
        assert!(r["signals"]["motion_permille"].is_object(), "existing signal kept");
        assert!(r["signals"]["input"]["human_permille"].as_i64().unwrap() >= 700);
        assert!(r["signals"]["input"]["claims"].as_str().unwrap().contains("not show who"));
        let b: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("bundle.json")).unwrap()).unwrap();
        assert_eq!(b["receipt_draft_sha256"], ev::jcs_sha(&r));
        assert_eq!(b["input"]["inputs_chain_head"], l.head);
        let _ = std::fs::remove_dir_all(&dir);
        let _ = hid::CROSS;
    }
    #[test]
    fn haptics_jsonl_is_written_committed_and_summarized() {
        let dir = std::env::temp_dir().join(format!("qorcap-haptic-proof-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir); std::fs::create_dir_all(&dir).unwrap();
        let anchor = 1_700_000_000_000u64;
        let mut l = InputLog::new(Some(dir.join("inputs.jsonl")), true, anchor);
        l.on_status(5, "connected", "DualSense Edge (USB)");
        let s = haptics::synth::stream(14.0, |t| {
            let v = if (5.0..5.25).contains(&t) || (11.0..11.2).contains(&t) { 150.0 * (2.0 * std::f64::consts::PI * 60.0 * t).sin() } else { 0.0 };
            ([v, 0.0, 0.0], [v, 0.0, 0.0], 0)
        });
        let mut seg = 0;
        for (t, p) in &s {
            l.on_report(*t, p);
            if *t >= (seg + 1) * 2_000_000 { l.seal(seg, *t); seg += 1; }
        }
        l.seal(seg, 14_000_000);
        // score change at 11.5 s (wall clock) -> haptic at 11.0 s is "near" it
        std::fs::write(dir.join("timeline.jsonl"), format!("{{\"home\":0,\"away\":0,\"t_ms\":{}}}\n{{\"home\":7,\"away\":0,\"t_ms\":{}}}\n", anchor + 1000, anchor + 11_500)).unwrap();
        let hb = std::fs::read(dir.join("haptics.jsonl")).unwrap();
        let p = build(&dir, None, 15);
        let h = &p["haptics"];
        assert_eq!(h["events"], 2, "{h:#}");
        assert_eq!(h["high_confidence"], 2);
        assert_eq!(h["imu_present"], true);
        assert_eq!(h["imu_rate_hz"], 250);
        assert_eq!((h["score_changes"].as_u64(), h["score_changes_with_haptic"].as_u64()), (Some(1), Some(1)));
        assert_eq!(h["haptics_sha256"], ev::sha256_hex(&hb));
        assert_eq!(h["haptics_file_matches_inputs"], true);
        assert_eq!(p["status"], "ok", "{p:#}");
        let rf = receipt_fields(&p);
        assert_eq!(rf["haptic_events"], 2);
        ev::jcs(&json!({"x": rf})).expect("receipt fields stay JCS/no-float safe");
        // each haptic line carries chunk_index = the segment whose checkpoint committed it, and wall time
        let first: Value = serde_json::from_str(std::str::from_utf8(&hb).unwrap().lines().next().unwrap()).unwrap();
        assert_eq!(first["chunk_index"], first["seg"]);
        assert_eq!(first["t_wall_ms"].as_u64().unwrap(), anchor + first["t_mono_ms"].as_u64().unwrap());
        // tampering with haptics.jsonl is caught
        std::fs::write(dir.join("haptics.jsonl"), String::from_utf8(hb).unwrap().replacen("\"peak\":", "\"peak\":1", 1)).unwrap();
        let p2 = build(&dir, None, 15);
        assert_eq!(p2["haptics"]["haptics_file_matches_inputs"], false);
        assert!(p2["warnings"].as_array().unwrap().iter().any(|w| w.as_str().unwrap().contains("haptics.jsonl")));
        let _ = std::fs::remove_dir_all(&dir);
    }
    #[test]
    fn missing_log_is_explicit() {
        let p = build(Path::new("/nonexistent-qorcap"), None, 15);
        assert_eq!(p["status"], "no_input_log");
        assert!(receipt_fields(&p)["human_permille"].is_null());
    }
    #[test]
    fn end_to_end_alignment_with_real_video() {
        let ok = std::process::Command::new("ffmpeg").arg("-version").stdout(std::process::Stdio::null()).status().map(|s| s.success()).unwrap_or(false);
        if !ok { eprintln!("ffmpeg not on PATH; skipping"); return; }
        let dir = std::env::temp_dir().join(format!("qorcap-e2e-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir); std::fs::create_dir_all(&dir).unwrap();
        // Screen flips brightness at T = 2, 5, 8, ... (and back at 3, 6, 9, ...). Presses land 200 ms before each flip at 2+3k.
        let st = std::process::Command::new("ffmpeg").args(["-v", "error", "-y", "-f", "lavfi", "-i",
            "color=c=gray:s=160x90:r=30,geq=lum='if(gte(mod(T\\,3)\\,2)\\,210\\,40)':cb=128:cr=128", "-t", "30", "-pix_fmt", "yuv420p",
            dir.join("video.mp4").to_str().unwrap()]).status().unwrap();
        assert!(st.success());
        std::fs::write(dir.join("manifest.jsonl"), "{\"type\":\"segment\",\"seq\":0,\"kind\":\"init\",\"file\":\"video.mp4\",\"mono_ms\":0}\n").unwrap();
        let mut l = InputLog::new(Some(dir.join("inputs.jsonl")), true, 0);
        l.on_status(1, "connected", "DualSense (USB)");
        for k in 0..9u64 {
            let t = (1_800 + 3_000 * k) * 1000;
            l.on_report(t, &Pad { buttons: hid::CROSS, ..Pad::neutral() });
            l.on_report(t + 80_000, &Pad::neutral());
        }
        l.seal(1, 30_000_000);
        let p = build(&dir, Some("ffmpeg"), 15);
        let a = &p["alignment"];
        assert_eq!(a["presses_tested"], 9, "{p:#}");
        assert!(a["match_permille"].as_i64().unwrap() >= 880, "{a:#}");
        assert_eq!(a["chance_permille"], 0, "{a:#}");
        let off = a["offset_ms"].as_i64().unwrap();
        assert!((166..=266).contains(&off), "offset {off}");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
