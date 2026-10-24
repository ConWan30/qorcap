"""Standard Madden scorebug with real digits from match-20261017-110028 (QorCap 0.3.14).

QorTime's ``madden27_standard`` pack has a "2" and a "3", but on the London HOU–JAX bar the leading
"2" matched that template at about 0.59 and the "3" at about 0.67, both under the 0.70 gate. Every
other check on those frames was fine (logo, white panel, quarter), so the timeline went blank from
11:12:59 to 11:19:52 while the score sat on 22, 23, and 29. The gate stays 0.70. These two vectors
are the mean of the real glyphs from that window (148 twos, 132 threes), added as extra variants.
A "1ST DOWNS" banner still fails ``score_panel_covered`` / ``score_slot_ink`` before any digit match.

The clock is read from the source frame, with mean glyphs from locked 170123 highlights, and kept
only when it holds or counts down. A FIELD GOAL or TOUCHDOWN banner blanks the scores and still
returns quarter and clock. The score grammar can abstain; it cannot raise a read. See
``qorcap_score_grammar`` for the windows.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

PACK = Path(__file__).resolve().parent / "packs" / "madden27_standard_score_110028.npz"
# QorCap 0.3.23: on match-20261018-084523 (CHI–ATL) the red home "14" scored 0.57-0.64 against the QorTime
# "4" template, so every read from 0-14 on was score_unsure and the draft stopped at 0-13 Q2 (real final 6-14).
# This pack holds the mean of 111 real "4" glyphs from that match (tools/add_standard_four_084523.py).
PACK_FOUR = Path(__file__).resolve().parent / "packs" / "madden27_standard_score_084523.npz"
# Mean clock glyphs from locked 170123 highlight frames. Stacked after sha16, so model_id is unchanged.
CLOCK_PACK = Path(__file__).resolve().parent / "packs" / "madden27_standard_clock_170123.npz"
# Logo search wider than the profile's ±3. The 0.90 NCC floor is unchanged. 11 px blur still misses it.
ANCHOR_SEARCH_PX = 8


QUARTER_FAIL = {"quarter_unsure", "quarter_missing", "quarter_unknown", "quarter_no_templates"}
_UNANCHORED = {"layout_unknown", "frame_invalid", "frame_too_small", "frame_aspect"}
# The score cells are covered or broken. Quarter and clock sit outside them and can still be read.
_RECOVER = {"score_panel_covered", "score_slot_ink", "score_shape", "score_glyph_count"}
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


def _clock_box_dark(reader, band, dx: int, dy: int) -> bool:
    """The clock box is still the dark well. A covered score panel does not light it up."""
    import cv2

    p = reader.profile
    cb = reader._crop(band, _shift(p.clock_box, dx, dy))
    if cb is None or cb.size == 0 or cb.shape[0] < 4:
        return False
    v = cv2.cvtColor(cb, cv2.COLOR_BGR2HSV)[..., 2].astype(np.float32)
    border = float(np.concatenate([v[:2].ravel(), v[-2:].ravel()]).mean())
    return border <= p.max_clock_box_border_v


def _stack_clock_variants(reader) -> None:
    """Extra clock glyphs. Groups are mutated in place; ``TemplatePack.sha16`` is left as built."""
    if not CLOCK_PACK.is_file():
        return
    z = np.load(CLOCK_PACK)
    clock = reader.pack.groups["clock"]
    for key in z.files:
        if len(key) < 2 or not key.startswith("d") or not key[1:].isdigit():
            continue
        label = key[1:]
        if label not in clock:
            continue
        vec = np.atleast_2d(np.asarray(z[key], dtype=np.float32))
        clock[label] = np.vstack([clock[label], vec])


class StandardBarReader:
    """``madden27_standard`` plus real digit variants.

    Scores and the quarter word are read on the 640×360 prep, so a full-resolution frame does not
    change them. The clock is read from the source frame. A banner that covers the scores blanks
    the scores and still returns quarter and clock. The score grammar can abstain; it cannot raise
    a read. ``t_ms`` None leaves the grammar and the clock history idle, so a one-shot fixture read
    stays a one-shot.
    """

    wants_full_res = True
    accepts_time = True

    def __init__(self, inner, grammar=None) -> None:
        from qorcap_clock import ClockHistory
        from qorcap_score_grammar import ScoreGrammar

        self.inner = inner
        self.profile = inner.profile
        self.profile_id = inner.profile.profile_id
        self.grammar = grammar if grammar is not None else ScoreGrammar()
        self.clocks = ClockHistory()

    def read(self, frame, t_ms=None):
        from qorcap_banner import detect_banner
        from qorcap_reader import prep

        small = prep(frame)
        fr = rescue_standard_read(self.inner, small)
        banner = None
        if fr.reason not in _UNANCHORED:
            fr = self._recover_quarter(small, fr)
            fr = self._apply_clock(frame, fr, t_ms)
            if t_ms is not None:
                banner = detect_banner(self.inner, small)
        if t_ms is not None:
            fr = self.grammar.filter(fr, t_ms, banner)
        return fr

    def _recover_quarter(self, small, fr):
        """Quarter only. Scores stay blank: a banner is not a digit source."""
        if fr.quarter is not None or fr.left_score is not None or fr.right_score is not None:
            return fr
        if fr.reason not in _RECOVER:
            return fr
        band, why = self.inner.reference_band(small)
        if band is None:
            return fr
        dx, dy = fr.offset if getattr(fr, "offset", None) else (0, 0)
        if not _clock_box_dark(self.inner, band, dx, dy):
            return fr
        q, _why, qc = self.inner._read_quarter(band, dx, dy)
        if q is None:
            return fr
        from dataclasses import replace
        return replace(fr, quarter=q, quarter_conf=round(float(qc), 4))

    def _apply_clock(self, frame, fr, t_ms):
        from dataclasses import replace

        from qorcap_clock import read_clock_box

        if fr.reason == "clock_box_missing":
            return fr
        dx, dy = fr.offset if getattr(fr, "offset", None) else (0, 0)
        full = read_clock_box(self.inner, frame, dx, dy)
        if full is not None:
            secs, text, conf, margin, red = full
            source = "full"
        else:
            secs, text = fr.clock_seconds, fr.clock_text
            conf, margin, red = fr.clock_conf, fr.clock_margin, fr.clock_red
            source = "inner"
        kept = self.clocks.filter(secs, fr.quarter, t_ms)
        if kept is None:
            if secs is None and source == "inner":
                return fr
            return replace(
                fr, clock_seconds=None, clock_text=None, clock_conf=None, clock_margin=None, clock_red=False,
            )
        if source == "inner" and kept == fr.clock_seconds:
            return fr
        return replace(
            fr,
            clock_seconds=int(kept),
            clock_text=text,
            clock_conf=None if conf is None else round(float(conf), 4),
            clock_margin=None if margin is None else round(float(margin), 4),
            clock_red=bool(red),
        )

    def note_lock(self, t_ms: int, away: int, home: int) -> None:
        """A score the agreement window locked. Not called from ``read``."""
        self.grammar.note_lock(t_ms, away, home)

    def reference_band(self, frame):
        """The prep band, so a full-resolution caller hashes the same crop as the 640 path."""
        from qorcap_reader import prep
        return self.inner.reference_band(prep(frame))

    def __getattr__(self, name):
        return getattr(self.inner, name)


def standard_reader():
    """QorTime's ScorebugReader plus the 110028 "2"/"3", the 084523 "4", and the 170123 clock variants."""
    from dataclasses import replace

    from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import ScorebugReader

    reader = ScorebugReader()
    if PACK.is_file():
        z = np.load(PACK)
        score = reader.pack.groups["score"]
        score["2"] = np.vstack([score["2"], np.atleast_2d(z["two"].astype(np.float32))])
        score["3"] = np.vstack([score["3"], np.atleast_2d(z["three"].astype(np.float32))])
    if PACK_FOUR.is_file():
        z = np.load(PACK_FOUR)
        score = reader.pack.groups["score"]
        score["4"] = np.vstack([score["4"], np.atleast_2d(z["four"].astype(np.float32))])
    _stack_clock_variants(reader)
    reader.profile = replace(reader.profile, anchor_search_px=ANCHOR_SEARCH_PX)
    return StandardBarReader(reader)
