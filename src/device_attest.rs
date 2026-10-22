//! Opt-in device key. The signature is a sidecar, `device_attest.v0.json`.
//!
//! `receipt.json` is not modified. This module does not open a gamepad and does not send
//! output or feature reports.
//!
//! The signed message is the UTF-8 prefix [`SIGNED_PREFIX`] plus the 64 lowercase hex
//! characters of one checkpoint hash. See `docs/DEVICE_ATTEST.md`.

use base64::Engine;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::path::{Path, PathBuf};
#[cfg(windows)]
use sha2::{Digest, Sha256};
use ::ring::signature::KeyPair;

pub const SCHEMA: &str = "qorcap-device-attest/v0";
pub const KEY_SCHEMA: &str = "qorcap-device-key/v0";
pub const STATUS_SCHEMA: &str = "qorcap-device-key-status/v0";
/// UTF-8 prefix of the signed message, including the trailing newline (0x0A). 24 bytes.
pub const SIGNED_PREFIX: &str = "qorcap-device-attest/v0\n";
pub const PROVIDER_TPM: &str = "Microsoft Platform Crypto Provider";
pub const PROVIDER_SOFTWARE: &str = "software";
pub const ALG_P256: &str = "ECDSA_P256";
pub const ALG_RSA: &str = "RSA_2048";
pub const TPM_KEY_NAME: &str = "QorCap-Device-Attest-v0";
pub const SIDECAR_NAME: &str = "device_attest.v0.json";
const SOFTWARE_FILE: &str = "device-attest-v0.p256.pkcs8";
const KEY_RECORD: &str = "device-attest-v0.key.json";

#[cfg_attr(not(windows), allow(dead_code))]
const EXPORT_BITS: u32 = 0x1 | 0x2 | 0x4 | 0x8;

pub fn default_key_dir() -> PathBuf { crate::credentials::default_key_dir() }

/// `qorcap-device-attest/v0\n` + 64 lowercase hex characters. 88 bytes, no trailing newline.
pub fn signing_bytes(chain_head_hex: &str) -> Result<Vec<u8>, String> {
    hex64(chain_head_hex)?;
    let mut m = SIGNED_PREFIX.as_bytes().to_vec();
    m.extend_from_slice(chain_head_hex.as_bytes());
    Ok(m)
}

