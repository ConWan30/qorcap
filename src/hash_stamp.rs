//! Opt-in auxiliary QorTime hash-only timestamp client and offline verifier (`qorcap hash-stamp`).
//!
//! Posts ONLY SHA-256 digests (`hand_timeline_head`, `hand_timeline_sha256`, optional
//! `frame_chain_head` / `receipt_sha256`, `chunks`) to a QorTime `/aux/v1/hash-stamp` endpoint,
//! cryptographically verifies the returned Ed25519 stamp before saving `hash_stamp.v1.json`,
//! and never modifies `receipt.json`, `checkpoints.jsonl`, or HID device state.

use crate::evidence;
use crate::hand::check::check_dir;
use ed25519_dalek::{Signature, VerifyingKey};
use serde::Serialize;
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

pub const SCHEMA_REQUEST: &str = "qortime.aux.hash_stamp_request.v1";
pub const SCHEMA_STAMP: &str = "qortime.aux.hash_stamp.v1";
pub const SERVICE_ID: &str = "qortime-aux-hash-stamp";
pub const DOMAIN_STAMP: &str = "QORTIME-AUX-HASH-STAMP-v1";
pub const DOMAIN_STAMP_ID: &str = "QORTIME-AUX-STAMP-ID-v1";
pub const STAMP_FILE: &str = "hash_stamp.v1.json";
pub const CLAIMS: &str = "Auxiliary hash-only timestamp: records when this QorTime service observed these SHA-256 digests. Not a referee match ruling, no gold, and does not establish capture validity or who held the controller.";

const MAX_RESPONSE_BYTES: usize = 64 * 1024;
const HTTP_TIMEOUT_SECS: u64 = 5;
const JCS_MAX: u64 = (1u64 << 53) - 1;
const MAX_CHUNKS: u64 = 10_000_000;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct VerifiedStamp {
    pub stamp_id: String,
    pub key_id: String,
    pub public_key: String,
    pub service: String,
    pub issued_wall_ms: u64,
    pub ledger_seq: u64,
    pub key_trusted: Option<bool>,
    pub out_path: Option<String>,
}

/// Length-prefixed domain framing: `u64_be(len(label)) || label || u64_be(len(data)) || data`.
pub fn frame(label: &str, data: &[u8]) -> Vec<u8> {
    let lb = label.as_bytes();
    let mut out = Vec::with_capacity(16 + lb.len() + data.len());
    out.extend_from_slice(&(lb.len() as u64).to_be_bytes());
    out.extend_from_slice(lb);
    out.extend_from_slice(&(data.len() as u64).to_be_bytes());
    out.extend_from_slice(data);
    out
}

pub fn is_hex(s: &str, len: usize) -> bool {
    s.len() == len && s.bytes().all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}

fn decode_hex<const N: usize>(s: &str, name: &str) -> Result<[u8; N], String> {
    if !is_hex(s, N * 2) {
        return Err(format!("{name} must be {} lowercase hex chars", N * 2));
    }
    let mut out = [0u8; N];
    let b = s.as_bytes();
    for i in 0..N {
        let hi = (b[2 * i] as char).to_digit(16).unwrap() as u8;
        let lo = (b[2 * i + 1] as char).to_digit(16).unwrap() as u8;
        out[i] = (hi << 4) | lo;
    }
    Ok(out)
}

fn safe_u64(v: &Value, name: &str) -> Result<u64, String> {
    let n = v.as_u64().ok_or_else(|| format!("{name} must be a non-negative integer"))?;
    if n > JCS_MAX {
        return Err(format!("{name} exceeds max safe integer"));
    }
    Ok(n)
}

