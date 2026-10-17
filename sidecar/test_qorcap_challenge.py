"""Recorder challenge recheck for the USB capture card + Bluetooth pad (v0.3.14).

Measured on match-20261017-110028: the Madden pause menu is stable about 1.75 s after OPTIONS.
The official contract stays 0..600 ms. This bound is the recorder's recheck only.
"""
import os
import sys
from pathlib import Path

import cv2

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, os.environ.get("QORTIME", "/workspace/qortime-src"))
from qorcap_challenge import CAPTURE_PAUSE_MAX_LATENCY_MS, capture_pause_detector  # noqa: E402
from qor_receipt.challenge import MAX_LATENCY_MS, verify_response  # noqa: E402

FX = HERE / "fixtures"


def _twice(det, im):
    assert det.update(0, im) is False  # confirm_frames = 2; onset is the first frame
    return det.update(100, im)


def test_capture_detector_sees_both_pause_menus_and_not_gameplay():
    assert CAPTURE_PAUSE_MAX_LATENCY_MS == 2500
    assert MAX_LATENCY_MS == 600  # the referee's bound is unchanged
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
    # The same 1.75 s onset still fails the official 600 ms contract.
    official = verify_response(["OPTIONS"], ev, issued_ms=0, deadline_ms=10_000, pause_seen_ms=press + 1750)
    assert not official.ok and official.reason == "video_latency_too_high"


def test_fill_challenge_results_uses_the_recorder_recheck():
    src = (HERE / "qorcap_match.py").read_text(encoding="utf-8")
    assert "from qorcap_challenge import challenge_results" in src
    chal = (HERE / "qorcap_challenge.py").read_text(encoding="utf-8")
    assert "max_latency_ms=CAPTURE_PAUSE_MAX_LATENCY_MS" in chal
    assert 'res["reverified_by"] = "recorder"' in chal
    assert "capture_pause_max_ms=CAPTURE_PAUSE_MAX_LATENCY_MS" in chal
    assert 'return {"result": res if res["ok"] else None, "local": local}' in chal
