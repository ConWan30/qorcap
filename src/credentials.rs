//! Content Credentials (C2PA) for a clip cut from a QorCap match.
//!
//! The signed receipt is the source of truth. This module only embeds a manifest that
//! points at that receipt by hash. It does not decide that a result is verified: the caller
//! passes the status the existing verifier already returned.
//!
//! Signing uses a local ES256 key QorCap generates under the user's app data. That key is
//! not the receipt key. Receipts are Ed25519 over JCS (`device.key` / `referee.key`); C2PA
//! wants an X.509 certificate. Reusing the receipt private key would give one file two jobs.
//! The clip is tied to the receipt by `receipt_sha256`, which anyone can recompute.
//!
//! Offline: no timestamp authority, no OCSP, no remote manifest fetch. The crate is built
//! with `rust_native_crypto` and without its default OpenSSL and HTTP features.
use crate::evidence;
use c2pa::assertions::{c2pa_action, Action, Actions, SoftwareAgent};
use c2pa::{Builder, BuilderIntent, Context, Ingredient, Reader, Settings, SigningAlg, ValidationState};
use rcgen::{CertificateParams, DistinguishedName, DnType, ExtendedKeyUsagePurpose, IsCa, KeyPair, KeyUsagePurpose, PKCS_ECDSA_P256_SHA256};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::io::{Cursor, Write};
use std::path::{Path, PathBuf};

pub const ENV: &str = "QORCAP_CONTENT_CREDENTIALS";
/// C2PA assertion label. A dotted `.v0` suffix is not used: c2pa-rs treats `.vN` as an
/// assertion-version suffix and omits it from the published label when N is below 2, so
/// `org.qorcap.match-receipt.v0` would be stored as the unversioned name `org.qorcap.match-receipt`.
/// The hyphen keeps the version visible to c2patool and contentcredentials.org/verify.
pub const LABEL: &str = "org.qorcap.match-receipt-v0";
pub const SCHEMA: &str = "org.qorcap.match-receipt-v0";
const CERT_NAME: &str = "c2pa-es256.cert.pem";
const KEY_NAME: &str = "c2pa-es256.key.pem";

/// `cli` is `--content-credentials`. `env_val` is `QORCAP_CONTENT_CREDENTIALS`
/// (same words as `QORCAP_FRAME_CLOCK`).
pub fn flag_enabled(cli: bool, env_val: Option<&str>) -> bool {
    cli || matches!(env_val.map(str::trim), Some("1" | "true" | "yes" | "on"))
}

/// Same directory as QorCap's other local keys (`%LOCALAPPDATA%\QorCap\keys`, or `~/.qorcap/keys`).
pub fn default_key_dir() -> PathBuf {
    if let Ok(h) = std::env::var("QORCAP_HOME") {
        return PathBuf::from(h).join("keys");
    }
    if cfg!(windows) {
        if let Ok(d) = std::env::var("LOCALAPPDATA") {
            return PathBuf::from(d).join("QorCap").join("keys");
        }
    }
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".into());
    PathBuf::from(home).join(".qorcap").join("keys")
}

/// What the clip is allowed to say about the match. Hashes and ids only.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct MatchReceipt {
    pub schema: String,
    pub receipt_sha256: String,
    pub match_id: String,
    pub start_mono_ms: u64,
    pub end_mono_ms: u64,
    pub start_frame: i64,
    pub end_frame: i64,
    pub video_span_sha256: String,
    pub video_segment_seqs: Vec<u64>,
    pub video_chain_head: String,
    pub input_span_sha256: String,
    pub qorcap_version: String,
    /// `verified` | `self_attested` | `partial` | `fail`. Never upgraded from the verifier.
    pub verification_status: String,
    /// The verifier's own word: `VERIFIED`, `SELF_ATTESTED`, `PARTIAL`, or `FAIL`.
    pub verifier_status: String,
    /// True only when `verification_status` is `verified`.
    pub verified: bool,
    /// `trimmed+repackaged` (stream copy) or `trimmed+transcoded`.
    pub edit: String,
}

