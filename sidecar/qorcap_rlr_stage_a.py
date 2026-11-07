"""RLR Stage A: offline, read-only R2 digital dwell and inter-press interval extraction.

Descriptive host-observed input timing only. Not reaction time, not a skill measure, not a
counting input. Novelty UNASSESSED. Extraction performance on real recordings is UNMEASURED in
this repository: the tests here use synthetic injected values only.

What it reads: one explicitly supplied, completed, immutable copy of ``inputs.jsonl``
(schema ``qorcap-inputs/v0``), optionally ``receipt.json`` next to it (only ``match.game`` and
``match.match_id`` are read, as declared values) and an optional metadata JSON the user supplies.
What it writes: one new JSON file at an explicit ``--out`` path outside the source folder.
It never overwrites, never edits a source file, never writes temp files next to the source, and
never touches capture, receipts, contracts, HUD readers, the referee, uploads or controller output.
No network, no device access, no video.

Source pin: src/inputs.rs and src/hid.rs at 061fa1b8fa80e96a368305767edff436e959d87e (v0.3.40),
checked against main 2135fa7 (src/inputs.rs changed in #133; the row fields used here are the same).
Digital R2 is bit ``1 << 7`` (128) of ``b`` on ``type=in`` rows. Analog ``r2`` is not this bit and
is not force. Rows are change-filtered; long row gaps and ``c`` jumps are not lost-report evidence.

Health rule ``rlr-health/v0-proposed`` is a PROPOSED review default, not approved eligibility
policy, not a physical timing bound, and must not be tuned against outcomes.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

TOOL = "qorcap-rlr-stage-a"
TOOL_VERSION = "0.1.0"
OUTPUT_SCHEMA = "qorcap-rlr-stage-a/v0"
PROCEDURE = "rlr-stage-a-procedure/v0"
HEALTH_RULE = "rlr-health/v0-proposed"
SUPPORTED_SCHEMA = "qorcap-inputs/v0"
SOURCE_PIN = "061fa1b8fa80e96a368305767edff436e959d87e"
R2_MASK = 1 << 7
MAX_LINE_BYTES = 1 << 20
STALE_RX_GAP_US = 50_000
HB_ZERO_FIELDS = ("faults", "short_reads", "parse_faults", "stale_over_50ms")
HB_REQUIRED = ("t_us", "hb_seq", "epoch", "rx_delta", "last_rx_t_us", "max_rx_gap_us") + HB_ZERO_FIELDS
QUANTILE_CONVENTION = ("sorted linear interpolation at index (n-1)*p, p in {0.25, 0.5, 0.75}; "
                       "n=0 gives null (UNMEASURED); n=1 gives the single value for Q1, median and Q3 "
                       "(IQR 0) and is labeled single_value")
CLAIM = ("descriptive host-observed digital-bit timing; not reaction time, not skill, "
         "not a measurement of the player; novelty UNASSESSED")
DISPOSITIONS = {"VERIFIED", "FAIL", "VOID", "NOT_VERIFIED", "MISMATCH", "UNKNOWN", "PASS"}
LATE = {"TIMELY", "LATE", "ADVERSE", "UNKNOWN"}
MODES = {"practice", "real"}

# Match 201029 stays VOID (owner decision). Matched on the declared id, or on the exact inputs.jsonl
# bytes, so an inputs-only run without metadata still carries VOID. A date-qualified suffix is used so a
# later session that happens to start at 20:10:29 is not marked VOID by mistake.
KNOWN_VOID_SESSION_IDS = {"201029"}
KNOWN_VOID_SESSION_SUFFIXES = ("20261106-201029",)
KNOWN_VOID_INPUT_SHA256 = {
    "2a467219c9989378487509e50db1ee3097945eab99dd3deda358eadb3fc96519": "201029",
}
HB_FIRST_VERSION = "0.3.39"

CONFIG = {
    "procedure": PROCEDURE,
    "health_rule": HEALTH_RULE,
    "health_rule_status": "PROPOSED; not approved eligibility policy; not a physical timing bound",
    "r2_mask": R2_MASK,
    "hb_zero_fields": list(HB_ZERO_FIELDS),
    "hb_required_fields": list(HB_REQUIRED),
    "rx_delta_min_exclusive": 0,
    "max_rx_gap_us_max_inclusive": STALE_RX_GAP_US,
    "hb_coverage": ("hb rows from the first hb with t_us >= start through the first hb with t_us >= end; "
                    "the hb before the first covered one (or hb_seq 0) opens the first covered interval; "
                    "contiguous hb_seq, strictly increasing t_us, one epoch"),
    "pairing": ("rise 0->1 and fall 1->0 of b & 128 on type=in rows; dwell=fall-rise; interval=next rise-rise; "
                "status rows, invalid rows, reorder, duplicate or same-time rows end the stretch for pairing"),
    "quantile": QUANTILE_CONVENTION,
    "max_line_bytes": MAX_LINE_BYTES,
}


class Rejected(Exception):
    def __init__(self, reason, detail=""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def config_hash() -> str:
    return hashlib.sha256(_canon(CONFIG)).hexdigest()


def tool_source_hash() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def file_sha256(path: Path):
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        while True:
            block = f.read(1 << 16)
            if not block:
                break
            h.update(block)
            n += len(block)
    return h.hexdigest(), n


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def quantiles(values):
    """Inclusive linear-interpolation quantiles over raw integer microseconds."""
    n = len(values)
    if n == 0:
        return {"n": 0, "status": "UNMEASURED", "q1_us": None, "median_us": None, "q3_us": None,
                "iqr_us": None, "q1_ms": None, "median_ms": None, "q3_ms": None, "iqr_ms": None,
                "convention": QUANTILE_CONVENTION}
    v = sorted(values)

    def q(p):
        k = (n - 1) * p
        i = int(k)
        frac = k - i
        if frac == 0 or i + 1 >= n:
            return v[i]
        # Weights are multiples of 1/4, so the interpolated microseconds stay exact.
        return v[i] + (v[i + 1] - v[i]) * frac

    q1, med, q3 = q(0.25), q(0.5), q(0.75)
    iqr = q3 - q1
    return {"n": n, "status": "single_value" if n == 1 else "measured",
            "q1_us": q1, "median_us": med, "q3_us": q3, "iqr_us": iqr,
            "q1_ms": q1 / 1000, "median_ms": med / 1000, "q3_ms": q3 / 1000, "iqr_ms": iqr / 1000,
            "convention": QUANTILE_CONVENTION}


class _Ref:
    __slots__ = ("line", "chunk_ordinal", "local_index", "seg", "t_us")

    def __init__(self, line, chunk_ordinal, local_index, seg, t_us):
        self.line, self.chunk_ordinal, self.local_index, self.seg, self.t_us = (
            line, chunk_ordinal, local_index, seg, t_us)


def _iter_lines(path: Path):
    """Yield (1-based line index, parsed object). Rejects malformed/truncated input; never repairs."""
    with open(path, "rb") as f:
        idx = 0
        while True:
            raw = f.readline(MAX_LINE_BYTES + 1)
            if not raw:
                return
            idx += 1
            if len(raw) > MAX_LINE_BYTES:
                raise Rejected("line_too_long", f"line {idx}")
            if not raw.endswith(b"\n"):
                raise Rejected("truncated_input", f"line {idx} has no newline terminator")
            text = raw.rstrip(b"\r\n")
            if not text.strip():
                raise Rejected("malformed_jsonl", f"blank line {idx}")
            try:
                obj = json.loads(text.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise Rejected("malformed_jsonl", f"line {idx}")
            if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
                raise Rejected("malformed_jsonl", f"line {idx} is not an object with a string type")
            yield idx, obj


def extract(inputs_path: Path, completion_evidence=None):
    """Parse one inputs.jsonl. Returns the derived result body (no wall-clock or run ids)."""
    header = None
    counts_by_type = {}
    skipped_types = {}
    status_rows = []
    hbs = []
    chunks = []
    chunk_problems = []
    records_in_chunk = 0
    chunk_seg = None
    stretch = 0
    exclusions = {}
    interruptions = {}
    censoring = {"left_censored_hold": 0, "right_censored_hold": 0, "dwell_censored_at_status": 0,
                 "dwell_censored_at_interruption": 0, "fall_without_observed_rise": 0}
    rises = falls = 0
    raw_pairs = []
    prev_bit = None
    rise_ref = None
    last_rise_ref = None
    last_t = None
    last_row = None
    in_rows = 0

    def bump(d, k):
        d[k] = d.get(k, 0) + 1

    def interrupt(reason):
        nonlocal prev_bit, rise_ref, last_rise_ref
        bump(interruptions, reason)
        if rise_ref is not None:
            censoring["dwell_censored_at_interruption"] += 1
        prev_bit = rise_ref = last_rise_ref = None

    for line, row in _iter_lines(inputs_path):
        typ = row["type"]
        bump(counts_by_type, typ)
        if header is None:
            if typ != "header":
                raise Rejected("missing_header", f"first row is type {typ!r}")
            header = row
            if row.get("schema") != SUPPORTED_SCHEMA:
                raise Rejected("unsupported_schema", str(row.get("schema"))[:80])
        elif typ == "header":
            raise Rejected("duplicate_header", f"line {line}")
        if typ == "chunk":
            n, seq, sha = row.get("n"), row.get("seq"), row.get("sha256")
            ok = True
            if not (_is_int(seq) and seq == len(chunks)):
                chunk_problems.append({"line": line, "problem": "seq_not_contiguous"})
                ok = False
            if not (_is_int(n) and n == records_in_chunk):
                chunk_problems.append({"line": line, "problem": "n_mismatch"})
                ok = False
            if not (isinstance(sha, str) and len(sha) == 64 and all(c in "0123456789abcdef" for c in sha)):
                chunk_problems.append({"line": line, "problem": "sha256_malformed"})
                ok = False
            if chunk_seg is not None and row.get("seg") != chunk_seg:
                chunk_problems.append({"line": line, "problem": "seg_mismatch"})
                ok = False
            chunks.append({"seq": seq if _is_int(seq) else None,
                           "declared_sha256": sha if isinstance(sha, str) else None,
                           "seg": row.get("seg"), "line": line, "structure_ok": ok})
            records_in_chunk = 0
            chunk_seg = None
            continue
        ordinal = len(chunks)
        local = records_in_chunk
        records_in_chunk += 1
        seg = row.get("seg")
        if chunk_seg is None:
            chunk_seg = seg
        elif seg != chunk_seg:
            chunk_problems.append({"line": line, "problem": "record_seg_differs_within_chunk"})
        if typ == "header":
            continue
        if typ == "status":
            status_rows.append({"line": line, "state": row.get("state") if isinstance(row.get("state"), str) else None})
            if rise_ref is not None:
                censoring["dwell_censored_at_status"] += 1
            stretch += 1
            prev_bit = rise_ref = last_rise_ref = None
            continue
        if typ == "hb":
            hbs.append(row)
            continue
        if typ != "in":
            bump(skipped_types, typ)
            continue
        in_rows += 1
        t, b = row.get("t_us"), row.get("b")
        if not (_is_int(t) and t >= 0 and _is_int(b) and b >= 0):
            interrupt("invalid_input_row")
            continue
        if last_t is not None and t < last_t:
            interrupt("reordered_or_reset_t_us")
            last_t, last_row = t, (t, b)
            continue
        if last_t is not None and t == last_t:
            interrupt("duplicate_same_time_row" if last_row == (t, b) else "same_time_contradiction")
            last_row = (t, b)
            continue
        last_t, last_row = t, (t, b)
        bit = bool(b & R2_MASK)
        ref = _Ref(line, ordinal, local, seg, t)
        if prev_bit is None:
            if bit:
                censoring["left_censored_hold"] += 1
        elif bit and not prev_bit:
            rises += 1
            if last_rise_ref is not None:
                raw_pairs.append(("interval", last_rise_ref, ref, stretch))
            rise_ref = last_rise_ref = ref
        elif prev_bit and not bit:
            falls += 1
            if rise_ref is not None:
                raw_pairs.append(("dwell", rise_ref, ref, stretch))
            else:
                censoring["fall_without_observed_rise"] += 1
            rise_ref = None
        prev_bit = bit
    if header is None:
        raise Rejected("empty_input", "no header row")
    if rise_ref is not None:
        censoring["right_censored_hold"] += 1
    unsealed_tail_records = records_in_chunk

    has_hb = bool(hbs)
    hb_t_ok = all(_is_int(h.get("t_us")) for h in hbs)
    hb_times = [h["t_us"] for h in hbs] if hb_t_ok else []
    hb_sorted = hb_t_ok and all(a < b for a, b in zip(hb_times, hb_times[1:]))

    def qualify(a_t, b_t):
        if not has_hb:
            return None, "continuity_unknown_no_hb"
        if not hb_t_ok:
            return None, "hb_field_missing"
        if not hb_sorted:
            return None, "hb_clock_not_increasing"
        r = bisect.bisect_left(hb_times, b_t)
        if r >= len(hbs):
            return None, "right_tail_uncovered_by_hb"
        lo = bisect.bisect_left(hb_times, a_t)
        if lo == 0 and hbs[0].get("hb_seq") != 0:
            return None, "left_uncovered_by_hb"
        sel = hbs[lo:r + 1]
        if any(k not in h for h in sel for k in HB_REQUIRED):
            return None, "hb_field_missing"
        if len({repr(h.get("epoch")) for h in sel}) != 1 or not _is_int(sel[0].get("epoch")):
            return None, "epoch_change_or_unknown"
        for j in range(lo, r + 1):
            x = hbs[j]
            if j > 0:
                p = hbs[j - 1]
                if not (_is_int(x.get("hb_seq")) and _is_int(p.get("hb_seq")) and x["hb_seq"] == p["hb_seq"] + 1):
                    return None, "hb_seq_gap"
            if any(not _is_int(x.get(k)) or x.get(k) != 0 for k in HB_ZERO_FIELDS):
                return None, "hb_fault_or_stale"
            if not _is_int(x.get("rx_delta")) or x["rx_delta"] <= 0:
                return None, "hb_no_receive"
            if not _is_int(x.get("max_rx_gap_us")) or x["max_rx_gap_us"] > STALE_RX_GAP_US:
                return None, "hb_rx_gap_over_limit"
            if x.get("last_rx_t_us") is None:
                return None, "hb_missing_receive_time"
        return sel[0]["epoch"], "qualified"

    def ref_json(ref):
        out = {"line": ref.line, "t_us": ref.t_us, "seg": ref.seg, "chunk_local_index": ref.local_index}
        if ref.chunk_ordinal < len(chunks):
            c = chunks[ref.chunk_ordinal]
            out.update({"chunk_seq": c["seq"], "chunk_declared_sha256": c["declared_sha256"],
                        "chunk_structure_ok": c["structure_ok"], "sealed": True})
        else:
            out.update({"chunk_seq": None, "chunk_declared_sha256": None, "chunk_structure_ok": None,
                        "sealed": False})
        return out

    moments = []
    qualified = {"dwell": {}, "interval": {}}
    complete = {"dwell": 0, "interval": 0}
    for kind, a, b, st in raw_pairs:
        complete[kind] += 1
        epoch, why = qualify(a.t_us, b.t_us)
        ra, rb = ref_json(a), ref_json(b)
        trace_q = bool(ra["sealed"] and rb["sealed"] and ra["chunk_structure_ok"] and rb["chunk_structure_ok"])
        if epoch is None:
            bump(exclusions, f"{kind}:{why}")
        else:
            qualified[kind].setdefault(epoch, []).append(b.t_us - a.t_us)
        moments.append({"kind": kind, "start": ra, "end": rb, "status_stretch": st,
                        "epoch": epoch if epoch is not None else "UNKNOWN",
                        "start_t_us": a.t_us, "end_t_us": b.t_us, "duration_us": b.t_us - a.t_us,
                        "health_status": "qualified" if epoch is not None else "excluded",
                        "health_reason": why, "trace_qualified": trace_q,
                        "chunk_binding": "declared_only", "binding_verified": False})

    abstain = []
    if not completion_evidence:
        abstain.append("completion_evidence_missing")
    if chunk_problems:
        abstain.append("chunk_structure_invalid")

    def stats(kind):
        per_epoch = {}
        pooled = []
        for ep in sorted(qualified[kind]):
            vals = qualified[kind][ep]
            per_epoch[str(ep)] = quantiles(vals)
            pooled.extend(vals)
        if abstain:
            s = quantiles([])
            s["reason"] = abstain[0]
            return {"session_aggregate": s, "per_epoch": {}, "qualified_values_withheld": len(pooled)}
        if len(per_epoch) > 1:
            agg = quantiles([])
            agg["status"] = "not_pooled"
            agg["reason"] = "epochs_not_merged"
            agg["note"] = "epochs are not merged into one series; per_epoch keeps each denominator"
            return {"session_aggregate": agg, "per_epoch": per_epoch, "qualified_values_withheld": 0}
        agg = quantiles(pooled)
        if not pooled:
            agg["reason"] = "continuity_unknown_no_hb" if not has_hb else "no_qualified_pairs"
        agg["note"] = "single epoch group or none; not a cross-epoch merge; per_epoch keeps the denominator"
        return {"session_aggregate": agg, "per_epoch": per_epoch, "qualified_values_withheld": 0}

    dwell, interval = stats("dwell"), stats("interval")
    if abstain:
        status = "abstained"
    else:
        measured_n = 0
        for block in (dwell, interval):
            if block["per_epoch"]:
                measured_n += sum((row["n"] or 0) for row in block["per_epoch"].values())
            else:
                measured_n += block["session_aggregate"]["n"] or 0
        status = "measured" if measured_n else "UNMEASURED"
    return {
        "status": status,
        "abstain_reasons": abstain,
        "source": {
            "schema": header.get("schema"), "schema_known": header.get("schema") == SUPPORTED_SCHEMA,
            "transport": header["transport"] if isinstance(header.get("transport"), str) else "UNKNOWN",
            "capture_version": next((header[k] for k in ("version", "qorcap_version", "capture_version")
                                     if isinstance(header.get(k), str)), "UNKNOWN"),
            "row_counts_by_type": counts_by_type,
            "skipped_row_types": skipped_types,
            "input_rows": in_rows,
            "status_rows": len(status_rows),
            "status_states": [s["state"] for s in status_rows],
            "status_stretches": len(status_rows) + 1,
            "hb_rows": len(hbs),
            "chunks": len(chunks),
            "unsealed_tail_records": unsealed_tail_records,
        },
        "clock_domain": {"actor": "host", "units": "us", "origin": "host-monotonic t_us (header clock field)",
                         "epoch_source": "hb rows only; status rows and raw row gaps never assign an epoch",
                         "mapping": "none; no wall, video or device clock mapping is asserted"},
        "continuity": "hb_present" if has_hb else "UNKNOWN",
        "chunk_binding": {"state": "declared_only", "binding_verified": False, "problems": chunk_problems,
                          "note": "chunk sha256/prev/head are as recorded; not recomputed or checked here"},
        "denominators": {"r2_rises": rises, "r2_falls": falls,
                         "complete_observed_pairs": complete,
                         "qualified_pairs": {k: sum(len(v) for v in qualified[k].values()) for k in qualified},
                         "excluded_pairs_by_reason": exclusions,
                         "interruptions": interruptions,
                         "censoring": censoring},
        "dwell": dwell,
        "interval": interval,
        "moments": moments,
    }


def _norm_disposition(raw, allowed):
    if raw is None:
        return {"raw": None, "normalized": "UNKNOWN", "state": "unknown"}
    s = str(raw)
    n = s.strip().upper().replace(" ", "_").replace("-", "_")
    return {"raw": s, "normalized": n if n in allowed else "UNKNOWN", "state": "declared"}


def build_metadata(meta, receipt_match, meta_source):
    meta = meta or {}
    rm = receipt_match or {}

    def field(meta_val, receipt_val, receipt_label):
        if meta_val is not None and receipt_val is not None and str(meta_val) != str(receipt_val):
            return {"value": None, "state": "mismatch", "declared": str(meta_val), "receipt_declared": str(receipt_val)}
        if meta_val is not None:
            return {"value": str(meta_val), "state": "declared", "source": meta_source}
        if receipt_val is not None:
            return {"value": str(receipt_val), "state": "declared", "source": receipt_label}
        return {"value": "UNKNOWN", "state": "unknown"}

    out = {"session_id": field(meta.get("session_id"), rm.get("match_id"), "receipt.json match.match_id (not checked)")}
    g = field(meta.get("game"), rm.get("game"), "receipt.json match.game (not checked)")
    label = g.pop("value")
    g["label"] = label if label is not None else "UNKNOWN"
    evidence = meta.get("game_identity_evidence")
    if isinstance(evidence, str) and evidence.strip() and g["state"] == "declared":
        g["identity"] = {"value": g["label"], "state": "declared_with_supplied_evidence",
                         "evidence": evidence, "checked_by_this_tool": False}
    else:
        g["identity"] = {"value": "UNKNOWN", "state": "unknown",
                         "reason": "a game label is not identity; no independent evidence supplied"}
    out["game"] = g
    mode = meta.get("mode")
    m = str(mode).strip().lower() if mode is not None else None
    out["mode"] = {"raw": mode, "value": m if m in MODES else "UNKNOWN",
                   "state": "declared" if m in MODES else "unknown"}
    out["receipt_disposition"] = _norm_disposition(meta.get("receipt_disposition"), DISPOSITIONS)
    out["verify_disposition"] = _norm_disposition(meta.get("verify_disposition"), DISPOSITIONS)
    out["late_disposition"] = _norm_disposition(meta.get("late_disposition"), LATE)
    out["provenance"] = meta.get("provenance") if isinstance(meta.get("provenance"), str) else "UNKNOWN"
    refs = meta.get("external_check_refs")
    out["external_check_refs"] = refs if isinstance(refs, list) else []
    out["checked_by_this_tool"] = False
    out["note"] = ("declared values are as supplied; this tool checks no receipt and never infers practice/real or "
                   "a disposition from folder names or verify text; VERIFIED here never establishes derived events "
                   "or counting eligibility")
    return out


def disposition_block(md, input_sha256):
    """Inherited disposition carried on every derived record. Never upgraded by this tool."""
    sources = []
    sid = md["session_id"]
    ids = [v for v in (sid.get("value"), sid.get("declared"), sid.get("receipt_declared")) if isinstance(v, str)]
    for v in ids:
        if v in KNOWN_VOID_SESSION_IDS or v.endswith(KNOWN_VOID_SESSION_SUFFIXES):
            sources.append("known VOID match 201029 (declared session id)")
            break
    if input_sha256 in KNOWN_VOID_INPUT_SHA256:
        sources.append("known VOID match 201029 (inputs.jsonl sha256)")
    declared = md["receipt_disposition"]["normalized"]
    if "VOID" in (declared, md["verify_disposition"]["normalized"]):
        sources.append("declared VOID disposition")
    void = bool(sources)
    conflict = void and declared not in ("VOID", "UNKNOWN")
    return {"disposition": "VOID" if void else declared,
            "void": void, "never_eligible": void,
            "sources": sources if void else ["declared receipt disposition" if declared != "UNKNOWN" else "none"],
            "declared_receipt_disposition": declared,
            "declared_conflicts_with_void": conflict,
            "note": "inherited only; derived values never change or upgrade it"}


def game_label_trust(md, result):
    """A receipt game label is declared, not identity. Before v0.3.39 Start Match defaulted to madden27."""
    g = md["game"]
    hb = result.get("source", {}).get("hb_rows") if isinstance(result, dict) else None
    if hb:
        era = f"hb rows present (capture v{HB_FIRST_VERSION}+)"
        trust = "declared_only"
    else:
        era = f"UNKNOWN; no hb rows, so the capture may predate v{HB_FIRST_VERSION}"
        trust = "untrusted_possible_pre_v0.3.39_default"
    g["capture_era"] = era
    g["label_trust"] = trust
    g["label_conflict"] = ("possible_default_madden27" if trust != "declared_only" and g["label"] == "madden27"
                           else ("metadata_receipt_mismatch" if g["state"] == "mismatch" else "none_detected"))
    g["note"] = ("game is a declared label; before v0.3.39 madden27 was the default receipt game, so a label "
                 "from an older receipt may not describe the game played")


def eligibility(md, disp_block=None):
    disp = {md["receipt_disposition"]["normalized"], md["verify_disposition"]["normalized"]}
    reasons = []
    if md["mode"]["value"] != "real":
        reasons.append("practice_or_unknown_mode")
    if disp & {"FAIL", "VOID", "MISMATCH", "NOT_VERIFIED", "UNKNOWN"}:
        reasons.append("inherited_disposition_not_verified")
    if md["late_disposition"]["normalized"] in {"LATE", "ADVERSE"}:
        reasons.append("late_or_adverse_witness")
    if any(md[k]["state"] == "mismatch" for k in ("session_id", "game")):
        reasons.append("metadata_mismatch")
    if disp_block and disp_block["void"]:
        reasons.append("void_never_eligible")
    return {"evidence_tier": "private_descriptive",
            "formal_counting": "not_eligible" if reasons else "not_evaluated",
            "formal_counting_reasons": reasons,
            "standing_upgrade": False, "gold": False, "referee_submit": False, "upload": False,
            "note": "Stage A extraction never creates counting, gold, standings or profile eligibility"}


def _real(p) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


def check_out_path(out: Path, protected_dirs, protected_files):
    ro = _real(out)
    for f in protected_files:
        if f is not None and ro == _real(f):
            raise Rejected("output_collides_with_source", "")
    for d in protected_dirs:
        rd = _real(d)
        if ro == rd or ro.startswith(rd.rstrip(os.sep) + os.sep):
            raise Rejected("output_inside_source_folder", "")
    if os.path.lexists(str(out)):
        raise Rejected("output_exists", "refusing to overwrite")
    if not Path(out).parent.is_dir():
        raise Rejected("output_parent_missing", "")


def analyze(inputs, out, completion_evidence=None, metadata_path=None, receipt_path=None,
            analysis_id=None, supersedes=None, supersede_reason=None, depends_on=(), now=None):
    inputs, out = Path(inputs), Path(out)
    check_out_path(out, [inputs.parent], [inputs, metadata_path, receipt_path])
    md_raw, md_src = {}, None
    hashes_before = {}
    if metadata_path is not None:
        hashes_before[str(metadata_path)] = file_sha256(Path(metadata_path))
        md_raw = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
        if not isinstance(md_raw, dict):
            raise Rejected("metadata_not_object", "")
        md_src = "supplied metadata file"
    receipt_match, receipt_state = None, "not_supplied"
    if receipt_path is not None:
        if Path(receipt_path).is_file():
            hashes_before[str(receipt_path)] = file_sha256(Path(receipt_path))
            try:
                rc = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
                m = rc.get("match") if isinstance(rc, dict) else None
                receipt_match = {k: m.get(k) for k in ("game", "match_id")} if isinstance(m, dict) else {}
                receipt_state = "read_declared_fields_only"
            except ValueError:
                receipt_state = "unreadable"
        else:
            receipt_state = "absent"
    md = build_metadata(md_raw, receipt_match, md_src)
    run = {"analysis_id": analysis_id or str(uuid.uuid4()),
           "created_at_wall": (now or datetime.now(timezone.utc)).isoformat(),
           "created_at_note": "wall clock; never used for dwell or interval",
           "supersedes": supersedes, "supersede_reason": supersede_reason,
           "depends_on": list(depends_on), "revision_status": "current",
           "note": "append-only: a revision is a new file with a new analysis_id; earlier outputs are never rewritten"}
    doc = {"schema": OUTPUT_SCHEMA,
           "tool": {"name": TOOL, "version": TOOL_VERSION, "procedure": PROCEDURE,
                    "source_sha256": tool_source_hash(), "config": CONFIG, "config_sha256": config_hash(),
                    "source_pin": {"commit": SOURCE_PIN, "files": ["src/inputs.rs", "src/hid.rs"], "checked_against": "main 2135fa7 (src/inputs.rs also changed in 2ed127a, #133; the fields used here are unchanged)",
                                   "note": "hb sequence field is hb_seq; hb fields not listed in config are not interpreted; chunk hashes are declared_only"}},
           "claim": CLAIM, "run": run,
           "input": {"name": inputs.name, "sha256": None, "length": None},
           "completion_evidence": {"value": completion_evidence,
                                   "state": "declared" if completion_evidence else "unknown"},
           "receipt_json": receipt_state, "metadata": md,
           "unmeasured": ["physical bit-edge latency and firmware threshold", "device clock mapping",
                          "hw units", "extraction agreement with an independent annotator on real recordings",
                          "chunk hash binding (declared only)"]}
    if not inputs.is_file():
        result = {"status": "rejected", "reject_reason": "input_missing", "detail": ""}
    else:
        sha, length = file_sha256(inputs)
        doc["input"].update({"sha256": sha, "length": length})
        try:
            result = extract(inputs, completion_evidence)
        except Rejected as e:
            result = {"status": "rejected", "reject_reason": e.reason, "detail": e.detail}
        if file_sha256(inputs) != (sha, length):
            raise Rejected("source_changed_during_analysis", "inputs")
        for m in result.get("moments", ()):
            m["session_id"] = md["session_id"]["value"]
            m["input_sha256"] = sha
            m["tool"] = f"{TOOL}/{TOOL_VERSION}"
            m["config_sha256"] = doc["tool"]["config_sha256"]
    for p, h in hashes_before.items():
        if file_sha256(Path(p)) != h:
            raise Rejected("source_changed_during_analysis", "metadata or receipt")
    disp = disposition_block(md, doc["input"]["sha256"])
    game_label_trust(md, result)
    doc["disposition"] = disp
    doc["eligibility"] = eligibility(md, disp)
    flag = {"disposition": disp["disposition"], "void": disp["void"], "never_eligible": disp["never_eligible"]}
    result["disposition"] = dict(flag)
    for m in result.get("moments", ()):
        m.update(flag)
    for kind in ("dwell", "interval"):
        block = result.get(kind)
        if isinstance(block, dict):
            block.update(flag)
            block["session_aggregate"].update(flag)
            for row in block["per_epoch"].values():
                row.update(flag)
    doc["result"] = result
    data = (json.dumps(doc, indent=1, sort_keys=True) + "\n").encode("utf-8")
    with open(out, "xb") as f:
        f.write(data)
    return doc


def summary_line(doc) -> str:
    r = doc["result"]
    if r.get("status") == "rejected":
        return f"rejected: {r['reject_reason']} disposition={doc['disposition']['disposition']}"
    d, i = r["dwell"]["session_aggregate"], r["interval"]["session_aggregate"]
    return (f"status={r['status']} disposition={doc['disposition']['disposition']} continuity={r['continuity']} "
            f"game_label={doc['metadata']['game']['label']}(declared, not identity) "
            f"dwell n={d['n']} median_ms={d['median_ms']} iqr_ms={d['iqr_ms']} "
            f"interval n={i['n']} median_ms={i['median_ms']} iqr_ms={i['iqr_ms']} "
            f"excluded={sum(r['denominators']['excluded_pairs_by_reason'].values())}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="RLR Stage A: offline read-only R2 dwell/interval description "
                                 "(not reaction time, not skill).")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--inputs", help="explicit completed copy of inputs.jsonl")
    src.add_argument("--session-dir", help="completed session folder copy: reads inputs.jsonl and, if present, "
                     "receipt.json match.game/match_id (declared only)")
    ap.add_argument("--out", required=True, help="new output JSON path outside the source folder (never overwritten)")
    ap.add_argument("--completion-evidence", default=None,
                    help="how the recording is known to be complete; without it qualified stats are withheld")
    ap.add_argument("--metadata", default=None, help="optional metadata JSON (session_id, game, mode, dispositions)")
    ap.add_argument("--analysis-id", default=None)
    ap.add_argument("--supersedes", default=None)
    ap.add_argument("--supersede-reason", default=None)
    ap.add_argument("--depends-on", action="append", default=[])
    a = ap.parse_args(argv)
    if a.session_dir:
        d = Path(a.session_dir)
        inputs, receipt = d / "inputs.jsonl", d / "receipt.json"
    else:
        inputs, receipt = Path(a.inputs), None
    try:
        doc = analyze(inputs, Path(a.out), a.completion_evidence, a.metadata, receipt, a.analysis_id,
                      a.supersedes, a.supersede_reason, a.depends_on)
    except Rejected as e:
        print(f"refused: {e.reason}", file=sys.stderr)
        return 2
    print(summary_line(doc))
    return 3 if doc["result"].get("status") == "rejected" else 0


if __name__ == "__main__":
    sys.exit(main())
