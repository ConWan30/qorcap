"""Full-resolution game clock for the standard bar.

The score path stays on the 640×360 prep. The clock box is cut from the source frame, in the
profile's own fractions, and matched with the stock clock glyphs and the same gates (white
0.93/0.06, red 0.88/0.08). No clock glyph in this reader was harvested from a held-out match.
The stock pack already has a ``6`` (nine glyphs from the September 2026 HDMI clips). A ``6`` that
misses that gate abstains. Nothing here substitutes another digit for it.

A clock is kept when it holds or counts down inside its quarter. A higher quarter may jump the
clock, up to 15:00. A quarter that goes backwards is a cut: the baseline starts over, it is not
a reject. A read with no quarter does not become the baseline, so it cannot blank the quarter
that follows. An upward flicker, or a drop bigger than the time since the last accepted clock,
is dropped and does not move the baseline until the same value has passed the gates on
``reanchor_reads`` reads spanning ``reanchor_span_ms`` (default 4 reads and 2 s, the same rule
as the score grammar). Stoppage is a hold. With no ``t_ms`` the digits are read and the history
is left alone.
"""
from __future__ import annotations

import cv2
import numpy as np

from qor_receipt._vendor.qoresence.vision.local_scorebug.glyphs import column_runs, norm_vec

QUARTER_CLOCK_MAX_S = 15 * 60
# Same bound as the score grammar: four gated reads spanning two seconds.
REANCHOR_READS = 4
REANCHOR_SPAN_MS = 2_000


def _quarter(quarter):
    if isinstance(quarter, int) and not isinstance(quarter, bool):
        return quarter
    return None


class ClockHistory:
    """Last accepted clock. Reader state only."""

    def __init__(self, reanchor_reads: int = REANCHOR_READS, reanchor_span_ms: int = REANCHOR_SPAN_MS) -> None:
        self.reanchor_reads = int(reanchor_reads)
        self.reanchor_span_ms = int(reanchor_span_ms)
        self.reset()

    def reset(self) -> None:
        self.last_t: int | None = None
        self.last_s: int | None = None
        self.last_q: int | None = None
        self.last_accept_t: int | None = None
        self._cand: int | None = None
        self._cand_n = 0
        self._cand_t0: int | None = None
        self.reanchored = False

    def _clear_cand(self) -> None:
        self._cand = None
        self._cand_n = 0
        self._cand_t0 = None

    def _accept(self, secs: int, quarter, t_ms: int) -> int:
        self.last_s = int(secs)
        q = _quarter(quarter)
        if q is not None:
            self.last_q = q
        self.last_accept_t = int(t_ms)
        self._clear_cand()
        return int(secs)

    def _reanchor(self, secs: int, t_ms: int) -> bool:
        if secs != self._cand:
            self._cand = int(secs)
            self._cand_n = 1
            self._cand_t0 = int(t_ms)
            return False
        self._cand_n += 1
        span = int(t_ms) - (int(t_ms) if self._cand_t0 is None else int(self._cand_t0))
        if self._cand_n >= self.reanchor_reads and span >= self.reanchor_span_ms:
            self.reanchored = True
            self._clear_cand()
            return True
        return False

    def filter(self, secs, quarter, t_ms):
        """Return ``secs`` when it is plausible, else None. None input does not move the baseline."""
        if secs is None or t_ms is None:
            return secs
        t_ms = int(t_ms)
        secs = int(secs)
        q = _quarter(quarter)
        if self.last_t is not None and t_ms < self.last_t:
            self.reset()
        self.last_t = t_ms
        if self.last_s is None:
            # A quarter-less read must not freeze the baseline for the quarter that follows.
            if q is None:
                return secs
            return self._accept(secs, q, t_ms)
        if q is not None and self.last_q is not None and q < self.last_q:
            return self._accept(secs, q, t_ms)
        if q is not None and (self.last_q is None or q > self.last_q) and 0 <= secs <= QUARTER_CLOCK_MAX_S:
            return self._accept(secs, q, t_ms)
        if secs <= self.last_s:
            elapsed_s = 0 if self.last_accept_t is None else max(0.0, (t_ms - self.last_accept_t) / 1000.0)
            if self.last_s - secs <= int(elapsed_s) + 1:
                return self._accept(secs, q, t_ms)
            if self._reanchor(secs, t_ms):
                return self._accept(secs, q, t_ms)
            return None
        if self._reanchor(secs, t_ms):
            return self._accept(secs, q, t_ms)
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
