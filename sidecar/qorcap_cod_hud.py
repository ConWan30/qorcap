"""Calibrated Call of Duty HUD reads for cod_mw4_hud.

Templates come from Con's private practice frames in ``sidecar/fixtures/cod``.
A read sets ``ok`` and ``calibrated`` only when that signal is on the crop.
Anything unsure stays not read. This module does not post a final and does not
write a receipt field.

XP plates are presence only. A 25 or a 50 is not an assist and not a kill.
The red kill marker is presence only. It is not a kill count.
DEFEAT agrees on the top-right header the same way VICTORY agrees on its
splash or its match-complete header. Both words use one letter gate, one
sharpness floor, and a field-color share. The word also has to sit in its
own box. A word elsewhere in the wide crop does not agree. If both words
agree on one frame, the read is ambiguous and does not confirm. The Kill
Confirmed header is a thinner cut: pieces of one letter are joined, then
checked against that header's own letters, on the same gates. A blurred
word does not agree. Home and away stay unread when the screen does not
label them.
"""
from __future__ import annotations

import json
from pathlib import Path

PACK_PATH = Path(__file__).resolve().parent / "packs" / "cod_mw4_hud_v1.npz"
# Text supplement. Masks and labels only. The shipped npz is not rewritten.
TEXT_PACK_PATH = Path(__file__).resolve().parent / "packs" / "cod_owner_row_digits.json"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "cod"
# Multi-sample scoreboard digit bank (plain rows, highlighted owner rows, personal card), with
# per-sample provenance. Built by build_cod_row_digits.py from private end boards.
ROW_BANK_PATH = Path(__file__).resolve().parent / "packs" / "cod_row_digits_v2.json"
SYNTHETIC_SAMPLE_NOTE = "synthetic sample, real-match recall UNMEASURED"

# One letter gate for both words. The practice frames clear it:
# VICTORY header 019 is at least 0.87 per letter, mean about 0.93, margin at least 0.13.
# DEFEAT headers are at least 0.96 per letter, margin at least 0.14.
# E and F are close. A letter that does not clear the next one stays unread.
_LETTER_MIN = 0.85
_LETTER_MEAN = 0.90
_LETTER_MARGIN = 0.08
# DEFEAT template order. The two E's are separate samples of the same letter.
_DEFEAT_LETTERS = ("D", "E", "F", "E", "A", "T")
# Top Sobel gradient over the word's text contrast. A 3x3 Sobel peaks near
# eight times a one-pixel step, so the ratio is divided by 8. The gradient
# is the 99th percentile and the contrast is the 5th-to-98th percentile gap.
# The same floor is used for both words. A clean word sits near 0.45. A
# sigma-2 blur sits near 0.20. Below this the word does not agree. The 95th
# percentile over the 10-90 gap scored the large splash near 0.33. A one-pixel
# blur and a 720p frame scaled up to 1080p then fell short of this floor,
# while those letters still matched.
_SHARP_MIN = 0.26
# Share of the word box that is that word's field color (saturation at least 60).
# DEFEAT is red (OpenCV hue near 0). VICTORY is the teal measured on both
# practice frames (median hue 88). The same share floor applies to both.
_FIELD_MIN = 0.20
_VICTORY_HUE = (70, 105)
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
# Full-frame fractions, then the measured letter width and height.
# The letter box has to sit inside the region and match that word's size
# within _WORD_SIZE_TOL. A header-sized word inside the splash rectangle
# does not agree. Measured on 1280x720.
# DEFEAT letters at the header origin: x 0.812-0.954, y 0.068-0.154 (0.142 by 0.086).
DEFEAT_WORD_BOX = (0.78, 0.03, 0.20, 0.17, 0.142, 0.086)
# VICTORY splash on end_173922_017: x 0.291-0.709, y 0.400-0.618 (0.417 by 0.218).
VICTORY_SPLASH_BOX = (0.24, 0.34, 0.52, 0.34, 0.417, 0.218)
# VICTORY header on end_173922_019: x 0.047-0.213, y 0.068-0.154 (0.166 by 0.086).
VICTORY_HEADER_BOX = (0.02, 0.03, 0.26, 0.17, 0.166, 0.086)
VICTORY_WORD_BOXES = (VICTORY_SPLASH_BOX, VICTORY_HEADER_BOX)
_WORD_SIZE_TOL = 0.25
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

# Qortrola30 on end_173922_019. The row template scores 1.0 on its own pixels.
# The same letters on the personal card score about 0.56. A neighboring name
# fragment scores about 0.42. JPEG quality 40 still scores about 0.91.
# Below this the letters did not agree, so the row is not read.
_OWNER_MIN = 0.80
# KILLS on that frame scores about 0.96. The other team's header scores about 0.54.
# DEATHS on the personal card scores about 0.86. The next hit scores about 0.40.
# A 720p frame scaled to 1080p scores about 0.74 on KILLS and about 0.72 on CONFIRMS.
# Splash and mid-match frames stay under 0.52.
_ROW_HEADER_MIN = 0.70
# Small end-screen digits. A clean glyph matches its own sample near 1.0.
# The next digit on these samples stays under 0.50.
_ROW_DIGIT_MIN = 0.75
_ROW_DIGIT_MARGIN = 0.15
# One column may be a score such as 1250, or a longer stat. Seven glyphs are not read.
_ROW_GLYPH_MAX = 6
_ROW_MIN_CONTRAST = 60  # V max minus median: below this a cell has no ink to read
# Lower look for a faded digit: lift over local background (floor, share of glyph lift), min area.
_ROW_LOW_MIN = 12.0
_ROW_LOW_FRAC = 0.12
_ROW_LOW_AREA = 6
# Dimmest glyph contrast over the brightest in one value; below it the value is not read.
_ROW_INK_RATIO = 0.75
# Derived ASSISTS and TDM DEATHS masks were cut at this value. The match gate is unchanged.
_DERIVED_HEADER_VALUE = 80
# Bright letters of the gamertag. Header words are dimmer, so they use their own mask.
_OWNER_SEARCH_VALUE = 150
_HEADER_SEARCH_VALUE = 90


