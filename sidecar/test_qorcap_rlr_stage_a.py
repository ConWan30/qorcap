"""RLR Stage A extractor tests.

Every row here is a SYNTHETIC injected test value, never a player measurement. No real session,
track name, gamertag or pad id is used. Expected values come from the injected press list through
an independent reference (statistics.quantiles, method="inclusive"), not from extractor internals.
"""
import hashlib
import json
import os
import statistics
from datetime import datetime, timezone
from pathlib import Path

import pytest

import qorcap_rlr_stage_a as R

FIX = Path(__file__).resolve().parent / "fixtures" / "rlr_stage_a"
R2 = 128
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class Synth:
    """Builds a synthetic qorcap-inputs/v0 file (header, status, in, hb, imu, haptic, chunk)."""

    def __init__(self, transport="usb-hid"):
        self.rows = []
        self.pending = []
        self.seq = 0
        self.seg = 0
        self.hb_seq = 0
        self.last_hb = 0
        self.add({"type": "header", "schema": "qorcap-inputs/v0", "t_us": 0, "transport": transport,
                  "clock": "synthetic", "synthetic": "injected test values, not a player measurement"})

    def add(self, row):
        self.pending.append(dict(row))
        return self

    def status(self, t, state="connected"):
        return self.add({"type": "status", "t_us": t, "state": state, "detail": "synthetic"})

    def inp(self, t, b, r2=0):
        return self.add({"type": "in", "t_us": t, "b": b, "lx": 128, "ly": 128, "rx": 128, "ry": 128,
                         "l2": 0, "r2": r2, "c": t % 256, "hw": t})

    def hb(self, t, epoch=1, **kw):
        row = {"type": "hb", "t_us": t, "hb_seq": self.hb_seq, "epoch": epoch, "rx_total": 0,
               "rx_delta": 250, "last_rx_t_us": t - 1000, "faults": 0, "max_rx_gap_us": 4000,
               "stale_over_50ms": 0, "short_reads": 0, "parse_faults": 0}
        row.update(kw)
        self.hb_seq += 1
        self.last_hb = t
        return self.add(row)

    def hbs_until(self, t_end, epoch=1, step=1_000_000, **kw):
        t = self.last_hb + step
        while t <= t_end:
            self.hb(t, epoch, **kw)
            t += step
        return self

    def seal(self):
        for r in self.pending:
            r["seg"] = self.seg
        sha = hashlib.sha256(json.dumps(self.pending, sort_keys=True).encode()).hexdigest()
        self.rows.extend(self.pending)
        self.rows.append({"type": "chunk", "seq": self.seq, "seg": self.seg, "n": len(self.pending),
                          "sha256": sha, "prev": "0" * 64, "head": "1" * 64, "t_us": 0})
        self.pending = []
        self.seq += 1
        self.seg += 1
        return self

    def presses(self, pairs, extra_bits=0):
        for rise, fall in pairs:
            self.inp(rise, R2 | extra_bits)
            if fall is not None:
                self.inp(fall, extra_bits)
        return self

    def text(self, seal=True):
        if seal and self.pending:
            self.seal()
        rows = self.rows + self.pending
        return "".join(json.dumps(r) + "\n" for r in rows)

    def write(self, path, seal=True):
        path.write_text(self.text(seal), encoding="utf-8", newline="\n")
        return path


def run(tmp_path, text_or_synth, name="out.json", completion="synthetic: sealed copy", meta=None, **kw):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    inp = src / "inputs.jsonl"
    if isinstance(text_or_synth, Synth):
        text_or_synth.write(inp)
    elif text_or_synth is not None:
        inp.write_bytes(text_or_synth.encode() if isinstance(text_or_synth, str) else text_or_synth)
    mp = None
    if meta is not None:
        mp = tmp_path / "meta.json"
        mp.write_text(json.dumps(meta), encoding="utf-8")
    outdir = tmp_path / "out"
    outdir.mkdir(exist_ok=True)
    return R.analyze(inp, outdir / name, completion, mp, now=NOW, **kw)


def ref(pairs):
    """Independent reference straight from the injected press list."""
    dw = [f - r for r, f in pairs if f is not None]
    iv = [b[0] - a[0] for a, b in zip(pairs, pairs[1:])]
    return dw, iv


def ref_q(vals):
    if len(vals) == 1:
        return vals[0], vals[0], vals[0]
    q1, med, q3 = statistics.quantiles(vals, n=4, method="inclusive")
    return q1, med, q3


def healthy(pairs, epoch=1):
    s = Synth().status(10, "connected").inp(1, 0)
    s.presses(pairs)
    s.hbs_until(max(f or r for r, f in pairs) + 1_000_000, epoch)
    return s


EXAMPLE = [(100_000, 300_000), (600_000, 900_000)]


