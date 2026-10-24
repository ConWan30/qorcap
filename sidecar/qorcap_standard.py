"""Standard Madden scorebug.

Score digits are read only from allowlisted templates in ``templates/provenance.json``
(matches 102332 and 111640). Those files are not in the repo yet. Until they are,
``read`` abstains with ``templates_missing`` and does not guess a score from the
vendored stock pack or from a pack cut from a held-out match.

The 110028 "2"/"3" and the 084523 "4" used to be stacked on the stock pack. 084523
is held out. 110028 is not an allowlisted template-source match. Both packs are
removed. ``model_id`` hashes the allowlisted template bytes (or an explicit
missing marker) plus the grammar and clock parameters.
"""
from __future__ import annotations

import hashlib
import json

import numpy as np

# Logo search wider than the profile's ±3. The 0.90 NCC floor is unchanged. 11 px blur still misses it.
# ±8 px and the full-resolution clock were tuned on the 170123 highlight kit. That match is held out.
# No template was cut from it. The constant is tracked debt in docs/TEMPLATE_PROVENANCE.md.
# Do not change it in a provenance-only patch.
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


def reader_model_id(inner, grammar, clocks) -> str:
    """Hash allowlisted template bytes plus the grammar and clock parameters.

    The vendored stock pack is not this id. Removed packs are not this id. An empty
    provenance file hashes as ``templates:missing``, which is not the previous id.
    Each loaded row contributes its role, label, and profile as well as its bytes,
    so relabelling a file changes the id. This id is what a receipt should name.
    It is not a checkpoint-hash input.
    """
    from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import READER_ID
    from qorcap_template_guard import authorized_entries

    h = hashlib.sha256()
    h.update(b"qorcap-template-provenance/v0")
    rows = sorted(authorized_entries(), key=lambda item: str(item[0].get("path")))
    if not rows:
        h.update(b"templates:missing")
    else:
        for entry, found in rows:
            h.update(str(entry.get("role") or "").encode())
            h.update(b"\0")
            h.update(str(entry.get("label") or "").encode())
            h.update(b"\0")
            h.update(str(entry.get("profile") or "").encode())
            h.update(b"\0")
            h.update(str(entry.get("path")).encode())
            h.update(b"\0")
            h.update(found.read_bytes())
    gate = inner.profile.clock_gate
    params = {
        "anchor_search_px": int(inner.profile.anchor_search_px),
        "min_logo_ncc": float(inner.profile.min_logo_ncc),
        "clock_full_res": True,
        "clock_gate_min_score": float(gate.min_score),
        "clock_gate_min_margin": float(gate.min_margin),
        "clock_reanchor_reads": int(clocks.reanchor_reads),
        "clock_reanchor_span_ms": int(clocks.reanchor_span_ms),
        "banner_window_ms": int(grammar.banner_window_ms),
        "try_window_ms": int(grammar.try_window_ms),
        "reanchor_reads": int(grammar.reanchor_reads),
        "reanchor_span_ms": int(grammar.reanchor_span_ms),
    }
    h.update(json.dumps(params, sort_keys=True, separators=(",", ":")).encode())
    return f"{READER_ID}:{inner.profile.layout_version}:{h.hexdigest()[:16]}"


class StandardBarReader:
    """``madden27_standard`` plus real digit variants.

    Scores and the quarter word are read on the 640×360 prep, so a full-resolution frame does not
    change them. The clock is read from the source frame with the stock glyphs. A banner that
    covers the scores blanks the scores and still returns quarter and clock. The score grammar can
    abstain or re-anchor; it cannot raise a read. ``t_ms`` None leaves the grammar and the clock
    history idle, so a one-shot fixture read stays a one-shot.
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

    @property
    def model_id(self) -> str:
        return reader_model_id(self.inner, self.grammar, self.clocks)

    def read(self, frame, t_ms=None):
        from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import FrameRead
        from qorcap_template_guard import (
            TEMPLATES_MISSING,
            TEMPLATES_MISSING_REASON,
            digits_complete,
            log_templates_missing,
        )

        # A partial digit set matches the nearest template and can misread.
        if not digits_complete("score", profile="standard"):
            log_templates_missing()
            return FrameRead(
                ok=False,
                reason=TEMPLATES_MISSING,
                profile_id=self.profile_id,
                detail={"templates_missing": TEMPLATES_MISSING_REASON},
            )
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
        reanchored = bool(self.clocks.reanchored)
        self.clocks.reanchored = False
        if kept is None:
            if secs is None and source == "inner":
                return fr
            return replace(
                fr, clock_seconds=None, clock_text=None, clock_conf=None, clock_margin=None, clock_red=False,
            )
        if source == "inner" and kept == fr.clock_seconds and not reanchored:
            return fr
        detail = dict(getattr(fr, "detail", None) or {})
        if reanchored:
            detail["clock_reanchor"] = True
        return replace(
            fr,
            clock_seconds=int(kept),
            clock_text=text,
            clock_conf=None if conf is None else round(float(conf), 4),
            clock_margin=None if margin is None else round(float(margin), 4),
            clock_red=bool(red),
            detail=detail,
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


def _install_authorized(reader) -> None:
    """Replace stock digits with harvested per-glyph rows. Retired npz paths are not loaded.

    Score rows are installed only when digits 0-9 are all present; otherwise
    ``read`` abstains and the stock score pack stays unused. A partial clock
    set is not installed, and the stock clock font is cleared so it cannot
    fill the gap. Quarter words are not a ten-digit set.
    """
    from qorcap_template_guard import digits_complete, harvested_vectors

    if not digits_complete("score", profile="standard"):
        return
    reader.pack.groups["score"] = harvested_vectors("score", profile="standard")
    if digits_complete("clock", profile="standard"):
        reader.pack.groups["clock"] = harvested_vectors("clock", profile="standard")
    else:
        reader.pack.groups["clock"] = {}
    quarter = harvested_vectors("quarter", profile="standard")
    if quarter:
        reader.pack.groups["quarter"] = quarter


def standard_reader():
    """QorTime geometry plus allowlisted templates. No held-out glyph is loaded."""
    from dataclasses import replace

    from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import ScorebugReader

    reader = ScorebugReader()
    _install_authorized(reader)
    reader.profile = replace(reader.profile, anchor_search_px=ANCHOR_SEARCH_PX)
    return StandardBarReader(reader)
