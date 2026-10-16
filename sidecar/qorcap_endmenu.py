"""Madden end-of-game menu score reader (QorCap v0.3.4).

The end-of-game menu (FINISH GAME / ... / RESTART beside the final score panel) is QorTime's authoritative final
frame: the scorebug can disappear before the final play's points are drawn on it (match-20261016-143212: scorebug
last read 0-17 at Q4 0:00, end menu 0-20). This module reads the two team scores from that panel.

Same approach as QorTime's vendored scorebug reader (``qor_receipt._vendor.qoresence.vision.local_scorebug``):
binary glyph masks from connected components, ``norm_vec`` (resize, zero-mean, L2) against a small template pack,
best/margin gate, and ``hole_signature`` as a topology guard. The end-menu digits are a larger white face than the
scorebug's dark-on-white digits, so they get their own pack (``packs/madden27_end_menu_v1.npz``, see
tools/make_end_menu_pack.py; per-digit provenance real/synthetic is reported in every read). When QorTime is on
the path, every glyph is additionally cross-checked against QorTime's own scorebug score templates: both must give
the same digit, otherwise the frame is unreadable (fail closed).

Layout (640x360): the panel shows AWAY on the LEFT and HOME on the right, exactly like the scorebug (verified on
both 2026-10-16 matches: Saints (away) left, Giants (home) right). Team abbreviations are NOT drawn on this
screen (team logos only), so none are read. A possession arrow (~11x7 px) sits between the scores and is ignored.

A read only counts once ``EndMenuAgreement`` sees the same score on N consecutive readable end-menu frames.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

GLYPH = (12, 18)  # (w, h) normalised end-menu glyph
PACK = Path(__file__).resolve().parent / "packs" / "madden27_end_menu_v1.npz"
BAND_Y = (28, 66)                      # score digits sit at y ~38-56
SIDES = {"away": (280, 368), "home": (384, 462)}  # logos at x<276 and x>465 stay outside
GLYPH_H = (15, 22)
GLYPH_MIN_AREA = 40
GLYPH_MAX_W = 26
INK = 150                              # HSV value threshold: white digits on the near-black panel
MIN_SCORE = 0.70   # leave-one-out (real digit vs its synthetic template): best >= 0.754
MIN_MARGIN = 0.20  # leave-one-out margin >= 0.387
HOLES = {"0": {"1c"}, "4": {"1c", "0"}, "6": {"1l"}, "9": {"1u"}, "8": {"2+"}}
NO_HOLE = set("12357")


def norm_vec(patch, size=GLYPH):
    """QorTime glyphs.norm_vec (resize INTER_AREA, zero-mean, L2-normalise)."""
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


def hole_signature(mask) -> str:
    try:
        from qor_receipt._vendor.qoresence.vision.local_scorebug.glyphs import hole_signature as hs
        return hs(mask)
    except ImportError:  # same algorithm as QorTime's
        import cv2
        ink = (np.asarray(mask) > 0).astype(np.uint8)
        bg = np.pad(1 - ink, 1, constant_values=1)
        n, lab = cv2.connectedComponents(bg, connectivity=4)
        rows = [np.nonzero((lab == k).any(axis=1))[0] - 1 for k in range(1, n) if k != lab[0, 0]]
        if not rows:
            return "0"
        if len(rows) >= 2:
            return "2+"
        c = (ink.shape[0] - 1) / 2.0
        return "1u" if rows[0].max() < c else "1l" if rows[0].min() > c else "1c"


def _prep(img):
    import cv2
    if img is not None and (img.shape[1], img.shape[0]) != (640, 360):
        img = cv2.resize(img, (640, 360), interpolation=cv2.INTER_AREA)
    return img


def side_glyphs(img, side: str) -> list[np.ndarray]:
    """Binary glyph masks (left to right) of one side's score; [] or a bad count means unreadable."""
    import cv2
    img = _prep(img)
    x0, x1 = SIDES[side]
    v = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[..., 2][BAND_Y[0]:BAND_Y[1], x0:x1]
    m = (v > INK).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    out = []
    for i in range(1, n):
        x, y, w, h, a = (int(t) for t in st[i])
        if a < GLYPH_MIN_AREA or h < GLYPH_H[0]:
            continue
        if h > GLYPH_H[1] or w > GLYPH_MAX_W or x == 0 or x + w >= x1 - x0:
            out.append((x, None))  # oversized / clipped blob: make the side unreadable
            continue
        out.append((x, (lab[y:y + h, x:x + w] == i).astype(np.uint8) * 255))
    return [g for _, g in sorted(out, key=lambda t: t[0])]


@dataclass
class EndMenuRead:
    ok: bool
    away: int | None = None
    home: int | None = None
    reason: str = ""
    conf: float = 0.0
    margin: float = 0.0
    digits: list[dict[str, Any]] = field(default_factory=list)


