//! Compile real external crates; negative controls require the intended Rust error code.
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
};
// Cargo's fingerprint layout is a test-only dependency. Unknown layouts fail closed;
// never select a possibly stale feature-off library by modification time.
fn artifact(deps: &Path, name: &str) -> PathBuf {
    let exe = std::env::current_exe().unwrap();
    let stem = exe.file_stem().unwrap().to_str().unwrap();
    let consumer = stem
        .strip_prefix("anchor_seen_at-")
        .expect("integration artifact");
    let fingerprints = deps.parent().unwrap().join(".fingerprint");
    let metadata: serde_json::Value = serde_json::from_slice(
        &fs::read(fingerprints.join(format!(
            "qorcap-{consumer}/test-integration-test-anchor_seen_at.json"
        )))
        .unwrap(),
    )
    .unwrap();
    let dependency = metadata["deps"]
        .as_array()
        .unwrap()
        .iter()
        .find(|d| d[1] == name)
        .unwrap();
    let value = dependency
        .as_array()
        .unwrap()
        .last()
        .unwrap()
        .as_u64()
        .unwrap();
    let expected: String = value
        .to_le_bytes()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    let mut matches = vec![];
    for entry in fs::read_dir(&fingerprints).unwrap() {
        let path = entry.unwrap().path();
        let marker = path.join(format!("lib-{name}"));
        if !marker.exists() {
            continue;
        }
        if fs::read_to_string(marker).unwrap().trim() != expected {
            continue;
        }
        let directory = path.file_name().unwrap().to_str().unwrap();
        let suffix = directory.strip_prefix(&format!("{name}-")).unwrap();
        let artifact = deps.join(format!("lib{name}-{suffix}.rlib"));
        assert!(artifact.is_file(), "bound compiler artifact missing");
        matches.push(artifact);
    }
    assert_eq!(matches.len(), 1, "exact dependency identity required");
    matches.pop().unwrap()
}
#[test]
fn p1_external_api_rejects_forgery_for_the_right_reasons() {
    let deps = std::env::current_exe()
        .unwrap()
        .parent()
        .unwrap()
        .to_path_buf();
    let dir = std::env::temp_dir().join(format!("qorcap-p1-compile-{}", std::process::id()));
    fs::create_dir(&dir).unwrap();
    let lib = artifact(&deps, "qorcap");
    let json = artifact(&deps, "serde_json");
    let mut cases: Vec<(String, String, Option<&str>)> = vec![];
    let types = [
        "OriginalExchange",
        "OpeningRecord",
        "PolicyObservation",
        "DecisionTimeRecord",
        "DurableLineage",
    ];
    for name in types {
        for (label, body, code) in [
            (
                "default",
                format!("fn f() {{ let _: {name} = Default::default(); }}"),
                "E0277",
            ),
            (
                "deserialize",
                format!("fn f() {{ let _ = serde_json::from_str::<{name}>(\"{{}}\"); }}"),
                "E0277",
            ),
            (
                "convert",
                format!("fn f() {{ let _: {name} = ().into(); }}"),
                "E0277",
            ),
        ] {
            cases.push((
                format!("{name}_{label}"),
                format!("use qorcap::anchor_seen_at::p1::{name}; {body}"),
                Some(code),
            ));
        }
    }
    for (name, field) in [
        ("OriginalExchange", "event"),
        ("OpeningRecord", "state"),
        ("PolicyObservation", "generation"),
        ("DecisionTimeRecord", "generation"),
        ("DurableLineage", "failed"),
    ] {
        cases.push((
            format!("{name}_private"),
            format!(
                "use qorcap::anchor_seen_at::p1::{name}; fn f(x:&{name}) {{ let _ = &x.{field}; }}"
            ),
            Some("E0616"),
        ));
        cases.push((format!("{name}_scratch"),format!("use qorcap::anchor_seen_at::p1::{name}; fn f() -> {name} {{ {name} {{ ..panic!() }} }}"),Some("E0451")));
    }
    for name in [
        "OpeningRecord",
        "PolicyObservation",
        "DecisionTimeRecord",
        "DurableLineage",
    ] {
        cases.push((format!("{name}_clone"),format!("use qorcap::anchor_seen_at::p1::{name}; fn c<T:Clone>(){{}} fn f(){{c::<{name}>();}}"),Some("E0277")));
    }
    cases.push(("nested_mutation".into(),"use qorcap::anchor_seen_at::p1::OriginalExchange; fn f(x:&OriginalExchange){x.commitment().session_id=String::new();}".into(),Some("E0594")));
    cases.push(("diagnostic_enum_conversion".into(),"use qorcap::anchor_seen_at::{p1::DecisionTimeRecord,readiness::Readiness}; fn f(x:Readiness)->DecisionTimeRecord{x.into()}".into(),Some("E0277")));
    cases.push(("decision_to_state_conversion".into(),"use qorcap::anchor_seen_at::{p1::DecisionTimeRecord,AnchorState}; fn f(x:DecisionTimeRecord)->AnchorState{x.into()}".into(),Some("E0277")));
    cases.push(("positive_getters".into(),r#"use qorcap::anchor_seen_at::p1::*;
fn f(x:&OriginalExchange,o:&OpeningRecord,p:&PolicyObservation,d:&DecisionTimeRecord,l:&DurableLineage) {
 let copy=x.clone(); let _=(copy.event(),x.challenge(),x.commitment(),x.origin(),x.policy());
 let _=(o.original(),o.state(),p,d.label(),d.policy_digest(),d.generation(),d.journal_head(),d.opening_event(),d.age_ns(),d.sample_offset_ns(),d.clock_scope(),d.publication_time_freshness_asserted(),l.opening(),l.diagnostic_coverage(),l.journal_head(),l.unavailable());
}"#.into(),None));
    for (index, (label, body, expected)) in cases.iter().enumerate() {
        let source = dir.join(format!("probe_{index}.rs"));
        fs::write(&source, body).unwrap();
        let output = Command::new("rustc")
            .args([
                "--edition=2021",
                "--crate-type=lib",
                "--emit=metadata",
                "--error-format=json",
                "--crate-name",
                "p1_probe",
            ])
            .arg(&source)
            .arg("-L")
            .arg(format!("dependency={}", deps.display()))
            .arg("--extern")
            .arg(format!("qorcap={}", lib.display()))
            .arg("--extern")
            .arg(format!("serde_json={}", json.display()))
            .arg("--out-dir")
            .arg(&dir)
            .output()
            .unwrap();
        let errors: Vec<String> = String::from_utf8(output.stderr)
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str::<serde_json::Value>(line).unwrap())
            .filter(|v| v["level"] == "error")
            .filter_map(|v| v["code"]["code"].as_str().map(str::to_owned))
            .collect();
        if let Some(code) = expected {
            assert!(!output.status.success(), "negative compiled: {label}");
            assert!(!errors.is_empty(), "no coded failure: {label}");
            assert!(
                errors.iter().all(|e| e == code),
                "wrong error: {label}: {errors:?}"
            );
        } else {
            assert!(
                output.status.success(),
                "positive failed with codes {errors:?}"
            );
        }
    }
    println!(
        "external compile controls: {} negative, 1 positive",
        cases.len() - 1
    );
}