# ---------------------------------------------------------------- brief example and quantiles

def test_brief_example_exact_values(tmp_path):
    doc = run(tmp_path, healthy(EXAMPLE))
    r = doc["result"]
    assert r["status"] == "measured"
    d, i = r["dwell"]["session_aggregate"], r["interval"]["session_aggregate"]
    assert [m["duration_us"] for m in r["moments"] if m["kind"] == "dwell"] == [200_000, 300_000]
    assert [m["duration_us"] for m in r["moments"] if m["kind"] == "interval"] == [500_000]
    assert d["n"] == 2 and d["median_ms"] == 250 and d["iqr_ms"] == 50
    assert i["n"] == 1 and i["median_ms"] == 500 and i["status"] == "single_value"
    assert r["denominators"]["r2_rises"] == 2 and r["denominators"]["r2_falls"] == 2
    assert r["denominators"]["excluded_pairs_by_reason"] == {}
    m = r["moments"][0]
    assert (m["start_t_us"], m["end_t_us"]) == (100_000, 300_000)
    assert m["start"]["sealed"] and m["trace_qualified"] and m["binding_verified"] is False


@pytest.mark.parametrize("n", [3, 4, 5, 8, 11])
def test_odd_even_lists_match_independent_reference(tmp_path, n):
    pairs = [(1_000_000 * k + 7, 1_000_000 * k + 7 + 50_000 + 13_337 * (k * k % 7)) for k in range(1, n + 1)]
    doc = run(tmp_path, healthy(pairs))
    dw, iv = ref(pairs)
    for kind, vals in (("dwell", dw), ("interval", iv)):
        agg = doc["result"][kind]["session_aggregate"]
        q1, med, q3 = ref_q(vals)
        assert agg["n"] == len(vals)
        assert agg["median_us"] == med and agg["q1_us"] == q1 and agg["q3_us"] == q3
        assert agg["iqr_ms"] == pytest.approx((q3 - q1) / 1000)


def test_quantiles_n0_n1_n2():
    z = R.quantiles([])
    assert z["n"] == 0 and z["median_ms"] is None and z["status"] == "UNMEASURED"
    one = R.quantiles([1234])
    assert one["median_us"] == 1234 and one["iqr_us"] == 0 and one["status"] == "single_value"
    two = R.quantiles([1000, 2000])
    assert (two["q1_us"], two["median_us"], two["q3_us"]) == (1250, 1500, 1750)


def test_large_integers_keep_exact_microseconds(tmp_path):
    base = 2 ** 40
    pairs = [(base + 1, base + 1 + 123_457), (base + 2_000_003, base + 2_000_003 + 98_765)]
    doc = run(tmp_path, healthy(pairs))
    durs = sorted(m["duration_us"] for m in doc["result"]["moments"] if m["kind"] == "dwell")
    assert durs == [98_765, 123_457]
    assert doc["result"]["dwell"]["session_aggregate"]["median_us"] == (98_765 + 123_457) / 2


# ---------------------------------------------------------------- edge semantics

def edges(tmp_path, s):
    r = run(tmp_path, s)["result"]
    return r, r["denominators"]


def test_initially_held_is_left_censored(tmp_path):
    s = Synth().status(10).inp(50_000, R2).inp(150_000, 0).presses([(400_000, 600_000)])
    s.hbs_until(2_000_000)
    r, d = edges(tmp_path, s)
    assert d["censoring"]["left_censored_hold"] == 1
    assert d["complete_observed_pairs"]["dwell"] == 1


def test_final_held_is_right_censored(tmp_path):
    s = Synth().status(10).inp(5, 0).presses([(100_000, 200_000), (500_000, None)]).hbs_until(2_000_000)
    r, d = edges(tmp_path, s)
    assert d["censoring"]["right_censored_hold"] == 1
    assert d["complete_observed_pairs"] == {"dwell": 1, "interval": 1}


def test_lone_fall_and_missing_rise(tmp_path):
    s = Synth().status(10).inp(5, R2).inp(100, 0).inp(200, 0).hbs_until(2_000_000)
    r, d = edges(tmp_path, s)
    assert d["r2_rises"] == 0 and d["complete_observed_pairs"]["dwell"] == 0
    assert r["dwell"]["session_aggregate"]["median_ms"] is None


def test_no_presses_and_all_idle(tmp_path):
    s = Synth().status(10)
    for t in range(0, 3_000_000, 250_000):
        s.inp(t + 1, 0)
    s.hbs_until(3_000_000)
    r, d = edges(tmp_path, s)
    assert d["r2_rises"] == 0
    assert r["dwell"]["session_aggregate"]["n"] == 0
    assert r["dwell"]["session_aggregate"]["median_ms"] is None
    assert r["status"] == "UNMEASURED"


