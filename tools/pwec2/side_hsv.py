#!/usr/bin/env python3
"""PWEC-2 local side cue. $0. No network. Info only. jev is false.

Reads one 1280x720 frame that has already had the kill-feed masked and the
name bands painted as flat fills. The fill colour is the side signal, read
together with the minimap dots. A hue outside the frozen bands is unsure.

OpenCV HSV: H 0-179, S and V 0-255. Boxes are half-open [x0, y0, x1, y1)
on a 1280x720 frame, origin top-left.

Frozen on the 40 calibration frames only (manifest set "calibration").
The Step-0 working bands are the start. Two flat-core medians on those
frames sat outside, so those two floors moved by the observed extreme plus
a 4-count margin and then stopped:

- enemy S min 80 -> 74, because a flat core median was S 78
- teammate H max 105 -> 110, because a flat core median was H 106

Every other floor stayed on the Step-0 band. Flat cal_* cores sat inside
it. Self-yellow chevron medians on those frames sat inside H 20-35,
S >= 80, V >= 120. That band overlaps XP gold (H 16-35, S >= 70, V >= 140)
at H 20-35, S >= 80, V >= 140. The overlap is kept: measured chevron
medians (the fixture is H 23, S 198, V 212) also meet the gold floors, so
excluding gold from yellow would reject the self chevron. Gold XP plates
are a centre-view cue and are not used to call a side from the disc.
Grey fills are RGB 40,40,40. Window frames were not used.

The map is read. It does not call a side by itself, and it does not override
a plate. A missing self chevron means map cues are unavailable (not a second
layout). Cone angle and r_max were not fit.

Vocabulary: MEASURED, ESTIMATED, UNMEASURED, UNKNOWN.
"""
from __future__ import annotations

import argparse
import json
import sys

import cv2
import numpy as np

FRAME_W = 1280
FRAME_H = 720

# MEASURED boxes from the Step-0 HUD pin. Half-open.
CENTRE_CROP = (320, 40, 960, 680)
MINIMAP_TILE = (12, 10, 184, 176)
MINIMAP_DISC = (19, 18, 175, 167)
KILLFEED_MASK = (0, 290, 430, 540)


def crop_local(box, crop=CENTRE_CROP):
    """Intersect a full-frame box with the centre crop; return crop-local coords."""
    bx0, by0, bx1, by1 = box
    cx0, cy0, cx1, cy1 = crop
    x0, y0 = max(bx0, cx0), max(by0, cy0)
    x1, y1 = min(bx1, cx1), min(by1, cy1)
    if x0 >= x1 or y0 >= y1:
        raise ValueError("box does not intersect the centre crop")
    return (x0 - cx0, y0 - cy0, x1 - cx0, y1 - cy0)


# Crop-local residual of KILLFEED_MASK: [0, 250, 110, 500).
CROP_FEED_MASK = crop_local(KILLFEED_MASK)
# Weapon / ammo intersects the centre crop at crop-local [600, 560, 640, 640).
CROP_WEAPON = (600, 560, 640, 640)
# Compass can leak into the first rows of the centre crop.
CROP_COMPASS_LEAK = (0, 0, 640, 4)

# Frozen HSV. See the module note for which floors moved.
RED_H = ((0, 15), (165, 179))
RED_S_MIN = 74
RED_V_MIN = 90
CYAN_H = (75, 110)
CYAN_S_MIN = 60
CYAN_V_MIN = 90
YELLOW_H = (20, 35)
YELLOW_S_MIN = 80
YELLOW_V_MIN = 120
GOLD_H = (16, 35)
GOLD_S_MIN = 70
GOLD_V_MIN = 140

GREY_RGB = (40, 40, 40)
GREY_TOL = 12
OOB_S_MIN = 60
OOB_V_MIN = 90

# Flat plate geometry. A side plate is an exact solid rectangle, which is
# what the fill paints. Interior pixels of a constant rectangle match their
# neighbours; the outer edge does not, so the component is the inset.
FLAT_TOL = 0
SPATIAL_STD_MAX = 1.0
PLATE_AREA = (140, 26000)
PLATE_W = (24, 260)
PLATE_H = (6, 160)
PLATE_FILL_MIN = 0.70

