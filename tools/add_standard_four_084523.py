"""Build packs/madden27_standard_score_084523.npz: the mean real "4" glyph of the standard bar.

On match-20261018-084523 (CHI–ATL, standard bar) the home "14" never passed: the QorTime "4" template scored
0.57-0.64 against the 0.70 gate, so every read from 0-14 on was score_unsure and the draft stopped at 0-13 Q2.
Input: 1 fps bottom bands (rows 600-720 of 1280x720) from video 470 s on, eye-checked as home 14.
Only bands up to b0260 are used to build the vector; the rest are held out by the test/eval check.
  python tools/add_standard_four_084523.py BANDS_DIR
"""
import glob
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sidecar"))
import cv2  # noqa: E402
from qorcap_reader import prep  # noqa: E402
from qorcap_standard import standard_reader  # noqa: E402

OUT = ROOT / "sidecar" / "packs" / "madden27_standard_score_084523.npz"


def right_glyphs(R, band_png):
    full = np.zeros((720, 1280, 3), np.uint8)
    full[600:720] = cv2.imread(band_png)
    img = prep(full)
    r = R.inner
    band, _ = r.reference_band(img)
    if band is None:
        return None
    fr = R.read(img)
    dx, dy = fr.offset if getattr(fr, "offset", None) else (0, 0)
    b = r.profile.right_score
    slot = r._crop(band, (b[0] + dx, b[1] + dx, b[2] + dy, b[3] + dy))
    if slot is None:
        return None
    g, _, _ = r.score_glyphs(slot)
    return g


def main():
    R = standard_reader()
    vecs = []
    for f in sorted(glob.glob(str(Path(sys.argv[1]) / "b*.png"))):
        if int(Path(f).stem[1:]) > 260:
            continue
        g = right_glyphs(R, f)
        if not g or len(g) != 2:
            continue
        m = R.inner.pack.match("score", g[0][0])
        if m.label == "1" and m.best > 0.8:
            vecs.append(g[1][0])
    v = np.mean(vecs, axis=0)
    v = (v / np.linalg.norm(v)).astype(np.float32)
    np.savez_compressed(OUT, four=v, source=np.array(f"real:match-20261018-084523:{len(vecs)}"))
    print("four from", len(vecs), "glyphs ->", OUT.name)


if __name__ == "__main__":
    main()
