//! Adversarial bundles for the checks QorCap already runs.
//!
//! The thresholds are the shipped ones: hash links from [`crate::bundle::verify`], Ed25519 over
//! JCS with `signatures` removed (QorTime `signing_bytes`), challenge rule v2 at 0..=2500 ms and
//! ±300 ms of the median (`RULE2_MAX_LATENCY_MS` / `RULE2_CONSISTENCY_MS`), [`crate::humanlike::score`],
//! [`crate::align::align_tracks`], and `contracts/receipt.v0.json` (`additionalProperties: false`).
//! Nothing here widens a bound or skips a failing check to make a row look caught.

use crate::{align, bundle, evidence as ev, hid, humanlike, inputproof, inputs};
use ed25519_dalek::{Signature, Signer, SigningKey, Verifier, VerifyingKey};
use serde::Serialize;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

pub const SCHEMA: &str = "qorcap-redteam-matrix/v0";
/// QorTime `qor_receipt.challenge.RULE2_MAX_LATENCY_MS`.
pub const RULE2_MAX_LATENCY_MS: i64 = 2_500;
/// QorTime `qor_receipt.challenge.RULE2_CONSISTENCY_MS`. A difference of exactly this passes.
pub const RULE2_CONSISTENCY_MS: i64 = 300;
const RULE2_MAX_WINDOW_MS: i64 = 120_000;
const LAYERS: [&str; 8] = [
    "hash chain",
    "signature",
    "challenge rule v2",
    "humanlike check",
    "input-video alignment",
    "input proof",
    "schema",
    "referee-only",
];

const RECEIPT_SCHEMA: &str = include_str!("../contracts/receipt.v0.json");

#[derive(Clone, Debug, Serialize)]
struct Layer {
    name: String,
    result: String,
    detail: String,
}

#[derive(Clone, Serialize)]
struct Closure {
    software_can_close: bool,
    hardware_required: bool,
    note: String,
}

#[derive(Clone, Serialize)]
struct Row {
    id: String,
    attack: String,
    variant: String,
    verdict: String,
    caught_by: Vec<String>,
    weak_layers: Vec<String>,
    blocks_verify_bundle: bool,
    /// True when a layer above rejects the attack on its own terms. `verify-bundle` is separate:
    /// it does not run the challenge rule, the human-likeness score, or the schema.
    fail_closed: bool,
    layers: Vec<Layer>,
    #[serde(skip_serializing_if = "Option::is_none")]
    closure: Option<Closure>,
}

#[derive(Serialize)]
struct Matrix {
    schema: String,
    rules_changed: bool,
    rule_v2: Value,
    layers: Vec<String>,
    rows: Vec<Row>,
}

struct Phase {
    id: &'static str,
    phase: &'static str,
    nonce: &'static str,
    issued_ms: i64,
    deadline_ms: i64,
    pause_seen_ms: Option<i64>,
    claim_ok: bool,
    claim_reason: &'static str,
    claim_latency: Option<i64>,
}

struct Built {
    _dir: TempDir,
    receipt: Value,
    recs: Vec<Value>,
    phases: Vec<Phase>,
    verify_errors: Vec<String>,
}

struct TempDir(PathBuf);
impl Drop for TempDir {
    fn drop(&mut self) { let _ = std::fs::remove_dir_all(&self.0); }
}

#[derive(Clone, Copy)]
enum Inputs {
    Human,
    Macro,
    SecondPad,
}

struct Plan {
    salt: u8,
    inputs: Inputs,
    device: &'static str,
    device_id: &'static str,
    /// Pause onset minus the OPTIONS press. `None` means the attack does not inject a response.
    latency_ms: Option<i64>,
    /// Receipt claims the challenge succeeded even when the log has no response.
    forge_challenge_ok: bool,
    splice_after_seal: bool,
    /// Edited bytes, then a new chain and a new signature. Distinct from a sealed splice.
    reseal: bool,
    tamper_score: bool,
    extra_field: bool,
}

pub fn matrix() -> Value { serde_json::to_value(build_matrix()).unwrap() }

pub fn matrix_pretty() -> String {
    format!("{}\n", serde_json::to_string_pretty(&build_matrix()).unwrap())
}

pub fn markdown() -> String { render_markdown(&build_matrix()) }

fn build_matrix() -> Matrix {
    let relays = [500, 1000, 1500, 2000, 2400, 3000];
    let mut rows = vec![
        row_control(),
        row_replay(),
    ];
    for ms in relays { rows.push(row_relay(ms)); }
    rows.push(row_virtual());
    rows.push(row_splice(false));
    rows.push(row_splice(true));
    rows.push(row_macro());
    rows.push(row_second_pad());
    rows.push(row_tamper());
    rows.push(row_extra());
    Matrix {
        schema: SCHEMA.into(),
        rules_changed: false,
        rule_v2: json!({
            "max_latency_ms": RULE2_MAX_LATENCY_MS,
            "consistency_ms": RULE2_CONSISTENCY_MS,
            "max_window_ms": RULE2_MAX_WINDOW_MS,
            "source": "qor_receipt.challenge RULE2_MAX_LATENCY_MS and RULE2_CONSISTENCY_MS; a gap of exactly 300 ms passes",
        }),
        layers: LAYERS.iter().map(|s| s.to_string()).collect(),
        rows,
    }
}

fn row_control() -> Row {
    let built = build(&Plan::honest(1));
    let align = alignment_layer(0, false);
    finish("honest_control", "control", "human inputs, live nonce answers at 1750 ms, USB3.0 Video", &built, align, None)
}

fn row_replay() -> Row {
    let mut plan = Plan::honest(2);
    plan.latency_ms = None;
    plan.forge_challenge_ok = true;
    let built = build(&plan);
    let align = pass_layer("input-video alignment", "the replayed pad log still lines up with the replayed picture; the fresh nonce is the check that sees staleness");
    finish("replay_stale_no_fresh_nonce", "pre-recorded video replayed as if live", "stale frames, no fresh nonce response", &built, align, None)
}

fn row_relay(ms: i64) -> Row {
    let mut plan = Plan::honest(3);
    plan.latency_ms = Some(ms);
    let built = build(&plan);
    let align = alignment_layer(ms, true);
    let closure = if ms > RULE2_MAX_LATENCY_MS {
        None
    } else {
        Some(Closure {
            software_can_close: true,
            hardware_required: false,
            note: format!(
                "Rule v2 accepts a constant latency of {ms} ms (ceiling {RULE2_MAX_LATENCY_MS} ms, and every success within ±{RULE2_CONSISTENCY_MS} ms of the median). The aligner reports the same delay as offset_ms and still scores a match; its search is ±3 s so a fixed pipeline delay is calibration, not a failure. Software can close a delay of this size by bounding accepted latency to the band measured on a live capture (recorded pause onsets sit near 1750 ms, spread under 100 ms). This suite does not add that bound. A relay smaller than that honest spread would remain inside the band."
            ),
        })
    };
    let secs = format!("{:.1}", ms as f64 / 1000.0);
    finish(&format!("relay_{}s", secs.replace('.', "_")), "constant relay delay, video versus pad log", &format!("{secs} s"), &built, align, closure)
}