impl MatchReceipt {
    pub fn consistent(&self) -> Result<(), String> {
        if self.schema != SCHEMA {
            return Err(format!("assertion schema is {}, expected {SCHEMA}", self.schema));
        }
        let claim = self.verification_status == "verified";
        if self.verified != claim {
            return Err("assertion verified flag does not match verification_status".into());
        }
        if claim && self.verifier_status != "VERIFIED" {
            return Err("assertion says verified but verifier_status is not VERIFIED".into());
        }
        if !claim && self.verifier_status == "VERIFIED" && self.verification_status != "fail" {
            return Err("verifier_status is VERIFIED but verification_status is not verified".into());
        }
        for (name, hex) in [
            ("receipt_sha256", self.receipt_sha256.as_str()),
            ("video_span_sha256", self.video_span_sha256.as_str()),
            ("video_chain_head", self.video_chain_head.as_str()),
            ("input_span_sha256", self.input_span_sha256.as_str()),
        ] {
            if hex.len() != 64 || !hex.bytes().all(|c| c.is_ascii_hexdigit()) {
                return Err(format!("{name} is not 64 hex characters"));
            }
        }
        if self.match_id.is_empty() {
            return Err("match_id is empty".into());
        }
        if self.end_mono_ms < self.start_mono_ms {
            return Err("end_mono_ms is before start_mono_ms".into());
        }
        Ok(())
    }
}

#[derive(Debug)]
pub struct ReadClip {
    pub signature_ok: bool,
    pub validation_state: String,
    pub status_codes: Vec<String>,
    pub assertion: Option<MatchReceipt>,
    pub assertion_error: Option<String>,
}

pub fn offline_settings() -> Result<Settings, String> {
    // verify_trust stays false so a self-signed cert is not treated as a broken signature.
    // Callers still have to say the signer is untrusted. Nothing here contacts a network.
    Settings::new()
        .with_json(r#"{"verify":{"verify_after_sign":true,"verify_after_reading":true,"verify_trust":false,"ocsp_fetch":false,"remote_manifest_fetch":false}}"#)
        .map_err(|e| format!("c2pa settings: {e}"))
}

fn offline_context() -> Result<Context, String> {
    Context::new().with_settings(offline_settings()?).map_err(|e| format!("c2pa context: {e}"))
}

/// Load the local signing key, or create one. Files are the certificate and the private key.
pub fn load_or_create_key(dir: &Path) -> Result<(String, String), String> {
    let cert_path = dir.join(CERT_NAME);
    let key_path = dir.join(KEY_NAME);
    if cert_path.is_file() && key_path.is_file() {
        let cert = std::fs::read_to_string(&cert_path).map_err(|e| format!("read {}: {e}", cert_path.display()))?;
        let key = std::fs::read_to_string(&key_path).map_err(|e| format!("read {}: {e}", key_path.display()))?;
        if cert.contains("BEGIN CERTIFICATE") && key.contains("BEGIN") {
            return Ok((cert, key));
        }
        return Err(format!("C2PA key in {} is not a PEM certificate and key; refusing to overwrite it", dir.display()));
    }
    if cert_path.exists() || key_path.exists() {
        return Err(format!("C2PA key in {} is incomplete (need both {CERT_NAME} and {KEY_NAME})", dir.display()));
    }
    std::fs::create_dir_all(dir).map_err(|e| format!("create {}: {e}", dir.display()))?;
    let pair = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).map_err(|e| format!("generate key: {e}"))?;
    let mut params = CertificateParams::default();
    let mut dn = DistinguishedName::new();
    dn.push(DnType::CommonName, "QorCap self-signed content credentials");
    dn.push(DnType::OrganizationName, "QorCap");
    params.distinguished_name = dn;
    params.is_ca = IsCa::ExplicitNoCa;
    params.key_usages = vec![KeyUsagePurpose::DigitalSignature];
    // C2PA's certificate profile requires an end-entity EKU it recognizes (email protection
    // is the one that does not imply a timestamp or OCSP responder) and an authority key id.
    // The cert stays ExplicitNoCa, so it is an end-entity key, not a CA.
    params.extended_key_usages = vec![ExtendedKeyUsagePurpose::EmailProtection];
    params.use_authority_key_identifier_extension = true;
    // rcgen's default window already runs 1975–4096, which is what we want for a local key
    // that must still validate a clip next season. Do not shrink it.
    let cert = params.self_signed(&pair).map_err(|e| format!("self-sign certificate: {e}"))?;
    let cert_pem = cert.pem();
    let key_pem = pair.serialize_pem();
    write_private(&cert_path, cert_pem.as_bytes())?;
    write_private(&key_path, key_pem.as_bytes())?;
    Ok((cert_pem, key_pem))
}

