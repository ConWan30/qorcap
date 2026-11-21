//! Canonical bytes and domain-separated digests (design section 3).
//!
//! `F(label, bytes)` = u64-BE length of the UTF-8 label, the label, u64-BE length of the bytes,
//! the bytes. `H(label, obj)` = SHA-256(F(label, JCS(obj))). Signatures cover exactly
//! `F(domain, JCS(unsigned_payload))`. Inputs are accepted only when they already are their own
//! RFC 8785 encoding, which rejects duplicate keys, reordered keys, whitespace and alternate
//! escapes. JSON numbers are not allowed anywhere: integers travel as bounded decimal strings.

use super::Reason;
use crate::evidence;
use base64::Engine;
use serde::{de::DeserializeOwned, Serialize};
use serde_json::Value;

pub const MAX_MESSAGE_BYTES: usize = 64 * 1024;
pub const ZERO_HEAD: &str = evidence::GENESIS;

pub fn frame(label: &str, bytes: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(16 + label.len() + bytes.len());
    out.extend_from_slice(&(label.len() as u64).to_be_bytes());
    out.extend_from_slice(label.as_bytes());
    out.extend_from_slice(&(bytes.len() as u64).to_be_bytes());
    out.extend_from_slice(bytes);
    out
}

fn no_numbers(v: &Value) -> Result<(), Reason> {
    match v {
        Value::Number(_) => Err(Reason::NumberNotAllowed),
        Value::Array(a) => a.iter().try_for_each(no_numbers),
        Value::Object(o) => o.values().try_for_each(no_numbers),
        _ => Ok(()),
    }
}

pub fn canonical_bytes<T: Serialize>(v: &T) -> Result<Vec<u8>, Reason> {
    let val = serde_json::to_value(v).map_err(|_| Reason::Malformed)?;
    no_numbers(&val)?;
    let out = evidence::jcs(&val).map(String::into_bytes).map_err(|_| Reason::NonCanonical)?;
    if out.len() > MAX_MESSAGE_BYTES { return Err(Reason::Oversized); }
    Ok(out)
}

/// `H(label, obj)` as lowercase hex.
pub fn digest<T: Serialize>(label: &str, v: &T) -> Result<String, Reason> {
    Ok(evidence::sha256_hex(&frame(label, &canonical_bytes(v)?)))
}

/// The exact bytes an Ed25519 signature covers.
pub fn signing_message<T: Serialize>(domain: &str, v: &T) -> Result<Vec<u8>, Reason> {
    Ok(frame(domain, &canonical_bytes(v)?))
}

/// Strict parse: size bound, UTF-8, no numbers, the bytes must equal their own JCS encoding,
/// the typed schema must accept it (unknown fields rejected) and re-encode to the same bytes
/// (so an explicit `null` for an absent optional field is rejected too).
pub fn parse_canonical<T: DeserializeOwned + Serialize>(bytes: &[u8]) -> Result<T, Reason> {
    if bytes.len() > MAX_MESSAGE_BYTES { return Err(Reason::Oversized); }
    let text = std::str::from_utf8(bytes).map_err(|_| Reason::Malformed)?;
    let val: Value = serde_json::from_str(text).map_err(|_| Reason::Malformed)?;
    no_numbers(&val)?;
    let canon = evidence::jcs(&val).map_err(|_| Reason::NonCanonical)?;
    if canon.as_bytes() != bytes { return Err(Reason::NonCanonical); }
    let typed: T = serde_json::from_value(val).map_err(|_| Reason::SchemaViolation)?;
    if canonical_bytes(&typed)? != bytes { return Err(Reason::NonCanonical); }
    Ok(typed)
}

pub fn is_hex_len(s: &str, len: usize) -> bool {
    s.len() == len && s.bytes().all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}
/// 256-bit digests and nonces.
pub fn is_hex64(s: &str) -> bool { is_hex_len(s, 64) }
/// 128-bit random identifiers (session, epochs, boot ids, chain ids).
pub fn is_id(s: &str) -> bool { is_hex_len(s, 32) }

/// Unsigned decimal string, no sign, no leading zeros, fits u64.
pub fn dec_u64(s: &str) -> Option<u64> {
    if s.is_empty() || s.len() > 20 || !s.bytes().all(|c| c.is_ascii_digit()) { return None; }
    if s.len() > 1 && s.starts_with('0') { return None; }
    s.parse().ok()
}
/// Signed decimal string (Unix wall microseconds), no `+`, no `-0`, no leading zeros.
pub fn dec_i64(s: &str) -> Option<i64> {
    match s.strip_prefix('-') {
        Some(rest) => { let m = dec_u64(rest)?; if m == 0 { return None; } i64::try_from(m).ok().map(|x| -x) }
        None => dec_u64(s).and_then(|m| i64::try_from(m).ok()),
    }
}
pub fn dec(n: u64) -> String { n.to_string() }

pub fn b64url(b: &[u8]) -> String { base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(b) }
/// Unpadded base64url; the decoder rejects padding and non-zero trailing bits, so only the
/// canonical encoding of a value decodes.
pub fn un_b64url(s: &str) -> Option<Vec<u8>> {
    let out = base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(s).ok()?;
    (b64url(&out) == s).then_some(out)
}

pub fn ensure(cond: bool, r: Reason) -> Result<(), Reason> { if cond { Ok(()) } else { Err(r) } }
