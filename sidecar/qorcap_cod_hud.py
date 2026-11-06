"""Calibrated Call of Duty HUD reads for cod_mw4_hud.

Templates come from Con's private practice frames in ``sidecar/fixtures/cod``.
A read sets ``ok`` and ``calibrated`` only when that signal is on the crop.
Anything unsure stays not read. This module does not post a final and does not
write a receipt field.

XP plates are presence only. A 25 or a 50 is not an assist and not a kill.
The red kill marker is presence only. It is not a kill count.
DEFEAT agrees on the top-right header the same way VICTORY does, when the
six letters agree, the field around the word is red, and the word is sharp
enough. A blurred word does not agree. The Kill Confirmed header is a thinner
cut: pieces of one letter are joined, then checked against that header's own
letters, on the same gates. Home and away stay unread when the screen does not
label them.
"""
from __future__ import annotations

from pathlib import Path

PACK_PATH = Path(__file__).resolve().parent / "packs" / "cod_mw4_hud_v1.npz"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "cod"

# Letter row on the splash and on the match-complete header.
_LETTER_MIN = 0.70
_LETTER_MEAN = 0.85
# E and F are close. A letter that does not clear the next one stays unread.
_LETTER_MARGIN = 0.08
# DEFEAT template order. The two E's are separate samples of the same letter.
_DEFEAT_LETTERS = ("D", "E", "F", "E", "A", "T")
# DEFEAT is tighter than VICTORY. Every letter has to clear this, and the mean too.
_DEFEAT_LETTER_MIN = 0.85
_DEFEAT_LETTER_MEAN = 0.90
# Top Sobel gradient over the word's text contrast. A 3x3 Sobel peaks near
# eight times a one-pixel step, so the ratio is divided by 8. A clean word
# sits near 0.45. A sigma-2 blur sits near 0.20. Below this the word does not agree.
_SHARP_MIN = 0.26
# Share of the word box that is red (OpenCV hue near 0, saturation at least 60).
_RED_MIN = 0.20
# Top-right header crop from the practice frames, pasted back at this origin
# on a 1280x720 frame. The crop is the word and the red field. It has no scoreboard.
DEFEAT_HEADER_ROI = "defeat_header_030.png"
DEFEAT_HEADER_ORIGIN = (980, 8)
# Kill Confirmed header (match-20261104-202724). Same kind of word crop, at the
# corner it occupied on that 720p frame. The letters are a thinner cut.
DEFEAT_KC_ROI = "defeat_header_202724_030.png"
DEFEAT_KC_ORIGIN = (968, 18)
# A finished DEFEAT letter is at least this share of the crop. Splash scraps are shorter.
_LETTER_HEIGHT_MIN = 0.10
# Pieces of one thinner letter sit within this many pixels. A and T together are wider
# than one letter, so a small gap does not merge them.
_STENCIL_GAP = 4
_LETTER_WIDTH_RATIO = 0.62
# Header words on the personal K/A row.
_HEADER_MIN = 0.70
# Digit prototypes from those same rows.
_DIGIT_MIN = 0.80
_DIGIT_MARGIN = 0.12
# Yellow "XP" plate. A lower score is not the plate.
_XP_MIN = 0.74
# White weapon-icon family from the killfeed icon sheet.
_ICON_MIN = 0.70

_HEADER_ORDER = ("SCORE", "KILLS", "ASSISTS", "DEATHS")


def _libs():
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None, None
    return cv2, np


def _pack():
    cv2, np = _libs()
    if np is None or not PACK_PATH.is_file():
        return None
    try:
        return np.load(PACK_PATH, allow_pickle=False)
    except Exception:
        return None


def _as_bgr(img):
    cv2, np = _libs()
    if np is None or not isinstance(img, np.ndarray):
        return None
    if img.ndim != 3 or img.shape[2] < 3 or img.dtype != np.uint8:
        return None
    if img.shape[0] < 16 or img.shape[1] < 16:
        return None
    if img.shape[2] > 3:
        img = img[:, :, :3]
    return img


def _region(name: str):
    from qorcap_cod import REGIONS
    for region, box in REGIONS:
        if region == name:
            return box
    raise KeyError(name)


def _crop(img, name: str):
    from qorcap_cod import crop_box
    return crop_box(img, _region(name))


def _not_read(region: str, reason: str, note: str):
    from qorcap_cod import RegionRead
    return RegionRead(region, "not_read", False, reason, False, note)


def _present(region: str, reason: str, note: str, **kw):
    from qorcap_cod import RegionRead
    return RegionRead(region, "read", True, reason, True, note, **kw)


def _vec(np, patch, size):
    cv2, _ = _libs()
    arr = cv2.resize(patch, size, interpolation=cv2.INTER_AREA).astype(np.float32)
    if arr.size == 0:
        return None
    if float(arr.max()) > 1.0:
        arr = arr / 255.0
    v = arr.ravel()
    v = v - float(v.mean())
    n = float(np.linalg.norm(v))
    return None if n < 1e-6 else v / n


