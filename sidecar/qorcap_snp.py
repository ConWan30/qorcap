"""Madden 27 Sunday Night (primetime) scorebug profile ``madden27_sunday_night`` (QorCap v0.3.11).

Madden's "Sunday Night Primetime" presentation (intro card "SUNDAY NIGHT PRIMETIME", "SNP" corner logo) replaces the
standard scorebug with a three-panel bar centred at the bottom of the screen:

    [away logo] :AWY  <away> | 1ST & 10 > / <QTR> <clock> :<play clock> +<yds> | <home>  HOM: [home logo]

The away panel is on the LEFT, home on the RIGHT (as in the standard bug); the panel of the team with the ball is
filled with that team's colour, the other is black. Score digits are white on either. The centre box always has a
thin white rectangular frame: that frame (two long horizontal lines at fixed rows) is the layout signature, so
detection does not depend on team colours or the corner logo. Frames: match-20261017-002326 (HOU "QOR" at CLE "EVE").

Same approach as the Monday Night profile (qorcap_mnp.py): all coordinates are 1280x720 reference pixels, binary
glyph components, ``norm_vec`` against a template pack built from real frames of that match
(``packs/madden27_sunday_night_v1.npz``, tools/make_sunday_night_pack.py), best/margin gate, fail closed.

The clock colon touches the following digit at 720p, so colon columns (two dots, empty top/middle/bottom rows) are
cleared before segmenting the clock.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from qorcap_mnp import _fr as _mnp_fr, norm_vec, to_ref

PROFILE_ID = "madden27_sunday_night"
PACK = Path(__file__).resolve().parent / "packs" / "madden27_sunday_night_v1.npz"
REF = (1280, 720)
BAR_Y = (652, 714)
FRAME_X = (580, 700)                 # centre-box white frame, columns that must be covered
FRAME_TOP, FRAME_BOT = (656, 662), (703, 709)
FRAME_MIN = 0.92                     # measured 0.99 when the bar is up; <= 0.57 on MNP / standard / menus
INNER = (664, 701)                   # inside the centre box: mostly black
INNER_MAX_BRIGHT = 0.30
SCORE_X = {"away": (499, 564), "home": (716, 787)}   # excludes the field gaps at x~566/712
SCORE_Y = (662, 704)
QUARTER_X, QUARTER_Y = (583, 611), (684, 702)
CLOCK_X, CLOCK_Y = (609, 654), (684, 702)
SCORE_SIZE, CLOCK_SIZE, QUARTER_SIZE = (12, 20), (8, 14), (8, 14)
SCORE_H, CLOCK_H = (18, 27), (10, 16)
SCORE_MAX_W, CLOCK_MAX_W = 16, 10
INK, SAT = 150, 90                   # white digits (low saturation) on black or team-colour panels
QUARTER_INK = 100                    # quarter ordinal: thin digit + grey superscript suffix
GATE = {"score": (0.80, 0.08), "clock": (0.75, 0.10), "quarter": (0.75, 0.12)}
QUARTERS = {"1": 1, "2": 2, "3": 3, "4": 4}   # "OT" has no digit -> quarter_unsure (fail closed)
NOT_VISIBLE = {"layout_unknown", "frame_invalid", "frame_too_small", "frame_aspect"}


def _fr(**kw):
    kw.setdefault("profile_id", PROFILE_ID)
    return _mnp_fr(**kw)


def hsv_of(frame):
    import cv2
    f = to_ref(frame)
    return None if f is None else cv2.cvtColor(f, cv2.COLOR_BGR2HSV)


def white(hsv, thr=INK):
    return (hsv[..., 2] > thr) & (hsv[..., 1] < SAT)


def frame_cover(hsv) -> tuple[float, float, float]:
    w = white(hsv)
    top = float(w[FRAME_TOP[0]:FRAME_TOP[1], FRAME_X[0]:FRAME_X[1]].any(axis=0).mean())
    bot = float(w[FRAME_BOT[0]:FRAME_BOT[1], FRAME_X[0]:FRAME_X[1]].any(axis=0).mean())
    inner = float((hsv[INNER[0]:INNER[1], FRAME_X[0] + 4:FRAME_X[1] - 4, 2] > 100).mean())
    return top, bot, inner


def is_present(hsv) -> bool:
    top, bot, inner = frame_cover(hsv)
    return top >= FRAME_MIN and bot >= FRAME_MIN and inner <= INNER_MAX_BRIGHT


def clock_thr(hsv) -> float:
    """A final-minute clock may be dimmer/red: scale the ink threshold to the clock's own peak."""
    pk = float(np.percentile(hsv[CLOCK_Y[0]:CLOCK_Y[1], CLOCK_X[0]:CLOCK_X[1], 2], 99.5))
    return INK * min(1.0, max(pk, 1.0) / 250.0)


