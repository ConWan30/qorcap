//! `qorcap verify-bundle DIR`: re-hash video bytes against manifest.jsonl and
//! re-check the checkpoint chain. Integrity/timing only; not identity.
use crate::evidence::{self as ev, GENESIS};
use serde_json::Value;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;

/// Parse JSONL, skipping blank lines and salvaging past a single corrupt line (MyTurn finalize case).
fn lines(p: &Path) -> Result<Vec<Value>, String> {
    lines_resilient(p, true)
}

fn lines_resilient(p: &Path, salvage: bool) -> Result<Vec<Value>, String> {
    // Share the file with the recorder. verify-bundle reads inputs.jsonl / manifest.jsonl while a match
    // is still appending them; an exclusive read is the sharing violation that used to drop input chunks.
    let s = crate::inputs::read_shared_string(p).map_err(|e| format!("{}: {e}", p.display()))?;
    let mut out = Vec::new();
    let mut bad = 0usize;
    for (i, l) in s.lines().filter(|l| !l.trim().is_empty()).enumerate() {
        match serde_json::from_str::<Value>(l) {
            Ok(v) => out.push(v),
            Err(e) => {
                bad += 1;
                let msg = format!("{}:{}: corrupt JSONL line skipped ({e})", p.display(), i + 1);
                if !salvage || bad > 3 {
                    return Err(msg);
                }
                eprintln!("WARN: {msg}");
            }
        }
    }
    Ok(out)
}

pub struct Report { pub segments: u64, pub bytes: u64, pub checkpoints: u64, pub chain_head: String, pub errors: Vec<String>,
    /// Controller input chunks verified (0 when the bundle has no inputs.jsonl).
    pub input_chunks: u64, pub input_head: Option<String> }

pub fn verify(dir: &Path) -> Result<Report, String> {
    let man = lines(&dir.join("manifest.jsonl"))?;
    let mut r = Report { segments: 0, bytes: 0, checkpoints: 0, chain_head: GENESIS.into(), errors: vec![], input_chunks: 0, input_head: None };
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
    // Controller inputs: re-hash chunks + chain, and tie each chunk to the checkpoint that committed it.
    let mut input_by_seg: Option<std::collections::HashMap<u64, String>> = None;
    let ip = dir.join("inputs.jsonl");
    if ip.exists() {
        match crate::inputs::read_file(&ip) {
            Ok((recs, chunks)) => {
                let (m, head, errs) = crate::inputs::verify_chunks(&recs, &chunks);
                r.errors.extend(errs); r.input_chunks = chunks.len() as u64; r.input_head = Some(head); input_by_seg = Some(m);
            }
            Err(e) => r.errors.push(e),
        }
    }
    let empty_input = crate::inputs::empty_hash();
    let mut committed = 0usize;
    if let Ok(cps) = lines(&dir.join("checkpoints.jsonl")) {
        let mut prev = GENESIS.to_string();
        for (i, c) in cps.iter().enumerate() {
            match ev::check_checkpoint(c, &prev) { Ok(h) => prev = h, Err(e) => { r.errors.push(format!("checkpoint {i}: {e}")); prev = c["hash"].as_str().unwrap_or("").into(); } }
            if c["seq"].as_u64() != Some(i as u64) { r.errors.push(format!("checkpoint {i}: seq gap")); }
            if let Some(h) = c["qorcap"]["chain_head"].as_str() { if !heads.contains(h) { r.errors.push(format!("checkpoint {i}: refers to unknown video chain head")); } }
            if let (Some(seg), Some(m)) = (c["qorcap"]["input_seg"].as_u64(), input_by_seg.as_ref()) {
                let want = m.get(&seg).cloned().unwrap_or_else(|| empty_input.clone());
                if c["input_hash"].as_str() != Some(want.as_str()) { r.errors.push(format!("checkpoint {i}: input_hash does not match inputs.jsonl chunk for segment {seg}")); }
                if m.contains_key(&seg) { committed += 1; }
            }
            r.checkpoints += 1;
        }
    }
    if let Some(m) = input_by_seg.as_ref() { if committed < m.len() { r.errors.push(format!("inputs: {} chunk(s) not committed by any checkpoint", m.len() - committed)); } }
    Ok(r)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    #[test]
    fn salvage_one_bad_jsonl_line() {
        let dir = std::env::temp_dir().join(format!("qorcap-jsonl-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let man = dir.join("manifest.jsonl");
        let mut f = std::fs::File::create(&man).unwrap();
        writeln!(f, r#"{{"type":"reopen","mono_ms":1}}"#).unwrap();
        writeln!(f, "{{this is not json").unwrap(); // corrupt
        writeln!(f, r#"{{"type":"segment","seq":0,"file":"video.mp4","offset":0,"bytes":0,"sha256":"","prev":"0000000000000000000000000000000000000000000000000000000000000000","chain_head":"0000000000000000000000000000000000000000000000000000000000000000"}}"#).unwrap();
        let v = lines(&man).unwrap();
        assert_eq!(v.len(), 2);
        assert_eq!(v[0]["type"], "reopen");
        assert_eq!(v[1]["type"], "segment");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
