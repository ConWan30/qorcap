"""The six voids from practice match-20261106-201029.

Synthetic rows and the redacted end-board fixture. Thresholds that are not
checked on a real match stay PROVISIONAL in the modules these tests call.
"""
import json
from pathlib import Path

import cv2
import numpy as np

import qorcap_cod as C
import qorcap_cod_hud as H
import qorcap_console as qc
import qorcap_lag as lag
import qorcap_match as qm
import qorcap_seal as seal

FIX = Path(__file__).resolve().parent / "fixtures" / "cod"


def test_seal_status_is_a_console_line_and_a_late_hash_does_not_open():
    waiting = qm.status_line(
        {"state": "capturing", "recording_secs": 4, "input": {"seal_line": "Seal: waiting"}},
        {}, 0, game="cod")
    assert waiting.endswith("Seal: waiting")
    quiet = qm.status_line({"state": "capturing", "recording_secs": 4}, {}, 0)
    assert "Seal:" not in quiet
    snap = qc.console_snapshot(
        {"state": "capturing", "input": {"state": "connected", "seal_line": "Seal: locked"}},
        {}, 0, None)
    text = qc.grid_text(qc.layout_dashboard(snap, width=78, height=18, tick=1, unicode=False))
    assert "Seal: locked" in text
    late = {
        "seal_hash": "ab" * 32, "status": "failed", "opening_gate": False, "reason": "late",
        "anchor_ack": "external-anchor",
    }
    assert seal.dual_bind_passes(late, dict(late), late["seal_hash"], "external-anchor") is False
    locked = {"seal_hash": "cd" * 32, "status": "locked", "opening_gate": True}
    assert seal.dual_bind_passes(locked, dict(locked), locked["seal_hash"], "external-anchor") is False
    acked = dict(locked, anchor_ack="external-anchor")
    assert seal.dual_bind_passes(acked, dict(acked), acked["seal_hash"], "external-anchor") is True
    assert seal.dual_bind_passes(acked, dict(acked), acked["seal_hash"], acked["seal_hash"]) is False
    assert seal.dual_bind_passes({"seal_hash": None, "status": "failed", "opening_gate": False},
                                  {"seal_hash": None, "opening_gate": False}, None, None) is False


