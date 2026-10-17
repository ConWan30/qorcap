"""Madden 27 *Thursday Night* (primetime "TNP") scorebug profile ``madden27_thursday_night`` (QorCap 0.3.13).

A compact two-row bar at the bottom centre: away (left) and home (right) team panels with big white scores either side
of a silver "TNP" shield; below, on black, down & distance, the quarter ordinal (grey "1ST"), the game clock (white),
the play clock (grey) and the ball spot. Different geometry and fonts from Sunday Night (``qorcap_snp``), so it is a
separate profile with its own pack (``packs/madden27_thursday_night_v1.npz``, built by
``tools/make_thursday_night_pack.py`` from match-20261017-010703).

Detection: normalised correlation of the shield (V channel) with the pack's shield template; measured 1.00 when the
bar is up and <= 0.25 on Sunday Night, Monday Night, standard, menus and pause frames. Everything else fails closed
exactly like the other profiles (glyph counts, template gates, ordinal suffix, clock sanity)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from qorcap_mnp import norm_vec
from qorcap_snp import _fr as _snp_fr, _runs, components, hsv_of, skeleton, white

PROFILE_ID = "madden27_thursday_night"
PACK = Path(__file__).resolve().parent / "packs" / "madden27_thursday_night_v1.npz"
BAR_Y = (638, 710)
BADGE = (slice(644, 684), slice(624, 658))
BADGE_MIN = 0.80
SCORE_X = {"away": (561, 620), "home": (662, 714)}
SCORE_Y = (645, 682)                 # starts just above the digits: bright stands behind the panel top merge otherwise
QUARTER_X, QUARTER_Y = (584, 612), (684, 706)
CLOCK_X, CLOCK_Y = (614, 669), (684, 706)       # play clock (grey, V <= ~190) starts at x~675
SCORE_SIZE, CLOCK_SIZE, QUARTER_SIZE = (12, 24), (8, 14), (8, 14)
SCORE_H, CLOCK_H, QUARTER_H = (24, 36), (10, 18), (7, 14)
SCORE_MAX_W, CLOCK_MAX_W = 24, 15
INK, CLOCK_INK, QUARTER_INK = 150, 205, 100
# Quarter must win by a clear margin. 0.12 let a fused "1ST" blob (digit touching the suffix) beat the real
# 2ND digit templates from match-20261017-010703 and label that quarter as Q1. Known-good quarters on these
# packs clear 0.41 (2 vs 3 is the tightest). Below that, the quarter stays unknown.
GATE = {"score": (0.80, 0.08), "clock": (0.75, 0.10), "quarter": (0.75, 0.30)}
QUARTERS = {"1": 1, "2": 2, "3": 3, "4": 4, "OT": 5}  # 2ND has no crop from match-20261017-091651 (templates stay the 010703 ones)
# A digit is 5–8 px wide. "OT" is the whole ordinal (~9). A "1ST" whose suffix touches the digit is ~12 and must
# be cut back to the digit; classifying that fused blob is what made 2ND read as 1ST.
QUARTER_DIGIT_MAX_W = 8
QUARTER_BLOB_MAX_W = 10


def _fr(**kw):
    kw["profile_id"] = PROFILE_ID
    return _snp_fr(**kw)


def _nv(a):
    a = np.asarray(a, np.float32)
    a = a - a.mean()
    return a / (float(np.linalg.norm(a)) + 1e-6)


def badge_score(hsv, template) -> float:
    return float((_nv(hsv[..., 2][BADGE]) * template).sum())


def score_glyphs(hsv, side):
    x0, x1 = SCORE_X[side]
    reg = hsv[SCORE_Y[0]:SCORE_Y[1], x0:x1]
    grey = (reg[..., 2].astype(np.float32) * (1.0 - reg[..., 1].astype(np.float32) / 255.0)).clip(0, 255)
    return components(white(reg, INK), SCORE_H, max_w=SCORE_MAX_W, gray=grey)


def clock_mask(hsv):
    v = hsv[CLOCK_Y[0]:CLOCK_Y[1], CLOCK_X[0]:CLOCK_X[1], 2]
    pk = float(np.percentile(v, 99.5))
    thr = max(150.0, CLOCK_INK * min(1.0, max(pk, 1.0) / 250.0))   # dimmer/red final-minute clock still reads
    return v, clear_colons(v > thr)


def clear_colons(m):
    """Zero colon columns: exactly two short ink runs (the dots, each <= a third of the digit height + 1) separated by
    a gap of >= 2 rows, with no ink in the digit band's top two or bottom two rows. Digit columns never look like
    that at 720p (a long stroke, or ink at the band's top/bottom). The TNP colon dots are taller than Sunday Night's,
    so the SNP centre-rows rule does not apply."""
    m = np.asarray(m, bool)
    rows = np.nonzero(m.any(axis=1))[0]
    if len(rows) < 8:
        return m
    y0, y1 = int(rows[0]), int(rows[-1])
    h = y1 - y0 + 1
    out = m.copy()
    for c in range(m.shape[1]):
        col = m[:, c]
        if col[y0:y0 + 2].any() or col[y1 - 1:y1 + 1].any():
            continue
        r = _runs(col)
        if len(r) == 2 and r[1][0] - r[0][1] >= 3 and max(e - b + 1 for b, e in r) <= h // 3 + 1:
            out[:, c] = False
    return out


def bridge(m):
    """Fill one-column gaps with ink on both sides in the same row: the TNP clock "4" has a 1-px break between its
    arm and stem at 720p. Digits are >= 2 columns apart (colons are cleared first), so this never joins two glyphs."""
    m = np.asarray(m, bool)
    out = m.copy()
    out[:, 1:-1] |= m[:, :-2] & m[:, 2:]
    return out


def clock_glyphs(hsv):
    v, m = clock_mask(hsv)
    m = bridge(m)
    return components(m, CLOCK_H, max_w=CLOCK_MAX_W, gray=v * (m.any(axis=0)[None, :] > 0))


def quarter_mask(hsv):
    return hsv[QUARTER_Y[0]:QUARTER_Y[1], QUARTER_X[0]:QUARTER_X[1], 2] > QUARTER_INK


def quarter_glyph(hsv):
    """The quarter digit only. A thin bridge to the ordinal suffix (1ST on match-20261017-091651) is cut, and a blob
    that is still wider than a digit or "OT" is refused so it cannot match another quarter's template."""
    import cv2
    m = quarter_mask(hsv).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    cands = []
    for i in range(1, n):
        x, y, w, h, a = (int(t) for t in st[i])
        if QUARTER_H[0] <= h <= QUARTER_H[1] + 4 and a >= 6:
            cands.append((x, y, w, h, i))
    if not cands:
        return None
    x, y, w, h, i = min(cands)
    g = lab[y:y + h, x:x + w] == i
    col = g.sum(axis=0)
    peak = 0
    cut = None
    for c, v in enumerate(int(t) for t in col):
        peak = max(peak, v)
        rest = col[c + 1:]
        if c >= 4 and peak >= 4 and v <= 2 and rest.size and int(rest.max()) >= 4:
            cut = c
            break
    if cut is not None:
        g = g[:, :cut]
        cols, rows = np.nonzero(g.any(axis=0))[0], np.nonzero(g.any(axis=1))[0]
        if len(cols) == 0:
            return None
        g = g[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]
    if g.shape[1] > (QUARTER_DIGIT_MAX_W if cut is not None else QUARTER_BLOB_MAX_W):
        return None
    return g.astype(np.uint8)


def quarter_suffix(hsv) -> bool:
    q = quarter_mask(hsv)
    cols = np.nonzero(q.any(axis=0))[0]
    if len(cols) == 0:
        return False
    rows = np.nonzero(q.any(axis=1))[0]
    mid = (int(rows[0]) + int(rows[-1])) // 2
    first_gap = next((c for c in range(int(cols[0]), q.shape[1]) if not q[:, c].any()), q.shape[1])
    right = q[:, first_gap:]
    return bool(right[:mid + 1].sum() >= 8 and right[mid + 3:].sum() <= 2)


class ThursdayNightReader:
    profile_id = PROFILE_ID
    wants_full_res = True

    def __init__(self, pack: str | Path = PACK) -> None:
        d = np.load(str(pack))
        self.groups: dict[str, tuple[list[str], np.ndarray]] = {}
        for g in ("score", "clock", "quarter"):
            self.groups[g] = ([str(x) for x in d[f"{g}_labels"]], d[f"{g}_vecs"].astype(np.float32))
        self.badge = d["badge"].astype(np.float32)
        self.source = {str(k): str(v) for k, v in zip(d["source_keys"], d["source_vals"])}

    @property
    def model_id(self) -> str:
        return f"qorcap_{PROFILE_ID}_v1"

    def classify(self, group, mask, size):
        v = norm_vec(mask, size)
        if v is None:
            return None, 0.0, 0.0
        labels, vecs = self.groups[group]
        if not labels:
            return None, 0.0, 0.0
        s = vecs @ v
        o = np.argsort(-s)
        best = float(s[o[0]])
        second = max((float(s[j]) for j in o[1:] if labels[j] != labels[o[0]]), default=-1.0)
        lo, mg = GATE[group]
        if best < lo or best - second < mg:
            return None, best, best - second
        return labels[o[0]], best, best - second

    def reference_band(self, frame):
        hsv = hsv_of(frame)
        return (None, "frame_invalid") if hsv is None else (np.ascontiguousarray(hsv[BAR_Y[0]:BAR_Y[1], :, 2]), "ok")

    def present(self, frame) -> bool:
        hsv = hsv_of(frame)
        return hsv is not None and badge_score(hsv, self.badge) >= BADGE_MIN

    def read(self, frame) -> Any:
        hsv = hsv_of(frame)
        if hsv is None:
            return _fr(ok=False, reason="frame_invalid")
        bs = badge_score(hsv, self.badge)
        if bs < BADGE_MIN:
            return _fr(ok=False, reason="layout_unknown", detail={"tnp_badge": round(bs, 3)})
        scores, conf, margin = {}, 1.0, 1.0
        for side in ("away", "home"):
            gs = score_glyphs(hsv, side)
            if not 1 <= len(gs) <= 2 or any(g is None for g in gs):
                return _fr(ok=False, reason=f"score_{side}_glyph_count")
            txt = ""
            for g in gs:
                lab, b, m = self.classify("score", g, SCORE_SIZE)
                if lab is None:
                    return _fr(ok=False, reason=f"score_{side}_unsure", detail={"best": round(b, 3), "margin": round(m, 3)})
                conf, margin = min(conf, b), min(margin, m)
                txt += lab
            if len(txt) == 2 and txt[0] == "0":
                return _fr(ok=False, reason=f"score_{side}_leading_zero")
            scores[side] = int(txt)
        partial = dict(left_score=scores["away"], right_score=scores["home"])
        # Quarter is not a gate for the clock. A missing/unknown ordinal (no 2ND sample this broadcast, a future
        # glyph the pack has not seen) still returns the scores and, when the clock glyphs clear their own gates,
        # the clock. ``ok`` stays false so the normal agreement key, which includes the quarter, is not used.
        q_reason = None
        q = quarter_glyph(hsv)
        if q is None or not quarter_suffix(hsv):
            q_reason = "quarter_missing"
        else:
            qlab, _, _ = self.classify("quarter", skeleton(q), QUARTER_SIZE)
            # A label with no real template in this pack is not a quarter we can emit (2ND stays the 010703
            # digit templates; if those do not win clearly, the quarter is unknown rather than borrowed from 1ST).
            if qlab is None or qlab not in QUARTERS or f"quarter:{qlab}" not in self.source:
                q_reason = "quarter_unsure"
            else:
                partial["quarter"] = QUARTERS[qlab]
        clock, clock_reason = {}, None
        cg = clock_glyphs(hsv)
        if not 3 <= len(cg) <= 4 or any(g is None for g in cg):
            clock_reason = "clock_glyph_count"
        else:
            cd = ""
            for g in cg:
                lab, _, _ = self.classify("clock", g, CLOCK_SIZE)
                if lab is None:
                    clock_reason = "clock_unsure"
                    break
                cd += lab
            else:
                mm, ss = cd[:-2], cd[-2:]
                if int(ss) > 59 or (len(mm) == 2 and mm[0] == "0") or int(mm) > 15:
                    clock_reason = "clock_invalid"
                else:
                    cx = hsv[CLOCK_Y[0]:CLOCK_Y[1], CLOCK_X[0]:CLOCK_X[1]]
                    ink = clock_mask(hsv)[1]
                    red = bool(ink.any() and (((cx[..., 0] < 10) | (cx[..., 0] > 165)) & (cx[..., 1] > 120))[ink].mean() > 0.5)
                    clock = dict(clock_seconds=int(mm) * 60 + int(ss), clock_text=f"{int(mm)}:{ss}", clock_red=red)
        if q_reason:
            return _fr(ok=False, reason=q_reason, score_conf=round(conf, 3), score_margin=round(margin, 3),
                       **partial, **clock)
        if clock_reason:
            return _fr(ok=False, reason=clock_reason, score_conf=round(conf, 3), score_margin=round(margin, 3), **partial)
        return _fr(ok=True, reason="ok", score_conf=round(conf, 3), score_margin=round(margin, 3), **partial, **clock)
