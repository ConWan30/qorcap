//! T-0031 qorcap fuzz suite: deterministic seeded property tests for the
//! hand timeline writer and checker.
//!
//! Every case is seeded (SplitMix64, fixed seeds below) and the seed is
//! printed in every failure message, so a failing run reproduces exactly.
//! No new crates, no network, nothing outside std plus the crate itself.
//!
//! The assertions are written against the real code at main 7114c32, not
//! against the task file's paraphrase:
//! - There is no `HandCheckStatus` type: `check_dir` returns
//!   `Result<Report, String>` and fail-closed means `!report.pass()`
//!   (`errors` non-empty) or `Err`.
//! - Whole-file trailing bytes past the sealed raw offset are a warning, not
//!   an error; only the sealed slice length is checked.
//! - `DeclaredEdgeSettings` has no allowed-value list: arbitrary strings pass
//!   through trimmed, `checked` is always false.
//! - `disk_fault` has no trigger from a test alone (seal is private and its
//!   errors are swallowed by advance); `disk_low` is covered instead and the
//!   gap is recorded in VERIFICATION.md.
//! - Mutated chunk `seq` values stay at or below MAX_SAFE_SEQ: above it the
//!   checker itself overflows an integer multiply in debug builds
//!   (check.rs, `seq * CHUNK_US`). That is a finding about the checker, not
//!   a case this suite can expect to pass.

use qorcap::evidence::{self as ev, GENESIS};
use qorcap::frameclock::FrameStamp;
use qorcap::hand::check::check_dir;
use qorcap::hand::device::{DeviceInfo, DeviceModel};
use qorcap::hand::timeline::{
    Config, HandTimeline, Qpc, CHUNK_US, FILE_NAME, MAX_OPEN_WINDOWS, RAW_FILE,
    RAW_RECORD_BYTES, SUMMARY_FILE,
};
use qorcap::hand::DeclaredEdgeSettings;
use qorcap::hid::{self, Pad, CROSS};
use serde_json::Value;
use std::path::PathBuf;

/// Fixed seed for every fuzz loop below. Each test derives its own stream
/// from it so the suites stay independent and reproducible.
const SEED: u64 = 0xC0FF_EEF0_0D00_31;

/// Largest chunk `seq` the checker can handle without overflowing.
/// `check_dir` computes `(seq * CHUNK_US, (seq + 1) * CHUNK_US)` with plain
/// `u64` multiplication, so `seq >= 18_446_744_073_709` panics on integer
/// overflow in debug builds (and `cargo test` runs debug with overflow
/// checks on). `18_446_744_073_708` is the largest safe value:
/// `(18_446_744_073_708 + 1) * 1_000_000 = 18_446_744_073_709_000_000`,
/// still below `u64::MAX` (`18_446_744_073_709_551_615`).
const MAX_SAFE_SEQ: u64 = 18_446_744_073_708;

/// Randomized case counts. The count test at the bottom asserts the total.
const AREA1_CASES: u32 = 90;
const AREA2_CASES: u32 = 70;
const AREA3_LIVE_CASES: u32 = 30;
const AREA3_FOLD_CASES: u32 = 10;
const AREA3_DISK_LOW_CASES: u32 = 10;
const AREA3_CASES: u32 = AREA3_LIVE_CASES + AREA3_FOLD_CASES + AREA3_DISK_LOW_CASES;
const AREA4_CASES: u32 = 40;
const QPC_CASES: u32 = 20;
const TOTAL_FUZZ_CASES: u32 = AREA1_CASES + AREA2_CASES + AREA3_CASES + AREA4_CASES + QPC_CASES;

/// SplitMix64: a small deterministic generator. Zero new crates, fixed seed,
/// same stream on every platform.
struct SplitMix64(u64);

impl SplitMix64 {
    fn new(seed: u64) -> Self {
        Self(seed)
    }
    fn next_u64(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }
    fn below(&mut self, n: u64) -> u64 {
        debug_assert!(n > 0, "below() needs a non-zero bound");
        self.next_u64() % n
    }
    fn byte(&mut self) -> u8 {
        self.next_u64() as u8
    }
}

fn tmp(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!(
        "qorcap-hand-{name}-{}-{:?}",
        std::process::id(),
        std::thread::current().id()
    ));
    let _ = std::fs::remove_dir_all(&d);
    d
}
fn edge() -> DeviceInfo {
    DeviceInfo {
        model: DeviceModel::DualSenseEdge,
        vid: 0x054C,
        pid: 0x0DF2,
        bcd_device: 0x0408,
        pad_id: "serial:EDGE1".into(),
    }
}
fn cfg(dir: &PathBuf, raw: bool, dev: Option<DeviceInfo>) -> Config {
    Config {
        dir: dir.clone(),
        hand_dir_source: "direct".into(),
        hand_dir_fallback_reason: None,
        raw_usb: raw,
        raw_min_free_bytes: None,
        wall_anchor_ms: 1_795_144_560_000,
        qpc: None,
        frame_clock_on: true,
        initial_device: dev,
        start_t_us: 0,
        declared_settings: Default::default(),
    }
}
fn report(counter: u8, buttons: u32, lx: u8, ts: u32) -> Vec<u8> {
    let mut p = Pad::neutral();
    p.counter = counter;
    p.buttons = buttons;
    p.lx = lx;
    p.sensor_ts = ts;
    hid::pack_usb(&p)
}
fn lines(dir: &PathBuf) -> Vec<Value> {
    std::fs::read_to_string(dir.join(FILE_NAME))
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect()
}