fn row_virtual() -> Row {
    let mut plan = Plan::honest(4);
    plan.device = "OBS Virtual Camera";
    plan.device_id = "@device_sw_{obs-virtual-cam}";
    let built = build(&plan);
    let align = pass_layer("input-video alignment", "device identity is a manifest string; press timing is unchanged");
    let closure = Some(Closure {
        software_can_close: false,
        hardware_required: true,
        note: "device and device_id are labels on the manifest line. The hash link is prev, seq, and the segment sha256, and verify-bundle does not read the device fields. An unmodified recorder can refuse a known virtual-camera name when it opens the device. A recorder willing to lie writes any name, and the bundle still verifies. Proving the frames entered through a physical HDMI capture device takes attestation of that device, or a hash of the HDMI stream taken before a PC can substitute a virtual camera.".into(),
    });
    finish("virtual_capture_device", "fake or virtual capture device identity", "OBS Virtual Camera written at seal time", &built, align, closure)
}

fn row_splice(reseal: bool) -> Row {
    let mut plan = Plan::honest(if reseal { 6 } else { 5 });
    if reseal { plan.reseal = true; } else { plan.splice_after_seal = true; }
    let built = build(&plan);
    let align = pass_layer("input-video alignment", "press times are unchanged; the edited bytes are not a decoded picture");
    let (id, variant, closure) = if reseal {
        ("splice_rehashed_resigned", "segment replaced, chain and receipt re-signed with the software key",
         Some(Closure {
             software_can_close: false,
             hardware_required: true,
             note: "The sealed-segment row fails the hash chain. This row rebuilds the manifest, the checkpoint chain, and both receipt signatures over the edited bytes, so those checks agree with the new file. The official referee re-hashes the segments it samples and re-reads the scorebug on that sample; a splice outside the sample stays consistent with the new chain (spot-check miss probability is about (1-f)^16 plus the forced tail and challenge segments). A head fixed before the file can be edited is capture-before-PC. A co-signer who already holds the pre-edit head would reject the new signature; a first submit of the rebuilt bundle has no earlier head on record.".into(),
         }))
    } else {
        ("splice_sealed_segment", "one segment's bytes flipped after the manifest was sealed", None)
    };
    finish(id, "edited or spliced video segment", variant, &built, align, closure)
}

fn row_macro() -> Row {
    let mut plan = Plan::honest(7);
    plan.inputs = Inputs::Macro;
    let built = build(&plan);
    let raw = alignment_for_presses(&key_presses_us(&built.recs), 6, false);
    let align = Layer { name: "input-video alignment".into(), result: raw.result, detail: raw.detail };
    finish("turbo_macro", "turbo or macro input", "perfectly periodic presses (100 ms CROSS, fixed holds)", &built, align, None)
}

fn row_second_pad() -> Row {
    let mut plan = Plan::honest(8);
    plan.inputs = Inputs::SecondPad;
    let built = build(&plan);
    let statuses: Vec<String> = built.recs.iter().filter(|r| r["type"] == "status")
        .map(|r| format!("{} {}", r["state"].as_str().unwrap_or(""), r["detail"].as_str().unwrap_or(""))).collect();
    let align = pass_layer("input-video alignment", format!("status lines in the sealed log: {}. Alignment does not read the serial", statuses.join(" | ")));
    finish("second_pad", "second pad mid-match", "different serial and descriptor after an unplug", &built, align, None)
}

fn row_tamper() -> Row {
    let mut plan = Plan::honest(9);
    plan.tamper_score = true;
    let built = build(&plan);
    let align = pass_layer("input-video alignment", "the video and the pad log are untouched");
    finish("receipt_tamper_score", "receipt field tampering", "result.home_score changed after signing", &built, align, None)
}

fn row_extra() -> Row {
    let mut plan = Plan::honest(10);
    plan.extra_field = true;
    let built = build(&plan);
    let align = pass_layer("input-video alignment", "the video and the pad log are untouched");
    finish("receipt_extra_field", "receipt extra field", "top-level device_attest added, then re-signed", &built, align, None)
}

fn finish(id: &str, attack: &str, variant: &str, built: &Built, align: Layer, closure: Option<Closure>) -> Row {
    let hash = hash_layer(&built.verify_errors);
    let sig = signature_layer(&built.receipt);
    let challenge = challenge_layer(&built.recs, &built.phases);
    let human = human_layer(&built.recs);
    let schema = schema_layer(&built.receipt);
    let referee = referee_layer(id, &hash, &challenge);
    let proof = input_proof_layer(&built.recs);
    let mut layers = vec![hash, sig, challenge, human, align, proof, schema, referee];
    // Keep the published layer order even if a helper returns a different name.
    layers.sort_by_key(|l| LAYERS.iter().position(|n| *n == l.name).unwrap_or(99));
    let caught_by: Vec<String> = layers.iter().filter(|l| l.result == "fail").map(|l| l.name.clone()).collect();
    let weak_layers: Vec<String> = layers.iter().filter(|l| l.result == "weak").map(|l| l.name.clone()).collect();
    let verdict = if id == "honest_control" {
        "pass"
    } else if !caught_by.is_empty() {
        "caught"
    } else if !weak_layers.is_empty() {
        "weak"
    } else {
        "missed"
    };
    let fail_closed = layers.iter().any(|l| {
        l.result == "fail" && matches!(l.name.as_str(), "hash chain" | "signature" | "challenge rule v2" | "schema")
    });
    Row {
        id: id.into(), attack: attack.into(), variant: variant.into(),
        verdict: verdict.into(), caught_by, weak_layers,
        blocks_verify_bundle: !built.verify_errors.is_empty(),
        fail_closed, layers, closure,
    }
}

impl Plan {
    fn honest(salt: u8) -> Self {
        Self {
            salt, inputs: Inputs::Human, device: "USB3.0 Video",
            device_id: "@device_pnp_\\\\?\\usb#vid_534d&pid_2109",
            latency_ms: Some(1750), forge_challenge_ok: false,
            splice_after_seal: false, reseal: false, tamper_score: false, extra_field: false,
        }
    }
}

