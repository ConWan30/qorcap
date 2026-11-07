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
        "X-Qorcap-Snapshot-Clock": "engine:1234",
        "X-Qorcap-Snapshot-Clock-Source": "engine_monotonic_ns",
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
        assert step["source"]["clock"] == "engine_monotonic_ns"
        ids.append(step["source"]["capture_id"])
    assert ids == [f"1:{seq}:abcd{seq}" for seq in range(1, 5)]


def test_missing_pts_is_not_taken_from_mono():
    headers = _bound_headers()
    del headers["X-Qorcap-Snapshot-Pts-Us"]
    step = match.cod_capture_step(headers, b"\xff\xd8\xff\xd9", set(), 1_700_000_000_000, True)
    assert step.get("ok") is True, step
    assert step["source"]["pts_us"] is None
    assert step["source"]["mono_us"] == 80_000
    assert step["source"]["clock"] == "engine_monotonic_ns"


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


ENGINE_ORIGIN = 7_000_000_000


def _engine_source(epoch, seq, start_us, pts_us, origin=ENGINE_ORIGIN):
    step = match.cod_capture_step(
        _bound_headers(**{
            "X-Qorcap-Snapshot-Clock": f"engine:{origin}",
            "X-Qorcap-Snapshot-Mono-Origin-Ns": str(origin),
            "X-Qorcap-Snapshot-Epoch": str(epoch),
            "X-Qorcap-Snapshot-Generation": str(epoch),
            "X-Qorcap-Snapshot-Seq": str(seq),
            "X-Qorcap-Snapshot-Capture-Id": f"{epoch}:{seq}:cafe{seq}",
            "X-Qorcap-Snapshot-Producer-Start-Us": str(start_us),
            "X-Qorcap-Snapshot-Pts-Us": str(pts_us),
            "X-Qorcap-Snapshot-Mono-Us": str(start_us + pts_us),
        }),
        bytes([0xFF, 0xD8, epoch, seq, 0xFF, 0xD9]),
        set(),
        1_700_000_000_000,
        True,
    )
    assert step.get("ok") is True, step
    return step["source"]


