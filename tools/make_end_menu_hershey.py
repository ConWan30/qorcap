"""Rebuild the Hershey stand-in rows in the pinned end-menu pack.

Digits 1, 4, and 7 in ``sidecar/packs/madden27_end_menu_v2.npz`` were only on
the removed 125430 rows. These stand-ins are OpenCV Hershey simplex, not those
pixels. ``norm_vec`` (the reader's resizer) turns each tight mask into the
stored vector.

``cv2.putText`` and ``cv2.resize(..., INTER_AREA)`` are not bit-stable across
CPU dispatches of one OpenCV wheel, so a live redraw is not the oracle.
``tools/end_menu_hershey_rows.json`` holds the little-endian float32 vectors
``--write`` produced. It is not an ``.npz``, so the provenance scan does not
treat it as a template. ``--check`` compares the pack to that snapshot.

    python tools/make_end_menu_hershey.py --check
    python tools/make_end_menu_hershey.py --write
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "sidecar" / "packs" / "madden27_end_menu_v2.npz"
SNAPSHOT = ROOT / "tools" / "end_menu_hershey_rows.json"
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
    """One float32 ``norm_vec`` per digit, from a live redraw on this machine."""
    sys.path.insert(0, str(ROOT / "sidecar"))
    from qorcap_endmenu import norm_vec

    out: dict[str, np.ndarray] = {}
    for digit in DIGITS:
        vec = norm_vec(hershey_mask(digit))
        if vec is None:
            raise SystemExit(f"hershey vector of {digit!r} is flat")
        out[digit] = np.ascontiguousarray(vec, dtype=np.float32)
    return out


def snapshot_vectors(path: Path | None = None) -> dict[str, np.ndarray]:
    """The committed stand-in vectors. These bytes are the check oracle."""
    path = path or SNAPSHOT
    if not path.is_file():
        raise SystemExit(f"missing hershey snapshot {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    digits = data.get("digits")
    if not isinstance(digits, dict):
        raise SystemExit(f"hershey snapshot {path} has no digits object")
    out: dict[str, np.ndarray] = {}
    for digit in DIGITS:
        raw = digits.get(digit)
        if not isinstance(raw, str):
            raise SystemExit(f"hershey snapshot is missing {digit!r}")
        vec = np.frombuffer(bytes.fromhex(raw), dtype="<f4").copy()
        if vec.shape != (12 * 18,):
            raise SystemExit(f"hershey snapshot row {digit!r} has length {vec.shape[0]}")
        out[digit] = vec
    return out


def write_snapshot(vectors: dict[str, np.ndarray], path: Path | None = None) -> None:
    path = path or SNAPSHOT
    payload = {
        "dtype": "float32-le",
        "length": 12 * 18,
        "digits": {
            digit: np.ascontiguousarray(vectors[digit], dtype="<f4").tobytes().hex()
            for digit in DIGITS
        },
    }
    path.write_bytes((json.dumps(payload, indent=2) + "\n").encode("utf-8"))


def _load(path: Path):
    with np.load(path) as blob:
        return (
            [str(x) for x in blob["labels"]],
            [str(x) for x in blob["source"]],
            np.ascontiguousarray(blob["vecs"], dtype=np.float32).copy(),
            np.ascontiguousarray(blob["ot_vec"], dtype=np.float32).copy(),
        )


def hershey_mismatch(path: Path | None = None) -> str | None:
    """None when the pack's stand-in rows equal the committed snapshot."""
    labels, sources, vecs, _ot = _load(path or PACK)
    fresh = snapshot_vectors()
    seen: dict[str, int] = {}
    for index, (label, source) in enumerate(zip(labels, sources)):
        if source != SOURCE:
            continue
        seen[label] = seen.get(label, 0) + 1
        if label not in fresh:
            return f"unexpected hershey label {label!r}"
        if not np.array_equal(vecs[index], fresh[label]):
            delta = float(np.max(np.abs(vecs[index] - fresh[label])))
            return f"hershey row {label!r} differs from the snapshot (max abs {delta})"
    if seen != {digit: 1 for digit in DIGITS}:
        return f"hershey rows are {seen}, expected one each of {DIGITS}"
    return None


def rows_match(path: Path | None = None) -> bool:
    return hershey_mismatch(path) is None


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
    write_snapshot(fresh)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--check"]:
        mismatch = hershey_mismatch()
        if mismatch:
            print(mismatch, file=sys.stderr)
            return 1
        print("hershey rows match the snapshot")
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