fn build(plan: &Plan) -> Built {
    // Two `cargo test` threads can build the same salt at once. The directory name has to
    // differ or they overwrite one inputs.jsonl and the hash chain fails for the wrong reason.
    static SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let n = SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("qorcap-redteam-{}-{}-{}", plan.salt, std::process::id(), n));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let hold = TempDir(dir.clone());

    let mut log = inputs::InputLog::new(Some(dir.join("inputs.jsonl")), true, 1_700_000_000_000);
    let max_t = match plan.inputs {
        Inputs::Human => load_synth(&mut log, false),
        Inputs::Macro => load_synth(&mut log, true),
        Inputs::SecondPad => load_second_pad(&mut log),
    };
    let start_ms = (max_t / 1000) as i64 + 5_000;
    let phases = phases_for(plan, start_ms);
    if plan.latency_ms.is_some() { inject_patterns(&mut log, &phases); }
    let (input_hash, input_head, input_n) = log.seal(0, max_t.max(1));

    let salt = if plan.reseal { plan.salt.wrapping_add(99) } else { plan.salt };
    let (video, lines, _head) = seal_video(salt, plan.device, plan.device_id);
    let cps = seal_checkpoints(&lines, &input_hash, &input_head, input_n);
    std::fs::write(dir.join("video.mp4"), &video).unwrap();
    write_jsonl(&dir.join("manifest.jsonl"), &lines);
    write_jsonl(&dir.join("checkpoints.jsonl"), &cps);
    std::fs::write(dir.join("bundle.json"), serde_json::to_string(&json!({
        "schema": "qorcap-bundle/v0", "qorcap_version": env!("CARGO_PKG_VERSION"),
        "challenge_rule": 2, "match_id": "redteam", "role": "home",
        "device": plan.device, "device_id": plan.device_id,
    })).unwrap()).unwrap();

    if plan.splice_after_seal {
        let off = lines[3]["offset"].as_u64().unwrap() as usize;
        let mut buf = std::fs::read(dir.join("video.mp4")).unwrap();
        buf[off] ^= 0xff;
        std::fs::write(dir.join("video.mp4"), buf).unwrap();
    }

    let player = software_key(1);
    let referee = software_key(2);
    let mut receipt = unsigned_receipt(&cps, &phases, &player, &referee);
    if plan.extra_field {
        receipt["device_attest"] = json!("should-be-rejected");
    }
    let mut receipt = sign_receipt(receipt, &player, &referee);
    if plan.tamper_score {
        receipt["result"]["home_score"] = json!(receipt["result"]["home_score"].as_i64().unwrap() + 1);
    }
    std::fs::write(dir.join("receipt.json"), serde_json::to_string_pretty(&receipt).unwrap()).unwrap();
    write_jsonl(&dir.join("challenges.jsonl"), &phases.iter().map(phase_json).collect::<Vec<_>>());

    let report = bundle::verify(&dir).unwrap_or_else(|e| bundle::Report {
        segments: 0, bytes: 0, checkpoints: 0, chain_head: String::new(),
        errors: vec![e], input_chunks: 0, input_head: None,
    });
    let (recs, _) = inputs::read_file(&dir.join("inputs.jsonl")).unwrap_or_default();
    Built { _dir: hold, receipt, recs, phases, verify_errors: report.errors }
}

fn load_synth(log: &mut inputs::InputLog, macro_bot: bool) -> u64 {
    log.on_status_with_id(1_000, "connected", "DualSense Edge (USB) serial=HONEST0001", Some("serial:HONEST0001"));
    let recs = if macro_bot { humanlike::synth::macro_bot().0 } else { humanlike::synth::human(1).0 };
    let mut max_t = 1_000u64;
    for r in &recs {
        let t = r["t_us"].as_u64().unwrap_or(0);
        max_t = max_t.max(t);
        log.on_report(t, &pad_from(r));
    }
    max_t
}

fn load_second_pad(log: &mut inputs::InputLog) -> u64 {
    log.on_status_with_id(1_000, "connected", "DualSense Edge (USB) serial=AAAA1111 path=hid#vid_054c&pid_0df2#1", Some("serial:AAAA1111"));
    let (recs, _) = humanlike::synth::human(1);
    let mid = recs.len() / 2;
    let mut max_t = 1_000u64;
    for (i, r) in recs.iter().enumerate() {
        let t = r["t_us"].as_u64().unwrap_or(0);
        max_t = max_t.max(t);
        if i == mid {
            log.on_status(t, "unplugged", "usb disconnect");
            log.on_status_with_id(t + 1_000, "connected", "DualSense Edge (USB) serial=BBBB2222 path=hid#vid_054c&pid_0df2#2 descriptor=054c:0df2", Some("serial:BBBB2222"));
        }
        log.on_report(t, &pad_from(r));
    }
    max_t
}

fn phases_for(plan: &Plan, start_ms: i64) -> Vec<Phase> {
    let nonces = [
        ("kickoff", "kickoff", "00112233445566778899aabbccddeeff"),
        ("halftime", "halftime", "ffffffffffffffffffffffffffffffff"),
        ("end", "end", "0102030405060708090a0b0c0d0e0f10"),
    ];
    let mut out = vec![];
    let mut issued = start_ms;
    for (id, phase, nonce) in nonces {
        let deadline = issued + 90_000;
        let options_ms = issued + 500;
        let (pause, claim_ok, claim_reason, claim_latency) = if let Some(lat) = plan.latency_ms {
            let pause = Some(options_ms + lat);
            let ok = (0..=RULE2_MAX_LATENCY_MS).contains(&lat);
            let reason = if ok { "ok" } else { "video_latency_too_high" };
            (pause, ok, reason, Some(lat))
        } else if plan.forge_challenge_ok {
            (None, true, "ok", Some(1750))
        } else {
            (None, false, "no_pause_press", None)
        };
        out.push(Phase { id, phase, nonce, issued_ms: issued, deadline_ms: deadline, pause_seen_ms: pause, claim_ok, claim_reason, claim_latency });
        issued = deadline + 1_000;
    }
    out
}

fn inject_patterns(log: &mut inputs::InputLog, phases: &[Phase]) {
    for ph in phases {
        let pattern = ev::pattern_for(ph.nonce).unwrap();
        let mut t_us = (ph.issued_ms as u64 + 500) * 1000;
        for name in pattern {
            let bit = hid::BUTTON_NAMES.iter().find(|(_, n)| *n == name).unwrap().0;
            let mut down = hid::Pad::neutral();
            down.buttons = bit;
            log.on_report(t_us, &down);
            t_us += 80_000;
            log.on_report(t_us, &hid::Pad::neutral());
            t_us += 120_000;
        }
    }
}