/// Re-seal a timeline after an edit: applies `edit` to every row, then
/// re-computes each chunk's records hash, `prev` and `head` so the chain
/// stays valid and only the edited property is under test. Copied from the
/// existing `tests/hand_timeline.rs` helper.
fn rehash_timeline_for_test(dir: &PathBuf, mut edit: impl FnMut(&mut Value)) {
    let path = dir.join(FILE_NAME);
    let rows: Vec<Value> = std::fs::read_to_string(&path)
        .unwrap()
        .lines()
        .filter(|l| !l.is_empty())
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    let mut rebuilt = String::new();
    let mut pending: Vec<Value> = Vec::new();
    let mut head = GENESIS.to_string();
    for mut row in rows {
        edit(&mut row);
        if row["type"] != "chunk" {
            pending.push(row);
            continue;
        }
        let seq = row["seq"].as_u64().unwrap();
        let recs = std::mem::take(&mut pending);
        let sha = ev::try_jcs_sha(&Value::Array(recs.clone())).unwrap();
        let new_head = ev::link(&head, seq, &sha);
        row["sha256"] = serde_json::json!(sha);
        row["prev"] = serde_json::json!(head);
        row["head"] = serde_json::json!(new_head);
        head = new_head;
        for r in recs.iter().chain(std::iter::once(&row)) {
            rebuilt.push_str(&ev::jcs(r).unwrap());
            rebuilt.push('\n');
        }
    }
    std::fs::write(&path, rebuilt).unwrap();
}

/// Rewrite every timeline row with `edit` applied, keeping JCS form, but
/// WITHOUT re-sealing: the chain is left exactly as the edit leaves it.
/// Use this when the point of the case is that the chain no longer links
/// (flipped `prev` / `head` / `sha256`, reordered `seq`).
fn rewrite_jsonl(dir: &PathBuf, mut edit: impl FnMut(&mut Value)) {
    let path = dir.join(FILE_NAME);
    let rows: Vec<Value> = std::fs::read_to_string(&path)
        .unwrap()
        .lines()
        .filter(|l| !l.is_empty())
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    let mut out = String::new();
    for mut row in rows {
        edit(&mut row);
        out.push_str(&ev::jcs(&row).unwrap());
        out.push('\n');
    }
    std::fs::write(&path, out).unwrap();
}

/// Drop every row for which `keep` returns false, keeping JCS form.
fn remove_rows(dir: &PathBuf, mut keep: impl FnMut(&Value) -> bool) {
    let path = dir.join(FILE_NAME);
    let rows: Vec<Value> = std::fs::read_to_string(&path)
        .unwrap()
        .lines()
        .filter(|l| !l.is_empty())
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    let mut out = String::new();
    for row in rows {
        if keep(&row) {
            out.push_str(&ev::jcs(&row).unwrap());
            out.push('\n');
        }
    }
    std::fs::write(&path, out).unwrap();
}

struct RawBind {
    seq: u64,
    offset: u64,
    len: u64,
    n_reports: u64,
}

fn raw_binds(dir: &PathBuf) -> Vec<RawBind> {
    lines(dir)
        .iter()
        .filter(|r| r["type"] == "raw_bind")
        .map(|r| RawBind {
            seq: r["chunk_seq"].as_u64().unwrap(),
            offset: r["offset"].as_u64().unwrap(),
            len: r["len"].as_u64().unwrap(),
            n_reports: r["n_reports"].as_u64().unwrap(),
        })
        .collect()
}

/// Build a small valid sealed session (raw on, Edge device) and return its
/// dir. The session must pass `check_dir` before any mutation runs, so a
/// later failure is always the mutation's fault, never the fixture's.
fn make_session(tag: &str, rng: &mut SplitMix64) -> PathBuf {
    let dir = tmp(tag);
    let mut tl = HandTimeline::open(cfg(&dir, true, Some(edge()))).unwrap();
    let n_reports = 500 + rng.below(150);
    let press_every = 100 + rng.below(75);
    let mut c: u8 = 0;
    for i in 0..n_reports {
        let b = if i % press_every == 0 { CROSS } else { 0 };
        let lx = if i % 250 == 125 { 200 } else { 128 };
        tl.on_report(
            1_000 + i * 4_000,
            Some(10_000 + i * 40_000),
            &report(c, b, lx, i as u32),
        );
        c = c.wrapping_add(1);
    }
    let end_t = 1_000 + n_reports * 4_000 + 100_000;
    tl.finish(end_t).unwrap();
    let rep = check_dir(&dir).unwrap();
    assert!(
        rep.pass(),
        "fresh session must pass before mutation: {:?}",
        rep.errors
    );
    assert!(rep.chunks >= 2, "fresh session must seal at least 2 chunks");
    dir
}

