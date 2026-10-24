"""FIELD GOAL / TOUCHDOWN fragments on the standard bar.

The matcher looks for a ``GOA`` fragment and a ``TOUC`` fragment. It does not OCR the banner and
it does not read a team. Both sides stay candidates.

No fragment pack is shipped. The only cuts in this repo were held-out 170123 frames (hl00 f175
for FIELD GOAL, hl01 f174 for TOUCHDOWN). Those matches are evaluation only. The navy TOUCHDOWN
on 125430 hl04 was missed by that held-out TOUC fragment (NCC about 0.31) and is still undetected:
there is no non-held-out navy or gold TOUCHDOWN frame to cut a template from. Without a pack,
``detect_banner`` returns None and the grammar does not arm. A +6 or +3 still takes the ordinary
path. The 1ST DOWNS fixture is not a field goal.

Gates, if a pack is supplied later: GOA 0.66 (1ST DOWNS peaked at 0.57) and TOUC 0.72.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

# Intentionally absent. See the module docstring. A future pack must not be cut from a held-out match.
PACK = Path(__file__).resolve().parent / "packs" / "madden27_banner_fragments.npz"

# NCC floors. DOWNS at 0.57 must miss. The weakest full FIELD GOAL in the clips is 0.69.
GOA_MIN = 0.66
TOUC_MIN = 0.72

# Reference-band rows/cols that hold the banner words (the strip is 30 px tall at 640×360).
_ROI = (8, 28, 160, 450)


_LOADED: tuple | None = None


def _load():
    global _LOADED
    if _LOADED is None:
        if not PACK.is_file():
            _LOADED = (None, None)
        else:
            z = np.load(PACK)
            _LOADED = (z["goa"].astype(np.uint8), z["touc"].astype(np.uint8))
    return _LOADED


def _ncc(mask: np.ndarray, tpl: np.ndarray) -> float:
    if tpl is None or mask.shape[0] < tpl.shape[0] or mask.shape[1] < tpl.shape[1]:
        return -1.0
    res = cv2.matchTemplate(mask, tpl, cv2.TM_CCOEFF_NORMED)
    if res.size == 0:
        return -1.0
    score = float(np.nanmax(res))
    return score if np.isfinite(score) else -1.0


def detect_banner_band(band) -> str | None:
    """``"fg"``, ``"td"``, or None. ``band`` is the profile reference strip."""
    goa, touc = _load()
    if goa is None or band is None:
        return None
    y0, y1, x0, x1 = _ROI
    if band.shape[0] < y1 or band.shape[1] < x1:
        return None
    roi = band[y0:y1, x0:x1]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    white = (gray > 180).astype(np.uint8) * 255
    dark = (cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)[..., 2] < 80).astype(np.uint8) * 255
    g, t = _ncc(white, goa), _ncc(dark, touc)
    fg, td = g >= GOA_MIN, t >= TOUC_MIN
    if fg and td:
        return "fg" if g >= t else "td"
    if fg:
        return "fg"
    if td:
        return "td"
    return None


def detect_banner(reader, frame) -> str | None:
    """Banner kind on a frame the standard reader can turn into its reference strip."""
    if frame is None or not hasattr(reader, "reference_band"):
        return None
    band, why = reader.reference_band(frame)
    if band is None or why != "ok":
        return None
    return detect_banner_band(band)
