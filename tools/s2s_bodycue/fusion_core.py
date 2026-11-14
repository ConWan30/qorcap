"""S2S body cue v0 fusion (phase B): one enemy-on-screen decision, yes / no / unknown, with reasons. numpy only.

Inputs per frame (all at native 1280x720):
  * v0.2 minimap red dots (count): a PRIOR only. A dot means an enemy on the MAP, never on screen by itself.
  * v0.2 in-world red tag (strip + diamond) records, HUD-gated by v0.2.
  * person boxes from YOLOX-Tiny and RF-DETR Nano on the 384 reticle crop (crop pixels, shifted to frame pixels).
  * the frame itself, for a friend/foe colour test in each person's head region: a red enemy nameplate/diamond
    versus a blue/cyan/green friendly nameplate, counted in OpenCV-convention HSV (H 0..179, S/V 0..255).

Decision rules (parameters in DEFAULT_PARAMS, frozen into FROZEN-fusion-params.json):
  yes      : a person box whose head region is foe-coloured (only while the v0.2 HUD gate sees the minimap;
             death/killcam/menu views are gated), or a v0.2 in-world tag near the reticle crop that overlaps a person's head region,
             or (if enabled) a strong unknown-colour person while the minimap shows enemy dots.
  no       : only on positive evidence: HUD on, at least one person box, every person box friend-coloured,
             no foe-coloured head, no v0.2 tag, and zero minimap dots. It still only means "the people seen are
             friendlies and no enemy is on the map near the player"; reported separately, never imputed.
  unknown  : everything else. Absence of evidence is never "no". A v0.2 record with a missing, None, wrong-type,
             non-finite or negative field that the rules read is malformed and always gives unknown (P174-M1).
"""
from __future__ import annotations

import math

import numpy as np

CROP_ORIGIN = (448, 168)  # reticle384 crop top-left at 1280x720 (bodycue_core.reticle_box)

DEFAULT_PARAMS = {
    "version": "s2s1-fusion/0.1-draft",
    "person_thr": {"yolox": 0.30, "rfdetr": 0.50},
    "dedupe_iou": 0.5,
    "min_box_h": 12,
    "max_box_h": 300,
    "person_cues_require_hud": True,
    "head_region": {"w_scale": 1.6, "w_min": 48, "up_frac": 0.45, "up_min": 28, "down_frac": 0.12},
    "reticle_mask": [628, 348, 652, 372],
    "red_hsv": {"h_lo": 9, "h_hi": 171, "s_min": 120, "v_min": 110},
    "friend_hsv": {"h": [78, 112], "s_min": 90, "v_min": 120},
    "green_hsv": {"h": [45, 77], "s_min": 120, "v_min": 120},
    "foe_red_px_min": 12, "foe_red_px_max": 900, "foe_ratio": 2.0,
    "friend_px_min": 12, "friend_ratio": 2.0,
    "tag_crop_pad": 96,
    "tag_requires_person": True,
    "dot_plus_person": {"enabled": False, "person_score_min": 0.60, "dots_min": 1},
    "no_rule_enabled": True,
}


def bgr_to_hsv(bgr: np.ndarray) -> np.ndarray:
    """uint8 BGR -> uint8 HSV in the OpenCV 8-bit convention (H = deg/2 in 0..179)."""
    f = bgr.astype(np.float32)
    b, g, r = f[..., 0], f[..., 1], f[..., 2]
    v = np.max(f, axis=-1); mn = np.min(f, axis=-1); d = v - mn
    s = np.where(v > 0, d / np.maximum(v, 1e-6) * 255.0, 0.0)
    dz = np.maximum(d, 1e-6)
    h = np.where(v == r, 60.0 * (g - b) / dz, np.where(v == g, 120.0 + 60.0 * (b - r) / dz, 240.0 + 60.0 * (r - g) / dz))
    h = np.where(d == 0, 0.0, h)
    h = np.mod(h, 360.0) / 2.0
    return np.stack([np.clip(np.round(h), 0, 179), np.clip(np.round(s), 0, 255), v], -1).astype(np.uint8)


def to_frame(xyxy, origin=CROP_ORIGIN):
    return [xyxy[0] + origin[0], xyxy[1] + origin[1], xyxy[2] + origin[0], xyxy[3] + origin[1]]


def iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / u if u > 0 else 0.0