def _libs():
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None, None
    return cv2, np


def _mask_from_rows(rows) -> object:
    """0/1 text rows to a uint8 mask. None when the rows are empty or ragged."""
    np = _libs()[1]
    if np is None or not rows:
        return None
    width = len(rows[0])
    if width < 1 or any(len(row) != width for row in rows):
        return None
    mask = np.zeros((len(rows), width), np.uint8)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch == "1":
                mask[y, x] = 255
    return mask


def _apply_text_supplement(data):
    """Merge the text digit bank and the derived column headers. The npz is not written."""
    np = _libs()[1]
    if np is None or not TEXT_PACK_PATH.is_file():
        return data
    extra = json.loads(TEXT_PACK_PATH.read_text(encoding="utf-8"))
    labels = [bytes(x).decode("ascii") for x in data["row_digit_labels"]] if "row_digit_labels" in data else []
    vecs = [data["row_digit_vecs"][i].astype(np.float32) for i in range(len(labels))] if labels else []
    for item in extra.get("digits", []):
        mask = _mask_from_rows(item.get("mask_rows"))
        vec = _vec(np, mask, (12, 18)) if mask is not None else None
        if vec is None:
            continue
        labels.append(str(item["label"]))
        vecs.append(vec.astype(np.float32))
    if vecs:
        data["row_digit_labels"] = np.array([s.encode("ascii") for s in labels])
        data["row_digit_vecs"] = np.stack(vecs).astype(np.float32)
    headers = extra.get("headers") or {}
    if "assists" in headers:
        mask = _mask_from_rows(headers["assists"].get("mask_rows"))
        if mask is not None:
            data["assists_header_mask"] = mask
    if "deaths_column" in headers:
        mask = _mask_from_rows(headers["deaths_column"].get("mask_rows"))
        if mask is not None:
            data["deaths_column_header_mask"] = mask
    return data


def _pack():
    cv2, np = _libs()
    if np is None or not PACK_PATH.is_file():
        return None
    try:
        raw = np.load(PACK_PATH, allow_pickle=False)
    except Exception:
        return None
    data = {key: raw[key] for key in raw.files}
    return _apply_text_supplement(data)


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


def _not_read(region: str, reason: str, note: str, stage: str | None = None):
    from qorcap_cod import RegionRead
    rd = RegionRead(region, "not_read", False, reason, False, note)
    if stage:
        # Which owner-row step stopped the read. Diagnostic only: not a score, not Counted,
        # and not a RegionRead field, so region rows and their evidence hashes are unchanged.
        rd.stage = stage
    return rd


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


def _wanted_clears(scores, want: int) -> bool:
    """The wanted template is the best, by the shared letter min and margin."""
    top = float(scores[want])
    if top < _LETTER_MIN:
        return False
    rest = [float(scores[i]) for i in range(len(scores)) if i != want]
    second = max(rest) if rest else -1.0
    return top - second >= _LETTER_MARGIN


def _victory_hits(img) -> list:
    """VICTORY rows that clear the shared letter, sharpness, and field gates.

    Each hit is the six wide letters. Location is checked by the caller.
    """
    cv2, np = _libs()
    pack = _pack()
    bgr = _as_bgr(img)
    if cv2 is None or pack is None or bgr is None or "victory" not in pack:
        return []
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
        return []
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
    hits = []
    for row in rows:
        found = _victory_row(row["items"])
        if not found:
            continue
        if _sharpness(bgr, found) < _SHARP_MIN:
            continue
        if _field_share(bgr, found, "victory") < _FIELD_MIN:
            continue
        hits.append(found)
    return hits


def _victory_row(items):
    np = _libs()[1]
    items = sorted(items, key=lambda g: g["x"])
    heights = [it["h"] for it in items]
    med = float(np.median(heights))
    if med < 8:
        return None
    items = [it for it in items if abs(it["h"] - med) / med <= 0.30]
    # V C T O R Y are template indexes 0,2,3,4,5,6. I is index 1 and is narrow.
    labeled = []
    for it in items:
        idx = int(np.argmax(it["scores"]))
        labeled.append((idx, float(it["scores"][idx]), it))
    # A false V earlier on the same band does not hide a later word.
    for j, (idx, _score, it) in enumerate(labeled):
        if idx == 0 and _wanted_clears(it["scores"], 0):
            found = _victory_from(labeled, j)
            if found:
                return found
    return None