DOT_AREA = (12, 3000)
SELF_MIN_AREA = 20
SELF_MAX_DIST_PX = 15.0
N_REQ = 1
JPEG_MARGIN = 4

FROZEN = {
    "red_h": RED_H,
    "red_s_min": RED_S_MIN,
    "red_v_min": RED_V_MIN,
    "cyan_h": CYAN_H,
    "cyan_s_min": CYAN_S_MIN,
    "cyan_v_min": CYAN_V_MIN,
    "yellow_h": YELLOW_H,
    "yellow_s_min": YELLOW_S_MIN,
    "yellow_v_min": YELLOW_V_MIN,
    "gold_h": GOLD_H,
    "gold_s_min": GOLD_S_MIN,
    "gold_v_min": GOLD_V_MIN,
    "grey_rgb": GREY_RGB,
    "grey_tol": GREY_TOL,
    "oob_s_min": OOB_S_MIN,
    "oob_v_min": OOB_V_MIN,
    "flat_tol": FLAT_TOL,
    "spatial_std_max": SPATIAL_STD_MAX,
    "plate_area": PLATE_AREA,
    "plate_w": PLATE_W,
    "plate_h": PLATE_H,
    "plate_fill_min": PLATE_FILL_MIN,
    "dot_area": DOT_AREA,
    "self_min_area": SELF_MIN_AREA,
    "self_max_dist_px": SELF_MAX_DIST_PX,
    "n_req": N_REQ,
    "jpeg_margin": JPEG_MARGIN,
    "centre_crop": CENTRE_CROP,
    "minimap_tile": MINIMAP_TILE,
    "minimap_disc": MINIMAP_DISC,
}


def _hue_in(h: np.ndarray, lo: int, hi: int) -> np.ndarray:
    return (h >= lo) & (h <= hi)


def _band_red(hsv: np.ndarray) -> np.ndarray:
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    hue = _hue_in(h, 0, 15) | _hue_in(h, 165, 179)
    return hue & (s >= RED_S_MIN) & (v >= RED_V_MIN)


def _band_cyan(hsv: np.ndarray) -> np.ndarray:
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    return _hue_in(h, CYAN_H[0], CYAN_H[1]) & (s >= CYAN_S_MIN) & (v >= CYAN_V_MIN)


def _band_yellow(hsv: np.ndarray) -> np.ndarray:
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    return _hue_in(h, YELLOW_H[0], YELLOW_H[1]) & (s >= YELLOW_S_MIN) & (v >= YELLOW_V_MIN)


def _band_gold(hsv: np.ndarray) -> np.ndarray:
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    return _hue_in(h, GOLD_H[0], GOLD_H[1]) & (s >= GOLD_S_MIN) & (v >= GOLD_V_MIN)


def _require_frame(bgr: np.ndarray) -> None:
    if not isinstance(bgr, np.ndarray) or bgr.ndim != 3 or bgr.shape != (FRAME_H, FRAME_W, 3):
        raise ValueError(f"expected uint8 BGR {FRAME_W}x{FRAME_H}, got {getattr(bgr, 'shape', None)}")
    if bgr.dtype != np.uint8:
        raise ValueError("expected uint8 BGR")


def _zero(img: np.ndarray, box) -> None:
    x0, y0, x1, y1 = box
    img[y0:y1, x0:x1] = 0


def _spatial_std(pix: np.ndarray) -> float:
    if pix.size == 0:
        return 999.0
    return float(np.max(np.std(pix.astype(np.float32), axis=0)))


