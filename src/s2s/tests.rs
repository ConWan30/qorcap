//! Synthetic fixtures only (PLAN-r10 section 13 row S1). No real session data.
use super::*;

const SID: &str = "match-20990101-000000";

fn meta() -> SessionMeta {
    SessionMeta {
        session_id: SID.into(),
        seal_status: "rehashed_match".into(),
        disposition: "ok".into(),
        in_calibration_set: false,
        holdout_reused: false,
    }
}
/// Test-only: the standby values hand-marked "frozen" plus a rehashed_match claim (meta()). In this
/// build that must still never yield COUNTED (QorProof H1); the descriptive values are what is tested.
fn frozen() -> Config {
    let mut c = Config::standby_a();
    c.status = "frozen".into();
    c
}

/// Change-only `in` rows plus 1 s hb rows, like inputs.jsonl.
struct F {
    rows: Vec<Value>,
    rx: u8,
    ry: u8,
    r2: i64,
    b: u64,
    end: u64,
}
impl F {
    fn new(end_s: u64) -> F {
        let mut f = F {
            rows: vec![
                json!({"type":"header","schema":"qorcap-inputs/v0","t_us":0,"transport":"usb-hid"}),
            ],
            rx: 128,
            ry: 128,
            r2: 0,
            b: 0,
            end: end_s * 1_000_000,
        };
        f.row(1_000);
        f
    }
    fn row(&mut self, t: u64) {
        self.rows.push(json!({"type":"in","t_us":t,"b":self.b,"lx":128,"ly":128,"rx":self.rx,"ry":self.ry,"l2":0,"r2":self.r2,"c":0,"hw":0}));
    }
    fn stick(&mut self, t: u64, rx: u8) -> &mut Self {
        self.rx = rx;
        self.row(t);
        self
    }
    /// Full R2 pull at t (one-row jump 0 -> 255) with the digital bit, release 60 ms later.
    fn tap(&mut self, t: u64) -> &mut Self {
        self.r2 = 255;
        self.b |= crate::hid::R2 as u64;
        self.row(t);
        self.r2 = 0;
        self.b &= !(crate::hid::R2 as u64);
        self.row(t + 60_000);
        self
    }
    fn done(&self) -> Vec<Value> {
        self.done_with(|_, h| h)
    }
    fn done_with(&self, edit: impl Fn(u64, Value) -> Value) -> Vec<Value> {
        let mut v = self.rows.clone();
        let mut s = 1u64;
        while s * 1_000_000 <= self.end {
            let h = json!({"type":"hb","t_us":s*1_000_000,"hb_seq":s-1,"epoch":1,"faults":0,"short_reads":0,"parse_faults":0,"stale_over_50ms":0,"max_rx_gap_us":4_100,"seq_breaks":0,"rx_delta":250});
            v.push(edit(s, h));
            s += 1;
        }
        v.sort_by_key(|r| r["t_us"].as_u64().unwrap());
        v
    }
}
const MS: u64 = 1_000;
fn go(rows: &[Value]) -> Value {
    analyze(rows, "sha-fixture", &meta(), &frozen())
}
fn outcomes(v: &Value) -> Vec<String> {
    v["anchors"]
        .as_array()
        .unwrap()
        .iter()
        .map(|a| a["outcome"].as_str().unwrap().to_string())
        .collect()
}

/// One clean onset: quiet from 1 s, flick at 3.0 s, R2 at 3.2 s.
fn one_onset() -> F {
    let mut f = F::new(6);
    f.stick(3_000 * MS, 200).tap(3_200 * MS);
    f
}

#[test]
fn clean_onset_has_bracket_and_provenance_and_is_not_counted() {
    let v = go(&one_onset().done());
    assert_eq!(v["tier"], "INFO_ONLY", "{v:#}");
    let a = &v["anchors"][0];
    assert_eq!(a["outcome"], "onset");
    assert_eq!(a["s2_s4_us"], 200_000);
    assert_eq!(a["s2_candidates"][0]["axis"], "rx");
    assert_eq!(
        a["s2_candidates"][0]["t_us_bracket"],
        json!([1_000, 3_000_000])
    );
    assert_eq!(a["s2_selected"]["s2_selection"], "s1_blind");
    assert_eq!(a["threshold_id"], v["threshold"]["threshold_id"]);
    assert_eq!(a["threshold_provenance"]["source"], "A_standby");
    assert_eq!(a["hb_span"]["epoch"], 1);
}

#[test]
fn multi_onset_keeps_every_candidate_and_selects_the_first() {
    let mut f = F::new(6);
    f.stick(2_000 * MS, 200)
        .stick(2_100 * MS, 128)
        .stick(2_400 * MS, 60)
        .stick(2_500 * MS, 128)
        .tap(2_800 * MS);
    let v = go(&f.done());
    let a = &v["anchors"][0];
    assert_eq!(a["s2_candidates"].as_array().unwrap().len(), 2, "{a:#}");
    assert_eq!(a["s2_candidates"][1]["axis"], "rx");
    assert!(a["flags"]
        .as_array()
        .unwrap()
        .contains(&json!("multi_onset")));
    assert_eq!(a["s2_s4_us"], 800_000);
    assert_eq!(v["outcome_table"]["multi_onset"], 1);
}

