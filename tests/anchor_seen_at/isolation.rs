//! Design section 9, isolation/contracts: the module is off by default, unreachable from any
//! match, receipt or verdict path, opens no sockets, writes no controller output, and introduces
//! no new status beyond unknown / seen_at_late / seen_at_retained.

use qorcap::anchor_seen_at::{AnchorState, PRACTICE_ONLY};
use std::fs;
use std::path::{Path, PathBuf};

fn root() -> PathBuf { PathBuf::from(env!("CARGO_MANIFEST_DIR")) }

fn files(dir: &Path, ext: &str) -> Vec<PathBuf> {
    let mut out = vec![];
    for e in fs::read_dir(dir).unwrap() {
        let p = e.unwrap().path();
        if p.is_dir() { out.extend(files(&p, ext)); } else if p.extension().is_some_and(|x| x == ext) { out.push(p); }
    }
    out.sort();
    out
}

fn module_sources() -> Vec<(PathBuf, String)> {
    files(&root().join("src/anchor_seen_at"), "rs").into_iter().map(|p| { let s = fs::read_to_string(&p).unwrap(); (p, s) }).collect()
}

#[test]
fn feature_is_off_by_default_and_labelled_practice_only() {
    let toml = fs::read_to_string(root().join("Cargo.toml")).unwrap();
    let default = toml.lines().find(|l| l.trim_start().starts_with("default =")).unwrap();
    assert!(!default.contains("practice-anchor-seen-at"), "{default}");
    assert!(toml.contains("PRACTICE-ONLY, off by default"));
    assert!(PRACTICE_ONLY.starts_with("practice-only"));
    let lib = fs::read_to_string(root().join("src/lib.rs")).unwrap();
    let lines: Vec<&str> = lib.lines().collect();
    let at = lines.iter().position(|l| l.trim() == "pub mod anchor_seen_at;").unwrap();
    assert_eq!(lines[at - 1].trim(), r#"#[cfg(feature = "practice-anchor-seen-at")]"#);
}

#[test]
fn nothing_outside_the_module_reaches_it() {
    let module_dir = root().join("src/anchor_seen_at");
    for p in files(&root().join("src"), "rs") {
        if p.starts_with(&module_dir) || p.ends_with("lib.rs") { continue; }
        assert!(!fs::read_to_string(&p).unwrap().contains("anchor_seen_at"), "{}", p.display());
    }
    for p in files(&root().join("sidecar"), "py") {
        assert!(!fs::read_to_string(&p).unwrap().contains("anchor_seen_at"), "{}", p.display());
    }
    let lib = fs::read_to_string(root().join("src/lib.rs")).unwrap();
    assert_eq!(lib.matches("anchor_seen_at").count(), 1);
}

#[test]
fn module_uses_no_network_hid_receipt_or_verdict_code() {
    let banned = ["std::net", "TcpStream", "TcpListener", "UdpSocket", "tiny_http", "hidapi", "crate::hid", "crate::api", "crate::engine",
        "crate::bundle", "crate::redteam", "crate::inputproof", "crate::credentials", "crate::device_attest", "std::process"];
    for (p, src) in module_sources() {
        // P1 local journal storage is the only filesystem user; no runtime path calls it.
        if !p.ends_with("p1.rs") {
            assert!(!src.contains("std::fs") && !src.contains("File::"), "unexpected storage dependency");
        }
        for b in banned { assert!(!src.contains(b), "{} uses {b}", p.display()); }
        for line in src.lines().filter(|l| l.contains("crate::")) {
            assert!(line.contains("crate::evidence"), "{}: only crate::evidence is allowed: {line}", p.display());
        }
    }
}

#[test]
fn no_gold_or_new_verdict_status() {
    for (p, src) in module_sources() {
        let lower = src.to_lowercase();
        assert!(!lower.contains("gold"), "{}", p.display());
        assert!(!src.contains("VERIFIED") && !src.contains("Verified"), "{}", p.display());
    }
    let codes = [AnchorState::default().status_code()];
    assert_eq!(codes, ["unknown"]);
}

#[test]
fn wording_stays_seen_at_only() {
    // Assembled from fragments so this file does not contain the phrases it bans.
    let banned: Vec<String> = [["pro", "ven"], ["anti", "-cheat"], ["verified", " skill"], ["att", "ested"], ["proof of ", "human"], ["who", " held the pad"]]
        .iter().map(|p| p.concat()).collect();
    let mut texts = module_sources();
    for p in files(&root().join("tests/anchor_seen_at"), "rs") { let s = fs::read_to_string(&p).unwrap(); texts.push((p, s)); }
    texts.push((root().join("Cargo.toml"), fs::read_to_string(root().join("Cargo.toml")).unwrap()));
    for (p, src) in texts {
        let lower = src.to_lowercase();
        for b in &banned { assert!(!lower.contains(b.as_str()), "{} contains a banned phrase", p.display()); }
    }
}

#[test]
fn receipt_v0_contract_bytes_are_unchanged() {
    let sums = fs::read_to_string(root().join("contracts/SHA256SUMS")).unwrap();
    let want = sums.lines().find(|l| l.ends_with("receipt.v0.json")).expect("receipt.v0.json pinned in SHA256SUMS").split_whitespace().next().unwrap().to_string();
    let got = qorcap::evidence::sha256_hex(&fs::read(root().join("contracts/receipt.v0.json")).unwrap());
    assert_eq!(got, want);
    for (p, src) in module_sources() { assert!(!src.contains("receipt.v0"), "{}", p.display()); }
}