fn write_private(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let mut f = std::fs::File::create(path).map_err(|e| format!("create {}: {e}", path.display()))?;
    f.write_all(bytes).map_err(|e| format!("write {}: {e}", path.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
    }
    Ok(())
}

/// `None` leaves `plain` unchanged. `Some` signs a new MP4 that embeds the manifest.
pub fn apply_credentials(plain: &[u8], creds: Option<SignRequest<'_>>) -> Result<Vec<u8>, String> {
    match creds {
        None => Ok(plain.to_vec()),
        Some(req) => sign_mp4(plain, req),
    }
}

pub struct SignRequest<'a> {
    pub assertion: &'a MatchReceipt,
    pub key_dir: &'a Path,
    pub transcoded: bool,
}

pub fn sign_mp4(plain: &[u8], req: SignRequest<'_>) -> Result<Vec<u8>, String> {
    req.assertion.consistent()?;
    if req.assertion.verification_status == "fail" {
        return Err("refusing to attach content credentials: verification_status is fail".into());
    }
    let (cert, key) = load_or_create_key(req.key_dir)?;
    // tsa_url is None: no timestamp authority, no network.
    let signer = c2pa::create_signer::from_keys(cert.as_bytes(), key.as_bytes(), SigningAlg::Es256, None)
        .map_err(|e| format!("c2pa signer: {e}"))?;
    let mut builder = Builder::from_context(offline_context()?).with_definition(json!({
        "claim_generator": format!("QorCap/{}", env!("CARGO_PKG_VERSION")),
        "title": "QorCap clip",
    })).map_err(|e| format!("c2pa builder: {e}"))?;
    let mut who = c2pa::ClaimGeneratorInfo::new("QorCap");
    who.set_version(env!("CARGO_PKG_VERSION"));
    builder.set_claim_generator_info(who);
    builder.set_intent(BuilderIntent::Edit);

    let mut ingredient = Ingredient::from_json(&json!({
        "title": "qorcap match video span",
        "format": "video/mp4",
        "relationship": "parentOf",
        "description": format!(
            "Source segment bytes are not embedded. SHA-256 {} is video_span_sha256 in {LABEL}.",
            req.assertion.video_span_sha256
        ),
    }).to_string()).map_err(|e| format!("ingredient: {e}"))?;
    ingredient.set_hash(&req.assertion.video_span_sha256);
    builder.add_ingredient(ingredient);

    let edited = Action::new(c2pa_action::EDITED)
        .set_description("Trimmed to a match-clock range. The range and source hashes are in org.qorcap.match-receipt-v0.")
        .set_software_agent(SoftwareAgent::String(format!("QorCap/{}", env!("CARGO_PKG_VERSION"))));
    let second_label = if req.transcoded { c2pa_action::TRANSCODED } else { c2pa_action::REPACKAGED };
    let second_desc = if req.transcoded {
        "Re-encoded so the trimmed range is a plain MP4."
    } else {
        "Remuxed to a plain MP4. The picture was stream-copied, then the container was repackaged."
    };
    let second = Action::new(second_label).set_description(second_desc);
    let actions = Actions::new().add_action(edited).add_action(second);
    builder.add_assertion(Actions::LABEL_VERSIONED, &actions).map_err(|e| format!("actions: {e}"))?;
    // JSON, not CBOR: c2patool then shows video_segment_seqs as a number list.
    // CBOR encoded that list as a byte string (`AA==` for `[0]`).
    builder.add_assertion_json(LABEL, req.assertion).map_err(|e| format!("assertion: {e}"))?;

    let mut source = Cursor::new(plain.to_vec());
    let mut dest = Cursor::new(Vec::new());
    builder.sign(signer.as_ref(), "video/mp4", &mut source, &mut dest).map_err(|e| format!("c2pa sign: {e}"))?;
    Ok(dest.into_inner())
}

