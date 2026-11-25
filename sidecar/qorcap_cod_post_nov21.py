"""Info-only post-Nov-21 CoD HUD readers and fixed Center-Box Trigger.

Every reader in this module is strictly informational (``info_only=True``,
``counted=False``). Nothing here writes to receipts, contracts, or hand
timelines, and nothing claims skill verification or controller authorship.

Key fixes for post-Nov-21 Large-Text and bright-scene captures:
* Stationary fixed Center-Box Trigger (no center-locking, no wide-crop red X):
  diagonal 45-degree symmetry separates white ``HIT`` ticks and red ``KILL``
  markers from vertical sights, cardinal hip-fire ticks, flashbangs, and yellow
  XP text.
* CoD-font digit decoder (``0-9``, ``0-100+``) unifying existing gray-edge
  scoreboard prototypes with ``cod_killfeed_font.json`` digit glyphs for
  magazine/reserve ammo, live team scores, XP values, and scoreboard cells.
* Post-Nov-21 yellow XP popup detector (``210 XP`` style).
* Post-Nov-21 multi-scale scoreboard owner-row + personal-card reader (including
  ``CAPTURES`` / ``OBJ. TIME`` boards without a ``DEATHS`` column).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

INFO_ONLY = True
COUNTED = False

REF_H = 720.0
CENTER_BOX_HALF_720 = 30
ARM_MIN_PX_720 = 5
OFF_AXIS_FLOOD_MAX = 0.25
FLASH_WHITE_FRAC_MAX = 0.45
FLASH_RED_FRAC_MAX = 0.42
STATIC_SUPPRESS_FRAMES = 4
STATIC_JACCARD_MIN = 0.85

DIGITS_MIN_SCORE = 0.56
DIGITS_MIN_MARGIN = 0.04
DIGIT_MIN_CONTRAST = 32

_BOX_MASK_CACHE: dict = {}
_DIGIT_BANK_CACHE: dict = {}
_TMPL_CACHE: dict = {}


def _scaled_f32_mask(cv2, np, mask, sc: float, cache_key: str | None = None):
    key = None if cache_key is None else (cache_key, round(float(sc), 4))
    if key is not None and key in _TMPL_CACHE:
        return _TMPL_CACHE[key]
    templ = cv2.resize(mask, None, fx=float(sc), fy=float(sc), interpolation=cv2.INTER_AREA).astype(np.float32)
    if key is not None:
        _TMPL_CACHE[key] = templ
    return templ


def _libs():
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None, None
    return cv2, np


def _as_bgr(img):
    _cv2, np = _libs()
    if np is None or not isinstance(img, np.ndarray):
        return None
    if img.ndim != 3 or img.shape[2] < 3 or img.dtype != np.uint8:
        return None
    if img.shape[0] < 120 or img.shape[1] < 200:
        return None
    return img[:, :, :3]


def _info(payload: dict) -> dict:
    out = dict(payload)
    out["info_only"] = INFO_ONLY
    out["counted"] = COUNTED
    return out


def _tophat_gray(cv2, np, gray, k: int):
    kk = max(5, int(k) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kk, kk))
    return cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, kernel)


def center_box_for_shape(height: int, width: int) -> dict | None:
    """Stationary fixed center ROI bounds and pixel ratios for any frame size."""
    if height < 120 or width < 200:
        return None
    scale = height / REF_H
    half = max(12, int(round(CENTER_BOX_HALF_720 * scale)))
    cx, cy = width // 2, height // 2
    x0 = max(0, cx - half)
    y0 = max(0, cy - half)
    x1 = min(width, cx + half)
    y1 = min(height, cy + half)
    roi_w, roi_h = x1 - x0, y1 - y0
    roi_px = roi_w * roi_h
    frame_px = width * height
    return {
        "x0": int(x0),
        "y0": int(y0),
        "x1": int(x1),
        "y1": int(y1),
        "w": int(roi_w),
        "h": int(roi_h),
        "scale": round(scale, 4),
        "roi_px": int(roi_px),
        "frame_px": int(frame_px),
        "area_pct": round(100.0 * roi_px / max(1, frame_px), 4),
    }


def _center_masks(np, h: int, w: int, scale: float):
    key = (h, w, round(scale, 3))
    if key in _BOX_MASK_CACHE:
        return _BOX_MASK_CACHE[key]
    cy, cx = h // 2, w // 2
    y_grid, x_grid = np.ogrid[-cy:h - cy, -cx:w - cx]
    radius = np.hypot(x_grid, y_grid)
    r_lo = max(4.0, 6.0 * scale)
    r_hi = max(12.0, 26.0 * scale)
    diag_band = (radius >= r_lo) & (radius <= r_hi) & (np.abs(np.abs(x_grid) - np.abs(y_grid)) <= max(2.5, 4.0 * scale))
    diag_quads = (
        diag_band & (x_grid < 0) & (y_grid < 0),
        diag_band & (x_grid > 0) & (y_grid < 0),
        diag_band & (x_grid < 0) & (y_grid > 0),
        diag_band & (x_grid > 0) & (y_grid > 0),
    )
    c_lo = max(5.0, 7.0 * scale)
    c_tol = max(2.0, 3.0 * scale)
    card_n = (radius >= c_lo) & (radius <= r_hi) & (np.abs(x_grid) <= c_tol) & (y_grid < 0) & (~diag_band)
    card_s = (radius >= c_lo) & (radius <= r_hi) & (np.abs(x_grid) <= c_tol) & (y_grid > 0) & (~diag_band)
    card_w = (radius >= c_lo) & (radius <= r_hi) & (np.abs(y_grid) <= c_tol) & (x_grid < 0) & (~diag_band)
    card_e = (radius >= c_lo) & (radius <= r_hi) & (np.abs(y_grid) <= c_tol) & (x_grid > 0) & (~diag_band)
    card_quads = (card_n, card_s, card_w, card_e)
    card_union = card_n | card_s | card_w | card_e
    dot_mask = radius <= max(2.5, 4.5 * scale)
    outer_ignore = ~(diag_band | card_union | dot_mask)
    val = (diag_quads, diag_band, card_quads, card_union, dot_mask, outer_ignore)
    _BOX_MASK_CACHE[key] = val
    return val


def _mask_jaccard(np, a, b) -> float:
    inter = int((a & b).sum())
    union = int((a | b).sum())
    return 0.0 if union == 0 else inter / float(union)


class CenterBoxTrigger:
    """Fixed center-ROI state detector with temporal baseline tracking.

    Detects diagonal ``HIT`` (white) and ``KILL`` (red) hitmarkers inside a
    stationary box centered on the crosshair dot. Rejects flashes, explosions,
    and off-center red blobs with ``ABSTAIN``.
    """

    def __init__(self, history_len: int = 6):
        self._history: deque = deque(maxlen=max(2, int(history_len)))

    def reset(self) -> None:
        self._history.clear()

    def analyze_frame(self, img, *, prior: dict | None = None) -> dict:
        cv2, np = _libs()
        bgr = _as_bgr(img)
        if cv2 is None or bgr is None:
            return _info({"ok": False, "state": "ABSTAIN", "reason": "frame_invalid", "triggered": False})
        H, W = bgr.shape[:2]
        roi = center_box_for_shape(H, W)
        if roi is None:
            return _info({"ok": False, "state": "ABSTAIN", "reason": "layout_unknown", "triggered": False})
        scale = H / REF_H
        box = bgr[roi["y0"]:roi["y1"], roi["x0"]:roi["x1"]]
        hsv = cv2.cvtColor(box, cv2.COLOR_BGR2HSV)
        hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]

        white_flood = float(((sat < 85) & (val > 205)).mean())
        red_mask = ((hue <= 12) | (hue >= 168)) & (sat > 100) & (val > 90)
        red_flood = float(red_mask.mean())
        if white_flood > FLASH_WHITE_FRAC_MAX:
            return _info({
                "ok": False, "state": "ABSTAIN", "reason": "white_flash_saturation",
                "triggered": False, "roi": roi, "white_flood": round(white_flood, 3),
                "_box_val": val, "_red_diag": None, "_card_arms": 0, "_state": "ABSTAIN",
            })
        if red_flood > FLASH_RED_FRAC_MAX:
            return _info({
                "ok": False, "state": "ABSTAIN", "reason": "red_splash_flood",
                "triggered": False, "roi": roi, "red_flood": round(red_flood, 3),
                "_box_val": val, "_red_diag": None, "_card_arms": 0, "_state": "ABSTAIN",
            })

        if prior is not None and prior.get("_box_val") is not None and prior["_box_val"].shape == val.shape:
            pval = prior["_box_val"]
            mean_jump = abs(float(val.mean()) - float(pval.mean()))
            diff_frac = float((np.abs(val.astype(np.int16) - pval.astype(np.int16)) > 55).mean())
            if mean_jump > 70.0 and diff_frac > 0.65:
                return _info({
                    "ok": False, "state": "ABSTAIN", "reason": "sudden_flash_jump",
                    "triggered": False, "roi": roi, "mean_jump": round(mean_jump, 1),
                    "_box_val": val, "_red_diag": None, "_card_arms": 0, "_state": "ABSTAIN",
                })

        diag_quads, diag_band, card_quads, _cu, dot_mask, outer_ignore = _center_masks(
            np, box.shape[0], box.shape[1], scale,
        )
        arm_min = max(2, int(round(ARM_MIN_PX_720 * scale * scale)))
        red_diag_mask = red_mask & diag_band
        red_counts = [int((red_mask & q).sum()) for q in diag_quads]
        red_arms = sum(c >= arm_min for c in red_counts)
        red_off = float((red_mask & (~diag_band)).mean())

        k = max(5, int(round(11 * scale)) | 1)
        blur = cv2.GaussianBlur(val, (k, k), 0)
        lift = val.astype(np.int16) - blur.astype(np.int16)
        white_ink = (sat < 90) & (val > 160) & (lift > 35)
        white_counts = [int((white_ink & q).sum()) for q in diag_quads]
        white_arms = sum(c >= arm_min for c in white_counts)
        white_off = float((white_ink & outer_ignore).mean())

        if white_off > OFF_AXIS_FLOOD_MAX:
            return _info({
                "ok": False, "state": "ABSTAIN", "reason": "white_clutter_center",
                "triggered": False, "roi": roi, "white_off": round(white_off, 3),
                "_box_val": val, "_red_diag": red_diag_mask, "_card_arms": 0, "_state": "ABSTAIN",
            })

        card_min = max(2, int(round(3 * scale * scale)))
        card_arms = sum(int((white_ink & q).sum()) >= card_min for q in card_quads)
        dot_px = int((white_ink & dot_mask).sum())

        state = "NONE"
        reason = "no_marker"
        if red_arms >= 3 and sum(red_counts) >= 4 * arm_min and red_off <= OFF_AXIS_FLOOD_MAX:
            state = "KILL"
            reason = "center_red_diagonal_marker"
        elif white_arms >= 3 and sum(white_counts) >= 4 * arm_min and red_arms < 2:
            state = "HIT"
            reason = "center_white_diagonal_marker"
        elif prior is not None and prior.get("_card_arms", 0) >= 2 and card_arms <= 1 and dot_px >= 1:
            state = "ADS_OR_FIRING"
            reason = "cardinal_ticks_retracted"
        elif dot_px >= 2 and card_arms == 0:
            state = "ADS_OR_FIRING"
            reason = "center_dot_no_hipfire_ticks"

        return _info({
            "ok": state in ("HIT", "KILL", "ADS_OR_FIRING", "NONE"),
            "state": state,
            "reason": reason,
            "triggered": state in ("HIT", "KILL"),
            "roi": roi,
            "red_arms": red_arms,
            "white_arms": white_arms,
            "card_arms": card_arms,
            "dot_px": dot_px,
            "_box_val": val,
            "_red_diag": red_diag_mask if state == "KILL" else None,
            "_card_arms": card_arms,
            "_state": state,
        })

    def step(self, img, *, t_us: int | None = None) -> dict:
        """Process one frame in temporal sequence with baseline delta and static-scene guard."""
        prior = self._history[-1] if self._history else None
        out = self.analyze_frame(img, prior=prior)
        state = out["state"]
        triggered = False
        if state == "KILL" and out.get("_red_diag") is not None:
            np = _libs()[1]
            static_runs = 0
            for prev in reversed(self._history):
                if prev.get("_red_diag") is not None and prev["_red_diag"].shape == out["_red_diag"].shape:
                    if _mask_jaccard(np, prev["_red_diag"], out["_red_diag"]) >= STATIC_JACCARD_MIN:
                        static_runs += 1
                    else:
                        break
                else:
                    break
            if static_runs >= STATIC_SUPPRESS_FRAMES:
                state = "ABSTAIN"
                out["state"] = "ABSTAIN"
                out["ok"] = False
                out["reason"] = "static_red_scene_suppressed"
            elif prior is None or prior.get("_state") != "KILL":
                triggered = True
        elif state == "HIT" and (prior is None or prior.get("_state") != "HIT"):
            triggered = True
        elif state == "ADS_OR_FIRING" and prior is not None and prior.get("_state") not in ("ADS_OR_FIRING", "HIT", "KILL"):
            triggered = True

        out["triggered"] = triggered
        out["t_us"] = t_us
        out.pop("_state", None)
        self._history.append({
            "t_us": t_us,
            "_box_val": out.pop("_box_val", None),
            "_red_diag": out.pop("_red_diag", None),
            "_card_arms": out.pop("_card_arms", 0),
            "_state": state,
        })
        return out


@dataclass(frozen=True)
class DigitGlyph:
    label: str
    score: float
    second: float
    x: int
    y: int
    w: int
    h: int


def _hud_mod():
    import qorcap_cod_hud as hud
    return hud


def _killfeed_mod():
    import qorcap_cod_killfeed as kf
    return kf


def _unified_digit_bank(hud=None, kf=None):
    """Normalized feature banks combining gray-edge CoD row digits, HUD digit vectors, and font digits."""
    hud = _hud_mod() if hud is None else hud
    kf = _killfeed_mod() if kf is None else kf
    cv2, np = _libs()
    if cv2 is None or np is None:
        return None
    cache_key = "default"
    if cache_key in _DIGIT_BANK_CACHE:
        return _DIGIT_BANK_CACHE[cache_key]
    vecs_432 = []
    labels_432 = []
    vecs_216 = []
    labels_216 = []
    row_bank = hud._row_digit_bank()
    if row_bank is not None:
        r_vecs, r_labels = row_bank
        for i, lbl in enumerate(r_labels):
            s = str(lbl)
            if s in "0123456789" and len(s) == 1:
                vecs_432.append(r_vecs[i])
                labels_432.append(s)
    pack = hud._pack()
    if pack is not None and "digit_vecs" in pack and "digit_labels" in pack:
        for i, raw_lbl in enumerate(pack["digit_labels"]):
            lbl = raw_lbl.decode("ascii") if isinstance(raw_lbl, (bytes, bytearray)) else str(raw_lbl)
            if lbl in "0123456789" and len(lbl) == 1:
                vecs_216.append(pack["digit_vecs"][i].astype(np.float32))
                labels_216.append(lbl)
    fbank = kf.font_bank()
    if fbank is not None:
        for ch in "0123456789":
            tmpl = kf.word_template(ch, 7.0, scale=2.0)
            if tmpl is None:
                continue
            mask = (tmpl > 100.0).astype(np.uint8) * 255
            ys, xs = np.where(mask > 0)
            if ys.size == 0:
                continue
            tight = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
            fv = hud._row_feature(tight, None)
            if fv is not None:
                vecs_432.append(fv)
                labels_432.append(ch)
            bv = hud._vec(np, tight, (12, 18))
            if bv is not None:
                vecs_216.append(bv)
                labels_216.append(ch)
    if not vecs_432 and not vecs_216:
        return None
    packed = {
        "vecs_432": np.stack(vecs_432).astype(np.float32) if vecs_432 else None,
        "labels_432": tuple(labels_432),
        "vecs_216": np.stack(vecs_216).astype(np.float32) if vecs_216 else None,
        "labels_216": tuple(labels_216),
    }
    _DIGIT_BANK_CACHE[cache_key] = packed
    return packed


def _classify_digit_component(hud, np, bank, glyph_mask, gray_patch, min_score: float, min_margin: float):
    by_lbl: dict[str, float] = {}
    if bank["vecs_432"] is not None:
        fv = hud._row_feature(glyph_mask, gray_patch)
        if fv is not None:
            scores = bank["vecs_432"] @ fv
            for idx, lbl in enumerate(bank["labels_432"]):
                s = float(scores[idx])
                if lbl not in by_lbl or s > by_lbl[lbl]:
                    by_lbl[lbl] = s
    if bank["vecs_216"] is not None:
        bv = hud._vec(np, glyph_mask, (12, 18))
        if bv is not None:
            scores = bank["vecs_216"] @ bv
            for idx, lbl in enumerate(bank["labels_216"]):
                s = float(scores[idx])
                if lbl not in by_lbl or s > by_lbl[lbl]:
                    by_lbl[lbl] = s
    if not by_lbl:
        return None
    ranked = sorted(by_lbl.items(), key=lambda kv: -kv[1])
    best_lbl, top = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else -1.0
    if top < min_score or (top - second) < min_margin:
        return None
    return best_lbl, top, second


def _extract_bright_components(
    cv2, np, patch_bgr, *,
    min_h: int,
    max_h: int,
    val_min: int = 150,
    tophat_min: int = 22,
    tophat_k: int = 11,
    color_mask=None,
):
    """Extract crisp bright glyph bounding boxes after background top-hat subtraction."""
    hsv = cv2.cvtColor(patch_bgr, cv2.COLOR_BGR2HSV)
    v = hsv[:, :, 2]
    if v.size == 0 or int(v.max()) - int(np.median(v)) < DIGIT_MIN_CONTRAST:
        return [], v
    th = _tophat_gray(cv2, np, v, tophat_k)
    fg = (v > val_min) & (th > tophat_min)
    if color_mask is not None:
        fg &= color_mask
    n, lab, st, _ = cv2.connectedComponentsWithStats(fg.astype(np.uint8), connectivity=8)
    comps = []
    min_area = max(6, int(round(min_h * 1.1)))
    for i in range(1, n):
        x, y, w, h, area = (int(t) for t in st[i])
        if h < min_h or h > max_h or w < 2 or area < min_area:
            continue
        aspect = w / float(h)
        if aspect < 0.08 or aspect > 1.05:
            continue
        g = (lab[y:y + h, x:x + w] == i).astype(np.uint8) * 255
        gx0, gy0 = max(0, x - 1), max(0, y - 1)
        gx1, gy1 = min(v.shape[1], x + w + 1), min(v.shape[0], y + h + 1)
        comps.append({"x": x, "y": y, "w": w, "h": h, "area": area, "glyph": g, "gray": v[gy0:gy1, gx0:gx1].copy()})
    comps.sort(key=lambda c: c["x"])
    return comps, v


def _decode_component_cluster(
    comps: list[dict], *,
    max_digits: int = 6,
    min_score: float = DIGITS_MIN_SCORE,
    min_margin: float = DIGITS_MIN_MARGIN,
) -> tuple[int | None, list[DigitGlyph], str | None]:
    """Decode an isolated horizontal cluster of digit components; fails closed on any ambiguity."""
    if not comps or len(comps) > max_digits:
        return None, [], "empty_or_too_many_glyphs"
    hud = _hud_mod()
    np = _libs()[1]
    bank = _unified_digit_bank(hud=hud)
    if np is None or bank is None:
        return None, [], "digit_bank_missing"
    tall = max(c["h"] for c in comps)
    short = min(c["h"] for c in comps)
    if tall > short * 1.45 and tall - short > 4:
        return None, [], "inconsistent_glyph_height"
    decoded: list[DigitGlyph] = []
    text = []
    for c in sorted(comps, key=lambda item: item["x"]):
        res = _classify_digit_component(hud, np, bank, c["glyph"], c["gray"], min_score, min_margin)
        if res is None:
            return None, decoded, "glyph_below_gate"
        lbl, top, sec = res
        decoded.append(DigitGlyph(lbl, round(top, 4), round(sec, 4), c["x"], c["y"], c["w"], c["h"]))
        text.append(lbl)
    val_str = "".join(text)
    if not val_str.isdigit():
        return None, decoded, "non_digit"
    return int(val_str), decoded, None


def read_cod_digits(
    patch_bgr, *,
    scale: float = 1.0,
    min_h_720: float = 8.0,
    max_h_720: float = 42.0,
    max_digits: int = 6,
    val_min: int = 140,
    tophat_min: int = 20,
    tophat_k_720: int = 11,
    min_score: float = DIGITS_MIN_SCORE,
    min_margin: float = DIGITS_MIN_MARGIN,
    color_mask=None,
) -> dict:
    """Read one integer (0-9 or 0-100+) from a pre-cropped CoD-font patch."""
    cv2, np = _libs()
    if cv2 is None or np is None or not isinstance(patch_bgr, np.ndarray) or patch_bgr.ndim != 3:
        return _info({"ok": False, "state": "ABSTAIN", "value": None, "reason": "patch_invalid", "glyphs": []})
    comps, _v = _extract_bright_components(
        cv2, np, patch_bgr[:, :, :3],
        min_h=max(5, int(round(min_h_720 * scale))),
        max_h=max(10, int(round(max_h_720 * scale))),
        val_min=val_min,
        tophat_min=tophat_min,
        tophat_k=max(5, int(round(tophat_k_720 * scale))),
        color_mask=color_mask,
    )
    val, glyphs, err = _decode_component_cluster(
        comps, max_digits=max_digits, min_score=min_score, min_margin=min_margin,
    )
    if val is None:
        return _info({
            "ok": False, "state": "ABSTAIN", "value": None,
            "reason": err or "unreadable", "glyphs": [g.__dict__ for g in glyphs],
        })
    return _info({
        "ok": True, "state": "READ", "value": val,
        "reason": "decoded", "glyphs": [g.__dict__ for g in glyphs],
    })


def _group_x_clusters(comps: list[dict], max_gap: int) -> list[list[dict]]:
    if not comps:
        return []
    ordered = sorted(comps, key=lambda c: c["x"])
    groups = [[ordered[0]]]
    for c in ordered[1:]:
        prev = groups[-1][-1]
        if c["x"] - (prev["x"] + prev["w"]) <= max_gap and abs(c["y"] - prev["y"]) <= max(6, prev["h"] // 2):
            groups[-1].append(c)
        else:
            groups.append([c])
    return groups


def read_ammo_counter(img) -> dict:
    """Read bottom-right magazine and reserve ammo counters (0-100+) on 16:9 CoD frames."""
    cv2, np = _libs()
    bgr = _as_bgr(img)
    if cv2 is None or bgr is None:
        return _info({"ok": False, "state": "ABSTAIN", "mag": None, "reserve": None, "reason": "frame_invalid"})
    H, W = bgr.shape[:2]
    scale = H / REF_H
    x0, x1 = int(0.70 * W), int(0.92 * W)
    y0, y1 = int(0.77 * H), int(0.94 * H)
    crop = bgr[y0:y1, x0:x1]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    best_mag = None
    mag_cands = []
    comps = []
    for v_cut, s_cut, th_cut in ((220, 60, 18), (190, 75, 20)):
        neutral = hsv[:, :, 1] < s_cut
        comps, _ = _extract_bright_components(
            cv2, np, crop,
            min_h=max(8, int(round(12 * scale))),
            max_h=max(20, int(round(44 * scale))),
            val_min=v_cut,
            tophat_min=th_cut,
            tophat_k=max(7, int(round(11 * scale))),
            color_mask=neutral,
        )
        # Filter out square/wide bullet-icon contours (w/h > 0.85)
        comps = [c for c in comps if c["w"] / float(c["h"]) <= 0.85]
        mag_cands = [c for c in comps if c["h"] >= int(round(20 * scale))]
        if mag_cands:
            break
    if not mag_cands:
        return _info({"ok": False, "state": "ABSTAIN", "mag": None, "reserve": None, "reason": "mag_digits_not_found"})
    mag_groups = _group_x_clusters(mag_cands, max_gap=max(6, int(round(12 * scale))))
    best_mag = max(mag_groups, key=lambda g: (sum(c["h"] for c in g) / len(g), len(g)))
    mag_val, mag_glyphs, err = _decode_component_cluster(best_mag, max_digits=3)
    if mag_val is None:
        return _info({"ok": False, "state": "ABSTAIN", "mag": None, "reserve": None, "reason": f"mag_{err}"})

    mag_x1 = max(c["x"] + c["w"] for c in best_mag)
    mag_y0 = min(c["y"] for c in best_mag)
    mag_y1 = max(c["y"] + c["h"] for c in best_mag)
    res_cands = [
        c for c in comps
        if c["x"] >= mag_x1 + max(2, int(round(4 * scale)))
        and c["h"] >= int(round(11 * scale))
        and c["h"] <= int(round(27 * scale))
        and c["y"] >= mag_y0 - int(round(6 * scale))
        and (c["y"] + c["h"]) <= mag_y1 + int(round(8 * scale))
    ]
    res_val = None
    if res_cands:
        res_groups = _group_x_clusters(res_cands, max_gap=max(5, int(round(10 * scale))))
        if res_groups:
            res_val, _, _ = _decode_component_cluster(res_groups[0], max_digits=3)
    return _info({
        "ok": True,
        "state": "READ",
        "mag": mag_val,
        "reserve": res_val,
        "reason": "decoded",
        "mag_bbox": [x0 + min(c["x"] for c in best_mag), y0 + mag_y0, x0 + mag_x1, y0 + mag_y1],
        "mag_glyphs": [g.__dict__ for g in mag_glyphs],
    })


def read_live_score(img) -> dict:
    """Read left HUD top/bottom live team scores (0-100+) when present."""
    cv2, np = _libs()
    bgr = _as_bgr(img)
    if cv2 is None or bgr is None:
        return _info({"ok": False, "state": "ABSTAIN", "top_score": None, "bottom_score": None, "reason": "frame_invalid"})
    H, W = bgr.shape[:2]
    scale = H / REF_H
    x0, x1 = int(0.01 * W), int(0.11 * W)
    y0, y1 = int(0.24 * H), int(0.42 * H)
    crop = bgr[y0:y1, x0:x1]
    comps, _ = _extract_bright_components(
        cv2, np, crop,
        min_h=max(8, int(round(11 * scale))),
        max_h=max(16, int(round(26 * scale))),
        val_min=160,
        tophat_min=20,
        tophat_k=max(7, int(round(9 * scale))),
    )
    if not comps:
        return _info({"ok": False, "state": "ABSTAIN", "top_score": None, "bottom_score": None, "reason": "no_score_digits"})
    rows: list[list[dict]] = []
    for c in sorted(comps, key=lambda item: item["y"]):
        placed = False
        for row in rows:
            if abs(c["y"] - row[0]["y"]) <= max(5, int(round(8 * scale))):
                row.append(c)
                placed = True
                break
        if not placed:
            rows.append([c])
    vals = []
    for row in sorted(rows, key=lambda r: r[0]["y"]):
        groups = _group_x_clusters(row, max_gap=max(6, int(round(10 * scale))))
        if not groups:
            continue
        val, _, _ = _decode_component_cluster(groups[0], max_digits=3)
        if val is not None:
            vals.append(val)
    if len(vals) < 2:
        return _info({
            "ok": False, "state": "ABSTAIN",
            "top_score": vals[0] if vals else None, "bottom_score": None,
            "reason": "incomplete_team_scores",
        })
    return _info({
        "ok": True,
        "state": "READ",
        "top_score": vals[0],
        "bottom_score": vals[1],
        "reason": "decoded",
    })


def read_kill_xp_popup(img) -> dict:
    """Detect post-Nov-21 bright-yellow XP popup (e.g. ``210 XP``) near upper-center HUD."""
    cv2, np = _libs()
    bgr = _as_bgr(img)
    if cv2 is None or bgr is None:
        return _info({"ok": False, "state": "ABSTAIN", "xp_value": None, "reason": "frame_invalid"})
    H, W = bgr.shape[:2]
    scale = H / REF_H
    x0, x1 = int(0.34 * W), int(0.68 * W)
    y0, y1 = int(0.24 * H), int(0.54 * H)
    band = bgr[y0:y1, x0:x1]
    hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
    yellow = (hsv[:, :, 0] >= 15) & (hsv[:, :, 0] <= 42) & (hsv[:, :, 1] >= 85) & (hsv[:, :, 2] >= 165)
    y_frac = float(yellow.mean())
    if y_frac > 0.35:
        return _info({"ok": False, "state": "ABSTAIN", "xp_value": None, "reason": "yellow_flood"})
    yellow_px = int(yellow.sum())
    if yellow_px < int(round(160 * scale * scale)):
        return _info({"ok": True, "state": "NONE", "xp_value": None, "yellow_px": yellow_px, "reason": "no_yellow_xp"})

    ys, xs = np.where(yellow)
    bx0, bx1 = max(0, int(xs.min()) - 4), min(band.shape[1], int(xs.max()) + 4)
    by0, by1 = max(0, int(ys.min()) - 4), min(band.shape[0], int(ys.max()) + 4)
    sub = band[by0:by1, bx0:bx1]
    sub_hsv = hsv[by0:by1, bx0:bx1]
    sub_yellow = yellow[by0:by1, bx0:bx1]
    kf = _killfeed_mod()
    xp_core = (sub_hsv[:, :, 2] * sub_yellow.astype(np.uint8)).astype(np.float32)

    best_xp = (-1.0, 0, 0, 0)
    if kf.font_bank() is not None:
        for mult in (1.25, 1.5, 1.75, 2.0, 2.25):
            t_xp = kf.word_template("XP", 7.0, scale=scale * mult)
            if t_xp is None or xp_core.shape[0] < t_xp.shape[0] or xp_core.shape[1] < t_xp.shape[1]:
                continue
            sc, loc = kf._ncc(cv2, xp_core, t_xp)
            if sc > best_xp[0]:
                best_xp = (sc, loc[0], loc[1], t_xp.shape[0])

    # Fallback morphological XP suffix check + crisp digits left of XP
    comps, _ = _extract_bright_components(
        cv2, np, sub,
        min_h=max(8, int(round(12 * scale))),
        max_h=max(24, int(round(48 * scale))),
        val_min=175,
        tophat_min=18,
        tophat_k=max(9, int(round(15 * scale))),
        color_mask=sub_yellow,
    )
    xp_present = best_xp[0] >= 0.45 or (yellow_px >= int(round(450 * scale * scale)) and len(comps) >= 2)
    if not xp_present:
        return _info({"ok": True, "state": "NONE", "xp_value": None, "yellow_px": yellow_px, "reason": "not_xp_popup"})

    xp_x = best_xp[1] if best_xp[0] >= 0.45 else int(sub.shape[1] * 0.55)
    num_comps = [c for c in comps if c["x"] + c["w"] <= xp_x + int(round(6 * scale))]
    xp_val, _, _ = _decode_component_cluster(num_comps, max_digits=5, min_score=0.52, min_margin=0.03)
    return _info({
        "ok": True,
        "state": "XP_POPUP",
        "xp_value": xp_val,
        "xp_score": round(max(0.0, best_xp[0]), 4),
        "yellow_px": yellow_px,
        "bbox": [x0 + bx0, y0 + by0, x0 + bx1, y0 + by1],
        "reason": "xp_popup_present",
    })


_EXTRA_MODE_HEADERS = ("SCORE", "KILLS", "DEATHS", "ASSISTS", "CONFIRMS", "CAPTURES", "OBJ. TIME", "DATA UPLOADED")


def _gray_match(cv2, np, gray_patch, mask, scales, *, cache_key: str | None = None):
    best = None
    for sc in scales:
        templ = _scaled_f32_mask(cv2, np, mask, sc, cache_key=cache_key)
        if templ.shape[0] < 6 or templ.shape[1] < 6:
            continue
        if gray_patch.shape[0] < templ.shape[0] or gray_patch.shape[1] < templ.shape[1]:
            continue
        res = cv2.matchTemplate(gray_patch, templ, cv2.TM_CCOEFF_NORMED)
        s = float(res.max())
        if best is None or s > best[0]:
            yy, xx = np.unravel_index(int(res.argmax()), res.shape)
            best = (s, int(xx), int(yy), int(templ.shape[1]), int(templ.shape[0]), float(sc))
    return best


def read_owner_row_post_nov21(img) -> dict:
    """Read Qortrola30's end scoreboard row across pre- and post-Nov-21 Large-Text / Hardpoint boards."""
    cv2, np = _libs()
    bgr = _as_bgr(img)
    if cv2 is None or bgr is None:
        return _info({"ok": False, "state": "ABSTAIN", "reason": "frame_invalid"})
    hud = _hud_mod()
    pack = hud._pack()
    base_scale = hud._board_scale(bgr)
    if pack is None or base_scale is None or "owner_mask" not in pack:
        return _info({"ok": False, "state": "ABSTAIN", "reason": "pack_or_scale_missing"})

    from qorcap_cod import OWNER_TAG
    kf = _killfeed_mod()
    H, W = bgr.shape[:2]
    ox0, ox1 = int(0.06 * W), int(0.38 * W)
    oy0, oy1 = int(0.22 * H), int(0.80 * H)
    owner_roi = bgr[oy0:oy1, ox0:ox1]
    owner_vm = hud._value_mask(owner_roi, 140)
    owner_scales = tuple(base_scale * m for m in (1.0, 1.15, 1.25, 1.35, 1.45, 1.55))
    best_owner = None
    for mult in owner_scales:
        hit = hud._best_owner_hit(owner_vm, pack, mult)
        if hit is not None and (best_owner is None or hit[0] > best_owner[0]):
            best_owner = (hit[0], ox0 + hit[1], oy0 + hit[2], hit[3], hit[4], mult)
    if (best_owner is None or best_owner[0] < 0.80) and kf.font_bank() is not None:
        owner_v = cv2.cvtColor(owner_roi, cv2.COLOR_BGR2HSV)[:, :, 2]
        owner_th = _tophat_gray(cv2, np, owner_v, max(7, int(round(11 * base_scale)))).astype(np.float32)
        for mult in owner_scales:
            t = kf.word_template(OWNER_TAG, 7.0, scale=mult)
            if t is None or owner_th.shape[0] < t.shape[0] or owner_th.shape[1] < t.shape[1]:
                continue
            sc, loc = kf._ncc(cv2, owner_th, t)
            if sc >= 0.72 and (best_owner is None or sc > best_owner[0]):
                best_owner = (sc, ox0 + int(loc[0]), oy0 + int(loc[1]), int(t.shape[1]), int(t.shape[0]), mult)
    if best_owner is None or best_owner[0] < 0.72:
        return _info({"ok": False, "state": "ABSTAIN", "reason": "owner_tag_not_matched"})

    if abs(best_owner[5] - base_scale) < 0.08 * base_scale:
        legacy = hud.read_owner_row(bgr)
        if legacy.ok and legacy.kills is not None and legacy.deaths is not None:
            return _info({
                "ok": True,
                "state": "READ",
                "source": "legacy_owner_row",
                "kills": legacy.kills,
                "deaths": legacy.deaths,
                "assists": legacy.assists,
                "confirms": legacy.confirms,
                "eliminations": None,
                "score": None,
                "mode_header": None,
                "deaths_status": getattr(legacy, "deaths_status", None),
                "player_tag": legacy.player_tag,
                "reason": legacy.reason,
            })

    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    v_f32 = hsv[:, :, 2].astype(np.float32)
    th_f32 = _tophat_gray(cv2, np, hsv[:, :, 2], max(7, int(round(11 * base_scale)))).astype(np.float32)

    _o_score, row_x, row_y, _row_w, row_h, row_scale = best_owner
    hy0 = max(0, row_y - int(round(160 * base_scale)))
    hy1 = max(hy0 + 1, row_y - int(round(4 * base_scale)))
    hx0 = max(0, row_x)
    hx1 = min(W, row_x + int(round(620 * base_scale)))
    hband = v_f32[hy0:hy1, hx0:hx1]
    hdr_scales = tuple(base_scale * m for m in (0.95, 1.0, 1.1, 1.15, 1.2, 1.25, 1.35))

    headers: dict[str, tuple] = {}
    for label, pkey in (
        ("SCORE", "score_header_mask"),
        ("KILLS", "kills_header_mask"),
        ("DEATHS", "deaths_header_mask"),
        ("ASSISTS", "assists_header_mask"),
        ("CONFIRMS", "confirms_header_mask"),
    ):
        mask = pack.get(pkey)
        if mask is None:
            continue
        hit = _gray_match(cv2, np, hband, mask, hdr_scales, cache_key=pkey)
        if hit is not None and hit[0] >= 0.68:
            headers[label] = (hit[0], hx0 + hit[1], hy0 + hit[2], hit[3], hit[4], hit[5])
    for mk, mask in (pack.get("mode_header_masks") or {}).items():
        hit = _gray_match(cv2, np, hband, mask, hdr_scales, cache_key=f"mode_{mk}")
        if hit is not None and hit[0] >= 0.68:
            headers[mk] = (hit[0], hx0 + hit[1], hy0 + hit[2], hit[3], hit[4], hit[5])

    if "KILLS" not in headers:
        return _info({"ok": False, "state": "ABSTAIN", "reason": "kills_header_not_found"})

    def _read_cell(col_hit) -> int | None:
        _sc, cx, _cy, cw, _ch, _hsc = col_hit
        px0 = max(0, cx - int(round(8 * base_scale)))
        px1 = min(W, cx + cw + int(round(14 * base_scale)))
        py0 = max(0, row_y - int(round(4 * base_scale)))
        py1 = min(H, row_y + row_h + int(round(6 * base_scale)))
        res = read_cod_digits(
            bgr[py0:py1, px0:px1],
            scale=base_scale,
            min_h_720=7.0,
            max_h_720=22.0,
            max_digits=6,
            val_min=115,
            tophat_min=18,
            tophat_k_720=9,
            min_score=0.55,
            min_margin=0.04,
        )
        return res["value"] if res["ok"] else None

    kills = _read_cell(headers["KILLS"])
    if kills is None:
        return _info({"ok": False, "state": "ABSTAIN", "reason": "kills_digits_unreadable"})
    score_val = _read_cell(headers["SCORE"]) if "SCORE" in headers else None
    assists_val = _read_cell(headers["ASSISTS"]) if "ASSISTS" in headers else None
    confirms_val = _read_cell(headers["CONFIRMS"]) if "CONFIRMS" in headers else None
    deaths_val = None
    deaths_source = None
    elims_val = None
    mode_header = next((k for k in ("CAPTURES", "CONFIRMS", "DATA UPLOADED") if k in headers), None)

    if "DEATHS" in headers and not hud._columns_share_x(headers["DEATHS"][:5], headers["KILLS"][:5], base_scale):
        deaths_val = _read_cell(headers["DEATHS"])
        deaths_source = "column"
    else:
        # Check bottom personal card (normal or Large-Text) + ELIMINATIONS/DEATHS
        cy0, cy1 = int(0.80 * H), int(0.96 * H)
        cx0, cx1 = int(0.26 * W), int(0.76 * W)
        card = bgr[cy0:cy1, cx0:cx1]
        card_v = v_f32[cy0:cy1, cx0:cx1]
        card_th = th_f32[cy0:cy1, cx0:cx1]
        card_owner_ok = False
        for idx_cm, cm in enumerate(pack.get("owner_card_masks") or []):
            hit = _gray_match(cv2, np, card_v, cm, (base_scale, base_scale * 1.15), cache_key=f"card_owner_{idx_cm}")
            if hit is not None and hit[0] >= 0.72:
                card_owner_ok = True
                break
        if not card_owner_ok and kf.font_bank() is not None:
            for m in (base_scale, base_scale * 1.15, base_scale * 1.25):
                t = kf.word_template(OWNER_TAG, 7.0, scale=m)
                if t is not None and card_th.shape[0] >= t.shape[0] and card_th.shape[1] >= t.shape[1]:
                    sc, _ = kf._ncc(cv2, card_th, t)
                    if sc >= 0.70:
                        card_owner_ok = True
                        break
        if card_owner_ok:
            elims_loc_x = None
            if kf.font_bank() is not None:
                best_el = None
                for m in (base_scale, base_scale * 1.15, base_scale * 1.25):
                    t_el = kf.word_template("ELIMINATIONS", 7.0, scale=m)
                    if t_el is not None and card_th.shape[0] >= t_el.shape[0] and card_th.shape[1] >= t_el.shape[1]:
                        sc, loc = kf._ncc(cv2, card_th, t_el)
                        if sc >= 0.65 and (best_el is None or sc > best_el[0]):
                            best_el = (sc, loc[0], loc[1], t_el.shape[1], t_el.shape[0])
                if best_el is not None:
                    elims_loc_x = best_el[1]
                    ex0 = max(0, cx0 + best_el[1] - int(round(8 * base_scale)))
                    ex1 = min(W, cx0 + best_el[1] + best_el[3] + int(round(16 * base_scale)))
                    ey0 = cy0 + best_el[2] + best_el[4] - 2
                    ey1 = min(H, ey0 + int(round(32 * base_scale)))
                    eres = read_cod_digits(
                        bgr[ey0:ey1, ex0:ex1], scale=base_scale, min_h_720=10.0, max_h_720=30.0,
                        max_digits=3, val_min=110, tophat_min=18, tophat_k_720=11, min_score=0.52, min_margin=0.03,
                    )
                    if eres["ok"]:
                        elims_val = eres["value"]
            dhit = None
            cand_v = _gray_match(cv2, np, card_v, pack["deaths_header_mask"], (base_scale, base_scale * 1.15), cache_key="deaths_hdr")
            if cand_v is not None and cand_v[0] >= 0.70:
                dhit = cand_v
            if dhit is None:
                cand_th = _gray_match(
                    cv2, np, card_th,
                    (pack["deaths_header_mask"] > 0).astype(np.float32) * 255.0,
                    (base_scale, base_scale * 1.15),
                    cache_key="deaths_hdr_th",
                )
                if cand_th is not None and cand_th[0] >= 0.65:
                    dhit = cand_th
            if dhit is None and kf.font_bank() is not None:
                best_dh = None
                for m in (base_scale, base_scale * 1.15, base_scale * 1.25):
                    t_d = kf.word_template("DEATHS", 7.0, scale=m)
                    if t_d is not None and card_th.shape[0] >= t_d.shape[0] and card_th.shape[1] >= t_d.shape[1]:
                        sc, loc = kf._ncc(cv2, card_th, t_d)
                        if sc >= 0.64 and (elims_loc_x is None or loc[0] > elims_loc_x + int(round(40 * base_scale))):
                            if best_dh is None or sc > best_dh[0]:
                                best_dh = (sc, loc[0], loc[1], t_d.shape[1], t_d.shape[0], m)
                dhit = best_dh
            if dhit is not None:
                dx0 = max(0, cx0 + dhit[1] - int(round(12 * base_scale)))
                dx1 = min(W, cx0 + dhit[1] + dhit[3] + int(round(28 * base_scale)))
                dy0 = cy0 + dhit[2] + dhit[4] - 2
                dy1 = min(H, dy0 + int(round(32 * base_scale)))
                dres = read_cod_digits(
                    bgr[dy0:dy1, dx0:dx1], scale=base_scale, min_h_720=10.0, max_h_720=30.0,
                    max_digits=3, val_min=110, tophat_min=18, tophat_k_720=11, min_score=0.52, min_margin=0.03,
                )
                if dres["ok"]:
                    deaths_val = dres["value"]
                    deaths_source = "personal_card"
        # Infer OBJ. TIME / no-column mode positively when card deaths read cleanly and deaths header absent
        if mode_header is None and deaths_source == "personal_card":
            deaths_band_max = 0.0
            dh_chk = _gray_match(cv2, np, hband, pack["deaths_header_mask"], hdr_scales, cache_key="deaths_hdr")
            if dh_chk is not None:
                deaths_band_max = dh_chk[0]
            if deaths_band_max < 0.55:
                mode_header = "NO_DEATHS_COLUMN_OR_OBJ_TIME"

    if deaths_val is None:
        return _info({
            "ok": False, "state": "ABSTAIN", "reason": "deaths_not_verified",
            "kills": kills, "score": score_val, "assists": assists_val, "eliminations": elims_val,
        })

    return _info({
        "ok": True,
        "state": "READ",
        "source": "post_nov21_multiscale",
        "row_scale": round(row_scale, 3),
        "kills": kills,
        "deaths": deaths_val,
        "assists": assists_val,
        "confirms": confirms_val,
        "eliminations": elims_val,
        "score": score_val,
        "mode_header": mode_header,
        "deaths_status": deaths_source,
        "player_tag": OWNER_TAG,
        "reason": "owner_row_and_deaths_read",
    })