/// Last checkpoint `hash` in the only non-empty `checkpoints.home` or `checkpoints.away` array.
pub fn chain_head_of(receipt: &Value) -> Result<String, String> {
    let cps = receipt.get("checkpoints").and_then(|c| c.as_object())
        .ok_or("receipt has no checkpoints object")?;
    let mut found = vec![];
    for role in ["home", "away"] {
        if let Some(arr) = cps.get(role).and_then(|v| v.as_array()) {
            if !arr.is_empty() { found.push((role, arr)); }
        }
    }
    match found.as_slice() {
        [(role, arr)] => {
            let hash = arr.last().and_then(|c| c.get("hash")).and_then(|h| h.as_str())
                .ok_or_else(|| format!("checkpoints.{role} last entry has no hash"))?;
            hex64(hash)?;
            Ok(hash.to_string())
        }
        [] => Err("receipt has no checkpoint chain head (home and away are both empty)".into()),
        _ => Err("receipt has checkpoint chains for both home and away; device attestation signs one chain head and will not choose".into()),
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct KeyRecord {
    pub schema: String,
    pub active: String,
    pub key_provider: String,
    pub algorithm: String,
    pub public_key_spki_der_b64: String,
    pub non_exportable: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tpm_key_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fallback: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TpmBlock {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub key_attestation_b64: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub key_attestation_claim: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ek_cert_b64: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ek_cert_property: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub platform_type: Option<String>,
}

impl TpmBlock {
    fn empty(&self) -> bool {
        self.key_attestation_b64.is_none() && self.ek_cert_b64.is_none() && self.platform_type.is_none()
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Sidecar {
    pub schema: String,
    pub key_provider: String,
    pub algorithm: String,
    pub public_key_spki_der_b64: String,
    pub non_exportable: Option<bool>,
    pub chain_head: String,
    pub signature_b64: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tpm: Option<TpmBlock>,
}

#[derive(Clone, Debug, Serialize)]
pub struct InitOutcome {
    pub key: KeyRecord,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sidecar: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub receipt_unchanged: Option<bool>,
}

#[derive(Clone, Debug, Serialize)]
pub struct VerifyReport {
    pub signature_ok: bool,
    pub chain_head: String,
    pub algorithm: String,
    pub key_provider: String,
    /// Value stored in the sidecar. The signature does not cover it.
    pub non_exportable_in_sidecar: Option<bool>,
    /// Re-read from the local TPM key when that key's public key matches the sidecar. Otherwise null.
    pub non_exportable_live: Option<bool>,
    /// `absent`, `present_unverified`, or `verified`.
    pub tpm_attestation: String,
    pub detail: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct Status {
    pub schema: String,
    pub tpm_provider_opens: bool,
    pub key: Option<KeyRecord>,
    pub sidecar: Option<VerifyReport>,
}

impl Status {
    pub fn exit_code(&self, bundle_requested: bool) -> i32 {
        if bundle_requested {
            return match &self.sidecar { Some(s) if s.signature_ok => 0, _ => 1 };
        }
        if self.key.is_none() { 1 } else { 0 }
    }
}

pub fn init(key_dir: &Path, software_only: bool, bundle: Option<&Path>) -> Result<InitOutcome, String> {
    let key = ensure_key(key_dir, software_only)?;
    let mut out = InitOutcome { key, sidecar: None, receipt_unchanged: None };
    if let Some(dir) = bundle {
        let name = write_sidecar(key_dir, dir)?;
        out.sidecar = Some(name);
        out.receipt_unchanged = Some(true);
    }
    Ok(out)
}

pub fn status(key_dir: &Path, bundle: Option<&Path>) -> Result<Status, String> {
    let key = if key_dir.join(KEY_RECORD).is_file() { Some(read_key(key_dir)?) } else { None };
    let sidecar = match bundle {
        Some(dir) => Some(verify_dir(key_dir, dir)?),
        None => None,
    };
    Ok(Status { schema: STATUS_SCHEMA.into(), tpm_provider_opens: tpm_provider_opens(), key, sidecar })
}

fn ensure_key(key_dir: &Path, software_only: bool) -> Result<KeyRecord, String> {
    if software_only {
        let rec = software_record(key_dir, None)?;
        write_json(&key_dir.join(KEY_RECORD), &rec)?;
        return Ok(rec);
    }
    if tpm_provider_opens() {
        match tpm_record() {
            Ok(rec) => {
                write_json(&key_dir.join(KEY_RECORD), &rec)?;
                return Ok(rec);
            }
            Err(e) => {
                let rec = software_record(key_dir, Some(e))?;
                write_json(&key_dir.join(KEY_RECORD), &rec)?;
                return Ok(rec);
            }
        }
    }
    let why = if cfg!(windows) {
        "Microsoft Platform Crypto Provider did not open".to_string()
    } else {
        "Microsoft Platform Crypto Provider is a Windows CNG provider".to_string()
    };
    let rec = software_record(key_dir, Some(why))?;
    write_json(&key_dir.join(KEY_RECORD), &rec)?;
    Ok(rec)
}

fn software_record(key_dir: &Path, fallback: Option<String>) -> Result<KeyRecord, String> {
    std::fs::create_dir_all(key_dir).map_err(|e| format!("create {}: {e}", key_dir.display()))?;
    let path = key_dir.join(SOFTWARE_FILE);
    let pkcs8 = if path.is_file() {
        std::fs::read(&path).map_err(|e| format!("read {}: {e}", path.display()))?
    } else {
        let (pkcs8, _) = generate_p256()?;
        write_private(&path, &pkcs8)?;
        pkcs8
    };
    let spki = spki_from_pkcs8(&pkcs8)?;
    Ok(KeyRecord {
        schema: KEY_SCHEMA.into(),
        active: "software".into(),
        key_provider: PROVIDER_SOFTWARE.into(),
        algorithm: ALG_P256.into(),
        public_key_spki_der_b64: b64(&spki),
        non_exportable: Some(false),
        tpm_key_name: None,
        fallback,
    })
}

fn write_sidecar(key_dir: &Path, bundle: &Path) -> Result<String, String> {
    let receipt_path = bundle.join("receipt.json");
    let before = std::fs::read(&receipt_path).map_err(|e| format!("read {}: {e}", receipt_path.display()))?;
    let receipt: Value = serde_json::from_slice(&before).map_err(|e| format!("receipt.json: {e}"))?;
    let chain_head = chain_head_of(&receipt)?;
    let message = signing_bytes(&chain_head)?;
    let key = read_key(key_dir)?;
    let (sig, tpm) = sign_with(&key, key_dir, &message)?;
    let side = Sidecar {
        schema: SCHEMA.into(),
        key_provider: key.key_provider,
        algorithm: key.algorithm,
        public_key_spki_der_b64: key.public_key_spki_der_b64,
        non_exportable: key.non_exportable,
        chain_head,
        signature_b64: b64(&sig),
        tpm: tpm.filter(|t| !t.empty()),
    };
    let path = bundle.join(SIDECAR_NAME);
    write_json(&path, &side)?;
    let after = std::fs::read(&receipt_path).map_err(|e| format!("re-read {}: {e}", receipt_path.display()))?;
    if before != after {
        let _ = std::fs::remove_file(&path);
        return Err("receipt.json changed while writing device_attest.v0.json".into());
    }
    Ok(SIDECAR_NAME.into())
}

fn verify_dir(key_dir: &Path, bundle: &Path) -> Result<VerifyReport, String> {
    let receipt_path = bundle.join("receipt.json");
    let side_path = bundle.join(SIDECAR_NAME);
    if !side_path.is_file() {
        return Ok(VerifyReport {
            signature_ok: false, chain_head: String::new(), algorithm: String::new(),
            key_provider: String::new(), non_exportable_in_sidecar: None, non_exportable_live: None,
            tpm_attestation: "absent".into(),
            detail: format!("{} is missing", side_path.display()),
        });
    }
    let receipt: Value = serde_json::from_slice(&std::fs::read(&receipt_path).map_err(|e| format!("read {}: {e}", receipt_path.display()))?)
        .map_err(|e| format!("receipt.json: {e}"))?;
    let expected = chain_head_of(&receipt)?;
    let raw = std::fs::read_to_string(&side_path).map_err(|e| format!("read {}: {e}", side_path.display()))?;
    let side: Sidecar = serde_json::from_str(&raw).map_err(|e| format!("device_attest.v0.json: {e}"))?;
    check_sidecar(&side)?;
    let mut detail = String::new();
    let mut ok = true;
    if side.chain_head != expected {
        ok = false;
        detail = format!("sidecar chain_head {} does not match the receipt checkpoint hash {expected}", side.chain_head);
    }
    let message = signing_bytes(&side.chain_head)?;
    let spki = b64_dec(&side.public_key_spki_der_b64)?;
    let sig = b64_dec(&side.signature_b64)?;
    if ok {
        if let Err(e) = verify_sig(&side.algorithm, &spki, &message, &sig) {
            ok = false;
            detail = e;
        }
    }
    let (tpm_attestation, live, note) = attest_note(&side, key_dir, &spki);
    if ok {
        detail = if note.is_empty() {
            "signature matches the receipt checkpoint hash".into()
        } else {
            format!("signature matches the receipt checkpoint hash. {note}")
        };
    } else if !note.is_empty() {
        detail = format!("{detail}. {note}");
    }
    Ok(VerifyReport {
        signature_ok: ok, chain_head: side.chain_head, algorithm: side.algorithm,
        key_provider: side.key_provider, non_exportable_in_sidecar: side.non_exportable,
        non_exportable_live: live, tpm_attestation, detail,
    })
}

fn check_sidecar(s: &Sidecar) -> Result<(), String> {
    if s.schema != SCHEMA { return Err(format!("schema is {}, expected {SCHEMA}", s.schema)); }
    if s.key_provider != PROVIDER_TPM && s.key_provider != PROVIDER_SOFTWARE {
        return Err(format!("key_provider {} is not {PROVIDER_TPM} or {PROVIDER_SOFTWARE}", s.key_provider));
    }
    if s.algorithm != ALG_P256 && s.algorithm != ALG_RSA {
        return Err(format!("algorithm {} is not {ALG_P256} or {ALG_RSA}", s.algorithm));
    }
    hex64(&s.chain_head)?;
    if s.public_key_spki_der_b64.is_empty() || s.signature_b64.is_empty() {
        return Err("public key or signature is empty".into());
    }
    if let Some(t) = &s.tpm {
        if let Some(c) = &t.key_attestation_claim {
            if c != "NCRYPT_CLAIM_PLATFORM" && c != "NCRYPT_CLAIM_SUBJECT_ONLY" {
                return Err(format!("unknown key_attestation_claim {c}"));
            }
        }
        if let Some(p) = &t.ek_cert_property {
            if p != "PCP_ECC_EKNVCERT" && p != "PCP_RSA_EKNVCERT" && p != "PCP_EKNVCERT" {
                return Err(format!("unknown ek_cert_property {p}"));
            }
        }
    }
    Ok(())
}

fn attest_note(side: &Sidecar, key_dir: &Path, spki: &[u8]) -> (String, Option<bool>, String) {
    let Some(tpm) = &side.tpm else { return ("absent".into(), None, String::new()) };
    if tpm.key_attestation_b64.is_none() && tpm.ek_cert_b64.is_none() {
        return ("absent".into(), None, String::new());
    }
    if side.key_provider != PROVIDER_TPM {
        return ("present_unverified".into(), None, "key_provider is software, so the TPM fields are not treated as proof".into());
    }
    let claim = match &tpm.key_attestation_b64 {
        Some(b) => b,
        None => return ("present_unverified".into(), None, "EK certificate bytes are present and were not chained to a manufacturer".into()),
    };
    let blob = match b64_dec(claim) {
        Ok(b) => b,
        Err(e) => return ("present_unverified".into(), None, e),
    };
    let claim_name = tpm.key_attestation_claim.clone().unwrap_or_default();
    match local_tpm_matches(key_dir, spki) {
        LocalTpm::Match(non_exportable) => match verify_platform_claim(&claim_name, &blob) {
            Ok(()) => ("verified".into(), non_exportable, "NCryptVerifyClaim accepted the blob for the local TPM key. That does not prove the capture software was unmodified.".into()),
            Err(e) => ("present_unverified".into(), non_exportable, e),
        },
        LocalTpm::Different => ("present_unverified".into(), None, "the local TPM key does not match this public key, so the claim was not checked".into()),
        LocalTpm::Unavailable(why) => ("present_unverified".into(), None, why),
    }
}

#[cfg_attr(not(windows), allow(dead_code))]
enum LocalTpm { Match(Option<bool>), Different, Unavailable(String) }

fn sign_with(key: &KeyRecord, key_dir: &Path, message: &[u8]) -> Result<(Vec<u8>, Option<TpmBlock>), String> {
    if key.active == "software" || key.key_provider == PROVIDER_SOFTWARE {
        let pkcs8 = std::fs::read(key_dir.join(SOFTWARE_FILE)).map_err(|e| format!("read software device key: {e}"))?;
        return Ok((sign_p256(&pkcs8, message)?, None));
    }
    if key.active == "tpm" {
        return sign_tpm(key, message);
    }
    Err(format!("device key active={} is not software or tpm", key.active))
}

fn read_key(dir: &Path) -> Result<KeyRecord, String> {
    let path = dir.join(KEY_RECORD);
    let raw = std::fs::read_to_string(&path).map_err(|e| format!("read {}: {e}", path.display()))?;
    let rec: KeyRecord = serde_json::from_str(&raw).map_err(|e| format!("device key record: {e}"))?;
    if rec.schema != KEY_SCHEMA { return Err(format!("device key schema is {}", rec.schema)); }
    Ok(rec)
}

fn write_json<T: Serialize>(path: &Path, value: &T) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| format!("create {}: {e}", parent.display()))?;
    }
    let text = serde_json::to_string_pretty(value).map_err(|e| e.to_string())? + "\n";
    std::fs::write(path, text).map_err(|e| format!("write {}: {e}", path.display()))
}

fn write_private(path: &Path, bytes: &[u8]) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| format!("create {}: {e}", parent.display()))?;
    }
    std::fs::write(path, bytes).map_err(|e| format!("write {}: {e}", path.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
    }
    #[cfg(windows)]
    {
        if let Ok(user) = std::env::var("USERNAME") {
            let user = user.trim();
            if !user.is_empty() && !user.contains(['"', '\r', '\n']) {
                let _ = std::process::Command::new("icacls").arg(path).arg("/inheritance:r").arg("/grant:r")
                    .arg(format!("{user}:F")).stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null()).status();
            }
        }
    }
    Ok(())
}