def _labels(pack, key: str) -> list[str]:
    return [bytes(x).decode("ascii") for x in pack[key]]


def _white_mask(img):
    cv2, np = _libs()
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    v, s = hsv[:, :, 2], hsv[:, :, 1]
    return ((v > 170) & (s < 90)).astype(np.uint8) * 255


def _victory(img) -> bool:
    """A VICTORY word: V, a narrow I, then C T O R Y, one row, tall glyphs."""
    cv2, np = _libs()
    pack = _pack()
    bgr = _as_bgr(img)
    if cv2 is None or pack is None or bgr is None or "victory" not in pack:
        return False
    templates = pack["victory"].astype(np.float32)
    mask = _white_mask(bgr)
    h, w = bgr.shape[:2]
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    glyphs = []
    for i in range(1, n):
        x, y, ww, hh, area = (int(t) for t in st[i])
        if hh < h * 0.045 or hh > h * 0.50 or ww > w * 0.14 or area < 30:
            continue
        if ww / max(hh, 1) > 0.90:
            continue
        g = (lab[y:y + hh, x:x + ww] == i).astype(np.uint8) * 255
        vec = _vec(np, g, (16, 32))
        if vec is None:
            continue
        scores = templates @ vec
        glyphs.append({"x": x, "y": y, "w": ww, "h": hh, "scores": scores})
    if len(glyphs) < 6:
        return False
    glyphs.sort(key=lambda g: g["y"] + g["h"] / 2)
    rows: list[dict] = []
    for g in glyphs:
        cy = g["y"] + g["h"] / 2
        placed = False
        for row in rows:
            if abs(cy - row["cy"]) <= max(g["h"], row["h"]) * 0.40:
                row["items"].append(g)
                row["cy"] = float(np.mean([it["y"] + it["h"] / 2 for it in row["items"]]))
                row["h"] = float(np.median([it["h"] for it in row["items"]]))
                placed = True
                break
        if not placed:
            rows.append({"items": [g], "cy": cy, "h": float(g["h"])})
    for row in rows:
        found = _victory_row(row["items"])
        if found and _sharpness(bgr, found) >= _SHARP_MIN:
            return True
    return False


def _victory_row(items):
    np = _libs()[1]
    items = sorted(items, key=lambda g: g["x"])
    heights = [it["h"] for it in items]
    med = float(np.median(heights))
    if med < 8:
        return None
    items = [it for it in items if abs(it["h"] - med) / med <= 0.30]
    # V C T O R Y are template indexes 0,2,3,4,5,6. I is index 1 and is narrow.
    need = (0, 2, 3, 4, 5, 6)
    found = []
    start = 0
    labeled = []
    for it in items:
        idx = int(np.argmax(it["scores"]))
        labeled.append((idx, float(it["scores"][idx]), it))
    for want in need:
        hit = None
        for j in range(start, len(labeled)):
            idx, score, it = labeled[j]
            if idx == want and score >= _LETTER_MIN:
                hit = j
                break
        if hit is None:
            return None
        found.append(labeled[hit])
        start = hit + 1
    scores = [sc for _, sc, _ in found]
    if float(np.mean(scores)) < _LETTER_MEAN:
        return None
    v = found[0][2]
    c = found[1][2]
    if not (v["x"] < c["x"]):
        return None
    med_w = float(np.median([it["w"] for _, _, it in found]))
    between = [it for _, _, it in labeled if v["x"] < it["x"] < c["x"]]
    if not any(it["w"] <= med_w * 0.55 for it in between):
        return None
    # The six wide letters sit in one band, not scattered across the frame.
    xs = [it["x"] for _, _, it in found]
    span = xs[-1] - xs[0]
    if span < med_w * 4 or span > med_w * 14:
        return None
    return [it for _, _, it in found]


def _component_mask(lab, comp):
    """Binary mask of one component, tight on its box."""
    x, y, ww, hh = comp["x"], comp["y"], comp["w"], comp["h"]
    return (lab[y:y + hh, x:x + ww] == comp["i"]).astype(_libs()[1].uint8) * 255


def _glyph_from_mask(mask, x, y):
    """A normalised glyph, or None when the patch has no ink."""
    np = _libs()[1]
    vec = _vec(np, mask, (16, 32))
    if vec is None:
        return None
    return {"x": int(x), "y": int(y), "w": int(mask.shape[1]), "h": int(mask.shape[0]), "vec": vec}


