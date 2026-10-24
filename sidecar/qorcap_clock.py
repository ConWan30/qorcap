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
that follows. An upward step inside a quarter is impossible: it is blanked and noted as
``clock_increase``. One frame cannot re-anchor. The same higher second, held for the
stoppage bar, blanks both clocks. While suspended, the stoppage gate does not adopt a
second above the last clock published in that quarter, or a second either contradicted
line could already have shown. An equal second may. A higher clock that is not one of
those lines returns through the running-clock gate or a quarter change. A drop bigger
than the time since the last accepted clock is not accepted on that frame.

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
  1 s at every sample; the check is per sample, not just the endpoints). Over that window a
  rate of about 0.7x–1.3x still fits; 2x does not. This is the one place a clock #76 blanked
  may be kept. The kept value is the read itself. See ``docs/SCOREBUG_CLOCK_VOTE.md``.

A second sustained line that disagrees with the accepted clock, higher or lower, blanks both.
Those two running lines stay blocked until a quarter change, or until a line that is neither
of them completes a gate. The block follows the same slope band as the running gate (about
0.7x–1.3x), not one exact 1.0x projection, so a contradicted misread at a rate that gate
accepts does not come back. One higher second held for the stoppage bar blanks both as
well. The anchor of that held line moves forward while the same second is still on screen,
and the block then follows the 1:1 countdown from the end of the hold, within the one-second
bin, for the rest of the quarter. While suspended, a stoppage re-anchor cannot be above the
last clock published in that quarter. An equal second may. It cannot adopt a second a
contradicted running line could already have shown, including after a 1.0x projection has
moved past that second. After a held contradiction it may adopt the clock that held second
contradicted, when this read still matches that clock at the first sample of the hold. A
higher clock that is neither blocked line returns through the running-clock gate or a
quarter change. While that blank holds, nothing is emitted except the read that completes
the re-anchor. A quarterless read is not emitted and does not join it. A lone runoff has
no second line, so the running-clock gate still adopts it (FU1).

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
        self._dispute: list[tuple[int, int]] = []
        self.suspended = False
        # (t_ms, seconds, "run"|"hold") of the two lines a disagreement rejected.
        # Neither may re-enter. A run is blocked across the running gate's slope band.
        self._blocked_line: tuple[int, int, str] | None = None
        self._blocked_other: tuple[int, int, str] | None = None
        # A higher second held against the accepted clock. The stoppage bar voids both.
        self._held: int | None = None
        self._held_n = 0
        self._held_t0: int | None = None
        # Last clock published in the current quarter. A void does not clear it.
        # While suspended, the stoppage gate cannot adopt a second above this.
        self._published_s: int | None = None

    def _clear_cand(self) -> None:
        self._cand = None
        self._cand_n = 0
        self._cand_t0 = None

    def _clear_run(self) -> None:
        self._run = []

    def _accept(self, secs: int, quarter, t_ms: int) -> int:
        prev_q = self.last_q
        self.last_s = int(secs)
        self._published_s = int(secs)
        q = _quarter(quarter)
        if q is not None:
            self.last_q = q
        self.last_accept_t = int(t_ms)
        self._clear_cand()
        self._clear_run()
        # An ordinary accept does not clear ``_dispute``. A parallel lower line has to
        # outlast those ticks to complete. ``_extend`` restarts the list when a sample
        # leaves its line, so the anchor read at completion is the line that just
        # finished, not an older one. A held disagreement does not use this list.
        if self.reanchored:
            # A new anchor replaces the dispute. The blocked line stays blocked.
            self._dispute = []
            self.suspended = False
        self._clear_held()
        self.accepted_now = True
        # Same quarter as the one already observed. A reset or a cut is not a continuation.
        self.continuation = prev_q is not None and self.last_q == prev_q
        return int(secs)

    def _extend(self, series, secs: int, t_ms: int):
        """Append one sample. A sample that breaks the 1:1 line starts a new run."""
        sample = (int(t_ms), int(secs))
        if not series:
            return [sample]
        trial = list(series) + [sample]
        if not countdown_run_ok(trial, self.run_tol_s):
            return [sample]
        return trial

    def _accepted_rate_band(self) -> tuple[float, float]:
        """Rates the running gate can keep accepting, about 0.7x–1.3x.

        One window allows ``|drop - dt| <= run_tol_s``. A constant rate inside
        ``1 ± tol/span`` stays inside a restarted window. 2x does not.
        """
        span_s = self.run_span_ms / 1000.0
        if span_s <= 0:
            return 1.0, 1.0
        slack = self.run_tol_s / span_s
        return 1.0 - slack, 1.0 + slack

    def _advance_held_anchor(self, secs: int, t_ms: int) -> None:
        """Keep a held line's anchor on the latest read of that same second.

        A countdown that resumes after the hold is the same offset only when it is
        measured from the end of the hold. The second did not fall while it was stopped,
        so the timestamp from the start of the hold is the wrong origin.
        """
        for name in ("_blocked_line", "_blocked_other"):
            origin = getattr(self, name)
            if not origin or len(origin) < 3 or origin[2] != "hold":
                continue
            if int(secs) == int(origin[1]) and int(t_ms) >= int(origin[0]):
                setattr(self, name, (int(t_ms), int(origin[1]), "hold"))

    def _held_continuation(self, origin, secs: int, t_ms: int) -> bool:
        """True when ``secs`` is this held second or its 1:1 countdown from the anchor.

        The bin is ``run_tol_s``. A clock one fixed offset away stays outside it.
        """
        t0, s0 = int(origin[0]), int(origin[1])
        dt = (int(t_ms) - t0) / 1000.0
        if dt < 0:
            return abs(int(secs) - s0) <= self.run_tol_s
        return abs(int(secs) - (s0 - dt)) <= self.run_tol_s

    def _held_and_accepted(self):
        """The held line and the accepted line from one held contradiction, if that is the pair."""
        origins = [o for o in (self._blocked_line, self._blocked_other) if o]
        holds = [o for o in origins if (o[2] if len(o) > 2 else "run") == "hold"]
        runs = [o for o in origins if (o[2] if len(o) > 2 else "run") != "hold"]
        if len(holds) == 1 and len(runs) == 1:
            return holds[0], runs[0]
        return None

    def _on_accepted_parallel(self, hold, accepted, secs: int, t_ms: int) -> bool:
        """True when ``secs`` is the accepted clock, projected from the hold anchor.

        The offset is the gap between the two stored seconds. ``t_ms`` is the time to
        project from, so a stoppage can keep the second that matched on its first read.
        """
        offset = int(hold[1]) - int(accepted[1])
        t0, s0 = int(hold[0]), int(hold[1])
        dt = (int(t_ms) - t0) / 1000.0
        projected = s0 if dt < 0 else s0 - dt
        return abs(int(secs) - (projected - offset)) <= self.run_tol_s

    def _on_line(self, origin, samples) -> bool:
        """True when every sample still sits on this contradicted line.

        A running line matches the running gate's slope band from its origin, not one
        exact 1.0x projection. A held line matches that second and the 1:1 countdown
        from the latest read of it.
        """
        if not origin or not samples:
            return False
        t0, s0 = int(origin[0]), int(origin[1])
        kind = origin[2] if len(origin) > 2 else "run"
        lo, hi = self._accepted_rate_band()
        for t, s in samples:
            dt = (int(t) - t0) / 1000.0
            if kind == "hold":
                if not self._held_continuation(origin, int(s), int(t)):
                    return False
                continue
            if dt < 0:
                return False
            drop = s0 - int(s)
            if drop < lo * dt - self.run_tol_s or drop > hi * dt + self.run_tol_s:
                return False
        return True

    def _on_trajectory(self, origin, secs: int, t_ms: int) -> bool:
        """True when ``secs`` is a value this line could already have shown.

        A running line covers its whole slope band up to now, so a second stays
        blocked after a 1.0x projection has moved past it. A held line covers the
        1:1 countdown from the end of the hold, not only that one second.
        """
        if not origin or int(t_ms) < int(origin[0]):
            return False
        t0, s0 = int(origin[0]), int(origin[1])
        kind = origin[2] if len(origin) > 2 else "run"
        if kind == "hold":
            return self._held_continuation(origin, secs, t_ms)
        dt = (int(t_ms) - t0) / 1000.0
        _lo, hi = self._accepted_rate_band()
        low = s0 - hi * dt - self.run_tol_s
        high = s0 + self.run_tol_s
        return low <= int(secs) <= high

    def _line_blocked(self, samples) -> bool:
        """True when every sample still sits on a line a disagreement already rejected."""
        if not self._blocked_line or not samples:
            return False
        if self._on_line(self._blocked_line, samples):
            return True
        return self._on_line(self._blocked_other, samples)

    def _second_blocked(self, secs: int, t_ms: int) -> bool:
        """True when this second is one either contradicted line could already have shown.

        An equal published second is exempt: that stoppage may return. After a held
        contradiction, so may the accepted clock, when this second matched it at the
        first read of the current hold. The held line's own countdown is not exempt.
        """
        if self._published_s is not None and int(secs) == int(self._published_s):
            return False
        pair = self._held_and_accepted()
        if pair is not None:
            hold, accepted = pair
            if self._on_trajectory(hold, secs, t_ms):
                return True
            when = int(t_ms)
            if (
                self._cand is not None
                and int(self._cand) == int(secs)
                and self._cand_t0 is not None
            ):
                when = int(self._cand_t0)
            if self._on_accepted_parallel(hold, accepted, secs, when):
                return False
            return self._on_trajectory(accepted, secs, t_ms)
        return self._on_trajectory(self._blocked_line, secs, t_ms) or self._on_trajectory(
            self._blocked_other, secs, t_ms)

    def _line(self, block, kind: str) -> tuple[int, int, str] | None:
        if block is None:
            return None
        stored = block[2] if len(block) > 2 else kind
        return (int(block[0]), int(block[1]), str(stored))

    def _void(self, block: tuple | None, also: tuple | None = None) -> None:
        """Drop the accepted clock and adopt neither of the two lines that contradicted.

        ``block`` and ``also`` are those lines. A later read is kept only when it is on
        neither of them, or the quarter changes.
        """
        if block is not None:
            self._blocked_line = self._line(block, "run")
        self._blocked_other = self._line(also, "run")
        self.last_s = None
        self.last_accept_t = None
        self.suspended = True
        self._clear_run()
        self._clear_cand()
        self._dispute = []
        self.accepted_now = False
        self.continuation = False
        self.reanchored = False
        self.reanchor_kind = None
        self.rule_note = "clock_disagree"
        self._clear_held()

    def _clear_held(self) -> None:
        self._held = None
        self._held_n = 0
        self._held_t0 = None

    def _held_disagreement(self, secs: int, t_ms: int) -> bool:
        """The same higher second, held for the stoppage bar.

        A stopped true clock cannot show the three distinct seconds a running dispute
        needs. Completion blanks both. It does not adopt this read.
        """
        if secs != self._held:
            self._held = int(secs)
            self._held_n = 1
            self._held_t0 = int(t_ms)
            return False
        self._held_n += 1
        span = int(t_ms) - (int(t_ms) if self._held_t0 is None else int(self._held_t0))
        return self._held_n >= self.reanchor_reads and span >= self.reanchor_span_ms

    def _reanchor(self, secs: int, t_ms: int) -> bool:
        """Stoppage: the same second, held, and not above the last published clock.

        While suspended, a second above the last clock published in the quarter
        does not join this candidate and is not adopted. An equal second may.
        A second either contradicted line could already have shown does not,
        including after a 1.0x projection has moved on. After a held
        contradiction, the accepted clock may: a hold of the second that matched
        it on the first read of this candidate is that clock, not the misread.
        A higher clock that is not one of those lines returns through the
        running-clock gate or a quarter change.
        """
        if self.suspended and self._published_s is not None and int(secs) > int(self._published_s):
            self._clear_cand()
            return False
        if self.suspended and self._second_blocked(secs, t_ms):
            self._clear_cand()
            return False
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
        # Every sample, not the endpoints. A mid-run jump fails here.
        return countdown_run_ok(samples, self.run_tol_s)

    def _offer_run(self, secs: int, t_ms: int) -> bool:
        """Append one gated sample. True when the run is a consistent 1:1 countdown."""
        self._run = self._extend(self._run, secs, t_ms)
        if not self._run_complete(self._run):
            return False
        if self._line_blocked(self._run):
            self._clear_run()
            return False
        self.reanchored = True
        self.reanchor_kind = "run"
        self.last_run = list(self._run)
        self._clear_run()
        return True

    def _offer_dispute(self, secs: int, t_ms: int) -> bool:
        """A second 1:1 line that disagrees with the accepted clock. Completion voids both."""
        self._dispute = self._extend(self._dispute, secs, t_ms)
        return self._run_complete(self._dispute)

    def _too_high(self, secs: int) -> bool:
        if secs > QUARTER_CLOCK_MAX_S:
            self.rule_note = "clock_above_max"
            self._clear_run()
            self._clear_cand()
            self._clear_held()
            return True
        return False

    def filter(self, secs, quarter, t_ms):
        """Return ``secs`` when it is plausible, else None. None input does not move the baseline.

        An impossible step returns None and sets ``rule_note``. A running-clock re-anchor may
        return ``secs`` after a drop the one-frame rule blanked. It never returns a different
        number than the read.
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
            self._clear_held()
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
        if self.suspended:
            # Check this before the quarterless early return. A read with no quarter used
            # to be emitted unfiltered while the baseline was blank.
            quarter_break = q is not None and self.last_q is not None and q != self.last_q
            if quarter_break:
                self._blocked_line = None
                self._blocked_other = None
                self.suspended = False
                self._clear_held()
            else:
                self._clear_held()
                if q is None:
                    return None
                if self._too_high(secs):
                    return None
                self._advance_held_anchor(secs, t_ms)
                if self._reanchor(secs, t_ms) or self._offer_run(secs, t_ms):
                    self.suspended = False
                    return self._accept(secs, q, t_ms)
                return None
        if self.last_s is None:
            # A quarter-less read must not freeze the baseline for the quarter that follows.
            self._clear_held()
            if q is None:
                return secs
            if self._too_high(secs):
                return None
            self._blocked_line = None
            self._blocked_other = None
            return self._accept(secs, q, t_ms)
        if q is not None and self.last_q is not None and q < self.last_q:
            # A quarter going backwards is a cut, not a reject.
            self._blocked_line = None
            self._blocked_other = None
            self.suspended = False
            if self._too_high(secs):
                return None
            return self._accept(secs, q, t_ms)
        if q is not None and (self.last_q is None or q > self.last_q):
            self._blocked_line = None
            self._blocked_other = None
            self.suspended = False
            if 0 <= secs <= QUARTER_CLOCK_MAX_S:
                return self._accept(secs, q, t_ms)
            self.rule_note = "clock_above_max"
            self._clear_run()
            self._clear_cand()
            return None
        if secs > self.last_s:
            # Higher than the accepted clock. One frame is an increase. A sustained 1:1
            # line, or the same second held for the stoppage bar, contradicts it: blank
            # both, adopt neither.
            self.rule_note = "clock_increase"
            self._clear_run()
            self._clear_cand()
            # A hold completes on its own. Do not attach a dispute that was not
            # extended on this frame: accepted ticks leave that list running, so
            # an older ``_dispute[0]`` is not this line's anchor.
            held_done = self._held_disagreement(secs, t_ms)
            dispute_done = False if held_done else self._offer_dispute(secs, t_ms)
            if held_done or dispute_done:
                block = None
                if self.last_accept_t is not None and self.last_s is not None:
                    block = (int(self.last_accept_t), int(self.last_s), "run")
                other = None
                if dispute_done and self._dispute:
                    other = (int(self._dispute[0][0]), int(self._dispute[0][1]), "run")
                elif self._held_t0 is not None and self._held is not None:
                    other = (int(self._held_t0), int(self._held), "hold")
                self._void(block, other)
            return None
        self._clear_held()
        elapsed_s = 0 if self.last_accept_t is None else max(0.0, (t_ms - self.last_accept_t) / 1000.0)
        if self.last_s - secs <= int(elapsed_s) + 1:
            return self._accept(secs, q, t_ms)
        if self._reanchor(secs, t_ms):
            return self._accept(secs, q, t_ms)
        if self._offer_run(secs, t_ms):
            return self._accept(secs, q, t_ms)
        # Lower twin of the increase path. A lone runoff is accepted above and never gets
        # here on its completing frame. A lower line that has outlasted the accepted clock
        # (the accepted ticks cleared ``_run`` and left ``_dispute`` running) blanks both.
        if self._offer_dispute(secs, t_ms):
            origin = None
            if self._dispute:
                origin = (int(self._dispute[0][0]), int(self._dispute[0][1]), "run")
            old = None
            if self.last_accept_t is not None and self.last_s is not None:
                old = (int(self.last_accept_t), int(self.last_s), "run")
            self._void(origin, old)
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