fn b64(bytes: &[u8]) -> String { base64::engine::general_purpose::STANDARD.encode(bytes) }
fn b64_dec(s: &str) -> Result<Vec<u8>, String> {
    base64::engine::general_purpose::STANDARD.decode(s.trim()).map_err(|e| format!("base64: {e}"))
}

fn hex64(s: &str) -> Result<(), String> {
    if s.len() == 64 && s.bytes().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()) { Ok(()) }
    else { Err(format!("chain head is not 64 lowercase hex characters")) }
}

fn generate_p256() -> Result<(Vec<u8>, Vec<u8>), String> {
    let rng = ::ring::rand::SystemRandom::new();
    let doc = ::ring::signature::EcdsaKeyPair::generate_pkcs8(&::ring::signature::ECDSA_P256_SHA256_FIXED_SIGNING, &rng)
        .map_err(|_| "could not generate a P-256 key".to_string())?;
    let spki = spki_from_pkcs8(doc.as_ref())?;
    Ok((doc.as_ref().to_vec(), spki))
}

fn spki_from_pkcs8(pkcs8: &[u8]) -> Result<Vec<u8>, String> {
    let rng = ::ring::rand::SystemRandom::new();
    let key = ::ring::signature::EcdsaKeyPair::from_pkcs8(&::ring::signature::ECDSA_P256_SHA256_FIXED_SIGNING, pkcs8, &rng)
        .map_err(|_| "software device key is not a P-256 PKCS#8 key".to_string())?;
    spki_p256(key.public_key().as_ref())
}

fn sign_p256(pkcs8: &[u8], message: &[u8]) -> Result<Vec<u8>, String> {
    let rng = ::ring::rand::SystemRandom::new();
    let key = ::ring::signature::EcdsaKeyPair::from_pkcs8(&::ring::signature::ECDSA_P256_SHA256_FIXED_SIGNING, pkcs8, &rng)
        .map_err(|_| "software device key is not a P-256 PKCS#8 key".to_string())?;
    let sig = key.sign(&rng, message).map_err(|_| "software sign failed".to_string())?;
    Ok(sig.as_ref().to_vec())
}

fn verify_sig(alg: &str, spki: &[u8], message: &[u8], sig: &[u8]) -> Result<(), String> {
    match (alg, parse_spki(spki)?) {
        (ALG_P256, Inner::P256(point)) => {
            let key = ::ring::signature::UnparsedPublicKey::new(&::ring::signature::ECDSA_P256_SHA256_FIXED, &point);
            key.verify(message, sig).map_err(|_| "ECDSA P-256 signature did not verify".to_string())
        }
        (ALG_RSA, Inner::Rsa(pkcs1)) => {
            let n = rsa_modulus_octets(&pkcs1)?;
            if n != 256 { return Err(format!("RSA modulus is {n} bytes, expected 256 for RSA-2048")); }
            let key = ::ring::signature::UnparsedPublicKey::new(&::ring::signature::RSA_PKCS1_2048_8192_SHA256, &pkcs1);
            key.verify(message, sig).map_err(|_| "RSA-2048 PKCS#1 SHA-256 signature did not verify".to_string())
        }
        (ALG_P256, Inner::Rsa(_)) => Err("algorithm is ECDSA_P256 but the public key is RSA".into()),
        (ALG_RSA, Inner::P256(_)) => Err("algorithm is RSA_2048 but the public key is P-256".into()),
        _ => Err(format!("algorithm {alg} is not supported")),
    }
}

