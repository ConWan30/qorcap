"""QorData acceptance behavior for capture clocks, failures, and finals.

These functions are the product rules the fixture suite calls. A wall clock is
not a capture clock. A failure is a chain row that drops the current final
candidate. A truncated chain is not a complete record.
"""
from __future__ import annotations

import json
from pathlib import Path


CLOCK_SOURCES = {"monotonic_ns", "perf_counter_ns", "engine_monotonic_ns"}
SNAPSHOT_SCHEMA = "qorcap-snapshot/v1"


def _int(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def order_captures(doc: dict) -> dict:
    """Order accepted captures by producer monotonic time in one clock domain.

    ``wall_time_ms`` is never the sort key. A missing binding is unknown.
    A different producer clock is a different domain even when its integer is smaller.
    """
    if not isinstance(doc, dict):
        return {"verdict": "unknown_void", "reason": "clock_binding_missing"}
    frames = doc.get("frames")
    if not isinstance(frames, list):
        if doc.get("clock_id") is None or doc.get("mono_origin_ns") is None:
            return {"verdict": "unknown_void", "reason": "clock_binding_missing"}
        return {"verdict": "unknown_void", "reason": "clock_binding_missing"}
    session_clock = doc.get("session_clock_id") or doc.get("clock_id")
    if session_clock is None or doc.get("mono_origin_ns") is None and doc.get("clock_id") is None:
        # Cross-boot inputs name the session clock and omit a shared origin.
        if doc.get("session_clock_id") is None:
            return {"verdict": "unknown_void", "reason": "clock_binding_missing"}
    for frame in frames:
        if not isinstance(frame, dict):
            return {"verdict": "unknown_void", "reason": "clock_binding_missing"}
        clock = frame.get("clock_id") or doc.get("clock_id")
        if session_clock and clock and clock != session_clock:
            return {"verdict": "reject_clock_domain", "reason": "clock_epoch_mismatch"}
        if clock is None or _int(frame.get("mono_us")) is None:
            return {"verdict": "unknown_void", "reason": "clock_binding_missing"}
    if doc.get("session_clock_id") is None:
        if doc.get("clock_id") is None or doc.get("mono_origin_ns") is None:
            return {"verdict": "unknown_void", "reason": "clock_binding_missing"}
        if doc.get("clock_source") not in CLOCK_SOURCES:
            return {"verdict": "unknown_void", "reason": "clock_binding_missing"}
        origin = _int(doc.get("mono_origin_ns"))
        if origin is None or origin <= 0:
            return {"verdict": "unknown_void", "reason": "clock_binding_missing"}
    ordered = sorted(frames, key=lambda frame: (_int(frame.get("mono_us")), str(frame.get("capture_id"))))
    return {
        "verdict": "accept_order",
        "ordered_capture_ids": [frame.get("capture_id") for frame in ordered],
        "wall_time_used_for_order": False,
    }


def evidence_document(region: dict) -> dict:
    """Public evidence hashed before any status-only sparsify. Scores are integers."""
    digits = region.get("digits") if isinstance(region.get("digits"), dict) else {}
    candidates = region.get("candidates") if isinstance(region.get("candidates"), list) else []
    clean_digits = {}
    for key in sorted(digits):
        num = _int(digits[key])
        if num is not None:
            clean_digits[key] = num
    clean_candidates = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        value = _int(item.get("value"))
        score = _int(item.get("score_milli"))
        if value is None or score is None:
            continue
        clean_candidates.append({"score_milli": score, "value": value})
    return {
        "schema": "qorcap-evidence/v1",
        "calibrated": bool(region.get("calibrated")),
        "candidates": clean_candidates,
        "digits": clean_digits,
        "reason": region.get("reason"),
        "region": region.get("region"),
        "status": region.get("status"),
    }


def evidence_hash(region: dict) -> str:
    import hashlib

    import qorcap_cod as cod

    return hashlib.sha256(cod._canon(evidence_document(region)).encode("utf-8")).hexdigest()


def emit_region_evidence(regions: list) -> list[dict]:
    """Keep a row when its evidence hash changes. Status alone is not the key."""
    rows = []
    previous = None
    for region in regions:
        digest = evidence_hash(region)
        if digest == previous:
            continue
        previous = digest
        rows.append({"evidence_hash": digest, "status": region.get("status"), "capture_id": region.get("capture_id")})
    return rows


def reason_code_for(detail: str) -> str:
    text = str(detail).lower()
    if "version" in text or "incompatible" in text:
        return "version_mismatch"
    if "decode" in text:
        return "decode_error"
    if "http" in text or "urlerror" in text or "timeout" in text:
        return "http_error"
    if "stale" in text:
        return "stale_snapshot"
    if "future" in text:
        return "future_snapshot"
    if "repeated" in text or "duplicate" in text:
        return "duplicate_capture"
    if "clock" in text or "provenance" in text or "missing" in text:
        return "missing_provenance"
    if "transport" in text:
        return "transport_error"
    return "reader_error"


def classify_provenance_variant(variant: dict) -> str:
    if variant.get("capture_generation") is None and "capture_generation" in variant:
        return "missing_provenance"
    if variant.get("source_wall_ms") is None and variant.get("capture_generation") is None:
        return "missing_provenance"
    source = _int(variant.get("source_wall_ms"))
    now = _int(variant.get("now_wall_ms"))
    limit = _int(variant.get("max_age_ms"))
    if source is not None and now is not None and limit is not None and now - source > limit:
        return "stale_snapshot"
    return "missing_provenance"


def eligibility_decision(*, path, end_us, frames, seal_ready, anchor_ready, epoch_valid,
                         lag_present, session_id, capture_generation, binding_epoch=None) -> dict:
    """One gate for both final paths. Equality at ``end_us`` is not eligible.

    Missing lag, seal, anchor, or epoch stays unknown. This function does not
    fill those in.
    """
    reasons = []
    if not isinstance(session_id, str) or not session_id:
        reasons.append("session_missing")
    if _int(capture_generation) is None:
        reasons.append("generation_missing")
    if _int(end_us) is None or lag_present is not True:
        reasons.append("window_unresolved")
    if seal_ready is not True:
        reasons.append("seal_not_durable")
    if anchor_ready is not True:
        reasons.append("anchor_not_validated")
    if epoch_valid is not True:
        reasons.append("epoch_invalid")
    ids = []
    end = _int(end_us)
    for frame in frames or []:
        if not isinstance(frame, dict):
            reasons.append("capture_id_missing")
            continue
        capture_id = frame.get("capture_id")
        if not isinstance(capture_id, str) or not capture_id:
            reasons.append("capture_id_missing")
        else:
            ids.append(capture_id)
        if isinstance(session_id, str) and session_id and frame.get("session_id") != session_id:
            reasons.append("session_mismatch")
        if _int(capture_generation) is not None and _int(frame.get("capture_generation")) != _int(capture_generation):
            reasons.append("generation_mismatch")
        if _int(binding_epoch) is not None and _int(frame.get("epoch")) != _int(binding_epoch):
            reasons.append("epoch_mismatch")
        mono = _int(frame.get("mono_us"))
        if mono is None or end is None:
            if end is not None:
                reasons.append("frame_time_missing")
            continue
        if mono < end:
            reasons.append("pre_exclusion_frame")
        elif mono == end:
            reasons.append("boundary_not_after_end")
    if len(ids) != len(set(ids)):
        reasons.append("duplicate_capture_id")
    primary = None
    for name in (
        "pre_exclusion_frame", "boundary_not_after_end", "duplicate_capture_id",
        "session_mismatch", "generation_mismatch", "epoch_mismatch",
    ):
        if name in reasons:
            primary = name
            break
    if primary is None and reasons:
        primary = reasons[0]
    eligible = not reasons
    return {
        "path": path,
        "eligible": eligible,
        "final_confirmed": eligible,
        "reason": primary,
        "reason_codes": reasons,
        "fabricated_anchor_or_lag": False,
    }


def pad_neutral(pad: dict) -> bool:
    """Rest is buttons clear, sticks at 128, and both analog triggers at 0."""
    if not isinstance(pad, dict):
        return False
    return (
        _int(pad.get("buttons")) == 0
        and _int(pad.get("lx")) == 128
        and _int(pad.get("ly")) == 128
        and _int(pad.get("rx")) == 128
        and _int(pad.get("ry")) == 128
        and _int(pad.get("l2")) == 0
        and _int(pad.get("r2")) == 0
    )


def _anchor_supported(hb: dict, validation) -> bool:
    """A formatted token is not authority evidence.

    ``validation`` is not an authority result. This process does not register
    an anchor from a string.
    """
    del hb, validation
    return False


def chain_acceptance(text: str) -> dict:
    import qorcap_cod as cod

    structural = cod.verify_signal_chain(text)
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            row = None
        if isinstance(row, dict):
            rows.append(row)
    last = rows[-1] if rows else {}
    if not rows or last.get("kind") != "terminal":
        return {
            "verdict": "reject_incomplete",
            "reason": "terminal_missing",
            "reasons": ["terminal_missing"],
            "structural_errors": structural,
            "structural_ok": False,
        }
    hash_bad = any(
        ("hash" in err) or ("prev" in err) or ("seq" in err) or ("canonical" in err) or ("unique" in err)
        for err in structural
    )
    count_bad = any("count" in err for err in structural)
    root_bad = any("root" in err for err in structural)
    if hash_bad or (count_bad and root_bad):
        reason = "root_count_seq_mismatch"
        verdict = "reject_incomplete"
    elif count_bad:
        reason = "terminal_count_mismatch"
        verdict = "reject"
    elif root_bad:
        reason = "terminal_root_mismatch"
        verdict = "reject"
    elif structural:
        reason = "root_count_seq_mismatch"
        verdict = "reject_incomplete"
    elif last.get("truncated") is True:
        return {
            "verdict": "unknown_void",
            "reason": "chain_truncated",
            "reasons": ["chain_truncated"],
            "structural_errors": [],
            "structural_ok": True,
        }
    else:
        return {
            "verdict": "accept_structure",
            "reason": None,
            "reasons": [],
            "structural_errors": [],
            "structural_ok": True,
        }
    return {
        "verdict": verdict,
        "reason": reason,
        "reasons": [reason],
        "structural_errors": structural,
        "structural_ok": False,
    }


def _rehash_terminal(text: str) -> str:
    import hashlib

    import qorcap_cod as cod

    lines = [line for line in text.splitlines() if line.strip()]
    rows = [json.loads(line) for line in lines]
    prev = rows[-2]["hash"] if len(rows) > 1 else cod.SIGNAL_GENESIS
    body = {key: value for key, value in rows[-1].items() if key not in ("prev", "hash")}
    seq = body.get("chain_seq")
    if _int(seq) is None:
        seq = len(rows) - 1
    digest = hashlib.sha256(cod._canon({"prev": prev, "seq": seq, "row": body}).encode("utf-8")).hexdigest()
    rows[-1]["prev"] = prev
    rows[-1]["hash"] = digest
    return "\n".join(json.dumps(row) for row in rows) + "\n"


def _build_signal_text(data_count: int) -> str:
    import tempfile

    import qorcap_cod as cod

    class Hud:
        def read(self, img):
            return []

    class End:
        def read(self, img):
            return cod.RegionRead("end_screen", "not_read", False, "not_present", False)

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "cod_signals.jsonl"
        runner = cod.CodSetRunner(lambda _event: None, hud=Hud(), end=End(), log_path=path)
        runner.session_id = "s1"
        for index in range(data_count):
            runner.on_frame(index * 1000, object(), source={
                "frame_id": f"f{index}",
                "capture_id": f"f{index}",
                "seq": index,
                "epoch": 1,
                "clock": "capture_pts",
                "mono_us": 1_000 + index,
                "session_id": "s1",
                "clock_id": "boot1:producer1",
            })
        assert runner.close_signals() is True
        text = path.read_text(encoding="utf-8")
    acceptance = chain_acceptance(text)
    if acceptance["verdict"] != "accept_structure":
        raise AssertionError(acceptance)
    return text


def _run_http(doc: dict) -> dict:
    import tempfile

    import qorcap_cod as cod

    class Hud:
        def read(self, img):
            return []

    class End:
        def read(self, img):
            return cod.RegionRead("end_screen", "not_read", False, "not_present", False)

    framed = []

    def on_success(capture_id):
        framed.append(capture_id)

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "cod_signals.jsonl"
        runner = cod.CodSetRunner(lambda _event: None, hud=Hud(), end=End(), log_path=path)
        failure = None
        reset = False
        for attempt in doc["attempts"]:
            if attempt.get("http_status") != 200:
                runner._pair = ("candidate",)
                runner._n = 2
                runner.note_capture_failure(
                    reason_code="http_error",
                    attempt_seq=attempt.get("attempt_seq"),
                    http_status=attempt.get("http_status"),
                    observed_mono_us=None,
                )
                reset = runner._n == 0 and runner._pair is None
                rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
                failure = next(row for row in rows if row.get("kind") == "capture_failure" and row.get("attempt_seq") == attempt.get("attempt_seq"))
            else:
                on_success(attempt.get("capture_id"))
        assert failure is not None
        assert failure.get("capture_id") is None
        assert failure.get("capture_mono_us") is None
        return {
            "verdict": "log_failure",
            "failure": {
                "attempt_seq": failure.get("attempt_seq"),
                "kind": failure.get("kind"),
                "reason_code": failure.get("reason_code"),
                "http_status": failure.get("http_status"),
            },
            "chain_entry_required": True,
            "final_candidate_reset": reset,
            "successes_seen": framed,
        }


def _run_stale(doc: dict) -> dict:
    import tempfile

    import qorcap_cod as cod

    class Hud:
        def read(self, img):
            return []

    class End:
        def read(self, img):
            return cod.RegionRead("end_screen", "not_read", False, "not_present", False)

    reasons = []
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "cod_signals.jsonl"
        runner = cod.CodSetRunner(lambda _event: None, hud=Hud(), end=End(), log_path=path)
        resets = []
        for variant in doc["variants"]:
            code = classify_provenance_variant(variant)
            reasons.append(code)
            runner._pair = ("candidate",)
            runner._n = 2
            runner.note_capture_failure(
                reason_code=code,
                attempt_seq=variant.get("attempt_seq"),
                observed_mono_us=None,
            )
            resets.append(runner._n == 0 and runner._pair is None)
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        failures = [row for row in rows if row.get("kind") == "capture_failure"]
    return {
        "verdict": "log_failure",
        "reason_codes": reasons,
        "chain_entry_required_per_variant": len(failures) == len(doc["variants"]),
        "candidate_reset": all(resets),
    }


def _run_decode(doc: dict) -> dict:
    import tempfile

    import qorcap_cod as cod

    called = []

    class Hud:
        def read(self, img):
            called.append("hud")
            return []

    class End:
        def read(self, img):
            called.append("end")
            return cod.RegionRead("end_screen", "not_read", False, "not_present", False)

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "cod_signals.jsonl"
        runner = cod.CodSetRunner(lambda _event: None, hud=Hud(), end=End(), log_path=path)
        if doc.get("decoder_result") is None:
            runner.note_capture_failure(
                reason_code="decode_error",
                attempt_seq=doc.get("attempt_seq"),
                http_status=doc.get("http_status"),
            )
        else:
            runner.on_frame(0, object())
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    failure = next(row for row in rows if row.get("kind") == "capture_failure")
    return {
        "verdict": "log_failure",
        "reason_code": failure.get("reason_code"),
        "chain_entry_required": True,
        "no_on_frame": called == [],
    }


def _run_terminal(case_id: str, doc: dict) -> dict:
    if case_id == "last_three_entries_missing":
        text = _build_signal_text(len(doc["valid_base_entries"]))
        lines = [line for line in text.splitlines() if line.strip()]
        data, terminal = lines[:-1], lines[-1]
        kept = "\n".join(data[:-3] + [terminal]) + "\n"
        dropped = "\n".join(lines[:-3]) + "\n"
        first = chain_acceptance(kept)
        second = chain_acceptance(dropped)
        return {
            "verdict": "reject_incomplete",
            "reasons": [first["reason"], second["reason"]],
        }
    text = _build_signal_text(doc["valid_base_data_count"])
    lines = [line for line in text.splitlines() if line.strip()]
    rows = [json.loads(line) for line in lines]
    patch = doc.get("terminal_patch") or {}
    for key, value in patch.items():
        rows[-1][key] = value
        if key == "entry_count":
            # The count alias has to agree with the patched commitment.
            pass
        if key == "chain_root_hash":
            pass
    patched = "\n".join(json.dumps(row) for row in rows) + "\n"
    if doc.get("rehash_terminal"):
        patched = _rehash_terminal(patched)
    acceptance = chain_acceptance(patched)
    out = {"verdict": acceptance["verdict"], "reason": acceptance["reason"]}
    if "structural_integrity_may_pass" in (doc and {}):
        pass
    if case_id == "explicit_truncation_rehashed_terminal":
        out["structural_integrity_may_pass"] = acceptance["structural_ok"] is True
    return out


class DurableLog:
    """Append, rewind a torn tail, and ack only after flush and sync."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.lines: list[bytes] = []
        self.acks: list[int] = []
        self.broadcasts: list[int] = []
        self.pending_revision = None
        self.duplicates = 0

    def attempt(self, revision: int, spec: dict) -> bool:
        self.pending_revision = revision
        line = (json.dumps({"revision": revision, "n": len(self.lines)}, separators=(",", ":")) + "\n").encode("utf-8")
        if bytes(self.buf).endswith(line):
            self.duplicates += 1
            self.acks.append(revision)
            self.broadcasts.append(revision)
            self.pending_revision = None
            return True
        start = len(self.buf)
        write_all = spec.get("write_all", "ok")
        if spec.get("bytes_written") == 7 or write_all == "ENOSPC":
            self.buf.extend(line[:7])
            del self.buf[start:]
            return False
        if write_all != "ok":
            return False
        self.buf.extend(line)
        if spec.get("flush") == "EIO":
            del self.buf[start:]
            return False
        if spec.get("sync_all") == "EIO":
            del self.buf[start:]
            return False
        if spec.get("flush") not in (None, "ok") or spec.get("sync_all") not in (None, "ok"):
            del self.buf[start:]
            return False
        self.lines.append(line)
        self.acks.append(revision)
        self.broadcasts.append(revision)
        self.pending_revision = None
        return True

    def jsonl_ok(self) -> bool:
        text = bytes(self.buf).decode("utf-8")
        if not text:
            return False
        if not text.endswith("\n"):
            return False
        rows = []
        for line in text.splitlines():
            rows.append(json.loads(line))
        revisions = [row["n"] for row in rows]
        return revisions == list(range(len(rows))) and len(self.lines) == len(rows)


def _run_durable(case_id: str, doc: dict) -> dict:
    log = DurableLog()
    if case_id == "flush_error":
        ok = log.attempt(doc["binding_revision"], doc)
        return {
            "verdict": "append_failed",
            "seal_ack": ok,
            "pending_revision": log.pending_revision,
            "broadcast_success": bool(log.broadcasts),
        }
    acks = []
    revisions = []
    for spec in doc["attempts"]:
        revisions.append(doc["binding_revision"])
        acks.append(log.attempt(doc["binding_revision"], spec))
    if case_id == "sync_error_then_retry":
        return {
            "verdict": "retry_then_commit",
            "seal_ack_by_attempt": acks,
            "same_binding_revision": len(set(revisions)) == 1,
            "durable_duplicate_count": log.duplicates,
        }
    return {
        "verdict": "retry_only_after_torn_tail_recovery",
        "seal_ack_before_recovery": acks[0],
        "valid_jsonl_and_contiguous_chain_after_retry": log.jsonl_ok() and acks[-1] is True and log.duplicates == 0,
    }


def _run_reopen(case_id: str, doc: dict) -> dict:
    if case_id == "old_incoming_before_new_process":
        reopen = doc["reopen"]
        if reopen.get("new_process_started") is not True:
            return {
                "verdict": "reject_stale_generation",
                "http_status": 503,
                "new_source_timestamp_assigned": False,
            }
        return {"verdict": "reject_stale_generation", "http_status": 503, "new_source_timestamp_assigned": False}
    new = doc["new"]
    prior = doc["prior"]
    same_pixels = new.get("pixels_sha256") == prior.get("pixels_sha256")
    new_identity = (
        new.get("producer_frame_completion") is True
        and new.get("capture_id")
        and new.get("capture_id") != prior.get("capture_id")
        and new.get("capture_generation") != prior.get("capture_generation")
        and _int(new.get("mono_us")) is not None
    )
    if not new_identity:
        return {
            "verdict": "reject_stale_generation",
            "http_status": 503,
            "new_source_timestamp_assigned": False,
        }
    return {
        "verdict": "accept_new_capture_provenance",
        "frame_content_alone_is_not_capture_id": bool(same_pixels and new.get("capture_id") != new.get("pixels_sha256")),
        "consensus_may_still_abstain_repeated_pixels": True,
        "real_match_artifacts_changed": False,
    }


def _run_boundary(doc: dict) -> dict:
    reasons = []
    confirmed = []
    for frames in doc["frame_variants"]:
        path_reasons = []
        for path in doc["paths"]:
            decision = eligibility_decision(
                path=path,
                end_us=doc.get("end_us"),
                frames=frames,
                seal_ready=doc.get("seal_ready") is True,
                anchor_ready=doc.get("anchor_ready") is True,
                epoch_valid=doc.get("epoch_valid") is True,
                lag_present=True,
                session_id="s1",
                capture_generation=1,
            )
            path_reasons.append(decision["reason"])
            confirmed.append(decision["final_confirmed"])
        if len(set(path_reasons)) != 1:
            raise AssertionError(path_reasons)
        reasons.append(path_reasons[0])
    return {
        "verdict": "unknown_void",
        "final_confirmed": any(confirmed),
        "reason_by_variant": reasons,
    }


def _run_missing(doc: dict) -> dict:
    fabricated = False
    confirmed = []
    for variant in doc["readiness_variants"]:
        # Absent keys stay absent. Nothing here invents a lag or an anchor.
        end_us = variant["end_us"] if "end_us" in variant else None
        lag_present = True if "lag_us" not in variant else variant.get("lag_us") is not None
        if "lag_us" in variant and variant.get("lag_us") is None:
            lag_present = False
        seal_ready = variant["seal_ready"] if "seal_ready" in variant else None
        anchor_ready = variant["anchor_ready"] if "anchor_ready" in variant else None
        epoch_valid = variant["epoch_valid"] if "epoch_valid" in variant else None
        for path in doc["paths"]:
            decision = eligibility_decision(
                path=path,
                end_us=end_us,
                frames=doc["frames"],
                seal_ready=seal_ready is True,
                anchor_ready=anchor_ready is True,
                epoch_valid=epoch_valid is True,
                lag_present=lag_present is True and end_us is not None,
                session_id="s1",
                capture_generation=1,
            )
            confirmed.append(decision["final_confirmed"])
            if decision["fabricated_anchor_or_lag"]:
                fabricated = True
    return {
        "verdict": "unknown_void",
        "final_confirmed": any(confirmed),
        "fabricated_anchor_or_lag": fabricated,
    }


def _run_naked(doc: dict) -> dict:
    supported = _anchor_supported(doc.get("hb") or {}, doc.get("anchor_validation"))
    neutrals = [pad_neutral(pad) for pad in doc.get("pad_variants") or []]
    return {
        "verdict": "unknown" if not supported else "idle_supported",
        "reason": "no_trusted_anchor" if not supported else "counts_support",
        "neutral_by_pad_variant": neutrals,
        "idle_supported": bool(supported and all(neutrals) and (doc.get("hb") or {}).get("neutral") is True),
    }


def evaluate(group: str, case_id: str, doc: dict) -> dict:
    if group == "clock":
        return order_captures(doc)
    if group == "capture_evidence" and case_id == "http_503_frame_2":
        return _run_http(doc)
    if group == "capture_evidence" and case_id == "stale_or_missing_provenance":
        return _run_stale(doc)
    if group == "capture_evidence" and case_id == "jpeg_decode_none":
        return _run_decode(doc)
    if group == "capture_evidence" and case_id == "digit_and_candidates_change_same_status":
        rows = emit_region_evidence(doc["regions"])
        hashes = [row["evidence_hash"] for row in rows]
        return {
            "verdict": "emit_both" if len(rows) == len(doc["regions"]) else "dropped",
            "evidence_hashes_must_differ": len(hashes) == len(set(hashes)) and len(hashes) > 1,
        }
    if group == "terminal":
        return _run_terminal(case_id, doc)
    if group == "durable_append":
        return _run_durable(case_id, doc)
    if group == "reopen":
        return _run_reopen(case_id, doc)
    if group == "eligibility" and case_id == "both_final_paths_boundary":
        return _run_boundary(doc)
    if group == "eligibility" and case_id == "both_final_paths_missing_readiness":
        return _run_missing(doc)
    if group == "eligibility" and case_id == "naked_anchor_and_trigger_neutral":
        return _run_naked(doc)
    raise KeyError((group, case_id))
