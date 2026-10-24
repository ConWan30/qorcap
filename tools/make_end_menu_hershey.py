"""Rebuild the Hershey stand-in rows in the pinned end-menu pack.

Digits 1, 4, and 7 in ``sidecar/packs/madden27_end_menu_v2.npz`` were only on
the removed 125430 rows. These stand-ins are OpenCV Hershey simplex, not those
pixels. The same function the reader uses (``norm_vec``) turns each tight
mask into the stored vector, so a rerun matches the committed rows byte for
byte.

    python tools/make_end_menu_hershey.py --check
    python tools/make_end_menu_hershey.py --write
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "sidecar" / "packs" / "madden27_end_menu_v2.npz"
SOURCE = "synthetic:hershey"
DIGITS = ("1", "4", "7")
FONT = cv2.FONT_HERSHEY_SIMPLEX
SCALE = 2.0
THICKNESS = 2
LINE = cv2.LINE_8


def hershey_mask(digit: str) -> np.ndarray:
    """Tight grayscale mask of one Hershey digit. Empty renders are a bug."""
    canvas = np.zeros((80, 80), np.uint8)
    cv2.putText(canvas, digit, (10, 60), FONT, SCALE, 255, THICKNESS, LINE)
    ys, xs = np.nonzero(canvas)
    if len(xs) == 0:
        raise SystemExit(f"hershey render of {digit!r} is empty")
    return canvas[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]


def hershey_vectors() -> dict[str, np.ndarray]:
    """One float32 ``norm_vec`` per digit, the bytes stored in the pack."""
    sys.path.insert(0, str(ROOT / "sidecar"))
    from qorcap_endmenu import norm_vec

    out: dict[str, np.ndarray] = {}
    for digit in DIGITS:
        vec = norm_vec(hershey_mask(digit))
        if vec is None:
            raise SystemExit(f"hershey vector of {digit!r} is flat")
        out[digit] = np.ascontiguousarray(vec, dtype=np.float32)
    return out


def _load(path: Path):
    with np.load(path) as blob:
        return (
            [str(x) for x in blob["labels"]],
            [str(x) for x in blob["source"]],
            np.ascontiguousarray(blob["vecs"], dtype=np.float32).copy(),
            np.ascontiguousarray(blob["ot_vec"], dtype=np.float32).copy(),
        )


def rows_match(path: Path | None = None) -> bool:
    labels, sources, vecs, _ot = _load(path or PACK)
    fresh = hershey_vectors()
    seen = set()
    for index, (label, source) in enumerate(zip(labels, sources)):
        if source != SOURCE:
            continue
        seen.add(label)
        if label not in fresh or not np.array_equal(vecs[index], fresh[label]):
            return False
    return seen == set(DIGITS)


def write_rows(path: Path | None = None) -> None:
    path = path or PACK
    labels, sources, vecs, ot = _load(path)
    fresh = hershey_vectors()
    replaced = {digit: 0 for digit in DIGITS}
    for index, (label, source) in enumerate(zip(labels, sources)):
        if source != SOURCE:
            continue
        if label not in fresh:
            raise SystemExit(f"unexpected hershey label {label!r}")
        vecs[index] = fresh[label]
        replaced[label] += 1
    missing = [digit for digit, count in replaced.items() if count != 1]
    if missing:
        raise SystemExit(f"pack is missing one hershey row for {missing}")
    np.savez_compressed(
        path,
        labels=np.array(labels),
        vecs=vecs,
        source=np.array(sources),
        ot_vec=ot,
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--check"]:
        if not rows_match():
            print("hershey rows do not match the generator", file=sys.stderr)
            return 1
        print("hershey rows match")
        return 0
    if args == ["--write"]:
        write_rows()
        if not rows_match():
            print("write did not round-trip", file=sys.stderr)
            return 1
        print(f"wrote {PACK}")
        return 0
    print("usage: make_end_menu_hershey.py --check|--write", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