enum Inner { P256(Vec<u8>), Rsa(Vec<u8>) }

fn spki_p256(point: &[u8]) -> Result<Vec<u8>, String> {
    if point.len() != 65 || point[0] != 0x04 { return Err("P-256 public key is not an uncompressed point".into()); }
    // ecPublicKey + prime256v1
    const ALG: &[u8] = &[0x30, 0x13, 0x06, 0x07, 0x2a, 0x86, 0x48, 0xce, 0x3d, 0x02, 0x01, 0x06, 0x08, 0x2a, 0x86, 0x48, 0xce, 0x3d, 0x03, 0x01, 0x07];
    let mut bit = vec![0x00];
    bit.extend_from_slice(point);
    Ok(der_seq(&[ALG, &der_tlv(0x03, &bit)]))
}

fn parse_spki(spki: &[u8]) -> Result<Inner, String> {
    let mut d = Der { s: spki, i: 0 };
    let (tag, body) = d.tlv()?;
    if tag != 0x30 || d.i != spki.len() { return Err("SPKI is not one SEQUENCE".into()); }
    let mut d = Der { s: body, i: 0 };
    let (at, alg) = d.tlv()?;
    if at != 0x30 { return Err("SPKI algorithm is not a SEQUENCE".into()); }
    let (bt, bits) = d.tlv()?;
    if bt != 0x03 || d.i != body.len() { return Err("SPKI missing BIT STRING".into()); }
    if bits.first() != Some(&0x00) { return Err("SPKI BIT STRING has unused bits".into()); }
    let key = &bits[1..];
    let mut ad = Der { s: alg, i: 0 };
    let (ot, oid) = ad.tlv()?;
    if ot != 0x06 { return Err("algorithm OID missing".into()); }
    const EC: &[u8] = &[0x2a, 0x86, 0x48, 0xce, 0x3d, 0x02, 0x01];
    const RSA: &[u8] = &[0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x01, 0x01];
    const P256: &[u8] = &[0x2a, 0x86, 0x48, 0xce, 0x3d, 0x03, 0x01, 0x07];
    if oid == EC {
        let (t2, curve) = ad.tlv()?;
        if t2 != 0x06 || curve != P256 { return Err("SPKI curve is not P-256".into()); }
        if key.len() != 65 || key[0] != 0x04 { return Err("P-256 point is not uncompressed".into()); }
        Ok(Inner::P256(key.to_vec()))
    } else if oid == RSA {
        Ok(Inner::Rsa(key.to_vec()))
    } else {
        Err("SPKI algorithm is not P-256 or RSA".into())
    }
}

fn rsa_modulus_octets(pkcs1: &[u8]) -> Result<usize, String> {
    let mut d = Der { s: pkcs1, i: 0 };
    let (t, body) = d.tlv()?;
    if t != 0x30 { return Err("RSAPublicKey is not a SEQUENCE".into()); }
    let mut d = Der { s: body, i: 0 };
    let (it, n) = d.tlv()?;
    if it != 0x02 { return Err("RSA modulus is not an INTEGER".into()); }
    let n = if n.first() == Some(&0x00) { &n[1..] } else { n };
    Ok(n.len())
}

#[cfg_attr(not(windows), allow(dead_code))]
fn point_from_ecc_public_blob(blob: &[u8]) -> Result<Vec<u8>, String> {
    if blob.len() < 8 { return Err("ECC public blob is short".into()); }
    let magic = u32::from_le_bytes(blob[0..4].try_into().unwrap());
    let cb = u32::from_le_bytes(blob[4..8].try_into().unwrap()) as usize;
    // BCRYPT_ECDSA_PUBLIC_P256_MAGIC 'ECS1'
    if magic != 0x3153_4345 || cb != 32 { return Err(format!("expected a P-256 public blob, magic {magic:#x} length {cb}")); }
    if blob.len() < 8 + 64 { return Err("ECC public blob is truncated".into()); }
    let mut point = vec![0x04];
    point.extend_from_slice(&blob[8..72]);
    Ok(point)
}

/// CNG `BCRYPT_RSAPUBLIC_BLOB` (`RSA1`) to SPKI. Used when the TPM key is RSA-2048.
#[cfg(any(windows, test))]
fn spki_from_rsa_public_blob(blob: &[u8]) -> Result<Vec<u8>, String> {
    if blob.len() < 24 { return Err("RSA public blob is short".into()); }
    let magic = u32::from_le_bytes(blob[0..4].try_into().unwrap());
    if magic != 0x3141_5352 { return Err(format!("RSA public blob magic is {magic:#x}")); }
    let cb_exp = u32::from_le_bytes(blob[8..12].try_into().unwrap()) as usize;
    let cb_mod = u32::from_le_bytes(blob[12..16].try_into().unwrap()) as usize;
    let cb_p1 = u32::from_le_bytes(blob[16..20].try_into().unwrap()) as usize;
    let cb_p2 = u32::from_le_bytes(blob[20..24].try_into().unwrap()) as usize;
    if cb_p1 != 0 || cb_p2 != 0 { return Err("RSA public blob contains primes".into()); }
    if blob.len() < 24 + cb_exp + cb_mod { return Err("RSA public blob is truncated".into()); }
    let exp = &blob[24..24 + cb_exp];
    let modulus = &blob[24 + cb_exp..24 + cb_exp + cb_mod];
    let pkcs1 = der_seq(&[&der_int(modulus), &der_int(exp)]);
    // rsaEncryption + NULL
    const ALG: &[u8] = &[0x30, 0x0d, 0x06, 0x09, 0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x01, 0x01, 0x05, 0x00];
    let mut bit = vec![0x00];
    bit.extend_from_slice(&pkcs1);
    let spki = der_seq(&[ALG, &der_tlv(0x03, &bit)]);
    if rsa_modulus_octets(&pkcs1)? != 256 { return Err("RSA public blob is not 2048 bits".into()); }
    Ok(spki)
}

struct Der<'a> { s: &'a [u8], i: usize }
impl<'a> Der<'a> {
    fn take(&mut self, n: usize) -> Result<&'a [u8], String> {
        if self.i + n > self.s.len() { return Err("truncated DER".into()); }
        let o = &self.s[self.i..self.i + n];
        self.i += n;
        Ok(o)
    }
    fn tlv(&mut self) -> Result<(u8, &'a [u8]), String> {
        let tag = self.take(1)?[0];
        let b0 = self.take(1)?[0];
        let len = if b0 < 128 {
            b0 as usize
        } else if b0 == 0x81 {
            self.take(1)?[0] as usize
        } else if b0 == 0x82 {
            let b = self.take(2)?;
            ((b[0] as usize) << 8) | b[1] as usize
        } else {
            return Err("unsupported DER length".into());
        };
        Ok((tag, self.take(len)?))
    }
}