#[test]
fn quiet_stick_is_no_onset() {
    let mut f = F::new(6);
    f.stick(2_000 * MS, 135).tap(3_000 * MS); // d = 7 <= release
    assert_eq!(outcomes(&go(&f.done())), ["no_onset"]);
}

#[test]
fn stick_already_moving_at_window_start_is_left_censored() {
    let mut f = F::new(6);
    f.stick(1_200 * MS, 220).tap(3_000 * MS); // deflected from before R2 - W through R2
    let v = go(&f.done());
    assert_eq!(outcomes(&v), ["left_censored_stick"]);
    assert!(v["anchors"][0]["s2_s4_us"].is_null());
}

#[test]
fn short_quiet_is_not_an_onset() {
    let mut f = F::new(6);
    // released for only 50 ms (< Q = 100 ms) before the second flick
    f.stick(1_500 * MS, 220)
        .stick(3_000 * MS, 128)
        .stick(3_050 * MS, 220)
        .tap(3_200 * MS);
    assert_eq!(outcomes(&go(&f.done())), ["left_censored_stick"]);
}

#[test]
fn ramp_through_the_hysteresis_band_still_onsets() {
    let mut f = F::new(6);
    f.stick(3_000 * MS, 148)
        .stick(3_008 * MS, 170)
        .tap(3_200 * MS); // d 20 then 42
    let a = &go(&f.done())["anchors"][0];
    assert_eq!(a["outcome"], "onset");
    assert_eq!(a["s2_candidates"][0]["t_us"], 3_008_000);
    assert_eq!(
        a["s2_candidates"][0]["quiet_us"], 1_300_000,
        "quiet runs from the window start (1.7 s) to the first rise above release"
    );
}

#[test]
fn hb_gap_before_onset_masks() {
    let f = one_onset();
    let bad_gap = f.done_with(|s, mut h| {
        if s == 2 {
            h["max_rx_gap_us"] = json!(80_000);
        }
        h
    });
    assert_eq!(outcomes(&go(&bad_gap)), ["masked:hb_gap_in_window"]);
    let missing = f.done_with(|s, mut h| {
        if s >= 3 {
            h["hb_seq"] = json!(s);
        }
        h
    }); // hb_seq jumps 1 -> 3
    assert_eq!(outcomes(&go(&missing)), ["masked:hb_gap_in_window"]);
    let fault = f.done_with(|s, mut h| {
        if s == 3 {
            h["faults"] = json!(1);
        }
        h
    });
    assert_eq!(outcomes(&go(&fault)), ["masked:hb_gap_in_window"]);
    let breaks = f.done_with(|s, mut h| {
        if s == 3 {
            h["seq_breaks"] = json!(2);
        }
        h
    });
    assert_eq!(outcomes(&go(&breaks)), ["masked:report_seq_break"]);
    let unrecorded = f.done_with(|_, mut h| {
        h.as_object_mut().unwrap().remove("seq_breaks");
        h
    });
    let v = go(&unrecorded);
    assert_eq!(outcomes(&v), ["onset"]);
    assert!(v["anchors"][0]["flags"]
        .as_array()
        .unwrap()
        .contains(&json!("seq_breaks_unrecorded")));
    // a gap outside the span leaves it alone
    let late = f.done_with(|s, mut h| {
        if s == 6 {
            h["max_rx_gap_us"] = json!(900_000);
        }
        h
    });
    assert_eq!(outcomes(&go(&late)), ["onset"]);
}

#[test]
fn epoch_change_in_window_masks() {
    let f = one_onset();
    let rows = f.done_with(|s, mut h| {
        if s >= 3 {
            h["epoch"] = json!(2);
        }
        h
    });
    assert_eq!(outcomes(&go(&rows)), ["masked:epoch_change_in_window"]);
    let mut g = one_onset();
    g.rows.push(
        json!({"type":"status","t_us":2_500_000,"state":"unplugged","detail":"usb disconnect"}),
    );
    assert_eq!(
        outcomes(&go(&g.done())),
        ["masked:controller_status_change_in_window"]
    );
}

#[test]
fn non_usb_transport_masks() {
    let mut f = one_onset();
    f.rows[0]["transport"] = json!("bluetooth");
    assert_eq!(outcomes(&go(&f.done())), ["masked:transport_not_usb"]);
    let mut g = one_onset();
    g.rows.remove(0);
    assert_eq!(outcomes(&go(&g.done())), ["masked:transport_unknown"]);
    let mut h = one_onset();
    h.rows.push(
        json!({"type":"in","t_us":2_900_000,"transport":"bt-0x31","b":0,"rx":200,"ry":128,"r2":0}),
    );
    assert_eq!(outcomes(&go(&h.done())), ["masked:transport_not_usb"]);
}