/// Fail-closed verification of a `qortime.aux.hash_stamp.v1` JSON document.
pub fn verify_stamp_json(
    stamp: &Value,
    expected_head: Option<&str>,
    expected_file_sha: Option<&str>,
    expected_chunks: Option<u64>,
    expected_frame_head: Option<&str>,
    expected_receipt_sha: Option<&str>,
    trusted_key: Option<&str>,
) -> Result<VerifiedStamp, String> {
    let top = stamp.as_object().ok_or("stamp must be a JSON object")?;
    let want_top: BTreeSet<&str> = ["public_key", "schema", "sig", "stamp_id", "unsigned_payload"]
        .into_iter()
        .collect();
    let got_top: BTreeSet<&str> = top.keys().map(String::as_str).collect();
    if got_top != want_top {
        return Err("invalid top-level keys in hash stamp".into());
    }
    if top["schema"].as_str() != Some(SCHEMA_STAMP) {
        return Err(format!("stamp schema must be {SCHEMA_STAMP}"));
    }

    let pub_hex = top["public_key"].as_str().ok_or("public_key missing")?;
    let sig_hex = top["sig"].as_str().ok_or("sig missing")?;
    let stamp_id = top["stamp_id"].as_str().ok_or("stamp_id missing")?;
    let pub_bytes: [u8; 32] = decode_hex(pub_hex, "public_key")?;
    let sig_bytes: [u8; 64] = decode_hex(sig_hex, "sig")?;
    if !is_hex(stamp_id, 64) {
        return Err("stamp_id must be 64 lowercase hex chars".into());
    }

    let u_val = &top["unsigned_payload"];
    let u = u_val.as_object().ok_or("unsigned_payload must be an object")?;
    let want_u: BTreeSet<&str> = [
        "alg",
        "claims",
        "client_nonce",
        "digests",
        "issued_wall_ms",
        "key_id",
        "ledger_seq",
        "prev_ledger_sha256",
        "schema",
        "service",
    ]
    .into_iter()
    .collect();
    let got_u: BTreeSet<&str> = u.keys().map(String::as_str).collect();
    if got_u != want_u {
        return Err("invalid unsigned_payload keys in hash stamp".into());
    }

    if u["schema"].as_str() != Some(SCHEMA_STAMP)
        || u["alg"].as_str() != Some("Ed25519")
        || u["service"].as_str() != Some(SERVICE_ID)
    {
        return Err("unsupported hash stamp metadata".into());
    }
    if u["claims"].as_str() != Some(CLAIMS) {
        return Err("unexpected claims text in hash stamp".into());
    }
    let client_nonce = u["client_nonce"].as_str().ok_or("client_nonce missing")?;
    let key_id = u["key_id"].as_str().ok_or("key_id missing")?;
    let prev_ledger = u["prev_ledger_sha256"].as_str().ok_or("prev_ledger_sha256 missing")?;
    if !is_hex(client_nonce, 64) || !is_hex(key_id, 16) || !is_hex(prev_ledger, 64) {
        return Err("invalid hex identifier in unsigned_payload".into());
    }
    let issued_wall_ms = safe_u64(&u["issued_wall_ms"], "issued_wall_ms")?;
    let ledger_seq = safe_u64(&u["ledger_seq"], "ledger_seq")?;

    let want_key_id = &evidence::sha256_hex(&pub_bytes)[..16];
    if key_id != want_key_id {
        return Err("key_id does not match public_key".into());
    }

    let digests = u["digests"].as_object().ok_or("digests must be an object")?;
    let allowed_digests: BTreeSet<&str> = [
        "chunks",
        "frame_chain_head",
        "hand_timeline_head",
        "hand_timeline_sha256",
        "receipt_sha256",
    ]
    .into_iter()
    .collect();
    for k in digests.keys() {
        if !allowed_digests.contains(k.as_str()) {
            return Err(format!("unsupported digests field in hash stamp: {k}"));
        }
    }
    let d_head = digests
        .get("hand_timeline_head")
        .and_then(Value::as_str)
        .filter(|s| is_hex(s, 64))
        .ok_or("digests.hand_timeline_head must be 64 lowercase hex chars")?;
    let d_sha = digests
        .get("hand_timeline_sha256")
        .and_then(Value::as_str)
        .filter(|s| is_hex(s, 64))
        .ok_or("digests.hand_timeline_sha256 must be 64 lowercase hex chars")?;
    let d_frame = match digests.get("frame_chain_head") {
        Some(v) => Some(
            v.as_str()
                .filter(|s| is_hex(s, 64))
                .ok_or("digests.frame_chain_head must be 64 lowercase hex chars")?,
        ),
        None => None,
    };
    let d_receipt = match digests.get("receipt_sha256") {
        Some(v) => Some(
            v.as_str()
                .filter(|s| is_hex(s, 64))
                .ok_or("digests.receipt_sha256 must be 64 lowercase hex chars")?,
        ),
        None => None,
    };
    let d_chunks = match digests.get("chunks") {
        Some(v) => {
            let c = safe_u64(v, "digests.chunks")?;
            if c > MAX_CHUNKS {
                return Err("digests.chunks out of bounds".into());
            }
            Some(c)
        }
        None => None,
    };

    let u_canon = evidence::jcs(u_val)?;
    let recomputed_id = evidence::sha256_hex(&frame(DOMAIN_STAMP_ID, u_canon.as_bytes()));
    if recomputed_id != stamp_id {
        return Err("stamp_id does not match canonical payload".into());
    }

    let vk = VerifyingKey::from_bytes(&pub_bytes).map_err(|_| "invalid Ed25519 public key")?;
    let sig = Signature::from_bytes(&sig_bytes);
    vk.verify_strict(&frame(DOMAIN_STAMP, u_canon.as_bytes()), &sig)
        .map_err(|_| "Ed25519 signature verification failed")?;

    if let Some(want) = expected_head {
        if d_head != want {
            return Err("stamp hand_timeline_head does not match checked session".into());
        }
    }
    if let Some(want) = expected_file_sha {
        if d_sha != want {
            return Err("stamp hand_timeline_sha256 does not match checked file".into());
        }
    }
    if let (Some(want), Some(got)) = (expected_chunks, d_chunks) {
        if got != want {
            return Err("stamp chunks count does not match checked session".into());
        }
    }
    if let (Some(want), Some(got)) = (expected_frame_head, d_frame) {
        if got != want {
            return Err("stamp frame_chain_head does not match session receipt".into());
        }
    }
    if let (Some(want), Some(got)) = (expected_receipt_sha, d_receipt) {
        if got != want {
            return Err("stamp receipt_sha256 does not match session receipt".into());
        }
    }

    let key_trusted = if let Some(pin_raw) = trusted_key {
        let pin = pin_raw.trim().to_ascii_lowercase();
        if !is_hex(&pin, 16) && !is_hex(&pin, 64) {
            return Err("--trusted-key must be a 16-hex key_id or 64-hex public_key".into());
        }
        if pin != key_id && pin != pub_hex {
            return Err(format!(
                "stamp signer key ({key_id}) does not match trusted key ({pin})"
            ));
        }
        Some(true)
    } else {
        None
    };

    Ok(VerifiedStamp {
        stamp_id: stamp_id.to_string(),
        key_id: key_id.to_string(),
        public_key: pub_hex.to_string(),
        service: SERVICE_ID.into(),
        issued_wall_ms,
        ledger_seq,
        key_trusted,
        out_path: None,
    })
}