def test_one_options_hold_does_not_measure_lag_and_exclusion_fails_closed():
    band = lag.lag_band([{"method": "options_pause", "lag_us": 180_000, "t_us": 1_500_000}])
    assert band["status"] == "not_measured"
    assert band["reason"] == "options_hold_alone"
    two = lag.lag_band([
        {"method": "options_pause", "lag_us": 180_000, "t_us": 1},
        {"method": "options_pause", "lag_us": 190_000, "t_us": 2},
    ])
    assert two["status"] == "not_measured"
    samples = [{"method": "ads_zoom", "lag_us": 40_000, "t_us": i * 1_000_000} for i in range(29)]
    samples.append({"method": "options_pause", "lag_us": 40_000, "t_us": 29_000_000})
    pooled = lag.lag_band(samples)
    assert pooled["status"] == "not_measured"
    copies = [{
        "method": "options_pause", "lag_us": 180_000, "t_us": 1_500_000,
        "probe_id": "options:1", "source_frame": "frame-1", "outcome": "observed",
    } for _ in range(30)]
    assert lag.lag_band(copies)["status"] == "not_measured"
    assert lag.lag_band(copies)["reason"] == "options_hold_alone"
    unique_options = [{
        "method": "options_pause", "lag_us": 180_000, "t_us": i * 1_000_000,
        "probe_id": f"options:{i}", "source_frame": f"frame-{i}", "outcome": "observed",
    } for i in range(30)]
    refused = lag.lag_band(unique_options)
    assert refused["status"] == "not_measured" and refused["reason"] == "not_a_causal_lag"
    ads = [{
        "method": "ads_zoom", "lag_us": 40_000, "t_us": i * 1_000_000,
        "probe_id": f"ads:{i}", "source_frame": f"src-{i}", "outcome": "observed",
    } for i in range(30)]
    diagnostic = lag.lag_band(ads)
    assert diagnostic["status"] == "diagnostic" and diagnostic["reason"] == "not_a_causal_lag"
    assert diagnostic["n"] == 30 and diagnostic["provisional"] is True
    assert lag.LagProbe().measured_us() is None
    same_time = [dict(row, t_us=0) for row in ads]
    assert lag.lag_band(same_time)["reason"] == "no_time_coverage"
    spread = [{"method": "ads_zoom", "lag_us": 10_000 if i < 20 else 80_000, "t_us": i} for i in range(30)]
    assert lag.lag_band(spread)["status"] == "not_measured"
    missing_menu = seal.seal_exclusion(
        options_release_us=2_000_000, menu_exit_us=None, lag_us=40_000, frame_us=16_667)
    assert missing_menu["status"] == "unresolved" and missing_menu["releases_counts"] is False
    assert missing_menu["end_us"] is None
    assert seal.event_released(missing_menu, 10**12) is False
    missing_lag = seal.seal_exclusion(
        options_release_us=2_000_000, menu_exit_us=3_000_000, lag_us=None, frame_us=16_667)
    assert missing_lag["reason"] == "lag_not_measured"
    assert seal.event_released(missing_lag, 9_000_000) is False
    resolved = seal.seal_exclusion(
        options_release_us=1_000_000, menu_exit_us=1_200_000, lag_us=500_000, frame_us=16_667)
    assert resolved["releases_counts"] is True
    assert seal.event_released(resolved, resolved["end_us"] - 1) is False
    assert seal.event_released(resolved, resolved["end_us"]) is True


def test_hitmarker_outside_the_fire_window_is_not_counted_and_consensus_abstains(tmp_path):
    class Hud:
        def read(self, img):
            hit = C.RegionRead("hitmarker", "read", True, "hitmarker", True)
            feed = C.RegionRead("killfeed", "read", True, "killfeed_icon", True)
            return [hit, feed]

    class End:
        def read(self, img):
            return C.RegionRead("end_screen", "not_read", False, "not_present", False)

    runner = C.CodSetRunner(lambda _e: None, hud=Hud(), end=End(), log_path=tmp_path / "cod_signals.jsonl")
    runner.wall_anchor_ms = 0
    runner.set_signals(
        [{"type": "in", "t_us": 0, "b": 0, "r2": 0},
         {"type": "in", "t_us": 100_000, "b": 0, "r2": 200}],
        [1_000_000], [], 0)
    runner.on_frame(1000, object(), source={"frame_id": "frame-a", "seq": 12, "captured_us": 1_000_000})
    row = json.loads((tmp_path / "cod_signals.jsonl").read_text(encoding="utf-8").splitlines()[0])
    signals = {s["signal"]: s for s in row["signals"]}
    assert signals["hitmarker"]["status"] == C.NOT_COUNTED
    assert signals["hitmarker"]["reason"] == "outside_fire_window"
    assert signals["hitmarker"]["gated"] is False
    assert signals["killfeed"]["reason"] == "no_nearby_hitmarker"
    assert row["kind"] == "set" and row["frame_index"] == 12 and row["mono_us"] is None
    assert row["clock"] == "unbound"
    assert row["anchored"] is False and row["source_frame"]["frame_id"] == "frame-a"
    assert row["prev"] == C.SIGNAL_GENESIS and len(row["hash"]) == 64
    assert runner.own_kill_coverage()["coverage"] == "unknown"

    board = cv2.imread(str(FIX / "end_173922_019.jpg"))
    assert board is not None
    black = np.zeros_like(board)
    assert H.frame_is_real(board) is True
    assert H.frame_is_real(black) is False
    assert H.frame_is_real(object()) is False
    short = H.consensus_owner_row([board, board, board, board], 13)
    assert short["status"] == "abstain" and short["reason"] == "short_real_window"
    repeated = H.consensus_owner_row([board] * 5, None)
    assert repeated["reason"] == "no_capture_provenance"
    proven = []
    for i in range(5):
        frame = board.copy()
        frame[0, i] = (i * 40) % 256
        proven.append({
            "img": frame, "source_id": f"src-{i}", "captured_us": i * 1_000_000, "pixel_id": f"pix-{i}",
        })
    unknown_feed = H.consensus_owner_row(proven, None)
    assert unknown_feed["status"] == "accepted" and unknown_feed["kills"] == 13
    assert unknown_feed["feed_check"] == "unknown"
    mismatch = H.consensus_owner_row(proven, {
        "coverage": "covered", "ownership_qualified": True, "deduped": True, "kills": 31,
    })
    assert mismatch["reason"] == "feed_inconsistent" and mismatch["kills"] == 13
    agreed = H.consensus_owner_row(proven, {
        "coverage": "covered", "ownership_qualified": True, "deduped": True, "kills": 13,
    })
    assert agreed["status"] == "accepted" and agreed["feed_check"] == "consistent"