def _victory_from(labeled, start_at: int):
    """One VICTORY chain starting at ``start_at``. None when the chain breaks."""
    np = _libs()[1]
    need = (0, 2, 3, 4, 5, 6)
    found = []
    start = start_at
    for want in need:
        hit = None
        for j in range(start, len(labeled)):
            idx, _score, it = labeled[j]
            if idx == want and _wanted_clears(it["scores"], want):
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

    The gradient is the 99th percentile of a 3x3 Sobel magnitude. Contrast
    is the gap from the 5th to the 98th percentile of the gray word box.
    A 3x3 Sobel peaks near eight times a one-pixel step, so the ratio is
    divided by 8. A clean splash and a clean header land near 0.45. A
    sigma-2 blur lands near 0.20. Below the floor the word does not agree.
    A low contrast returns 0, so an unreadable box does not agree.
    """
    cv2, np = _libs()
    box = _word_box(glyphs, bgr.shape)
    if cv2 is None or box is None:
        return 0.0
    x0, y0, x1, y1 = box
    gray = cv2.cvtColor(bgr[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY).astype(np.float32)
    lo, hi = np.percentile(gray, [5.0, 98.0])
    contrast = float(hi - lo)
    if contrast < 8.0:
        return 0.0
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    top = float(np.percentile(cv2.magnitude(gx, gy), 99.0))
    return top / contrast / 8.0


def _field_share(bgr, glyphs, kind: str) -> float:
    """Share of the word box that is this word's field color.

    ``red`` is DEFEAT (hue near 0). ``victory`` is the teal measured on the
    practice splash and header (OpenCV hue about 88). Saturation is at least 60
    either way, and the share has to clear the same floor.
    """
    cv2, np = _libs()
    box = _word_box(glyphs, bgr.shape)
    if cv2 is None or box is None:
        return 0.0
    x0, y0, x1, y1 = box
    hsv = cv2.cvtColor(bgr[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    hue, sat = hsv[:, :, 0], hsv[:, :, 1]
    if kind == "red":
        mask = ((hue <= 10) | (hue >= 170)) & (sat >= 60)
    elif kind == "victory":
        lo, hi = _VICTORY_HUE
        mask = (hue >= lo) & (hue <= hi) & (sat >= 60)
    else:
        return 0.0
    return float(np.mean(mask))


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
    # A glyph earlier on the same band can score as D. Try each D so the
    # header is still found when VICTORY sits on that band.
    for j, (ch, score, margin, _it) in enumerate(labeled):
        if ch == "D" and score >= _LETTER_MIN and margin >= _LETTER_MARGIN:
            found = _defeat_from(labeled, j)
            if found:
                return found
    return None


def _defeat_from(labeled, start_at: int):
    """One DEFEAT chain starting at ``start_at``. None when the chain breaks."""
    np = _libs()[1]
    found = []
    start = start_at
    for want in _DEFEAT_LETTERS:
        hit = None
        for j in range(start, len(labeled)):
            ch, score, margin, _it = labeled[j]
            if ch == want and score >= _LETTER_MIN and margin >= _LETTER_MARGIN:
                hit = j
                break
        if hit is None:
            return None
        found.append(labeled[hit])
        start = hit + 1
    scores = [sc for _, sc, _, _ in found]
    if float(np.mean(scores)) < _LETTER_MEAN:
        return None
    med_w = float(np.median([it["w"] for _, _, _, it in found]))
    xs = [it["x"] for _, _, _, it in found]
    span = xs[-1] - xs[0]
    if med_w < 1 or span < med_w * 4 or span > med_w * 14:
        return None
    return [it for _, _, _, it in found]


def _defeat_rows(pack):
    """Calibrated header rows. Row 0 is the solid cut. Later rows are thinner cuts.

    A word still has to clear the shared letter gate, the sharpness floor, and
    the red field. Another row does not relax those gates, and the glitched
    splash is not a row.
    """
    rows = pack["defeat"].astype(_libs()[1].float32)
    if rows.ndim != 3 or rows.shape[1] != len(_DEFEAT_LETTERS) or rows.shape[0] < 1:
        return None
    return rows


def _defeat_hits(img) -> list:
    """DEFEAT rows that clear the shared letter, sharpness, and red-field gates.

    Each hit is the six header letters. Solid letters match the first
    calibrated row. A thinner cut is joined, then matched to its own row, on
    those same gates. Location is checked by the caller. The centered splash
    is cut by scan lines, so it stays unread. A blurred word stays unread.
    Team totals are not home and away: the screen does not label those sides,
    and the receipt side is the side chosen at the start.
    """
    cv2, _np = _libs()
    pack = _pack()
    bgr = _as_bgr(img)
    if cv2 is None or pack is None or bgr is None or "defeat" not in pack:
        return []
    rows = _defeat_rows(pack)
    if rows is None:
        return []
    glyphs = _letter_glyphs(_white_mask(bgr))
    if len(glyphs) < 6:
        return []
    hits = []
    seen = set()
    for templates in rows:
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
            if _field_share(bgr, found, "red") < _FIELD_MIN:
                continue
            key = tuple((int(g["x"]), int(g["y"]), int(g["w"]), int(g["h"])) for g in found)
            if key in seen:
                continue
            seen.add(key)
            hits.append(found)
    return hits


def _crop_origin(img, name: str) -> tuple[int, int]:
    """Pixel origin of a normalized crop, using the same rounding as ``crop_box``."""
    h, w = int(img.shape[0]), int(img.shape[1])
    x, y, _bw, _bh = _region(name)
    x0 = max(0, min(w - 1, int(round(float(x) * w))))
    y0 = max(0, min(h - 1, int(round(float(y) * h))))
    return x0, y0


def _norm_word(glyphs, origin: tuple[int, int], shape) -> tuple[float, float, float, float] | None:
    """Letter box as fractions of the full frame. Glyphs are in crop pixels."""
    if not glyphs:
        return None
    ox, oy = origin
    h, w = int(shape[0]), int(shape[1])
    if h < 1 or w < 1:
        return None
    x0 = min(int(g["x"]) for g in glyphs) + ox
    y0 = min(int(g["y"]) for g in glyphs) + oy
    x1 = max(int(g["x"]) + int(g["w"]) for g in glyphs) + ox
    y1 = max(int(g["y"]) + int(g["h"]) for g in glyphs) + oy
    return (x0 / w, y0 / h, (x1 - x0) / w, (y1 - y0) / h)


def _inside(word, box, slack: float = 0.002) -> bool:
    """True when ``word`` sits inside ``box``. Both are (x, y, w, h) fractions."""
    if word is None:
        return False
    x, y, w, h = word
    bx, by, bw, bh = box
    return (x >= bx - slack and y >= by - slack
            and x + w <= bx + bw + slack and y + h <= by + bh + slack)


def _located(glyphs, origin, shape, boxes) -> bool:
    """Letter box inside the region, and close to that word's measured size."""
    word = _norm_word(glyphs, origin, shape)
    if word is None:
        return False
    for box in boxes:
        if not _inside(word, box[:4]):
            continue
        expect_w, expect_h = box[4], box[5]
        if abs(word[2] - expect_w) > expect_w * _WORD_SIZE_TOL:
            continue
        if abs(word[3] - expect_h) > expect_h * _WORD_SIZE_TOL:
            continue
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


def _board_scale(img):
    """720p and 1080p 16:9 frames. A short strip or a word crop is not the board."""
    h, w = int(img.shape[0]), int(img.shape[1])
    if h < 2 or w < 2:
        return None
    scale = h / 720.0
    if scale < 0.90 or scale > 1.60:
        return None
    if w / float(h) < 1.4:
        return None
    return scale