fn parse_http_url(raw_url: &str) -> Result<(String, u16, String), String> {
    let trimmed = raw_url.trim();
    if trimmed.starts_with("https://") {
        return Err("HTTPS URL not supported directly by standalone qorcap binary; use a local http:// QorTime endpoint".into());
    }
    let rest = trimmed
        .strip_prefix("http://")
        .ok_or_else(|| format!("URL must start with http:// (got `{trimmed}`)"))?;
    let (authority, prefix) = match rest.find('/') {
        Some(idx) => (&rest[..idx], &rest[idx..]),
        None => (rest, ""),
    };
    if authority.is_empty() || authority.contains('@') {
        return Err("invalid HTTP URL authority".into());
    }
    let (host, port) = if let Some(after_bracket) = authority.strip_prefix('[') {
        let end = after_bracket.find(']').ok_or("unclosed IPv6 host bracket")?;
        let h = &after_bracket[..end];
        let rem = &after_bracket[end + 1..];
        let p = if rem.is_empty() {
            80
        } else if let Some(ps) = rem.strip_prefix(':') {
            ps.parse::<u16>().map_err(|_| "invalid TCP port")?
        } else {
            return Err("invalid IPv6 URL authority".into());
        };
        (format!("[{h}]"), p)
    } else if let Some((h, ps)) = authority.rsplit_once(':') {
        let p = ps.parse::<u16>().map_err(|_| "invalid TCP port")?;
        (h.to_string(), p)
    } else {
        (authority.to_string(), 80)
    };
    let clean_prefix = prefix.trim_end_matches('/');
    let path = format!("{clean_prefix}/aux/v1/hash-stamp");
    Ok((host, port, path))
}

