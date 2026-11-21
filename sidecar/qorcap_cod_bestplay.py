"""Call of Duty "BEST PLAY" title read (practice auto stop only).

After a match ends Call of Duty shows, in order: the VICTORY or DEFEAT splash, the end scoreboard
(about 7 s), a short black cut, then the "Best Play of the Match" replay. A few seconds into that
replay the BEST PLAY title is drawn at the bottom right. Seeing that title means the end scoreboard
has already been on screen, so the practice auto stop can stop there.

The read is one template match of the title's bright ink (HSV value > 200) against a mask in
``packs/cod_best_play.json``, at the frame height's scale, inside a fixed search box. It reads only
the title word. It does not read the player name on that screen and does not post anything.

This is a recording-control read. It is not a final, not a score, not a receipt field, and not
an input to any check of the bundle. The threshold is PROVISIONAL; see docs/COD_BEST_PLAY_STOP.md for the tuning and
held-out counts.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PACK_PATH = Path(__file__).resolve().parent / "packs" / "cod_best_play.json"
SCHEMA = "qorcap-cod-best-play/v0"


@dataclass(frozen=True)
class BestPlayRead:
    seen: bool
    score: float | None  # None: not evaluated (no pack, no OpenCV, or a frame that is not 16:9 720p..1080p)
    reason: str


def _libs():
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None, None
    return cv2, np


@lru_cache(maxsize=1)
def _load():
    cv2, np = _libs()
    if np is None or not PACK_PATH.is_file():
        return None
    try:
        doc = json.loads(PACK_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if doc.get("schema") != SCHEMA:
        return None
    rows = doc.get("mask_rows") or []
    if not rows or any(len(r) != len(rows[0]) for r in rows):
        return None
    mask = np.array([[255 if ch == "1" else 0 for ch in r] for r in rows], np.uint8)
    box = doc.get("box_720p") or {}
    try:
        search = tuple(int(v) for v in box["search"])
        thr_v = int(box["value_thr"])
        down = float(box["downscale"])
        gate = float(doc["threshold"])
    except (KeyError, TypeError, ValueError):
        return None
    if len(search) != 4 or not (0.0 < down <= 1.0) or not (0.0 < gate < 1.0):
        return None
    return {"mask": mask, "search": search, "value_thr": thr_v, "down": down, "threshold": gate}


def threshold() -> float | None:
    pack = _load()
    return None if pack is None else pack["threshold"]


def read_best_play(img) -> BestPlayRead:
    """BEST PLAY title on this frame, or not. Never raises on a bad frame."""
    cv2, np = _libs()
    pack = _load()
    if pack is None or cv2 is None:
        return BestPlayRead(False, None, "pack_missing")
    if not isinstance(img, np.ndarray) or img.ndim != 3 or img.shape[2] < 3 or img.dtype != np.uint8:
        return BestPlayRead(False, None, "frame_invalid")
    h, w = int(img.shape[0]), int(img.shape[1])
    scale = h / 720.0
    if scale < 0.90 or scale > 1.60 or w / float(h) < 1.7 or w / float(h) > 1.8:
        return BestPlayRead(False, None, "frame_size")
    x0, y0, x1, y1 = (int(round(v * scale)) for v in pack["search"])
    crop = img[y0:y1, x0:x1, :3]
    hsv = cv2.cvtColor(np.ascontiguousarray(crop), cv2.COLOR_BGR2HSV)
    ink = (hsv[:, :, 2] > pack["value_thr"]).astype(np.uint8) * 255
    f = pack["down"] / scale  # back to the template's 720p-halved grid
    ink = cv2.resize(ink, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
    ink = (ink > 127).astype(np.uint8) * 255
    templ = pack["mask"]
    if ink.shape[0] < templ.shape[0] or ink.shape[1] < templ.shape[1]:
        return BestPlayRead(False, None, "frame_size")
    if not ink.any():
        return BestPlayRead(False, 0.0, "no_ink")
    score = float(cv2.matchTemplate(ink, templ, cv2.TM_CCOEFF_NORMED).max())
    if not np.isfinite(score):
        return BestPlayRead(False, 0.0, "no_ink")
    seen = score >= pack["threshold"]
    return BestPlayRead(seen, round(score, 4), "best_play" if seen else "not_present")
