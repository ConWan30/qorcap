"""v0.3.21: real Thursday Night quarter digits from match-20261017-232726 and match-20261018-002316.

On those two matches the 010703 quarter templates scored 0.36-0.70 against the real "1ST/2ND/3RD" digits (gate 0.75,
margin 0.30): 1,330 of 1,656 bars with a sure score posted no quarter. This appends 40 real skeletons per (match,
quarter) from tools/thursday_night_labels/quarter_232726_002316.json (labels: clock-reset segments, cross-checked by
k-means clusters of the glyphs, Glimpse crop reads 88/88 and an eye check). Gates are unchanged.

  python tools/add_tnp_quarter_232726_002316.py FRAMES_ROOT [PACK]

FRAMES_ROOT/<match>/<video stem>/f%05d.png are 1280x120 bottom bands (rows 600-720, ffmpeg fps=1/2,crop=1280:120:0:600).
"""
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sidecar"))
import cv2  # noqa: E402
import qorcap_tnp as T  # noqa: E402
from qorcap_mnp import norm_vec  # noqa: E402
from qorcap_snp import hsv_of, skeleton  # noqa: E402

LABELS = ROOT / "tools" / "thursday_night_labels" / "quarter_232726_002316.json"
TAG = {"match-20261017-232726": "232726", "match-20261018-002316": "002316"}


def frame(root: Path, rec: dict):
    band = cv2.imread(str(root / rec["match"] / Path(rec["video"]).stem / f"f{rec['i']:05d}.png"))
    if band is None:
        raise SystemExit(f"missing frame {rec}")
    img = np.zeros((720, 1280, 3), np.uint8)
    img[600:720] = band
    return img


def main():
    root = Path(sys.argv[1])
    pack = Path(sys.argv[2]) if len(sys.argv) > 2 else T.PACK
    d = dict(np.load(pack))
    labels = list(map(str, d["quarter_labels"]))
    vecs = list(d["quarter_vecs"].reshape(len(labels), -1))
    src = {str(k): str(v) for k, v in zip(d["source_keys"], d["source_vals"])}
    if any("232726" in v or "002316" in v for v in src.values()):
        raise SystemExit("pack already has the 232726/002316 quarters")
    counts: dict[tuple[str, str], int] = {}
    for rec in json.loads(LABELS.read_text())["frames"]:
        h = hsv_of(frame(root, rec))
        q = T.quarter_glyph(h)
        if q is None or not T.quarter_suffix(h):
            raise SystemExit(f"no quarter glyph in {rec}")
        v = norm_vec(skeleton(q), T.QUARTER_SIZE)
        lab = str(rec["quarter"])
        labels.append(lab)
        vecs.append(v)
        counts[(lab, rec["match"])] = counts.get((lab, rec["match"]), 0) + 1
    for (lab, m), n in sorted(counts.items()):
        key = f"quarter:{lab}"
        src[key] = (src[key] + "+" if src.get(key) else "real:") + f"{m}:{n}"
    d["quarter_labels"] = np.array(labels, dtype="<U2")
    d["quarter_vecs"] = np.array(vecs, np.float32)
    keys = sorted(src)
    d["source_keys"], d["source_vals"] = np.array(keys), np.array([src[k] for k in keys])
    np.savez_compressed(pack, **d)
    print({k: src[k] for k in keys if k.startswith("quarter")})


if __name__ == "__main__":
    main()