def test_signal_rows_chain_and_keep_moving(tmp_path):
    class Hud:
        def __init__(self):
            self.n = 0

        def read(self, img):
            self.n += 1
            reason = "present" if self.n == 2 else "not_present"
            ok = self.n == 2
            return [C.RegionRead("hitmarker", "read" if ok else "not_read", ok, reason, ok)]

    class End:
        def read(self, img):
            return C.RegionRead("end_screen", "not_read", False, "not_present", False)

    path = tmp_path / "cod_signals.jsonl"
    runner = C.CodSetRunner(lambda _e: None, hud=Hud(), end=End(), log_path=path)
    runner.on_frame(0, object(), source={"frame_id": "a", "seq": 4, "captured_us": 10})
    runner.on_frame(100, object(), source={"frame_id": "b", "seq": 5, "captured_us": 20})
    runner.on_frame(200, object(), source={"frame_id": "c", "seq": 6, "captured_us": 30})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["kind"] == "set"
    assert json.loads(lines[1])["kind"] == "hud"
    assert json.loads(lines[1])["frame_index"] == 5
    assert json.loads(lines[1])["anchored"] is False
    assert C.verify_signal_chain("\n".join(lines))
    assert runner.close_signals() is True
    assert C.verify_signal_chain(path.read_text(encoding="utf-8")) == []
    terminal = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert terminal["kind"] == "terminal" and terminal["complete"] is False and terminal["count"] == len(lines)
    stuck = runner._chain_seq
    runner.log_path = tmp_path
    assert runner._write({"t_ms": 1, "kind": "hud", "signals": []}) is False
    assert runner._chain_seq == stuck
    tampered = path.read_text(encoding="utf-8").replace("hud", "nope", 1)
    assert C.verify_signal_chain(tampered)


def test_stick_stays_counted_grip_off_rumble_info_only():
    assert "stick_turn_vs_camera" not in C.INFO_ONLY
    assert C.COUNTED_SIGNALS["stick_turn_vs_camera"]["status"] == "Counted"
    assert "triple" in C.COUNTED_SIGNALS["stick_turn_vs_camera"]["note"]
    assert C.INFO_ONLY["rumble_vs_hit"]["label"] == "INFO-ONLY"
    doc = C.session_document()
    assert doc["grip"] == "off"
    assert doc["counted"]["stick_turn_vs_camera"]["label"] == "Counted"
    assert doc["info_only"]["rumble_vs_hit"]["status"] == C.NOT_COUNTED


def _blank_end():
    class End:
        def read(self, img):
            return C.RegionRead("end_screen", "not_read", False, "not_present", False)

    return End()