def test_held_repeats_and_analog_changes_make_no_edges(tmp_path):
    s = Synth().status(10).inp(1, 0)
    s.inp(100_000, R2).inp(104_000, R2, r2=200).inp(108_000, R2 | 1, r2=255).inp(300_000, 0)
    s.inp(400_000, 0, r2=255).inp(404_000, 0, r2=10).inp(408_000, 0, r2=254)
    s.hbs_until(2_000_000)
    r, d = edges(tmp_path, s)
    assert d["r2_rises"] == 1 and d["r2_falls"] == 1
    assert [m["duration_us"] for m in r["moments"]] == [200_000]


def test_other_buttons_do_not_change_r2_edges(tmp_path):
    s = Synth().status(10).inp(1, 0).presses(EXAMPLE, extra_bits=1 | 2 | 256).hbs_until(2_000_000)
    r, _ = edges(tmp_path, s)
    assert r["dwell"]["session_aggregate"]["median_ms"] == 250


def test_long_idle_gap_and_c_jump_do_not_imply_loss(tmp_path):
    s = Synth().status(10).inp(1, 0).inp(100_000, R2).add(
        {"type": "in", "t_us": 30_000_000, "b": 0, "c": 3, "r2": 0}).inp(31_000_000, R2).inp(31_100_000, 0)
    s.hbs_until(32_000_000)
    r, d = edges(tmp_path, s)
    assert d["excluded_pairs_by_reason"] == {} and d["interruptions"] == {}
    assert d["qualified_pairs"]["dwell"] == 2


# ---------------------------------------------------------------- health

def test_absent_hb_is_unknown_with_counts_and_null_stats(tmp_path):
    s = Synth().status(10).inp(1, 0).presses(EXAMPLE)
    r = run(tmp_path, s)["result"]
    assert r["continuity"] == "UNKNOWN"
    d = r["denominators"]
    assert d["r2_rises"] == 2 and d["complete_observed_pairs"] == {"dwell": 2, "interval": 1}
    assert d["qualified_pairs"] == {"dwell": 0, "interval": 0}
    assert d["excluded_pairs_by_reason"] == {"dwell:continuity_unknown_no_hb": 2,
                                             "interval:continuity_unknown_no_hb": 1}
    agg = r["dwell"]["session_aggregate"]
    assert agg["median_ms"] is None and agg["iqr_ms"] is None and agg["status"] == "UNMEASURED"
    assert agg["reason"] == "continuity_unknown_no_hb"
    assert all(m["epoch"] == "UNKNOWN" and m["health_status"] == "excluded" for m in r["moments"])


@pytest.mark.parametrize("field,value,reason", [
    ("rx_delta", 0, "hb_no_receive"),
    ("faults", 1, "hb_fault_or_stale"),
    ("short_reads", 2, "hb_fault_or_stale"),
    ("parse_faults", 1, "hb_fault_or_stale"),
    ("stale_over_50ms", 1, "hb_fault_or_stale"),
    ("max_rx_gap_us", 50_001, "hb_rx_gap_over_limit"),
    ("last_rx_t_us", None, "hb_missing_receive_time"),
])
def test_bad_hb_in_the_span_excludes(tmp_path, field, value, reason):
    s = Synth().status(10).inp(1, 0).presses([(100_000, 1_500_000)])
    s.hb(1_000_000, 1, **{field: value}).hb(2_000_000, 1)
    r = run(tmp_path, s)["result"]
    assert r["denominators"]["excluded_pairs_by_reason"] == {f"dwell:{reason}": 1}
    assert r["dwell"]["session_aggregate"]["median_ms"] is None


def test_intermediate_health_matters_not_only_endpoints(tmp_path):
    s = Synth().status(10).inp(1, 0).presses([(100_000, 3_500_000)])
    s.hb(1_000_000).hb(2_000_000, faults=3).hb(3_000_000).hb(4_000_000)
    r = run(tmp_path, s)["result"]
    assert r["denominators"]["excluded_pairs_by_reason"] == {"dwell:hb_fault_or_stale": 1}


def test_uncovered_tail_is_not_extrapolated(tmp_path):
    s = Synth().status(10).inp(1, 0).hb(1_000_000).presses([(1_100_000, 1_300_000)])
    r = run(tmp_path, s)["result"]
    assert r["denominators"]["excluded_pairs_by_reason"] == {"dwell:right_tail_uncovered_by_hb": 1}


def test_uncovered_first_span(tmp_path):
    s = Synth().status(10).inp(1, 0).presses([(100_000, 300_000)])
    s.hb_seq = 5
    s.hb(1_000_000).hb(2_000_000)
    r = run(tmp_path, s)["result"]
    assert r["denominators"]["excluded_pairs_by_reason"] == {"dwell:left_uncovered_by_hb": 1}


