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


QUARTER_FAIL = {"quarter_unsure", "quarter_missing", "quarter_unknown", "quarter_no_templates"}
# A regulation quarter opens at 15:00 and is never above that. 10:00–15:00 is the two-digit minute.
MMSS_MIN_S = 10 * 60
MMSS_MAX_S = 15 * 60


def _shift(box, dx, dy):
    x1, x2, y1, y2 = box
    return (x1 + dx, x2 + dx, y1 + dy, y2 + dy)


def clock_seconds_mmss(reader, band, dx: int, dy: int):
    """Read a 10:00–15:00 clock. QorTime's segmenter only accepts one minute digit (``clock_layout`` above 9:59).

    Fail closed: five ink runs, a two-dot colon in the middle, each digit through the normal clock gate,
    minutes 10–15. Anything else stays unread. A red clock is the final minute, never 10:00+.
    """
    from qor_receipt._vendor.qoresence.vision.local_scorebug.glyphs import column_runs, norm_vec

    p = reader.profile
    patch = reader._crop(band, _shift(p.clock_text, dx, dy))
    if patch is None:
        return None
    if reader.clock_is_red(patch):
        return None
    v = reader.clock_intensity(patch, False)
    peak = float(np.percentile(v, 98))
    if peak < p.clock_min_peak:
        return None
    mask = (v > p.clock_ink_frac * peak).astype(np.uint8)
    runs: list[tuple[int, int]] = []
    for a, b in column_runs(mask):
        if b - a >= p.clock_split_w:
            cols = mask[:, a:b].sum(0)
            w = b - a
            lo, hi = w // 3, 2 * w // 3 + 1
            cut = a + lo + int(cols[lo:hi].argmin())
            runs += [(a, cut), (cut, b)]
        else:
            runs.append((a, b))
    if len(runs) != 5:
        return None
    (ca, cb) = runs[2]
    if cb - ca > 3:
        return None
    rows = np.flatnonzero(mask[:, ca:cb].any(1))
    if rows.size == 0 or np.all(np.diff(rows) == 1):
        return None
    gate = p.clock_gate
    lo_w, hi_w = p.clock_digit_w
    labels = []
    for a, b in (runs[0], runs[1], runs[3], runs[4]):
        if not lo_w <= b - a <= hi_w:
            return None
        vec = norm_vec(v[:, a:b], p.clock_glyph_size)
        if vec is None:
            return None
        m = reader.pack.match("clock", vec)
        if m is None or m.best < gate.min_score or m.margin < gate.min_margin:
            return None
        labels.append(m.label)
    if labels[0] != "1" or labels[1] not in "012345":
        return None
    secs = int(labels[2] + labels[3])
    if secs > 59:
        return None
    total = int(labels[0]) * 600 + int(labels[1]) * 60 + secs
    if not MMSS_MIN_S <= total <= MMSS_MAX_S:
        return None
    return total, f"{labels[0]}{labels[1]}:{labels[2]}{labels[3]}"


def _fill_clock(reader, frame, fr):
    """Best-effort clock, including 10:00–15:00, without replacing a clock the reader already accepted."""
    if getattr(fr, "clock_seconds", None) is not None:
        return fr
    if getattr(fr, "left_score", None) is None:
        return fr
    band, why = reader.reference_band(frame)
    if band is None:
        return fr
    dx, dy = fr.offset if getattr(fr, "offset", None) else (0, 0)
    secs, text, cwhy, _cc, _cm = reader._read_clock(band, dx, dy)
    if secs is None and cwhy == "clock_layout":
        got = clock_seconds_mmss(reader, band, dx, dy)
        if got is None:
            return fr
        secs, text = got
    elif secs is None:
        return fr
    from dataclasses import replace
    return replace(fr, clock_seconds=secs, clock_text=text)


def rescue_standard_read(reader, frame):
    """A sure score pair is a read even when the quarter word failed.

    QorTime's ``ScorebugReader.read`` returns before copying the pair or trying the clock when the
    quarter glyph misses. The score lock does not use the quarter. 15:00 (two minute digits) is read
    here; a one-minute clock is unchanged.
    """
    fr = reader.read(frame)
    if fr.clock_seconds is None and getattr(fr, "left_score", None) is not None:
        fr = _fill_clock(reader, frame, fr)
    if fr.ok or fr.reason not in QUARTER_FAIL:
        return fr
    pair = (getattr(fr, "detail", None) or {}).get("partial_pair")
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        return fr
    try:
        left, right = int(pair[0]), int(pair[1])
    except (TypeError, ValueError):
        return fr
    from dataclasses import replace
    fr = replace(fr, ok=False, reason="quarter_unsure", quarter=None, left_score=left, right_score=right)
    return _fill_clock(reader, frame, fr)


class StandardBarReader:
    """``madden27_standard`` plus the 110028 digit variants, with scores kept when the quarter misses."""

    wants_full_res = False

    def __init__(self, inner) -> None:
        self.inner = inner
        self.profile = inner.profile
        self.profile_id = inner.profile.profile_id

    def read(self, frame):
        return rescue_standard_read(self.inner, frame)

    def reference_band(self, frame):
        return self.inner.reference_band(frame)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def standard_reader():
    """QorTime's ScorebugReader plus the 110028 "2" and "3" variants."""
    from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import ScorebugReader

    reader = ScorebugReader()
    if PACK.is_file():
        z = np.load(PACK)
        score = reader.pack.groups["score"]
        score["2"] = np.vstack([score["2"], np.atleast_2d(z["two"].astype(np.float32))])
        score["3"] = np.vstack([score["3"], np.atleast_2d(z["three"].astype(np.float32))])
    return StandardBarReader(reader)