def _runs(col):
    idx = np.nonzero(col)[0]
    if len(idx) == 0:
        return []
    cuts = np.nonzero(np.diff(idx) > 1)[0]
    starts = np.r_[idx[0], idx[cuts + 1]]
    ends = np.r_[idx[cuts], idx[-1]]
    return list(zip(starts.tolist(), ends.tolist()))


def clear_colons(m):
    """Zero colon columns. At 720p the clock colon touches the next digit; a colon column holds exactly two short ink
    runs (the dots) separated by a gap around the band centre, with no ink in the band's first two rows. Digit
    columns never look like that (they have a long stroke, a centre bar, or ink in the band's top rows)."""
    rows = np.nonzero(m.any(axis=1))[0]
    if len(rows) < 8:
        return m
    y0, y1 = int(rows[0]), int(rows[-1])
    h = y1 - y0 + 1
    m0, m1 = y0 + int(h * 0.5), y0 + int(h * 0.6)       # band centre rows: empty between the two dots
    out = m.copy()
    for c in range(m.shape[1]):
        col = m[:, c]
        if col[y0:y0 + 2].any() or col[m0:m1 + 1].any():
            continue
        r = _runs(col)
        if len(r) == 2 and r[0][1] < m0 and r[1][0] > m1 and max(e - b for b, e in r) <= h // 3:
            out[:, c] = 0
    return out


def _split(mask, patch, max_w):
    """Two touching glyphs -> split at the weakest ink column of the middle third (same rule as the Monday Night
    reader's split_pair); returns (mask, patch) pairs trimmed to their ink."""
    w = mask.shape[1]
    if max_w is None or w <= max_w:
        return [(mask, patch)]
    a, b = w // 3, w - w // 3
    c = a + int(np.argmin(mask[:, a:b].sum(axis=0)))
    out = []
    for lo, hi in ((0, c), (c + 1 if mask[:, c].any() else c, w)):
        pm = mask[:, lo:hi]
        cols, rows = np.nonzero(pm.any(axis=0))[0], np.nonzero(pm.any(axis=1))[0]
        if len(cols) == 0:
            return [(mask, patch)]
        sl = (slice(rows[0], rows[-1] + 1), slice(lo + cols[0], lo + cols[-1] + 1))
        out.append((mask[sl], patch[sl]))
    return out


def components(mask, hrange, max_w=None, min_area=12, gray=None):
    """Glyph patches (left to right) with heights in ``hrange``; None entries = clipped/oversize blobs. With ``gray``
    (a 0..255 map of the same region) a patch is the grey glyph (gray/255 inside the 1-px-dilated component), which
    keeps the partly filled counters of the heavy primetime font (5 vs 6, 3 vs 8); else the binary mask."""
    import cv2
    m = mask.astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    out = []
    W = m.shape[1]
    k = np.ones((3, 3), np.uint8)
    for i in range(1, n):
        x, y, w, h, a = (int(t) for t in st[i])
        if h < hrange[0] or a < min_area:
            continue
        if h > hrange[1] or x == 0 or x + w >= W:
            out.append((x, None))
            continue
        g = (lab[y:y + h, x:x + w] == i).astype(np.uint8)
        if gray is not None:
            sel = cv2.dilate((lab == i).astype(np.uint8), k)[y:y + h, x:x + w]
            patch = np.asarray(gray, np.float32)[y:y + h, x:x + w] / 255.0 * sel
        else:
            patch = g
        for j, (pm, pp) in enumerate(_split(g, patch, max_w)):
            if pm.shape[0] >= hrange[0]:
                out.append((x + j * 0.5, pp))
    return [g for _, g in sorted(out, key=lambda t: t[0])]


def score_glyphs(hsv, side):
    x0, x1 = SCORE_X[side]
    reg = hsv[SCORE_Y[0]:SCORE_Y[1], x0:x1]
    # grey = whiteness (value scaled down by saturation) so the red/colour panel itself stays dark
    grey = (reg[..., 2].astype(np.float32) * (1.0 - reg[..., 1].astype(np.float32) / 255.0)).clip(0, 255)
    return components(white(reg), SCORE_H, max_w=SCORE_MAX_W, gray=grey)


def clock_glyphs(hsv):
    v = hsv[CLOCK_Y[0]:CLOCK_Y[1], CLOCK_X[0]:CLOCK_X[1], 2]
    m = clear_colons(v > clock_thr(hsv))   # black box: value only (a red final-minute clock still reads)
    return components(m, CLOCK_H, max_w=CLOCK_MAX_W, gray=v * (m.any(axis=0)[None, :] > 0))


