"""Build sidecar/packs/madden27_monday_night_v1.npz from real frames of a Monday Night match.

    python tools/make_monday_night_pack.py BAR_STRIP.mp4 tools/monday_night_labels.json [--out PACK]

BAR_STRIP.mp4 is the bottom 72 rows of the 1280x720 recording, e.g.
    ffmpeg -i video.mp4 -vf "fps=2,crop=1280:72:0:648" -an strip.mp4
Glyphs are segmented exactly as sidecar/qorcap_mnp.py does, greedily clustered (deterministic, frame order), and
the clusters (ranked by size) are labelled by hand from a montage; the labels file maps rank -> label per group.
Each template is the mean of up to 300 member vectors. Score digits never seen on the score (e.g. "5" in the
source match) are taken from the clock glyph of the same font, resized, and marked synthetic in the pack.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sidecar"))
import qorcap_mnp as M  # noqa: E402


def frames(path):
    import cv2
    cap = cv2.VideoCapture(str(path))
    while True:
        ok, s = cap.read()
        if not ok:
            return
        f = np.zeros((720, 1280, 3), np.uint8)
        f[720 - s.shape[0]:] = s
        yield f


def glyph_sets(f):
    hsv = M.bar_hsv(f)
    if M.logo_orange(hsv) < M.LOGO_MIN_ORANGE:
        return {}
    v = hsv[..., 2]
    out = {"score": [g for s in ("away", "home") for g in M.components(v, *M.SCORE_X[s], M.SCORE_H) if g is not None],
           "clock": [g for g in M.components(v, *M.CLOCK_X, M.CLOCK_H, thr=M.clock_thr(v), max_w=M.CLOCK_MAX_W) if g is not None]}
    q = (v[M.QUARTER_Y[0] - M.BAR_Y[0]:M.QUARTER_Y[1] - M.BAR_Y[0], M.QUARTER_X[0]:M.QUARTER_X[1]] > M.QUARTER_INK)
    ys, xs = np.nonzero(q)
    out["quarter"] = [q[ys.min():ys.max() + 1, xs.min():xs.max() + 1].astype(np.uint8)] if len(xs) >= 20 else []
    return out


SIZES = {"score": M.SCORE_SIZE, "clock": M.CLOCK_SIZE, "quarter": M.QUARTER_SIZE}


def cluster(video, thr=0.9):
    cl = {g: ([], []) for g in SIZES}
    for f in frames(video):
        for g, masks in glyph_sets(f).items():
            cents, mem = cl[g]
            for m in masks:
                v = M.norm_vec(m, SIZES[g])
                if v is None:
                    continue
                if cents:
                    s = np.array(cents) @ v
                    j = int(s.argmax())
                    if s[j] > thr:
                        mem[j].append(v)
                        continue
                cents.append(v)
                mem.append([v])
    return {g: sorted(mem, key=len, reverse=True) for g, (_, mem) in cl.items()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("strip")
    ap.add_argument("labels")
    ap.add_argument("--out", default=str(M.PACK))
    a = ap.parse_args(argv)
    labels = json.loads(Path(a.labels).read_text())
    cl = cluster(a.strip)
    out, src = {}, {}
    for g in SIZES:
        labs, vecs = [], []
        for rank, lab in labels[g].items():
            mem = cl[g][int(rank)][:300]
            v = np.mean(mem, axis=0)
            labs.append(lab)
            vecs.append(v / np.linalg.norm(v))
            src.setdefault(f"{g}:{lab}", f"real:{a.labels and Path(a.strip).name}")
        out[g] = (labs, vecs)
    # synthetic score digits from the clock font where the source match never showed them
    import cv2
    clabs, cvecs = out["clock"]
    for d in "0123456789":
        if d not in out["score"][0] and d in clabs:
            cv = cvecs[clabs.index(d)].reshape(M.CLOCK_SIZE[1], M.CLOCK_SIZE[0])
            sv = cv2.resize(cv, M.SCORE_SIZE, interpolation=cv2.INTER_CUBIC).ravel()
            sv = sv - sv.mean()
            out["score"][0].append(d)
            out["score"][1].append(sv / np.linalg.norm(sv))
            src[f"score:{d}"] = "synthetic:clock_font_resized"
    np.savez_compressed(a.out, **{f"{g}_labels": np.array(out[g][0]) for g in SIZES},
                        **{f"{g}_vecs": np.array(out[g][1], np.float32) for g in SIZES},
                        source_keys=np.array(list(src)), source_vals=np.array(list(src.values())))
    print({g: sorted(set(out[g][0])) for g in SIZES}, {k: v for k, v in src.items() if v.startswith("synthetic")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