fn decode_chunked(raw: &[u8]) -> Result<Vec<u8>, String> {
    let mut out = Vec::new();
    let mut rest = raw;
    loop {
        let Some(pos) = rest.windows(2).position(|w| w == b"\r\n") else {
            return Err("truncated chunked HTTP response".into());
        };
        let size_line = std::str::from_utf8(&rest[..pos])
            .map_err(|_| "non-UTF-8 chunk header")?
            .split(';')
            .next()
            .unwrap_or("")
            .trim();
        let sz = usize::from_str_radix(size_line, 16).map_err(|_| "bad HTTP chunk size")?;
        rest = &rest[pos + 2..];
        if sz == 0 {
            break;
        }
        if rest.len() < sz + 2 || out.len() + sz > MAX_RESPONSE_BYTES {
            return Err("HTTP chunked body truncated or oversized".into());
        }
        out.extend_from_slice(&rest[..sz]);
        rest = &rest[sz + 2..];
    }
    Ok(out)
}

pub fn post_hash_stamp_http(base_url: &str, body_bytes: &[u8]) -> Result<Value, String> {
    let (host, port, path) = parse_http_url(base_url)?;
    let host_unbracketed = host.trim_start_matches('[').trim_end_matches(']');
    let addr = (host_unbracketed, port)
        .to_socket_addrs()
        .map_err(|e| format!("cannot resolve {host}:{port}: {e}"))?
        .next()
        .ok_or_else(|| format!("no address resolved for {host}:{port}"))?;

    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_secs(HTTP_TIMEOUT_SECS))
        .map_err(|e| format!("connect {addr}: {e}"))?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(HTTP_TIMEOUT_SECS)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(HTTP_TIMEOUT_SECS)));

    let host_hdr = if port == 80 { host.clone() } else { format!("{host}:{port}") };
    let head = format!(
        "POST {path} HTTP/1.1\r\nHost: {host_hdr}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body_bytes.len()
    );
    stream
        .write_all(head.as_bytes())
        .and_then(|_| stream.write_all(body_bytes))
        .and_then(|_| stream.flush())
        .map_err(|e| format!("HTTP send failed: {e}"))?;

    let mut reader = BufReader::new(stream);
    let mut status_line = String::new();
    reader
        .read_line(&mut status_line)
        .map_err(|e| format!("HTTP read failed: {e}"))?;
    let status_code: u16 = status_line
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse().ok())
        .ok_or_else(|| format!("malformed HTTP status line: `{}`", status_line.trim()))?;

    let mut content_len: Option<usize> = None;
    let mut chunked = false;
    loop {
        let mut line = String::new();
        let n = reader
            .read_line(&mut line)
            .map_err(|e| format!("HTTP header read failed: {e}"))?;
        if n == 0 || line == "\r\n" || line == "\n" {
            break;
        }
        if let Some((k, v)) = line.split_once(':') {
            let kl = k.trim().to_ascii_lowercase();
            let vl = v.trim();
            if kl == "content-length" {
                content_len = vl.parse::<usize>().ok();
            } else if kl == "transfer-encoding" && vl.to_ascii_lowercase().contains("chunked") {
                chunked = true;
            }
        }
    }

    let raw_body = if let Some(len) = content_len {
        if len > MAX_RESPONSE_BYTES {
            return Err(format!("HTTP response exceeds {MAX_RESPONSE_BYTES} bytes"));
        }
        let mut buf = vec![0u8; len];
        reader
            .read_exact(&mut buf)
            .map_err(|e| format!("HTTP body read failed: {e}"))?;
        buf
    } else {
        let mut buf = Vec::new();
        reader
            .take((MAX_RESPONSE_BYTES + 1) as u64)
            .read_to_end(&mut buf)
            .map_err(|e| format!("HTTP body read failed: {e}"))?;
        if buf.len() > MAX_RESPONSE_BYTES {
            return Err(format!("HTTP response exceeds {MAX_RESPONSE_BYTES} bytes"));
        }
        if chunked {
            decode_chunked(&buf)?
        } else {
            buf
        }
    };

    if status_code != 200 {
        let snippet = String::from_utf8_lossy(&raw_body);
        return Err(format!("QorTime endpoint returned HTTP {status_code}: {}", snippet.trim()));
    }
    serde_json::from_slice(&raw_body).map_err(|e| format!("invalid JSON from QorTime: {e}"))
}

fn generate_client_nonce(head: &str, file_sha: &str) -> String {
    let wall_ns = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let seed = format!("QORCAP-STAMP-NONCE:{}:{wall_ns}:{head}:{file_sha}", std::process::id());
    evidence::sha256_hex(seed.as_bytes())
}