def _classify_colour(bgr_med: np.ndarray, hsv_med: np.ndarray) -> str:
    """One flat colour -> red, cyan, gold, grey, oob, or other."""
    b = int(bgr_med[0])
    g = int(bgr_med[1])
    r = int(bgr_med[2])
    if max(abs(r - 40), abs(g - 40), abs(b - 40)) <= GREY_TOL and (max(r, g, b) - min(r, g, b)) <= 8:
        return "grey"
    h, s, v = int(hsv_med[0]), int(hsv_med[1]), int(hsv_med[2])
    red_hue = (0 <= h <= 15) or (165 <= h <= 179)
    cyan_hue = CYAN_H[0] <= h <= CYAN_H[1]
    gold_hue = GOLD_H[0] <= h <= GOLD_H[1]
    if red_hue and s >= RED_S_MIN and v >= RED_V_MIN:
        return "red"
    if cyan_hue and s >= CYAN_S_MIN and v >= CYAN_V_MIN:
        return "cyan"
    if gold_hue and s >= GOLD_S_MIN and v >= GOLD_V_MIN:
        return "gold"
    # A team or XP hue below its floor is not a cue, and it is not an out-of-band hue.
    if red_hue or cyan_hue or gold_hue:
        return "other"
    if s >= OOB_S_MIN and v >= OOB_V_MIN:
        return "oob"
    return "other"


def _neighbour(a: np.ndarray, dy: int, dx: int) -> np.ndarray:
    """Copy of `a` shifted by (dy, dx). Cells with no source stay a sentinel."""
    out = np.full_like(a, 9999)
    y_dst = slice(max(dy, 0), a.shape[0] + min(dy, 0))
    x_dst = slice(max(dx, 0), a.shape[1] + min(dx, 0))
    y_src = slice(max(-dy, 0), a.shape[0] - max(dy, 0))
    x_src = slice(max(-dx, 0), a.shape[1] - max(dx, 0))
    out[y_dst, x_dst] = a[y_src, x_src]
    return out


def _flat_plates(bgr: np.ndarray) -> list[dict]:
    """Solid rectangles.

    A pixel counts only when all four neighbours are the same colour. That
    leaves a one-pixel gap around a flat fill, so the fill does not merge
    with a flat background of a different colour.
    """
    a = bgr.astype(np.int16)
    same = np.ones(a.shape[:2], dtype=bool)
    for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
        same &= np.max(np.abs(a - _neighbour(a, dy, dx)), axis=2) <= FLAT_TOL
    n, labels, stats, _cent = cv2.connectedComponentsWithStats(same.astype(np.uint8), connectivity=8)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    found = []
    amin, amax = PLATE_AREA
    wmin, wmax = PLATE_W
    hmin, hmax = PLATE_H
    for i in range(1, n):
        area = int(stats[i, cv2.CC_STAT_AREA])
        w = int(stats[i, cv2.CC_STAT_WIDTH])
        h = int(stats[i, cv2.CC_STAT_HEIGHT])
        if area < amin or area > amax or w < wmin or w > wmax or h < hmin or h > hmax:
            continue
        if area / float(w * h) < PLATE_FILL_MIN:
            continue
        ys, xs = np.where(labels == i)
        pix = bgr[ys, xs]
        if _spatial_std(pix) > SPATIAL_STD_MAX:
            continue
        bgr_med = np.median(pix, axis=0)
        hsv_med = np.median(hsv[ys, xs], axis=0)
        kind = _classify_colour(bgr_med, hsv_med)
        found.append({
            "kind": kind,
            "area": area,
            "w": w,
            "h": h,
            "x": int(stats[i, cv2.CC_STAT_LEFT]),
            "y": int(stats[i, cv2.CC_STAT_TOP]),
        })
    return found


def _dot_present(mask: np.ndarray) -> bool:
    n, _labels, stats, _cent = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    lo, hi = DOT_AREA
    for i in range(1, n):
        area = int(stats[i, cv2.CC_STAT_AREA])
        if lo <= area <= hi:
            return True
    return False


def _self_chevron(yellow: np.ndarray) -> bool:
    n, _labels, stats, cent = cv2.connectedComponentsWithStats(yellow.astype(np.uint8), connectivity=8)
    height, width = yellow.shape[:2]
    cx, cy = width / 2.0, height / 2.0
    for i in range(1, n):
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area < SELF_MIN_AREA:
            continue
        dx = float(cent[i][0]) - cx
        dy = float(cent[i][1]) - cy
        if (dx * dx + dy * dy) ** 0.5 <= SELF_MAX_DIST_PX:
            return True
    return False


