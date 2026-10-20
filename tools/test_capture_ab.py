"""Offline tests for the capture A/B promotion rule. No card, no Windows."""
from __future__ import annotations

import importlib.util
from pathlib import Path

p = Path(__file__).resolve().parent / "capture_ab.py"
spec = importlib.util.spec_from_file_location("capture_ab", p)
ab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ab)


def _row(**over):
    base = {
        "capture_ok": True,
        "drop_frames": 0,
        "dup_frames": 0,
        "audio_lag_s": 9.8,
        "video_queued_mb": None,
        "buffer_warnings": 100,
        "cpu_pct": 20.0,
        "disk_mb_per_min": 60.0,
        "same_footage": True,
        "reads_attempted": 40,
        "reads_succeeded": 30,
        "wrong_reads": 0,
        "wrong_pairs": [],
        "missed_scores": 0,
        "missed_pairs": [],
        "baseline_locks": 1,
    }
    base.update(over)
    return base


def test_audio_line_from_the_match_log_is_about_ten_seconds():
    w = ab.parse_buffer_warning("real-time buffer [USB3.0 Audio] too full (62% of 3,041,280)")
    assert w["kind"] == "audio"
    assert w["percent"] == 62
    assert w["size_bytes"] == 3_041_280
    assert abs(w["lag_s"] - 9.82) < 0.02
    video = ab.parse_buffer_warning(
        "real-time buffer [USB3.0 Video] too full or near too full (62% of size: 536870912)! frame dropped!"
    )
    assert video["kind"] == "video"
    assert video["lag_s"] is None
    assert abs(video["queued_mb"] - 317.44) < 0.1
    assert ab.parse_buffer_warning("opening the device") is None
    summary = ab.summarize_buffer("note\nreal-time buffer [USB3.0 Audio] too full (62% of 3,041,280)\n")
    assert summary["buffer_warnings"] == 1
    assert abs(summary["audio_lag_s"] - 9.82) < 0.02


def test_equal_variant_may_be_promoted():
    d = ab.promotion_decision(_row(), _row())
    assert d["promote"] is True
    assert d["reasons"] == []
    assert d["disk_raised"] is False


def test_each_gate_blocks_on_its_own():
    assert any("wrong" in r for r in ab.promotion_decision(_row(), _row(wrong_reads=1, wrong_pairs=[[7, 0]]))["reasons"])
    assert any("fewer successful" in r for r in ab.promotion_decision(_row(), _row(reads_succeeded=29))["reasons"])
    assert any("missed" in r for r in ab.promotion_decision(_row(), _row(missed_scores=1, missed_pairs=[[0, 7]]))["reasons"])
    assert any("dropped" in r for r in ab.promotion_decision(_row(), _row(drop_frames=1))["reasons"])
    assert any("duplicate" in r for r in ab.promotion_decision(_row(), _row(dup_frames=2))["reasons"])
    assert any("audio lag" in r for r in ab.promotion_decision(_row(), _row(audio_lag_s=11.0))["reasons"])
    assert any("CPU" in r for r in ab.promotion_decision(_row(), _row(cpu_pct=40.0))["reasons"])
    disk = ab.promotion_decision(_row(), _row(disk_mb_per_min=61.0))
    assert disk["disk_raised"] is True
    assert any("disk" in r for r in disk["reasons"])
    assert any("same footage" in r for r in ab.promotion_decision(_row(), _row(same_footage=False))["reasons"])
    assert any("not proven" in r for r in ab.promotion_decision(_row(baseline_locks=0, reads_succeeded=0), _row(reads_succeeded=0))["reasons"])
    assert any("not measured" in r for r in ab.promotion_decision(_row(), _row(cpu_pct=None))["reasons"])


def test_small_cpu_and_disk_noise_does_not_block():
    # +3 points is under the 8 point gate. +0.04 MB/min is under the 0.05 floor.
    d = ab.promotion_decision(_row(), _row(cpu_pct=23.0, disk_mb_per_min=60.04, audio_lag_s=10.2))
    assert d["promote"] is True, d["reasons"]
    assert d["disk_raised"] is False


def test_score_delta_against_baseline_locks():
    base = [{"home": 7, "away": 0}, {"home": 7, "away": 3}]
    same = ab.score_delta(base, [{"home": 7, "away": 0}, {"home": 7, "away": 3}])
    assert same["wrong_reads"] == 0 and same["missed_scores"] == 0
    bad = ab.score_delta(base, [{"home": 7, "away": 0}, {"home": 8, "away": 0}])
    assert bad["wrong_reads"] == 1 and bad["missed_scores"] == 1
    assert bad["wrong_pairs"] == [[8, 0]]


def test_baseline_command_is_todays_match_capture():
    cmd = ab.record_command("qorcap.exe", r"C:\ffmpeg.exe", 8791, "q.log", "v.mp4", 15, [])
    assert "--input-format" in cmd and cmd[cmd.index("--input-format") + 1] == "mjpeg"
    assert cmd[cmd.index("--res") + 1] == "1280x720"
    assert cmd[cmd.index("--fps") + 1] == "30"
    blob = " ".join(cmd)
    for flag in ("--yuy2-720p60", "--reader-snapshot", "--rtbufsize", "--short-probe",
                 "--buffer-watch", "--no-signal", "--replug-resilient", "--audio-rtbufsize"):
        assert flag not in blob
    names = [n for n, _ in ab.VARIANTS]
    assert names[0] == "baseline" and names.count("baseline") == 1
    assert ab.VARIANTS[0][1] == []


def test_cpu_and_disk_math():
    assert abs(ab.cpu_percent(1.5, 10) - 15.0) < 1e-9
    assert ab.cpu_percent(None, 10) is None
    # 60 MB over 60s is 60 MB/min. 8 Mbit/s is about that.
    assert abs(ab.disk_mb_per_min(60 * 1024 * 1024, 60) - 60.0) < 1e-6
