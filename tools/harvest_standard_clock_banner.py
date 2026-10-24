"""Harvest standard-bar clock glyphs and FIELD GOAL / TOUCHDOWN fragments.

Refuses held-out matches. Does not write a partial pack: every clock digit 0-9 and both banner
fragments have to come from the supplied frames, or the script stops and writes nothing.

Held out of training (evaluation only):

* 170123, 021003, 125430
* Con's labelled games in ``sidecar/eval/human_labels.jsonl``: 084523 and 193534

    python tools/harvest_standard_clock_banner.py FRAMES_DIR

``FRAMES_DIR`` holds images whose paths include the source file name and a frame index, for
example ``match-20261017-110028/f175.png``. A path that contains a held-out id is refused before
any glyph is saved. This script does not synthesize a missing digit.

The reader currently ships no output from this script. The footage in the repos does not cover
the clock digits and does not contain a non-held-out FIELD GOAL or TOUCHDOWN banner. See
``sidecar/packs/template_manifest.json``.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Match ids. A path, file name, or manifest row that contains one of these is not training data.
# Match ids, plus the public highlight clip ids. A bare hl00/f175.png path does not contain
# "170123", and that frame is still held out.
HELD_OUT = (
    "170123", "021003", "125430", "084523", "193534",
    "hl00-q3-a3-h0", "hl01-q4-a9-h0", "hl02-q4-a10-h0",
    "hl00-q1-a0-h6", "hl01-q1-a0-h7", "hl02-q2-a6-h7",
    "hl03-q2-a7-h7", "hl04-q2-a7-h13", "hl05-q2-a7-h14",
)
CLOCK_DIGITS = tuple("0123456789")


def held_out_reason(text: str) -> str | None:
    """The held-out id inside ``text``, or None."""
    folded = str(text).replace("\\", "/")
    for item in HELD_OUT:
        if item in folded:
            return item
    return None


def frame_index(path: Path) -> int | None:
    """``f175.png`` or ``frame_175.png`` -> 175. None when the name has no index."""
    stem = path.stem
    digits = ""
    if stem.startswith("f") and stem[1:].isdigit():
        digits = stem[1:]
    else:
        for token in stem.replace("-", "_").split("_"):
            if token.isdigit():
                digits = token
    return int(digits) if digits else None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("no frames directory: nothing harvested", file=sys.stderr)
        print("held-out ids:", ", ".join(HELD_OUT), file=sys.stderr)
        return 2
    root = Path(args[0])
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2
    paths = [p for p in sorted(root.rglob("*")) if p.suffix.lower() in {".png", ".jpg", ".jpeg"}]
    bad = [(p, held_out_reason(p)) for p in paths if held_out_reason(p)]
    if held_out_reason(root) or bad:
        hit = held_out_reason(root) or bad[0][1]
        print(f"refusing held-out footage ({hit}). no templates written.", file=sys.stderr)
        return 2
    # Imported only after the held-out check, so a refusal does not need the reader stack.
    from collections import defaultdict

    import cv2
    import numpy as np

    ROOT = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(ROOT / "sidecar"))
    from qor_receipt._vendor.qoresence.vision.local_scorebug.glyphs import column_runs, norm_vec
    from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import ScorebugReader
    from qorcap_clock import _box
    from qorcap_reader import prep

    reader = ScorebugReader()
    buckets: dict[str, list] = defaultdict(list)
    manifest_rows = []
    for path in paths:
        img = cv2.imread(str(path))
        if img is None:
            continue
        small = prep(img) if img.shape[0] > 400 else img
        band, _why = reader.reference_band(small)
        if band is None:
            continue
        ncc, (dx, dy) = reader.find_anchor(band)
        if ncc < reader.profile.min_logo_ncc:
            continue
        patch, _sx = _box(img if img.shape[0] > 400 else small, reader.profile, reader.profile.clock_text, dx, dy)
        if patch is None:
            continue
        v = reader.clock_intensity(patch, bool(reader.clock_is_red(patch)))
        peak = float(np.percentile(v, 98))
        if peak < reader.profile.clock_min_peak:
            continue
        mask = (v > reader.profile.clock_ink_frac * peak).astype(np.uint8)
        runs = []
        split = max(1, int(round(reader.profile.clock_split_w)))
        for a, b in column_runs(mask):
            runs.append((a, b))
        if len(runs) != 4:
            continue
        gate = reader.profile.clock_gate
        lo, hi = reader.profile.clock_digit_w
        for a, b in (runs[0], runs[2], runs[3]):
            if not lo <= b - a <= hi:
                continue
            vec = norm_vec(v[:, a:b], reader.profile.clock_glyph_size)
            if vec is None:
                continue
            m = reader.pack.match("clock", vec)
            if m is None or m.best < 0.85 or m.margin < 0.02 or m.label not in CLOCK_DIGITS:
                continue
            buckets[m.label].append(vec)
            manifest_rows.append({
                "kind": "clock",
                "label": m.label,
                "source_file": str(path),
                "frame_index": frame_index(path),
            })
    missing = [d for d in CLOCK_DIGITS if d not in buckets]
    if missing:
        print("stopping: clock digits not covered:", ",".join(missing), file=sys.stderr)
        print("no templates written. do not fall back to a held-out match or a guessed font.", file=sys.stderr)
        return 3
    print("clock digits covered", {k: len(v) for k, v in sorted(buckets.items())}, "rows", len(manifest_rows))
    # A pack also needs a FIELD GOAL fragment and a TOUCHDOWN fragment from these frames.
    # This pass does not cut banner templates, and it does not invent them.
    print(
        "stopping: no FIELD GOAL or TOUCHDOWN fragment was cut from this footage. "
        "no templates written.",
        file=sys.stderr,
    )
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