def merge_persons(yolox_boxes, rfdetr_boxes, P=DEFAULT_PARAMS) -> list[dict]:
    """Union of both models' person boxes above their thresholds, frame coords, IoU-deduped (higher score kept)."""
    cand = [{"xyxy": to_frame(b["xyxy"]), "score": b["score"], "src": "yolox"} for b in yolox_boxes
            if b["score"] >= P["person_thr"]["yolox"]]
    cand += [{"xyxy": to_frame(b["xyxy"]), "score": b["score"], "src": "rfdetr"} for b in rfdetr_boxes
             if b["score"] >= P["person_thr"]["rfdetr"]]
    cand = [c for c in cand if P["min_box_h"] <= c["xyxy"][3] - c["xyxy"][1] <= P["max_box_h"]]
    cand.sort(key=lambda c: -c["score"])
    out = []
    for c in cand:
        hit = next((o for o in out if iou(o["xyxy"], c["xyxy"]) >= P["dedupe_iou"]), None)
        if hit is None:
            out.append(dict(c, srcs=[c["src"]]))
        elif c["src"] not in hit["srcs"]:
            hit["srcs"].append(c["src"])
    return out


def head_region(xyxy, P=DEFAULT_PARAMS, wh=(1280, 720)) -> list[int]:
    x0, y0, x1, y1 = xyxy
    w, h = x1 - x0, y1 - y0; cx = (x0 + x1) / 2
    hr = P["head_region"]
    hw = max(w * hr["w_scale"], hr["w_min"]) / 2
    up = max(h * hr["up_frac"], hr["up_min"])
    r = [int(round(cx - hw)), int(round(y0 - up)), int(round(cx + hw)), int(round(y0 + h * hr["down_frac"]))]
    return [max(0, r[0]), max(0, r[1]), min(wh[0], r[2]), min(wh[1], r[3])]


def colour_counts(hsv: np.ndarray, region, P=DEFAULT_PARAMS) -> dict:
    x0, y0, x1, y1 = region
    sub = hsv[y0:y1, x0:x1].astype(np.int32)
    if sub.size == 0:
        return {"red": 0, "friend": 0, "green": 0, "area": 0}
    keep = np.ones(sub.shape[:2], bool)
    mx0, my0, mx1, my1 = P["reticle_mask"]
    keep[max(0, my0 - y0):max(0, my1 - y0), max(0, mx0 - x0):max(0, mx1 - x0)] = False
    h, s, v = sub[..., 0], sub[..., 1], sub[..., 2]
    rc, fc, gc = P["red_hsv"], P["friend_hsv"], P["green_hsv"]
    red = ((h <= rc["h_lo"]) | (h >= rc["h_hi"])) & (s >= rc["s_min"]) & (v >= rc["v_min"]) & keep
    fr = (h >= fc["h"][0]) & (h <= fc["h"][1]) & (s >= fc["s_min"]) & (v >= fc["v_min"]) & keep
    gr = (h >= gc["h"][0]) & (h <= gc["h"][1]) & (s >= gc["s_min"]) & (v >= gc["v_min"]) & keep
    return {"red": int(red.sum()), "friend": int(fr.sum()), "green": int(gr.sum()), "area": int(keep.sum())}


def friend_or_foe(c: dict, P=DEFAULT_PARAMS) -> str:
    red, fr = c["red"], c["friend"] + c["green"]
    if P["foe_red_px_min"] <= red <= P["foe_red_px_max"] and red >= P["foe_ratio"] * fr:
        return "foe"
    if fr >= P["friend_px_min"] and fr >= P["friend_ratio"] * red:
        return "friend"
    return "foe_unknown"


def tag_near_crop(tag: dict, P=DEFAULT_PARAMS) -> bool:
    x, y, w, h = tag["strip"]
    cx, cy = x + w / 2, y + h / 2
    pad = P["tag_crop_pad"]
    return 448 - pad <= cx <= 832 + pad and 168 - pad <= cy <= 552 + pad


def tag_on_person(tag: dict, region) -> bool:
    """True if the v0.2 tag strip box overlaps a person's head region."""
    x, y, w, h = tag["strip"]
    return not (x + w <= region[0] or region[2] <= x or y + h <= region[1] or region[3] <= y)


HUD_STATES = ("gameplay_hud", "no_minimap")