fn seal_video(salt: u8, device: &str, device_id: &str) -> (Vec<u8>, Vec<Value>, String) {
    let mut chain = ev::SegmentChain::default();
    let mut video = vec![];
    let mut lines = vec![];
    let mut off = 0u64;
    for i in 0..7u64 {
        let mut body = format!("qorcap-redteam-segment-{i}-salt-{salt}-").into_bytes();
        body.extend(std::iter::repeat(salt.wrapping_add(i as u8)).take(64));
        let (seq, sha, prev, head) = chain.push(&body);
        let frames = 60 * (i + 1);
        let mono = 2_000 * (i + 1);
        lines.push(json!({
            "type": "segment", "seq": seq, "kind": "media", "file": "video.mp4",
            "offset": off, "bytes": body.len(), "sha256": sha, "prev": prev, "chain_head": head,
            "mono_ms": mono, "wall_ms": 1_700_000_000_000u64 + mono, "frames": frames,
            "device": device, "device_id": device_id,
        }));
        off += body.len() as u64;
        video.extend_from_slice(&body);
    }
    let head = chain.head;
    (video, lines, head)
}

fn seal_checkpoints(lines: &[Value], input_hash: &str, input_head: &str, input_n: usize) -> Vec<Value> {
    let mut cp = ev::CheckpointChain::default();
    let mut out = vec![];
    for (i, line) in lines.iter().enumerate() {
        let mut ext = json!({
            "trigger": "segment", "segment_seq": i as u64,
            "segment_hash": line["sha256"], "chain_head": line["chain_head"],
            "mono_ms": line["mono_ms"], "match_id": "redteam", "role": "home",
        });
        let ih = if i == 0 {
            ext["input_seg"] = json!(0);
            ext["input_n"] = json!(input_n);
            ext["input_head"] = json!(input_head);
            Some(input_hash.to_string())
        } else { None };
        out.push(cp.next_with_input(line["mono_ms"].as_u64().unwrap(), ext, ih).unwrap());
    }
    out
}

fn unsigned_receipt(cps: &[Value], phases: &[Phase], player: &SigningKey, referee: &SigningKey) -> Value {
    let ended = cps.last().and_then(|c| c["t_ms"].as_u64()).unwrap_or(2);
    let mut draft = ev::draft_receipt("redteam", "home", "local:redteam", 1, ended,
        &json!({"home": 7, "away": 3, "quarter": 4, "t_ms": ended, "basis": "clock_zero"}), &[]);
    draft["players"][0]["pubkey"] = json!(ev::hex(&player.verifying_key().to_bytes()));
    draft["players"][0]["key_kind"] = json!("software");
    draft["referee"]["pubkey"] = json!(ev::hex(&referee.verifying_key().to_bytes()));
    draft["referee"]["key_id"] = json!(key_id(referee));
    draft["referee"]["key_kind"] = json!("local-self-attested");
    draft["challenges"] = json!(phases.iter().map(phase_json).collect::<Vec<_>>());
    draft["checkpoints"] = json!({"home": cps.iter().map(receipt_cp).collect::<Vec<_>>()});
    draft["signatures"] = json!({"players": {}, "referee": Value::Null});
    draft
}

fn receipt_cp(c: &Value) -> Value {
    json!({
        "seq": c["seq"], "t_ms": c["t_ms"], "frames_hash": c["frames_hash"],
        "input_hash": c["input_hash"], "prev": c["prev"], "hash": c["hash"],
        "referee_ts_ms": c["t_ms"], "referee_sig": "ab".repeat(64),
    })
}

fn phase_json(ph: &Phase) -> Value {
    let mut result = json!({"ok": ph.claim_ok, "reason": ph.claim_reason, "rule": 2});
    if let Some(lat) = ph.claim_latency { result["latency_ms"] = json!(lat); }
    json!({
        "id": ph.id, "role": "home", "phase": ph.phase, "nonce": ph.nonce,
        "pattern": ev::pattern_for(ph.nonce).unwrap(),
        "issued_ms": ph.issued_ms, "deadline_ms": ph.deadline_ms,
        "result": result,
    })
}

fn hash_layer(errors: &[String]) -> Layer {
    if errors.is_empty() {
        pass_layer("hash chain", "verify-bundle: video bytes, checkpoint links, and input chunks agree")
    } else {
        fail_layer("hash chain", errors.join("; "))
    }
}

fn signature_layer(receipt: &Value) -> Layer {
    match verify_signatures(receipt) {
        Ok(()) => pass_layer("signature", "Ed25519 over JCS(receipt without signatures) matches the player and referee keys in the receipt"),
        Err(e) => fail_layer("signature", e),
    }
}

fn challenge_layer(recs: &[Value], phases: &[Phase]) -> Layer {
    let presses = down_presses(recs);
    let mut reasons = vec![];
    let mut ok_lats = vec![];
    for ph in phases {
        if ph.deadline_ms - ph.issued_ms > RULE2_MAX_WINDOW_MS {
            reasons.push(format!("{}: challenge_window_too_long", ph.id));
            continue;
        }
        let pattern = ev::pattern_for(ph.nonce).unwrap();
        let window: Vec<(i64, String)> = presses.iter()
            .filter(|(t, _)| *t >= ph.issued_ms && *t <= ph.deadline_ms)
            .cloned().collect();
        let prefs: Vec<&str> = pattern.iter().copied().collect();
        let wrefs: Vec<(i64, &str)> = window.iter().map(|(t, b)| (*t, b.as_str())).collect();
        let got = verify_response(&prefs, &wrefs, ph.pause_seen_ms, RULE2_MAX_LATENCY_MS);
        if got.ok {
            if let Some(lat) = got.latency_ms { ok_lats.push(lat); }
        } else {
            reasons.push(format!("{}: {}", ph.id, got.reason));
        }
    }
    if reasons.is_empty() {
        let over = deviations_over(&ok_lats);
        if over.is_empty() {
            pass_layer("challenge rule v2", format!("latencies {ok_lats:?} ms inside 0..={RULE2_MAX_LATENCY_MS} and within ±{RULE2_CONSISTENCY_MS} of the median"))
        } else {
            fail_layer("challenge rule v2", format!("latency_inconsistent at {over:?}; latencies {ok_lats:?}"))
        }
    } else {
        fail_layer("challenge rule v2", reasons.join("; "))
    }
}

fn input_proof_layer(recs: &[Value]) -> Layer {
    let claim = inputproof::controller_corroboration(recs);
    let detail = claim["detail"].as_str().unwrap_or("").to_string();
    if claim["ok"] == json!(true) { pass_layer("input proof", detail) } else { fail_layer("input proof", detail) }
}

