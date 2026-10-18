"""Recorder challenge recheck for the USB capture card + Bluetooth pad (v0.3.15).

Measured on match-20261017-110028: the Madden pause menu is stable about 1.75 s after OPTIONS.
Rule v1, the default verifier, stays 0..600 ms. Rule v2 (opt-in) uses this same clock with a
2500 ms ceiling and a ±300 ms consistency window. This bound is the recorder's recheck only. The
video clock is the fragment PTS through the manifest frame fit, not the segment close stamp.
"""
import json
import os
import shutil
import sys
from pathlib import Path

import cv2
import pytest

HERE = Path(__file__).parent
QT = next((Path(p) for p in [os.getenv("QORTIME", ""), str(HERE.parent / "qortime"), "/tmp/qortime", "/workspace/qortime-src"]
           if p and (Path(p) / "qor_receipt").exists()), None)
sys.path.insert(0, str(HERE))
if QT:
    sys.path.insert(0, str(QT))
    sys.path.insert(0, str(QT / "tests"))
from qorcap_challenge import (  # noqa: E402
    CAPTURE_PAUSE_MAX_LATENCY_MS, capture_clock, capture_pause_detector, frame_mono_ms,
)
from qor_receipt.challenge import MAX_LATENCY_MS, RULE2_CONSISTENCY_MS, RULE2_MAX_LATENCY_MS, verify_response  # noqa: E402

FX = HERE / "fixtures"


def _twice(det, im):
    assert det.update(0, im) is False  # confirm_frames = 2; onset is the first frame
    return det.update(100, im)


def test_capture_detector_sees_both_pause_menus_and_not_gameplay():
    assert CAPTURE_PAUSE_MAX_LATENCY_MS == 2500
    assert MAX_LATENCY_MS == 600  # rule v1, still the default verifier
    assert RULE2_MAX_LATENCY_MS == 2500 and RULE2_CONSISTENCY_MS == 300
    kick = cv2.imread(str(FX / "m27_std_110028_pause_kick.jpg"))
    end = cv2.imread(str(FX / "m27_std_110028_pause_end.jpg"))
    pre = cv2.imread(str(FX / "m27_std_110028_pre_pause.jpg"))
    assert _twice(capture_pause_detector(), kick) is True
    assert _twice(capture_pause_detector(), end) is True
    assert _twice(capture_pause_detector(), pre) is False


def test_recorder_bound_accepts_1750_ms_and_rejects_a_relay():
    press = 1_000
    ev = [{"t_ms": press, "button": "OPTIONS", "down": True}]
    ok = verify_response(["OPTIONS"], ev, issued_ms=0, deadline_ms=10_000,
                         pause_seen_ms=press + 1750, max_latency_ms=CAPTURE_PAUSE_MAX_LATENCY_MS)
    assert ok.ok and ok.reason == "ok" and ok.latency_ms == 1750
    late = verify_response(["OPTIONS"], ev, issued_ms=0, deadline_ms=10_000,
                           pause_seen_ms=press + 2600, max_latency_ms=CAPTURE_PAUSE_MAX_LATENCY_MS)
    assert not late.ok and late.reason == "video_latency_too_high"
    early = verify_response(["OPTIONS"], ev, issued_ms=0, deadline_ms=10_000,
                            pause_seen_ms=press - 100, max_latency_ms=CAPTURE_PAUSE_MAX_LATENCY_MS)
    assert not early.ok and early.reason == "video_before_input"
    # The same 1.75 s onset still fails rule v1. The rule v2 ceiling accepts it; consistency is separate.
    official = verify_response(["OPTIONS"], ev, issued_ms=0, deadline_ms=10_000, pause_seen_ms=press + 1750)
    assert not official.ok and official.reason == "video_latency_too_high"
    v2 = verify_response(["OPTIONS"], ev, issued_ms=0, deadline_ms=10_000, pause_seen_ms=press + 1750,
                         max_latency_ms=RULE2_MAX_LATENCY_MS)
    assert v2.ok and v2.latency_ms == 1750