def _letter_components(mask):
    """White components that could be part of a header letter.

    Huge blobs (the glitched splash) and specks stay out. The height floor is
    not applied here: a thinner cut splits one letter into a tall piece and a
    shorter piece, and the short piece is still part of the letter.
    """
    cv2, _np = _libs()
    h, w = mask.shape[:2]
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    comps = []
    for i in range(1, n):
        x, y, ww, hh, area = (int(t) for t in st[i])
        if hh > h * 0.50 or ww > w * 0.14 or area < 80:
            continue
        comps.append({"x": x, "y": y, "w": ww, "h": hh, "area": area, "i": i})
    return lab, comps


def _strict_letters(lab, comps, shape):
    """Letters that already clear the height floor as one component.

    Splash scan-line scraps are shorter than the header letters. A shorter
    floor would take those scraps as letters, so this floor stays put.
    """
    h = shape[0]
    glyphs = []
    for comp in comps:
        ww, hh = comp["w"], comp["h"]
        if hh < h * _LETTER_HEIGHT_MIN or ww / max(hh, 1) > 0.90:
            continue
        glyph = _glyph_from_mask(_component_mask(lab, comp), comp["x"], comp["y"])
        if glyph is not None:
            glyphs.append(glyph)
    return glyphs


def _join_split_letters(lab, comps, anchors, shape):
    """Join pieces of one thinner letter. A row of six whole letters is not passed here.

    The band is the tall pieces. A shorter piece joins when it sits in that band
    and the joined width is still one letter. Two neighboring letters, such as
    A and T, are wider than that, so they stay apart. The joined box uses the
    band height when the piece starts at the cap, so a sliced letter is not
    dropped by the height floor. No ink is invented in the gap.
    """
    np = _libs()[1]
    h, w = shape[:2]
    if len(anchors) < 4:
        return None
    y0 = int(np.median([g["y"] for g in anchors]))
    y1 = int(np.median([g["y"] + g["h"] for g in anchors]))
    band_h = max(1, y1 - y0)
    max_w = band_h * _LETTER_WIDTH_RATIO
    eligible = []
    for comp in comps:
        if comp["w"] > max_w:
            continue
        top, bot = comp["y"], comp["y"] + comp["h"]
        if min(bot, y1) - max(top, y0) < 4:
            continue
        eligible.append(comp)
    eligible.sort(key=lambda comp: (comp["x"], -comp["h"]))
    clusters: list[list] = []
    for comp in eligible:
        placed = False
        for cluster in clusters:
            left = min(it["x"] for it in cluster)
            right = max(it["x"] + it["w"] for it in cluster)
            if comp["x"] <= right + _STENCIL_GAP:
                new_left = min(left, comp["x"])
                new_right = max(right, comp["x"] + comp["w"])
                if new_right - new_left <= max_w:
                    cluster.append(comp)
                    placed = True
                    break
        if not placed:
            clusters.append([comp])
    glyphs = []
    for cluster in clusters:
        if min(it["y"] for it in cluster) > y0 + 12:
            continue
        x0 = min(it["x"] for it in cluster)
        x1 = max(it["x"] + it["w"] for it in cluster)
        top = min(it["y"] for it in cluster)
        bot = max(it["y"] + it["h"] for it in cluster)
        if abs(top - y0) <= 8:
            top = min(top, y0)
            bot = max(bot, y1)
        ww, hh = x1 - x0, bot - top
        if hh < h * _LETTER_HEIGHT_MIN or hh > h * 0.50 or ww > w * 0.14 or ww / max(hh, 1) > 0.90:
            continue
        canvas = np.zeros((hh, ww), np.uint8)
        for comp in cluster:
            part = _component_mask(lab, comp)
            yy, xx = comp["y"] - top, comp["x"] - x0
            canvas[yy:yy + comp["h"], xx:xx + comp["w"]] = np.maximum(
                canvas[yy:yy + comp["h"], xx:xx + comp["w"]], part)
        if int((canvas > 0).sum()) < 80:
            continue
        glyph = _glyph_from_mask(canvas, x0, top)
        if glyph is not None:
            glyphs.append(glyph)
    return glyphs


def _letter_glyphs(mask):
    """Tall narrow glyphs for a DEFEAT word.

    A header whose six letters are already separate components is returned as
    those components. A thinner cut that comes back short of six is joined
    first. The height floor on a finished letter does not move.
    """
    lab, comps = _letter_components(mask)
    strict = _strict_letters(lab, comps, mask.shape)
    if len(strict) >= 6:
        return strict
    joined = _join_split_letters(lab, comps, strict, mask.shape)
    if not joined:
        return strict
    return joined


def _band_rows(glyphs):
    """Stack glyphs whose centers sit on one horizontal band."""
    np = _libs()[1]
    rows: list[dict] = []
    for g in sorted(glyphs, key=lambda it: it["y"] + it["h"] / 2):
        cy = g["y"] + g["h"] / 2
        placed = False
        for row in rows:
            if abs(cy - row["cy"]) <= max(g["h"], row["h"]) * 0.40:
                row["items"].append(g)
                row["cy"] = float(np.mean([it["y"] + it["h"] / 2 for it in row["items"]]))
                row["h"] = float(np.median([it["h"] for it in row["items"]]))
                placed = True
                break
        if not placed:
            rows.append({"items": [g], "cy": cy, "h": float(g["h"])})
    return rows