class EndMenuReader:
    def __init__(self, pack: str | Path | None = PACK, scorebug_pack=None) -> None:
        d = np.load(str(pack))
        self.labels = [str(x) for x in d["labels"]]
        self.vecs = d["vecs"].astype(np.float32)
        self.source = dict(zip(self.labels, (str(x) for x in d["source"])))
        if scorebug_pack is None:
            scorebug_pack = load_scorebug_pack()
        self.xpack = scorebug_pack  # QorTime TemplatePack (or None: cross-check skipped and reported)

    def _digit(self, g) -> tuple[str | None, dict]:
        v = norm_vec(g)
        if v is None:
            return None, {"why": "flat"}
        s = self.vecs @ v
        order = np.argsort(-s)
        lab, best, second = self.labels[order[0]], float(s[order[0]]), float(s[order[1]])
        holes = hole_signature(g)
        info = {"label": lab, "best": round(best, 3), "margin": round(best - second, 3), "holes": holes,
                "source": self.source[lab].split(":")[0]}
        if best < MIN_SCORE or best - second < MIN_MARGIN:
            return None, dict(info, why="unsure")
        if (lab in HOLES and holes not in HOLES[lab]) or (lab in NO_HOLE and holes != "0"):
            return None, dict(info, why="holes")
        if self.xpack is not None:
            from qor_receipt._vendor.qoresence.vision.local_scorebug.glyphs import GLYPH_SIZE
            xv = norm_vec(g, GLYPH_SIZE)
            xm = self.xpack.match("score", xv)
            # QorTime's score pack has no "8" (it uses the two-hole topology instead).
            xl = "8" if (xm is None or lab == "8") and holes == "2+" else (xm.label if xm else None)
            info["xcheck"] = xl
            if xl != lab:
                return None, dict(info, why="xcheck_disagrees")
        else:
            info["xcheck"] = "skipped"
        return lab, info

    def read(self, img) -> EndMenuRead:
        if img is None:
            return EndMenuRead(False, reason="no_frame")
        img = _prep(img)
        vals, digits, conf, margin = {}, [], 1.0, 1.0
        for side in ("away", "home"):
            gs = side_glyphs(img, side)
            if not 1 <= len(gs) <= 2 or any(g is None for g in gs):
                return EndMenuRead(False, reason=f"{side}_glyph_count", digits=digits)
            txt = ""
            for g in gs:
                lab, info = self._digit(g)
                digits.append(dict(info, side=side))
                if lab is None:
                    return EndMenuRead(False, reason=f"{side}_{info.get('why')}", digits=digits)
                conf, margin = min(conf, info["best"]), min(margin, info["margin"])
                txt += lab
            if len(txt) == 2 and txt[0] == "0":
                return EndMenuRead(False, reason=f"{side}_leading_zero", digits=digits)
            vals[side] = int(txt)
        return EndMenuRead(True, vals["away"], vals["home"], "ok", conf, margin, digits)


def load_scorebug_pack():
    try:
        from qor_receipt._vendor.qoresence.vision.local_scorebug import reader as r
        import glob
        p = sorted(glob.glob(str(Path(r.__file__).parent / "packs" / "madden27_standard_v*.npz")))
        return r.TemplatePack.load(p[-1]) if p else None
    except Exception:
        return None


class EndMenuAgreement:
    """Stable once ``n`` CONSECUTIVE readable end-menu frames give the same score over >= ``min_span_ms``.
    A different score or an unreadable end-menu frame restarts the streak."""

    def __init__(self, n: int = 3, min_span_ms: int = 400) -> None:
        self.n, self.min_span_ms = n, min_span_ms
        self.streak: list[tuple[int, int, int]] = []
        self.stable: dict | None = None
        self.reads = 0
        self.unreadable = 0

    def offer(self, t_ms: int, rd: EndMenuRead) -> dict | None:
        if not rd.ok:
            self.unreadable += 1
            self.streak = []
            return self.stable
        self.reads += 1
        if self.streak and (self.streak[-1][1], self.streak[-1][2]) != (rd.away, rd.home):
            self.streak = []
        self.streak.append((t_ms, rd.away, rd.home))
        if len(self.streak) >= self.n and self.streak[-1][0] - self.streak[0][0] >= self.min_span_ms:
            if self.stable is None or (self.stable["away"], self.stable["home"]) != (rd.away, rd.home):
                srcs = sorted({d["source"] for d in rd.digits})
                self.stable = {"away": rd.away, "home": rd.home, "t_ms": self.streak[0][0], "until_ms": t_ms,
                               "frames": len(self.streak), "conf": round(rd.conf, 3), "margin": round(rd.margin, 3),
                               "template_sources": srcs,
                               "xcheck": "skipped" if any(d.get("xcheck") == "skipped" for d in rd.digits) else "qortime_scorebug_pack"}
            else:
                self.stable.update(until_ms=t_ms, frames=len(self.streak))
        return self.stable