def test_wall_time_is_not_stored_as_mono_us(tmp_path):
    wall = 1_700_000_000_000
    clocks = qm.split_clocks(wall_ms=wall, pts_us=50_000, anchor_wall_ms=wall - 10)
    assert clocks["clock"] == "unbound"
    assert clocks["wall_ms"] == wall
    assert clocks["pts_us"] == 50_000
    assert clocks["mono_us"] is None
    assert clocks["mono_us"] != wall * 1000
    ordered = qm.order_captures({
        "session_id": "s1",
        "clock_id": "boot1:producer1",
        "clock_source": "monotonic_ns",
        "mono_origin_ns": 9_000_000_000,
        "frames": [
            {"capture_id": "a", "mono_us": 1_000_000, "wall_time_ms": wall + 2_000},
            {"capture_id": "b", "mono_us": 1_250_000, "wall_time_ms": wall},
        ],
    })
    assert ordered["ordered_capture_ids"] == ["a", "b"]
    assert ordered["wall_time_used_for_order"] is False

    class Hud:
        def read(self, img):
            return []

    runner = C.CodSetRunner(lambda _e: None, hud=Hud(), end=_blank_end(), log_path=tmp_path / "wall.jsonl")
    runner.wall_anchor_ms = 0
    runner.on_frame(1000, object(), source={
        "frame_id": "wall-frame", "seq": 1, "captured_us": wall * 1000, "clock": "wall", "wall_ms": wall,
    })
    row = json.loads((tmp_path / "wall.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["mono_us"] is None and row["clock"] == "wall" and row["wall_ms"] == wall
    runner.on_frame(1000, object(), source={"frame_id": "bare", "seq": 2, "captured_us": 9_999_999})
    bare = json.loads((tmp_path / "wall.jsonl").read_text(encoding="utf-8").splitlines()[1])
    assert bare["mono_us"] is None
    assert bare["mono_us"] != 9_999_999
    assert bare["clock"] == "unbound"


def test_signal_stream_keeps_every_frame_digit_change_and_capture_gap(tmp_path):
    class Hud:
        def __init__(self):
            self.kills = 1

        def read(self, img):
            return [C.RegionRead("scoreboard", "read", True, "scoreboard_ka", True, kills=self.kills, assists=0)]

    path = tmp_path / "cod_signals.jsonl"
    runner = C.CodSetRunner(lambda _e: None, hud=Hud(), end=_blank_end(), log_path=path)
    runner.on_frame(0, object(), source={"frame_id": "a", "seq": 1, "captured_us": 10})
    runner.on_frame(10, object(), source={"frame_id": "b", "seq": 2, "captured_us": 20})
    runner.hud.kills = 2
    runner.on_frame(20, object(), source={"frame_id": "c", "seq": 3, "captured_us": 30})
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["kind"] for row in rows] == ["set", "hud", "hud"]
    assert [row["eval_n"] for row in rows] == [1, 2, 3]
    assert [row["frame_index"] for row in rows] == [1, 2, 3]
    assert rows[0]["regions"][0]["kills"] == 1
    assert rows[1]["regions"][0]["kills"] == 1
    assert rows[2]["regions"][0]["kills"] == 2
    assert rows[0]["regions"][0]["status"] == rows[2]["regions"][0]["status"]
    assert all(row["end_eval"]["reason"] == "end_not_read" for row in rows)
    assert all(row["evaluated"] is True for row in rows)
    assert runner.note_capture_gap("snapshot_http:URLError") is True
    assert runner.note_capture_gap("snapshot_api_incompatible") is True
    assert runner.note_capture_gap("snapshot_decode") is True
    chained = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["reason_code"] for row in chained if row["kind"] == "capture_failure"] == [
        "http_error", "version_mismatch", "decode_error",
    ]
    assert all(row.get("capture_id") is None for row in chained if row["kind"] == "capture_failure")
    runner.log_path = tmp_path
    assert runner.close_signals() is False
    assert runner._signals_closed is False
    runner.log_path = path
    assert runner.close_signals() is True
    assert runner.close_signals() is False
    assert C.verify_signal_chain(path.read_text(encoding="utf-8")) == []
    runner.log_path = tmp_path
    before = runner.stats["signal_log_failures"]
    stuck = runner._chain_seq
    assert runner.note_capture_gap("snapshot_http:OSError") is False
    assert runner.stats["signal_log_failures"] == before + 1
    assert runner._chain_seq == stuck