/// `check_dir` must return `Ok` with errors, and one of them must contain
/// `needle`. An `Err` here would mean the timeline file itself is unreadable,
/// which none of these mutations do.
fn must_fail(dir: &PathBuf, needle: &str, ctx: &str) {
    match check_dir(dir) {
        Ok(rep) => {
            assert!(!rep.pass(), "{ctx}: expected check_dir to fail, got PASS");
            assert!(
                rep.errors.iter().any(|e| e.contains(needle)),
                "{ctx}: expected an error containing {needle:?}, got {:?}",
                rep.errors
            );
        }
        Err(e) => panic!("{ctx}: check_dir returned Err, expected Ok(report) with errors: {e}"),
    }
}

/// `check_dir` must pass with no errors and a warning containing `needle`.
fn must_pass_with_warning(dir: &PathBuf, needle: &str, ctx: &str) {
    let rep = check_dir(dir).unwrap_or_else(|e| panic!("{ctx}: check_dir returned Err: {e}"));
    assert!(
        rep.pass(),
        "{ctx}: expected PASS, got errors {:?}",
        rep.errors
    );
    assert!(
        rep.warnings.iter().any(|w| w.contains(needle)),
        "{ctx}: expected a warning containing {needle:?}, got {:?}",
        rep.warnings
    );
}

/// The `clean()` rule for one declared setting, restated for the test:
/// trim, drop when empty or `UNKNOWN` in any casing.
fn expected_clean(s: &str) -> Option<String> {
    let t = s.trim();
    if t.is_empty() || t.eq_ignore_ascii_case("UNKNOWN") {
        None
    } else {
        Some(t.to_string())
    }
}

/// Random declared-setting strings: plain words, `UNKNOWN` in various
/// casings and paddings, whitespace-only strings, and arbitrary unicode.
fn random_setting_string(rng: &mut SplitMix64) -> String {
    const POOL: &[char] = &[
        'a', 'b', 'z', 'A', 'Z', '0', '9', ' ', '_', '-', '+', '/', '(', ')', '\t', '\n',
        '\r', '\u{00A0}', '\u{2003}', '\u{200B}', 'é', 'ß', 'λ', 'Д', '中', '🎮',
    ];
    const WORDS: &[&str] = &[
        "UNKNOWN",
        "unknown",
        "UnKnOwN",
        " UNKNOWN ",
        "short",
        "full",
        "precise",
        "on",
        "FN+TRIANGLE",
        "",
    ];
    if rng.below(4) == 0 {
        return WORDS[(rng.below(WORDS.len() as u64)) as usize].to_string();
    }
    let n = rng.below(25) as usize;
    (0..n)
        .map(|_| POOL[(rng.below(POOL.len() as u64)) as usize])
        .collect()
}

/// The `Qpc::ticks_to_t_us` rule, restated for the test: `None` exactly when
/// the frequency is zero or the result does not fit in `u64`; otherwise the
/// exact value, with the backwards case clamping at zero.
fn expected_ticks_to_t_us(q: &Qpc, ticks: u64) -> Option<u64> {
    if q.freq_hz == 0 {
        return None;
    }
    if ticks >= q.origin_ticks {
        let delta =
            (u128::from(ticks - q.origin_ticks) * 1_000_000) / u128::from(q.freq_hz);
        u64::try_from(delta)
            .ok()
            .and_then(|d| q.origin_t_us.checked_add(d))
    } else {
        let delta =
            (u128::from(q.origin_ticks - ticks) * 1_000_000) / u128::from(q.freq_hz);
        u64::try_from(delta)
            .ok()
            .map(|d| q.origin_t_us.saturating_sub(d))
    }
}