def skeleton(mask):
    """1-px morphological skeleton (the quarter digit is a thin font; templates for unseen quarters come from
    skeletonised clock digits, so both sides are compared as skeletons)."""
    import cv2
    img = (np.asarray(mask) > 0).astype(np.uint8)
    img = cv2.copyMakeBorder(img, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
    k = cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3))
    sk = np.zeros_like(img)
    while img.any():
        er = cv2.erode(img, k)
        sk |= img - cv2.dilate(er, k)
        img = er
    sk = sk[1:-1, 1:-1]
    ys, xs = np.nonzero(sk)
    return sk if len(xs) == 0 else sk[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def quarter_glyph(hsv):
    """The quarter digit: the leftmost tall component in the quarter box (the ordinal suffix is superscript-small)."""
    q = hsv[QUARTER_Y[0]:QUARTER_Y[1], QUARTER_X[0]:QUARTER_X[1], 2] > QUARTER_INK
    gs = components(q, CLOCK_H, min_area=8)
    return gs[0] if gs else None


def quarter_suffix(hsv) -> bool:
    """Ordinal suffix present right of the digit (small grey glyphs in the upper half): guards against reading a
    stray clock-like digit as the quarter."""
    q = hsv[QUARTER_Y[0]:QUARTER_Y[1], QUARTER_X[0]:QUARTER_X[1], 2] > QUARTER_INK
    cols = np.nonzero(q.any(axis=0))[0]
    if len(cols) == 0:
        return False
    rows = np.nonzero(q.any(axis=1))[0]
    mid = (int(rows[0]) + int(rows[-1])) // 2
    first_gap = next((c for c in range(int(cols[0]), q.shape[1]) if not q[:, c].any()), q.shape[1])
    right = q[:, first_gap:]
    return bool(right[:mid + 1].sum() >= 8 and right[mid + 3:].sum() <= 2)


class SundayNightReader:
    profile_id = PROFILE_ID
    wants_full_res = True

    def __init__(self, pack: str | Path = PACK) -> None:
        d = np.load(str(pack))
        self.groups: dict[str, tuple[list[str], np.ndarray]] = {}
        for g in ("score", "clock", "quarter"):
            self.groups[g] = ([str(x) for x in d[f"{g}_labels"]], d[f"{g}_vecs"].astype(np.float32))
        self.source = {str(k): str(v) for k, v in zip(d["source_keys"], d["source_vals"])}

    @property
    def model_id(self) -> str:
        return f"qorcap_{PROFILE_ID}_v1"

    def classify(self, group: str, mask, size) -> tuple[str | None, float, float]:
        v = norm_vec(mask, size)
        if v is None:
            return None, 0.0, 0.0
        labels, vecs = self.groups[group]
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
        return hsv is not None and is_present(hsv)

    def read(self, frame) -> Any:
        hsv = hsv_of(frame)
        if hsv is None:
            return _fr(ok=False, reason="frame_invalid")
        top, bot, inner = frame_cover(hsv)
        if not (top >= FRAME_MIN and bot >= FRAME_MIN and inner <= INNER_MAX_BRIGHT):
            return _fr(ok=False, reason="layout_unknown", detail={"snp_frame": [round(top, 2), round(bot, 2), round(inner, 2)]})
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
        q = quarter_glyph(hsv)
        if q is None or not quarter_suffix(hsv):
            return _fr(ok=False, reason="quarter_missing", **partial)
        qlab, _, _ = self.classify("quarter", skeleton(q), QUARTER_SIZE)
        if qlab is None or qlab not in QUARTERS:
            return _fr(ok=False, reason="quarter_unsure", **partial)
        cg = clock_glyphs(hsv)
        if not 3 <= len(cg) <= 4 or any(g is None for g in cg):
            return _fr(ok=False, reason="clock_glyph_count", **partial)
        cd = ""
        for g in cg:
            lab, b, m = self.classify("clock", g, CLOCK_SIZE)
            if lab is None:
                return _fr(ok=False, reason="clock_unsure", **partial)
            cd += lab
        mm, ss = cd[:-2], cd[-2:]
        if int(ss) > 59 or (len(mm) == 2 and mm[0] == "0") or int(mm) > 15:
            return _fr(ok=False, reason="clock_invalid", **partial)
        cx = hsv[CLOCK_Y[0]:CLOCK_Y[1], CLOCK_X[0]:CLOCK_X[1]]
        ink = cx[..., 2] > clock_thr(hsv)
        red = bool(ink.any() and (((cx[..., 0] < 10) | (cx[..., 0] > 165)) & (cx[..., 1] > 120))[ink].mean() > 0.5)
        return _fr(ok=True, reason="ok", quarter=QUARTERS[qlab], clock_seconds=int(mm) * 60 + int(ss),
                   clock_text=f"{int(mm)}:{ss}", clock_red=red, score_conf=round(conf, 3), score_margin=round(margin, 3),
                   **partial)