fn human_layer(recs: &[Value]) -> Layer {
    let s = humanlike::score(recs);
    let label = s["label"].as_str().unwrap_or("");
    let score = s["score_permille"].to_string();
    if label.starts_with("bot-like") {
        Layer { name: "humanlike check".into(), result: "fail".into(),
            detail: format!("label {label}, score_permille {score}. Advisory: verify-bundle stays OK and input_proof status stays ok") }
    } else {
        pass_layer("humanlike check", format!("label {label}, score_permille {score}"))
    }
}

fn schema_layer(receipt: &Value) -> Layer {
    let schema: Value = serde_json::from_str(RECEIPT_SCHEMA).unwrap();
    let mut errs = vec![];
    schema_check(&schema, &schema, receipt, "", &mut errs);
    errs.sort();
    errs.dedup();
    if errs.is_empty() {
        pass_layer("schema", "receipt.v0.json accepts this object")
    } else {
        fail_layer("schema", errs.join("; "))
    }
}

fn referee_layer(id: &str, hash: &Layer, challenge: &Layer) -> Layer {
    match id {
        "splice_rehashed_resigned" => Layer {
            name: "referee-only".into(), result: "weak".into(),
            detail: "A fresh submit is hashed against the manifest the submitter just built. The sampled scorebug re-read can miss a splice outside that sample. Weak, and it does not close the row.".into(),
        },
        "replay_stale_no_fresh_nonce" if challenge.result == "fail" => pass_layer(
            "referee-only", "the official referee runs this same rule v2; the catch is recorded on the challenge layer"),
        _ if hash.result == "fail" => pass_layer("referee-only", "the hash chain already rejects the bytes; the referee re-hashes the same way"),
        _ => pass_layer("referee-only", "no referee-only check rejects this row beyond the layers above"),
    }
}

fn pass_layer(name: &str, detail: impl Into<String>) -> Layer {
    Layer { name: name.into(), result: "pass".into(), detail: detail.into() }
}
fn fail_layer(name: &str, detail: impl Into<String>) -> Layer {
    Layer { name: name.into(), result: "fail".into(), detail: detail.into() }
}

/// `attack` is true for a relay row: a recovered offset is a weak signal, because the aligner does not reject it.
fn alignment_layer(delay_ms: i64, attack: bool) -> Layer {
    let frames: Vec<usize> = (0..100).map(|i| 200 + i * 41).collect();
    let presses: Vec<u64> = frames.iter().map(|&f| (f as f64 * 1000.0 / align::FPS * 1000.0).round() as u64).collect();
    let delay_frames = (delay_ms as f64 / (1000.0 / align::FPS)).round() as usize;
    alignment_for_presses(&presses, delay_frames + 6, attack).into_named(delay_ms)
}

struct AlignRaw { result: String, detail: String }

impl AlignRaw {
    fn into_named(self, delay_ms: i64) -> Layer {
        let detail = if delay_ms == 0 { self.detail } else { format!("injected delay {delay_ms} ms; {}", self.detail) };
        Layer { name: "input-video alignment".into(), result: self.result, detail }
    }
}

fn alignment_for_presses(presses: &[u64], lag_frames: usize, attack: bool) -> AlignRaw {
    if presses.len() < 8 {
        return AlignRaw { result: "pass".into(), detail: "too few key presses to score alignment".into() };
    }
    let frames: Vec<usize> = presses.iter().map(|t| ((*t as f64 / 1000.0) / (1000.0 / align::FPS)).round() as usize).collect();
    let last = frames.iter().copied().max().unwrap_or(0) + lag_frames + 40;
    let track = spike_track(last + 8, &frames, lag_frames);
    let clock = align::FileClock { file: "video.mp4".into(), start_ms: 0.0, ms_per_frame: 1000.0 / align::FPS, end_ms: 1e12, fitted: true };
    let scored = align::align_tracks(&[track], &[clock], presses, align::DEFAULT_N_FRAMES);
    let detail = format!(
        "match_permille {} chance_permille {} offset_ms {} presses_tested {}",
        scored["match_permille"], scored["chance_permille"], scored["offset_ms"], scored["presses_tested"]
    );
    let matched = scored["match_permille"].as_i64().unwrap_or(0);
    let offset = scored["offset_ms"].as_i64();
    if attack && matched >= 700 && offset.is_some() {
        AlignRaw { result: "weak".into(), detail: format!("{detail}. The ±3 s search keeps the match and records the delay as the offset; nothing rejects the bundle") }
    } else {
        AlignRaw { result: "pass".into(), detail }
    }
}

fn spike_track(len: usize, spikes: &[usize], lag: usize) -> align::Energy {
    let mut e: Vec<f32> = (0..len).map(|i| 1.0 + 0.2 * ((i * 7 % 5) as f32)).collect();
    for &s in spikes {
        for j in 0..4 {
            let k = s + lag + j;
            if k < e.len() { e[k] = 40.0; }
        }
    }
    align::Energy { e }
}

fn key_presses_us(recs: &[Value]) -> Vec<u64> { align::key_presses(recs) }

fn pad_from(r: &Value) -> hid::Pad {
    let g = |k: &str, d: u64| r[k].as_u64().unwrap_or(d) as u8;
    hid::Pad {
        buttons: r["b"].as_u64().unwrap_or(0) as u32,
        lx: g("lx", 128), ly: g("ly", 128), rx: g("rx", 128), ry: g("ry", 128),
        l2: g("l2", 0), r2: g("r2", 0),
        ..hid::Pad::neutral()
    }
}

fn down_presses(recs: &[Value]) -> Vec<(i64, String)> {
    let mut rows: Vec<&Value> = recs.iter().filter(|r| r["type"] == "in").collect();
    rows.sort_by_key(|r| r["t_us"].as_u64().unwrap_or(0));
    let mut prev = 0u32;
    let mut out = vec![];
    for r in rows {
        let b = r["b"].as_u64().unwrap_or(0) as u32;
        let rose = b & !prev;
        let t_ms = (r["t_us"].as_u64().unwrap_or(0) / 1000) as i64;
        for (bit, name) in hid::BUTTON_NAMES {
            if rose & bit != 0 { out.push((t_ms, name.to_string())); }
        }
        prev = b;
    }
    out
}

