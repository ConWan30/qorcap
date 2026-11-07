#!/usr/bin/env python3
"""Inspect owner-row digits and the TDM ASSISTS header. Text report only.

Invocation from the repo root:

    python3 sidecar/prepare_cod_digits.py

Reads the shipped pack sidecar/packs/cod_mw4_hud_v1.npz (read-only), the
committed fixture sidecar/fixtures/cod/end_173922_019.jpg, and the private
frame .qor-grok-internal/private-inputs/f_0846.jpg. Prints one JSON object
and ASCII masks. Does not write images, npz, or other files.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import qorcap_cod_hud as H

# Same cuts build_calibration_pack uses on end_173922_019. Not a new sample set.
FIXTURE_ROW_SPECS = (
    (530, 552, 300, 580, 145, ((331, "2"), (345, "4"), (352, "5"), (432, "1"), (439, "3"), (534, "7"))),
    (645, 670, 580, 1000, 120, ((628, "1"), (636, "7"), (768, "1"), (776, "3"))),
)

# Measured on the TDM board in the task brief. Labels are attached only when
# the component count in that column equals the length of the measured text.
MEASURED_COLUMNS = (
    ("score", "1250"),
    ("kills", "9"),
    ("assists", "2"),
    ("deaths", "21"),
)


def _repo() -> Path:
    return Path(__file__).resolve().parents[1]


def _private_frame() -> Path:
    return _repo() / ".qor-grok-internal" / "private-inputs" / "f_0846.jpg"


def _fixture_frame() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "cod" / "end_173922_019.jpg"


def _mask_rows(mask) -> list[str]:
    rows = []
    for y in range(int(mask.shape[0])):
        rows.append("".join("1" if int(mask[y, x]) else "0" for x in range(int(mask.shape[1]))))
    return rows


def _ascii(mask) -> list[str]:
    lines = []
    for y in range(int(mask.shape[0])):
        lines.append("".join("#" if int(mask[y, x]) else "." for x in range(int(mask.shape[1]))))
    return lines


def _peaks(search, templ, thr: float, min_dist: int = 8):
    """All template peaks at or above thr. Does not change reader gates."""
    cv2, np = H._libs()
    if search.shape[0] < templ.shape[0] or search.shape[1] < templ.shape[1]:
        return []
    res = cv2.matchTemplate(search, templ, cv2.TM_CCOEFF_NORMED)
    hits = []
    work = res.copy()
    h, w = int(templ.shape[0]), int(templ.shape[1])
    while True:
        score = float(work.max())
        if score < thr:
            break
        y, x = np.unravel_index(int(work.argmax()), work.shape)
        hits.append({"score": round(score, 4), "x": int(x), "y": int(y), "w": w, "h": h})
        y0, y1 = max(0, int(y) - min_dist), min(work.shape[0], int(y) + min_dist + 1)
        x0, x1 = max(0, int(x) - min_dist), min(work.shape[1], int(x) + min_dist + 1)
        work[y0:y1, x0:x1] = -1.0
        if len(hits) >= 12:
            break
    return hits


def _components(mask, *, min_area: int, min_h: int, min_w: int, max_w: int, max_h: int | None = None):
    cv2, _np = H._libs()
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    comps = []
    for i in range(1, n):
        x, y, ww, hh, area = (int(t) for t in st[i])
        if area < min_area or hh < min_h or ww < min_w or ww > max_w:
            continue
        if max_h is not None and hh > max_h:
            continue
        glyph = (lab[y:y + hh, x:x + ww] == i).astype("uint8") * 255
        comps.append({"x": x, "y": y, "w": ww, "h": hh, "area": area, "glyph": glyph})
    comps.sort(key=lambda c: (c["x"], c["y"]))
    return comps


def _group_words(comps, gap: int):
    words = []
    for comp in comps:
        if words and comp["x"] <= words[-1]["x1"] + gap and abs((comp["y"] + comp["h"] / 2) - words[-1]["cy"]) <= 6:
            word = words[-1]
            word["parts"].append(comp)
            word["x1"] = max(word["x1"], comp["x"] + comp["w"])
            word["y0"] = min(word["y0"], comp["y"])
            word["y1"] = max(word["y1"], comp["y"] + comp["h"])
            word["cy"] = (word["y0"] + word["y1"]) / 2
        else:
            words.append({
                "parts": [comp],
                "x0": comp["x"],
                "x1": comp["x"] + comp["w"],
                "y0": comp["y"],
                "y1": comp["y"] + comp["h"],
                "cy": comp["y"] + comp["h"] / 2,
            })
    return words


def _word_mask(shape, parts, origin_xy):
    _np = H._libs()[1]
    ox, oy = origin_xy
    x0 = min(p["x"] for p in parts)
    y0 = min(p["y"] for p in parts)
    x1 = max(p["x"] + p["w"] for p in parts)
    y1 = max(p["y"] + p["h"] for p in parts)
    canvas = _np.zeros((y1 - y0, x1 - x0), _np.uint8)
    for p in parts:
        g = p["glyph"]
        canvas[p["y"] - y0:p["y"] - y0 + g.shape[0], p["x"] - x0:p["x"] - x0 + g.shape[1]] = g
    return {"x": x0 + ox, "y": y0 + oy, "w": int(canvas.shape[1]), "h": int(canvas.shape[0]), "mask": canvas}


def _vec_list(glyph):
    np = H._libs()[1]
    vec = H._vec(np, glyph, (12, 18))
    if vec is None:
        return None
    return [round(float(v), 5) for v in vec]


def _best_label(vec, bank_vecs, labels):
    np = H._libs()[1]
    if vec is None or bank_vecs is None or len(labels) == 0:
        return None
    scores = bank_vecs @ np.asarray(vec, dtype=np.float32)
    by: dict[str, float] = {}
    for i, name in enumerate(labels):
        score = float(scores[i])
        if name not in by or score > by[name]:
            by[name] = score
    ranked = sorted(by.items(), key=lambda kv: -kv[1])
    best, top = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else None
    margin = None if second is None else top - second
    return {
        "best": best,
        "top": round(top, 4),
        "second": None if second is None else round(second, 4),
        "margin": None if margin is None else round(margin, 4),
        "clears_row_gate": bool(top >= H._ROW_DIGIT_MIN and margin is not None and margin >= H._ROW_DIGIT_MARGIN),
    }


def _column_digits(bgr, hit, row_y, row_h, scale, bank):
    """Same window and value cut as _digits_for_column. Reports boxes, no read."""
    cv2, np = H._libs()
    _score, x, _y, w, _h = hit
    x0 = int(x - int(4 * scale))
    x1 = int(x + w + int(8 * scale))
    y0 = int(row_y - int(2 * scale))
    y1 = int(row_y + row_h + int(6 * scale))
    h, wimg = bgr.shape[:2]
    x0, x1 = max(0, x0), min(wimg, x1)
    y0, y1 = max(0, y0), min(h, y1)
    hsv = cv2.cvtColor(bgr[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    mask = (hsv[:, :, 2] > 130).astype(np.uint8) * 255
    vecs, labels = bank
    glyphs = []
    for comp in _components(mask, min_area=8, min_h=max(5, int(round(6 * scale))), min_w=2, max_w=max(10, int(round(18 * scale)))):
        vec = _vec_list(comp["glyph"])
        glyphs.append({
            "x": comp["x"] + x0,
            "y": comp["y"] + y0,
            "w": comp["w"],
            "h": comp["h"],
            "area": comp["area"],
            "match": _best_label(vec, vecs, labels),
            "mask_rows": _mask_rows(comp["glyph"]),
            "vec": vec,
        })
    return {"window": [x0, y0, x1, y1], "thr": 130, "glyphs": glyphs}


def _fixture_bank(end, pack_labels_note):
    cv2, np = H._libs()
    end_v = cv2.cvtColor(end, cv2.COLOR_BGR2HSV)[:, :, 2]
    samples = []
    for y0, y1, x0, x1, thr, spec in FIXTURE_ROW_SPECS:
        mask = (end_v[y0:y1, x0:x1] > thr).astype(np.uint8) * 255
        comps = _components(mask, min_area=8, min_h=6, min_w=2, max_w=40)
        for comp in comps:
            name = None
            for kx, label in spec:
                if abs((comp["x"] + x0) - kx) <= 4:
                    name = label
                    break
            if name is None:
                continue
            samples.append({
                "label": name,
                "x": comp["x"] + x0,
                "y": comp["y"] + y0,
                "w": comp["w"],
                "h": comp["h"],
                "area": comp["area"],
                "mask_rows": _mask_rows(comp["glyph"]),
                "vec": _vec_list(comp["glyph"]),
                "source": "end_173922_019",
            })
    return {"note": pack_labels_note, "samples": samples}


def _pack_summary(pack):
    files = list(pack.files)
    labels = H._labels(pack, "row_digit_labels") if "row_digit_labels" in files else []
    return {
        "files": files,
        "has_assists_header_mask": "assists_header_mask" in files,
        "row_digit_labels": labels,
        "row_digit_label_set": sorted(set(labels)),
        "missing_digits_in_shipped_bank": [d for d in "0123456789" if d not in set(labels)],
        "row_digit_gate": {"min": H._ROW_DIGIT_MIN, "margin": H._ROW_DIGIT_MARGIN},
        "header_gate": H._ROW_HEADER_MIN,
        "consensus_frames": H.CONSENSUS_FRAMES,
    }


def _header_words(bgr, kills_hit):
    cv2, np = H._libs()
    _score, kx, ky, kw, kh = kills_hit
    y0 = max(0, ky - 4)
    y1 = min(bgr.shape[0], ky + kh + 6)
    x0 = max(0, kx - 220)
    x1 = min(bgr.shape[1], kx + kw + 220)
    band = bgr[y0:y1, x0:x1]
    v = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)[:, :, 2]
    mask = (v > 80).astype(np.uint8) * 255
    comps = _components(mask, min_area=8, min_h=5, min_w=2, max_w=80, max_h=kh + 8)
    words = []
    for word in _group_words(comps, gap=4):
        packed = _word_mask(mask.shape, word["parts"], (x0, y0))
        words.append({
            "x": packed["x"],
            "y": packed["y"],
            "w": packed["w"],
            "h": packed["h"],
            "parts": len(word["parts"]),
            "mask_rows": _mask_rows(packed["mask"]),
        })
    return words


def _pick_word(words, x_lo: int, x_hi: int):
    inside = [w for w in words if w["x"] >= x_lo and w["x"] < x_hi]
    inside.sort(key=lambda w: w["x"])
    return inside


def build_report() -> dict:
    cv2, np = H._libs()
    if cv2 is None:
        return {"ok": False, "error": "opencv or numpy missing"}
    pack = H._pack()
    if pack is None:
        return {"ok": False, "error": "shipped pack missing"}
    private = _private_frame()
    fixture = _fixture_frame()
    frame = cv2.imread(str(private))
    end = cv2.imread(str(fixture))
    summary = _pack_summary(pack)
    if frame is None or end is None:
        return {
            "ok": False,
            "error": "frame missing",
            "private_exists": private.is_file(),
            "fixture_exists": fixture.is_file(),
            "pack": summary,
        }
    scale = H._board_scale(frame)
    owner_search = H._value_mask(frame, H._OWNER_SEARCH_VALUE)
    owner = H._best_mask(owner_search, pack["owner_mask"], scale, H._OWNER_MIN) if scale else None
    header_search = H._value_mask(frame, H._HEADER_SEARCH_VALUE)
    kills_all = _peaks(header_search, pack["kills_header_mask"], H._ROW_HEADER_MIN)
    deaths_all = _peaks(header_search, pack["deaths_header_mask"], H._ROW_HEADER_MIN) if "deaths_header_mask" in pack.files else []
    confirms_all = _peaks(header_search, pack["confirms_header_mask"], H._ROW_HEADER_MIN) if "confirms_header_mask" in pack.files else []
    report = {
        "ok": True,
        "private_frame": "f_0846.jpg",
        "shape": [int(frame.shape[0]), int(frame.shape[1])],
        "scale": scale,
        "pack": summary,
        "fixture_row_digits": _fixture_bank(end, "cuts mirrored from build_calibration_pack"),
        "owner_hit": None if owner is None else {
            "score": round(float(owner[0]), 4), "x": owner[1], "y": owner[2], "w": owner[3], "h": owner[4],
        },
        "kills_template_hits": kills_all,
        "deaths_template_hits": deaths_all,
        "confirms_template_hits": confirms_all,
        "unmeasured": [],
    }
    if owner is None or scale is None:
        report["ok"] = False
        report["error"] = "owner row template did not clear the existing gate"
        return report
    _os, row_x, row_y, _ow, row_h = owner
    kills_for_row = None
    for hit in kills_all:
        boxed = (hit["score"], hit["x"], hit["y"], hit["w"], hit["h"])
        if H._column_header(boxed, row_x, row_y, scale) is not None:
            kills_for_row = hit
            break
    report["kills_header_for_owner_row"] = kills_for_row
    if kills_for_row is None:
        report["ok"] = False
        report["error"] = "kills header was not above the owner row"
        return report
    words = _header_words(frame, (kills_for_row["score"], kills_for_row["x"], kills_for_row["y"], kills_for_row["w"], kills_for_row["h"]))
    report["header_words_near_kills"] = [
        {k: v for k, v in w.items() if k != "mask_rows"} | {"mask_h": len(w["mask_rows"]), "mask_w": len(w["mask_rows"][0]) if w["mask_rows"] else 0}
        for w in words
    ]
    kx, kw = kills_for_row["x"], kills_for_row["w"]
    # Words strictly right of KILLS, in order: measured TDM order is ASSISTS then DEATHS.
    right = _pick_word(words, kx + kw - 2, kx + kw + 180)
    left = _pick_word(words, kx - 180, kx - 2)
    named = {}
    if left:
        named["score"] = left[-1]
    if len(right) >= 1:
        named["assists"] = right[0]
    if len(right) >= 2:
        named["deaths"] = right[1]
    report["column_header_candidates"] = {
        name: {k: v for k, v in word.items() if k != "mask_rows"}
        for name, word in named.items()
    }
    # Full masks for the two headers the shipped pack does not tie to this board.
    report["derived_header_masks"] = {}
    for name in ("assists", "deaths"):
        word = named.get(name)
        if word is None:
            report["unmeasured"].append(f"{name} header was not separated from the kills header band")
            continue
        report["derived_header_masks"][name] = {
            "x": word["x"], "y": word["y"], "w": word["w"], "h": word["h"],
            "mask_rows": word["mask_rows"],
            "note": "derived text mask from f_0846 header band at value>80; not a committed image",
        }
    bank = None
    if "row_digit_vecs" in pack.files and "row_digit_labels" in pack.files:
        bank = (pack["row_digit_vecs"].astype(np.float32), H._labels(pack, "row_digit_labels"))
    columns = {}
    kills_box = (kills_for_row["score"], kills_for_row["x"], kills_for_row["y"], kills_for_row["w"], kills_for_row["h"])
    header_hits = {"kills": kills_box}
    for name in ("score", "assists", "deaths"):
        word = named.get(name)
        if word is None:
            continue
        header_hits[name] = (1.0, word["x"], word["y"], word["w"], word["h"])
    for name, _text in MEASURED_COLUMNS:
        hit = header_hits.get(name)
        if hit is None or bank is None:
            columns[name] = None
            continue
        columns[name] = _column_digits(frame, hit, row_y, row_h, scale, bank)
        got = columns[name]["glyphs"]
        if len(got) == len(_text):
            for glyph, ch in zip(got, _text):
                glyph["measured_label"] = ch
        else:
            columns[name]["measured_label_skipped"] = True
            report["unmeasured"].append(
                f"{name} column glyph count {len(got)} did not match measured length {len(_text)}; labels not attached"
            )
    report["owner_row_columns"] = columns
    present = set(summary["row_digit_label_set"])
    for col in columns.values():
        if not col:
            continue
        for glyph in col["glyphs"]:
            label = glyph.get("measured_label")
            if label:
                present.add(label)
    report["digits_still_without_a_real_owner_sample"] = [d for d in "0123456789" if d not in present]
    if report["digits_still_without_a_real_owner_sample"]:
        report["unmeasured"].append(
            "digits without a real owner-row sample on the shipped bank or f_0846 measured columns: "
            + "".join(report["digits_still_without_a_real_owner_sample"])
        )
    report["cross_header_note"] = (
        "deaths_template_hits are the shipped personal-card DEATHS mask. "
        "derived_header_masks.deaths is the column word on this TDM board. They are separate."
    )
    return report


def _print_ascii(report) -> None:
    print("--- ASCII masks ---")
    headers = report.get("derived_header_masks") or {}
    for name, word in headers.items():
        print(f"header {name} @ {word['x']},{word['y']} {word['w']}x{word['h']}")
        for line in _ascii_from_rows(word["mask_rows"]):
            print(line)
    columns = report.get("owner_row_columns") or {}
    for name, col in columns.items():
        if not col:
            continue
        print(f"column {name} window {col['window']} glyphs {len(col['glyphs'])}")
        for glyph in col["glyphs"]:
            label = glyph.get("measured_label", "?")
            match = glyph.get("match")
            print(f"  glyph {label} box {glyph['x']},{glyph['y']} {glyph['w']}x{glyph['h']} match {match}")
            for line in _ascii_from_rows(glyph["mask_rows"]):
                print("  " + line)
    samples = (report.get("fixture_row_digits") or {}).get("samples") or []
    print(f"fixture samples {len(samples)}")
    for sample in samples:
        print(f"fixture {sample['label']} @ {sample['x']},{sample['y']} {sample['w']}x{sample['h']}")
        for line in _ascii_from_rows(sample["mask_rows"]):
            print("  " + line)


def _ascii_from_rows(rows: list[str]) -> list[str]:
    return [row.replace("1", "#").replace("0", ".") for row in rows]


def main() -> int:
    report = build_report()
    # Vecs stay in the JSON. ASCII is a second view of the same masks.
    print(json.dumps(report, indent=2, sort_keys=True))
    if report.get("ok"):
        _print_ascii(report)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