def test_snapshot_api_version_is_required_and_madden_stays_on_its_clock():
    import inspect

    seen = set()
    fresh = qm.snapshot_submission(qm.SNAPSHOT_API_VERSION, 1, 3, 1_000, "abc", seen)
    assert fresh["ok"] is True and fresh["pts_us"] == 1_000 and fresh["seq"] == 3
    assert "captured_us" not in fresh
    assert qm.snapshot_submission(qm.SNAPSHOT_API_VERSION, 1, 3, 2_000, "def", seen)["error"] == "snapshot_repeated"
    assert qm.snapshot_submission(None, 1, 4, 3_000, "ghi", set())["error"] == "snapshot_api_incompatible"
    assert qm.snapshot_submission(2, 1, 4, 3_000, "ghi", set())["error"] == "snapshot_api_incompatible"
    legacy = qm.cod_capture_step({}, b"\xff\xd8\xff\xd9", set(), 10, True)
    assert legacy["error"] == "version_mismatch"
    headers = {
        "X-Qorcap-Snapshot-Api": "1",
        "X-Qorcap-Snapshot-Schema": qm.SNAPSHOT_SCHEMA,
        "X-Qorcap-Snapshot-Epoch": "1",
        "X-Qorcap-Snapshot-Seq": "1",
        "X-Qorcap-Snapshot-Pts-Us": "40000",
    }
    assert qm.cod_capture_step(headers, b"jpeg-a", set(), None, True)["error"] == "clock_binding_missing"
    bound_headers = dict(headers)
    bound_headers.update({
        "X-Qorcap-Snapshot-Session": "s1",
        "X-Qorcap-Snapshot-Clock": "boot1:producer1",
        "X-Qorcap-Snapshot-Clock-Source": "producer_instant",
        "X-Qorcap-Snapshot-Mono-Origin-Ns": "0",
        "X-Qorcap-Snapshot-Capture-Id": "c1",
        "X-Qorcap-Snapshot-Generation": "1",
        "X-Qorcap-Snapshot-Mono-Us": "40000",
    })
    # A child-relative clock with a zero origin is not on the controller clock.
    assert qm.cod_capture_step(bound_headers, b"jpeg-b", set(), 10, True)["error"] == "clock_binding_missing"
    bound_headers.update({
        "X-Qorcap-Snapshot-Clock": "engine:7000000000",
        "X-Qorcap-Snapshot-Clock-Source": "engine_monotonic_ns",
        "X-Qorcap-Snapshot-Mono-Origin-Ns": "7000000000",
    })
    bound = qm.cod_capture_step(bound_headers, b"jpeg-b", set(), 10, True)
    assert bound["source"]["clock"] == "engine_monotonic_ns"
    assert bound["source"]["mono_us"] == 40_000
    assert bound["source"]["mono_us"] != 10 * 1000
    assert bound["source"]["capture_id"] == "c1"
    old_schema = dict(bound_headers)
    old_schema["X-Qorcap-Snapshot-Schema"] = "qorcap-snapshot/v0"
    assert qm.cod_capture_step(old_schema, b"jpeg-old", set(), 10, True)["error"] == "version_mismatch"
    assert qm.cod_capture_step(bound_headers, b"jpeg-c", set(), 10, False)["error"] == "snapshot_decode"
    madden = inspect.getsource(qm.start_reader)
    assert "snapshot_submission" not in madden
    assert "cod_capture_step" not in madden
    assert "t_ms = int(time.time() * 1000)" in madden