pub fn read_mp4(bytes: &[u8]) -> Result<ReadClip, String> {
    let mut stream = Cursor::new(bytes);
    let reader = Reader::from_context(offline_context()?)
        .with_stream("video/mp4", &mut stream)
        .map_err(|e| format!("content credentials could not be read: {e}"))?;
    let state = reader.validation_state();
    let signature_ok = matches!(state, ValidationState::Valid | ValidationState::Trusted);
    let status_codes = reader.validation_status().unwrap_or(&[]).iter().map(|s| s.code().to_string()).collect();
    let (assertion, assertion_error) = match reader.active_manifest() {
        None => (None, Some("no active C2PA manifest".into())),
        Some(m) => match m.find_assertion::<MatchReceipt>(LABEL) {
            Ok(a) => match a.consistent() {
                Ok(()) => (Some(a), None),
                Err(e) => (Some(a), Some(e)),
            },
            Err(e) => (None, Some(format!("manifest has no usable {LABEL} assertion ({e})"))),
        },
    };
    Ok(ReadClip {
        signature_ok,
        validation_state: format!("{state:?}"),
        status_codes,
        assertion,
        assertion_error,
    })
}

/// SHA-256 of the exact file bytes. This is the hash the clip stores for `receipt.json`.
pub fn file_sha256(path: &Path) -> Result<String, String> {
    let bytes = std::fs::read(path).map_err(|e| format!("read {}: {e}", path.display()))?;
    Ok(evidence::sha256_hex(&bytes))
}

pub fn sha256_json(v: &Value) -> Result<String, String> {
    evidence::try_jcs_sha(v).map_err(|e| format!("JCS: {e}"))
}

/// Flip one byte of the media (or, if that is not covered, the first byte that breaks validation).
/// The C2PA box itself is excluded from the BMFF hash, so a change inside `ftyp` or the manifest
/// box is the wrong place to prove tamper detection.
#[cfg(test)]
pub(crate) fn tamper_media_byte(signed: &[u8]) -> Vec<u8> {
    let mut offsets = Vec::new();
    let mut i = 0usize;
    while i + 8 <= signed.len() {
        let size = u32::from_be_bytes([signed[i], signed[i + 1], signed[i + 2], signed[i + 3]]) as usize;
        if size < 8 || i + size > signed.len() {
            break;
        }
        let typ = &signed[i + 4..i + 8];
        if typ == b"mdat" || typ == b"moov" {
            let at = (i + 16).min(signed.len() - 1);
            offsets.push(at);
        }
        i += size;
    }
    if signed.len() > 64 {
        offsets.push(signed.len() / 2);
    }
    for off in offsets {
        let mut bad = signed.to_vec();
        bad[off] ^= 0xff;
        if tamper_detected(&bad) {
            return bad;
        }
    }
    for off in (0..signed.len()).step_by(32) {
        let mut bad = signed.to_vec();
        bad[off] ^= 0xff;
        if tamper_detected(&bad) {
            return bad;
        }
    }
    panic!("no single-byte change broke the C2PA signature");
}

