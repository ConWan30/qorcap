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
                eprintln!("WARN: {}", escape_output(&msg));
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
    pub input_chunks: u64, pub input_head: Option<String>,
    /// Not failures: things a reader must not take as evidence (e.g. an unbound capture stop record).
    pub warnings: Vec<String> }

/// Practice capture stop record: a stop checkpoint that names `capture_stop_sha256` must match the file's
/// bytes. A record file that no checkpoint binds (a stop write that failed, a crash, or a stale `state: open`
/// file) is a warning, never an error, so older bundles still pass: it is UNBOUND and is not evidence.
fn check_capture_stop(dir: &Path, cps: &[Value], errors: &mut Vec<String>, warnings: &mut Vec<String>) {
    let mut bound = false;
    for (i, c) in cps.iter().enumerate() {
        let q = &c["qorcap"];
        let Some(want) = q["capture_stop_sha256"].as_str() else { continue };
        bound = true;
        let name = q["capture_stop_file"].as_str().unwrap_or(crate::capstop::FILE_NAME);
        match contained_file(dir, name).and_then(|p| std::fs::read(&p).map_err(|e| format!("cannot read {name}: {e}"))) {
            Ok(bytes) => if ev::sha256_hex(&bytes) != want { errors.push(format!("checkpoint {i}: {name} SHA-256 does not match the stop checkpoint (record altered)")); },
            Err(e) => errors.push(format!("checkpoint {i}: capture stop record: {e}")),
        }
    }
    // A temp file left by an interrupted write (capstop::write_file renames it into place).
    let tmp = format!("{}.tmp", crate::capstop::FILE_NAME);
    if std::fs::symlink_metadata(dir.join(&tmp)).is_ok() {
        warnings.push(format!("{tmp} is present (an interrupted capture stop record write): not evidence"));
    }
    if bound { return; }
    let name = crate::capstop::FILE_NAME;
    let Ok(p) = contained_file(dir, name) else { return };
    let Ok(text) = std::fs::read_to_string(&p) else { return };
    // The file is unbound, so untrusted: only `open` or `stop` is echoed, anything else prints as UNKNOWN,
    // so its text can never add lines to verify-bundle output (or the verify.txt made from it).
    let state = serde_json::from_str::<Value>(&text).ok()
        .and_then(|v| v["state"].as_str().filter(|s| matches!(*s, "open" | "stop")).map(str::to_owned))
        .unwrap_or_else(|| crate::capstop::UNKNOWN.into());
    warnings.push(format!("{name} is present but no checkpoint binds it (state: {state}): UNBOUND, not evidence"));
}

/// Unicode general category Cf (format), plus the line and paragraph separators
/// U+2028 / U+2029 (Zl / Zp). Ranges from Unicode 15.1. Rust `char::is_control()`
/// is Cc only and would let these through.
fn is_unicode_format(c: char) -> bool {
    matches!(c,
        '\u{00AD}' |
        '\u{0600}'..='\u{0605}' |
        '\u{061C}' |
        '\u{06DD}' |
        '\u{070F}' |
        '\u{0890}'..='\u{0891}' |
        '\u{08E2}' |
        '\u{180E}' |
        '\u{200B}'..='\u{200F}' |
        '\u{2028}'..='\u{2029}' |
        '\u{202A}'..='\u{202E}' |
        '\u{2060}'..='\u{2064}' |
        '\u{2066}'..='\u{206F}' |
        '\u{FEFF}' |
        '\u{FFF9}'..='\u{FFFB}' |
        '\u{110BD}' |
        '\u{110CD}' |
        '\u{13430}'..='\u{1343F}' |
        '\u{1BCA0}'..='\u{1BCA3}' |
        '\u{1D173}'..='\u{1D17A}' |
        '\u{E0001}' |
        '\u{E0020}'..='\u{E007F}'
    )
}

/// Render an untrusted string so no control character, CR, LF, ANSI escape,
/// backslash, or Unicode format character (including bidi overrides) can reach
/// verify-bundle stdout, stderr, or the sidecar's verify.txt.
///
/// Backslash is emitted as `\\` first, then controls, so a literal `\n` in a
/// name is distinct from an escaped newline. Printable text other than
/// backslash (including spaces and ordinary Unicode) is unchanged, so a valid
/// ASCII bundle's output stays byte-identical.
pub fn escape_output(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{1b}' => out.push_str("\\x1b"),
            '\0' => out.push_str("\\0"),
            c if c.is_control() || is_unicode_format(c) => {
                out.push_str(&format!("\\u{{{:x}}}", c as u32));
            }
            c => out.push(c),
        }
    }
    out
}

