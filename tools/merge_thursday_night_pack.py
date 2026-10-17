"""Merge an incremental Thursday Night pack (built from newer real frames with its own labels) into the base pack:
concatenates the templates, sums the per-digit real-glyph counts, keeps the base shield template."""
import sys
import numpy as np


def main(base, add, out):
    a, b = np.load(base), np.load(add)
    pack = {"badge": a["badge"]}
    for g in ("score", "clock", "quarter"):
        pack[f"{g}_labels"] = np.concatenate([a[f"{g}_labels"], b[f"{g}_labels"]]).astype("<U2")
        pack[f"{g}_vecs"] = np.concatenate([a[f"{g}_vecs"].reshape(len(a[f"{g}_labels"]), -1),
                                            b[f"{g}_vecs"].reshape(len(b[f"{g}_labels"]), -1)]).astype(np.float32)
    src = {}
    for d in (a, b):
        for k, v in zip(d["source_keys"], d["source_vals"]):
            pre, n = str(v).rsplit(":", 1)
            src[str(k)] = (pre, src.get(str(k), (pre, 0))[1] + int(n))
    keys = sorted(src)
    np.savez_compressed(out, source_keys=np.array(keys), source_vals=np.array([f"{src[k][0]}:{src[k][1]}" for k in keys]), **pack)
    print("wrote", out, {k: src[k][1] for k in keys})


if __name__ == "__main__":
    main(*sys.argv[1:4])
