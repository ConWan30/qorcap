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
    }
    assert seal.dual_bind_passes(late, dict(late), late["seal_hash"]) is False
    locked = {"seal_hash": "cd" * 32, "status": "locked", "opening_gate": True}
    assert seal.dual_bind_passes(locked, dict(locked), locked["seal_hash"]) is True
    assert seal.dual_bind_passes({"seal_hash": None, "status": "failed", "opening_gate": False},
                                  {"seal_hash": None, "opening_gate": False}, None) is False


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
    measured = lag.lag_band(samples)
    assert measured["status"] == "measured" and measured["n"] == 30 and measured["provisional"] is True
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
    runner.on_frame(1000, object())
    row = json.loads((tmp_path / "cod_signals.jsonl").read_text(encoding="utf-8").splitlines()[0])
    signals = {s["signal"]: s for s in row["signals"]}
    assert signals["hitmarker"]["status"] == C.NOT_COUNTED
    assert signals["hitmarker"]["reason"] == "outside_fire_window"
    assert signals["hitmarker"]["gated"] is False
    assert signals["killfeed"]["reason"] == "no_nearby_hitmarker"
    assert row["kind"] == "set" and row["frame_index"] == 0 and row["mono_us"] == 1_000_000
    assert row["prev"] == C.SIGNAL_GENESIS and len(row["hash"]) == 64

    board = cv2.imread(str(FIX / "end_173922_019.jpg"))
    assert board is not None
    black = np.zeros_like(board)
    assert H.frame_is_real(board) is True
    assert H.frame_is_real(black) is False
    assert H.frame_is_real(object()) is False
    short = H.consensus_owner_row([board, board, board, board], 13)
    assert short["status"] == "abstain" and short["reason"] == "short_real_window"
    unknown = H.consensus_owner_row([board] * 5, None)
    assert unknown["reason"] == "no_own_kill_count"
    mismatch = H.consensus_owner_row([board] * 5, 31)
    assert mismatch["reason"] == "own_kill_mismatch"
    agreed = H.consensus_owner_row([board] * 5, 13)
    assert agreed["status"] == "accepted" and agreed["kills"] == 13 and agreed["deaths"] == 13


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
    runner.on_frame(0, object())
    runner.on_frame(100, object())
    runner.on_frame(200, object())
    lines = path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["kind"] == "set"
    assert json.loads(lines[1])["kind"] == "hud"
    assert json.loads(lines[1])["frame_index"] == 1
    assert C.verify_signal_chain(path.read_text(encoding="utf-8")) == []
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
