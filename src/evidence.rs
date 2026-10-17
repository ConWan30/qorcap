//! QorTime-native evidence layer.
//!
//! * `jcs`: RFC 8785 canonical JSON (QorTime's restricted profile: no floats).
//! * `SegmentChain`: SHA-256 of every encoded fMP4 segment (init + each moof/mdat
//!   fragment, exactly the bytes written to disk), chained as
//!   `head_n = SHA-256(JCS({prev, seq, sha256}))` from 64x"0".
//! * `CheckpointChain`: QorTime-shaped checkpoints `{seq,t_ms,frames_hash,input_hash,prev,hash}`
//!   (hash = SHA-256(JCS(body)), same rule as `qor_receipt/checkpoints.py`) whose
//!   `frames_hash` commits to the QorCap evidence object (segment hash, chain head,
//!   score/clock state, nonce) carried alongside as `qorcap`.
//! * `pattern_for`: port of QorTime's nonce -> DualSense pattern.
//!
//! Honesty rail: this proves footage integrity and timing (what bytes, in what
//! order, seen when), and since 0.3.0 that the logged controller inputs were read
//! in sync with it. It does NOT prove who is holding the controller.
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

pub const GENESIS: &str = "0000000000000000000000000000000000000000000000000000000000000000";

pub fn sha256_hex(b: &[u8]) -> String { hex(&Sha256::digest(b)) }
pub fn hex(b: &[u8]) -> String { b.iter().map(|x| format!("{x:02x}")).collect() }

/// RFC 8785 JCS. Errors on floats / unsafe integers (QorTime profile).
pub fn jcs(v: &Value) -> Result<String, String> {
    let mut s = String::new();
    write_jcs(v, &mut s)?;
    Ok(s)
}
fn write_jcs(v: &Value, s: &mut String) -> Result<(), String> {
    match v {
        Value::Null => s.push_str("null"),
        Value::Bool(b) => s.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => {
            let i = n.as_i64().or_else(|| n.as_u64().and_then(|u| i64::try_from(u).ok())).ok_or("float or huge integer not allowed")?;
            if i.unsigned_abs() > (1u64 << 53) - 1 { return Err("integer exceeds 2^53-1".into()); }
            s.push_str(&i.to_string());
        }
        Value::String(t) => s.push_str(&serde_json::to_string(t).unwrap()),
        Value::Array(a) => { s.push('['); for (i, x) in a.iter().enumerate() { if i > 0 { s.push(','); } write_jcs(x, s)?; } s.push(']'); }
        Value::Object(o) => {
            let mut keys: Vec<&String> = o.keys().collect();
            keys.sort_by(|a, b| a.encode_utf16().cmp(b.encode_utf16()));
            s.push('{');
            for (i, k) in keys.iter().enumerate() {
                if i > 0 { s.push(','); }
                s.push_str(&serde_json::to_string(k).unwrap()); s.push(':'); write_jcs(&o[*k], s)?;
            }
            s.push('}');
        }
    }
    Ok(())
}
pub fn jcs_sha(v: &Value) -> String { sha256_hex(jcs(v).expect("jcs").as_bytes()) }

/// Port of qor_receipt.challenge.pattern_for.
pub fn pattern_for(nonce_hex: &str) -> Result<Vec<&'static str>, String> {
    const A: [&str; 8] = ["CROSS", "CIRCLE", "SQUARE", "TRIANGLE", "UP", "DOWN", "LEFT", "RIGHT"];
    if nonce_hex.len() != 32 || !nonce_hex.bytes().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()) {
        return Err("nonce must be 32 lowercase hex chars (16 bytes)".into());
    }
    let bytes: Vec<u8> = (0..16).map(|i| u8::from_str_radix(&nonce_hex[2 * i..2 * i + 2], 16).unwrap()).collect();
    let d = Sha256::digest(&bytes);
    let mut out: Vec<&str> = vec![];
    let mut i = 0;
    while out.len() < 4 {
        let mut b = A[(d[i % 32] % 8) as usize];
        i += 1;
        if out.last() == Some(&b) { b = A[(A.iter().position(|x| *x == b).unwrap() + 1) % 8]; }
        out.push(b);
    }
    let mut p = vec!["OPTIONS"]; p.extend(out);
    Ok(p)
}