struct Cr { ok: bool, reason: &'static str, latency_ms: Option<i64> }

/// Same shape as QorTime `verify_response` with the rule v2 ceiling passed in.
fn verify_response(pattern: &[&str], presses: &[(i64, &str)], pause_seen_ms: Option<i64>, max_latency_ms: i64) -> Cr {
    let Some(start) = response_start(pattern, presses) else {
        return Cr { ok: false, reason: "no_pause_press", latency_ms: None };
    };
    let seq: Vec<&str> = presses.iter().skip(start).take(pattern.len()).map(|(_, b)| *b).collect();
    if seq != pattern { return Cr { ok: false, reason: "pattern_mismatch", latency_ms: None }; }
    let p_ms = presses[start].0;
    let Some(seen) = pause_seen_ms else {
        return Cr { ok: false, reason: "pause_not_seen_on_video", latency_ms: None };
    };
    let lat = seen - p_ms;
    if lat < 0 { return Cr { ok: false, reason: "video_before_input", latency_ms: Some(lat) }; }
    if lat > max_latency_ms { return Cr { ok: false, reason: "video_latency_too_high", latency_ms: Some(lat) }; }
    Cr { ok: true, reason: "ok", latency_ms: Some(lat) }
}

fn response_start(pattern: &[&str], presses: &[(i64, &str)]) -> Option<usize> {
    let mut first = None;
    for (i, (_, b)) in presses.iter().enumerate() {
        if *b != "OPTIONS" { continue; }
        if first.is_none() { first = Some(i); }
        let seq: Vec<&str> = presses.iter().skip(i).take(pattern.len()).map(|(_, b)| *b).collect();
        if seq == pattern { return Some(i); }
    }
    first
}

fn median_ms(values: &[i64]) -> f64 {
    let mut s = values.to_vec();
    s.sort();
    let mid = s.len() / 2;
    if s.len() % 2 == 1 { s[mid] as f64 } else { (s[mid - 1] + s[mid]) as f64 / 2.0 }
}

fn deviations_over(latencies: &[i64]) -> Vec<usize> {
    if latencies.len() < 2 { return vec![]; }
    let med = median_ms(latencies);
    latencies.iter().enumerate().filter(|(_, l)| (**l as f64 - med).abs() > RULE2_CONSISTENCY_MS as f64).map(|(i, _)| i).collect()
}

fn software_key(which: u8) -> SigningKey {
    let mut b = [0u8; 32];
    for (i, slot) in b.iter_mut().enumerate() {
        *slot = which.wrapping_mul(17).wrapping_add(i as u8).wrapping_add(1);
    }
    SigningKey::from_bytes(&b)
}

fn key_id(key: &SigningKey) -> String { ev::sha256_hex(&key.verifying_key().to_bytes())[..16].to_string() }

fn sign_receipt(mut rec: Value, player: &SigningKey, referee: &SigningKey) -> Value {
    let payload = signing_bytes(&rec);
    rec["signatures"] = json!({
        "players": {"home": {"key_id": key_id(player), "sig": ev::hex(&player.sign(&payload).to_bytes())}},
        "referee": {"key_id": key_id(referee), "sig": ev::hex(&referee.sign(&payload).to_bytes())},
    });
    rec
}

fn signing_bytes(rec: &Value) -> Vec<u8> {
    let mut c = rec.clone();
    c.as_object_mut().unwrap().remove("signatures");
    ev::jcs(&c).unwrap().into_bytes()
}

fn verify_signatures(receipt: &Value) -> Result<(), String> {
    let payload = signing_bytes(receipt);
    let player_pk = unhex(receipt["players"][0]["pubkey"].as_str().unwrap_or(""))?;
    let player_sig = unhex(receipt["signatures"]["players"]["home"]["sig"].as_str().unwrap_or(""))?;
    check_ed25519(&player_pk, &player_sig, &payload).map_err(|e| format!("player signature: {e}"))?;
    let ref_pk = unhex(receipt["referee"]["pubkey"].as_str().unwrap_or(""))?;
    let ref_sig = unhex(receipt["signatures"]["referee"]["sig"].as_str().unwrap_or(""))?;
    check_ed25519(&ref_pk, &ref_sig, &payload).map_err(|e| format!("referee signature: {e}"))?;
    Ok(())
}

fn check_ed25519(pk: &[u8], sig: &[u8], msg: &[u8]) -> Result<(), String> {
    let pk: [u8; 32] = pk.try_into().map_err(|_| "pubkey is not 32 bytes".to_string())?;
    let sig: [u8; 64] = sig.try_into().map_err(|_| "sig is not 64 bytes".to_string())?;
    let key = VerifyingKey::from_bytes(&pk).map_err(|e| e.to_string())?;
    key.verify(msg, &Signature::from_bytes(&sig)).map_err(|e| e.to_string())
}

fn unhex(s: &str) -> Result<Vec<u8>, String> {
    if s.len() % 2 != 0 || !s.bytes().all(|c| c.is_ascii_hexdigit()) { return Err(format!("not hex ({})", s.len())); }
    (0..s.len() / 2).map(|i| u8::from_str_radix(&s[2 * i..2 * i + 2], 16).map_err(|e| e.to_string())).collect()
}

fn schema_check(root: &Value, node: &Value, inst: &Value, path: &str, out: &mut Vec<String>) {
    let node = resolve(root, node);
    if let Some(alts) = node.get("oneOf").and_then(|a| a.as_array()) {
        let mut lists = vec![];
        for alt in alts {
            let mut e = vec![];
            schema_check(root, alt, inst, path, &mut e);
            if e.is_empty() { return; }
            lists.push(e);
        }
        let pick = lists.iter().find(|e| e.iter().any(|s| s.contains("additional"))).unwrap_or_else(|| &lists[0]);
        out.extend(pick.clone());
        return;
    }
    if let Some(t) = node.get("type") {
        if !type_ok(t, inst) {
            out.push(format!("{path}: type"));
            return;
        }
    }
    if let Some(c) = node.get("const") {
        if c != inst { out.push(format!("{path}: const")); }
    }
    if let Some(en) = node.get("enum").and_then(|e| e.as_array()) {
        if !en.iter().any(|x| x == inst) { out.push(format!("{path}: enum")); }
    }
    if let Some(n) = inst.as_i64() {
        if let Some(min) = node.get("minimum").and_then(|m| m.as_i64()) {
            if n < min { out.push(format!("{path}: minimum")); }
        }
        if let Some(max) = node.get("maximum").and_then(|m| m.as_i64()) {
            if n > max { out.push(format!("{path}: maximum")); }
        }
    }
    if let (Some(pat), Some(s)) = (node.get("pattern").and_then(|p| p.as_str()), inst.as_str()) {
        if !hex_pattern(pat, s) { out.push(format!("{path}: pattern")); }
    }
    let obj = inst.as_object();
    if let (Some(req), Some(obj)) = (node.get("required").and_then(|r| r.as_array()), obj) {
        for k in req {
            let ks = k.as_str().unwrap_or("");
            if !obj.contains_key(ks) { out.push(format!("{path}/{ks}: required")); }
        }
    }
    if let Some(obj) = obj {
        let props = node.get("properties").and_then(|p| p.as_object());
        match node.get("additionalProperties") {
            Some(Value::Bool(false)) => {
                for k in obj.keys() {
                    if !props.map(|p| p.contains_key(k)).unwrap_or(false) {
                        out.push(format!("{path}/{k}: additional property"));
                    }
                }
            }
            Some(sub) if sub.is_object() => {
                for (k, v) in obj {
                    if props.map(|p| p.contains_key(k)).unwrap_or(false) { continue; }
                    schema_check(root, sub, v, &format!("{path}/{k}"), out);
                }
            }
            _ => {}
        }
        if let Some(props) = props {
            for (k, sub) in props {
                if let Some(v) = obj.get(k) {
                    schema_check(root, sub, v, &format!("{path}/{k}"), out);
                }
            }
        }
    }
    if let Some(items) = node.get("items") {
        if let Some(arr) = inst.as_array() {
            for (i, v) in arr.iter().enumerate() {
                schema_check(root, items, v, &format!("{path}/{i}"), out);
            }
        }
    }
}

fn resolve<'a>(root: &'a Value, node: &'a Value) -> &'a Value {
    let Some(r) = node.get("$ref").and_then(|s| s.as_str()) else { return node };
    let mut cur = root;
    for part in r.trim_start_matches("#/").split('/') {
        match cur.get(part) {
            Some(n) => cur = n,
            None => return node,
        }
    }
    if cur.get("$ref").is_some() && !std::ptr::eq(cur, node) { resolve(root, cur) } else { cur }
}

fn type_ok(schema_type: &Value, inst: &Value) -> bool {
    let mut names = vec![];
    if let Some(s) = schema_type.as_str() { names.push(s); }
    if let Some(a) = schema_type.as_array() { names.extend(a.iter().filter_map(|x| x.as_str())); }
    names.iter().any(|t| match *t {
        "object" => inst.is_object(),
        "array" => inst.is_array(),
        "string" => inst.is_string(),
        "boolean" => inst.is_boolean(),
        "null" => inst.is_null(),
        "integer" => inst.as_i64().is_some(),
        _ => false,
    })
}

fn hex_pattern(pat: &str, s: &str) -> bool {
    let Some(n) = pat.strip_prefix("^[0-9a-f]{").and_then(|r| r.strip_suffix("}$")) else { return true };
    let Ok(n) = n.parse::<usize>() else { return true };
    s.len() == n && s.bytes().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
}

fn write_jsonl(path: &Path, rows: &[Value]) {
    let mut s = String::new();
    for r in rows { s.push_str(&r.to_string()); s.push('\n'); }
    std::fs::write(path, s).unwrap();
}

fn render_markdown(m: &Matrix) -> String {
    let mut s = String::new();
    s.push_str("# Red team\n\n");
    s.push_str("Synthetic bundles built from the in-tree fixtures (`humanlike::synth`, the hash chain, Ed25519 over JCS, `contracts/receipt.v0.json`). ");
    s.push_str("Each row is scored by the checks that already ship. No threshold was widened and no check was skipped to change a verdict.\n\n");
    s.push_str("Run `qorcap redteam` to print the JSON matrix. `qorcap redteam --out tests/redteam/matrix.v0.json --markdown docs/REDTEAM.md` rewrites these two files. ");
    s.push_str("`cargo test --test redteam` checks that a fresh run matches both files.\n\n");
    s.push_str(&format!("Rule v2 ceiling is {} ms. Successful latencies must sit within ±{} ms of their median (a gap of exactly {} ms passes). ",
        RULE2_MAX_LATENCY_MS, RULE2_CONSISTENCY_MS, RULE2_CONSISTENCY_MS));
    s.push_str("`verify-bundle` re-hashes bytes. It does not score human-likeness, alignment, the controller-identity claim, the receipt schema, or rule v2.\n\n");
    s.push_str("## Matrix\n\n");
    s.push_str("| id | attack | verdict | caught by | verify-bundle |\n");
    s.push_str("|---|---|---|---|---|\n");
    for r in &m.rows {
        let caught = if r.caught_by.is_empty() { "—".into() } else { r.caught_by.join(", ") };
        let vb = if r.blocks_verify_bundle { "FAIL" } else { "OK" };
        s.push_str(&format!("| `{}` | {} | {} | {} | {} |\n", r.id, r.attack, r.verdict, caught, vb));
    }
    s.push_str("\n`weak` means a layer reports the attack and still accepts the bundle. `missed` means every layer accepts it. `caught` means a layer rejects it. The human-likeness catch is advisory: the label is bot-like and `verify-bundle` stays OK. The controller-identity catch fails the input-proof `controller_corroboration` claim; `verify-bundle` stays OK.\n\n");
    s.push_str("## Missed and weak rows\n\n");
    for r in m.rows.iter().filter(|r| r.verdict == "missed" || r.verdict == "weak") {
        s.push_str(&format!("### `{}`\n\n", r.id));
        s.push_str(&format!("{} — {}.\n\n", r.attack, r.variant));
        if let Some(c) = &r.closure {
            let who = if c.hardware_required {
                "Closing this takes hardware attestation or capture-before-PC."
            } else if c.software_can_close {
                "Software can close this. The check is not added in this suite."
            } else {
                "Software cannot close this."
            };
            s.push_str(&format!("{}\n\n{}\n\n", c.note, who));
        }
        for l in r.layers.iter().filter(|l| l.result != "pass") {
            s.push_str(&format!("- **{}** ({}): {}\n", l.name, l.result, l.detail));
        }
        s.push('\n');
    }
    s.push_str("## Caught rows, in short\n\n");
    for r in m.rows.iter().filter(|r| r.verdict == "caught") {
        let how = r.layers.iter().filter(|l| l.result == "fail").map(|l| format!("{}: {}", l.name, l.detail)).collect::<Vec<_>>().join(" ");
        s.push_str(&format!("- `{}`: {}\n", r.id, how));
    }
    s.push_str("\n## Control\n\n");
    if let Some(c) = m.rows.iter().find(|r| r.id == "honest_control") {
        s.push_str(&format!("`honest_control` is `{}`. A clean bundle with a 1750 ms pause latency, human-like presses, and an intact chain is accepted by every layer. That is the check that this suite did not invert a rule.\n", c.verdict));
    }
    s.push_str("\n## Bugs\n\n");
    s.push_str("No fail-closed check accepted a sealed one-byte splice or an extra top-level receipt field. The missed and weak rows are checks the current code does not make. No verification rule was edited.\n");
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rule_v2_numbers_match_qortime() {
        assert_eq!((RULE2_MAX_LATENCY_MS, RULE2_CONSISTENCY_MS), (2_500, 300));
        let pattern = ["OPTIONS", "CROSS", "SQUARE", "DOWN", "SQUARE"];
        let presses = vec![(1_500, "OPTIONS"), (1_600, "CROSS"), (1_700, "SQUARE"), (1_800, "DOWN"), (1_900, "SQUARE")];
        let ok = verify_response(&pattern, &presses, Some(1_500 + 1_750), RULE2_MAX_LATENCY_MS);
        assert!(ok.ok && ok.latency_ms == Some(1_750));
        let late = verify_response(&pattern, &presses, Some(1_500 + 2_600), RULE2_MAX_LATENCY_MS);
        assert!(!late.ok && late.reason == "video_latency_too_high");
        let early = verify_response(&pattern, &presses, Some(1_400), RULE2_MAX_LATENCY_MS);
        assert!(!early.ok && early.reason == "video_before_input");
        let none = verify_response(&pattern, &[], None, RULE2_MAX_LATENCY_MS);
        assert!(!none.ok && none.reason == "no_pause_press");
        assert!(deviations_over(&[1_000, 1_600]).is_empty()); // median 1300, gap 300
        assert_eq!(deviations_over(&[1_000, 1_601]), vec![0, 1]); // median 1300.5, gap 300.5
        assert!(deviations_over(&[500, 500, 500]).is_empty());
    }

    #[test]
    fn receipt_schema_still_forbids_unknown_keys() {
        let s: Value = serde_json::from_str(RECEIPT_SCHEMA).unwrap();
        assert_eq!(s["additionalProperties"], false);
        assert!(s["properties"].get("device_attest").is_none());
    }

    #[test]
    fn matrix_verdicts_and_closures() {
        let m = build_matrix();
        assert!(!m.rules_changed);
        let expect = [
            ("honest_control", "pass", None),
            ("replay_stale_no_fresh_nonce", "caught", Some("challenge rule v2")),
            ("relay_0_5s", "weak", None),
            ("relay_1_0s", "weak", None),
            ("relay_1_5s", "weak", None),
            ("relay_2_0s", "weak", None),
            ("relay_2_4s", "weak", None),
            ("relay_3_0s", "caught", Some("challenge rule v2")),
            ("virtual_capture_device", "missed", None),
            ("splice_sealed_segment", "caught", Some("hash chain")),
            ("splice_rehashed_resigned", "weak", None),
            ("turbo_macro", "caught", Some("humanlike check")),
            ("second_pad", "caught", Some("input proof")),
            ("receipt_tamper_score", "caught", Some("signature")),
            ("receipt_extra_field", "caught", Some("schema")),
        ];
        assert_eq!(m.rows.len(), expect.len());
        for (row, (id, verdict, caught)) in m.rows.iter().zip(expect) {
            assert_eq!(row.id, id, "{}", row.id);
            assert_eq!(row.verdict, verdict, "{} layers {:#?}", row.id, row.layers);
            if let Some(layer) = caught {
                assert_eq!(row.caught_by, vec![layer.to_string()], "{}", row.id);
            } else if verdict != "pass" {
                assert!(row.caught_by.is_empty(), "{} caught by {:?}", row.id, row.caught_by);
            }
        }
        let control = &m.rows[0];
        assert!(control.layers.iter().all(|l| l.result == "pass"), "{:#?}", control.layers);
        let replay = row(&m, "replay_stale_no_fresh_nonce");
        assert!(!replay.blocks_verify_bundle);
        assert!(replay.layers.iter().any(|l| l.name == "hash chain" && l.result == "pass"));
        assert!(replay.layers.iter().any(|l| l.name == "signature" && l.result == "pass"));
        let r3 = row(&m, "relay_3_0s");
        assert!(r3.layers.iter().any(|l| l.name == "challenge rule v2" && l.result == "fail" && l.detail.contains("video_latency_too_high")));
        for id in ["relay_0_5s", "relay_1_0s", "relay_1_5s", "relay_2_0s", "relay_2_4s"] {
            let r = row(&m, id);
            assert!(r.weak_layers.iter().any(|n| n == "input-video alignment"), "{id} {:?}", r.weak_layers);
            assert!(r.layers.iter().any(|l| l.name == "challenge rule v2" && l.result == "pass"), "{id}");
            assert!(r.layers.iter().any(|l| l.name == "input proof" && l.result == "pass"), "{id}");
            let c = r.closure.as_ref().unwrap();
            assert!(c.software_can_close && !c.hardware_required, "{id}");
            assert!(c.note.contains("does not add that bound"), "{id}");
        }
        let v = row(&m, "virtual_capture_device");
        assert!(!v.blocks_verify_bundle);
        let c = v.closure.as_ref().unwrap();
        assert!(!c.software_can_close && c.hardware_required);
        let splice = row(&m, "splice_sealed_segment");
        assert!(splice.blocks_verify_bundle);
        assert!(splice.layers.iter().any(|l| l.name == "hash chain" && l.detail.contains("SHA-256")));
        let reseal = row(&m, "splice_rehashed_resigned");
        assert!(!reseal.blocks_verify_bundle);
        assert!(reseal.weak_layers.iter().any(|n| n == "referee-only"));
        let c = reseal.closure.as_ref().unwrap();
        assert!(!c.software_can_close && c.hardware_required);
        let mac = row(&m, "turbo_macro");
        assert!(!mac.blocks_verify_bundle && !mac.fail_closed);
        let pad = row(&m, "second_pad");
        assert!(!pad.blocks_verify_bundle);
        assert!(pad.closure.is_none());
        assert_eq!(pad.caught_by, vec!["input proof".to_string()]);
        let proof = pad.layers.iter().find(|l| l.name == "input proof").unwrap();
        assert_eq!(proof.result, "fail");
        assert!(proof.detail.contains("serial:AAAA1111") && proof.detail.contains("serial:BBBB2222"), "{}", proof.detail);
        let logged = pad.layers.iter().find(|l| l.name == "input-video alignment").unwrap();
        assert_eq!(logged.result, "pass");
        assert!(logged.detail.contains("serial=AAAA1111") && logged.detail.contains("serial=BBBB2222"), "{}", logged.detail);
        let tamper = row(&m, "receipt_tamper_score");
        assert!(tamper.layers.iter().any(|l| l.name == "schema" && l.result == "pass"));
        let extra = row(&m, "receipt_extra_field");
        assert!(extra.layers.iter().any(|l| l.name == "schema" && l.detail.contains("device_attest")));
        assert!(extra.layers.iter().any(|l| l.name == "signature" && l.result == "pass"));
        let md = render_markdown(&m);
        assert!(md.contains("relay_2_4s") && md.contains("capture-before-PC"));
    }

    fn row<'a>(m: &'a Matrix, id: &str) -> &'a Row { m.rows.iter().find(|r| r.id == id).unwrap() }
}
