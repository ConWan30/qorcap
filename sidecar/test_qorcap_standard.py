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
