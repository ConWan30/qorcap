"""Append eye-verified real quarter glyphs (skeletons) to a Sunday Night pack, replacing synthetic ones:
  python tools/add_real_quarter_glyphs.py PACK SOURCE DIGIT FRAME..."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sidecar"))
import cv2  # noqa: E402
import qorcap_snp as S  # noqa: E402
from qorcap_mnp import norm_vec  # noqa: E402


def main():
    pack, source, digit, *frames = sys.argv[1:]
    d = dict(np.load(pack))
    labels = list(map(str, d["quarter_labels"]))
    vecs = list(d["quarter_vecs"].reshape(len(labels), -1))
    src = {str(k): str(v) for k, v in zip(d["source_keys"], d["source_vals"])}
    key = f"quarter:{digit}"
    if src.get(key, "").startswith("synthetic"):
        keep = [i for i, lab in enumerate(labels) if lab != digit]
        labels, vecs = [labels[i] for i in keep], [vecs[i] for i in keep]
        src[key] = ""
    n = 0
    for f in frames:
        h = S.hsv_of(cv2.imread(f))
        q = S.quarter_glyph(h)
        if q is None or not S.quarter_suffix(h):
            print("skip", f)
            continue
        v = norm_vec(S.skeleton(q), S.QUARTER_SIZE)
        if v is not None:
            labels.append(digit)
            vecs.append(v)
            n += 1
    src[key] = (src[key] + "+" if src.get(key) else "real:") + f"{source}:{n}"
    d["quarter_labels"] = np.array(labels, dtype="<U2")
    d["quarter_vecs"] = np.array(vecs, np.float32)
    keys = sorted(src)
    d["source_keys"], d["source_vals"] = np.array(keys), np.array([src[k] for k in keys])
    np.savez_compressed(pack, **d)
    print(key, src[key])


if __name__ == "__main__":
    main()
