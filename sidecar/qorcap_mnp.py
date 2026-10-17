"""Madden 27 Monday Night (primetime) scorebug profile ``madden27_monday_night`` (QorCap v0.3.5).

Madden's primetime presentations replace the standard scorebug with their own overlay. On match-20261016-170000
(Monday Night presentation, HOU at CHI/"QOR") QorTime's ``madden27_standard`` reader returned ``layout_unknown`` on
every frame, so the live timeline was one ``scorebug_absent`` span. This module reads the Monday Night bar:

    [MNP logo] [away logo] --- AWY  <away>  >  <home>  HOM --- [home logo]  <QTR> <clock> [:pc] / <down> / ...

All coordinates are 1280x720 reference pixels (frames of any 16:9 size are rescaled to 1280x720 first; at 640x360
the score digits touch each other, so QorCap feeds this reader full-resolution frames). Same approach as QorTime's
vendored reader: binary glyph components, ``norm_vec`` (resize, zero-mean, L2) against a template pack built from
real frames of that match (``packs/madden27_monday_night_v1.npz``, tools/make_monday_night_pack.py), best/margin
gate, fail closed. AWAY is on the LEFT, HOME on the right (as in the standard bug).

Only the Monday Night variant is read here; other primetime overlays read as ``layout_unknown`` (fail closed). The
Sunday Night overlay has its own profile (qorcap_snp.py, ``madden27_sunday_night``) and so does Thursday Night
(qorcap_tnp.py, ``madden27_thursday_night``); AutoProfileReader tries both.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

PROFILE_ID = "madden27_monday_night"
PACK = Path(__file__).resolve().parent / "packs" / "madden27_monday_night_v1.npz"
REF = (1280, 720)
BAR_Y = (684, 720)
LOGO = (40, 210)                    # orange "MNP" wordmark, bar rows
LOGO_MIN_ORANGE = 0.035             # measured 0.06-0.08 when the bar is up, <= 0.02 otherwise
SCORE_X = {"away": (400, 490), "home": (516, 606)}
CLOCK_X = (876, 940)
QUARTER_X, QUARTER_Y = (836, 882), (688, 710)
SCORE_SIZE, CLOCK_SIZE, QUARTER_SIZE = (10, 16), (8, 14), (24, 10)
SCORE_H, CLOCK_H = (15, 21), (11, 15)
CLOCK_MAX_W = 13                    # a clock digit is <= 10 px wide; wider = two touching digits
INK = 150                           # HSV value: white (and red final-minute) digits on the navy bar
QUARTER_INK = 90                    # quarter word is grey
GATE = {"score": (0.80, 0.08), "clock": (0.75, 0.12), "quarter": (0.85, 0.08)}
QUARTERS = {"1ST": 1, "2ND": 2, "3RD": 3, "4TH": 4, "OT": 5}
NOT_VISIBLE = {"layout_unknown", "frame_invalid", "frame_too_small", "frame_aspect"}


def norm_vec(patch, size):
    import cv2
    arr = np.asarray(patch, dtype=np.float32)
    if arr.size == 0:
        return None
    if arr.max() > 1.0:
        arr = arr / 255.0
    v = cv2.resize(arr, size, interpolation=cv2.INTER_AREA).ravel().astype(np.float32)
    v = v - float(v.mean())
    n = float(np.linalg.norm(v))
    return None if n < 1e-6 else v / n


def to_ref(frame):
    import cv2
    if frame is None or getattr(frame, "ndim", 0) != 3:
        return None
    if (frame.shape[1], frame.shape[0]) == REF:
        return frame
    interp = cv2.INTER_AREA if frame.shape[1] > REF[0] else cv2.INTER_CUBIC
    return cv2.resize(frame, REF, interpolation=interp)


def bar_hsv(frame):
    import cv2
    f = to_ref(frame)
    return None if f is None else cv2.cvtColor(f[BAR_Y[0]:BAR_Y[1]], cv2.COLOR_BGR2HSV)


def logo_orange(hsv) -> float:
    lg = hsv[:, LOGO[0]:LOGO[1]]
    return float(((lg[..., 0] > 5) & (lg[..., 0] < 25) & (lg[..., 1] > 120) & (lg[..., 2] > 120)).mean())


def clock_thr(v) -> float:
    """The final-minute clock is red and dimmer (V ~210 vs ~250): scale the ink threshold to the clock's own peak."""
    pk = float(np.percentile(v[:, CLOCK_X[0]:CLOCK_X[1]], 99.5))
    return INK * min(1.0, max(pk, 1.0) / 250.0)