fn der_len(n: usize) -> Vec<u8> {
    if n < 128 { vec![n as u8] }
    else if n < 256 { vec![0x81, n as u8] }
    else { vec![0x82, (n >> 8) as u8, (n & 0xff) as u8] }
}
fn der_tlv(tag: u8, body: &[u8]) -> Vec<u8> {
    let mut o = vec![tag];
    o.extend(der_len(body.len()));
    o.extend_from_slice(body);
    o
}
fn der_seq(parts: &[&[u8]]) -> Vec<u8> {
    let mut body = vec![];
    for p in parts { body.extend_from_slice(p); }
    der_tlv(0x30, &body)
}
#[cfg(any(windows, test))]
fn der_int(bytes: &[u8]) -> Vec<u8> {
    let mut v = bytes;
    while v.len() > 1 && v[0] == 0 { v = &v[1..]; }
    let mut body = vec![];
    if v.is_empty() || v[0] & 0x80 != 0 { body.push(0); }
    body.extend_from_slice(v);
    der_tlv(0x02, &body)
}

fn tpm_provider_opens() -> bool { tpm_backend::provider_opens() }
fn tpm_record() -> Result<KeyRecord, String> { tpm_backend::record() }
fn sign_tpm(key: &KeyRecord, message: &[u8]) -> Result<(Vec<u8>, Option<TpmBlock>), String> { tpm_backend::sign(key, message) }
fn local_tpm_matches(key_dir: &Path, spki: &[u8]) -> LocalTpm { tpm_backend::matches(key_dir, spki) }
fn verify_platform_claim(name: &str, blob: &[u8]) -> Result<(), String> { tpm_backend::verify_claim(name, blob) }

#[cfg(not(windows))]
mod tpm_backend {
    use super::*;
    pub fn provider_opens() -> bool { false }
    pub fn record() -> Result<KeyRecord, String> { Err("Microsoft Platform Crypto Provider is a Windows CNG provider".into()) }
    pub fn sign(_key: &KeyRecord, _message: &[u8]) -> Result<(Vec<u8>, Option<TpmBlock>), String> {
        Err("the device key is in a Windows TPM and cannot be used on this OS".into())
    }
    pub fn matches(_key_dir: &Path, _spki: &[u8]) -> LocalTpm {
        LocalTpm::Unavailable("NCryptVerifyClaim runs on Windows".into())
    }
    pub fn verify_claim(_name: &str, _blob: &[u8]) -> Result<(), String> {
        Err("NCryptVerifyClaim runs on Windows".into())
    }
}

#[cfg(windows)]
mod tpm_backend {
    use super::*;
    use std::ffi::c_void;

    const SILENT: u32 = 0x40;
    const NTE_BAD_KEYSET: i32 = 0x8009_0016_u32 as i32;
    const CLAIM_SUBJECT_ONLY: u32 = 0x2;
    const CLAIM_PLATFORM: u32 = 0x0001_0000;

    type CreateClaim = unsafe extern "system" fn(usize, usize, u32, *mut c_void, *mut u8, u32, *mut u32, u32) -> i32;
    type VerifyClaim = unsafe extern "system" fn(usize, usize, u32, *mut c_void, *const u8, u32, *mut c_void, u32) -> i32;

    #[link(name = "ncrypt")]
    extern "system" {
        fn NCryptOpenStorageProvider(out: *mut usize, name: *const u16, flags: u32) -> i32;
        fn NCryptFreeObject(h: usize) -> i32;
        fn NCryptIsAlgSupported(prov: usize, alg: *const u16, flags: u32) -> i32;
        fn NCryptOpenKey(prov: usize, out: *mut usize, name: *const u16, legacy: u32, flags: u32) -> i32;
        fn NCryptCreatePersistedKey(prov: usize, out: *mut usize, alg: *const u16, name: *const u16, legacy: u32, flags: u32) -> i32;
        fn NCryptSetProperty(obj: usize, name: *const u16, input: *const u8, cb: u32, flags: u32) -> i32;
        fn NCryptFinalizeKey(key: usize, flags: u32) -> i32;
        fn NCryptGetProperty(obj: usize, name: *const u16, out: *mut u8, cb: u32, pcb: *mut u32, flags: u32) -> i32;
        fn NCryptExportKey(key: usize, export_key: usize, kind: *const u16, params: *mut c_void, out: *mut u8, cb: u32, pcb: *mut u32, flags: u32) -> i32;
        fn NCryptSignHash(key: usize, pad: *mut c_void, hash: *const u8, cb_hash: u32, sig: *mut u8, cb_sig: u32, pcb: *mut u32, flags: u32) -> i32;
        fn NCryptDeleteKey(key: usize, flags: u32) -> i32;
    }
    #[link(name = "kernel32")]
    extern "system" {
        fn GetModuleHandleW(name: *const u16) -> *mut c_void;
        fn LoadLibraryW(name: *const u16) -> *mut c_void;
        fn GetProcAddress(module: *mut c_void, name: *const i8) -> *mut c_void;
    }

    struct Handle(usize);
    impl Drop for Handle {
        fn drop(&mut self) { if self.0 != 0 { unsafe { NCryptFreeObject(self.0); } } }
    }

    #[repr(C)]
    struct Pkcs1Pad { alg: *const u16 }

    pub fn provider_opens() -> bool { open_provider().is_ok() }

    pub fn record() -> Result<KeyRecord, String> {
        let prov = open_provider()?;
        let (key, alg) = open_or_create(prov.0)?;
        let spki = export_spki(key.0, &alg)?;
        let non_exportable = read_non_exportable(key.0);
        Ok(KeyRecord {
            schema: KEY_SCHEMA.into(), active: "tpm".into(), key_provider: PROVIDER_TPM.into(),
            algorithm: alg, public_key_spki_der_b64: b64(&spki), non_exportable,
            tpm_key_name: Some(TPM_KEY_NAME.into()), fallback: None,
        })
    }

    pub fn sign(key: &KeyRecord, message: &[u8]) -> Result<(Vec<u8>, Option<TpmBlock>), String> {
        let prov = open_provider()?;
        let (handle, alg) = open_only(prov.0)?;
        if alg != key.algorithm { return Err(format!("TPM key algorithm is {alg}, key record says {}", key.algorithm)); }
        let spki = export_spki(handle.0, &alg)?;
        if b64(&spki) != key.public_key_spki_der_b64 {
            return Err("TPM public key does not match the device key record".into());
        }
        let sig = sign_hash(handle.0, &alg, message)?;
        let tpm = collect(prov.0, handle.0, &alg);
        Ok((sig, tpm.filter(|t| !t.empty())))
    }

    pub fn matches(_key_dir: &Path, spki: &[u8]) -> LocalTpm {
        let prov = match open_provider() { Ok(p) => p, Err(e) => return LocalTpm::Unavailable(e) };
        let (key, alg) = match open_only(prov.0) { Ok(v) => v, Err(e) => return LocalTpm::Unavailable(e) };
        let mine = match export_spki(key.0, &alg) { Ok(s) => s, Err(e) => return LocalTpm::Unavailable(e) };
        if mine.as_slice() == spki { LocalTpm::Match(read_non_exportable(key.0)) } else { LocalTpm::Different }
    }