def test_nonzero_producer_offset_and_reopen_map_onto_the_input_clock(tmp_path, monkeypatch):
    log = runner(tmp_path)
    log.set_signals([{"type": "header", "mono_origin_ns": ENGINE_ORIGIN, "wall_anchor_ms": 1}], [], [], 1)
    observed = []
    original = cod.correlate_pair

    def observe(**kw):
        observed.append(kw["screen_us"])
        return original(**kw)

    monkeypatch.setattr(cod, "correlate_pair", observe)
    sources = [_engine_source(1, 1, 8_000_000, 40_000), _engine_source(2, 1, 18_000_000, 40_000)]
    for source in sources:
        assert source["producer_start_us"] in (8_000_000, 18_000_000)
        assert source["clock_id"] == f"engine:{ENGINE_ORIGIN}"
        log._source = source
        log._signals(source["mono_us"] // 1000, {})
        assert log._capture_ref()["mono_us"] == source["mono_us"]
        assert log._evidence_us() == source["mono_us"]
    assert observed and sorted(set(observed)) == [8_040_000, 18_040_000], "screen time is engine time, not media PTS"
    log.on_frame(sources[1]["mono_us"] // 1000, object(), source=sources[1])
    row = json.loads(log.log_path.read_text().splitlines()[-1])
    assert row["mono_us"] == 18_040_000 and row["clock"] == "engine_monotonic_ns", row
    assert row["mono_origin_ns"] == ENGINE_ORIGIN


@pytest.mark.parametrize("input_origin, source_patch", [
    (None, {}),
    (ENGINE_ORIGIN + 1, {}),
    (ENGINE_ORIGIN, {"mono_origin_ns": 0}),
    (0, {"mono_origin_ns": 0}),
    (ENGINE_ORIGIN, {"clock": "monotonic_ns", "clock_source": "monotonic_ns"}),
    (ENGINE_ORIGIN, {"clock": "capture_pts", "clock_source": "producer_instant"}),
])
def test_screen_time_stays_unknown_without_one_engine_origin(tmp_path, input_origin, source_patch):
    log = runner(tmp_path)
    header = {"type": "header", "wall_anchor_ms": 1}
    if input_origin is not None:
        header["mono_origin_ns"] = input_origin
    log.set_signals([header], [], [], 1)
    source = dict(_engine_source(1, 1, 8_000_000, 40_000))
    source.update(source_patch)
    log._source = source
    assert log._screen_us() is None
    assert log._capture_ref()["mono_us"] is None
    assert log._evidence_us() is None


@pytest.mark.parametrize("patch", [
    {"X-Qorcap-Snapshot-Mono-Origin-Ns": "0"},
    {"X-Qorcap-Snapshot-Clock-Source": "monotonic_ns"},
    {"X-Qorcap-Snapshot-Clock-Source": "producer_instant"},
])
def test_zero_or_legacy_snapshot_binding_is_rejected(patch):
    step = match.cod_capture_step(_bound_headers(**patch), b"\xff\xd8\xff\xd9", set(), None, True)
    assert step == {"error": "clock_binding_missing"}


def test_generation_is_the_epoch_and_a_per_frame_counter_is_rejected(tmp_path):
    step = match.cod_capture_step(
        _bound_headers(**{"X-Qorcap-Snapshot-Generation": "7"}), b"\xff\xd8\xff\xd9", set(), None, True)
    assert step["error"] == "provenance_generation_mismatch", step
    log = runner(tmp_path)
    log.set_signals([{"type": "header", "mono_origin_ns": ENGINE_ORIGIN}], [], [], None)
    for seq in range(1, 5):
        log.on_frame(seq, object(), source=_engine_source(1, seq, 8_000_000, 40_000 + seq))
    assert log.epoch_fault is False, "four frames in one epoch are one generation"
    log.on_frame(9, object(), source=_engine_source(2, 1, 18_000_000, 40_000))
    assert log.epoch_fault is True, "a new producer epoch is a new generation"


def test_stale_snapshot_is_not_a_capture(tmp_path):
    headers = {"X-Qorcap-Snapshot-State": "stale", "X-Qorcap-Snapshot-Age-Us": "2500000"}
    step = match.cod_capture_step(headers, b"\xff\xd8\xff\xd9", set(), None, True)
    assert step["error"] == "stale_snapshot", step
    log = runner(tmp_path)
    assert log.note_capture_gap(step["error"])
    row = json.loads(log.log_path.read_text())
    assert row["reason_code"] == "stale_snapshot" and row["http_status"] is None, row
    assert row["capture_id"] is None and row["mono_us"] is None


@pytest.mark.parametrize("reason", [
    "snapshot_http:URLError", "snapshot_http:TimeoutError", "snapshot_http:timeout",
    "snapshot_http:ConnectionRefusedError", "snapshot_http:RemoteDisconnected",
])
def test_no_response_has_no_http_status(tmp_path, reason):
    log = runner(tmp_path)
    assert log.note_capture_gap(reason)
    row = json.loads(log.log_path.read_text())
    assert row["http_status"] is None and row["response"] == "none", row
    assert row["reason_code"] == "http_error"


def test_an_explicit_status_is_kept(tmp_path):
    log = runner(tmp_path)
    assert log.note_capture_gap("snapshot_http:HTTPError", http_status=503)
    row = json.loads(log.log_path.read_text())
    assert row["http_status"] == 503 and row["response"] == "http", row


class BrokenHud:
    def read(self, image):
        raise ValueError("bad crop")


def test_hud_exception_is_a_chained_fault(tmp_path):
    log = cod.CodSetRunner(lambda event: None, hud=BrokenHud(), end=End(), log_path=tmp_path / "signals.jsonl")
    log.on_frame(5, object(), source=None)
    assert log.close_signals()
    text = log.log_path.read_text()
    rows = [json.loads(line) for line in text.splitlines()]
    faults = [r for r in rows if r.get("kind") == "hook_fault"]
    assert len(faults) == 1, rows
    assert faults[0]["hook"] == "hud" and faults[0]["reason_code"] == "hud_hook_error"
    assert faults[0]["error"] == "ValueError"
    assert cod.verify_signal_chain(text) == []
    assert qd.chain_acceptance(text)["verdict"] == "accept_structure"


def _one_terminal(log):
    text = log.log_path.read_text()
    rows = [json.loads(line) for line in text.splitlines()]
    terminals = [r for r in rows if r.get("kind") == "terminal"]
    assert len(terminals) == 1, rows
    terminal = terminals[0]
    assert rows[-1] is terminal
    body = rows[:-1]
    assert terminal["count"] == len(body) == terminal["chain_seq"]
    assert terminal["last_seq"] == len(body) - 1
    assert terminal["root"] == body[-1]["hash"] == terminal["prev"]
    assert terminal["truncated"] is False
    assert cod.verify_signal_chain(text) == []
    assert qd.chain_acceptance(text)["verdict"] == "accept_structure"


def test_terminal_fsync_failure_then_retry_leaves_one_terminal(tmp_path, monkeypatch):
    log = runner(tmp_path)
    assert log._write({"kind": "hud"})
    real = cod.os.fsync
    calls = {"n": 0}

    def fail_once(fd):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("injected EIO")
        return real(fd)

    monkeypatch.setattr(cod.os, "fsync", fail_once)
    assert log.close_signals() is False
    assert len(log.log_path.read_text().splitlines()) == 1, "the unsynced terminal is cut back"
    assert log.close_signals() is True
    assert log.close_signals() is False, "a closed log does not write a second terminal"
    _one_terminal(log)


def test_terminal_fsync_failing_until_close_retry_leaves_one_terminal(tmp_path, monkeypatch):
    log = runner(tmp_path)
    assert log._write({"kind": "hud"})
    assert log._write({"kind": "hud"})
    with monkeypatch.context() as patched:
        patched.setattr(cod.os, "fsync", lambda _: (_ for _ in ()).throw(OSError("injected EIO")))
        assert log.close_signals() is False
        assert log.close_signals() is False
    assert log.close_signals() is True
    _one_terminal(log)


def test_zero_origin_does_not_order_captures():
    doc = {
        "clock_id": "engine:0", "clock_source": "engine_monotonic_ns", "mono_origin_ns": 0,
        "frames": [{"capture_id": "a", "mono_us": 2}, {"capture_id": "b", "mono_us": 1}],
    }
    assert qd.order_captures(doc)["verdict"] == "unknown_void"
    doc["mono_origin_ns"] = ENGINE_ORIGIN
    assert qd.order_captures(doc)["ordered_capture_ids"] == ["b", "a"]
