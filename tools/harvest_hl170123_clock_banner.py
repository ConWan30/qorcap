"""Harvest standard-bar clock glyphs and FIELD GOAL / TOUCHDOWN fragments from public highlights.

Re-runnable. Does not load the clock npz or the banner npz, so it does not train on its own output.
Clock labels come from the existing pack's own argmax, and only when that match is already at
least 0.85 with a margin of 0.02. ``labels.json`` is not the digit key. Fragments are the letter
chain on one locked banner frame each: FIELD GOAL white ink on hl00 f175 (``GOA``), TOUCHDOWN dark
ink on hl01 f174 (``TOUC``).

    python tools/harvest_hl170123_clock_banner.py FRAMES_DIR

``FRAMES_DIR`` holds ``hl00-q3-a3-h0/f007.png`` and the other clean 1280×720 samples (frame 7 then
every 12 for hl00; frame 6 then every 12 for hl01 and hl02). Only ``fNNN.png`` is used.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sidecar"))

from qor_receipt._vendor.qoresence.vision.local_scorebug.glyphs import column_runs, norm_vec  # noqa: E402
from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import ScorebugReader  # noqa: E402
from qorcap_reader import prep  # noqa: E402
from qorcap_standard import PACK, PACK_FOUR  # noqa: E402

CLOCK_OUT = ROOT / "sidecar" / "packs" / "madden27_standard_clock_170123.npz"
BANNER_OUT = ROOT / "sidecar" / "packs" / "madden27_banner_fragments_170123.npz"
CLOCK_MIN_SCORE = 0.85
CLOCK_MIN_MARGIN = 0.02
FG_FRAME = "hl00-q3-a3-h0/f175.png"
TD_FRAME = "hl01-q4-a9-h0/f174.png"


def score_reader() -> ScorebugReader:
    """Stock clock pack plus the already-shipped score variants. No harvested clock glyphs."""
    reader = ScorebugReader()
    if PACK.is_file():
        z = np.load(PACK)
        score = reader.pack.groups["score"]
        score["2"] = np.vstack([score["2"], np.atleast_2d(z["two"].astype(np.float32))])
        score["3"] = np.vstack([score["3"], np.atleast_2d(z["three"].astype(np.float32))])
    if PACK_FOUR.is_file():
        z = np.load(PACK_FOUR)
        score = reader.pack.groups["score"]
        score["4"] = np.vstack([score["4"], np.atleast_2d(z["four"].astype(np.float32))])
    return reader


def _chain(mask: np.ndarray):
    n, _lab, stats, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), connectivity=8)
    boxes = []
    for i in range(1, n):
        x, y, w, h, area = (int(t) for t in stats[i])
        if 5 <= h <= 11 and 2 <= w <= 18 and area >= 12:
            boxes.append((x, y, w, h))
    boxes.sort()
    if not boxes:
        return None
    best: list = []
    cur = [boxes[0]]
    for b in boxes[1:]:
        gap = b[0] - (cur[-1][0] + cur[-1][2])
        if gap <= 6:
            cur.append(b)
        else:
            if len(cur) > len(best):
                best = cur
            cur = [b]
    if len(cur) > len(best):
        best = cur
    return best if len(best) >= 4 else None


def _fragment(mask: np.ndarray, start: int, stop: int) -> np.ndarray:
    boxes = _chain(mask)
    if boxes is None or len(boxes) < stop:
        raise SystemExit(f"letter chain too short for [{start}:{stop}]")
    sel = boxes[start:stop]
    x0 = sel[0][0]
    y0 = min(b[1] for b in sel)
    x1 = sel[-1][0] + sel[-1][2]
    y1 = max(b[1] + b[3] for b in sel)
    return ((mask[y0:y1, x0:x1] > 0).astype(np.uint8)) * 255


def harvest_clock(reader: ScorebugReader, frames: Path) -> dict[str, np.ndarray]:
    buckets: dict[str, list] = defaultdict(list)
    for path in sorted(frames.glob("hl*/f[0-9][0-9][0-9].png")):
        img = cv2.imread(str(path))
        if img is None:
            continue
        small = prep(img)
        band, why = reader.reference_band(small)
        if band is None:
            continue
        ncc, (dx, dy) = reader.find_anchor(band)
        if ncc < reader.profile.min_logo_ncc:
            continue
        # Segment at full resolution. A glyph the 0.93 gate refuses can still be harvested
        # when the stock pack's own argmax clears 0.85 / 0.02.
        from qorcap_clock import _box

        sx = img.shape[1] / float(reader.profile.ref_w)
        patch, _sx = _box(img, reader.profile, reader.profile.clock_text, dx, dy)
        if patch is None:
            continue
        red = bool(reader.clock_is_red(patch))
        v = reader.clock_intensity(patch, red)
        peak = float(np.percentile(v, 98))
        if peak < reader.profile.clock_min_peak:
            continue
        mask = (v > reader.profile.clock_ink_frac * peak).astype(np.uint8)
        split = max(1, int(round(reader.profile.clock_split_w * sx)))
        runs: list[tuple[int, int]] = []
        for a, b in column_runs(mask):
            if b - a >= split:
                cols = mask[:, a:b].sum(0)
                w = b - a
                lo, hi = w // 3, 2 * w // 3 + 1
                if hi <= lo:
                    runs.append((a, b))
                    continue
                cut = a + lo + int(cols[lo:hi].argmin())
                runs += [(a, cut), (cut, b)]
            else:
                runs.append((a, b))
        if len(runs) != 4:
            continue
        lo_w = max(1, int(round(reader.profile.clock_digit_w[0] * sx)))
        hi_w = max(lo_w, int(round(reader.profile.clock_digit_w[1] * sx)))
        for a, b in (runs[0], runs[2], runs[3]):
            if not lo_w <= b - a <= hi_w:
                continue
            vec = norm_vec(v[:, a:b], reader.profile.clock_glyph_size)
            if vec is None:
                continue
            m = reader.pack.match("clock", vec)
            if m is None or m.best < CLOCK_MIN_SCORE or m.margin < CLOCK_MIN_MARGIN:
                continue
            if m.label not in reader.pack.groups["clock"]:
                continue
            buckets[m.label].append(vec)
    means = {}
    counts = {}
    for label, vecs in buckets.items():
        acc = np.mean(np.stack(vecs), axis=0).astype(np.float32)
        means[label] = acc / float(np.linalg.norm(acc))
        counts[label] = len(vecs)
    return means, counts


def harvest_banners(reader: ScorebugReader, frames: Path):
    fg = cv2.imread(str(frames / FG_FRAME))
    td = cv2.imread(str(frames / TD_FRAME))
    if fg is None or td is None:
        raise SystemExit(f"need {FG_FRAME} and {TD_FRAME} under {frames}")
    fg_band, _ = reader.reference_band(prep(fg))
    roi = fg_band[10:28, 190:430]
    white = (cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY) > 180).astype(np.uint8) * 255
    goa = _fragment(white, 4, 7)
    td_band, _ = reader.reference_band(prep(td))
    roi = td_band[8:28, 160:450]
    dark = (cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)[..., 2] < 80).astype(np.uint8) * 255
    touc = _fragment(dark, 0, 4)
    return goa, touc


def main() -> None:
    frames = Path(sys.argv[1])
    reader = score_reader()
    means, counts_map = harvest_clock(reader, frames)
    payload = {f"d{label}": vec for label, vec in sorted(means.items())}
    counts = ",".join(f"{k}:{counts_map[k]}" for k in sorted(counts_map))
    payload["source"] = np.array(f"hl170123-clean argmax>={CLOCK_MIN_SCORE}/{CLOCK_MIN_MARGIN} {counts}")
    np.savez_compressed(CLOCK_OUT, **payload)
    goa, touc = harvest_banners(reader, frames)
    np.savez_compressed(
        BANNER_OUT,
        goa=goa,
        touc=touc,
        source=np.array(f"hl170123 {FG_FRAME} GOA {TD_FRAME} TOUC"),
    )
    print("clock", counts, "->", CLOCK_OUT.name)
    print("banner", goa.shape, touc.shape, "->", BANNER_OUT.name)


if __name__ == "__main__":
    main()
