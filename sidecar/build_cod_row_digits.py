#!/usr/bin/env python3
"""Build the CoD end-scoreboard digit bank (packs/cod_row_digits_v2.json) from private end boards.

Invocation from the repo root (private frames stay on the build box; they are not committed):

    python3 sidecar/build_cod_row_digits.py SPEC.json [--write] [--fixtures]

SPEC.json lists end boards: {"boards": {"<match>": {"path": ..., "rows": [...], "cols": {"L": [[name, x], ...],
"R": [...]}, "owner": ["L", row], "L": ["v v v", ...], "R": [...], "card": [[name, x0, x1, y0, y1, value], ...]}}}.
Values are transcribed by eye from the full-resolution frame. Every cell is cut with the reader's own
segmentation (_row_glyph_pieces); a cell whose glyph count is not the length of its value is skipped
and reported, never forced. Each kept glyph becomes one sample with its provenance (match, side, row,
column, value, position, ink class). Identical samples are kept once.

Classes: "plain" (white row ink), "owner" (Qortrola30's highlighted row), "card" (personal card,
larger font). A digit/class with no real sample gets a "derived" sample: a real plain-row sample of
that digit resampled to the class's measured glyph height (same game font, other size). Derived
samples say so in their provenance. A digit with no real sample in any class stops the build.

Without --write it only prints the leave-one-board-out report (train on the other boards, read every
cell of the held-out board at the shipped gates). --fixtures writes digit-only cell crops and their
values to fixtures/cod/rowdigits/ (no gamertags: the crop is the number cell only).
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

import qorcap_cod_hud as H

HERE = Path(__file__).resolve().parent
FIXTURE_ROWS = HERE / "fixtures" / "cod" / "rowdigits"


def _rows01(m) -> list[str]:
    return ["".join("1" if int(v) else "0" for v in row) for row in m]


def _hex(g) -> list[str]:
    return ["".join(f"{int(v):02x}" for v in row) for row in g]


def cells(key: str, b: dict):
    rows = b["rows"]
    for side in ("L", "R"):
        for r, line in enumerate(b.get(side, [])):
            for (name, x), val in zip(b["cols"][side], line.split()):
                cls = "owner" if [side, r] == list(b["owner"]) else "plain"
                yield {"board": key, "side": side, "row": r, "col": name, "val": val, "cls": cls,
                       "box": (x - 6, x + 50, rows[r] - 4, rows[r] + 14)}
    for name, x0, x1, y0, y1, val in b.get("card", []):
        yield {"board": key, "side": "card", "row": 0, "col": name, "val": val, "cls": "card", "box": (x0, x1, y0, y1)}


def harvest(spec: dict):
    import cv2
    samples, skipped = [], []
    for key, b in spec["boards"].items():
        img = cv2.imread(b["path"])
        if img is None:
            raise SystemExit(f"cannot read {b['path']}")
        for c in cells(key, b):
            p = H._row_glyph_pieces(img, *c["box"], 130, 1.0)
            if p is None or len(p) != len(c["val"]):
                skipped.append(dict(c, glyphs=None if p is None else len(p)))
                continue
            for i, ((x, g, gray), ch) in enumerate(zip(p, c["val"])):
                samples.append(dict(c, ch=ch, mask=g, gray=gray, pos=i))
    return samples, skipped


def derive(samples):
    """Derived samples for digit/class pairs with no real sample. Never for a digit with no real sample."""
    import cv2
    have = collections.defaultdict(list)
    for s in samples:
        have[(s["ch"], s["cls"])].append(s)
    heights = {cls: sorted(s["mask"].shape[0] for s in samples if s["cls"] == cls) for cls in ("plain", "owner", "card")}
    out = []
    for d in "0123456789":
        plain = have.get((d, "plain"), [])
        if not plain:
            raise SystemExit(f"digit {d} has no real plain-row sample; refusing to ship it")
        for cls in ("owner", "card"):
            if have.get((d, cls)):
                continue
            hs = heights[cls]
            h = hs[len(hs) // 2] if hs else plain[0]["mask"].shape[0]
            for src in plain[:3]:
                m, g = src["mask"], src["gray"]
                w = max(2, round(m.shape[1] * h / m.shape[0]))
                m2 = (cv2.resize(m, (w, h), interpolation=cv2.INTER_AREA) > 127).astype("uint8") * 255
                g2 = cv2.resize(g, (w + 2, h + 2), interpolation=cv2.INTER_AREA)
                out.append(dict(src, cls=cls, mask=m2, gray=g2, derived_from=src))
    return out


def provenance(s) -> str:
    base = f"{s['board']} {s['side']}{s['row']} {s['col']}={s['val']} pos{s['pos']}"
    if "derived_from" in s:
        return f"derived {s['cls']}: plain-row sample {base} resampled to {s['cls']} glyph height"
    return f"real {s['cls']}: {base}"


def bank_vectors(samples):
    import numpy as np
    labels, vecs = [], []
    for s in samples:
        v = H._row_feature(s["mask"], s["gray"])
        if v is not None:
            labels.append(s["ch"])
            vecs.append(v)
    return np.stack(vecs), labels


def lobo(spec, samples):
    """Leave one board out: train on the rest, read every held-out cell at the shipped gates."""
    import cv2
    cell_stats = collections.Counter()
    digit = collections.defaultdict(collections.Counter)
    false_reads, no_reads = [], []
    for key, b in spec["boards"].items():
        train = [s for s in samples if s["board"] != key]
        bank = bank_vectors(train)
        img = cv2.imread(b["path"])
        for c in cells(key, b):
            r = H._row_digit_string(img, *c["box"], 130, 1.0, bank=bank)
            st = "ok" if r == c["val"] else ("no_read" if r is None else "FALSE")
            cell_stats[(c["cls"], st)] += 1
            if st == "FALSE":
                false_reads.append((key, c["side"], c["row"], c["col"], c["val"], r))
            elif st == "no_read":
                no_reads.append((key, c["side"], c["row"], c["col"], c["val"]))
        for s in samples:
            if s["board"] != key or "derived_from" in s:
                continue
            got, _t, _s = H._row_classify(*bank, s["mask"], s["gray"])
            digit[(s["ch"], s["cls"])]["ok" if got == s["ch"] else ("no_read" if got is None else "FALSE")] += 1
    return cell_stats, digit, false_reads, no_reads


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--fixtures", action="store_true")
    a = ap.parse_args(argv)
    spec = json.loads(Path(a.spec).read_text(encoding="utf-8"))
    real, skipped = harvest(spec)
    derived = derive(real)
    cell_stats, digit, false_reads, no_reads = lobo(spec, real)
    report = {
        "boards": list(spec["boards"]),
        "real_samples": len(real),
        "derived_samples": len(derived),
        "skipped_cells": [(s["board"], s["side"], s["row"], s["col"], s["val"], s["glyphs"]) for s in skipped],
        "lobo_cells": {f"{k[0]}:{k[1]}": v for k, v in sorted(cell_stats.items())},
        "lobo_digits": {f"{d}:{c}": dict(v) for (d, c), v in sorted(digit.items())},
        "lobo_false_reads": false_reads,
        "lobo_no_reads": no_reads,
        "real_per_digit_class": {f"{d}:{c}": n for (d, c), n in sorted(collections.Counter((s["ch"], s["cls"]) for s in real).items())},
    }
    print(json.dumps(report, indent=1))
    if a.write:
        seen, out = set(), []
        for s in real + derived:
            key = (s["ch"], s["mask"].shape, s["mask"].tobytes(), s["gray"].tobytes())
            if key in seen:
                continue
            seen.add(key)
            out.append({"label": s["ch"], "class": s["cls"], "provenance": provenance(s),
                        "mask_rows": _rows01(s["mask"] > 0), "gray_rows": _hex(s["gray"])})
        doc = {"kind": "cod_row_digits_v2",
               "note": ("Call of Duty end-scoreboard digits from private practice end boards, transcribed by eye. "
                        "Feature: ink mask + own gray patch (H._row_feature). Gates unchanged: score >= 0.75, "
                        "margin >= 0.15. A value that does not pass stays unread."),
               "boards": list(spec["boards"]), "samples": out}
        H.ROW_BANK_PATH.write_text(json.dumps(doc, separators=(",", ":")) + "\n", encoding="utf-8")
        print(f"wrote {H.ROW_BANK_PATH} ({len(out)} samples)", file=sys.stderr)
    if a.fixtures:
        import cv2
        FIXTURE_ROWS.mkdir(parents=True, exist_ok=True)
        labels = {}
        for key, b in spec["boards"].items():
            img = cv2.imread(b["path"])
            for c in cells(key, b):
                x0, x1, y0, y1 = c["box"]
                name = f"{key}_{c['side']}{c['row']}_{c['col']}.png"
                cv2.imwrite(str(FIXTURE_ROWS / name), img[y0:y1, x0:x1])
                labels[name] = {"board": key, "class": c["cls"], "value": c["val"]}
        (FIXTURE_ROWS / "labels.json").write_text(json.dumps(labels, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
