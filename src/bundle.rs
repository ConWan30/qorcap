//! `qorcap verify-bundle DIR`: re-hash video bytes against manifest.jsonl and
//! re-check the checkpoint chain. Integrity/timing only; not identity.
use crate::evidence::{self as ev, GENESIS};
use serde_json::Value;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;

fn lines(p: &Path) -> Result<Vec<Value>, String> {
    let s = std::fs::read_to_string(p).map_err(|e| format!("{}: {e}", p.display()))?;
    s.lines().filter(|l| !l.trim().is_empty()).enumerate()
        .map(|(i, l)| serde_json::from_str(l).map_err(|e| format!("{}:{}: {e}", p.display(), i + 1))).collect()
}

pub struct Report { pub segments: u64, pub bytes: u64, pub checkpoints: u64, pub chain_head: String, pub errors: Vec<String> }

pub fn verify(dir: &Path) -> Result<Report, String> {
    let man = lines(&dir.join("manifest.jsonl"))?;
    let mut r = Report { segments: 0, bytes: 0, checkpoints: 0, chain_head: GENESIS.into(), errors: vec![] };
    let mut expect_off: std::collections::HashMap<String, u64> = Default::default();
    let mut heads = std::collections::HashSet::new();
    heads.insert(GENESIS.to_string());
    for m in man.iter().filter(|m| m["type"] == "segment") {
        let seq = m["seq"].as_u64().unwrap_or(u64::MAX);
        let file = m["file"].as_str().unwrap_or("");
        let (off, len) = (m["offset"].as_u64().unwrap_or(0), m["bytes"].as_u64().unwrap_or(0));
        let tag = format!("segment {seq} ({file}@{off})");
        if seq != r.segments { r.errors.push(format!("{tag}: seq gap (expected {})", r.segments)); }
        let eo = expect_off.entry(file.to_string()).or_insert(0);
        if off != *eo { r.errors.push(format!("{tag}: offset gap (expected {eo})")); }
        *eo = off + len;
        let mut buf = vec![0u8; len as usize];
        let rd = std::fs::File::open(dir.join(file)).and_then(|mut f| { f.seek(SeekFrom::Start(off))?; f.read_exact(&mut buf) });
        match rd {
            Err(e) => r.errors.push(format!("{tag}: cannot read video bytes: {e}")),
            Ok(()) => { if ev::sha256_hex(&buf) != m["sha256"].as_str().unwrap_or("") { r.errors.push(format!("{tag}: SHA-256 mismatch (video bytes altered)")); } }
        }
        if m["prev"].as_str() != Some(r.chain_head.as_str()) { r.errors.push(format!("{tag}: prev link broken")); }
        let head = ev::link(&r.chain_head, seq, m["sha256"].as_str().unwrap_or(""));
        if m["chain_head"].as_str() != Some(head.as_str()) { r.errors.push(format!("{tag}: chain head mismatch")); }
        r.chain_head = head.clone();
        heads.insert(head);
        r.segments += 1; r.bytes += len;
    }
    for (f, end) in &expect_off {
        let real = std::fs::metadata(dir.join(f)).map(|m| m.len()).unwrap_or(0);
        if real != *end { r.errors.push(format!("{f}: {real} bytes on disk but manifest covers {end} (unhashed tail or truncation)")); }
    }
    if let Ok(cps) = lines(&dir.join("checkpoints.jsonl")) {
        let mut prev = GENESIS.to_string();
        for (i, c) in cps.iter().enumerate() {
            match ev::check_checkpoint(c, &prev) { Ok(h) => prev = h, Err(e) => { r.errors.push(format!("checkpoint {i}: {e}")); prev = c["hash"].as_str().unwrap_or("").into(); } }
            if c["seq"].as_u64() != Some(i as u64) { r.errors.push(format!("checkpoint {i}: seq gap")); }
            if let Some(h) = c["qorcap"]["chain_head"].as_str() { if !heads.contains(h) { r.errors.push(format!("checkpoint {i}: refers to unknown video chain head")); } }
            r.checkpoints += 1;
        }
    }
    Ok(r)
}