def _scaled_u8(mask, scale: float):
    cv2, np = _libs()
    if abs(scale - 1.0) < 0.02:
        return mask
    out = cv2.resize(mask, None, fx=float(scale), fy=float(scale), interpolation=cv2.INTER_AREA)
    return (out > 40).astype(np.uint8) * 255


def _value_mask(img, thr: int):
    cv2, np = _libs()
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    return (hsv[:, :, 2] > thr).astype(np.uint8) * 255


def _best_mask(search, mask, scale: float, thr: float):
    """Best template hit, or None under ``thr``. One scale, from the frame height."""
    cv2, np = _libs()
    if search is None or mask is None or cv2 is None:
        return None
    templ = _scaled_u8(mask, scale)
    if templ is None or templ.ndim != 2 or templ.shape[0] < 8 or templ.shape[1] < 8:
        return None
    if search.shape[0] < templ.shape[0] or search.shape[1] < templ.shape[1]:
        return None
    res = cv2.matchTemplate(search, templ, cv2.TM_CCOEFF_NORMED)
    score = float(res.max())
    if score < thr:
        return None
    y, x = np.unravel_index(int(res.argmax()), res.shape)
    return (score, int(x), int(y), int(templ.shape[1]), int(templ.shape[0]))


def _columns_share_x(hit, other, scale: float) -> bool:
    """True when two header boxes sit on the same column. A shared edge counts."""
    if hit is None or other is None:
        return False
    _score, x, _y, w, _h = hit
    _score2, ox, _y2, ow, _h2 = other
    slack = max(1, int(round(scale)))
    return x < ox + ow + slack and ox < x + w + slack


def _column_header(hit, row_x: int, row_y: int, scale: float):
    """A header above this row and on its side of the board. Far columns are not this row."""
    if hit is None:
        return None
    _score, x, y, w, h = hit
    if y + h > row_y + int(6 * scale):
        return None
    if row_y - y > int(200 * scale):
        return None
    if x + w < row_x:
        return None
    if x - row_x > int(520 * scale):
        return None
    return hit


def _best_column_hit(search, mask, scale: float, thr: float, row_x: int, row_y: int):
    """Highest template peak that sits in this row's column band.

    The global peak can be the same word on the personal card. That peak is
    not this column. The match gate is the same ``thr`` as the other headers.
    """
    cv2, np = _libs()
    if search is None or mask is None or cv2 is None:
        return None
    templ = _scaled_u8(mask, scale)
    if templ is None or templ.ndim != 2 or templ.shape[0] < 8 or templ.shape[1] < 8:
        return None
    if search.shape[0] < templ.shape[0] or search.shape[1] < templ.shape[1]:
        return None
    work = cv2.matchTemplate(search, templ, cv2.TM_CCOEFF_NORMED)
    height, width = int(templ.shape[0]), int(templ.shape[1])
    for _ in range(8):
        score = float(work.max())
        if score < thr:
            return None
        y, x = np.unravel_index(int(work.argmax()), work.shape)
        hit = (score, int(x), int(y), width, height)
        if _column_header(hit, row_x, row_y, scale) is not None:
            return hit
        y0, y1 = max(0, int(y) - 6), min(work.shape[0], int(y) + 7)
        x0, x1 = max(0, int(x) - 6), min(work.shape[1], int(x) + 7)
        work[y0:y1, x0:x1] = -1.0
    return None


