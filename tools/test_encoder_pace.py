"""Offline encoder pace report from a QorCap log. No video, no receipt, no network."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import encoder_pace as ep

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "encoder_pace" / "match-230248-redacted.txt"
DROP_V = ("ffmpeg: [in#0/dshow @ 0000000000000000] real-time buffer [Elgato Game Capture Neo] [video input] "
          "too full or near too full (62% of size: 512000000 [rtbufsize parameter])! frame dropped!")
DROP_A = ("ffmpeg: [in#1/dshow @ 0000000000000000] real-time buffer [Elgato Game Capture Neo (Elgato Game Capture Neo)] "
          "[audio input] too full or near too full (70% of size: 3041280 [rtbufsize parameter])! frame dropped!")


def _line(sec: float, level: str, msg: str) -> str:
    whole = int(sec)
    frac = round((sec - whole) * 1000)
    m, s = divmod(whole, 60)
    return f"2026-11-15T05:{m:02d}:{s:02d}.{frac:03d}0000Z [{level}] {msg}"


def _progress(rates, step=5.0):
    """One progress line every `step` s; rates[i] fps for window i."""
    out, f, t = [_line(0, "INFO", "encoder progress: frame=0 fps=0 speed=1x dup=0 drop=0")], 0, 0.0
    for r in rates:
        t += step
        f += round(r * step)
        out.append(_line(t, "INFO", f"encoder progress: frame={f} fps=60.00 speed=1x dup=0 drop=0"))
    return out


def _report(lines, fps=None):
    records, stats = ep.parse_log(lines)
    return ep.analyse(records, fps, stats)


FIXTURE_SHA256 = "e24ccc3f55d0b71081e88a3d19d8e482c8466dc1829523eea8a38679f1a5e199"


def test_fixture_from_match_230248_matches_the_hand_count():
    import hashlib
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256
    rep = _report(FIXTURE.read_text(encoding="utf-8").splitlines())
    assert rep["lines"] == {"lines": 168, "parsed_lines": 168, "skipped_lines": 0, "blank_lines": 0}
    seg = rep["segments"]
    assert len(seg) == 1
    assert seg[0]["nominal_fps"] == 60.0 and seg[0]["nominal_fps_source"] == "capture argv"
    assert seg[0]["windows"] == 98 and seg[0]["slow_windows"] == 62
    assert seg[0]["min_fps"] == 46.08
    assert seg[0]["peak_deficit_s"] == 21.02
    assert seg[0]["peak_deficit_at"] == "2026-11-15T05:10:03.865Z"
    assert len(rep["episodes"]) == 1
    e = rep["episodes"][0]
    assert e["start"] == "2026-11-15T05:04:28.663Z" and e["slow_windows"] == 59 and e["max_deficit_s"] == 21.02
    # The fixture keeps the drop stretch from 05:04:34Z (first drop line) to 05:04:38Z: 38 lines plus 88 repeated.
    d = rep["drops"]
    assert (d["video_lines"], d["audio_lines"], d["other_lines"], d["repeats"], d["unread_repeats"]) == (38, 0, 0, 88, 0)
    assert d["lines"] == 38 and d["messages"] == 126
    assert d["dropped_frames"] == "UNMEASURED"
    assert rep["cause"] == "UNMEASURED"
    assert d["status"] == "MEASURED" and seg[0]["drops"]["messages"] == 126
    assert sum(w["messages"] for w in seg[0]["drops"]["per_30s"]) == 126
    # The end flush is fast, not slow.
    assert max(w["fps"] for w in rep["windows"]) > 170


def test_real_time_has_no_episode_and_zero_deficit():
    rep = _report(_progress([60] * 12))
    assert rep["segments"][0]["slow_windows"] == 0
    assert rep["episodes"] == []
    assert all(w["deficit_s"] == 0 for w in rep["windows"])
    assert rep["segments"][0]["nominal_fps_source"].startswith("default 60")
    # No argv and no ffmpeg line: drop messages are UNKNOWN, not 0.
    assert rep["drops"]["status"] == "UNKNOWN" and rep["drops"]["messages"] is None


def test_one_slow_window_is_not_an_episode_and_two_are():
    assert _report(_progress([60, 55, 60, 55, 60]))["episodes"] == []
    rep = _report(_progress([60, 55, 55] + [60] * 6 + [55, 55]))
    eps = rep["episodes"]
    assert len(eps) == 2
    assert eps[0]["closed"] == "back to real time" and eps[0]["slow_windows"] == 2
    assert eps[1]["closed"].startswith("still below real time")
    # 2 slow windows at 55 fps: 2 x 5 s x 5/60 behind.
    assert eps[0]["max_deficit_s"] == 0.83


def test_middle_rate_neither_opens_nor_closes():
    rep = _report(_progress([55, 55] + [59.2] * 10))
    assert len(rep["episodes"]) == 1
    assert rep["episodes"][0]["closed"].startswith("still below real time")


def test_fps_argument_and_argv_set_the_threshold():
    lines = _progress([29.6] * 4)
    assert len(_report(lines)["episodes"]) == 1  # 60 fps assumed: slow
    rep = _report(lines, fps=30)
    assert rep["episodes"] == [] and rep["segments"][0]["nominal_fps_source"] == "argument"
    argv = _line(0, "INFO", "capture ffmpeg argv: C:\\REDACTED\\ffmpeg.exe -i x -r 30 -c:v h264_mf pipe:1")
    assert _report([argv] + lines)["segments"][0]["nominal_fps"] == 30.0


def test_drop_runs_follow_the_capstop_rule():
    lines = [
        _line(1, "WARN", DROP_V),
        _line(1.1, "WARN", "ffmpeg: Last message repeated 5 times"),
        _line(1.2, "INFO", "encoder progress: frame=60 fps=60 speed=1x"),  # QorCap's own line: run stays open
        _line(1.3, "WARN", "ffmpeg: Last message repeated 3 times"),
        _line(2, "WARN", DROP_A),
        _line(2.1, "WARN", "ffmpeg: Last message repeated x times"),
        _line(3, "WARN", "ffmpeg: [h264_mf @ 0000000000000000] stream format change"),  # ends the run
        _line(3.1, "WARN", "ffmpeg: Last message repeated 7 times"),
        _line(40, "WARN", DROP_V),
    ]
    d = _report(lines)["drops"]
    assert (d["video_lines"], d["audio_lines"], d["repeats"], d["unread_repeats"]) == (2, 1, 8, 1)
    assert d["messages"] == 11
    assert d["status"] == "MEASURED"
    assert [w["messages"] for w in _report(lines)["segments"][0]["drops"]["per_30s"]] == [10, 1]


def test_writer_restart_starts_a_new_segment():
    a = _progress([60, 60])
    b = [_line(20, "INFO", "encoder progress: frame=10 speed=1x"), _line(25, "INFO", "encoder progress: frame=310 speed=1x")]
    rep = _report(a + b)
    assert len(rep["segments"]) == 2
    assert all(w["frames"] >= 0 for w in rep["windows"])


def test_garbage_and_empty_input_are_unknown_not_zero():
    empty = _report([])
    assert empty["segments"] == [] and empty["pace_status"] == "UNKNOWN"
    assert empty["drops"]["status"] == "UNKNOWN" and empty["drops"]["messages"] is None
    assert ep.is_unknown(empty)
    junk = ["", "garbage", "2026-99-99T99:99:99Z [INFO] encoder progress: frame=1", "\x00\ufffd",
            _line(1, "INFO", "encoder progress: frame=N/A speed=N/A"),
            _line(3, "INFO", "some other QorCap line")]
    rep = _report(junk)
    assert rep["lines"] == {"lines": 6, "parsed_lines": 2, "skipped_lines": 3, "blank_lines": 1}
    assert rep["segments"] == [] and rep["pace_status"] == "UNKNOWN"
    assert rep["drops"]["messages"] is None and rep["drops"]["video_lines"] is None
    # An ffmpeg line makes the file drop-capable, but with no progress window pace stays UNKNOWN.
    rep = _report(junk + [_line(2, "WARN", "ffmpeg:"), _line(3, "WARN", "ffmpeg: Last message repeated 4 times")])
    assert rep["drops"]["status"] == "MEASURED" and rep["drops"]["messages"] == 0
    assert rep["pace_status"] == "UNKNOWN" and rep["segments"][0]["min_fps"] is None and ep.is_unknown(rep)


def test_cli_json_and_table(tmp_path, capsys):
    assert ep.main([str(FIXTURE)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["drops"]["messages"] == 126
    assert out["input_sha256"] == FIXTURE_SHA256
    assert ep.main([str(FIXTURE), "--table"]) == 0
    text = capsys.readouterr().out
    assert "62/98 windows slow" in text and "dropped frames UNMEASURED" in text and "cause: UNMEASURED" in text
    assert f"input sha256 {FIXTURE_SHA256}" in text
    assert ep.main([str(tmp_path / "missing.log")]) == 2
    assert ep.main([str(FIXTURE), "--fps", "0"]) == 2


def test_unreadable_file_exits_3_with_unknown(tmp_path, capsys):
    bad = tmp_path / "not-a-qorcap.log"
    bad.write_text("hello\nprefix 2026-11-15T05:02:57Z [INFO] encoder progress: frame=1\n", encoding="utf-8")
    assert ep.main([str(bad)]) == 3
    io = capsys.readouterr()
    out = json.loads(io.out)
    assert out["pace_status"] == "UNKNOWN" and out["drops"]["messages"] is None
    assert out["lines"]["skipped_lines"] == 2 and "exit 3" in io.err
    assert ep.main([str(bad), "--table"]) == 3
    text = capsys.readouterr().out
    assert "drop messages: UNKNOWN" in text and "pace: UNKNOWN" in text


def test_two_sessions_appended_into_one_file_are_never_bridged():
    def argv(t, r):
        return _line(t, "INFO", f"capture ffmpeg argv: C:\\REDACTED\\ffmpeg.exe -i x -r {r} -c:v h264_mf pipe:1")
    first = [argv(0, 60)] + [_line(1 + 5 * i, "INFO", f"encoder progress: frame={300 * i} speed=1x") for i in range(4)]
    first += [_line(17, "WARN", DROP_V), _line(17.5, "WARN", "ffmpeg: Last message repeated 2 times")]
    # Second session 60 s later at 30 fps; its first frame (2000) is above the first session's last (900).
    second = [argv(80, 30)] + [_line(81 + 5 * i, "INFO", f"encoder progress: frame={2000 + 150 * i} speed=1x") for i in range(4)]
    second += [_line(82, "WARN", "ffmpeg: Last message repeated 9 times")]  # no drop line in this session: not counted
    rep = _report(first + second)
    a, b = rep["segments"]
    assert (a["nominal_fps"], b["nominal_fps"]) == (60.0, 30.0)
    assert a["reason"] == b["reason"] == "capture argv"
    assert a["windows"] == b["windows"] == 3, "no window spans the 60 s gap"
    assert a["slow_windows"] == b["slow_windows"] == 0 and rep["episodes"] == []
    assert a["peak_deficit_s"] == b["peak_deficit_s"] == 0
    assert a["drops"]["messages"] == 3 and b["drops"]["messages"] == 0
    assert rep["drops"]["messages"] == 3 and rep["drops"]["status"] == "MEASURED"
    assert [w["segment"] for w in rep["windows"]] == [0, 0, 0, 1, 1, 1]


def test_bad_rates_exit_2_with_a_message(tmp_path, capsys):
    for v in ["0", "-5", "nan", "inf", "-inf"]:
        assert ep.main([str(FIXTURE), f"--fps={v}"]) == 2, v
        assert "finite number > 0" in capsys.readouterr().err
    zero = tmp_path / "r0.log"
    zero.write_text(_line(0, "INFO", "capture ffmpeg argv: C:\\REDACTED\\ffmpeg.exe -i x -r 0 -c:v h264_mf pipe:1") + "\n"
                    + "\n".join(_progress([60, 60])) + "\n", encoding="utf-8")
    assert ep.main([str(zero)]) == 2
    assert "capture argv" in capsys.readouterr().err
    # --fps overrides the argv rate, so the same file is readable with a valid --fps.
    assert ep.main([str(zero), "--fps", "60"]) == 0