#[derive(Debug, Clone)]
pub struct SegmentChain { pub seq: u64, pub head: String }
impl Default for SegmentChain { fn default() -> Self { Self { seq: 0, head: GENESIS.into() } } }
impl SegmentChain {
    /// Returns (seq, sha256, prev, new_head).
    pub fn push(&mut self, bytes: &[u8]) -> (u64, String, String, String) {
        let sha = sha256_hex(bytes);
        let prev = self.head.clone();
        let head = link(&prev, self.seq, &sha);
        let seq = self.seq;
        self.seq += 1; self.head = head.clone();
        (seq, sha, prev, head)
    }
}
pub fn link(prev: &str, seq: u64, sha: &str) -> String { jcs_sha(&json!({"prev": prev, "seq": seq, "sha256": sha})) }

#[derive(Debug, Clone)]
pub struct CheckpointChain { pub seq: u64, pub prev: String }
impl Default for CheckpointChain { fn default() -> Self { Self { seq: 0, prev: GENESIS.into() } } }
impl CheckpointChain {
    /// `ext` = QorCap evidence object; committed via frames_hash = SHA-256(JCS(ext)).
    pub fn next(&mut self, t_ms: u64, ext: Value) -> Value { self.next_with_input(t_ms, ext, None) }
    /// `input_hash` = SHA-256(JCS([input records])) of the controller chunk sealed with this checkpoint
    /// (None = SHA-256(JCS([])), the pre-0.3.0 value).
    pub fn next_with_input(&mut self, t_ms: u64, ext: Value, input_hash: Option<String>) -> Value {
        let mut body = Map::new();
        body.insert("seq".into(), json!(self.seq));
        body.insert("t_ms".into(), json!(t_ms));
        body.insert("frames_hash".into(), json!(jcs_sha(&ext)));
        body.insert("input_hash".into(), json!(input_hash.unwrap_or_else(|| jcs_sha(&json!([])))));
        body.insert("prev".into(), json!(self.prev));
        let h = jcs_sha(&Value::Object(body.clone()));
        body.insert("hash".into(), json!(h));
        body.insert("qorcap".into(), ext);
        self.seq += 1; self.prev = h;
        Value::Object(body)
    }
}

/// Recompute a checkpoint's QorTime hash and its ext commitment.
pub fn check_checkpoint(cp: &Value, prev: &str) -> Result<String, String> {
    let body = json!({"seq": cp["seq"], "t_ms": cp["t_ms"], "frames_hash": cp["frames_hash"], "input_hash": cp["input_hash"], "prev": cp["prev"]});
    if cp["prev"] != prev { return Err("prev link broken".into()); }
    let h = jcs_sha(&body);
    if cp["hash"] != h.as_str() { return Err("hash mismatch".into()); }
    if cp["frames_hash"] != jcs_sha(&cp["qorcap"]).as_str() { return Err("qorcap evidence does not match frames_hash".into()); }
    Ok(h)
}

/// Incremental splitter of a fragmented-MP4 byte stream into segments:
/// segment 0 = everything before the first `moof` (ftyp+moov), then each
/// `moof` + following boxes (mdat) up to the next `moof`.
#[derive(Default)]
pub struct Mp4Splitter { buf: Vec<u8>, cur: Vec<u8>, pos: usize, seen_moof: bool, pub offset: u64 }
impl Mp4Splitter {
    /// Feed bytes; returns completed segments as (file_offset, bytes).
    pub fn feed(&mut self, data: &[u8]) -> Vec<(u64, Vec<u8>)> {
        self.buf.extend_from_slice(data);
        let mut out = vec![];
        loop {
            let b = &self.buf[self.pos..];
            if b.len() < 8 { break; }
            let mut size = u32::from_be_bytes([b[0], b[1], b[2], b[3]]) as u64;
            let ty = &b[4..8];
            if size == 1 { if b.len() < 16 { break; } size = u64::from_be_bytes(b[8..16].try_into().unwrap()); }
            if size < 8 { size = b.len() as u64; } // malformed/size-0 box: treat rest as one box
            if (b.len() as u64) < size { break; }
            let is_moof = ty == b"moof";
            if is_moof && (!self.cur.is_empty()) {
                let seg = std::mem::take(&mut self.cur);
                let off = self.offset; self.offset += seg.len() as u64;
                out.push((off, seg));
            }
            if is_moof { self.seen_moof = true; }
            self.cur.extend_from_slice(&b[..size as usize]);
            self.pos += size as usize;
        }
        if self.pos > 0 { self.buf.drain(..self.pos); self.pos = 0; }
        out
    }
    /// End of stream: flush the last segment (and any trailing partial bytes).
    pub fn finish(&mut self) -> Option<(u64, Vec<u8>)> {
        let mut seg = std::mem::take(&mut self.cur);
        seg.extend(self.buf.drain(..));
        if seg.is_empty() { return None; }
        let off = self.offset; self.offset += seg.len() as u64;
        Some((off, seg))
    }
}