def test_missing_hb_seq_excludes(tmp_path):
    s = Synth().status(10).inp(1, 0).presses([(100_000, 2_500_000)]).hb(1_000_000)
    s.hb_seq += 1
    s.hb(2_000_000).hb(3_000_000)
    r = run(tmp_path, s)["result"]
    assert r["denominators"]["excluded_pairs_by_reason"] == {"dwell:hb_seq_gap": 1}


def test_epoch_change_never_pairs_across(tmp_path):
    s = Synth().status(10).inp(1, 0).presses([(100_000, 1_500_000)]).hb(1_000_000, 1).hb(2_000_000, 2)
    r = run(tmp_path, s)["result"]
    assert r["denominators"]["excluded_pairs_by_reason"] == {"dwell:epoch_change_or_unknown": 1}


def test_per_epoch_stats_and_denominators(tmp_path):
    s = Synth().status(10).inp(1, 0).presses([(100_000, 300_000), (500_000, 700_000)]).hbs_until(1_000_000, 1)
    s.status(1_200_000, "unplugged").status(1_300_000, "connected")
    s.inp(1_400_000, 0).presses([(1_500_000, 1_900_000)]).hbs_until(3_000_000, 2)
    r = run(tmp_path, s)["result"]
    pe = r["dwell"]["per_epoch"]
    assert set(pe) == {"1", "2"}
    assert pe["1"]["n"] == 2 and pe["2"]["n"] == 1 and pe["2"]["median_ms"] == 400
    assert r["denominators"]["censoring"]["dwell_censored_at_status"] == 0
    iv = [m for m in r["moments"] if m["kind"] == "interval"]
    assert [m["duration_us"] for m in iv] == [400_000]


def test_multi_epoch_session_aggregate_is_not_pooled(tmp_path):
    s = Synth().status(10).inp(1, 0).presses([(100_000, 300_000)]).hbs_until(1_000_000, 1)
    s.status(1_200_000, "unplugged").status(1_300_000, "connected")
    s.inp(1_400_000, 0).presses([(1_500_000, 1_900_000)]).hbs_until(3_000_000, 2)
    r = run(tmp_path, s)["result"]
    agg = r["dwell"]["session_aggregate"]
    assert agg["status"] == "not_pooled" and agg["median_ms"] is None
    assert {k: v["n"] for k, v in r["dwell"]["per_epoch"].items()} == {"1": 1, "2": 1}
    assert r["status"] == "measured"


def test_stall_and_recovery_status_breaks_pairing(tmp_path):
    s = Synth().status(10).inp(1, 0).inp(100_000, R2).status(200_000, "stalled")
    s.status(300_000, "connected").inp(400_000, 0).presses([(500_000, 600_000)]).hbs_until(2_000_000)
    r = run(tmp_path, s)["result"]
    d = r["denominators"]
    assert d["censoring"]["dwell_censored_at_status"] == 1
    assert d["complete_observed_pairs"] == {"dwell": 1, "interval": 0}
    assert r["source"]["status_stretches"] == 4


# ---------------------------------------------------------------- addendum shapes

def test_multi_status_reconnect_never_pairs_across_boundary(tmp_path):
    s = Synth().status(10).inp(1, 0).inp(100_000, R2)
    s.status(150_000, "unplugged").status(160_000, "connected").inp(170_000, 0)
    s.inp(200_000, R2).status(250_000, "missing").status(260_000, "connected").inp(270_000, R2).inp(300_000, 0)
    r = run(tmp_path, s)["result"]
    d = r["denominators"]
    assert d["complete_observed_pairs"] == {"dwell": 0, "interval": 0}
    assert d["censoring"]["dwell_censored_at_status"] == 2
    assert d["censoring"]["left_censored_hold"] == 1
    assert r["source"]["status_rows"] == 5


def test_empty_input_header_status_chunk_only(tmp_path):
    s = Synth().status(10).status(20, "missing")
    r = run(tmp_path, s)["result"]
    assert r["source"]["input_rows"] == 0
    assert r["denominators"]["r2_rises"] == 0
    assert r["dwell"]["session_aggregate"]["median_ms"] is None
    assert r["status"] == "UNMEASURED" and r["continuity"] == "UNKNOWN"


def test_missing_input_file_is_a_clean_rejection(tmp_path):
    doc = run(tmp_path, None)
    assert doc["result"] == {"status": "rejected", "reject_reason": "input_missing", "detail": ""}
    assert (tmp_path / "out" / "out.json").is_file()


def test_unknown_row_types_are_skipped_and_counted(tmp_path):
    s = Synth().status(10).inp(1, 0)
    s.add({"type": "imu", "t_us": 50, "present": True, "rate_hz": 250})
    s.inp(100_000, R2).add({"type": "haptic", "t_us": 150_000}).add({"type": "future_kind", "t_us": 160_000})
    s.inp(300_000, 0).inp(600_000, R2).inp(900_000, 0).hbs_until(2_000_000)
    r = run(tmp_path, s)["result"]
    assert r["source"]["skipped_row_types"] == {"imu": 1, "haptic": 1, "future_kind": 1}
    assert r["dwell"]["session_aggregate"]["median_ms"] == 250


