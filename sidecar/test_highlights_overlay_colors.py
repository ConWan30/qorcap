"""No gold on a clip overlay. Pure string checks on overlay_filter, no ffmpeg, so both sidecar suites run them.

A clip is cut before the referee rules, and gold is reserved for a referee-verified result
(Con's card t3228s12397; QorProof P1). The score was 0xf0c95c gold from #36 until this change.
"""
import colorsys
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import qorcap_highlights as hl


def _hue_sat(hexrgb):
    r, g, b = (int(hexrgb[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, _, s = colorsys.rgb_to_hls(r, g, b)
    return h * 360, s


def _gold_like(hexrgb):
    hue, sat = _hue_sat(hexrgb)
    return 25 <= hue <= 65 and sat >= 0.25


@pytest.mark.parametrize("height", [360, 720, 1080])
def test_no_gold_on_a_clip_overlay(tmp_path, height):
    """No overlay colour may be gold (QorProof P1, card t3228s12397)."""
    vf = hl.overlay_filter("AWAY 0 - HOME 6   Q1 1:40", "lock #2 ab  receipt cd", tmp_path, "hl00", None, height)
    assert "f0c95c" not in vf.lower()
    colors = re.findall(r"(?:font)?color=0x([0-9a-fA-F]{6})", vf)
    # two boxes and three text colours, all hex (no named colour such as "gold" can slip in)
    assert len(colors) == 5 and len(re.findall(r"color=", vf)) == 5, vf
    for c in colors:
        assert not _gold_like(c), f"gold-like overlay colour 0x{c} in {vf}"
    assert hl.SCORE_COLOR == "0xffffff"


def test_the_gold_check_would_catch_the_old_colour():
    assert _gold_like("f0c95c")