def split_pair(mask, max_w):
    """Two touching glyphs (e.g. the red final-minute "00", whose strokes bleed together) -> split at the weakest
    ink column of the middle third; a single glyph is returned as is."""
    w = mask.shape[1]
    if w <= max_w:
        return [mask]
    a, b = w // 3, w - w // 3
    c = a + int(np.argmin(mask[:, a:b].sum(axis=0)))
    parts = []
    for p in (mask[:, :c], mask[:, c + 1:] if mask[:, c].any() else mask[:, c:]):
        cols = np.nonzero(p.any(axis=0))[0]
        rows = np.nonzero(p.any(axis=1))[0]
        if len(cols) == 0:
            return [mask]
        parts.append(p[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1])
    return parts


def components(v, x0, x1, hrange, thr=INK, max_w=None):
    """Glyph masks (left to right) in bar columns x0..x1 with heights in ``hrange``; None entries = clipped/oversize.
    ``max_w``: wider blobs are treated as two touching glyphs and split."""
    import cv2
    m = (v[:, x0:x1] > thr).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    out = []
    for i in range(1, n):
        x, y, w, h, a = (int(t) for t in st[i])
        if h < hrange[0] or a < 15:
            continue
        if h > hrange[1] or x == 0 or x + w >= x1 - x0:
            out.append((x, None))
            continue
        g = (lab[y:y + h, x:x + w] == i).astype(np.uint8)
        for k, part in enumerate(split_pair(g, max_w) if max_w else [g]):
            out.append((x + k * 0.5, part))
    return [g for _, g in sorted(out, key=lambda t: t[0])]


def _fr(**kw):
    """QorTime's FrameRead when available (AgreementWindow consumes it), else a compatible local one."""
    kw.setdefault("profile_id", PROFILE_ID)
    try:
        from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import FrameRead
    except ImportError:
        FrameRead = _LocalFrameRead
    return FrameRead(**kw)


class _LocalFrameRead:
    def __init__(self, ok, reason, profile_id, left_score=None, right_score=None, quarter=None, clock_seconds=None,
                 clock_text=None, clock_red=False, score_conf=None, score_margin=None, detail=None, **_):
        self.ok, self.reason, self.profile_id = ok, reason, profile_id
        self.left_score, self.right_score, self.quarter = left_score, right_score, quarter
        self.clock_seconds, self.clock_text, self.clock_red = clock_seconds, clock_text, clock_red
        self.score_conf, self.score_margin, self.detail = score_conf, score_margin, detail or {}

    @property
    def pair(self):
        return None if self.left_score is None or self.right_score is None else (self.left_score, self.right_score)