def reconcile_event_signals(
    *,
    center_box: dict,
    xp_popup: dict | None = None,
    killfeed_lines: list[dict] | None = None,
    hid_window_active: bool | None = None,
    t_us: int | None = None,
) -> dict:
    """Binding multi-signal event model: HIT, KILL, ADS_OR_FIRING, NONE, or ABSTAIN.

    Rules:
    * Wide-crop red X is never accepted or triggered here; only fixed center_box.
    * HIT = white center-box hitmarker without KILL/ABSTAIN conflict.
    * KILL = red center-box hitmarker change AND yellow XP popup present, without
      contradictory non-self-only killfeed attribution in window.
    * Disagreement -> ABSTAIN.
    """
    c_state = (center_box or {}).get("state", "ABSTAIN")
    xp_state = (xp_popup or {}).get("state", "NONE")
    xp_val = (xp_popup or {}).get("xp_value")
    if c_state == "ABSTAIN":
        return _info({
            "event": "ABSTAIN", "t_us": t_us, "reason": center_box.get("reason", "center_abstain"),
            "center_state": c_state, "xp_state": xp_state, "xp_value": xp_val,
            "hid_window_active": hid_window_active, "killfeed_confirm": None,
        })
    lines = killfeed_lines or []
    kf_self = any(ln.get("killer") == "self" for ln in lines)
    kf_not_self_only = bool(lines) and all(ln.get("killer") == "not_self" for ln in lines)

    if c_state == "KILL":
        if xp_state != "XP_POPUP":
            return _info({
                "event": "ABSTAIN", "t_us": t_us, "reason": "red_center_without_xp_popup",
                "center_state": c_state, "xp_state": xp_state, "xp_value": xp_val,
                "hid_window_active": hid_window_active, "killfeed_confirm": False,
            })
        if kf_not_self_only and not kf_self:
            return _info({
                "event": "ABSTAIN", "t_us": t_us, "reason": "killfeed_contradicts_self_kill",
                "center_state": c_state, "xp_state": xp_state, "xp_value": xp_val,
                "hid_window_active": hid_window_active, "killfeed_confirm": False,
            })
        return _info({
            "event": "KILL", "t_us": t_us, "reason": "center_red_plus_xp_popup",
            "center_state": c_state, "xp_state": xp_state, "xp_value": xp_val,
            "hid_window_active": hid_window_active, "killfeed_confirm": True if kf_self else None,
        })

    if c_state == "HIT":
        return _info({
            "event": "HIT", "t_us": t_us, "reason": "center_white_hitmarker",
            "center_state": c_state, "xp_state": xp_state, "xp_value": xp_val,
            "hid_window_active": hid_window_active, "killfeed_confirm": None,
        })

    if xp_state == "XP_POPUP" and c_state in ("NONE", "ADS_OR_FIRING"):
        # XP popup without center hitmarker can be assist/objective/medal XP, not an automatic KILL.
        return _info({
            "event": "ABSTAIN", "t_us": t_us, "reason": "xp_popup_without_center_marker",
            "center_state": c_state, "xp_state": xp_state, "xp_value": xp_val,
            "hid_window_active": hid_window_active, "killfeed_confirm": kf_self or None,
        })

    return _info({
        "event": c_state, "t_us": t_us, "reason": center_box.get("reason", "idle"),
        "center_state": c_state, "xp_state": xp_state, "xp_value": xp_val,
        "hid_window_active": hid_window_active, "killfeed_confirm": None,
    })