    pub fn verify_claim(name: &str, blob: &[u8]) -> Result<(), String> {
        let ty = claim_type(name)?;
        let verify = load_verify()?;
        let prov = open_provider()?;
        let (key, _) = open_only(prov.0)?;
        let st = unsafe { verify(key.0, 0, ty, std::ptr::null_mut(), blob.as_ptr(), blob.len() as u32, std::ptr::null_mut(), 0) };
        if st == 0 { Ok(()) } else { Err(format!("NCryptVerifyClaim returned {st:#010x}")) }
    }

    fn claim_type(name: &str) -> Result<u32, String> {
        match name {
            "NCRYPT_CLAIM_PLATFORM" => Ok(CLAIM_PLATFORM),
            "NCRYPT_CLAIM_SUBJECT_ONLY" => Ok(CLAIM_SUBJECT_ONLY),
            other => Err(format!("unknown claim type {other}")),
        }
    }

    fn open_provider() -> Result<Handle, String> {
        let mut h = 0usize;
        let name = wide(PROVIDER_TPM);
        let st = unsafe { NCryptOpenStorageProvider(&mut h, name.as_ptr(), SILENT) };
        if st != 0 || h == 0 { return Err(format!("NCryptOpenStorageProvider({PROVIDER_TPM}) returned {st:#010x}")); }
        Ok(Handle(h))
    }

    fn open_only(prov: usize) -> Result<(Handle, String), String> {
        match open_named(prov) {
            Ok(h) => {
                let alg = map_alg(&algorithm_of(h.0)?)?;
                if alg == ALG_RSA {
                    let bits = get_dword(h.0, "Length")?;
                    if bits != 2048 { return Err(format!("existing TPM key is RSA-{bits}, not RSA-2048")); }
                }
                Ok((h, alg))
            }
            Err(OpenErr::Missing) => Err(format!("no TPM key named {TPM_KEY_NAME}")),
            Err(OpenErr::Other(e)) => Err(e),
        }
    }

    fn open_or_create(prov: usize) -> Result<(Handle, String), String> {
        match open_named(prov) {
            Ok(h) => {
                let alg = map_alg(&algorithm_of(h.0)?)?;
                if alg == ALG_RSA { let bits = get_dword(h.0, "Length")?; if bits != 2048 { return Err(format!("existing TPM key is RSA-{bits}, not RSA-2048")); } }
                Ok((h, alg))
            }
            Err(OpenErr::Missing) => create_new(prov),
            Err(OpenErr::Other(e)) => Err(e),
        }
    }

    enum OpenErr { Missing, Other(String) }

    fn open_named(prov: usize) -> Result<Handle, OpenErr> {
        let mut h = 0usize;
        let name = wide(TPM_KEY_NAME);
        let st = unsafe { NCryptOpenKey(prov, &mut h, name.as_ptr(), 0, SILENT) };
        if st == 0 && h != 0 { Ok(Handle(h)) }
        else if st == NTE_BAD_KEYSET { Err(OpenErr::Missing) }
        else { Err(OpenErr::Other(format!("NCryptOpenKey({TPM_KEY_NAME}) returned {st:#010x}"))) }
    }

    fn create_new(prov: usize) -> Result<(Handle, String), String> {
        if alg_ok(prov, "ECDSA_P256") {
            if let Ok(h) = create_one(prov, "ECDSA_P256", None) { return Ok((h, ALG_P256.into())); }
        }
        if alg_ok(prov, "RSA") {
            if let Ok(h) = create_one(prov, "RSA", Some(2048)) { return Ok((h, ALG_RSA.into())); }
        }
        Err("Platform Crypto Provider did not create an ECDSA P-256 or RSA-2048 key".into())
    }

    fn alg_ok(prov: usize, alg: &str) -> bool {
        let w = wide(alg);
        unsafe { NCryptIsAlgSupported(prov, w.as_ptr(), SILENT) == 0 }
    }

    fn create_one(prov: usize, alg: &str, bits: Option<u32>) -> Result<Handle, String> {
        let mut raw = 0usize;
        let alg_w = wide(alg);
        let name = wide(TPM_KEY_NAME);
        let st = unsafe { NCryptCreatePersistedKey(prov, &mut raw, alg_w.as_ptr(), name.as_ptr(), 0, 0) };
        if st != 0 || raw == 0 { return Err(format!("NCryptCreatePersistedKey({alg}) returned {st:#010x}")); }
        let mut h = Handle(raw);
        if let Some(bits) = bits {
            let st = unsafe { NCryptSetProperty(h.0, wide("Length").as_ptr(), &bits as *const u32 as *const u8, 4, 0) };
            if st != 0 { delete_key(&mut h); return Err(format!("set Length {bits} returned {st:#010x}")); }
        }
        let policy: u32 = 0;
        let _ = unsafe { NCryptSetProperty(h.0, wide("Export Policy").as_ptr(), &policy as *const u32 as *const u8, 4, 0) };
        let st = unsafe { NCryptFinalizeKey(h.0, SILENT) };
        if st != 0 { delete_key(&mut h); return Err(format!("NCryptFinalizeKey returned {st:#010x}")); }
        Ok(h)
    }

    fn delete_key(h: &mut Handle) {
        if h.0 == 0 { return; }
        let st = unsafe { NCryptDeleteKey(h.0, SILENT) };
        if st == 0 { h.0 = 0; }
    }

    fn algorithm_of(key: usize) -> Result<String, String> {
        Ok(utf16(&get_bytes(key, "Algorithm Name")?))
    }

    fn map_alg(name: &str) -> Result<String, String> {
        match name {
            "ECDSA_P256" => Ok(ALG_P256.into()),
            "RSA" => Ok(ALG_RSA.into()),
            other => Err(format!("existing TPM key {TPM_KEY_NAME} uses {other}, which this command does not sign with")),
        }
    }

    fn export_spki(key: usize, alg: &str) -> Result<Vec<u8>, String> {
        if alg == ALG_P256 {
            let blob = export_blob(key, "ECCPUBLICBLOB")?;
            spki_p256(&point_from_ecc_public_blob(&blob)?)
        } else if alg == ALG_RSA {
            let blob = export_blob(key, "RSAPUBLICBLOB")?;
            spki_from_rsa_public_blob(&blob)
        } else {
            Err(format!("cannot export {alg}"))
        }
    }

