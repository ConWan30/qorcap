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

struct ExcludedTail { file: String, covered: u64, tail_bytes: u64, sha: String }

fn recorded_tails(dir: &Path) -> Vec<ExcludedTail> {
    let Ok(s) = std::fs::read_to_string(dir.join("bundle.json")) else { return vec![] };
    let Ok(v) = serde_json::from_str::<Value>(&s) else { return vec![] };
    let Some(arr) = v.get("excluded_tails").and_then(|x| x.as_array()) else { return vec![] };
    arr.iter().filter_map(|t| {
        Some(ExcludedTail {
            file: t.get("file")?.as_str()?.to_string(),
            covered: t.get("covered_bytes")?.as_u64()?,
            tail_bytes: t.get("tail_bytes")?.as_u64()?,
            sha: t.get("tail_sha256")?.as_str()?.to_string(),
        })
    }).collect()
}

/// A manifest or bundle name that is one file directly inside `dir`.
///
/// Rejects `..`, separators, and a Windows drive prefix (`C:name`). `Path::join` replaces
/// the directory when the name is absolute or has a prefix, which would let `verify-bundle`
/// hash a file outside the bundle and report the tampered video as intact.
pub fn contained_file(dir: &Path, name: &str) -> Result<std::path::PathBuf, String> {
    let one = std::path::Path::new(name).components().count() == 1
        && matches!(std::path::Path::new(name).components().next(), Some(std::path::Component::Normal(_)));
    if name.is_empty() || !one || name.contains(['/', '\\', ':', '\0']) || name == "." || name == ".." {
        return Err(format!("refusing a path that is not a single file in the bundle ({name})"));
    }
    let path = dir.join(name);
    if path.parent() != Some(dir) {
        return Err(format!("refusing a path that is not a single file in the bundle ({name})"));
    }
    // `File::open` follows a symlink. A link named `video.mp4` can point at a key outside the bundle.
    if std::fs::symlink_metadata(&path).map(|m| m.file_type().is_symlink()).unwrap_or(false) {
        return Err(format!("refusing a path that is not a single file in the bundle ({name})"));
    }
    Ok(path)
}

/// Bytes past the last hashed segment of each video file. The tail is named and hashed so
/// `verify` can accept exactly those bytes and reject anything else. Nothing is added to the chain.
pub fn measure_excluded_tails(dir: &Path) -> Vec<Value> {
    let Ok(man) = lines(&dir.join("manifest.jsonl")) else { return vec![] };
    let mut end: std::collections::BTreeMap<String, u64> = Default::default();
    for m in man.iter().filter(|m| m["type"] == "segment") {
        let file = match m["file"].as_str() { Some(f) if !f.is_empty() => f, _ => continue };
        if contained_file(dir, file).is_err() { continue; }
        let e = m["offset"].as_u64().unwrap_or(0).saturating_add(m["bytes"].as_u64().unwrap_or(0));
        end.entry(file.to_string()).and_modify(|x| *x = (*x).max(e)).or_insert(e);
    }
    let mut out = Vec::new();
    for (file, covered) in end {
        let Ok(path) = contained_file(dir, &file) else { continue };
        let real = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
        if real <= covered { continue; }
        let extra = real - covered;
        let Ok(sha) = sha256_range(&path, covered, extra) else { continue };
        out.push(serde_json::json!({
            "file": file, "covered_bytes": covered, "tail_bytes": extra, "tail_sha256": sha,
        }));
    }
    out
}

