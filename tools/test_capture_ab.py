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
    # Baseline 60 MB/min tolerates max(4, 10%) = 6, so 70 is over and 63 is not.
    disk = ab.promotion_decision(_row(), _row(disk_mb_per_min=70.0))
    assert disk["disk_raised"] is True
    assert any("disk" in r for r in disk["reasons"])
    assert any("same footage" in r for r in ab.promotion_decision(_row(), _row(same_footage=False))["reasons"])
    assert any("not proven" in r for r in ab.promotion_decision(_row(baseline_locks=0, reads_succeeded=8), _row(reads_succeeded=8))["reasons"])
    assert any("not measured" in r for r in ab.promotion_decision(_row(), _row(cpu_pct=None))["reasons"])
    assert any("not measured" in r for r in ab.promotion_decision(_row(), _row(disk_mb_per_min=None))["reasons"])


def test_small_cpu_and_disk_noise_does_not_block():
    # +3 points is under the 8 point gate. +3 MB/min is inside max(4, 10% of 60).
    d = ab.promotion_decision(_row(), _row(cpu_pct=23.0, disk_mb_per_min=63.0, audio_lag_s=10.2))
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
    assert ab.cpu_percent(0.0, 10) is None
    assert ab.cpu_percent(1.5, 0) is None
    # 60 MB over 60s is 60 MB/min. 8 Mbit/s is about that.
    assert abs(ab.disk_mb_per_min(60 * 1024 * 1024, 60) - 60.0) < 1e-6
    assert ab.parse_duration("15.5\n") == 15.5
    assert ab.parse_duration("N/A") is None
    assert ab.parse_duration("0") is None
    assert ab.parse_duration("") is None


def test_zero_cpu_samples_do_not_pass():
    assert ab.cpu_percent_from_samples([]) is None
    assert ab.cpu_percent_from_samples([(0.0, 0.0)]) is None
    assert ab.cpu_percent_from_samples([(0.0, 0.0), (2.0, 0.0)]) is None
    assert ab.cpu_percent_from_samples([(0.0, 1.0), (2.0, 1.0)]) is None
    assert ab.cpu_percent_from_samples([(0.0, 0.0), (0.2, 0.5)]) is None
    assert abs(ab.cpu_percent_from_samples([(0.0, 0.0), (1.0, 0.5)]) - 50.0) < 1e-9
    flat = ab.promotion_decision(_row(), _row(cpu_pct=0.0))
    assert flat["promote"] is False
    assert any("not measured" in r for r in flat["reasons"])
    assert ab.promotion_label(flat) == "undecided"
    both_zero = ab.promotion_decision(_row(cpu_pct=0.0), _row(cpu_pct=0.0))
    assert both_zero["promote"] is False


def test_tiny_read_counts_are_undecided():
    d = ab.promotion_decision(_row(reads_succeeded=2), _row(reads_succeeded=1))
    assert d["promote"] is False
    assert any("too few score reads" in r for r in d["reasons"])
    assert not any("fewer successful" in r for r in d["reasons"])
    assert ab.promotion_label(d) == "undecided"
    # Enough samples still fail a real drop in agreed frames.
    fewer = ab.promotion_decision(_row(reads_succeeded=20), _row(reads_succeeded=8))
    assert any("fewer successful" in r for r in fewer["reasons"])
    assert ab.promotion_label(fewer) == "no"


def test_scorebug_region_flags_a_change_and_ignores_a_still_bar():
    width, height = 32, 8
    still = bytes([20] * (width * height))
    same = ab.take_picture_stable([(width, still), (width, still), (width, bytes(still))])
    assert same["stable"] is True
    moved = bytearray(still)
    for i in range(16):
        moved[i] = 220
    changed = ab.take_picture_stable([(width, still), (width, bytes(moved))])
    assert changed["stable"] is False
    assert "changed" in changed["reason"]
    few = ab.take_picture_stable([(width, still)])
    assert few["stable"] is None
    # One stray frame in a paused take is not a score change.
    stray = [(width, still)] * 12 + [(width, bytes(moved))]
    assert ab.take_picture_stable(stray)["stable"] is True
    mean, tile = ab.region_distance(still, bytes(moved), width)
    assert tile > ab.TILE_LIMIT
    assert mean > 0
    assert ab.regions_match((width, still), (width, still))["stable"] is True
    assert ab.regions_match((width, still), (width, bytes(moved)))["stable"] is False


def test_baseline_is_a_reference_not_a_promotion():
    assert ab.promotion_label({"promote": False, "role": "reference", "reasons": []}) == "ref"
    assert ab.promotion_label({"promote": True, "reasons": []}) == "YES"
    assert ab.promotion_label({"promote": False, "reasons": ["more dropped frames (1 > 0)"]}) == "no"
    assert ab.VARIANTS[0] == ("baseline", [])
    assert "11.6" in ab.YUY2_NOT_RECOMMENDED
    assert "not recommended" in ab.YUY2_NOT_RECOMMENDED.lower()


def test_ffmpeg_resolver_uses_the_bundle_and_rejects_a_missing_path(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "qorcap.exe").write_bytes(b"")
    (bundle / "ffmpeg.exe").write_bytes(b"")
    assert ab.resolve_ffmpeg(None, str(bundle / "qorcap.exe"), use_path=False) == str(bundle / "ffmpeg.exe")
    missing = tmp_path / "missing" / "ffmpeg.exe"
    try:
        ab.resolve_ffmpeg(str(missing), str(bundle / "qorcap.exe"), use_path=False)
        raise AssertionError("missing --ffmpeg should stop")
    except SystemExit as e:
        assert "--ffmpeg" in str(e)
        assert str(missing) in str(e)
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "qorcap.exe").write_bytes(b"")
    try:
        ab.resolve_ffmpeg(None, str(empty / "qorcap.exe"), use_path=False)
        raise AssertionError("bundle without ffmpeg should stop")
    except SystemExit as e:
        text = str(e)
        assert "--ffmpeg" in text
        assert "release bundle" in text
    hint = ab.venv_python_hint()
    assert hint.endswith("python.exe")
    assert "QorCap" in hint and "venv" in hint