#[test]
fn raw_slice_fuzz_fails_closed() {
    let mut rng = SplitMix64::new(SEED);
    for i in 0..AREA1_CASES {
        let tag = format!("a1-{i}");
        let dir = make_session(&tag, &mut rng);
        let kind = rng.below(10);
        let ctx = format!("seed={SEED:#x} iter={i} kind={kind}");
        match kind {
            // 0: sealed slice length not a multiple of 80.
            0 => {
                let binds = raw_binds(&dir);
                let b = binds.iter().filter(|b| b.n_reports > 0).last().unwrap();
                let new_len = b.len + 40;
                rehash_timeline_for_test(&dir, |row| {
                    if row["type"] == "raw_bind" && row["chunk_seq"] == b.seq {
                        row["len"] = serde_json::json!(new_len);
                    } else if row["type"] == "chunk" && row["seq"] == b.seq {
                        row["raw_len"] = serde_json::json!(new_len);
                    }
                });
                must_fail(&dir, "does not match n_reports x 80", &ctx);
            }
            // 1: sealed slice length a multiple of 80 but not n_reports x 80.
            1 => {
                let binds = raw_binds(&dir);
                let b = binds.iter().filter(|b| b.n_reports > 0).last().unwrap();
                let new_len = b.len + 80;
                rehash_timeline_for_test(&dir, |row| {
                    if row["type"] == "raw_bind" && row["chunk_seq"] == b.seq {
                        row["len"] = serde_json::json!(new_len);
                    } else if row["type"] == "chunk" && row["seq"] == b.seq {
                        row["raw_len"] = serde_json::json!(new_len);
                    }
                });
                must_fail(&dir, "does not match n_reports x 80", &ctx);
            }
            // 2: slice offset leaves a gap after the previous slice.
            2 => {
                let binds = raw_binds(&dir);
                let b = binds.iter().find(|b| b.seq >= 1).unwrap();
                let new_off = b.offset + RAW_RECORD_BYTES as u64;
                rehash_timeline_for_test(&dir, |row| {
                    if row["type"] == "raw_bind" && row["chunk_seq"] == b.seq {
                        row["offset"] = serde_json::json!(new_off);
                    } else if row["type"] == "chunk" && row["seq"] == b.seq {
                        row["raw_offset"] = serde_json::json!(new_off);
                    }
                });
                must_fail(&dir, "leaves a gap", &ctx);
            }
            // 3: slice offset overlaps the previous slice.
            3 => {
                let binds = raw_binds(&dir);
                let b = binds.iter().find(|b| b.seq >= 1).unwrap();
                let new_off = b.offset - RAW_RECORD_BYTES as u64;
                rehash_timeline_for_test(&dir, |row| {
                    if row["type"] == "raw_bind" && row["chunk_seq"] == b.seq {
                        row["offset"] = serde_json::json!(new_off);
                    } else if row["type"] == "chunk" && row["seq"] == b.seq {
                        row["raw_offset"] = serde_json::json!(new_off);
                    }
                });
                must_fail(&dir, "overlaps", &ctx);
            }
            // 4: a raw record's t_us runs backwards inside its slice. The
            // slice hash is recomputed so only the t_us rule is under test.
            4 => {
                let binds = raw_binds(&dir);
                let cands: Vec<&RawBind> =
                    binds.iter().filter(|b| b.n_reports >= 2).collect();
                let b = cands[(rng.below(cands.len() as u64)) as usize];
                let j = 1 + rng.below(b.n_reports - 1);
                let mut raw = std::fs::read(dir.join(RAW_FILE)).unwrap();
                let rec_off = (b.offset + j * RAW_RECORD_BYTES as u64) as usize;
                let prev_t = u64::from_le_bytes(
                    raw[rec_off - RAW_RECORD_BYTES + 8..rec_off - RAW_RECORD_BYTES + 16]
                        .try_into()
                        .unwrap(),
                );
                raw[rec_off + 8..rec_off + 16]
                    .copy_from_slice(&(prev_t - 1).to_le_bytes());
                let new_sha = ev::sha256_hex(
                    &raw[b.offset as usize..(b.offset + b.len) as usize],
                );
                std::fs::write(dir.join(RAW_FILE), &raw).unwrap();
                rehash_timeline_for_test(&dir, |row| {
                    if row["type"] == "raw_bind" && row["chunk_seq"] == b.seq {
                        row["sha256"] = serde_json::json!(new_sha);
                    } else if row["type"] == "chunk" && row["seq"] == b.seq {
                        row["raw_sha256"] = serde_json::json!(new_sha);
                    }
                });
                must_fail(&dir, "decreases below previous", &ctx);
            }
            // 5: an `in` row's t_us runs backwards (two consecutive `in`
            // rows in one chunk swap times). Re-sealed so only the
            // backwards rule is under test.
            5 => {
                let rows = lines(&dir);
                let mut pairs: Vec<usize> = vec![];
                let mut ordinal = 0usize;
                for k in 0..rows.len().saturating_sub(1) {
                    if rows[k]["type"] == "in" {
                        if rows[k + 1]["type"] == "in"
                            && rows[k]["chunk_seq"] == rows[k + 1]["chunk_seq"]
                        {
                            pairs.push(ordinal);
                        }
                        ordinal += 1;
                    }
                }
                assert!(
                    !pairs.is_empty(),
                    "{ctx}: session must contain consecutive in rows in one chunk"
                );
                let want = pairs[(rng.below(pairs.len() as u64)) as usize];
                let (t0, t1) = {
                    let ins: Vec<&Value> =
                        rows.iter().filter(|r| r["type"] == "in").collect();
                    (
                        ins[want]["t_us"].as_u64().unwrap(),
                        ins[want + 1]["t_us"].as_u64().unwrap(),
                    )
                };
                assert!(t0 < t1, "{ctx}: swapped in rows must start in order");
                let mut seen = 0usize;
                rehash_timeline_for_test(&dir, |row| {
                    if row["type"] == "in" {
                        if seen == want {
                            row["t_us"] = serde_json::json!(t1);
                        } else if seen == want + 1 {
                            row["t_us"] = serde_json::json!(t0);
                        }
                        seen += 1;
                    }
                });
                must_fail(&dir, "in t_us goes backwards", &ctx);
            }
            // 6: one bit flips in the 64-byte report body of a raw record
            // that an `in` row points at. The slice hash is recomputed, so
            // only the per-row hash membership rule is under test.
            6 => {
                let rows = lines(&dir);
                let ins: Vec<&Value> =
                    rows.iter().filter(|r| r["type"] == "in").collect();
                assert!(!ins.is_empty(), "{ctx}: session must contain in rows");
                let target = ins[(rng.below(ins.len() as u64)) as usize];
                let t = target["t_us"].as_u64().unwrap();
                let mut raw = std::fs::read(dir.join(RAW_FILE)).unwrap();
                let mut rec_off = None;
                for off in (0..raw.len()).step_by(RAW_RECORD_BYTES) {
                    if u64::from_le_bytes(
                        raw[off + 8..off + 16].try_into().unwrap(),
                    ) == t
                    {
                        rec_off = Some(off);
                        break;
                    }
                }
                let rec_off = rec_off.expect("in row must have a raw record");
                raw[rec_off + 16 + (rng.below(64) as usize)] ^= 1;
                let binds = raw_binds(&dir);
                let b = binds
                    .iter()
                    .find(|b| {
                        (rec_off as u64) >= b.offset
                            && (rec_off as u64) < b.offset + b.len
                    })
                    .unwrap();
                let new_sha = ev::sha256_hex(
                    &raw[b.offset as usize..(b.offset + b.len) as usize],
                );
                std::fs::write(dir.join(RAW_FILE), &raw).unwrap();
                rehash_timeline_for_test(&dir, |row| {
                    if row["type"] == "raw_bind" && row["chunk_seq"] == b.seq {
                        row["sha256"] = serde_json::json!(new_sha);
                    } else if row["type"] == "chunk" && row["seq"] == b.seq {
                        row["raw_sha256"] = serde_json::json!(new_sha);
                    }
                });
                let rep = check_dir(&dir).unwrap();
                assert!(!rep.pass(), "{ctx}: flipped body byte must fail");
                assert!(
                    rep.errors
                        .iter()
                        .any(|e| e.contains("an in row's raw_sha256 is not in the raw slice")),
                    "{ctx}: expected the per-row hash error, got {:?}",
                    rep.errors
                );
                assert!(
                    !rep.errors
                        .iter()
                        .any(|e| e.contains("raw slice SHA-256 does not match")),
                    "{ctx}: slice hash was recomputed, so it must still match: {:?}",
                    rep.errors
                );
            }
            // 7: one bit flips in a raw record's qpc_ticks bytes (0..8).
            // The per-row hash covers only bytes 16..80, so the failure
            // must be the whole-slice hash, not the per-row one.
            7 => {
                let mut raw = std::fs::read(dir.join(RAW_FILE)).unwrap();
                let rec = (rng.below((raw.len() / RAW_RECORD_BYTES) as u64)) as usize;
                let off = rec * RAW_RECORD_BYTES + (rng.below(8) as usize);
                raw[off] ^= 1;
                std::fs::write(dir.join(RAW_FILE), &raw).unwrap();
                let rep = check_dir(&dir).unwrap();
                assert!(!rep.pass(), "{ctx}: flipped qpc byte must fail");
                assert!(
                    rep.errors
                        .iter()
                        .any(|e| e.contains("raw slice SHA-256 does not match")),
                    "{ctx}: expected the slice hash error, got {:?}",
                    rep.errors
                );
                assert!(
                    !rep.errors.iter().any(|e| e
                        .contains("an in row's raw_sha256 is not in the raw slice")),
                    "{ctx}: per-row hashes cover only bytes 16..80: {:?}",
                    rep.errors
                );
            }
            // 8: the raw file is truncated short of a sealed slice.
            8 => {
                let mut raw = std::fs::read(dir.join(RAW_FILE)).unwrap();
                let cut = 1 + rng.below(100) as usize;
                raw.truncate(raw.len() - cut);
                std::fs::write(dir.join(RAW_FILE), &raw).unwrap();
                must_fail(&dir, "truncated (slice", &ctx);
            }
            // 9: trailing bytes past the sealed offset are a warning, not
            // an error: the sealed slices still hash and the file passes.
            _ => {
                let mut raw = std::fs::read(dir.join(RAW_FILE)).unwrap();
                let extra = 1 + rng.below(200) as usize;
                raw.resize(raw.len() + extra, 0xAA);
                std::fs::write(dir.join(RAW_FILE), &raw).unwrap();
                must_pass_with_warning(&dir, "trailing byte(s) after sealed offset", &ctx);
            }
        }
        let _ = std::fs::remove_dir_all(&dir);
    }
}