fn read_optional_session_bindings(dir: &Path) -> (Option<String>, Option<String>) {
    let frame_head = std::fs::read_to_string(dir.join("receipt.json"))
        .ok()
        .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        .and_then(|v| v["frame_clock"]["chain_head"].as_str().map(str::to_string))
        .filter(|s| is_hex(s, 64));
    let receipt_sha = std::fs::read_to_string(dir.join("receipt.sha256"))
        .ok()
        .and_then(|t| t.split_whitespace().next().map(str::to_string))
        .filter(|s| is_hex(s, 64));
    (frame_head, receipt_sha)
}

pub fn run_stamp(
    dir: &Path,
    url: Option<&str>,
    trusted_key: Option<&str>,
    out_override: Option<&Path>,
    force: bool,
    verify_only: bool,
    client_nonce_override: Option<&str>,
) -> Result<VerifiedStamp, String> {
    let rep = check_dir(dir)?;
    if !rep.pass() {
        return Err(format!("hand timeline check failed: {}", rep.errors.join("; ")));
    }
    if rep.off || rep.chunks == 0 {
        return Err("cannot hash-stamp an empty or off hand timeline (chunks == 0)".into());
    }
    let (frame_head, receipt_sha) = read_optional_session_bindings(dir);

    if verify_only {
        let stamp_path = out_override
            .map(PathBuf::from)
            .or_else(|| {
                let direct = dir.join(STAMP_FILE);
                if direct.exists() {
                    Some(direct)
                } else {
                    rep.hand_dir.as_ref().map(|hd| hd.join(STAMP_FILE))
                }
            })
            .unwrap_or_else(|| dir.join(STAMP_FILE));
        let raw = std::fs::read_to_string(&stamp_path)
            .map_err(|e| format!("cannot read {}: {e}", stamp_path.display()))?;
        let val: Value = serde_json::from_str(&raw)
            .map_err(|e| format!("{}: not valid JSON ({e})", stamp_path.display()))?;
        let mut res = verify_stamp_json(
            &val,
            Some(&rep.head),
            Some(&rep.file_sha256),
            Some(rep.chunks),
            frame_head.as_deref(),
            receipt_sha.as_deref(),
            trusted_key,
        )?;
        res.out_path = Some(stamp_path.to_string_lossy().into_owned());
        return Ok(res);
    }

    let endpoint = url.ok_or("--url <QorTime HTTP URL> is required unless --verify-only is set")?;
    let out_path = out_override
        .map(PathBuf::from)
        .unwrap_or_else(|| dir.join(STAMP_FILE));
    if out_path.exists() && !force {
        return Err(format!(
            "{} already exists (pass --force to overwrite)",
            out_path.display()
        ));
    }

    let nonce = client_nonce_override
        .map(str::to_string)
        .unwrap_or_else(|| generate_client_nonce(&rep.head, &rep.file_sha256));
    if !is_hex(&nonce, 64) {
        return Err("client_nonce must be 64 lowercase hex chars".into());
    }

    let mut digests = json!({
        "chunks": rep.chunks,
        "hand_timeline_head": rep.head,
        "hand_timeline_sha256": rep.file_sha256,
    });
    if let Some(fh) = &frame_head {
        digests["frame_chain_head"] = json!(fh);
    }
    if let Some(rs) = &receipt_sha {
        digests["receipt_sha256"] = json!(rs);
    }
    let req = json!({
        "schema": SCHEMA_REQUEST,
        "client_nonce": nonce,
        "digests": digests,
    });
    let req_bytes = evidence::jcs(&req)?;
    let resp_val = post_hash_stamp_http(endpoint, req_bytes.as_bytes())?;

    let mut verified = verify_stamp_json(
        &resp_val,
        Some(&rep.head),
        Some(&rep.file_sha256),
        Some(rep.chunks),
        frame_head.as_deref(),
        receipt_sha.as_deref(),
        trusted_key,
    )?;

    if resp_val["unsigned_payload"]["client_nonce"].as_str() != Some(nonce.as_str()) {
        return Err("QorTime stamp client_nonce mismatch".into());
    }

    if let Some(parent) = out_path.parent().filter(|p| !p.as_os_str().is_empty()) {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("cannot create {}: {e}", parent.display()))?;
    }
    let formatted = evidence::jcs(&resp_val)? + "\n";
    std::fs::write(&out_path, formatted.as_bytes())
        .map_err(|e| format!("cannot write {}: {e}", out_path.display()))?;
    verified.out_path = Some(out_path.to_string_lossy().into_owned());
    Ok(verified)
}