def select_capture_bursts(
    events: list[dict], *,
    pre_ms: int = 35,
    post_ms: int = 500,
    merge_gap_ms: int = 1200,
) -> list[dict]:
    """Build deduplicated post-session burst extraction windows anchored on sealed timeline events."""
    triggers = [e for e in events if e.get("event") in ("HIT", "KILL") and isinstance(e.get("t_us"), int)]
    triggers.sort(key=lambda e: e["t_us"])
    pre_us = int(pre_ms) * 1000
    post_us = int(post_ms) * 1000
    gap_us = int(merge_gap_ms) * 1000
    windows: list[dict] = []
    for ev in triggers:
        t0 = max(0, ev["t_us"] - pre_us)
        t1 = ev["t_us"] + post_us
        if windows and t0 <= windows[-1]["end_us"] + gap_us:
            cur = windows[-1]
            cur["end_us"] = max(cur["end_us"], t1)
            cur["events"].append(ev["event"])
            cur["trigger_t_us"].append(ev["t_us"])
        else:
            windows.append({
                "start_us": t0,
                "end_us": t1,
                "trigger_t_us": [ev["t_us"]],
                "events": [ev["event"]],
                "clock_note": "same clock domain; residual UNMEASURED",
                "info_only": INFO_ONLY,
                "counted": COUNTED,
            })
    return windows