def _personal_header(hit, row_x: int, row_y: int, row_h: int, scale: float):
    """A label below the row, on the personal card. A column header above the row is not this."""
    if hit is None:
        return None
    _score, x, y, _w, _h = hit
    if y < row_y + max(4, row_h // 2):
        return None
    if y - row_y > int(220 * scale):
        return None
    if x < row_x - int(40 * scale):
        return None
    if x - row_x > int(800 * scale):
        return None
    return hit


_ROW_GRAY_WEIGHT = 0.5  # share of the glyph's own gray patch in the feature; the rest is its ink mask
_ROW_BANK_CACHE: dict = {}


def _hex_rows(rows):
    """Hex text rows (two characters per pixel) to a uint8 array. None when empty or ragged."""
    np = _libs()[1]
    if np is None or not rows or any(len(r) != len(rows[0]) or len(r) % 2 for r in rows):
        return None
    try:
        return np.array([[int(r[i:i + 2], 16) for i in range(0, len(r), 2)] for r in rows], np.uint8)
    except ValueError:
        return None


def _row_feature(glyph, gray=None):
    """One glyph as a unit vector: its ink mask and its own gray patch (min-max), both 12x18.

    The gray half keeps the anti-aliased edge pixels that a single ink cut drops on a 9-pixel
    720p digit. Without a gray patch (a painted mask) the mask stands in for it.
    """
    np = _libs()[1]
    b = _vec(np, glyph, (12, 18))
    if b is None:
        return None
    # A painted mask has no gray patch: pad it by the same 1 pixel the reader keeps around a glyph.
    g = np.pad(glyph, 1) if gray is None else gray
    g = g.astype(np.float32)
    lo, hi = float(g.min()), float(g.max())
    if hi - lo < 1.0:
        return None
    gv = _vec(np, (g - lo) / (hi - lo), (12, 18))
    if gv is None:
        return None
    v = np.concatenate([b * np.sqrt(1.0 - _ROW_GRAY_WEIGHT), gv * np.sqrt(_ROW_GRAY_WEIGHT)])
    n = float(np.linalg.norm(v))
    return None if n < 1e-6 else (v / n).astype(np.float32)


def _row_bank_samples(path=None):
    """(label, mask, gray, provenance) from the v1 text masks and the v2 multi-sample bank."""
    out = []
    if TEXT_PACK_PATH.is_file():
        for item in json.loads(TEXT_PACK_PATH.read_text(encoding="utf-8")).get("digits", []):
            m = _mask_from_rows(item.get("mask_rows"))
            if m is not None:
                out.append((str(item["label"]), m, None, "v1:" + str(item.get("provenance"))))
    path = ROW_BANK_PATH if path is None else Path(path)
    if path.is_file():
        for item in json.loads(path.read_text(encoding="utf-8")).get("samples", []):
            m = _mask_from_rows(item.get("mask_rows"))
            g = _hex_rows(item.get("gray_rows"))
            if m is not None and str(item.get("label", "")) in "0123456789" and len(str(item.get("label", ""))) == 1:
                out.append((str(item["label"]), m, g, str(item.get("provenance"))))
    return out


def _row_digit_bank(path=None):
    """Feature vectors and labels for the scoreboard digits (cached per bank file and mtime)."""
    np = _libs()[1]
    if np is None:
        return None
    p = ROW_BANK_PATH if path is None else Path(path)
    key = (str(p), p.stat().st_mtime_ns if p.is_file() else None,
           TEXT_PACK_PATH.stat().st_mtime_ns if TEXT_PACK_PATH.is_file() else None)
    if key in _ROW_BANK_CACHE:
        return _ROW_BANK_CACHE[key]
    labels, vecs = [], []
    for label, mask, gray, _prov in _row_bank_samples(p):
        v = _row_feature(mask, gray)
        if v is not None:
            labels.append(label)
            vecs.append(v)
    bank = (np.stack(vecs), labels) if vecs else None
    _ROW_BANK_CACHE.clear()
    _ROW_BANK_CACHE[key] = bank
    return bank


def _row_classify(vecs, labels, glyph, gray=None):
    """(label, top, second) for one glyph. label is None unless both gates pass."""
    v = _row_feature(glyph, gray)
    if v is None:
        return None, 0.0, 0.0
    scores = vecs @ v
    by: dict[str, float] = {}
    for i, name in enumerate(labels):
        score = float(scores[i])
        if name not in by or score > by[name]:
            by[name] = score
    ranked = sorted(by.items(), key=lambda kv: -kv[1])
    best, top = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else -1.0
    if top < _ROW_DIGIT_MIN or top - second < _ROW_DIGIT_MARGIN:
        return None, top, second
    return best, top, second


def _row_cell_thr(v, thr: int) -> int | None:
    """Ink cut for one cell: Otsu on the cell's value channel, kept within ``thr`` -40..+30.

    A fixed cut thinned the 3 on a highlighted 720p row (practice 194037) until it no longer
    matched. None when the cell has no ink contrast (nothing to read).
    """
    cv2, np = _libs()
    if v.size == 0 or int(v.max()) - int(np.median(v)) < _ROW_MIN_CONTRAST:
        return None
    t, _ = cv2.threshold(v, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return int(min(thr + 30, max(thr - 40, int(t))))


def _row_glyph_pieces(img, x0: int, x1: int, y0: int, y1: int, thr: int, scale: float):
    """Glyph masks in one cell, left to right, or None.

    None when there is no ink, too many glyphs, or ink that is not a glyph and not a speck:
    a piece of 8+ pixels or 4+ rows (or loose pieces of 8+ pixels together) that could not be joined to a glyph, or a glyph-height
    piece wider than one digit. Dropping such a piece silently turned a 13 into a 1 (practice
    194037), and a faded 1 left a 21 reading as 2.
    """
    cv2, np = _libs()
    bgr = _as_bgr(img)
    if cv2 is None or bgr is None:
        return None
    h, w = bgr.shape[:2]
    x0, x1 = max(0, int(x0)), min(w, int(x1))
    y0, y1 = max(0, int(y0)), min(h, int(y1))
    if x1 - x0 < 3 or y1 - y0 < 3:
        return None
    hsv = cv2.cvtColor(bgr[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    cut = _row_cell_thr(hsv[:, :, 2], thr)
    if cut is None:
        return None
    mask = (hsv[:, :, 2] > cut).astype(np.uint8) * 255
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    minh = max(5, int(round(6 * scale)))
    maxw = max(10, int(round(18 * scale)))
    tallh = max(4, int(round(4 * scale)))
    kept = []
    frags = []
    for i in range(1, n):
        x, y, ww, hh, area = (int(t) for t in st[i])
        if ww > maxw and hh >= minh and area >= 8:
            return None  # wider than one digit at glyph height: merged or not a digit
        if ww > maxw or hh < 1:
            continue
        g = (lab[y:y + hh, x:x + ww] == i).astype(np.uint8) * 255
        piece = {"x": x, "y": y, "w": ww, "h": hh, "area": area, "glyph": g}
        if ww >= 2 and area >= 8 and hh >= minh:
            kept.append(piece)
        elif 3 <= area < 8 or (area >= 3 and hh < minh) or hh >= tallh:
            # Half a faint 1 is one or two pixels wide but half a glyph tall: it is ink, not a speck.
            frags.append(piece)
    # A bright cut can split one glyph. Join a short piece only when it sits
    # on the same x span, within two pixels, and does not widen that span.
    # A piece that is not there stays out, so a cut glyph is not completed
    # with invented ink. A speck beside the glyph is not joined.
    span_tol = max(1, int(round(scale)))
    for frag in frags:
        frag["joined"] = False
        for host in kept:
            overlap = min(host["x"] + host["w"], frag["x"] + frag["w"]) - max(host["x"], frag["x"])
            if overlap < max(2, frag["w"] // 2):
                continue
            below = frag["y"] - (host["y"] + host["h"])
            above = host["y"] - (frag["y"] + frag["h"])
            if not (0 <= below <= 2 or 0 <= above <= 2):
                continue
            if frag["x"] < host["x"] - span_tol or frag["x"] + frag["w"] > host["x"] + host["w"] + span_tol:
                continue
            # A cut piece covers the glyph. A narrower speck is not that piece.
            if frag["w"] < host["w"] - span_tol:
                continue
            x0b = min(host["x"], frag["x"])
            y0b = min(host["y"], frag["y"])
            x1b = max(host["x"] + host["w"], frag["x"] + frag["w"])
            y1b = max(host["y"] + host["h"], frag["y"] + frag["h"])
            if x1b - x0b > host["w"] + span_tol or x1b - x0b > maxw:
                continue
            if any(
                other is not host and x0b < other["x"] + other["w"] and other["x"] < x1b
                for other in kept
            ):
                continue
            canvas = np.zeros((y1b - y0b, x1b - x0b), np.uint8)
            canvas[host["y"] - y0b:host["y"] - y0b + host["h"], host["x"] - x0b:host["x"] - x0b + host["w"]] = host["glyph"]
            canvas[frag["y"] - y0b:frag["y"] - y0b + frag["h"], frag["x"] - x0b:frag["x"] - x0b + frag["w"]] = np.maximum(
                canvas[frag["y"] - y0b:frag["y"] - y0b + frag["h"], frag["x"] - x0b:frag["x"] - x0b + frag["w"]],
                frag["glyph"],
            )
            host["x"], host["y"], host["w"], host["h"] = x0b, y0b, x1b - x0b, y1b - y0b
            host["glyph"] = canvas
            frag["joined"] = True
            break
    loose = [frag for frag in frags if not frag["joined"]]
    if any(frag["area"] >= 8 or frag["h"] >= tallh for frag in loose):
        return None
    if not kept or len(kept) > _ROW_GLYPH_MAX:
        return None
    # Several small pieces together are a faded glyph, not one speck (practice 162225 '13'
    # with its 3 faded). The bar grows with the glyph size so a large card glyph's fringe is not one.
    if sum(frag["area"] for frag in loose) >= max(8, 0.35 * max(piece["area"] for piece in kept)):
        return None
    # Every digit of this font is the same height. A shorter glyph is a digit that lost ink.
    tall = max(piece["h"] for piece in kept)
    if any(tall - piece["h"] > max(2, int(round(0.2 * tall))) for piece in kept):
        return None
    v = hsv[:, :, 2]
    bgv = float(np.median(v))
    # Every digit of one value is drawn in the same ink. A glyph much dimmer than the brightest
    # one is a faded digit whose cut kept only part of it (a faded 4 kept as its stem reads 1).
    peaks = [float(v[p["y"]:p["y"] + p["h"], p["x"]:p["x"] + p["w"]].max()) - bgv for p in kept]
    if min(peaks) < _ROW_INK_RATIO * max(peaks):
        return None
    # A whole digit fainter than its neighbours falls under the cut and leaves no piece, so the
    # value would lose that digit. Look again at a lower cut on the local background (a 5x5
    # opening removes thin strokes and keeps a highlighted row's gradient): a glyph-like piece
    # there that is beside every kept glyph is a missing digit.
    k = max(5, int(round(5 * scale)))
    lift = v.astype(np.float32) - cv2.morphologyEx(v, cv2.MORPH_OPEN, np.ones((k, k), np.uint8)).astype(np.float32)
    peak = max(float(lift[p["y"]:p["y"] + p["h"], p["x"]:p["x"] + p["w"]].max()) for p in kept)
    low = (lift > max(_ROW_LOW_MIN, _ROW_LOW_FRAC * peak)).astype(np.uint8)
    # Only the glyph band: a label above the digits (the personal card) is not a digit.
    band0 = max(0, min(p["y"] for p in kept) - 1)
    band1 = max(p["y"] + p["h"] for p in kept) + 1
    low[:band0] = 0
    low[band1:] = 0
    # Take out each kept glyph's own ink, grown by one pixel for its edge.
    own = np.zeros_like(low)
    for p in kept:
        own[p["y"]:p["y"] + p["h"], p["x"]:p["x"] + p["w"]] |= (p["glyph"] > 0).astype(np.uint8)
    low[cv2.dilate(own, np.ones((5, 5), np.uint8)) > 0] = 0
    ln, _llab, lst, _ = cv2.connectedComponentsWithStats(low, connectivity=8)
    for i in range(1, ln):
        _lx, _ly, _lw, lh, la = (int(t) for t in lst[i])
        if lh >= tallh and la >= _ROW_LOW_AREA:
            return None
    comps = []
    for piece in sorted(kept, key=lambda item: item["x"]):
        gx0, gy0 = max(0, piece["x"] - 1), max(0, piece["y"] - 1)
        gx1, gy1 = min(v.shape[1], piece["x"] + piece["w"] + 1), min(v.shape[0], piece["y"] + piece["h"] + 1)
        comps.append((piece["x"], piece["glyph"], v[gy0:gy1, gx0:gx1].copy()))
    return comps


def _row_digit_string(img, x0: int, x1: int, y0: int, y1: int, thr: int, scale: float, bank=None) -> str | None:
    """Digits in one column, any length up to _ROW_GLYPH_MAX. None when any glyph is missing,
    below the score gate, or too close to another digit. An unread value stays unread."""
    np = _libs()[1]
    bank = _row_digit_bank() if bank is None else bank
    if np is None or bank is None:
        return None
    vecs, labels = bank
    comps = _row_glyph_pieces(img, x0, x1, y0, y1, thr, scale)
    if comps is None:
        return None
    text = ""
    for _x, glyph, gray in comps:
        best, _top, _second = _row_classify(vecs, labels, glyph, gray)
        if best is None:
            return None
        text += best
    return text if text.isdigit() else None


def _stat_int(text: str | None) -> int | None:
    if not text or not text.isdigit() or len(text) > _ROW_GLYPH_MAX:
        return None
    return int(text)


def _digits_for_column(img, hit, row_y: int, row_h: int, scale: float) -> int | None:
    _score, x, _y, w, _h = hit
    text = _row_digit_string(
        img, x - int(4 * scale), x + w + int(8 * scale),
        row_y - int(2 * scale), row_y + row_h + int(6 * scale), 130, scale)
    return _stat_int(text)


def _digits_under_label(img, hit, scale: float) -> int | None:
    _score, x, y, w, h = hit
    text = _row_digit_string(
        img, x - int(8 * scale), x + w + int(12 * scale),
        y + h, y + h + int(28 * scale), 130, scale)
    return _stat_int(text)


def _owner_row_header_hits(bgr, scale, pack, row_x, row_y, row_h):
    """Column headers for this row.

    The TDM deaths word is a column only when it does not sit on the kills
    header. The same x is not a second stat. The personal-card deaths label
    is separate and stays below the row.
    """
    header_search = _value_mask(bgr, _HEADER_SEARCH_VALUE)
    found = {}
    for key, label in (
        ("kills_header_mask", "KILLS"),
        ("deaths_header_mask", "DEATHS"),
        ("confirms_header_mask", "CONFIRMS"),
    ):
        if key not in pack:
            continue
        hit = _best_mask(header_search, pack[key], scale, _ROW_HEADER_MIN)
        if hit is not None:
            found[label] = hit
    kills_h = _column_header(found.get("KILLS"), row_x, row_y, scale)
    assists_h = None
    if "assists_header_mask" in pack:
        assists_h = _best_column_hit(
            _value_mask(bgr, _DERIVED_HEADER_VALUE), pack["assists_header_mask"],
            scale, _ROW_HEADER_MIN, row_x, row_y)
    confirms_h = _column_header(found.get("CONFIRMS"), row_x, row_y, scale)
    deaths_h = None
    if "deaths_column_header_mask" in pack:
        deaths_h = _best_column_hit(
            _value_mask(bgr, _DERIVED_HEADER_VALUE), pack["deaths_column_header_mask"],
            scale, _ROW_HEADER_MIN, row_x, row_y)
    if _columns_share_x(deaths_h, kills_h, scale):
        deaths_h = None
    if deaths_h is None:
        deaths_h = _column_header(found.get("DEATHS"), row_x, row_y, scale)
    if _columns_share_x(deaths_h, kills_h, scale):
        deaths_h = None
    personal = None
    if deaths_h is None:
        personal = _personal_header(found.get("DEATHS"), row_x, row_y, row_h, scale)
    return kills_h, deaths_h, assists_h, confirms_h, personal


def read_owner_row(img):
    """Qortrola30's end-scoreboard row.

    The gamertag letters have to agree. Kills come from the KILLS column on
    that row. Deaths come from a DEATHS column on that row, or from the DEATHS
    label on the personal card below it when the row has no deaths column.
    Assists are read only when that column is on the board. Confirms are not
    assists. A missing row, a different tag, or an unreadable kill or death
    is not read. This does not fill home or away.
    """
    from qorcap_cod import OWNER_TAG
    bgr = _as_bgr(img)
    if bgr is None:
        return _not_read("owner_row", "frame_invalid", "frame could not be cropped; it is not Counted", "frame_invalid")
    scale = _board_scale(bgr)
    pack = _pack()
    if scale is None or pack is None or "owner_mask" not in pack:
        return _not_read(
            "owner_row", "not_present",
            "Qortrola30's scoreboard row was not read. It is not Counted.",
            "board_scale" if scale is None else "pack_missing")
    name_hit = _best_mask(_value_mask(bgr, _OWNER_SEARCH_VALUE), pack["owner_mask"], scale, _OWNER_MIN)
    if name_hit is None:
        return _not_read(
            "owner_row", "not_present",
            "Qortrola30's scoreboard row was not read. It is not Counted.", "tag_not_matched")
    _name_score, row_x, row_y, _row_w, row_h = name_hit
    kills_h, deaths_h, assists_h, confirms_h, personal = _owner_row_header_hits(
        bgr, scale, pack, row_x, row_y, row_h)
    if kills_h is None:
        return _not_read(
            "owner_row", "not_present",
            "Kills were not read on Qortrola30's row. They are not Counted.", "kills_header_not_found")
    kills = _digits_for_column(bgr, kills_h, row_y, row_h, scale)
    if kills is None:
        return _not_read(
            "owner_row", "not_present",
            "Kills were not read on Qortrola30's row. They are not Counted.", "kills_not_read")
    assists = None
    if assists_h is not None:
        assists = _digits_for_column(bgr, assists_h, row_y, row_h, scale)
        if assists is None:
            return _not_read(
                "owner_row", "not_present",
                "Assists are on the board and were not read. They are not Counted.", "assists_not_read")
    confirms = None
    if confirms_h is not None:
        confirms = _digits_for_column(bgr, confirms_h, row_y, row_h, scale)
    if deaths_h is not None:
        deaths = _digits_for_column(bgr, deaths_h, row_y, row_h, scale)
    else:
        deaths = _digits_under_label(bgr, personal, scale) if personal is not None else None
    if deaths is None:
        return _not_read(
            "owner_row", "not_present",
            "Deaths were not read for Qortrola30. They are not Counted.", "deaths_not_read")
    return _present(
        "owner_row", "owner_row",
        "Qortrola30's scoreboard row was read. Kills and deaths are that row, not a home or away score.",
        kills=kills, deaths=deaths, assists=assists, confirms=confirms, player_tag=OWNER_TAG,
    )


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
    origin = _crop_origin(img, "end_screen")
    shape = getattr(img, "shape", bgr.shape)
    victory = [g for g in _victory_hits(bgr) if _located(g, origin, shape, VICTORY_WORD_BOXES)]
    defeat = [g for g in _defeat_hits(bgr) if _located(g, origin, shape, (DEFEAT_WORD_BOX,))]
    # Both words are evaluated. A frame that agrees as both is not read.
    if victory and defeat:
        return _not_read(
            "end_screen", "ambiguous_result",
            "VICTORY and DEFEAT both agree on this frame, so it is not read.")
    if victory:
        return _present(
            "end_screen", "victory",
            "VICTORY on the end screen. Home and away were not read on this hook.",
            confirm=True)
    if defeat:
        return _present(
            "end_screen", "defeat",
            "DEFEAT on the end screen. Home and away were not read on this hook.",
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

    end = cv2.imread(str(fixture_dir / "end_173922_019.jpg"))
    if end is None:
        raise RuntimeError("missing end_173922_019.jpg")
    end_v = cv2.cvtColor(end, cv2.COLOR_BGR2HSV)[:, :, 2]
    owner_mask = (end_v[534:550, 172:238] > 150).astype(np.uint8) * 255
    kills_header_mask = (end_v[400:417, 427:456] > 80).astype(np.uint8) * 255
    confirms_header_mask = (end_v[400:414, 527:577] > 80).astype(np.uint8) * 255
    deaths_header_mask = (end_v[632:640, 758:791] > 120).astype(np.uint8) * 255
    if min(owner_mask.mean(), kills_header_mask.mean(), deaths_header_mask.mean()) < 10:
        raise RuntimeError("owner row templates are empty")
    # Measured glyph x on the 720p frame. The second 2 of 2245 is dimmer and is not a sample.
    row_specs = (
        (530, 552, 300, 580, 145, ((331, "2"), (345, "4"), (352, "5"), (432, "1"), (439, "3"), (534, "7"))),
        (645, 670, 580, 1000, 120, ((628, "1"), (636, "7"), (768, "1"), (776, "3"))),
    )
    row_labels: list[str] = []
    row_vecs = []
    for y0, y1, x0, x1, thr, spec in row_specs:
        mask = (end_v[y0:y1, x0:x1] > thr).astype(np.uint8) * 255
        n, lab, st, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        got = set()
        for i in range(1, n):
            x, y, ww, hh, area = (int(t) for t in st[i])
            if area < 8 or hh < 6 or ww < 2:
                continue
            name = None
            for kx, label in spec:
                if abs((x + x0) - kx) <= 4:
                    name = label
                    break
            if name is None:
                continue
            g = (lab[y:y + hh, x:x + ww] == i).astype(np.uint8) * 255
            vec = _vec(np, g, (12, 18))
            if vec is None:
                raise RuntimeError(f"empty digit glyph {name} at {x + x0}")
            row_labels.append(name)
            row_vecs.append(vec)
            got.add(name)
        missing = {label for _kx, label in spec} - got
        if missing:
            raise RuntimeError(f"missing owner-row digits {sorted(missing)}")
    row_digit_vecs = np.stack(row_vecs).astype(np.float32)

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
        owner_mask=owner_mask,
        kills_header_mask=kills_header_mask,
        confirms_header_mask=confirms_header_mask,
        deaths_header_mask=deaths_header_mask,
        row_digit_labels=np.array([s.encode("ascii") for s in row_labels]),
        row_digit_vecs=row_digit_vecs,
    )


# PROVISIONAL. A fade or a black frame is not a real end-board frame.
REAL_MEAN_MIN = 18.0
FADE_STD_MAX = 8.0
FADE_MEAN_MAX = 40.0
CONSENSUS_FRAMES = 5


def frame_is_real(img) -> bool:
    """False for a missing, black, or faded frame. A non-array does not raise."""
    bgr = _as_bgr(img)
    if bgr is None:
        return False
    mean = float(bgr.mean())
    if mean < REAL_MEAN_MIN:
        return False
    if mean < FADE_MEAN_MAX and float(bgr.std()) < FADE_STD_MAX:
        return False
    return True


def _frame_parts(item):
    if isinstance(item, dict) and "img" in item:
        return item.get("img"), item
    return item, None


def _pixel_id(img) -> str | None:
    try:
        import hashlib
        import numpy as np
        if not isinstance(img, np.ndarray):
            return None
        return hashlib.sha256(np.ascontiguousarray(img).tobytes()).hexdigest()
    except Exception:
        return None


def _feed_check(feed, kills: int) -> str:
    """The feed is a consistency check. An unknown tally does not decide the digits."""
    if not isinstance(feed, dict):
        return "unknown"
    if feed.get("coverage") != "covered" or feed.get("ownership_qualified") is not True or feed.get("deduped") is not True:
        return "unknown"
    counted = feed.get("kills")
    if isinstance(counted, bool) or not isinstance(counted, int):
        return "unknown"
    if counted != kills:
        return "inconsistent"
    return "consistent"


def consensus_owner_row(frames, feed=None) -> dict:
    """Five distinct captured frames must read the same kills and deaths.

    A repeated image, a missing capture time, or a missing source id abstains.
    The own-kill feed does not supply the digits. It can only disagree when it
    is ownership-qualified, deduplicated, and covered. The single-frame reader
    is unchanged.
    """
    real = []
    for item in frames or []:
        img, meta = _frame_parts(item)
        if frame_is_real(img):
            real.append((img, meta or {}))
    tail = real[-CONSENSUS_FRAMES:]
    empty = {"status": "abstain", "kills": None, "deaths": None, "feed_check": "unknown"}
    if len(tail) < CONSENSUS_FRAMES:
        return {**empty, "reason": "short_real_window"}
    ident = []
    for img, meta in tail:
        pixel = meta.get("pixel_id") if isinstance(meta.get("pixel_id"), str) and meta.get("pixel_id") else _pixel_id(img)
        source = meta.get("source_id") if isinstance(meta.get("source_id"), str) else meta.get("frame_id")
        captured = meta.get("captured_us")
        if not pixel or not isinstance(source, str) or not source:
            return {**empty, "reason": "no_capture_provenance"}
        if isinstance(captured, bool) or not isinstance(captured, int):
            return {**empty, "reason": "no_capture_provenance"}
        ident.append((pixel, source, captured))
    if len({row[0] for row in ident}) < CONSENSUS_FRAMES or len({row[1] for row in ident}) < CONSENSUS_FRAMES:
        return {**empty, "reason": "repeated_frame"}
    times = [row[2] for row in ident]
    if any(later <= earlier for earlier, later in zip(times, times[1:])):
        return {**empty, "reason": "repeated_frame"}
    reads = [read_owner_row(img) for img, _meta in tail]
    if not all(r.ok and r.calibrated and r.kills is not None and r.deaths is not None for r in reads):
        return {**empty, "reason": "row_not_read"}
    kills = {r.kills for r in reads}
    deaths = {r.deaths for r in reads}
    if len(kills) != 1 or len(deaths) != 1:
        return {**empty, "reason": "frames_disagree"}
    k = next(iter(kills))
    d = next(iter(deaths))
    check = _feed_check(feed, int(k))
    if check == "inconsistent":
        return {"status": "abstain", "reason": "feed_inconsistent", "kills": k, "deaths": d, "feed_check": check}
    last = reads[-1]
    return {
        "status": "accepted", "reason": "five_frame_consensus",
        "kills": k, "deaths": d, "assists": last.assists, "confirms": last.confirms,
        "player_tag": last.player_tag, "feed_check": check,
    }
