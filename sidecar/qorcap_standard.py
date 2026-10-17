"""Standard Madden scorebug with real digits from match-20261017-110028 (QorCap 0.3.14).

QorTime's ``madden27_standard`` pack has a "2" and a "3", but on the London HOU–JAX bar the leading
"2" matched that template at about 0.59 and the "3" at about 0.67, both under the 0.70 gate. Every
other check on those frames was fine (logo, white panel, quarter), so the timeline went blank from
11:12:59 to 11:19:52 while the score sat on 22, 23, and 29. The gate stays 0.70. These two vectors
are the mean of the real glyphs from that window (148 twos, 132 threes), added as extra variants.
A "1ST DOWNS" banner still fails ``score_panel_covered`` / ``score_slot_ink`` before any digit match.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

PACK = Path(__file__).resolve().parent / "packs" / "madden27_standard_score_110028.npz"


def standard_reader():
    """QorTime's ScorebugReader plus the 110028 "2" and "3" variants."""
    from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import ScorebugReader

    reader = ScorebugReader()
    if not PACK.is_file():
        return reader
    z = np.load(PACK)
    score = reader.pack.groups["score"]
    score["2"] = np.vstack([score["2"], np.atleast_2d(z["two"].astype(np.float32))])
    score["3"] = np.vstack([score["3"], np.atleast_2d(z["three"].astype(np.float32))])
    return reader
