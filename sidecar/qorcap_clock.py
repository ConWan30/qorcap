"""Full-resolution game clock for the standard bar.

The score path stays on the 640×360 prep. The clock box is cut from the source frame, in the
profile's own fractions, and matched with the same gates (white 0.93/0.06, red 0.88/0.08). Mean
glyphs harvested from locked highlight frames are extra variants of the existing clock labels.
They do not add a label and they are stacked on the pack after ``sha16`` is computed, so
``model_id`` is unchanged.

A clock is kept only when it holds or counts down inside its quarter. A higher quarter may jump
the clock, up to 15:00. An upward flicker is dropped and does not move the baseline. Stoppage is
a hold. With no ``t_ms`` the digits are read and the history is left alone.
"""
from __future__ import annotations

import cv2
import numpy as np

from qor_receipt._vendor.qoresence.vision.local_scorebug.glyphs import column_runs, norm_vec

QUARTER_CLOCK_MAX_S = 15 * 60


class ClockHistory:
    """Last accepted clock. Reader state only."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.last_t: int | None = None
        self.last_s: int | None = None
        self.last_q: int | None = None

    def filter(self, secs, quarter, t_ms):
        """Return ``secs`` when it is plausible, else None. None input does not move the baseline."""
        if secs is None or t_ms is None:
            return secs
        t_ms = int(t_ms)
        if self.last_t is not None and t_ms < self.last_t:
            self.reset()
        self.last_t = t_ms
        secs = int(secs)
        if self.last_s is None:
            self.last_s = secs
            self.last_q = quarter if isinstance(quarter, int) else None
            return secs
        if (
            isinstance(quarter, int)
            and not isinstance(quarter, bool)
            and isinstance(self.last_q, int)
            and quarter > self.last_q
            and 0 <= secs <= QUARTER_CLOCK_MAX_S
        ):
            self.last_s = secs
            self.last_q = quarter
            return secs
        if secs <= self.last_s:
            self.last_s = secs
            if isinstance(quarter, int) and not isinstance(quarter, bool):
                self.last_q = quarter
            return secs
        return None


def _box(frame, profile, box, dx, dy):
    h, w = int(frame.shape[0]), int(frame.shape[1])
    sx = w / float(profile.ref_w)
    sy = h / float(profile.ref_h)
    x1, x2, y1, y2 = box
    xa = int(round((x1 + dx) * sx))
    xb = int(round((x2 + dx) * sx))
    ya = int(round((y1 + dy) * sy))
    yb = int(round((y2 + dy) * sy))
    xa, ya = max(0, xa), max(0, ya)
    xb, yb = min(w, xb), min(h, yb)
    if xb <= xa or yb <= ya:
        return None, sx
    return frame[ya:yb, xa:xb], sx


def read_clock_box(reader, frame, dx: int, dy: int):
    """``(seconds, text, conf, margin, red)`` or None. Same gates as the vendored clock."""
    from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import RED_CLOCK_GATE

    if frame is None or getattr(frame, "ndim", 0) != 3:
        return None
    p = reader.profile
    patch, sx = _box(frame, p, p.clock_text, dx, dy)
    if patch is None or patch.shape[0] < 2 or patch.shape[1] < 4:
        return None
    red = bool(reader.clock_is_red(patch))
    if red and not reader.read_red_clock:
        return None
    v = reader.clock_intensity(patch, red)
    peak = float(np.percentile(v, 98))
    if peak < p.clock_min_peak:
        return None
    mask = (v > p.clock_ink_frac * peak).astype(np.uint8)
    split = max(1, int(round(p.clock_split_w * sx)))
    runs: list[tuple[int, int]] = []
    for a, b in column_runs(mask):
        if b - a >= split:
            cols = mask[:, a:b].sum(0)
            w = b - a
            lo, hi = w // 3, 2 * w // 3 + 1
            if hi <= lo:
                runs.append((a, b))
                continue
            cut = a + lo + int(cols[lo:hi].argmin())
            runs += [(a, cut), (cut, b)]
        else:
            runs.append((a, b))
    if len(runs) != 4:
        return None
    (ma, mb), (ca, cb), (s1a, s1b), (s2a, s2b) = runs
    if cb - ca > max(3, int(round(3 * sx))):
        return None
    rows = np.flatnonzero(mask[:, ca:cb].any(1))
    if rows.size == 0 or np.all(np.diff(rows) == 1):
        return None
    lo_w = max(1, int(round(p.clock_digit_w[0] * sx)))
    hi_w = max(lo_w, int(round(p.clock_digit_w[1] * sx)))
    gate = RED_CLOCK_GATE if red else p.clock_gate
    digits = []
    conf, margin = 1.0, 1.0
    for i, (a, b) in enumerate(((ma, mb), (s1a, s1b), (s2a, s2b))):
        if not lo_w <= b - a <= hi_w:
            return None
        vec = norm_vec(v[:, a:b], p.clock_glyph_size)
        if vec is None:
            return None
        m = reader.pack.match("clock", vec)
        if m is None:
            return None
        conf, margin = min(conf, m.best), min(margin, m.margin)
        if red and i == 0:
            if m.label != "0" or m.best < gate.min_score:
                return None
        elif m.best < gate.min_score or m.margin < gate.min_margin:
            return None
        digits.append(m.label)
    mm, s1, s2 = (int(d) for d in digits)
    if s1 > 5:
        return None
    total = mm * 60 + s1 * 10 + s2
    return total, f"{mm}:{s1}{s2}", conf, margin, red
