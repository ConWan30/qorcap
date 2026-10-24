"""Full-resolution game clock for the standard bar.

The score path stays on the 640×360 prep. The clock box is cut from the source frame, in the
profile's own fractions, and matched with the stock clock glyphs and the same gates (white
0.93/0.06, red 0.88/0.08). No clock glyph in this reader was harvested from a held-out match.
The stock pack already has a ``6`` (nine glyphs from the September 2026 HDMI clips). A ``6`` that
misses that gate abstains. Nothing here substitutes another digit for it.

A clock is kept when it holds or counts down inside its quarter. A higher quarter may jump the
clock, up to 15:00 (the regulation cap; the configured quarter length is not known, so a reset
is not guessed). A quarter that goes backwards is a cut: the baseline starts over, it is not
a reject. A read with no quarter does not become the baseline, so it cannot blank the quarter
that follows. An upward step inside a quarter is impossible: it is blanked, noted as
``clock_increase``, and it cannot re-anchor. A drop bigger than the time since the last
accepted clock is not accepted on that frame.

Two ways back in, both stricter than one frame:

* Stoppage. The same lower value on ``reanchor_reads`` reads spanning ``reanchor_span_ms``
  (default 4 reads and 2 s). A running clock changes every wall second, so this gate stays shut
  through a runoff.
* Running clock (FU1). A run of gated reads that counts down at about one game-second per
  wall-second. Parameters, and why these values: ``run_reads`` 4 (one to three reads cannot
  finish it; same count as the stoppage gate), ``run_span_ms`` 3_000 (over 2 s a doubled clock
  can still land inside a ±1 s bin; over 3 s it cannot), ``run_min_distinct`` 3 (a hold is the
  stoppage path; three displayed seconds is what a 1:1 clock shows and a frozen one does not),
  ``run_tol_s`` 1 (a displayed second is a one-second bin, so a perfect 1:1 line stays within
  1 s at every sample; the check is per sample, not just the endpoints). This is the one place
  a clock #76 blanked may be kept. The kept value is the read itself, and it has to obey the
  countdown. See ``docs/SCOREBUG_CLOCK_VOTE.md``.

Stoppage is a hold. With no ``t_ms`` the digits are read and the history is left alone.
"""
from __future__ import annotations

import cv2
import numpy as np

from qor_receipt._vendor.qoresence.vision.local_scorebug.glyphs import column_runs, norm_vec

QUARTER_CLOCK_MAX_S = 15 * 60
# Same bound as the score grammar: four gated reads spanning two seconds.
REANCHOR_READS = 4
REANCHOR_SPAN_MS = 2_000
# Running-clock re-anchor. A 1:1 countdown cannot hold one second for two wall seconds.
# Justification: docs/SCOREBUG_CLOCK_VOTE.md.
RUN_READS = 4
RUN_SPAN_MS = 3_000
RUN_MIN_DISTINCT = 3
RUN_TOL_S = 1


def _quarter(quarter):
    if isinstance(quarter, int) and not isinstance(quarter, bool):
        return quarter
    return None


def countdown_run_ok(samples, tol_s: float) -> bool:
    """True when every sample sits on a 1:1 countdown from the first, within ``tol_s``.

    The line is ``s0 - wall_seconds``. A sample higher than the one before it is an increase
    and fails. The tolerance is the displayed-second bin, checked at every sample.
    """
    if not samples:
        return False
    t0, s0 = int(samples[0][0]), int(samples[0][1])
    prev = s0
    for t, s in samples:
        t, s = int(t), int(s)
        if t < t0 or s > prev:
            return False
        if abs(s - (s0 - (t - t0) / 1000.0)) > float(tol_s):
            return False
        prev = s
    return True


def posted_quarter(fr):
    """Quarter a timeline event may carry. An inferred quarter is not observed."""
    detail = getattr(fr, "detail", None) or {}
    if detail.get("quarter_inferred"):
        return None
    return getattr(fr, "quarter", None)


def apply_inferred_quarter(fr, clocks):
    """Carry the last observed quarter when this frame did not read one.

    The carry is unambiguous only when this call accepted a clock that continued the same
    quarter: no reset, no cut, no increase. ``quarter`` stays None. The value lives in
    ``detail`` as ``inferred_quarter`` with ``quarter_inferred`` true. It is not a lock,
    not a final, and not an observed quarter.
    """
    if getattr(fr, "quarter", None) is not None:
        return fr
    if not getattr(clocks, "accepted_now", False) or not getattr(clocks, "continuation", False):
        return fr
    q = _quarter(getattr(clocks, "last_q", None))
    if q is None or getattr(fr, "clock_seconds", None) is None:
        return fr
    from dataclasses import replace
    detail = dict(getattr(fr, "detail", None) or {})
    detail["quarter_inferred"] = True
    detail["inferred_quarter"] = q
    try:
        return replace(fr, quarter=None, detail=detail)
    except TypeError:
        fr.quarter = None
        fr.detail = detail
        return fr


