"""Product-path checks for the QorData closure probes.

These call the runner, the capture step, and the shared eligibility gate.
They do not stand in a second model of the answer.
"""
from __future__ import annotations

import hashlib
import json

import pytest

import qorcap_cod as cod
import qorcap_match as match
import qorcap_qordata as qd


class Hud:
    def read(self, image):
        return []


class End:
    def read(self, image):
        return cod.RegionRead("end_screen", "not_read", False, "not_present", False)


def runner(tmp_path):
    return cod.CodSetRunner(lambda event: None, hud=Hud(), end=End(), log_path=tmp_path / "signals.jsonl")


def rehash(rows):
    prev = cod.SIGNAL_GENESIS
    for seq, row in enumerate(rows):
        body = {k: v for k, v in row.items() if k not in ("hash", "prev")}
        digest = hashlib.sha256(cod._canon({"prev": prev, "seq": seq, "row": body}).encode()).hexdigest()
        row.update(prev=prev, hash=digest)
        prev = digest
    return "\n".join(json.dumps(r) for r in rows)


def test_acceptance_rejects_missing_truncation(tmp_path):
    log = runner(tmp_path)
    log._write({"kind": "hud"})
    log.close_signals()
    rows = [json.loads(line) for line in log.log_path.read_text().splitlines()]
    del rows[-1]["truncated"]
    result = qd.chain_acceptance(rehash(rows))
    assert result["verdict"] != "accept_structure", result


@pytest.mark.parametrize("mismatch", ["session_id", "capture_generation", "epoch"])
def test_real_final_gate_rejects_cross_binding(tmp_path, mismatch):
    log = runner(tmp_path)
    log.session_id = "s1"
    log.capture_generation = 1
    log.capture_epoch = 1
    log.seal_durable = True
    log.anchor_validated = True
    log.independent_anchor = "independent-v1:" + "a" * 64
    log.seal_hash = "b" * 64
    log._seal_window = lambda: {"end_us": 100, "releases_counts": True, "status": "resolved"}
    frames = [
        {"capture_id": "a", "mono_us": 101, "session_id": "s1", "capture_generation": 1, "epoch": 1},
        {"capture_id": "b", "mono_us": 102, "session_id": "s1", "capture_generation": 1, "epoch": 1},
    ]
    frames[1][mismatch] = "s2" if mismatch == "session_id" else 2
    log._support = frames
    result = log.final_eligibility(visual_ok=True, evidence_us=102)
    assert result["confirmed"] is False, result
    expected = {
        "session_id": "session_mismatch",
        "capture_generation": "generation_mismatch",
        "epoch": "epoch_mismatch",
    }[mismatch]
    assert expected in result["reason_codes"], result


def test_failure_records_original_http_status_and_observation(tmp_path):
    log = runner(tmp_path)
    log.note_capture_gap("snapshot_http:HTTPError")
    row = json.loads(log.log_path.read_text())
    assert row["http_status"] == 503 and isinstance(row["observed_mono_us"], int), row


def test_digits_survive_actual_public_projection_and_logging(tmp_path):
    log = runner(tmp_path)
    for value in (7, 1):
        rd = cod.RegionRead("scoreboard", "read", True, "same", True, kills=value)
        log._stream_hud(0, [rd], [], {})
    rows = [json.loads(line) for line in log.log_path.read_text().splitlines()]
    assert [x["regions"][0]["digits"]["kills"] for x in rows] == [7, 1]
    assert rows[0]["regions"][0]["evidence_hash"] != rows[1]["regions"][0]["evidence_hash"]


def test_candidate_only_change_is_logged(tmp_path):
    log = runner(tmp_path)
    for value in (7, 1):
        rd = cod.RegionRead("scoreboard", "read", True, "same", True, kills=7)
        rd.candidates = [{"value": value, "score_milli": 900}]
        log._stream_hud(0, [rd], [], {})
    rows = [json.loads(line) for line in log.log_path.read_text().splitlines()]
    assert rows[0]["regions"][0]["evidence_hash"] != rows[1]["regions"][0]["evidence_hash"]


def test_missing_api_version_rejected_before_frame():
    assert match.cod_capture_step({}, b"jpeg", set(), None, True)["error"] == "version_mismatch"


def test_capture_chain_retains_session_and_epoch(tmp_path):
    log = runner(tmp_path)
    log.on_frame(1, object(), source={
        "frame_id": "1:1", "capture_id": "1:1", "clock": "capture_pts", "mono_us": 1000,
        "captured_us": 1000, "clock_id": "epoch-1:producer", "session_id": "s1", "epoch": 1,
        "capture_generation": 1, "seq": 1, "mono_origin_ns": 1234,
    })
    row = json.loads(log.log_path.read_text().splitlines()[0])
    assert row.get("session_id") == "s1" and row.get("epoch") == 1 and row.get("mono_origin_ns") == 1234, row


def test_log_error_does_not_declare_nontruncated(tmp_path, monkeypatch):
    log = runner(tmp_path)
    with monkeypatch.context() as patched:
        patched.setattr(cod.os, "fsync", lambda _: (_ for _ in ()).throw(OSError("injected EIO")))
        assert not log._write({"kind": "hud"})
    assert log.close_signals()
    terminal = json.loads(log.log_path.read_text().splitlines()[-1])
    assert terminal["truncated"] is True, terminal