fn sha256_range(path: &Path, start: u64, len: u64) -> Result<String, String> {
    use sha2::{Digest, Sha256};
    let mut f = std::fs::File::open(path).map_err(|e| e.to_string())?;
    f.seek(SeekFrom::Start(start)).map_err(|e| e.to_string())?;
    let mut hasher = Sha256::new();
    let mut left = len;
    let mut buf = [0u8; 1 << 16];
    while left > 0 {
        let want = std::cmp::min(buf.len(), left as usize);
        let n = f.read(&mut buf[..want]).map_err(|e| e.to_string())?;
        if n == 0 { return Err("unexpected end of file".into()); }
        hasher.update(&buf[..n]);
        left -= n as u64;
    }
    Ok(ev::hex(hasher.finalize().as_slice()))
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
        match contained_file(dir, file) {
            Ok(p) => {
                let mut buf = vec![0u8; len as usize];
                match std::fs::File::open(p).and_then(|mut f| { f.seek(SeekFrom::Start(off))?; f.read_exact(&mut buf) }) {
                    Err(e) => r.errors.push(format!("{tag}: cannot read video bytes: {e}")),
                    Ok(()) => { if ev::sha256_hex(&buf) != m["sha256"].as_str().unwrap_or("") { r.errors.push(format!("{tag}: SHA-256 mismatch (video bytes altered)")); } }
                }
            }
            Err(msg) => r.errors.push(format!("{tag}: {msg}")),
        }
        if m["prev"].as_str() != Some(r.chain_head.as_str()) { r.errors.push(format!("{tag}: prev link broken")); }
        let head = ev::link(&r.chain_head, seq, m["sha256"].as_str().unwrap_or(""));
        if m["chain_head"].as_str() != Some(head.as_str()) { r.errors.push(format!("{tag}: chain head mismatch")); }
        r.chain_head = head.clone();
        heads.insert(head);
        r.segments += 1; r.bytes += len;
    }
    let tails = recorded_tails(dir);
    for (f, end) in &expect_off {
        let path = match contained_file(dir, f) {
            Ok(p) => p,
            Err(msg) => { r.errors.push(format!("{f}: {msg}")); continue; }
        };
        let real = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
        if real == *end { continue; }
        if real > *end {
            let extra = real - *end;
            if let Some(t) = tails.iter().find(|t| t.file == *f) {
                if t.covered == *end && t.tail_bytes == extra {
                    match sha256_range(&path, *end, extra) {
                        Ok(h) if h == t.sha => continue,
                        Ok(_) => r.errors.push(format!("{f}: excluded tail SHA-256 does not match bundle.json")),
                        Err(e) => r.errors.push(format!("{f}: cannot read excluded tail: {e}")),
                    }
                    continue;
                }
            }
            r.errors.push(format!("{f}: {real} bytes on disk but manifest covers {end} (unhashed tail or truncation)"));
        } else {
            r.errors.push(format!("{f}: {real} bytes on disk but manifest covers {end} (truncated)"));
        }
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
        let bind = crate::framebind::check_dir(dir, &cps);
        r.errors.extend(bind.errors);
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

    #[test]
    fn excluded_tail_is_accepted_only_when_the_record_matches() {
        let dir = std::env::temp_dir().join(format!("qorcap-tail-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let body = b"abcdef";
        let mut file = body.to_vec();
        file.extend_from_slice(b"TAIL");
        std::fs::write(dir.join("video.mp4"), &file).unwrap();
        let sha = ev::sha256_hex(body);
        let head = ev::link(GENESIS, 0, &sha);
        let line = serde_json::json!({"type":"segment","seq":0,"file":"video.mp4","offset":0,"bytes": body.len(),
            "sha256": sha, "prev": GENESIS, "chain_head": head});
        std::fs::write(dir.join("manifest.jsonl"), format!("{line}\n")).unwrap();
        let bare = verify(&dir).unwrap();
        assert!(bare.errors.iter().any(|e| e.contains("unhashed tail")), "{:?}", bare.errors);
        let tails = measure_excluded_tails(&dir);
        assert_eq!(tails[0]["tail_bytes"], 4);
        assert_eq!(tails[0]["covered_bytes"], body.len() as u64);
        std::fs::write(dir.join("bundle.json"), serde_json::to_string(&serde_json::json!({"excluded_tails": tails})).unwrap()).unwrap();
        let ok = verify(&dir).unwrap();
        assert!(ok.errors.is_empty(), "{:?}", ok.errors);
        let mut wrong: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("bundle.json")).unwrap()).unwrap();
        wrong["excluded_tails"][0]["tail_bytes"] = serde_json::json!(3);
        std::fs::write(dir.join("bundle.json"), wrong.to_string()).unwrap();
        let mismatch = verify(&dir).unwrap();
        assert!(mismatch.errors.iter().any(|e| e.contains("unhashed tail")), "{:?}", mismatch.errors);
        std::fs::write(dir.join("video.mp4"), &body[..body.len() - 1]).unwrap();
        std::fs::write(dir.join("bundle.json"), "{}").unwrap();
        let short = verify(&dir).unwrap();
        assert!(short.errors.iter().any(|e| e.contains("truncated") || e.contains("cannot read")), "{:?}", short.errors);
        let _ = std::fs::remove_dir_all(&dir);
    }

    fn seg_line(file: &str, body: &[u8]) -> String {
        let sha = ev::sha256_hex(body);
        let head = ev::link(GENESIS, 0, &sha);
        format!("{}\n", serde_json::json!({"type":"segment","seq":0,"file":file,"offset":0,"bytes": body.len(),
            "sha256": sha, "prev": GENESIS, "chain_head": head}))
    }

    #[test]
    fn verify_does_not_follow_a_segment_path_outside_the_bundle() {
        let dir = std::env::temp_dir().join(format!("qorcap-escape-{}", std::process::id()));
        let outside = std::env::temp_dir().join(format!("qorcap-outside-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let _ = std::fs::remove_file(&outside);
        std::fs::create_dir_all(&dir).unwrap();
        let secret = b"SECRET-KEY-MATERIAL";
        std::fs::write(&outside, secret).unwrap();
        std::fs::write(dir.join("video.mp4"), b"tampered-video").unwrap();
        let escaped = format!("../{}", outside.file_name().unwrap().to_string_lossy());
        std::fs::write(dir.join("manifest.jsonl"), seg_line(&escaped, secret)).unwrap();
        let report = verify(&dir).unwrap();
        assert!(report.errors.iter().any(|e| e.contains("not a single file")), "{:?}", report.errors);
        assert!(report.errors.iter().all(|e| !e.contains("SHA-256 mismatch")), "outside bytes must not be hashed: {:?}", report.errors);
        for name in ["../video.mp4", "/etc/passwd", r"..\keys\device.key", "C:video.mp4", "a/b.mp4", ""] {
            assert!(contained_file(&dir, name).is_err(), "{name}");
        }
        assert!(contained_file(&dir, "video.mp4").is_ok());
        #[cfg(unix)]
        {
            use std::os::unix::fs::symlink;
            std::fs::remove_file(dir.join("video.mp4")).unwrap();
            symlink(&outside, dir.join("video.mp4")).unwrap();
            std::fs::write(dir.join("manifest.jsonl"), seg_line("video.mp4", secret)).unwrap();
            let linked = verify(&dir).unwrap();
            assert!(linked.errors.iter().any(|e| e.contains("not a single file")), "{:?}", linked.errors);
            assert!(linked.errors.iter().all(|e| !e.contains("SHA-256 mismatch")), "symlink target must not be hashed: {:?}", linked.errors);
            assert!(std::fs::read(&outside).unwrap() == secret);
        }
        let _ = std::fs::remove_dir_all(&dir);
        let _ = std::fs::remove_file(&outside);
    }
}