def test_header_schema_transport_and_unknown_capture_version(tmp_path):
    r = run(tmp_path, healthy(EXAMPLE))["result"]
    assert r["source"]["schema"] == "qorcap-inputs/v0" and r["source"]["transport"] == "usb-hid"
    assert r["source"]["capture_version"] == "UNKNOWN"


def test_no_unqualified_tier_exists(tmp_path):
    s = Synth().status(10).inp(1, 0).presses(EXAMPLE)
    doc = run(tmp_path, s)
    blob = json.dumps(doc)
    assert "unqualified" not in blob
    assert doc["eligibility"]["evidence_tier"] == "private_descriptive"
    assert all(v["median_ms"] is None for k in ("dwell", "interval")
               for v in [doc["result"][k]["session_aggregate"]] + list(doc["result"][k]["per_epoch"].values()))


# ---------------------------------------------------------------- rows and structure

@pytest.mark.parametrize("bad,reason", [
    ({"type": "in", "t_us": "5", "b": 0}, "invalid_input_row"),
    ({"type": "in", "t_us": 5.5, "b": 0}, "invalid_input_row"),
    ({"type": "in", "t_us": 250_000, "b": True}, "invalid_input_row"),
    ({"type": "in", "t_us": 250_000}, "invalid_input_row"),
])
def test_invalid_rows_interrupt(tmp_path, bad, reason):
    s = Synth().status(10).inp(1, 0).inp(100_000, R2).add(bad).inp(300_000, 0).hbs_until(2_000_000)
    d = run(tmp_path, s)["result"]["denominators"]
    assert d["interruptions"] == {reason: 1}
    assert d["complete_observed_pairs"]["dwell"] == 0
    assert d["censoring"]["dwell_censored_at_interruption"] == 1


def test_reorder_duplicate_and_contradiction_are_not_repaired(tmp_path):
    s = Synth().status(10).inp(1, 0).inp(100_000, R2).inp(90_000, 0)
    s.inp(200_000, R2).inp(200_000, R2).inp(300_000, R2).inp(300_000, 0).inp(400_000, 0).hbs_until(2_000_000)
    d = run(tmp_path, s)["result"]["denominators"]
    assert d["interruptions"] == {"reordered_or_reset_t_us": 1, "duplicate_same_time_row": 1,
                                  "same_time_contradiction": 1}
    assert d["complete_observed_pairs"]["dwell"] == 0


@pytest.mark.parametrize("text,reason", [
    ('{"type":"header","schema":"qorcap-inputs/v0","t_us":0}\n{"type":"in","t_us":1,', "truncated_input"),
    ('{"type":"header","schema":"qorcap-inputs/v0","t_us":0}\nnot json\n', "malformed_jsonl"),
    ('{"type":"header","schema":"qorcap-inputs/v0","t_us":0}\n[1,2]\n', "malformed_jsonl"),
    ('{"type":"header","schema":"qorcap-inputs/v9","t_us":0}\n', "unsupported_schema"),
    ('{"type":"in","t_us":1,"b":0}\n', "missing_header"),
    ("", "empty_input"),
])
def test_bad_files_are_rejected_without_repair(tmp_path, text, reason):
    r = run(tmp_path, text)["result"]
    assert r["status"] == "rejected" and r["reject_reason"] == reason


def test_invalid_chunk_structure_abstains(tmp_path):
    s = healthy(EXAMPLE)
    text = s.text().replace('"seq": 0', '"seq": 3')
    r = run(tmp_path, text)["result"]
    assert r["status"] == "abstained" and "chunk_structure_invalid" in r["abstain_reasons"]
    assert r["chunk_binding"]["problems"][0]["problem"] == "seq_not_contiguous"
    assert r["dwell"]["session_aggregate"]["median_ms"] is None


def test_chunk_n_mismatch_is_flagged(tmp_path):
    text = healthy(EXAMPLE).text()
    lines = text.splitlines(keepends=True)
    lines[-1] = lines[-1].replace('"n": ', '"n": 1').replace('"n": 1', '"n": 9', 1)
    r = run(tmp_path, "".join(lines))["result"]
    assert any(p["problem"] == "n_mismatch" for p in r["chunk_binding"]["problems"])


