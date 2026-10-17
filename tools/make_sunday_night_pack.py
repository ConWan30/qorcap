"""Build sidecar/packs/madden27_sunday_night_v1.npz from real frames of a Sunday Night match.

    python tools/make_sunday_night_pack.py FRAMES... --labels tools/sunday_night_labels.json [--out PACK]
    python tools/make_sunday_night_pack.py FRAMES... --montage DIR      # write cluster montages for labelling

FRAMES are 16:9 stills (any size, rescaled to 1280x720) and/or videos (every frame read), e.g. the bottom strip of
the recording:  ffmpeg -i video.mp4 -vf "fps=4,crop=1280:72:0:648" -an -crf 8 strip.mp4 Glyphs are segmented exactly as sidecar/qorcap_snp.py does and greedily clustered (deterministic,
input order); clusters (ranked by size, ties by first appearance) are labelled by hand from the montages; the labels
file maps rank -> label per group. All clusters with the same label are pooled; each template is the mean of up to 300
member vectors. Score digits never shown on the score during the source match are taken from the clock glyph of the
same font family, resized; quarter digits never shown are skeletonised real clock digits (the quarter is a thin
font, compared as a skeleton). Both are marked synthetic in the pack (provenance in source_keys/source_vals).
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sidecar"))
import qorcap_snp as S  # noqa: E402

SIZES = {"score": S.SCORE_SIZE, "clock": S.CLOCK_SIZE, "quarter": S.QUARTER_SIZE}


def frames(paths):
    import cv2
    for p in paths:
        if Path(p).suffix.lower() in (".mp4", ".mkv", ".mov"):
            cap = cv2.VideoCapture(str(p))
            while True:
                ok, f = cap.read()
                if not ok:
                    break
                if f.shape[0] < 360:      # bottom strip of a 1280x720 recording (ffmpeg crop=1280:72:0:648)
                    full = np.zeros((720, 1280, 3), np.uint8)
                    full[720 - f.shape[0]:] = f
                    f = full
                yield f
        else:
            f = cv2.imread(str(p))
            if f is not None:
                yield f


def glyph_sets(f):
    hsv = S.hsv_of(f)
    if hsv is None or not S.is_present(hsv):
        return {}
    sc = [g for s in ("away", "home") for g in S.score_glyphs(hsv, s)]
    ck = S.clock_glyphs(hsv)
    q = S.quarter_glyph(hsv)
    q = S.skeleton(q) if q is not None and S.quarter_suffix(hsv) else None
    return {"score": [g for g in sc if g is not None] if 1 <= len(sc) <= 4 else [],
            "clock": [g for g in ck if g is not None] if 3 <= len(ck) <= 4 else [],
            "quarter": [q] if q is not None else []}


def cluster(paths, thr={"score": 0.92, "clock": 0.92, "quarter": 0.92}):
    cl = {g: ([], [], []) for g in SIZES}   # centroids, members, example masks
    for f in frames(paths):
        for g, masks in glyph_sets(f).items():
            cents, mem, ex = cl[g]
            for m in masks:
                v = S.norm_vec(m, SIZES[g])
                if v is None:
                    continue
                if cents:
                    s = np.array(cents) @ v
                    j = int(s.argmax())
                    if s[j] > thr[g]:
                        mem[j].append(v)
                        if len(ex[j]) < 50:
                            ex[j].append(m)
                        continue
                cents.append(v)
                mem.append([v])
                ex.append([m])
    out = {}
    for g, (_, mem, ex) in cl.items():
        order = sorted(range(len(mem)), key=lambda i: (-len(mem[i]), i))
        out[g] = [(mem[i], ex[i]) for i in order]
    return out


def montage(cl, d):
    import cv2
    Path(d).mkdir(parents=True, exist_ok=True)
    for g, items in cl.items():
        tiles = []
        for r, (mem, ex) in enumerate(items):
            t = np.zeros((70, 60), np.uint8)
            ex = ex[0]
            e = cv2.resize(ex * 255, (ex.shape[1] * 2, ex.shape[0] * 2), interpolation=cv2.INTER_NEAREST)[:48, :56]
            t[2:2 + e.shape[0], 2:2 + e.shape[1]] = e
            cv2.putText(t, f"{r}:{len(mem)}", (1, 66), 0, 0.35, 255, 1)
            tiles.append(t)
        while len(tiles) % 12:
            tiles.append(np.zeros((70, 60), np.uint8))
        cv2.imwrite(str(Path(d) / f"{g}.png"), np.vstack([np.hstack(tiles[i:i + 12]) for i in range(0, len(tiles), 12)]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("frames", nargs="+")
    ap.add_argument("--labels")
    ap.add_argument("--montage")
    ap.add_argument("--out", default=str(S.PACK))
    ap.add_argument("--source", default="match-20261017-002326")
    a = ap.parse_args(argv)
    paths = sorted(p for pat in a.frames for p in (glob.glob(pat) or [pat]))
    cl = cluster(paths)
    if a.montage:
        montage(cl, a.montage)
        print({g: [len(m) for m, _ in items] for g, items in cl.items()})
        return 0
    labels = json.loads(Path(a.labels).read_text())
    out, src = {}, {}
    for g in SIZES:
        pool: dict[str, list] = {}
        for rank, lab in labels[g].items():
            if lab in ("", "?", "x"):
                continue
            pool.setdefault(lab, []).extend(cl[g][int(rank)][0])
        labs, vecs = [], []
        for lab, mem in pool.items():
            v = np.mean(mem[:300], axis=0)
            labs.append(lab)
            vecs.append(v / np.linalg.norm(v))
            src[f"{g}:{lab}"] = f"real:{a.source}:n={len(mem)}"
        out[g] = (labs, vecs)
    # quarter digits never shown (the source match ended in the 1st quarter): skeletonised real clock digits
    for dgt in "1234":
        if dgt not in out["quarter"][0]:
            masks = [m for rank, lab in labels["clock"].items() if lab == dgt for m in cl["clock"][int(rank)][1]]
            if masks:
                vs = [S.norm_vec(S.skeleton(m), S.QUARTER_SIZE) for m in masks]
                v = np.mean([x for x in vs if x is not None], axis=0)
                out["quarter"][0].append(dgt)
                out["quarter"][1].append(v / np.linalg.norm(v))
                src[f"quarter:{dgt}"] = "synthetic:clock_font_skeleton"
    import cv2
    clabs, cvecs = out["clock"]
    for dgt in "0123456789":
        if dgt not in out["score"][0] and dgt in clabs:
            cv = cvecs[clabs.index(dgt)].reshape(S.CLOCK_SIZE[1], S.CLOCK_SIZE[0])
            sv = cv2.resize(cv, S.SCORE_SIZE, interpolation=cv2.INTER_CUBIC).ravel()
            sv = sv - sv.mean()
            out["score"][0].append(dgt)
            out["score"][1].append(sv / np.linalg.norm(sv))
            src[f"score:{dgt}"] = "synthetic:clock_font_resized"
    np.savez_compressed(a.out, **{f"{g}_labels": np.array(out[g][0]) for g in SIZES},
                        **{f"{g}_vecs": np.array(out[g][1], np.float32) for g in SIZES},
                        source_keys=np.array(list(src)), source_vals=np.array(list(src.values())))
    print({g: sorted(set(out[g][0])) for g in SIZES}, {k: v for k, v in src.items() if v.startswith("synthetic")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