def _map_cues(bgr: np.ndarray) -> dict:
    x0, y0, x1, y1 = MINIMAP_DISC
    disc = bgr[y0:y1, x0:x1]
    hsv = cv2.cvtColor(disc, cv2.COLOR_BGR2HSV)
    self_yellow = _self_chevron(_band_yellow(hsv))
    if not self_yellow:
        return {"map_available": False, "self_yellow": False, "m_red": False, "m_cyan": False}
    return {
        "map_available": True,
        "self_yellow": True,
        "m_red": _dot_present(_band_red(hsv)),
        "m_cyan": _dot_present(_band_cyan(hsv)),
    }


def classify_frame(bgr: np.ndarray) -> dict:
    """Frame cues. side is enemy, teammate, or unsure. Map flags do not set side."""
    _require_frame(bgr)
    x0, y0, x1, y1 = CENTRE_CROP
    crop = bgr[y0:y1, x0:x1].copy()
    _zero(crop, CROP_FEED_MASK)
    _zero(crop, CROP_WEAPON)
    _zero(crop, CROP_COMPASS_LEAK)
    plates = _flat_plates(crop)
    counts = {"red": 0, "cyan": 0, "gold": 0, "grey": 0, "oob": 0, "other": 0}
    for plate in plates:
        counts[plate["kind"]] = counts.get(plate["kind"], 0) + 1
    c_red = counts["red"] > 0
    c_cyan = counts["cyan"] > 0
    out_of_band = counts["oob"] > 0
    if out_of_band or (c_red and c_cyan):
        side = "unsure"
    elif c_red:
        side = "enemy"
    elif c_cyan:
        side = "teammate"
    else:
        side = "unsure"
    cues = _map_cues(bgr)
    return {
        "side": side,
        "c_red": c_red,
        "c_cyan": c_cyan,
        "out_of_band": out_of_band,
        "n_red_plates": counts["red"],
        "n_cyan_plates": counts["cyan"],
        "n_gold_plates": counts["gold"],
        "n_grey_plates": counts["grey"],
        "n_oob_plates": counts["oob"],
        **cues,
    }


def classify_window(frames: list[np.ndarray]) -> dict:
    """Window side. n_req is 1. Map cues are reported and do not call the side.

    unsure when a frame has an out-of-band plate, when red and cyan plates
    both appear (same frame or across the window), or when no plate fires.
    """
    infos = [classify_frame(frame) for frame in frames]
    reds = sum(1 for info in infos if info["c_red"] and not info["c_cyan"] and not info["out_of_band"])
    cyans = sum(1 for info in infos if info["c_cyan"] and not info["c_red"] and not info["out_of_band"])
    if any(info["out_of_band"] for info in infos):
        side, reason = "unsure", "out_of_band"
    elif any(info["c_red"] and info["c_cyan"] for info in infos) or (reds and cyans):
        side, reason = "unsure", "conflict"
    elif reds >= N_REQ and cyans == 0:
        side, reason = "enemy", "plate_red"
    elif cyans >= N_REQ and reds == 0:
        side, reason = "teammate", "plate_cyan"
    else:
        side, reason = "unsure", "no_plate"
    agrees = False
    if side == "enemy" and any(info["m_red"] for info in infos):
        agrees = True
    if side == "teammate" and any(info["m_cyan"] for info in infos):
        agrees = True
    return {
        "side": side,
        "reason": reason,
        "n_req": N_REQ,
        "n_frames": len(infos),
        "map_calls_side": False,
        "map_agrees": agrees,
        "frames": infos,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local side cue for one 1280x720 frame. No network.")
    parser.add_argument("frame", help="Path to one 1280x720 image. Calibration frames only, if real.")
    args = parser.parse_args(argv)
    bgr = cv2.imread(args.frame, cv2.IMREAD_COLOR)
    if bgr is None:
        print("could not read the frame", file=sys.stderr)
        return 2
    try:
        cue = classify_frame(bgr)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    json.dump({k: cue[k] for k in cue if k != "frames"}, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