/// `result.winner` for a score pair: "home" / "away", "tie" on equal scores, "unknown" when no score was read.
/// (Until v0.3.11 a tie, and "no read at all" as 0-0, were drafted as an "away" win.)
pub fn result_winner(home: Option<i64>, away: Option<i64>) -> &'static str {
    match (home, away) {
        (Some(h), Some(a)) if h > a => "home",
        (Some(h), Some(a)) if a > h => "away",
        (Some(_), Some(_)) => "tie",
        _ => "unknown",
    }
}

/// Build a draft, UNSIGNED `qor-receipt/v0` receipt from what QorCap observed.
/// Keys, referee countersignatures, challenge verdicts and signatures are left
/// for QorTime (agent/referee) to fill; placeholders are all-zero / null.
pub fn draft_receipt(match_id: &str, role: &str, player_id: &str, started_ms: u64, ended_ms: u64, fin: &Value, challenges: &[Value]) -> Value {
    let (h, a) = (fin["home"].as_i64(), fin["away"].as_i64());
    let winner = result_winner(h, a);
    // A final (clock 0:00 / end menu) is in the 4th quarter or later; an unconfirmed last read keeps the quarter it
    // actually showed (or null when nothing was read) instead of being forced to 4.
    let quarter = if fin["unconfirmed"] == true { fin["quarter"].as_i64().filter(|q| *q >= 1).map(Value::from).unwrap_or(Value::Null) }
                  else { json!(fin["quarter"].as_i64().unwrap_or(4).max(4)) };
    let ch: Vec<Value> = challenges.iter().map(|c| json!({
        "id": c["id"], "role": c["role"], "phase": c["phase"], "nonce": c["nonce"], "pattern": c["pattern"],
        "issued_ms": c["issued_ms"], "deadline_ms": c["deadline_ms"], "result": Value::Null })).collect();
    json!({
        "schema": "qor-receipt/v0",
        "match": {"match_id": match_id, "game": "madden27", "platform": "ps5", "scorebug_profile": "madden27_standard",
                  "mode": "single", "started_ms": started_ms, "ended_ms": ended_ms},
        "result": {"home_score": h, "away_score": a, "quarter": quarter,
                   "final_t_ms": fin["t_ms"], "winner": winner, "method": "agreed_scorebug_final", "final_basis": fin["basis"]},
        "players": [{"role": role, "player_id": player_id, "pubkey": GENESIS, "key_kind": "software"}],
        "referee": {"pubkey": GENESIS, "key_id": "0000000000000000", "key_kind": "unset"},
        "challenges": ch,
        "checkpoints": {},
        "agreement": {"timelines_agree": Value::Null, "mismatches": []},
        "signals": {"motion_permille": {role: Value::Null}},
        "trust_tier": "single-attested",
        "signatures": {"players": {}, "referee": Value::Null}
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn draft_winner_tie_and_unknown_are_not_away() {
        assert_eq!(result_winner(Some(7), Some(3)), "home");
        assert_eq!(result_winner(Some(3), Some(7)), "away");
        assert_eq!(result_winner(Some(7), Some(7)), "tie");
        assert_eq!(result_winner(None, None), "unknown");
        let tie = draft_receipt("m1", "home", "p", 1, 2, &json!({"home": 7, "away": 7, "quarter": 1, "t_ms": 2, "basis": null, "unconfirmed": true}), &[]);
        assert_eq!(tie["result"]["winner"], "tie");
        assert_eq!(tie["result"]["quarter"], 1); // real quarter kept, not forced to 4
        let none = draft_receipt("m1", "home", "p", 1, 2, &json!({"home": null, "away": null, "quarter": null, "t_ms": 2, "basis": null, "unconfirmed": true}), &[]);
        assert_eq!((none["result"]["winner"].as_str(), none["result"]["quarter"].is_null()), (Some("unknown"), true));
        assert!(none["result"]["home_score"].is_null() && none["result"]["away_score"].is_null());
        let fin = draft_receipt("m1", "home", "p", 1, 2, &json!({"home": 21, "away": 21, "quarter": 5, "t_ms": 2, "basis": "end_menu"}), &[]);
        assert_eq!((fin["result"]["winner"].as_str(), fin["result"]["quarter"].as_i64()), (Some("tie"), Some(5)));
    }

    #[test]
    fn jcs_matches_qortime_vector() {
        let v: Value = serde_json::from_str(include_str!("../tests/vectors/jcs_vector.json")).unwrap();
        let c = jcs(&v["input"]).unwrap();
        assert_eq!(c, v["canonical"].as_str().unwrap());
        assert_eq!(sha256_hex(c.as_bytes()), v["sha256"].as_str().unwrap());
        assert!(jcs(&json!({"x": 1.5})).is_err());
    }
    #[test]
    fn pattern_matches_python() {
        assert_eq!(pattern_for("00112233445566778899aabbccddeeff").unwrap(), ["OPTIONS", "CROSS", "SQUARE", "DOWN", "SQUARE"]);
        assert_eq!(pattern_for("ffffffffffffffffffffffffffffffff").unwrap(), ["OPTIONS", "SQUARE", "LEFT", "DOWN", "UP"]);
        assert!(pattern_for("xyz").is_err());
        assert!(pattern_for("FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF").is_err());
    }
    #[test]
    fn checkpoint_hash_matches_python_rule() {
        // qor_receipt.checkpoints.make_checkpoint(0,1000,'a'*64,'b'*64,GENESIS)['hash']
        let body = json!({"seq":0,"t_ms":1000,"frames_hash":"a".repeat(64),"input_hash":"b".repeat(64),"prev":GENESIS});
        assert_eq!(jcs_sha(&body), "03245993acbd244a96e72e8bcabcb484e4f1bfd19a74ca9ee60502276b22c016");
        assert_eq!(jcs_sha(&json!([])), "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945");
        let mut c = CheckpointChain::default();
        let a = c.next(5, json!({"trigger": "segment"}));
        let b = c.next(6, json!({"trigger": "score_change", "score": {"home": 7, "away": 0}}));
        let ha = check_checkpoint(&a, GENESIS).unwrap();
        check_checkpoint(&b, &ha).unwrap();
        let mut t = b.clone(); t["qorcap"]["score"]["home"] = json!(14);
        assert!(check_checkpoint(&t, &ha).is_err(), "tampered score detected");
    }
    fn bx(ty: &[u8; 4], n: usize) -> Vec<u8> { let mut v = ((8 + n) as u32).to_be_bytes().to_vec(); v.extend_from_slice(ty); v.extend(vec![7u8; n]); v }
    #[test]
    fn splitter_segments_and_chain() {
        let stream = [bx(b"ftyp", 4), bx(b"moov", 20), bx(b"moof", 10), bx(b"mdat", 100), bx(b"moof", 10), bx(b"mdat", 50)].concat();
        let mut s = Mp4Splitter::default();
        let mut segs = vec![];
        for ch in stream.chunks(7) { segs.extend(s.feed(ch)); }
        segs.extend(s.finish());
        assert_eq!(segs.iter().map(|x| x.1.len()).collect::<Vec<_>>(), vec![12 + 28, 18 + 108, 18 + 58]);
        assert_eq!(segs[1].0, 40);
        assert_eq!(segs.iter().map(|x| x.1.clone()).collect::<Vec<_>>().concat(), stream);
        let mut c = SegmentChain::default();
        let (_, sha0, _, h0) = c.push(&segs[0].1);
        assert_eq!(h0, link(GENESIS, 0, &sha0));
        let (seq, _, prev, _) = c.push(&segs[1].1);
        assert_eq!((seq, prev), (1, h0));
    }
    #[test]
    fn draft_receipt_shape() {
        let r = draft_receipt("m1", "home", "psn:con", 1, 2, &json!({"home": 21, "away": 24, "quarter": 4, "t_ms": 2, "basis": "clock_zero"}), &[]);
        assert_eq!(r["result"]["winner"], "away");
        assert_eq!(r["result"]["final_basis"], "clock_zero");
        assert!(jcs(&r).is_ok(), "no floats");
    }
}