#[cfg(test)]
fn tamper_detected(bad: &[u8]) -> bool {
    match read_mp4(bad) {
        Err(_) => true,
        Ok(r) => !r.signature_ok,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn flag_words_match_the_frame_clock() {
        assert!(!flag_enabled(false, None));
        assert!(!flag_enabled(false, Some("")));
        assert!(!flag_enabled(false, Some("0")));
        assert!(!flag_enabled(false, Some("off")));
        for w in ["1", "true", "yes", "on", " true "] {
            assert!(flag_enabled(false, Some(w)), "{w}");
        }
        assert!(flag_enabled(true, None));
    }

    #[test]
    fn flag_off_clip_matches_the_plain_cut_byte_for_byte() {
        let plain = include_bytes!("../tests/fixtures/tiny_clip.mp4");
        let out = apply_credentials(plain, None).unwrap();
        assert_eq!(out, plain);
        assert!(!contains(&out, LABEL.as_bytes()));
        assert!(!contains(&out, b"c2pa"));
    }

    #[test]
    fn signed_clip_round_trips_the_assertion_and_a_tampered_clip_does_not() {
        let plain = include_bytes!("../tests/fixtures/tiny_clip.mp4");
        let dir = std::env::temp_dir().join(format!("qorcap-c2pa-key-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let assertion = sample(false);
        let signed = apply_credentials(plain, Some(SignRequest { assertion: &assertion, key_dir: &dir, transcoded: false })).unwrap();
        assert_ne!(signed, plain);
        assert!(contains(&signed, LABEL.as_bytes()));
        assert!(!contains(&signed, b"local:player"));
        let read = read_mp4(&signed).unwrap();
        assert!(read.signature_ok, "state {} codes {:?}", read.validation_state, read.status_codes);
        assert!(read.assertion.is_some(), "{:?}", read.assertion_error);
        assert_eq!(read.assertion.unwrap(), assertion);
        let bad = tamper_media_byte(&signed);
        let tampered = read_mp4(&bad);
        match tampered {
            Err(e) => assert!(e.contains("could not be read") || e.contains("signature") || e.contains("c2pa") || e.contains("content credentials"), "{e}"),
            Ok(r) => assert!(!r.signature_ok, "tampered clip still validated: {} {:?}", r.validation_state, r.status_codes),
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn verified_flag_must_match_the_status() {
        let mut a = sample(false);
        a.verified = true;
        assert!(a.consistent().is_err());
        a.verification_status = "verified".into();
        assert!(a.consistent().is_err(), "VERIFIED status with a SELF_ATTESTED verifier word");
        a.verifier_status = "VERIFIED".into();
        assert!(a.consistent().is_ok());
    }

    fn sample(verified: bool) -> MatchReceipt {
        MatchReceipt {
            schema: SCHEMA.into(),
            receipt_sha256: "ab".repeat(32),
            match_id: "m-test-1".into(),
            start_mono_ms: 0,
            end_mono_ms: 1000,
            start_frame: 0,
            end_frame: 30,
            video_span_sha256: "cd".repeat(32),
            video_segment_seqs: vec![0],
            video_chain_head: "ef".repeat(32),
            input_span_sha256: "11".repeat(32),
            qorcap_version: env!("CARGO_PKG_VERSION").into(),
            verification_status: if verified { "verified" } else { "self_attested" }.into(),
            verifier_status: if verified { "VERIFIED" } else { "SELF_ATTESTED" }.into(),
            verified,
            edit: "trimmed+repackaged".into(),
        }
    }

    fn contains(hay: &[u8], needle: &[u8]) -> bool {
        hay.windows(needle.len()).any(|w| w == needle)
    }
}