def test_unsealed_tail_is_not_trace_qualified(tmp_path):
    s = Synth().status(10).inp(1, 0).presses([EXAMPLE[0]]).seal()
    s.presses([EXAMPLE[1]]).hbs_until(2_000_000)
    r = run(tmp_path, s.text(seal=False))["result"]
    assert r["source"]["unsealed_tail_records"] > 0
    moms = {(m["kind"], m["start_t_us"]): m for m in r["moments"]}
    assert moms[("dwell", 100_000)]["trace_qualified"] is True
    assert moms[("dwell", 600_000)]["trace_qualified"] is False
    assert moms[("dwell", 600_000)]["end"]["sealed"] is False


def test_cross_chunk_moment_and_index_accounting(tmp_path):
    s = Synth()                                  # header is chunk 0 record 0
    s.status(10).inp(1, 0).inp(100_000, R2).seal()  # chunk 0: header, status, in, in -> rise at index 3
    s.hb(150_000)                                # chunk 1 index 0 (hb_seq 0)
    s.inp(300_000, 0).hbs_until(2_000_000)       # fall at chunk 1 index 1
    r = run(tmp_path, s)["result"]
    m = r["moments"][0]
    assert (m["start"]["chunk_seq"], m["start"]["chunk_local_index"], m["start"]["line"]) == (0, 3, 4)
    assert (m["end"]["chunk_seq"], m["end"]["chunk_local_index"], m["end"]["line"]) == (1, 1, 7)
    assert m["start"]["chunk_declared_sha256"] != m["end"]["chunk_declared_sha256"]
    assert m["chunk_binding"] == "declared_only" and m["binding_verified"] is False


# ---------------------------------------------------------------- metadata, dispositions, completion

def test_missing_completion_evidence_withholds_stats_but_keeps_counts(tmp_path):
    r = run(tmp_path, healthy(EXAMPLE), completion=None)
    res = r["result"]
    assert res["status"] == "abstained" and res["abstain_reasons"] == ["completion_evidence_missing"]
    assert res["denominators"]["qualified_pairs"]["dwell"] == 2
    assert res["dwell"]["session_aggregate"]["median_ms"] is None
    assert r["completion_evidence"]["state"] == "unknown"


@pytest.mark.parametrize("disp", ["VOID", "FAIL", "NOT VERIFIED", "MISMATCH", "UNKNOWN", "verified", "weird"])
def test_dispositions_round_trip_and_never_upgrade(tmp_path, disp):
    meta = {"session_id": "synthetic-void-case", "mode": "practice", "receipt_disposition": disp,
            "late_disposition": "late"}
    doc = run(tmp_path, healthy(EXAMPLE), meta=meta)
    md = doc["metadata"]
    assert md["receipt_disposition"]["raw"] == disp
    assert md["late_disposition"]["normalized"] == "LATE"
    assert md["checked_by_this_tool"] is False
    e = doc["eligibility"]
    assert e["formal_counting"] == "not_eligible" and e["gold"] is False
    assert e["referee_submit"] is False and e["upload"] is False and e["standing_upgrade"] is False


def test_void_201029_stays_void(tmp_path):
    meta = {"session_id": "match-20261106-201029", "mode": "practice", "receipt_disposition": "VOID"}
    doc = run(tmp_path, healthy(EXAMPLE), meta=meta)
    assert doc["metadata"]["receipt_disposition"]["normalized"] == "VOID"
    assert doc["eligibility"]["formal_counting"] == "not_eligible"
    real = run(tmp_path, healthy(EXAMPLE), name="real.json",
               meta={"session_id": "201029", "mode": "real", "receipt_disposition": "VERIFIED",
                     "verify_disposition": "VERIFIED"})
    assert "match_201029_remains_void" in real["eligibility"]["formal_counting_reasons"]
    assert real["eligibility"]["formal_counting"] == "not_eligible"


def test_verified_real_is_still_only_not_evaluated(tmp_path):
    meta = {"mode": "real", "receipt_disposition": "VERIFIED", "verify_disposition": "VERIFIED"}
    doc = run(tmp_path, healthy(EXAMPLE), meta=meta)
    assert doc["metadata"]["receipt_disposition"]["state"] == "declared"
    assert doc["eligibility"]["formal_counting"] == "not_evaluated"
    assert doc["eligibility"]["standing_upgrade"] is False


def test_missing_metadata_is_unknown(tmp_path):
    md = run(tmp_path, healthy(EXAMPLE))["metadata"]
    assert md["session_id"]["state"] == "unknown" and md["game"]["value"] == "UNKNOWN"
    assert md["mode"]["value"] == "UNKNOWN" and md["receipt_disposition"]["normalized"] == "UNKNOWN"