/// The text `qorcap verify-bundle` prints for a report, one entry per line, and its exit code.
/// Every untrusted string (file names, ids, states, errors, warnings, chain heads) is passed
/// through [`escape_output`] here, which is the only print path for a successful verify.
pub fn report_lines(r: &Report) -> (Vec<String>, i32) {
    let mut out = vec![format!("segments={} bytes={} checkpoints={} chain_head={}", r.segments, r.bytes, r.checkpoints, escape_output(&r.chain_head))];
    if let Some(h) = &r.input_head { out.push(format!("controller input chunks={} inputs_chain_head={}", r.input_chunks, escape_output(h))); }
    for e in &r.errors { out.push(format!("FAIL: {}", escape_output(e))); }
    for w in &r.warnings { out.push(format!("WARN: {}", escape_output(w))); }
    out.push(if r.errors.is_empty() { "OK: video bytes match the hash chain; checkpoints link. (Integrity + timing only; does not prove who played.)".into() } else { "FAILED".into() });
    (out, if r.errors.is_empty() { 0 } else { 1 })
}

pub fn verify(dir: &Path) -> Result<Report, String> {
    let man = lines(&dir.join("manifest.jsonl"))?;
    let mut r = Report { segments: 0, bytes: 0, checkpoints: 0, chain_head: GENESIS.into(), errors: vec![], input_chunks: 0, input_head: None, warnings: vec![] };
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
        check_capture_stop(dir, &cps, &mut r.errors, &mut r.warnings);
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

    fn scratch(tag: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!(
            "qorcap-vb-{}-{}-{}",
            tag,
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ))
    }

    fn write_segment(dir: &Path, file: &str, body: &[u8]) -> String {
        let sha = ev::sha256_hex(body);
        let head = ev::link(GENESIS, 0, &sha);
        std::fs::write(
            dir.join("manifest.jsonl"),
            format!(
                "{}\n",
                serde_json::json!({"type":"segment","seq":0,"file":file,"offset":0,"bytes": body.len(),
                    "sha256": sha, "prev": GENESIS, "chain_head": head})
            ),
        )
        .unwrap();
        head
    }

    /// Valid bundles (ordinary file names, no controls) must print the same bytes as before
    /// the escape helper: escape is a no-op on this text.
    #[test]
    fn valid_bundle_report_lines_are_byte_identical() {
        let dir = scratch("ok");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let body = b"abcdef";
        std::fs::write(dir.join("video.mp4"), body).unwrap();
        let head = write_segment(&dir, "video.mp4", body);
        let rep = verify(&dir).unwrap();
        assert!(rep.errors.is_empty() && rep.warnings.is_empty(), "{:?} {:?}", rep.errors, rep.warnings);
        let (lines, code) = report_lines(&rep);
        assert_eq!(code, 0);
        let expected = format!(
            "segments=1 bytes=6 checkpoints=0 chain_head={head}\nOK: video bytes match the hash chain; checkpoints link. (Integrity + timing only; does not prove who played.)"
        );
        assert_eq!(lines.join("\n"), expected);
        assert_eq!(lines.join("\n").as_bytes(), expected.as_bytes());
        // A typical FAIL with a valid file name is also unchanged (escape is a no-op).
        std::fs::write(dir.join("video.mp4"), b"XXXXXX").unwrap();
        let bad = verify(&dir).unwrap();
        let (fail_lines, fail_code) = report_lines(&bad);
        assert_eq!(fail_code, 1);
        let fail_expected = format!(
            "segments=1 bytes=6 checkpoints=0 chain_head={head}\nFAIL: segment 0 (video.mp4@0): SHA-256 mismatch (video bytes altered)\nFAILED"
        );
        assert_eq!(fail_lines.join("\n"), fail_expected);
        let _ = std::fs::remove_dir_all(&dir);
    }

    fn assert_no_control_in_report(lines: &[String]) {
        for l in lines {
            assert!(
                !l.chars().any(|c| c.is_control() || is_unicode_format(c)),
                "control or format character in report line: {l:?}"
            );
        }
        let text = lines.join("\n");
        assert_eq!(text.lines().count(), lines.len(), "{text:?}");
        assert_eq!(text.lines().filter(|l| l.starts_with("OK:")).count(), 0, "{text}");
        assert!(!text.contains('\u{1b}'), "{text:?}");
    }

    /// Manifest `file` and checkpoint `capture_stop_file` used to be printed raw, so a newline,
    /// CRLF, ESC[, or tab could add a fake `OK:` line or an ANSI run to stdout / verify.txt.
    #[test]
    fn verify_bundle_escapes_injected_file_names() {
        let cases: &[(&str, &str)] = &[
            ("nl", "video.mp4\nOK: fake"),
            ("crlf", "video.mp4\r\nOK: fake"),
            ("esc", "video.mp4\u{1b}[32mOK: fake"),
            ("tab", "video.mp4\tOK: fake"),
            ("bidi", "video.mp4\u{202e}fake"),
            ("zwsp", "video.mp4\u{200b}fake"),
            ("bsn", "video.mp4\\nOK: fake"),
        ];
        for (tag, name) in cases {
            let dir = scratch(&format!("inj-{tag}"));
            let _ = std::fs::remove_dir_all(&dir);
            std::fs::create_dir_all(&dir).unwrap();
            write_segment(&dir, name, b"abcdef");
            let rep = verify(&dir).unwrap();
            assert!(!rep.errors.is_empty(), "{tag}: expected a FAIL, got {:?}", rep.errors);
            let (lines, code) = report_lines(&rep);
            assert_eq!(code, 1, "{tag}");
            assert_eq!(lines.last().map(String::as_str), Some("FAILED"), "{tag}: {lines:?}");
            assert_no_control_in_report(&lines);
            let text = lines.join("\n");
            assert!(text.contains("fake"), "{tag}: escaped name should still be visible: {text}");
            let _ = std::fs::remove_dir_all(&dir);
        }

        // The same injection through a bound capture-stop file name on a checkpoint.
        for (tag, name) in cases {
            let dir = scratch(&format!("stop-{tag}"));
            let _ = std::fs::remove_dir_all(&dir);
            std::fs::create_dir_all(&dir).unwrap();
            std::fs::write(dir.join("manifest.jsonl"), "").unwrap();
            let cp = serde_json::json!({"qorcap":{"capture_stop_sha256": "00".repeat(32), "capture_stop_file": name}});
            std::fs::write(dir.join("checkpoints.jsonl"), format!("{cp}\n")).unwrap();
            let rep = verify(&dir).unwrap();
            assert!(
                rep.errors.iter().any(|e| e.contains("capture stop") || e.contains(*name) || e.contains("cannot read")),
                "{tag}: {name:?} not reflected in errors: {:?}",
                rep.errors
            );
            let (lines, code) = report_lines(&rep);
            assert_eq!(code, 1, "{tag}");
            assert_eq!(lines.last().map(String::as_str), Some("FAILED"), "{tag}: {lines:?}");
            assert_no_control_in_report(&lines);
            let _ = std::fs::remove_dir_all(&dir);
        }
    }
    #[test]
    fn escape_output_is_identity_for_plain_text_and_strips_controls() {
        assert_eq!(escape_output(""), "");
        assert_eq!(escape_output("video.mp4"), "video.mp4");
        assert_eq!(
            escape_output("segment 0 (video.mp4@0): SHA-256 mismatch (video bytes altered)"),
            "segment 0 (video.mp4@0): SHA-256 mismatch (video bytes altered)"
        );
        assert_eq!(escape_output("café.mp4"), "café.mp4");
        assert_eq!(escape_output("video.mp4\nOK: fake"), "video.mp4\\nOK: fake");
        assert_eq!(escape_output("video.mp4\r\nOK: fake"), "video.mp4\\r\\nOK: fake");
        assert_eq!(escape_output("video.mp4\u{1b}[32mOK: fake"), "video.mp4\\x1b[32mOK: fake");
        assert_eq!(escape_output("video.mp4\tOK: fake"), "video.mp4\\tOK: fake");
        assert_eq!(escape_output("x\u{2028}OK: fake"), "x\\u{2028}OK: fake");
        assert_eq!(escape_output("\0"), "\\0");
        assert_eq!(escape_output("a\u{07}b\u{9b}c"), "a\\u{7}b\\u{9b}c");
        // A literal backslash-n in a name is distinct from an escaped newline.
        assert_eq!(escape_output("video.mp4\\nOK: fake"), "video.mp4\\\\nOK: fake");
        assert_ne!(escape_output("video.mp4\nOK: fake"), escape_output("video.mp4\\nOK: fake"));
        assert_eq!(escape_output("video.mp4\u{202e}fake"), "video.mp4\\u{202e}fake");
        assert_eq!(escape_output("video.mp4\u{200b}fake"), "video.mp4\\u{200b}fake");
        assert_eq!(escape_output("\u{202a}\u{2066}\u{feff}"), "\\u{202a}\\u{2066}\\u{feff}");
        let cleaned = escape_output("a\n\r\t\u{1b}[0m\u{2028}\u{2029}\0\u{202e}\u{200b}\\");
        assert!(!cleaned.chars().any(|c| c.is_control() || is_unicode_format(c)), "{cleaned:?}");
        assert!(cleaned.ends_with("\\\\"), "{cleaned:?}");
        // Every Cf / Zl / Zp code point is escaped and does not appear raw.
        const FORMAT: &[(u32, u32)] = &[
            (0x00AD, 0x00AD), (0x0600, 0x0605), (0x061C, 0x061C), (0x06DD, 0x06DD),
            (0x070F, 0x070F), (0x0890, 0x0891), (0x08E2, 0x08E2), (0x180E, 0x180E),
            (0x200B, 0x200F), (0x2028, 0x2029), (0x202A, 0x202E), (0x2060, 0x2064),
            (0x2066, 0x206F), (0xFEFF, 0xFEFF), (0xFFF9, 0xFFFB), (0x110BD, 0x110BD),
            (0x110CD, 0x110CD), (0x13430, 0x1343F), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A),
            (0xE0001, 0xE0001), (0xE0020, 0xE007F),
        ];
        for &(lo, hi) in FORMAT {
            for u in lo..=hi {
                let c = char::from_u32(u).unwrap();
                let got = escape_output(&c.to_string());
                assert_eq!(got, format!("\\u{{{u:x}}}"), "U+{u:04X}");
                assert!(!got.contains(c), "U+{u:04X} leaked: {got:?}");
            }
        }
    }
}