class ClockHistory:
    """Last accepted clock. Reader state only."""

    def __init__(self, reanchor_reads: int = REANCHOR_READS, reanchor_span_ms: int = REANCHOR_SPAN_MS,
                 run_reads: int = RUN_READS, run_span_ms: int = RUN_SPAN_MS,
                 run_min_distinct: int = RUN_MIN_DISTINCT, run_tol_s: int = RUN_TOL_S) -> None:
        self.reanchor_reads = int(reanchor_reads)
        self.reanchor_span_ms = int(reanchor_span_ms)
        self.run_reads = int(run_reads)
        self.run_span_ms = int(run_span_ms)
        self.run_min_distinct = int(run_min_distinct)
        self.run_tol_s = int(run_tol_s)
        self.reset()

    def reset(self) -> None:
        self.last_t: int | None = None
        self.last_s: int | None = None
        self.last_q: int | None = None
        self.last_accept_t: int | None = None
        self._cand: int | None = None
        self._cand_n = 0
        self._cand_t0: int | None = None
        self._run: list[tuple[int, int]] = []
        self.reanchored = False
        self.reanchor_kind: str | None = None
        self.last_run: list[tuple[int, int]] | None = None
        self.rule_note: str | None = None
        self.accepted_now = False
        self.continuation = False

    def _clear_cand(self) -> None:
        self._cand = None
        self._cand_n = 0
        self._cand_t0 = None

    def _clear_run(self) -> None:
        self._run = []

    def _accept(self, secs: int, quarter, t_ms: int) -> int:
        prev_q = self.last_q
        self.last_s = int(secs)
        q = _quarter(quarter)
        if q is not None:
            self.last_q = q
        self.last_accept_t = int(t_ms)
        self._clear_cand()
        self._clear_run()
        self.accepted_now = True
        # Same quarter as the one already observed. A reset or a cut is not a continuation.
        self.continuation = prev_q is not None and self.last_q == prev_q
        return int(secs)

    def _reanchor(self, secs: int, t_ms: int) -> bool:
        """Stoppage: the same lower second, held. An increase never reaches this."""
        if secs != self._cand:
            self._cand = int(secs)
            self._cand_n = 1
            self._cand_t0 = int(t_ms)
            return False
        self._cand_n += 1
        span = int(t_ms) - (int(t_ms) if self._cand_t0 is None else int(self._cand_t0))
        if self._cand_n >= self.reanchor_reads and span >= self.reanchor_span_ms:
            self.reanchored = True
            self.reanchor_kind = "stop"
            self._clear_cand()
            return True
        return False

    def _run_complete(self, samples) -> bool:
        if len(samples) < self.run_reads:
            return False
        span = int(samples[-1][0]) - int(samples[0][0])
        if span < self.run_span_ms:
            return False
        if len({int(s) for _, s in samples}) < self.run_min_distinct:
            return False
        drop = int(samples[0][1]) - int(samples[-1][1])
        if drop <= 0:
            return False
        if abs(drop - span / 1000.0) > self.run_tol_s:
            return False
        return countdown_run_ok(samples, self.run_tol_s)

    def _offer_run(self, secs: int, t_ms: int) -> bool:
        """Append one gated sample. True when the run is a consistent 1:1 countdown."""
        sample = (int(t_ms), int(secs))
        if not self._run:
            self._run = [sample]
            return False
        trial = self._run + [sample]
        if not countdown_run_ok(trial, self.run_tol_s):
            self._run = [sample]
            return False
        self._run = trial
        if not self._run_complete(self._run):
            return False
        self.reanchored = True
        self.reanchor_kind = "run"
        self.last_run = list(self._run)
        self._clear_run()
        return True

    def _too_high(self, secs: int) -> bool:
        if secs > QUARTER_CLOCK_MAX_S:
            self.rule_note = "clock_above_max"
            self._clear_run()
            self._clear_cand()
            return True
        return False

    def filter(self, secs, quarter, t_ms):
        """Return ``secs`` when it is plausible, else None. None input does not move the baseline.

        An impossible step returns None and sets ``rule_note``. A running-clock re-anchor may
        return ``secs`` after a drop the one-frame rule blanked. It never returns a different
        number, and it never returns a clock that increased inside the quarter.
        """
        self.rule_note = None
        self.accepted_now = False
        self.continuation = False
        self.reanchored = False
        self.reanchor_kind = None
        self.last_run = None
        if secs is None or t_ms is None:
            return secs
        t_ms = int(t_ms)
        secs = int(secs)
        if secs < 0:
            self.rule_note = "clock_negative"
            self._clear_run()
            self._clear_cand()
            return None
        q = _quarter(quarter)
        if self.last_t is not None and t_ms < self.last_t:
            self.reset()
            self.rule_note = None
            self.accepted_now = False
            self.continuation = False
            self.reanchored = False
            self.reanchor_kind = None
            self.last_run = None
        self.last_t = t_ms
        if self.last_s is None:
            # A quarter-less read must not freeze the baseline for the quarter that follows.
            if q is None:
                return secs
            if self._too_high(secs):
                return None
            return self._accept(secs, q, t_ms)
        if q is not None and self.last_q is not None and q < self.last_q:
            # A quarter going backwards is a cut, not a reject.
            if self._too_high(secs):
                return None
            return self._accept(secs, q, t_ms)
        if q is not None and (self.last_q is None or q > self.last_q):
            if 0 <= secs <= QUARTER_CLOCK_MAX_S:
                return self._accept(secs, q, t_ms)
            self.rule_note = "clock_above_max"
            self._clear_run()
            self._clear_cand()
            return None
        if secs > self.last_s:
            # Inside a quarter the clock cannot go up, not even if the higher second is held.
            self.rule_note = "clock_increase"
            self._clear_run()
            self._clear_cand()
            return None
        elapsed_s = 0 if self.last_accept_t is None else max(0.0, (t_ms - self.last_accept_t) / 1000.0)
        if self.last_s - secs <= int(elapsed_s) + 1:
            return self._accept(secs, q, t_ms)
        if self._reanchor(secs, t_ms):
            return self._accept(secs, q, t_ms)
        if self._offer_run(secs, t_ms):
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
