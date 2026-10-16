"""Build sidecar/packs/madden27_end_menu_v1.npz: digit templates for Madden's end-of-game menu score.

    python tools/make_end_menu_pack.py FRAMES_DIR:AWAY:HOME [...] --fonts F1.ttf F2.ttf F3.ttf

FRAMES_DIR holds 640x360 PNG frames of the end-of-game menu whose true final (AWAY-HOME) is known
(confirmed by the player). Digits seen in those frames become REAL templates (mean glyph over every frame);
the remaining digits are SYNTHETIC: the mean of the same digit rendered in the given fonts, which were picked
because, with each real digit swapped for its synthetic version, every real glyph still classified correctly
(leave-one-out on both matches: best >= 0.754, margin >= 0.387, 0 wrong). Provenance per digit is stored in the pack (``source``) and reported in reads.

v2 (QorCap v0.3.5): v1 plus a REAL 9 from match-20261016-170000 (Monday Night presentation, final HOU 34 - 29; home
side only, the away score is covered by the online name tags):
    python tools/make_end_menu_pack.py DIR:?:29 --augment sidecar/packs/madden27_end_menu_v1.npz

v1 (QorCap v0.3.4): real 0 1 2 4 7 from match-20261016-125430 (7-14, 95 frames) and match-20261016-143212
(0-20, 300 frames); synthetic 3 5 6 8 9 from Hind SemiBold + Prompt SemiBold + Ubuntu Bold.
"""
from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "sidecar"))
from qorcap_endmenu import GLYPH, norm_vec, side_glyphs  # noqa: E402
from qorcap_reader import madden_final_screen  # noqa: E402


def render(font: str, d: str) -> np.ndarray:
    from PIL import Image, ImageDraw, ImageFont
    f = ImageFont.truetype(font, 120)
    im = Image.new("L", (200, 200), 0)
    ImageDraw.Draw(im).text((20, 10), d, 255, font=f)
    a = (np.array(im) > 127).astype(np.uint8) * 255
    ys, xs = np.nonzero(a)
    return a[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sets", nargs="+")
    ap.add_argument("--fonts", nargs="+")
    ap.add_argument("--augment", help="existing pack: keep its digits, replace only SYNTHETIC ones with real ones seen here")
    ap.add_argument("--out", default=str(HERE.parent / "sidecar" / "packs" / "madden27_end_menu_v2.npz"))
    a = ap.parse_args(argv)
    if not a.fonts and not a.augment:
        ap.error("--fonts or --augment is required")
    samples: dict[str, list[np.ndarray]] = {}
    for s in a.sets:
        d, away, home = s.rsplit(":", 2)
        for f in sorted(glob.glob(str(Path(d) / "*.png"))):
            img = cv2.imread(f)
            if not madden_final_screen(img):
                continue
            # "?" marks a side that is not usable in these frames (e.g. occluded by an online name tag)
            for side, txt in (("away", away), ("home", home)):
                if txt == "?":
                    continue
                gs = side_glyphs(img, side)
                if len(gs) != len(txt) or any(g is None for g in gs):
                    continue
                for g, c in zip(gs, txt):
                    samples.setdefault(c, []).append(g)
    base = {}
    if a.augment:
        d = np.load(a.augment)
        base = {str(lab): (v, str(s)) for lab, v, s in zip(d["labels"], d["vecs"], d["source"])}
    labels, vecs, src = [], [], []
    for dgt in "0123456789":
        if dgt in base and (base[dgt][1].startswith("real") or dgt not in samples):
            v, s = base[dgt]
        elif dgt in samples:
            m = np.mean([cv2.resize(g, GLYPH, interpolation=cv2.INTER_AREA).astype(np.float32) for g in samples[dgt]], 0)
            v, s = norm_vec(m), f"real:{len(samples[dgt])}"
        else:
            v = norm_vec(np.mean([cv2.resize(render(f, dgt), GLYPH, interpolation=cv2.INTER_AREA).astype(np.float32)
                                  for f in a.fonts], 0))
            s = "synthetic:" + "+".join(Path(f).stem for f in a.fonts)
        labels.append(dgt), vecs.append(v), src.append(s)
        print(dgt, s)
    np.savez_compressed(a.out, labels=np.array(labels), vecs=np.stack(vecs), source=np.array(src))
    print("wrote", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