def _finite_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def v02_problems(v02) -> list[str]:
    """Schema check of every v0.2 field the decision reads (P174-M1). Empty list = well formed.

    Missing, None, wrong-type, non-finite or negative values are problems. They are never read as a clean zero:
    a malformed record can only give `unknown`."""
    if not isinstance(v02, dict):
        return ["v02_record_not_an_object"]
    probs = []
    if v02.get("hud_state") not in HUD_STATES:
        probs.append("hud_state_missing_or_invalid")
    md = v02.get("minimap_enemy_dots")
    c = md.get("count") if isinstance(md, dict) else None
    if not isinstance(md, dict):
        probs.append("minimap_enemy_dots_missing_or_not_object")
    elif not (isinstance(c, int) and not isinstance(c, bool)) or c < 0:
        probs.append("minimap_dot_count_missing_nonint_or_negative")
    it = v02.get("inworld_tag")
    if not isinstance(it, dict):
        probs.append("inworld_tag_missing_or_not_object")
    else:
        if not isinstance(it.get("gated_no_hud"), bool):
            probs.append("inworld_tag_gated_no_hud_missing_or_not_bool")
        tags = it.get("tags")
        if not isinstance(tags, list):
            probs.append("inworld_tag_tags_missing_or_not_list")
        elif not all(isinstance(t, dict) and isinstance(t.get("strip"), (list, tuple)) and len(t["strip"]) == 4
                     and all(_finite_number(v) for v in t["strip"]) for t in tags):
            probs.append("inworld_tag_strip_malformed")
    return probs


def decide(v02: dict, persons: list[dict], hsv: np.ndarray | None, P=DEFAULT_PARAMS) -> dict:
    """v02: one frame record of detect_enemy_v02 output. persons: merge_persons() output. hsv: full-frame HSV.

    A malformed v0.2 record (see v02_problems) returns `unknown` with the problems as reasons, never `yes`/`no`."""
    probs = v02_problems(v02)
    if probs:
        return {"decision": "unknown", "enemy_flag": False, "reasons": ["malformed_v02_record: " + ", ".join(probs)],
                "malformed": probs, "hud": None, "minimap_dots": None, "tags_near_crop": None, "persons": []}
    hud = v02["hud_state"] == "gameplay_hud"
    dots = v02["minimap_enemy_dots"]["count"]
    tags = [] if v02["inworld_tag"]["gated_no_hud"] else v02["inworld_tag"]["tags"]
    near_tags = [t for t in tags if tag_near_crop(t, P)]
    reasons, people = [], []
    hud_ok = not (P["person_cues_require_hud"] and not hud)
    for p in persons:
        region = head_region(p["xyxy"], P)
        c = colour_counts(hsv, region, P) if hsv is not None else {"red": 0, "friend": 0, "green": 0, "area": 0}
        ff = friend_or_foe(c, P) if hsv is not None else "foe_unknown"
        people.append({"xyxy": [round(v, 1) for v in p["xyxy"]], "score": p["score"], "srcs": p["srcs"],
                       "head_region": region, "colour_px": c, "ff": ff})
    person_gate = P["person_cues_require_hud"] and not hud  # death/killcam/menu views: person colours not counted
    foes = [] if person_gate else [q for q in people if q["ff"] == "foe"]
    friends = [q for q in people if q["ff"] == "friend"]
    unknowns = [q for q in people if q["ff"] == "foe_unknown"]
    dp = P["dot_plus_person"]
    if foes:
        reasons.append(f"person_with_red_head_region x{len(foes)}")
    if P["tag_requires_person"]:
        near_tags = [t for t in near_tags if hud_ok and any(tag_on_person(t, q["head_region"]) for q in people)]
    if near_tags:
        reasons.append(f"v02_inworld_tag_near_crop x{len(near_tags)}" + (" on a person head region" if P["tag_requires_person"] else ""))
    if dp["enabled"] and not person_gate and dots >= dp["dots_min"] and any(q["score"] >= dp["person_score_min"] for q in unknowns):
        reasons.append("strong_unknown_person_plus_minimap_dots")
    if reasons:
        decision = "yes"
    elif (P["no_rule_enabled"] and hud and people and len(friends) == len(people) and dots == 0 and not tags):
        decision = "no"; reasons.append("only_friend_coloured_persons_no_tag_no_map_dots")
    else:
        decision = "unknown"
        if not people:
            reasons.append("no_person_box")
        if unknowns:
            reasons.append(f"person_colour_unknown x{len(unknowns)}")
        if friends and not foes:
            reasons.append(f"friend_coloured_persons x{len(friends)}")
        if dots:
            reasons.append(f"minimap_dots_prior x{dots} (enemy on map, not on screen)")
        if not hud:
            reasons.append("no_gameplay_hud (person cues gated)" if person_gate else "no_gameplay_hud")
    return {"decision": decision, "enemy_flag": decision == "yes", "reasons": reasons, "malformed": [], "hud": hud,
            "minimap_dots": dots, "tags_near_crop": len(near_tags), "persons": people}