/// The 4 ms analog throttle (src/inputs.rs ANALOG_MIN_GAP_US, re-confirmed at main 4888a63): a crossing
/// is stored only on the next report >= 4 ms after the last logged row, and a brief excursion that
/// reverts inside 4 ms is never logged. S2 is the logged row time; the bracket shows the uncertainty.
#[test]
fn throttled_crossings() {
    let mut f = F::new(6);
    f.stick(3_000 * MS, 140)
        .stick(3_005 * MS, 200)
        .tap(3_200 * MS); // d 12 logged, real crossing unlogged, d 72 logged 5 ms later
    let a = &go(&f.done())["anchors"][0];
    assert_eq!(a["s2_candidates"][0]["t_us"], 3_005_000);
    assert_eq!(a["s2_candidates"][0]["prev_row_gap_us"], 5_000);
    assert_eq!(
        a["s2_candidates"][0]["t_us_bracket"],
        json!([3_000_000, 3_005_000])
    );
    // an excursion that reverted inside the throttle left no row: no onset
    let mut g = F::new(6);
    g.stick(3_000 * MS, 130)
        .stick(3_004 * MS, 131)
        .tap(3_200 * MS);
    assert_eq!(outcomes(&go(&g.done())), ["no_onset"]);
}

#[test]
fn burst_merges_taps_and_reused_onset_is_reported_not_counted_twice() {
    let mut f = F::new(8);
    f.stick(4_000 * MS, 200);
    f.tap(4_300 * MS).tap(4_450 * MS).tap(4_600 * MS); // gaps 150 ms <= B: one burst, 3 edges
    f.tap(5_000 * MS); // 400 ms after the last edge: new burst; its window still holds the 4.0 s onset
    let v = go(&f.done());
    assert_eq!(v["counts"]["r2_edges"], 4);
    assert_eq!(v["counts"]["r2_bursts"], 2);
    assert_eq!(v["anchors"][0]["burst"]["edges_in_burst"], 3);
    assert_eq!(outcomes(&v), ["onset", "onset_reused"]);
    assert!(v["anchors"][1]["s2_s4_us"].is_null());
    assert_eq!(v["descriptive"]["s2_s4"]["n"], 1);
}

/// R10-2: a slow squeeze (no > 60 jump between consecutive rows) makes no edge, even with the bit set.
#[test]
fn slow_squeeze_makes_no_edge_and_the_bit_is_diagnostic_only() {
    let mut f = F::new(6);
    f.stick(3_000 * MS, 200);
    for (i, r2) in [30, 60, 90, 120, 150, 180, 210, 240, 255]
        .iter()
        .enumerate()
    {
        f.r2 = *r2;
        if *r2 >= 120 {
            f.b |= crate::hid::R2 as u64;
        }
        f.row(3_100 * MS + i as u64 * 8 * MS);
    }
    let v = go(&f.done());
    assert_eq!(v["counts"]["r2_edges"], 0, "{v:#}");
    assert_eq!(v["counts"]["r2_bit_rises_diagnostic"], 1);
    assert!(v["anchors"].as_array().unwrap().is_empty());
}

#[test]
fn edge_rule_matches_sidecar_trigger_edges() {
    // rows lacking an integer r2 are skipped and do not move prev; exactly > 60 between consecutive rows
    let rows = vec![
        json!({"type":"in","t_us":1,"r2":0}),
        json!({"type":"in","t_us":2,"r2":60}),
        json!({"type":"in","t_us":3}),
        json!({"type":"in","t_us":4,"r2":121}),
        json!({"type":"hb","t_us":5,"r2":255}),
        json!({"type":"in","t_us":6,"r2":0}),
        json!({"type":"in","t_us":7,"r2":61}),
    ];
    assert_eq!(r2_edges(&rows, 60), vec![4, 7]);
    assert_eq!(
        bursts(&[0, 250_000, 500_001, 600_000], 250_000),
        vec![(0, 2), (500_001, 2)]
    );
}

/// The selector has no S1 input. Injecting or moving any screen-side row or field changes nothing.
#[test]
fn s1_perturbation_invariance() {
    let base = one_onset().done();
    let want = serde_json::to_string(&go(&base)).unwrap();
    for s1_t in [
        1_700_000u64,
        2_000_000,
        2_999_999,
        3_000_001,
        3_150_000,
        3_200_000,
    ] {
        let mut rows = base.clone();
        rows.push(json!({"type":"s1","t_us":s1_t,"frame_idx":s1_t / 16_667,"conf":0.9,"tier":"ESTIMATED"}));
        rows.push(json!({"type":"frame","t_us":s1_t,"file":"video.mp4","frame":7,"pts_90k":0}));
        for r in rows.iter_mut().filter(|r| r["type"] == "in") {
            r["s1_frame_idx"] = json!(s1_t / 16_667);
            r["s1_link"] = json!({"t_us": s1_t});
        }
        rows.sort_by_key(|r| r["t_us"].as_u64().unwrap());
        assert_eq!(
            serde_json::to_string(&go(&rows)).unwrap(),
            want,
            "S1 at {s1_t}"
        );
    }
}

/// Post-death spans are a screen read: the COUNTED headline is the same with or without them.
#[test]
fn post_death_rows_leave_the_headline_unchanged() {
    let mut f = F::new(12);
    for k in 0..5u64 {
        let t = 2_000 * MS + k * 2_000 * MS;
        f.stick(t, 200).stick(t + 300 * MS, 128).tap(t + 150 * MS);
    }
    let rows = f.done();
    let a = go(&rows);
    let mut dead = rows.clone();
    dead.push(
        json!({"type":"screen","t_us":4_000_000,"state":"post_death_span","until_us":8_000_000}),
    );
    dead.push(json!({"type":"post_death","t_us":6_000_000}));
    dead.sort_by_key(|r| r["t_us"].as_u64().unwrap());
    let b = go(&dead);
    assert_eq!(a["outcome_table"], b["outcome_table"]);
    assert_eq!(a["anchors"], b["anchors"]);
    assert_eq!(a["headline"], b["headline"]);
}

