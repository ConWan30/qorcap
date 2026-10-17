"""Build packs/madden27_thursday_night_v1.npz from real Thursday Night frames (QorCap 0.3.11).

  python tools/make_thursday_night_pack.py FRAMES... --montage DIR          # cluster glyphs, write cluster grids
  python tools/make_thursday_night_pack.py FRAMES... --labels L.json --out P.npz --source MATCH

Glyphs are greedily clustered (cosine >= 0.92); a human labels each cluster by rank from the montage (``--labels``:
{"score": {"0": "7", ...}, "clock": {...}, "quarter": {...}}; "x" or missing = dropped). Only labelled real glyphs
become templates: no synthetic digits. Digits never seen stay absent from the pack, so reading them fails closed
(``*_unsure``) until real frames are added. Per-digit provenance goes in source_keys/source_vals."""
from __future__ import annotations

import argparse, glob, json, os, sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sidecar"))
import cv2  # noqa: E402
import qorcap_tnp as T  # noqa: E402
from qorcap_mnp import norm_vec  # noqa: E402
from qorcap_snp import hsv_of, skeleton  # noqa: E402

SIZES = {"score": T.SCORE_SIZE, "clock": T.CLOCK_SIZE, "quarter": T.QUARTER_SIZE}


def glyphs(img, badge):
    hsv = hsv_of(img)
    if hsv is None or (badge is not None and T.badge_score(hsv, badge) < T.BADGE_MIN):
        return {}
    out = {"score": [], "clock": [], "quarter": []}
    for side in ("away", "home"):
        gs = T.score_glyphs(hsv, side)
        if 1 <= len(gs) <= 2 and all(g is not None for g in gs):
            out["score"] += gs
    cg = T.clock_glyphs(hsv)
    if 3 <= len(cg) <= 4 and all(g is not None for g in cg):
        out["clock"] += cg
    q = T.quarter_glyph(hsv)
    if q is not None and T.quarter_suffix(hsv):
        out["quarter"].append(skeleton(q))
    return out


def cluster(vecs, thr=0.92):
    reps, members = [], []
    for i, v in enumerate(vecs):
        s = [float(r @ v) for r in reps]
        j = int(np.argmax(s)) if s else -1
        if j >= 0 and s[j] >= thr:
            members[j].append(i)
        else:
            reps.append(v)
            members.append([i])
    order = sorted(range(len(members)), key=lambda k: -len(members[k]))
    return [members[k] for k in order]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("frames", nargs="+")
    ap.add_argument("--montage")
    ap.add_argument("--labels")
    ap.add_argument("--out")
    ap.add_argument("--source", default="unknown")
    ap.add_argument("--badge-from", type=int, default=0, help="build the shield template from the first N frames' median")
    a = ap.parse_args()
    files = sorted(f for p in a.frames for f in glob.glob(p))
    imgs = [(f, cv2.imread(f)) for f in files]
    vs = [hsv_of(im)[..., 2][T.BADGE].astype(np.float32) for _, im in imgs if im is not None]
    badge = T._nv(np.median(vs, axis=0))
    # keep only frames that match the median shield (bar up)
    G = {"score": [], "clock": [], "quarter": []}
    for f, im in imgs:
        if im is None:
            continue
        for g, lst in glyphs(im, badge).items():
            G[g] += [(f, x) for x in lst]
    labels = json.load(open(a.labels)) if a.labels else {}
    pack = {"badge": badge}
    src_k, src_v = [], []
    for g, items in G.items():
        vecs = [norm_vec(x, SIZES[g]) for _, x in items]
        keep = [i for i, v in enumerate(vecs) if v is not None]
        cl = cluster([vecs[i] for i in keep])
        cl = [[keep[i] for i in c] for c in cl]
        if a.montage:
            os.makedirs(a.montage, exist_ok=True)
            tiles = []
            for k, c in enumerate(cl):
                row = [cv2.resize((np.asarray(items[i][1], np.float32) * 255).clip(0, 255).astype(np.uint8)
                                  if np.asarray(items[i][1]).max() <= 1.0 else np.asarray(items[i][1], np.uint8),
                                  (24, 40), interpolation=cv2.INTER_NEAREST) for i in c[:8]]
                row += [np.zeros((40, 24), np.uint8)] * (8 - len(row))
                r = np.hstack(row)
                r = np.hstack([np.full((40, 60), 40, np.uint8), r])
                cv2.putText(r, f"{k}:{len(c)}", (2, 25), 0, 0.45, 255, 1)
                tiles.append(r)
            if tiles:
                cv2.imwrite(os.path.join(a.montage, f"{g}.png"), cv2.resize(np.vstack(tiles), None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST))
            print(g, "clusters", [len(c) for c in cl])
        if a.out:
            lab = labels.get(g, {})
            L, V = [], []
            for k, c in enumerate(cl):
                d = lab.get(str(k))
                if d in (None, "x"):
                    continue
                for i in c:
                    L.append(d)
                    V.append(vecs[i])
            for d in sorted(set(L)):
                src_k.append(f"{g}:{d}")
                src_v.append(f"real:{a.source}:{L.count(d)}")
            pack[f"{g}_labels"] = np.array(L, dtype="<U2")
            pack[f"{g}_vecs"] = np.array(V, np.float32).reshape(len(V), -1)
    if a.out:
        np.savez_compressed(a.out, source_keys=np.array(src_k), source_vals=np.array(src_v), **pack)
        print("wrote", a.out, dict(zip(src_k, src_v)))


if __name__ == "__main__":
    main()