def test_nonzero_recorder_offset_and_reopen_do_not_use_wall_anchor(tmp_path, monkeypatch):
    log = runner(tmp_path)
    log.wall_anchor_ms = 1_700_000_000_000
    observed = []
    original = cod.correlate_pair

    def observe(**kw):
        observed.append(kw["screen_us"])
        return original(**kw)

    monkeypatch.setattr(cod, "correlate_pair", observe)
    for epoch, offset in ((1, 8_000_000), (2, 18_000_000)):
        log._source = {
            "clock": "capture_pts", "mono_us": 40_000, "epoch": epoch, "recorder_offset_us": offset,
        }
        log._signals(40, {})
    assert observed, "the signal path did not ask for a screen time"
    assert all(item is None or item >= 0 for item in observed), observed


def test_formatted_token_and_boolean_are_not_validation_evidence(tmp_path):
    log = runner(tmp_path)
    log.session_id = "s1"
    log.capture_generation = 1
    log.capture_epoch = 1
    log.seal_durable = True
    log.anchor_validated = True
    log.independent_anchor = "independent-v1:" + "a" * 64
    log.seal_hash = "b" * 64
    log._seal_window = lambda: {"end_us": 100, "releases_counts": True, "status": "resolved"}
    log._support = [{
        "capture_id": "a", "mono_us": 101, "session_id": "s1", "capture_generation": 1, "epoch": 1,
    }]
    decision = log.final_eligibility(visual_ok=True, evidence_us=101)
    assert not decision["confirmed"], "formatted token + boolean was enough without validation evidence"
    assert "anchor_not_validated" in decision["reason_codes"], decision


def _bound_headers(**extra):
    headers = {
        "X-Qorcap-Snapshot-Api": "1",
        "X-Qorcap-Snapshot-Schema": match.SNAPSHOT_SCHEMA,
        "X-Qorcap-Snapshot-Session": "s1",
        "X-Qorcap-Snapshot-Clock": "epoch-1:1234",
        "X-Qorcap-Snapshot-Clock-Source": "monotonic_ns",
        "X-Qorcap-Snapshot-Mono-Origin-Ns": "1234",
        "X-Qorcap-Snapshot-Epoch": "1",
        "X-Qorcap-Snapshot-Generation": "1",
        "X-Qorcap-Snapshot-Mono-Us": "80000",
        "X-Qorcap-Snapshot-Pts-Us": "40000",
        "X-Qorcap-Snapshot-Seq": "1",
        "X-Qorcap-Snapshot-Capture-Id": "1:1:abcd",
    }
    headers.update(extra)
    return headers


def test_four_distinct_snapshots_share_one_media_pts():
    seen = set()
    ids = []
    for seq in range(1, 5):
        body = bytes([0xFF, 0xD8, seq, 0xFF, 0xD9])
        step = match.cod_capture_step(
            _bound_headers(**{
                "X-Qorcap-Snapshot-Seq": str(seq),
                "X-Qorcap-Snapshot-Capture-Id": f"1:{seq}:abcd{seq}",
                "X-Qorcap-Snapshot-Mono-Us": str(80_000 + seq),
            }),
            body,
            seen,
            1_700_000_000_000,
            True,
        )
        assert step.get("ok") is True, step
        assert step["source"]["pts_us"] == 40_000
        assert step["source"]["mono_us"] == 80_000 + seq
        assert step["source"]["mono_us"] != step["source"]["pts_us"]
        assert step["source"]["clock"] == "monotonic_ns"
        ids.append(step["source"]["capture_id"])
    assert ids == [f"1:{seq}:abcd{seq}" for seq in range(1, 5)]


def test_missing_pts_is_not_taken_from_mono():
    headers = _bound_headers()
    del headers["X-Qorcap-Snapshot-Pts-Us"]
    step = match.cod_capture_step(headers, b"\xff\xd8\xff\xd9", set(), 1_700_000_000_000, True)
    assert step.get("ok") is True, step
    assert step["source"]["pts_us"] is None
    assert step["source"]["mono_us"] == 80_000
    assert step["source"]["clock"] == "monotonic_ns"


def test_binding_comparison_covers_a_frame_without_mono(tmp_path):
    log = runner(tmp_path)
    log.session_id = "s1"
    log.capture_generation = 1
    log.capture_epoch = 1
    log.seal_durable = True
    log._seal_window = lambda: {"end_us": 100, "releases_counts": True, "status": "resolved"}
    log._support = [{
        "capture_id": "a", "session_id": "s2", "capture_generation": 1, "epoch": 1,
    }]
    decision = log.final_eligibility(visual_ok=True, evidence_us=None)
    assert decision["confirmed"] is False
    assert "session_mismatch" in decision["reason_codes"], decision


def test_on_frame_binds_session_generation_and_epoch(tmp_path):
    log = runner(tmp_path)
    log.on_frame(1, object(), source={
        "capture_id": "1:1:aa",
        "frame_id": "1:1:aa",
        "clock": "monotonic_ns",
        "clock_source": "monotonic_ns",
        "mono_us": 10,
        "session_id": "match-1",
        "epoch": 3,
        "capture_generation": 3,
        "seq": 1,
        "clock_id": "epoch-3:9",
        "mono_origin_ns": 9,
    })
    assert log.session_id == "match-1"
    assert log.capture_epoch == 3
    assert log.capture_generation == 3