/// Full denominator table: every outcome kind is counted and they sum to the bursts.
#[test]
fn denominator_table_counts_every_outcome() {
    let mut f = F::new(20);
    f.stick(3_000 * MS, 200).tap(3_200 * MS); // onset
    f.tap(3_700 * MS); // onset_reused (same onset)
    f.stick(4_000 * MS, 128).tap(6_000 * MS); // no_onset
    f.stick(7_000 * MS, 220).tap(9_000 * MS); // left_censored_stick
    f.stick(9_500 * MS, 128)
        .stick(12_000 * MS, 200)
        .tap(12_100 * MS); // masked below
    let rows = f.done_with(|s, mut h| {
        if s == 12 {
            h["max_rx_gap_us"] = json!(70_000);
        }
        h
    });
    let v = go(&rows);
    let t = &v["outcome_table"]["outcomes"];
    assert_eq!(
        (
            t["onset"].as_u64(),
            t["onset_reused"].as_u64(),
            t["no_onset"].as_u64(),
            t["left_censored_stick"].as_u64(),
            t["masked:hb_gap_in_window"].as_u64()
        ),
        (Some(1), Some(1), Some(1), Some(1), Some(1)),
        "{v:#}"
    );
    let sum: u64 = t
        .as_object()
        .unwrap()
        .values()
        .map(|x| x.as_u64().unwrap())
        .sum();
    assert_eq!(sum, v["outcome_table"]["r2_bursts"].as_u64().unwrap());
    assert_eq!(v["outcome_table"]["gate_passing_r2_bursts"], 4);
    assert_eq!(v["descriptive"]["outcome_table"], v["outcome_table"]);
}

fn many(n: u64) -> Vec<Value> {
    let mut f = F::new(3 * n + 4);
    for k in 0..n {
        let t = 3_000 * MS + k * 3_000 * MS;
        f.stick(t, 200)
            .tap(t + 100 * MS + k * MS)
            .stick(t + 500 * MS, 128);
    }
    f.done()
}

#[test]
fn below_the_30_burst_minimum_the_headline_is_unmeasured() {
    let v = go(&many(29));
    assert_eq!(v["outcome_table"]["outcomes"]["onset"], 29);
    assert_eq!(
        v["descriptive"]["s2_s4"]["status"], "UNMEASURED",
        "{:#}",
        v["descriptive"]
    );
    assert!(v["descriptive"]["s2_s4"].get("median_us").is_none());
    let w = go(&many(30));
    assert_eq!(w["descriptive"]["s2_s4"]["status"], "ok");
    assert_eq!(w["descriptive"]["s2_s4"]["n"], 30);
    assert_eq!(w["descriptive"]["s2_s4"]["median_us"], 114_000);
    assert_eq!(w["headline"]["label"], "R2 press bursts");
}

#[test]
fn threshold_id_is_deterministic_and_hashes_rule_sessions_and_pins() {
    let a = Config::standby_a();
    assert_eq!(a.threshold_id(), Config::standby_a().threshold_id());
    assert_eq!(a.threshold_id().len(), 64);
    let mut s = a.clone();
    s.status = "frozen".into();
    assert_ne!(
        s.threshold_id(),
        a.threshold_id(),
        "freeze status is hashed: a hand-edited \"frozen\" file has a different id than the ledgered one would need"
    );
    // exclusions are add-only (QorProof r2 R-M1b): emptying the config list leaves the built-ins in
    // force, so the effective list and the id are unchanged; adding one changes the id
    let mut x = a.clone();
    x.excluded_sessions.clear();
    assert_eq!(x.effective_exclusions(), builtin_exclusions());
    assert_eq!(x.threshold_id(), a.threshold_id());
    let mut y = a.clone();
    y.excluded_sessions.push(Excluded {
        session: "999999".into(),
        reason: "test".into(),
        inputs_sha256: None,
    });
    assert_ne!(y.threshold_id(), a.threshold_id(), "the effective exclusion list is hashed");
    for change in 0..5 {
        let mut c = a.clone();
        match change {
            0 => c.params.onset_d = 27,
            1 => c.params.quiet_us = 100_001,
            2 => c.code_pins.main_sha = "0".repeat(40),
            3 => c.tuning_sessions.push(TuningSession {
                session_id: "x".into(),
                seal_status: "failed".into(),
                inputs_sha256: "00".into(),
            }),
            _ => c.heldout_sessions.push("y".into()),
        }
        assert_ne!(c.threshold_id(), a.threshold_id(), "change {change}");
    }
    // the checked-in standby config file is the same rule
    let file: Config = serde_json::from_str(
        &std::fs::read_to_string(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/docs/s2s/threshold_A.standby.json"
        ))
        .unwrap(),
    )
    .unwrap();
    assert_eq!(file, a);
}