class MondayNightReader:
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
        hsv = bar_hsv(frame)
        return (None, "frame_invalid") if hsv is None else (np.ascontiguousarray(hsv[..., 2]), "ok")

    def present(self, frame) -> bool:
        hsv = bar_hsv(frame)
        return hsv is not None and logo_orange(hsv) >= LOGO_MIN_ORANGE

    def read(self, frame) -> Any:
        hsv = bar_hsv(frame)
        if hsv is None:
            return _fr(ok=False, reason="frame_invalid")
        orange = logo_orange(hsv)
        if orange < LOGO_MIN_ORANGE:
            return _fr(ok=False, reason="layout_unknown", detail={"mnp_orange": round(orange, 3)})
        v = hsv[..., 2]
        scores, conf, margin = {}, 1.0, 1.0
        for side in ("away", "home"):
            gs = components(v, *SCORE_X[side], SCORE_H)
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
        # quarter word (grey)
        q = (v[QUARTER_Y[0] - BAR_Y[0]:QUARTER_Y[1] - BAR_Y[0], QUARTER_X[0]:QUARTER_X[1]] > QUARTER_INK).astype(np.uint8)
        ys, xs = np.nonzero(q)
        if len(xs) < 20:
            return _fr(ok=False, reason="quarter_missing", left_score=scores["away"], right_score=scores["home"])
        qlab, _, _ = self.classify("quarter", q[ys.min():ys.max() + 1, xs.min():xs.max() + 1], QUARTER_SIZE)
        if qlab is None:
            return _fr(ok=False, reason="quarter_unsure", left_score=scores["away"], right_score=scores["home"])
        # clock m:ss (colon is below the glyph size filter)
        cg = components(v, *CLOCK_X, CLOCK_H, thr=clock_thr(v), max_w=CLOCK_MAX_W)
        if len(cg) != 3 or any(g is None for g in cg):
            return _fr(ok=False, reason="clock_glyph_count", left_score=scores["away"], right_score=scores["home"])
        cd = ""
        for g in cg:
            lab, b, m = self.classify("clock", g, CLOCK_SIZE)
            if lab is None:
                return _fr(ok=False, reason="clock_unsure", left_score=scores["away"], right_score=scores["home"])
            cd += lab
        if int(cd[1:]) > 59:
            return _fr(ok=False, reason="clock_invalid")
        cx = hsv[:, CLOCK_X[0]:CLOCK_X[1]]
        ink = cx[..., 2] > INK
        red = bool(ink.any() and (((cx[..., 0] < 10) | (cx[..., 0] > 165)) & (cx[..., 1] > 120))[ink].mean() > 0.5)
        return _fr(ok=True, reason="ok", left_score=scores["away"], right_score=scores["home"], quarter=QUARTERS[qlab],
                   clock_seconds=int(cd[0]) * 60 + int(cd[1:]), clock_text=f"{cd[0]}:{cd[1:]}", clock_red=red,
                   score_conf=round(conf, 3), score_margin=round(margin, 3))


def default_extra_readers() -> list:
    """Primetime profiles tried after QorTime's standard reader, in order: Monday Night, Sunday Night (v0.3.11)."""
    from qorcap_snp import SundayNightReader
    from qorcap_tnp import ThursdayNightReader
    return [MondayNightReader(), SundayNightReader(), ThursdayNightReader()]


class AutoProfileReader:
    """Reads with QorTime's ``madden27_standard`` reader unchanged; when that layout is not on screen, tries the
    primetime profiles (Monday Night, then Sunday Night). Whichever profile yields agreed-quality reads becomes ``active`` (sticky, so the other is
    only tried when the active one stops seeing its layout). ``profile_counts`` is reported in the bundle."""

    wants_full_res = True

    def __init__(self, standard, extra: list | None = None, prep=None) -> None:
        self.readers = [standard] + list(extra if extra is not None else default_extra_readers())
        self.prep = prep  # downscale applied to frames for the standard reader (keeps it bit-identical to before)
        self.active = 0
        self.profile_counts: dict[str, int] = {}
        self._last = 0

    @staticmethod
    def _pid(r) -> str:
        return getattr(r, "profile_id", None) or getattr(getattr(r, "profile", None), "profile_id", "madden27_standard")

    @property
    def active_profile(self) -> str:
        if self.profile_counts:
            return max(self.profile_counts.items(), key=lambda kv: kv[1])[0]
        return self._pid(self.readers[0])

    def _frame_for(self, r, img):
        return img if getattr(r, "wants_full_res", False) or self.prep is None else self.prep(img)

    def read(self, img):
        order = [self.active] + [i for i in range(len(self.readers)) if i != self.active]
        first = None
        for i in order:
            r = self.readers[i]
            fr = r.read(self._frame_for(r, img))
            if first is None:
                first = (i, fr)
            if fr.ok or fr.reason not in NOT_VISIBLE:
                if fr.ok:
                    pid = self._pid(r)
                    self.profile_counts[pid] = self.profile_counts.get(pid, 0) + 1
                    self.active = i
                self._last = i
                return fr
        self._last = first[0]
        return first[1]

    def reference_band(self, img):
        r = self.readers[self._last]
        return r.reference_band(self._frame_for(r, img)) if hasattr(r, "reference_band") else (None, "")