    fn export_blob(key: usize, kind: &str) -> Result<Vec<u8>, String> {
        let w = wide(kind);
        let mut needed = 0u32;
        let st = unsafe { NCryptExportKey(key, 0, w.as_ptr(), std::ptr::null_mut(), std::ptr::null_mut(), 0, &mut needed, 0) };
        if st != 0 || needed == 0 || needed > 1_000_000 { return Err(format!("NCryptExportKey({kind}) size returned {st:#010x}")); }
        let mut buf = vec![0u8; needed as usize];
        let mut wrote = 0u32;
        let st = unsafe { NCryptExportKey(key, 0, w.as_ptr(), std::ptr::null_mut(), buf.as_mut_ptr(), needed, &mut wrote, 0) };
        if st != 0 { return Err(format!("NCryptExportKey({kind}) returned {st:#010x}")); }
        buf.truncate(wrote as usize);
        Ok(buf)
    }

    fn sign_hash(key: usize, alg: &str, message: &[u8]) -> Result<Vec<u8>, String> {
        let hash = Sha256::digest(message);
        let sha = wide("SHA256");
        let mut pad = Pkcs1Pad { alg: sha.as_ptr() };
        let (pad_ptr, flags): (*mut c_void, u32) = if alg == ALG_RSA {
            (&mut pad as *mut Pkcs1Pad as *mut c_void, 0x2)
        } else {
            (std::ptr::null_mut(), 0)
        };
        let mut needed = 0u32;
        let st = unsafe { NCryptSignHash(key, pad_ptr, hash.as_ptr(), hash.len() as u32, std::ptr::null_mut(), 0, &mut needed, flags) };
        if st != 0 || needed == 0 || needed > 1_000_000 { return Err(format!("NCryptSignHash size returned {st:#010x}")); }
        let mut buf = vec![0u8; needed as usize];
        let mut wrote = 0u32;
        let st = unsafe { NCryptSignHash(key, pad_ptr, hash.as_ptr(), hash.len() as u32, buf.as_mut_ptr(), needed, &mut wrote, flags) };
        if st != 0 { return Err(format!("NCryptSignHash returned {st:#010x}")); }
        buf.truncate(wrote as usize);
        Ok(buf)
    }

    fn read_non_exportable(key: usize) -> Option<bool> {
        let bits = get_dword(key, "Export Policy").ok()?;
        Some(bits & EXPORT_BITS == 0)
    }

    fn collect(prov: usize, key: usize, alg: &str) -> Option<TpmBlock> {
        let mut t = TpmBlock { key_attestation_b64: None, key_attestation_claim: None, ek_cert_b64: None, ek_cert_property: None, platform_type: None };
        if let Some((name, blob)) = create_claim(key) {
            t.key_attestation_b64 = Some(b64(&blob));
            t.key_attestation_claim = Some(name);
        }
        let props: &[&str] = if alg == ALG_RSA { &["PCP_RSA_EKNVCERT", "PCP_EKNVCERT"] } else { &["PCP_ECC_EKNVCERT", "PCP_EKNVCERT"] };
        for prop in props {
            if let Ok(bytes) = get_bytes(prov, prop) {
                if bytes.len() >= 64 {
                    t.ek_cert_b64 = Some(b64(&bytes));
                    t.ek_cert_property = Some((*prop).into());
                    break;
                }
            }
        }
        if let Ok(bytes) = get_bytes(prov, "PCP_PLATFORM_TYPE") {
            let s = utf16(&bytes);
            if !s.is_empty() && s.len() < 200 { t.platform_type = Some(s); }
        }
        if t.empty() { None } else { Some(t) }
    }

    fn create_claim(key: usize) -> Option<(String, Vec<u8>)> {
        let create = load_create()?;
        for (name, ty) in [("NCRYPT_CLAIM_PLATFORM", CLAIM_PLATFORM), ("NCRYPT_CLAIM_SUBJECT_ONLY", CLAIM_SUBJECT_ONLY)] {
            let mut needed = 0u32;
            let st = unsafe { create(key, 0, ty, std::ptr::null_mut(), std::ptr::null_mut(), 0, &mut needed, 0) };
            if st != 0 || needed == 0 || needed > 1_000_000 { continue; }
            let mut buf = vec![0u8; needed as usize];
            let mut wrote = 0u32;
            let st = unsafe { create(key, 0, ty, std::ptr::null_mut(), buf.as_mut_ptr(), needed, &mut wrote, 0) };
            if st == 0 && wrote > 0 {
                buf.truncate(wrote as usize);
                return Some((name.to_string(), buf));
            }
        }
        None
    }

    fn get_bytes(obj: usize, prop: &str) -> Result<Vec<u8>, String> {
        let w = wide(prop);
        let mut needed = 0u32;
        let st = unsafe { NCryptGetProperty(obj, w.as_ptr(), std::ptr::null_mut(), 0, &mut needed, SILENT) };
        if st != 0 { return Err(format!("NCryptGetProperty({prop}) size returned {st:#010x}")); }
        if needed == 0 { return Ok(vec![]); }
        if needed > 1_000_000 { return Err(format!("{prop} is {needed} bytes")); }
        let mut buf = vec![0u8; needed as usize];
        let mut wrote = 0u32;
        let st = unsafe { NCryptGetProperty(obj, w.as_ptr(), buf.as_mut_ptr(), needed, &mut wrote, SILENT) };
        if st != 0 { return Err(format!("NCryptGetProperty({prop}) returned {st:#010x}")); }
        buf.truncate(wrote as usize);
        Ok(buf)
    }

    fn get_dword(obj: usize, prop: &str) -> Result<u32, String> {
        let b = get_bytes(obj, prop)?;
        if b.len() < 4 { return Err(format!("{prop} is {} bytes", b.len())); }
        Ok(u32::from_le_bytes([b[0], b[1], b[2], b[3]]))
    }

    fn utf16(bytes: &[u8]) -> String {
        let mut u = vec![];
        let mut i = 0;
        while i + 1 < bytes.len() {
            let c = u16::from_le_bytes([bytes[i], bytes[i + 1]]);
            if c == 0 { break; }
            u.push(c);
            i += 2;
        }
        String::from_utf16_lossy(&u)
    }

    fn wide(s: &str) -> Vec<u16> { s.encode_utf16().chain(std::iter::once(0)).collect() }

    fn proc(name: &str) -> Option<*mut c_void> {
        let module_name = wide("ncrypt.dll");
        let mut module = unsafe { GetModuleHandleW(module_name.as_ptr()) };
        if module.is_null() { module = unsafe { LoadLibraryW(module_name.as_ptr()) }; }
        if module.is_null() { return None; }
        let mut z = name.as_bytes().to_vec();
        z.push(0);
        let p = unsafe { GetProcAddress(module, z.as_ptr() as *const i8) };
        if p.is_null() { None } else { Some(p) }
    }