/// R10-2 freeze gate: every pinned code reference still says what the pin expects at this checkout.
#[test]
fn code_pins_have_not_drifted() {
    for r in &Config::standby_a().code_pins.refs {
        let text =
            std::fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join(&r.path)).unwrap();
        let line: usize = r.line.parse().unwrap();
        let got = text.lines().nth(line - 1).unwrap_or("");
        assert!(
            got.contains(&r.expect),
            "{}:{} drifted: {got:?} does not contain {:?}",
            r.path,
            r.line,
            r.expect
        );
    }
    // defl here is the same encoding as humanlike's
    for (x, y) in [(128u8, 128u8), (0, 128), (255, 255), (154, 100), (102, 128)] {
        assert_eq!(
            defl(x, y),
            ((x as i16 - 128)
                .unsigned_abs()
                .max((y as i16 - 128).unsigned_abs()))
            .min(127) as u8
        );
    }
    assert_eq!(
        (defl(0, 128), defl(154, 128), defl(141, 128)),
        (127, 26, 13)
    );
}

#[test]
fn not_counted_tiers_never_reach_the_headline() {
    let rows = many(30);
    let standby = analyze(&rows, "sha", &meta(), &Config::standby_a());
    assert_eq!(standby["tier"], "INFO_ONLY");
    assert!(standby["info_only_reasons"]
        .as_array()
        .unwrap()
        .contains(&json!("stick_onset_threshold_provisional")));
    assert_eq!(standby["headline"]["status"], "NOT_COUNTED");
    assert!(standby["headline"].get("s2_s4").is_none());
    let mut m = meta();
    m.seal_status = "failed".into();
    let failed = analyze(&rows, "sha", &m, &frozen());
    assert!(failed["info_only_reasons"]
        .as_array()
        .unwrap()
        .contains(&json!("seal_failed")));
    assert_eq!(failed["headline"]["status"], "NOT_COUNTED");
    let mut h = meta();
    h.holdout_reused = true;
    let reused = analyze(&rows, "sha", &h, &frozen());
    assert_eq!(reused["tier"], DESCRIPTIVE_ONLY_HOLDOUT_REUSED);
    assert_eq!(reused["headline"]["status"], "NOT_COUNTED");
    assert_eq!(
        reused["descriptive"]["tier"],
        DESCRIPTIVE_ONLY_HOLDOUT_REUSED
    );
    assert_eq!(reused["descriptive"]["s2_s4"]["n"], 30);
    assert!(reused["anchors"]
        .as_array()
        .unwrap()
        .iter()
        .all(|a| a["tier"] == DESCRIPTIVE_ONLY_HOLDOUT_REUSED));
}

#[test]
fn excluded_and_calibration_sessions_report_no_values() {
    let rows = many(30);
    for sid in [
        "match-20261112-073204",
        "073204",
        "match-20261110-201029",
        "match-20261111-112342",
    ] {
        let mut m = meta();
        m.session_id = sid.into();
        let v = analyze(&rows, "sha", &m, &frozen());
        assert_eq!(v["status"], "excluded", "{sid}");
        assert!(v.get("anchors").is_none() && v.get("headline").is_none());
    }
    for d in ["VOID", "void", " Fail "] {
        let mut m = meta();
        m.disposition = d.into();
        assert_eq!(
            analyze(&rows, "sha", &m, &frozen())["status"],
            "excluded",
            "{d}"
        );
    }
    // listed by inputs sha256 as well as by id
    let mut cfg = frozen();
    cfg.excluded_sessions.push(Excluded {
        session: "zzz".into(),
        reason: "test".into(),
        inputs_sha256: Some("sha-x".into()),
    });
    assert_eq!(analyze(&rows, "sha-x", &meta(), &cfg)["status"], "excluded");
    let mut c = meta();
    c.in_calibration_set = true;
    let v = analyze(&rows, "sha", &c, &frozen());
    assert_eq!(v["status"], "in_calibration_set");
    assert_eq!(v["tier"], CALIBRATION_SET_TIER);
    assert_eq!(v["m4_gate_passing_r2_bursts"], 30);
    assert!(
        v.get("anchors").is_none() && v.get("headline").is_none() && v.get("descriptive").is_none()
    );
}

#[test]
fn never_writes_inside_the_bundle_and_leaves_inputs_untouched() {
    let base = std::env::temp_dir().join(format!("qorcap-s2s-{}", std::process::id()));
    let bundle = base.join("match-20990101-000000");
    let _ = std::fs::remove_dir_all(&base);
    std::fs::create_dir_all(&bundle).unwrap();
    let text: String = one_onset()
        .done()
        .iter()
        .map(|r| r.to_string() + "\n")
        .collect();
    std::fs::write(bundle.join("inputs.jsonl"), &text).unwrap();
    std::fs::write(
        bundle.join("bundle.json"),
        r#"{"match_id":"local-20990101-000000"}"#,
    )
    .unwrap();
    let before = ev::sha256_hex(&std::fs::read(bundle.join("inputs.jsonl")).unwrap());
    let listing = |d: &Path| {
        let mut v: Vec<String> = std::fs::read_dir(d)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
            .collect();
        v.sort();
        v
    };
    for bad in [
        bundle.join("s2s.json"),
        bundle.join("sub").join("..").join("s2s.json"),
    ] {
        assert!(run(&bundle, &bad, &meta(), &frozen())
            .unwrap_err()
            .contains("inside the bundle"));
    }
    std::fs::create_dir_all(bundle.join("sub")).unwrap();
    assert!(run(
        &bundle.join("inputs.jsonl"),
        &bundle.join("sub").join("x.json"),
        &meta(),
        &frozen()
    )
    .is_err());
    std::fs::remove_dir_all(bundle.join("sub")).unwrap();
    let out = base.join("s2s-report.json");
    let rep = run(&bundle, &out, &meta(), &frozen()).unwrap();
    assert_eq!(rep["session"]["inputs_sha256"], before);
    assert!(out.exists());
    assert_eq!(
        listing(&bundle),
        vec!["bundle.json".to_string(), "inputs.jsonl".to_string()]
    );
    assert_eq!(
        ev::sha256_hex(&std::fs::read(bundle.join("inputs.jsonl")).unwrap()),
        before
    );
    let _ = std::fs::remove_dir_all(&base);
}