def _word_box(glyphs, shape, pad: int = 2):
    """Tight box around the matched letters, in the same image as those glyphs."""
    h, w = shape[:2]
    x0 = max(0, min(int(g["x"]) for g in glyphs) - pad)
    y0 = max(0, min(int(g["y"]) for g in glyphs) - pad)
    x1 = min(w, max(int(g["x"] + g["w"]) for g in glyphs) + pad)
    y1 = min(h, max(int(g["y"] + g["h"]) for g in glyphs) + pad)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    return x0, y0, x1, y1


def _sharpness(bgr, glyphs) -> float:
    """Top gradient divided by text contrast on the word box.

    A clean word lands near 0.45. A sigma-2 blur lands near 0.20.
    """
    cv2, np = _libs()
    box = _word_box(glyphs, bgr.shape)
    if cv2 is None or box is None:
        return 0.0
    x0, y0, x1, y1 = box
    gray = cv2.cvtColor(bgr[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY).astype(np.float32)
    lo, hi = np.percentile(gray, [10.0, 90.0])
    contrast = float(hi - lo)
    if contrast < 8.0:
        return 0.0
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    top = float(np.percentile(cv2.magnitude(gx, gy), 95.0))
    return top / contrast / 8.0


def _red_around(bgr, glyphs) -> float:
    """Share of the word box that is red at saturation 60 or higher."""
    cv2, np = _libs()
    box = _word_box(glyphs, bgr.shape)
    if cv2 is None or box is None:
        return 0.0
    x0, y0, x1, y1 = box
    hsv = cv2.cvtColor(bgr[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    hue, sat = hsv[:, :, 0], hsv[:, :, 1]
    red = ((hue <= 10) | (hue >= 170)) & (sat >= 60)
    return float(np.mean(red))


def _defeat_row(items):
    """D E F E A T, left to right, one band. A near-tie or a short row stays unread."""
    np = _libs()[1]
    items = sorted(items, key=lambda g: g["x"])
    heights = [it["h"] for it in items]
    med = float(np.median(heights))
    if med < 8:
        return None
    items = [it for it in items if abs(it["h"] - med) / med <= 0.30]
    labeled = []
    for it in items:
        by: dict[str, float] = {}
        for i, ch in enumerate(_DEFEAT_LETTERS):
            score = float(it["scores"][i])
            if ch not in by or score > by[ch]:
                by[ch] = score
        ranked = sorted(by.items(), key=lambda kv: -kv[1])
        best, top = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else -1.0
        labeled.append((best, top, top - second, it))
    found = []
    start = 0
    for want in _DEFEAT_LETTERS:
        hit = None
        for j in range(start, len(labeled)):
            ch, score, margin, _it = labeled[j]
            if ch == want and score >= _DEFEAT_LETTER_MIN and margin >= _LETTER_MARGIN:
                hit = j
                break
        if hit is None:
            return None
        found.append(labeled[hit])
        start = hit + 1
    scores = [sc for _, sc, _, _ in found]
    if float(np.mean(scores)) < _DEFEAT_LETTER_MEAN:
        return None
    med_w = float(np.median([it["w"] for _, _, _, it in found]))
    xs = [it["x"] for _, _, _, it in found]
    span = xs[-1] - xs[0]
    if med_w < 1 or span < med_w * 4 or span > med_w * 14:
        return None
    return [it for _, _, _, it in found]


def _defeat_rows(pack):
    """Calibrated header rows. Row 0 is the solid cut. Later rows are thinner cuts.

    A word still has to clear 0.85 on every letter, mean 0.90, the margin, the
    sharpness gate, and the red field. Another row does not relax those gates,
    and the glitched splash is not a row.
    """
    rows = pack["defeat"].astype(_libs()[1].float32)
    if rows.ndim != 3 or rows.shape[1] != len(_DEFEAT_LETTERS) or rows.shape[0] < 1:
        return None
    return rows


def _defeat_on_row(bgr, glyphs, templates) -> bool:
    scored = []
    for glyph in glyphs:
        item = dict(glyph)
        item["scores"] = templates @ glyph["vec"]
        scored.append(item)
    for row in _band_rows(scored):
        found = _defeat_row(row["items"])
        if not found:
            continue
        if _sharpness(bgr, found) < _SHARP_MIN:
            continue
        if _red_around(bgr, found) < _RED_MIN:
            continue
        return True
    return False


def _defeat(img) -> bool:
    """A sharp DEFEAT header on a red field.

    The letters are the top-right header. Each one has to clear 0.85 and the
    mean has to clear 0.90, and the field around the word has to be red. Solid
    letters match the first calibrated row. A thinner cut is joined, then
    matched to its own row, on those same gates. The centered splash is cut by
    scan lines, so it stays unread. A blurred word stays unread. Team totals
    are not home and away: the screen does not label those sides, and the
    receipt side is the side chosen at the start.
    """
    cv2, _np = _libs()
    pack = _pack()
    bgr = _as_bgr(img)
    if cv2 is None or pack is None or bgr is None or "defeat" not in pack:
        return False
    rows = _defeat_rows(pack)
    if rows is None:
        return False
    glyphs = _letter_glyphs(_white_mask(bgr))
    if len(glyphs) < 6:
        return False
    for templates in rows:
        if _defeat_on_row(bgr, glyphs, templates):
            return True
    return False


def _hitmarker_white(img) -> bool:
    """Compact bright strokes on a dark center crop. A bright scene is not a hitmarker."""
    cv2, np = _libs()
    bgr = _as_bgr(img)
    if cv2 is None or bgr is None:
        return False
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    if float(np.median(gray)) > 28:
        return False
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    k = 31 if min(gray.shape[:2]) >= 48 else 0
    if k == 0:
        return False
    blur = cv2.GaussianBlur(gray, (k, k), 0)
    bright = (gray.astype(np.int16) - blur.astype(np.int16) > 70) & (hsv[:, :, 1] < 90) & (gray > 160)
    if float(bright.mean()) > 0.02:
        return False
    n, lab, st, _ = cv2.connectedComponentsWithStats(bright.astype(np.uint8) * 255, connectivity=8)
    h, w = gray.shape[:2]
    hits = 0
    for i in range(1, n):
        x, y, ww, hh, area = (int(t) for t in st[i])
        if not (40 <= area <= 400) or ww > 90 or hh > 36:
            continue
        fill = area / float(ww * hh)
        aspect = ww / float(hh)
        if not (0.08 <= fill <= 0.40) or not (0.70 <= aspect <= 1.40):
            continue
        cx, cy = (x + ww / 2) / w, (y + hh / 2) / h
        if not (0.30 <= cx <= 0.75 and 0.30 <= cy <= 0.75):
            continue
        pad = 18
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(w, x + ww + pad), min(h, y + hh + pad)
        win = bright[y0:y1, x0:x1]
        mask = np.ones(win.shape, dtype=bool)
        mask[y - y0:y - y0 + hh, x - x0:x - x0 + ww] = False
        outside = int(win[mask].sum()) if mask.any() else 0
        if outside > 8:
            continue
        hits += 1
    return hits == 1


def _red_x(img) -> bool:
    """A red X on a dark field. A red blob in a bright scene is not this marker."""
    cv2, np = _libs()
    bgr = _as_bgr(img)
    if cv2 is None or bgr is None:
        return False
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    red = (((hue < 12) | (hue > 168)) & (sat > 100) & (val > 80))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask = cv2.morphologyEx(red.astype(np.uint8) * 255, cv2.MORPH_CLOSE, kernel)
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]
    found = 0
    for i in range(1, n):
        x, y, ww, hh, area = (int(t) for t in st[i])
        if not (100 <= area <= 700) or not (14 <= ww <= 36 and 14 <= hh <= 40):
            continue
        if not (0.65 <= ww / float(hh) <= 1.5):
            continue
        patch = lab[y:y + hh, x:x + ww] == i
        fill = area / float(ww * hh)
        if not (0.40 <= fill <= 0.72):
            continue
        cy, cx = hh / 2, ww / 2
        quads = (
            patch[:int(cy), :int(cx)].mean(),
            patch[:int(cy), int(cx):].mean(),
            patch[int(cy):, :int(cx)].mean(),
            patch[int(cy):, int(cx):].mean(),
        )
        if min(float(q) for q in quads) < 0.18:
            continue
        x0, y0 = max(0, x - 6), max(0, y - 6)
        x1, y1 = min(w, x + ww + 6), min(h, y + hh + 6)
        ring = gray[y0:y1, x0:x1]
        keep = np.ones(ring.shape, dtype=bool)
        keep[y - y0:y - y0 + hh, x - x0:x - x0 + ww] = False
        if keep.sum() < 10 or float(ring[keep].mean()) > 40:
            continue
        found += 1
        if found >= 1:
            return True
    return False


def _xp_feature(img):
    cv2, np = _libs()
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    yellow = ((hue > 15) & (hue < 45) & (sat > 60)).astype(np.uint8)
    return (val * yellow).astype(np.uint8)


def _xp_present(img) -> bool:
    cv2, np = _libs()
    pack = _pack()
    bgr = _as_bgr(img)
    if cv2 is None or pack is None or bgr is None or "xp" not in pack:
        return False
    templ = pack["xp"]
    feat = _xp_feature(bgr)
    if feat.shape[0] < templ.shape[0] or feat.shape[1] < templ.shape[1]:
        return False
    result = cv2.matchTemplate(feat, templ, cv2.TM_CCOEFF_NORMED)
    return float(result.max()) >= _XP_MIN


def _killfeed_present(img) -> bool:
    cv2, np = _libs()
    pack = _pack()
    bgr = _as_bgr(img)
    if cv2 is None or pack is None or bgr is None or "icon" not in pack:
        return False
    icon = pack["icon"]
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    if gray.shape[0] < icon.shape[0] or gray.shape[1] < icon.shape[1]:
        return False
    result = cv2.matchTemplate(gray, icon, cv2.TM_CCOEFF_NORMED)
    return float(result.max()) >= _ICON_MIN


def _header_hits(img) -> list[tuple[str, float, int, int, int, int]]:
    cv2, np = _libs()
    pack = _pack()
    if cv2 is None or pack is None:
        return []
    labels = _labels(pack, "header_labels")
    vecs = pack["header_vecs"].astype(np.float32)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    mask = ((hue > 75) & (hue < 110) & (sat > 40) & (val > 80)).astype(np.uint8) * 255
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    comps = []
    for i in range(1, n):
        x, y, ww, hh, area = (int(t) for t in st[i])
        if area > 8 and 6 <= hh <= 28 and ww >= 3:
            comps.append((x, y, ww, hh))
    comps.sort()
    groups: list[list[tuple[int, int, int, int]]] = []
    for comp in comps:
        if not groups or comp[0] > groups[-1][-1][0] + groups[-1][-1][2] + 10:
            groups.append([comp])
        else:
            groups[-1].append(comp)
    hits = []
    for group in groups:
        x0 = min(c[0] for c in group)
        x1 = max(c[0] + c[2] for c in group)
        y0 = min(c[1] for c in group)
        y1 = max(c[1] + c[3] for c in group)
        if x1 - x0 < 20 or y1 - y0 > 40:
            continue
        vec = _vec(np, mask[y0:y1, x0:x1], (48, 16))
        if vec is None:
            continue
        scores = vecs @ vec
        idx = int(np.argmax(scores))
        best = float(scores[idx])
        if best >= _HEADER_MIN:
            hits.append((labels[idx], best, x0, x1, y0, y1))
    return hits


def _digits_under(img, x0: int, x1: int, y0: int, y1: int) -> str | None:
    cv2, np = _libs()
    pack = _pack()
    if cv2 is None or pack is None:
        return None
    labels = _labels(pack, "digit_labels")
    vecs = pack["digit_vecs"].astype(np.float32)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    mask = ((hue > 20) & (hue < 75) & (sat > 50) & (val > 90)).astype(np.uint8) * 255
    h, w = mask.shape[:2]
    mask[:max(0, y0), :] = 0
    mask[min(h, y1):, :] = 0
    mask[:, :max(0, x0)] = 0
    mask[:, min(w, x1):] = 0
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    comps = []
    for i in range(1, n):
        x, y, ww, hh, area = (int(t) for t in st[i])
        if hh >= 12 and 4 <= ww <= 18 and area > 30:
            g = (lab[y:y + hh, x:x + ww] == i).astype(np.uint8) * 255
            comps.append((x, g))
    comps.sort()
    if not comps or len(comps) > 3:
        return None
    text = ""
    for _, glyph in comps:
        vec = _vec(np, glyph, (12, 18))
        if vec is None:
            return None
        scores = vecs @ vec
        by: dict[str, float] = {}
        for lab_i, name in enumerate(labels):
            score = float(scores[lab_i])
            if name not in by or score > by[name]:
                by[name] = score
        ranked = sorted(by.items(), key=lambda kv: -kv[1])
        best, top = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else -1.0
        if top < _DIGIT_MIN or top - second < _DIGIT_MARGIN:
            return None
        text += best
    return text if text.isdigit() else None


def _scoreboard_numbers(img) -> tuple[int, int] | None:
    bgr = _as_bgr(img)
    if bgr is None:
        return None
    hits = _header_hits(bgr)
    kills = next((h for h in hits if h[0] == "KILLS"), None)
    assists = next((h for h in hits if h[0] == "ASSISTS"), None)
    if kills is None or assists is None:
        return None
    if assists[2] <= kills[2]:
        return None
    if abs(kills[4] - assists[4]) > 8:
        return None
    def column(hit):
        _name, _score, x0, x1, y0, y1 = hit
        height = max(8, y1 - y0)
        return _digits_under(bgr, x0 - 16, x1 + 12, y1 - 2, y1 + height * 3)
    k_text = column(kills)
    a_text = column(assists)
    if k_text is None or a_text is None:
        return None
    return int(k_text), int(a_text)


def read_hitmarker(img, *, cropped: bool = False):
    src = _crop(img, "hitmarker") if cropped else img
    bgr = _as_bgr(src)
    if bgr is None:
        return _not_read("hitmarker", "frame_invalid", "frame could not be cropped; it is not Counted")
    if _hitmarker_white(bgr):
        return _present("hitmarker", "hitmarker", "Hitmarker strokes are on the center crop.")
    if _red_x(bgr):
        return _present("hitmarker", "red_x", "Red kill marker is present. It is not a kill count.")
    return _not_read("hitmarker", "not_present", "No hitmarker was read.")


def read_red_x(img):
    """Presence of a red X on the crop. Not a kill count."""
    bgr = _as_bgr(img)
    if bgr is None:
        return _not_read("red_x", "frame_invalid", "frame could not be cropped; it is not Counted")
    if _red_x(bgr):
        return _present("red_x", "red_x", "Red kill marker is present. It is not a kill count.")
    return _not_read("red_x", "not_present", "No red kill marker was read.")


def read_kill_popup(img, *, cropped: bool = False):
    src = _crop(img, "kill_popup") if cropped else img
    bgr = _as_bgr(src)
    if bgr is None:
        return _not_read("kill_popup", "frame_invalid", "frame could not be cropped; it is not Counted")
    if _xp_present(bgr):
        return _present(
            "kill_popup", "xp_plate",
            "XP plate is present. The number is not an assist and not a kill count.")
    return _not_read("kill_popup", "not_present", "No kill popup was read.")


def read_killfeed(img, *, cropped: bool = False):
    src = _crop(img, "killfeed") if cropped else img
    bgr = _as_bgr(src)
    if bgr is None:
        return _not_read("killfeed", "frame_invalid", "frame could not be cropped; it is not Counted")
    if _killfeed_present(bgr):
        return _present("killfeed", "killfeed_icon", "Killfeed weapon icon is present.")
    return _not_read("killfeed", "not_present", "No killfeed icon was read.")


def read_scoreboard(img, *, cropped: bool = False):
    src = _crop(img, "scoreboard") if cropped else img
    numbers = _scoreboard_numbers(src)
    if numbers is None:
        bgr = _as_bgr(src)
        reason = "frame_invalid" if bgr is None else "not_present"
        note = ("frame could not be cropped; it is not Counted" if bgr is None
                else "Scoreboard kills and assists were not read. They are not Counted.")
        return _not_read("scoreboard", reason, note)
    kills, assists = numbers
    return _present(
        "scoreboard", "scoreboard_ka",
        "Kills and assists were read. No offline count is on this frame, so the comparison is not Counted.",
        board_kills=kills, board_assists=assists)


def read_end(img):
    src = _crop(img, "end_screen")
    bgr = _as_bgr(src)
    if bgr is None:
        return _not_read("end_screen", "frame_invalid", "frame could not be cropped; it is not Counted")
    if _victory(bgr):
        return _present(
            "end_screen", "victory",
            "VICTORY on the end screen. Home and away were not both read, so no final score is posted.",
            confirm=True)
    if _defeat(bgr):
        return _present(
            "end_screen", "defeat",
            "DEFEAT on the end screen. Home and away were not both read, so no final score is posted.",
            confirm=True)
    return _not_read("end_screen", "not_present", "This frame is not an end screen. No final is posted.")


def read_hud(img) -> list:
    from qorcap_cod import STUB_NOTE
    out = []
    for name in ("crosshair", "hitmarker", "kill_popup", "killfeed", "pause", "scoreboard"):
        if name in ("crosshair", "pause"):
            if _as_bgr(_crop(img, name)) is None:
                out.append(_not_read(name, "frame_invalid", "frame could not be cropped; it is not Counted"))
            else:
                out.append(_not_read(name, "stub_not_calibrated", STUB_NOTE))
        elif name == "hitmarker":
            out.append(read_hitmarker(img, cropped=True))
        elif name == "kill_popup":
            out.append(read_kill_popup(img, cropped=True))
        elif name == "killfeed":
            out.append(read_killfeed(img, cropped=True))
        else:
            out.append(read_scoreboard(img, cropped=True))
    return out


def _place_fixture(fixture_dir: Path, name: str, origin: tuple[int, int]):
    """Paste a word crop onto a blank 720p frame at its original corner."""
    cv2, np = _libs()
    roi = cv2.imread(str(fixture_dir / name))
    if roi is None:
        raise RuntimeError(f"missing {name}")
    canvas = np.zeros((720, 1280, 3), np.uint8)
    x, y = origin
    h, w = roi.shape[:2]
    if y < 0 or x < 0 or y + h > canvas.shape[0] or x + w > canvas.shape[1]:
        raise RuntimeError(f"{name} does not fit at {origin}")
    canvas[y:y + h, x:x + w] = roi
    return canvas


def _defeat_header_vecs(fixture_dir: Path, name: str, origin: tuple[int, int]):
    """Six header letters, left to right, from one placed word crop."""
    np = _libs()[1]
    header_im = _place_fixture(fixture_dir, name, origin)
    header = _crop(header_im, "end_screen")
    if header is None:
        raise RuntimeError(f"{name} could not be placed")
    header_g = sorted(_letter_glyphs(_white_mask(header)), key=lambda g: g["x"])
    if len(header_g) != 6:
        raise RuntimeError(f"expected 6 DEFEAT header letters in {name}, saw {len(header_g)}")
    return np.stack([g["vec"] for g in header_g]).astype(np.float32)


def _defeat_pack_rows(fixture_dir: Path):
    """One row per calibrated header cut, D E F E A T.

    Row 0 is the solid Domination header. Row 1 is the thinner Kill Confirmed
    header. The glitched splash is not a template.
    """
    np = _libs()[1]
    rows = [
        _defeat_header_vecs(fixture_dir, DEFEAT_HEADER_ROI, DEFEAT_HEADER_ORIGIN),
        _defeat_header_vecs(fixture_dir, DEFEAT_KC_ROI, DEFEAT_KC_ORIGIN),
    ]
    return np.stack(rows).astype(np.float32)


def build_calibration_pack(fixture_dir: Path = FIXTURE_DIR, out_path: Path = PACK_PATH) -> None:
    """Rebuild the template pack from the committed practice fixtures."""
    cv2, np = _libs()
    if cv2 is None:
        raise RuntimeError("opencv and numpy are required to build the pack")
    splash = cv2.imread(str(fixture_dir / "end_173922_017.jpg"))
    mask = _white_mask(splash)
    h, w = splash.shape[:2]
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    letters = []
    for i in range(1, n):
        x, y, ww, hh, area = (int(t) for t in st[i])
        if hh > h * 0.12 and ww < w * 0.10 and area > 100:
            g = (lab[y:y + hh, x:x + ww] == i).astype(np.uint8) * 255
            vec = _vec(np, g, (16, 32))
            letters.append((x, vec))
    letters.sort()
    if len(letters) != 7:
        raise RuntimeError(f"expected 7 VICTORY letters, saw {len(letters)}")
    victory = np.stack([vec for _, vec in letters]).astype(np.float32)
    defeat = _defeat_pack_rows(fixture_dir)

    board = cv2.imread(str(fixture_dir / "sb_003.png"))
    hsv = cv2.cvtColor(board, cv2.COLOR_BGR2HSV)
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    header = ((hue > 75) & (hue < 110) & (sat > 40) & (val > 80)).astype(np.uint8)
    boxes = ((66, 16, 120, 37), (218, 16, 263, 37), (368, 16, 435, 37), (520, 16, 583, 37))
    header_vecs = []
    for x0, y0, x1, y1 in boxes:
        header_vecs.append(_vec(np, header[y0:y1, x0:x1] * 255, (48, 16)))
    header_vecs = np.stack(header_vecs).astype(np.float32)

    boards = (
        ("sb_001.png", "585042921"),
        ("sb_002.png", "6770491226"),
        ("sb_003.png", "5580381231"),
    )
    digit_labels = []
    digit_vecs = []
    for name, text in boards:
        im = cv2.imread(str(fixture_dir / name))
        hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
        hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
        ink = ((hue > 20) & (hue < 75) & (sat > 50) & (val > 90)).astype(np.uint8) * 255
        ink[:int(ink.shape[0] * 0.45), :] = 0
        n, lab, st, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
        comps = []
        for i in range(1, n):
            x, y, ww, hh, area = (int(t) for t in st[i])
            if hh >= 14 and ww >= 5 and area > 40:
                g = (lab[y:y + hh, x:x + ww] == i).astype(np.uint8) * 255
                comps.append((x, g))
        comps.sort()
        if len(comps) != len(text):
            raise RuntimeError(f"{name} digit count {len(comps)} != {len(text)}")
        for (_, glyph), ch in zip(comps, text):
            digit_labels.append(ch)
            digit_vecs.append(_vec(np, glyph, (12, 18)))
    digit_vecs = np.stack(digit_vecs).astype(np.float32)

    popup = cv2.imread(str(fixture_dir / "kill17_popup.png"))
    xp = _xp_feature(popup)[18:58, 120:185]

    sheet = cv2.imread(str(fixture_dir / "v2_kf_icons.png"), cv2.IMREAD_GRAYSCALE)
    cell = sheet[5:55, 100:190]
    _thr, binary = cv2.threshold(cell, 80, 255, cv2.THRESH_BINARY)
    ys, xs = np.where(binary > 0)
    icon = binary[ys.min():ys.max() + 1, xs.min():xs.max() + 1]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        out_path,
        victory=victory,
        defeat=defeat,
        header_labels=np.array([s.encode("ascii") for s in _HEADER_ORDER]),
        header_vecs=header_vecs,
        digit_labels=np.array([s.encode("ascii") for s in digit_labels]),
        digit_vecs=digit_vecs,
        xp=xp,
        icon=icon,
    )