    fn load_create() -> Option<CreateClaim> {
        let p = proc("NCryptCreateClaim")?;
        Some(unsafe { std::mem::transmute(p) })
    }
    fn load_verify() -> Result<VerifyClaim, String> {
        let p = proc("NCryptVerifyClaim").ok_or("NCryptVerifyClaim is not exported by ncrypt.dll")?;
        Ok(unsafe { std::mem::transmute(p) })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn head() -> String { "ab".repeat(32) }

    #[test]
    fn signed_message_is_prefix_plus_chain_head() {
        let m = signing_bytes(&head()).unwrap();
        assert_eq!(m.len(), 88);
        assert!(m.starts_with(SIGNED_PREFIX.as_bytes()));
        assert_eq!(&m[SIGNED_PREFIX.len()..], head().as_bytes());
        assert!(signing_bytes(&"AB".repeat(32)).is_err());
    }

    #[test]
    fn chain_head_refuses_two_roles_and_an_empty_receipt() {
        let one = json!({"checkpoints": {"home": [{"hash": head()}], "away": []}});
        assert_eq!(chain_head_of(&one).unwrap(), head());
        let away = json!({"checkpoints": {"away": [{"hash": "cd".repeat(32)}]}});
        assert_eq!(chain_head_of(&away).unwrap(), "cd".repeat(32));
        let both = json!({"checkpoints": {"home": [{"hash": head()}], "away": [{"hash": head()}]}});
        assert!(chain_head_of(&both).unwrap_err().contains("both home and away"));
        assert!(chain_head_of(&json!({"checkpoints": {}})).is_err());
    }

    #[test]
    fn receipt_schema_still_rejects_a_device_attest_field() {
        let s: Value = serde_json::from_str(include_str!("../contracts/receipt.v0.json")).unwrap();
        assert_eq!(s["additionalProperties"], false);
        assert!(s["properties"].get("device_attest").is_none());
        let side: Value = serde_json::from_str(include_str!("../contracts/device_attest.v0.json")).unwrap();
        assert_eq!(side["additionalProperties"], false);
        assert_eq!(side["properties"]["schema"]["const"], SCHEMA);
    }

    #[test]
    fn software_sidecar_verifies_and_does_not_touch_the_receipt() {
        let dir = std::env::temp_dir().join(format!("qorcap-devkey-{}-{}", std::process::id(), line!()));
        let _ = std::fs::remove_dir_all(&dir);
        let keys = dir.join("keys");
        let bundle = dir.join("bundle");
        std::fs::create_dir_all(&bundle).unwrap();
        let receipt = json!({"checkpoints": {"home": [{"hash": head()}], "away": []}, "schema": "qor-receipt/v0"});
        let receipt_text = serde_json::to_string_pretty(&receipt).unwrap();
        std::fs::write(bundle.join("receipt.json"), &receipt_text).unwrap();
        let out = init(&keys, true, Some(&bundle)).unwrap();
        assert_eq!(out.receipt_unchanged, Some(true));
        assert_eq!(std::fs::read_to_string(bundle.join("receipt.json")).unwrap(), receipt_text);
        assert_eq!(out.key.key_provider, PROVIDER_SOFTWARE);
        assert_eq!(out.key.non_exportable, Some(false));
        assert!(out.key.fallback.is_none());
        let pkcs8 = std::fs::read(keys.join(SOFTWARE_FILE)).unwrap();
        let side = std::fs::read_to_string(bundle.join(SIDECAR_NAME)).unwrap();
        assert!(!side.contains(&b64(&pkcs8)));
        let mode = std::fs::metadata(keys.join(SOFTWARE_FILE)).unwrap().permissions();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(mode.mode() & 0o777, 0o600);
        }
        let _ = mode;
        let report = verify_dir(&keys, &bundle).unwrap();
        assert!(report.signature_ok, "{}", report.detail);
        assert_eq!(report.tpm_attestation, "absent");
        let mut parsed: Sidecar = serde_json::from_str(&side).unwrap();
        let mut sig = b64_dec(&parsed.signature_b64).unwrap();
        sig[0] ^= 0xff;
        parsed.signature_b64 = b64(&sig);
        std::fs::write(bundle.join(SIDECAR_NAME), serde_json::to_string_pretty(&parsed).unwrap()).unwrap();
        let bad = verify_dir(&keys, &bundle).unwrap();
        assert!(!bad.signature_ok);
        parsed.signature_b64 = side_sig(&side);
        parsed.non_exportable = Some(true);
        std::fs::write(bundle.join(SIDECAR_NAME), serde_json::to_string_pretty(&parsed).unwrap()).unwrap();
        let flipped = verify_dir(&keys, &bundle).unwrap();
        assert!(flipped.signature_ok, "non_exportable is outside the signature: {}", flipped.detail);
        assert_eq!(flipped.non_exportable_in_sidecar, Some(true));
        let mut extra: Value = serde_json::from_str(&std::fs::read_to_string(bundle.join(SIDECAR_NAME)).unwrap()).unwrap();
        extra["device_note"] = json!("nope");
        std::fs::write(bundle.join(SIDECAR_NAME), extra.to_string()).unwrap();
        assert!(verify_dir(&keys, &bundle).unwrap_err().contains("unknown field"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    fn side_sig(text: &str) -> String {
        serde_json::from_str::<Sidecar>(text).unwrap().signature_b64
    }

    #[test]
    fn rsa_public_blob_round_trips_through_spki() {
        let mut blob = vec![0u8; 24 + 3 + 256];
        blob[0..4].copy_from_slice(&0x3141_5352u32.to_le_bytes());
        blob[8..12].copy_from_slice(&3u32.to_le_bytes());
        blob[12..16].copy_from_slice(&256u32.to_le_bytes());
        blob[24] = 0x01;
        blob[25] = 0x00;
        blob[26] = 0x01;
        blob[27] = 0x80;
        let spki = spki_from_rsa_public_blob(&blob).unwrap();
        match parse_spki(&spki).unwrap() {
            Inner::Rsa(pkcs1) => assert_eq!(rsa_modulus_octets(&pkcs1).unwrap(), 256),
            Inner::P256(_) => panic!("rsa blob parsed as p256"),
        }
    }

    #[cfg(windows)]
    #[test]
    fn tpm_key_when_the_platform_provider_opens() {
        if !tpm_provider_opens() {
            eprintln!("skip: Microsoft Platform Crypto Provider did not open");
            return;
        }
        let dir = std::env::temp_dir().join(format!("qorcap-tpmkey-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let out = match init(&dir, false, None) {
            Ok(v) => v,
            Err(e) => { eprintln!("skip: TPM init failed: {e}"); return; }
        };
        if out.key.key_provider == PROVIDER_SOFTWARE {
            eprintln!("skip: provider opened but key creation fell back: {}", out.key.fallback.unwrap_or_default());
            return;
        }
        assert_eq!(out.key.key_provider, PROVIDER_TPM);
        assert!(out.key.algorithm == ALG_P256 || out.key.algorithm == ALG_RSA);
        assert_ne!(out.key.non_exportable, Some(false), "TPM key reports an export policy bit");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