/// Wording rule: the output never uses these words.
#[test]
fn output_wording() {
    let v = serde_json::to_string(&go(&many(30)))
        .unwrap()
        .to_lowercase()
        .replace("provenance", "");
    for w in [
        "verified",
        "proven",
        "skill",
        "reaction",
        "anti-cheat",
        "engagement",
    ] {
        assert!(!v.contains(w), "{w} in {v}");
    }
}

/// Write a synthetic bundle `base/<folder>` with `bundle.json` match_id `mid` (None = no bundle.json).
fn fixture_bundle(base: &Path, folder: &str, mid: Option<&str>, rows: &[Value]) -> PathBuf {
    let b = base.join(folder);
    std::fs::create_dir_all(&b).unwrap();
    let text: String = rows.iter().map(|r| r.to_string() + "\n").collect();
    std::fs::write(b.join("inputs.jsonl"), text).unwrap();
    if let Some(m) = mid {
        std::fs::write(b.join("bundle.json"), json!({"match_id": m}).to_string()).unwrap();
    }
    b
}
fn scratch(tag: &str) -> PathBuf {
    let base = std::env::temp_dir().join(format!("qorcap-s2s-{tag}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&base);
    std::fs::create_dir_all(&base).unwrap();
    base
}
fn no_counted_anywhere(v: &Value) {
    assert_eq!(v["counted"]["enabled"], false);
    assert_ne!(v["tier"], "COUNTED");
    assert_ne!(v["headline"]["tier"], "COUNTED");
    assert!(v
        .get("anchors")
        .and_then(|a| a.as_array())
        .is_none_or(|a| a.iter().all(|r| r["tier"] != "COUNTED")));
    let text = serde_json::to_string(v)
        .unwrap()
        .replace(COUNTED_DISABLED_NOTE, "");
    assert!(!text.contains("\"COUNTED\""), "{text}");
}

/// QorProof H1: a hand-edited "frozen" config plus --seal-status rehashed_match still yields no COUNTED.
#[test]
fn hand_frozen_config_and_rehashed_claim_never_yield_counted() {
    const _: () = assert!(!COUNTED_ENABLED, "COUNTED must stay disabled in this build");
    let base = scratch("h1");
    let b = fixture_bundle(
        &base,
        "match-20990101-000001",
        Some("local-20990101-000001"),
        &many(30),
    );
    let cfg_path = base.join("frozen.json");
    let mut j = serde_json::to_value(Config::standby_a()).unwrap();
    j["status"] = json!("frozen");
    std::fs::write(&cfg_path, j.to_string()).unwrap();
    let cfg: Config = serde_json::from_str(&std::fs::read_to_string(&cfg_path).unwrap()).unwrap();
    assert_eq!(cfg.status, "frozen");
    let rep = run(&b, &base.join("out.json"), &meta_for("000001"), &cfg).unwrap();
    no_counted_anywhere(&rep);
    assert_eq!(rep["tier"], "INFO_ONLY");
    assert_eq!(rep["info_only_reasons"], json!([COUNTED_DISABLED_REASON]));
    assert_eq!(rep["session"]["seal_status_claim"], "rehashed_match");
    assert_eq!(rep["session"]["seal_status_checked"], false);
    assert_eq!(rep["threshold"]["provenance"]["status_claim"], "frozen");
    assert_eq!(rep["threshold"]["provenance"]["status_checked"], false);
    assert_eq!(rep["headline"]["status"], "NOT_COUNTED");
    assert_eq!(rep["descriptive"]["s2_s4"]["n"], 30);
    for v in [go(&many(30)), go(&many(29)), go(&one_onset().done())] {
        no_counted_anywhere(&v);
    }
    let _ = std::fs::remove_dir_all(&base);
}

/// QorProof M1: the exclusion is bound to the bundle's own id, not the typed one.
#[test]
fn exclusion_is_bound_to_the_bundle_and_a_rename_fails_closed() {
    let base = scratch("m1");
    let rows = one_onset().done();
    let out = base.join("out.json");
    let typed = |id: &str| SessionMeta {
        session_id: id.into(),
        ..meta()
    };
    // the real 073204 bundle under a harmless typed id: refused by id check; untyped: excluded
    let ex = fixture_bundle(
        &base,
        "match-20261110-073204",
        Some("local-20261110-073204"),
        &rows,
    );
    assert!(run(&ex, &out, &typed("match-20990101-000000"), &frozen())
        .unwrap_err()
        .contains("does not match the bundle's own id"));
    let v = run(&ex, &out, &typed(""), &frozen()).unwrap();
    assert_eq!(v["status"], "excluded");
    assert_eq!(v["session"]["session_id"], "match-20261110-073204");
    for ok_typed in [
        "073204",
        "20261110-073204",
        "local-20261110-073204",
        "match-20261110-073204",
    ] {
        assert_eq!(
            run(&ex, &out, &typed(ok_typed), &frozen()).unwrap()["status"],
            "excluded",
            "{ok_typed}"
        );
    }
    // rename bypass: folder renamed, bundle.json still says 073204 -> mismatch, refused
    let renamed = fixture_bundle(
        &base,
        "match-20261110-999999",
        Some("local-20261110-073204"),
        &rows,
    );
    assert!(run(&renamed, &out, &typed("999999"), &frozen())
        .unwrap_err()
        .contains("bundle id mismatch"));
    let odd = fixture_bundle(&base, "my-copy", Some("local-20261110-073204"), &rows);
    assert!(run(&odd, &out, &typed(""), &frozen())
        .unwrap_err()
        .contains("bundle id UNKNOWN"));
    let nojson = fixture_bundle(&base, "match-20261110-111111", None, &rows);
    assert!(run(&nojson, &out, &typed(""), &frozen())
        .unwrap_err()
        .contains("bundle id UNKNOWN"));
    // bare inputs.jsonl path works the same (its folder is the bundle)
    assert!(
        run(&renamed.join("inputs.jsonl"), &out, &typed(""), &frozen())
            .unwrap_err()
            .contains("bundle id mismatch")
    );
    // a clean bundle runs, and the typed id must agree
    let good = fixture_bundle(
        &base,
        "match-20990101-000002",
        Some("local-20990101-000002"),
        &rows,
    );
    assert_eq!(
        run(&good, &out, &typed("000002"), &frozen()).unwrap()["status"],
        "ok"
    );
    assert!(run(&good, &out, &typed("000003"), &frozen()).is_err());
    let _ = std::fs::remove_dir_all(&base);
}

/// QorProof r2 R-M1b: a config can add exclusions but never remove the built-in ones.
#[test]
fn exclusions_are_add_only() {
    let base = scratch("m1b");
    let rows = one_onset().done();
    let out = base.join("out.json");
    let mut empty = serde_json::to_value(Config::standby_a()).unwrap();
    empty["excluded_sessions"] = json!([]);
    let mut omitted = serde_json::to_value(Config::standby_a()).unwrap();
    omitted.as_object_mut().unwrap().remove("excluded_sessions");
    for (what, j) in [("empty list", empty), ("field omitted", omitted)] {
        let cfg: Config = serde_json::from_value(j).unwrap();
        assert!(cfg.excluded_sessions.is_empty(), "{what}");
        assert_eq!(cfg.effective_exclusions(), builtin_exclusions(), "{what}");
        assert_eq!(cfg.threshold_id(), Config::standby_a().threshold_id(), "{what}");
        for stamp in ["073204", "201029", "153536", "112342"] {
            let b = fixture_bundle(
                &base,
                &format!("match-20261110-{stamp}"),
                Some(&format!("local-20261110-{stamp}")),
                &rows,
            );
            let v = run(&b, &out, &meta_for(""), &cfg).unwrap();
            assert_eq!(v["status"], "excluded", "{what} {stamp}");
            let _ = std::fs::remove_dir_all(&b);
        }
    }
    // an added entry is honoured and shows in the effective list
    let mut more = Config::standby_a();
    more.excluded_sessions.push(Excluded {
        session: "000777".into(),
        reason: "test".into(),
        inputs_sha256: None,
    });
    assert_eq!(more.effective_exclusions().len(), builtin_exclusions().len() + 1);
    let b = fixture_bundle(
        &base,
        "match-20990101-000777",
        Some("local-20990101-000777"),
        &rows,
    );
    assert_eq!(run(&b, &out, &meta_for(""), &more).unwrap()["status"], "excluded");
    let _ = std::fs::remove_dir_all(&base);
}

/// QorProof r2 R-M1a, KNOWN RESIDUAL: a copy of a listed bundle with BOTH the folder name and
/// bundle.json match_id re-stamped to a new consistent id is not caught by id. The content key
/// (`inputs_sha256`) would catch it, but it is None for every built-in entry (filling it needs the
/// real files, which this tool does not read). Acceptable only while every output is INFO_ONLY.
#[test]
fn restamped_copy_is_a_known_residual_while_info_only() {
    let base = scratch("m1a");
    let rows = one_onset().done();
    let out = base.join("out.json");
    let orig = fixture_bundle(
        &base,
        "match-20261110-073204",
        Some("local-20261110-073204"),
        &rows,
    );
    let v = run(&orig, &out, &meta_for(""), &frozen()).unwrap();
    assert_eq!(v["status"], "excluded");
    let copy = fixture_bundle(
        &base,
        "match-20261110-424242",
        Some("local-20261110-424242"),
        &rows,
    );
    let rep = run(&copy, &out, &meta_for(""), &frozen()).unwrap();
    assert_eq!(rep["status"], "ok", "residual: id-only exclusion misses a consistent re-stamp");
    assert_eq!(rep["tier"], "INFO_ONLY");
    no_counted_anywhere(&rep);
    assert!(builtin_exclusions().iter().all(|e| e.inputs_sha256.is_none()));
    // once a content hash is filled in, the same re-stamped copy is excluded
    let sha = rep["session"]["inputs_sha256"].as_str().unwrap().to_string();
    let mut cfg = frozen();
    cfg.excluded_sessions.push(Excluded {
        session: "073204".into(),
        reason: "synthetic: hash of the fixture's inputs.jsonl".into(),
        inputs_sha256: Some(sha),
    });
    assert_eq!(run(&copy, &out, &meta_for(""), &cfg).unwrap()["status"], "excluded");
    let _ = std::fs::remove_dir_all(&base);
}

/// QorProof M2: temp + rename, links at `out` refused, and every bundle folder protected.
#[test]
fn output_path_hardening() {
    let base = scratch("m2");
    let rows = one_onset().done();
    let b = fixture_bundle(
        &base,
        "match-20990101-000004",
        Some("local-20990101-000004"),
        &rows,
    );
    let other = fixture_bundle(
        &base,
        "match-20990101-000005",
        Some("local-20990101-000005"),
        &rows,
    );
    let m = meta_for("match-20990101-000004");
    // another bundle (and a folder below it) is refused, not only the input's
    assert!(run(&b, &other.join("s2s.json"), &m, &frozen())
        .unwrap_err()
        .contains("bundle marker"));
    std::fs::create_dir_all(other.join("deep")).unwrap();
    assert!(run(&b, &other.join("deep").join("s2s.json"), &m, &frozen())
        .unwrap_err()
        .contains("bundle marker"));
    // an unnamed folder that holds a receipt counts as a bundle too
    let loose = base.join("loose");
    std::fs::create_dir_all(&loose).unwrap();
    std::fs::write(loose.join("receipt.json"), "{}").unwrap();
    assert!(run(&b, &loose.join("s2s.json"), &m, &frozen()).is_err());
    let before = std::fs::read(other.join("inputs.jsonl")).unwrap();
    #[cfg(unix)]
    {
        let link = base.join("link.json");
        std::os::unix::fs::symlink(other.join("inputs.jsonl"), &link).unwrap();
        assert!(run(&b, &link, &m, &frozen())
            .unwrap_err()
            .contains("symlink"));
        let hard = base.join("hard.json");
        std::fs::hard_link(other.join("inputs.jsonl"), &hard).unwrap();
        assert!(run(&b, &hard, &m, &frozen())
            .unwrap_err()
            .contains("hard link"));
        // even past the checks, the rename replaces the entry instead of writing through it
        let rep = analyze(&rows, "sha", &m, &frozen());
        assert!(write_report(&b, &hard, &rep).is_err());
    }
    #[cfg(windows)]
    {
        // QorProof r2: Windows hard links are refused too (nNumberOfLinks)
        let hard = base.join("hard.json");
        std::fs::hard_link(other.join("inputs.jsonl"), &hard).unwrap();
        assert!(run(&b, &hard, &m, &frozen())
            .unwrap_err()
            .contains("hard link (2 links)"));
        let rep = analyze(&rows, "sha", &m, &frozen());
        assert!(write_report(&b, &hard, &rep).is_err());
    }
    assert_eq!(std::fs::read(other.join("inputs.jsonl")).unwrap(), before);
    // an existing regular report is replaced atomically; no temp file is left
    let out = base.join("report.json");
    std::fs::write(&out, "old").unwrap();
    run(&b, &out, &m, &frozen()).unwrap();
    assert!(std::fs::read_to_string(&out).unwrap().contains(SCHEMA));
    let leftovers: Vec<_> = std::fs::read_dir(&base)
        .unwrap()
        .filter_map(|e| e.ok())
        .filter(|e| e.file_name().to_string_lossy().contains(".tmp-"))
        .collect();
    assert!(leftovers.is_empty());
    // a bare relative inputs.jsonl has an empty bundle path: every write is refused (fail-safe)
    assert!(inside(Path::new(""), &base.join("anything.json")));
    let _ = std::fs::remove_dir_all(&base);
}
fn meta_for(id: &str) -> SessionMeta {
    SessionMeta {
        session_id: id.into(),
        ..meta()
    }
}

/// QorProof L4: nonsense configs are refused before anything runs, and pct cannot underflow.
#[test]
fn invalid_configs_are_refused() {
    assert!(Config::standby_a().validate().is_ok());
    for k in 0..5 {
        let mut c = Config::standby_a();
        match k {
            0 => c.params.min_bursts_for_headline = 0,
            1 => c.params.release_d = 26,
            2 => c.params.onset_d = 200,
            3 => c.params.window_us = c.params.quiet_us,
            _ => c.params.edge_delta = 0,
        }
        assert!(c.validate().is_err(), "case {k}");
        let base = scratch(&format!("l4-{k}"));
        let b = fixture_bundle(
            &base,
            "match-20990101-000006",
            Some("local-20990101-000006"),
            &one_onset().done(),
        );
        assert!(run(&b, &base.join("o.json"), &meta(), &c)
            .unwrap_err()
            .contains("invalid s2s config"));
        let _ = std::fs::remove_dir_all(&base);
    }
    let mut c = Config::standby_a();
    c.params.min_bursts_for_headline = 0;
    let v = analyze(&F::new(3).done(), "sha", &meta(), &c); // zero onsets: must not panic
    assert_eq!(v["descriptive"]["s2_s4"]["status"], "UNMEASURED");
}
