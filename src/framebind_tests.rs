use super::*;
use crate::evidence::CheckpointChain;

fn temp_dir(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("qorcap-bind-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn manifest() -> Value {
    json!({
        "schema": READERS_SCHEMA,
        "qorcap_version": "0.3.32",
        "python": "3.12.0",
        "numpy": null,
        "opencv": null,
        "ffmpeg": {"state": "unbound"},
        "glimpse": {"state": "unbound"},
        "readers": [{
            "id": "scorebug",
            "version": "0.3.32",
            "files": [{
                "kind": "code",
                "path": "sidecar/qorcap_standard.py",
                "sha256": "ab".repeat(32),
            }, {
                "kind": "pack",
                "path": "sidecar/packs/madden27_standard_score_110028.npz",
                "sha256": "cd".repeat(32),
            }]
        }]
    })
}

fn merge(dst: &mut Value, src: Value) {
    if let (Some(d), Some(s)) = (dst.as_object_mut(), src.as_object()) {
        for (k, v) in s {
            d.insert(k.clone(), v.clone());
        }
    }
}

fn seal_two(dir: &Path) -> (FrameBind, Vec<Value>) {
    let b = FrameBind::open(true, Some(dir), 30);
    b.append_frame(1, 0, 0, 0, b"au-0").unwrap();
    b.append_frame(1, 30, 1120, 90_000, b"au-30").unwrap();
    let mut ext0 = json!({"trigger": "start", "segment_seq": 1});
    merge(&mut ext0, b.commitment(true).unwrap());
    let mut chain = CheckpointChain::default();
    let cp0 = chain.next(10, ext0).unwrap();
    b.append_frame(2, 90, 3719, 270_000, b"au-90").unwrap();
    let mut ext1 = json!({"trigger": "segment", "segment_seq": 2});
    merge(&mut ext1, b.commitment(false).unwrap());
    let cp1 = chain.next(20, ext1).unwrap();
    b.seal().unwrap();
    let mut ext2 = json!({"trigger": "stop", "segment_seq": 2});
    merge(&mut ext2, b.commitment(false).unwrap());
    let cp2 = chain.next(30, ext2).unwrap();
    (b, vec![cp0, cp1, cp2])
}

fn lines_of(dir: &Path) -> Vec<String> {
    std::fs::read_to_string(dir.join(FRAMES_FILE))
        .unwrap()
        .lines()
        .map(String::from)
        .collect()
}

fn put(dir: &Path, lines: &[String]) {
    std::fs::write(
        dir.join(FRAMES_FILE),
        lines.iter().map(|s| format!("{s}\n")).collect::<String>(),
    )
    .unwrap();
}

fn rebuild(cps: &[Value]) -> Vec<Value> {
    let mut prev = GENESIS.to_string();
    let mut out = vec![];
    for c in cps {
        let mut c = c.clone();
        c["frames_hash"] = json!(ev::jcs_sha(&c["qorcap"]));
        c["prev"] = json!(prev);
        let body = json!({
            "seq": c["seq"],
            "t_ms": c["t_ms"],
            "frames_hash": c["frames_hash"],
            "input_hash": c["input_hash"],
            "prev": c["prev"],
        });
        let h = ev::jcs_sha(&body);
        c["hash"] = json!(h);
        prev = h;
        out.push(c);
    }
    out
}

#[test]
fn flag_defaults_off() {
    assert!(!flag_enabled(false, None));
    assert!(!flag_enabled(false, Some("0")));
    assert!(flag_enabled(false, Some("1")));
    assert!(flag_enabled(true, None));
    let dir = temp_dir("off");
    let b = FrameBind::open(false, Some(&dir), 30);
    assert!(!b.enabled());
    assert!(b.commitment(true).is_none());
    assert!(!dir.join(FRAMES_FILE).exists());
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn tampering_any_sidecar_line_breaks_checkpoint_verification() {
    let dir = temp_dir("tamper");
    let (_b, cps) = seal_two(&dir);
    let ok = check_dir(&dir, &cps);
    assert!(ok.errors.is_empty(), "{:?}", ok.errors);
    assert!(ok.frame_claim, "{:?}", ok.frame_reason);
    assert_eq!(ok.frame_status, "verified");
    assert_eq!(ok.tail, "sealed");
    let original = std::fs::read(dir.join(FRAMES_FILE)).unwrap();
    let lines = sidecar_lines(&original);
    assert!(lines.len() >= 4, "header plus frames");
    for i in 0..lines.len() {
        let mut broken = original.clone();
        let start = lines[i].as_ptr() as usize - original.as_ptr() as usize;
        let at = start + lines[i].len() / 2;
        broken[at] ^= 0x01;
        std::fs::write(dir.join(FRAMES_FILE), &broken).unwrap();
        let report = check_dir(&dir, &cps);
        assert!(!report.frame_claim, "line {i} still claimed");
        assert!(!report.errors.is_empty(), "line {i} did not fail verification");
        assert_eq!(report.frame_status, "broken");
    }
    std::fs::write(dir.join(FRAMES_FILE), &original).unwrap();
    let mut text = String::from_utf8(original.clone()).unwrap();
    assert!(text.contains("\"mono_ms\":1120"));
    text = text.replacen("\"mono_ms\":1120", "\"mono_ms\":1121", 1);
    std::fs::write(dir.join(FRAMES_FILE), text.as_bytes()).unwrap();
    let retimed = check_dir(&dir, &cps);
    assert!(!retimed.frame_claim);
    assert!(retimed.errors.iter().any(|e| e.contains("does not match")));
    std::fs::write(dir.join(FRAMES_FILE), &original).unwrap();
    let mut extra = original.clone();
    extra.extend_from_slice(b"{\"type\":\"frame\"}\n");
    std::fs::write(dir.join(FRAMES_FILE), &extra).unwrap();
    let added = check_dir(&dir, &cps);
    assert!(!added.frame_claim);
    assert!(added
        .errors
        .iter()
        .any(|e| e.contains("added") || e.contains("unsealed")));
    let dropped = original[..original.len().saturating_sub(2)].to_vec();
    std::fs::write(dir.join(FRAMES_FILE), &dropped).unwrap();
    let short = check_dir(&dir, &cps);
    assert!(!short.frame_claim && !short.errors.is_empty());
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn missing_sidecar_degrades_to_no_frame_claim() {
    let dir = temp_dir("missing");
    let mut chain = CheckpointChain::default();
    let old = vec![chain.next(5, json!({"trigger": "segment"})).unwrap()];
    let absent = check_dir(&dir, &old);
    assert!(absent.errors.is_empty(), "{:?}", absent.errors);
    assert_eq!(absent.frame_status, "missing");
    assert!(!absent.frame_claim);
    assert!(!absent.reader_claim);
    assert!(absent.skew.is_empty());
    std::fs::write(
        dir.join(FRAMES_FILE),
        "{\"type\":\"frame\",\"frame\":0,\"mono_ms\":1}\n",
    )
    .unwrap();
    let stray = check_dir(&dir, &old);
    assert!(
        stray.errors.is_empty(),
        "an uncommitted sidecar must not fail the match: {:?}",
        stray.errors
    );
    assert_eq!(stray.frame_status, "stripped");
    assert!(!stray.frame_claim);
    assert!(stray.skew.is_empty());

    std::fs::remove_file(dir.join(FRAMES_FILE)).unwrap();
    let (_b, cps) = seal_two(&dir);
    std::fs::remove_file(dir.join(FRAMES_FILE)).unwrap();
    let gone = check_dir(&dir, &cps);
    assert!(!gone.frame_claim);
    assert_eq!(gone.frame_status, "broken");
    assert!(gone.errors.iter().any(|e| e.contains("missing")));
    assert!(gone.skew.is_empty());
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn timing_skew_between_clip_time_and_mono_ms_is_measurable_from_the_sidecar() {
    let rows = vec![
        FrameRow {
            seg: 1,
            frame: 0,
            mono_ms: 5_000,
            pts: 0,
            au_sha256: "a".repeat(64),
        },
        FrameRow {
            seg: 1,
            frame: 30,
            mono_ms: 6_120,
            pts: 90_000,
            au_sha256: "b".repeat(64),
        },
        FrameRow {
            seg: 2,
            frame: 90,
            mono_ms: 8_719,
            pts: 270_000,
            au_sha256: "c".repeat(64),
        },
    ];
    let skew = measure_skew(&rows);
    assert_eq!(skew[0].skew_ms, 0);
    assert_eq!(skew[1].clip_ms, 1_000);
    assert_eq!(skew[1].skew_ms, 120);
    assert_eq!(skew[2].clip_ms, 3_000);
    assert_eq!(skew[2].skew_ms, 719);

    let points = [(0i64, 5_000u64), (300, 15_000u64)];
    let against = measure_skew_against_clip_clock(&rows, &points);
    assert_eq!(against[1].clip_ms, 6_000);
    assert_eq!(against[1].skew_ms, 120);
    assert_eq!(against[2].clip_ms, 8_000);
    assert_eq!(against[2].skew_ms, 719);

    let dir = temp_dir("skew");
    let (_b, cps) = seal_two(&dir);
    let report = check_dir(&dir, &cps);
    assert!(report.frame_claim, "{:?}", report.errors);
    let by_frame: Vec<(i64, i64)> = report.skew.iter().map(|s| (s.frame, s.skew_ms)).collect();
    assert!(by_frame.contains(&(30, 120)), "{by_frame:?}");
    assert!(by_frame.contains(&(90, 719)), "{by_frame:?}");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn readers_hash_is_committed_only_by_the_first_checkpoint() {
    let dir = temp_dir("readers");
    let b = FrameBind::open(true, Some(&dir), 30);
    let sha = b.install_readers(&manifest()).unwrap();
    let file = std::fs::read(dir.join(READERS_FILE)).unwrap();
    assert_eq!(ev::sha256_hex(&file), sha);
    assert!(b.claim_opening());
    let mut ext = json!({"trigger": "start"});
    merge(&mut ext, b.commitment(true).unwrap());
    assert_eq!(ext["readers"]["sha256"], sha);
    let mut chain = CheckpointChain::default();
    let cp0 = chain.next(1, ext).unwrap();
    let mut later = json!({"trigger": "segment"});
    merge(&mut later, b.commitment(false).unwrap());
    assert!(later.get("readers").is_none());
    let cp1 = chain.next(2, later).unwrap();
    let ok = check_dir(&dir, &[cp0.clone(), cp1]);
    assert!(ok.reader_claim, "{:?}", ok.errors);
    assert_eq!(ok.reader_status, "verified");
    assert_eq!(ok.frame_status, "verified");
    assert!(!ok.frame_claim, "a header with no sealed frames is not a frame-time claim");
    assert_eq!(ok.tail, "open");

    let mut flipped = file.clone();
    let last = flipped.len() - 2;
    flipped[last] ^= 0x01;
    std::fs::write(dir.join(READERS_FILE), &flipped).unwrap();
    let bad = check_dir(&dir, &[cp0.clone()]);
    assert!(!bad.reader_claim);
    assert_eq!(bad.reader_status, "broken");
    assert!(bad.errors.iter().any(|e| e.contains("readers.json")));

    std::fs::remove_file(dir.join(READERS_FILE)).unwrap();
    let missing = check_dir(&dir, &[cp0]);
    assert!(!missing.reader_claim);
    assert!(missing.errors.iter().any(|e| e.contains("missing")));

    let unbound_dir = temp_dir("readers-unbound");
    let u = FrameBind::open(true, Some(&unbound_dir), 30);
    let mut ext = json!({"trigger": "start"});
    merge(&mut ext, u.commitment(true).unwrap());
    assert_eq!(ext["readers"]["state"], "unbound");
    let cp = CheckpointChain::default().next(1, ext).unwrap();
    let report = check_dir(&unbound_dir, &[cp]);
    assert!(!report.reader_claim);
    assert_eq!(report.reader_status, "missing");
    assert!(report.errors.is_empty(), "{:?}", report.errors);
    assert_eq!(report.frame_status, "verified");
    let _ = std::fs::remove_dir_all(&dir);
    let _ = std::fs::remove_dir_all(&unbound_dir);
}

#[test]
fn readers_manifest_rejects_key_paths_and_late_binding() {
    let mut bad = manifest();
    bad["readers"][0]["files"][0]["path"] = json!("keys/referee.key");
    assert!(validate_readers(&bad).is_err());
    bad["readers"][0]["files"][0]["path"] = json!("../sidecar/qorcap_standard.py");
    assert!(validate_readers(&bad).is_err());
    let mut extra = manifest();
    extra["reader_verified_by_referee"] = json!(true);
    assert!(
        validate_readers(&extra).is_err(),
        "closed schema rejects a forged referee claim"
    );
    let dir = temp_dir("late");
    let b = FrameBind::open(true, Some(&dir), 30);
    assert!(b.claim_opening());
    let err = b.install_readers(&manifest()).unwrap_err();
    assert!(err.contains("after the first checkpoint"));
    assert!(!dir.join(READERS_FILE).exists());
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn readers_write_failure_leaves_checkpoint_zero_unbound() {
    let dir = temp_dir("readers-write-fail");
    let b = FrameBind::open(true, Some(&dir), 30);
    // A directory where the file should land makes the rename fail. The hash must not stick.
    std::fs::create_dir(dir.join(READERS_FILE)).unwrap();
    let err = b.install_readers(&manifest()).unwrap_err();
    assert!(err.contains("cannot write"), "{err}");
    let committed = b.commitment(true).unwrap();
    assert_eq!(committed["readers"]["state"], "unbound");
    assert!(committed["readers"].get("sha256").is_none());
    let mut ext = json!({"trigger": "start", "segment_seq": 0});
    merge(&mut ext, committed);
    let cp = CheckpointChain::default().next(1, ext).unwrap();
    let report = check_dir(&dir, &[cp]);
    assert_eq!(report.reader_status, "missing");
    assert!(!report.reader_claim);
    assert_ne!(report.reader_status, "verified");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn write_failure_does_not_advance_the_hash_and_is_not_a_claim() {
    let dir = temp_dir("write-fail");
    let b = FrameBind::open(true, Some(&dir), 30);
    b.append_frame(1, 0, 0, 0, b"au").unwrap();
    let before = b.commitment(true).unwrap();
    let n = before["frame_bind"]["n"].as_u64().unwrap();
    let head = before["frame_bind"]["head"].as_str().unwrap().to_string();
    if std::path::Path::new("/dev/full").exists() {
        b.retarget_writes_for_test(std::path::Path::new("/dev/full"));
        let err = b.append_frame(1, 1, 33, 3000, b"next").unwrap_err();
        assert!(err.contains("no frame claim"), "{err}");
        let after = b.commitment(true).unwrap();
        assert_eq!(after["frame_bind"]["state"], "failed");
        assert_eq!(after["frame_bind"]["n"], n);
        assert_eq!(after["frame_bind"]["head"], head);
        let mut ext = json!({"trigger": "start", "segment_seq": 1});
        merge(&mut ext, after);
        let cp = CheckpointChain::default().next(1, ext).unwrap();
        let report = check_dir(&dir, &[cp]);
        assert_eq!(report.frame_status, "broken");
        assert!(!report.frame_claim);
        assert!(
            report.errors.is_empty(),
            "an honest failed write is not a hash mismatch: {:?}",
            report.errors
        );
    }
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn encoded_video_samples_are_hashed_and_audio_is_not() {
    let init = {
        let moov = super::box_of(b"moov", &[trak(1, b"vide"), trak(2, b"soun")].concat());
        [super::box_of(b"ftyp", b"iso5"), moov].concat()
    };
    assert_eq!(video_track_id(&init), Some(1));
    let v0 = b"FRAME0";
    let v1 = b"FRAME1!!";
    let audio = b"AUD";
    let tfhd_v = tfhd(1);
    let tfhd_a = tfhd(2);
    let trun_v = trun(&[v0.len() as u32, v1.len() as u32], 0);
    let trun_a = trun(&[audio.len() as u32], 0);
    let traf_v = super::box_of(b"traf", &[tfhd_v, trun_v].concat());
    let traf_a = super::box_of(b"traf", &[tfhd_a, trun_a].concat());
    let moof_len = 8 + traf_v.len() + traf_a.len();
    let video_off = moof_len as i32 + 8;
    let audio_off = video_off + (v0.len() + v1.len()) as i32;
    let trun_v = trun(&[v0.len() as u32, v1.len() as u32], video_off);
    let trun_a = trun(&[audio.len() as u32], audio_off);
    let traf_v = super::box_of(b"traf", &[tfhd(1), trun_v].concat());
    let traf_a = super::box_of(b"traf", &[tfhd(2), trun_a].concat());
    let moof = super::box_of(b"moof", &[traf_v, traf_a].concat());
    assert_eq!(moof.len(), moof_len);
    let mdat = super::box_of(
        b"mdat",
        &[v0.as_slice(), v1.as_slice(), audio.as_slice()].concat(),
    );
    let seg = [moof, mdat].concat();
    match video_samples(&seg, 1, None) {
        SampleRead::Frames(h) => {
            assert_eq!(h[0].hash, ev::sha256_hex(v0));
            assert_eq!(h[1].hash, ev::sha256_hex(v1));
            assert!(h.iter().all(|s| s.pts90.is_none()));
        }
        SampleRead::NoVideo => panic!("no video"),
        SampleRead::Unreadable(e) => panic!("{e}"),
    }
    let bad = trun_with_cto();
    let err = parse_trun(&bad, None, None).unwrap_err();
    assert!(err.contains("composition offset"));
}

#[test]
fn receipt_schema_does_not_gain_sidecar_fields() {
    let schema: Value = serde_json::from_str(include_str!("../contracts/receipt.v0.json")).unwrap();
    assert_eq!(schema["additionalProperties"], false);
    let top: Vec<&str> = schema["properties"]
        .as_object()
        .unwrap()
        .keys()
        .map(|s| s.as_str())
        .collect();
    for forbidden in ["frame_bind", "readers", "frames_hash_v2", "qorcap"] {
        assert!(!top.contains(&forbidden), "{forbidden} is a receipt field");
    }
    let cp = &schema["$defs"]["checkpoint"]["properties"];
    assert!(cp.get("frame_bind").is_none());
    assert!(cp.get("readers").is_none());
    assert!(cp.get("qorcap").is_none());
    assert_eq!(schema["$defs"]["checkpoint"]["additionalProperties"], false);
    let draft = ev::draft_receipt(
        "m",
        "home",
        "p",
        1,
        2,
        &json!({"home": 1, "away": 0, "quarter": 4, "t_ms": 2, "basis": "clock_zero"}),
        &[],
    );
    let allowed: std::collections::BTreeSet<&String> =
        schema["properties"].as_object().unwrap().keys().collect();
    for k in draft.as_object().unwrap().keys() {
        assert!(allowed.contains(k), "draft receipt gained {k}");
    }
    assert!(draft["checkpoints"].as_object().unwrap().is_empty());
}

fn trak(id: u32, kind: &[u8; 4]) -> Vec<u8> {
    let mut tkhd_p = vec![0u8; 16];
    tkhd_p[12..16].copy_from_slice(&id.to_be_bytes());
    let mut hdlr_p = vec![0u8; 12];
    hdlr_p[8..12].copy_from_slice(kind);
    let mdia = super::box_of(b"mdia", &super::box_of(b"hdlr", &hdlr_p));
    super::box_of(b"trak", &[super::box_of(b"tkhd", &tkhd_p), mdia].concat())
}

fn tfhd(track: u32) -> Vec<u8> {
    let mut p = vec![0u8; 8];
    p[0..4].copy_from_slice(&0x020000u32.to_be_bytes());
    p[4..8].copy_from_slice(&track.to_be_bytes());
    super::box_of(b"tfhd", &p)
}

fn trun(sizes: &[u32], data_offset: i32) -> Vec<u8> {
    let mut p = Vec::new();
    p.extend_from_slice(&0x201u32.to_be_bytes());
    p.extend_from_slice(&(sizes.len() as u32).to_be_bytes());
    p.extend_from_slice(&data_offset.to_be_bytes());
    for s in sizes {
        p.extend_from_slice(&s.to_be_bytes());
    }
    super::box_of(b"trun", &p)
}

fn trun_with_cto() -> Vec<u8> {
    let mut p = Vec::new();
    p.extend_from_slice(&0xA01u32.to_be_bytes());
    p.extend_from_slice(&1u32.to_be_bytes());
    p.extend_from_slice(&0i32.to_be_bytes());
    p.extend_from_slice(&4u32.to_be_bytes());
    p.extend_from_slice(&1i32.to_be_bytes());
    p
}

fn init_timed(timescale: u32) -> Vec<u8> {
    let mut tkhd = vec![0u8; 16];
    tkhd[12..16].copy_from_slice(&1u32.to_be_bytes());
    let mut hdlr = vec![0u8; 12];
    hdlr[8..12].copy_from_slice(b"vide");
    let mut mdhd = vec![0u8; 20];
    mdhd[12..16].copy_from_slice(&timescale.to_be_bytes());
    let mdia = super::box_of(
        b"mdia",
        &[
            super::box_of(b"mdhd", &mdhd),
            super::box_of(b"hdlr", &hdlr),
        ]
        .concat(),
    );
    let trak = super::box_of(b"trak", &[super::box_of(b"tkhd", &tkhd), mdia].concat());
    [
        super::box_of(b"ftyp", b"iso5"),
        super::box_of(b"moov", &trak),
    ]
    .concat()
}

fn media_pts(samples: &[&[u8]], base: u32, dur: u32) -> Vec<u8> {
    let mut tf = vec![0u8; 8];
    tf[0..4].copy_from_slice(&0x020000u32.to_be_bytes());
    tf[4..8].copy_from_slice(&1u32.to_be_bytes());
    let mut td = vec![0u8; 8];
    td[4..8].copy_from_slice(&base.to_be_bytes());
    let mk = |off: i32| {
        let mut tr = Vec::new();
        tr.extend_from_slice(&0x301u32.to_be_bytes());
        tr.extend_from_slice(&(samples.len() as u32).to_be_bytes());
        tr.extend_from_slice(&off.to_be_bytes());
        for s in samples {
            tr.extend_from_slice(&dur.to_be_bytes());
            tr.extend_from_slice(&(s.len() as u32).to_be_bytes());
        }
        super::box_of(
            b"moof",
            &super::box_of(
                b"traf",
                &[
                    super::box_of(b"tfhd", &tf),
                    super::box_of(b"tfdt", &td),
                    super::box_of(b"trun", &tr),
                ]
                .concat(),
            ),
        )
    };
    let moof_len = mk(0).len();
    let moof = mk(moof_len as i32 + 8);
    [moof, super::box_of(b"mdat", &samples.concat())].concat()
}

#[test]
fn a1_edits_fail_but_tail_truncation_is_reported_as_an_open_prefix() {
    let d = temp_dir("a1");
    let cps = seal_two(&d).1;
    let orig = lines_of(&d);
    assert!(check_dir(&d, &cps).frame_claim);
    let mut r = orig.clone();
    r.swap(1, 2);
    put(&d, &r);
    assert!(!check_dir(&d, &cps).frame_claim, "reorder");
    let mut r = orig.clone();
    r.remove(3);
    put(&d, &r);
    assert!(!check_dir(&d, &cps).frame_claim, "drop");
    let mut r = orig.clone();
    r.insert(2, r[1].clone());
    put(&d, &r);
    assert!(!check_dir(&d, &cps).frame_claim, "insert");
    let mut r = orig.clone();
    r[2] = r[2].replace("\"mono_ms\":1120", "\"mono_ms\":1121");
    put(&d, &r);
    assert!(!check_dir(&d, &cps).frame_claim, "retime");
    std::fs::write(
        d.join(FRAMES_FILE),
        orig.iter().map(|s| format!("{s}\r\n")).collect::<String>(),
    )
    .unwrap();
    assert!(!check_dir(&d, &cps).frame_claim, "crlf");
    let n1 = cps[1]["qorcap"]["frame_bind"]["n"].as_u64().unwrap() as usize;
    put(&d, &orig[..n1]);
    let rep = check_dir(&d, &cps[..2]);
    assert!(rep.errors.is_empty(), "{:?}", rep.errors);
    assert_eq!(rep.tail, "open");
    assert!(!rep.frame_claim, "an open prefix is not a finished frame claim");
    assert_eq!(rep.frame_status, "verified");
    let _ = std::fs::remove_dir_all(&d);
}

#[test]
fn a5_strip_is_stripped_and_frame_bind_without_readers_is_broken() {
    let d = temp_dir("a5");
    let cps = seal_two(&d).1;
    let mut naive = cps.clone();
    for c in naive.iter_mut() {
        c["qorcap"].as_object_mut().unwrap().remove("frame_bind");
    }
    assert!(ev::check_checkpoint(&naive[0], GENESIS).is_err());
    let mut s = cps.clone();
    for c in s.iter_mut() {
        let o = c["qorcap"].as_object_mut().unwrap();
        o.remove("frame_bind");
        o.remove("readers");
    }
    let s = rebuild(&s);
    let mut prev = GENESIS.to_string();
    for c in &s {
        prev = ev::check_checkpoint(c, &prev).unwrap();
    }
    let rep = check_dir(&d, &s);
    assert!(rep.errors.is_empty(), "{:?}", rep.errors);
    assert_eq!(rep.frame_status, "stripped");
    assert_eq!(rep.reader_status, "stripped");
    assert!(!rep.frame_claim && !rep.reader_claim);
    let mut r = cps.clone();
    r[0]["qorcap"].as_object_mut().unwrap().remove("readers");
    let r = rebuild(&r);
    let rep = check_dir(&d, &r);
    assert_eq!(rep.frame_status, "broken");
    assert_eq!(rep.reader_status, "broken");
    assert!(!rep.frame_claim);
    assert!(rep.errors.iter().any(|e| e.contains("readers is absent")));
    let _ = std::fs::remove_dir_all(&d);
}

#[test]
fn a3_garbage_rows_are_not_a_frame_claim() {
    let d = temp_dir("a3");
    let b = FrameBind::open(true, Some(&d), 30);
    b.append_frame(99, 500, 10_000_000, 0, b"anything").unwrap();
    b.append_frame(1, 3, 0, 3000, b"x").unwrap();
    let mut ext = json!({"trigger": "segment", "segment_seq": 1});
    merge(&mut ext, b.commitment(true).unwrap());
    let cps = vec![CheckpointChain::default().next(1, ext).unwrap()];
    let rep = check_dir(&d, &cps);
    assert!(!rep.frame_claim);
    assert_eq!(rep.frame_status, "broken");
    assert!(!rep.errors.is_empty(), "garbage rows must fail closed");
    assert!(rep.skew.is_empty());
    let _ = std::fs::remove_dir_all(&d);
}

#[test]
fn a2_dropped_sample_does_not_shift_mono_ms() {
    let d = temp_dir("a2");
    let b = FrameBind::open(true, Some(&d), 30);
    b.on_segment(0, "init", &init_timed(90_000));
    for (i, t) in [0u64, 33, 66, 100].iter().enumerate() {
        b.observe_ts(&crate::frameclock::video_ts_packet(0x100, i as u64 * 3000), *t);
    }
    b.on_segment(1, "media", &media_pts(&[b"F1", b"F2", b"F3"], 3000, 3000));
    let text = std::fs::read_to_string(d.join(FRAMES_FILE)).unwrap();
    assert!(
        !text.contains("align_gap"),
        "matched PTS values are timed frames:\n{text}"
    );
    assert!(text.contains("\"mono_ms\":33"), "{text}");
    assert!(text.contains("\"mono_ms\":66"), "{text}");
    assert!(text.contains("\"mono_ms\":100"), "{text}");
    assert!(
        !text.contains("\"mono_ms\":0"),
        "the dropped arrival must not stamp the first sample:\n{text}"
    );
    b.observe_ts(&crate::frameclock::video_ts_packet(0x100, 12_000), 200);
    b.on_segment(2, "media", &media_pts(&[b"F4", b"ORPHAN"], 15_000, 3000));
    let text = std::fs::read_to_string(d.join(FRAMES_FILE)).unwrap();
    assert!(
        text.contains("align_gap"),
        "a sample PTS with no arrival is a gap, not a shifted time:\n{text}"
    );
    let _ = std::fs::remove_dir_all(&d);
}

#[test]
fn a4_readers_post_cannot_change_after_claim_and_extra_keys_are_rejected() {
    let d = temp_dir("a4");
    let b = FrameBind::open(true, Some(&d), 30);
    let m = |s: &str| {
        let mut v = manifest();
        v["readers"][0]["files"][0]["sha256"] = json!(s.repeat(32));
        v["readers"][0]["files"].as_array_mut().unwrap().pop();
        v
    };
    b.install_readers(&m("ab")).unwrap();
    let sha2 = b.install_readers(&m("cd")).unwrap();
    assert!(b.claim_opening());
    assert_eq!(b.commitment(true).unwrap()["readers"]["sha256"], json!(sha2));
    assert!(b.install_readers(&m("ef")).is_err());
    let mut x = m("ab");
    x["reader_verified_by_referee"] = json!(true);
    assert!(validate_readers(&x).is_err());
    let _ = std::fs::remove_dir_all(&d);
}

fn engine(dir: &Path, manifest_path: Option<&Path>) -> std::sync::Arc<crate::engine::Engine> {
    use clap::Parser;
    let d = dir.to_string_lossy().into_owned();
    let mut args = vec![
        "q",
        "record",
        "--device",
        "testpattern",
        "--bundle",
        &d,
        "--no-controller",
        "--frame-bind",
    ];
    let shown;
    if let Some(p) = manifest_path {
        shown = p.to_string_lossy().into_owned();
        args.push("--readers-manifest");
        args.push(&shown);
    }
    let crate::cli::Cmd::Record(r) = crate::cli::Cli::try_parse_from(args).unwrap().cmd else {
        panic!()
    };
    crate::engine::Engine::new(r, "ffmpeg-x".into())
}

fn cps_of(dir: &Path) -> Vec<Value> {
    std::fs::read_to_string(dir.join("checkpoints.jsonl"))
        .unwrap_or_default()
        .lines()
        .filter(|l| !l.is_empty())
        .map(|l| serde_json::from_str(l).unwrap())
        .collect()
}

#[test]
fn e1_opening_is_checkpoint_zero_and_a_late_readers_post_cannot_change_it() {
    let d = temp_dir("e1");
    let path = d.join("readers.input.json");
    std::fs::write(&path, serde_json::to_string(&manifest()).unwrap()).unwrap();
    let e = engine(&d, Some(&path));
    e.seal_opening();
    e.challenge(&json!({"nonce": "0123456789abcdef0123456789abcdef"}))
        .unwrap();
    let err = e
        .event(&json!({"type": "readers", "manifest": manifest()}))
        .unwrap_err();
    assert!(err.contains("cannot change"), "{err}");
    let cps = cps_of(&d);
    assert_eq!(cps[0]["qorcap"]["trigger"], "start");
    assert!(cps[0]["qorcap"]["readers"]["sha256"].is_string());
    assert!(cps.iter().any(|c| c["qorcap"]["trigger"] == "challenge"));
    assert!(cps
        .iter()
        .all(|c| c["qorcap"]["trigger"] != "challenge" || c["seq"].as_u64() != Some(0)));
    let rep = check_dir(&d, &cps);
    assert!(rep.reader_claim, "{:?}", rep.errors);
    let _ = std::fs::remove_dir_all(&d);
}

#[test]
fn e2_bundle_tail_truncation_and_missing_checkpoints_are_reported() {
    let d = temp_dir("e2");
    let e = engine(&d, None);
    e.seal_opening();
    let video = d.join("video.mp4");
    let mut all = Vec::new();
    let mut off = 0u64;
    for i in 0..3u8 {
        let seg = vec![i; 16];
        all.extend_from_slice(&seg);
        std::fs::write(&video, &all).unwrap();
        e.frame_bind
            .append_frame(i as u64, i as i64, i as u64 * 33, i as u64 * 3000, &seg)
            .unwrap();
        e.on_segment(video.to_str().unwrap(), off, &seg);
        off += 16;
    }
    e.frame_bind.seal().unwrap();
    e.checkpoint("stop", json!({}));
    let full = crate::bundle::verify(&d).unwrap();
    assert!(full.errors.is_empty(), "{:?}", full.errors);
    assert_eq!(full.frame_tail, "sealed");
    let cps = cps_of(&d);
    let keep = cps.len() - 1;
    let n = cps[keep - 1]["qorcap"]["frame_bind"]["n"].as_u64().unwrap() as usize;
    std::fs::write(
        d.join("checkpoints.jsonl"),
        cps[..keep]
            .iter()
            .map(|c| format!("{}\n", serde_json::to_string(c).unwrap()))
            .collect::<String>(),
    )
    .unwrap();
    let fl = lines_of(&d);
    put(&d, &fl[..n]);
    let r = crate::bundle::verify(&d).unwrap();
    assert!(r.errors.is_empty(), "{:?}", r.errors);
    assert_eq!(r.frame_tail, "open");
    assert_ne!(r.frame_status, "missing");
    std::fs::remove_file(d.join("checkpoints.jsonl")).unwrap();
    let r = crate::bundle::verify(&d).unwrap();
    assert_eq!(r.frame_tail, "checkpoints_missing");
    assert_eq!(r.frame_status, "broken");
    assert!(r.errors.iter().any(|e| e.contains("checkpoints.jsonl")));
    let _ = std::fs::remove_dir_all(&d);
}
