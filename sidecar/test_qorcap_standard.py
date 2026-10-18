"""Standard Madden bar, match-20261017-110028 (v0.3.14).

The QorTime pack's "2" and "3" miss this broadcast (under the 0.70 gate). The overlay variants read them.
A "1ST DOWNS" banner drawn in the score cells must not become a score.
"""
import os
import sys
from pathlib import Path

import cv2

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, os.environ.get("QORTIME", "/workspace/qortime-src"))
from qorcap_standard import standard_reader  # noqa: E402
from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import ScorebugReader  # noqa: E402

FX = HERE / "fixtures"


def img(name):
    return cv2.imread(str(FX / name))


def _clock_band(reader, digits):
    """Paint clock digits into the standard clock box. ``digits`` is four labels plus a colon kind."""
    import numpy as np
    p = reader.profile
    top = reader._band_top()
    band = np.zeros((p.ref_h - top, p.ref_w, 3), np.uint8)
    x1, x2, y1, y2 = p.clock_text
    patch = band[y1 - top:y2 - top, x1:x2]
    canvas = np.zeros((y2 - y1, x2 - x1), np.uint8)

    def paint(x, label):
        t = reader.pack.groups["clock"][label][0].reshape(9, 6).astype(np.float32)
        img = (t - t.min()) / (t.max() - t.min() + 1e-9)
        canvas[0:9, x:x + 6] = (img * 255).astype(np.uint8)

    paint(0, digits[0])
    paint(7, digits[1])
    if digits[2] == "dots":
        canvas[1:3, 14:16] = 255
        canvas[6:8, 14:16] = 255
    else:
        canvas[1:8, 14:16] = 255  # one bar: not a colon
    paint(17, digits[3])
    paint(24, digits[4])
    patch[:] = canvas[:, :, None]
    return band


def test_standard_bar_reads_15_00_and_keeps_the_score_when_the_quarter_fails():
    """Q2 opens at 15:00. The vendored segmenter calls that clock_layout and drops the pair with the quarter.

    The rescue keeps away 6 / home 21 and reads 15:00. 16:00 and a bar-shaped colon stay unread.
    """
    from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import FrameRead, ScorebugReader
    from qorcap_standard import clock_seconds_mmss, rescue_standard_read

    raw = ScorebugReader()
    band = _clock_band(raw, ("1", "5", "dots", "0", "0"))
    assert raw._read_clock(band, 0, 0)[0] is None  # two minute digits are not a learned M:SS
    assert clock_seconds_mmss(raw, band, 0, 0) == (900, "15:00")
    assert clock_seconds_mmss(raw, _clock_band(raw, ("1", "6", "dots", "0", "0")), 0, 0) is None
    assert clock_seconds_mmss(raw, _clock_band(raw, ("1", "5", "bar", "0", "0")), 0, 0) is None

    class Inner:
        def __init__(self):
            self.profile = raw.profile
            self.pack = raw.pack

        def read(self, _frame):
            return FrameRead(ok=False, reason="quarter_unsure", profile_id="madden27_standard",
                             offset=(0, 0), detail={"partial_pair": [6, 21]})

        def reference_band(self, _frame):
            return band, "ok"

        def _read_clock(self, b, dx, dy):
            return raw._read_clock(b, dx, dy)

        def _crop(self, b, box):
            return raw._crop(b, box)

        def clock_is_red(self, patch):
            return raw.clock_is_red(patch)

        def clock_intensity(self, patch, red):
            return raw.clock_intensity(patch, red)

    import numpy as np
    fr = rescue_standard_read(Inner(), np.zeros((360, 640, 3), np.uint8))
    assert not fr.ok and fr.reason == "quarter_unsure" and fr.quarter is None
    assert (fr.left_score, fr.right_score, fr.clock_seconds) == (6, 21, 900)


def test_standard_bar_reads_23_and_30_and_refuses_the_downs_banner():
    raw, aug = ScorebugReader(), standard_reader()
    q3 = img("m27_std_110028_q3_23_0.jpg")
    miss = raw.read(q3)
    assert not miss.ok  # the stall: stock templates do not clear 0.70 on this "2"/"3"
    hit = aug.read(q3)
    assert hit.ok and (hit.left_score, hit.right_score, hit.quarter) == (23, 0, 3), hit
    q4 = aug.read(img("m27_std_110028_q4_30_6.jpg"))
    assert q4.ok and (q4.left_score, q4.right_score, q4.quarter) == (30, 6, 4), q4
    banner = aug.read(img("m27_std_110028_downs.jpg"))
    assert not banner.ok
    assert banner.reason in ("score_panel_covered", "score_slot_ink", "score_unsure", "layout_unknown")
    assert (banner.left_score, banner.right_score) != (6, 4)
    assert (banner.left_score, banner.right_score) != (61, 4)
