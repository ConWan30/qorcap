"""Append eye-verified real score glyphs to a Sunday/Thursday Night pack:
  python tools/add_real_score_glyphs.py {snp|tnp} PACK SOURCE FRAME:SIDE:TEXT...
e.g. snp packs/madden27_sunday_night_v1.npz match-20261017-021003 s_021221.jpg:away:0
Each glyph of that side's score becomes a template labelled with the matching character of TEXT (the glyph count
must equal len(TEXT), otherwise the frame is skipped). Provenance: real:<SOURCE>:<count> is added per digit
(a digit that was synthetic becomes 'real:...' once a real glyph exists; the synthetic templates are dropped)."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sidecar"))
import cv2  # noqa: E402
from qorcap_mnp import norm_vec  # noqa: E402


def main():
    kind, pack, source, *items = sys.argv[1:]
    if kind == "snp":
        import qorcap_snp as M
    else:
        import qorcap_tnp as M
    from qorcap_snp import hsv_of
    d = dict(np.load(pack))
    labels, vecs = list(map(str, d["score_labels"])), list(d["score_vecs"].reshape(len(d["score_labels"]), -1))
    src = {str(k): str(v) for k, v in zip(d["source_keys"], d["source_vals"])}
    added = {}
    for it in items:
        f, side, text = it.rsplit(":", 2)
        gs = M.score_glyphs(hsv_of(cv2.imread(f)), side)
        if len(gs) != len(text) or any(g is None for g in gs):
            print("skip", it, "glyphs", len(gs))
            continue
        for g, ch in zip(gs, text):
            v = norm_vec(g, M.SCORE_SIZE)
            if v is None:
                continue
            labels.append(ch)
            vecs.append(v)
            added[ch] = added.get(ch, 0) + 1
    for ch, n in added.items():
        k = f"score:{ch}"
        old = src.get(k, "")
        if old.startswith("synthetic"):
            keep = [i for i, lab in enumerate(labels) if lab != ch]
            # drop synthetic templates of this digit (those added before this run)
            n_syn = sum(1 for lab in map(str, d["score_labels"]) if lab == ch)
            idx = [i for i, lab in enumerate(labels) if lab == ch][:n_syn]
            keep = [i for i in range(len(labels)) if i not in set(idx)]
            labels, vecs = [labels[i] for i in keep], [vecs[i] for i in keep]
            src[k] = f"real:{source}:{n}"
        elif old.startswith("real"):
            src[k] = f"{old}+{source}:{n}"
        else:
            src[k] = f"real:{source}:{n}"
    d["score_labels"] = np.array(labels, dtype="<U2")
    d["score_vecs"] = np.array(vecs, np.float32)
    keys = sorted(src)
    d["source_keys"], d["source_vals"] = np.array(keys), np.array([src[k] for k in keys])
    np.savez_compressed(pack, **d)
    print("added", added, {k: src[k] for k in keys if k.startswith("score")})


if __name__ == "__main__":
    main()