def test_session_dir_reads_game_from_receipt_only(tmp_path):
    d = tmp_path / "match-practice-folder-VERIFIED"
    d.mkdir()
    healthy(EXAMPLE).write(d / "inputs.jsonl")
    (d / "receipt.json").write_text(json.dumps({"match": {"game": "synthgame", "match_id": "synthetic-1"}}))
    (d / "verify.txt").write_text("VERIFIED PASS")
    out = tmp_path / "o.json"
    rc = R.main(["--session-dir", str(d), "--out", str(out), "--completion-evidence", "synthetic"])
    assert rc == 0
    doc = json.loads(out.read_text())
    md = doc["metadata"]
    assert md["game"] == {"value": "synthgame", "state": "declared", "source": "receipt.json match.game (not checked)"}
    assert md["mode"]["value"] == "UNKNOWN"
    assert md["receipt_disposition"]["normalized"] == "UNKNOWN"
    assert md["verify_disposition"]["normalized"] == "UNKNOWN"


def test_session_dir_without_receipt_game_is_unknown(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    healthy(EXAMPLE).write(d / "inputs.jsonl")
    out = tmp_path / "o.json"
    assert R.main(["--session-dir", str(d), "--out", str(out), "--completion-evidence", "synthetic"]) == 0
    doc = json.loads(out.read_text())
    assert doc["metadata"]["game"]["value"] == "UNKNOWN" and doc["receipt_json"] == "absent"


def test_metadata_receipt_mismatch_is_kept(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    healthy(EXAMPLE).write(d / "inputs.jsonl")
    (d / "receipt.json").write_text(json.dumps({"match": {"game": "synthgame"}}))
    meta = tmp_path / "m.json"
    meta.write_text(json.dumps({"game": "othergame"}))
    out = tmp_path / "o.json"
    R.main(["--session-dir", str(d), "--out", str(out), "--completion-evidence", "x", "--metadata", str(meta)])
    doc = json.loads(out.read_text())
    assert doc["metadata"]["game"]["state"] == "mismatch"
    assert "metadata_mismatch" in doc["eligibility"]["formal_counting_reasons"]


def test_cli_missing_session_file_exits_3(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    out = tmp_path / "o.json"
    assert R.main(["--session-dir", str(d), "--out", str(out), "--completion-evidence", "x"]) == 3
    assert json.loads(out.read_text())["result"]["reject_reason"] == "input_missing"


# ---------------------------------------------------------------- read-only, paths, determinism, lineage

def _tree(p):
    return {str(x.relative_to(p)): hashlib.sha256(x.read_bytes()).hexdigest() for x in sorted(p.rglob("*")) if x.is_file()}


def test_source_bytes_and_folder_unchanged(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    healthy(EXAMPLE).write(d / "inputs.jsonl")
    (d / "receipt.json").write_text('{"match":{"game":"g"}}')
    before = _tree(d)
    R.main(["--session-dir", str(d), "--out", str(tmp_path / "o.json"), "--completion-evidence", "x"])
    assert _tree(d) == before


def test_output_refusals(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    inp = healthy(EXAMPLE).write(d / "inputs.jsonl")
    with pytest.raises(R.Rejected) as e:
        R.analyze(inp, inp, "x")
    assert e.value.reason == "output_collides_with_source"
    with pytest.raises(R.Rejected) as e:
        R.analyze(inp, d / "new.json", "x")
    assert e.value.reason == "output_inside_source_folder"
    out = tmp_path / "o.json"
    out.write_text("old")
    with pytest.raises(R.Rejected) as e:
        R.analyze(inp, out, "x")
    assert e.value.reason == "output_exists"
    assert out.read_text() == "old"
    assert R.main(["--inputs", str(inp), "--out", str(d / "x.json")]) == 2


def _can_symlink(tmp_path):
    try:
        os.symlink(tmp_path, tmp_path / "probe-link", target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False


def test_symlink_alias_into_source_is_refused(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    inp = healthy(EXAMPLE).write(d / "inputs.jsonl")
    if _can_symlink(tmp_path):
        alias = tmp_path / "alias"
        os.symlink(d, alias, target_is_directory=True)
        with pytest.raises(R.Rejected) as e:
            R.analyze(inp, alias / "o.json", "x")
        assert e.value.reason == "output_inside_source_folder"
        with pytest.raises(R.Rejected) as e:
            R.analyze(inp, alias / "inputs.jsonl", "x")
        assert e.value.reason == "output_collides_with_source"
    # Without symlink permission (some Windows runners) the path-alias case still runs through "..".
    with pytest.raises(R.Rejected) as e:
        R.analyze(inp, d / ".." / "src" / "o.json", "x")
    assert e.value.reason == "output_inside_source_folder"


def test_deterministic_apart_from_run_section(tmp_path):
    s = healthy(EXAMPLE)
    a = run(tmp_path, s, name="a.json")
    b = run(tmp_path, s, name="b.json")
    assert a["run"]["analysis_id"] != b["run"]["analysis_id"]
    for doc in (a, b):
        doc.pop("run")
    assert a == b


def test_lineage_is_append_only(tmp_path):
    first = run(tmp_path, healthy(EXAMPLE), name="a.json", analysis_id="rev-1")
    second = run(tmp_path, healthy(EXAMPLE), name="b.json", analysis_id="rev-2",
                 supersedes="rev-1", supersede_reason="synthetic revision", depends_on=["rev-1"])
    assert second["run"]["supersedes"] == "rev-1" and second["run"]["depends_on"] == ["rev-1"]
    assert json.loads((tmp_path / "out" / "a.json").read_text())["run"]["analysis_id"] == "rev-1"
    assert first["run"]["supersedes"] is None


def test_moments_carry_full_references(tmp_path):
    doc = run(tmp_path, healthy(EXAMPLE), meta={"session_id": "synthetic-s"})
    m = doc["result"]["moments"][0]
    for k in ("kind", "session_id", "input_sha256", "start", "end", "epoch", "start_t_us", "end_t_us",
              "duration_us", "health_status", "health_reason", "tool", "config_sha256", "status_stretch"):
        assert k in m
    assert m["session_id"] == "synthetic-s" and m["input_sha256"] == doc["input"]["sha256"]
    assert m["epoch"] == 1


def test_no_private_paths_or_pad_ids_in_output(tmp_path):
    s = Synth().add({"type": "status", "t_us": 5, "state": "connected", "detail": "pad serial=SYNTH123",
                     "pad_id": "serial:SYNTH123"}).inp(1, 0).presses(EXAMPLE).hbs_until(2_000_000)
    doc = run(tmp_path, s)
    blob = json.dumps(doc)
    assert "SYNTH123" not in blob and str(tmp_path) not in blob
    assert doc["input"]["name"] == "inputs.jsonl"


def test_module_has_no_network_device_or_video_imports():
    src = Path(R.__file__).read_text(encoding="utf-8")
    for bad in ("socket", "urllib", "requests", "http", "hid", "serial", "cv2", "subprocess", "numpy"):
        assert f"import {bad}" not in src and f"from {bad}" not in src


def test_wording_stays_descriptive():
    banned = ["who" + "-held", "anti" + "-cheat", "attest" + "ed", "proof-of" + "-human",
              "verified" + " skill", "is" + " con"]
    docs = Path(__file__).resolve().parent.parent / "docs" / "rlr-stage-a.md"
    for p in (Path(R.__file__), FIX / "README.md", docs):
        low = p.read_text(encoding="utf-8").lower()
        for w in banned:
            assert w not in low, (p.name, w)
    assert "not reaction time" in R.CLAIM and "not skill" in R.CLAIM


# ---------------------------------------------------------------- committed synthetic fixtures

FIXTURES = {
    "no_hb_v0.jsonl": lambda: Synth().status(10).inp(1, 0).presses(EXAMPLE),
    "empty_input.jsonl": lambda: Synth().status(10).status(20, "missing"),
    "multi_status_reconnect.jsonl": lambda: (Synth().status(10).inp(1, 0).inp(100_000, R2)
                                             .status(150_000, "unplugged").status(160_000, "connected")
                                             .inp(170_000, 0).presses([(200_000, 260_000)])),
    "hb_qualified.jsonl": lambda: healthy(EXAMPLE).add({"type": "imu", "t_us": 950_000, "present": True})
                                                  .add({"type": "haptic", "t_us": 960_000}),
}


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_committed_fixture_matches_builder(name):
    assert (FIX / name).read_text(encoding="utf-8") == FIXTURES[name]().text()


def test_committed_fixture_results(tmp_path):
    def go(name):
        out = tmp_path / (name + ".out.json")
        R.analyze(FIX / name, out, "synthetic fixture", now=NOW)
        return json.loads(out.read_text())["result"]

    nohb = go("no_hb_v0.jsonl")
    assert nohb["continuity"] == "UNKNOWN" and nohb["dwell"]["session_aggregate"]["median_ms"] is None
    assert nohb["denominators"]["complete_observed_pairs"] == {"dwell": 2, "interval": 1}
    empty = go("empty_input.jsonl")
    assert empty["source"]["input_rows"] == 0 and empty["status"] == "UNMEASURED"
    multi = go("multi_status_reconnect.jsonl")
    assert multi["denominators"]["censoring"]["dwell_censored_at_status"] == 1
    assert multi["denominators"]["complete_observed_pairs"] == {"dwell": 1, "interval": 0}
    ok = go("hb_qualified.jsonl")
    assert ok["dwell"]["session_aggregate"]["median_ms"] == 250
    assert ok["dwell"]["session_aggregate"]["iqr_ms"] == 50
    assert ok["interval"]["session_aggregate"]["median_ms"] == 500
    assert ok["source"]["skipped_row_types"] == {"imu": 1, "haptic": 1}
    assert not (FIX / "missing_session").exists() or not (FIX / "missing_session" / "inputs.jsonl").exists()