#[test]
fn chunk_chain_mutations_fail_closed() {
    let mut rng = SplitMix64::new(SEED ^ 0xC4A1);
    for i in 0..AREA2_CASES {
        let tag = format!("a2-{i}");
        let dir = make_session(&tag, &mut rng);
        let kind = rng.below(8);
        let ctx = format!("seed={SEED:#x} iter={i} kind={kind}");
        match kind {
            // 0: flip a sealed chunk's `prev` link.
            0 => {
                rewrite_jsonl(&dir, |row| {
                    if row["type"] == "chunk" && row["seq"] == 1 {
                        row["prev"] = serde_json::json!("0".repeat(64));
                    }
                });
                must_fail(&dir, "prev does not match the previous head", &ctx);
            }
            // 1: flip a sealed chunk's `head`.
            1 => {
                rewrite_jsonl(&dir, |row| {
                    if row["type"] == "chunk" && row["seq"] == 1 {
                        row["head"] = serde_json::json!("f".repeat(64));
                    }
                });
                must_fail(&dir, "head does not re-link", &ctx);
            }
            // 2: flip a sealed chunk's records hash.
            2 => {
                rewrite_jsonl(&dir, |row| {
                    if row["type"] == "chunk" && row["seq"] == 1 {
                        row["sha256"] = serde_json::json!("0".repeat(64));
                    }
                });
                must_fail(&dir, "records SHA-256 does not match", &ctx);
            }
            // 3: a chunk's `seq` jumps out of order.
            3 => {
                rewrite_jsonl(&dir, |row| {
                    if row["type"] == "chunk" && row["seq"] == 1 {
                        row["seq"] = serde_json::json!(5);
                    }
                });
                must_fail(&dir, "expected seq", &ctx);
            }
            // 4: a chunk's `seq` sits exactly on the largest value the
            // checker can multiply without overflowing in debug builds.
            // The case must fail (never panic) — the test completing at
            // all is the overflow finding's boundary check.
            4 => {
                rewrite_jsonl(&dir, |row| {
                    if row["type"] == "chunk" && row["seq"] == 1 {
                        row["seq"] = serde_json::json!(MAX_SAFE_SEQ);
                    }
                });
                must_fail(&dir, "expected seq", &ctx);
            }
            // 5: drop a whole sealed window (its records and its chunk).
            // The drop is detected through the summary binding, not the
            // chain: with the window gone the file hash and last head no
            // longer match hand_timeline_summary.v0.json. (Deleting the
            // summary instead would leave a valid 1-chunk timeline, so
            // the summary must stay for this case to mean anything.)
            5 => {
                remove_rows(&dir, |row| {
                    !(row["type"] == "chunk" && row["seq"] == 1)
                        && !(row["chunk_seq"] == 1 && row["type"] != "chunk")
                });
                let rep = check_dir(&dir).unwrap();
                assert!(!rep.pass(), "{ctx}: dropped chunk must fail");
                assert!(
                    rep.errors.iter().any(|e| e.contains("sha256 does not match")
                        || e.contains("head does not match the last chunk")),
                    "{ctx}: expected a summary-binding error, got {:?}",
                    rep.errors
                );
            }
            // 6: complete records after the last sealed chunk are an
            // unsealed tail: a warning, not a failure. The summary file
            // is removed first, as in a real crash before finish: with
            // the summary present its stored file hash would (correctly)
            // stop matching the longer file.
            6 => {
                std::fs::remove_file(dir.join(SUMMARY_FILE)).unwrap();
                let rows = lines(&dir);
                let last_in = rows
                    .iter()
                    .filter(|r| r["type"] == "in")
                    .last()
                    .unwrap()
                    .clone();
                let base_t = last_in["t_us"].as_u64().unwrap();
                let mut text = std::fs::read_to_string(dir.join(FILE_NAME)).unwrap();
                for k in 1..=2u64 {
                    let mut tail = last_in.clone();
                    tail["t_us"] = serde_json::json!(base_t + k * 1_000);
                    text.push_str(&ev::jcs(&tail).unwrap());
                    text.push('\n');
                }
                std::fs::write(dir.join(FILE_NAME), text).unwrap();
                must_pass_with_warning(&dir, "unsealed tail", &ctx);
            }
            // 7: a file ending mid-line is an error, not a warning.
            _ => {
                let path = dir.join(FILE_NAME);
                let mut bytes = std::fs::read(&path).unwrap();
                bytes.truncate(bytes.len() - 5);
                std::fs::write(&path, bytes).unwrap();
                must_fail(&dir, "not JSON", &ctx);
            }
        }
        let _ = std::fs::remove_dir_all(&dir);
    }
}