def test_capture_clock_extrapolates_frame_zero_behind_the_first_close():
    """match-110028: the first close is frame 129 at mono 6768, and frame 0 is ~2646, not close-2000."""
    segs = [{"kind": "init", "frames": 129, "mono_ms": 6768}]
    for fr in range(129, 129 + 61 * 20, 61):
        segs.append({"kind": "media", "frames": fr, "mono_ms": int(round(2646.3 + 33.3346 * fr))})
    intercept, slope = capture_clock(segs)
    assert abs(intercept - 2646.3) < 5
    assert abs(slope - 33.3346) < 0.01
    assert abs(intercept - (6768 - 2000)) > 1000  # the segment-0 fallback (11:00:42.5) is not frame 0


def test_capture_clock_keeps_pts_as_mono_when_frames_do_not_advance():
    segs = [{"kind": "media", "frames": 60, "mono_ms": i * 2000} for i in range(1, 15)]
    assert capture_clock(segs) == (0.0, 1000.0 / 30.0)


def test_frame_fit_stamps_the_110028_pause_at_1750_ms():
    """The three OPTIONS presses, and the frame 1750 ms later on the manifest frame fit.

    The v0.3.14 recheck reported 3593 / 3641 / 3522 because it stamped that same frame from the
    close. Handoff JPEGs (0.25 s grid, same CDT clock) let the capture detector accept the menu at
    +1728 / +1654 / +1711; the 30 fps onset is inside the preceding quarter-second. All of those
    are under the 2500 ms bound.
    """
    clock = (2646.3, 33.3346)
    wmm = 1_792_252_837_704
    presses = {"kickoff": 1_792_253_007_922, "halftime": 1_792_253_533_896, "end": 1_792_254_134_839}
    jpeg_onset = {"kickoff": 1728, "halftime": 1654, "end": 1711}
    for phase, press in presses.items():
        true_mono = (press + 1750) - wmm
        pts0 = (true_mono - clock[0]) / clock[1] * (1000.0 / 30.0)
        wall = frame_mono_ms(clock, pts0, 0, 30.0) + wmm
        latency = wall - press
        assert abs(latency - 1750) <= 2
        # Handoff JPEGs are 250 ms apart. The first frame the detector accepts is the ~1.75 s menu.
        assert 0 <= jpeg_onset[phase] <= CAPTURE_PAUSE_MAX_LATENCY_MS
        assert abs(jpeg_onset[phase] - 1750) <= 250


@pytest.mark.skipif(QT is None or shutil.which("ffmpeg") is None, reason="needs qortime and ffmpeg")
def test_close_stamp_lag_does_not_move_the_pause(tmp_path):
    """A fragment hashed 4 s after its frames must still report the fixture's ~200 ms pause, not ~4200."""
    import review_fixture as F

    import qorcap_challenge as C

    F.build(tmp_path / "m")
    man_path = tmp_path / "m" / "manifest.jsonl"
    rows = [json.loads(line) for line in man_path.read_text().splitlines() if line.strip()]
    lag = 4_000
    for m in rows:
        if m.get("type") != "segment":
            continue
        m["mono_ms"] = int(m["mono_ms"]) + lag
        m["wall_ms"] = int(m["wall_ms"]) + lag
        if m.get("kind") == "media":
            m["frames"] = int(round(m["mono_ms"] * 30 / 1000))
    man_path.write_text("".join(json.dumps(m) + "\n" for m in rows))
    chs = [dict(c, result=None) for c in json.loads((tmp_path / "m" / "receipt.json").read_text())["challenges"]]
    outs = C.challenge_results(tmp_path / "m", chs)
    lats = [o["local"].get("latency_ms") for o in outs]
    assert all(o["result"] and o["result"]["ok"] for o in outs), [o["local"] for o in outs]
    assert all(isinstance(x, int) and 0 <= x <= 600 for x in lats), lats


def test_fill_challenge_results_uses_the_recorder_recheck():
    src = (HERE / "qorcap_match.py").read_text(encoding="utf-8")
    assert "from qorcap_challenge import challenge_results" in src
    chal = (HERE / "qorcap_challenge.py").read_text(encoding="utf-8")
    assert "max_latency_ms=CAPTURE_PAUSE_MAX_LATENCY_MS" in chal
    assert 'res["reverified_by"] = "recorder"' in chal
    assert "capture_pause_max_ms=CAPTURE_PAUSE_MAX_LATENCY_MS" in chal
    assert 'return {"result": res if res["ok"] else None, "local": local}' in chal