#[test]
fn missing_timeline_file_is_an_error_not_a_panic() {
    let dir = tmp("a1-missing-file");
    let mut tl = HandTimeline::open(cfg(&dir, true, Some(edge()))).unwrap();
    tl.on_report(1_000, Some(10_000), &report(0, 0, 128, 1));
    tl.finish(1_500_000).unwrap();
    std::fs::remove_file(dir.join(FILE_NAME)).unwrap();
    assert!(
        check_dir(&dir).is_err(),
        "a missing timeline file must fail closed with Err"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn hand_timeline_stress_no_panic_and_windows_bounded() {
    let mut rng = SplitMix64::new(SEED ^ 0xA3);
    // Part A: live stress — 1 kHz reports with random input changes and
    // random clock rollbacks. A rollback is a status row, not a rejection:
    // with raw off the session still passes; with raw on the raw records'
    // t_us runs backwards and the checker must fail closed on that.
    for i in 0..AREA3_LIVE_CASES {
        let raw = rng.below(2) == 0;
        let dir = tmp(&format!("a3-live-{i}"));
        let mut tl = HandTimeline::open(cfg(&dir, raw, Some(edge()))).unwrap();
        let mut pad = Pad::neutral();
        let mut t: u64 = 1_000;
        let mut last_key_t: u64 = 0;
        let mut rolled_back = false;
        for step in 0..3000u32 {
            if t >= 2_500_000 {
                break;
            }
            pad.counter = step as u8;
            pad.sensor_ts = step * 1_000;
            // Change the input only once the clock has passed the last
            // key change, so a later rollback can never put a new `in`
            // row before an older one in the file.
            if t > last_key_t && rng.below(50) == 0 {
                pad.buttons = if pad.buttons == 0 { CROSS } else { 0 };
                last_key_t = t;
            }
            if t > last_key_t && rng.below(100) == 0 {
                pad.lx = rng.byte();
                last_key_t = t;
            }
            tl.on_report(t, Some(t * 10), &hid::pack_usb(&pad));
            assert!(
                tl.open_windows_len() <= MAX_OPEN_WINDOWS,
                "open windows exceeded cap at t={t} (iter {i})"
            );
            // One forced rollback per iteration plus random ones, all with
            // the same pad state so no `in` row is emitted for them.
            if step == 1500 || rng.below(400) == 0 {
                let back = 1_000 + rng.below(49_000);
                let rt = t.saturating_sub(back);
                tl.on_report(rt, Some(rt * 10), &hid::pack_usb(&pad));
                assert_eq!(
                    tl.live().state, "clock_rollback",
                    "rollback at t={t} back to {rt} must surface (iter {i})"
                );
                assert!(
                    tl.open_windows_len() <= MAX_OPEN_WINDOWS,
                    "open windows exceeded cap after rollback (iter {i})"
                );
                rolled_back = true;
                t = rt;
            }
            t += 1_000;
        }
        assert!(rolled_back, "each live iteration must roll the clock back");
        tl.finish(2_600_000).unwrap();
        let rep = check_dir(&dir).unwrap();
        if raw {
            assert!(
                !rep.pass(),
                "raw rollback must fail closed on the raw t_us check (iter {i})"
            );
            assert!(
                rep.errors
                    .iter()
                    .any(|e| e.contains("decreases below previous")),
                "raw rollback must trip the raw t_us rule (iter {i}): {:?}",
                rep.errors
            );
        } else {
            assert!(
                rep.pass(),
                "rollback with raw off is a status row only (iter {i}): {:?}",
                rep.errors
            );
        }
        let _ = std::fs::remove_dir_all(&dir);
    }
    // Part B: window cap — `on_frames` does not advance the seal clock, so
    // frame stamps across 9+ windows open windows without sealing any.
    // The 9th window folds into the highest open one and is marked late.
    for i in 0..AREA3_FOLD_CASES {
        let dir = tmp(&format!("a3-fold-{i}"));
        let mut tl = HandTimeline::open(cfg(&dir, false, Some(edge()))).unwrap();
        let n_windows = 9 + rng.below(3);
        for k in 0..n_windows {
            let ft = 100_000 + k * CHUNK_US;
            tl.on_frames(&[FrameStamp {
                file: "video.ts".into(),
                frame: k as i64,
                t_mono_us: ft,
                pts_90k: ft * 90 / 1_000_000,
            }]);
            assert!(
                tl.open_windows_len() <= MAX_OPEN_WINDOWS,
                "fold case {i}: open windows exceeded cap at window {k}"
            );
        }
        assert_eq!(tl.open_windows_len(), MAX_OPEN_WINDOWS);
        tl.finish(20_000_000).unwrap();
        let recs = lines(&dir);
        let hb7 = recs
            .iter()
            .find(|r| r["type"] == "hb" && r["chunk_seq"] == 7)
            .expect("chunk 7 hb");
        let folded = n_windows - 8;
        assert_eq!(
            hb7["late_records"].as_u64().unwrap(),
            folded,
            "folded frames must land late in the highest window (iter {i}): {hb7:?}"
        );
        let rep = check_dir(&dir).unwrap();
        assert!(
            !rep.pass(),
            "future-late frame records must fail closed (iter {i})"
        );
        assert!(
            rep.errors
                .iter()
                .any(|e| e.contains("is not from an earlier window")),
            "folded frames arrive after their window (iter {i}): {:?}",
            rep.errors
        );
        let _ = std::fs::remove_dir_all(&dir);
    }
    // Part C: low disk space pauses raw capture. A free-space floor of
    // u64::MAX is below every real disk, so the next seal must pause.
    for i in 0..AREA3_DISK_LOW_CASES {
        let dir = tmp(&format!("a3-disklow-{i}"));
        let mut c = cfg(&dir, true, Some(edge()));
        c.raw_min_free_bytes = Some(u64::MAX);
        let mut tl = HandTimeline::open(c).unwrap();
        for j in 0..750u64 {
            tl.on_report(
                1_000 + j * 4_000,
                Some(10_000 + j * 40_000),
                &report(j as u8, if j % 100 == 0 { CROSS } else { 0 }, 128, j as u32),
            );
        }
        tl.finish(3_500_000).unwrap();
        assert_eq!(
            tl.raw_pause(),
            Some("disk_low"),
            "free-space floor of u64::MAX must pause raw (iter {i})"
        );
        assert_eq!(
            tl.disk_faults(),
            0,
            "disk_low is a pause, not a fault (iter {i})"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }
}

#[test]
fn declared_edge_settings_fuzz_checked_false_and_unknown_defaults() {
    let mut rng = SplitMix64::new(SEED ^ 0xE99E);
    const FIXED: [&str; 6] = ["  hello  ", "UNKNOWN", "unknown", "  ", "", "Fn+Triangle"];
    const KEYS: [&str; 6] = [
        "edge_profile",
        "l2_stop",
        "r2_stop",
        "stick_curve_left",
        "stick_curve_right",
        "in_game_trigger_effect",
    ];
    for i in 0..AREA4_CASES {
        let mut raw: [String; 6] = Default::default();
        for (j, s) in raw.iter_mut().enumerate() {
            *s = if i < 6 {
                FIXED[(i as usize + j) % 6].to_string()
            } else {
                random_setting_string(&mut rng)
            };
        }
        let settings = DeclaredEdgeSettings::from_flags_and_env(
            Some(raw[0].as_str()),
            None,
            Some(raw[1].as_str()),
            None,
            Some(raw[2].as_str()),
            None,
            Some(raw[3].as_str()),
            None,
            Some(raw[4].as_str()),
            None,
            Some(raw[5].as_str()),
            None,
        );
        let expected: Vec<Option<String>> =
            raw.iter().map(|s| expected_clean(s)).collect();
        let json = settings.to_json();
        assert_eq!(json["checked"], false, "checked is always false (iter {i})");
        assert_eq!(
            json["source"],
            if expected.iter().any(|e| e.is_some()) {
                "user_declared"
            } else {
                "none"
            },
            "source must match whether anything survived (iter {i})"
        );
        assert_eq!(
            settings.any_set(),
            expected.iter().any(|e| e.is_some()),
            "any_set must match whether anything survived (iter {i})"
        );
        for (idx, e) in expected.iter().enumerate() {
            let want = e.as_deref().unwrap_or("UNKNOWN");
            assert_eq!(json[KEYS[idx]], want, "iter {i} field {}", KEYS[idx]);
        }
        // Every 8th iteration, round-trip the same settings through a real
        // session header.
        if i % 8 == 0 {
            let dir = tmp(&format!("a4-{i}"));
            let mut c = cfg(&dir, false, Some(edge()));
            c.declared_settings = settings.clone();
            let mut tl = HandTimeline::open(c).unwrap();
            tl.on_report(10_000, Some(100_000), &report(0, 0, 128, 1));
            tl.finish(1_500_000).unwrap();
            let recs = lines(&dir);
            assert_eq!(
                recs[0]["declared_settings"], json,
                "header must carry the same settings (iter {i})"
            );
            let _ = std::fs::remove_dir_all(&dir);
        }
    }
}

#[test]
fn qpc_ticks_to_t_us_fuzz_boundaries() {
    // Regression anchors from the existing single-case test.
    let q = Qpc {
        freq_hz: 10_000_000,
        origin_ticks: 50_000_000,
        origin_t_us: 1_000,
    };
    assert_eq!(q.ticks_to_t_us(50_000_000), Some(1_000));
    assert_eq!(q.ticks_to_t_us(50_010_000), Some(2_000));
    assert_eq!(q.ticks_to_t_us(300_000_000), Some(25_001_000));
    assert_eq!(q.ticks_to_t_us(49_995_000), Some(500));
    assert_eq!(
        Qpc {
            freq_hz: 0,
            origin_ticks: 100,
            origin_t_us: 0
        }
        .ticks_to_t_us(200),
        None
    );
    let mut rng = SplitMix64::new(SEED ^ 0x51C);
    for i in 0..QPC_CASES {
        let freq = match rng.below(8) {
            0 => 0,
            1 => 1,
            2 => 10,
            3 => 1_000,
            4 => 10_000_000,
            5 => u64::MAX,
            6 => rng.next_u64(),
            _ => 1 + rng.below(1_000_000),
        };
        let origin_ticks = rng.next_u64();
        let origin_t_us = match rng.below(4) {
            0 => 0,
            1 => u64::MAX,
            2 => rng.next_u64(),
            _ => 1_000,
        };
        let q = Qpc {
            freq_hz: freq,
            origin_ticks,
            origin_t_us,
        };
        let mut ticks = vec![
            0,
            1,
            u64::MAX,
            origin_ticks,
            origin_ticks.saturating_sub(1),
            origin_ticks.saturating_add(1),
        ];
        for _ in 0..8 {
            ticks.push(rng.next_u64());
        }
        for &t in &ticks {
            let got = q.ticks_to_t_us(t);
            let want = expected_ticks_to_t_us(&q, t);
            assert_eq!(got, want, "qpc mismatch iter={i} q={q:?} ticks={t}");
        }
        // The conversion never runs backwards as ticks grow.
        let mut sorted = ticks.clone();
        sorted.sort_unstable();
        let mut prev: Option<u64> = None;
        for &t in &sorted {
            if let Some(v) = q.ticks_to_t_us(t) {
                if let Some(p) = prev {
                    assert!(
                        v >= p,
                        "ticks_to_t_us must not decrease: {p} then {v} (iter {i})"
                    );
                }
                prev = Some(v);
            }
        }
    }
}

#[test]
fn fuzz_case_count_is_at_least_250() {
    assert_eq!(
        TOTAL_FUZZ_CASES,
        AREA1_CASES + AREA2_CASES + AREA3_CASES + AREA4_CASES + QPC_CASES
    );
    assert!(
        TOTAL_FUZZ_CASES >= 250,
        "T-0031 needs at least 250 randomized cases, got {TOTAL_FUZZ_CASES}"
    );
}
